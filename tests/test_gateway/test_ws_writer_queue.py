"""Per-connection WebSocket writer queue tests.

Covers the per-connection writer queue invariants for outbound ordering,
backpressure, and terminal cleanup.

Categories:
    - Unit: direct WsConnection behavior with a fake socket.
    - Observability: structured-log assertions via
      structlog.testing.capture_logs.
    - Integration: writer task lifecycle on disconnect-equivalent.

"""
from __future__ import annotations

import asyncio
import json

import pytest
import structlog
from starlette.websockets import WebSocketState

import opensquilla.gateway.websocket as websocket_module
from opensquilla.contracts.gateway_transport import ANSWER_GENERATION_RESET_CAPABILITY
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.protocol import make_event, make_ok_res
from opensquilla.gateway.websocket import (
    _LOSSY_EVENTS,
    _SENTINEL_STOP,
    WsConnection,
    _OutboundFrame,
)

# ---------------------------------------------------------------------------
# Fake WebSocket helpers
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """Minimal Starlette WebSocket stand-in for unit tests."""

    def __init__(self) -> None:
        self.client_state = WebSocketState.CONNECTED
        self.sent: list[str] = []
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self._send_event: asyncio.Event | None = None  # set => block send_text
        self._send_unblock: asyncio.Event | None = None  # set => unblock send_text
        self.client = None

    async def send_text(self, text: str) -> None:
        if self._send_event is not None and self._send_unblock is not None:
            self._send_event.set()
            await self._send_unblock.wait()
        self.sent.append(text)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.close_code = code
        self.close_reason = reason
        self.client_state = WebSocketState.DISCONNECTED


def _make_conn(*, enabled: bool = True, maxsize: int = 16) -> WsConnection:
    """Build a connected WsConnection with the writer task started."""
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id=f"test-{id(fake)}", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=maxsize, enabled=enabled)
    return conn


async def _flush_writer(conn: WsConnection, *, deadline: float = 1.0) -> None:
    """Wait until the writer's outbox is fully drained or deadline elapses."""
    end = asyncio.get_event_loop().time() + deadline
    while asyncio.get_event_loop().time() < end:
        if conn._outbox is None or conn._outbox.empty():
            await asyncio.sleep(0)
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_enabled", [False, True])
@pytest.mark.parametrize(
    ("client_capable", "principal_capable", "expected_event"),
    [
        (False, True, "session.event.error"),
        (True, False, "session.event.answer_generation_reset"),
    ],
)
async def test_terminal_reset_projection_uses_connect_caps_at_send_boundary(
    writer_enabled: bool,
    client_capable: bool,
    principal_capable: bool,
    expected_event: str,
) -> None:
    fake = _FakeWebSocket()
    conn = WsConnection(
        conn_id="cx-reset-projection",
        ws=fake,  # type: ignore[arg-type]
        client_caps=(
            frozenset({ANSWER_GENERATION_RESET_CAPABILITY})
            if client_capable
            else frozenset()
        ),
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read"}),
            is_owner=False,
            authenticated=True,
            capabilities=(
                frozenset({ANSWER_GENERATION_RESET_CAPABILITY})
                if principal_capable
                else frozenset({"host.execute"})
            ),
        ),
    )
    conn._start_writer(maxsize=8, enabled=writer_enabled)
    original = {
        "kind": "answer_generation_reset",
        "turn_id": "turn-reset",
        "assistant_message_id": "assistant-reset",
        "authoritative_text_snapshot": "partial fallback",
        "terminal": True,
        "terminal_text_snapshot": "The model could not complete this answer.",
        "terminal_error_message": "INTERNAL_MESSAGE",
        "terminal_error_code": "INTERNAL_CODE",
        "terminal_failure_kind": "INTERNAL_KIND",
        "session_key": "agent:main:reset",
        "stream_seq": 17,
    }
    payload = dict(original)
    try:
        await conn.send_event(
            "session.event.answer_generation_reset",
            payload,
            meta={"replayed": True},
        )
        if writer_enabled:
            await _flush_writer(conn)
        assert len(fake.sent) == 1
        frame = json.loads(fake.sent[0])
        assert frame["event"] == expected_event
        assert frame["payload"]["stream_seq"] == 17
        assert frame["meta"] == {"replayed": True}
        assert "terminal_error_message" not in frame["payload"]
        assert "terminal_error_code" not in frame["payload"]
        assert "terminal_failure_kind" not in frame["payload"]
        if expected_event == "session.event.error":
            assert frame["payload"]["message"] == original["terminal_text_snapshot"]
            assert frame["payload"]["code"] == "ensemble_fixed_error"
        else:
            assert "message" not in frame["payload"]
        # Per-client projection must never rewrite the canonical replay payload.
        assert payload == original
    finally:
        await conn._stop_writer()


