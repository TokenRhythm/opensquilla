"""Real loopback transport gates; no profile, subprocess Gateway, or LLM.

The ASGI endpoint is production handle_ws_connection. Only two synthetic RPCs
are added; snapshot and flow controls use their production dispatcher/contracts.
Python consumption is not evidence of Chromium rendering or packaged behavior.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from websockets.asyncio.client import ClientConnection, connect

from opensquilla.gateway import rpc_sessions, rpc_transport, session_streams, websocket
from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.protocol import MAX_PAYLOAD_BYTES, make_ok_res
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.transport_flow import FLOW_WINDOW_FRAMES, get_transport_budget

# These imports register only handlers; nothing boots providers or profiles.
assert rpc_sessions and rpc_transport


class _RestrictedDispatcher:
    def __init__(self, subscriptions: websocket.SubscriptionManager) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.order: list[str] = []
        self.subscriptions = subscriptions

    def list_methods(self) -> list[str]:
        return [
            "test.slow",
            "test.next",
            "test.subscribe",
            "transport.flow.update",
            "sessions.messages.snapshot.read",
        ]

    async def dispatch(self, req_id: str, method: str, params: Any, ctx: Any) -> Any:
        if method not in self.list_methods():
            raise AssertionError(f"Unexpected test method: {method}")
        if method == "test.slow":
            self.order.append(req_id)
            self.started.set()
            await self.release.wait()
        elif method == "test.next":
            self.order.append(req_id)
        elif method == "test.subscribe":
            self.subscriptions.subscribe_messages(ctx.conn_id, params["key"])
        else:
            return await get_dispatcher().dispatch(req_id, method, params, ctx)
        return make_ok_res(req_id, {"method": method})


@pytest.fixture
async def gateway_socket(tmp_path, monkeypatch):
    registry = websocket.ConnectionRegistry()
    streams = session_streams.SessionStreamRegistry(stream_generation="socket-gate")
    monkeypatch.setattr(websocket, "_registry", registry)
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    initial_bytes = get_transport_budget().used
    subscriptions = websocket.SubscriptionManager()
    dispatcher = _RestrictedDispatcher(subscriptions)
    config = GatewayConfig(
        auth=AuthConfig(mode="none", token=None),
        state_dir=str(tmp_path / "state"),
        config_path=str(tmp_path / "config.toml"),
        ws_transport_flow_enabled=True,
        ws_writer_queue_enabled=True,
        client_ws_keepalive_timeout_s=0,
    )

    async def endpoint(ws):
        await websocket.handle_ws_connection(
            ws,
            config,
            dispatcher=dispatcher,
            subscription_manager=subscriptions,
        )

    # Reserve the loopback port without a bind-close-rebind race. Passing the
    # owned socket to Uvicorn keeps this isolated from any installed Gateway.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            Starlette(routes=[WebSocketRoute("/ws", endpoint)]),
            host="127.0.0.1",
            port=port,
            ws="websockets",
            lifespan="off",
            ws_max_size=MAX_PAYLOAD_BYTES,
            ws_ping_interval=20.0,
            ws_ping_timeout=120.0,
            timeout_graceful_shutdown=2,
            log_level="error",
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.001)
        yield SimpleNamespace(
            uri=f"ws://127.0.0.1:{port}/ws",
            registry=registry,
            streams=streams,
            dispatcher=dispatcher,
            subscriptions=subscriptions,
        )
    finally:
        dispatcher.release.set()
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
        assert get_transport_budget().used == initial_bytes


async def _receive_until(client: ClientConnection, observed: list[dict], **fields: Any) -> dict:
    async with asyncio.timeout(5):
        while True:
            frame = json.loads(await client.recv())
            observed.append(frame)
            if all(frame.get(key) == value for key, value in fields.items()):
                return frame


async def _request(client, observed, request_id, method, params):
    await client.send(
        json.dumps(
            {
                "type": "req",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
    )
    return await _receive_until(client, observed, type="res", id=request_id)


@asynccontextmanager
async def _connected(gateway, *, flow=True):
    async with connect(gateway.uri, max_size=MAX_PAYLOAD_BYTES, max_queue=256) as client:
        observed: list[dict] = []
        challenge = await _receive_until(client, observed, event="connect.challenge")
        assert challenge["payload"]["nonce"]
        await client.send(
            json.dumps(
                {
                    "type": "req",
                    "id": "connect",
                    "method": "connect",
                    "params": {
                        "minProtocol": 3,
                        "maxProtocol": 3,
                        "caps": ["transport.probe.v1", *(["transport.flow.v1"] if flow else [])],
                    },
                }
            )
        )
        hello = await _receive_until(client, observed, type="hello-ok")
        assert hello["policy"]["transport_probe_nonce"] is True
        assert hello["policy"]["client_ws_keepalive_timeout_ms"] == 0
        yield client, observed, hello


async def test_real_socket_slow_rpc_preserves_nonce_probe_and_fifo(gateway_socket):
    async with _connected(gateway_socket, flow=False) as (client, frames, hello):
        assert "transport_flow" not in hello["policy"]
        await client.send(
            json.dumps(
                {
                    "type": "req",
                    "id": "slow",
                    "method": "test.slow",
                    "params": {},
                }
            )
        )
        await asyncio.wait_for(gateway_socket.dispatcher.started.wait(), 5)
        await client.send(
            json.dumps(
                {
                    "type": "req",
                    "id": "next",
                    "method": "test.next",
                    "params": {},
                }
            )
        )
        await client.send(json.dumps({"type": "ping", "nonce": "real-socket-control"}))
        await _receive_until(client, frames, type="pong", nonce="real-socket-control")
        assert gateway_socket.dispatcher.order == ["slow"]
        # Also exercise RFC 6455 control ping via the actual websockets backend.
        pong_waiter = await client.ping(b"native-control")
        await asyncio.wait_for(pong_waiter, 2)
        gateway_socket.dispatcher.release.set()
        await _receive_until(client, frames, type="res", id="next")
        assert gateway_socket.dispatcher.order == ["slow", "next"]
        assert [f["id"] for f in frames if f["type"] == "res"] == ["slow", "next"]


async def test_real_socket_disconnect_reconnect_gets_new_transport_identity(gateway_socket):
    async with _connected(gateway_socket) as (_, _, first):
        first_id = first["server"]["conn_id"]
        first_epoch = first["policy"]["transport_flow"]["delivery_epoch"]
    async with asyncio.timeout(5):
        while gateway_socket.registry.get(first_id) is not None:
            await asyncio.sleep(0.001)
    async with _connected(gateway_socket) as (client, frames, second):
        assert second["server"]["conn_id"] != first_id
        assert second["policy"]["transport_flow"]["delivery_epoch"] != first_epoch
        stale = await _request(
            client,
            frames,
            "stale",
            "transport.flow.update",
            {
                "delivery_epoch": first_epoch,
                "ack_delivery_id": 0,
            },
        )
        assert stale["ok"] is False
        await client.send(json.dumps({"type": "ping", "nonce": "new-identity"}))
        await _receive_until(client, frames, type="pong", nonce="new-identity")


async def test_real_socket_flow_pause_and_snapshot_staging_crosses_ack_hole(gateway_socket):
    key = "agent:main:socket-snapshot"
    async with _connected(gateway_socket) as (client, frames, hello):
        epoch = hello["policy"]["transport_flow"]["delivery_epoch"]
        assert hello["policy"]["transport_flow"]["window_frames"] == 128
        subscribed = await _request(client, frames, "subscribe", "test.subscribe", {"key": key})
        assert subscribed["ok"]
        conn = gateway_socket.registry.get(hello["server"]["conn_id"])
        assert conn is not None and conn.flow_enabled
        for index in range(FLOW_WINDOW_FRAMES + 1):
            event = gateway_socket.streams.record(
                key,
                "session.event.text_delta",
                {"task_id": "task", "text": f"part-{index}"},
            )
            await conn.send_event("session.event.text_delta", event)
        # Keep a multi-segment snapshot in authoritative live state after the
        # ordinary window is exhausted. The client intentionally leaves ID 1
        # unowned and does not cumulatively ACK any ordinary delivery yet.
        event = gateway_socket.streams.record(
            key,
            "session.event.text_delta",
            {"task_id": "task", "text": "中" * 160_000},
        )
        await conn.send_event("session.event.text_delta", event)
        await client.send(json.dumps({"type": "ping", "nonce": "credit-paused"}))
        await _receive_until(client, frames, type="pong", nonce="credit-paused")
        assert len([f for f in frames if f.get("event") == "session.event.text_delta"]) == 128
        assert len(conn._flow.deliveries) == 128
        assert key in conn._flow.dirty

        parts = []
        first = None
        index = 0
        while first is None or index < first["segment_count"]:
            params = {"key": key, "sync_revision": "real-wire-install"}
            if first is not None:
                params.update(snapshot_id=first["snapshot_id"], segment_index=index)
            response = await _request(
                client,
                frames,
                f"snapshot-{index}",
                "sessions.messages.snapshot.read",
                params,
            )
            assert response["ok"], response.get("error")
            piece = response["payload"]
            first = first or piece
            parts.append(base64.b64decode(piece["data"], validate=True))
            staged = await _request(
                client,
                frames,
                f"stage-{index}",
                "transport.flow.update",
                {
                    "delivery_epoch": epoch,
                    "ack_delivery_id": 0,
                    "staged_delivery_ids": [piece["delivery"]["delivery_id"]],
                },
            )
            assert staged["ok"], staged.get("error")
            assert staged["payload"]["ack_delivery_id"] == 0
            assert len(conn._flow.deliveries) == 128
            index += 1
        assert first["segment_count"] >= 3
        snapshot = json.loads(b"".join(parts))
        assert snapshot["current_stream_seq"] == event["stream_seq"]
        resume = {
            "key": key,
            "snapshot_id": first["snapshot_id"],
            "sync_revision": "real-wire-install",
            "stream_generation": first["stream_generation"],
            "stream_seq": first["current_stream_seq"],
        }
        installed = await _request(
            client,
            frames,
            "install",
            "transport.flow.update",
            {
                "delivery_epoch": epoch,
                "ack_delivery_id": piece["delivery"]["delivery_id"],
                "dirty_keys": [key],
                "resume": [resume],
            },
        )
        assert installed["ok"], installed.get("error")
        assert installed["payload"]["dirty_keys"] == []
        assert not conn._flow.deliveries
        repeated = await _request(
            client,
            frames,
            "install-retry",
            "transport.flow.update",
            {
                "delivery_epoch": epoch,
                "ack_delivery_id": piece["delivery"]["delivery_id"],
                "resume": [resume],
            },
        )
        assert repeated["ok"], repeated.get("error")
        assert not conn._flow.dirty
        await client.send(json.dumps({"type": "ping", "nonce": "same-socket-recovered"}))
        await _receive_until(client, frames, type="pong", nonce="same-socket-recovered")
        assert conn.conn_id == hello["server"]["conn_id"]
