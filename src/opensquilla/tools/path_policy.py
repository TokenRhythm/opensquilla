"""Shared path-intent checks for tool-facing filesystem inputs."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from opensquilla.tools.types import ToolError

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
