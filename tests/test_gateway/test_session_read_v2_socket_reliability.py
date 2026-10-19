"""Read-v2 scheduling evidence over real sockets, handlers and temporary SQLite.

The ``detached`` parameter is an intentionally test-only candidate. It records
why merely extending the general detached allowlist is not an acceptable fix.
Gates delay real operations; they never replace persistence or session mutation.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import asynccontextmanager

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from websockets.asyncio.client import connect

from opensquilla.gateway import rpc_sessions, rpc_transport, session_streams, websocket
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.protocol import MAX_PAYLOAD_BYTES
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.transport_flow import SESSION_FLOW_V2_CAPABILITY
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

assert rpc_sessions and rpc_transport

KEY = "agent:main:webchat:read-v2-reliability"
READ_METHODS = frozenset({
    "sessions.read.open.v2", "sessions.read.state.v2", "sessions.read.install.v2",
    "sessions.read.close.v2", "sessions.history.page.v2",
})


class Client:
    def __init__(self, socket):
        self.socket = socket
        self.frames = []
        self.requests = []
        self.changed = asyncio.Event()
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        async for frame in self.socket:
            self.frames.append(json.loads(frame))
            self.changed.set()

    async def receive(self, **match):
        async with asyncio.timeout(5):
            while True:
                for frame in self.frames:
                    if all(frame.get(key) == value for key, value in match.items()):
                        return frame
                self.changed.clear()
                await self.changed.wait()

    async def send(self, request_id, method, params):
        self.requests.append((request_id, method))
        await self.socket.send(json.dumps({
            "type": "req", "id": request_id, "method": method, "params": params,
        }))

    async def request(self, request_id, method, params):
        await self.send(request_id, method, params)
        frame = await self.receive(type="res", id=request_id)
        assert frame["ok"], frame
        return frame["payload"]

    def response(self, request_id):
        return next((frame for frame in self.frames
                     if frame.get("type") == "res" and frame.get("id") == request_id), None)

    async def barrier(self, nonce):
        await self.socket.send(json.dumps({"type": "ping", "nonce": nonce}))
        await self.receive(type="pong", nonce=nonce)

    async def close(self):
        await self.socket.close()
        await self.reader


class Dispatcher:
    def __init__(self):
        self.inner = get_dispatcher()
        self.mutation_started = asyncio.Event()
        self.release_mutation = asyncio.Event()

    def list_methods(self):
        return self.inner.list_methods()

    async def dispatch(self, request_id, method, params, ctx):
        if request_id == "mutation":
            self.mutation_started.set()
            await self.release_mutation.wait()
        return await self.inner.dispatch(request_id, method, params, ctx)


@asynccontextmanager
async def stack(tmp_path, monkeypatch, *, detached=False):
    if detached:
        monkeypatch.setattr(websocket, "_DETACHED_RPC_METHODS",
                            websocket._DETACHED_RPC_METHODS | READ_METHODS)
        monkeypatch.setattr(websocket, "_MAX_DETACHED_REQUESTS_PER_CONNECTION",
                            websocket._MAX_DETACHED_REQUESTS_PER_CONNECTION + len(READ_METHODS))
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    registry = websocket.ConnectionRegistry()
    monkeypatch.setattr(websocket, "_registry", registry)
    monkeypatch.setattr(rpc_sessions, "_V2_READ_LEASES", {})
    monkeypatch.setattr(rpc_sessions, "_V2_READ_OPEN_BY_CONNECTION", {})
    monkeypatch.setattr(rpc_sessions, "_V2_READ_CONNECTION_OWNERS", {})
    streams = session_streams.SessionStreamRegistry(stream_generation="read-v2-real-socket")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    manager = SessionManager(storage, checkpoint_workspace_dir=tmp_path / "workspace")
    original = await manager.create(KEY)
    await manager.append_message(KEY, "user", "actual durable transcript")
    subscriptions = websocket.SubscriptionManager()
    dispatcher = Dispatcher()
    config = GatewayConfig(
        auth=AuthConfig(mode="none", token=None), state_dir=str(tmp_path / "state"),
        config_path=str(tmp_path / "config.toml"), ws_writer_queue_enabled=True,
        client_ws_keepalive_timeout_s=0,
    )

    async def endpoint(ws):
        await websocket.handle_ws_connection(
            ws, config, dispatcher=dispatcher, subscription_manager=subscriptions,
            session_manager=manager,
        )

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        Starlette(routes=[WebSocketRoute("/ws", endpoint)]), host="127.0.0.1", port=port,
        ws="websockets", lifespan="off", ws_max_size=MAX_PAYLOAD_BYTES,
        timeout_graceful_shutdown=2, log_level="error", access_log=False,
    ))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    clients = []

    async def new_client():
        client = Client(await connect(f"ws://127.0.0.1:{port}/ws", max_size=MAX_PAYLOAD_BYTES))
        clients.append(client)
        await client.receive(event="connect.challenge")
        await client.send("connect", "connect", {
            "minProtocol": 4, "maxProtocol": 4, "caps": [
                "transport.flow.v1", "transport.recovery.v1", "transport.probe.v1",
                SESSION_FLOW_V2_CAPABILITY,
            ],
        })
        hello = await client.receive(type="hello-ok")
        client.conn_id = hello["server"]["conn_id"]
        connection = registry.get(client.conn_id)
        assert connection is not None and connection._recovery_enabled
        assert connection._flow is not None
        assert connection._session_flow_v2_enabled
        return client

    try:
        async with asyncio.timeout(5):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.001)
        yield storage, manager, original, registry, dispatcher, new_client
    finally:
        dispatcher.release_mutation.set()
        for client in clients:
            await client.close()
        server.should_exit = True
        await asyncio.wait_for(serving, 5)
        listener.close()
        await asyncio.wait_for(storage.close(), 5)


async def open_read(client, prefix=""):
    opened = await client.request(prefix + "open", "sessions.read.open.v2", {"key": KEY})
    return opened, {"key": KEY, "lease_id": opened["lease_id"],
                    "recovery_id": opened["recovery_id"]}


@pytest.mark.parametrize("detached", [False, True], ids=["current", "candidate"])
async def test_own_real_history_delays_confirmation(tmp_path, monkeypatch, detached):
    async with stack(tmp_path, monkeypatch, detached=detached) as values:
        storage, _, _, _, _, new_client = values
        client = await new_client()
        opened, params = await open_read(client)
        entered, release = asyncio.Event(), asyncio.Event()
        actual_history = rpc_sessions.read_chat_history_v4

        async def gated_history(*args, **kwargs):
            history = await actual_history(*args, **kwargs)
            assert history["messages"]  # Real handler and SQLite read completed.
            entered.set()
            await release.wait()
            return history

        monkeypatch.setattr(rpc_sessions, "read_chat_history_v4", gated_history)
        try:
            await client.send("history", "sessions.history.page.v2", {
                "key": KEY, "direction": "before",
            })
            await asyncio.wait_for(entered.wait(), 5)
            await client.send("state", "sessions.read.state.v2", params)
            await client.barrier("state-received")
            if detached:
                assert (await client.receive(type="res", id="state"))["ok"]
                await client.request("install", "sessions.read.install.v2", params)
                receipt = await storage.get_session_read_recovery_receipt(opened["recovery_id"])
                assert receipt is not None and receipt["status"] == "installed"
            else:
                await asyncio.sleep(0.03)
                assert client.response("state") is None
            release.set()
            assert (await client.receive(type="res", id="history"))["ok"]
            assert (await client.receive(type="res", id="state"))["ok"]
            await client.request("close", "sessions.read.close.v2", params)
            assert (client.conn_id, opened["lease_id"]) not in rpc_sessions._V2_READ_LEASES
        finally:
            release.set()


@pytest.mark.parametrize("detached", [False, True], ids=["current", "candidate"])
@pytest.mark.parametrize("stage", ["before", "after"])
async def test_real_disconnect_around_durable_receipt(tmp_path, monkeypatch, detached, stage):
    async with stack(tmp_path, monkeypatch, detached=detached) as values:
        storage, _, _, registry, _, new_client = values
        client = await new_client()
        opened, params = await open_read(client)
        entered, release, cancelled, settled = (asyncio.Event() for _ in range(4))
        persist = storage.upsert_session_read_recovery_receipt

        async def gated_persist(receipt):
            try:
                if stage == "before":
                    entered.set()
                    await release.wait()
                result = await persist(receipt)
                if stage == "after":
                    entered.set()
                    await release.wait()
                return result
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                settled.set()

        monkeypatch.setattr(storage, "upsert_session_read_recovery_receipt", gated_persist)
        try:
            await client.send("install", "sessions.read.install.v2", params)
            await asyncio.wait_for(entered.wait(), 5)
            await client.close()
            async with asyncio.timeout(5):
                while registry.get(client.conn_id) is not None:
                    await asyncio.sleep(0.001)
            assert cancelled.is_set() is detached
            assert (client.conn_id, opened["lease_id"]) not in rpc_sessions._V2_READ_LEASES
            release.set()
            await asyncio.wait_for(settled.wait(), 5)
            receipt = await storage.get_session_read_recovery_receipt(opened["recovery_id"])
            assert (receipt is not None) is (stage == "after" or not detached)
            if receipt:
                assert receipt["status"] == "installed"
            # A genuine new socket cannot query the deleted connection lease.
            replacement = await new_client()
            await replacement.send("old-state", "sessions.read.state.v2", params)
            stale = await replacement.receive(type="res", id="old-state")
            assert not stale["ok"] and stale["error"]["code"] == "READ_STALE"
            fresh, fresh_params = await open_read(replacement, "new-")
            assert fresh["lease_id"] != opened["lease_id"]
            fresh_result = await replacement.request(
                "fresh-install", "sessions.read.install.v2", fresh_params,
            )
            assert fresh_result["status"] == "installed"
            assert await storage.get_session_read_recovery_receipt(fresh["recovery_id"])
        finally:
            release.set()


@pytest.mark.parametrize("detached", [False, True], ids=["current", "candidate"])
@pytest.mark.parametrize("cross_connection", [False, True], ids=["same", "cross"])
@pytest.mark.parametrize("mutation", ["reset", "delete", "truncate"])
async def test_real_mutation_order_and_lease_fences(
    tmp_path, monkeypatch, detached, cross_connection, mutation,
):
    async with stack(tmp_path, monkeypatch, detached=detached) as values:
        storage, manager, original, _, dispatcher, new_client = values
        reader = await new_client()
        writer = await new_client() if cross_connection else reader
        opened, params = await open_read(reader)
        await reader.request("initial-install", "sessions.read.install.v2", params)
        await writer.send("mutation", f"sessions.{mutation}", {"key": KEY, "maxMessages": 0})
        await asyncio.wait_for(dispatcher.mutation_started.wait(), 5)
        await reader.send("state-before", "sessions.read.state.v2", params)
        await reader.barrier("state-seen")
        can_pass = detached or cross_connection
        if can_pass:
            before = await reader.receive(type="res", id="state-before")
            assert before["ok"] and before["payload"]["status"] == "installed"
        else:
            await asyncio.sleep(0.03)
            assert reader.response("state-before") is None
        dispatcher.release_mutation.set()
        completed = await writer.receive(type="res", id="mutation")
        assert completed["ok"], completed
        state = await reader.request("state-after", "sessions.read.state.v2", params)
        current = await storage.get_session(KEY)
        if mutation == "truncate":
            assert await manager.get_transcript(KEY) == []
            assert current.session_id == original.session_id and current.epoch == original.epoch
            assert state["status"] == "installed"  # Existing identity fences do not see truncate.
        else:
            assert state["status"] == "rebase_required"
            if mutation == "delete":
                assert current is None
            else:
                assert current.session_id != original.session_id
        await reader.request("close", "sessions.read.close.v2", params)
        await reader.send("reopen", "sessions.read.open.v2", {"key": KEY})
        reopened = await reader.receive(type="res", id="reopen")
        if mutation == "delete":
            assert not reopened["ok"] and reopened["error"]["code"] == "NOT_FOUND"
        else:
            assert reopened["ok"] and reopened["payload"]["lease_id"] != opened["lease_id"]
            assert reopened["payload"]["session_id"] == current.session_id


async def test_detached_candidate_history_saturation_rejects_confirmation(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch, detached=True) as values:
        _, _, _, _, _, new_client = values
        client = await new_client()
        _, params = await open_read(client)
        limit = websocket._MAX_DETACHED_REQUESTS_PER_CONNECTION
        entered, release = asyncio.Event(), asyncio.Event()
        actual_history = rpc_sessions.read_chat_history_v4
        count = 0

        async def gated_history(*args, **kwargs):
            nonlocal count
            result = await actual_history(*args, **kwargs)
            count += 1
            if count == limit:
                entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(rpc_sessions, "read_chat_history_v4", gated_history)
        try:
            for index in range(limit):
                await client.send(f"history-{index}", "sessions.history.page.v2", {
                    "key": KEY, "direction": "before",
                })
            await asyncio.wait_for(entered.wait(), 5)
            await client.send("state", "sessions.read.state.v2", params)
            response = await client.receive(type="res", id="state")
            assert not response["ok"]
            assert response["error"]["retryable"] is True
            assert "Too many detached requests" in response["error"]["message"]
            release.set()
            for index in range(limit):
                assert (await client.receive(type="res", id=f"history-{index}"))["ok"]
            retried = await client.request("state-retry", "sessions.read.state.v2", params)
            assert retried["status"] == "staged"
        finally:
            release.set()


async def test_confirmation_first_removes_own_history_dependency_without_new_lane(
    tmp_path, monkeypatch,
):
    async with stack(tmp_path, monkeypatch) as values:
        storage, _, _, _, _, new_client = values
        client = await new_client()
        started = time.perf_counter_ns()
        opened, params = await open_read(client)
        await client.request("state", "sessions.read.state.v2", params)
        installed = await client.request("install", "sessions.read.install.v2", params)
        ready_ms = (time.perf_counter_ns() - started) / 1_000_000
        assert installed["status"] == "installed"
        assert await storage.get_session_read_recovery_receipt(opened["recovery_id"])
        entered, release = asyncio.Event(), asyncio.Event()
        actual_history = rpc_sessions.read_chat_history_v4

        async def gated_history(*args, **kwargs):
            history = await actual_history(*args, **kwargs)
            entered.set()
            await release.wait()
            return history

        monkeypatch.setattr(rpc_sessions, "read_chat_history_v4", gated_history)
        try:
            await client.send("history", "sessions.history.page.v2", {
                "key": KEY, "direction": "before",
            })
            await asyncio.wait_for(entered.wait(), 5)
            assert client.response("install")["payload"]["status"] == "installed"
            assert client.response("history") is None
            read_requests = [method for _, method in client.requests
                             if method.startswith("sessions.read.")]
            history_requests = [method for _, method in client.requests
                                if method == "sessions.history.page.v2"]
            assert len(read_requests) == 3 and len(history_requests) == 1
            print(json.dumps({"control": "confirmation-before-history", "readiness_ms": ready_ms,
                              "readiness_rpcs": len(read_requests),
                              "history_rpcs": len(history_requests),
                              "recovery_retries": len(read_requests) - len(set(read_requests))}))
            release.set()
            assert (await client.receive(type="res", id="history"))["ok"]
        finally:
            release.set()


async def test_detached_candidate_still_waits_for_native_shared_reader(tmp_path, monkeypatch):
    async with stack(tmp_path, monkeypatch, detached=True) as values:
        storage, _, _, _, _, new_client = values
        client = await new_client()
        _, params = await open_read(client)
        entered, release = threading.Event(), threading.Event()

        def gate():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Native read gate was not released")
            return 1

        reader = storage._transcript_reader
        assert reader is not None
        await reader.create_function("read_v2_gate", 0, gate)
        actual_history = rpc_sessions.read_chat_history_v4

        async def gated_history(*args, **kwargs):
            # Execute on the actual shared query-only SQLite connection. This
            # is a deterministic native stall, not a claim about its incidence.
            async with reader.execute("SELECT read_v2_gate()") as cursor:
                await cursor.fetchone()
            return await actual_history(*args, **kwargs)

        monkeypatch.setattr(rpc_sessions, "read_chat_history_v4", gated_history)
        try:
            await client.send("history", "sessions.history.page.v2", {
                "key": KEY, "direction": "before",
            })
            assert await asyncio.to_thread(entered.wait, 2)
            await client.send("state", "sessions.read.state.v2", params)
            await client.barrier("native-state-enqueued")
            await asyncio.sleep(0.03)
            assert client.response("state") is None
            release.set()
            assert (await client.receive(type="res", id="state"))["ok"]
            assert (await client.receive(type="res", id="history"))["ok"]
        finally:
            release.set()


@pytest.mark.parametrize("detached", [False, True], ids=["current", "candidate"])
async def test_disconnect_during_native_receipt_insert_retains_writer_until_settled(
    tmp_path, monkeypatch, detached,
):
    async with stack(tmp_path, monkeypatch, detached=detached) as values:
        storage, _, _, registry, _, new_client = values
        client = await new_client()
        opened, params = await open_read(client)
        entered, release = threading.Event(), threading.Event()
        settled, cancelled = asyncio.Event(), asyncio.Event()

        def gate():
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Native receipt gate was not released")
            return 1

        await storage.conn.create_function("receipt_insert_gate", 0, gate)
        await storage.conn.execute(
            "CREATE TRIGGER gate_receipt BEFORE INSERT ON session_read_recovery_receipts "
            "BEGIN SELECT receipt_insert_gate(); END"
        )
        await storage.conn.commit()
        persist = storage.upsert_session_read_recovery_receipt

        async def observe_persist(receipt):
            try:
                return await persist(receipt)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                settled.set()

        monkeypatch.setattr(storage, "upsert_session_read_recovery_receipt", observe_persist)
        try:
            await client.send("install", "sessions.read.install.v2", params)
            assert await asyncio.to_thread(entered.wait, 2)
            await client.close()
            async with asyncio.timeout(5):
                while registry.get(client.conn_id) is not None:
                    await asyncio.sleep(0.001)
            assert not settled.is_set()
            assert storage._operation_lock.locked()
            assert await storage.get_session_read_recovery_receipt(opened["recovery_id"]) is None
            release.set()
            await asyncio.wait_for(settled.wait(), 5)
            assert cancelled.is_set() is detached
            assert not storage._operation_lock.locked()
            assert not storage.conn.in_transaction
            receipt = await storage.get_session_read_recovery_receipt(opened["recovery_id"])
            assert (receipt is not None) is (not detached)
            # A new write proves actual rollback/commit and gate cleanup.
            await storage.upsert_session_read_recovery_receipt({
                "recovery_id": "after-settlement", "session_key": KEY,
                "session_id": opened["session_id"], "session_epoch": opened["session_epoch"],
                "connection_epoch": "new", "subscription_epoch": "new",
                "stream_generation": "read-v2-real-socket", "base_seq": 0,
                "target_seq": 0, "consumed_through_seq": 0, "status": "installed", "proof": {},
            })
            assert await storage.get_session_read_recovery_receipt("after-settlement")
        finally:
            release.set()
