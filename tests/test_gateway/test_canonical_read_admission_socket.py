"""Canonical history and metadata share physical admission across WS clients."""
from __future__ import annotations

import asyncio
import json
import socket
import threading
from contextlib import AsyncExitStack
from types import SimpleNamespace
from uuid import uuid4

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from websockets.asyncio.client import connect

from opensquilla.cli.gateway_client import GatewayClient, session_history_all
from opensquilla.gateway import session_streams, websocket
from opensquilla.gateway.adapters.session_history_projection import read_chat_history_v4
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.protocol import MAX_PAYLOAD_BYTES
from opensquilla.gateway.recovery_scheduler import get_recovery_scheduler
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.gateway.transport_flow import get_transport_budget
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus
from opensquilla.session.storage import SessionStorage

HISTORY = "sessions.history.page.v2"
HYDRATE = "sessions.messages.hydrate"
KEY = "agent:main:webchat:admission"
OTHER = "agent:main:webchat:independent"
CAPS = ["transport.flow.v1", "transport.recovery.v1", "transport.probe.v1"]


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


class RpcSocket:
    def __init__(self, socket, hello):
        self.socket = socket
        self.hello = hello
        self.pending = {}
        self.pongs = asyncio.Queue()
        self.listener = asyncio.create_task(self.listen())

    async def listen(self):
        async for raw in self.socket:
            frame = json.loads(raw)
            future = self.pending.get(frame.get("id"))
            if frame.get("type") == "res" and future is not None and not future.done():
                future.set_result(frame)
            elif frame.get("type") == "pong":
                self.pongs.put_nowait(frame)

    async def send(self, method, params):
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.socket.send(json.dumps({"type": "req", "id": request_id,
            "method": method, "params": params}))
        return request_id, future

    async def call(self, method, params):
        _, future = await self.send(method, params)
        response = await asyncio.wait_for(future, 3)
        assert response["ok"], response.get("error")
        return response["payload"]

    async def read(self, method, key=KEY):
        params = {"key": key}
        if method == HISTORY:
            params.update(direction="before", target_items=10, target_bytes=1048576)
        return await self.send(method, params)

    async def ping(self):
        await self.socket.send(json.dumps({"type": "ping"}))
        await asyncio.wait_for(self.pongs.get(), 1)


@pytest.fixture
async def read_socket_case(tmp_path, monkeypatch):
    registry = websocket.ConnectionRegistry()
    monkeypatch.setattr(websocket, "_registry", registry)
    monkeypatch.setattr(session_streams, "_session_streams",
        session_streams.SessionStreamRegistry())
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path))
    initial_bytes = get_transport_budget().used
    storage = await SessionStorage.open(str(tmp_path / "admission.sqlite"))
    manager = SessionManager(storage, inject_time_prefix=False)
    for key in (KEY, OTHER):
        await manager.create(key)
        await manager.append_message(key, "user", "synthetic retained user")
        await storage.create_agent_task(AgentTaskRecord(
            session_key=key, status=AgentTaskStatus.SUCCEEDED))
    runtime = TaskRuntime(storage=storage, turn_handler=lambda _: None)
    subscriptions = websocket.SubscriptionManager()
    config = GatewayConfig(auth=AuthConfig(mode="none", token=None),
        state_dir=str(tmp_path), config_path=str(tmp_path / "config.toml"),
        workspace_dir=str(tmp_path), ws_writer_queue_enabled=True, client_ws_keepalive_timeout_s=0)
    entered, release = threading.Event(), threading.Event()
    gate = SimpleNamespace(operation=None, used=False, attempts=[])
    original_read = storage._run_recovery_read

    async def gated_read(operation, callback):
        gate.attempts.append(operation)
        async def execute(reader):
            if operation == gate.operation and not gate.used:
                gate.used = True

                def native_gate():
                    entered.set()
                    if not release.wait(10):
                        raise RuntimeError("Synthetic SQLite gate not released")
                    return 1

                await reader.create_function("admission_gate", 0, native_gate)
                async with reader.execute("SELECT admission_gate()") as cursor:
                    await cursor.fetchone()
            return await callback(reader)
        return await original_read(operation, execute)

    monkeypatch.setattr(storage, "_run_recovery_read", gated_read)

    async def endpoint(ws):
        await websocket.handle_ws_connection(ws, config, get_dispatcher(), session_manager=manager,
            subscription_manager=subscriptions, task_runtime=runtime)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.setblocking(False)
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[WebSocketRoute("/ws", endpoint)]),
        lifespan="off", log_level="critical", access_log=False, timeout_graceful_shutdown=2))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    clients = []
    async with AsyncExitStack() as stack:
        async def client(modern=True):
            connection = await stack.enter_async_context(connect(
                f"ws://127.0.0.1:{listener.getsockname()[1]}/ws", max_size=MAX_PAYLOAD_BYTES))
            challenge = json.loads(await connection.recv())
            assert challenge["event"] == "connect.challenge"
            await connection.send(json.dumps({"type": "req", "id": "connect", "method": "connect",
                "params": {"minProtocol": 4, "maxProtocol": 4, "caps": CAPS if modern else []}}))
            hello = json.loads(await connection.recv())
            assert hello["type"] == "hello-ok"
            result = RpcSocket(connection, hello)
            clients.append(result)
            if modern:
                await result.call("sessions.messages.subscribe", {"key": KEY, "fast_ack": True})
            return result

        try:
            await until(lambda: server.started)
            yield SimpleNamespace(storage=storage, manager=manager, registry=registry,
                gate=gate, entered=entered, release=release, client=client,
                url=f"ws://127.0.0.1:{listener.getsockname()[1]}/ws")
        finally:
            release.set()
            await stack.aclose()
            for item in clients:
                await item.listener
            server.should_exit = True
            await asyncio.wait_for(serving, 5)
            listener.close()
            await storage.close()
    assert get_transport_budget().used == initial_bytes


