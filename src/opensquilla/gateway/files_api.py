"""Workspace file browsing endpoints for the Web UI file tree.

``GET /api/v1/files`` lists one directory of a *trusted project workspace*
(non-recursive, matching the lazy-loading file tree on the client), and
``GET /api/v1/files/content`` reads a bounded text slice for preview.

Security model:
- Only workspaces registered in the project-workspace store and already
  trusted (``trusted_at`` set) are addressable, via
  :func:`opensquilla.project_workspaces.resolve_validated_project_workspace`.
  Arbitrary directories cannot be listed or read.
- Relative paths are normalized; any path that resolves outside the
  workspace root (``..`` segments, absolute paths, symlinks escaping the
  root) is rejected before any I/O.
- Dot entries (``.git``, ``.env``, ...) are excluded from listings so
  secrets and VCS internals never surface in the tree or previews.
- Root ``.gitignore`` rules are honored (last-match-wins, directory-suffix
  and negation semantics — the same compact ruleset the OpenTUI completion
  walker uses, kept self-contained here to avoid a gateway→tui import).
- All blocking I/O runs in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import forbidden_origin_response, request_origin_allowed
from opensquilla.project_workspaces import (
    ProjectWorkspaceStateError,
    resolve_validated_project_workspace,
)

log = logging.getLogger(__name__)

DEFAULT_MAX_CONTENT_BYTES = 1 * 1024 * 1024
MAX_CONTENT_BYTES_CAP = 4 * 1024 * 1024
_MAX_LIST_ENTRIES = 20_000
_BINARY_PROBE_BYTES = 8192


class WorkspacePathError(ValueError):
    """A relative path argument cannot be resolved inside the workspace."""


@dataclass(frozen=True)
class _WorkspaceRoot:
    workspace_id: str
    name: str
    root: Path


# ---------------------------------------------------------------------------
# Path normalization / containment
# ---------------------------------------------------------------------------


def normalize_workspace_rel_path(raw: str | None) -> str:
    """Normalize a client-supplied relative path to POSIX form.

    Empty/None is the workspace root. Rejects absolute paths, ``..`` and
    empty segments. Windows drive letters and backslashes are neutralized
    (the containment check in :func:`_resolve_inside_workspace` is the
    authoritative guard).
    """
    if raw is None:
        return ""
    value = str(raw).replace("\\", "/").strip()
    if not value or value in {".", "./"}:
        return ""
    if value.startswith("/"):
        raise WorkspacePathError("absolute path not allowed")
    parts: list[str] = []
    for part in value.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            # Internal ``..`` segments collapse; escaping above the workspace
            # root is rejected (and re-checked at resolve time).
            if not parts:
                raise WorkspacePathError("path traversal not allowed")
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def _resolve_inside_workspace(root: Path, rel: str) -> Path:
    """Resolve ``root/rel`` and prove the result stays inside ``root``."""
    target = (root / rel).resolve() if rel else root
    if target != root and root not in target.parents:
        raise WorkspacePathError("path escapes workspace")
    return target


# ---------------------------------------------------------------------------
# .gitignore filtering (compact, self-contained — see module docstring)
# ---------------------------------------------------------------------------


def _load_gitignore_patterns(root: Path) -> list[tuple[str, bool]]:
    gitignore = root / ".gitignore"
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    rules: list[tuple[str, bool]] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
            if not line:
                continue
        rules.append((line.lstrip("/"), negated))
    return rules


def _pattern_matches(rel: str, parts: list[str], pattern: str) -> bool:
    normalized = pattern.strip("/")
    if not normalized:
        return False
    if pattern.endswith("/") and (rel == normalized or rel.startswith(normalized + "/")):
        return True
    if "/" in normalized:
        return fnmatch.fnmatch(rel, normalized) or rel.startswith(normalized + "/")
    if fnmatch.fnmatch(Path(rel).name, normalized):
        return True
    return any(fnmatch.fnmatch(part, normalized) for part in parts)


def _is_ignored(rel_posix: str, rules: list[tuple[str, bool]]) -> bool:
    # Git semantics: the LAST matching rule wins, so a later "!keep.log"
    # re-includes a file excluded by an earlier "*.log".
    rel = rel_posix.strip("/")
    parts = rel.split("/") if rel else []
    ignored = False
    for pattern, negated in rules:
        if _pattern_matches(rel, parts, pattern):
            ignored = not negated
    return ignored


# Well-known dependency/build directories excluded even when a .gitignore
# rule or negation would re-include them (mirrors the OpenTUI completion
# walker's _SKIP_DIRS).
_ALWAYS_SKIP_DIRS = frozenset({"node_modules", ".venv", "__pycache__"})


def _entry_visible(name: str, rel: str, rules: list[tuple[str, bool]]) -> bool:
    # Dot entries are always excluded: VCS internals and dotfile secrets
    # (.env, credentials) must never surface in the tree or previews.
    if name.startswith("."):
        return False
    if name in _ALWAYS_SKIP_DIRS:
        return False
    return not _is_ignored(rel, rules)


# ---------------------------------------------------------------------------
# Blocking filesystem work (run in a worker thread)
# ---------------------------------------------------------------------------


def _list_dir_blocking(root: Path, rel: str) -> dict[str, Any]:
    target = _resolve_inside_workspace(root, rel)
    if not target.is_dir():
        raise FileNotFoundError(rel)
    rules = _load_gitignore_patterns(root)
    entries: list[dict[str, Any]] = []
    with os.scandir(target) as it:
        for info in it:
            try:
                name = info.name
            except OSError:
                continue
            rel_child = f"{rel}/{name}" if rel else name
            if not _entry_visible(name, rel_child, rules):
                continue
            is_dir = info.is_dir(follow_symlinks=False)
            entry: dict[str, Any] = {
                "name": name,
                "path": rel_child,
                "type": "directory" if is_dir else "file",
            }
            if not is_dir:
                try:
                    stat = info.stat(follow_symlinks=False)
                    entry["size"] = stat.st_size
                    entry["mtime"] = int(stat.st_mtime * 1000)
                except OSError:
                    pass
            entries.append(entry)
    if len(entries) > _MAX_LIST_ENTRIES:
        raise OSError(f"directory has more than {_MAX_LIST_ENTRIES} entries")
    entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
    return {"path": rel, "entries": entries}


def _read_content_blocking(root: Path, rel: str, max_bytes: int) -> dict[str, Any]:
    target = _resolve_inside_workspace(root, rel)
    if not target.is_file():
        raise FileNotFoundError(rel)
    rules = _load_gitignore_patterns(root)
    if not _entry_visible(target.name, rel, rules):
        raise WorkspacePathError("path not visible")
    size = target.stat().st_size
    with target.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    if b"\x00" in raw[:_BINARY_PROBE_BYTES]:
        return {
            "path": rel,
            "size": size,
            "binary": True,
            "truncated": False,
            "content": None,
        }
    return {
        "path": rel,
        "size": size,
        "binary": False,
        "truncated": truncated,
        "content": raw.decode("utf-8", errors="replace"),
    }


# ---------------------------------------------------------------------------
# Auth / workspace resolution (request side)
# ---------------------------------------------------------------------------


def _authorization_token_matches(config: GatewayConfig, request: Request) -> bool:
    """Header-only Bearer token check (same posture as the upload endpoint)."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    token = header[7:].strip()
    if token == config.auth.token:
        return True
    from opensquilla.gateway.desktop_ownership import (
        active_desktop_gateway_auth_token_matches,
    )

    return active_desktop_gateway_auth_token_matches(token)


