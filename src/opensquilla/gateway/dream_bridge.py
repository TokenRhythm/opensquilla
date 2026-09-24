"""Runtime bridge for reconciling Dream jobs after their configuration changes.

Boot owns scheduler registration; settings updates invoke this callback after
committing the live config so Dream remains independently configurable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

ReconcileDreamCronsFn = Callable[[], Awaitable[None]]

_reconciler: ReconcileDreamCronsFn | None = None


def register_dream_reconciler(fn: ReconcileDreamCronsFn | None) -> None:
    """Boot installs the reconciler once the scheduler is ready."""
    global _reconciler
    _reconciler = fn


def get_dream_reconciler() -> ReconcileDreamCronsFn | None:
    """RPC + tests read the live reconciler; ``None`` means restart-gated."""
    return _reconciler


def reset_dream_reconciler() -> None:
    """Clear the module-level singleton (gateway shutdown / tests)."""
    global _reconciler
    _reconciler = None


__all__ = [
    "ReconcileDreamCronsFn",
    "get_dream_reconciler",
    "register_dream_reconciler",
    "reset_dream_reconciler",
]
