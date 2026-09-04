"""Steering protocol decoding and adapters over existing session/runtime primitives."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any, cast

from opensquilla.application.turn_admission import PendingInputGuard, SteerTurn
from opensquilla.application.turn_steering import (
    AppendedSteeringInput,
    NormalizedSteeringText,
    PreparedSteeringInput,
    RuntimeSteeringDecision,
    SteeringAcceptance,
    SteeringAcceptanceError,
    SteeringContext,
    SteeringDisposition,
    SteeringDispositionReadError,
    SteeringIdentity,
    SteeringIdentityConflictError,
    SteeringNotice,
    SteeringPersistenceUnavailableError,
    SteeringRollbackError,
    SteeringSession,
    SteeringTranscript,
)
from opensquilla.gateway.admission_input import (
    is_web_source_hint,
    normalized_source_hint,
    source_scope_from_hint,
)
from opensquilla.gateway.input_normalization import normalize_incoming_text
from opensquilla.gateway.project_workspace_runtime import map_project_workspace_error
from opensquilla.gateway.rpc import RpcHandlerError, RpcUnavailableError
from opensquilla.gateway.session_services import get_session_lock, get_session_storage
from opensquilla.gateway.turn_ingress import request_identity
from opensquilla.project_workspaces import (
    ProjectWorkspaceGuard,
    ProjectWorkspaceStateError,
    resolve_validated_project_workspace,
)
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import (
    PendingChatInputConflictError,
    PendingChatInputNotFoundError,
    SessionStorage,
    StaleEpochError,
    StorageBusyError,
    TurnAcceptanceResult,
    TurnIngressConflictError,
)
from opensquilla.session.turn_context import turn_context_scope


def _optional_text(params: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        if name not in params:
            continue
        value = params[name]
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"params.{name} must be a string")
        return value.strip() or None
    return None


def decode_steering_command(
    params: dict[str, Any],
    *,
    key: str,
    durable: bool,
    principal_role: str,
    connection_id: str,
    pending: PendingInputGuard | None = None,
) -> SteerTurn:
    """Resolve v4 aliases and authenticated identity before the Application seam."""
    source = normalized_source_hint(params)
    web_source = is_web_source_hint(source)
    target = _optional_text(params, "expected_turn_id", "expectedTurnId") if durable else None
    request_id = _optional_text(params, "client_request_id", "clientRequestId") if durable else None
    message_id = _optional_text(params, "client_message_id", "clientMessageId")
    if durable:
        for field, value in (
            ("expected_turn_id", target),
            ("client_request_id", request_id),
            ("client_message_id", message_id),
        ):
            if value is None:
                raise ValueError(f"params.{field} is required")
            if len(value) > 256:
                raise ValueError(f"params.{field} must not exceed 256 characters")
    default_surface = str(
        source.get("channel_id")
        or (
            f"{source.get('caller_kind', 'rpc')}:{source.get('channel_kind', 'rpc')}"
            if durable
            else f"web:{connection_id}"
        )
    )
    surface = _optional_text(params, "surface_id", "surfaceId") or default_surface
    scope = fingerprint = ""
    if durable and pending is None:
        source_scope = source_scope_from_hint(source, principal_role)
        identity = request_identity(
            params,
            request_session_key=key,
            source_scope=f"{source_scope}:steer.v2"[:256],
            fingerprint_params={
                "message": params["message"],
                "intent": "steer.v2",
                "queueMode": {
                    "expected_turn_id": target,
                    "client_message_id": message_id,
                    "surface_id": surface,
                },
            },
        )
        scope, fingerprint, request_id = (
            identity.source_scope,
            identity.request_fingerprint,
            identity.client_request_id,
        )
    return SteerTurn(
        session_key=key,
        message=params["message"],
        mode="durable" if durable else "legacy",
        expected_turn_id=target,
        client_request_id=request_id,
        client_message_id=message_id or (uuid.uuid4().hex if not durable else None),
        surface_id=surface,
        source_scope=scope,
        request_fingerprint=fingerprint,
        is_web_source=web_source,
        pending_input=pending,
        has_non_text_input=params.get("attachments") not in (None, [])
        or any(
            params.get(field) is not None
            for field in (
                "intent",
                "model",
                "model_id",
                "workspaceId",
                "workspace_id",
                "collaborationMode",
                "collaboration_mode",
                "runMode",
                "run_mode",
            )
        ),
    )


def map_steering_error(error: Exception, *, is_owner: bool = False) -> Exception:
    """Retain v4 failure classification without exposing it to Application Ports."""
    if isinstance(error, SteeringPersistenceUnavailableError):
        return RpcUnavailableError(str(error))
    if isinstance(error, SteeringIdentityConflictError):
        return RpcHandlerError("IDEMPOTENCY_CONFLICT", str(error), retryable=False, accepted=False)
    if isinstance(error, SteeringRollbackError):
        return RpcHandlerError(
            "STEER_RACE_DIRTY",
            "The active turn ended and the just-appended steer input could not be rolled back. "
            "The transcript contains a rejected orphan; automatic queue fallback is disabled "
            "to prevent duplication.",
            details={
                "session_key": error.session_key,
                "orphan_message_id": error.message_id,
                "target_turn_id": error.target_turn_id,
                "fallback_safe": False,
                "remediation": "dedup by orphan_message_id before resending",
            },
            retryable=False,
        )
    if isinstance(error, ProjectWorkspaceStateError):
        mapped = map_project_workspace_error(error, owner=is_owner)
        details = dict(mapped.details) if isinstance(mapped.details, dict) else {}
        return RpcHandlerError(
            mapped.code,
            mapped.message,
            details={**details, "fallback_safe": True},
            retryable=mapped.retryable,
            retry_after_ms=mapped.retry_after_ms,
            accepted=False,
        )
    if not isinstance(error, SteeringAcceptanceError):
        return error
    error = error.failure
    if isinstance(error, StorageBusyError):
        return RpcHandlerError(
            "STORAGE_BUSY",
            "Session storage is temporarily busy. Retry with the same client_request_id.",
            details={
                "operation": error.operation,
                "waited_ms": error.waited_ms,
                "fallback_safe": False,
            },
            retryable=True,
            retry_after_ms=error.retry_after_ms,
            accepted=False,
        )
    if isinstance(error, StaleEpochError):
        return RpcHandlerError(
            "SESSION_CHANGED",
            "The session changed while the steer was being accepted.",
            details={"fallback_safe": True},
            retryable=True,
            accepted=False,
        )
    if isinstance(error, TurnIngressConflictError):
        return RpcHandlerError(
            "IDEMPOTENCY_CONFLICT",
            str(error),
            details={"fallback_safe": False},
            retryable=False,
            accepted=False,
        )
    if isinstance(error, PendingChatInputNotFoundError):
        return RpcHandlerError(
            "PENDING_INPUT_NOT_FOUND",
            "Pending input disappeared before steer acceptance",
            details={"fallback_safe": False},
            retryable=True,
            accepted=False,
        )
    if isinstance(error, PendingChatInputConflictError):
        return RpcHandlerError(
            "PENDING_INPUT_CONFLICT",
            "Pending input changed before steer acceptance",
            details={"fallback_safe": False},
            retryable=True,
            accepted=False,
        )
    return error


def _context_payload(context: SteeringContext) -> dict[str, Any]:
    result: dict[str, Any] = {
        "turn_id": context.turn_id,
        "target_turn_id": context.turn_id,
        "client_message_id": context.client_message_id,
        "surface_id": context.surface_id,
        "intent": "steer",
        "disposition": context.disposition,
        "revision": context.revision,
    }
    if context.client_request_id is not None:
        result["client_request_id"] = context.client_request_id
    return result


def _native_steering_acceptance(value: object) -> TurnAcceptanceResult:
    if not isinstance(value, TurnAcceptanceResult):
        raise TypeError("Steering acceptance requires a native durable receipt")
    return value


class GatewaySteeringPrimitives:
    """Adapt individual operations, retaining the existing runtime and storage engines."""

    def __init__(
        self,
        *,
        session_manager: Any,
        task_runtime: Any,
        turn_runner: object | None,
        emit_steer: Callable[[str, dict[str, Any]], Awaitable[None]],
        emit_disposition: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._manager = session_manager
        self._runtime = task_runtime
        self._runner = turn_runner
        self._emit_steer = emit_steer
        self._emit_disposition = emit_disposition

    def _storage(self) -> SessionStorage:
        if self._manager is None:
            raise KeyError("No session manager available")
        storage = get_session_storage(self._manager)
        if storage is None:
            raise KeyError("No session storage available")
        return cast(SessionStorage, storage)

    async def session(self, key: str) -> SteeringSession:
        session = await self._storage().get_session(key)
        if session is None:
            raise KeyError(f"Session not found: {key}")
        return session

    @property
    def durable_available(self) -> bool:
        return callable(getattr(self._runtime, "admit_steer", None))

    @property
    def legacy_available(self) -> bool:
        return callable(getattr(self._runtime, "active_task_id", None)) and callable(
            getattr(self._runtime, "steer", None)
        )

    def normalize(self, message: str, *, is_web_source: bool) -> NormalizedSteeringText:
        normalized = normalize_incoming_text(
            message,
            source_hint={"caller_kind": "web" if is_web_source else "cli"},
            attachments=[],
        )
        return NormalizedSteeringText(
            normalized.message_text,
            normalized.semantic_message,
            bool(normalized.generated_attachments),
        )

    async def receipt(self, identity: SteeringIdentity) -> SteeringAcceptance | None:
        getter = getattr(self._storage(), "get_turn_ingress_receipt", None)
        if not callable(getter):
            return None
        result = await getter(
            source_scope=identity.source_scope,
            request_session_key=identity.request_session_key,
            client_request_id=identity.client_request_id,
        )
        return _native_steering_acceptance(result) if result is not None else None

    async def workspace_guard(self, session: SteeringSession) -> ProjectWorkspaceGuard | None:
        workspace_id = getattr(session, "workspace_id", None)
        if not isinstance(workspace_id, str) or not workspace_id:
            return None
        return (await resolve_validated_project_workspace(self._storage(), workspace_id)).guard

    async def prepare(
        self,
        key: str,
        message: str,
        context: SteeringContext,
        session: SteeringSession,
    ) -> PreparedSteeringInput:
        prepare = getattr(self._manager, "prepare_message", None)
        if not callable(prepare) or not callable(getattr(self._storage(), "accept_turn", None)):
            raise SteeringPersistenceUnavailableError(
                "Same-turn steer requires durable atomic session storage"
            )
        if not isinstance(session, SessionNode):
            raise TypeError("Durable steering preparation requires a native session")
        entry, epoch = await prepare(
            key,
            role="user",
            content=message,
            turn_context=_context_payload(context),
            session_node=session,
        )
        if not isinstance(entry, TranscriptEntry):
            raise TypeError("Durable steering preparation requires a native transcript entry")
        return PreparedSteeringInput(entry, epoch)

    async def persist(
        self,
        prepared: PreparedSteeringInput,
        *,
        active_turn_id: str,
        identity: SteeringIdentity,
        workspace_guard: ProjectWorkspaceGuard | None,
        pending: PendingInputGuard | None,
    ) -> SteeringAcceptance:
        entry = prepared.entry
        if not isinstance(entry, TranscriptEntry):
            raise TypeError("Durable steering persistence requires a native transcript entry")
        return await self._storage().accept_turn(
            entry,
            expected_epoch=prepared.expected_epoch,
            updated_at=int(time.time() * 1000),
            task_record=None,
            receipt_task_id=active_turn_id,
            source_scope=identity.source_scope,
            request_session_key=identity.request_session_key,
            client_request_id=identity.client_request_id,
            request_fingerprint=identity.request_fingerprint,
            workspace_guard=workspace_guard,
            pending_input_id=pending.pending_input_id if pending else None,
            pending_input_fingerprint=pending.request_fingerprint if pending else None,
            pending_input_revision=pending.expected_revision if pending else None,
        )

    @staticmethod
    def acceptance_failure(error: Exception) -> SteeringAcceptanceError | None:
        if isinstance(
            error,
            (
                StorageBusyError,
                StaleEpochError,
                TurnIngressConflictError,
                PendingChatInputNotFoundError,
                PendingChatInputConflictError,
            ),
        ):
            return SteeringAcceptanceError(error)
        return None

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
    ) -> RuntimeSteeringDecision:
        result = await self._runtime.admit_steer(
            key,
            target,
            message,
            persist=persist,
            semantic_message=semantic_message,
            client_request_id=client_request_id,
            client_message_id=client_message_id,
            surface_id=surface_id,
        )
        return RuntimeSteeringDecision(
            result.accepted,
            result.task_id,
            _native_steering_acceptance(result.persisted) if result.persisted is not None else None,
            result.failure_code,
            result.capability,
        )

    def notify_appended(self, entry: SteeringTranscript) -> None:
        notify = getattr(self._manager, "notify_message_appended", None)
        if callable(notify):
            if not isinstance(entry, TranscriptEntry):
                raise TypeError("Steering append notification requires a native transcript entry")
            notify(entry)

    async def disposition(self, acceptance: SteeringAcceptance) -> SteeringDisposition:
        storage, receipt = self._storage(), acceptance.receipt
        try:
            getter = getattr(storage, "get_canonical_transcript_entry", None)
            if callable(getter):
                entry = await getter(receipt.session_id, receipt.message_id)
            else:
                transcript = getattr(storage, "get_canonical_transcript", None)
                if not callable(transcript):
                    transcript = storage.get_transcript
                entries = await transcript(receipt.session_id)
                entry = next(
                    (item for item in entries if item.message_id == receipt.message_id), None
                )
            context = (
                dict(entry.turn_context)
                if entry is not None and isinstance(entry.turn_context, dict)
                else {}
            )
        except Exception as exc:
            raise SteeringDispositionReadError(str(exc)) from exc
        promoted = context.get("promoted_turn_id") or context.get("turn_id")
        return SteeringDisposition(
            disposition=str(context.get("disposition") or "steering"),
            revision=int(context.get("revision") or 1),
            client_message_id=context.get("client_message_id"),
            surface_id=context.get("surface_id"),
            promoted_turn_id=promoted if isinstance(promoted, str) else None,
            applied_iteration=context.get("applied_iteration"),
            model_call_id=context.get("model_call_id"),
            promoted_from_turn_id=context.get("promoted_from_turn_id"),
            failure_code=context.get("failure_code"),
            retryable=context.get("retryable"),
            recovery=context.get("recovery"),
        )

    async def active_turn(self, key: str) -> str | None:
        return cast(str | None, await self._runtime.active_task_id(key))

    def session_lock(self, key: str) -> AbstractAsyncContextManager[object]:
        return get_session_lock(self._runner, key) or nullcontext()

    async def append(
        self,
        key: str,
        message: str,
        context: SteeringContext,
    ) -> AppendedSteeringInput:
        with turn_context_scope(_context_payload(context)):
            entry = await self._manager.append_message(
                key,
                role="user",
                content=message,
            )
        content = getattr(entry, "content", None)
        return AppendedSteeringInput(
            content if isinstance(content, str) else None,
            getattr(entry, "message_id", None),
        )

    async def steer_runtime(
        self,
        key: str,
        message: str,
        *,
        semantic_message: str,
        message_id: str | None,
        client_message_id: str,
        surface_id: str,
    ) -> str | None:
        return cast(
            str | None,
            await self._runtime.steer(
                key,
                message,
                semantic_message=semantic_message,
                persisted_user_message_id=message_id,
                client_message_id=client_message_id,
                surface_id=surface_id,
            ),
        )

    async def remove(self, key: str, message_id: str) -> bool:
        remove = getattr(self._manager, "remove_message", None)
        return bool(await remove(key, message_id)) if callable(remove) else False

    async def update_context(self, key: str, message_id: str, context: SteeringContext) -> bool:
        update = getattr(self._manager, "update_message_turn_context", None)
        return (
            bool(await update(key, message_id, _context_payload(context)))
            if callable(update)
            else False
        )

    async def publish_steer(self, notice: SteeringNotice) -> None:
        if notice.durable:
            payload = self._disposition_payload(notice)
        else:
            payload = {
                "session_key": notice.session_key,
                "turn_id": notice.context.turn_id,
                "client_message_id": notice.context.client_message_id,
                "user_message_id": notice.message_id,
                "surface_id": notice.context.surface_id,
                "disposition": "next_safe_boundary",
            }
        await self._emit_steer(notice.session_key, payload)

    async def publish_disposition(self, notice: SteeringNotice) -> None:
        await self._emit_disposition(notice.session_key, self._disposition_payload(notice))

    @staticmethod
    def _disposition_payload(notice: SteeringNotice) -> dict[str, Any]:
        payload = {
            "session_key": notice.session_key,
            "user_message_id": notice.message_id,
            **_context_payload(notice.context),
        }
        if notice.durable:
            payload.update(key=notice.session_key, task_id=notice.context.turn_id)
        if notice.rejected_orphan:
            payload.update(failure_code="STEER_RACE_DIRTY", retryable=False, fallback_safe=False)
        return payload
