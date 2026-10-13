from __future__ import annotations

import json
import linecache
import time

from opensquilla.gateway import stall_watchdog
from opensquilla.gateway.stall_watchdog import GatewayStallWatchdog


def test_stall_watchdog_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("OPENSQUILLA_STALL_DIAGNOSTICS", raising=False)
    monkeypatch.setenv("OPENSQUILLA_STALL_DIAGNOSTICS_LOOP_LAG", "1")
    assert GatewayStallWatchdog.from_environment() is None


def test_stall_watchdog_samples_main_thread_without_locals(tmp_path) -> None:
    output = tmp_path / "stalls.jsonl"
    watchdog = GatewayStallWatchdog(
        output_path=output,
        threshold_s=0.25,
        sample_interval_s=0.05,
        stack_interval_s=0.1,
    )
    watchdog.start()
    try:
        # The event-loop heartbeat is intentionally not called.  A separate
        # watchdog thread must still capture the blocked main Python stack.
        time.sleep(0.55)
    finally:
        watchdog.stop()

    events = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    samples = [event for event in events if event["type"] == "stack_sample"]
    assert samples
    assert any(item["function"] == "test_stall_watchdog_samples_main_thread_without_locals"
               for item in samples[0]["stack"])
    assert all("locals" not in item for item in samples[0]["stack"])
    assert all(item["code"] is None for item in samples[0]["stack"])
    assert samples[0]["heartbeat_started"] is False
    assert samples[0]["same_heartbeat_window"] is True
    assert samples[0]["capture_ts"] <= samples[0]["ts"]
    assert (samples[0]["capture_monotonic_s"]
            <= samples[0]["capture_finished_monotonic_s"])
    assert any(event["type"] == "stall_started" for event in events)


def test_stall_watchdog_records_heartbeat_age_and_observation_span(tmp_path) -> None:
    output = tmp_path / "stalls.jsonl"
    watchdog = GatewayStallWatchdog(
        output_path=output,
        threshold_s=0.2,
        sample_interval_s=0.05,
        stack_interval_s=0.5,
    )
    watchdog.start()
    try:
        watchdog.beat()
        time.sleep(0.35)
        watchdog.beat()
        time.sleep(0.12)
    finally:
        watchdog.stop()

    events = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    started = next(event for event in events if event["type"] == "stall_started")
    ended = next(event for event in events if event["type"] == "stall_ended")
    assert started["heartbeat_age_ms"] == started["lag_ms"]
    assert isinstance(started["heartbeat_seq"], int)
    assert ended["observed_duration_ms"] == ended["duration_ms"]
    assert ended["heartbeat_to_observation_span_ms"] >= ended["observed_duration_ms"]
    assert "stale_heartbeat_window_lower_bound_ms" not in ended
    assert ended["heartbeat_seq"] > started["heartbeat_seq"]


def test_stack_capture_precedes_log_writes_and_keeps_capture_time(tmp_path, monkeypatch) -> None:
    watchdog = GatewayStallWatchdog(output_path=tmp_path / "stalls.jsonl")
    watchdog._heartbeat_at = 10.0
    monotonic_now = [13.0]
    wall_now = [100.0]
    monkeypatch.setattr(stall_watchdog.time, "monotonic", lambda: monotonic_now[0])
    monkeypatch.setattr(stall_watchdog.time, "time", lambda: wall_now[0])
    captured = []
    original_write = watchdog._write

    def capture():
        captured.append(True)
        monotonic_now[0] = 13.01
        return []

    def delayed_write(payload, **kwargs):
        assert captured, "Writing a detection event must not precede frame capture"
        wall_now[0] = 101.0
        original_write(payload, **kwargs)

    monkeypatch.setattr(watchdog, "_main_stack", capture)
    monkeypatch.setattr(watchdog, "_write", delayed_write)
    watchdog._sample_once()
    events = [json.loads(line) for line in watchdog.output_path.read_text().splitlines()]
    sample = next(event for event in events if event["type"] == "stack_sample")
    assert sample["capture_ts"] == 100.0
    assert sample["ts"] == 101.0
    assert sample["capture_monotonic_s"] == 13.0
    assert sample["capture_finished_monotonic_s"] == 13.01
    assert sample["heartbeat_age_ms"] == 3000


def test_stack_capture_marks_heartbeat_progress_during_capture(tmp_path, monkeypatch) -> None:
    watchdog = GatewayStallWatchdog(output_path=tmp_path / "stalls.jsonl")
    watchdog.beat()

    def capture():
        watchdog.beat()
        return []

    monkeypatch.setattr(watchdog, "_main_stack", capture)
    sample = watchdog._stack_sample(detected_heartbeat_seq=1)
    assert sample["heartbeat_seq_before"] == 1
    assert sample["heartbeat_seq_after"] == 2
    assert sample["heartbeat_started"] is True
    assert sample["same_heartbeat_window"] is False


