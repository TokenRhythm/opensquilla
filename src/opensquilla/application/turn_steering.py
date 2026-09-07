"""Expected-turn steering and legacy append rollback, without transport ownership."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

import structlog

from opensquilla.application.turn_admission import (
    PendingInputGuard,
    SteerTurn,
    SteerTurnResult,
)
from opensquilla.application.turn_input import (
    TurnRequestIdentity as SteeringIdentity,
)
from opensquilla.application.turn_input import complete_durable_ingress
from opensquilla.project_workspaces import ProjectWorkspaceGuard

log = structlog.get_logger(__name__)


class SteeringSession(Protocol):
    """Read-only identity used to prepare a same-session input."""

    @property
    def session_key(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def epoch(self) -> int: ...

    @property
    def workspace_id(self) -> str | None: ...


class SteeringTranscript(Protocol):
    """Prepared content identity; persistence owns the native record."""

    @property
    def content(self) -> str | None: ...

    @property
    def message_id(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def session_key(self) -> str: ...


class SteeringReceipt(Protocol):
    @property
    def request_fingerprint(self) -> str: ...

    @property
    def accepted_session_key(self) -> str: ...

    @property
    def session_id(self) -> str: ...

    @property
    def message_id(self) -> str: ...

    @property
    def task_id(self) -> str | None: ...

    @property
    def accepted_at(self) -> int: ...


class SteeringAcceptance(Protocol):
    @property
    def receipt(self) -> SteeringReceipt: ...

    @property
    def replayed(self) -> bool: ...


class SteeringIdentityConflictError(RuntimeError):
    """An accepted client identity was retried with different steering content."""


class SteeringPersistenceUnavailableError(RuntimeError):
    """The session cannot atomically persist a same-turn input."""


class SteeringDispositionReadError(RuntimeError):
    """The receipt is available but its latest transcript could not be read."""


class SteeringAcceptanceError(RuntimeError):
    """A durable acceptance attempt failed; its phase determines retry safety."""

    def __init__(self, failure: Exception) -> None:
        super().__init__(str(failure))
        self.failure = failure


class SteeringRollbackError(RuntimeError):
    """A rejected legacy input remains durable and must not be resent blindly."""

    def __init__(self, key: str, message_id: str | None, target_turn_id: str) -> None:
        super().__init__("The rejected steering input could not be rolled back")
        self.session_key = key
        self.message_id = message_id
        self.target_turn_id = target_turn_id


@dataclass(frozen=True, slots=True)
class NormalizedSteeringText:
    message: str
    semantic_message: str
    generated_attachments: bool


@dataclass(frozen=True, slots=True)
class SteeringContext:
    turn_id: str
    client_message_id: str
    surface_id: str
    client_request_id: str | None = None
    disposition: Literal["steering", "rejected"] = "steering"
    revision: int = 1


@dataclass(frozen=True, slots=True)
class PreparedSteeringInput:
    entry: SteeringTranscript
    expected_epoch: int


@dataclass(frozen=True, slots=True)
class AppendedSteeringInput:
    message_text: str | None
    message_id: str | None


@dataclass(frozen=True, slots=True)
class RuntimeSteeringDecision:
    accepted: bool
    task_id: str | None = None
    persisted: SteeringAcceptance | None = None
    failure_code: str | None = None
    capability: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class SteeringNotice:
    session_key: str
    message_id: str | None
    context: SteeringContext
    durable: bool = False
    rejected_orphan: bool = False


@dataclass(frozen=True, slots=True)
class SteeringDisposition:
    disposition: str = "steering"
    revision: int = 1
    client_message_id: str | None = None
    surface_id: str | None = None
    promoted_turn_id: str | None = None
    applied_iteration: int | None = None
    model_call_id: str | None = None
    promoted_from_turn_id: str | None = None
    failure_code: str | None = None
    retryable: bool | None = None
    recovery: str | None = None


class SteeringPrimitives(Protocol):
    """Individual persistence/runtime operations; sequencing belongs to TurnSteering."""

    async def session(self, key: str) -> SteeringSession: ...

    @property
    def durable_available(self) -> bool: ...

    @property
    def legacy_available(self) -> bool: ...

    def normalize(self, message: str, *, is_web_source: bool) -> NormalizedSteeringText: ...

    async def receipt(self, identity: SteeringIdentity) -> SteeringAcceptance | None: ...

    async def workspace_guard(self, session: SteeringSession) -> ProjectWorkspaceGuard | None: ...

    async def prepare(
        self,
        key: str,
        message: str,
        context: SteeringContext,
        session: SteeringSession,
    ) -> PreparedSteeringInput: ...

    async def persist(
        self,
        prepared: PreparedSteeringInput,
        *,
        active_turn_id: str,
        identity: SteeringIdentity,
        workspace_guard: ProjectWorkspaceGuard | None,
        pending: PendingInputGuard | None,
    ) -> SteeringAcceptance: ...

    def acceptance_failure(self, error: Exception) -> SteeringAcceptanceError | None: ...

    async def admit_runtime(
        self,
        key: str,
        target: str,
        message: str,
        *,
        semantic_message: str,
        persist: Callable[[str], Awaitable[SteeringAcceptance]],
        client_request_id: str,
        client_message_id: str,
        surface_id: str,
    ) -> RuntimeSteeringDecision: ...

    def notify_appended(self, entry: SteeringTranscript) -> None: ...

    async def disposition(self, acceptance: SteeringAcceptance) -> SteeringDisposition: ...

    async def active_turn(self, key: str) -> str | None: ...

    def session_lock(self, key: str) -> AbstractAsyncContextManager[object]: ...

    async def append(
        self,
        key: str,
        message: str,
        context: SteeringContext,
    ) -> AppendedSteeringInput: ...

    async def steer_runtime(
        self,
        key: str,
        message: str,
        *,
        semantic_message: str,
        message_id: str | None,
        client_message_id: str,
        surface_id: str,
    ) -> str | None: ...

    async def remove(self, key: str, message_id: str) -> bool: ...

    async def update_context(
        self,
        key: str,
        message_id: str,
        context: SteeringContext,
    ) -> bool: ...

    async def publish_steer(self, notice: SteeringNotice) -> None: ...

    async def publish_disposition(self, notice: SteeringNotice) -> None: ...


def _metric(disposition: str, *, key: str, failure_code: str | None = None) -> None:
    labels = {"failure_code": failure_code} if failure_code is not None else {}
    log.info(
        "steer_inputs_total",
        metric="steer_inputs_total",
        value=1,
        disposition=disposition,
        session_key=key,
        **labels,
    )


def rejected_steer(
    command: SteerTurn,
    *,
    failure_code: str,
    capability: Mapping[str, object] | None = None,
    active_turn_id: str | None = None,
) -> SteerTurnResult:
    """A proven non-acceptance can safely fall back to the pending queue."""
    _metric("rejected", key=command.session_key, failure_code=failure_code)
    result: SteerTurnResult = {
        "status": "not_accepted",
        "accepted": False,
        "key": command.session_key,
        "session_key": command.session_key,
        "expected_turn_id": command.expected_turn_id,
        "failure_code": failure_code,
        "retryable": False,
        "fallback_safe": True,
    }
    if active_turn_id:
        result["active_turn_id"] = active_turn_id
    if capability is not None:
        result["steer_capability"] = capability
    return result


class TurnSteering:
    """Own replay, atomic expected-turn acceptance and rejected legacy input recovery."""

    def __init__(self, ports: SteeringPrimitives) -> None:
        self._ports = ports

    async def steer(self, command: SteerTurn) -> SteerTurnResult:
        if command.mode == "durable":
            return await self._durable(command)
        return await self._legacy(command)

    async def _durable(self, command: SteerTurn) -> SteerTurnResult:
        ports, key = self._ports, command.session_key
        target = self._required(command.expected_turn_id, "expected_turn_id")
        request_id = self._required(command.client_request_id, "client_request_id")
        message_id = self._required(command.client_message_id, "client_message_id")
        if command.has_non_text_input or command.message.lstrip().startswith(("/", "!")):
            return self._unsupported(command, "text_only")
        session = await ports.session(key)
        if not ports.durable_available:
            return rejected_steer(
                command,
                failure_code="STEER_V2_UNAVAILABLE",
                capability={
                    "mode": "disabled",
                    "expected_turn_id": target,
                    "input_kinds": [],
                    "reason": "gateway_upgrade_required",
                },
            )
        normalized = ports.normalize(command.message, is_web_source=command.is_web_source)
        if normalized.generated_attachments:
            return self._unsupported(command, "generated_attachment")
        surface = command.surface_id or "web:web"
        pending = command.pending_input
        identity = SteeringIdentity(
            source_scope=(pending.source_scope or "") if pending else command.source_scope,
            request_session_key=key,
            client_request_id=request_id,
            request_fingerprint=pending.request_fingerprint
            if pending
            else command.request_fingerprint,
        )
        log.info("sessions.steer_v2.requested", session_key=key, expected_turn_id=target)
        _metric("requested", key=key)
        previous = await ports.receipt(identity)
        if previous is not None:
            if previous.receipt.request_fingerprint != identity.request_fingerprint:
                raise SteeringIdentityConflictError(
                    "client_request_id was already used for a different steer"
                )
            log.info("sessions.steer_v2.replayed", session_key=key, expected_turn_id=target)
            _metric("replayed", key=key)
            return await self._response(previous, request_id, message_id, surface)

        # Replay precedes mutable workspace checks and transcript preparation.
        guard = await ports.workspace_guard(session)
        context = SteeringContext(target, message_id, surface, client_request_id=request_id)
        prepared = await ports.prepare(key, normalized.message, context, session)
        content = prepared.entry.content
        message = content if isinstance(content, str) else normalized.message

        async def persist(active_turn_id: str) -> SteeringAcceptance:
            if active_turn_id != target:
                raise RuntimeError("steer admission changed the expected turn")
            return await ports.persist(
                prepared,
                active_turn_id=active_turn_id,
                identity=identity,
                workspace_guard=guard,
                pending=pending,
            )

        try:
            decision = await complete_durable_ingress(
                ports.admit_runtime(
                    key,
                    target,
                    message,
                    semantic_message=normalized.semantic_message,
                    persist=persist,
                    client_request_id=request_id,
                    client_message_id=message_id,
                    surface_id=surface,
                )
            )
        except Exception as exc:
            failure = ports.acceptance_failure(exc)
            if failure is None:
                raise
            raise failure from exc
        if not decision.accepted:
            # Another copy can commit while this copy observes the terminal fence.
            previous = await ports.receipt(identity)
            if previous is not None:
                return await self._response(previous, request_id, message_id, surface)
            log.info(
                "sessions.steer_v2.not_accepted",
                session_key=key,
                expected_turn_id=target,
                failure_code=decision.failure_code,
            )
            return rejected_steer(
                command,
                failure_code=decision.failure_code or "ACTIVE_TURN_NOT_STEERABLE",
                capability=decision.capability,
                active_turn_id=decision.task_id,
            )
        acceptance = decision.persisted
        if acceptance is None:
            raise RuntimeError("accepted steering input has no durable receipt")
        if not acceptance.replayed:
            ports.notify_appended(prepared.entry)
            notice = SteeringNotice(key, acceptance.receipt.message_id, context, durable=True)
            try:
                await ports.publish_steer(notice)
                await ports.publish_disposition(notice)
            except Exception:  # noqa: BLE001 - durable acceptance is authoritative.
                log.warning(
                    "sessions.steer_v2.accepted_event_emit_failed",
                    session_key=key,
                    message_id=acceptance.receipt.message_id,
                    exc_info=True,
                )
        log.info(
            "sessions.steer_v2.accepted",
            session_key=key,
            expected_turn_id=target,
            replayed=acceptance.replayed,
        )
        _metric("accepted", key=key)
        return await self._response(acceptance, request_id, message_id, surface)

    async def _legacy(self, command: SteerTurn) -> SteerTurnResult:
        ports, key = self._ports, command.session_key
        log.info(
            "sessions.steer.legacy_used",
            session_key=key,
            deprecated=True,
            replacement="sessions.steer.v2",
        )
        _metric("legacy_requested", key=key)
        await ports.session(key)
        if not ports.legacy_available:
            return {"status": "unavailable", "accepted": False, "key": key}
        target = await ports.active_turn(key)
        if not target:
            return {"status": "idle", "accepted": False, "key": key}
        normalized = ports.normalize(command.message, is_web_source=command.is_web_source)
        if normalized.generated_attachments:
            raise ValueError("Steering does not support generated attachments")
        message_id = command.client_message_id
        surface = command.surface_id
        if message_id is None or surface is None:
            raise ValueError("legacy steering requires resolved client and surface identities")
        context = SteeringContext(target, message_id, surface)
        async with ports.session_lock(key):
            entry = await ports.append(key, normalized.message, context)
        message = entry.message_text if entry.message_text is not None else normalized.message
        user_message_id = entry.message_id
        accepted_turn = await ports.steer_runtime(
            key,
            message,
            semantic_message=normalized.semantic_message,
            message_id=user_message_id,
            client_message_id=message_id,
            surface_id=surface,
        )
        if not accepted_turn:
            removed = False
            rollback_error: str | None = None
            if user_message_id:
                try:
                    removed = await ports.remove(key, user_message_id)
                except Exception as exc:  # noqa: BLE001 - classify dirty rollback.
                    rollback_error = str(exc)
            if removed:
                return {"status": "idle", "accepted": False, "key": key}
            rejected = SteeringContext(
                target, message_id, surface, disposition="rejected", revision=2
            )
            if user_message_id:
                try:
                    updated = await ports.update_context(key, user_message_id, rejected)
                    if not updated:
                        log.warning(
                            "sessions.steer.dirty_context_update_missed",
                            session_key=key,
                            message_id=user_message_id,
                        )
                except Exception:  # noqa: BLE001 - dirty failure remains authoritative.
                    log.warning(
                        "sessions.steer.dirty_context_update_failed",
                        session_key=key,
                        message_id=user_message_id,
                        exc_info=True,
                    )
            try:
                await ports.publish_disposition(
                    SteeringNotice(key, user_message_id, rejected, rejected_orphan=True)
                )
            except Exception:  # noqa: BLE001 - caller still receives dirty failure.
                log.warning(
                    "sessions.steer.dirty_disposition_emit_failed",
                    session_key=key,
                    message_id=user_message_id,
                    exc_info=True,
                )
            log.warning(
                "sessions.steer.rollback_failed",
                session_key=key,
                message_id=user_message_id,
                error=rollback_error,
            )
            raise SteeringRollbackError(key, user_message_id, target)
        accepted_context = SteeringContext(accepted_turn, message_id, surface)
        if user_message_id:
            try:
                await ports.update_context(key, user_message_id, accepted_context)
            except Exception:  # noqa: BLE001 - input already belongs to the runtime.
                log.warning(
                    "sessions.steer.context_update_failed",
                    session_key=key,
                    message_id=user_message_id,
                    exc_info=True,
                )
        notice = SteeringNotice(key, user_message_id, accepted_context)
        try:
            await ports.publish_steer(notice)
            await ports.publish_disposition(notice)
        except Exception:  # noqa: BLE001 - runtime acceptance is authoritative.
            log.warning(
                "sessions.steer.accepted_event_emit_failed",
                session_key=key,
                message_id=user_message_id,
                exc_info=True,
            )
        return {
            "status": "accepted",
            "accepted": True,
            "key": key,
            "turn_id": accepted_turn,
            "client_message_id": message_id,
            "user_message_id": user_message_id,
            "surface_id": surface,
            "disposition": "next_safe_boundary",
        }

    async def _response(
        self,
        acceptance: SteeringAcceptance,
        request_id: str,
        message_id: str,
        surface: str,
    ) -> SteerTurnResult:
        receipt = acceptance.receipt
        try:
            state = await self._ports.disposition(acceptance)
        except SteeringDispositionReadError:
            log.warning(
                "sessions.steer_v2.disposition_read_failed",
                session_key=receipt.accepted_session_key,
                message_id=receipt.message_id,
                exc_info=True,
            )
            state = SteeringDisposition()
        result: SteerTurnResult = {
            "status": "accepted",
            "accepted": True,
            "replayed": acceptance.replayed,
            "key": receipt.accepted_session_key,
            "session_key": receipt.accepted_session_key,
            "session_id": receipt.session_id,
            "task_id": receipt.task_id,
            "turn_id": receipt.task_id,
            "client_request_id": request_id,
            "client_message_id": state.client_message_id or message_id,
            "user_message_id": receipt.message_id,
            "surface_id": state.surface_id or surface,
            "disposition": state.disposition,
            "revision": state.revision,
            "fallback_safe": True,
        }
        if state.disposition == "promoted" and state.promoted_turn_id:
            result["promoted_turn_id"] = state.promoted_turn_id
        if state.applied_iteration is not None:
            result["applied_iteration"] = state.applied_iteration
        if state.model_call_id is not None:
            result["model_call_id"] = state.model_call_id
        if state.promoted_from_turn_id is not None:
            result["promoted_from_turn_id"] = state.promoted_from_turn_id
        if state.failure_code is not None:
            result["failure_code"] = state.failure_code
        if state.retryable is not None:
            result["retryable"] = state.retryable
        if state.recovery is not None:
            result["recovery"] = state.recovery
        return result

    @staticmethod
    def _required(value: str | None, field: str) -> str:
        if value is None:
            raise ValueError(f"params.{field} is required")
        if len(value) > 256:
            raise ValueError(f"params.{field} must not exceed 256 characters")
        return value

    @staticmethod
    def _unsupported(command: SteerTurn, reason: str) -> SteerTurnResult:
        return rejected_steer(
            command,
            failure_code="STEER_UNSUPPORTED_INPUT",
            capability={
                "mode": "queue_only",
                "expected_turn_id": command.expected_turn_id,
                "input_kinds": ["text"],
                "reason": reason,
            },
        )
