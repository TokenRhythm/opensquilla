"""Owner-only RPC lifecycle for persisted project workspaces."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from typing import Any, cast

from opensquilla.engine.steps.router_decision_record import (
    drain_pending_flushes_for_sessions,
)
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.session_services import get_session_lock, get_session_storage
from opensquilla.gateway.subagent_announce import (
    quiesce_background_completion_sessions,
)
from opensquilla.project_workspaces import (
    adopt_legacy_project_workspaces,
    project_workspace_payload,
    resolve_project_path,
)
from opensquilla.session.models import ProjectWorkspace
from opensquilla.session.storage import ProjectSessionSnapshotMismatchError

_d = get_dispatcher()


async def _settle_despite_cancellation[T](awaitable: Awaitable[T]) -> T:
    """Settle one irreversible operation before propagating caller cancellation."""

    operation = asyncio.ensure_future(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    if cancellation is not None:
        with contextlib.suppress(BaseException):
            operation.result()
        raise cancellation
    return operation.result()


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
    return cast(ProjectWorkspace, workspace)


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
    scheduler = getattr(ctx, "cron_scheduler", None)
    affected_job_ids: list[str] = []
    if scheduler is not None:
        jobs = await scheduler.list_jobs()
        affected = [
            job
            for job in jobs
            if (getattr(job, "payload", None) or {}).get("_workspace_id") == workspace_id
        ]
        for job in affected:
            payload = dict(job.payload)
            payload["_workspace_unavailable"] = "removed"
            await scheduler.update_job(job.id, payload=payload)
            await scheduler.pause_job(job.id)
            affected_job_ids.append(job.id)
    await storage.remove_project_workspace(workspace_id)
    return {
        "removed": True,
        "workspaceId": workspace_id,
        "pausedCronJobIds": affected_job_ids,
        "pausedCronJobCount": len(affected_job_ids),
    }


@_d.method("workspaces.history.delete", scope="operator.write")
async def _handle_workspaces_history_delete(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    workspace_id = _workspace_id(params)
    storage = _storage(ctx)

    async def _delete_fenced_history() -> dict[str, Any]:
        while True:
            candidate_keys = await storage.list_project_workspace_session_keys(
                workspace_id
            )
            async with AsyncExitStack() as fences:
                # A child terminal tail can schedule a parent wake, so install
                # this fence before cancelling/draining TaskRuntime drivers.
                await fences.enter_async_context(
                    quiesce_background_completion_sessions(candidate_keys)
                )

                task_runtime = getattr(ctx, "task_runtime", None)
                quiesce_runtime = getattr(task_runtime, "quiesce_sessions", None)
                if callable(quiesce_runtime):
                    await fences.enter_async_context(
                        quiesce_runtime(candidate_keys)
                    )

                await fences.enter_async_context(
                    get_agent_task_registry().quiesce_sessions(candidate_keys)
                )

                for session_key in sorted(candidate_keys):
                    lock = get_session_lock(ctx.turn_runner, session_key)
                    if lock is not None:
                        await fences.enter_async_context(lock)

                # These fire-and-forget durable writes are outside the driver
                # tasks above. Let matching work settle naturally: cancelling a
                # wrapper cannot stop an underlying writer thread.
                await drain_pending_flushes_for_sessions(candidate_keys)
                drain_turn_writes = getattr(
                    ctx.turn_runner,
                    "drain_session_background_writes",
                    None,
                )
                if callable(drain_turn_writes):
                    await drain_turn_writes(candidate_keys)

                session_ids: dict[str, str] = {}
                for session_key in candidate_keys:
                    node = await storage.get_session(session_key)
                    session_id = getattr(node, "session_id", None)
                    if isinstance(session_id, str) and session_id:
                        session_ids[session_key] = session_id

                try:
                    deleted = await storage.delete_project_workspace_sessions(
                        workspace_id,
                        expected_session_keys=candidate_keys,
                    )
                except ProjectSessionSnapshotMismatchError:
                    # Release this stale generation of every fence, then
                    # resnapshot the whole project and retry.
                    continue
                except KeyError as exc:
                    raise RpcHandlerError(
                        "WORKSPACE_NOT_FOUND",
                        "Project workspace not found.",
                    ) from exc

                evict_runtime_state = getattr(
                    ctx.session_manager,
                    "evict_session_runtime_state",
                    None,
                )
                if callable(evict_runtime_state):
                    for session_key in deleted:
                        evict_runtime_state(
                            session_key,
                            session_id=session_ids.get(session_key),
                        )

                return {
                    "workspaceId": workspace_id,
                    # Counts every deleted project session: roots and children.
                    "deletedTaskCount": len(deleted),
                    "deletedSessionKeys": deleted,
                }

    return await _settle_despite_cancellation(_delete_fenced_history())


__all__ = [
    "_handle_workspaces_history_delete",
    "_handle_workspaces_list",
    "_handle_workspaces_open",
    "_handle_workspaces_pin",
    "_handle_workspaces_remove",
    "_handle_workspaces_update",
]
