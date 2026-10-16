"""Owner-only access to files in a session's current workspace, without publication."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import hmac
import json
import os
import stat
import threading
import time
from array import array
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from uuid import uuid4

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
    request_principal_is_owner_async,
)
from opensquilla.gateway.project_workspace_runtime import authoritative_project_run_context
from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.paths import native_io_path
from opensquilla.project_workspaces import (
    ProjectWorkspaceStateError,
    resolve_validated_project_workspace,
)
from opensquilla.sandbox.path_validation import decide_path_access
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.session.keys import canonicalize_session_key, parse_agent_id

MAX_WORKSPACE_FILE_BYTES = 32 * 1024 * 1024
MAX_RESOLVE_PATHS = 64
MAX_TEXT_PAGE_LINES = 200
MAX_TEXT_LINE_BYTES = 256 * 1024
MAX_TEXT_PAGE_BYTES = 1024 * 1024
MAX_TEXT_SEARCH_CHARS = 512
MAX_DIRECTORY_PAGE_SIZE = 200
MAX_DIRECTORY_ENTRIES = 100_000
MAX_DIRECTORY_CACHE_BYTES = 64 * 1024 * 1024
MAX_DIRECTORY_CACHE_ITEMS = 16
MAX_TEXT_INDEX_CACHE_BYTES = 8 * 1024 * 1024
MAX_TEXT_INDEX_CACHE_ITEMS = 64
DIRECTORY_SNAPSHOT_TTL_SECONDS = 60
_TEXT_INDEX_STRIDE = MAX_TEXT_PAGE_LINES
_READ_CHUNK_BYTES = 64 * 1024
_WINDOWS = os.name == "nt"
_MAX_REQUEST_BYTES = 64 * 1024
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_NATIVE_METADATA_SIGNING_CONTEXT = b"opensquilla-native-workspace-file-v1\n"
_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"})
# Workspace preview types must not depend on OS file associations. In particular,
# Windows can register .ts as MPEG transport streams, .csv as Excel, and omit WebP.
# Keep this local to workspace files: the generic artifact publication policy has
# its own contract, and unknown extensions still use its existing fallback.
_WORKSPACE_MIME_TYPES = {
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".xml": "application/xml",
    ".svg": "image/svg+xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".xhtml": "application/xhtml+xml",
    ".css": "text/css",
    ".js": "text/javascript",
    ".jsx": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".vue": "text/plain",
    ".py": "text/x-python",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".zip": "application/zip",
}
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


def _workspace_file_mime(name: str) -> str:
    return _WORKSPACE_MIME_TYPES.get(Path(name).suffix.lower()) or artifact_mime_for_name(name)


class WorkspaceFileUnavailableError(ValueError):
    """No readable file exists under the supplied session authority."""


class WorkspaceFileChangedError(WorkspaceFileUnavailableError):
    """The requested revision is no longer current; reload explicitly."""


class WorkspaceDirectoryChangedError(WorkspaceFileUnavailableError):
    """A directory snapshot changed or expired; restart at the first page."""


class WorkspaceTextLimitError(Exception):
    """An authorized text file exceeds a bounded source preview limit."""

    def __init__(self, limit: str, max_bytes: int) -> None:
        super().__init__("Text preview limit exceeded")
        self.limit = limit
        self.max_bytes = max_bytes


class _BoundedCache:
    """Per-Gateway LRU with explicit accounting; never an authority cache."""

    def __init__(self, max_bytes: int, max_items: int) -> None:
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.bytes = 0
        self.values: OrderedDict[Any, tuple[Any, int]] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self.lock:
            entry = self.values.get(key)
            if entry is None:
                return None
            self.values.move_to_end(key)
            return entry[0]

    def put(self, key: Any, value: Any, size: int) -> None:
        with self.lock:
            previous = self.values.pop(key, None)
            if previous is not None:
                self.bytes -= previous[1]
            if size > self.max_bytes:
                return
            self.values[key] = (value, size)
            self.bytes += size
            while self.bytes > self.max_bytes or len(self.values) > self.max_items:
                _, (_, removed_size) = self.values.popitem(last=False)
                self.bytes -= removed_size


@dataclass(frozen=True)
class _TextIndex:
    offsets: array
    total_lines: int


@dataclass(frozen=True)
class _DirectorySnapshot:
    workspace: _Workspace
    relative_path: str
    revision: str
    entries: list[dict[str, Any]]
    truncated: bool
    expires_at: float


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
        self._directory_cache = _BoundedCache(MAX_DIRECTORY_CACHE_BYTES, MAX_DIRECTORY_CACHE_ITEMS)
        self._text_index_cache = _BoundedCache(
            MAX_TEXT_INDEX_CACHE_BYTES, MAX_TEXT_INDEX_CACHE_ITEMS
        )

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

    async def project_workspace(self, project_id: str) -> _Workspace:
        """Resolve an existing trusted project without creating a session or root."""
        storage = get_session_storage(self.manager)
        if not project_id or storage is None:
            raise WorkspaceFileUnavailableError()
        try:
            validated = await resolve_validated_project_workspace(storage, project_id)
            root = checked_path(Path(validated.canonical_path), ".")
            metadata = native_io_path(root).lstat()
            if not stat.S_ISDIR(metadata.st_mode) or _is_reparse_point(root):
                raise WorkspaceFileUnavailableError()
        except (OSError, ValueError, ProjectWorkspaceStateError) as exc:
            raise WorkspaceFileUnavailableError() from exc
        identity = ["project", project_id, str(root), metadata.st_dev, metadata.st_ino]
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
                    path, metadata, revision = _readable_file(workspace, raw)
                except (OSError, ValueError):
                    continue
                relative = path.relative_to(workspace.root).as_posix()
                mime = _workspace_file_mime(path.name)
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
                        "revision": revision,
                        "resource": _resource_identity(workspace, relative),
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

    async def read(
        self, session_key: str, path: str, binding: str, expected_revision: str = ""
    ) -> tuple[str, str, bytes]:
        workspace = await self.workspace(session_key)
        if not binding or binding != workspace.binding:
            raise WorkspaceFileUnavailableError()
        target, data = await asyncio.to_thread(_read_file, workspace, path, expected_revision)
        if await self.workspace(session_key) != workspace:
            raise WorkspaceFileUnavailableError()
        return target.name, _workspace_file_mime(target.name), data

    async def read_page(
        self,
        session_key: str,
        path: str,
        binding: str,
        start_line: int,
        end_line: int,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        workspace = await self.workspace(session_key)
        if not binding or binding != workspace.binding:
            raise WorkspaceFileUnavailableError()
        target, content, total_lines, revision = await asyncio.to_thread(
            _read_file_page,
            workspace,
            path,
            start_line,
            end_line,
            self._text_index_cache,
            expected_revision,
        )
        if await self.workspace(session_key) != workspace:
            raise WorkspaceFileUnavailableError()
        return {
            "relativePath": target.relative_to(workspace.root).as_posix(),
            "content": content,
            "totalLines": total_lines,
            "startLine": start_line,
            "endLine": min(end_line, total_lines),
            "revision": revision,
            "resource": _resource_identity(
                workspace, target.relative_to(workspace.root).as_posix()
            ),
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
        target, metadata, revision = await asyncio.to_thread(_readable_file, workspace, path)
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
            "mime": _workspace_file_mime(target.name),
            "size": metadata.st_size,
            "identity": _native_file_identity(metadata),
            "revision": revision,
            "resource": _resource_identity(workspace, relative),
        }

    async def search(
        self,
        session_key: str,
        path: str,
        binding: str,
        query: str,
        expected_revision: str = "",
    ) -> dict[str, Any]:
        workspace = await self.workspace(session_key)
        if not binding or binding != workspace.binding:
            raise WorkspaceFileUnavailableError()
        target, _, total_lines, match_line, revision = await asyncio.to_thread(
            _scan_text_file,
            workspace,
            path,
            query=query,
            index_cache=self._text_index_cache,
            expected_revision=expected_revision,
        )
        if await self.workspace(session_key) != workspace:
            raise WorkspaceFileUnavailableError()
        return {
            "relativePath": target.relative_to(workspace.root).as_posix(),
            "totalLines": total_lines,
            "matchLine": match_line,
            "revision": revision,
            "resource": _resource_identity(
                workspace, target.relative_to(workspace.root).as_posix()
            ),
        }

    async def list_dir(
        self,
        session_key: str,
        path: str,
        binding: str,
        cursor: str | int,
        limit: int,
    ) -> dict[str, Any]:
        """List one directory page without exposing host paths.

        The first request may omit the binding so the client can discover the
        current workspace identity. Subsequent requests must echo it. The
        opaque cursor binds subsequent pages to one bounded, expiring snapshot.
        Root, directory identity and policy are revalidated on every request.
        """
        workspace = await self.workspace(session_key)
        if binding and binding != workspace.binding:
            raise WorkspaceFileUnavailableError()
        if limit < 1 or limit > MAX_DIRECTORY_PAGE_SIZE:
            raise WorkspaceFileUnavailableError()
        result = await asyncio.to_thread(self._directory_page, workspace, path, str(cursor), limit)
        if await self.workspace(session_key) != workspace:
            raise WorkspaceFileUnavailableError()
        return result

    def _directory_page(
        self, workspace: _Workspace, path: str, cursor: str, limit: int
    ) -> dict[str, Any]:
        target = _directory(workspace, path)
        relative = target.relative_to(workspace.root).as_posix()
        relative = "" if relative == "." else relative
        revision = _file_revision(native_io_path(target).lstat())
        if cursor in ("", "0") or cursor.isdigit():
            # Numeric offsets remain accepted for older clients. New clients
            # always receive a snapshot cursor, so pagination is stable.
            offset = int(cursor or "0")
            entries, truncated = _list_directory(workspace, path)
            if revision != _file_revision(native_io_path(_directory(workspace, path)).lstat()):
                raise WorkspaceDirectoryChangedError()
            token = uuid4().hex
            # Account for dict/str overhead as well as serialized values. No
            # file bytes are held in either cache.
            sizes = [512 + len(json.dumps(entry).encode()) * 4 for entry in entries]
            size = 512 + sum(sizes)
            while entries and size > self._directory_cache.max_bytes:
                entries.pop()
                size -= sizes.pop()
                truncated = True
            snapshot = _DirectorySnapshot(
                workspace,
                relative,
                revision,
                entries,
                truncated,
                time.monotonic() + DIRECTORY_SNAPSHOT_TTL_SECONDS,
            )
            self._directory_cache.put(token, snapshot, size)
        else:
            token, _, raw_offset = cursor.partition(":")
            if len(token) != 32 or not raw_offset.isdigit():
                raise WorkspaceDirectoryChangedError()
            offset = int(raw_offset)
            snapshot = self._directory_cache.get(token)
            if (
                snapshot is None
                or snapshot.expires_at <= time.monotonic()
                or snapshot.workspace != workspace
                or snapshot.relative_path != relative
                or snapshot.revision != revision
            ):
                raise WorkspaceDirectoryChangedError()
        page = snapshot.entries[offset : offset + limit]
        next_offset = offset + len(page)
        next_cursor = f"{token}:{next_offset}" if next_offset < len(snapshot.entries) else None
        return {
            "workspaceBinding": workspace.binding,
            "relativePath": relative,
            "directoryRevision": revision,
            "entries": page,
            "nextCursor": next_cursor,
            "hasMore": next_cursor is not None,
            "truncated": snapshot.truncated,
        }


_NOISY_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        "out",
        "coverage",
        "target",
        ".next",
        ".nuxt",
        ".turbo",
        ".pytest_cache",
        ".mypy_cache",
        ".gradle",
    }
)


def _directory_relative_path(workspace: _Workspace, raw: str) -> str:
    target = _directory(workspace, raw)
    relative = target.relative_to(workspace.root).as_posix()
    return "" if relative == "." else relative


def _directory(workspace: _Workspace, raw: str) -> Path:
    if len(raw) > 4096 or any(ord(char) < 32 for char in raw):
        raise WorkspaceFileUnavailableError()
    relative = raw.replace("\\", "/") or "."
    requested = Path(relative)
    if requested.is_absolute():
        try:
            relative = requested.relative_to(workspace.root).as_posix()
        except ValueError as exc:
            raise WorkspaceFileUnavailableError() from exc
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
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceFileUnavailableError()
    return target


def _list_directory(workspace: _Workspace, raw: str) -> tuple[list[dict[str, Any]], bool]:
    target = _directory(workspace, raw)
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        iterator = os.scandir(native_io_path(target))
        with iterator:
            for entry in iterator:
                name = entry.name
                if not name or name in (".", ".."):
                    continue
                try:
                    # ``native_io_path`` may use the Windows extended prefix
                    # for the scandir handle. Build the logical child from
                    # the already-normalized workspace path so policy checks
                    # and ``relative_to`` see the same spelling as the root.
                    child = target / name
                    if _is_reparse_point(child):
                        continue
                    if (
                        decide_path_access(
                            child, workspace=workspace.root, profile=workspace.profile
                        ).status
                        != "allowed"
                    ):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    is_directory = stat.S_ISDIR(info.st_mode)
                    is_file = stat.S_ISREG(info.st_mode)
                    if not is_directory and not is_file:
                        continue
                    if is_directory and name.casefold() in _NOISY_DIRECTORY_NAMES:
                        continue
                    relative = child.relative_to(workspace.root).as_posix()
                    mime = _workspace_file_mime(name)
                    kind = (
                        "directory"
                        if is_directory
                        else (
                            "image"
                            if mime in _IMAGE_MIMES
                            else (
                                "text"
                                if child.suffix.lower() in _TEXT_SUFFIXES
                                or mime.startswith("text/")
                                else "download"
                            )
                        )
                    )
                    entries.append(
                        {
                            "name": name,
                            "path": relative,
                            "type": "directory" if is_directory else "file",
                            "kind": kind,
                            "mime": mime if not is_directory else "",
                            "size": None if is_directory else info.st_size,
                            "mtimeNs": str(info.st_mtime_ns),
                            "resource": _resource_identity(workspace, relative),
                        }
                    )
                    if len(entries) > MAX_DIRECTORY_ENTRIES:
                        truncated = True
                        break
                except (OSError, ValueError):
                    continue
    except OSError as exc:
        raise WorkspaceFileUnavailableError() from exc
    entries.sort(
        key=lambda value: (
            0 if value["type"] == "directory" else 1,
            str(value["name"]).casefold(),
            value["path"],
        )
    )
    return entries[:MAX_DIRECTORY_ENTRIES], truncated


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
    workspace: _Workspace,
    raw: str,
    start_line: int,
    end_line: int,
    index_cache: _BoundedCache | None = None,
    expected_revision: str = "",
) -> tuple[Path, str, int, str]:
    if start_line < 1 or end_line < start_line or end_line - start_line >= MAX_TEXT_PAGE_LINES:
        raise WorkspaceFileUnavailableError()
    target, content, total, _, revision = _scan_text_file(
        workspace,
        raw,
        start_line=start_line,
        end_line=end_line,
        index_cache=index_cache,
        expected_revision=expected_revision,
    )
    if start_line > total:
        raise WorkspaceFileUnavailableError()
    return target, content, total, revision


def _resource_identity(workspace: _Workspace, relative_path: str) -> dict[str, str]:
    """Identity within a connection, deliberately independent of content version."""
    return {
        "kind": "workspace-file",
        "workspaceBinding": workspace.binding,
        "relativePath": relative_path,
    }


def _file_revision(metadata: os.stat_result) -> str:
    values = [
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    ]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _check_revision(actual: str, expected: str) -> None:
    if expected and (
        len(expected) != 64
        or any(char not in "0123456789abcdef" for char in expected)
        or not hmac.compare_digest(actual, expected)
    ):
        raise WorkspaceFileChangedError()


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
    index_cache: _BoundedCache | None = None,
    expected_revision: str = "",
) -> tuple[Path, str, int, int | None, str]:
    """Scan bounded UTF-8 input using exactly str.splitlines() line boundaries.

    Source line endings are preserved, including CRLF split across read chunks.
    The first scan validates all text and builds a sparse byte-offset index.
    Subsequent pages seek to the nearest checkpoint and read at most one page
    plus a checkpoint interval. Search still scans text, and refreshes the same
    index so its following preview page never repeats a full scan.
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
        revision = _file_revision(opened)
        _check_revision(revision, expected_revision)
        cache_key = (workspace.binding, target.relative_to(workspace.root).as_posix(), revision)
        index = index_cache.get(cache_key) if index_cache is not None else None
        use_index = index is not None and start_line > 0 and not query
        if use_index and start_line > index.total_lines:
            raise WorkspaceFileUnavailableError()
        checkpoint = (start_line - 1) // _TEXT_INDEX_STRIDE if use_index else 0
        offset = index.offsets[checkpoint] if use_index else 0
        if offset:
            stream.seek(offset)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        selected: list[str] = []
        selected_bytes = 0
        consumed = offset
        total = checkpoint * _TEXT_INDEX_STRIDE if use_index else 0
        indexed_bytes = offset
        offsets = array("I", [0])
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
                if (
                    chunk
                    and lines
                    and (
                        lines[-1].endswith("\r")
                        or lines[-1][-1] not in "\n\v\f\r\x1c\x1d\x1e\x85\u2028\u2029"
                    )
                ):
                    pending = lines.pop()
                if len(pending.encode("utf-8")) > MAX_TEXT_LINE_BYTES:
                    raise WorkspaceTextLimitError("line", MAX_TEXT_LINE_BYTES)
                for line in lines:
                    line_bytes = len(line.encode("utf-8"))
                    if line_bytes > MAX_TEXT_LINE_BYTES:
                        raise WorkspaceTextLimitError("line", MAX_TEXT_LINE_BYTES)
                    total += 1
                    indexed_bytes += line_bytes
                    if not use_index and total % _TEXT_INDEX_STRIDE == 0:
                        offsets.append(indexed_bytes)
                    if needle and match_line is None and needle in line.lower():
                        match_line = total
                    if start_line <= total <= end_line:
                        selected_bytes += line_bytes
                        if selected_bytes > MAX_TEXT_PAGE_BYTES:
                            raise WorkspaceTextLimitError("page", MAX_TEXT_PAGE_BYTES)
                        selected.append(line)
                if not chunk or (use_index and total >= end_line):
                    break
            after_read = os.fstat(stream.fileno())
        except (UnicodeError, ValueError) as exc:
            raise WorkspaceFileUnavailableError() from exc
    _, after = _file(workspace, raw)
    if (
        (not use_index and consumed != before.st_size)
        or not _same_file_snapshot(before, after)
        or not _same_file_snapshot(before, after_read)
        or opened.st_ctime_ns != after_read.st_ctime_ns
    ):
        raise WorkspaceFileUnavailableError()
    total_lines = index.total_lines if use_index else max(1, total)
    if not use_index and index_cache is not None:
        index_cache.put(
            cache_key,
            _TextIndex(offsets, total_lines),
            512 + len(offsets) * offsets.itemsize + len(str(cache_key)) * 4,
        )
    return target, "".join(selected), total_lines, match_line, revision


