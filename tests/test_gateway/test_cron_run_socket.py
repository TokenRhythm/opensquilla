"""Manual cron admission and completion over the production WebSocket dispatcher."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from websockets.asyncio.client import connect

from opensquilla.gateway import websocket
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.transport_flow import get_transport_budget
from opensquilla.scheduler.engine import SchedulerEngine
from opensquilla.scheduler.payloads import make_agent_turn_payload
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import ScheduleKind
from tests.test_gateway.test_connection_stability_socket import _receive_until


async def _send(client, request_id, method, params=None):
    await client.send(json.dumps({
        "type": "req", "id": request_id, "method": method, "params": params or {},
    }))


async def _response(client, frames, request_id):
    for frame in frames:
        if frame.get("type") == "res" and frame.get("id") == request_id:
            return frame
    return await _receive_until(client, frames, type="res", id=request_id)


async def _call(client, frames, request_id, method, params=None):
    await _send(client, request_id, method, params)
    async with asyncio.timeout(2):
        return await _response(client, frames, request_id)


@asynccontextmanager
async def _connected(gateway, *, scopes=None):
    async with connect(gateway.uri, max_size=16 * 1024 * 1024) as client:
        frames = []
        await _receive_until(client, frames, event="connect.challenge")
        await _send(
            client,
            "connect",
            "connect",
            {
                "minProtocol": 3,
                "maxProtocol": 3,
                "caps": ["transport.probe.v1", "transport.flow.v1"],
                "role": "operator",
                "scopes": scopes if scopes is not None else ["operator.admin"],
                "auth": {"token": gateway.config.auth.token}
                if gateway.config.auth.mode == "token"
                else {},
            },
        )
        hello = await _receive_until(client, frames, type="hello-ok")
        yield client, frames, hello


async def _until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.fixture
async def cron_socket(tmp_path):
    budget_before = get_transport_budget().used
    workers_before = set(websocket._ORDINARY_WORKERS)
    store = JobStore(str(tmp_path / "cron.db"))
    await store.open()
    engine = SchedulerEngine(store)
    started, release, observed = {}, {}, []

    async def handler(job):
        observed.append((job.id, job.name))
        started[job.id].set()
        await release[job.id].wait()
        return f"done {job.name}"

    engine.register_handler("agent_run", handler)

    async def add_job(name="original"):
        job = await engine.add_job(name=name, schedule_kind=ScheduleKind.CRON,
            schedule_value="0 9 * * *", payload=make_agent_turn_payload("synthetic"),
            jitter_seconds=0)
        started[job.id], release[job.id] = asyncio.Event(), asyncio.Event()
        return job

    config = GatewayConfig(auth=AuthConfig(mode="none"), state_dir=str(tmp_path / "state"),
        config_path=str(tmp_path / "config.toml"), client_ws_keepalive_timeout_s=0)
    subscriptions = websocket.SubscriptionManager()

    async def endpoint(ws):
        await websocket.handle_ws_connection(ws, config, dispatcher=get_dispatcher(),
            cron_scheduler=engine, subscription_manager=subscriptions)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        Starlette(routes=[WebSocketRoute("/ws", endpoint)]), host="127.0.0.1", port=port,
        ws="websockets", lifespan="off", timeout_graceful_shutdown=2,
        log_level="error", access_log=False,
    ))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await _until(lambda: server.started or task.done())
        assert server.started
        yield SimpleNamespace(
            uri=f"ws://127.0.0.1:{port}/ws",
            config=config,
            engine=engine,
            store=store,
            started=started,
            release=release,
            observed=observed,
            add_job=add_job,
        )
    finally:
        for event in release.values():
            event.set()
        await engine.stop()
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
        await store.close()
        await _until(lambda: websocket._ORDINARY_WORKERS == workers_before)
        assert get_transport_budget().used == budget_before


async def test_running_cron_allows_queries_controls_and_another_job(cron_socket):
    gateway = cron_socket
    first, second = await gateway.add_job("first"), await gateway.add_job("second")
    async with _connected(gateway) as (client, frames, hello):
        await _send(client, "first", "cron.run", {"id": first.id})
        await asyncio.wait_for(gateway.started[first.id].wait(), 2)
        for index, (method, params) in enumerate([
            ("cron.list", {}), ("cron.status", {"id": first.id}),
            ("cron.runs", {"id": first.id}), ("config.get", {}),
        ]):
            assert (await _call(client, frames, f"read-{index}", method, params))["ok"]
        await _send(client, "second", "cron.run", {"id": second.id})
        await asyncio.wait_for(gateway.started[second.id].wait(), 2)
        duplicate = await _call(client, frames, "duplicate", "cron.run", {"id": first.id})
        assert duplicate["ok"] and duplicate["payload"]["status"] == "busy"
        assert gateway.observed == [(first.id, "first"), (second.id, "second")]
        ack = await _call(client, frames, "ack", "transport.flow.update", {
            "delivery_epoch": hello["policy"]["transport_flow"]["delivery_epoch"],
            "ack_delivery_id": 0,
        })
        assert ack["ok"]
        await client.send(json.dumps({"type": "ping", "nonce": "during-cron"}))
        await _receive_until(client, frames, type="pong", nonce="during-cron")
        assert not any(f.get("id") in {"first", "second"} for f in frames)
        for request_id, job in [("first", first), ("second", second)]:
            gateway.release[job.id].set()
            result = await _response(client, frames, request_id)
            assert result["ok"] and result["payload"]["success"]
            assert result["payload"]["status"] == "accepted"
            assert len(await gateway.engine.get_runs(job.id)) == 1


async def test_update_commits_before_run_admission_and_queued_successor(cron_socket, monkeypatch):
    gateway = cron_socket
    job = await gateway.add_job()
    entered, release_update = asyncio.Event(), asyncio.Event()
    update = gateway.engine.update_job

    async def delayed_update(*args, **kwargs):
        entered.set()
        await release_update.wait()
        return await update(*args, **kwargs)

    monkeypatch.setattr(gateway.engine, "update_job", delayed_update)
    try:
        async with _connected(gateway) as (client, frames, _):
            await _send(client, "update", "cron.update", {"id": job.id, "name": "updated"})
            await asyncio.wait_for(entered.wait(), 2)
            await _send(client, "run", "cron.run", {"id": job.id})
            await _send(client, "subscribe", "cron.subscribe")
            assert not gateway.started[job.id].is_set()
            release_update.set()
            assert (await _response(client, frames, "update"))["ok"]
            await asyncio.wait_for(gateway.started[job.id].wait(), 2)
            async with asyncio.timeout(2):
                assert (await _response(client, frames, "subscribe"))["ok"]
            assert gateway.observed == [(job.id, "updated")]
            assert not any(f.get("id") == "run" for f in frames)
            gateway.release[job.id].set()
            assert (await _response(client, frames, "run"))["payload"]["reply"] == "done updated"
    finally:
        release_update.set()


@pytest.mark.parametrize("phase", ["before_admission", "after_admission"])
async def test_disconnect_keeps_admitted_run_until_persistence(cron_socket, monkeypatch, phase):
    gateway = cron_socket
    job = await gateway.add_job()
    reserve_entered, reserve_release = asyncio.Event(), asyncio.Event()
    reserve = gateway.store.reserve_manual_job

    async def delayed_reserve(*args, **kwargs):
        reserve_entered.set()
        await reserve_release.wait()
        return await reserve(*args, **kwargs)

    if phase == "before_admission":
        monkeypatch.setattr(gateway.store, "reserve_manual_job", delayed_reserve)
    try:
        async with _connected(gateway) as (client, _, hello):
            await _send(client, "run", "cron.run", {"id": job.id})
            event = reserve_entered if phase == "before_admission" else gateway.started[job.id]
            await asyncio.wait_for(event.wait(), 2)
        await _until(lambda: websocket.get_registry().get(hello["server"]["conn_id"]) is None)
        assert gateway.engine._manual_run_tasks
        assert websocket._ORDINARY_WORKERS
        reserve_release.set()
        await asyncio.wait_for(gateway.started[job.id].wait(), 2)
        gateway.release[job.id].set()
        await _until(lambda: not gateway.engine._manual_run_tasks)
        runs = await gateway.engine.get_runs(job.id)
        assert len(runs) == 1 and runs[0].success
        assert not (await gateway.store.get(job.id)).reservation_token
    finally:
        reserve_release.set()


async def test_global_worker_saturation_keeps_cron_reads_out_of_full_fifo(cron_socket, monkeypatch):
    gateway = cron_socket
    job = await gateway.add_job()
    monkeypatch.setattr(websocket, "_MAX_ORDINARY_WORKERS", 1)
    reserve_entered, reserve_release = asyncio.Event(), asyncio.Event()
    reserve = gateway.store.reserve_manual_job

    async def delayed_reserve(*args, **kwargs):
        reserve_entered.set()
        await reserve_release.wait()
        return await reserve(*args, **kwargs)

    monkeypatch.setattr(gateway.store, "reserve_manual_job", delayed_reserve)
    try:
        async with _connected(gateway) as (client, frames, hello):
            await _send(client, "run", "cron.run", {"id": job.id})
            await asyncio.wait_for(reserve_entered.wait(), 2)
            for index in range(8):
                await _send(client, f"queued-{index}", "cron.subscribe")
            conn = websocket.get_registry().get(hello["server"]["conn_id"])
            await _until(lambda: conn._ordinary_queue.qsize() == 8)
            reserve_release.set()
            await asyncio.wait_for(gateway.started[job.id].wait(), 2)
            assert len(websocket._ORDINARY_WORKERS) == 1
            for index, method in enumerate(["cron.list", "cron.status", "cron.runs"]):
                result = await _call(client, frames, f"read-{index}", method,
                    {} if method == "cron.list" else {"id": job.id})
                assert result["ok"], result
            rejected = await _call(client, frames, "rejected", "cron.run", {"id": job.id})
            assert not rejected["ok"] and rejected["error"]["code"] == "UNAVAILABLE"
            assert rejected["error"]["accepted"] is False
            assert len(gateway.observed) == 1
            gateway.release[job.id].set()
            assert (await _response(client, frames, "run"))["payload"]["success"]
            assert (await _response(client, frames, "queued-7"))["ok"]
    finally:
        reserve_release.set()


@pytest.mark.parametrize("denial", ["unauthorized", "invalid", "missing", "disabled", "no_handler"])
async def test_cron_rejection_does_not_admit_execution(cron_socket, denial):
    gateway = cron_socket
    job = await gateway.add_job()
    if denial == "disabled":
        await gateway.engine.pause_job(job.id)
    if denial == "no_handler":
        gateway.engine._timer._handlers.clear()
    if denial == "unauthorized":
        gateway.config.auth = AuthConfig(
            mode="token", token="cron-read-only-test", token_scopes=["operator.read"]
        )
    scopes = ["operator.read"] if denial == "unauthorized" else None
    params = (
        {"id": 7} if denial == "invalid" else {"id": "missing" if denial == "missing" else job.id}
    )
    async with _connected(gateway, scopes=scopes) as (client, frames, _):
        result = await _call(client, frames, "run", "cron.run", params)
        if denial in {"unauthorized", "invalid"}:
            assert not result["ok"]
        else:
            assert result["ok"] and not result["payload"]["success"]
        assert not gateway.observed
        assert not (await gateway.store.get(job.id)).reservation_token
        assert (await _call(client, frames, "list", "cron.list"))["ok"]
