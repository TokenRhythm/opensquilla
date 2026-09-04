"""Transport-neutral turn admission, cancellation, and steering use cases.

The Gateway owns v4 aliases, authentication, guest projection, and ``RpcContext``.
This Module supplies the single application entry point used by the canonical
WebChat methods and their session-oriented compatibility names.  The mature
durable ingress and runtime cancellation state machines remain behind the Port.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, TypedDict

from opensquilla.application.turn_acceptance_ports import (
    AdmissionCollaborationSnapshot,
    AdmissionRoutingSnapshot,
)
from opensquilla.application.turn_input import (
    DocumentTurnContext,
    IncomingTurnSource,
    MemoryCapturePolicy,
    PlanAdmissionContext,
)
from opensquilla.session_key import canonicalize_session_key

type TurnAdmissionSurface = Literal["webchat", "session"]
type TurnSteerMode = Literal["durable", "legacy"]
type InitialCollaborationMode = Literal["default", "plan"]
type InitialRoutingMode = Literal["direct", "router", "ensemble"]


class AcceptedCollaboration(TypedDict):
    mode: str
    revision: int


class AcceptedRouting(TypedDict):
    mode: str


class AdmitTurnResult(TypedDict, total=False):
    """Stable application projection for an accepted or replayed turn."""

    ok: bool | None
    status: str | None
    accepted: bool | None
    sessionKey: str | None
    session_key: str | None
    key: str | None
    session_id: str | None
    message_id: str | None
    user_message_id: str | None
    client_message_id: str | None
    clientMessageId: str | None
    client_request_id: str | None
    clientRequestId: str | None
    task_id: str | None
    taskId: str | None
    turn_id: str | None
    replayed: bool | None
    instant_accept: bool | None
    task_status: str | None
    taskStatus: str | None
    terminal_reason: str | None
    terminalReason: str | None
    terminal_message: str | None
    terminalMessage: str | None
    reason: str | None
    acceptedPromptAnnotationIds: list[str] | None
    accepted_prompt_annotation_ids: list[str] | None
    surface_id: str | None
    surfaceId: str | None
    acceptedCollaboration: AcceptedCollaboration
    collaboration: AdmissionCollaborationSnapshot
    acceptedRouting: AcceptedRouting
    routing: AdmissionRoutingSnapshot


class CancelTurnResult(TypedDict, total=False):
    """Stable application projection for exact or session-wide cancellation."""

    ok: bool | None
    status: str | None
    aborted: bool | None
    sessionKey: str | None
    session_key: str | None
    key: str | None
    reason: str | None
    task_id: str | None
    taskId: str | None
    cancelled: bool | None
    cancelled_tasks: int
    cancelled_processes: int
    cancelled_sessions: int
    cancelled_compactions: int


class SteerTurnResult(TypedDict, total=False):
    """Stable application projection for durable and legacy steering."""

    status: str | None
    accepted: bool | None
    replayed: bool | None
    key: str | None
    session_key: str | None
    session_id: str | None
    expected_turn_id: str | None
    task_id: str | None
    turn_id: str | None
    user_message_id: str | None
    client_request_id: str | None
    client_message_id: str | None
    surface_id: str | None
    disposition: str | None
    revision: int | None
    promoted_turn_id: str | None
    promoted_from_turn_id: str | None
    active_turn_id: str | None
    applied_iteration: int | None
    model_call_id: str | None
    fallback_safe: bool | None
    failure_code: str | None
    retryable: bool | None
    recovery: str | None
    reason: str | None
    steer_capability: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class PendingInputGuard:
    pending_input_id: str
    request_fingerprint: str
    expected_revision: int
    source_scope: str | None = None


@dataclass(frozen=True, slots=True)
class AdmitTurn:
    session_key: str
    message: str
    surface: TurnAdmissionSurface
    source: IncomingTurnSource = IncomingTurnSource()
    capture: MemoryCapturePolicy = MemoryCapturePolicy()
    client_request_id: str = ""
    request_fingerprint: str = ""
    source_scope: str = ""
    explicit_request_id: bool = False
    client_message_id: str | None = None
    surface_id: str | None = None
    attachments: tuple[dict[str, Any], ...] = ()
    intent: str = "continue"
    intent_was_provided: bool = False
    fork_before_message_id: str | None = None
    workspace_id: str | None = None
    prompt_annotation_ids: tuple[str, ...] = ()
    document_context: DocumentTurnContext | None = None
    display_text: str | None = None
    queue_mode: str | None = None
    initial_collaboration_mode: InitialCollaborationMode | None = None
    initial_routing_mode: InitialRoutingMode | None = None
    pending_input: PendingInputGuard | None = None

    # Only internal Plan/background producers supply these controls. Gateway
    # decoders never derive them from untrusted request fields.
    plan: PlanAdmissionContext | None = None
    trusted_run_kind: str | None = None
    explicit_intent_registered: bool = False


@dataclass(frozen=True, slots=True)
class CancelTurn:
    session_key: str
    surface: TurnAdmissionSurface
    task_id: str | None
    task_scoped: bool
    source: str | None


@dataclass(frozen=True, slots=True)
class SteerTurn:
    session_key: str
    message: str
    mode: TurnSteerMode
    expected_turn_id: str | None = None
    client_request_id: str | None = None
    client_message_id: str | None = None
    surface_id: str | None = None
    source_scope: str = ""
    request_fingerprint: str = ""
    is_web_source: bool = True
    has_non_text_input: bool = False
    pending_input: PendingInputGuard | None = None


class TurnIngressPort(Protocol):
    """Durably accept one typed turn command."""

    async def admit(self, command: AdmitTurn) -> AdmitTurnResult: ...


class TurnCancellationPort(Protocol):
    """Cancel runtime work without widening an exact-task request."""

    async def cancel(self, command: CancelTurn) -> CancelTurnResult: ...


class TurnSteeringPort(Protocol):
    """Attach input to the selected running-turn steering state machine."""

    async def steer(self, command: SteerTurn) -> SteerTurnResult: ...


class TurnAdmission:
    """One application implementation for canonical and legacy turn commands."""

    def __init__(
        self,
        *,
        ingress: TurnIngressPort,
        cancellation: TurnCancellationPort,
        steering: TurnSteeringPort,
    ) -> None:
        self._ingress = ingress
        self._cancellation = cancellation
        self._steering = steering

    async def admit(self, command: AdmitTurn) -> AdmitTurnResult:
        key = self._session_key(command.session_key)
        if not isinstance(command.message, str):
            raise ValueError("message must be a string")
        self._validate_pending_guard(command.pending_input)
        return await self._ingress.admit(replace(command, session_key=key))

    async def cancel(self, command: CancelTurn) -> CancelTurnResult:
        key = self._session_key(command.session_key)
        task_id = command.task_id.strip() if isinstance(command.task_id, str) else None
        if task_id == "":
            task_id = None
        if command.task_scoped and task_id is None:
            # Never broaden an exact-task cancellation into a session-wide one.
            return {"aborted": False, "key": key, "reason": "task_id_required"}
        return await self._cancellation.cancel(replace(command, session_key=key, task_id=task_id))

    async def steer(self, command: SteerTurn) -> SteerTurnResult:
        key = self._session_key(command.session_key)
        if not isinstance(command.message, str):
            raise ValueError("message must be a string")
        if not command.message.strip():
            raise ValueError("message must not be blank")
        self._validate_pending_guard(command.pending_input, require_source=True)
        return await self._steering.steer(replace(command, session_key=key))

    @staticmethod
    def _validate_pending_guard(
        guard: PendingInputGuard | None,
        *,
        require_source: bool = False,
    ) -> None:
        if guard is None:
            return
        if not guard.pending_input_id.strip() or not guard.request_fingerprint.strip():
            raise ValueError("pending input identity must be non-empty")
        if guard.expected_revision < 1:
            raise ValueError("pending input revision must be positive")
        if require_source and not (guard.source_scope or "").strip():
            raise ValueError("pending input source scope must be non-empty")

    @staticmethod
    def _session_key(value: str) -> str:
        key = canonicalize_session_key(value)
        if not key:
            raise ValueError("session_key must be non-empty")
        return key


__all__ = [
    "AdmitTurn",
    "AdmitTurnResult",
    "CancelTurn",
    "CancelTurnResult",
    "InitialCollaborationMode",
    "InitialRoutingMode",
    "PendingInputGuard",
    "SteerTurn",
    "SteerTurnResult",
    "TurnAdmission",
    "TurnCancellationPort",
    "TurnIngressPort",
    "TurnAdmissionSurface",
    "TurnSteeringPort",
    "TurnSteerMode",
]
