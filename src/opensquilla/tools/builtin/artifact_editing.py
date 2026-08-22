"""Context-bound tools for the Artifact IDE.

These tools deliberately have no model-facing document, revision, session, path,
XML, JavaScript-execution, or CDP arguments.  Authority comes exclusively from
the short-lived ``ToolContext`` populated by authenticated Web/Desktop ingress.
Every call revalidates that binding against the live ArtifactSession head before
reading or applying a mutation.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import secrets
import zipfile
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    Anchor,
    AnchorState,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactSessionService,
    ChangeSet,
    ChangeSetStatus,
    CommitResult,
    Document,
    Revision,
    WriterLease,
)
from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactSessionNotFoundError,
)
from opensquilla.artifact_session.mutation_attempts import (
    ArtifactMutationCleanupAmbiguousError,
)
from opensquilla.artifact_validation import (
    ArtifactValidationError as DeliveryValidationError,
)
from opensquilla.artifact_validation import validate_artifact_for_delivery
from opensquilla.artifacts import (
    DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
    DEFAULT_ARTIFACT_MAX_BYTES,
    ArtifactError,
    ArtifactRef,
    ArtifactStore,
    is_complete_single_file_preview_bundle,
)
from opensquilla.tools.builtin.artifact_range_grants import (
    ArtifactRangeBinding,
    ArtifactRangeGrantError,
    DocumentGrantBinding,
    ResolvedRangeGrant,
    clear_context_registry,
    document_grant_registry_for_context,
    registry_for_context,
)
from opensquilla.tools.builtin.document_format_adapters import (
    DocumentAdapterError,
    DocumentMutationError,
    DocumentMutationRetryPolicy,
    GrantedMutationInput,
    HtmlDocumentFormatAdapter,
    get_document_format_adapter,
    mutation_error_from_adapter,
)
from opensquilla.tools.types import (
    CallerKind,
    InteractionMode,
    RetryableToolInputError,
    SafeToolError,
    ToolContext,
    current_tool_context,
)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})
_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

_MAX_HTML_SOURCE_BYTES = 2 * 1024 * 1024
_MAX_HTML_PATCHES = 100
_MAX_HTML_EXPECTED_BYTES = 2 * 1024 * 1024
_MAX_HTML_REPLACEMENT_BYTES = 2 * 1024 * 1024
_MAX_DOCUMENT_MUTATIONS = 100
_MAX_DOCUMENT_INPUT_BYTES = 2 * 1024 * 1024
_MAX_HTML_READ_CHUNK_CHARS = 16 * 1024
_DEFAULT_HTML_READ_CHUNK_CHARS = 8 * 1024
_MAX_HTML_RANGE_TEXT_BYTES = 16 * 1024
_MAX_HTML_SEARCH_LITERAL_BYTES = 4 * 1024
_MAX_HTML_SEARCH_MATCHES = 16
_MAX_HTML_CONTEXT_CHARS = 160
_MAX_SUMMARY_CHARS = 1_000
_MAX_ANCHOR_JSON_BYTES = 32 * 1024
_MAX_OOXML_MEMBERS = 10_000
_MAX_OOXML_INFLATED_BYTES = 200 * 1024 * 1024
_WRITER_LEASE_TTL_MS = 60_000
_HTML_OFFSET_ENCODING = "unicode-code-point"

_NO_ARGUMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class _ArtifactScope:
    ctx: ToolContext
    context: Any
    service: ArtifactSessionService
    document: Document
    revision: Revision
    anchors: tuple[Anchor, ...]
    session_epoch: int = 0

    @property
    def anchor(self) -> Anchor | None:
        """Compatibility projection for legacy single-selection tools."""

        return self.anchors[0] if len(self.anchors) == 1 else None


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _candidate_source(adapter_id: str, turn_id: str) -> str:
    """Build a restart-recovery marker for one turn's hidden candidates.

    The turn id itself is deliberately not persisted in the artifact source;
    a short hash gives recovery an exact, bounded ownership key without
    exposing arbitrary task metadata in artifact listings.
    """

    turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
    return f"document_{adapter_id}_agent_candidate:{turn_digest}"


async def _emit_artifact_state(
    scope: _ArtifactScope,
    *,
    action: str,
    revision_id: str | None = None,
    change_set_id: str | None = None,
) -> bool:
    """Best-effort metadata notification after a durable mutation commits.

    The boolean lets a loop remember that the notification was actually
    delivered.  A finish response can be replayed after the SQLite commit;
    callers must not emit ``source.patched`` twice, but should retry when the
    first best-effort notification itself failed.
    """

    emitter = scope.ctx.artifact_event_emitter
    if not callable(emitter):
        return False
    try:
        # ``source.patched`` is delivered after the durable transaction and
        # may be retried after a lost response.  Resolve its sequence from the
        # exact committed revision/change set; using the document's newest row
        # can point clients at an unrelated later mutation and suppress the
        # refresh via their monotonic sequence fence.
        exact_lookup = getattr(scope.service, "audit_event_for_mutation", None)
        if callable(exact_lookup) and (revision_id is not None or change_set_id is not None):
            latest = await exact_lookup(
                scope.document.document_id,
                revision_id=revision_id,
                change_set_id=change_set_id,
            )
        elif revision_id is not None or change_set_id is not None:
            # Compatibility with older service doubles: filter the append-only
            # list locally instead of falling back to an unrelated newest row.
            list_events = getattr(scope.service, "list_audit_events", None)
            latest = None
            if callable(list_events):
                events = await list_events(scope.document.document_id)
                for event in events:
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
            latest = await scope.service.latest_audit_event(scope.document.document_id)
        if latest is None:
            return False
        await emitter(
            {
                "artifactEventSeq": latest.sequence,
                "documentId": scope.document.document_id,
                "revisionId": revision_id,
                "changeSetId": change_set_id,
                "action": action,
            }
        )
        return True
    except Exception:  # noqa: BLE001 - notification failure cannot undo the mutation
        return False


def _format_for(name: str, media_type: str, kind: object | None = None) -> str:
    suffix = Path(name).suffix.lower()
    mime = media_type.split(";", 1)[0].strip().lower()
    if suffix == ".docx" or mime == _DOCX_MIME:
        return "docx"
    if suffix == ".xlsx" or mime == _XLSX_MIME:
        return "xlsx"
    if suffix == ".pptx" or mime == _PPTX_MIME:
        return "pptx"
    kind_value = getattr(kind, "value", kind)
    if suffix in _HTML_SUFFIXES or mime in _HTML_MIMES or kind_value == "html":
        return "html"
    return "other"


def _actor(scope: _ArtifactScope) -> Actor:
    actor_id = str(scope.ctx.agent_id or "").strip()
    if not actor_id:
        raise SafeToolError("Artifact editing requires an authenticated agent identity.")
    return Actor(ActorKind.AGENT, actor_id)


def _stale_context() -> DocumentMutationError:
    return DocumentMutationError(
        "DOCUMENT_CONTEXT_STALE",
        "The document selection expired or changed. Refresh the current document.",
        retry_policy="refresh",
    )


async def _current_scope(
    tool_name: str,
    *,
    require_anchor: bool = False,
    required_format: str | None = None,
) -> _ArtifactScope:
    """Resolve only server-injected authority and revalidate it fail-closed."""

    ctx = current_tool_context.get()
    if (
        ctx is None
        or not ctx.is_owner
        or ctx.caller_kind is not CallerKind.WEB
        or ctx.interaction_mode is not InteractionMode.INTERACTIVE
        or ctx.subagent_depth != 0
        or ctx.guest_safe
    ):
        raise SafeToolError(
            "Artifact tools require the owner in an interactive Web/Desktop editor turn."
        )

    context = ctx.artifact_context
    service = ctx.artifact_session
    if context is None or not isinstance(service, ArtifactSessionService):
        raise _stale_context()

    # Importing the Gateway type at invocation time avoids making the builtin
    # tool package depend on Gateway initialization order.
    from opensquilla.gateway.artifact_contexts import (
        BoundDocumentContext,
        BoundPromptAnnotationContext,
    )

    if not isinstance(context, (BoundDocumentContext, BoundPromptAnnotationContext)):
        raise _stale_context()
    if tool_name not in context.tool_names:
        raise SafeToolError("This capability is not available in the current editor context.")
    if (
        not isinstance(ctx.session_key, str)
        or ctx.session_key != context.session_key
        or not isinstance(ctx.artifact_session_id, str)
        or ctx.artifact_session_id != context.session_id
    ):
        raise _stale_context()

    try:
        expected_revision_id = (
            None if isinstance(context, BoundDocumentContext) else context.revision_id
        )
        current = await service.get_document_head(
            context.document_id,
            expected_revision_id=expected_revision_id,
        )
        document = current.document
        revision = current.revision
    except (ArtifactConflictError, ArtifactSessionNotFoundError):
        raise _stale_context() from None
    if (
        document.session_key != context.session_key
        or document.session_id != context.session_id
        or revision.document_id != document.document_id
        or (
            isinstance(context, BoundPromptAnnotationContext)
            and document.head_revision_id != context.revision_id
        )
    ):
        raise _stale_context()

    artifact_format = _format_for(revision.filename, revision.media_type, document.kind)
    if artifact_format != context.artifact_format:
        raise _stale_context()
    if required_format is not None and artifact_format != required_format:
        raise SafeToolError(f"This tool is available only for {required_format.upper()} artifacts.")

    if isinstance(context, BoundDocumentContext):
        if require_anchor:
            raise RetryableToolInputError(
                "Select content in the artifact before using this tool."
            )
        return _ArtifactScope(
            ctx=ctx,
            context=context,
            service=service,
            document=document,
            revision=revision,
            anchors=(),
        )

    anchors: list[Anchor] = []
    for target in context.targets:
        try:
            anchor = await service.get_anchor(target.anchor_id)
        except ArtifactSessionNotFoundError:
            raise _stale_context() from None
        if (
            anchor.document_id != document.document_id
            or anchor.revision_id != revision.revision_id
            or (
                target.status == "ready"
                and anchor.state is not AnchorState.RESOLVED
            )
            or (
                target.status == "contextual"
                and anchor.state is not AnchorState.ORPHANED
            )
        ):
            raise _stale_context()
        anchors.append(anchor)
    turn_id = str(ctx.task_id or "").strip()
    if not turn_id or len(context.annotation_ids) != len(anchors):
        raise _stale_context()
    try:
        annotations = []
        for annotation_id in context.annotation_ids:
            annotations.append(await service.get_prompt_annotation(annotation_id))
    except ArtifactSessionNotFoundError:
        raise _stale_context() from None
    for annotation, target in zip(
        annotations,
        context.targets,
        strict=True,
    ):
        if (
            annotation.session_key != context.session_key
            or annotation.session_id != context.session_id
            or annotation.document_id != document.document_id
            or annotation.revision_id != revision.revision_id
            or annotation.anchor_id != target.anchor_id
            or annotation.status.value != "sent"
            or annotation.sent_turn_id != turn_id
        ):
            raise _stale_context()
    epochs = {annotation.session_epoch for annotation in annotations}
    if len(epochs) != 1:
        raise _stale_context()
    session_epoch = next(iter(epochs))
    if require_anchor and not anchors:
        raise RetryableToolInputError("Select content in the artifact before using this tool.")
    return _ArtifactScope(
        ctx=ctx,
        context=context,
        service=service,
        document=document,
        revision=revision,
        anchors=tuple(anchors),
        session_epoch=session_epoch,
    )


def _store(scope: _ArtifactScope) -> ArtifactStore:
    root = scope.ctx.artifact_media_root
    if not isinstance(root, str) or not root.strip():
        raise SafeToolError("Artifact storage is unavailable for this editor turn.")
    return ArtifactStore(root)


async def _current_payload(
    scope: _ArtifactScope,
) -> tuple[ArtifactStore, ArtifactRef, bytes]:
    store = _store(scope)
    # PromptAnnotation candidate loops keep the canonical Document head frozen
    # until ``document_finish(commit)``.  Once a draft exists, all subsequent
    # source reads and writers must operate on that draft rather than silently
    # falling back to the old revision.  The controller only exposes an opaque
    # blob reference; path resolution and integrity checks remain owned by the
    # session-scoped ArtifactStore.
    candidate_controller = getattr(scope.ctx, "artifact_candidate_loop_controller", None)
    candidate_blob = getattr(candidate_controller, "candidate_artifact", None)
    if candidate_blob is not None:
        try:
            ref, path = await asyncio.to_thread(
                store.resolve_for_download,
                candidate_blob.artifact_id,
                session_id=scope.context.session_id,
            )
            payload = await asyncio.to_thread(path.read_bytes)
        except (ArtifactError, OSError, ValueError):
            raise SafeToolError(
                "The current candidate bytes failed integrity validation. Discard and retry."
            ) from None
        if (
            ref.session_key != scope.context.session_key
            or ref.sha256 != candidate_blob.sha256
            or ref.name != candidate_blob.filename
            or ref.mime != candidate_blob.media_type
            or ref.size != candidate_blob.byte_size
            or len(payload) != ref.size
            or hashlib.sha256(payload).hexdigest() != ref.sha256
        ):
            raise SafeToolError(
                "The current candidate bytes failed integrity validation. Discard and retry."
            )
        return store, ref, payload
    try:
        ref, path = await asyncio.to_thread(
            store.resolve_for_download,
            scope.revision.artifact_id,
            session_id=scope.context.session_id,
        )
        payload = await asyncio.to_thread(path.read_bytes)
    except (ArtifactError, OSError, ValueError):
        raise SafeToolError(
            "The current artifact bytes failed integrity validation. Reopen or regenerate it."
        ) from None
    if (
        ref.session_key != scope.context.session_key
        or ref.sha256 != scope.revision.artifact_sha256
        or ref.name != scope.revision.filename
        or ref.mime != scope.revision.media_type
        or ref.size != scope.revision.byte_size
        or len(payload) != ref.size
        or hashlib.sha256(payload).hexdigest() != ref.sha256
    ):
        raise SafeToolError(
            "The current artifact bytes failed integrity validation. Reopen or regenerate it."
        )
    return store, ref, payload


async def _stage_prepared_document_mutation(
    prepared: PreparedDocumentMutation,
    *,
    tool_use_id: str | None = None,
    proposal_sha256: str | None = None,
) -> str | None:
    """Publish a validated candidate without advancing the Document head.

    The normal document tools retain their historical immediate-commit path.
    A PromptAnnotation turn injects ``ArtifactCandidateLoopController`` and
    therefore takes this branch: bytes are published as an internal artifact,
    the single draft ChangeSet is CAS-updated, and the model receives a
    ``candidate_staged`` result.  No revision/event is emitted here.
    """

    scope = prepared.scope
    controller = getattr(scope.ctx, "artifact_candidate_loop_controller", None)
    if controller is None:
        return None

    # A provider may replay a writer call after its response was lost.  Check
    # the turn-local DRAFT receipt before publishing another physical blob;
    # the controller persists the opaque id/digest in validation JSON, so this
    # also works after a controller is reconstructed from the same turn.
    replay_candidate = getattr(controller, "replay_candidate", None)
    if (
        callable(replay_candidate)
        and isinstance(tool_use_id, str)
        and tool_use_id
        and isinstance(proposal_sha256, str)
        and proposal_sha256
    ):
        replay = await replay_candidate(
            tool_use_id=tool_use_id,
            proposal_sha256=proposal_sha256,
        )
        if replay is not None:
            prepared.release_grants()
            state = getattr(controller, "state", None)
            return _json(
                {
                    "status": "candidate_staged",
                    "durable": False,
                    "replayed": True,
                    "candidateSha256": getattr(state, "candidate_sha256", None),
                    "candidateEpoch": getattr(state, "candidate_epoch", None),
                    "changeSetState": "draft",
                    "nextAction": "document_browser_inspect",
                }
            )

    max_bytes = scope.ctx.artifact_max_bytes
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        max_bytes = DEFAULT_ARTIFACT_MAX_BYTES
    disk_budget = scope.ctx.artifact_disk_budget_bytes
    if not isinstance(disk_budget, int) or isinstance(disk_budget, bool) or disk_budget <= 0:
        disk_budget = DEFAULT_ARTIFACT_DISK_BUDGET_BYTES

    previous_blob = getattr(controller, "candidate_artifact", None)
    candidate: ArtifactRef | None = None
    try:
        # Persist the turn-local DRAFT before publishing bytes.  If the process
        # dies after the blob is created but before the candidate CAS returns,
        # restart recovery can reject this empty/staged draft and use the
        # turn-scoped source marker to remove the orphan.  Publishing first
        # would leave an unjournaled internal bucket when draft creation itself
        # loses its response.
        ensure_draft = getattr(controller, "ensure_draft", None)
        if callable(ensure_draft):
            await ensure_draft(
                operations=prepared.operations,
                actor=prepared.actor,
                summary=prepared.summary,
            )
        candidate_id = prepared.store.allocate_artifact_id()
        candidate = await asyncio.to_thread(
            prepared.store.publish_bytes,
            prepared.candidate_bytes,
            session_id=scope.context.session_id,
            session_key=scope.context.session_key,
            name=prepared.ref.name,
            mime=prepared.ref.mime,
            source=_candidate_source(prepared.adapter_id, prepared.turn_id),
            max_bytes=max_bytes,
            disk_budget_bytes=disk_budget,
            visibility="internal",
            artifact_id=candidate_id,
        )
        candidate_validation = await asyncio.to_thread(
            _validated_payload,
            prepared.candidate_bytes,
            artifact_format=prepared.artifact_format,
            ref=candidate,
        )
        _staged = await controller.stage_candidate(
            candidate_artifact=ArtifactBlobRef(
                artifact_id=candidate.id,
                sha256=candidate.sha256,
                filename=candidate.name,
                media_type=candidate.mime,
                byte_size=candidate.size,
            ),
            operations=prepared.operations,
            actor=prepared.actor,
            validation={
                **candidate_validation,
                **prepared.validation_summary,
                "source_sha256": candidate.sha256,
                "status": "candidate_staged",
            },
            summary=prepared.summary,
            tool_use_id=tool_use_id,
            proposal_sha256=proposal_sha256,
        )
        # Another in-flight retry may have won the DRAFT CAS while this call
        # was publishing its private blob.  The controller returns the
        # already-durable ChangeSet in that case; never bind or advertise the
        # unreferenced blob created by the losing request.
        if (
            candidate is not None
            and _staged is not None
            and isinstance(tool_use_id, str)
            and isinstance(proposal_sha256, str)
            and getattr(_staged, "candidate_artifact_id", None) != candidate.id
        ):
            try:
                await asyncio.to_thread(
                    prepared.store.delete_ref,
                    session_id=scope.context.session_id,
                    artifact_id=candidate.id,
                )
            except Exception:  # noqa: BLE001 - orphan GC remains the safe fallback
                pass
            prepared.release_grants()
            state = getattr(controller, "state", None)
            return _json(
                {
                    "status": "candidate_staged",
                    "durable": False,
                    "replayed": True,
                    "candidateSha256": getattr(state, "candidate_sha256", None),
                    "candidateEpoch": getattr(state, "candidate_epoch", None),
                    "changeSetState": "draft",
                    "nextAction": "document_browser_inspect",
                }
            )
    except BaseException:
        # A draft CAS may have committed just before its response was lost.
        # Reconcile the turn-local controller before deciding whether the new
        # blob is safe to delete; deleting a blob still referenced by DRAFT
        # would strand a candidate that recovery can no longer verify.
        if candidate is not None:
            reconcile = getattr(controller, "reconcile", None)
            if callable(reconcile):
                try:
                    await asyncio.shield(reconcile())
                except BaseException:  # noqa: BLE001 - retain the original error
                    pass
            durable_candidate = getattr(controller, "candidate_artifact", None)
            candidate_is_referenced = bool(
                durable_candidate is not None
                and durable_candidate.artifact_id == candidate.id
                and durable_candidate.sha256 == candidate.sha256
            )
            if not candidate_is_referenced:
                try:
                    await asyncio.to_thread(
                        prepared.store.delete_ref,
                        session_id=scope.context.session_id,
                        artifact_id=candidate.id,
                    )
                except Exception:  # noqa: BLE001 - preserve the original failure
                    pass
        prepared.release_grants()
        raise

    # Grants describe the source bytes used by this proposal.  A new candidate
    # epoch must force fresh inspect/read/locate evidence on the next iteration.
    prepared.release_grants()
    clear_context_registry(scope.ctx)
    setattr(scope.ctx, "_artifact_browser_verification_token", None)
    setattr(scope.ctx, "_artifact_browser_verification_sha256", None)
    if previous_blob is not None and previous_blob.artifact_id != candidate.id:
        try:
            await asyncio.to_thread(
                prepared.store.delete_ref,
                session_id=scope.context.session_id,
                artifact_id=previous_blob.artifact_id,
            )
        except Exception:  # noqa: BLE001 - orphan GC remains the safe fallback
            pass
    state = getattr(controller, "state", None)
    # Bind only an opaque gateway-issued handle when a v4 bridge implements the
    # optional candidate-preview method.  Older clients remain source-only and
    # cannot claim a verified visual commit.
    bridge = getattr(scope.ctx, "desktop_artifact_bridge", None)
    preview_service = getattr(scope.ctx, "artifact_preview_service", None)
    bind = getattr(bridge, "bind_candidate_preview", None)
    preview_handle = getattr(controller, "preview_handle", None)
    previous_preview_bound = bool(
        getattr(scope.ctx, "_artifact_candidate_preview_bound", False)
        or getattr(scope.ctx, "_artifact_candidate_preview_cleanup_pending", False)
    )
    setattr(scope.ctx, "_artifact_candidate_preview_bound", False)
    register_preview = getattr(preview_service, "register_candidate_preview", None)
    preview_registered = False
    registration_attempted = callable(register_preview) and isinstance(preview_handle, str)
    # From this point onward a cancellation may leave either the Gateway
    # mapping or the native surface half-updated.  Keep cleanup eligible until
    # a successful canonical restore proves that both sides are detached.
    setattr(scope.ctx, "_artifact_candidate_preview_registration_attempted", registration_attempted)
    setattr(
        scope.ctx,
        "_artifact_candidate_preview_cleanup_pending",
        previous_preview_bound or (registration_attempted and callable(bind)),
    )
    if callable(register_preview) and isinstance(preview_handle, str):
        try:
            register_preview(
                handle=preview_handle,
                artifact_id=candidate.id,
                session_id=scope.context.session_id,
                session_key=scope.context.session_key,
            )
            preview_registered = True
        except asyncio.CancelledError:
            # Registration is synchronous today, but preserve the same
            # fail-closed cleanup contract if the preview service becomes
            # cancellable in a future implementation.
            setattr(scope.ctx, "_artifact_candidate_preview_cleanup_pending", True)
            raise
        except Exception:  # noqa: BLE001 - source candidate remains recoverable
            # Never let a stale mapping for the same opaque handle be reused for
            # this newer candidate.  Source staging remains recoverable, but a
            # visual bind is forbidden until the Gateway can register the exact
            # candidate bytes.
            retire_preview = getattr(preview_service, "retire_candidate_preview", None)
            if callable(retire_preview) and isinstance(preview_handle, str):
                try:
                    retire_preview(preview_handle)
                except Exception:  # noqa: BLE001 - bounded in-memory cleanup
                    pass
            preview_service = None
    if callable(bind) and preview_registered and isinstance(preview_handle, str):
        try:
            # The bridge client starts in v3 for rolling compatibility and
            # upgrades to v4 after its capability handshake.  Candidate
            # preview binding is a v4-only operation, so perform that
            # handshake before invoking the method instead of treating a
            # still-unnegotiated client as an old Electron shell.
            capabilities = getattr(bridge, "capabilities", None)
            if callable(capabilities):
                await capabilities()
            await bind(preview_handle)
            setattr(scope.ctx, "_artifact_candidate_preview_bound", True)
            setattr(scope.ctx, "_artifact_candidate_preview_cleanup_pending", False)
        except asyncio.CancelledError:
            # Do not retire an opaque handle while bind may have reached the
            # renderer.  The turn-finalizer will restore canonical preview and
            # release the mapping under a shielded cleanup task.
            setattr(scope.ctx, "_artifact_candidate_preview_cleanup_pending", True)
            raise
        except Exception:  # noqa: BLE001 - preserve cleanup state on bridge faults
            setattr(scope.ctx, "_artifact_candidate_preview_cleanup_pending", True)
            return _json(
                {
                    "status": "candidate_staged",
                    "candidateSha256": candidate.sha256,
                    "candidateEpoch": getattr(state, "candidate_epoch", None),
                    "preview": "unavailable",
                    "warning": (
                        "The candidate is staged but the bound preview could not be updated."
                    ),
                    "nextAction": "document_finish_discard",
                }
            )
    elif callable(bind) and not preview_registered:
        setattr(scope.ctx, "_artifact_candidate_preview_cleanup_pending", previous_preview_bound)
        return _json(
            {
                "status": "candidate_staged",
                "candidateSha256": candidate.sha256,
                "candidateEpoch": getattr(state, "candidate_epoch", None),
                "preview": "unavailable",
                "warning": (
                    "The candidate is staged but its opaque preview handle could not be registered."
                ),
                "nextAction": "document_finish_discard",
            }
        )
    elif not callable(bind):
        # Without a v4 bridge (whether or not registration was available), a
        # candidate can never produce the verification receipt required by
        # document_finish(commit).  Retire any registered mapping and avoid
        # sending the model into an inspect/unavailable retry loop.
        if preview_registered:
            retire_preview = getattr(preview_service, "retire_candidate_preview", None)
            if callable(retire_preview):
                try:
                    retire_preview(preview_handle)
                except Exception:  # noqa: BLE001 - bounded in-memory cleanup
                    pass
        setattr(scope.ctx, "_artifact_candidate_preview_cleanup_pending", False)
        return _json(
            {
                "status": "candidate_staged",
                "candidateSha256": candidate.sha256,
                "candidateEpoch": getattr(state, "candidate_epoch", None),
                "preview": "unavailable",
                "warning": (
                    "The candidate is staged but no compatible bound preview bridge is available."
                ),
                "nextAction": "document_finish_discard",
            }
        )
    return _json(
        {
            "status": "candidate_staged",
            "durable": False,
            "candidateSha256": candidate.sha256,
            "candidateEpoch": getattr(state, "candidate_epoch", None),
            "changeSetState": "draft",
            "nextAction": "document_browser_inspect",
        }
    )


async def _require_single_file_html(
    scope: _ArtifactScope,
    store: ArtifactStore,
    ref: ArtifactRef,
) -> None:
    """Fail closed before a source tool can discard a preview bundle sidecar."""

    try:
        manifest = await asyncio.to_thread(
            store.describe_preview_bundle,
            ref.id,
            session_id=scope.context.session_id,
        )
    except (ArtifactError, OSError, ValueError):
        raise SafeToolError(
            "The current HTML preview bundle failed integrity validation. "
            "Reopen or regenerate it."
        ) from None
    if manifest is not None and not is_complete_single_file_preview_bundle(manifest):
        raise SafeToolError(
            "Agent HTML source tools do not support multi-file preview bundles yet. "
            "Partial or warning-bearing bundles are also unsupported. "
            "The bundle and its sidecar files were left unchanged."
        )


def _decode_html(payload: bytes) -> str:
    if len(payload) > _MAX_HTML_SOURCE_BYTES:
        raise SafeToolError(
            "The HTML source is too large for an agent source-editing turn. "
            "Split the deliverable into smaller source files first."
        )
    try:
        source = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise SafeToolError("HTML source editing requires UTF-8 content.") from None
    if not source:
        raise SafeToolError("The HTML artifact is empty.")
    return source


def _bounded_anchor_value(value: object, field: str) -> object:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise SafeToolError(
            f"The selected artifact {field} is not valid structured data."
        ) from None
    if len(encoded) > _MAX_ANCHOR_JSON_BYTES:
        raise SafeToolError(f"The selected artifact {field} is too large to return safely.")
    return value


def _bounded_anchor_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if len(value.encode("utf-8")) > _MAX_ANCHOR_JSON_BYTES:
        raise SafeToolError(f"The selected artifact {field} is too large to return safely.")
    return value


class _HtmlStructureParser(HTMLParser):
    """Extract a bounded semantic outline without executing page content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tag_counts: Counter[str] = Counter()
        self.headings: list[dict[str, object]] = []
        self.title_parts: list[str] = []
        self._capture_title = 0
        self._heading: tuple[int, list[str]] | None = None
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        name = tag.lower()
        self.tag_counts[name] += 1
        if name in {"script", "style", "template"}:
            self._ignored_depth += 1
        if name == "title":
            self._capture_title += 1
        if name in {"h1", "h2", "h3", "h4", "h5", "h6"} and len(self.headings) < 100:
            self._heading = (int(name[1]), [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        if name == "title" and self._capture_title:
            self._capture_title -= 1
        if self._heading is not None and name == f"h{self._heading[0]}":
            text = " ".join("".join(self._heading[1]).split())[:500]
            self.headings.append({"level": self._heading[0], "text": text})
            self._heading = None
        if name in {"script", "style", "template"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._capture_title and sum(len(item) for item in self.title_parts) < 500:
            self.title_parts.append(data)
        if self._heading is not None and sum(len(item) for item in self._heading[1]) < 500:
            self._heading[1].append(data)

    def payload(self) -> dict[str, object]:
        title = " ".join("".join(self.title_parts).split())[:500]
        return {
            "title": title or None,
            "headings": self.headings,
            "element_counts": dict(self.tag_counts.most_common(100)),
            "links": self.tag_counts["a"],
            "forms": self.tag_counts["form"],
            "interactive_elements": sum(
                self.tag_counts[name]
                for name in ("a", "button", "input", "select", "textarea", "details")
            ),
        }


def _html_structure(source: str) -> dict[str, object]:
    parser = _HtmlStructureParser()
    try:
        parser.feed(source)
        parser.close()
    except (TypeError, ValueError):
        raise SafeToolError("The HTML source could not be parsed safely.") from None
    return parser.payload()


@dataclass(frozen=True, slots=True)
class _SourceRange:
    start: int
    end: int
    kind: str
    annotation_orders: tuple[int, ...]
    confidence: str = "exact"
    detail: str | None = None


class _ElementCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append(
            (
                tag.lower(),
                {name.lower(): value or "" for name, value in attrs},
            )
        )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _candidate_epoch(scope: _ArtifactScope) -> int:
    """Return the current draft epoch for grant/result binding.

    Ordinary document turns use epoch zero.  PromptAnnotation candidate turns
    advance this value whenever a writer replaces the draft, so source cursors
    and semantic grants are cryptographically scoped to the bytes they saw.
    """

    controller = getattr(scope.ctx, "artifact_candidate_loop_controller", None)
    value = getattr(controller, "candidate_epoch", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _range_binding(
    scope: _ArtifactScope,
    source_sha256: str,
    *,
    adapter_id: str = "html",
    adapter_version: int = 1,
) -> ArtifactRangeBinding:
    task_id = str(scope.ctx.task_id or "").strip()
    if not task_id:
        raise SafeToolError("Artifact source ranges require a durable turn identity.")
    return ArtifactRangeBinding(
        task_id=task_id,
        session_key=scope.context.session_key,
        session_id=scope.context.session_id,
        session_epoch=scope.session_epoch,
        document_id=scope.document.document_id,
        revision_id=scope.revision.revision_id,
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        candidate_epoch=_candidate_epoch(scope),
    )


def _document_grant_binding(
    scope: _ArtifactScope,
    source_sha256: str,
    *,
    adapter_id: str,
    adapter_version: int,
) -> DocumentGrantBinding:
    task_id = str(scope.ctx.task_id or "").strip()
    if not task_id:
        raise SafeToolError("Document mutation grants require a durable turn identity.")
    return DocumentGrantBinding(
        task_id=task_id,
        session_key=scope.context.session_key,
        session_id=scope.context.session_id,
        session_epoch=scope.session_epoch,
        document_id=scope.document.document_id,
        revision_id=scope.revision.revision_id,
        source_sha256=source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        candidate_epoch=_candidate_epoch(scope),
    )


def _range_error(exc: ArtifactRangeGrantError) -> DocumentMutationError:
    retry_policy: DocumentMutationRetryPolicy
    if exc.code in {
        "ARTIFACT_RANGE_LIMIT",
        "ARTIFACT_RANGE_QUERY_LIMIT",
        "ARTIFACT_RANGE_TOKEN_INVALID",
    }:
        retry_policy = "forbidden"
    elif exc.code in {
        "ARTIFACT_CURSOR_INVALID",
        "ARTIFACT_RANGE_STALE",
        "ARTIFACT_RANGE_TOKEN_USED",
    }:
        retry_policy = "refresh"
    else:
        retry_policy = "correctable"
    return DocumentMutationError(
        exc.code,
        exc.user_message,
        retry_policy=retry_policy,
    )


def _consume_range_query(scope: _ArtifactScope, *, query_key: str | None = None) -> int:
    try:
        return registry_for_context(scope.ctx).consume_query_budget(query_key=query_key)
    except ArtifactRangeGrantError as exc:
        raise _range_error(exc) from None


def _anchor_opening_range(anchor: Anchor, source: str, source_sha256: str) -> _SourceRange:
    locator = anchor.locator
    if not isinstance(locator, dict):
        raise SafeToolError("The selected HTML element is not source-backed.")
    start = locator.get("start_offset")
    end = locator.get("start_tag_end_offset", locator.get("end_offset"))
    locator_sha = locator.get("source_sha256")
    tag_name = locator.get("tag_name")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
        or start < 0
        or end <= start
        or end > len(source)
        or not isinstance(locator_sha, str)
        or locator_sha.lower() != source_sha256
        or not isinstance(tag_name, str)
    ):
        raise SafeToolError("The selected HTML element no longer matches the current source.")
    opening = source[start:end]
    if not re.match(rf"(?is)^\s*<{re.escape(tag_name)}(?:\s|/?>)", opening):
        raise SafeToolError("The selected HTML opening tag could not be verified.")
    return _SourceRange(start, end, "opening_tag", ())


def _parse_opening_element(opening: str) -> tuple[str, dict[str, str]] | None:
    parser = _ElementCollector()
    try:
        parser.feed(opening)
        parser.close()
    except (TypeError, ValueError):
        return None
    return parser.elements[0] if len(parser.elements) == 1 else None


_OPTIONAL_END_TAGS = frozenset(
    {
        "colgroup",
        "dd",
        "dt",
        "li",
        "optgroup",
        "option",
        "p",
        "rp",
        "rt",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
    }
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _ExplicitElementBoundaryParser(HTMLParser):
    """Find one explicit balanced element without interpreting raw text/comments as tags."""

    def __init__(self, source: str, opening: _SourceRange, tag_name: str) -> None:
        super().__init__(convert_charrefs=True)
        self._source = source
        self._opening = opening
        self._tag_name = tag_name
        self._line_starts = [0]
        for match in re.finditer(r"\n", source):
            self._line_starts.append(match.end())
        self._active = False
        self._depth = 0
        self.opening_verified = False
        self.close_span: tuple[int, int] | None = None

    def _offset(self) -> int:
        line, column = self.getpos()
        if line < 1 or line > len(self._line_starts):
            return -1
        return self._line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        offset = self._offset()
        raw = self.get_starttag_text() or ""
        if not self._active:
            if (
                offset == self._opening.start
                and offset + len(raw) == self._opening.end
                and raw == self._source[self._opening.start : self._opening.end]
                and tag.lower() == self._tag_name
            ):
                self._active = True
                self._depth = 1
                self.opening_verified = True
            return
        if tag.lower() == self._tag_name:
            self._depth += 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        del tag, attrs
        # A nested self-closing tag never changes the selected element depth.

    def handle_endtag(self, tag: str) -> None:
        if not self._active or tag.lower() != self._tag_name:
            return
        self._depth -= 1
        if self._depth != 0:
            return
        offset = self._offset()
        match = re.match(
            rf"(?is)</{re.escape(self._tag_name)}\s*>",
            self._source[offset:],
        )
        if match is not None:
            self.close_span = (offset, offset + match.end())
        self._active = False


def _element_ranges(
    source: str,
    opening: _SourceRange,
    *,
    annotation_order: int,
) -> list[_SourceRange]:
    parsed = _parse_opening_element(source[opening.start : opening.end])
    if parsed is None:
        return []
    tag_name, _attrs = parsed
    if tag_name in {"script", "style", "template"}:
        return []
    if tag_name in _VOID_TAGS or tag_name in _OPTIONAL_END_TAGS:
        return []
    if source[max(opening.start, opening.end - 2) : opening.end].rstrip().endswith("/>"):
        return []
    parser = _ExplicitElementBoundaryParser(source, opening, tag_name)
    try:
        parser.feed(source)
        parser.close()
    except (TypeError, ValueError):
        return []
    if not parser.opening_verified or parser.close_span is None:
        return []
    close_start, close_end = parser.close_span
    if re.search(
        r"(?is)<\s*(?:iframe|noembed|noframes|plaintext|template|textarea|title|xmp)\b",
        source[opening.end:close_start],
    ):
        return []
    ranges: list[_SourceRange] = []
    inner = source[opening.end:close_start]
    if inner and "<" not in inner and len(inner.encode("utf-8")) <= _MAX_HTML_RANGE_TEXT_BYTES:
        ranges.append(
            _SourceRange(
                opening.end,
                close_start,
                "text_content",
                (annotation_order,),
            )
        )
    if (
        close_end - opening.start > 0
        and len(source[opening.start:close_end].encode("utf-8"))
        <= _MAX_HTML_RANGE_TEXT_BYTES
    ):
        ranges.append(
            _SourceRange(
                opening.start,
                close_end,
                "element_source",
                (annotation_order,),
            )
        )
    return ranges


def _attribute_range(
    source: str,
    opening: _SourceRange,
    *,
    annotation_order: int,
    attribute_name: str,
) -> _SourceRange | None:
    opening_text = source[opening.start : opening.end]
    match = re.search(
        rf"(?is)(?:^|\s){re.escape(attribute_name)}\s*=\s*(['\"])(.*?)\1",
        opening_text,
    )
    if match is None:
        return None
    start = opening.start + match.start(2)
    end = opening.start + match.end(2)
    if end <= start:
        return None
    return _SourceRange(
        start,
        end,
        f"attribute:{attribute_name.lower()}",
        (annotation_order,),
    )


def _style_blocks(source: str) -> list[tuple[int, int]]:
    return [
        match.span(1)
        for match in re.finditer(r"(?is)<style\b[^>]*>(.*?)</style\s*>", source)
    ]


def _script_blocks(source: str) -> list[tuple[int, int]]:
    return [
        match.span(1)
        for match in re.finditer(r"(?is)<script\b[^>]*>(.*?)</script\s*>", source)
    ]


def _opening_tag_spans(source: str) -> list[tuple[int, int]]:
    """Return quote-aware HTML start-tag spans for conservative search fencing."""

    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(source):
        start = source.find("<", index)
        if start < 0 or start + 1 >= len(source):
            break
        first = source[start + 1]
        if not first.isalpha():
            index = start + 1
            continue
        cursor = start + 2
        quote: str | None = None
        while cursor < len(source):
            char = source[cursor]
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == ">":
                spans.append((start, cursor + 1))
                cursor += 1
                break
            cursor += 1
        index = max(start + 1, cursor)
    return spans


def _css_rules(source: str) -> list[tuple[int, int, str]]:
    """Return conservative top-level CSS rules from inline style blocks."""

    rules: list[tuple[int, int, str]] = []
    for block_start, block_end in _style_blocks(source):
        index = block_start
        selector_start = block_start
        quote: str | None = None
        comment = False
        while index < block_end:
            char = source[index]
            next_char = source[index + 1] if index + 1 < block_end else ""
            if comment:
                if char == "*" and next_char == "/":
                    comment = False
                    index += 2
                    continue
                index += 1
                continue
            if quote is not None:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = None
                index += 1
                continue
            if char == "/" and next_char == "*":
                comment = True
                index += 2
                continue
            if char in {"'", '"'}:
                quote = char
                index += 1
                continue
            if char != "{":
                index += 1
                continue
            selector = source[selector_start:index].strip()
            depth = 1
            body_index = index + 1
            body_quote: str | None = None
            body_comment = False
            while body_index < block_end and depth:
                body_char = source[body_index]
                body_next = source[body_index + 1] if body_index + 1 < block_end else ""
                if body_comment:
                    if body_char == "*" and body_next == "/":
                        body_comment = False
                        body_index += 2
                        continue
                    body_index += 1
                    continue
                if body_quote is not None:
                    if body_char == "\\":
                        body_index += 2
                        continue
                    if body_char == body_quote:
                        body_quote = None
                    body_index += 1
                    continue
                if body_char == "/" and body_next == "*":
                    body_comment = True
                    body_index += 2
                    continue
                if body_char in {"'", '"'}:
                    body_quote = body_char
                elif body_char == "{":
                    depth += 1
                elif body_char == "}":
                    depth -= 1
                body_index += 1
            if depth:
                break
            raw_start = selector_start
            while raw_start < index and source[raw_start].isspace():
                raw_start += 1
            if selector and not selector.startswith("@"):
                rules.append((raw_start, body_index, selector))
            selector_start = body_index
            index = body_index
    return rules


_SIMPLE_SELECTOR_RE = re.compile(
    r"^(?P<tag>[A-Za-z][A-Za-z0-9-]*)?(?P<id>#[A-Za-z_][\w-]*)?"
    r"(?P<classes>(?:\.[A-Za-z_][\w-]*)*)$"
)


def _selector_matches(
    selector: str,
    element: tuple[str, dict[str, str]],
) -> bool:
    match = _SIMPLE_SELECTOR_RE.fullmatch(selector.strip())
    if match is None or not any(match.groups()):
        return False
    tag_name, attrs = element
    tag = match.group("tag")
    expected_id = match.group("id")
    expected_classes = {
        value for value in match.group("classes").split(".") if value
    }
    actual_classes = set(attrs.get("class", "").split())
    return (
        (tag is None or tag.lower() == tag_name)
        and (expected_id is None or attrs.get("id") == expected_id[1:])
        and expected_classes <= actual_classes
    )


def _related_css_ranges(
    source: str,
    opening: _SourceRange,
    *,
    annotation_order: int,
) -> list[_SourceRange]:
    selected = _parse_opening_element(source[opening.start : opening.end])
    if selected is None:
        return []
    collector = _ElementCollector()
    try:
        collector.feed(source)
        collector.close()
    except (TypeError, ValueError):
        return []
    ranges: list[_SourceRange] = []
    for start, end, selector in _css_rules(source):
        if "," in selector or not _selector_matches(selector, selected):
            continue
        matches = [
            element for element in collector.elements if _selector_matches(selector, element)
        ]
        if len(matches) != 1 or len(source[start:end].encode("utf-8")) > _MAX_HTML_RANGE_TEXT_BYTES:
            continue
        ranges.append(
            _SourceRange(
                start,
                end,
                "related_css_rule",
                (annotation_order,),
                confidence="high",
                detail=selector.strip(),
            )
        )
    return ranges


def _grant_payload(
    scope: _ArtifactScope,
    source: str,
    source_sha256: str,
    value: _SourceRange,
) -> dict[str, object]:
    registry = registry_for_context(scope.ctx)
    try:
        token = registry.mint_range(
            binding=_range_binding(scope, source_sha256),
            source=source,
            start=value.start,
            end=value.end,
            kind=value.kind,
            annotation_orders=value.annotation_orders,
        )
    except ArtifactRangeGrantError as exc:
        raise _range_error(exc) from None
    text = source[value.start : value.end]
    return {
        "rangeToken": token,
        "kind": value.kind,
        "text": text,
        "before": source[max(0, value.start - _MAX_HTML_CONTEXT_CHARS) : value.start],
        "after": source[value.end : value.end + _MAX_HTML_CONTEXT_CHARS],
        "confidence": value.confidence,
        "detail": value.detail,
    }


def _range_preview_payload(
    source: str,
    value: _SourceRange,
) -> dict[str, object]:
    """Project exact source context without creating edit authority."""

    return {
        "editable": False,
        "kind": value.kind,
        "text": source[value.start : value.end],
        "before": source[max(0, value.start - _MAX_HTML_CONTEXT_CHARS) : value.start],
        "after": source[value.end : value.end + _MAX_HTML_CONTEXT_CHARS],
        "confidence": value.confidence,
        "detail": value.detail,
    }


def _annotation_order(value: object, count: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= count:
        raise SafeToolError("ARTIFACT_ANNOTATION_ORDER_INVALID: Annotation order is invalid.")
    return value


_UNSAFE_SCRIPT_SURFACE_RE = re.compile(
    r"(?is)<\s*/?\s*script\b|\bon[a-z0-9_-]+\s*=|javascript\s*:"
)


def _valid_css_declaration_list(value: str) -> bool:
    """Conservatively accept a complete CSS declaration list.

    This deliberately supports less than the full CSS grammar.  A grant is an
    edit authority, so an unfamiliar construct must fail closed instead of
    being handed to the browser as an unvalidated style mutation.
    """

    if not value.strip() or any(char in value for char in "{}<>"):
        return False
    quote: str | None = None
    parentheses = 0
    brackets = 0
    escaped = False
    comment = False
    segment_start = 0
    segments: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        next_char = value[index + 1] if index + 1 < len(value) else ""
        if comment:
            if char == "*" and next_char == "/":
                comment = False
                index += 2
                continue
            index += 1
            continue
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and next_char == "*":
            comment = True
            index += 2
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            parentheses += 1
        elif char == ")":
            parentheses -= 1
            if parentheses < 0:
                return False
        elif char == "[":
            brackets += 1
        elif char == "]":
            brackets -= 1
            if brackets < 0:
                return False
        elif char == ";" and parentheses == 0 and brackets == 0:
            segments.append(value[segment_start:index])
            segment_start = index + 1
        index += 1
    if quote is not None or parentheses or brackets or escaped or comment:
        return False
    segments.append(value[segment_start:])
    declarations = 0
    property_name_re = re.compile(r"^(?:--[A-Za-z0-9_-]+|-?[A-Za-z_][A-Za-z0-9_-]*)$")
    for segment in segments:
        cleaned = re.sub(r"(?s)/\*.*?\*/", "", segment).strip()
        if not cleaned:
            continue
        property_name, separator, property_value = cleaned.partition(":")
        if (
            not separator
            or property_name_re.fullmatch(property_name.strip()) is None
            or not property_value.strip()
        ):
            return False
        declarations += 1
    return declarations > 0


def _opening_without_style(opening: str) -> tuple[str, str | None] | None:
    """Remove one style attribute while preserving every other source byte."""

    tag = re.match(r"(?is)<[A-Za-z][A-Za-z0-9:-]*", opening)
    if tag is None:
        return None
    cursor = tag.end()
    style_span: tuple[int, int, str] | None = None
    while cursor < len(opening):
        whitespace_start = cursor
        while cursor < len(opening) and opening[cursor].isspace():
            cursor += 1
        if opening.startswith("/>", cursor):
            cursor += 2
            break
        if cursor < len(opening) and opening[cursor] == ">":
            cursor += 1
            break
        if cursor == whitespace_start:
            return None
        name_start = cursor
        while (
            cursor < len(opening)
            and not opening[cursor].isspace()
            and opening[cursor] not in "=/><"
        ):
            cursor += 1
        if cursor == name_start:
            return None
        name = opening[name_start:cursor].lower()
        while cursor < len(opening) and opening[cursor].isspace():
            cursor += 1
        value: str | None = None
        quoted = False
        if cursor < len(opening) and opening[cursor] == "=":
            cursor += 1
            while cursor < len(opening) and opening[cursor].isspace():
                cursor += 1
            if cursor >= len(opening):
                return None
            if opening[cursor] in {"'", '"'}:
                quoted = True
                quote = opening[cursor]
                cursor += 1
                value_start = cursor
                cursor = opening.find(quote, cursor)
                if cursor < 0:
                    return None
                value = opening[value_start:cursor]
                cursor += 1
            else:
                value_start = cursor
                while (
                    cursor < len(opening)
                    and not opening[cursor].isspace()
                    and opening[cursor] not in "><"
                ):
                    cursor += 1
                if cursor == value_start:
                    return None
                value = opening[value_start:cursor]
        if name == "style":
            if style_span is not None or value is None or not quoted:
                return None
            style_span = (whitespace_start, cursor, value)
    if cursor != len(opening):
        return None
    if style_span is None:
        return opening, None
    start, end, value = style_span
    return opening[:start] + opening[end:], value


def _validate_grant_replacement(
    *,
    source: str,
    grant: ResolvedRangeGrant,
    action: str,
    text: str,
    prompt_annotation: bool = False,
) -> None:
    """Conservatively validate edits whose grant carries a structural promise."""

    if _UNSAFE_SCRIPT_SURFACE_RE.search(text):
        raise SafeToolError(
            "ARTIFACT_SCRIPT_EDIT_UNSUPPORTED: Script and event-handler edits require "
            "a validated JavaScript parser."
        )
    original = source[grant.start : grant.end]
    if prompt_annotation and grant.kind == "element_source":
        raise SafeToolError(
            "ARTIFACT_ELEMENT_EDIT_UNSUPPORTED: Prompt annotations cannot replace an "
            "entire element source range. Use text, opening-tag, attribute, or related-CSS "
            "ranges."
        )
    if grant.kind == "text_content" and (
        "<" in text
        or re.search(
            r"(?i)&(?:#(?:x[0-9a-f]+|[0-9]+);?|[a-z][a-z0-9]{1,31};?)",
            text,
        )
    ):
        raise SafeToolError(
            "ARTIFACT_TEXT_EDIT_INVALID: Text-content ranges accept literal plain text only; "
            "HTML tags and character references are not allowed."
        )
    if grant.kind == "opening_tag" and action != "replace":
        raise SafeToolError(
            "ARTIFACT_OPENING_TAG_INVALID: Opening-tag ranges only support replacement."
        )
    if grant.kind.startswith("attribute:"):
        if action != "replace":
            raise SafeToolError(
                "ARTIFACT_ATTRIBUTE_EDIT_INVALID: Attribute ranges only support replacement."
            )
        attribute_name = grant.kind.partition(":")[2]
        if any(char in text for char in "<>'\""):
            raise SafeToolError(
                "ARTIFACT_ATTRIBUTE_EDIT_INVALID: Attribute value is not source-safe."
            )
        if attribute_name == "id" and not re.fullmatch(r"[A-Za-z_][\w:.-]*", text):
            raise SafeToolError("ARTIFACT_ATTRIBUTE_EDIT_INVALID: id value is invalid.")
        if attribute_name == "class" and not all(
            re.fullmatch(r"[A-Za-z_][\w:-]*", token) for token in text.split()
        ):
            raise SafeToolError("ARTIFACT_ATTRIBUTE_EDIT_INVALID: class value is invalid.")
        if attribute_name == "style" and not _valid_css_declaration_list(text):
            raise SafeToolError(
                "ARTIFACT_CSS_EDIT_INVALID: Inline style is not a validated declaration list."
            )
    elif grant.kind == "related_css_rule":
        if action != "replace":
            raise SafeToolError(
                "ARTIFACT_CSS_EDIT_INVALID: Related CSS rules only support replacement."
            )
        original_selector = original.split("{", 1)[0].strip()
        wrapped = f"<style>{text}</style>"
        candidate_rules = _css_rules(wrapped)
        rule_body_start = text.find("{")
        rule_body_end = text.rfind("}")
        if (
            len(candidate_rules) != 1
            or candidate_rules[0][2].strip() != original_selector
            or rule_body_start < 0
            or rule_body_end <= rule_body_start
            or text[rule_body_end + 1 :].strip()
            or not _valid_css_declaration_list(
                text[rule_body_start + 1 : rule_body_end]
            )
            or "<" in text
            or ">" in text
        ):
            raise SafeToolError(
                "ARTIFACT_CSS_EDIT_INVALID: Replacement must preserve one verified selector."
            )
    elif grant.kind == "opening_tag" and action == "replace":
        original_element = _parse_opening_element(original)
        candidate_element = _parse_opening_element(text)
        original_without_style = _opening_without_style(original)
        candidate_without_style = _opening_without_style(text)
        if (
            original_element is None
            or candidate_element is None
            or original_element[0] != candidate_element[0]
            or original_without_style is None
            or candidate_without_style is None
            or original_without_style[0] != candidate_without_style[0]
        ):
            raise SafeToolError(
                "ARTIFACT_OPENING_TAG_INVALID: Replacement must preserve the selected tag "
                "and every existing non-style attribute byte."
            )
        candidate_attrs = candidate_element[1]
        candidate_id = candidate_attrs.get("id")
        if candidate_id is not None and not re.fullmatch(
            r"[A-Za-z_][\w:.-]*", candidate_id
        ):
            raise SafeToolError("ARTIFACT_OPENING_TAG_INVALID: id value is invalid.")
        candidate_classes = candidate_attrs.get("class")
        if candidate_classes is not None and not all(
            re.fullmatch(r"[A-Za-z_][\w:-]*", token)
            for token in candidate_classes.split()
        ):
            raise SafeToolError("ARTIFACT_OPENING_TAG_INVALID: class value is invalid.")
        candidate_style = candidate_attrs.get("style")
        if candidate_style is not None and not _valid_css_declaration_list(
            candidate_style
        ):
            raise SafeToolError(
                "ARTIFACT_CSS_EDIT_INVALID: Inline style is not a validated declaration list."
            )
    elif re.search(r"(?is)<\s*/?\s*style\b|\bstyle\s*=", text):
        raise SafeToolError(
            "ARTIFACT_CSS_EDIT_UNSUPPORTED: CSS changes require a verified style or "
            "related-rule range."
        )


def _assert_source_preserving_splice(
    source: str,
    updated: str,
    patches: list[dict[str, object]],
) -> None:
    def position(item: dict[str, object], field: str) -> int:
        value = item[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise SafeToolError(
                "ARTIFACT_SPLICE_INVARIANT_FAILED: Source range is not an integer."
            )
        return value

    ordered = sorted(patches, key=lambda item: position(item, "start_offset"))
    parts: list[str] = []
    cursor = 0
    for patch in ordered:
        start = position(patch, "start_offset")
        end = position(patch, "end_offset")
        replacement = patch["replacement"]
        assert isinstance(replacement, str)
        parts.extend((source[cursor:start], replacement))
        cursor = end
    parts.append(source[cursor:])
    if "".join(parts) != updated:
        raise SafeToolError(
            "ARTIFACT_SPLICE_INVARIANT_FAILED: Unselected source bytes would change."
        )


def _take_utf8_chunk(source: str, start: int, max_chars: int) -> tuple[str, int]:
    """Return at most ``max_chars`` and at most 16 KiB of UTF-8 source."""

    end = min(len(source), start + max_chars)
    while end > start and len(source[start:end].encode("utf-8")) > 16 * 1024:
        end -= max(1, (end - start) // 8)
    while end < len(source):
        candidate = source[start : end + 1]
        if len(candidate) > max_chars or len(candidate.encode("utf-8")) > 16 * 1024:
            break
        end += 1
    return source[start:end], end


def _validate_ooxml_envelope(payload: bytes, artifact_format: str) -> dict[str, object]:
    required_member = {
        "docx": "word/document.xml",
        "xlsx": "xl/workbook.xml",
    }[artifact_format]
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            infos = archive.infolist()
    except (OSError, ValueError, zipfile.BadZipFile):
        raise SafeToolError(
            f"The {artifact_format.upper()} artifact is not a readable OOXML package."
        ) from None
    if not infos or len(infos) > _MAX_OOXML_MEMBERS:
        raise SafeToolError(
            f"The {artifact_format.upper()} package structure is unsafe or incomplete."
        )
    total_size = 0
    names: set[str] = set()
    for info in infos:
        normalized = info.filename.replace("\\", "/")
        pure = PurePosixPath(normalized)
        if (
            not normalized
            or normalized.startswith("/")
            or ".." in pure.parts
            or info.flag_bits & 0x1
            or info.file_size < 0
        ):
            raise SafeToolError(f"The {artifact_format.upper()} package contains unsafe members.")
        total_size += info.file_size
        if total_size > _MAX_OOXML_INFLATED_BYTES:
            raise SafeToolError(
                f"The expanded {artifact_format.upper()} package is too large to validate safely."
            )
        names.add(normalized)
    if "[Content_Types].xml" not in names or required_member not in names:
        raise SafeToolError(
            f"The {artifact_format.upper()} package is missing required OOXML parts."
        )
    return {
        "level": "package_envelope",
        "members": len(infos),
        "expanded_bytes": total_size,
    }


def _validated_payload(
    payload: bytes,
    *,
    artifact_format: str,
    ref: ArtifactRef,
) -> dict[str, object]:
    if artifact_format == "html":
        source = _decode_html(payload)
        structure = _html_structure(source)
        element_counts = structure.get("element_counts")
        elements = (
            sum(
                value
                for value in element_counts.values()
                if isinstance(value, int) and not isinstance(value, bool)
            )
            if isinstance(element_counts, dict)
            else 0
        )
        return {
            "level": "utf8_html_scan_and_storage_integrity",
            "encoding": "utf-8",
            "elements": elements,
            "html_structure_scan": "completed",
            "css_validation": "not_performed",
            "visual_validation": "not_performed",
        }
    if artifact_format in {"docx", "xlsx"}:
        return _validate_ooxml_envelope(payload, artifact_format)
    if artifact_format == "pptx":
        try:
            report = validate_artifact_for_delivery(
                payload,
                source_name=ref.name,
                name=ref.name,
                mime=ref.mime,
                source="document_apply",
            )
        except DeliveryValidationError as exc:
            raise SafeToolError(exc.user_message) from None
        return {
            "level": "format_and_storage_integrity",
            "warnings": list(report.warnings),
        }
    return {"level": "storage_integrity"}


def _validate_patches(
    source: str,
    patches: object,
    *,
    offset_encoding: object,
) -> tuple[str, list[dict[str, object]]]:
    if offset_encoding != _HTML_OFFSET_ENCODING:
        raise RetryableToolInputError(
            "offset_encoding must be unicode-code-point exactly as returned by "
            "document_read."
        )
    if not isinstance(patches, list) or not patches:
        raise RetryableToolInputError("patches must be a non-empty array.")
    if len(patches) > _MAX_HTML_PATCHES:
        raise RetryableToolInputError(
            f"patches supports at most {_MAX_HTML_PATCHES} edits per call."
        )

    normalized: list[tuple[int, int, str, str, int]] = []
    expected_bytes = 0
    replacement_bytes = 0
    expected_keys = {"start_offset", "end_offset", "expected_text", "replacement"}
    for index, item in enumerate(patches):
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise RetryableToolInputError(
                f"patches[{index}] must contain only start_offset, end_offset, "
                "expected_text, and replacement."
            )
        start = item["start_offset"]
        end = item["end_offset"]
        expected_text = item["expected_text"]
        replacement = item["replacement"]
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not isinstance(expected_text, str)
            or not isinstance(replacement, str)
        ):
            raise RetryableToolInputError(f"patches[{index}] has invalid value types.")
        if start < 0 or end <= start or end > len(source):
            raise RetryableToolInputError(f"patches[{index}] is outside the current source range.")
        if not expected_text:
            raise RetryableToolInputError(
                f"patches[{index}] must replace a non-empty current-source range. "
                "For an insertion, include an adjacent stable source fragment in both "
                "expected_text and replacement."
            )
        expected_bytes += len(expected_text.encode("utf-8"))
        if expected_bytes > _MAX_HTML_EXPECTED_BYTES:
            raise RetryableToolInputError(
                "The expected source text is too large for one atomic HTML edit."
            )
        if source[start:end] != expected_text:
            raise RetryableToolInputError(
                f"patches[{index}] expected_text does not match the current source at the "
                "declared Unicode code-point range. Read document_read again and copy "
                "the exact source slice."
            )
        replacement_bytes += len(replacement.encode("utf-8"))
        if replacement_bytes > _MAX_HTML_REPLACEMENT_BYTES:
            raise RetryableToolInputError(
                "The replacement text is too large for one atomic HTML edit."
            )
        normalized.append((start, end, expected_text, replacement, index))

    normalized.sort(key=lambda item: (item[0], item[1]))
    for previous, current in zip(normalized, normalized[1:], strict=False):
        if current[0] < previous[1] or current[0] == previous[0]:
            raise RetryableToolInputError(
                "Source patches must not overlap or share a start offset."
            )

    updated = source
    audit_patches: list[dict[str, object]] = []
    for start, end, _expected_text, replacement, _original_index in reversed(normalized):
        updated = updated[:start] + replacement + updated[end:]
    if not updated:
        raise RetryableToolInputError("The edited HTML source must not be empty.")
    if updated == source:
        raise RetryableToolInputError("The requested HTML edit does not change the source.")
    updated_bytes = updated.encode("utf-8")
    if len(updated_bytes) > min(DEFAULT_ARTIFACT_MAX_BYTES, _MAX_HTML_SOURCE_BYTES):
        raise RetryableToolInputError(
            "The edited HTML source exceeds the agent-editing size limit."
        )
    for start, end, expected_text, replacement, _original_index in normalized:
        audit_patches.append(
            {
                "start_offset": start,
                "end_offset": end,
                "expected_chars": len(expected_text),
                "expected_sha256": hashlib.sha256(expected_text.encode("utf-8")).hexdigest(),
                "replacement_chars": len(replacement),
                "replacement_sha256": hashlib.sha256(replacement.encode("utf-8")).hexdigest(),
            }
        )
    return updated, audit_patches


@dataclass(frozen=True, slots=True)
class PreparedDocumentMutation:
    """Validated, side-effect-free candidate ready for the shared commit kernel."""

    scope: _ArtifactScope
    store: ArtifactStore
    ref: ArtifactRef
    turn_id: str
    summary: str
    artifact_format: str
    adapter_id: str
    adapter_version: int
    base_revision_id: str
    source_sha256: str
    candidate_bytes: bytes
    candidate_sha256: str
    operations: tuple[dict[str, object], ...]
    validation_summary: dict[str, object]
    mutation_kind: str
    patch_count: int
    actor: Actor
    registry: Any
    reservation_id: str
    proposal_sha256: str

    def release_grants(self) -> None:
        self.registry.release_reservation(self.reservation_id)


def _prepare_semantic_document_mutation(
    *,
    scope: _ArtifactScope,
    store: ArtifactStore,
    ref: ArtifactRef,
    raw: bytes,
    turn_id: str,
    summary: str,
    mutations: list[dict[str, object]],
    proposal_sha256: str | None,
) -> PreparedDocumentMutation:
    if not mutations or len(mutations) > _MAX_DOCUMENT_MUTATIONS:
        raise DocumentMutationError(
            "DOCUMENT_MUTATIONS_INVALID",
            "Mutations must be a non-empty bounded array.",
            retry_policy="correctable",
        )
    canonical: list[dict[str, object]] = []
    tokens: list[str] = []
    input_bytes = 0
    for index, mutation in enumerate(mutations):
        if not isinstance(mutation, dict) or not {"grant_token"} <= set(mutation) <= {
            "grant_token",
            "input",
        }:
            raise DocumentMutationError(
                "DOCUMENT_MUTATION_INVALID",
                f"Mutation {index} has an invalid shape.",
                retry_policy="correctable",
            )
        token = mutation.get("grant_token")
        if not isinstance(token, str):
            raise DocumentMutationError(
                "DOCUMENT_MUTATION_INVALID",
                f"Mutation {index} has an invalid grant token.",
                retry_policy="correctable",
            )
        item = dict(mutation)
        if "input" in item:
            try:
                input_bytes += len(
                    json.dumps(
                        item["input"],
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except (TypeError, ValueError):
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_INPUT_INVALID",
                    f"Mutation {index} input is not valid JSON.",
                    retry_policy="correctable",
                ) from None
        if input_bytes > _MAX_DOCUMENT_INPUT_BYTES:
            raise DocumentMutationError(
                "DOCUMENT_MUTATION_INPUT_TOO_LARGE",
                "Mutation input exceeds the turn limit.",
                retry_policy="correctable",
            )
        canonical.append(item)
        tokens.append(token)

    try:
        adapter = get_document_format_adapter(scope.context.artifact_format)
    except DocumentAdapterError as exc:
        raise mutation_error_from_adapter(exc) from None
    registry = document_grant_registry_for_context(scope.ctx)
    reservation_id = f"document-mutation:{secrets.token_urlsafe(16)}"
    binding = _document_grant_binding(
        scope,
        ref.sha256,
        adapter_id=adapter.format_id,
        adapter_version=adapter.adapter_version,
    )
    try:
        grants = registry.reserve_grants(
            binding=binding,
            tokens=tokens,
            reservation_id=reservation_id,
        )
    except ArtifactRangeGrantError as exc:
        raise _range_error(exc) from None
    try:
        valid_orders = set(range(len(scope.anchors)))
        if any(
            not grant.annotation_orders
            or any(order not in valid_orders for order in grant.annotation_orders)
            or grant.adapter_id != adapter.format_id
            or grant.adapter_version != adapter.adapter_version
            for grant in grants
        ):
            raise DocumentMutationError(
                "DOCUMENT_MUTATION_GRANT_SCOPE_INVALID",
                "Every mutation grant must be bound to a current document selection.",
                retry_policy="forbidden",
            )
        granted_inputs = tuple(
            GrantedMutationInput(
                operation=grant.operation,
                target_fingerprint=grant.target_fingerprint,
                annotation_orders=grant.annotation_orders,
                has_input="input" in mutation,
                input_value=mutation.get("input"),
                adapter_locator=grant.adapter_locator,
            )
            for grant, mutation in zip(grants, canonical, strict=True)
        )
        candidate = adapter.prepare_granted_mutations(
            raw,
            mutations=granted_inputs,
        )
    except DocumentAdapterError as exc:
        registry.release_reservation(reservation_id)
        raise mutation_error_from_adapter(exc) from None
    except BaseException:
        registry.release_reservation(reservation_id)
        raise

    candidate_sha256 = hashlib.sha256(candidate.candidate_bytes).hexdigest()
    if proposal_sha256 is None:
        proposal_sha256 = hashlib.sha256(
            json.dumps(
                {"mutations": canonical, "tool": "document_apply"},
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    else:
        proposal_sha256 = str(proposal_sha256).strip().lower()
        if _SHA256_RE.fullmatch(proposal_sha256) is None:
            registry.release_reservation(reservation_id)
            raise DocumentMutationError(
                "DOCUMENT_MUTATION_PROPOSAL_INVALID",
                "The mutation proposal digest is invalid.",
                retry_policy="forbidden",
            )
    audit_facts = [dict(item) for item in candidate.audit_facts]
    semantic_operations = [dict(item) for item in candidate.semantic_operations]
    operations: tuple[dict[str, object], ...] = (
        {
            "op": "document_semantic_mutation",
            "adapter": adapter.format_id,
            "adapter_version": adapter.adapter_version,
            "expected_source_sha256": ref.sha256,
            "result_source_sha256": candidate_sha256,
            "adapter_audit": audit_facts,
            "semantic_operations": semantic_operations,
        },
    )
    validation_summary: dict[str, object] = {
        "format": adapter.format_id,
        "source_sha256": candidate_sha256,
        "mutation_count": len(canonical),
        "range_grant_validation": "completed",
        **candidate.validation_summary,
        "status": "passed",
    }
    return PreparedDocumentMutation(
        scope=scope,
        store=store,
        ref=ref,
        turn_id=turn_id,
        summary=summary,
        artifact_format=adapter.format_id,
        adapter_id=adapter.format_id,
        adapter_version=adapter.adapter_version,
        base_revision_id=scope.revision.revision_id,
        source_sha256=ref.sha256,
        candidate_bytes=candidate.candidate_bytes,
        candidate_sha256=candidate_sha256,
        operations=operations,
        validation_summary=validation_summary,
        mutation_kind="document_semantic",
        patch_count=len(canonical),
        actor=_actor(scope),
        registry=registry,
        reservation_id=reservation_id,
        proposal_sha256=proposal_sha256,
    )


async def _prepare_document_mutation(
    expected_sha256: str,
    patches: list[dict[str, object]],
    summary: str,
    *,
    _tool_name: str = "document_apply",
    _semantic_operations: list[dict[str, object]] | None = None,
    _proposal_sha256: str | None = None,
) -> PreparedDocumentMutation:
    scope = await _current_scope(
        _tool_name,
        required_format=None if _semantic_operations is not None else "html",
    )
    expected = str(expected_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(expected):
        raise SafeToolError(
            "ARTIFACT_EXPECTED_SHA_INVALID: expected_sha256 must be exactly 64 "
            "hexadecimal characters."
        )
    if not isinstance(summary, str) or not summary.strip():
        raise SafeToolError(
            "ARTIFACT_SUMMARY_INVALID: summary must be a non-empty reviewer-facing "
            "description."
        )
    summary = summary.strip()
    if len(summary) > _MAX_SUMMARY_CHARS:
        raise SafeToolError(
            f"ARTIFACT_SUMMARY_INVALID: summary must be at most {_MAX_SUMMARY_CHARS} "
            "characters."
        )
    turn_id = str(scope.ctx.task_id or "").strip()
    if not turn_id:
        raise SafeToolError("Artifact edits require a durable turn identity.")

    store, ref, raw = await _current_payload(scope)
    if _semantic_operations is None:
        await _require_single_file_html(scope, store, ref)
    if expected != ref.sha256:
        if _semantic_operations is not None:
            raise DocumentMutationError(
                "DOCUMENT_MUTATION_CONFLICT",
                "The document changed after its mutation grants were issued. "
                "Refresh the document before trying again.",
                retry_policy="refresh",
            )
        raise SafeToolError(
            "ARTIFACT_SOURCE_STALE: The canonical source changed after ranges were read. "
            "Read and locate the current source again."
        )
    if _semantic_operations is not None:
        return _prepare_semantic_document_mutation(
            scope=scope,
            store=store,
            ref=ref,
            raw=raw,
            turn_id=turn_id,
            summary=summary,
            mutations=_semantic_operations,
            proposal_sha256=_proposal_sha256,
        )
    source = _decode_html(raw)
    semantic_mutations = _semantic_operations is not None
    requested_patches: object = _semantic_operations if semantic_mutations else patches
    if (
        not isinstance(requested_patches, list)
        or not requested_patches
        or len(requested_patches) > _MAX_HTML_PATCHES
    ):
        if semantic_mutations:
            raise DocumentMutationError(
                "DOCUMENT_MUTATIONS_INVALID",
                "Mutations must be a non-empty bounded array.",
                retry_policy="correctable",
            )
        raise SafeToolError("ARTIFACT_PATCHES_INVALID: Patches must be a bounded array.")
    patch_by_token: dict[str, dict[str, object]] = {}
    replacement_bytes = 0
    for index, patch in enumerate(requested_patches):
        if not isinstance(patch, dict):
            if semantic_mutations:
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_INVALID",
                    f"Mutation {index} is not an object.",
                    retry_policy="correctable",
                )
            raise SafeToolError(f"ARTIFACT_PATCH_INVALID: Patch {index} is not an object.")
        token = (
            patch.get("grant_token") if semantic_mutations else patch.get("range_token")
        )
        if semantic_mutations:
            keys = set(patch)
            required_keys = {"grant_token"}
            allowed_keys = {*required_keys, "input"}
            if not required_keys <= keys or not keys <= allowed_keys:
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_INVALID",
                    f"Mutation {index} has an invalid shape.",
                    retry_policy="correctable",
                )
            input_value = patch.get("input")
            if not isinstance(token, str):
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_INVALID",
                    f"Mutation {index} has invalid values.",
                    retry_policy="correctable",
                )
            if isinstance(input_value, str):
                replacement_bytes += len(input_value.encode("utf-8"))
            elif input_value is not None:
                try:
                    replacement_bytes += len(
                        json.dumps(
                            input_value,
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                except (TypeError, ValueError):
                    raise DocumentMutationError(
                        "DOCUMENT_MUTATION_INPUT_INVALID",
                        f"Mutation {index} input is not valid JSON.",
                        retry_policy="correctable",
                    ) from None
        else:
            if set(patch) != {"range_token", "action", "text"}:
                raise SafeToolError(
                    f"ARTIFACT_PATCH_INVALID: Patch {index} has an invalid shape."
                )
            action = patch.get("action")
            text = patch.get("text")
            if (
                not isinstance(token, str)
                or not isinstance(action, str)
                or action not in {"replace", "insert_before", "insert_after"}
                or not isinstance(text, str)
            ):
                raise SafeToolError(
                    f"ARTIFACT_PATCH_INVALID: Patch {index} has invalid values."
                )
            replacement_bytes += len(text.encode("utf-8"))
        if replacement_bytes > _MAX_HTML_REPLACEMENT_BYTES:
            if semantic_mutations:
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_INPUT_TOO_LARGE",
                    "Mutation input exceeds the turn limit.",
                    retry_policy="correctable",
                )
            raise SafeToolError(
                "ARTIFACT_REPLACEMENT_TOO_LARGE: Replacement text exceeds the turn limit."
            )
        if token in patch_by_token:
            if semantic_mutations:
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_GRANT_DUPLICATE",
                    "Every mutation grant must be present exactly once.",
                    retry_policy="correctable",
                )
            raise SafeToolError(
                "ARTIFACT_RANGE_DUPLICATE: Every source range must be present exactly once."
            )
        patch_by_token[token] = patch
    reservation_id = f"range-edit:{secrets.token_urlsafe(16)}"
    registry = registry_for_context(scope.ctx)
    binding = _range_binding(scope, ref.sha256)
    try:
        resolved = registry.reserve_ranges(
            binding=binding,
            source=source,
            tokens=list(patch_by_token),
            reservation_id=reservation_id,
        )
    except ArtifactRangeGrantError as exc:
        raise _range_error(exc) from None
    valid_orders = set(range(len(scope.anchors)))
    if semantic_mutations and any(
        not grant.annotation_orders
        or any(order not in valid_orders for order in grant.annotation_orders)
        for grant in resolved
    ):
        registry.release_reservation(reservation_id)
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_GRANT_SCOPE_INVALID",
            "Every mutation grant must be bound to a current document selection.",
            retry_policy="forbidden",
        )
    offset_patches: list[dict[str, object]] = []
    semantic_audit: list[dict[str, object]] = []
    grant_by_token: dict[str, ResolvedRangeGrant] = {
        grant.token: grant for grant in resolved
    }
    validated_css_mutation = False
    adapter = get_document_format_adapter("html") if semantic_mutations else None
    for token, patch in patch_by_token.items():
        grant = grant_by_token[token]
        expected_text = source[grant.start : grant.end]
        if semantic_mutations:
            assert isinstance(adapter, HtmlDocumentFormatAdapter)
            if (
                grant.adapter_id != adapter.format_id
                or grant.adapter_version != adapter.adapter_version
            ):
                registry.release_reservation(reservation_id)
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_GRANT_ADAPTER_MISMATCH",
                    "The mutation grant is not valid for the active document adapter.",
                    retry_policy="forbidden",
                )
            granted_operation = grant.operation
            has_input = "input" in patch
            if granted_operation in {"remove_attribute", "remove_node"} and has_input:
                registry.release_reservation(reservation_id)
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_INPUT_UNEXPECTED",
                    "This mutation grant does not accept input. Copy its applyTemplate "
                    "exactly and omit the input field entirely.",
                    retry_policy="correctable",
                )
            input_required = granted_operation in {
                "replace_text",
                "set_attribute",
                "set_style",
            }
            if input_required and not has_input:
                registry.release_reservation(reservation_id)
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_INPUT_REQUIRED",
                    "This mutation grant requires input. Copy its applyTemplate and replace "
                    "the placeholder with the requested value.",
                    retry_policy="correctable",
                )
            input_value = patch.get("input")
            try:
                prepared = adapter.apply_grant(
                    source,
                    start=grant.start,
                    end=grant.end,
                    grant_kind=grant.kind,
                    input_value=input_value,
                )
            except DocumentAdapterError as exc:
                registry.release_reservation(reservation_id)
                raise mutation_error_from_adapter(exc) from None
            action = "replace"
            text = prepared.replacement
            validated_css_mutation = validated_css_mutation or prepared.css_mutation
            semantic_audit.append(
                {
                    "operation": prepared.operation,
                    "target_kind": prepared.target_kind,
                    "attribute_name": prepared.attribute_name,
                    "grant_kind": grant.kind,
                    "annotation_orders": list(grant.annotation_orders),
                    "target_fingerprint": grant.target_fingerprint,
                }
            )
        else:
            action = patch["action"]
            text = patch["text"]
            assert isinstance(action, str) and isinstance(text, str)
            try:
                _validate_grant_replacement(
                    source=source,
                    grant=grant,
                    action=action,
                    text=text,
                    prompt_annotation=hasattr(scope.context, "annotation_ids"),
                )
            except SafeToolError:
                registry.release_reservation(reservation_id)
                raise
            if grant.kind in {"attribute:style", "related_css_rule"} or (
                grant.kind == "opening_tag"
                and action == "replace"
                and re.search(r"(?is)\bstyle\s*=", expected_text + text) is not None
            ):
                validated_css_mutation = True
        replacement = (
            text
            if action == "replace"
            else text + expected_text
            if action == "insert_before"
            else expected_text + text
        )
        offset_patches.append(
            {
                "start_offset": grant.start,
                "end_offset": grant.end,
                "expected_text": expected_text,
                "replacement": replacement,
            }
        )
    try:
        updated, audit_patches = _validate_patches(
            source,
            offset_patches,
            offset_encoding=_HTML_OFFSET_ENCODING,
        )
        _assert_source_preserving_splice(source, updated, offset_patches)
        adapter_validation = adapter.validate(updated) if adapter is not None else {}
    except DocumentAdapterError as exc:
        registry.release_reservation(reservation_id)
        raise mutation_error_from_adapter(exc) from None
    except Exception as exc:
        registry.release_reservation(reservation_id)
        if isinstance(exc, SafeToolError):
            if semantic_mutations:
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_VALIDATION_FAILED",
                    "The proposed mutations could not be validated safely.",
                    retry_policy="correctable",
                ) from None
            raise SafeToolError(f"ARTIFACT_PATCH_INVALID: {exc.user_message}") from None
        raise
    audit_by_span = {
        (item["start_offset"], item["end_offset"]): item for item in audit_patches
    }
    for grant in resolved:
        audit = audit_by_span[(grant.start, grant.end)]
        audit["grant_kind"] = grant.kind
        audit["annotation_orders"] = list(grant.annotation_orders)
    candidate_bytes = updated.encode("utf-8")
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    actor = _actor(scope)
    if _proposal_sha256 is None:
        proposal_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "expected_sha256": expected,
                    "mutations": requested_patches,
                    "mutation_kind": (
                        "document_semantic" if semantic_mutations else "html_source_patch"
                    ),
                    "summary": summary,
                    "tool": _tool_name,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    else:
        proposal_sha256 = str(_proposal_sha256).strip().lower()
        if _SHA256_RE.fullmatch(proposal_sha256) is None:
            registry.release_reservation(reservation_id)
            raise DocumentMutationError(
                "DOCUMENT_MUTATION_PROPOSAL_INVALID",
                "The mutation proposal digest is invalid.",
                retry_policy="forbidden",
            )
    mutation_kind = "document_semantic" if semantic_mutations else "html_source_patch"
    adapter_id = adapter.format_id if adapter is not None else "html"
    adapter_version = adapter.adapter_version if adapter is not None else 1
    operations: tuple[dict[str, object], ...] = (
        {
            "op": (
                "document_semantic_mutation"
                if semantic_mutations
                else "html_source_patch"
            ),
            "adapter": adapter_id if semantic_mutations else None,
            "adapter_version": adapter_version if semantic_mutations else None,
            "expected_source_sha256": ref.sha256,
            "result_source_sha256": candidate_sha256,
            "offset_encoding": _HTML_OFFSET_ENCODING,
            "patches": audit_patches,
            "semantic_operations": semantic_audit if semantic_mutations else None,
        },
    )
    validation_summary: dict[str, object] = {
        "format": "html",
        "encoding": "utf-8",
        "source_sha256": candidate_sha256,
        "patch_count": len(audit_patches),
        "range_grant_validation": "completed",
        "semantic_adapter_validation": (
            adapter_validation if semantic_mutations else "not_applicable"
        ),
        "css_validation": (
            "modified_grants_completed"
            if validated_css_mutation
            else "not_performed"
        ),
        "script_validation": "not_applicable_no_script_grants",
        "status": "passed",
    }
    return PreparedDocumentMutation(
        scope=scope,
        store=store,
        ref=ref,
        turn_id=turn_id,
        summary=summary,
        artifact_format="html",
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        base_revision_id=scope.revision.revision_id,
        source_sha256=ref.sha256,
        candidate_bytes=candidate_bytes,
        candidate_sha256=candidate_sha256,
        operations=operations,
        validation_summary=validation_summary,
        mutation_kind=mutation_kind,
        patch_count=len(audit_patches),
        actor=actor,
        registry=registry,
        reservation_id=reservation_id,
        proposal_sha256=proposal_sha256,
    )


async def _prepare_document_text_patch(
    expected_sha256: str,
    edits: list[dict[str, object]],
    *,
    proposal_sha256: str,
) -> PreparedDocumentMutation:
    """Prepare an exact-text patch against a server-bound document head.

    The model supplies no path, revision, session, or source offsets.  Every
    expected fragment must identify exactly one range in the current canonical
    source, after which the ordinary mutation commit kernel owns lease, CAS,
    candidate publication, validation, ChangeSet, revision, and event commit.
    """

    scope = await _current_scope("document_patch", required_format="html")
    expected = str(expected_sha256 or "").strip().lower()
    if _SHA256_RE.fullmatch(expected) is None:
        raise DocumentMutationError(
            "DOCUMENT_PATCH_EXPECTED_SHA_INVALID",
            "expectedSha256 must be exactly 64 hexadecimal characters.",
            retry_policy="correctable",
        )
    turn_id = str(scope.ctx.task_id or "").strip()
    if not turn_id:
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_AUTHORITY_UNAVAILABLE",
            "Commit authority is unavailable.",
            retry_policy="forbidden",
        )
    if not isinstance(edits, list) or not edits or len(edits) > _MAX_HTML_PATCHES:
        raise DocumentMutationError(
            "DOCUMENT_PATCH_EDITS_INVALID",
            "edits must be a non-empty bounded array.",
            retry_policy="correctable",
        )

    store, ref, raw = await _current_payload(scope)
    await _require_single_file_html(scope, store, ref)
    if expected != ref.sha256:
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_CONFLICT",
            "The document changed after it was read. Read the current document and retry.",
            retry_policy="refresh",
        )
    source = _decode_html(raw)
    patches: list[dict[str, object]] = []
    canonical_edits: list[dict[str, str]] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or set(edit) != {"expectedText", "replacement"}:
            raise DocumentMutationError(
                "DOCUMENT_PATCH_EDIT_INVALID",
                f"edits[{index}] must contain only expectedText and replacement.",
                retry_policy="correctable",
            )
        expected_text = edit.get("expectedText")
        replacement = edit.get("replacement")
        if not isinstance(expected_text, str) or not expected_text:
            raise DocumentMutationError(
                "DOCUMENT_PATCH_EXPECTED_TEXT_INVALID",
                f"edits[{index}].expectedText must be a non-empty string.",
                retry_policy="correctable",
            )
        if not isinstance(replacement, str):
            raise DocumentMutationError(
                "DOCUMENT_PATCH_REPLACEMENT_INVALID",
                f"edits[{index}].replacement must be a string.",
                retry_policy="correctable",
            )
        start = source.find(expected_text)
        if start < 0:
            raise DocumentMutationError(
                "DOCUMENT_PATCH_TEXT_NOT_FOUND",
                f"edits[{index}].expectedText is not present in the current source.",
                retry_policy="correctable",
            )
        if source.find(expected_text, start + 1) >= 0:
            raise DocumentMutationError(
                "DOCUMENT_PATCH_TEXT_AMBIGUOUS",
                f"edits[{index}].expectedText is not unique in the current source.",
                retry_policy="correctable",
            )
        patches.append(
            {
                "start_offset": start,
                "end_offset": start + len(expected_text),
                "expected_text": expected_text,
                "replacement": replacement,
            }
        )
        canonical_edits.append(
            {"expectedText": expected_text, "replacement": replacement}
        )

    try:
        updated, audit_patches = _validate_patches(
            source,
            patches,
            offset_encoding=_HTML_OFFSET_ENCODING,
        )
        _assert_source_preserving_splice(source, updated, patches)
        adapter = get_document_format_adapter("html")
        adapter_validation = adapter.validate(updated)
    except DocumentAdapterError as exc:
        raise mutation_error_from_adapter(exc) from None
    except RetryableToolInputError as exc:
        raise DocumentMutationError(
            "DOCUMENT_PATCH_INVALID",
            exc.user_message,
            retry_policy="correctable",
        ) from None

    normalized_proposal_sha256 = str(proposal_sha256 or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized_proposal_sha256) is None:
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_PROPOSAL_INVALID",
            "The mutation proposal digest is invalid.",
            retry_policy="forbidden",
        )
    candidate_bytes = updated.encode("utf-8")
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    registry = registry_for_context(scope.ctx)
    reservation_id = f"document-text-patch:{secrets.token_urlsafe(16)}"
    operations: tuple[dict[str, object], ...] = (
        {
            "op": "document_text_patch",
            "adapter": adapter.format_id,
            "adapter_version": adapter.adapter_version,
            "expected_source_sha256": ref.sha256,
            "result_source_sha256": candidate_sha256,
            "offset_encoding": _HTML_OFFSET_ENCODING,
            "patches": audit_patches,
        },
    )
    return PreparedDocumentMutation(
        scope=scope,
        store=store,
        ref=ref,
        turn_id=turn_id,
        summary=f"Applied {len(canonical_edits)} document text edits",
        artifact_format="html",
        adapter_id=adapter.format_id,
        adapter_version=adapter.adapter_version,
        base_revision_id=scope.revision.revision_id,
        source_sha256=ref.sha256,
        candidate_bytes=candidate_bytes,
        candidate_sha256=candidate_sha256,
        operations=operations,
        validation_summary={
            "format": "html",
            "encoding": "utf-8",
            "source_sha256": candidate_sha256,
            "patch_count": len(audit_patches),
            "text_match_validation": "unique_current_source",
            "semantic_adapter_validation": adapter_validation,
            "status": "passed",
        },
        mutation_kind="document_text_patch",
        patch_count=len(audit_patches),
        actor=_actor(scope),
        registry=registry,
        reservation_id=reservation_id,
        proposal_sha256=normalized_proposal_sha256,
    )


