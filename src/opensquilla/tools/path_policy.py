"""Shared path-intent checks for tool-facing filesystem inputs."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from opensquilla.tools.types import ToolError, WorkspaceAccessError

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_FOREIGN_POSIX_ROOTS = {
    "Applications",
    "Library",
    "System",
    "Users",
    "Volumes",
    "bin",
    "etc",
    "home",
    "cygdrive",
    "mnt",
    "opt",
    "private",
    "tmp",
    "usr",
    "var",
}


def resolve_bound_workspace(workspace: str | None) -> Path | None:
    """Validate an existing tool-context root without creating or retargeting it.

    Gateway bindings already name canonical paths. A missing or redirected
    root must not turn a relative operation into work in another directory.
    This checks the path's current meaning, not a permanent inode identity:
    restoring an ordinary directory at the same path remains supported.
    Unbound internal jobs retain their existing runtime/scratch fallback.
    """
    if not workspace:
        return None
    root = Path(workspace).expanduser().absolute()
    try:
        resolved = root.resolve(strict=True)
        if resolved != root:
            raise WorkspaceAccessError(
                "workspace_binding_changed: The active workspace path now points to a "
                "different location. Restore or reopen the project before using its files."
            )
        if not resolved.is_dir():
            raise NotADirectoryError(str(root))
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceAccessError(
            "workspace_unavailable: The active workspace is missing or inaccessible. "
            "Restore or reopen the project before using its files."
        ) from exc
    return resolved


def is_foreign_host_path(path: str, *, platform: str) -> bool:
    """Return True when *path* is an absolute path for a different host OS."""

    text = str(path).strip()
    if not text:
        return False
    if text.lower().startswith("file://"):
        return True

    if platform == "nt":
        normalized = text.replace("\\", "/")
        if not normalized.startswith("/") or normalized.startswith("//"):
            return False
        parts = PurePosixPath(normalized).parts
        return len(parts) >= 2 and (
            parts[1] in _FOREIGN_POSIX_ROOTS
            or bool(re.fullmatch(r"[A-Za-z]", parts[1]))
        )

    return bool(_WINDOWS_DRIVE_RE.match(text))


def foreign_host_path_error(path: str, *, workspace: Path | None = None) -> ToolError:
    text = str(path)
    name = (
        PurePosixPath(text.replace("\\", "/")).name
        or PureWindowsPath(text).name
        or "<filename>"
    )
    details = [
        "foreign_host_path: requested path is from another host/platform",
        f"requested path: {path}",
    ]
    if workspace is not None:
        details.append(f"active workspace: {workspace}")
    details.append(
        "Use a workspace-relative path after creating the file inside the active workspace"
        f" (for example: {name})."
    )
    return ToolError(". ".join(details))


def reject_foreign_host_path(
    path: str,
    *,
    platform: str,
    workspace: Path | None = None,
) -> None:
    if is_foreign_host_path(path, platform=platform):
        raise foreign_host_path_error(path, workspace=workspace)