# ---------------------------------------------------------------------------
# seq is minted at dequeue and is strictly monotonic + contiguous.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seq_minted_at_dequeue_is_monotonic_and_contiguous() -> None:
    conn = _make_conn(maxsize=32)
    try:
        for i in range(5):
            await conn.send_event("session.event.text_delta", {"chunk": str(i)})
        await _flush_writer(conn)

        assert len(conn.ws.sent) == 5  # type: ignore[attr-defined]
        seqs = [json.loads(line)["seq"] for line in conn.ws.sent]  # type: ignore[attr-defined]
        assert seqs == list(range(1, 6))
    finally:
        await conn._stop_writer()


# ---------------------------------------------------------------------------
# lossy drop emits gateway.ws_writer_drop log with full field set.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lossy_drop_log_has_full_field_set_for_tick() -> None:
    """Tick is the only LOSSY event. Its payload has no session_key / stream_seq.

    Asserts the drop log fields are present and null-safe.
    Strategy: maxsize=4, push 5 ticks synchronously (no yield) so the writer
    hasn't dequeued anything yet → 5th push hits QueueFull → eviction.
    """
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id="cx-tick", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=4, enabled=True)

    # Block the writer's first send so it never drains the queue.
    fake._send_event = asyncio.Event()
    fake._send_unblock = asyncio.Event()
    try:
        with structlog.testing.capture_logs() as logs:
            # Push 4 ticks synchronously (queue path has no inner await,
            # so no yield → writer doesn't run yet).
            for i in range(4):
                await conn.send_event("tick", {"time_ms": i})
            # Push 5th — queue is full (writer hasn't started). Triggers
            # same-kind eviction.
            await conn.send_event("tick", {"time_ms": 999})

        drop_logs = [
            entry for entry in logs if entry.get("event") == "gateway.ws_writer_drop"
        ]
        assert len(drop_logs) == 1, f"expected 1 drop, got: {drop_logs}"
        record = drop_logs[0]
        assert record["conn_id"] == "cx-tick"
        assert record["event_name"] == "tick"
        assert record["session_key"] is None
        assert record["stream_seq"] is None
        assert record["queue_depth"] >= 0
        assert record["eviction"] is True
        assert record["log_level"] == "warning"
    finally:
        if fake._send_unblock is not None:
            fake._send_unblock.set()
        await conn._stop_writer()