async def _commit_prepared_document_mutation(
    prepared: PreparedDocumentMutation,
) -> str:
    """Durably commit exactly one fully validated proposal."""

    scope = prepared.scope
    store = prepared.store
    ref = prepared.ref
    turn_id = prepared.turn_id
    summary = prepared.summary
    artifact_format = prepared.artifact_format
    candidate_bytes = prepared.candidate_bytes
    actor = prepared.actor
    registry = prepared.registry
    reservation_id = prepared.reservation_id

    # Candidate-loop callers stage before this legacy durable path. Keep this
    # guard here as well for direct/internal callers that bypass the semantic
    # tool adapters.
    staged_result = await _stage_prepared_document_mutation(prepared)
    if staged_result is not None:
        return staged_result

    if (
        prepared.base_revision_id != scope.revision.revision_id
        or prepared.source_sha256 != ref.sha256
        or hashlib.sha256(candidate_bytes).hexdigest() != prepared.candidate_sha256
    ):
        prepared.release_grants()
        raise DocumentMutationError(
            "DOCUMENT_MUTATION_PREPARED_STATE_INVALID",
            "The prepared document mutation is inconsistent.",
            retry_policy="forbidden",
        )

    max_bytes = scope.ctx.artifact_max_bytes
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        max_bytes = DEFAULT_ARTIFACT_MAX_BYTES
    disk_budget = scope.ctx.artifact_disk_budget_bytes
    if not isinstance(disk_budget, int) or isinstance(disk_budget, bool) or disk_budget <= 0:
        disk_budget = DEFAULT_ARTIFACT_DISK_BUDGET_BYTES

    candidate: ArtifactRef | None = None
    candidate_id: str | None = None
    committed_change: ChangeSet | None = None
    applied: CommitResult | None = None
    lease: WriterLease | None = None
    candidate_publish: asyncio.Task[ArtifactRef] | None = None
    try:
        holder_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()[:24]
        holder_id = f"artifact-agent:{actor.actor_id[:128]}:{holder_digest}"
        lease = await scope.service.acquire_writer_lease(
            document_id=scope.document.document_id,
            holder_id=holder_id,
            ttl_ms=_WRITER_LEASE_TTL_MS,
            actor=actor,
        )
        locked_document = await scope.service.get_document(scope.document.document_id)
        if (
            locked_document.head_revision_id != scope.revision.revision_id
            or locked_document.state_revision != scope.document.state_revision
        ):
            raise ArtifactConflictError("document changed before the agent write lease")
        candidate_id = store.allocate_artifact_id()
        candidate_sha256 = prepared.candidate_sha256
        if scope.ctx.artifact_mutation_attempt_controller is not None:
            await scope.service.register_mutation_candidate(
                document_id=scope.document.document_id,
                turn_id=turn_id,
                candidate_session_id=scope.context.session_id,
                candidate_artifact_id=candidate_id,
                candidate_artifact_sha256=candidate_sha256,
            )
        candidate_publish = asyncio.create_task(
            asyncio.to_thread(
                store.publish_bytes,
                candidate_bytes,
                session_id=scope.context.session_id,
                session_key=scope.context.session_key,
                name=ref.name,
                mime=ref.mime,
                source=f"document_{prepared.adapter_id}_agent_edit",
                max_bytes=max_bytes,
                disk_budget_bytes=disk_budget,
                visibility="internal",
                artifact_id=candidate_id,
            )
        )
        candidate = await asyncio.shield(candidate_publish)
        candidate_validation = await asyncio.to_thread(
            _validated_payload,
            candidate_bytes,
            artifact_format=artifact_format,
            ref=candidate,
        )
        applied, committed_change = await scope.service.commit_change_set_atomically(
            document_id=scope.document.document_id,
            base_revision_id=scope.revision.revision_id,
            expected_document_state_revision=locked_document.state_revision,
            operations=prepared.operations,
            actor=actor,
            turn_id=turn_id,
            summary=summary,
            candidate_artifact=ArtifactBlobRef(
                artifact_id=candidate.id,
                sha256=candidate.sha256,
                filename=candidate.name,
                media_type=candidate.mime,
                byte_size=candidate.size,
            ),
            validation={
                **candidate_validation,
                **prepared.validation_summary,
                "source_sha256": candidate.sha256,
            },
            lease=lease,
            require_lease=True,
        )
    except BaseException as exc:  # reconcile every failed multi-resource edit
        if candidate is None and candidate_publish is not None:
            try:
                candidate = await asyncio.shield(candidate_publish)
            except Exception:  # noqa: BLE001 - publication failed before an artifact existed
                pass
        durable_change: ChangeSet | None = None
        durable_state_known = False
        try:
            durable_change = await scope.service.get_change_set_by_turn(
                document_id=scope.document.document_id,
                turn_id=turn_id,
            )
            durable_state_known = True
        except Exception:  # noqa: BLE001 - ambiguous durable state must retain bytes
            pass

        if (
            applied is None
            and candidate is not None
            and durable_change is not None
            and durable_change.status is ChangeSetStatus.APPLIED
            and durable_change.applied_revision_id is not None
        ):
            try:
                durable_document = await scope.service.get_document(
                    durable_change.document_id
                )
                durable_revision = await scope.service.get_revision(
                    durable_change.applied_revision_id
                )
            except Exception:  # noqa: BLE001 - preserve bytes on ambiguous commit state
                pass
            else:
                if (
                    durable_document.head_revision_id == durable_revision.revision_id
                    and durable_revision.change_set_id == durable_change.change_set_id
                    and durable_revision.artifact_id == candidate.id
                    and durable_revision.artifact_sha256 == candidate.sha256
                ):
                    applied = CommitResult(
                        document=durable_document,
                        revision=durable_revision,
                    )
                    committed_change = durable_change

        # The atomic repository boundary either commits an APPLIED change set and
        # head revision together or leaves no row. A non-applied row can only be
        # legacy/concurrent state, so retain its referenced bytes and fail closed.
        candidate_can_be_deleted = (
            applied is None
            and durable_state_known
            and (
                durable_change is None
                or (
                    candidate is not None
                    and durable_change.status is ChangeSetStatus.APPLIED
                    and durable_change.candidate_artifact_id != candidate.id
                )
            )
        )

        candidate_cleanup_ambiguous = False
        if applied is None and candidate is not None and candidate_can_be_deleted:
            try:
                deleted = await asyncio.to_thread(
                    store.delete_ref,
                    session_id=scope.context.session_id,
                    artifact_id=candidate.id,
                )
            except (ArtifactError, OSError, ValueError):
                candidate_cleanup_ambiguous = True
            else:
                # A journaled, freshly-published candidate must be proven gone.
                # ``False`` is not safe to treat as success: a concurrent or
                # partially-failed deletion can otherwise leave an orphan whose
                # attempt dispatch would terminalize and boot would never scan.
                candidate_cleanup_ambiguous = not deleted
            if candidate_cleanup_ambiguous:
                controller = scope.ctx.artifact_mutation_attempt_controller
                if controller is not None:
                    try:
                        await controller.mark_active_ambiguous(
                            "writer_candidate_cleanup_failed"
                        )
                    except Exception:  # noqa: BLE001 - dispatch retries the durable fence
                        pass
        if applied is None:
            registry.release_reservation(reservation_id)
            if not isinstance(exc, Exception):
                raise
            if candidate_cleanup_ambiguous:
                raise ArtifactMutationCleanupAmbiguousError(
                    "The failed artifact edit candidate could not be safely cleaned up."
                ) from None
            if isinstance(exc, ArtifactConflictError):
                raise DocumentMutationError(
                    "DOCUMENT_MUTATION_CONFLICT",
                    "The document changed before this mutation could commit. "
                    "Refresh the document before trying again.",
                    retry_policy="refresh",
                ) from None
            raise SafeToolError(
                "The document mutation could not be applied atomically. "
                "Refresh the document and retry."
            ) from None
        if not isinstance(exc, Exception):
            raise
    finally:
        if lease is not None:
            try:
                await scope.service.release_writer_lease(lease=lease, actor=actor)
            except Exception:  # noqa: BLE001 - the bounded lease expires if release fails
                pass

    assert applied is not None and committed_change is not None and candidate is not None
    registry.consume_reservation(reservation_id)
    await _emit_artifact_state(
        scope,
        action="source.patched",
        revision_id=applied.revision.revision_id,
        change_set_id=committed_change.change_set_id,
    )

    return _json(
        {
            "status": "applied",
            "terminal_response": summary,
            "change_set": {
                "state": "applied",
                "summary": committed_change.summary,
                "patch_count": prepared.patch_count,
                "base_sha256": ref.sha256,
                "candidate_sha256": candidate.sha256,
                "candidate_bytes": candidate.size,
                "mutation_kind": prepared.mutation_kind,
            },
            "revision": {
                "generation": applied.revision.generation,
                "sha256": applied.revision.artifact_sha256,
            },
            "document_head_changed": True,
            "revert_scope": "entire_agent_turn",
        }
    )


__all__: list[str] = []
