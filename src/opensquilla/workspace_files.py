"""Live, session-bound project inputs, distinct from immutable upload material.

This composition facade normalizes the shared input contract and lazily probes
access through the active tool context and sandbox executor. The contracts,
tools, and sandbox packages do not depend on this facade.
"""

from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from opensquilla.contracts.attachments import MAX_ATTACHMENTS
from opensquilla.execution_workspaces import validate_execution_workspace
from opensquilla.project_workspaces import resolve_validated_project_workspace

MAX_WORKSPACE_FILES = MAX_ATTACHMENTS


def normalize_workspace_files(value: object) -> list[dict[str, Any]]:
    """Validate wire metadata without granting access or snapshotting bytes."""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > MAX_WORKSPACE_FILES:
        raise ValueError(f"workspaceFiles must contain at most {MAX_WORKSPACE_FILES} files")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {
            "workspaceId",
            "relativePath",
            "name",
            "mime",
            "size",
        }:
            raise ValueError("invalid workspace file reference")
        identity, relative = item.get("workspaceId"), item.get("relativePath")
        if not isinstance(identity, str) or not identity or len(identity) > 256:
            raise ValueError("workspace file requires a workspace identity")
        if (
            not isinstance(relative, str)
            or not relative
            or len(relative) > 4096
            or "\\" in relative
            or any(ord(char) < 32 for char in relative)
            or PurePosixPath(relative).is_absolute()
            or PureWindowsPath(relative).drive
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or ":" in relative
        ):
            raise ValueError("workspace file requires a canonical relative path")
        name, mime = item.get("name"), item.get("mime")
        if not isinstance(name, str) or not name or len(name) > 1024:
            raise ValueError("workspace file requires a display name")
        if not isinstance(mime, str) or not mime or len(mime) > 256:
            raise ValueError("workspace file requires a MIME type")
        if any(ord(char) < 32 for char in name + mime):
            raise ValueError("workspace display metadata contains control characters")
        clean: dict[str, Any] = {
            "workspaceId": identity,
            "relativePath": relative,
            "name": name,
            "mime": mime,
        }
        if "size" in item:
            size = item["size"]
            if type(size) is not int or size < 0:
                raise ValueError("workspace file size must be non-negative")
            clean["size"] = size
        key = (identity, relative)
        if key not in seen:
            result.append(clean)
            seen.add(key)
    return result


@dataclass(frozen=True)
class ResolvedWorkspaceFile:
    ref: dict[str, Any]
    path: Path


async def session_workspace_binding(session: Any, storage: Any) -> tuple[str, Path]:
    project_id = getattr(session, "workspace_id", None)
    if project_id:
        project = await resolve_validated_project_workspace(storage, project_id)
        return project_id, Path(project.canonical_path)
    binding = getattr(session, "execution_workspace", None)
    if binding is None:
        raise ValueError("workspace file binding is unavailable")
    validated = await asyncio.to_thread(validate_execution_workspace, binding)
    return validated["id"], Path(validated["root"])


def canonical_workspace_file(root: Path, relative: str) -> Path:
    logical = root.joinpath(*PurePosixPath(relative).parts)
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or bool(
            getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise ValueError("workspace file mapping changed (link or junction)")
    resolved = logical.resolve(strict=True)
    if resolved != logical or not resolved.is_relative_to(root):
        raise ValueError("workspace file mapping changed")
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise ValueError("workspace input must be a regular file")
    return resolved


async def probe_file_access(path: Path, tool_context: Any) -> None:
    """Use the file tools' policy and actual execution backend, without parsing."""
    from opensquilla.sandbox.operation_runtime import SandboxOperation
    from opensquilla.tools.builtin.filesystem import (
        _active_filesystem_run_mode,
        _filesystem_operation_workspace,
        _gate_workspace_strict_read,
        _run_sandbox_operation_if_required,
        _sandbox_path_access_envelope,
        _sensitive_access_block,
    )
    from opensquilla.tools.types import current_tool_context

    expected = path.stat()
    canonical = path.resolve(strict=True)
    token = current_tool_context.set(tool_context)
    try:
        blocked = _sensitive_access_block("read_file", path, str(path))
        blocked = blocked or _sandbox_path_access_envelope(path, write=False)
        if blocked is not None:
            raise PermissionError(str(blocked.get("reason") or "workspace file access denied"))
        _gate_workspace_strict_read("read_file", path, str(path))
        workspace = _filesystem_operation_workspace()
        if workspace is not None:
            result = await _run_sandbox_operation_if_required(
                SandboxOperation.filesystem(
                    kind="probe_file",
                    workspace=workspace,
                    run_mode=_active_filesystem_run_mode(),
                    path=path,
                    paths=(path,),
                    display_path=str(path),
                )
            )
            if result is not None:
                metadata = getattr(result, "metadata", {})
                if (metadata.get("device"), metadata.get("inode")) != (
                    expected.st_dev,
                    expected.st_ino,
                ):
                    raise ValueError("workspace file changed while opening")
                if path.resolve(strict=True) != canonical:
                    raise ValueError("workspace file mapping changed")
                return

        def read_probe() -> None:
            descriptor = os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            )
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                    raise ValueError("workspace file changed while opening")
                if not stat.S_ISREG(opened.st_mode):
                    raise ValueError("workspace input must be a regular file")
                stream.read(1)

        await asyncio.to_thread(read_probe)
        if path.resolve(strict=True) != canonical:
            raise ValueError("workspace file mapping changed")
    finally:
        current_tool_context.reset(token)


async def validate_workspace_files(
    refs: object,
    *,
    session: Any,
    storage: Any,
    tool_context: Any,
) -> list[ResolvedWorkspaceFile]:
    normalized = normalize_workspace_files(refs)
    if not normalized:
        return []
    identity, root = await session_workspace_binding(session, storage)
    actual_root = getattr(tool_context, "workspace_dir", None)
    if not actual_root or Path(actual_root).resolve(strict=True) != root:
        raise ValueError("workspace file execution mapping changed")
    results = []
    for ref in normalized:
        if ref["workspaceId"] != identity:
            raise ValueError("workspace file binding changed")
        path = await asyncio.to_thread(canonical_workspace_file, root, ref["relativePath"])
        await probe_file_access(path, tool_context)
        results.append(ResolvedWorkspaceFile(ref, path))
    return results