# ---------------------------------------------------------------------------
# CONTROL overflow triggers force-close 1011 with overflow_close log.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_overflow_triggers_force_close_with_overflow_log() -> None:
    """Push 5 text_delta synchronously into a maxsize=4 queue. The 5th
    overflows on a CONTROL frame → force_close(1011).
    """
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id="cx-overflow", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=4, enabled=True)

    fake._send_event = asyncio.Event()
    fake._send_unblock = asyncio.Event()
    try:
        with structlog.testing.capture_logs() as logs:
            # Fill the queue with 4 CONTROL frames (text_delta).
            for i in range(4):
                await conn.send_event(
                    "session.event.text_delta",
                    {"chunk": str(i), "session_key": "sess-A", "stream_seq": i + 1},
                )
            # 5th overflows — synchronous flow, writer hasn't yielded yet.
            await conn.send_event(
                "session.event.text_delta",
                {"chunk": "overflow", "session_key": "sess-A", "stream_seq": 999},
            )
            # Yield so the scheduled _force_close task gets to run.
            await asyncio.sleep(0.05)

        overflow_logs = [
            entry
            for entry in logs
            if entry.get("event") == "gateway.ws_writer_overflow_close"
        ]
        assert len(overflow_logs) == 1, f"expected 1 overflow, got: {overflow_logs}"
        record = overflow_logs[0]
        assert record["conn_id"] == "cx-overflow"
        assert record["event_name"] == "session.event.text_delta"
        assert record["session_key"] == "sess-A"
        assert record["stream_seq"] == 999
        assert record["queue_depth"] >= 0
        assert record["log_level"] == "error"
        assert conn._closing is True
    finally:
        if fake._send_unblock is not None:
            fake._send_unblock.set()
        await asyncio.sleep(0.1)
        await conn._stop_writer()
    # Force-close should have invoked ws.close with code 1011.
    assert fake.close_code == 1011
    assert fake.close_reason == "writer_backpressure"


# ---------------------------------------------------------------------------
# writer cancel during blocked send completes within bounded budget.
# ---------------------------------------------------------------------------


class _ShieldedFakeWebSocket(_FakeWebSocket):
    """Simulates a TCP-wedged socket: send_text shields itself from cancel.

    A real OS-level TCP send buffer that is full will not respond to
    asyncio cancellation within a bounded time. We simulate this by
    having send_text shield the inner await, so a writer task that is
    cancelled will not actually unblock its send_text call. This is the
    failure mode that ``_force_close``'s ``wait_for(2.0)`` budget guards.
    """

    async def send_text(self, text: str) -> None:
        if self._send_event is not None and self._send_unblock is not None:
            self._send_event.set()
            # asyncio.shield prevents inner awaitable from being cancelled.
            try:
                await asyncio.shield(self._send_unblock.wait())
            except asyncio.CancelledError:
                # Outer cancel propagated, but the shielded inner is still
                # waiting on the never-set event. Re-raise so the writer
                # exits — but this only happens after cancel + the
                # shielded waiter is also cancelled by force_close GC.
                # For the test's purpose we want to simulate "stuck even
                # after cancel" — keep waiting until unblock is set.
                if not self._send_unblock.is_set():
                    await self._send_unblock.wait()
                raise
        self.sent.append(text)


@pytest.mark.asyncio
async def test_writer_cancel_during_blocked_send_within_budget() -> None:
    """When ws.send_text is genuinely wedged (cancel cannot propagate),
    ``_force_close`` still completes within ~3s due to wait_for(2.0).

    gateway.ws_writer_force_close_timeout is emitted with conn_id and reason.
    """
    fake = _ShieldedFakeWebSocket()
    conn = WsConnection(conn_id="cx-stuck", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=4, enabled=True)

    fake._send_event = asyncio.Event()
    fake._send_unblock = asyncio.Event()  # never set during the test

    try:
        await conn.send_event("session.event.text_delta", {"chunk": "x"})
        await asyncio.wait_for(fake._send_event.wait(), timeout=1.0)

        with structlog.testing.capture_logs() as logs:
            start = asyncio.get_event_loop().time()
            await conn._force_close(reason="test_stuck", code=1011)
            elapsed = asyncio.get_event_loop().time() - start

        # Bounded by wait_for(2.0) + close overhead. Allow up to 3s.
        assert elapsed < 3.0, f"force_close took {elapsed:.2f}s, exceeded 3s"
        timeout_logs = [
            entry
            for entry in logs
            if entry.get("event") == "gateway.ws_writer_force_close_timeout"
        ]
        assert len(timeout_logs) == 1, f"expected 1 timeout log, got: {timeout_logs}"
        assert timeout_logs[0]["conn_id"] == "cx-stuck"
        assert timeout_logs[0]["reason"] == "test_stuck"
    finally:
        # Unblock the shielded waiter so leaked task can exit during teardown.
        fake._send_unblock.set()
        conn._writer_task = None


