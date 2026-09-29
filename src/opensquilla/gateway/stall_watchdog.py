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
import traceback
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
    ) -> None:
        self.output_path = output_path
        self.threshold_s = threshold_s
        self.sample_interval_s = sample_interval_s
        self.stack_interval_s = stack_interval_s
        self._heartbeat_at = time.monotonic()
        self._main_thread_id = threading.get_ident()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._event_count = 0
        self._bytes_written = 0
        self._stall_started_at: float | None = None
        self._last_stack_at = 0.0

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
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("gateway.stall_diagnostics_unavailable", error=str(exc))
            return
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

    def beat(self) -> None:
        """Record progress from the event-loop task."""

        self._heartbeat_at = time.monotonic()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.sample_interval_s * 4))
        self._write({"type": "watchdog_stopped"})

    def _run(self) -> None:
        while not self._stop.wait(self.sample_interval_s):
            now = time.monotonic()
            lag_s = now - self._heartbeat_at
            if lag_s < self.threshold_s:
                if self._stall_started_at is not None:
                    self._write({
                        "type": "stall_ended",
                        "duration_ms": round((now - self._stall_started_at) * 1000),
                    })
                    self._stall_started_at = None
                continue
            if self._stall_started_at is None:
                self._stall_started_at = now
                self._write({"type": "stall_started", "lag_ms": round(lag_s * 1000)})
            if now - self._last_stack_at >= self.stack_interval_s:
                self._last_stack_at = now
                self._write({
                    "type": "stack_sample",
                    "lag_ms": round(lag_s * 1000),
                    "stack": self._main_stack(),
                })

    def _main_stack(self) -> list[dict[str, Any]]:
        frame = sys._current_frames().get(self._main_thread_id)
        if frame is None:
            return []
        return [
            {
                "file": item.filename,
                "line": item.lineno,
                "function": item.name,
                "code": item.line,
            }
            for item in traceback.extract_stack(frame)[-64:]
        ]

    def _write(self, payload: dict[str, Any]) -> None:
        if self._event_count >= _MAX_EVENTS:
            return
        encoded = (json.dumps({"ts": time.time(), **payload}, ensure_ascii=False) + "\n").encode()
        with self._write_lock:
            if self._event_count >= _MAX_EVENTS or self._bytes_written + len(encoded) > _MAX_BYTES:
                return
            try:
                with self.output_path.open("ab") as stream:
                    stream.write(encoded)
            except OSError as exc:
                log.warning("gateway.stall_diagnostics_write_failed", error=str(exc))
                self._stop.set()
                return
            self._event_count += 1
            self._bytes_written += len(encoded)

