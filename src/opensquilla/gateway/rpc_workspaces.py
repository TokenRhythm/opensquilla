"""Owner-only RPC lifecycle for persisted project workspaces."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any, cast

from opensquilla.engine.steps.router_decision_record import (
    drain_pending_flushes_for_sessions,
)
from opensquilla.gateway.adapters.workspace_catalog_contract import (
    register_workspace_catalog_contract,
)
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.session_services import get_session_lock, get_session_storage
from opensquilla.gateway.subagent_announce import (
    quiesce_background_completion_sessions,
)
from opensquilla.git_runtime import GitRunResult
from opensquilla.project_workspaces import (
    ProjectWorkspaceStateError,
    adopt_legacy_project_workspaces,
    project_workspace_payload,
    resolve_project_path,
    resolve_validated_project_workspace,
)
from opensquilla.session.models import ProjectWorkspace
from opensquilla.session.storage import ProjectSessionSnapshotMismatchError
from opensquilla.workspace_commit_message import (
    WorkspaceCommitMessageError,
    draft_workspace_commit_message,
)
from opensquilla.workspace_git_changes import (
    WorkspaceGitPreconditionError,
    WorkspaceGitUnavailableError,
    WorkspacePathError,
    commit_index,
    discard_paths,
    is_untracked_path,
    normalize_repo_path,
    push_current_branch,
    read_staged_index_diff,
    read_workspace_changes,
    read_workspace_diff,
    stage_paths,
    undo_last_commit,
)

_d = get_dispatcher()

_MAX_GIT_ERROR_CHARS = 400

# A refused write names what the operator should do next, so each precondition
# gets its own wire code instead of one "failed".
_PRECONDITION_ERROR_CODES = {
    "untracked_path": "UNTRACKED_PATH",
    "nothing_staged": "NOTHING_STAGED",
    "no_upstream": "NO_UPSTREAM",
    "commit_published": "COMMIT_PUBLISHED",
    "no_parent": "NOTHING_TO_UNDO",
}


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


_WORKSPACE_STATE_NOT_FOUND_REASONS = frozenset({"not_found", "removed"})


def _workspace_state_error(exc: ProjectWorkspaceStateError) -> RpcHandlerError:
    if exc.reason in _WORKSPACE_STATE_NOT_FOUND_REASONS:
        return RpcHandlerError("WORKSPACE_NOT_FOUND", "Project workspace not found.")
    return RpcHandlerError(
        "UNAVAILABLE",
        f"Project workspace is unavailable ({exc.reason}).",
    )


async def _git_workspace_path(ctx: RpcContext, workspace_id: str) -> str:
    """Resolve the trusted canonical path a Git read may run against."""

    storage = _storage(ctx)
    try:
        validated = await resolve_validated_project_workspace(storage, workspace_id)
    except ProjectWorkspaceStateError as exc:
        raise _workspace_state_error(exc) from exc
    return validated.canonical_path


async def _handle_workspaces_git_status(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    workspace_id = _workspace_id(params)
    workspace_path = await _git_workspace_path(ctx, workspace_id)
    # Git runs in a worker thread: a cold `status` on a large tree must not
    # stall the Gateway event loop that also serves chat and event streams.
    changes = await asyncio.to_thread(read_workspace_changes, workspace_path)
    return {
        "available": changes.available,
        "availabilityReason": changes.availability_reason,
        "branch": changes.branch,
        "detached": changes.detached,
        "upstream": changes.upstream,
        "ahead": changes.ahead,
        "behind": changes.behind,
        "totalCount": changes.total_count,
        "truncated": changes.truncated,
        "addedLines": changes.added_lines,
        "removedLines": changes.removed_lines,
        "entries": [
            {
                "path": entry.path,
                "previousPath": entry.previous_path,
                "changeType": entry.change_type,
                "staged": entry.staged,
                "unstaged": entry.unstaged,
                "addedLines": entry.added_lines,
                "removedLines": entry.removed_lines,
            }
            for entry in changes.entries
        ],
    }


async def _handle_workspaces_git_diff(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    values = _params(params)
    workspace_id = _workspace_id(values)
    staged = values.get("staged", False)
    if not isinstance(staged, bool):
        raise RpcHandlerError("INVALID_PARAMS", "staged must be a boolean")
    raw_path = values.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise RpcHandlerError("INVALID_PARAMS", "path is required")
    try:
        repo_path = normalize_repo_path(raw_path)
    except WorkspacePathError as exc:
        # A present path that escapes the workspace is a different failure from
        # a missing parameter, and the Contract declares both codes separately.
        raise RpcHandlerError("INVALID_PATH", str(exc)) from exc
    workspace_path = await _git_workspace_path(ctx, workspace_id)
    try:
        untracked = await asyncio.to_thread(
            is_untracked_path,
            workspace_path,
            repo_path,
        )
        diff = await asyncio.to_thread(
            read_workspace_diff,
            workspace_path,
            repo_path,
            staged=staged,
            untracked=untracked,
        )
    except WorkspaceGitUnavailableError as exc:
        raise RpcHandlerError(
            "UNAVAILABLE",
            f"Git is unavailable for this workspace ({exc.reason}).",
        ) from exc
    return {
        "path": diff.path,
        "staged": diff.staged,
        "text": diff.text,
        "truncated": diff.truncated,
        "binary": diff.binary,
    }


def _git_failure_message(result: GitRunResult | None) -> str:
    """Report Git's own words, bounded, instead of a generic failure."""

    detail = ""
    if result is not None:
        detail = result.stderr_text.strip() or result.stdout_text.strip()
    detail = " ".join(detail.split())
    if not detail:
        return "Git rejected the operation."
    return f"Git rejected the operation: {detail[:_MAX_GIT_ERROR_CHARS]}"