# ---------------------------------------------------------------------------
# kill switch disabled -> no writer task, legacy direct-send used.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_switch_disabled_uses_legacy_direct_send() -> None:
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id="cx-legacy", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=4, enabled=False)

    assert conn._writer_task is None
    assert conn._outbox is None
    assert conn._queue_enabled is False

    await conn.send_event("session.event.text_delta", {"chunk": "x"})
    # Direct-send path: payload should already be on the wire.
    assert len(fake.sent) == 1
    payload = json.loads(fake.sent[0])
    assert payload["seq"] == 1


@pytest.mark.asyncio
async def test_kill_switch_direct_send_timeout_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeWebSocket()
    fake._send_event = asyncio.Event()
    fake._send_unblock = asyncio.Event()
    conn = WsConnection(conn_id="cx-legacy-timeout", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=4, enabled=False)
    monkeypatch.setattr(websocket_module, "_DIRECT_SEND_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            conn.send_event("session.event.text_delta", {"chunk": "x"}),
            timeout=0.5,
        )

    assert conn._closing is True
    assert fake.close_code == 1011
    assert fake.close_reason == "direct_send_timeout"


@pytest.mark.asyncio
async def test_cancellation_resistant_direct_send_is_bounded_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _ShieldedFakeWebSocket()
    fake._send_event = asyncio.Event()
    fake._send_unblock = asyncio.Event()
    conn = WsConnection(conn_id="cx-legacy-shielded", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=4, enabled=False)
    monkeypatch.setattr(websocket_module, "_DIRECT_SEND_TIMEOUT_SECONDS", 0.01)

    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                conn.send_event("session.event.text_delta", {"chunk": "x"}),
                timeout=0.5,
            )
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 0.5
        assert conn._closing is True
        assert fake.close_code == 1011

        # The cancellation-resistant socket still owns the original low-level
        # send, but the connection is terminal and cannot start a second one.
        await conn.send_event("session.event.text_delta", {"chunk": "late"})
        await conn.send_res(make_ok_res("late-response", {"ok": True}))
        await conn.send_raw_text('{"type":"pong"}')
        assert fake._send_event.is_set() is True
        assert fake.sent == []
    finally:
        fake._send_unblock.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_raw_pong_uses_the_connection_writer_queue() -> None:
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id="cx-raw-pong", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=4, enabled=True)
    try:
        await conn.send_raw_text('{"type":"pong"}')
        await _flush_writer(conn)
        assert fake.sent == ['{"type":"pong"}']
    finally:
        await conn._stop_writer()


@pytest.mark.asyncio
async def test_stopped_writer_refuses_event_response_and_raw_fallback_sends() -> None:
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id="cx-stopped", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=4, enabled=True)
    await conn._stop_writer()

    await conn.send_event("session.event.text_delta", {"chunk": "late"})
    await conn.send_res(make_ok_res("late-response", {"ok": True}))
    await conn.send_raw_text('{"type":"pong"}')

    assert conn._closing is True
    assert fake.sent == []


# ---------------------------------------------------------------------------
# pre-auth direct-send (writer not started) uses legacy path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_auth_direct_send_when_writer_not_started() -> None:
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id="cx-preauth", ws=fake)  # type: ignore[arg-type]
    # NOTE: not calling _start_writer.

    assert conn._writer_task is None
    assert conn._outbox is None

    await conn.send_event("connect.challenge", {"nonce": "abc"})
    # Direct-send path: payload should already be on the wire.
    assert len(fake.sent) == 1
    payload = json.loads(fake.sent[0])
    assert payload["event"] == "connect.challenge"
    assert payload["seq"] == 1


