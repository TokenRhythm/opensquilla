"""Prepare the normal session route, workspace, and optional user page context."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.application.turn_admission import AdmitTurn
from opensquilla.artifact_session import (
    ArtifactSessionService,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
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
    page_context_text: str | None = None




async def prepare_route(
    command: AdmitTurn,
    *,
    session: SessionNode,
    key: str,
    session_id: str,
    atomic_intent_plan: PreparedSessionIntent | None,
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
    page_context_resolver: Callable[..., Awaitable[dict[str, Any]]],
) -> PreparedRuntimeRoute:
    """Bind authority, workspace and native services without accepting a turn."""
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
    page_context_text = None
    if command.page_context is not None:
        from opensquilla.gateway.page_context import render_page_context

        resolved_context = await page_context_resolver(
            command.page_context,
            session_key=key,
            session_id=session_id,
            workspace=workspace_dir,
        )
        page_context_text = render_page_context(resolved_context)
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

            artifact_session_service = await ArtifactSessionService.from_session_storage(storage)
            route_envelope.runtime_services["generated_artifact_adopter"] = (
                GeneratedArtifactAdopter(
                    service=artifact_session_service,
                    store=ArtifactStore(media_root),
                    session_key=key,
                    session_id=session_id,
                    event_emitter=event_emitter_factory(key),
                    workspace=workspace_dir,
                    preview_service=preview_service,
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
        page_context_text,
    )