def test_stack_capture_marks_progress_between_detection_and_capture(tmp_path) -> None:
    watchdog = GatewayStallWatchdog(output_path=tmp_path / "stalls.jsonl")
    watchdog.beat()
    sample = watchdog._stack_sample(detected_heartbeat_seq=0)
    assert sample["heartbeat_seq_before"] == sample["heartbeat_seq_after"] == 1
    assert sample["same_heartbeat_window"] is False


def test_poll_clock_and_heartbeat_share_one_snapshot(tmp_path, monkeypatch) -> None:
    watchdog = GatewayStallWatchdog(
        output_path=tmp_path / "stalls.jsonl", threshold_s=0.2,
    )
    now = [9.0]
    monkeypatch.setattr(stall_watchdog.time, "monotonic", lambda: now[0])

    class HeartbeatBeforeAcquisition:
        def __enter__(self):
            # Model progress while the observer is waiting to acquire the
            # heartbeat lock. A clock read before acquisition is now stale.
            watchdog._heartbeat_at = 10.5
            watchdog._heartbeat_seq = 1
            now[0] = 11.0

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(watchdog, "_heartbeat_lock", HeartbeatBeforeAcquisition())
    events = []
    monkeypatch.setattr(watchdog, "_write", lambda payload: events.append(payload))
    watchdog._sample_once()
    started = next(event for event in events if event["type"] == "stall_started")
    assert started["heartbeat_age_ms"] == 500


def test_stack_capture_is_bounded_and_never_loads_source(tmp_path, monkeypatch) -> None:
    watchdog = GatewayStallWatchdog(output_path=tmp_path / "stalls.jsonl")

    def reject_source_access(*args, **kwargs):
        raise AssertionError("Stack sampling must not consult linecache")

    monkeypatch.setattr(linecache, "checkcache", reject_source_access)
    monkeypatch.setattr(linecache, "getline", reject_source_access)
    monkeypatch.setattr(linecache, "getlines", reject_source_access)

    def recurse(depth):
        return recurse(depth - 1) if depth else watchdog._main_stack()

    stack = recurse(80)
    assert len(stack) == 64
    assert stack[-1]["function"] == "_main_stack"
    assert all(frame["code"] is None for frame in stack)
    assert all(set(frame) == {"file", "line", "function", "code"} for frame in stack)


def test_recovery_observation_span_can_include_time_after_actual_recovery(
    tmp_path, monkeypatch
) -> None:
    watchdog = GatewayStallWatchdog(
        output_path=tmp_path / "stalls.jsonl", threshold_s=0.2,
    )
    watchdog._heartbeat_at = 100.0
    now = [100.25]
    monkeypatch.setattr(stall_watchdog.time, "monotonic", lambda: now[0])
    events = []
    monkeypatch.setattr(watchdog, "_write", lambda payload: events.append(payload))
    watchdog._sample_once()
    now[0] = 100.4
    watchdog.beat()
    now[0] = 100.45
    watchdog._sample_once()
    ended = next(event for event in events if event["type"] == "stall_ended")
    assert ended["observed_duration_ms"] == 200
    # Recovery happened at 400 ms; the 450 ms span ends at the observing poll.
    assert ended["heartbeat_to_observation_span_ms"] == 450
    assert "stale_heartbeat_window_lower_bound_ms" not in ended


def test_stall_watchdog_does_not_emit_stalls_after_heartbeat_failure(tmp_path) -> None:
    output = tmp_path / "stalls.jsonl"
    watchdog = GatewayStallWatchdog(
        output_path=output,
        threshold_s=0.1,
        sample_interval_s=0.02,
        stack_interval_s=0.02,
    )
    assert watchdog.start()
    try:
        watchdog.heartbeat_failed(RuntimeError("test heartbeat failure"))
        time.sleep(0.08)
    finally:
        watchdog.stop()

    events = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert any(event["type"] == "heartbeat_failed" for event in events)
    assert not any(event["type"] == "stall_started" for event in events)


def test_stall_watchdog_emits_event_loop_lag_summary_when_enabled(tmp_path) -> None:
    output = tmp_path / "stalls.jsonl"
    watchdog = GatewayStallWatchdog(
        output_path=output,
        threshold_s=1.0,
        sample_interval_s=0.02,
        stack_interval_s=0.5,
        loop_lag_enabled=True,
    )
    assert watchdog.start()
    try:
        for value in (0.0, 10.0, 50.0, 150.0, 500.0):
            watchdog.record_loop_lag(value)
    finally:
        watchdog.stop()

    events = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    stopped = next(event for event in events if event["type"] == "watchdog_stopped")
    assert stopped["loop_lag_source"] == "event_loop_heartbeat"
    assert stopped["loop_lag_scope"] == "resumed_heartbeat_samples"
    assert stopped["loop_lag_status"] == "measured"
    assert stopped["loop_lag_sample_count"] == 5
    assert stopped["loop_lag_p99_ms"] == 500.0
    assert stopped["loop_lag_max_ms"] == 500.0


