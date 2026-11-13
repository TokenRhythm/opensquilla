"""Narrow seams between the pet service and the gateway (PRD §7.4).

The pet package depends only on these Protocols plus stdlib/httpx. The boot
path injects concrete implementations; tests inject fakes. This keeps the
service portable to a future built-in plugin without importing gateway
private modules.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

# An in-process session-event observer callback: (session_key, event_name, payload).
EventObserver = Callable[[str, str, dict[str, Any]], None]


@runtime_checkable
class SessionEventSource(Protocol):
    """The live session-event funnel the pet service subscribes to."""

    def add_local_observer(self, callback: EventObserver) -> Callable[[], None]:
        """Register an observer; return an idempotent removal handle."""
        ...


@runtime_checkable
class RpcInvoker(Protocol):
    """Invoke a gateway RPC method in-process with operator authority.

    Implementations wrap ``get_dispatcher().dispatch`` with a fixed operator
    :class:`RpcContext`, so the pet dispatch path reuses the exact
    ``sessions.resolve`` / ``sessions.send`` admission code (no bypass).
    Raises on transport/RPC failure; returns the response payload on success.
    """

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        ...


@runtime_checkable
class Redactor(Protocol):
    """Mask credential-shaped material and truncate free text for the pet."""

    def __call__(self, text: str, *, max_len: int = ...) -> str:
        ...


@runtime_checkable
class SessionMetadataResolver(Protocol):
    """Resolve a session's friendly title and project workspace path.

    Returns ``(title, project_path)`` (either may be empty). Used to label the
    pet notification with the project name instead of the raw session key, and
    to render ``{session}`` in message templates. Best-effort: implementations
    should return empty strings rather than raise on lookup failure.
    """

    async def __call__(self, session_key: str) -> tuple[str, str]:
        ...


__all__ = [
    "EventObserver",
    "SessionEventSource",
    "RpcInvoker",
    "Redactor",
    "SessionMetadataResolver",
]
