"""Project-workspace path handling, projection, and legacy-session adoption."""

from __future__ import annotations

import asyncio
import os
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.models import ProjectWorkspace


@dataclass(frozen=True)
class ResolvedProjectPath:
    path: str
    path_key: str
    name: str


ProjectWorkspaceStateReason = Literal[
    "not_found",
    "removed",
    "untrusted",
    "unavailable",
    "canonical_changed",
    "guard_required",
    "binding_changed",
]


class ProjectWorkspaceStateError(RuntimeError):
    def __init__(self, reason: ProjectWorkspaceStateReason) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ProjectWorkspaceGuard:
    workspace_id: str
    path: str
    path_key: str


@dataclass(frozen=True)
class ValidatedProjectWorkspace:
    workspace: ProjectWorkspace
    canonical_path: str
    guard: ProjectWorkspaceGuard


def _normalized_path(candidate: Path) -> str:
    return unicodedata.normalize("NFC", str(candidate))


def project_path_key(value: str | Path, *, strict: bool = False) -> str:
    candidate = Path(value).expanduser().resolve(strict=strict)
    return os.path.normcase(_normalized_path(candidate)).replace("\\", "/")


def resolve_project_path(value: Any) -> ResolvedProjectPath:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("workspace_path_required")
    try:
        candidate = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("workspace_not_found") from exc
    if not candidate.is_dir():
        raise ValueError("workspace_not_directory")
    if candidate.parent == candidate:
        raise ValueError("workspace_root_not_allowed")
    normalized = _normalized_path(candidate)
    return ResolvedProjectPath(
        path=normalized,
        path_key=os.path.normcase(normalized).replace("\\", "/"),
        name=candidate.name or normalized,
    )


def _legacy_project_path(value: str) -> ResolvedProjectPath | None:
    try:
        candidate = Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if candidate.parent == candidate:
        return None
    normalized = _normalized_path(candidate)
    return ResolvedProjectPath(
        path=normalized,
        path_key=os.path.normcase(normalized).replace("\\", "/"),
        name=candidate.name or normalized,
    )


def workspace_is_available(workspace: ProjectWorkspace) -> bool:
    try:
        _validate_stored_project_path(workspace)
    except ProjectWorkspaceStateError:
        return False
    return True


def _validate_stored_project_path(workspace: ProjectWorkspace) -> str:
    try:
        candidate = Path(workspace.path).expanduser().resolve(strict=True)
        if not candidate.is_dir() or candidate.parent == candidate:
            raise ProjectWorkspaceStateError("unavailable")
        with os.scandir(candidate):
            pass
        canonical = _normalized_path(candidate)
        if project_path_key(candidate, strict=True) != workspace.path_key:
            raise ProjectWorkspaceStateError("canonical_changed")
        if canonical != workspace.path:
            raise ProjectWorkspaceStateError("canonical_changed")
        return canonical
    except ProjectWorkspaceStateError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectWorkspaceStateError("unavailable") from exc


async def resolve_validated_project_workspace(
    storage: Any,
    workspace_id: str,
) -> ValidatedProjectWorkspace:
    workspace = await storage.get_project_workspace(workspace_id)
    if workspace is None:
        raise ProjectWorkspaceStateError("not_found")
    if workspace.removed_at is not None:
        raise ProjectWorkspaceStateError("removed")
    if workspace.trusted_at is None:
        raise ProjectWorkspaceStateError("untrusted")
    canonical_path = await asyncio.to_thread(
        _validate_stored_project_path,
        workspace,
    )
    return ValidatedProjectWorkspace(
        workspace=workspace,
        canonical_path=canonical_path,
        guard=ProjectWorkspaceGuard(
            workspace_id=workspace.workspace_id,
            path=canonical_path,
            path_key=workspace.path_key,
        ),
    )


async def project_workspace_payload(storage: Any, workspace: ProjectWorkspace) -> dict[str, Any]:
    availability_reason: ProjectWorkspaceStateReason | None = None
    try:
        validated = await resolve_validated_project_workspace(
            storage,
            workspace.workspace_id,
        )
        workspace = validated.workspace
    except ProjectWorkspaceStateError as exc:
        availability_reason = exc.reason
    payload = {
        "id": workspace.workspace_id,
        "name": workspace.display_name,
        "path": workspace.path,
        "taskCount": await storage.count_project_workspace_tasks(
            workspace.workspace_id
        ),
        "pinned": workspace.pinned_at is not None,
        "available": availability_reason is None,
    }
    if availability_reason is not None:
        payload["availabilityReason"] = availability_reason
    return payload


async def adopt_legacy_project_workspaces(
    storage: Any,
    config: Any,
    *,
    now_ms: int | None = None,
) -> None:
    """Bind pre-feature sessions whose persisted workspace is non-default."""

    async def _adopt() -> None:
        clock = int(time.time() * 1000) if now_ms is None else int(now_ms)
        after_rowid = 0
        page_size = 500
        while True:
            candidates = await storage.list_legacy_project_workspace_candidates(
                after_rowid=after_rowid,
                limit=page_size,
            )
            if not candidates:
                return
            for rowid, session_key, agent_id, origin in candidates:
                after_rowid = rowid
                if not isinstance(origin, dict):
                    continue
                run_context = origin.get(RUN_CONTEXT_ORIGIN_KEY)
                if not isinstance(run_context, dict):
                    continue
                raw_workspace = run_context.get("workspace")
                if not isinstance(raw_workspace, str) or not raw_workspace.strip():
                    continue
                resolved = _legacy_project_path(raw_workspace)
                if resolved is None:
                    continue
                default_key = project_path_key(
                    resolve_agent_workspace_dir(agent_id, config),
                    strict=False,
                )
                if resolved.path_key == default_key:
                    continue
                await storage.adopt_legacy_session_workspace(
                    session_key,
                    expected_agent_id=agent_id,
                    expected_origin=origin,
                    path=resolved.path,
                    path_key=resolved.path_key,
                    display_name=resolved.name,
                    trusted_at=clock,
                    now_ms=clock,
                )
            if len(candidates) < page_size:
                return

    run_once = getattr(storage, "run_legacy_project_adoption_once", None)
    if callable(run_once):
        await run_once(_adopt)
    else:
        await _adopt()


__all__ = [
    "ProjectWorkspaceGuard",
    "ProjectWorkspaceStateError",
    "ProjectWorkspaceStateReason",
    "ResolvedProjectPath",
    "ValidatedProjectWorkspace",
    "adopt_legacy_project_workspaces",
    "project_path_key",
    "project_workspace_payload",
    "resolve_project_path",
    "resolve_validated_project_workspace",
    "workspace_is_available",
]
