"""Ordinary bursts wait for consumption without becoming recovery failures."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketState

from opensquilla.gateway.protocol import make_ok_res
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.transport_flow import (
    FLOW_WINDOW_FRAMES,
    SESSION_FLOW_V2_CAPABILITY,
    get_transport_budget,
)
from opensquilla.gateway.websocket import (
    SubscriptionManager,
    WsConnection,
    _dispatch_request,
    get_registry,
)


class Socket:
    client_state = WebSocketState.CONNECTED

    def __init__(self):
        self.frames = []
        self.closed = []
        self.first_send_gate = None

    async def send_text(self, text):
        self.frames.append(json.loads(text))
        if self.first_send_gate is not None:
            gate, self.first_send_gate = self.first_send_gate, None
            await gate.wait()

    async def close(self, **kwargs):
        self.closed.append(kwargs)


@pytest.fixture
async def conn():
    baseline = get_transport_budget().used
    connection = WsConnection(
        "pending-flow", Socket(), client_caps=frozenset({SESSION_FLOW_V2_CAPABILITY}),
    )
    connection._recovery_enabled = True
    connection._subscriptions = SubscriptionManager()
    connection._enable_flow()
    get_registry().register(connection)
    for key in ("A", "B"):
        connection._subscriptions.subscribe_messages(connection.conn_id, key)
        assert connection._register_flow_subscription_epoch(key)
    connection._start_writer(maxsize=512, enabled=True)
    yield connection
    await connection._stop_writer()
    connection._cleanup_transport()
    get_registry().unregister(connection.conn_id)
    assert not connection._pending_flow
    assert connection._transport_bytes == 0
    assert get_transport_budget().used == baseline


async def send(conn, key, sequence, text="x"):
    await conn.send_event("session.event.text_delta", {
        "session_key": key, "stream_generation": "g", "stream_seq": sequence, "text": text,
    })


def events(conn):
    return [frame for frame in conn.ws.frames if frame.get("event") == "session.event.text_delta"]


def ack(conn, key, delivery):
    conn.apply_session_flow_update_v2({
        "connection_epoch": conn._flow.epoch,
        "consumed": [{
            "subscription_epoch": conn._subscriptions.get_message_subscription_epoch(
                conn.conn_id, key,
            ),
            "through_delivery_id": delivery,
        }],
    })


async def test_burst_above_connection_window_waits_and_drains_exactly_once(conn):
    for sequence in range(1, 401):
        await send(conn, "A", sequence)
    assert conn._flow.next_id == FLOW_WINDOW_FRAMES + 1
    assert sum(map(len, conn._pending_flow.values())) == 400 - FLOW_WINDOW_FRAMES
    assert conn._transport_bytes == (
        sum(item.size for item in conn._flow.deliveries.values())
        + sum(frame.budget_bytes for queue in conn._pending_flow.values() for frame in queue)
    )
    await asyncio.sleep(0)
    while conn._flow.deliveries:
        ack(conn, "A", max(conn._flow.deliveries))
        await asyncio.sleep(0)
    assert [frame["payload"]["stream_seq"] for frame in events(conn)] == list(range(1, 401))
    assert [frame["seq"] for frame in events(conn)] == list(range(1, 401))
    assert not conn._flow.dirty and not conn._pending_flow and not conn.ws.closed
    assert conn._transport_bytes == 0


async def test_pending_lanes_take_turns_when_shared_credit_returns(conn):
    for sequence in range(1, FLOW_WINDOW_FRAMES + 3):
        await send(conn, "A", sequence)
    for sequence in range(1, 4):
        await send(conn, "B", sequence)
    await asyncio.sleep(0)
    ack(conn, "A", FLOW_WINDOW_FRAMES)
    await asyncio.sleep(0)
    assert [frame["payload"]["session_key"] for frame in events(conn)[FLOW_WINDOW_FRAMES:]] == [
        "A", "B", "A", "B", "B",
    ]
    assert not conn._flow.dirty


async def test_pending_payload_and_metadata_are_frozen_before_producer_returns(conn):
    for sequence in range(1, FLOW_WINDOW_FRAMES + 1):
        await send(conn, "A", sequence)
    payload = {"session_key": "A", "stream_seq": 129, "text": "original"}
    meta = {"source": {"label": "original"}}
    await conn.send_event("session.event.text_delta", payload, meta=meta)
    payload["text"] = "changed after enqueue"
    meta["source"]["label"] = "changed after enqueue"
    await asyncio.sleep(0)
    ack(conn, "A", FLOW_WINDOW_FRAMES)
    await asyncio.sleep(0)
    assert events(conn)[-1]["payload"]["text"] == "original"
    assert events(conn)[-1]["meta"]["source"]["label"] == "original"


@pytest.mark.parametrize("size", [1_100_000, 5_000_000])
async def test_valid_large_frame_waits_for_empty_window_without_starvation(conn, size):
    await send(conn, "A", 1, "first")
    await send(conn, "B", 1, "z" * size)
    for sequence in range(2, 5):
        await send(conn, "A", sequence)
    await asyncio.sleep(0)
    if size > 4 * 1024 * 1024:
        assert len(events(conn)) == 1
        ack(conn, "A", 1)
        await asyncio.sleep(0)
        assert events(conn)[1]["payload"]["session_key"] == "B"
        ack(conn, "B", events(conn)[1]["meta"]["flow"]["delivery_id"])
        await asyncio.sleep(0)
    assert [frame["payload"]["text"] for frame in events(conn)
            if frame["payload"]["session_key"] == "B"] == ["z" * size]
    assert not conn._flow.dirty and not conn._pending_flow


async def test_control_burst_preserves_fifo_and_precedes_queued_stream(conn):
    for sequence in range(1, 12):
        await send(conn, "A", sequence)
    for index in range(64):
        await conn.send_res(
            make_ok_res(str(index), {"accepted": True}),
            transport_control=True, transport_priority=True,
        )
    await asyncio.sleep(0)
    assert [frame.get("id") for frame in conn.ws.frames[:64]] == [str(i) for i in range(64)]
    assert [frame["seq"] for frame in events(conn)] == list(range(1, 12))
    assert not conn.ws.closed and not conn._closing
    assert conn._flow_control_frames == 0


async def test_ordered_control_proof_does_not_overtake_replay_prefix(conn):
    await send(conn, "A", 1, "replayed prefix")
    await conn.send_res(make_ok_res("resume", {"replay_to_seq": 1}), transport_control=True)
    await asyncio.sleep(0)
    assert conn.ws.frames[0]["event"] == "session.event.text_delta"
    assert conn.ws.frames[1]["id"] == "resume"


async def test_legacy_resume_control_reply_keeps_replay_ordering_fence(conn):
    await send(conn, "A", 1, "legacy replay prefix")
    dispatcher = AsyncMock()
    dispatcher.dispatch.return_value = make_ok_res("legacy-resume", {"dirty_keys": []})
    await _dispatch_request(
        conn, dispatcher, "legacy-resume", "transport.flow.update",
        {"resume": [{"key": "A"}]}, RpcContext(conn_id=conn.conn_id),
    )
    await asyncio.sleep(0)
    assert conn.ws.frames[0]["event"] == "session.event.text_delta"
    assert conn.ws.frames[1]["id"] == "legacy-resume"


async def test_retire_releases_pending_and_old_epoch_cannot_ack_replacement(conn):
    for sequence in range(1, 151):
        await send(conn, "A", sequence)
    old_epoch = conn._subscriptions.get_message_subscription_epoch(conn.conn_id, "A")
    receipt = conn._subscriptions.unsubscribe_messages(conn.conn_id, "A")
    assert not conn._pending_flow
    conn.apply_session_flow_update_v2({
        "connection_epoch": conn._flow.epoch,
        "discarded_lanes": [{name: receipt[name] for name in (
            "subscription_epoch", "retire_token", "final_published_id",
        )}],
    })
    await asyncio.sleep(0)
    assert not events(conn) and conn._transport_bytes == 0
    conn._subscriptions.subscribe_messages(conn.conn_id, "A")
    assert conn._register_flow_subscription_epoch("A")
    await send(conn, "A", 1, "replacement")
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="not current"):
        conn.apply_session_flow_update_v2({
            "connection_epoch": conn._flow.epoch,
            "consumed": [{"subscription_epoch": old_epoch, "through_delivery_id": 128}],
        })
    assert [frame["payload"]["text"] for frame in events(conn)] == ["replacement"]


async def test_ack_during_physical_send_drains_after_write_unwinds(conn):
    gate = asyncio.Event()
    conn.ws.first_send_gate = gate
    for sequence in range(1, FLOW_WINDOW_FRAMES + 2):
        await send(conn, "A", sequence)
    await asyncio.sleep(0)
    assert conn._flow.deliveries[1].sending
    ack(conn, "A", 1)
    assert conn._pending_flow  # Physical send still owns its reservation.
    gate.set()
    await asyncio.sleep(0)
    assert not conn._pending_flow
    assert len(events(conn)) == FLOW_WINDOW_FRAMES + 1
    assert not conn._flow.dirty


async def test_recovery_takeover_releases_pending_suffix(conn):
    for sequence in range(1, 151):
        await send(conn, "A", sequence)
    assert conn._pending_flow
    conn._mark_flow_dirty({"session_key": "A", "stream_generation": "g", "stream_seq": 150})
    assert not conn._pending_flow
    await asyncio.sleep(0)
    assert len(events(conn)) == FLOW_WINDOW_FRAMES
    assert conn._transport_bytes == sum(item.size for item in conn._flow.deliveries.values())