@pytest.mark.parametrize(("first_method", "first_modern", "second_modern", "same_socket"), [
    (HISTORY, True, True, True), (HYDRATE, True, True, True),
    (HISTORY, True, True, False), (HYDRATE, True, True, False),
    (HISTORY, False, True, False), (HYDRATE, True, False, False),
    (HISTORY, False, False, False),
])
async def test_canonical_and_metadata_reads_queue_same_key_across_client_capabilities(
    read_socket_case, first_method, first_modern, second_modern, same_socket,
):
    case = read_socket_case
    first = await case.client(first_modern)
    second = first if same_socket else await case.client(second_modern)
    case.gate.operation = "history.view" if first_method == HISTORY else "list_recent_agent_tasks"
    _, first_result = await first.read(first_method)
    assert await asyncio.to_thread(case.entered.wait, 2)
    second_method = HYDRATE if first_method == HISTORY and second_modern else HISTORY
    request_id, second_result = await second.read(second_method)
    await second.ping()
    scheduler = get_recovery_scheduler()
    await until(lambda: second_result.done() or any(
        op.request_id == request_id for op in scheduler._jobs))
    queued = next((op for op in scheduler._jobs if op.request_id == request_id), None)
    assert queued is not None and not queued.started, "Same-key reads must queue before the pool"
    assert not second_result.done()
    case.release.set()
    responses = await asyncio.wait_for(asyncio.gather(first_result, second_result), 3)
    assert all(response["ok"] for response in responses), [row.get("error") for row in responses]
    for method, response in zip((first_method, second_method), responses, strict=True):
        if method == HYDRATE:
            assert len(response["payload"]["tasks"]) == 1
        else:
            assert response["payload"]["canonical_available"]
            assert len(response["payload"]["items"]) == 1


async def test_canonical_read_does_not_block_control_or_another_session(read_socket_case):
    case = read_socket_case
    client = await case.client()
    lease = await client.call("sessions.read.open.v2", {"key": KEY})
    case.gate.operation = "history.view"
    _, history = await client.read(HISTORY)
    assert await asyncio.to_thread(case.entered.wait, 2)
    connection = case.registry.get(client.hello["server"]["conn_id"])
    await asyncio.wait_for(client.call("transport.flow.update", {
        "delivery_epoch": connection._flow.epoch, "ack_delivery_id": 0,
    }), 1)
    closed = await asyncio.wait_for(client.call("sessions.read.close.v2", {
        "key": KEY, "lease_id": lease["lease_id"],
    }), 1)
    assert closed["closed"]
    _, other = await client.read(HISTORY, OTHER)
    assert (await asyncio.wait_for(other, 1))["ok"]
    assert not history.done() and not case.release.is_set()
    case.release.set()
    assert (await asyncio.wait_for(history, 3))["ok"]


async def test_compaction_dispatch_tail_does_not_block_committed_reads(read_socket_case):
    case = read_socket_case
    client = await case.client(False)
    # The same pending dispatch tail exists while a wait:true manual RPC awaits
    # its model; no compaction/provider execution is needed to test admission.
    _, completion = websocket._admit_session_mutation("sessions.contextCompact", {"key": KEY},
        RpcContext(conn_id="manual-wait-true", session_manager=case.manager))
    try:
        _, history = await client.read(HISTORY)
        assert (await asyncio.wait_for(history, 1))["ok"]
        assert not completion.done()
        assert get_recovery_scheduler().mutation_tail(case.storage, KEY) is not None
    finally:
        completion.set_result(None)


async def test_cancelled_legacy_history_keeps_key_until_native_cleanup(read_socket_case):
    case = read_socket_case
    first, second = await case.client(False), await case.client(False)
    assert HISTORY in first.hello["policy"]["cancellable_request_methods"]
    case.gate.operation = "history.view"
    request_id, history = await first.read(HISTORY)
    assert await asyncio.to_thread(case.entered.wait, 2)
    next_id, successor = await second.read(HISTORY)
    await second.ping()
    scheduler = get_recovery_scheduler()
    await until(lambda: any(op.request_id == next_id for op in scheduler._jobs))
    await first.socket.send(json.dumps({"type": "cancel", "id": request_id}))
    await first.ping()
    cancelled = await asyncio.wait_for(history, 1)
    assert not cancelled["ok"] and cancelled["error"]["code"] == "SNAPSHOT_STALE"
    await until(lambda: case.storage._recovery_read_pool.retiring_count == 1)
    assert case.storage._recovery_read_pool.active_count == 1
    assert not successor.done()
    assert not next(op for op in scheduler._jobs if op.request_id == next_id).started
    _, independent = await second.read(HISTORY, OTHER)
    assert (await asyncio.wait_for(independent, 1))["ok"]
    assert not case.release.is_set()
    case.release.set()
    assert (await asyncio.wait_for(successor, 3))["ok"]
    await until(lambda: case.storage._recovery_read_pool.active_count == 0)


