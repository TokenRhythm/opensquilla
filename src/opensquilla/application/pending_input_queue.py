"""Transport-neutral durable pending-input queue use cases."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, TypedDict

from opensquilla.application.turn_admission import (
    AdmitTurn,
    AdmitTurnResult,
    PendingInputGuard,
    SteerTurn,
    SteerTurnResult,
    TurnAdmission,
)
from opensquilla.application.turn_input import complete_durable_ingress
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
class EnqueuePendingInput:
    turn: AdmitTurn
    pending_input_id: str
    confirmed_plain_text: bool = False
    position: int | None = None


@dataclass(frozen=True, slots=True)
class DispatchPendingInput:
    session_key: str
    pending_input_id: str
    client_request_id: str
    request_fingerprint: str
    source_scope: str


@dataclass(frozen=True, slots=True)
class SteerPendingInput:
    session_key: str
    pending_input_id: str
    client_request_id: str
    client_message_id: str
    request_fingerprint: str
    expected_revision: int
    source_scope: str
    expected_turn_id: str | None
    surface_id: str | None = None
    retry_message: str | None = None
    is_web_source: bool = True
    default_surface_id: str = "web:web"


@dataclass(frozen=True, slots=True)
class StoredPendingInput:
    pending_input_id: str
    session_key: str
    source_scope: str
    client_request_id: str
    client_message_id: str
    request_fingerprint: str
    revision: int
    turn: AdmitTurn
    projection: PendingInputProjection
    material_scopes: frozenset[str] = frozenset()
    has_non_text_semantics: bool = False
    default_surface_id: str = "web:web"


@dataclass(frozen=True, slots=True)
class PendingDispatchIdentity:
    session_key: str
    source_scope: str
    client_request_id: str
    client_message_id: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class PendingDispatchReplay:
    request_fingerprint: str
    session_id: str
    result: AdmitTurnResult


@dataclass(frozen=True, slots=True)
class StagedPendingAttachments:
    attachments: tuple[dict[str, Any], ...]
    consumed_upload_ids: tuple[str, ...] = ()


type PendingFailureReason = Literal[
    "control-command",
    "registered-control-command",
    "display-mismatch",
    "initial-routing",
    "session-unavailable",
    "fingerprint-required",
    "missing",
    "fingerprint-conflict",
    "dispatch-identity",
    "steer-identity",
]


class PendingQueueRejectedError(RuntimeError):
    def __init__(self, reason: PendingFailureReason, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class PendingEnqueueRejectedError(RuntimeError):
    def __init__(self, reason: Literal["full", "cancelled", "dispatched", "conflict"]) -> None:
        super().__init__(reason)
        self.reason = reason


class PendingMaterialRejectedError(RuntimeError):
    def __init__(
        self,
        reason: Literal["conflict", "corrupt", "expired", "restart-lost", "invalid"],
        message: str,
        *,
        attachment_index: int | None = None,
        file_uuid: str | None = None,
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.attachment_index = attachment_index
        self.file_uuid = file_uuid
        self.recoverable = recoverable


@dataclass(frozen=True, slots=True)
class MovePendingInput:
    session_key: str
    pending_input_id: str
    expected_revision: int
    position: int


@dataclass(frozen=True, slots=True)
class PendingInputRevision:
    pending_input_id: str
    expected_revision: int


@dataclass(frozen=True, slots=True)
class ReorderPendingInputs:
    session_key: str
    items: tuple[PendingInputRevision, ...]


@dataclass(frozen=True, slots=True)
class CancelPendingInput:
    session_key: str
    pending_input_id: str
    expected_revision: int | None = None


class PendingInputConflictError(RuntimeError):
    """The queue's identity set or a durable revision changed."""


class PendingInputMissingError(LookupError):
    """The requested staged item no longer exists."""


class PendingCancellationConflictError(RuntimeError):
    """The durable revision changed before cancellation could commit."""


