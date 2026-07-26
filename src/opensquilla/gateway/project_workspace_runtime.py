"""Authoritative project-workspace resolution at runtime boundaries."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.project_workspaces import (
    ProjectWorkspaceGuard,
    ProjectWorkspaceStateError,
    ValidatedProjectWorkspace,
    resolve_validated_project_workspace,
)
from opensquilla.sandbox.run_context import (
    RunContext,
    effective_project_run_mode,
    get_run_context,
)
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage

_NOT_FOUND_REASONS = frozenset({"not_found", "removed", "untrusted"})


@dataclass(frozen=True)
class AcceptedRunModeOverride:
    """Ingress-vetted per-turn mode kept outside mutable route metadata."""

    run_mode: RunMode
    run_mode_source: str | None
    source: str


def apply_accepted_run_mode_override(
    context: RunContext,
    override: Any,
) -> RunContext:
    """Overlay only vetted mode provenance onto a freshly resolved context."""

    if not isinstance(override, AcceptedRunModeOverride):
        return context
    return replace(
        context,
        run_mode=override.run_mode,
        run_mode_source=override.run_mode_source,
        source=override.source,
    )


async def resolve_session_project_workspace(
    storage: SessionStorage,
    session: SessionNode,
) -> ValidatedProjectWorkspace | None:
    workspace_id = getattr(session, "workspace_id", None)
    if not workspace_id:
        return None
    return await resolve_validated_project_workspace(storage, workspace_id)


async def authoritative_project_run_context(
    *,
    storage: SessionStorage,
    session_manager: Any,
    session: SessionNode,
    config: Any,
    default_workspace: str | None,
) -> tuple[RunContext, ProjectWorkspaceGuard | None]:
    context = await get_run_context(
        session_manager,
        session.session_key,
        config=config,
        workspace=default_workspace,
        session_node=session,
    )
    validated = await resolve_session_project_workspace(storage, session)
    if validated is None:
        return context, None
    return (
        replace(
            effective_project_run_mode(context, config),
            workspace=validated.canonical_path,
        ),
        validated.guard,
    )


async def project_workspace_snapshot(
    storage: SessionStorage,
    session: SessionNode,
) -> dict[str, Any] | None:
    workspace_id = getattr(session, "workspace_id", None)
    if not workspace_id:
        return None
    workspace = await storage.get_project_workspace(workspace_id)
    if workspace is None:
        return {
            "id": workspace_id,
            "name": None,
            "path": None,
            "available": False,
            "removed": False,
            "availabilityReason": "not_found",
        }
    if workspace.removed_at is not None:
        return {
            "id": workspace.workspace_id,
            "name": workspace.display_name,
            "path": workspace.path,
            "available": False,
            "removed": True,
            "availabilityReason": "removed",
        }

    availability_reason: str | None = None
    try:
        validated = await resolve_validated_project_workspace(
            storage,
            workspace.workspace_id,
        )
        workspace = validated.workspace
        path = validated.canonical_path
    except ProjectWorkspaceStateError as exc:
        availability_reason = exc.reason
        path = workspace.path
    return {
        "id": workspace.workspace_id,
        "name": workspace.display_name,
        "path": path,
        "available": availability_reason is None,
        "removed": False,
        "availabilityReason": availability_reason,
    }


def map_project_workspace_error(
    error: ProjectWorkspaceStateError,
    *,
    owner: bool,
) -> RpcHandlerError:
    del owner
    if error.reason in _NOT_FOUND_REASONS:
        return RpcHandlerError(
            "WORKSPACE_NOT_FOUND",
            "Project workspace not found.",
            details={"reason": error.reason},
        )
    return RpcHandlerError(
        "WORKSPACE_UNAVAILABLE",
        "The project directory is unavailable.",
        details={"reason": error.reason},
    )


__all__ = [
    "AcceptedRunModeOverride",
    "apply_accepted_run_mode_override",
    "authoritative_project_run_context",
    "map_project_workspace_error",
    "project_workspace_snapshot",
    "resolve_session_project_workspace",
]
