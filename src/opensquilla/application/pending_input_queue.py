"""Transport-neutral durable pending-input queue use cases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from opensquilla.session_key import canonicalize_session_key


@dataclass(frozen=True, slots=True)
class PendingInputRequest:
    session_key: str
    attributes: Mapping[str, Any]
    pending_input_id: str | None = None
    expected_revision: int | None = None


class PendingInputQueuePort(Protocol):
    async def enqueue(self, request: PendingInputRequest) -> Mapping[str, Any]: ...

    async def list(self, request: PendingInputRequest) -> Mapping[str, Any]: ...

    async def update(self, request: PendingInputRequest) -> Mapping[str, Any]: ...

    async def reorder(self, request: PendingInputRequest) -> Mapping[str, Any]: ...

    async def cancel(self, request: PendingInputRequest) -> Mapping[str, Any]: ...

    async def dispatch(self, request: PendingInputRequest) -> Mapping[str, Any]: ...

    async def steer(self, request: PendingInputRequest) -> Mapping[str, Any]: ...


class PendingInputQueue:
    """Own queue identity and revision invariants across all seven use cases."""

    def __init__(self, port: PendingInputQueuePort) -> None:
        self._port = port

    async def enqueue(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._port.enqueue(self._normalize(request, require_id=True))

    async def list(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._port.list(self._normalize(request))

    async def update(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._port.update(
            self._normalize(request, require_id=True, require_revision=True)
        )

    async def reorder(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._port.reorder(self._normalize(request))

    async def cancel(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._port.cancel(self._normalize(request, require_id=True))

    async def dispatch(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._port.dispatch(self._normalize(request, require_id=True))

    async def steer(self, request: PendingInputRequest) -> Mapping[str, Any]:
        return await self._port.steer(
            self._normalize(request, require_id=True, require_revision=True)
        )

    @staticmethod
    def _normalize(
        request: PendingInputRequest,
        *,
        require_id: bool = False,
        require_revision: bool = False,
    ) -> PendingInputRequest:
        key = canonicalize_session_key(request.session_key)
        if not key:
            raise ValueError("session_key must be non-empty")
        pending_input_id = request.pending_input_id
        if pending_input_id is not None:
            pending_input_id = pending_input_id.strip()
        if require_id and not pending_input_id:
            raise ValueError("pending_input_id must be non-empty")
        if require_revision and (
            request.expected_revision is None or request.expected_revision < 1
        ):
            raise ValueError("expected_revision must be positive")
        return replace(
            request,
            session_key=key,
            pending_input_id=pending_input_id,
        )


__all__ = ["PendingInputQueue", "PendingInputQueuePort", "PendingInputRequest"]
