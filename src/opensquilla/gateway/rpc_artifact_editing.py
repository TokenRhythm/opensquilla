"""Document resources, version history, restoration, and read-only source.

Legacy editor writes return a stable upgrade error at the wire boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from opensquilla.application.artifact_workbench import (
    ChangeIdentity,
    ChangeListQuery,
    ChangeRevert,
    DocumentCapabilitiesQuery,
    DocumentIdentity,
    DocumentOpen,
    DocumentRename,
    PromptAnnotationQuery,
    RevisionListQuery,
    RevisionRestore,
    SessionDocumentsQuery,
    SourceRead,
)
from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    Anchor,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactSessionService,
    ChangeSet,
    ChangeSetStatus,
    CommitResult,
    Document,
    PromptAnnotation,
    PromptAnnotationStatus,
    Revision,
    RevisionSource,
)
from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactSessionNotFoundError,
)
from opensquilla.artifact_session.models import head_restore_receipt_state_revision
from opensquilla.artifact_session.working_files import get_working_files, restore_working_revision
from opensquilla.artifacts import (
    DEFAULT_ARTIFACT_MAX_BYTES,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactStore,
)
from opensquilla.gateway.adapters.artifact_workbench import (
    GatewayArtifactWorkbenchAdapter,
)
from opensquilla.gateway.adapters.artifact_workbench_contract import (
    register_artifact_workbench_contract,
)
from opensquilla.gateway.artifact_product_errors import (
    ArtifactProductErrorCode,
    artifact_product_error,
    logged_artifact_product_error,
)
from opensquilla.gateway.event_bridge import EventBridge
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.gateway.session_services import (
    SessionServiceUnavailableError,
    get_session_storage,
    session_id_for_key,
)
from opensquilla.gateway.websocket import get_registry
from opensquilla.html_format import is_html
from opensquilla.paths import media_root_from_config
from opensquilla.session.keys import canonicalize_session_key

_d = get_dispatcher()

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_SOURCE_OFFSET_ENCODING = "unicode-code-point"


def _actor(ctx: RpcContext) -> Actor:
    public_id = getattr(ctx.principal, "token_public_id", None)
    actor_id = public_id if isinstance(public_id, str) and public_id else None
    if actor_id is None:
        actor_id = "local-owner" if ctx.principal.is_owner else ctx.principal.role
    return Actor(kind=ActorKind.USER, actor_id=actor_id)


async def _service(ctx: RpcContext) -> ArtifactSessionService:
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            retryable=True,
            reason_code="service_unavailable",
        )
    return await ArtifactSessionService.from_session_storage(storage)


async def _scope(
    session_key: str,
    ctx: RpcContext,
) -> tuple[str, str, ArtifactSessionService]:
    session_key = canonicalize_session_key(session_key)
    try:
        session_id = await session_id_for_key(ctx.session_manager, session_key)
    except SessionServiceUnavailableError as exc:
        raise RpcUnavailableError(str(exc)) from exc
    if session_id is None:
        raise artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            reason_code="session_unavailable",
        )
    return session_key, session_id, await _service(ctx)


async def _session_epoch(ctx: RpcContext, session_key: str) -> int:
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            retryable=True,
            reason_code="service_unavailable",
        )
    return int(await storage.get_epoch(session_key))


def _not_found(kind: str, identifier: str) -> RpcHandlerError:
    del kind, identifier
    return artifact_product_error(
        ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
        reason_code="resource_unavailable",
    )


def _conflict(
    exc: Exception,
    *,
    code: ArtifactProductErrorCode = ArtifactProductErrorCode.DOCUMENT_CHANGED,
    operation: str = "artifact_document.mutate",
) -> RpcHandlerError:
    return logged_artifact_product_error(
        code,
        exc,
        operation=operation,
        retryable=False,
    )


async def _scoped_document(
    service: ArtifactSessionService,
    *,
    document_id: str,
    session_key: str,
    session_id: str,
) -> Document:
    try:
        document = await service.get_document(document_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("Document", document_id) from None
    if document.session_key != session_key or document.session_id != session_id:
        raise _not_found("Document", document_id)
    return document


async def _scoped_revision(
    service: ArtifactSessionService,
    *,
    document: Document,
    revision_id: str,
) -> Revision:
    try:
        revision = await service.get_revision(revision_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("Revision", revision_id) from None
    if revision.document_id != document.document_id:
        raise _not_found("Revision", revision_id)
    return revision


def _format_for(name: str, media_type: str, kind: ArtifactKind | None = None) -> str:
    suffix = Path(name).suffix.lower()
    mime = media_type.split(";", 1)[0].strip().lower()
    if suffix == ".docx" or mime == _DOCX_MIME:
        return "docx"
    if suffix == ".xlsx" or mime == _XLSX_MIME:
        return "xlsx"
    if suffix == ".pptx" or mime == _PPTX_MIME:
        return "pptx"
    if is_html(name, mime) or kind is ArtifactKind.HTML:
        return "html"
    return "other"


def _kind_for(ref: ArtifactRef) -> ArtifactKind:
    match _format_for(ref.name, ref.mime):
        case "docx":
            return ArtifactKind.DOCUMENT
        case "xlsx":
            return ArtifactKind.SPREADSHEET
        case "pptx":
            return ArtifactKind.PRESENTATION
        case "html":
            return ArtifactKind.HTML
        case _:
            return ArtifactKind.OTHER


def _capabilities(artifact_format: str) -> dict[str, Any]:
    html = artifact_format == "html"
    capabilities: dict[str, Any] = {
        "download": True,
        "versionHistory": True,
        "publish": html,
        "preview": html,
        "source": html,
        "manualEdit": False,
        "agentEdit": False,
        "sourceEdit": False,
        "browserUse": False,
        "selectionContext": False,
        "selection": False,
        "promptAnnotations": False,
        "engine": None,
    }
    if not html:
        capabilities["unavailableReason"] = (
            "office_adapter_not_available"
            if artifact_format in {"docx", "xlsx", "pptx"}
            else "unsupported_format"
        )
    return capabilities


def _html_integrity_failure_capabilities() -> dict[str, Any]:
    return {
        **_capabilities("html"),
        "preview": False,
        "source": False,
        "unavailableReason": "artifact_integrity_error",
    }


async def _revision_capabilities(
    ctx: RpcContext,
    document: Document,
    revision: Revision,
) -> dict[str, Any]:
    artifact_format = _format_for(revision.filename, revision.media_type, document.kind)
    capabilities = _capabilities(artifact_format)
    if artifact_format != "html":
        return capabilities
    if document.session_id is None:
        return _html_integrity_failure_capabilities()
    store = ArtifactStore(media_root_from_config(ctx.config))
    try:
        await asyncio.to_thread(
            store.validate_preview_bundle,
            revision.artifact_id,
            session_id=document.session_id,
        )
        await asyncio.to_thread(
            store.resolve_preview_resource,
            revision.artifact_id,
            session_id=document.session_id,
        )
    except (ArtifactNotFoundError, ArtifactIntegrityError, OSError, ValueError):
        return _html_integrity_failure_capabilities()
    return capabilities


def _revision_payload(revision: Revision) -> dict[str, Any]:
    return {
        "id": revision.revision_id,
        "documentId": revision.document_id,
        "parentRevisionId": revision.parent_revision_id,
        "generation": revision.generation,
        "artifactId": revision.artifact_id,
        "source": revision.source.value,
        "actorKind": revision.actor_kind.value,
        "actorId": revision.actor_id,
        "changeSetId": revision.change_set_id,
        "copiedFromRevisionId": revision.copied_from_revision_id,
        "sha256": revision.artifact_sha256,
        "name": revision.filename,
        "mime": revision.media_type,
        "size": revision.byte_size,
        "createdAt": revision.created_at,
        "schemaVersion": revision.schema_version,
        "downloadUrl": (
            f"/api/v1/artifact-documents/{revision.document_id}?revisionId={revision.revision_id}"
        ),
    }


def _document_payload(
    document: Document,
    head: Revision,
    *,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_format = _format_for(head.filename, head.media_type, document.kind)
    effective_capabilities = capabilities or _capabilities(artifact_format)
    return {
        "id": document.document_id,
        "sessionKey": document.session_key,
        "sessionId": document.session_id,
        "name": document.name,
        "kind": document.kind.value,
        "format": artifact_format,
        "headRevisionId": document.head_revision_id,
        "generation": document.generation,
        "stateRevision": document.state_revision,
        "capabilities": effective_capabilities,
        "editorState": (
            "source_ready"
            if effective_capabilities["sourceEdit"]
            else "preview_ready"
            if effective_capabilities["preview"]
            else "download_only"
        ),
        "latestDownloadUrl": f"/api/v1/artifact-documents/{document.document_id}",
        "createdAt": document.created_at,
        "updatedAt": document.updated_at,
        "schemaVersion": document.schema_version,
        "head": _revision_payload(head),
    }


def _change_set_payload(change_set: ChangeSet) -> dict[str, Any]:
    candidate = change_set.candidate_artifact
    return {
        "id": change_set.change_set_id,
        "documentId": change_set.document_id,
        "baseRevisionId": change_set.base_revision_id,
        "resultRevisionId": change_set.applied_revision_id,
        "turnId": change_set.turn_id,
        "state": change_set.status.value,
        "stateRevision": change_set.state_revision,
        "summary": change_set.summary,
        "operations": list(change_set.operations),
        "candidateArtifact": (
            None
            if candidate is None
            else {
                "id": candidate.artifact_id,
                "sha256": candidate.sha256,
                "name": candidate.filename,
                "mime": candidate.media_type,
                "size": candidate.byte_size,
            }
        ),
        "validation": change_set.validation,
        "createdByKind": change_set.created_by_kind.value,
        "createdById": change_set.created_by_id,
        "createdAt": change_set.created_at,
        "updatedAt": change_set.updated_at,
        "schemaVersion": change_set.schema_version,
    }


def _prompt_annotation_payload(
    annotation: PromptAnnotation,
    *,
    anchor: Anchor,
    current_head_revision_id: str,
) -> dict[str, Any]:
    target_status = "contextual"
    target_reason = "no_match"
    target_kind = str(anchor.locator.get("tag_name") or "element")
    target_text = anchor.quote
    return {
        "id": annotation.annotation_id,
        "documentId": annotation.document_id,
        "revisionId": annotation.revision_id,
        "anchorId": annotation.anchor_id,
        "anchor": _anchor_payload(anchor),
        "body": annotation.body,
        "status": annotation.status.value,
        "freshness": ("current" if annotation.revision_id == current_head_revision_id else "stale"),
        "targetStatus": target_status,
        "targetReason": target_reason,
        "targetKind": target_kind,
        "targetText": target_text,
        "stateRevision": annotation.state_revision,
        "sentMessageId": annotation.sent_message_id,
        "sentTurnId": annotation.sent_turn_id,
        "sentOrder": annotation.sent_order,
        "createdAt": annotation.created_at,
        "updatedAt": annotation.updated_at,
        "schemaVersion": annotation.schema_version,
    }


def _anchor_payload(anchor: Anchor) -> dict[str, Any]:
    return {
        "anchorId": anchor.anchor_id,
        "documentId": anchor.document_id,
        "revisionId": anchor.revision_id,
        "kind": anchor.kind.value,
        "locator": anchor.locator,
        "quote": anchor.quote,
        "context": anchor.context,
        "state": anchor.state.value,
        "remappedFromAnchorId": anchor.remapped_from_anchor_id,
        "createdAt": anchor.created_at,
        "schemaVersion": anchor.schema_version,
    }


async def _prompt_annotation_anchor(
    service: ArtifactSessionService,
    annotation: PromptAnnotation,
) -> Anchor:
    try:
        anchor = await service.get_anchor(annotation.anchor_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("Anchor", annotation.anchor_id) from None
    if (
        anchor.anchor_id != annotation.anchor_id
        or anchor.document_id != annotation.document_id
        or anchor.revision_id != annotation.revision_id
    ):
        raise _not_found("Anchor", annotation.anchor_id)
    return anchor


async def _document_with_head(
    ctx: RpcContext,
    service: ArtifactSessionService,
    document: Document,
    head: Revision | None = None,
) -> dict[str, Any]:
    effective_head = head or await service.get_revision(document.head_revision_id)
    capabilities = await _revision_capabilities(ctx, document, effective_head)
    return _document_payload(document, effective_head, capabilities=capabilities)


async def _mutation_document_payload(
    ctx: RpcContext,
    service: ArtifactSessionService,
    result: CommitResult,
) -> dict[str, Any]:
    """Return a coherent document projection for a durable mutation receipt.

    An idempotent replay can arrive after a later collaborator has advanced the
    document. The receipt must still identify the originally applied revision,
    while the mutable document projection must describe the current head rather
    than pairing a new headRevisionId with the old revision payload.
    """

    head = (
        result.revision if result.document.head_revision_id == result.revision.revision_id else None
    )
    return await _document_with_head(ctx, service, result.document, head)


async def _emit_artifact_state(
    ctx: RpcContext,
    *,
    session_key: str,
    service: ArtifactSessionService,
    document_id: str,
    action: str,
    revision_id: str | None = None,
    change_set_id: str | None = None,
) -> None:
    # Resolve a notification sequence from the exact durable mutation.  A
    # source.patched replay can happen after another audit event has landed;
    # ``latest_audit_event`` would then fence the UI with the wrong sequence.
    exact_lookup = getattr(service, "audit_event_for_mutation", None)
    if callable(exact_lookup) and (revision_id is not None or change_set_id is not None):
        latest = await exact_lookup(
            document_id,
            revision_id=revision_id,
            change_set_id=change_set_id,
        )
    elif revision_id is not None or change_set_id is not None:
        latest = None
        list_events = getattr(service, "list_audit_events", None)
        if callable(list_events):
            for event in await list_events(document_id):
                event_type = getattr(event, "event_type", "")
                exact_pair = revision_id is not None and change_set_id is not None
                if not exact_pair and not (
                    isinstance(event_type, str)
                    and (
                        event_type.startswith("revision.")
                        or event_type
                        in {
                            "document.created",
                            "document.restored",
                            "document.reverted",
                            "change_set.applied",
                        }
                    )
                ):
                    continue
                if revision_id is not None and event.revision_id != revision_id:
                    continue
                if change_set_id is not None and event.change_set_id != change_set_id:
                    continue
                if latest is None or event.sequence > latest.sequence:
                    latest = event
    else:
        latest = await service.latest_audit_event(document_id)
    if latest is None:
        return
    payload = {
        "artifactEventSeq": latest.sequence,
        "documentId": document_id,
        "revisionId": revision_id,
        "changeSetId": change_set_id,
        "action": action,
    }
    bridge = EventBridge(ctx.subscription_manager, get_registry())
    # Keep the legacy session event during the compatibility window while the
    # format-neutral workbench migrates to the document lifecycle name.
    await bridge.emit(session_key, "session.event.artifact_state", payload)
    await bridge.emit(session_key, "document.state_changed", payload)


async def _commit_revision_copy_mutation(
    service: ArtifactSessionService,
    *,
    document: Document,
    target_revision: Revision,
    expected_head_revision_id: str,
    expected_state_revision: int,
    actor: Actor,
    turn_id: str,
    operations: tuple[dict[str, Any], ...],
    summary: str,
    source: RevisionSource,
    revision_event_type: str,
) -> tuple[CommitResult, ChangeSet, bool]:
    replay = await _applied_mutation_replay(
        service,
        document_id=document.document_id,
        turn_id=turn_id,
        base_revision_id=expected_head_revision_id,
        operations=operations,
        candidate_sha256=target_revision.artifact_sha256,
        candidate_artifact_id=target_revision.artifact_id,
    )
    if replay is not None:
        replay_result, replay_change = replay
        return replay_result, replay_change, True

    result: CommitResult | None = None
    committed_change: ChangeSet | None = None
    try:
        result, committed_change = await service.commit_change_set_atomically(
            document_id=document.document_id,
            base_revision_id=expected_head_revision_id,
            expected_document_state_revision=expected_state_revision,
            operations=operations,
            candidate_artifact=target_revision.artifact,
            validation={
                "target_revision_id": target_revision.revision_id,
                "target_sha256": target_revision.artifact_sha256,
                "status": "passed",
            },
            actor=actor,
            turn_id=turn_id,
            summary=summary,
            source=source,
            copied_from_revision_id=target_revision.revision_id,
            revision_event_type=revision_event_type,
        )
    except BaseException as exc:
        try:
            replay = await _applied_mutation_replay(
                service,
                document_id=document.document_id,
                turn_id=turn_id,
                base_revision_id=expected_head_revision_id,
                operations=operations,
                candidate_sha256=target_revision.artifact_sha256,
                candidate_artifact_id=target_revision.artifact_id,
            )
        except ArtifactConflictError:
            replay = None
        if replay is not None:
            result, committed_change = replay
        if result is None:
            if isinstance(exc, ArtifactConflictError):
                raise
            raise
        if not isinstance(exc, Exception):
            raise
    assert result is not None and committed_change is not None
    return result, committed_change, False


async def _artifact_capabilities(
    query: DocumentCapabilitiesQuery,
    ctx: RpcContext,
) -> dict[str, Any]:
    formats = {name: _capabilities(name) for name in ("docx", "xlsx", "pptx", "html")}
    document_id = query.document_id
    if document_id is None:
        return {"formats": formats, "desktopFirst": True}
    assert query.session_key is not None
    session_key, session_id, service = await _scope(query.session_key, ctx)
    document = await _scoped_document(
        service,
        document_id=document_id,
        session_key=session_key,
        session_id=session_id,
    )
    head = await service.get_revision(document.head_revision_id)
    artifact_format = _format_for(head.filename, head.media_type, document.kind)
    return {
        "documentId": document.document_id,
        "format": artifact_format,
        "capabilities": await _revision_capabilities(ctx, document, head),
    }


async def _document_open(
    command: DocumentOpen,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(command.session_key, ctx)
    artifact_id = command.artifact_id
    store = ArtifactStore(media_root_from_config(ctx.config))
    try:
        ref, _ = await asyncio.to_thread(
            store.resolve_for_download,
            artifact_id,
            session_id=session_id,
        )
    except (ArtifactNotFoundError, ArtifactIntegrityError, ValueError):
        raise _not_found("Artifact", artifact_id) from None

    try:
        result, adopted = await service.adopt_document(
            session_key=session_key,
            session_id=session_id,
            name=ref.name,
            kind=_kind_for(ref),
            initial_artifact=ArtifactBlobRef(
                artifact_id=ref.id,
                sha256=ref.sha256,
                filename=ref.name,
                media_type=ref.mime,
                byte_size=ref.size,
            ),
            actor=_actor(ctx),
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.DOCUMENT_CHANGED,
            operation="document.open",
        ) from exc
    if adopted:
        await _emit_artifact_state(
            ctx,
            session_key=session_key,
            service=service,
            document_id=result.document.document_id,
            revision_id=result.revision.revision_id,
            action="document.opened",
        )
    return {
        "document": await _mutation_document_payload(ctx, service, result),
        "adopted": adopted,
    }


async def _documents_list(
    query: SessionDocumentsQuery,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(query.session_key, ctx)
    documents = await service.list_documents(
        session_key=session_key,
        session_id=session_id,
        limit=query.limit,
    )
    return {"documents": [await _document_with_head(ctx, service, item) for item in documents]}


async def _document_get(
    identity: DocumentIdentity,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(identity.session_key, ctx)
    document = await _scoped_document(
        service,
        document_id=identity.document_id,
        session_key=session_key,
        session_id=session_id,
    )
    return {"document": await _document_with_head(ctx, service, document)}


async def _document_rename(
    command: DocumentRename,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(command.session_key, ctx)
    document_id = command.document_id
    await _scoped_document(
        service,
        document_id=document_id,
        session_key=session_key,
        session_id=session_id,
    )
    try:
        document = await service.rename_document(
            document_id=document_id,
            expected_state_revision=command.expected_state_revision,
            name=command.name,
            actor=_actor(ctx),
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.DOCUMENT_CHANGED,
            operation="document.rename",
        ) from exc
    await _emit_artifact_state(
        ctx,
        session_key=session_key,
        service=service,
        document_id=document_id,
        revision_id=document.head_revision_id,
        action="document.renamed",
    )
    return {"document": await _document_with_head(ctx, service, document)}


async def _document_close(
    identity: DocumentIdentity,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(identity.session_key, ctx)
    document = await _scoped_document(
        service,
        document_id=identity.document_id,
        session_key=session_key,
        session_id=session_id,
    )
    return {
        "document": await _document_with_head(ctx, service, document),
        "closed": True,
    }


async def _revisions_list(
    query: RevisionListQuery,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(query.session_key, ctx)
    document = await _scoped_document(
        service,
        document_id=query.document_id,
        session_key=session_key,
        session_id=session_id,
    )
    revisions = await service.list_revisions(
        document.document_id,
        limit=query.limit,
    )
    if all(item.revision_id != document.head_revision_id for item in revisions):
        revisions = (*revisions, await service.get_revision(document.head_revision_id))
    return {"revisions": [_revision_payload(item) for item in revisions]}


async def _revision_restore(
    command: RevisionRestore,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(command.session_key, ctx)
    document_id = command.document_id
    document = await _scoped_document(
        service,
        document_id=document_id,
        session_key=session_key,
        session_id=session_id,
    )
    target_id = command.revision_id
    target_revision = await _scoped_revision(
        service,
        document=document,
        revision_id=target_id,
    )
    expected_head = command.expected_head_revision_id
    expected_state_revision = command.expected_state_revision
    request_id = command.request_id
    turn_id = f"revision-restore:{request_id}"
    try:
        result, mutation_change, replayed = await restore_working_revision(
            service, ArtifactStore(media_root_from_config(ctx.config)),
            document_id=document_id,
            session_key=session_key,
            session_id=session_id,
            target_revision_id=target_revision.revision_id,
            expected_head_revision_id=expected_head,
            expected_state_revision=expected_state_revision,
            actor=_actor(ctx),
            turn_id=turn_id,
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.DOCUMENT_CHANGED,
            operation="revision.restore",
        ) from exc
    if not replayed and not (mutation_change.validation or {}).get("no_op", False):
        await _emit_artifact_state(
            ctx,
            session_key=session_key,
            service=service,
            document_id=document_id,
            revision_id=result.revision.revision_id,
            change_set_id=mutation_change.change_set_id,
            action="revision.restored",
        )
    return {
        "document": await _mutation_document_payload(ctx, service, result),
        "revision": _revision_payload(result.revision),
        "changeSet": _change_set_payload(mutation_change),
        "receipt": _mutation_receipt_payload(
            request_id=request_id,
            base_revision_id=expected_head,
            result=result,
            change_set=mutation_change,
        ),
    }


async def _changes_list(
    query: ChangeListQuery,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(query.session_key, ctx)
    document = await _scoped_document(
        service,
        document_id=query.document_id,
        session_key=session_key,
        session_id=session_id,
    )
    changes = await service.list_change_sets(
        document.document_id,
        limit=query.limit,
    )
    return {"changeSets": [
        _change_set_payload(item) for item in changes
        if not ((item.validation or {}).get("restore_mode") == "head_pointer"
                and (item.validation or {}).get("no_op") is True)
    ]}


async def _change_get(
    identity: ChangeIdentity,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(identity.session_key, ctx)
    document = await _scoped_document(
        service,
        document_id=identity.document_id,
        session_key=session_key,
        session_id=session_id,
    )
    change_id = identity.change_set_id
    try:
        change_set = await service.get_change_set(change_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("ChangeSet", change_id) from None
    if change_set.document_id != document.document_id:
        raise _not_found("ChangeSet", change_id)
    return {"changeSet": _change_set_payload(change_set)}


async def _change_revert(
    command: ChangeRevert,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(command.session_key, ctx)
    document_id = command.document_id
    document = await _scoped_document(
        service,
        document_id=document_id,
        session_key=session_key,
        session_id=session_id,
    )
    change_id = command.change_set_id
    try:
        change_set = await service.get_change_set(change_id)
    except ArtifactSessionNotFoundError:
        raise _not_found("ChangeSet", change_id) from None
    if change_set.document_id != document.document_id:
        raise _not_found("ChangeSet", change_id)
    if change_set.applied_revision_id is None or (
        (change_set.validation or {}).get("restore_mode") == "head_pointer"
        and (change_set.validation or {}).get("no_op") is True
    ):
        raise artifact_product_error(
            ArtifactProductErrorCode.MUTATION_NOT_APPLIED,
            reason_code="change_not_applied",
        )
    target_revision = await _scoped_revision(
        service,
        document=document,
        revision_id=change_set.base_revision_id,
    )
    expected_head = command.expected_head_revision_id
    expected_state_revision = command.expected_state_revision
    request_id = command.request_id
    turn_id = f"change-revert:{request_id}"
    operations: tuple[dict[str, Any], ...] = (
        {
            "op": "revert_change_set",
            "reverted_change_set_id": change_id,
            "target_revision_id": target_revision.revision_id,
            "target_sha256": target_revision.artifact_sha256,
            "expected_document_state_revision": expected_state_revision,
        },
    )
    try:
        replay = await _applied_mutation_replay(
            service,
            document_id=document_id,
            turn_id=turn_id,
            base_revision_id=expected_head,
            operations=operations,
            candidate_sha256=target_revision.artifact_sha256,
            candidate_artifact_id=target_revision.artifact_id,
        )
    except ArtifactConflictError as exc:
        raise _conflict(
            exc,
            code=ArtifactProductErrorCode.DOCUMENT_CHANGED,
            operation="change.revert_replay",
        ) from exc
    if replay is not None:
        result, mutation_change = replay
        replayed = True
    else:
        if document.head_revision_id != change_set.applied_revision_id:
            raise artifact_product_error(
                ArtifactProductErrorCode.DOCUMENT_CHANGED,
                reason_code="change_not_current",
            )
        try:
            result, mutation_change, replayed = await _commit_revision_copy_mutation(
                service,
                document=document,
                target_revision=target_revision,
                expected_head_revision_id=expected_head,
                expected_state_revision=expected_state_revision,
                actor=_actor(ctx),
                turn_id=turn_id,
                operations=operations,
                summary="Revert applied document change",
                source=RevisionSource.REVERT,
                revision_event_type="document.reverted",
            )
        except ArtifactConflictError as exc:
            raise _conflict(exc) from exc
    await _sync_restored_working_files(ctx, service, result.document)
    if not replayed:
        await _emit_artifact_state(
            ctx,
            session_key=session_key,
            service=service,
            document_id=document_id,
            revision_id=result.revision.revision_id,
            change_set_id=mutation_change.change_set_id,
            action="change.reverted",
        )
    return {
        "document": await _mutation_document_payload(ctx, service, result),
        "revision": _revision_payload(result.revision),
        # Preserve the v1 field: callers asked to revert this applied change.
        "changeSet": _change_set_payload(change_set),
        "mutationChangeSet": _change_set_payload(mutation_change),
        "receipt": _mutation_receipt_payload(
            request_id=request_id,
            base_revision_id=expected_head,
            result=result,
            change_set=mutation_change,
        ),
    }


async def _prompt_annotations_list(
    query: PromptAnnotationQuery,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(query.session_key, ctx)
    session_epoch = await _session_epoch(ctx, session_key)
    document_id = query.document_id
    if document_id is not None:
        await _scoped_document(
            service,
            document_id=document_id,
            session_key=session_key,
            session_id=session_id,
        )
    try:
        status = PromptAnnotationStatus(query.status)
    except ValueError as exc:
        raise ValueError("annotation status is unsupported") from exc
    annotations = await service.list_prompt_annotations(
        session_key=session_key,
        session_id=session_id,
        session_epoch=session_epoch,
        status=status,
        document_id=document_id,
        limit=query.limit,
    )
    documents: dict[str, Document] = {}
    payloads: list[dict[str, Any]] = []
    for annotation in annotations:
        document = documents.get(annotation.document_id)
        if document is None:
            document = await _scoped_document(
                service,
                document_id=annotation.document_id,
                session_key=session_key,
                session_id=session_id,
            )
            documents[annotation.document_id] = document
        payloads.append(
            _prompt_annotation_payload(
                annotation,
                anchor=await _prompt_annotation_anchor(service, annotation),
                current_head_revision_id=document.head_revision_id,
            )
        )
    return {"annotations": payloads}


async def _resolve_source_revision(
    *,
    ctx: RpcContext,
    service: ArtifactSessionService,
    session_id: str,
    document: Document,
    revision_id: str,
) -> tuple[Revision, str]:
    revision = await _scoped_revision(service, document=document, revision_id=revision_id)
    if _format_for(revision.filename, revision.media_type, document.kind) != "html":
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED, reason_code="format_unsupported"
        )
    binding = await get_working_files(service, document.document_id)
    try:
        if binding is not None and revision_id == document.head_revision_id:
            path = binding.entry
        else:
            store = ArtifactStore(media_root_from_config(ctx.config))
            resource = await asyncio.to_thread(
                store.resolve_preview_resource, revision.artifact_id, session_id=session_id
            )
            path = resource.path
        source = await asyncio.to_thread(_read_source_text, path)
    except ArtifactNotFoundError:
        raise _not_found("Revision", revision_id) from None
    except (ArtifactIntegrityError, OSError, ValueError) as exc:
        raise logged_artifact_product_error(
            ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
            exc,
            operation="artifact.source.read",
            retryable=True,
        ) from exc
    return revision, source


async def _source_read(
    query: SourceRead,
    ctx: RpcContext,
) -> dict[str, Any]:
    session_key, session_id, service = await _scope(query.session_key, ctx)
    document = await _scoped_document(
        service,
        document_id=query.document_id,
        session_key=session_key,
        session_id=session_id,
    )
    revision_id = query.revision_id or document.head_revision_id
    revision, source = await _resolve_source_revision(
        ctx=ctx,
        service=service,
        session_id=session_id,
        document=document,
        revision_id=revision_id,
    )
    return {
        "source": {
            "documentId": document.document_id,
            "revisionId": revision.revision_id,
            "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "text": source,
            "language": "html",
            "offsetEncoding": _SOURCE_OFFSET_ENCODING,
            "stateRevision": document.state_revision,
        }
    }


def _mutation_receipt_payload(
    *,
    request_id: str,
    base_revision_id: str,
    result: CommitResult,
    change_set: ChangeSet,
) -> dict[str, Any]:
    state_revision = _mutation_result_state_revision(result, change_set)
    return {
        "requestId": request_id,
        "documentId": result.document.document_id,
        "baseRevisionId": base_revision_id,
        "resultRevisionId": result.revision.revision_id,
        "changeSetId": change_set.change_set_id,
        "stateRevision": state_revision,
        "status": "applied",
    }


def _mutation_result_state_revision(
    result: CommitResult,
    change_set: ChangeSet,
) -> int:
    restored_state = head_restore_receipt_state_revision(change_set, result.revision)
    if restored_state is not None:
        return restored_state
    expected = {
        operation.get("expected_document_state_revision")
        for operation in change_set.operations
        if isinstance(operation.get("expected_document_state_revision"), int)
        and not isinstance(operation.get("expected_document_state_revision"), bool)
    }
    if len(expected) == 1:
        value = next(iter(expected))
        assert isinstance(value, int)
        if value > 0:
            return value + 1
    return result.document.state_revision


async def _applied_mutation_replay(
    service: ArtifactSessionService,
    *,
    document_id: str,
    turn_id: str,
    base_revision_id: str,
    operations: tuple[dict[str, Any], ...],
    candidate_sha256: str,
    candidate_artifact_id: str | None = None,
) -> tuple[CommitResult, ChangeSet] | None:
    change_set = await service.get_change_set_by_turn(
        document_id=document_id,
        turn_id=turn_id,
    )
    if change_set is None:
        return None
    if (
        change_set.base_revision_id != base_revision_id
        or change_set.operations != operations
        or change_set.candidate_artifact_sha256 != candidate_sha256
        or (
            candidate_artifact_id is not None
            and change_set.candidate_artifact_id != candidate_artifact_id
        )
    ):
        raise ArtifactConflictError(
            "clientRequestId was already used for a different document mutation"
        )
    if change_set.status is not ChangeSetStatus.APPLIED or change_set.applied_revision_id is None:
        raise ArtifactConflictError("document mutation receipt is not applied")
    document = await service.get_document(document_id)
    revision = await service.get_revision(change_set.applied_revision_id)
    restored_state = head_restore_receipt_state_revision(change_set, revision)
    if restored_state is not None:
        return CommitResult(document=document, revision=revision), change_set
    if (
        revision.change_set_id != change_set.change_set_id
        or revision.artifact_sha256 != candidate_sha256
        or revision.artifact_id != change_set.candidate_artifact_id
    ):
        raise ArtifactConflictError("applied document mutation receipt is inconsistent")
    return CommitResult(document=document, revision=revision), change_set


def _read_source_text(path: Path) -> str:
    with path.open("rb") as stream:
        payload = stream.read(DEFAULT_ARTIFACT_MAX_BYTES + 1)
    if len(payload) > DEFAULT_ARTIFACT_MAX_BYTES:
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED, reason_code="size_unsupported"
        )
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise artifact_product_error(
            ArtifactProductErrorCode.RESOURCE_UNSUPPORTED, reason_code="encoding_unsupported"
        ) from exc


async def _sync_restored_working_files(
    ctx: RpcContext, service: ArtifactSessionService, document: Document
) -> None:
    if document.kind is not ArtifactKind.HTML:
        return
    if document.session_id is None:
        # Historical unscoped documents cannot authorize a session workspace copy.
        return
    if await get_working_files(service, document.document_id) is None:
        return
    from opensquilla.gateway.workbench_resource_runtime import ensure_document_working_files

    await ensure_document_working_files(
        ctx,
        service=service,
        session_key=document.session_key,
        session_id=document.session_id,
        document_id=document.document_id,
    )


async def _handle_retired_document_editing(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    raise RpcHandlerError(
        "DOCUMENT_EDITING_RETIRED",
        "This HTML editor API has been retired. Update the client, reopen the page, "
        "and send annotations as ordinary chat input. Existing versions remain available.",
        details={"action": "update_client_and_reopen_page"},
    )


class _ArtifactEditingRuntimePort:
    """Bind typed Workbench commands to the existing artifact-session implementation."""

    def __init__(self, ctx: RpcContext) -> None:
        self._ctx = ctx

    async def capabilities(self, query: DocumentCapabilitiesQuery) -> dict[str, Any]:
        return await _artifact_capabilities(query, self._ctx)

    async def open_document(self, command: DocumentOpen) -> dict[str, Any]:
        return await _document_open(command, self._ctx)

    async def list_documents(self, query: SessionDocumentsQuery) -> dict[str, Any]:
        return await _documents_list(query, self._ctx)

    async def get_document(self, identity: DocumentIdentity) -> dict[str, Any]:
        return await _document_get(identity, self._ctx)

    async def rename_document(self, command: DocumentRename) -> dict[str, Any]:
        return await _document_rename(command, self._ctx)

    async def close_document(self, identity: DocumentIdentity) -> dict[str, Any]:
        return await _document_close(identity, self._ctx)

    async def list_revisions(self, query: RevisionListQuery) -> dict[str, Any]:
        return await _revisions_list(query, self._ctx)

    async def restore_revision(self, command: RevisionRestore) -> dict[str, Any]:
        return await _revision_restore(command, self._ctx)

    async def list_changes(self, query: ChangeListQuery) -> dict[str, Any]:
        return await _changes_list(query, self._ctx)

    async def get_change(self, identity: ChangeIdentity) -> dict[str, Any]:
        return await _change_get(identity, self._ctx)

    async def revert_change(self, command: ChangeRevert) -> dict[str, Any]:
        return await _change_revert(command, self._ctx)

    async def list_annotations(self, query: PromptAnnotationQuery) -> dict[str, Any]:
        return await _prompt_annotations_list(query, self._ctx)

    async def read_source(self, query: SourceRead) -> dict[str, Any]:
        return await _source_read(query, self._ctx)


_RETIRED_EDITOR_METHODS = frozenset(
    (
        "documents.editSessions.start",
        "documents.editSessions.heartbeat",
        "documents.editSessions.close",
        "artifacts.prompt_annotations.create",
        "artifacts.prompt_annotations.focus",
        "artifacts.prompt_annotations.update",
        "artifacts.prompt_annotations.discard",
        "artifacts.source.patch",
    )
)

_ARTIFACT_EDITING_METHODS = (
    "artifacts.edit.capabilities",
    "artifacts.documents.open",
    "artifacts.documents.list",
    "artifacts.documents.get",
    "artifacts.documents.rename",
    "artifacts.documents.close",
    "documents.editSessions.start",
    "documents.editSessions.heartbeat",
    "documents.editSessions.close",
    "artifacts.revisions.list",
    "artifacts.revisions.restore",
    "artifacts.changes.list",
    "artifacts.changes.get",
    "artifacts.changes.revert",
    "artifacts.prompt_annotations.list",
    "artifacts.prompt_annotations.create",
    "artifacts.prompt_annotations.focus",
    "artifacts.prompt_annotations.update",
    "artifacts.prompt_annotations.discard",
    "artifacts.source.read",
    "artifacts.source.patch",
)

(
    _handle_artifact_capabilities,
    _handle_document_open,
    _handle_documents_list,
    _handle_document_get,
    _handle_document_rename,
    _handle_document_close,
    _handle_edit_session_start,
    _handle_edit_session_heartbeat,
    _handle_edit_session_close,
    _handle_revisions_list,
    _handle_revision_restore,
    _handle_changes_list,
    _handle_change_get,
    _handle_change_revert,
    _handle_prompt_annotations_list,
    _handle_prompt_annotation_create,
    _handle_prompt_annotation_focus,
    _handle_prompt_annotation_update,
    _handle_prompt_annotation_discard,
    _handle_source_read,
    _handle_source_patch,
) = tuple(
    (
        _handle_retired_document_editing
        if method in _RETIRED_EDITOR_METHODS
        else GatewayArtifactWorkbenchAdapter.bind(method, _ArtifactEditingRuntimePort)
    )
    for method in _ARTIFACT_EDITING_METHODS
)

for _artifact_method, _artifact_implementation in zip(
    _ARTIFACT_EDITING_METHODS,
    (
        _handle_artifact_capabilities,
        _handle_document_open,
        _handle_documents_list,
        _handle_document_get,
        _handle_document_rename,
        _handle_document_close,
        _handle_edit_session_start,
        _handle_edit_session_heartbeat,
        _handle_edit_session_close,
        _handle_revisions_list,
        _handle_revision_restore,
        _handle_changes_list,
        _handle_change_get,
        _handle_change_revert,
        _handle_prompt_annotations_list,
        _handle_prompt_annotation_create,
        _handle_prompt_annotation_focus,
        _handle_prompt_annotation_update,
        _handle_prompt_annotation_discard,
        _handle_source_read,
        _handle_source_patch,
    ),
    strict=True,
):
    if _artifact_method in _RETIRED_EDITOR_METHODS:
        _d.method(_artifact_method, scope="operator.write")(_artifact_implementation)
    else:
        register_artifact_workbench_contract(
            _d,
            _artifact_method,
            _artifact_implementation,
            internal_error=RpcHandlerError,
            guest_allowed_checker=is_guest_rpc_method_allowed,
        )


__all__ = [
    "_handle_artifact_capabilities",
    "_handle_change_get",
    "_handle_change_revert",
    "_handle_changes_list",
    "_handle_document_close",
    "_handle_document_get",
    "_handle_document_open",
    "_handle_document_rename",
    "_handle_documents_list",
    "_handle_edit_session_close",
    "_handle_edit_session_heartbeat",
    "_handle_edit_session_start",
    "_handle_prompt_annotation_create",
    "_handle_prompt_annotation_discard",
    "_handle_prompt_annotation_focus",
    "_handle_prompt_annotation_update",
    "_handle_prompt_annotations_list",
    "_handle_revision_restore",
    "_handle_revisions_list",
    "_handle_source_patch",
    "_handle_source_read",
]
