"""Owner-only access to files in a session's current workspace, without publication."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import hmac
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
MAX_TEXT_PAGE_LINES = 200
MAX_TEXT_LINE_BYTES = 256 * 1024
MAX_TEXT_PAGE_BYTES = 1024 * 1024
MAX_TEXT_SEARCH_CHARS = 512
_READ_CHUNK_BYTES = 64 * 1024
_WINDOWS = os.name == "nt"
_MAX_REQUEST_BYTES = 64 * 1024
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_NATIVE_METADATA_SIGNING_CONTEXT = b"opensquilla-native-workspace-file-v1\n"
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


def _workspace_metadata_signature(
    instance_id: str,
    instance_nonce: str,
    session_key: str,
    path: str,
    workspace_binding: str,
) -> str:
    """Sign the exact metadata request accepted from the trusted Desktop main process."""

    payload = json.dumps(
        {
            "v": 1,
            "instanceId": instance_id,
            "sessionKey": session_key,
            "path": path,
            "workspaceBinding": workspace_binding,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hmac.new(
        instance_nonce.encode("ascii"),
        _NATIVE_METADATA_SIGNING_CONTEXT + payload,
        hashlib.sha256,
    ).hexdigest()


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

    async def resolve(
        self, session_key: str, paths: list[str], *, native_actions: bool = False
    ) -> dict[str, Any]:
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
                        "textPaging": kind == "text",
                        "nativeActions": native_actions and _native_identity_supported(metadata),
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

    async def read_page(
        self, session_key: str, path: str, binding: str, start_line: int, end_line: int
    ) -> dict[str, Any]:
        workspace = await self.workspace(session_key)
        if not binding or binding != workspace.binding:
            raise WorkspaceFileUnavailableError()
        target, content, total_lines = await asyncio.to_thread(
            _read_file_page, workspace, path, start_line, end_line
        )
        if await self.workspace(session_key) != workspace:
            raise WorkspaceFileUnavailableError()
        return {
            "relativePath": target.relative_to(workspace.root).as_posix(),
            "content": content,
            "totalLines": total_lines,
            "startLine": start_line,
            "endLine": min(end_line, total_lines),
        }

    async def metadata(self, session_key: str, path: str, binding: str) -> dict[str, Any]:
        """Return controlled native-open metadata for one workspace file.

        The absolute paths in this response are intended for the trusted
        Electron main process only.  Renderer-facing catalogue and content
        responses continue to expose relative paths exclusively.
        """
        workspace = await self.workspace(session_key)
        if not binding or binding != workspace.binding:
            raise WorkspaceFileUnavailableError()
        target, metadata = await asyncio.to_thread(_readable_file, workspace, path)
        if await self.workspace(session_key) != workspace:
            raise WorkspaceFileUnavailableError()
        relative = target.relative_to(workspace.root).as_posix()
        if not _native_identity_supported(metadata):
            raise WorkspaceFileUnavailableError()
        return {
            "workspaceBinding": workspace.binding,
            "relativePath": relative,
            "sourcePath": str(target),
            "workspace": str(workspace.root),
            "name": target.name,
            "mime": artifact_mime_for_name(target.name),
            "size": metadata.st_size,
            "identity": _native_file_identity(metadata),
        }

    async def search(
        self, session_key: str, path: str, binding: str, query: str
    ) -> dict[str, Any]:
        workspace = await self.workspace(session_key)
        if not binding or binding != workspace.binding:
            raise WorkspaceFileUnavailableError()
        target, _, total_lines, match_line = await asyncio.to_thread(
            _scan_text_file, workspace, path, query=query
        )
        if await self.workspace(session_key) != workspace:
            raise WorkspaceFileUnavailableError()
        return {
            "relativePath": target.relative_to(workspace.root).as_posix(),
            "totalLines": total_lines,
            "matchLine": match_line,
        }


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


def _read_file_page(
    workspace: _Workspace, raw: str, start_line: int, end_line: int
) -> tuple[Path, str, int]:
    if (
        start_line < 1
        or end_line < start_line
        or end_line - start_line >= MAX_TEXT_PAGE_LINES
    ):
        raise WorkspaceFileUnavailableError()
    target, content, total, _ = _scan_text_file(
        workspace, raw, start_line=start_line, end_line=end_line
    )
    if start_line > total:
        raise WorkspaceFileUnavailableError()
    return target, content, total


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        os.path.samestat(left, right)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        # On Windows CPython lstat still exposes creation time as ctime, while
        # fstat can expose change time. Compare ctime only within fstat below.
        and (_WINDOWS or left.st_ctime_ns == right.st_ctime_ns)
    )


def _native_identity_supported(metadata: os.stat_result) -> bool:
    # libuv exposes a 64-bit FileId; CPython may expose a distinct 128-bit ID
    # (for example on ReFS). Hide native actions when these cannot be compared.
    return not _WINDOWS or metadata.st_ino <= (1 << 64) - 1


def _native_file_identity(metadata: os.stat_result) -> dict[str, str]:
    return {
        # CPython uses the full Windows volume serial; libuv uses its low DWORD.
        "dev": str(metadata.st_dev & 0xFFFFFFFF if _WINDOWS else metadata.st_dev),
        "ino": str(metadata.st_ino),
        "size": str(metadata.st_size),
        "mtimeNs": str(metadata.st_mtime_ns),
        "ctimeNs": str(metadata.st_ctime_ns),
    }


def _read_flags() -> int:
    # Do not block if an attacker swaps a checked regular file for a FIFO.
    # Windows has no O_NONBLOCK; its reparse-point and identity checks remain.
    return (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _scan_text_file(
    workspace: _Workspace,
    raw: str,
    *,
    start_line: int = 0,
    end_line: int = 0,
    query: str = "",
) -> tuple[Path, str, int, int | None]:
    """Scan bounded UTF-8 input using exactly str.splitlines() line boundaries.

    Source line endings are preserved, including CRLF split across read chunks.
    Both pagination and case-insensitive substring search do one bounded scan;
    neither allocates the entire file or an unbounded individual line.
    """
    target, before = _file(workspace, raw)
    if len(query) > MAX_TEXT_SEARCH_CHARS or "\x00" in query:
        raise WorkspaceFileUnavailableError()
    needle = query.lower()
    flags = _read_flags()
    with os.fdopen(os.open(native_io_path(target), flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not _same_file_snapshot(before, opened):
            raise WorkspaceFileUnavailableError()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        selected: list[str] = []
        selected_bytes = 0
        consumed = 0
        total = 0
        pending = ""
        match_line: int | None = None
        try:
            while True:
                chunk = stream.read(min(_READ_CHUNK_BYTES, before.st_size - consumed + 1))
                consumed += len(chunk)
                if consumed > before.st_size or consumed > MAX_WORKSPACE_FILE_BYTES:
                    raise WorkspaceFileUnavailableError()
                pending += decoder.decode(chunk, final=not chunk)
                if "\x00" in pending:
                    raise WorkspaceFileUnavailableError()
                lines = pending.splitlines(keepends=True)
                pending = ""
                # A final CR may be the first half of a CRLF on the next read.
                if chunk and lines and (
                    lines[-1].endswith("\r")
                    or lines[-1][-1] not in "\n\v\f\r\x1c\x1d\x1e\x85\u2028\u2029"
                ):
                    pending = lines.pop()
                if len(pending.encode("utf-8")) > MAX_TEXT_LINE_BYTES:
                    raise WorkspaceFileUnavailableError()
                for line in lines:
                    line_bytes = len(line.encode("utf-8"))
                    if line_bytes > MAX_TEXT_LINE_BYTES:
                        raise WorkspaceFileUnavailableError()
                    total += 1
                    if needle and match_line is None and needle in line.lower():
                        match_line = total
                    if start_line <= total <= end_line:
                        selected_bytes += line_bytes
                        if selected_bytes > MAX_TEXT_PAGE_BYTES:
                            raise WorkspaceFileUnavailableError()
                        selected.append(line)
                if not chunk:
                    break
            after_read = os.fstat(stream.fileno())
        except (UnicodeError, ValueError) as exc:
            raise WorkspaceFileUnavailableError() from exc
    _, after = _file(workspace, raw)
    if (
        consumed != before.st_size
        or not _same_file_snapshot(before, after)
        or not _same_file_snapshot(before, after_read)
        or opened.st_ctime_ns != after_read.st_ctime_ns
    ):
        raise WorkspaceFileUnavailableError()
    return target, "".join(selected), max(1, total), match_line


def _read_file(workspace: _Workspace, raw: str) -> tuple[Path, bytes]:
    target, before = _file(workspace, raw)
    flags = _read_flags()
    with os.fdopen(os.open(native_io_path(target), flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not _same_file_snapshot(before, opened):
            raise WorkspaceFileUnavailableError()
        data = stream.read(MAX_WORKSPACE_FILE_BYTES + 1)
        after_read = os.fstat(stream.fileno())
    _, after = _file(workspace, raw)
    if (
        len(data) > MAX_WORKSPACE_FILE_BYTES
        or len(data) != before.st_size
        or not _same_file_snapshot(before, after)
        or not _same_file_snapshot(before, after_read)
        or opened.st_ctime_ns != after_read.st_ctime_ns
    ):
        raise WorkspaceFileUnavailableError()
    return target, data


def _readable_file(workspace: _Workspace, raw: str) -> tuple[Path, os.stat_result]:
    """Confirm read access without loading every referenced file into memory."""
    target, before = _file(workspace, raw)
    flags = _read_flags()
    with os.fdopen(os.open(native_io_path(target), flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not _same_file_snapshot(before, opened):
            raise WorkspaceFileUnavailableError()
    _, after = _file(workspace, raw)
    if not _same_file_snapshot(before, after):
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

    def native_owner(request: Request) -> tuple[str, str] | None:
        owner = getattr(request.app.state, "desktop_gateway_ownership", None)
        instance_id = getattr(owner, "instance_id", None)
        nonce = getattr(owner, "instance_nonce", None)
        if (
            not isinstance(instance_id, str)
            or not instance_id
            or not isinstance(nonce, str)
            or not nonce
            or not nonce.isascii()
        ):
            return None
        return instance_id, nonce

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
                request.headers.get("x-opensquilla-session-key", ""),
                paths,
                native_actions=native_owner(request) is not None,
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

    async def page(request: Request) -> Response:
        denied = guard(request)
        if denied is not None:
            return denied
        try:
            start_line = int(request.query_params.get("startLine", ""))
            end_line = int(request.query_params.get("endLine", ""))
        except ValueError:
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        if (
            start_line < 1
            or end_line < start_line
            or end_line - start_line >= MAX_TEXT_PAGE_LINES
        ):
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            result = await service.read_page(
                request.headers.get("x-opensquilla-session-key", ""),
                request.query_params.get("path", ""),
                request.query_params.get("workspaceBinding", ""),
                start_line,
                end_line,
            )
        except (OSError, ValueError, RpcHandlerError, ProjectWorkspaceStateError):
            return unavailable()
        return JSONResponse(result, headers=_HEADERS)

    async def search(request: Request) -> Response:
        denied = guard(request)
        if denied is not None:
            return denied
        query = request.query_params.get("query", "").strip()
        if not query or len(query) > MAX_TEXT_SEARCH_CHARS or "\x00" in query:
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            result = await service.search(
                request.headers.get("x-opensquilla-session-key", ""),
                request.query_params.get("path", ""),
                request.query_params.get("workspaceBinding", ""),
                query,
            )
        except (OSError, ValueError, RpcHandlerError, ProjectWorkspaceStateError):
            return unavailable()
        return JSONResponse(result, headers=_HEADERS)

    async def metadata(request: Request) -> Response:
        denied = guard(request)
        if denied is not None:
            return denied
        owner = native_owner(request)
        session_key = request.headers.get("x-opensquilla-session-key", "")
        path = request.query_params.get("path", "")
        binding = request.query_params.get("workspaceBinding", "")
        supplied_signature = request.headers.get("x-opensquilla-native-signature", "")
        if (
            owner is None
            or len(supplied_signature) != 64
            or any(char not in "0123456789abcdef" for char in supplied_signature)
            or not hmac.compare_digest(
                _workspace_metadata_signature(*owner, session_key, path, binding),
                supplied_signature,
            )
        ):
            return JSONResponse(
                {"code": "NATIVE_WORKSPACE_FILE_FORBIDDEN"}, status_code=403, headers=_HEADERS
            )
        try:
            result = await service.metadata(
                session_key,
                path,
                binding,
            )
        except (OSError, ValueError, RpcHandlerError, ProjectWorkspaceStateError):
            return unavailable()
        return JSONResponse(result, headers=_HEADERS)

    app.router.routes.extend(
        [
            Route("/api/v1/workspace-files/resolve", resolve, methods=["POST"]),
            Route("/api/v1/workspace-files/content", content, methods=["GET"]),
            Route("/api/v1/workspace-files/page", page, methods=["GET"]),
            Route("/api/v1/workspace-files/search", search, methods=["GET"]),
            Route("/api/v1/workspace-files/metadata", metadata, methods=["GET"]),
        ]
    )