# ---------------------------------------------------------------------------
# same-kind eviction for tick (replaces oldest tick, not other event).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_kind_eviction_only_drops_same_kind() -> None:
    """Mix 3 text_delta + 1 tick to fill maxsize=4. Overflow with another
    tick must evict the queued tick (same kind), preserving text_deltas.
    """
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id="cx-evict", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=4, enabled=True)

    fake._send_event = asyncio.Event()
    fake._send_unblock = asyncio.Event()
    try:
        # Synchronous fill: 3 text_delta + 1 tick = 4 items (queue full).
        # No yield happens during these pushes (queue path is sync).
        await conn.send_event(
            "session.event.text_delta",
            {"chunk": "b", "session_key": "s", "stream_seq": 2},
        )
        await conn.send_event(
            "session.event.text_delta",
            {"chunk": "c", "session_key": "s", "stream_seq": 3},
        )
        await conn.send_event("tick", {"time_ms": 100})
        await conn.send_event(
            "session.event.text_delta",
            {"chunk": "d", "session_key": "s", "stream_seq": 4},
        )

        # Overflow with another tick: should evict the queued tick.
        with structlog.testing.capture_logs() as logs:
            await conn.send_event("tick", {"time_ms": 200})

        drop_logs = [
            entry
            for entry in logs
            if entry.get("event") == "gateway.ws_writer_drop"
        ]
        assert len(drop_logs) == 1
        assert drop_logs[0]["event_name"] == "tick"
        assert drop_logs[0]["eviction"] is True

        # Queue should still contain 3 text_deltas + 1 tick (the new one).
        backing = list(conn._outbox._queue)  # type: ignore[union-attr]
        kinds = [item.kind for item in backing if isinstance(item, _OutboundFrame)]
        assert kinds.count("event:tick") == 1
        assert kinds.count("event:session.event.text_delta") == 3
    finally:
        if fake._send_unblock is not None:
            fake._send_unblock.set()
        await conn._stop_writer()


# ---------------------------------------------------------------------------
# drop log null-safe extraction for tick payload missing fields.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drop_log_null_safe_for_tick_payload() -> None:
    """Tick payload has only {time_ms}; session_key / stream_seq must be None
    in the drop log fields.
    """
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id="cx-null", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=2, enabled=True)

    fake._send_event = asyncio.Event()
    fake._send_unblock = asyncio.Event()
    try:
        # Synchronous fill to maxsize=2, then 1 more triggers eviction.
        await conn.send_event("tick", {"time_ms": 1})
        await conn.send_event("tick", {"time_ms": 2})
        with structlog.testing.capture_logs() as logs:
            await conn.send_event("tick", {"time_ms": 3})

        drop_logs = [
            e for e in logs if e.get("event") == "gateway.ws_writer_drop"
        ]
        assert len(drop_logs) == 1
        record = drop_logs[0]
        assert record["session_key"] is None
        assert record["stream_seq"] is None
    finally:
        if fake._send_unblock is not None:
            fake._send_unblock.set()
        await conn._stop_writer()


# ---------------------------------------------------------------------------
# Sanity: _LOSSY_EVENTS is exactly {"tick"}.
# ---------------------------------------------------------------------------


def test_lossy_events_is_only_tick() -> None:
    """The lossy set MUST be exactly {tick}.

    Any change to this set must preserve the upstream record() invariant.
    """
    assert _LOSSY_EVENTS == frozenset({"tick"})


# ---------------------------------------------------------------------------
# stop_writer is idempotent and cleans up on disconnect-equivalent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_writer_is_idempotent_and_clears_task() -> None:
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id="cx-stop", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=4, enabled=True)
    assert conn._writer_task is not None

    # First stop: cancels writer.
    await conn._stop_writer()
    assert conn._writer_task is None
    # Second stop: no-op.
    await conn._stop_writer()
    assert conn._writer_task is None


