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


def current_turn_context() -> dict[str, Any] | None:
    """Return an isolated copy of the current durable turn identity."""
    value = _CURRENT_TURN_CONTEXT.get()
    return dict(value) if value is not None else None


@contextmanager
def turn_context_scope(value: Mapping[str, Any] | None) -> Iterator[None]:
    """Apply *value* to transcript writes in this async execution context."""
    normalized = dict(value) if value is not None else None
    token = _CURRENT_TURN_CONTEXT.set(normalized)
    try:
        yield
    finally:
        _CURRENT_TURN_CONTEXT.reset(token)


__all__ = ["current_turn_context", "turn_context_scope"]
