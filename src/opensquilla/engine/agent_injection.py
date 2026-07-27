"""In-process input providers used at safe agent boundaries."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PendingInputProvider(Protocol):
    """Port for draining prompts queued for injection into the active agent turn.

    Implementations must be accessed from the same asyncio event loop as the
    agent. This is the in-process injection channel contract: append and drain
    occur under single-threaded cooperative scheduling, so a synchronous
    ``drain_pending`` call has atomic semantics without locks or awaits.
    """

    def drain_pending(self) -> list[str]:
        """Return all pending injection text and clear the provider."""


@runtime_checkable
class UserInputProvider(Protocol):
    """Port for a structured, deferred answer to one tool call.

    Unlike :class:`PendingInputProvider`, this protocol never turns an answer
    into a new user message. The agent emits an intermediate tool result,
    waits here, and then supplies the answer as the final result for the same
    ``tool_use_id``.
    """

    def open_request(
        self,
        *,
        session_key: str,
        task_id: str,
        tool_use_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Register a request and return its public payload with a request id."""

    async def wait_for_response(self, request_id: str) -> dict[str, Any]:
        """Wait until the registered request receives a validated response."""

    def cancel_request(self, request_id: str) -> None:
        """Cancel and forget a request whose owning turn is unwinding."""


class ListPendingInputProvider:
    """Default in-process pending-input provider backed by a list."""

    def __init__(self) -> None:
        self._pending: list[str] = []

    def append(self, text: str) -> None:
        """Queue one pending input, ignoring empty or whitespace-only text."""

        if not text.strip():
            return
        self._pending.append(text)

    def drain_pending(self) -> list[str]:
        """Return queued inputs in order and reset the provider."""

        pending = list(self._pending)
        self._pending = []
        return pending

    def __len__(self) -> int:
        return len(self._pending)

    def __bool__(self) -> bool:
        return bool(self._pending)
