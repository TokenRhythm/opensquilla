"""Resolve Codex filesystem special paths for the current host platform."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Literal

FileSystemPlatform = Literal["linux", "macos", "windows"]

WINDOWS_PROFILE_READ_EXCLUSIONS = frozenset(
    {
        ".ssh",
        ".tsh",
        ".brev",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".config",
        ".npm",
        ".pki",
        ".terraform.d",
    }
)

WINDOWS_PLATFORM_READ_ROOTS = (
    PureWindowsPath(r"C:\Windows"),
    PureWindowsPath(r"C:\Program Files"),
    PureWindowsPath(r"C:\Program Files (x86)"),
    PureWindowsPath(r"C:\ProgramData"),
)


class FileSystemSpecialPath(StrEnum):
    ROOT = "root"
    SLASH_TMP = "slash_tmp"
    TMPDIR = "tmpdir"


@dataclass(frozen=True)
class FileSystemPlatformContext:
    platform: FileSystemPlatform
    cwd: PurePath
    home: PurePath
    helper_roots: tuple[PurePath, ...] = ()
    writable_roots: tuple[PurePath, ...] = ()
    user_profile_children: tuple[PurePath, ...] | None = None
    env: Mapping[str, str] = field(default_factory=dict)


def current_platform_context(
    *,
    cwd: Path,
    writable_roots: Iterable[PurePath] = (),
    helper_roots: Iterable[PurePath] = (),
) -> FileSystemPlatformContext:
    """Capture the host values needed to resolve platform-special paths."""

    platform = _current_platform()
    home_available = True
    try:
        home = Path.home()
    except (OSError, RuntimeError):
        home = cwd
        home_available = False
    user_profile_children: tuple[PurePath, ...] | None = None
    if platform == "windows":
        if not home_available:
            user_profile_children = ()
        else:
            try:
                user_profile_children = tuple(home.iterdir())
            except OSError:
                user_profile_children = ()
    return FileSystemPlatformContext(
        platform=platform,
        cwd=cwd,
        home=home,
        helper_roots=tuple(helper_roots),
        writable_roots=tuple(writable_roots),
        user_profile_children=user_profile_children,
        env=dict(os.environ),
    )


def resolve_special_path(
    special: FileSystemSpecialPath,
    context: FileSystemPlatformContext,
) -> tuple[PurePath, ...]:
    """Resolve one symbolic Codex filesystem root in stable declaration order."""

    if special is FileSystemSpecialPath.ROOT:
        if context.platform != "windows":
            return (PurePosixPath("/"),)
        exclusions = {name.casefold() for name in WINDOWS_PROFILE_READ_EXCLUSIONS}
        profile_roots = (
            path
            for path in context.user_profile_children or ()
            if path.name.casefold() not in exclusions
        )
        return _deduplicate_paths(
            (
                *WINDOWS_PLATFORM_READ_ROOTS,
                *(_as_windows_path(path) for path in context.helper_roots),
                *(_as_windows_path(path) for path in profile_roots),
                _as_windows_path(context.cwd),
                *(_as_windows_path(path) for path in context.writable_roots),
            )
        )

    if special is FileSystemSpecialPath.SLASH_TMP:
        if context.platform == "windows":
            return ()
        return (PurePosixPath("/tmp"),)

    if context.platform == "windows":
        paths: list[PurePath] = []
        for name in ("TEMP", "TMP", "TMPDIR"):
            if not (raw := context.env.get(name)):
                continue
            path = _as_windows_path(raw)
            if path.is_absolute():
                paths.append(path)
        return _deduplicate_paths(paths)

    raw_tmpdir = context.env.get("TMPDIR")
    if not raw_tmpdir:
        return ()
    tmpdir = PurePosixPath(raw_tmpdir)
    return (tmpdir,) if tmpdir.is_absolute() else ()


def resolve_temp_write_paths(
    context: FileSystemPlatformContext,
    *,
    include_slash_tmp: bool,
    include_tmpdir: bool,
) -> tuple[PurePath, ...]:
    """Resolve the enabled temporary write roots with stable de-duplication."""

    paths: list[PurePath] = []
    if include_slash_tmp:
        paths.extend(resolve_special_path(FileSystemSpecialPath.SLASH_TMP, context))
    if include_tmpdir:
        paths.extend(resolve_special_path(FileSystemSpecialPath.TMPDIR, context))
    return _deduplicate_paths(paths)


def _current_platform() -> FileSystemPlatform:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _as_windows_path(path: str | PurePath) -> PureWindowsPath:
    return PureWindowsPath(str(path))


def _deduplicate_paths(paths: Iterable[PurePath]) -> tuple[PurePath, ...]:
    unique: list[PurePath] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        if isinstance(path, PureWindowsPath):
            key = ("windows", path.as_posix().casefold())
        else:
            key = ("posix", path.as_posix())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return tuple(unique)


__all__ = [
    "FileSystemPlatform",
    "FileSystemPlatformContext",
    "FileSystemSpecialPath",
    "WINDOWS_PLATFORM_READ_ROOTS",
    "WINDOWS_PROFILE_READ_EXCLUSIONS",
    "current_platform_context",
    "resolve_special_path",
    "resolve_temp_write_paths",
]
