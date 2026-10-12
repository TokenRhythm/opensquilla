"""Runtime callback seams for the self-learning loop.

The offline orchestrator must not import the engine (that would close an
``engine -> squilla_router -> engine`` package cycle), yet a promotion or
rollback has to invalidate the engine-side strategy cache in-process. The
engine's router step registers its invalidator here at import time; the
orchestrator only ever calls the seam. Unregistered (unit tests, standalone
trainers) the seam is a no-op, matching the loop's fail-open posture.
"""

from __future__ import annotations

from collections.abc import Callable

_cache_invalidator: Callable[[], None] | None = None


def set_cache_invalidator(fn: Callable[[], None] | None) -> None:
    """Register (or clear) the engine-side strategy-cache invalidator."""

    global _cache_invalidator  # noqa: PLW0603
    _cache_invalidator = fn


def invalidate_router_cache() -> bool:
    """Invoke the registered invalidator. Returns False when none is set."""

    if _cache_invalidator is None:
        return False
    _cache_invalidator()
    return True
