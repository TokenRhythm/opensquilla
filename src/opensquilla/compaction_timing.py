"""Shared compaction timing policy, independent of session/engine imports.

The provider keeps its ordinary I/O timeout.  Compaction adds one absolute
operation deadline and an idle guard that only semantic model progress renews.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

DEFAULT_COMPACTION_TOTAL_TIMEOUT_SECONDS = 600.0
DEFAULT_COMPACTION_REQUEST_TIMEOUT_SECONDS = 120.0


def _positive_seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def resolve_compaction_total_timeout(value: object = None) -> float:
    """Keep explicit positive legacy budgets; otherwise use the bounded default."""
    return _positive_seconds(value) or DEFAULT_COMPACTION_TOTAL_TIMEOUT_SECONDS


def resolve_compaction_idle_timeout(
    request_timeout: object,
    legacy_timeout: object = None,
) -> float:
    """An explicit legacy timeout selects the idle guard, never the I/O policy.

    Unset/invalid legacy values inherit the actual normal request timeout.  A
    saved explicit 90 remains 90; defaults are no longer silently fixed to 90.
    """
    return (
        _positive_seconds(legacy_timeout)
        or _positive_seconds(request_timeout)
        or DEFAULT_COMPACTION_REQUEST_TIMEOUT_SECONDS
    )


class CompactionIdleTimeoutError(TimeoutError):
    """The summary stream exhausted its semantic no-progress allowance."""


class CompactionOperationTimeoutError(TimeoutError):
    """The immutable operation/parent deadline ended the stream."""


class CompactionProgress:
    """Renew only the idle deadline, without extending the operation deadline."""

    def __init__(
        self,
        timer: asyncio.Timeout,
        *,
        idle_timeout_seconds: float,
        deadline_at_monotonic: float | None,
        clock: Callable[[], float],
    ) -> None:
        self._timer = timer
        self._idle = idle_timeout_seconds
        self._deadline = deadline_at_monotonic
        self._clock = clock

    def timeout_error(self) -> TimeoutError:
        if self._deadline is not None and self._clock() >= self._deadline:
            return CompactionOperationTimeoutError("compaction operation deadline expired")
        return CompactionIdleTimeoutError(
            f"compaction made no semantic progress for {self._idle:g} seconds"
        )

    def observe(self, event: object) -> None:
        now = self._clock()
        scheduled = self._timer.when()
        # Also check synchronous streams: an iterator yielding without awaiting
        # must not keep renewing a timer whose cancellation callback is pending.
        if scheduled is not None and now >= scheduled:
            raise self.timeout_error()
        if getattr(event, "kind", "") not in {"text_delta", "reasoning_delta"}:
            return
        text = getattr(event, "text", "")
        if not isinstance(text, str) or not text.strip():
            return
        idle_deadline = now + self._idle
        self._timer.reschedule(
            min(idle_deadline, self._deadline)
            if self._deadline is not None
            else idle_deadline
        )


@asynccontextmanager
async def compaction_progress_timeout(
    *,
    idle_timeout_seconds: float,
    deadline_at_monotonic: float | None = None,
) -> AsyncIterator[CompactionProgress]:
    """Watch semantic idle time under the existing absolute operation deadline.

    Caller cancellation and provider-raised timeouts retain their original
    type.  Only expiration owned by this guard is translated to its cause.
    """
    idle = resolve_compaction_idle_timeout(idle_timeout_seconds)
    clock = asyncio.get_running_loop().time
    idle_deadline = clock() + idle
    timer = asyncio.timeout_at(
        min(idle_deadline, deadline_at_monotonic)
        if deadline_at_monotonic is not None
        else idle_deadline
    )
    progress = CompactionProgress(
        timer,
        idle_timeout_seconds=idle,
        deadline_at_monotonic=deadline_at_monotonic,
        clock=clock,
    )
    try:
        async with timer:
            yield progress
    except TimeoutError as exc:
        if timer.expired():
            raise progress.timeout_error() from exc
        raise
