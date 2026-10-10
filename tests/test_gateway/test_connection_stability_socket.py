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
            "transport.sessionFlow.update.v2",
            "sessions.messages.snapshot.read",
            "sessions.messages.unsubscribe",
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
            connection = websocket.get_registry().get(ctx.conn_id)
            if connection is not None:
                connection._register_flow_subscription_epoch(params["key"])
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
async def _connected(gateway, *, flow=True, session_flow_v2=False):
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
                        "caps": [
                            "transport.probe.v1",
                            *(["transport.flow.v1"] if flow else []),
                            *(["transport.session-flow.v2"] if session_flow_v2 else []),
                        ],
                    },
                }
            )
        )
        hello = await _receive_until(client, observed, type="hello-ok")
        assert hello["policy"]["transport_probe_nonce"] is True
        assert hello["policy"]["client_ws_keepalive_timeout_ms"] == 0
        yield client, observed, hello


async def test_real_socket_session_flow_v2_requires_and_exposes_v2_method(gateway_socket):
    async with _connected(gateway_socket, session_flow_v2=True) as (client, frames, hello):
        flow = hello["policy"]["transport_flow"]
        assert flow["capability"] == "transport.session-flow.v2"
        assert flow["lane_mode"] is True
        response = await _request(
            client,
            frames,
            "session-flow-v2-empty",
            "transport.sessionFlow.update.v2",
            {
                "connection_epoch": flow["delivery_epoch"],
                "consumed": [],
                "staged_recovery": [],
                "discarded_lanes": [],
            },
        )
        assert response["ok"] is True
        assert response["payload"]["connection_epoch"] == flow["delivery_epoch"]


async def test_real_socket_session_flow_v2_switches_keep_snapshot_credit_usable(gateway_socket):
    """Retired history must not invalidate ACKs for the next draft's snapshot."""
    async with _connected(gateway_socket, session_flow_v2=True) as (client, frames, hello):
        epoch = hello["policy"]["transport_flow"]["delivery_epoch"]
        for index in range(32):
            key = f"agent:main:socket-switch-{index}"
            subscribed = await _request(
                client, frames, f"subscribe-{index}", "test.subscribe", {"key": key},
            )
            assert subscribed["ok"]
            retired = await _request(
                client, frames, f"unsubscribe-{index}",
                "sessions.messages.unsubscribe", {"key": key},
            )
            assert retired["ok"]
            receipt = retired["payload"]["lane_retire"]
            confirmed = await _request(
                client, frames, f"retire-{index}", "transport.sessionFlow.update.v2",
                {"connection_epoch": epoch, "discarded_lanes": [{
                    field: receipt[field] for field in (
                        "subscription_epoch", "retire_token", "final_published_id",
                    )
                }]},
            )
            assert confirmed["ok"], (index, confirmed)

        # A new draft has no durable row; its read still needs an ACK before
        # the browser can install authority and enable Send.
        key = "agent:main:socket-new-draft"
        await _request(client, frames, "subscribe-draft", "test.subscribe", {"key": key})
        snapshot = await _request(
            client, frames, "draft-snapshot", "sessions.messages.snapshot.read",
            {"key": key, "sync_revision": "post-switch-draft"},
        )
        assert snapshot["ok"], snapshot
        piece = snapshot["payload"]
        assert piece["segment_count"] == 1
        credit = await _request(
            client, frames, "draft-credit", "transport.sessionFlow.update.v2",
            {"connection_epoch": epoch, "staged_recovery": [piece["delivery"]]},
        )
        assert credit["ok"], credit
        assert set(credit["payload"]) == {
            "connection_epoch", "consumed", "staged_recovery", "discarded_lanes",
        }
        installed = await _request(
            client, frames, "draft-install", "transport.flow.update",
            {"delivery_epoch": epoch, "ack_delivery_id": 0, "resume": [{
                "key": key, "snapshot_id": piece["snapshot_id"],
                "sync_revision": piece["sync_revision"],
                "stream_generation": piece["stream_generation"],
                "stream_seq": piece["current_stream_seq"],
            }]},
        )
        assert installed["ok"], installed
        assert installed["payload"]["dirty_keys"] == []
        conn = gateway_socket.registry.get(hello["server"]["conn_id"])
        assert conn is not None and conn._flow is not None
        assert not conn._flow.deliveries


