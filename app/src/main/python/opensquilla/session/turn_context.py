"""Per-turn transcript identity propagated through asynchronous turn execution.

The gateway owns the durable identity.  A ContextVar lets the shared turn loop
attach that identity to assistant/tool/system writes without widening every
provider and tool callback signature.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_CURRENT_TURN_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "opensquilla_current_turn_context",
    default=None,
)


def _copy_turn_context(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy turn metadata without sharing mutable activity marker records."""

    copied = dict(value)
    markers = value.get("activity_markers")
    if isinstance(markers, list):
        copied["activity_markers"] = [
            dict(marker) if isinstance(marker, Mapping) else marker for marker in markers
        ]
    return copied


def current_turn_context() -> dict[str, Any] | None:
    """Return an isolated copy of the current durable turn metadata."""
    value = _CURRENT_TURN_CONTEXT.get()
    return _copy_turn_context(value) if value is not None else None


def append_current_turn_activity_marker(marker: Mapping[str, Any]) -> bool:
    """Append one durable activity marker to the active turn, idempotently.

    The marker stays in the per-turn ``ContextVar`` until the assistant
    transcript entry is written, at which point the existing session append
    path persists it inside ``turn_context``.  A stable marker id prevents
    at-least-once lifecycle delivery from duplicating UI history.
    """

    marker_id = str(marker.get("id") or "").strip()
    current = _CURRENT_TURN_CONTEXT.get()
    if not marker_id or current is None:
        return False

    raw_markers = current.get("activity_markers")
    markers = (
        [dict(existing) for existing in raw_markers if isinstance(existing, Mapping)]
        if isinstance(raw_markers, list)
        else []
    )
    if any(str(existing.get("id") or "").strip() == marker_id for existing in markers):
        return False

    stored_marker = dict(marker)
    stored_marker["id"] = marker_id
    updated = dict(current)
    updated["activity_markers"] = [*markers, stored_marker]
    _CURRENT_TURN_CONTEXT.set(updated)
    return True


@contextmanager
def turn_context_scope(value: Mapping[str, Any] | None) -> Iterator[None]:
    """Apply *value* to transcript writes in this async execution context."""
    normalized = _copy_turn_context(value) if value is not None else None
    token = _CURRENT_TURN_CONTEXT.set(normalized)
    try:
        yield
    finally:
        _CURRENT_TURN_CONTEXT.reset(token)


__all__ = [
    "append_current_turn_activity_marker",
    "current_turn_context",
    "turn_context_scope",
]
