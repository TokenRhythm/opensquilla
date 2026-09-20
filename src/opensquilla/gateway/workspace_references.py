"""Read revision-pinned source references inside an authorized session workspace."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any

from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.gateway.project_workspace_runtime import (
    authoritative_project_run_context,
    map_project_workspace_error,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.project_workspaces import (
    ProjectWorkspaceStateError,
    resolve_validated_project_workspace,
)
from opensquilla.sandbox.sensitive_paths import sensitive_path_marker
from opensquilla.session.keys import canonicalize_session_key
from opensquilla.tools.source_edit_contract import (
    workspace_file_reference,
    workspace_reference_id,
)

_MAX_SOURCE_BYTES = 1024 * 1024
_REVISION = re.compile(r"file_[0-9a-f]{16}")


def _invalid(message: str) -> RpcHandlerError:
    return RpcHandlerError("INVALID_REFERENCE", message)


def _validated_reference(params: Any) -> tuple[str, dict[str, Any], str, str, int, int]:
    if not isinstance(params, dict):
        raise _invalid("A source reference and sessionKey are required.")
    key = params.get("sessionKey")
    if not isinstance(key, str) or not key.strip():
        raise _invalid("sessionKey is required.")
    key = canonicalize_session_key(key)
    reference = params.get("reference")
    if not isinstance(reference, dict) or (
        type(reference.get("version")) is not int
        or reference["version"] != 1
        or reference.get("kind") != "workspace_file"
    ):
        raise _invalid("A version 1 workspace file reference is required.")
    scope = reference.get("scope")
    locator = reference.get("locator")
    state = reference.get("state")
    capabilities = reference.get("capabilities")
    if (
        not isinstance(scope, dict) or not isinstance(locator, dict)
        or not isinstance(state, dict) or not isinstance(capabilities, dict)
    ):
        raise _invalid("The reference is incomplete.")
    if capabilities.get("open") is not True or state.get("available") is not True:
        raise _invalid("The source reference is unavailable.")
    scoped_key = scope.get("sessionKey")
    if scoped_key is not None and (
        not isinstance(scoped_key, str) or not scoped_key.strip()
        or canonicalize_session_key(scoped_key) != key
    ):
        raise RpcHandlerError("WORKSPACE_MISMATCH", "The reference belongs to another session.")
    # No stable Gateway identity is advertised yet. Never silently accept one
    # supplied by a different client/instance that we cannot verify.
    if scope.get("gatewayInstanceId") is not None:
        raise RpcHandlerError("WORKSPACE_MISMATCH", "The Gateway instance cannot be verified.")
    relative = locator.get("relativePath")
    if not isinstance(relative, str) or (
        not relative or len(relative) > 4096 or relative != relative.strip()
        or "\\" in relative or ":" in relative or relative.startswith("/")
        or any(ord(char) < 32 for char in relative)
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or reference.get("id") != relative
    ):
        raise _invalid("The source path must be a canonical workspace-relative path.")
    revision = state.get("revision")
    if not isinstance(revision, str) or _REVISION.fullmatch(revision) is None:
        raise _invalid("A source revision is required.")
    start = locator.get("startLine")
    end = locator.get("endLine")
    if type(start) is not int or type(end) is not int or start < 1 or end < start:
        raise _invalid("The source line range is invalid.")
    return key, reference, relative, revision, start, end


def _read_source(root: Path, relative: str, revision: str) -> str:
    try:
        root = root.resolve(strict=True)
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root):
            raise _invalid("The source path leaves the workspace.")
        if sensitive_path_marker(str(path), workspace=root) is not None:
            raise RpcHandlerError("FILE_UNAVAILABLE", "This file cannot be previewed.")
        # Open without following a replaced final symlink, and never block on
        # a device/FIFO. Recheck both the path and descriptor before reading.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            current = (root / relative).resolve(strict=True)
            if current != path or not current.is_relative_to(root):
                raise _invalid("The source path changed while opening.")
            current_info = current.stat()
            if (info.st_dev, info.st_ino) != (current_info.st_dev, current_info.st_ino):
                raise _invalid("The source file changed while opening.")
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_SOURCE_BYTES:
                raise RpcHandlerError("FILE_UNAVAILABLE", "Preview requires a file under 1 MiB.")
            data = handle.read(_MAX_SOURCE_BYTES + 1)
        if len(data) > _MAX_SOURCE_BYTES:
            raise RpcHandlerError("FILE_UNAVAILABLE", "Preview requires a file under 1 MiB.")
        actual_revision = f"file_{hashlib.sha256(data).hexdigest()[:16]}"
        if actual_revision != revision:
            raise RpcHandlerError("STALE_REFERENCE", "The file changed. Read it again to refresh.")
        if b"\x00" in data:
            raise RpcHandlerError("FILE_UNAVAILABLE", "Only UTF-8 source files can be previewed.")
        return data.decode("utf-8")
    except FileNotFoundError:
        raise RpcHandlerError("FILE_NOT_FOUND", "The source file no longer exists.") from None
    except (OSError, UnicodeError, RuntimeError):
        raise RpcHandlerError("FILE_UNAVAILABLE", "The source file cannot be previewed.") from None


async def read_workspace_reference(params: Any, ctx: RpcContext) -> dict[str, Any]:
    # Match the workspace catalogue: local filesystem access is an owner-only
    # capability; operator.read alone and shared/guest channel roles cannot grant it.
    if not ctx.principal.is_owner:
        raise RpcHandlerError("OWNER_REQUIRED", "Source previews require a locally proven owner.")
    key, reference, relative, revision, start, end = _validated_reference(params)
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcHandlerError("UNAVAILABLE", "Session storage is unavailable.")
    session = await storage.get_session(key)
    if session is None:
        raise RpcHandlerError("SESSION_NOT_FOUND", "The session no longer exists.")
    try:
        context, guard = await authoritative_project_run_context(
            storage=storage,
            session_manager=ctx.session_manager,
            session=session,
            config=ctx.config,
            default_workspace=str(resolve_agent_workspace_dir(session.agent_id, ctx.config)),
        )
        if not context.workspace:
            raise RpcHandlerError("WORKSPACE_UNAVAILABLE", "The session workspace is unavailable.")
        root = Path(context.workspace)
        workspace_id = session.workspace_id or workspace_reference_id(root)
        scoped_workspace = reference["scope"].get("workspaceId")
        if scoped_workspace is not None and scoped_workspace != workspace_id:
            raise RpcHandlerError(
                "WORKSPACE_MISMATCH", "The reference belongs to another workspace.",
            )
        content = await asyncio.to_thread(_read_source, root, relative, revision)
        if guard is not None:
            refreshed = await resolve_validated_project_workspace(storage, guard.workspace_id)
            if refreshed.guard != guard:
                raise RpcHandlerError("WORKSPACE_MISMATCH", "The workspace changed while opening.")
    except ProjectWorkspaceStateError as exc:
        raise map_project_workspace_error(exc, owner=True) from exc
    total_lines = len(content.splitlines())
    if end > total_lines:
        raise _invalid("The source line range exceeds the file length.")
    resolved = workspace_file_reference(
        relative, revision=revision, start_line=start, end_line=end,
        session_key=key, workspace_id=workspace_id,
    )
    return {
        "reference": resolved,
        "relativePath": relative,
        "revision": revision,
        "content": content,
        "totalLines": total_lines,
        "startLine": start,
        "endLine": end,
    }
