"""Owner-only access to files in a session's current workspace, without publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.artifact_session.working_files import checked_path
from opensquilla.artifacts import _is_reparse_point, artifact_mime_for_name
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.origin_guard import (
    forbidden_origin_response,
    request_origin_allowed,
    request_principal_is_owner,
)
from opensquilla.gateway.project_workspace_runtime import authoritative_project_run_context
from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.paths import native_io_path
from opensquilla.project_workspaces import ProjectWorkspaceStateError
from opensquilla.sandbox.path_validation import decide_path_access
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.session.keys import canonicalize_session_key, parse_agent_id

MAX_WORKSPACE_FILE_BYTES = 32 * 1024 * 1024
MAX_RESOLVE_PATHS = 64
_MAX_REQUEST_BYTES = 64 * 1024
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"})
_TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".xml",
        ".svg",
        ".html",
        ".htm",
        ".xhtml",
        ".css",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".vue",
        ".py",
        ".sh",
        ".ps1",
        ".sql",
        ".log",
        ".rst",
        ".tex",
    }
)


class WorkspaceFileUnavailableError(ValueError):
    """No readable file exists under the supplied session authority."""


@dataclass(frozen=True)
class _Workspace:
    root: Path
    binding: str
    profile: FileSystemPermissionProfile


class WorkspaceFiles:
    """Resolve authority from storage for every request, never from a client root."""

    def __init__(self, config: GatewayConfig, session_manager: Any) -> None:
        self.config = config
        self.manager = session_manager

    async def workspace(self, session_key: str) -> _Workspace:
        storage = get_session_storage(self.manager)
        if not session_key or storage is None:
            raise WorkspaceFileUnavailableError()
        key = canonicalize_session_key(session_key)
        session = await storage.get_session(key)
        if session is None:
            raise WorkspaceFileUnavailableError()
        default = resolve_agent_workspace_dir(parse_agent_id(key), self.config)
        try:
            context, _ = await authoritative_project_run_context(
                storage=storage,
                session_manager=self.manager,
                session=session,
                config=self.config,
                default_workspace=str(default) if default else None,
                include_user_grants=False,
            )
            if not context.workspace:
                raise WorkspaceFileUnavailableError()
            root = checked_path(Path(context.workspace), ".")
            root_stat = native_io_path(root).lstat()
            if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse_point(root):
                raise WorkspaceFileUnavailableError()
        except (OSError, ValueError, RpcHandlerError, ProjectWorkspaceStateError) as exc:
            raise WorkspaceFileUnavailableError() from exc
        # This is an identity fence, not an authorization token. Every content
        # request still authenticates, authorizes, and resolves the current root.
        identity = [
            key,
            session.session_id,
            session.epoch,
            str(root),
            root_stat.st_dev,
            root_stat.st_ino,
        ]
        binding = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        profile = FileSystemPermissionProfile.workspace(
            workspace=root,
            denied_read_roots=(
                Path(path).expanduser() for path in self.config.sandbox.denied_read_roots
            ),
            denied_read_globs=self.config.sandbox.denied_read_globs,
            host_root_readonly=False,
            tmp_writable=False,
            tmpdir_env_writable=False,
        )
        return _Workspace(root, binding, profile)

    async def resolve(self, session_key: str, paths: list[str]) -> dict[str, Any]:
        workspace = await self.workspace(session_key)

        def collect() -> list[dict[str, Any]]:
            files = []
            for raw in dict.fromkeys(paths):
                try:
                    path, metadata = _readable_file(workspace, raw)
                except (OSError, ValueError):
                    continue
                relative = path.relative_to(workspace.root).as_posix()
                mime = artifact_mime_for_name(path.name)
                kind = (
                    "image"
                    if mime in _IMAGE_MIMES
                    else (
                        "text"
                        if path.suffix.lower() in _TEXT_SUFFIXES or mime.startswith("text/")
                        else "download"
                    )
                )
                files.append(
                    {
                        "requestedPath": raw,
                        "path": relative,
                        "name": path.name,
                        "mime": mime,
                        "size": metadata.st_size,
                        "kind": kind,
                        "contentUrl": "/api/v1/workspace-files/content?"
                        + urlencode(
                            {
                                "path": relative,
                                "workspaceBinding": workspace.binding,
                            }
                        ),
                    }
                )
            return files

        files = await asyncio.to_thread(collect)
        if await self.workspace(session_key) != workspace:
            raise WorkspaceFileUnavailableError()
        return {"workspaceBinding": workspace.binding, "files": files}

    async def read(self, session_key: str, path: str, binding: str) -> tuple[str, str, bytes]:
        workspace = await self.workspace(session_key)
        if not binding or binding != workspace.binding:
            raise WorkspaceFileUnavailableError()
        target, data = await asyncio.to_thread(_read_file, workspace, path)
        if await self.workspace(session_key) != workspace:
            raise WorkspaceFileUnavailableError()
        return target.name, artifact_mime_for_name(target.name), data


def _file(workspace: _Workspace, raw: str) -> tuple[Path, os.stat_result]:
    if not raw or len(raw) > 4096 or any(ord(char) < 32 for char in raw):
        raise WorkspaceFileUnavailableError()
    # Windows spellings are accepted on Windows; foreign drive/URL forms are
    # rejected by checked_path rather than interpreted as a workspace alias.
    requested = Path(raw)
    relative = (
        requested.relative_to(workspace.root).as_posix()
        if requested.is_absolute()
        else raw.replace("\\", "/")
    )
    target = checked_path(workspace.root, relative)
    current = workspace.root
    for part in target.relative_to(workspace.root).parts:
        current /= part
        if _is_reparse_point(current):
            raise WorkspaceFileUnavailableError()
    if (
        decide_path_access(target, workspace=workspace.root, profile=workspace.profile).status
        != "allowed"
    ):
        raise WorkspaceFileUnavailableError()
    metadata = native_io_path(target).lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_WORKSPACE_FILE_BYTES:
        raise WorkspaceFileUnavailableError()
    return target, metadata


def _read_file(workspace: _Workspace, raw: str) -> tuple[Path, bytes]:
    target, before = _file(workspace, raw)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(native_io_path(target), flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise WorkspaceFileUnavailableError()
        data = stream.read(MAX_WORKSPACE_FILE_BYTES + 1)
        after_read = os.fstat(stream.fileno())
    _, after = _file(workspace, raw)
    if (
        len(data) > MAX_WORKSPACE_FILE_BYTES
        or len(data) != before.st_size
        or not os.path.samestat(before, after)
        or after.st_mtime_ns != before.st_mtime_ns
        or after_read.st_mtime_ns != before.st_mtime_ns
    ):
        raise WorkspaceFileUnavailableError()
    return target, data


def _readable_file(workspace: _Workspace, raw: str) -> tuple[Path, os.stat_result]:
    """Confirm read access without loading every referenced file into memory."""
    target, before = _file(workspace, raw)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(native_io_path(target), flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not os.path.samestat(before, opened):
            raise WorkspaceFileUnavailableError()
    _, after = _file(workspace, raw)
    if not os.path.samestat(before, after) or before.st_mtime_ns != after.st_mtime_ns:
        raise WorkspaceFileUnavailableError()
    return target, after


def register_workspace_file_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    session_manager: Any = None,
) -> None:
    service = WorkspaceFiles(config, session_manager)

    def guard(request: Request) -> Response | None:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        if not request_principal_is_owner(config, request):
            return JSONResponse({"code": "OWNER_REQUIRED"}, status_code=403, headers=_HEADERS)
        return None

    def unavailable() -> Response:
        return JSONResponse(
            {"code": "WORKSPACE_FILE_UNAVAILABLE"}, status_code=404, headers=_HEADERS
        )

    async def resolve(request: Request) -> Response:
        denied = guard(request)
        if denied is not None:
            return denied
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_REQUEST_BYTES:
                return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            payload = json.loads(body)
            paths = payload.get("paths") if isinstance(payload, dict) else None
            if (
                not isinstance(paths, list)
                or len(paths) > MAX_RESOLVE_PATHS
                or any(not isinstance(path, str) or len(path) > 4096 for path in paths)
            ):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            result = await service.resolve(
                request.headers.get("x-opensquilla-session-key", ""), paths
            )
        except (OSError, ValueError, RpcHandlerError, ProjectWorkspaceStateError):
            return unavailable()
        return JSONResponse(result, headers=_HEADERS)

    async def content(request: Request) -> Response:
        denied = guard(request)
        if denied is not None:
            return denied
        try:
            name, mime, data = await service.read(
                request.headers.get("x-opensquilla-session-key", ""),
                request.query_params.get("path", ""),
                request.query_params.get("workspaceBinding", ""),
            )
        except (OSError, ValueError, RpcHandlerError, ProjectWorkspaceStateError):
            return unavailable()
        # Always serve source as an attachment; HTML/SVG is never executed at
        # the authenticated Gateway origin. The client may display text safely.
        return Response(
            data,
            media_type=mime,
            headers={
                **_HEADERS,
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(name, safe=""),
            },
        )

    app.router.routes.extend(
        [
            Route("/api/v1/workspace-files/resolve", resolve, methods=["POST"]),
            Route("/api/v1/workspace-files/content", content, methods=["GET"]),
        ]
    )
