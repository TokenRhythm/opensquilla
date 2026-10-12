"""Opt-in event-loop stall stack capture.

The normal Gateway diagnostics only notice a long event-loop tick *after* the
loop gets scheduled again.  That loses the only useful evidence when a
synchronous call blocks the loop for tens of seconds.  This module keeps a
small daemon thread outside the loop and samples the Gateway's main Python
thread while its heartbeat is stale.

It is deliberately opt-in.  The default client does not create a thread or a
file.  Diagnostic builds can set ``OPENSQUILLA_STALL_DIAGNOSTICS=1`` and,
optionally, ``OPENSQUILLA_STALL_DIAGNOSTICS_PATH``.  Samples contain only
file/function/line metadata; locals, arguments, prompts, credentials, and
provider responses are never serialized.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_ENABLED_VALUES = {"1", "true", "yes", "on"}
_DEFAULT_THRESHOLD_S = 2.0
_DEFAULT_SAMPLE_INTERVAL_S = 0.25
_DEFAULT_STACK_INTERVAL_S = 1.0
_MAX_EVENTS = 256
_MAX_BYTES = 1_000_000
_RESERVED_SUMMARY_BYTES = 4_096
_MAX_LOOP_LAG_SAMPLES = 100_000
_MAX_LOOP_LAG_TIMELINE_SAMPLES = 1_024
_RESERVED_TIMELINE_BYTES = 256_000
_MAX_STACK_FRAMES = 64


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _ENABLED_VALUES


def _float_env(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def _default_output_path() -> Path:
    from opensquilla.paths import default_opensquilla_home

    return default_opensquilla_home() / "logs" / "gateway-stalls.jsonl"


class GatewayStallWatchdog:
    """Sample the Gateway main thread while the event-loop heartbeat is stale."""

    def __init__(
        self,
        *,
        output_path: Path,
        threshold_s: float = _DEFAULT_THRESHOLD_S,
        sample_interval_s: float = _DEFAULT_SAMPLE_INTERVAL_S,
        stack_interval_s: float = _DEFAULT_STACK_INTERVAL_S,
        loop_lag_enabled: bool = False,
    ) -> None:
        self.output_path = output_path
        self.threshold_s = threshold_s
        self.sample_interval_s = sample_interval_s
        self.stack_interval_s = stack_interval_s
        # Loop-lag collection is deliberately separate from stack capture.
        # It remains opt-in so normal clients do not retain a sample buffer or
        # add a second diagnostic contract to their shutdown path.
        self.loop_lag_enabled = loop_lag_enabled
        self._heartbeat_at = time.monotonic()
        self._main_thread_id = threading.get_ident()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._heartbeat_lock = threading.Lock()
        self._heartbeat_seq = 0
        self._heartbeat_alive = True
        self._event_count = 0
        self._bytes_written = 0
        self._stall_started_at: float | None = None
        self._stall_detected_lag_ms: int | None = None
        self._last_stack_at = 0.0
        self._loop_lag_samples: list[float] = []
        self._loop_lag_sample_count = 0
        self._loop_lag_samples_dropped = 0
        self._loop_lag_timeline: list[tuple[int, float, float, int, float, float]] = []
        self._loop_lag_timeline_dropped = 0
        self._loop_lag_timeline_missing = 0

    @classmethod
    def from_environment(cls) -> GatewayStallWatchdog | None:
        if not _truthy(os.environ.get("OPENSQUILLA_STALL_DIAGNOSTICS")):
            return None
        raw_path = os.environ.get("OPENSQUILLA_STALL_DIAGNOSTICS_PATH", "").strip()
        path = Path(raw_path) if raw_path else _default_output_path()
        return cls(
            output_path=path,
            threshold_s=_float_env(
                "OPENSQUILLA_STALL_DIAGNOSTICS_THRESHOLD_S",
                _DEFAULT_THRESHOLD_S,
                minimum=0.25,
                maximum=60.0,
            ),
            sample_interval_s=_float_env(
                "OPENSQUILLA_STALL_DIAGNOSTICS_SAMPLE_INTERVAL_S",
                _DEFAULT_SAMPLE_INTERVAL_S,
                minimum=0.05,
                maximum=5.0,
            ),
            stack_interval_s=_float_env(
                "OPENSQUILLA_STALL_DIAGNOSTICS_STACK_INTERVAL_S",
                _DEFAULT_STACK_INTERVAL_S,
                minimum=0.1,
                maximum=10.0,
            ),
            loop_lag_enabled=_truthy(
                os.environ.get("OPENSQUILLA_STALL_DIAGNOSTICS_LOOP_LAG")
            ),
        )

    def start(self) -> bool:
        if self._thread is not None:
            return True
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("gateway.stall_diagnostics_unavailable", error=str(exc))
            return False
        self._thread = threading.Thread(
            target=self._run,
            name="opensquilla-gateway-stall-watchdog",
            daemon=True,
        )
        self._thread.start()
        self._write({
            "type": "watchdog_started",
            "threshold_ms": round(self.threshold_s * 1000),
            "sample_interval_ms": round(self.sample_interval_s * 1000),
        })
        return True

    def beat(self) -> None:
        """Record progress from the event-loop task."""

        with self._heartbeat_lock:
            self._heartbeat_at = time.monotonic()
            self._heartbeat_seq += 1

    def record_loop_lag(
        self, lag_ms: float, *, expected_wake_s: float | None = None,
        wake_s: float | None = None, wake_perf_ns: int | None = None,
        wake_ts: float | None = None,
    ) -> None:
        """Record one event-loop scheduling delay sample.

        The caller is the event-loop heartbeat task, so a sample is only
        produced when that task gets scheduled.  This measures scheduler
        delay directly; the independent watchdog thread remains responsible
        for evidence while the loop is synchronously blocked.  The aggregate
        excludes work before the task establishes its first deadline, and
        any final delay for which the task never resumes to record a sample.
        """

        if not self.loop_lag_enabled:
            return
        try:
            value = max(0.0, float(lag_ms))
        except (TypeError, ValueError):
            return
        with self._heartbeat_lock:
            self._loop_lag_sample_count += 1
            if len(self._loop_lag_samples) < _MAX_LOOP_LAG_SAMPLES:
                self._loop_lag_samples.append(value)
            else:
                self._loop_lag_samples_dropped += 1
            # Timing metadata stays in RAM until stop. The monotonic deadline
            # and wake share the event loop clock; wall/perf observations only
            # correlate that sample with other process logs and harnesses.
            if (expected_wake_s is None or wake_s is None
                    or wake_perf_ns is None or wake_ts is None):
                self._loop_lag_timeline_missing += 1
            elif len(self._loop_lag_timeline) < _MAX_LOOP_LAG_TIMELINE_SAMPLES:
                self._loop_lag_timeline.append((
                    self._heartbeat_seq + 1, expected_wake_s, wake_s,
                    wake_perf_ns, wake_ts, value,
                ))
            else:
                self._loop_lag_timeline_dropped += 1

    def heartbeat_failed(self, error: BaseException) -> None:
        """Stop diagnostics when their own heartbeat can no longer run.

        A dead heartbeat cannot distinguish a loop stall from a monitoring
        failure. Stopping here prevents the watchdog thread from emitting a
        misleading stall sample with a frozen sequence number.
        """

        with self._heartbeat_lock:
            self._heartbeat_alive = False
            heartbeat_seq = self._heartbeat_seq
        self._write({
            "type": "heartbeat_failed",
            "heartbeat_seq": heartbeat_seq,
            "error_type": type(error).__name__,
        })
        self._stop.set()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.sample_interval_s * 4))
        now = time.monotonic()
        with self._heartbeat_lock:
            heartbeat_at = self._heartbeat_at
            heartbeat_seq = self._heartbeat_seq
            heartbeat_alive = self._heartbeat_alive
        payload: dict[str, Any] = {
            "type": "watchdog_stopped",
            "heartbeat_seq": heartbeat_seq,
            "heartbeat_alive": heartbeat_alive,
            "heartbeat_started": heartbeat_seq > 0,
        }
        if self.loop_lag_enabled:
            payload.update(self._loop_lag_summary())
            with self._heartbeat_lock:
                timeline = list(self._loop_lag_timeline)
                timeline_missing = self._loop_lag_timeline_missing
                timeline_dropped = self._loop_lag_timeline_dropped
            timeline_written = self._write({
                "type": "loop_lag_timeline",
                "columns": ["heartbeat_seq", "expected_wake_s", "wake_s",
                            "observed_perf_ns", "observed_ts", "lag_ms"],
                "samples": timeline,
            }, force=True, reserve_bytes=_RESERVED_SUMMARY_BYTES)
            payload.update({
                "loop_lag_timeline_sample_count": len(timeline),
                "loop_lag_timeline_missing": timeline_missing,
                "loop_lag_timeline_dropped": timeline_dropped,
                "loop_lag_timeline_written": timeline_written,
                "loop_lag_timeline_complete": (
                    timeline_written and not timeline_missing and not timeline_dropped
                ),
            })
        if self._stall_started_at is not None:
            observed_duration_ms = round((now - self._stall_started_at) * 1000)
            heartbeat_age_ms = round((now - heartbeat_at) * 1000)
            payload.update({
                "stall_active": True,
                "heartbeat_age_ms": heartbeat_age_ms,
                "observed_duration_ms": observed_duration_ms,
                "heartbeat_to_observation_span_ms": (
                    (self._stall_detected_lag_ms or 0) + observed_duration_ms
                ),
            })
        # Force the summary through the bounded event budget.  A long run can
        # legitimately consume the ordinary stall/stack event slots; losing
        # the aggregate would make the SLO evidence unverifiable.
        self._write(payload, force=True)

    def _loop_lag_summary(self) -> dict[str, Any]:
        with self._heartbeat_lock:
            samples = sorted(self._loop_lag_samples)
            sample_count = self._loop_lag_sample_count
            dropped = self._loop_lag_samples_dropped
        if not samples:
            return {
                "loop_lag_source": "event_loop_heartbeat",
                "loop_lag_scope": "resumed_heartbeat_samples",
                "loop_lag_sample_count": sample_count,
                "loop_lag_samples_dropped": dropped,
                "loop_lag_status": "insufficient_samples",
            }
        p99_index = min(len(samples) - 1, max(0, int((len(samples) * 99 + 99) / 100) - 1))
        return {
            "loop_lag_source": "event_loop_heartbeat",
            "loop_lag_scope": "resumed_heartbeat_samples",
            "loop_lag_sample_count": sample_count,
            "loop_lag_samples_dropped": dropped,
            "loop_lag_p99_ms": round(samples[p99_index], 3),
            "loop_lag_max_ms": round(samples[-1], 3),
            "loop_lag_status": "measured",
        }

    def _run(self) -> None:
        while not self._stop.wait(self.sample_interval_s):
            self._sample_once()

    def _sample_once(self) -> None:
        with self._heartbeat_lock:
            heartbeat_at = self._heartbeat_at
            heartbeat_seq = self._heartbeat_seq
            heartbeat_alive = self._heartbeat_alive
            now = time.monotonic()
        if not heartbeat_alive:
            return
        lag_s = now - heartbeat_at
        if lag_s < self.threshold_s:
            if self._stall_started_at is not None:
                observed_duration_ms = round((now - self._stall_started_at) * 1000)
                self._write({
                    "type": "stall_ended",
                    # Keep duration_ms for existing consumers. Both spans end
                    # when recovery is observed, possibly after the heartbeat
                    # actually resumed. Neither is a lower bound on blocking.
                    "duration_ms": observed_duration_ms,
                    "observed_duration_ms": observed_duration_ms,
                    "heartbeat_to_observation_span_ms": (
                        (self._stall_detected_lag_ms or 0) + observed_duration_ms
                    ),
                    "heartbeat_alive": True,
                    "heartbeat_seq": heartbeat_seq,
                })
                self._stall_started_at = None
                self._stall_detected_lag_ms = None
            return
        events: list[dict[str, Any]] = []
        if self._stall_started_at is None:
            self._stall_started_at = now
            self._stall_detected_lag_ms = round(lag_s * 1000)
            events.append({
                "type": "stall_started",
                "lag_ms": self._stall_detected_lag_ms,
                "heartbeat_age_ms": self._stall_detected_lag_ms,
                "heartbeat_alive": True,
                "heartbeat_seq": heartbeat_seq,
                "heartbeat_started": heartbeat_seq > 0,
                "detected_monotonic_s": now,
            })
        if now - self._last_stack_at >= self.stack_interval_s:
            self._last_stack_at = now
            events.append(self._stack_sample(detected_heartbeat_seq=heartbeat_seq))
        # Capture before any filesystem writes. The main thread may progress
        # while this thread logs; write timestamps must not stand in for the
        # time at which a frame was observed.
        for event in events:
            self._write(event)

    def _stack_sample(self, *, detected_heartbeat_seq: int) -> dict[str, Any]:
        with self._heartbeat_lock:
            heartbeat_at = self._heartbeat_at
            seq_before = self._heartbeat_seq
            alive_before = self._heartbeat_alive
        capture_monotonic_s = time.monotonic()
        capture_ts = time.time()
        stack = self._main_stack()
        capture_finished_monotonic_s = time.monotonic()
        with self._heartbeat_lock:
            seq_after = self._heartbeat_seq
            alive_after = self._heartbeat_alive
        age_ms = round((capture_monotonic_s - heartbeat_at) * 1000)
        return {
            "type": "stack_sample",
            "lag_ms": age_ms,
            "heartbeat_age_ms": age_ms,
            "heartbeat_alive": alive_after,
            "heartbeat_started": seq_before > 0,
            "heartbeat_seq": seq_before,
            "detected_heartbeat_seq": detected_heartbeat_seq,
            "heartbeat_seq_before": seq_before,
            "heartbeat_seq_after": seq_after,
            # An unchanged sequence is necessary for attribution to the
            # detected window; it does not prove a frame caused that delay.
            "same_heartbeat_window": (
                alive_before and alive_after
                and detected_heartbeat_seq == seq_before == seq_after
            ),
            "capture_ts": capture_ts,
            "capture_monotonic_s": capture_monotonic_s,
            "capture_finished_monotonic_s": capture_finished_monotonic_s,
            "stack": stack,
        }

    def _main_stack(self) -> list[dict[str, Any]]:
        frame = sys._current_frames().get(self._main_thread_id)
        stack: list[dict[str, Any]] = []
        try:
            while frame is not None and len(stack) < _MAX_STACK_FRAMES:
                stack.append({
                    "file": frame.f_code.co_filename,
                    "line": frame.f_lineno,
                    "function": frame.f_code.co_name,
                    # Preserve the frame shape without linecache's source
                    # reads, filesystem stats, or serialization of literals.
                    "code": None,
                })
                frame = frame.f_back
        finally:
            # Do not retain live frame references after sampling.
            del frame
        stack.reverse()
        return stack

    def _write(
        self, payload: dict[str, Any], *, force: bool = False, reserve_bytes: int = 0,
    ) -> bool:
        if not force and self._event_count >= _MAX_EVENTS:
            return False
        encoded = (json.dumps({"ts": time.time(), **payload}, ensure_ascii=False) + "\n").encode()
        with self._write_lock:
            reserved = reserve_bytes if force else (
                _RESERVED_SUMMARY_BYTES
                + (_RESERVED_TIMELINE_BYTES if self.loop_lag_enabled else 0)
            )
            byte_limit = _MAX_BYTES - reserved
            if (
                (not force and self._event_count >= _MAX_EVENTS)
                or self._bytes_written + len(encoded) > byte_limit
            ):
                return False
            try:
                with self.output_path.open("ab") as stream:
                    stream.write(encoded)
            except OSError as exc:
                log.warning("gateway.stall_diagnostics_write_failed", error=str(exc))
                self._stop.set()
                return False
            self._event_count += 1
            self._bytes_written += len(encoded)
            return True