# ---------------------------------------------------------------------------
# Sentinel for stop is exposed from module (regression guard).
# ---------------------------------------------------------------------------


def test_sentinel_stop_is_exported() -> None:
    """_SENTINEL_STOP must be a sentinel object distinct from any frame."""
    assert _SENTINEL_STOP is not None
    assert not isinstance(_SENTINEL_STOP, _OutboundFrame)


# ---------------------------------------------------------------------------
# server-emitted text_delta payloads are
# preserved end-to-end across a force_close + reconnect+replay cycle.
#
# We don't run a real WS server here. Instead we verify the property
# mechanically:
#   1. Writer queue receives N text_deltas.
#   2. Queue overflow triggers control overflow → force_close.
#   3. SessionStreamRegistry has all N entries available for replay
#      (no upstream drop because text_delta is CONTROL, not LOSSY).
#
# The invariant: the registry's record() is called by upstream emitters
# BEFORE WsConnection.send_event, so even if the connection is force-closed,
# the registry retains the full sequence for client replay via since_stream_seq.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_delta_is_control_not_lossy() -> None:
    """text_delta is CONTROL, so it is never silently dropped.

    The only LOSSY event is tick. This test guards against any future change to
    ``_LOSSY_EVENTS`` that would re-introduce silent text truncation.
    """
    # text_delta MUST NOT be lossy.
    assert "session.event.text_delta" not in _LOSSY_EVENTS
    # run_heartbeat MUST NOT be lossy (it goes through record() upstream).
    assert "session.event.run_heartbeat" not in _LOSSY_EVENTS
    # tick is the ONLY allowed lossy event.
    assert _LOSSY_EVENTS == frozenset({"tick"})


@pytest.mark.asyncio
async def test_principle2_text_delta_overflow_closes_not_drops() -> None:
    """Saturate maxsize=2 queue with text_deltas synchronously. Overflow
    must trigger 1011 close (CONTROL semantic), NOT a silent drop.
    """
    fake = _FakeWebSocket()
    conn = WsConnection(conn_id="cx-p2", ws=fake)  # type: ignore[arg-type]
    conn._start_writer(maxsize=2, enabled=True)

    fake._send_event = asyncio.Event()
    fake._send_unblock = asyncio.Event()
    try:
        with structlog.testing.capture_logs() as logs:
            # Synchronous: fill queue (2) + 1 overflow.
            for i in range(3):
                await conn.send_event(
                    "session.event.text_delta",
                    {"chunk": f"d{i}", "session_key": "s", "stream_seq": i + 1},
                )
            await asyncio.sleep(0.05)

        drop_logs = [
            e for e in logs if e.get("event") == "gateway.ws_writer_drop"
        ]
        assert drop_logs == [], "text_delta MUST NOT be silently dropped"
        overflow_logs = [
            e for e in logs if e.get("event") == "gateway.ws_writer_overflow_close"
        ]
        assert len(overflow_logs) >= 1, "control overflow must trigger force-close"
        assert conn._closing is True
    finally:
        if fake._send_unblock is not None:
            fake._send_unblock.set()
        await asyncio.sleep(0.1)
        await conn._stop_writer()
    assert fake.close_code == 1011


# ---------------------------------------------------------------------------
# make_event integration smoke (verifies the writer wires payload correctly).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writer_emits_make_event_envelope() -> None:
    conn = _make_conn(maxsize=4)
    try:
        await conn.send_event("session.event.text_delta", {"chunk": "hi"})
        await _flush_writer(conn)
        assert len(conn.ws.sent) == 1  # type: ignore[attr-defined]
        wire = json.loads(conn.ws.sent[0])  # type: ignore[attr-defined]
        expected = json.loads(
            make_event(
                "session.event.text_delta", {"chunk": "hi"}, seq=1
            ).model_dump_json()
        )
        for key in ("type", "event", "payload", "seq"):
            assert wire[key] == expected[key]
    finally:
        await conn._stop_writer()
