"""Content-free timing records for local compaction work and durable commits."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

import structlog

log = structlog.get_logger(__name__)


def report_compaction_diagnostic(event: str, **fields: object) -> None:
    """Emit host metadata without making logging a compaction prerequisite."""
    try:
        log.info(event, **fields)
    except Exception:
        # Diagnostics must not prevent work or replace its original failure.
        pass


@contextmanager
def compaction_diagnostic_phase(
    *, compaction_id: str | None, phase: str, step: str,
) -> Iterator[None]:
    """Record entry before work begins, including failed or cancelled exits.

    Step names are fixed host labels. Exception messages and values from the
    transcript must never be attached to these records. A start without an end
    can be correlated with the owning process's exit diagnostics.
    """

    fields = {
        "compaction_id": compaction_id, "pid": os.getpid(), "phase": phase, "step_name": step,
    }
    started_at = time.monotonic()
    report_compaction_diagnostic("compaction.phase_started", **fields, status="started")
    status = "completed"
    error_type = None
    try:
        yield
    except BaseException as exc:
        status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
        if isinstance(exc, TimeoutError):
            status = "timed_out"
        error_type = type(exc).__name__
        raise
    finally:
        report_compaction_diagnostic(
            "compaction.phase_finished", **fields,
            duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
            status=status, error_type=error_type,
        )
