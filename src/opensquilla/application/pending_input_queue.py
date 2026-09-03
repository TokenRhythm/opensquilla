"""Transport-neutral durable pending-input queue use cases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol, TypedDict

from opensquilla.application.turn_admission import AdmitTurnResult, SteerTurnResult
from opensquilla.session_key import canonicalize_session_key


class PendingInputAttachmentProjection(TypedDict, total=False):
    name: str | None
    mime: str | None
    type: str | None
    size: int | None


class PendingInputProjection(TypedDict, total=False):
    """Public queue row without internal material capabilities."""

    pendingInputId: str
    pending_input_id: str
    sessionKey: str
    session_key: str
    clientRequestId: str
    client_request_id: str
    clientMessageId: str
    client_message_id: str
    requestFingerprint: str
    request_fingerprint: str
    message: str
    intent: str | None
    attachments: list[PendingInputAttachmentProjection]
    position: int
    revision: int
    createdAt: int
    updatedAt: int
    replayed: bool
    schemaVersion: int
    displayText: str
    confirmedPlainText: bool
    promptAnnotationIds: list[str]


class PendingInputEnqueueResult(PendingInputProjection, total=False):
    status: str


class PendingInputListResult(TypedDict, total=False):
    sessionKey: str
    items: list[PendingInputProjection]
    maxPending: int


class PendingInputUpdateResult(PendingInputProjection, total=False):
    status: str


class PendingInputReorderResult(TypedDict, total=False):
    status: str
    sessionKey: str
    items: list[PendingInputProjection]


class PendingInputCancelResult(TypedDict, total=False):
    status: str
    cancelled: bool
    alreadyMissing: bool
    pendingInputId: str
    sessionKey: str


@dataclass(frozen=True, slots=True)
class PendingInputRequest:
    session_key: str
    attributes: Mapping[str, Any]
    pending_input_id: str | None = None
    expected_revision: int | None = None


class PendingInputQueuePort(Protocol):
    async def enqueue(self, request: PendingInputRequest) -> PendingInputEnqueueResult: ...

    async def list(self, request: PendingInputRequest) -> PendingInputListResult: ...

    async def update(self, request: PendingInputRequest) -> PendingInputUpdateResult: ...

    async def reorder(self, request: PendingInputRequest) -> PendingInputReorderResult: ...

    async def cancel(self, request: PendingInputRequest) -> PendingInputCancelResult: ...

    async def dispatch(self, request: PendingInputRequest) -> AdmitTurnResult: ...

    async def steer(self, request: PendingInputRequest) -> SteerTurnResult: ...


class PendingInputQueue:
    """Own queue identity and revision invariants across all seven use cases."""

    def __init__(self, port: PendingInputQueuePort) -> None:
        self._port = port

    async def enqueue(self, request: PendingInputRequest) -> PendingInputEnqueueResult:
        return await self._port.enqueue(self._normalize(request, require_id=True))

    async def list(self, request: PendingInputRequest) -> PendingInputListResult:
        return await self._port.list(self._normalize(request))

    async def update(self, request: PendingInputRequest) -> PendingInputUpdateResult:
        return await self._port.update(
            self._normalize(request, require_id=True, require_revision=True)
        )

    async def reorder(self, request: PendingInputRequest) -> PendingInputReorderResult:
        return await self._port.reorder(self._normalize(request))

    async def cancel(self, request: PendingInputRequest) -> PendingInputCancelResult:
        return await self._port.cancel(self._normalize(request, require_id=True))

    async def dispatch(self, request: PendingInputRequest) -> AdmitTurnResult:
        return await self._port.dispatch(self._normalize(request, require_id=True))

    async def steer(self, request: PendingInputRequest) -> SteerTurnResult:
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


__all__ = [
    "PendingInputAttachmentProjection",
    "PendingInputCancelResult",
    "PendingInputEnqueueResult",
    "PendingInputListResult",
    "PendingInputProjection",
    "PendingInputQueue",
    "PendingInputQueuePort",
    "PendingInputReorderResult",
    "PendingInputRequest",
    "PendingInputUpdateResult",
]
