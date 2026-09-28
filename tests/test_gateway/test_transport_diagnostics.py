"""Low-frequency transport diagnostics contain counters, never user data."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.gateway import websocket
from opensquilla.gateway.websocket import WsConnection


def test_diagnostics_only_exposes_numeric_watermarks_and_booleans():
    conn = WsConnection("diagnostic-connection", AsyncMock())
    conn._enable_flow()
    delivery_id = conn._flow.admit(123)
    conn._flow.mark_sent(delivery_id)
    conn._flow.mark_dirty("PRIVATE_SESSION_KEY", "PRIVATE_GENERATION")
    try:
        sample = conn.transport_diagnostics()
        assert all(type(value) in (int, bool, str) or value is None for value in sample.values())
        assert sample["flow_unacked_frames"] == 1
        assert sample["flow_unacked_bytes"] == 123
        assert sample["flow_last_admitted_delivery_id"] == delivery_id
        assert sample["flow_ack_delivery_id"] == 0
        assert sample["flow_dirty_sessions"] == 1
        assert sample["queue_oldest_age_ms"] == 0
        assert sample["last_inbound_age_ms"] is None
        assert sample["last_outbound_age_ms"] is None
        assert sample["probe_wait_age_ms"] is None
        assert sample["writer_starvation_reason"] == "none"
        assert sample["writer_task_limit"] == websocket._MAX_WRITER_TASKS
        assert sample["close_task_count"] == len(websocket._SOCKET_CLOSE_TASKS)
        assert "PRIVATE" not in json.dumps(sample)
        assert conn._flow.epoch not in json.dumps(sample)
        conn._flow.acknowledge(conn._flow.epoch, delivery_id)
        assert conn.transport_diagnostics()["flow_unacked_bytes"] == 0
    finally:
        conn._cleanup_transport()


def test_writer_diagnostics_reports_activity_ages_and_redacted_starvation(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(websocket.time, "monotonic", lambda: clock[0])
    conn = WsConnection("diagnostic-connection", AsyncMock())
    conn._queue_enabled = True
    conn._outbox = asyncio.Queue()
    conn._last_inbound_at = 7.0
    conn._last_outbound_at = 8.0
    conn._probe_wait_started_at = 9.0
    conn._outbox.put_nowait(
        websocket._OutboundFrame(
            kind="raw",
            classification="control",
            payload=None,
            event_name=None,
            res_frame=None,
            raw_text='{"type":"pong"}',
            enqueued_at=6.0,
        )
    )

    sample = conn.transport_diagnostics()
    assert sample["queue_oldest_age_ms"] == 4000
    assert sample["last_inbound_age_ms"] == 3000
    assert sample["last_outbound_age_ms"] == 2000
    assert sample["probe_wait_age_ms"] == 1000
    assert sample["writer_starvation_reason"] == "probe_wait"
    assert "pong" not in json.dumps(sample)

    conn._probe_wait_started_at = None
    assert conn.transport_diagnostics()["writer_starvation_reason"] == "writer_task_missing"
    conn._cleanup_transport()


async def test_tick_samples_once_per_minute_and_reports_scheduler_delay(monkeypatch):
    clock = [0.0]
    wake_delays = iter([0.025, 0.0, 0.0, 0.0])
    logger = Mock()

    async def sleep(seconds):
        try:
            extra_delay = next(wake_delays)
        except StopIteration:
            raise asyncio.CancelledError from None
        clock[0] += seconds + extra_delay

    # No real sleep, extra task, thread or network. This fake merely advances
    # the existing tick's monotonic observation boundary four times.
    monkeypatch.setattr(
        websocket, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 0)
    )
    monkeypatch.setattr(websocket.asyncio, "sleep", sleep)
    monkeypatch.setattr(websocket, "log", logger)
    conn = SimpleNamespace(
        conn_id="diagnostic-connection",
        send_event=AsyncMock(),
        transport_diagnostics=lambda: {"queue_depth": 2, "flow_unacked_frames": 7},
    )
    with pytest.raises(asyncio.CancelledError):
        await websocket._tick_loop(conn, 30_000)
    samples = [
        call
        for call in logger.debug.call_args_list
        if call.args == ("gateway.ws_transport_sample",)
    ]
    assert len(samples) == 2
    assert samples[0].kwargs["event_loop_lag_ms"] == 25.0
    assert samples[1].kwargs["event_loop_lag_ms"] == 0.0
    assert samples[0].kwargs["queue_depth"] == 2
    assert conn.send_event.await_count == 4