class PendingInputQueuePort(Protocol):
    def owner_lock(self, pending_input_id: str) -> AbstractAsyncContextManager[object]: ...

    def session_lock(self, key: str) -> AbstractAsyncContextManager[object]: ...

    def control_commands(self) -> frozenset[str]: ...

    async def current_session_id(self, key: str) -> str | None: ...

    def has_recovery_manifest(self, scope: str, pending_id: str) -> bool: ...

    def fingerprint(self, turn: AdmitTurn, confirmed: bool) -> str: ...

    async def stage_attachments(
        self,
        scope: str,
        pending_id: str,
        attachments: tuple[dict[str, Any], ...],
        enqueue_fingerprint: str,
    ) -> StagedPendingAttachments: ...

    async def insert_pending(
        self,
        command: EnqueuePendingInput,
        fingerprint: str,
    ) -> tuple[StoredPendingInput, bool]: ...

    async def pending_exists(self, pending_id: str) -> bool: ...

    async def load_pending(self, pending_id: str) -> StoredPendingInput | None: ...

    async def dispatch_identity(self, pending_id: str) -> PendingDispatchIdentity | None: ...

    async def replay_dispatch(
        self,
        source_scope: str,
        key: str,
        request_id: str,
    ) -> PendingDispatchReplay | None: ...

    async def evict_upload(self, upload_id: str) -> None: ...

    async def list_items(self, key: str) -> list[PendingInputProjection]: ...

    async def reposition(
        self,
        key: str,
        pending_input_id: str,
        revision: int,
        position: int,
    ) -> PendingInputProjection: ...

    async def reorder_durable(
        self,
        key: str,
        revisions: tuple[PendingInputRevision, ...],
    ) -> list[PendingInputProjection]: ...

    def cancellation_lock(self, pending_input_id: str) -> AbstractAsyncContextManager[object]: ...

    async def cancellation_material_scopes(self, key: str, pending_input_id: str) -> set[str]: ...

    async def cancel_durable(
        self,
        key: str,
        pending_input_id: str,
        revision: int | None,
    ) -> bool: ...

    async def cleanup_promotions(
        self,
        key: str,
        pending_input_id: str,
        scopes: set[str],
    ) -> None: ...

    def cleanup_material(self, pending_input_id: str, scopes: set[str]) -> None: ...


