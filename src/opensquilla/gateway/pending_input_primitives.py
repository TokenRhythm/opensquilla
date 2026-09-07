"""Durable storage and attachment primitives for PendingInputQueue."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Literal, cast

import structlog

from opensquilla.application.pending_input_queue import (
    EnqueuePendingInput,
    PendingDispatchIdentity,
    PendingEnqueueRejectedError,
    PendingInputProjection,
    PendingMaterialRejectedError,
    StagedPendingAttachments,
    StoredPendingInput,
)
from opensquilla.application.turn_admission import AdmitTurn
from opensquilla.attachment_refs import (
    PENDING_CHAT_INPUT_MATERIAL_STORE,
    PendingChatInputManifestConflictError,
    PendingChatInputManifestCorruptError,
    read_pending_chat_input_manifest,
)
from opensquilla.engine.commands import DEFAULT_REGISTRY, Surface
from opensquilla.gateway import attachment_ingest
from opensquilla.gateway.admission_input import decode_admit_turn, source_hint_from_turn
from opensquilla.gateway.turn_ingress import request_fingerprint
from opensquilla.paths import media_root_from_config
from opensquilla.session.storage import (
    PendingChatInput,
    PendingChatInputAlreadyDispatchedError,
    PendingChatInputCancelledError,
    PendingChatInputCapacityError,
    PendingChatInputConflictError,
    SessionStorage,
)

log = structlog.get_logger(__name__)


def pending_input_payload(turn: AdmitTurn, confirmed_plain_text: bool) -> dict[str, Any]:
    """Encode the existing durable payload without changing its fingerprint shape."""

    source = source_hint_from_turn(turn.source)
    if turn.capture.no_memory_capture:
        source["no_memory_capture"] = True
    if turn.capture.input_provenance is not None:
        source["input_provenance"] = dict(turn.capture.input_provenance)
    payload: dict[str, Any] = {
        "key": turn.session_key,
        "message": turn.message,
        "attachments": list(turn.attachments),
        "queueMode": "followup",
        "clientRequestId": turn.client_request_id,
        "clientMessageId": turn.client_message_id,
        "_source": source,
    }
    if turn.intent_was_provided:
        payload["intent"] = turn.intent
    for name, value in (
        ("workspaceId", turn.workspace_id),
        ("collaborationMode", turn.initial_collaboration_mode),
        ("initialRoutingMode", turn.initial_routing_mode),
        ("displayText", turn.display_text),
    ):
        if value is not None:
            payload[name] = value
    if confirmed_plain_text:
        payload["confirmedPlainText"] = True
    if turn.prompt_annotation_ids:
        payload["promptAnnotationIds"] = list(turn.prompt_annotation_ids)
    return payload


def pending_input_projection(
    row: PendingChatInput,
    *,
    replayed: bool = False,
) -> PendingInputProjection:
    payload = row.payload
    attachments = []
    for attachment in payload.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        attachments.append(
            {
                "name": attachment.get("name"),
                "mime": attachment.get("mime") or attachment.get("type"),
                "type": attachment.get("type") or attachment.get("mime"),
                "size": attachment.get("size"),
            }
        )
    result: dict[str, Any] = {
        "pendingInputId": row.pending_input_id,
        "pending_input_id": row.pending_input_id,
        "sessionKey": row.session_key,
        "session_key": row.session_key,
        "clientRequestId": row.client_request_id,
        "client_request_id": row.client_request_id,
        "clientMessageId": row.client_message_id,
        "client_message_id": row.client_message_id,
        "requestFingerprint": row.request_fingerprint,
        "request_fingerprint": row.request_fingerprint,
        "message": str(payload.get("message") or ""),
        "intent": payload.get("intent"),
        "attachments": attachments,
        "position": row.position,
        "revision": row.state_revision,
        "createdAt": row.created_at,
        "updatedAt": row.updated_at,
        "replayed": replayed,
        "schemaVersion": row.schema_version,
    }
    display = payload.get("displayText")
    if isinstance(display, str):
        result["displayText"] = display
    if payload.get("confirmedPlainText") is True:
        result["confirmedPlainText"] = True
    annotations = payload.get("promptAnnotationIds")
    if isinstance(annotations, list) and annotations:
        result["promptAnnotationIds"] = [item for item in annotations if isinstance(item, str)][:16]
    routing = payload.get("initialRoutingMode")
    if isinstance(routing, str):
        result["initialRoutingMode"] = routing
    return cast(PendingInputProjection, result)


def stored_pending_input(row: PendingChatInput) -> StoredPendingInput:
    # Source scope is immutable storage identity, not re-derived current authority.
    turn = replace(
        decode_admit_turn(row.payload, surface="session"),
        source_scope=row.source_scope,
        request_fingerprint=row.request_fingerprint,
    )
    scopes = frozenset(
        item["scope"]
        for item in row.payload.get("attachments") or []
        if isinstance(item, dict)
        and item.get("store") == PENDING_CHAT_INPUT_MATERIAL_STORE
        and item.get("pending_input_id") == row.pending_input_id
        and isinstance(item.get("scope"), str)
        and item["scope"]
    )
    source = turn.source
    return StoredPendingInput(
        pending_input_id=row.pending_input_id,
        session_key=row.session_key,
        source_scope=row.source_scope,
        client_request_id=row.client_request_id,
        client_message_id=row.client_message_id,
        request_fingerprint=row.request_fingerprint,
        revision=row.state_revision,
        turn=turn,
        projection=pending_input_projection(row),
        material_scopes=scopes,
        has_non_text_semantics=any(
            row.payload.get(name) is not None
            for name in (
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
        default_surface_id=source.channel_id or f"{source.caller_kind}:{source.channel_kind}",
    )


class GatewayPendingInputPrimitives:
    """Individual existing storage/material operations; no queue orchestration."""

    @property
    def storage(self) -> SessionStorage:
        raise NotImplementedError

    @property
    def config(self) -> object:
        raise NotImplementedError

    @staticmethod
    def control_commands() -> frozenset[str]:
        names = {"/plan"}
        for command in DEFAULT_REGISTRY.for_surface(Surface.WEB_CHAT):
            names.add(command.name.casefold())
            names.update(alias.casefold() for alias in command.aliases)
        return frozenset(names)

    @staticmethod
    def fingerprint(turn: AdmitTurn, confirmed: bool) -> str:
        return request_fingerprint(pending_input_payload(turn, confirmed))

    async def current_session_id(self, key: str) -> str | None:
        session = await self.storage.get_session(key)
        identity = getattr(session, "session_id", None)
        return identity if isinstance(identity, str) and identity else None

    def has_recovery_manifest(self, scope: str, pending_id: str) -> bool:
        return (
            read_pending_chat_input_manifest(
                media_root=media_root_from_config(self.config),
                session_id=scope,
                pending_input_id=pending_id,
            )
            is not None
        )

    async def stage_attachments(
        self,
        scope: str,
        pending_id: str,
        attachments: tuple[dict[str, Any], ...],
        enqueue_fingerprint: str,
    ) -> StagedPendingAttachments:
        settings = getattr(self.config, "attachments", None)
        disk_budget = getattr(settings, "transcript_disk_budget_bytes", None)
        opaque_limit = getattr(settings, "opaque_max_bytes", None)
        try:
            staged = await attachment_ingest.stage_pending_chat_input_attachments(
                list(attachments),
                material_root=media_root_from_config(self.config),
                session_id=scope,
                pending_input_id=pending_id,
                enqueue_fingerprint=enqueue_fingerprint,
                disk_budget_bytes=disk_budget if isinstance(disk_budget, int) else None,
                accept_opaque=bool(getattr(settings, "accept_opaque", True)),
                opaque_limit_bytes=opaque_limit if isinstance(opaque_limit, int) else None,
            )
        except PendingChatInputManifestConflictError as exc:
            raise PendingMaterialRejectedError("conflict", str(exc)) from exc
        except PendingChatInputManifestCorruptError as exc:
            raise PendingMaterialRejectedError("corrupt", str(exc)) from exc
        except attachment_ingest.AttachmentResolutionError as exc:
            reason: Literal["restart-lost", "expired"] = (
                "restart-lost"
                if exc.code == attachment_ingest.ATTACHMENT_LOST_IN_RESTART_CODE
                else "expired"
            )
            raise PendingMaterialRejectedError(
                reason,
                str(exc),
                attachment_index=exc.attachment_index,
                file_uuid=exc.file_uuid,
                recoverable=exc.recoverable,
            ) from exc
        except (OSError, ValueError) as exc:
            raise PendingMaterialRejectedError("invalid", str(exc)) from exc
        return StagedPendingAttachments(
            tuple(staged.attachments), tuple(staged.consumed_file_uuids)
        )

    async def insert_pending(
        self,
        command: EnqueuePendingInput,
        fingerprint: str,
    ) -> tuple[StoredPendingInput, bool]:
        turn = command.turn
        try:
            row, replayed = await self.storage.enqueue_pending_chat_input(
                pending_input_id=command.pending_input_id,
                session_key=turn.session_key,
                source_scope=turn.source_scope,
                client_request_id=turn.client_request_id,
                client_message_id=cast(str, turn.client_message_id),
                request_fingerprint=fingerprint,
                payload=pending_input_payload(turn, command.confirmed_plain_text),
                position=command.position,
            )
        except PendingChatInputCapacityError as exc:
            raise PendingEnqueueRejectedError("full") from exc
        except PendingChatInputCancelledError as exc:
            raise PendingEnqueueRejectedError("cancelled") from exc
        except PendingChatInputAlreadyDispatchedError as exc:
            raise PendingEnqueueRejectedError("dispatched") from exc
        except PendingChatInputConflictError as exc:
            raise PendingEnqueueRejectedError("conflict") from exc
        return stored_pending_input(row), replayed

    async def load_pending(self, pending_id: str) -> StoredPendingInput | None:
        row = await self.storage.get_pending_chat_input(pending_id)
        return stored_pending_input(row) if row is not None else None

    async def pending_exists(self, pending_id: str) -> bool:
        return await self.storage.get_pending_chat_input(pending_id) is not None

    async def dispatch_identity(self, pending_id: str) -> PendingDispatchIdentity | None:
        receipt = await self.storage.get_pending_chat_input_dispatch_receipt(pending_id)
        if receipt is None:
            return None
        return PendingDispatchIdentity(
            receipt.session_key,
            receipt.source_scope,
            receipt.client_request_id,
            receipt.client_message_id,
            receipt.request_fingerprint,
        )

    @staticmethod
    async def evict_upload(upload_id: str) -> None:
        from opensquilla.gateway.uploads import get_upload_store

        store = get_upload_store()
        try:
            await store.evict(upload_id)
        except Exception:  # noqa: BLE001 - durable owner already exists.
            log.warning("pending_inputs.upload_evict_failed", file_uuid=upload_id[:8])
