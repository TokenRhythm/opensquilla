"""Consumption credit is independent from successful socket writes."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

from opensquilla.gateway.transport_flow import (
    FLOW_WINDOW_FRAMES,
    MAX_WIRE_BYTES,
    FlowWindow,
    TransportBudget,
    get_transport_budget,
)
from opensquilla.gateway.websocket import SubscriptionManager, WsConnection


def test_only_sent_current_epoch_ack_releases_credit() -> None:
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release)
    delivery_id = flow.admit(120)
    assert delivery_id == 1
    with pytest.raises(ValueError, match="unsent"):
        flow.acknowledge(flow.epoch, 1)
    flow.mark_sent(1)
    with pytest.raises(ValueError, match="epoch"):
        flow.acknowledge("old", 1)
    with pytest.raises(ValueError, match="ahead"):
        flow.acknowledge(flow.epoch, 2)
    assert budget.used == 120
    flow.acknowledge(flow.epoch, 1)
    flow.acknowledge(flow.epoch, 1)
    assert budget.used == 0


def test_large_frame_is_admitted_when_ordinary_window_is_empty() -> None:
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release)
    small = flow.admit(1)
    assert flow.admit(MAX_WIRE_BYTES) is None
    flow.mark_sent(small)
    flow.acknowledge(flow.epoch, small)
    large = flow.admit(MAX_WIRE_BYTES)
    assert large is not None
    assert flow.admit(1) is None
    flow.mark_sent(large)
    flow.acknowledge(flow.epoch, large)
    assert budget.used == 0


def test_wire_limit_does_not_count_internal_sequence_reservation_margin() -> None:
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release)
    assert flow.admit(MAX_WIRE_BYTES + 32, wire_size=MAX_WIRE_BYTES) is not None
    flow.close()
    assert flow.admit(MAX_WIRE_BYTES + 32, wire_size=MAX_WIRE_BYTES + 1) is None
    assert budget.used == 0


def test_snapshot_bootstrap_is_bounded_even_with_zero_ordinary_credit() -> None:
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release)
    for _ in range(FLOW_WINDOW_FRAMES):
        assert flow.admit(20) is not None
    assert flow.admit(20) is None
    assert flow.admit(300_000, recovery=True) is not None
    assert flow.admit(300_000, recovery=True) is None
    flow.close()
    assert budget.used == 0


def test_snapshot_staging_passes_ordinary_ack_hole_without_unbounded_tombstones() -> None:
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release)
    first = flow.admit(20)
    flow.mark_sent(first)
    for _ in range(1024):
        recovery = flow.admit(300_000, recovery=True)
        flow.mark_sent(recovery)
        flow.stage(flow.epoch, recovery)
        flow.stage(flow.epoch, recovery)
        assert flow.ack_id == 0
        assert len(flow.deliveries) == 1
        assert budget.used == 20
    with pytest.raises(ValueError, match="not a recovery"):
        flow.stage(flow.epoch, first)
    flow.acknowledge(flow.epoch, recovery)
    assert budget.used == 0


def test_ack_during_send_drain_retains_bytes_until_write_finishes() -> None:
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release)
    delivery_id = flow.admit(120)
    flow.mark_sending(delivery_id)
    flow.acknowledge(flow.epoch, delivery_id)
    assert flow.ack_id == delivery_id
    assert budget.used == 120
    flow.mark_sent(delivery_id)
    assert budget.used == 0
    recovery = flow.admit(300_000, recovery=True)
    flow.mark_sending(recovery)
    flow.stage(flow.epoch, recovery)
    assert flow.admit(1, recovery=True) is None
    assert budget.used == 300_000
    flow.mark_sent(recovery)
    assert budget.used == 0


def test_unsent_or_future_staging_cannot_release_bytes() -> None:
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release)
    recovery = flow.admit(300_000, recovery=True)
    with pytest.raises(ValueError, match="unsent"):
        flow.stage(flow.epoch, recovery)
    with pytest.raises(ValueError, match="not a recovery"):
        flow.stage(flow.epoch, recovery + 1)
    assert budget.used == 300_000
    flow.close()


def test_valid_4096_character_session_key_does_not_latch_global_dirty() -> None:
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release)
    key = "s" * 4096
    assert flow.mark_dirty(key)
    assert flow.status()["dirty_keys"] == [key]
    assert not flow.global_dirty


class _FastSocket:
    client_state = application_state = WebSocketState.CONNECTED
    client = SimpleNamespace(host="127.0.0.1", port=12345)

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.closed: list[int] = []

    async def send_text(self, text: str) -> None:
        self.frames.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append(code)


@pytest.mark.parametrize("closing", [False, True])
async def test_dequeued_control_reservation_released_when_transport_already_closed(closing):
    before = get_transport_budget().used
    ws = _FastSocket()
    conn = WsConnection("dequeued-close", ws)  # type: ignore[arg-type]
    conn._enable_flow()
    conn._start_writer(maxsize=512, enabled=True)
    await conn.send_raw_text('{"type":"pong"}')
    assert conn._transport_bytes > 0
    if closing:
        conn._closing = True
    else:
        ws.client_state = WebSocketState.DISCONNECTED
    await conn._writer_task
    await conn._stop_writer()
    conn._cleanup_transport()
    assert conn._transport_bytes == 0
    assert get_transport_budget().used == before


async def test_exact_25_mib_event_is_sent_instead_of_permanently_dirty():
    from opensquilla.gateway.protocol import make_event

    class MeasuringSocket(_FastSocket):
        wire_sizes: list[int] = []

        async def send_text(self, text: str) -> None:
            self.wire_sizes.append(len(text.encode("utf-8")))

    ws = MeasuringSocket()
    conn = WsConnection("exact-wire-limit", ws)  # type: ignore[arg-type]
    conn._enable_flow()
    conn._seq = 9  # Cover the additional digit in writer-assigned seq=10.
    conn._start_writer(maxsize=512, enabled=True)
    payload = {"session_key": "s", "text": ""}
    meta = {"flow": {"delivery_epoch": conn._flow.epoch, "delivery_id": 1}}
    overhead = len(
        make_event("session.event.text_delta", payload, seq=10, meta=meta).model_dump_json()
    )
    payload["text"] = "x" * (MAX_WIRE_BYTES - overhead)
    try:
        await conn.send_event("session.event.text_delta", payload)
        await asyncio.sleep(0)
        assert ws.wire_sizes == [MAX_WIRE_BYTES]
        assert not conn._flow.dirty
        assert len(conn._flow.deliveries) == 1
        assert not ws.closed
        conn._flow.acknowledge(conn._flow.epoch, 1)
    finally:
        await conn._stop_writer()
        conn._cleanup_transport()


async def test_fast_socket_does_not_bypass_renderer_credit_and_other_peer_remains_fast() -> None:
    before = get_transport_budget().used
    slow_socket, fast_socket = _FastSocket(), _FastSocket()
    slow = WsConnection("frozen-renderer", slow_socket)  # type: ignore[arg-type]
    fast = WsConnection("healthy-renderer", fast_socket)  # type: ignore[arg-type]
    subscriptions = SubscriptionManager()
    for conn in (slow, fast):
        conn._subscriptions = subscriptions
        subscriptions.subscribe_messages(conn.conn_id, "session")
        conn._enable_flow()
        conn._start_writer(maxsize=512, enabled=True)
    try:
        for sequence in range(1, 10_001):
            await slow.send_event(
                "session.event.text_delta",
                {
                    "session_key": "session",
                    "stream_generation": "g",
                    "stream_seq": sequence,
                    "text": "x",
                },
            )
        await asyncio.sleep(0)
        data = [
            frame
            for frame in slow_socket.frames
            if frame.get("event") == "session.event.text_delta"
        ]
        assert len(data) == FLOW_WINDOW_FRAMES
        assert slow_socket.closed == []
        assert len(slow._flow.deliveries) == FLOW_WINDOW_FRAMES
        assert slow._flow.dirty["session"] == ("g", 10_000)
        assert (
            len(
                [
                    frame
                    for frame in slow_socket.frames
                    if frame.get("event") == "transport.flow.dirty"
                ]
            )
            == 1
        )
        for sequence in range(1, 401):
            await fast.send_event(
                "session.event.text_delta",
                {
                    "session_key": "session",
                    "stream_generation": "g",
                    "stream_seq": sequence,
                    "text": "x",
                },
            )
            await asyncio.sleep(0)
            receipt = fast_socket.frames[-1]["meta"]["flow"]
            fast.apply_flow_update(
                {
                    "delivery_epoch": receipt["delivery_epoch"],
                    "ack_delivery_id": receipt["delivery_id"],
                }
            )
        assert len(fast_socket.frames) == 400
        assert not fast._flow.dirty
        assert not fast_socket.closed
    finally:
        for conn in (slow, fast):
            await conn._stop_writer()
            conn._cleanup_transport()
    assert get_transport_budget().used == before


async def test_bad_event_is_dirty_without_poisoning_the_writer() -> None:
    ws = _FastSocket()
    conn = WsConnection("bad-event", ws)  # type: ignore[arg-type]
    conn._enable_flow()
    conn._start_writer(maxsize=512, enabled=True)
    try:
        await conn.send_event("session.event.text_delta", {"session_key": "a", "text": object()})
        await conn.send_raw_text('{"type":"pong"}')
        await asyncio.sleep(0)
        assert "a" in conn._flow.dirty
        assert any(frame["type"] == "pong" for frame in ws.frames)
        assert ws.closed == []
    finally:
        await conn._stop_writer()
        conn._cleanup_transport()


async def test_dirty_stream_does_not_emit_one_control_notice_per_later_token() -> None:
    ws = _FastSocket()
    conn = WsConnection("already-dirty", ws)  # type: ignore[arg-type]
    conn._enable_flow()
    conn._start_writer(maxsize=512, enabled=True)
    try:
        conn._mark_flow_dirty({"session_key": "s", "stream_seq": 1})
        await asyncio.sleep(0)
        for sequence in range(2, 302):
            await conn.send_event(
                "session.event.text_delta", {"session_key": "s", "stream_seq": sequence}
            )
            await asyncio.sleep(0)
        assert len(ws.frames) == 1
        assert ws.frames[0]["event"] == "transport.flow.dirty"
        assert "ack_delivery_id" not in ws.frames[0]["payload"]
        assert conn._flow.dirty["s"][1] == 301
    finally:
        await conn._stop_writer()
        conn._cleanup_transport()


@pytest.mark.parametrize("character,count,global_notice", [("中", 24, False), ("\x00", 128, True)])
async def test_long_dirty_keys_keep_encoded_control_bytes_accounted_and_pong_available(
    character,
    count,
    global_notice,
):
    before = get_transport_budget().used

    class AccountedSocket(_FastSocket):
        async def send_text(self, text: str) -> None:
            # Check while the active writer still owns its reservation.
            assert len(text.encode("utf-8")) <= conn._flow_control_bytes
            await super().send_text(text)

    ws = AccountedSocket()
    conn = WsConnection("unicode-dirty", ws)  # type: ignore[arg-type]
    conn._enable_flow()
    conn._start_writer(maxsize=512, enabled=True)
    try:
        for index in range(count):
            conn._mark_flow_dirty({"session_key": f"{index:03}" + character * 4093})
        await conn.send_raw_text('{"type":"pong"}')
        await asyncio.sleep(0)
        assert ws.frames[0]["payload"]["global_dirty"] is global_notice
        assert len(ws.frames[0]["payload"]["dirty_keys"]) == (0 if global_notice else count)
        assert ws.frames[-1] == {"type": "pong"}
        assert len(conn._flow.dirty) == count
        assert not conn._flow.global_dirty  # Compact notice is not a permanent barrier.
        assert conn._flow_control_bytes == 0
        assert not ws.closed
    finally:
        await conn._stop_writer()
        conn._cleanup_transport()
    assert get_transport_budget().used == before


async def test_resume_replays_only_complete_tail_on_the_existing_subscription(monkeypatch) -> None:
    from opensquilla.gateway import session_streams

    streams = session_streams.SessionStreamRegistry(stream_generation="g")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    for text in ("base", "tail-1", "tail-2"):
        streams.record("s", "session.event.text_delta", {"text": text})
    ws = _FastSocket()
    conn = WsConnection("resume", ws)  # type: ignore[arg-type]
    conn._subscriptions = SubscriptionManager()
    conn._subscriptions.subscribe_messages(conn.conn_id, "s")
    conn._enable_flow()
    conn._start_writer(maxsize=512, enabled=True)
    closed: list[bool] = []
    conn._snapshot_transfer = SimpleNamespace(
        matches_install=lambda value: not closed and value.get("snapshot_id") == "snapshot",
        close=lambda: closed.append(True),
    )
    try:
        conn._flow.mark_dirty("s", "g", 3)
        update = {
            "delivery_epoch": conn._flow.epoch,
            "ack_delivery_id": 0,
            "resume": [
                {
                    "key": "s",
                    "snapshot_id": "snapshot",
                    "sync_revision": "r",
                    "stream_generation": "g",
                    "stream_seq": 1,
                }
            ],
        }
        result = conn.apply_flow_update(update)
        await asyncio.sleep(0)
        assert result["dirty_keys"] == []
        assert [frame["payload"]["text"] for frame in ws.frames] == ["tail-1", "tail-2"]
        assert conn.conn_id in conn._subscriptions.get_message_subscribers("s")
        assert closed == [True]
        assert ws.closed == []
        assert conn.apply_flow_update(update) == result
        assert conn.apply_flow_update({**update, "dirty_keys": ["s"]}) == result
        await asyncio.sleep(0)
        assert len(ws.frames) == 2  # Lost response retry does not replay twice.
        assert closed == [True]
        conn._subscriptions.unsubscribe_messages(conn.conn_id, "s")
        conn._subscriptions.subscribe_messages(conn.conn_id, "s")
        with pytest.raises(ValueError, match="not current"):
            conn.apply_flow_update(update)
    finally:
        await conn._stop_writer()
        conn._cleanup_transport()


async def test_incomplete_tail_remains_dirty_instead_of_skipping_missing_text(monkeypatch) -> None:
    from opensquilla.gateway import session_streams

    streams = session_streams.SessionStreamRegistry(max_events_per_session=2, stream_generation="g")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    for index in range(8):
        streams.record("s", "session.event.text_delta", {"text": str(index)})
    ws = _FastSocket()
    conn = WsConnection("gap", ws)  # type: ignore[arg-type]
    conn._subscriptions = SubscriptionManager()
    conn._subscriptions.subscribe_messages(conn.conn_id, "s")
    conn._enable_flow()
    conn._start_writer(maxsize=512, enabled=True)
    conn._snapshot_transfer = SimpleNamespace(
        matches_install=lambda value: True, close=lambda: None
    )
    try:
        conn._flow.mark_dirty("s", "g", 8)
        result = conn.apply_flow_update(
            {
                "delivery_epoch": conn._flow.epoch,
                "ack_delivery_id": 0,
                "resume": [
                    {
                        "key": "s",
                        "snapshot_id": "snapshot",
                        "sync_revision": "r",
                        "stream_generation": "g",
                        "stream_seq": 1,
                    }
                ],
            }
        )
        await asyncio.sleep(0)
        assert result["dirty_keys"] == ["s"]
        assert [frame["event"] for frame in ws.frames] == ["transport.flow.dirty"]
        assert ws.closed == []
    finally:
        await conn._stop_writer()
        conn._cleanup_transport()


def test_invalid_session_update_cannot_release_delivery_credit() -> None:
    conn = WsConnection("authority", _FastSocket())  # type: ignore[arg-type]
    conn._subscriptions = SubscriptionManager()
    conn._enable_flow()
    delivery_id = conn._flow.admit(100)
    conn._flow.mark_sent(delivery_id)
    try:
        with pytest.raises(ValueError, match="not subscribed"):
            conn.apply_flow_update(
                {
                    "delivery_epoch": conn._flow.epoch,
                    "ack_delivery_id": delivery_id,
                    "dirty_keys": ["another-session"],
                }
            )
        assert conn._flow.ack_id == 0
        assert conn._transport_bytes == 100
    finally:
        conn._cleanup_transport()


async def test_global_barrier_requires_every_live_lease_and_retired_key_does_not_block_ack(
    monkeypatch,
):
    from opensquilla.gateway import session_streams, websocket

    streams = session_streams.SessionStreamRegistry(stream_generation="global-g")
    monkeypatch.setattr(session_streams, "_session_streams", streams)
    registry = websocket.ConnectionRegistry()
    monkeypatch.setattr(websocket, "_registry", registry)
    conn = WsConnection("global-owner", _FastSocket())  # type: ignore[arg-type]
    conn._subscriptions = SubscriptionManager()
    registry.register(conn)
    conn._enable_flow()
    conn._start_writer(maxsize=512, enabled=True)
    keys = [f"s-{index}" for index in range(129)]
    try:
        for key in keys:
            conn._subscriptions.subscribe_messages(conn.conn_id, key)
            conn._mark_flow_dirty({"session_key": key})
        assert conn._flow.global_dirty
        assert len(conn._flow.dirty) == 128
        revision = conn._flow.global_revision

        def resume(key):
            conn._snapshot_transfer = SimpleNamespace(
                matches_install=lambda value: value["key"] == key,
                close=lambda: None,
            )
            return conn.apply_flow_update(
                {
                    "delivery_epoch": conn._flow.epoch,
                    "ack_delivery_id": 0,
                    "resume": [
                        {
                            "key": key,
                            "snapshot_id": f"snapshot-{key}-{conn._flow.global_revision}",
                            "sync_revision": "r",
                            "stream_generation": "global-g",
                            "stream_seq": 0,
                        }
                    ],
                }
            )

        assert resume(keys[0])["global_dirty"]
        assert conn._subscriptions.get_message_flow_revision(conn.conn_id, keys[0]) == revision
        # A new unknown loss cannot be covered by the old partially completed
        # revision. A newly added lease also starts without a proof.
        conn._mark_flow_dirty({})
        assert conn._flow.global_revision > revision
        conn._subscriptions.subscribe_messages(conn.conn_id, "new-lease")
        for key in keys:
            result = resume(key)
        assert result["global_dirty"]  # A cannot speak for newly added B.
        assert resume("new-lease")["global_dirty"] is False

        conn._mark_flow_dirty({"session_key": keys[0]})
        conn._mark_flow_dirty({"session_key": keys[1]})
        conn._subscriptions.unsubscribe_messages(conn.conn_id, keys[1])
        assert keys[1] not in conn._flow.dirty
        assert keys[1] not in conn._flow_installed
        ordinary = conn._flow.admit(100)
        conn._flow.mark_sent(ordinary)
        result = conn.apply_flow_update(
            {
                "delivery_epoch": conn._flow.epoch,
                "ack_delivery_id": ordinary,
            }
        )
        assert result["dirty_keys"] == [keys[0]]
        assert result["ack_delivery_id"] == ordinary
    finally:
        await conn._stop_writer()
        conn._cleanup_transport()
        registry.unregister(conn.conn_id)


async def test_unsubscribe_can_finish_global_barrier_but_not_another_unrecovered_lease(monkeypatch):
    from opensquilla.gateway import websocket

    registry = websocket.ConnectionRegistry()
    monkeypatch.setattr(websocket, "_registry", registry)
    conn = WsConnection("retired-global", _FastSocket())  # type: ignore[arg-type]
    conn._subscriptions = SubscriptionManager()
    registry.register(conn)
    conn._enable_flow()
    for key in ("a", "b"):
        conn._subscriptions.subscribe_messages(conn.conn_id, key)
    conn._flow.mark_global_dirty()
    try:
        conn._subscriptions.unsubscribe_messages(conn.conn_id, "a")
        assert conn._flow.global_dirty
        conn._subscriptions.unsubscribe_messages(conn.conn_id, "b")
        assert not conn._flow.global_dirty
    finally:
        conn._cleanup_transport()
        registry.unregister(conn.conn_id)