def test_stall_watchdog_loop_lag_is_opt_in(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENSQUILLA_STALL_DIAGNOSTICS", "1")
    monkeypatch.delenv("OPENSQUILLA_STALL_DIAGNOSTICS_LOOP_LAG", raising=False)
    watchdog = GatewayStallWatchdog.from_environment()
    assert watchdog is not None
    assert watchdog.loop_lag_enabled is False


def test_loop_lag_timeline_is_ram_only_until_stop_and_preserves_deadlines(tmp_path) -> None:
    output = tmp_path / "stalls.jsonl"
    watchdog = GatewayStallWatchdog(output_path=output, loop_lag_enabled=True)
    for index, lag in enumerate((650.0, 15.0)):
        watchdog.record_loop_lag(
            lag, expected_wake_s=10.1 + index, wake_s=10.1 + index + lag / 1000,
            wake_perf_ns=100_000_000 + index, wake_ts=1_790_000_000.0 + index,
        )
        watchdog.beat()
    assert not output.exists()
    watchdog.stop()
    events = [json.loads(line) for line in output.read_text().splitlines()]
    timeline, summary = events
    assert timeline["type"] == "loop_lag_timeline"
    assert timeline["samples"] == [
        [1, 10.1, 10.75, 100_000_000, 1_790_000_000.0, 650.0],
        [2, 11.1, 11.115, 100_000_001, 1_790_000_001.0, 15.0],
    ]
    assert summary["loop_lag_max_ms"] == 650.0
    assert summary["loop_lag_timeline_complete"] is True


def test_loop_lag_timeline_exhaustion_is_explicit_without_truncating_aggregate(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(stall_watchdog, "_MAX_LOOP_LAG_TIMELINE_SAMPLES", 2)
    output = tmp_path / "stalls.jsonl"
    watchdog = GatewayStallWatchdog(output_path=output, loop_lag_enabled=True)
    for index in range(3):
        watchdog.record_loop_lag(
            index * 500, expected_wake_s=float(index), wake_s=float(index + 1),
            wake_perf_ns=index, wake_ts=float(index),
        )
        watchdog.beat()
    watchdog.stop()
    summary = json.loads(output.read_text().splitlines()[-1])
    assert summary["loop_lag_sample_count"] == 3
    assert summary["loop_lag_max_ms"] == 1000.0
    assert summary["loop_lag_timeline_sample_count"] == 2
    assert summary["loop_lag_timeline_dropped"] == 1
    assert summary["loop_lag_timeline_complete"] is False


def test_loop_lag_timeline_and_summary_survive_ordinary_event_budget(tmp_path) -> None:
    output = tmp_path / "stalls.jsonl"
    watchdog = GatewayStallWatchdog(output_path=output, loop_lag_enabled=True)
    for index in range(stall_watchdog._MAX_LOOP_LAG_TIMELINE_SAMPLES):
        watchdog.record_loop_lag(
            15.0, expected_wake_s=12345678.123456789, wake_s=12345678.138456789,
            wake_perf_ns=12345678138456789, wake_ts=1791462824.7346392,
        )
        watchdog.beat()
    for _ in range(stall_watchdog._MAX_EVENTS + 1):
        watchdog._write({"type": "test", "padding": "x" * 5000})
    watchdog.stop()
    events = [json.loads(line) for line in output.read_text().splitlines()]
    assert events[-2]["type"] == "loop_lag_timeline"
    assert len(events[-2]["samples"]) == stall_watchdog._MAX_LOOP_LAG_TIMELINE_SAMPLES
    assert events[-1]["type"] == "watchdog_stopped"
    assert events[-1]["loop_lag_timeline_complete"] is True
    assert output.stat().st_size <= stall_watchdog._MAX_BYTES


def test_loop_lag_timeline_disabled_or_unwritten_never_claims_coverage(
    tmp_path, monkeypatch
) -> None:
    disabled = GatewayStallWatchdog(output_path=tmp_path / "off.jsonl")
    disabled.record_loop_lag(1, expected_wake_s=1, wake_s=2, wake_perf_ns=3, wake_ts=4)
    assert disabled._loop_lag_timeline == []
    assert disabled._loop_lag_samples == []

    output = tmp_path / "unwritten.jsonl"
    watchdog = GatewayStallWatchdog(output_path=output, loop_lag_enabled=True)
    watchdog.record_loop_lag(650)
    # Force the timeline outside its reserved budget while retaining summary room.
    watchdog._bytes_written = stall_watchdog._MAX_BYTES - stall_watchdog._RESERVED_SUMMARY_BYTES
    watchdog.stop()
    summary = json.loads(output.read_text().splitlines()[-1])
    assert summary["loop_lag_timeline_written"] is False
    assert summary["loop_lag_timeline_missing"] == 1
    assert summary["loop_lag_timeline_complete"] is False
    assert summary["loop_lag_max_ms"] == 650.0
