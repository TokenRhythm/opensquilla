"""Prepare fixed artifact and execution capabilities for one turn admission."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from opensquilla.application.turn_admission import AdmitTurn
from opensquilla.artifact_session import (
    ArtifactConflictError as ArtifactPromptAnnotationConflictError,
)
from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactPromptAnnotationNotFoundError,
)
from opensquilla.artifact_session import (
    ArtifactSessionService,
    PreparedPromptAnnotationTarget,
    PromptAnnotation,
)
from opensquilla.artifact_session import (
    ArtifactValidationError as ArtifactPromptAnnotationValidationError,
)
from opensquilla.gateway.artifact_contexts import BoundDocumentContext, BoundPromptAnnotationContext
from opensquilla.gateway.artifact_product_errors import (
    ArtifactProductErrorCode,
    logged_artifact_product_error,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.desktop_artifact_bridge import TurnAuthorityCleanup
from opensquilla.gateway.project_workspace_runtime import (
    AcceptedRunModeOverride,
    apply_accepted_run_mode_override,
    apply_run_context_route_metadata,
    authoritative_project_run_context,
    map_project_workspace_error,
)
from opensquilla.gateway.routing import RouteEnvelope
from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.project_workspaces import ProjectWorkspaceGuard, ProjectWorkspaceStateError
from opensquilla.run_mode import RunMode
from opensquilla.sandbox.guest_profile import GuestProfile, GuestProfileBoundaryError
from opensquilla.sandbox.mode_resolver import ModeResolutionError, ResolvedMode, resolve_mode
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    RunContext,
    resolve_default_run_mode,
)
from opensquilla.sandbox.run_mode_policy import (
    coerce_run_mode_for_principal,
    principal_has_host_execute,
)
from opensquilla.sandbox.setup_runtime import current_sandbox_capability_report
from opensquilla.session.manager import PreparedSessionIntent
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage

if TYPE_CHECKING:
    from opensquilla.session.manager import SessionManager

log = structlog.get_logger(__name__)
type ArtifactEventEmitter = Callable[[dict[str, Any]], Awaitable[None]]


class FollowupAnnotationFocus(Protocol):
    async def __call__(self, *, session_id: str, document_id: str) -> str | None: ...


class AdmissionAuthorityScope(Protocol):
    def register(self, cleanup: TurnAuthorityCleanup) -> None: ...


@dataclass
class ArtifactBinding:
    context: BoundDocumentContext | BoundPromptAnnotationContext | None = None
    service: ArtifactSessionService | None = None
    event_emitter: ArtifactEventEmitter | None = None
    annotations: tuple[PromptAnnotation, ...] = ()
    targets: tuple[PreparedPromptAnnotationTarget, ...] = ()
    snapshots: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PreparedRuntimeRoute:
    agent_id: str
    envelope: RouteEnvelope
    turn_id: str
    run_context: RunContext
    mode_resolution: ResolvedMode
    guest_profile: GuestProfile | None
    accepted_run_mode_override: AcceptedRunModeOverride | None
    accepted_run_mode_origin: dict[str, Any] | None
    workspace_guard: ProjectWorkspaceGuard | None
    session: SessionNode
    configured_workspace_dir: str | None
    host_execute_allowed: bool


async def bind_artifact(
    command: AdmitTurn,
    *,
    key: str,
    session_id: str,
    session: SessionNode,
    storage: SessionStorage,
    media_root: str | Path,
    principal_actor_id: str | None,
    event_emitter_factory: Callable[[str], ArtifactEventEmitter],
    load_followup_focus: FollowupAnnotationFocus,
) -> ArtifactBinding:
    """Preflight one immutable document or annotation target against durable revisions."""
    prompt_annotation_ids = command.prompt_annotation_ids
    document_context_request = command.document_context
    artifact_turn_context: BoundDocumentContext | BoundPromptAnnotationContext | None = None
    artifact_session_service: ArtifactSessionService | None = None
    artifact_event_emitter: ArtifactEventEmitter | None = None
    prompt_annotation_rows: tuple[PromptAnnotation, ...] = ()
    prepared_prompt_annotation_targets: tuple[PreparedPromptAnnotationTarget, ...] = ()
    prompt_annotation_snapshots: tuple[dict[str, Any], ...] = ()
    if prompt_annotation_ids:
        from opensquilla.artifact_session import (
            ActorKind,
            PreparedPromptAnnotationTarget,
        )
        from opensquilla.artifact_session.html_anchors import remap_html_anchor
        from opensquilla.artifacts import ArtifactError, ArtifactStore
        from opensquilla.gateway.artifact_contexts import (
            PROMPT_ANNOTATION_TOOL_NAMES,
            BoundPromptAnnotationContext,
            BoundPromptAnnotationTarget,
        )
        from opensquilla.prompt_annotations import (
            PromptAnnotationSnapshotError,
            normalize_prompt_annotation_snapshots,
            render_active_prompt_annotation_context,
        )

        try:
            artifact_session_service = await ArtifactSessionService.from_session_storage(storage)
            prompt_annotation_rows = await artifact_session_service.preflight_prompt_annotations(
                annotation_ids=prompt_annotation_ids,
                session_key=key,
                session_id=session_id,
                session_epoch=int(getattr(session, "epoch", 0) or 0),
                require_current_head=False,
            )
            annotation_document = await artifact_session_service.get_document(
                prompt_annotation_rows[0].document_id
            )
            annotation_revision = await artifact_session_service.get_revision(
                annotation_document.head_revision_id
            )
            annotation_anchors = tuple(
                [
                    await artifact_session_service.get_anchor(annotation.anchor_id)
                    for annotation in prompt_annotation_rows
                ]
            )
            store = ArtifactStore(media_root)
            source_cache: dict[str, str] = {}

            async def _revision_source(revision: Any) -> str:
                cached = source_cache.get(revision.revision_id)
                if cached is not None:
                    return cached
                try:
                    supports_editing = await asyncio.to_thread(
                        store.supports_single_file_editing,
                        revision.artifact_id,
                        session_id=session_id,
                    )
                    if not supports_editing:
                        raise ArtifactPromptAnnotationValidationError(
                            "prompt annotations require one editable HTML file"
                        )
                    ref, path = await asyncio.to_thread(
                        store.resolve_for_download,
                        revision.artifact_id,
                        session_id=session_id,
                    )
                    payload = await asyncio.to_thread(path.read_bytes)
                except (ArtifactError, OSError, ValueError) as exc:
                    raise ArtifactPromptAnnotationValidationError(
                        "the annotated page is temporarily unavailable"
                    ) from exc
                if (
                    ref.session_key != key
                    or ref.sha256 != revision.artifact_sha256
                    or ref.size != revision.byte_size
                    or len(payload) != ref.size
                ):
                    raise ArtifactPromptAnnotationValidationError(
                        "the annotated page failed integrity validation"
                    )
                try:
                    source = payload.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ArtifactPromptAnnotationValidationError(
                        "prompt annotations require UTF-8 HTML"
                    ) from exc
                source_cache[revision.revision_id] = source
                return source

            current_source = await _revision_source(annotation_revision)
            raw_snapshots: list[dict[str, Any]] = []
            prepared_targets: list[PreparedPromptAnnotationTarget] = []
            bound_targets: list[BoundPromptAnnotationTarget] = []
            actor_id = (
                principal_actor_id
                if isinstance(principal_actor_id, str) and principal_actor_id
                else "local-owner"
            )
            for order, (annotation, anchor) in enumerate(
                zip(prompt_annotation_rows, annotation_anchors, strict=True)
            ):
                old_revision = await artifact_session_service.get_revision(annotation.revision_id)
                if old_revision.document_id != annotation_document.document_id:
                    raise ArtifactPromptAnnotationConflictError(
                        "prompt annotation revision belongs to another document"
                    )
                try:
                    resolution = remap_html_anchor(
                        old_source=await _revision_source(old_revision),
                        current_source=current_source,
                        anchor=anchor,
                    )
                except ValueError as exc:
                    raise ArtifactPromptAnnotationValidationError(
                        "the annotation target could not be normalized"
                    ) from exc
                locator = dict(resolution.locator)
                tag_name = locator.get("tag_name") or locator.get("tagName")
                if not isinstance(tag_name, str) or not tag_name.strip():
                    raise ArtifactPromptAnnotationConflictError(
                        "prompt annotation anchor lost its element tag"
                    )
                new_anchor_id = artifact_session_service.allocate_id("anchor")
                prepared_targets.append(
                    PreparedPromptAnnotationTarget(
                        expected_annotation=annotation,
                        previous_anchor_id=anchor.anchor_id,
                        anchor_id=new_anchor_id,
                        audit_event_id=artifact_session_service.allocate_id("audit"),
                        revision_id=annotation_revision.revision_id,
                        kind=resolution.kind,
                        locator=locator,
                        quote=resolution.quote,
                        context=dict(resolution.context),
                        state=resolution.state,
                        actor_kind=ActorKind.USER,
                        actor_id=actor_id,
                    )
                )
                bound_targets.append(
                    BoundPromptAnnotationTarget(
                        annotation_id=annotation.annotation_id,
                        anchor_id=new_anchor_id,
                        status=resolution.status,
                        reason=resolution.reason,
                        tag_name=tag_name.lower(),
                        target_kind=resolution.target_kind,
                        target_text=resolution.target_text,
                    )
                )
                raw_snapshots.append(
                    {
                        "version": 1,
                        "annotationId": annotation.annotation_id,
                        "order": order,
                        "body": annotation.body,
                        "targetStatus": resolution.status,
                        "targetReason": resolution.reason,
                        "targetKind": resolution.target_kind,
                        "targetText": resolution.target_text,
                        "document": {
                            "id": annotation_document.document_id,
                            "name": annotation_document.name,
                            "kind": annotation_document.kind.value,
                        },
                        "revision": {
                            "id": annotation_revision.revision_id,
                            "generation": annotation_revision.generation,
                            "sha256": annotation_revision.artifact_sha256,
                        },
                        "anchor": {
                            "id": new_anchor_id,
                            "kind": resolution.kind.value,
                            "tagName": tag_name.lower(),
                            "locator": locator,
                            "quote": resolution.quote,
                        },
                    }
                )
            prompt_annotation_snapshots = normalize_prompt_annotation_snapshots(raw_snapshots)
            request_context_prompt = render_active_prompt_annotation_context(
                prompt_annotation_snapshots
            )
            if request_context_prompt is None:
                raise ArtifactPromptAnnotationValidationError("prompt annotation context is empty")
            operation_class = (
                "selection_edit" if len(prompt_annotation_rows) == 1 else "structural_edit"
            )
            artifact_turn_context = BoundPromptAnnotationContext(
                session_key=key,
                session_id=session_id,
                document_id=annotation_document.document_id,
                revision_id=annotation_revision.revision_id,
                targets=tuple(bound_targets),
                snapshots=prompt_annotation_snapshots,
                artifact_format="html",
                tool_names=PROMPT_ANNOTATION_TOOL_NAMES,
                operation_class=operation_class,
                request_context_prompt=request_context_prompt,
            )
            prepared_prompt_annotation_targets = tuple(prepared_targets)
            artifact_event_emitter = event_emitter_factory(key)
        except ArtifactPromptAnnotationNotFoundError as exc:
            raise logged_artifact_product_error(
                ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
                exc,
                operation="prompt_annotations.prepare",
                retryable=True,
                session_key=key,
            ) from exc
        except ArtifactPromptAnnotationConflictError as exc:
            raise logged_artifact_product_error(
                ArtifactProductErrorCode.ANNOTATION_BUSY,
                exc,
                operation="prompt_annotations.prepare",
                retryable=True,
                session_key=key,
            ) from exc
        except (ArtifactPromptAnnotationValidationError, PromptAnnotationSnapshotError) as exc:
            raise logged_artifact_product_error(
                ArtifactProductErrorCode.ANNOTATION_UNAVAILABLE,
                exc,
                operation="prompt_annotations.prepare",
                retryable=False,
                session_key=key,
            ) from exc
    elif document_context_request is not None:
        from opensquilla.gateway.artifact_contexts import (
            DOCUMENT_CONTEXT_TOOL_NAMES,
            BoundDocumentContext,
        )

        try:
            artifact_session_service = await ArtifactSessionService.from_session_storage(storage)
            current_head = await artifact_session_service.get_document_head(
                document_context_request.document_id,
            )
            document = current_head.document
            revision = current_head.revision
            if (
                document.session_key != key
                or document.session_id != session_id
                or document.head_revision_id != revision.revision_id
                or revision.document_id != document.document_id
            ):
                raise ArtifactPromptAnnotationConflictError(
                    "document context is not the current head for this session"
                )
            kind_value = getattr(document.kind, "value", document.kind)
            media_type = revision.media_type.split(";", 1)[0].strip().lower()
            if (
                kind_value != "html"
                and media_type not in {"text/html", "application/xhtml+xml"}
                and Path(revision.filename).suffix.lower() not in {".html", ".htm", ".xhtml"}
            ):
                raise RpcHandlerError(
                    ArtifactProductErrorCode.RESOURCE_UNSUPPORTED.value,
                    "This file cannot be edited here.",
                    retryable=False,
                    accepted=False,
                )
            followup_focus = await load_followup_focus(
                session_id=session_id,
                document_id=document.document_id,
            )
            base_document_context_prompt = (
                "<active_document_context>\n"
                "The currently opened HTML document is bound to this turn. If the user asks "
                "to inspect or modify the open page, use the bound document tools, not "
                "workspace file tools. The first source read MUST be document_read with "
                'view=source and no cursor, or cursor="" only when the provider adapter '
                "requires that field. Never invent a non-empty cursor: only pass the exact "
                "nextCursor returned by the preceding document_read response when hasMore "
                "is true. To modify the open page, call document_patch with the sha256 "
                "returned by document_read and exact, unique expectedText from the returned "
                "source. write_file, edit_file, and apply_patch operate on workspace files; "
                "they do not update this Document and MUST NOT substitute for document_patch. "
                "Those workspace mutators are unavailable while this Document is bound; do "
                "not attempt to call them.\n"
                "</active_document_context>"
            )
            artifact_turn_context = BoundDocumentContext(
                session_key=key,
                session_id=session_id,
                document_id=document.document_id,
                revision_id=revision.revision_id,
                artifact_format="html",
                tool_names=DOCUMENT_CONTEXT_TOOL_NAMES,
                operation_class="document_edit",
                request_context_prompt="\n\n".join(
                    part for part in (base_document_context_prompt, followup_focus) if part
                ),
            )
            artifact_event_emitter = event_emitter_factory(key)
        except ArtifactPromptAnnotationNotFoundError as exc:
            raise logged_artifact_product_error(
                ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
                exc,
                operation="document_context.bind",
                retryable=True,
                session_key=key,
            ) from exc
        except ArtifactPromptAnnotationConflictError as exc:
            raise logged_artifact_product_error(
                ArtifactProductErrorCode.DOCUMENT_UNAVAILABLE,
                exc,
                operation="document_context.bind",
                retryable=False,
                session_key=key,
            ) from exc

    return ArtifactBinding(
        artifact_turn_context,
        artifact_session_service,
        artifact_event_emitter,
        prompt_annotation_rows,
        prepared_prompt_annotation_targets,
        prompt_annotation_snapshots,
    )


async def prepare_route(
    command: AdmitTurn,
    *,
    session: SessionNode,
    key: str,
    session_id: str,
    atomic_intent_plan: PreparedSessionIntent | None,
    binding: ArtifactBinding,
    workspace_guard: ProjectWorkspaceGuard | None,
    storage: SessionStorage,
    sessions: SessionManager,
    config: GatewayConfig,
    principal: Principal,
    conn_id: str,
    media_root: str | Path,
    preview_service: object | None,
    effective_agent_id: Callable[[SessionNode, str], str],
    run_mode_hint: RunMode | None,
    elevated_hint: str | None,
    guest_safe: bool,
    guest_profile_factory: Callable[[str], GuestProfile],
    event_emitter_factory: Callable[[str], ArtifactEventEmitter],
    candidate_loop_supported: Callable[[object], bool],
    source_only_context: Callable[[BoundPromptAnnotationContext], BoundPromptAnnotationContext],
    authority_scope: AdmissionAuthorityScope | None,
) -> PreparedRuntimeRoute:
    """Bind authority, workspace and native services without accepting a turn."""
    artifact_turn_context = binding.context
    artifact_session_service = binding.service
    artifact_event_emitter = binding.event_emitter
    from opensquilla.agents.scope import resolve_agent_workspace_dir
    from opensquilla.gateway.routing import (
        build_cli_route_envelope,
        build_web_route_envelope,
    )

    agent_id = effective_agent_id(session, key)
    workspace_path = resolve_agent_workspace_dir(agent_id, config)
    configured_workspace_dir = str(workspace_path) if workspace_path is not None else None
    workspace_dir = configured_workspace_dir
    turn_id = uuid.uuid4().hex
    guest_profile = None
    capability_report = None
    accepted_run_mode_override = None
    accepted_run_mode_origin: dict[str, Any] | None = None
    if guest_safe:
        capability_report = await current_sandbox_capability_report(config)
        try:
            resolve_mode(RunMode.SAFE, principal, capability_report)
        except ModeResolutionError as exc:
            raise RpcHandlerError(
                "SANDBOX_UNAVAILABLE",
                "Safe mode is unavailable for this unauthenticated request.",
                details={"reason": exc.code, **capability_report.to_payload()},
            ) from exc
        try:
            guest_profile = guest_profile_factory(turn_id)
        except GuestProfileBoundaryError as exc:
            raise RpcHandlerError(
                exc.code,
                "The managed Web guest workspace is unavailable.",
            ) from exc
        run_context = guest_profile.run_context()
        authoritative_guard = None
    else:
        try:
            run_context, authoritative_guard = await authoritative_project_run_context(
                storage=storage,
                session_manager=sessions,
                session=session,
                config=config,
                default_workspace=workspace_dir,
            )
        except ProjectWorkspaceStateError as exc:
            raise map_project_workspace_error(exc, owner=principal.is_owner) from exc
        if authoritative_guard is not None:
            workspace_guard = authoritative_guard
        if not guest_safe and principal_has_host_execute(principal):
            global_mode, global_source = await resolve_default_run_mode(
                sessions,
                config,
            )
            accepted_run_mode_override = AcceptedRunModeOverride(
                run_mode=global_mode,
                run_mode_source="operator_default",
                source=global_source,
            )
            run_context = apply_accepted_run_mode_override(
                run_context,
                accepted_run_mode_override,
            )
        run_context = replace(
            run_context,
            run_mode=coerce_run_mode_for_principal(run_context.run_mode, principal),
        )
    if run_mode_hint is not None:
        accepted_run_mode_override = AcceptedRunModeOverride(
            run_mode=run_mode_hint,
            run_mode_source="user",
            source="request",
        )
        run_context = apply_accepted_run_mode_override(
            run_context,
            accepted_run_mode_override,
        )
        current_origin = getattr(session, "origin", None)
        accepted_run_mode_origin = {
            **(current_origin if isinstance(current_origin, dict) else {}),
            RUN_CONTEXT_ORIGIN_KEY: run_context.to_origin_payload(),
        }
        if atomic_intent_plan is None:
            update_session = getattr(sessions, "update", None)
            if callable(update_session):
                session = await update_session(
                    key,
                    origin=accepted_run_mode_origin,
                )
    if run_context.run_mode is RunMode.FULL:
        mode_resolution = ResolvedMode(
            desired_mode=RunMode.FULL,
            effective_mode=RunMode.FULL,
        )
    else:
        if capability_report is None:
            capability_report = await current_sandbox_capability_report(config)
        try:
            mode_resolution = resolve_mode(
                run_context.run_mode,
                principal,
                capability_report,
            )
        except ModeResolutionError as exc:
            raise RpcHandlerError(
                "SANDBOX_MODE_UNAVAILABLE",
                "The requested execution mode is unavailable.",
                details={"reason": exc.code, **capability_report.to_payload()},
            ) from exc

    workspace_dir = run_context.workspace or workspace_dir
    host_execute_allowed = principal_has_host_execute(principal)
    session_epoch = int(getattr(session, "epoch", 0) or 0)
    if command.source.caller_kind == "cli" or command.source.channel_kind == "cli":
        route_envelope = build_cli_route_envelope(
            session_key=key,
            agent_id=agent_id,
            source_name=command.source.source_name or "rpc",
            channel_id=command.source.channel_id or "cli:rpc",
            sender_id=command.source.sender_id,
            session_id=getattr(session, "session_id", None),
            session_epoch=session_epoch,
            principal_is_owner=principal.is_owner,
            principal_host_execute=host_execute_allowed,
            run_mode=run_context.run_mode.value,
        )
    else:
        route_envelope = build_web_route_envelope(
            session_key=key,
            agent_id=agent_id,
            conn_id=conn_id,
            sender_id=command.source.sender_id,
            channel_id=command.source.channel_id or f"web:{conn_id}",
            source_name=command.source.source_name or "RPC",
            tool_source_kind=command.source.source_kind,
            session_id=getattr(session, "session_id", None),
            session_epoch=session_epoch,
            principal_is_owner=principal.is_owner,
            principal_host_execute=host_execute_allowed,
        )
    apply_run_context_route_metadata(
        route_envelope,
        run_context,
        principal_is_owner=principal.is_owner,
    )
    route_envelope.metadata["sandbox_mode_resolution"] = mode_resolution.to_payload()
    if guest_profile is not None:
        route_envelope.metadata["guest_safe"] = True
        route_envelope.metadata["guest_profile_root"] = str(guest_profile.root)
        route_envelope.metadata["guest_managed_root"] = str(guest_profile.managed_root)
        route_envelope.metadata["guest_environment"] = dict(guest_profile.environment)
        route_envelope.runtime_services["guest_profile_factory"] = lambda task_id: (
            guest_profile_factory(task_id)
        )
    if artifact_turn_context is not None and artifact_session_service is not None:
        route_envelope.runtime_services["artifact_context"] = artifact_turn_context
        route_envelope.runtime_services["artifact_session"] = artifact_session_service
        route_envelope.runtime_services["artifact_event_emitter"] = artifact_event_emitter
        if preview_service is not None:
            route_envelope.runtime_services["artifact_preview_service"] = preview_service
        from opensquilla.gateway.artifact_contexts import BoundPromptAnnotationContext

        if (
            isinstance(artifact_turn_context, BoundPromptAnnotationContext)
            and route_envelope.source_kind.value == "web"
            and route_envelope.interaction_mode.value == "interactive"
            and principal.is_owner
            and not guest_safe
        ):
            from opensquilla.gateway.desktop_artifact_bridge import (
                TurnAuthorityCleanup,
                get_desktop_artifact_bridge_client,
            )

            try:
                desktop_artifact_bridge = get_desktop_artifact_bridge_client()
            except ValueError:
                # An incomplete or non-loopback Desktop environment must never
                # turn into ambient bridge authority. Artifact source editing
                # remains available and native-surface operations fail closed.
                log.warning("artifact.desktop_bridge_environment_rejected")
                desktop_artifact_bridge = None
            if desktop_artifact_bridge is not None:
                # Negotiate before route construction so a legacy, unavailable,
                # or non-HTML shell receives the source-only five-tool contract.
                # Protocol version alone is insufficient: without an active
                # v4 browser inspect surface plus candidate bind/restore, the
                # ten-tool loop could stage a draft that can never be committed.
                # Keep the useful durable source-writer compatibility path
                # rather than exposing a candidate that can only be discarded.
                turn_authority_cleanup = None
                try:
                    bound_desktop_artifact_bridge = await desktop_artifact_bridge.acquire_binding()
                    turn_authority_cleanup = (
                        TurnAuthorityCleanup(bound_desktop_artifact_bridge.aclose)
                        if bound_desktop_artifact_bridge is not None
                        else None
                    )
                    if turn_authority_cleanup is not None and authority_scope is not None:
                        authority_scope.register(turn_authority_cleanup)
                    bridge_capabilities = (
                        await bound_desktop_artifact_bridge.capabilities()
                        if bound_desktop_artifact_bridge is not None
                        else None
                    )
                except Exception:  # noqa: BLE001 - preserve unavailable bridge semantics
                    bound_desktop_artifact_bridge = None
                    bridge_capabilities = None
                if (
                    not candidate_loop_supported(bridge_capabilities)
                    or turn_authority_cleanup is None
                ):
                    if turn_authority_cleanup is not None:
                        await turn_authority_cleanup.aclose()
                    artifact_turn_context = source_only_context(artifact_turn_context)
                else:
                    route_envelope.runtime_services["desktop_artifact_bridge"] = (
                        bound_desktop_artifact_bridge
                    )
                    route_envelope.runtime_services["turn_authority_cleanup"] = (
                        turn_authority_cleanup
                    )
                    route_envelope.runtime_services.setdefault("turn_cleanup_callbacks", []).append(
                        turn_authority_cleanup.aclose
                    )
            else:
                # A browser-less web client must not receive the autonomous
                # writer/finish contract.  It can still use the durable
                # source-only editor, while the v4 Electron client gets the
                # full candidate-preview loop above.
                artifact_turn_context = source_only_context(artifact_turn_context)
            # The initial runtime-service entry is made before bridge
            # negotiation.  Replace it after any downgrade so routing creates
            # the matching legacy mutation controller, not a dead candidate
            # controller.
            route_envelope.runtime_services["artifact_context"] = artifact_turn_context
    if (
        route_envelope.source_kind.value == "web"
        and route_envelope.interaction_mode.value == "interactive"
        and principal.is_owner
        and not guest_safe
    ):
        try:
            from opensquilla.artifacts import ArtifactStore
            from opensquilla.gateway.generated_artifact_adoption import (
                GeneratedArtifactAdopter,
            )

            if artifact_session_service is None:
                artifact_session_service = await ArtifactSessionService.from_session_storage(
                    storage
                )
            route_envelope.runtime_services["generated_artifact_adopter"] = (
                GeneratedArtifactAdopter(
                    service=artifact_session_service,
                    store=ArtifactStore(media_root),
                    session_key=key,
                    session_id=session_id,
                    event_emitter=(
                        artifact_event_emitter
                        if callable(artifact_event_emitter)
                        else event_emitter_factory(key)
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - adoption is a recoverable enhancement
            log.warning(
                "generated_artifact_adopter_unavailable",
                session_key=key,
                error_type=type(exc).__name__,
            )
    if elevated_hint is not None:
        route_envelope.metadata["elevated"] = elevated_hint

    binding.context = artifact_turn_context
    binding.service = artifact_session_service
    binding.event_emitter = artifact_event_emitter
    return PreparedRuntimeRoute(
        agent_id,
        route_envelope,
        turn_id,
        run_context,
        mode_resolution,
        guest_profile,
        accepted_run_mode_override,
        accepted_run_mode_origin,
        workspace_guard,
        session,
        configured_workspace_dir,
        host_execute_allowed,
    )
