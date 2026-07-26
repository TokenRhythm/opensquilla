"""Shared per-entry formatting for resilient directory listings."""

from __future__ import annotations

import errno
import stat
from pathlib import Path

_BROKEN_SYMLINK_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
_WINDOWS_ERROR_CANT_RESOLVE_FILENAME = 1921


def _is_broken_symlink_target_error(exc: OSError) -> bool:
    return (
        exc.errno in _BROKEN_SYMLINK_ERRNOS
        or getattr(exc, "winerror", None) == _WINDOWS_ERROR_CANT_RESOLVE_FILENAME
    )


def format_directory_entry(
    entry: Path,
    *,
    follow_target: bool = True,
    symlink_target_is_broken: bool = False,
) -> tuple[bool, str]:
    """Return ``(is_directory, display_line)`` without failing on child metadata.

    ``follow_target=False`` is the fail-closed form for callers whose path-policy
    resolution failed. It uses only ``lstat`` metadata and never follows a link.
    ``symlink_target_is_broken`` lets a caller preserve an already-confirmed
    symlink-loop result without following the target again.
    """

    try:
        metadata = entry.lstat()
    except OSError:
        return False, f"[file] {entry.name} (metadata unavailable)"
    mode = metadata.st_mode

    if stat.S_ISLNK(mode):
        if symlink_target_is_broken:
            return False, f"[link] {entry.name} (broken symlink)"
        if not follow_target:
            return False, f"[link] {entry.name} (target metadata unavailable)"
        try:
            size = entry.stat().st_size
        except OSError as exc:
            if _is_broken_symlink_target_error(exc):
                return False, f"[link] {entry.name} (broken symlink)"
            return False, f"[link] {entry.name} (target metadata unavailable)"
        return False, f"[link] {entry.name} ({size} bytes target)"

    if stat.S_ISDIR(mode):
        return True, f"[dir]  {entry.name}/"

    if not follow_target:
        return False, f"[file] {entry.name} ({metadata.st_size} bytes)"

    try:
        size = entry.stat().st_size
    except OSError:
        return False, f"[file] {entry.name} (size unavailable)"
    return False, f"[file] {entry.name} ({size} bytes)"


__all__ = ["format_directory_entry"]
