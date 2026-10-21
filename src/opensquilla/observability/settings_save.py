"""Opt-in, payload-free timing for a local settings-save investigation."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

import structlog

_METHODS = frozenset({
    "config.set", "config.patch", "config.patch.safe", "config.apply", "config.reload",
})
_current: ContextVar[tuple[str, str, str, float] | None] = ContextVar(
    "settings_save_timing", default=None,
)
_log = structlog.get_logger(__name__)


def _enabled(request_id: str, method: str) -> bool:
    return (
        isinstance(method, str) and isinstance(request_id, str)
        and method in _METHODS
        and os.environ.get("OPENSQUILLA_SETTINGS_SAVE_DIAGNOSTICS") == "1"
    )


def _identity(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _emit(identity: str, connection: str, method: str, phase: str, started: float) -> None:
    # Diagnostics must not change a mutation's success/failure semantics.
    try:
        now = time.perf_counter()
        _log.info(
            "config.save_timing", request_id=identity, connection_id=connection,
            method=method, phase=phase, elapsed_ms=round((now - started) * 1000, 3),
            monotonic_ms=round(now * 1000, 3),
        )
    except Exception:
        pass


def settings_save_transport_stage(
    request_id: str, method: str, phase: str, connection_id: str = "",
) -> None:
    if _enabled(request_id, method):
        _emit(_identity(request_id), _identity(connection_id), method, phase, time.perf_counter())


@contextmanager
def settings_save_timing(
    request_id: str, method: str, connection_id: str = "",
) -> Iterator[None]:
    if not _enabled(request_id, method):
        yield
        return
    token = _current.set((
        _identity(request_id), _identity(connection_id), method, time.perf_counter(),
    ))
    try:
        settings_save_stage("dispatch")
        yield
    finally:
        settings_save_stage("dispatch_finished")
        _current.reset(token)


def settings_save_stage(phase: str) -> None:
    current = _current.get()
    if current is not None:
        identity, connection, method, started = current
        _emit(identity, connection, method, phase, started)