async def test_real_socket_session_flow_v2_retire_batch_accepts_old_lane_ack(gateway_socket):
    """A retire request may race with the last ACK for the old lane.

    The client batches both records in one v2 control RPC.  The old lane is
    already removed from the subscription manager at that point, so the
    server must validate the retire fence before rejecting the old ACK.
    """
    key = "agent:main:socket-v2-retire"
    async with _connected(gateway_socket, session_flow_v2=True) as (client, frames, hello):
        flow = hello["policy"]["transport_flow"]
        await _request(client, frames, "subscribe-retire", "test.subscribe", {"key": key})
        conn = gateway_socket.registry.get(hello["server"]["conn_id"])
        assert conn is not None and conn._flow is not None
        event = gateway_socket.streams.record(
            key,
            "session.event.text_delta",
            {"task_id": "retire", "text": "old"},
        )
        await conn.send_event("session.event.text_delta", event)
        delivered = await _receive_until(client, frames, event="session.event.text_delta")
        delivery_id = delivered["meta"]["flow"]["delivery_id"]
        subscription_epoch = delivered["meta"]["session_flow_v2"]["subscription_epoch"]
        assert subscription_epoch == gateway_socket.subscriptions.get_message_subscription_epoch(
            conn.conn_id, key,
        )
        await _request(
            client,
            frames,
            "unsubscribe-retire",
            "sessions.messages.unsubscribe",
            {"key": key},
        )
        # The production unsubscribe handler returns the retire receipt.  Use
        # the authoritative result from the RPC so this test exercises the
        # exact client batch shape rather than an invented token.
        unsubscribe = next(
            frame for frame in reversed(frames)
            if frame.get("type") == "res" and frame.get("id") == "unsubscribe-retire"
        )
        lane_retire = unsubscribe["payload"]["lane_retire"]
        response = await _request(
            client,
            frames,
            "retire-batch",
            "transport.sessionFlow.update.v2",
            {
                "connection_epoch": flow["delivery_epoch"],
                "consumed": [{
                    "subscription_epoch": subscription_epoch,
                    "through_delivery_id": delivery_id,
                }],
                "discarded_lanes": [{
                    "subscription_epoch": lane_retire["subscription_epoch"],
                    "retire_token": lane_retire["retire_token"],
                    "final_published_id": lane_retire["final_published_id"],
                }],
            },
        )
        assert response["ok"], json.dumps(response, ensure_ascii=False)
        assert not conn._flow.deliveries


async def test_real_socket_session_flow_v2_retire_batch_disambiguates_two_lanes(gateway_socket):
    """Connection-scoped epochs must not collide across simultaneous sessions."""
    key_a = "agent:main:socket-v2-two-lanes-a"
    key_b = "agent:main:socket-v2-two-lanes-b"
    async with _connected(gateway_socket, session_flow_v2=True) as (client, frames, hello):
        flow = hello["policy"]["transport_flow"]
        await _request(client, frames, "subscribe-two-a", "test.subscribe", {"key": key_a})
        await _request(client, frames, "subscribe-two-b", "test.subscribe", {"key": key_b})
        conn = gateway_socket.registry.get(hello["server"]["conn_id"])
        assert conn is not None and conn._flow is not None
        for key, task_id in ((key_a, "two-a"), (key_b, "two-b")):
            event = gateway_socket.streams.record(
                key, "session.event.text_delta", {"task_id": task_id, "text": task_id},
            )
            await conn.send_event("session.event.text_delta", event)
        delivered_a = await _receive_until(client, frames, event="session.event.text_delta")
        delivered_b = await _receive_until(client, frames, event="session.event.text_delta")
        by_task = {frame["payload"]["task_id"]: frame for frame in (delivered_a, delivered_b)}
        assert set(by_task) == {"two-a", "two-b"}
        epoch_a = by_task["two-a"]["meta"]["session_flow_v2"]["subscription_epoch"]
        epoch_b = by_task["two-b"]["meta"]["session_flow_v2"]["subscription_epoch"]
        assert epoch_a != epoch_b
        await _request(
            client, frames, "unsubscribe-two-a", "sessions.messages.unsubscribe", {"key": key_a}
        )
        unsubscribe = next(
            frame for frame in reversed(frames)
            if frame.get("type") == "res" and frame.get("id") == "unsubscribe-two-a"
        )
        lane_retire = unsubscribe["payload"]["lane_retire"]
        response = await _request(
            client,
            frames,
            "retire-two-a",
            "transport.sessionFlow.update.v2",
            {
                "connection_epoch": flow["delivery_epoch"],
                "consumed": [
                    {
                        "subscription_epoch": epoch_a,
                        "through_delivery_id": by_task["two-a"]["meta"]["session_flow_v2"][
                            "delivery_id"
                        ],
                    }
                ],
                "discarded_lanes": [
                    {
                        "subscription_epoch": lane_retire["subscription_epoch"],
                        "retire_token": lane_retire["retire_token"],
                        "final_published_id": lane_retire["final_published_id"],
                    }
                ],
            },
        )
        assert response["ok"]


