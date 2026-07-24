"""Owner-only RPC lifecycle for persisted project workspaces."""

from __future__ import annotations

import time
from typing import Any

from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.project_workspaces import (
    adopt_legacy_project_workspaces,
    project_workspace_payload,
    resolve_project_path,
)
from opensquilla.session.models import ProjectWorkspace

_d = get_dispatcher()


def _require_owner(ctx: RpcContext) -> None:
    if not ctx.principal.is_owner:
        raise RpcHandlerError(
            "OWNER_REQUIRED",
            "Project workspaces require a locally proven owner.",
        )


def _storage(ctx: RpcContext) -> Any:
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcHandlerError("UNAVAILABLE", "Session storage is unavailable.")
    return storage


def _params(params: dict | None) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise RpcHandlerError("INVALID_PARAMS", "params object required")
    return params


def _workspace_id(params: dict | None) -> str:
    value = _params(params).get("workspaceId")
    if not isinstance(value, str) or not value.strip():
        raise RpcHandlerError("INVALID_PARAMS", "workspaceId is required")
    return value.strip()


async def _active_workspace(
    storage: Any,
    workspace_id: str,
) -> ProjectWorkspace:
    workspace = await storage.get_project_workspace(workspace_id)
    if workspace is None or workspace.removed_at is not None:
        raise RpcHandlerError("WORKSPACE_NOT_FOUND", "Project workspace not found.")
    return workspace


async def _payload(storage: Any, workspace: ProjectWorkspace) -> dict[str, Any]:
    return await project_workspace_payload(storage, workspace)


@_d.method("workspaces.list", scope="operator.read")
async def _handle_workspaces_list(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    storage = _storage(ctx)
    await adopt_legacy_project_workspaces(storage, ctx.config)
    workspaces = await storage.list_project_workspaces()
    return {
        "workspaces": [await _payload(storage, workspace) for workspace in workspaces]
    }


@_d.method("workspaces.open", scope="operator.write")
async def _handle_workspaces_open(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    values = _params(params)
    if values.get("trusted") is not True:
        raise RpcHandlerError(
            "WORKSPACE_TRUST_REQUIRED",
            "Opening a project requires explicit trust.",
        )
    try:
        resolved = resolve_project_path(values.get("path"))
    except ValueError as exc:
        raise RpcHandlerError(
            "INVALID_WORKSPACE_PATH",
            str(exc),
        ) from exc
    now = int(time.time() * 1000)
    storage = _storage(ctx)
    workspace = await storage.create_or_restore_project_workspace(
        path=resolved.path,
        path_key=resolved.path_key,
        display_name=resolved.name,
        trusted_at=now,
        now_ms=now,
    )
    return {"workspace": await _payload(storage, workspace)}


@_d.method("workspaces.update", scope="operator.write")
async def _handle_workspaces_update(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    values = _params(params)
    workspace_id = _workspace_id(values)
    name = values.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RpcHandlerError("INVALID_PARAMS", "name is required")
    if len(name.strip()) > 120:
        raise RpcHandlerError("INVALID_PARAMS", "name is too long")
    storage = _storage(ctx)
    await _active_workspace(storage, workspace_id)
    workspace = await storage.update_project_workspace(
        workspace_id,
        display_name=name.strip(),
    )
    return {"workspace": await _payload(storage, workspace)}


@_d.method("workspaces.pin", scope="operator.write")
async def _handle_workspaces_pin(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    values = _params(params)
    workspace_id = _workspace_id(values)
    if not isinstance(values.get("pinned"), bool):
        raise RpcHandlerError("INVALID_PARAMS", "pinned must be a boolean")
    storage = _storage(ctx)
    await _active_workspace(storage, workspace_id)
    workspace = await storage.set_project_workspace_pin(
        workspace_id,
        pinned=values["pinned"],
    )
    return {"workspace": await _payload(storage, workspace)}


@_d.method("workspaces.remove", scope="operator.write")
async def _handle_workspaces_remove(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    workspace_id = _workspace_id(params)
    storage = _storage(ctx)
    await _active_workspace(storage, workspace_id)
    await storage.remove_project_workspace(workspace_id)
    return {"removed": True, "workspaceId": workspace_id}


@_d.method("workspaces.history.delete", scope="operator.write")
async def _handle_workspaces_history_delete(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    workspace_id = _workspace_id(params)
    storage = _storage(ctx)
    await _active_workspace(storage, workspace_id)
    deleted = await storage.delete_project_workspace_sessions(workspace_id)
    return {
        "workspaceId": workspace_id,
        "deletedTaskCount": len(deleted),
        "deletedSessionKeys": deleted,
    }


__all__ = [
    "_handle_workspaces_history_delete",
    "_handle_workspaces_list",
    "_handle_workspaces_open",
    "_handle_workspaces_pin",
    "_handle_workspaces_remove",
    "_handle_workspaces_update",
]
