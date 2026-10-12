"""Runtime bridge for live dream-cron reconciliation.

The ``memory_dream`` cron jobs are registered by boot from the live config;
config RPC edits (notably the self-learning -> dream linkage) change
``memory.dream.*`` in-place without a restart. Boot installs its idempotent
registrar here so the RPC layer can re-reconcile the jobs against the updated
config immediately — otherwise the linkage would flip the flags while the
scheduler still has no (or paused) dream jobs until the next restart, which is
exactly the silent never-trains gap the linkage exists to close.

Mirrors ``auto_propose_bridge``: boot owns the wiring, this module only holds
the lookup.
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