async def test_queued_canonical_read_deadline_includes_admission_wait(
    read_socket_case, monkeypatch,
):
    case = read_socket_case
    first, second = await case.client(False), await case.client()
    case.gate.operation = "history.view"
    _, history = await first.read(HISTORY)
    assert await asyncio.to_thread(case.entered.wait, 2)
    # Only the later request receives this deadline; the existing physical
    # owner keeps its original seven-second native cleanup budget.
    monkeypatch.setattr(websocket, "READ_BUDGET_SECONDS", 0.08)
    _, queued = await second.read(HISTORY)
    response = await asyncio.wait_for(queued, 1)
    assert not response["ok"] and response["error"]["code"] == "STORAGE_BUSY"
    assert response["error"]["retryable"]
    assert case.gate.attempts.count("history.view") == 1
    assert not case.release.is_set()
    case.release.set()
    assert (await asyncio.wait_for(history, 3))["ok"]


async def test_reset_from_another_socket_retires_old_history_owner(read_socket_case):
    case = read_socket_case
    first, second = await case.client(False), await case.client()
    case.gate.operation = "history.view"
    _, old_history = await first.read(HISTORY)
    assert await asyncio.to_thread(case.entered.wait, 2)
    reset = await asyncio.wait_for(second.call("sessions.reset", {"key": KEY}), 1)
    assert reset["key"] == KEY
    stale = await asyncio.wait_for(old_history, 1)
    assert not stale["ok"] and stale["error"]["code"] == "SNAPSHOT_STALE"
    next_id, fresh_history = await second.read(HISTORY)
    scheduler = get_recovery_scheduler()
    await second.ping()
    await until(lambda: any(op.request_id == next_id for op in scheduler._jobs))
    assert not next(op for op in scheduler._jobs if op.request_id == next_id).started
    assert not case.release.is_set()
    case.release.set()
    response = await asyncio.wait_for(fresh_history, 3)
    assert response["ok"], response.get("error")
    assert response["payload"]["session_epoch"] == 1
    assert response["payload"]["items"] == []


async def test_compaction_cannot_hide_an_earlier_reset_read_barrier(read_socket_case):
    case = read_socket_case
    client = await case.client(False)
    ctx = RpcContext(conn_id="reset-then-compact", session_manager=case.manager)
    _, reset = websocket._admit_session_mutation("sessions.reset", {"key": KEY}, ctx)
    _, compact = websocket._admit_session_mutation("sessions.contextCompact", {"key": KEY}, ctx)
    try:
        request_id, history = await client.read(HISTORY)
        await client.ping()
        scheduler = get_recovery_scheduler()
        await until(lambda: any(op.request_id == request_id for op in scheduler._jobs))
        assert not next(op for op in scheduler._jobs if op.request_id == request_id).started
        assert not history.done()
        reset.set_result(None)
        assert (await asyncio.wait_for(history, 1))["ok"]
        assert not compact.done()
    finally:
        if not reset.done():
            reset.set_result(None)
        compact.set_result(None)


@pytest.mark.parametrize("surface", ["cli_export", "cli_bootstrap", "direct_history"])
async def test_ordinary_history_does_not_compete_with_admitted_metadata(
    read_socket_case, surface,
):
    case = read_socket_case
    modern = await case.client()
    # The still-held metadata gate proves independence. Allow SQLite worker
    # scheduling within a watchdog shorter than the gate's ten-second bound.
    read_timeout_s = 5
    cli = GatewayClient(request_timeout_s=read_timeout_s)
    await cli.connect(case.url)
    case.gate.operation = "list_recent_agent_tasks"
    _, metadata = await modern.read(HYDRATE)
    assert await asyncio.to_thread(case.entered.wait, 2)
    try:
        if surface == "cli_export":
            history = await session_history_all(cli.session_history, KEY)
        elif surface == "cli_bootstrap":
            bootstrap = await cli.bootstrap_session(KEY)
            history = bootstrap["history"]
        else:
            # The HTTP history endpoint dispatches this projection without a
            # recovery budget, as do other ordinary in-process callers.
            history = await asyncio.wait_for(read_chat_history_v4(
                {"sessionKey": KEY}, RpcContext(conn_id="_http", session_manager=case.manager),
            ), read_timeout_s)
        assert len(history["messages"]) == 1
        assert history["canonical_available"]
        assert not metadata.done() and not case.release.is_set()
        assert "history.view" not in case.gate.attempts
    finally:
        case.release.set()
        await cli.close()
    assert (await asyncio.wait_for(metadata, 3))["ok"]