async def test_real_socket_session_flow_v2_replacement_fences_idle_old_ack(gateway_socket):
    """A same-key replacement fences an old ACK before its first new event."""
    key = "agent:main:socket-v2-retire-idle"
    async with _connected(gateway_socket, session_flow_v2=True) as (client, frames, hello):
        flow = hello["policy"]["transport_flow"]
        await _request(client, frames, "subscribe-idle-old", "test.subscribe", {"key": key})
        conn = gateway_socket.registry.get(hello["server"]["conn_id"])
        assert conn is not None and conn._flow is not None
        event = gateway_socket.streams.record(
            key,
            "session.event.text_delta",
            {"task_id": "retire-idle", "text": "old"},
        )
        await conn.send_event("session.event.text_delta", event)
        delivered = await _receive_until(client, frames, event="session.event.text_delta")
        old_delivery_id = delivered["meta"]["session_flow_v2"]["delivery_id"]
        old_epoch = delivered["meta"]["session_flow_v2"]["subscription_epoch"]
        await _request(
            client,
            frames,
            "unsubscribe-idle-old",
            "sessions.messages.unsubscribe",
            {"key": key},
        )
        await _request(
            client,
            frames,
            "subscribe-idle-new",
            "test.subscribe",
            {"key": key},
        )
        stale = await _request(
            client,
            frames,
            "late-idle-old-ack",
            "transport.sessionFlow.update.v2",
            {
                "connection_epoch": flow["delivery_epoch"],
                "consumed": [{
                    "subscription_epoch": old_epoch,
                    "through_delivery_id": old_delivery_id,
                }],
                "staged_recovery": [],
                "discarded_lanes": [],
            },
        )
        assert stale["ok"] is False
        assert stale["error"]["code"] in {"INVALID_REQUEST", "FLOW_STALE"}


async def test_real_socket_session_flow_v2_ack_is_idempotent_and_rejects_future_id(gateway_socket):
    key = "agent:main:socket-v2-ack-order"
    async with _connected(gateway_socket, session_flow_v2=True) as (client, frames, hello):
        flow = hello["policy"]["transport_flow"]
        await _request(client, frames, "subscribe-ack-order", "test.subscribe", {"key": key})
        conn = gateway_socket.registry.get(hello["server"]["conn_id"])
        assert conn is not None and conn._flow is not None
        event = gateway_socket.streams.record(
            key,
            "session.event.text_delta",
            {"task_id": "ack-order", "text": "one"},
        )
        await conn.send_event("session.event.text_delta", event)
        delivered = await _receive_until(client, frames, event="session.event.text_delta")
        delivery_id = delivered["meta"]["flow"]["delivery_id"]
        subscription_epoch = delivered["meta"]["session_flow_v2"]["subscription_epoch"]
        params = {
            "connection_epoch": flow["delivery_epoch"],
            "consumed": [{
                "subscription_epoch": subscription_epoch,
                "through_delivery_id": delivery_id,
            }],
        }
        first = await _request(
            client, frames, "ack-order-1", "transport.sessionFlow.update.v2", params
        )
        assert first["ok"], first
        assert not conn._flow.deliveries
        duplicate = await _request(
            client, frames, "ack-order-duplicate", "transport.sessionFlow.update.v2", params,
        )
        assert duplicate["ok"], duplicate
        future = {
            "connection_epoch": flow["delivery_epoch"],
            "consumed": [{
                "subscription_epoch": subscription_epoch,
                "through_delivery_id": delivery_id + 99,
            }],
        }
        rejected = await _request(
            client, frames, "ack-order-future", "transport.sessionFlow.update.v2", future,
        )
        assert not rejected["ok"]
        assert rejected["error"]["code"] in {"INVALID_REQUEST", "FLOW_STALE"}


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
