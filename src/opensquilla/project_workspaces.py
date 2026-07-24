"""Project-workspace path handling, projection, and legacy-session adoption."""

from __future__ import annotations

import os
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.models import ProjectWorkspace


@dataclass(frozen=True)
class ResolvedProjectPath:
    path: str
    path_key: str
    name: str


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
        return Path(workspace.path).is_dir()
    except OSError:
        return False


async def project_workspace_payload(storage: Any, workspace: ProjectWorkspace) -> dict[str, Any]:
    return {
        "id": workspace.workspace_id,
        "name": workspace.display_name,
        "path": workspace.path,
        "taskCount": await storage.count_project_workspace_tasks(workspace.workspace_id),
        "pinned": workspace.pinned_at is not None,
        "available": workspace_is_available(workspace),
    }


async def adopt_legacy_project_workspaces(
    storage: Any,
    config: Any,
    *,
    now_ms: int | None = None,
) -> None:
    """Bind pre-feature sessions whose persisted workspace is non-default."""

    clock = int(time.time() * 1000) if now_ms is None else int(now_ms)
    offset = 0
    page_size = 500
    while True:
        sessions = await storage.list_sessions(limit=page_size, offset=offset)
        if not sessions:
            return
        for session in sessions:
            if getattr(session, "workspace_id", None):
                continue
            origin = getattr(session, "origin", None)
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
                resolve_agent_workspace_dir(
                    str(getattr(session, "agent_id", "main") or "main"),
                    config,
                ),
                strict=False,
            )
            if resolved.path_key == default_key:
                continue
            workspace = await storage.create_or_restore_project_workspace(
                path=resolved.path,
                path_key=resolved.path_key,
                display_name=resolved.name,
                trusted_at=clock,
                now_ms=clock,
            )
            await storage.bind_session_workspace(
                str(session.session_key),
                workspace.workspace_id,
            )
        if len(sessions) < page_size:
            return
        offset += page_size


__all__ = [
    "ResolvedProjectPath",
    "adopt_legacy_project_workspaces",
    "project_path_key",
    "project_workspace_payload",
    "resolve_project_path",
    "workspace_is_available",
]