class PendingInputQueue:
    """Own durable queue identity, material and acceptance ordering."""

    def __init__(
        self,
        port: PendingInputQueuePort,
        *,
        turns: TurnAdmission | None = None,
    ) -> None:
        self._port = port
        self._turns = turns

    async def list(self, session_key: str) -> PendingInputListResult:
        key = self._key(session_key)
        return {"sessionKey": key, "items": await self._port.list_items(key), "maxPending": 5}

    async def update(self, command: MovePendingInput) -> PendingInputUpdateResult:
        key = self._key(command.session_key)
        pending_id = self._identity(command.pending_input_id)
        revision = self._revision(command.expected_revision)
        if isinstance(command.position, bool) or not isinstance(command.position, int):
            raise ValueError("params.position must be an integer")
        row = await self._port.reposition(key, pending_id, revision, command.position)
        return {"status": "updated", **row}

    async def reorder(self, command: ReorderPendingInputs) -> PendingInputReorderResult:
        key = self._key(command.session_key)
        if not 2 <= len(command.items) <= 5:
            raise ValueError("params.items must contain 2-5 rows")
        revisions = tuple(
            PendingInputRevision(
                self._identity(item.pending_input_id), self._revision(item.expected_revision)
            )
            for item in command.items
        )
        if any(len(item.pending_input_id) > 256 for item in revisions):
            raise ValueError("params.pendingInputId must not exceed 256 characters")
        if len({item.pending_input_id for item in revisions}) != len(revisions):
            raise ValueError("params.items pendingInputId values must be unique")
        rows = await self._port.reorder_durable(key, revisions)
        return {"status": "reordered", "sessionKey": key, "items": rows}

    async def cancel(self, command: CancelPendingInput) -> PendingInputCancelResult:
        key = self._key(command.session_key)
        pending_id = self._identity(command.pending_input_id)
        revision = command.expected_revision
        if revision is not None and (isinstance(revision, bool) or not isinstance(revision, int)):
            raise ValueError("params.expectedRevision must be an integer")
        async with self._port.cancellation_lock(pending_id):
            scopes = await self._port.cancellation_material_scopes(key, pending_id)
            removed = await self._port.cancel_durable(key, pending_id, revision)
            # Durable CAS is authoritative. Never discard attachment ownership
            # on a stale revision, before cancel commits, or without reference proof.
            await self._port.cleanup_promotions(key, pending_id, scopes)
            self._port.cleanup_material(pending_id, scopes)
        return {
            "status": "cancelled",
            "cancelled": True,
            "alreadyMissing": not removed,
            "pendingInputId": pending_id,
            "sessionKey": key,
        }

    @staticmethod
    def _key(value: str) -> str:
        key = canonicalize_session_key(value)
        if not key:
            raise ValueError("session_key must be non-empty")
        return key

    @staticmethod
    def _identity(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("pending_input_id must be non-empty")
        return value.strip()

    @staticmethod
    def _revision(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("expected_revision must be positive")
        return value

    async def enqueue(self, command: EnqueuePendingInput) -> PendingInputEnqueueResult:
        turn = replace(command.turn, session_key=self._key(command.turn.session_key))
        pending_id = self._identity(command.pending_input_id)
        self._validate_queued_content(turn, command.confirmed_plain_text)
        turn = replace(
            turn,
            client_request_id=self._client_identity(turn.client_request_id, "clientRequestId"),
            client_message_id=self._client_identity(turn.client_message_id, "clientMessageId"),
        )
        if turn.initial_routing_mode is not None:
            raise PendingQueueRejectedError("initial-routing")
        position = command.position
        if position is not None and (
            isinstance(position, bool) or not isinstance(position, int) or position < 0
        ):
            raise ValueError("params.position must be a non-negative integer")
        command = replace(command, turn=turn, pending_input_id=pending_id)

        async def materialize_and_enqueue() -> PendingInputEnqueueResult:
            async with self._port.owner_lock(pending_id), self._port.session_lock(turn.session_key):
                scope = await self._port.current_session_id(turn.session_key)
                if scope is None:
                    raise PendingQueueRejectedError("session-unavailable", retryable=True)
                staged_turn = turn
                had_manifest = False
                upload_ids: tuple[str, ...] = ()
                if turn.attachments:
                    had_manifest = self._port.has_recovery_manifest(scope, pending_id)
                    try:
                        staged = await self._port.stage_attachments(
                            scope,
                            pending_id,
                            turn.attachments,
                            self._port.fingerprint(turn, command.confirmed_plain_text),
                        )
                    except PendingMaterialRejectedError as exc:
                        if not had_manifest and exc.reason not in {"conflict", "corrupt"}:
                            self._port.cleanup_material(pending_id, {scope})
                        raise
                    staged_turn = replace(turn, attachments=staged.attachments)
                    upload_ids = staged.consumed_upload_ids
                fingerprint = self._port.fingerprint(staged_turn, command.confirmed_plain_text)
                try:
                    row, replayed = await self._port.insert_pending(
                        replace(command, turn=staged_turn),
                        fingerprint,
                    )
                except PendingEnqueueRejectedError:
                    # A prior ambiguous attempt still owns its recovery material.
                    if not had_manifest and not await self._port.pending_exists(pending_id):
                        self._port.cleanup_material(pending_id, {scope})
                    raise
                for upload_id in upload_ids:
                    await self._port.evict_upload(upload_id)
                return {"status": "staged", **row.projection, "replayed": replayed}

        return await complete_durable_ingress(materialize_and_enqueue())

    def _validate_queued_content(self, turn: AdmitTurn, confirmed: bool) -> None:
        if not isinstance(turn.message, str) or not turn.message.strip():
            raise ValueError("params.message must be a non-empty string")
        if not isinstance(confirmed, bool):
            raise ValueError("params.confirmedPlainText must be a boolean")
        control = turn.message.strip()
        if confirmed and control.split(maxsplit=1)[0].casefold() in self._port.control_commands():
            raise PendingQueueRejectedError("registered-control-command")
        display = turn.display_text.strip() if turn.display_text is not None else ""
        escaped = control.startswith("/") and display.startswith("//") and display[1:] == control
        if control.startswith("!") or (
            control.startswith("/")
            and not control.startswith("//")
            and not escaped
            and not confirmed
        ):
            raise PendingQueueRejectedError("control-command")
        if turn.display_text is not None and display != control and not escaped:
            raise PendingQueueRejectedError("display-mismatch")
        if len(turn.prompt_annotation_ids) > 16:
            raise ValueError("params.promptAnnotationIds supports at most 16 items")
        if any(
            not isinstance(item, str) or not item.strip() for item in turn.prompt_annotation_ids
        ):
            raise ValueError("params.promptAnnotationIds must contain non-empty strings")
        if len(set(turn.prompt_annotation_ids)) != len(turn.prompt_annotation_ids):
            raise ValueError("params.promptAnnotationIds must contain unique ids")

    @staticmethod
    def _client_identity(value: str | None, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"params.{name} is required")
        if len(value.strip()) > 256:
            raise ValueError(f"params.{name} must not exceed 256 characters")
        return value.strip()

    async def dispatch(self, command: DispatchPendingInput) -> AdmitTurnResult:
        key = self._key(command.session_key)
        pending_id = self._identity(command.pending_input_id)
        request_id = self._client_identity(command.client_request_id, "clientRequestId")
        fingerprint = command.request_fingerprint.strip()
        if not fingerprint:
            raise PendingQueueRejectedError("fingerprint-required")
        async with self._port.owner_lock(pending_id):
            row = await self._port.load_pending(pending_id)
            if row is None:
                tombstone = await self._port.dispatch_identity(pending_id)
                if tombstone is None or (
                    tombstone.session_key != key
                    or tombstone.source_scope != command.source_scope
                    or tombstone.client_request_id != request_id
                    or tombstone.request_fingerprint != fingerprint
                ):
                    raise PendingQueueRejectedError("missing")
                replay = await self._port.replay_dispatch(command.source_scope, key, request_id)
                if replay is None:
                    raise PendingQueueRejectedError("missing")
                if replay.request_fingerprint != fingerprint:
                    raise PendingQueueRejectedError("fingerprint-conflict")
                scopes = {replay.session_id}
                current = await self._port.current_session_id(key)
                if current is not None:
                    scopes.add(current)
                self._port.cleanup_material(pending_id, scopes)
                return replay.result
            if (
                row.session_key != key
                or row.client_request_id != request_id
                or row.request_fingerprint != fingerprint
            ):
                raise PendingQueueRejectedError("dispatch-identity")
            if self._turns is None:
                raise RuntimeError("Turn admission is unavailable")
            response = await self._turns.admit(
                replace(
                    row.turn,
                    pending_input=PendingInputGuard(pending_id, fingerprint, row.revision),
                )
            )
            self._port.cleanup_material(pending_id, set(row.material_scopes))
            return response

    async def steer(self, command: SteerPendingInput) -> SteerTurnResult:
        key = self._key(command.session_key)
        pending_id = self._identity(command.pending_input_id)
        request_id = self._client_identity(command.client_request_id, "clientRequestId")
        message_id = self._client_identity(command.client_message_id, "clientMessageId")
        fingerprint = command.request_fingerprint.strip()
        if not fingerprint:
            raise PendingQueueRejectedError("fingerprint-required")
        revision = self._revision(command.expected_revision)
        async with self._port.owner_lock(pending_id):
            row = await self._port.load_pending(pending_id)
            if row is None:
                tombstone = await self._port.dispatch_identity(pending_id)
                if tombstone is None or (
                    tombstone.session_key != key
                    or tombstone.source_scope != command.source_scope
                    or tombstone.client_request_id != request_id
                    or tombstone.client_message_id != message_id
                    or tombstone.request_fingerprint != fingerprint
                ):
                    raise PendingQueueRejectedError("missing")
                message = command.retry_message
                if not isinstance(message, str) or not message.strip():
                    raise ValueError("params.message must be a non-empty string")
                is_web_source = command.is_web_source
                default_surface = command.default_surface_id
            else:
                if (
                    row.session_key != key
                    or row.source_scope != command.source_scope
                    or row.client_request_id != request_id
                    or row.client_message_id != message_id
                    or row.request_fingerprint != fingerprint
                    or row.revision != revision
                ):
                    raise PendingQueueRejectedError("steer-identity", retryable=True)
                message = row.turn.message
                is_web_source = row.turn.source.is_web
                default_surface = row.default_surface_id
                if not message.strip() or row.turn.attachments or row.has_non_text_semantics:
                    from opensquilla.application.turn_steering import rejected_steer

                    expected_turn = self._client_identity(
                        command.expected_turn_id, "expected_turn_id"
                    )
                    return rejected_steer(
                        SteerTurn(key, message, "durable", expected_turn_id=expected_turn),
                        failure_code="STEER_UNSUPPORTED_INPUT",
                        capability={
                            "mode": "queue_only",
                            "expected_turn_id": expected_turn,
                            "input_kinds": ["text"],
                            "reason": "text_only",
                        },
                    )
            if self._turns is None:
                raise RuntimeError("Turn admission is unavailable")
            return await self._turns.steer(
                SteerTurn(
                    session_key=key,
                    message=message,
                    mode="durable",
                    expected_turn_id=command.expected_turn_id,
                    client_request_id=request_id,
                    client_message_id=message_id,
                    surface_id=command.surface_id or default_surface,
                    source_scope=command.source_scope,
                    request_fingerprint=fingerprint,
                    is_web_source=is_web_source,
                    pending_input=PendingInputGuard(
                        pending_id, fingerprint, revision, command.source_scope
                    ),
                )
            )