async def _handle_workspaces_git_stage(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Stage or unstage paths in a trusted workspace's index.

    This is the workspace review surface's first write, so it follows the
    owner-facing write precedent already set by ``workspaces.update`` and
    ``sandbox.path.create-directory``: the contract declares
    ``operator.write``, the handler re-checks ownership, and the target path is
    the trusted canonical one resolved from the stored workspace rather than
    anything the caller sent.
    """

    _require_owner(ctx)
    values = _params(params)
    workspace_id = _workspace_id(values)
    staged = values.get("staged")
    if not isinstance(staged, bool):
        raise RpcHandlerError("INVALID_PARAMS", "staged must be a boolean")
    raw_paths = values.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise RpcHandlerError("INVALID_PARAMS", "paths must be a non-empty array")
    try:
        repo_paths = tuple(normalize_repo_path(path) for path in raw_paths)
    except WorkspacePathError as exc:
        # A present path that escapes the workspace is a different failure from
        # a missing parameter, and the Contract declares both codes separately.
        raise RpcHandlerError("INVALID_PATH", str(exc)) from exc
    workspace_path = await _git_workspace_path(ctx, workspace_id)
    applied = await _run_git_write(
        lambda: stage_paths(workspace_path, repo_paths, staged=staged)
    )
    return {"staged": staged, "affectedPaths": list(applied)}


def _paths_param(values: dict[str, Any]) -> tuple[str, ...]:
    raw_paths = values.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        raise RpcHandlerError("INVALID_PARAMS", "paths must be a non-empty array")
    try:
        return tuple(normalize_repo_path(path) for path in raw_paths)
    except WorkspacePathError as exc:
        # A present path that escapes the workspace is a different failure from
        # a missing parameter, and the Contract declares both codes separately.
        raise RpcHandlerError("INVALID_PATH", str(exc)) from exc


async def _run_git_write[T](operation: Callable[[], T]) -> T:
    """Run one workspace write in a worker thread and map its failures.

    Every write on this surface fails in the same three shapes, so the mapping
    lives here: Git is absent, Git ran and refused (reported in its own words),
    or the request was refused before it could change anything.
    """

    try:
        return await asyncio.to_thread(operation)
    except WorkspaceGitPreconditionError as exc:
        raise RpcHandlerError(
            _PRECONDITION_ERROR_CODES[exc.code],
            exc.message,
        ) from exc
    except WorkspaceGitUnavailableError as exc:
        if exc.reason != "failed":
            raise RpcHandlerError(
                "UNAVAILABLE",
                f"Git is unavailable for this workspace ({exc.reason}).",
            ) from exc
        raise RpcHandlerError("GIT_FAILED", _git_failure_message(exc.result)) from exc


async def _handle_workspaces_git_discard(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    values = _params(params)
    workspace_id = _workspace_id(values)
    repo_paths = _paths_param(values)
    workspace_path = await _git_workspace_path(ctx, workspace_id)
    discarded = await _run_git_write(
        lambda: discard_paths(workspace_path, repo_paths)
    )
    return {"discardedPaths": list(discarded)}


async def _handle_workspaces_git_commit(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    _require_owner(ctx)
    values = _params(params)
    workspace_id = _workspace_id(values)
    raw_message = values.get("message")
    if not isinstance(raw_message, str) or not raw_message.strip():
        raise RpcHandlerError("INVALID_PARAMS", "message is required")
    workspace_path = await _git_workspace_path(ctx, workspace_id)
    sha, subject = await _run_git_write(
        lambda: commit_index(workspace_path, raw_message)
    )
    return {"sha": sha, "subject": subject}


async def _handle_workspaces_git_commit_message_draft(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Draft a commit message for the staged index.

    Declared ``operator.read`` because it is a read of the workspace plus a
    text answer: it changes no file, stages nothing, and commits nothing. The
    patch it describes comes from the trusted canonical workspace path, never
    from the request, and the model is the already-connected one.
    """

    _require_owner(ctx)
    workspace_id = _workspace_id(_params(params))
    workspace_path = await _git_workspace_path(ctx, workspace_id)
    # Mapped here rather than through `_run_git_write`: that helper reports a
    # refused Git process as GIT_FAILED, which is the right code for a write that
    # did not happen and is not declared for this read.
    try:
        staged = await asyncio.to_thread(read_staged_index_diff, workspace_path)
    except WorkspaceGitPreconditionError as exc:
        raise RpcHandlerError(_PRECONDITION_ERROR_CODES[exc.code], exc.message) from exc
    except WorkspaceGitUnavailableError as exc:
        raise RpcHandlerError(
            "UNAVAILABLE",
            f"Git is unavailable for this workspace ({exc.reason}).",
        ) from exc
    try:
        # `truncated` travels with the patch: a description of a patch the
        # transport already cut must not present its file list as complete.
        draft = await draft_workspace_commit_message(
            ctx,
            staged.text,
            diff_truncated=staged.truncated,
        )
    except WorkspaceCommitMessageError as exc:
        # A failed draft is its own outcome: the workspace is present and Git
        # answered, so reporting UNAVAILABLE would send the operator looking
        # for a problem that is not there.
        raise RpcHandlerError("COMMIT_MESSAGE_FAILED", exc.message) from exc
    return {"subject": draft.subject, "body": draft.body}


async def _handle_workspaces_git_push(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Push the current branch to the upstream the status read already named.

    The upstream is re-read here rather than taken from the request, so a caller
    cannot choose where the branch is published.
    """

    _require_owner(ctx)
    workspace_id = _workspace_id(params)
    workspace_path = await _git_workspace_path(ctx, workspace_id)
    changes = await asyncio.to_thread(read_workspace_changes, workspace_path)
    output = await _run_git_write(
        lambda: push_current_branch(
            workspace_path,
            upstream=changes.upstream,
        )
    )
    return {"upstream": str(changes.upstream), "output": output}


async def _handle_workspaces_git_undo_commit(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Undo the tip commit, refusing one that the upstream already has.

    The upstream and the ahead count come from the status read here rather than
    from the request, so a caller cannot talk the guard out of the way.
    """

    _require_owner(ctx)
    workspace_id = _workspace_id(params)
    workspace_path = await _git_workspace_path(ctx, workspace_id)
    changes = await asyncio.to_thread(read_workspace_changes, workspace_path)
    sha, subject = await _run_git_write(
        lambda: undo_last_commit(
            workspace_path,
            upstream=changes.upstream,
            ahead=changes.ahead,
        )
    )
    return {"sha": sha, "subject": subject}


_WORKSPACE_CATALOG_CONTRACT_IMPLEMENTATIONS = {
    "workspaces.list": _handle_workspaces_list,
    "workspaces.git.status": _handle_workspaces_git_status,
    "workspaces.git.diff": _handle_workspaces_git_diff,
    "workspaces.git.stage": _handle_workspaces_git_stage,
    "workspaces.git.discard": _handle_workspaces_git_discard,
    "workspaces.git.commit": _handle_workspaces_git_commit,
    "workspaces.git.commitMessage.draft": _handle_workspaces_git_commit_message_draft,
    "workspaces.git.push": _handle_workspaces_git_push,
    "workspaces.git.undoCommit": _handle_workspaces_git_undo_commit,
    "workspaces.open": _handle_workspaces_open,
    "workspaces.update": _handle_workspaces_update,
    "workspaces.pin": _handle_workspaces_pin,
    "workspaces.remove": _handle_workspaces_remove,
    "workspaces.history.delete": _handle_workspaces_history_delete,
}

_WORKSPACE_CATALOG_CONTRACT_HANDLERS = {
    method: register_workspace_catalog_contract(
        _d,
        method,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    for method, implementation in _WORKSPACE_CATALOG_CONTRACT_IMPLEMENTATIONS.items()
}


__all__ = [
    "_handle_workspaces_history_delete",
    "_handle_workspaces_list",
    "_handle_workspaces_open",
    "_handle_workspaces_pin",
    "_handle_workspaces_remove",
    "_handle_workspaces_update",
]
