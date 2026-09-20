"""Session-owned editable copies of immutable attachment inputs.

Path selection is not authorization. Callers must authorize both the source read
and target write and execute ``copy_attachment_file`` in the filesystem worker.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from opensquilla.attachment_workspace import AttachmentWorkspaceConflictError, _safe_path_segment

_MAX_COPY_BYTES = 160 * 1024 * 1024


def attachment_original_key(path: Path, workspace: Path) -> str | None:
    """Recognize only the controlled immutable layout, never arbitrary filenames."""
    try:
        parts = path.relative_to(workspace).parts
    except ValueError:
        return None
    if (
        len(parts) == 4
        and parts[:2] == (".opensquilla", "attachments")
        and re.match(r"^[0-9a-f]{12}-.+", parts[3])
    ):
        return Path(*parts).as_posix()
    return None


def working_path_for_entry(
    entry: dict[str, Any],
    *,
    workspace: Path,
    session_id: str,
) -> Path:
    recorded_root = entry.get("workspace_root")
    if not isinstance(recorded_root, str) or Path(recorded_root) != workspace:
        raise AttachmentWorkspaceConflictError("attachment working-file workspace binding changed")
    raw = entry.get("path")
    if not isinstance(raw, str) or not raw or entry.get("session_id") != session_id:
        raise AttachmentWorkspaceConflictError("attachment working-file session mismatch")
    expected_root = (
        workspace
        / ".opensquilla"
        / "attachments"
        / _safe_path_segment(session_id, fallback="session")
        / "working"
    )
    candidate = workspace / raw
    if candidate.parent != expected_root or candidate.resolve().parent != expected_root:
        raise AttachmentWorkspaceConflictError(
            "attachment working-file path is invalid or redirected"
        )
    if not candidate.is_file() or candidate.is_symlink():
        raise AttachmentWorkspaceConflictError(
            "attachment working file is missing or redirected; original was not substituted"
        )
    return candidate


def copy_attachment_file(source: Path, target: Path, expected_sha: str = "") -> str:
    """Copy once, verify the immutable source, and never replace a conflict."""
    return _copy_attachment_file(source, target, expected_sha or source.name.split("-", 1)[0])


def _copy_attachment_file(source: Path, target: Path, expected_sha: str | None) -> str:
    expected = source.lstat()
    if not stat.S_ISREG(expected.st_mode) or bool(
        getattr(expected, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    ):
        raise AttachmentWorkspaceConflictError("attachment source is not a regular file")
    descriptor = os.open(
        source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    with os.fdopen(descriptor, "rb") as original:
        opened = os.fstat(original.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            expected.st_dev, expected.st_ino,
        ):
            raise AttachmentWorkspaceConflictError("attachment source changed while opening")
        if opened.st_size > _MAX_COPY_BYTES:
            raise AttachmentWorkspaceConflictError("attachment exceeds the working-copy byte limit")
        if target.exists() or target.is_symlink():
            raise AttachmentWorkspaceConflictError("attachment working-file target already exists")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
        digest = hashlib.sha256()
        count = 0
        try:
            with temporary.open("xb") as output:
                while chunk := original.read(1024 * 1024):
                    count += len(chunk)
                    if count > _MAX_COPY_BYTES:
                        raise AttachmentWorkspaceConflictError(
                            "attachment exceeds the copy byte limit"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            after = os.fstat(original.fileno())
            if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                raise AttachmentWorkspaceConflictError("attachment source changed while copying")
            sha = digest.hexdigest()
            if expected_sha is not None and not sha.startswith(expected_sha):
                raise AttachmentWorkspaceConflictError("immutable attachment content conflict")
            os.chmod(temporary, 0o600)
            os.link(temporary, target)
            return sha
        finally:
            temporary.unlink(missing_ok=True)


async def fork_attachment_working_files(
    parent_map: dict[str, dict[str, Any]],
    *,
    source_workspace: str | Path,
    destination_workspace: str | Path,
    source_session_id: str,
    child_session_id: str,
    allowed_hashes: set[str] | None = None,
    copy_file: Callable[[Path, Path], Awaitable[None]],
) -> dict[str, dict[str, Any]]:
    """Snapshot only the selected branch's editable files into its independent scope."""
    source_root = Path(source_workspace).expanduser().resolve()
    destination_root = Path(destination_workspace).expanduser().resolve()
    parent_scope = _safe_path_segment(source_session_id, fallback="session")
    child_scope = _safe_path_segment(child_session_id, fallback="session")
    if source_session_id == child_session_id:
        raise AttachmentWorkspaceConflictError("fork requires an independent session scope")
    result: dict[str, dict[str, Any]] = {}
    for key, entry in parent_map.items():
        if not entry.get("path"):
            continue
        original = source_root / key
        if attachment_original_key(original, source_root) != key:
            raise AttachmentWorkspaceConflictError("invalid attachment original in fork metadata")
        if Path(key).parts[2] != parent_scope:
            raise AttachmentWorkspaceConflictError("fork attachment source session mismatch")
        sha = entry.get("sha256", "")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise AttachmentWorkspaceConflictError("fork attachment original hash is missing")
        if not original.name.startswith(sha[:12] + "-"):
            raise AttachmentWorkspaceConflictError("fork attachment original hash mismatch")
        if allowed_hashes is not None and sha not in allowed_hashes:
            continue
        source = working_path_for_entry(
            entry,
            workspace=source_root,
            session_id=source_session_id,
        )
        child_key = Path(".opensquilla", "attachments", child_scope, original.name).as_posix()
        target = destination_root / ".opensquilla" / "attachments" / child_scope / "working"
        target = target / original.name
        if target.parent.resolve() != target.parent:
            raise AttachmentWorkspaceConflictError("fork working directory is redirected")
        await copy_file(source, target)
        result[child_key] = {
            "path": target.relative_to(destination_root).as_posix(),
            "sha256": sha,
            "session_id": child_session_id,
            "workspace_root": str(destination_root),
        }
    return result


def snapshot_attachment_working_file(source: Path, target: Path) -> str:
    """Worker-only snapshot of one bounded, identity-pinned regular source file."""
    return _copy_attachment_file(source, target, None)
