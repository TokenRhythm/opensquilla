"""Low-frequency transport diagnostics contain counters, never user data."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.gateway import websocket
from opensquilla.gateway.protocol import MAX_PAYLOAD_BYTES, ReqFrame, ResFrame, make_error_res
from opensquilla.gateway.transport_flow import get_transport_budget
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


@pytest.mark.parametrize("path", ["stale", "dirty", "close"])
@pytest.mark.parametrize("sample_fails", [False, True])
async def test_failed_admission_diagnostics_cannot_skip_cleanup_or_original_recovery(
    monkeypatch, path, sample_fails,
):
    before = get_transport_budget().used
    conn = WsConnection("diagnostic-failure", AsyncMock())
    conn._enable_flow()
    conn._outbox = asyncio.Queue()
    frame = websocket._OutboundFrame(
        kind="event" if path == "dirty" else "res", classification="control",
        payload={"session_key": "s"},
        event_name="session.event.text_delta" if path == "dirty" else None,
        res_frame=None if path == "dirty" else ResFrame(id="read", ok=True, payload={"key": "s"}),
    )
    original = (
        websocket.FlowDeliveryStaleError("PRIVATE_ORIGINAL")
        if path == "stale" else ValueError("PRIVATE_ORIGINAL")
    )
    prepare = conn._prepare_flow_frame

    def fail_after_reservation(candidate):
        if candidate is not frame:
            return prepare(candidate)
        assert conn.reserve_transport_bytes(17)
        candidate.budget_bytes = 17
        raise original

    sample = (
        Mock(side_effect=RuntimeError("PRIVATE_DIAGNOSTIC")) if sample_fails
        else Mock(wraps=conn._flow_failure_diagnostics)
    )
    logger, dirty, close = Mock(), Mock(), AsyncMock()
    monkeypatch.setattr(conn, "_prepare_flow_frame", fail_after_reservation)
    monkeypatch.setattr(conn, "_flow_failure_diagnostics", sample, raising=False)
    monkeypatch.setattr(conn, "_mark_flow_dirty", dirty)
    monkeypatch.setattr(conn, "_force_close", close)
    monkeypatch.setattr(websocket, "log", logger)
    try:
        conn._enqueue_frame(frame)
        await asyncio.sleep(0)
        sample.assert_called_once_with()
        assert frame.budget_bytes == 0
        logged = logger.warning.call_args.kwargs
        assert logged["exception_type"] == type(original).__name__
        assert logged["reason_code"] == "flow_admission_unclassified"
        assert logged["diagnostics_available"] is not sample_fails
        if not sample_fails:
            # The log describes the failed admission, not the state after its cleanup.
            assert logged["transport_reserved_bytes"] == 17
            assert logged["global_transport_reserved_bytes"] == before + 17
            assert logged["connection_transport_limit_bytes"] == websocket.CONNECTION_BUFFER_BYTES
            assert logged["global_transport_limit_bytes"] == get_transport_budget().limit
        if path == "close":
            close.assert_awaited_once_with(reason="transport_resource_limit", code=1013)
            dirty.assert_not_called()
            assert conn._closing
        else:
            close.assert_not_called()
            dirty.assert_called_once()
            assert not conn._closing
            if path == "stale":
                reply = conn._outbox.get_nowait()
                assert reply.res_frame.error.code == "SNAPSHOT_STALE"
                assert reply.res_frame.error.retryable
                conn._release_outbound_budget(reply)
        assert conn._transport_bytes == 0
    finally:
        conn._cleanup_transport()
    assert get_transport_budget().used == before


async def test_oversized_response_returns_rpc_error_without_closing_socket(monkeypatch):
    """C30 regression: a response wire overflow is request-scoped."""

    before = get_transport_budget().used
    monkeypatch.setattr(websocket, "MAX_PAYLOAD_BYTES", 512)
    conn = WsConnection("oversized-response", AsyncMock())
    conn._enable_flow()
    conn._outbox = asyncio.Queue()
    try:
        conn._enqueue_frame(websocket._OutboundFrame(
            kind="res",
            classification="control",
            payload=None,
            event_name=None,
            res_frame=ResFrame(id="large", ok=True, payload={"content": "x" * 4096}),
        ))
        assert not conn._closing
        reply = conn._outbox.get_nowait()
        assert reply.res_frame is not None
        assert reply.res_frame.id == "large"
        assert reply.res_frame.error is not None
        assert reply.res_frame.error.code == "RESPONSE_TOO_LARGE"
        assert reply.res_frame.error.accepted is None
        assert reply.res_frame.error.retryable is False
        assert reply.res_frame.error.details == {"max_payload_bytes": 512}

        # The same socket can still carry a subsequent control response.
        conn._enqueue_frame(websocket._OutboundFrame(
            kind="res",
            classification="control",
            payload=None,
            event_name=None,
            res_frame=ResFrame(id="follow-up", ok=True, payload={"ok": True}),
        ))
        follow_up = conn._outbox.get_nowait()
        assert follow_up.res_frame is not None
        assert follow_up.res_frame.id == "follow-up"
        conn._release_outbound_budget(reply)
        conn._release_outbound_budget(follow_up)
    finally:
        conn._cleanup_transport()
    assert get_transport_budget().used == before


@pytest.mark.parametrize("failure", ["wire", "post-reservation"])
async def test_response_fallback_failure_closes_once_and_releases_its_budget(monkeypatch, failure):
    before = get_transport_budget().used
    monkeypatch.setattr(websocket, "MAX_PAYLOAD_BYTES", 1 if failure == "wire" else 512)
    conn = WsConnection("failed-response-fallback", AsyncMock())
    conn._enable_flow()
    conn._outbox = asyncio.Queue()
    frame = websocket._OutboundFrame(
        kind="res", classification="control", payload=None, event_name=None,
        res_frame=ResFrame(id="request", ok=True, payload={"content": "x" * 4096}),
    )
    prepare, attempted = conn._prepare_flow_frame, []

    def prepare_then_fail(candidate):
        attempted.append(candidate)
        result = prepare(candidate)
        if failure == "post-reservation" and candidate is not frame:
            assert candidate.budget_bytes > 0
            raise websocket._FlowAdmissionError(
                "late admission rejection", reason_code="transport_reservation_rejected",
            )
        return result

    close, logger = AsyncMock(), Mock()
    monkeypatch.setattr(conn, "_prepare_flow_frame", prepare_then_fail)
    monkeypatch.setattr(conn, "_force_close", close)
    monkeypatch.setattr(websocket, "log", logger)
    try:
        conn._enqueue_frame(frame)
        await asyncio.sleep(0)
        assert len(attempted) == 2
        assert attempted[1].res_frame.error.code == "RESPONSE_TOO_LARGE"
        assert attempted[1].res_frame.error.accepted is None
        assert all(candidate.budget_bytes == 0 for candidate in attempted)
        close.assert_awaited_once_with(reason="transport_resource_limit", code=1013)
        assert logger.warning.call_count == 1
        assert conn._closing and conn._outbox.empty()
        assert conn._transport_bytes == conn._flow_control_bytes == conn._flow_control_frames == 0
        assert get_transport_budget().used == before
    finally:
        conn._cleanup_transport()


async def test_near_wire_limit_request_id_cannot_recurse_through_error_responses(monkeypatch):
    # The real inbound limit allows this id, but both error envelopes exceed it.
    # Keep the id intact: truncating it would break request correlation.
    request = ReqFrame(id="i" * (MAX_PAYLOAD_BYTES - 80), method="x")
    assert len(request.model_dump_json().encode()) <= MAX_PAYLOAD_BYTES
    response = make_error_res(request.id, "METHOD_NOT_FOUND", "Unknown method")
    assert len(response.model_dump_json().encode()) > MAX_PAYLOAD_BYTES
    before = get_transport_budget().used
    conn = WsConnection("near-limit-id", AsyncMock())
    conn._enable_flow()
    conn._outbox = asyncio.Queue()
    prepare, close = Mock(wraps=conn._prepare_flow_frame), AsyncMock()
    monkeypatch.setattr(conn, "_prepare_flow_frame", prepare)
    monkeypatch.setattr(conn, "_force_close", close)
    try:
        conn._enqueue_frame(websocket._OutboundFrame(
            kind="res", classification="control", payload=None, event_name=None,
            res_frame=response,
        ))
        await asyncio.sleep(0)
        assert prepare.call_count == 2
        fallback = prepare.call_args.args[0]
        assert fallback.res_frame.id == request.id
        assert fallback.res_frame.error.accepted is None
        close.assert_awaited_once_with(reason="transport_resource_limit", code=1013)
        assert conn._closing and conn._outbox.empty()
        assert conn._transport_bytes == 0
        assert get_transport_budget().used == before
    finally:
        conn._cleanup_transport()


def test_wire_preflight_rejects_oversized_payload_before_recursive_protocol_copy(monkeypatch):
    """A clearly oversized response/event never enters the full encoder."""

    monkeypatch.setattr(websocket, "MAX_PAYLOAD_BYTES", 512)
    original = websocket.encode_payload_for_protocol
    original_model_copy = ResFrame.model_copy
    large_calls: list[object] = []

    def guarded(payload, *, protocol):
        if isinstance(payload, dict) and len(str(payload.get("content", ""))) > 1024:
            large_calls.append(payload)
            raise AssertionError("oversized payload reached recursive protocol encoder")
        return original(payload, protocol=protocol)

    monkeypatch.setattr(websocket, "encode_payload_for_protocol", guarded)

    def guarded_model_copy(self, *args, **kwargs):
        payload = self.payload
        if isinstance(payload, dict) and len(str(payload.get("content", ""))) > 1024:
            raise AssertionError("oversized payload reached Pydantic frame serializer")
        return original_model_copy(self, *args, **kwargs)

    monkeypatch.setattr(ResFrame, "model_copy", guarded_model_copy)
    conn = WsConnection("preflight", AsyncMock())
    conn._enable_flow()
    conn._outbox = asyncio.Queue()
    try:
        conn._enqueue_frame(websocket._OutboundFrame(
            kind="res", classification="control", payload=None, event_name=None,
            res_frame=ResFrame(id="large", ok=True, payload={"content": "x" * 4096}),
        ))
        response = conn._outbox.get_nowait()
        assert response.res_frame is not None
        assert response.res_frame.error is not None
        assert response.res_frame.error.code == "RESPONSE_TOO_LARGE"
        conn._enqueue_frame(websocket._OutboundFrame(
            kind="event", classification="lossy", payload={"content": "x" * 4096},
            event_name="session.event.text_delta", res_frame=None,
        ))
        assert large_calls == []
    finally:
        conn._cleanup_transport()


def test_admission_failure_counters_include_acknowledged_inflight_reservations():
    before = get_transport_budget().used
    conn = WsConnection("diagnostic-inflight", AsyncMock())
    conn._enable_flow()
    try:
        delivery_id = conn._flow.admit(123)
        conn._flow.mark_sending(delivery_id)
        conn._flow.acknowledge(conn._flow.epoch, delivery_id)
        assert conn._flow.deliveries[delivery_id].acknowledged
        sample = conn._flow_failure_diagnostics()
        assert sample["flow_reserved_frames"] == 1
        assert sample["flow_reserved_bytes"] == sample["transport_reserved_bytes"] == 123
        conn._flow.mark_sent(delivery_id)
        sample = conn._flow_failure_diagnostics()
        assert sample["flow_reserved_frames"] == sample["flow_reserved_bytes"] == 0
    finally:
        conn._cleanup_transport()
    assert get_transport_budget().used == before


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
