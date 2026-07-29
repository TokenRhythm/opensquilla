"""In-process input providers used at safe agent boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class PendingInputApplication:
    """One claimed input batch that entered a provider request."""

    texts: tuple[str, ...]
    iteration: int
    model_call_id: str


@runtime_checkable
class PendingInputProvider(Protocol):
    """Port for claiming prompts queued for injection into the active agent turn.

    Implementations must be accessed from the same asyncio event loop as the
    agent. ``drain_pending`` claims one FIFO batch, but does not mean the model
    has seen it. The agent calls ``mark_applied`` only after the next provider
    stream has been created from a request containing that batch. A claimed
    batch that is never marked remains reclaimable by the runtime for promotion
    to a follow-up turn.
    """

    def drain_pending(self) -> list[str]:
        """Claim and return all pending injection text in FIFO order."""

    def peek_pending(self) -> list[str]:
        """Return the next FIFO batch without claiming or mutating it."""

    def mark_applied(self, *, iteration: int, model_call_id: str) -> Any:
        """Mark the claimed batch as included; implementations may persist async."""


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
        self._claimed: list[str] = []
        self._applications: list[PendingInputApplication] = []

    def append(self, text: str) -> None:
        """Queue one pending input, ignoring empty or whitespace-only text."""

        if not text.strip():
            return
        self._pending.append(text)

    def drain_pending(self) -> list[str]:
        """Claim queued inputs in order without declaring them applied."""

        if self._claimed:
            return []
        pending = list(self._pending)
        self._pending = []
        self._claimed = pending
        return pending

    def peek_pending(self) -> list[str]:
        """Return the next unclaimed FIFO batch without changing ownership."""

        return [] if self._claimed else list(self._pending)

    def mark_applied(self, *, iteration: int, model_call_id: str) -> None:
        """Record that the claimed batch entered the identified model call."""

        if not self._claimed:
            return
        self._applications.append(
            PendingInputApplication(
                texts=tuple(self._claimed),
                iteration=iteration,
                model_call_id=model_call_id,
            )
        )
        self._claimed = []

    def reclaim_pending(self) -> list[str]:
        """Return every not-yet-applied input and clear the provider."""

        pending = [*self._claimed, *self._pending]
        self._claimed = []
        self._pending = []
        return pending

    @property
    def applications(self) -> tuple[PendingInputApplication, ...]:
        """Return immutable application receipts in call order."""

        return tuple(self._applications)

    def __len__(self) -> int:
        return len(self._claimed) + len(self._pending)

    def __bool__(self) -> bool:
        return bool(self._claimed or self._pending)