async def _resolve_root(
    request: Request,
    config: GatewayConfig,
    session_manager: Any,
) -> _WorkspaceRoot | JSONResponse:
    if not request_origin_allowed(request, config):
        return forbidden_origin_response()
    if config.auth.mode == "token":
        if config.auth.token and not _authorization_token_matches(config, request):
            return JSONResponse(
                {"error": "Authorization header (Bearer …) required.", "code": "UNAUTHORIZED"},
                status_code=401,
            )
    from opensquilla.gateway.session_services import get_session_storage

    storage = get_session_storage(session_manager)
    if storage is None:
        return JSONResponse(
            {"error": "workspace store unavailable", "code": "WORKSPACE_STORE_UNAVAILABLE"},
            status_code=503,
        )
    workspace_id = str(request.query_params.get("workspace") or "").strip()
    if not workspace_id:
        return JSONResponse(
            {"error": "missing 'workspace' query parameter", "code": "BAD_REQUEST"},
            status_code=400,
        )
    try:
        validated = await resolve_validated_project_workspace(storage, workspace_id)
    except ProjectWorkspaceStateError as exc:
        if exc.reason in {"not_found", "removed"}:
            return JSONResponse(
                {"error": f"workspace {workspace_id} not found", "code": "NOT_FOUND"},
                status_code=404,
            )
        return JSONResponse(
            {
                "error": f"workspace {workspace_id} unavailable",
                "code": "WORKSPACE_UNAVAILABLE",
                "reason": exc.reason,
            },
            status_code=409,
        )
    root = Path(validated.canonical_path)
    return _WorkspaceRoot(
        workspace_id=validated.workspace.workspace_id,
        name=validated.workspace.display_name,
        root=root,
    )


