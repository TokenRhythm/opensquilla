"""Real WebSocket and native SQLite recovery isolation, without providers."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from websockets.asyncio.client import connect

from opensquilla.compat import aiosqlite
from opensquilla.gateway import rpc_sessions, rpc_transport, session_streams, websocket
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.protocol import MAX_PAYLOAD_BYTES
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.transport_flow import FLOW_WINDOW_FRAMES, get_transport_budget
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage

assert rpc_sessions and rpc_transport


@pytest.mark.parametrize("fallback", [False, True], ids=["aiosqlite", "sqlite3-fallback"])
async def test_other_session_installs_over_real_socket_while_native_read_retires(
    tmp_path, monkeypatch, fallback,
):
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", fallback)
    registry = websocket.ConnectionRegistry()
    streams = session_streams.SessionStreamRegistry(stream_generation="native-socket-recovery")
    monkeypatch.setattr(websocket, "_registry", registry)
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    initial_bytes = get_transport_budget().used
    subscriptions = websocket.SubscriptionManager()
    storage = await SessionStorage.open(str(tmp_path / "recovery.sqlite"))
    key_a, key_b = "agent:main:webchat:slow", "agent:main:webchat:healthy"
    for key in (key_a, key_b):
        await storage.upsert_session(SessionNode(
            session_key=key, session_id=key.rsplit(":", 1)[-1], agent_id="main",
        ))
    streams.record(key_b, "session.event.text_delta", {
        "task_id": "synthetic-task", "text": "x" * 500_000,
    })
    entered, release = threading.Event(), threading.Event()

    def gate():
        entered.set()
        if not release.wait(10):
            raise RuntimeError("Synthetic native gate was not released")
        return 1

    original_open = storage._open_recovery_reader

    async def open_reader():
        reader = await original_open()
        await reader.create_function("recovery_socket_gate", 0, gate)
        return reader

    monkeypatch.setattr(storage, "_open_recovery_reader", open_reader)
    original_get = storage.get_session

    async def get_session(key):
        if key == key_a:
            await storage._read_history_query("SELECT recovery_socket_gate()", ())
        return await original_get(key)

    monkeypatch.setattr(storage, "get_session", get_session)
    config = GatewayConfig(
        auth=AuthConfig(mode="none", token=None),
        state_dir=str(tmp_path / "state"), config_path=str(tmp_path / "config.toml"),
        ws_writer_queue_enabled=True, client_ws_keepalive_timeout_s=0,
    )

    async def endpoint(ws):
        await websocket.handle_ws_connection(
            ws, config, dispatcher=get_dispatcher(), subscription_manager=subscriptions,
            session_manager=SimpleNamespace(storage=storage),
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
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.001)
        async with connect(f"ws://127.0.0.1:{port}/ws", max_size=MAX_PAYLOAD_BYTES) as client:
            async def receive(**match):
                async with asyncio.timeout(5):
                    while True:
                        frame = json.loads(await client.recv())
                        if all(frame.get(k) == value for k, value in match.items()):
                            return frame

            async def send(request_id, method, params):
                await client.send(json.dumps({
                    "type": "req", "id": request_id, "method": method, "params": params,
                }))

            async def request(request_id, method, params):
                await send(request_id, method, params)
                frame = await receive(type="res", id=request_id)
                assert frame["ok"], frame.get("error")
                return frame["payload"]

            await receive(event="connect.challenge")
            await send("connect", "connect", {
                "minProtocol": 4, "maxProtocol": 4,
                "caps": ["transport.flow.v1", "transport.recovery.v1", "transport.probe.v1"],
            })
            hello = await receive(type="hello-ok")
            assert {"chat.history", "sessions.messages.subscribe",
                    "sessions.messages.snapshot.read", "sessions.messages.resume"} <= set(
                hello["policy"]["cancellable_request_methods"],
            )
            conn = registry.get(hello["server"]["conn_id"])
            assert conn is not None and conn._recovery_enabled
            await request("sub-a", "sessions.messages.subscribe", {"key": key_a, "fast_ack": True})
            await send("read-a", "sessions.messages.snapshot.read", {
                "key": key_a, "sync_revision": "slow-attempt",
            })
            assert await asyncio.to_thread(entered.wait, 2)
            await client.send(json.dumps({"type": "cancel", "id": "read-a"}))
            await client.send(json.dumps({"type": "ping", "nonce": "cancel-observed"}))
            await receive(type="pong", nonce="cancel-observed")
            started = time.monotonic()
            async with asyncio.timeout(1):
                await request("sub-b", "sessions.messages.subscribe", {
                    "key": key_b, "fast_ack": True,
                })
                part = await request("read-b-0", "sessions.messages.snapshot.read", {
                    "key": key_b, "sync_revision": "healthy-attempt",
                })
                assert 1 < part["segment_count"] and part["byte_length"] <= 1024 * 1024
                first = part
                chunks = []
                for index in range(first["segment_count"]):
                    if index:
                        part = await request(f"read-b-{index}", "sessions.messages.snapshot.read", {
                            "key": key_b, "sync_revision": first["sync_revision"],
                            "snapshot_id": first["snapshot_id"], "segment_index": index,
                        })
                    chunks.append(base64.b64decode(part["data"]))
                    await request(f"stage-b-{index}", "transport.flow.update", {
                        "delivery_epoch": part["delivery"]["delivery_epoch"],
                        "ack_delivery_id": 0,
                        "staged_delivery_ids": [part["delivery"]["delivery_id"]],
                    })
                proof = await request("install-b", "sessions.messages.resume", {
                    **{k: first[k] for k in ("key", "sync_revision", "snapshot_id",
                                            "stream_generation")},
                    "stream_seq": first["current_stream_seq"],
                })
                decoded = json.loads(b"".join(chunks))
                assert decoded["key"] == key_b
                assert proof["session_id"] == "healthy"
                assert proof["replay_to_seq"] == first["current_stream_seq"]
            assert time.monotonic() - started < 1
            assert not release.is_set()
            assert "read-a" in conn._recovery_operations
            assert storage._recovery_read_pool.active_count == 1
            release.set()
            async with asyncio.timeout(2):
                while "read-a" in conn._recovery_operations:
                    await asyncio.sleep(0.001)
    finally:
        release.set()
        server.should_exit = True
        await asyncio.wait_for(server_task, 5)
        listener.close()
        await storage.close()
    assert get_transport_budget().used == initial_bytes


async def test_v2_recovery_hands_off_continuing_output_without_loss_over_real_socket(
    tmp_path, monkeypatch,
):
    """A real lane ACK completes one recovery while the producer keeps emitting."""
    registry = websocket.ConnectionRegistry()
    streams = session_streams.SessionStreamRegistry(stream_generation="continuing-socket-recovery")
    monkeypatch.setattr(websocket, "_registry", registry)
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    initial_bytes = get_transport_budget().used
    subscriptions = websocket.SubscriptionManager()
    storage = await SessionStorage.open(str(tmp_path / "continuing.sqlite"))
    key_a, key_b = "agent:main:webchat:continuing", "agent:main:webchat:independent"
    for key in (key_a, key_b):
        await storage.upsert_session(SessionNode(
            session_key=key, session_id=key.rsplit(":", 1)[-1], agent_id="main",
        ))
    config = GatewayConfig(
        auth=AuthConfig(mode="none", token=None),
        state_dir=str(tmp_path / "state"), config_path=str(tmp_path / "config.toml"),
        ws_writer_queue_enabled=True, client_ws_keepalive_timeout_s=0,
    )

    async def endpoint(ws):
        await websocket.handle_ws_connection(
            ws, config, dispatcher=get_dispatcher(), subscription_manager=subscriptions,
            session_manager=SimpleNamespace(storage=storage),
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
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.001)
        async with connect(f"ws://127.0.0.1:{port}/ws", max_size=MAX_PAYLOAD_BYTES) as client:
            received = []
            calls = []

            async def receive(**match):
                async with asyncio.timeout(5):
                    while True:
                        frame = json.loads(await client.recv())
                        received.append(frame)
                        if all(frame.get(k) == value for k, value in match.items()):
                            return frame

            async def request(request_id, method, params):
                calls.append(method)
                await client.send(json.dumps({
                    "type": "req", "id": request_id, "method": method, "params": params,
                }))
                frame = await receive(type="res", id=request_id)
                assert frame["ok"], frame.get("error")
                return frame["payload"]

            await receive(event="connect.challenge")
            await client.send(json.dumps({
                "type": "req", "id": "connect", "method": "connect", "params": {
                    "minProtocol": 4, "maxProtocol": 4,
                    "caps": [
                        "transport.flow.v1", "transport.session-flow.v2", "transport.recovery.v1",
                        "transport.probe.v1",
                    ],
                },
            }))
            hello = await receive(type="hello-ok")
            conn = registry.get(hello["server"]["conn_id"])
            assert conn is not None and conn._recovery_enabled and conn._session_flow_v2_enabled
            for label, key in (("a", key_a), ("b", key_b)):
                await request(f"sub-{label}", "sessions.messages.subscribe", {
                    "key": key, "fast_ack": True,
                })

            expected = []

            async def emit(text, key=key_a):
                payload = streams.record(key, "session.event.text_delta", {
                    "task_id": f"active-{key.rsplit(':', 1)[-1]}", "text": text,
                })
                if key == key_a:
                    expected.append(text)
                await conn.send_event("session.event.text_delta", payload)

            def stream_frames(key):
                return [
                    frame for frame in received
                    if frame.get("event") == "session.event.text_delta"
                    and frame["payload"]["session_key"] == key
                ]

            async def ack(request_id, key, through):
                while not any(
                    frame["meta"]["flow"]["delivery_id"] == through
                    for frame in stream_frames(key)
                ):
                    await receive()
                frame = next(
                    frame for frame in stream_frames(key)
                    if frame["meta"]["flow"]["delivery_id"] == through
                )
                lane = frame["meta"]["session_flow_v2"]
                return await request(request_id, "transport.sessionFlow.update.v2", {
                    "connection_epoch": lane["connection_epoch"],
                    "consumed": [{
                        "subscription_epoch": lane["subscription_epoch"],
                        "through_delivery_id": through,
                    }],
                })

            # Ordinary short bursts no longer exhaust an eight-frame lane.
            for index in range(9):
                await emit(f"burst-{index}|")
            while len(stream_frames(key_a)) < 9:
                await receive()
            assert len(stream_frames(key_a)) == 9
            assert not conn._flow.dirty
            await ack("ack-burst", key_a, stream_frames(key_a)[-1]["meta"]["flow"]["delivery_id"])
            # Model a consumer asking for an authoritative base explicitly;
            # a healthy nine-frame burst must not manufacture a dirty event.
            await request("consumer-dirty", "transport.flow.update", {
                "delivery_epoch": conn._flow.epoch, "ack_delivery_id": 0,
                "dirty_keys": [key_a],
            })

            part = await request("snapshot-a", "sessions.messages.snapshot.read", {
                "key": key_a, "sync_revision": "one-continuing-recovery",
            })
            assert part["segment_count"] == 1 and part["current_stream_seq"] == 9
            decoded = json.loads(base64.b64decode(part["data"]))
            base_text = "".join(
                event["payload"]["text"] for event in decoded["events"]
                if event["event"] == "session.event.text_delta"
            )
            assert base_text == "".join(expected)
            await request("stage-a", "transport.sessionFlow.update.v2", {
                "connection_epoch": part["delivery"]["delivery_epoch"],
                "staged_recovery": [part["delivery"]],
            })
            # Freeze more than two real windows, then keep producing beyond
            # the 500-event raw cache while the finite proof is consumed.
            replay_count = FLOW_WINDOW_FRAMES * 2 + 17
            for index in range(replay_count):
                await emit(f"replay-{index}|")
            receipt = {
                **{name: part[name] for name in (
                    "key", "sync_revision", "snapshot_id", "stream_generation",
                )},
                "stream_seq": part["current_stream_seq"],
            }
            proof = await request("resume-a", "sessions.messages.resume", receipt)
            assert proof["replay_to_seq"] == 9 + replay_count
            for index in range(601):
                await emit(f"owned-tail-{index}|")
            assert sum(len(queue) for queue in conn._pending_flow.values()) == 601
            for index in range(3):
                pending = conn._pending_replays.get(key_a)
                if pending is None:
                    break
                through = max(pending.pending_delivery_ids)
                await emit(f"during-ack-{index}|")
                if index == 0:
                    await emit("other-session-remains-live|", key_b)
                await ack(f"ack-replay-{index}", key_a, through)
            assert key_a not in conn._pending_replays
            assert key_a not in conn._flow.dirty
            assert conn.installed_snapshot_proof(receipt) == proof
            assert conn._flow.deliveries, "the handoff tail is owned, not falsely ACKed"
            await emit("after-handoff|")
            # Real ACKs, not an idle producer, release successive FIFO windows.
            for index in range(8):
                lane_ids = [
                    delivery_id for delivery_id, delivery in conn._flow.deliveries.items()
                    if delivery.lane == key_a
                ]
                if not lane_ids:
                    break
                await ack(f"ack-tail-{index}", key_a, max(lane_ids))
            assert not conn._pending_flow
            await client.send(json.dumps({"type": "ping", "nonce": "tail-sent"}))
            await receive(type="pong", nonce="tail-sent")
            assert key_a not in conn._flow.dirty
            tail = [
                frame for frame in stream_frames(key_a)
                if frame["payload"]["stream_seq"] > part["current_stream_seq"]
            ]
            assert [frame["payload"]["stream_seq"] for frame in tail] == list(
                range(part["current_stream_seq"] + 1, streams.current_seq(key_a) + 1),
            )
            recovered_text = base_text + "".join(frame["payload"]["text"] for frame in tail)
            assert recovered_text == "".join(expected)
            assert [frame["payload"]["text"] for frame in stream_frames(key_b)] == [
                "other-session-remains-live|",
            ]
            assert streams.live_snapshot(key_a).task_id == "active-continuing"
            assert calls.count("sessions.messages.snapshot.read") == 1
            assert calls.count("sessions.messages.resume") == 1
            await ack(
                "ack-final-b", key_b,
                stream_frames(key_b)[-1]["meta"]["flow"]["delivery_id"],
            )
            assert not conn._flow.deliveries
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, 5)
        listener.close()
        await storage.close()
    assert get_transport_budget().used == initial_bytes
