"""Post-ready indexing of legacy transcript content lengths.

The backfill is deliberately outside the core readiness path.  It uses the
storage-owned bounded batches and may be cancelled at any point; rows that
were not committed remain NULL and are picked up after restart.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)
_DEFAULT_INITIAL_DELAY_S = 60.0


async def run_content_length_backfill(
    storage: object,
    *,
    batch_size: int = 4,
    initial_delay_s: float = _DEFAULT_INITIAL_DELAY_S,
    notify_directory_changed: Callable[[], Awaitable[None]] | None = None,
) -> None:
    """Resume the legacy transcript byte-length index after readiness."""

    total = 0
    attempted_batch = False
    should_notify = False
    try:
        # A legacy body scan can consume substantial Windows file-cache and
        # CPU bandwidth even on a separate SQLite connection.  Keep it out of
        # the first-paint/reconnect window; this is maintenance work, not a
        # prerequisite for serving the core Gateway.
        if initial_delay_s > 0:
            await asyncio.sleep(initial_delay_s)
        while True:
            backfill = getattr(storage, "backfill_transcript_content_lengths")
            attempted_batch = True
            count = await backfill(batch_size=batch_size, max_batches=1)
            if not count:
                should_notify = total > 0
                break
            total += int(count)
            log.info("gateway.content_length_backfill_progress", rows=total)
            # Yield to interactive reads and writes between tiny transactions.
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        log.info("gateway.content_length_backfill_cancelled", rows=total)
        raise
    except Exception as exc:
        # This task must never affect core readiness.  A later restart can
        # resume NULL rows, so keep the failure visible and exit cleanly.
        log.warning(
            "gateway.content_length_backfill_failed",
            rows=total,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        # A batch may have committed before its connection cleanup failed,
        # without returning a count. Conservatively invalidate once on failure.
        should_notify = attempted_batch

    # A single directory invalidation covers all committed batches. Keep it out
    # of finally: cancellation/shutdown must not wait for a fresh broadcast.
    if should_notify and notify_directory_changed is not None:
        try:
            await notify_directory_changed()
        except Exception:
            log.warning("gateway.content_length_backfill_notify_failed", exc_info=True)


__all__ = ["run_content_length_backfill"]