def _workspace_header(root: _WorkspaceRoot) -> dict[str, Any]:
    return {
        "workspace": {
            "id": root.workspace_id,
            "name": root.name,
            "path": unicodedata.normalize("NFC", str(root.root)),
        }
    }


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_workspace_files_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    session_manager: Any = None,
) -> None:
    """Register GET /api/v1/files and GET /api/v1/files/content."""

    async def list_handler(request: Request) -> JSONResponse:
        resolved = await _resolve_root(request, config, session_manager)
        if isinstance(resolved, JSONResponse):
            return resolved
        try:
            rel = normalize_workspace_rel_path(request.query_params.get("path"))
        except WorkspacePathError:
            return JSONResponse(
                {"error": "invalid path", "code": "BAD_REQUEST"}, status_code=400
            )
        try:
            result = await asyncio.to_thread(_list_dir_blocking, resolved.root, rel)
        except FileNotFoundError:
            return JSONResponse(
                {"error": "path not found", "code": "NOT_FOUND"}, status_code=404
            )
        except MemoryError:
            # A single failed request must never take the gateway process down.
            log.warning("workspace_files.list_memory_error path=%s", rel)
            return JSONResponse(
                {"error": "unable to list directory", "code": "LIST_FAILED"},
                status_code=500,
            )
        except OSError as exc:
            message = str(exc)
            if "escapes workspace" in message or "traversal" in message:
                return JSONResponse(
                    {"error": "invalid path", "code": "BAD_REQUEST"}, status_code=400
                )
            log.warning("workspace_files.list_failed", path=rel, error=message)
            return JSONResponse(
                {"error": "unable to list directory", "code": "LIST_FAILED"},
                status_code=500,
            )
        payload = _workspace_header(resolved)
        payload.update(result)
        return JSONResponse(payload)

    async def content_handler(request: Request) -> JSONResponse:
        resolved = await _resolve_root(request, config, session_manager)
        if isinstance(resolved, JSONResponse):
            return resolved
        try:
            rel = normalize_workspace_rel_path(request.query_params.get("path"))
        except WorkspacePathError:
            return JSONResponse(
                {"error": "invalid path", "code": "BAD_REQUEST"}, status_code=400
            )
        if not rel:
            return JSONResponse(
                {"error": "a file path is required", "code": "BAD_REQUEST"}, status_code=400
            )
        raw_max = request.query_params.get("max_bytes")
        max_bytes = DEFAULT_MAX_CONTENT_BYTES
        if raw_max is not None and raw_max.strip().isdigit():
            max_bytes = min(int(raw_max), MAX_CONTENT_BYTES_CAP)
            if max_bytes <= 0:
                max_bytes = DEFAULT_MAX_CONTENT_BYTES
        try:
            result = await asyncio.to_thread(
                _read_content_blocking, resolved.root, rel, max_bytes
            )
        except FileNotFoundError:
            return JSONResponse(
                {"error": "path not found", "code": "NOT_FOUND"}, status_code=404
            )
        except WorkspacePathError:
            return JSONResponse(
                {"error": "invalid path", "code": "BAD_REQUEST"}, status_code=400
            )
        except MemoryError:
            # Under host memory pressure even the bounded 1MB read can fail;
            # answer 500 instead of letting the exception kill the process.
            log.warning("workspace_files.read_memory_error path=%s", rel)
            return JSONResponse(
                {"error": "unable to read file", "code": "READ_FAILED"}, status_code=500
            )
        except OSError as exc:
            log.warning("workspace_files.read_failed", path=rel, error=str(exc))
            return JSONResponse(
                {"error": "unable to read file", "code": "READ_FAILED"}, status_code=500
            )
        payload = _workspace_header(resolved)
        payload.update(result)
        return JSONResponse(payload)

    app.router.routes.append(
        Route("/api/v1/files", list_handler, methods=["GET"])
    )
    app.router.routes.append(
        Route("/api/v1/files/content", content_handler, methods=["GET"])
    )


__all__ = [
    "DEFAULT_MAX_CONTENT_BYTES",
    "MAX_CONTENT_BYTES_CAP",
    "WorkspacePathError",
    "normalize_workspace_rel_path",
    "register_workspace_files_routes",
]
