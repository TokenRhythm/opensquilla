"""Runtime bridge for live channel reconciliation.

Channel adapters are built by boot from the live config; the channel CRUD
RPCs change ``config.channels`` in-place without a restart. Boot installs a
reconciler here so the RPC layer can make the running adapters match the
updated config immediately — add, remove, or rebuild-and-swap, one channel at
a time. Webhook-mode adapters stay restart-gated (their HTTP routes are bound
at boot), and a ``None`` reconciler means everything is restart-gated.

Mirrors ``dream_bridge``: boot owns the wiring, this module only holds the
lookup.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

# Returns the per-channel outcome map from ChannelManager.reconcile.
ReconcileChannelsFn = Callable[[], Awaitable[dict[str, str]]]

_reconciler: ReconcileChannelsFn | None = None


def register_channels_reconciler(fn: ReconcileChannelsFn | None) -> None:
    """Boot installs the reconciler once channel dependencies are ready."""
    global _reconciler
    _reconciler = fn


def get_channels_reconciler() -> ReconcileChannelsFn | None:
    """RPC + tests read the live reconciler; ``None`` means restart-gated."""
    return _reconciler


def reset_channels_reconciler() -> None:
    """Clear the module-level singleton (gateway shutdown / tests)."""
    global _reconciler
    _reconciler = None


__all__ = [
    "ReconcileChannelsFn",
    "get_channels_reconciler",
    "register_channels_reconciler",
    "reset_channels_reconciler",
]