def _read_file(workspace: _Workspace, raw: str, expected_revision: str = "") -> tuple[Path, bytes]:
    target, before = _file(workspace, raw)
    flags = _read_flags()
    with os.fdopen(os.open(native_io_path(target), flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not _same_file_snapshot(before, opened):
            raise WorkspaceFileUnavailableError()
        _check_revision(_file_revision(opened), expected_revision)
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


def _readable_file(workspace: _Workspace, raw: str) -> tuple[Path, os.stat_result, str]:
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
    return target, after, _file_revision(opened)


def register_workspace_file_routes(
    app: Starlette,
    *,
    config: GatewayConfig,
    session_manager: Any = None,
) -> None:
    service = WorkspaceFiles(config, session_manager)

    async def guard(request: Request) -> Response | None:
        if not request_origin_allowed(request, config):
            return forbidden_origin_response()
        if not await request_principal_is_owner_async(config, request):
            return JSONResponse({"code": "OWNER_REQUIRED"}, status_code=403, headers=_HEADERS)
        return None

    def unavailable() -> Response:
        return JSONResponse(
            {"code": "WORKSPACE_FILE_UNAVAILABLE"}, status_code=404, headers=_HEADERS
        )

    def changed(*, directory: bool = False) -> Response:
        return JSONResponse(
            {"code": "WORKSPACE_DIRECTORY_CHANGED" if directory else "WORKSPACE_FILE_CHANGED"},
            status_code=409,
            headers=_HEADERS,
        )

    def text_limit(error: WorkspaceTextLimitError) -> Response:
        return JSONResponse(
            {"code": "WORKSPACE_TEXT_LIMIT", "limit": error.limit, "maxBytes": error.max_bytes},
            status_code=413,
            headers=_HEADERS,
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
        denied = await guard(request)
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
        denied = await guard(request)
        if denied is not None:
            return denied
        try:
            name, mime, data = await service.read(
                request.headers.get("x-opensquilla-session-key", ""),
                request.query_params.get("path", ""),
                request.query_params.get("workspaceBinding", ""),
                request.query_params.get("expectedRevision", ""),
            )
        except WorkspaceFileChangedError:
            return changed()
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
        denied = await guard(request)
        if denied is not None:
            return denied
        try:
            start_line = int(request.query_params.get("startLine", ""))
            end_line = int(request.query_params.get("endLine", ""))
        except ValueError:
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        if start_line < 1 or end_line < start_line or end_line - start_line >= MAX_TEXT_PAGE_LINES:
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            result = await service.read_page(
                request.headers.get("x-opensquilla-session-key", ""),
                request.query_params.get("path", ""),
                request.query_params.get("workspaceBinding", ""),
                start_line,
                end_line,
                request.query_params.get("expectedRevision", ""),
            )
        except WorkspaceFileChangedError:
            return changed()
        except WorkspaceTextLimitError as error:
            return text_limit(error)
        except (OSError, ValueError, RpcHandlerError, ProjectWorkspaceStateError):
            return unavailable()
        return JSONResponse(result, headers=_HEADERS)

    async def search(request: Request) -> Response:
        denied = await guard(request)
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
                request.query_params.get("expectedRevision", ""),
            )
        except WorkspaceFileChangedError:
            return changed()
        except WorkspaceTextLimitError as error:
            return text_limit(error)
        except (OSError, ValueError, RpcHandlerError, ProjectWorkspaceStateError):
            return unavailable()
        return JSONResponse(result, headers=_HEADERS)

    async def list_directory(request: Request) -> Response:
        denied = await guard(request)
        if denied is not None:
            return denied
        raw_cursor = request.query_params.get("cursor", "0")
        raw_limit = request.query_params.get("limit", str(MAX_DIRECTORY_PAGE_SIZE))
        if (
            not raw_limit.isdigit()
            or len(raw_cursor) > 64
            or (raw_cursor != "" and not all(char in "0123456789abcdef:" for char in raw_cursor))
            or len(raw_limit) > 4
        ):
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        limit = int(raw_limit)
        if limit < 1 or limit > MAX_DIRECTORY_PAGE_SIZE:
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        path = request.query_params.get("path", "")
        if len(path) > 4096:
            return JSONResponse({"code": "INVALID_REQUEST"}, status_code=400, headers=_HEADERS)
        try:
            result = await service.list_dir(
                request.headers.get("x-opensquilla-session-key", ""),
                path,
                request.query_params.get("workspaceBinding", ""),
                raw_cursor,
                limit,
            )
        except WorkspaceDirectoryChangedError:
            return changed(directory=True)
        except (OSError, ValueError, RpcHandlerError, ProjectWorkspaceStateError):
            return unavailable()
        return JSONResponse(result, headers=_HEADERS)

    async def metadata(request: Request) -> Response:
        denied = await guard(request)
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
            Route("/api/v1/workspace-files/list", list_directory, methods=["GET"]),
            Route("/api/v1/workspace-files/metadata", metadata, methods=["GET"]),
        ]
    )
    from opensquilla.gateway.workspace_file_apps import register_workspace_file_app_routes

    register_workspace_file_app_routes(app, config=config, service=service)
