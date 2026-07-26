"""Canonical filesystem permissions shared by sandbox backends and direct tools.

The default Linux workspace profile mirrors Codex's normal sandbox posture:
the host filesystem is readable, only declared roots are writable, and agent
metadata inside writable project roots is re-protected as read-only.  Explicit
denied-read entries are policy, not a built-in list of "sensitive" path names.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

from opensquilla.sandbox.platform_permissions import (
    FileSystemPlatformContext,
    FileSystemSpecialPath,
    current_platform_context,
    resolve_special_path,
    resolve_temp_write_paths,
)

PROTECTED_METADATA_NAMES = (".git", ".agents", ".codex")


class FileSystemAccess(StrEnum):
    """Effective access granted to one canonical host path."""

    DENY = "deny"
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class FileSystemPermissionEntry:
    """An access rule rooted at ``path``.

    More-specific paths win.  If two entries have identical specificity, the
    later declaration wins, allowing protected and denied subpaths to be
    layered over broad writable roots.
    """

    path: PurePath
    access: FileSystemAccess


@dataclass(frozen=True)
class FileSystemPermissionProfile:
    """Resolved filesystem policy for one sandboxed operation."""

    entries: tuple[FileSystemPermissionEntry, ...]
    denied_read_globs: tuple[str, ...] = ()
    default_access: FileSystemAccess = FileSystemAccess.DENY

    @classmethod
    def workspace(
        cls,
        *,
        workspace: PurePath,
        readable_roots: Iterable[PurePath] = (),
        writable_roots: Iterable[PurePath] = (),
        denied_read_roots: Iterable[PurePath] = (),
        denied_read_globs: Iterable[str] = (),
        host_root_readonly: bool = True,
        tmp_writable: bool = True,
        tmpdir_env_writable: bool = True,
        protect_metadata: bool = True,
        platform_context: FileSystemPlatformContext | None = None,
    ) -> FileSystemPermissionProfile:
        """Build Codex's host-readable, declared-roots-writable profile."""

        workspace = _canonical(workspace)
        declared_writable = [workspace, *(_canonical(path) for path in writable_roots)]
        context = platform_context or current_platform_context(
            cwd=workspace,  # type: ignore[arg-type]
            writable_roots=tuple(declared_writable),
        )
        declared_writable.extend(
            _canonical_platform_path(path, context)
            for path in resolve_temp_write_paths(
                context,
                include_slash_tmp=tmp_writable,
                include_tmpdir=tmpdir_env_writable,
            )
        )
        declared_writable = list(_deduplicate_paths(declared_writable))

        entries: list[FileSystemPermissionEntry] = []
        if host_root_readonly:
            entries.extend(
                FileSystemPermissionEntry(
                    _canonical_platform_path(path, context),
                    FileSystemAccess.READ,
                )
                for path in resolve_special_path(FileSystemSpecialPath.ROOT, context)
            )
        entries.extend(
            FileSystemPermissionEntry(_lexical_absolute(path), FileSystemAccess.READ)
            for path in readable_roots
        )
        entries.extend(
            FileSystemPermissionEntry(path, FileSystemAccess.WRITE) for path in declared_writable
        )
        if protect_metadata:
            entries.extend(
                FileSystemPermissionEntry(root / name, FileSystemAccess.READ)
                for root in declared_writable
                for name in PROTECTED_METADATA_NAMES
            )
        entries.extend(
            FileSystemPermissionEntry(_lexical_absolute(path), FileSystemAccess.DENY)
            for path in denied_read_roots
        )
        return cls(
            entries=tuple(entries),
            denied_read_globs=tuple(str(pattern) for pattern in denied_read_globs),
        )

    @classmethod
    def read_only(
        cls,
        *,
        readable_roots: Iterable[PurePath] = (),
        denied_read_roots: Iterable[PurePath] = (),
        denied_read_globs: Iterable[str] = (),
        host_root_readonly: bool = True,
        platform_context: FileSystemPlatformContext | None = None,
    ) -> FileSystemPermissionProfile:
        context = platform_context or current_platform_context(cwd=Path.cwd())
        entries: list[FileSystemPermissionEntry] = []
        if host_root_readonly:
            entries.extend(
                FileSystemPermissionEntry(
                    _canonical_platform_path(path, context),
                    FileSystemAccess.READ,
                )
                for path in resolve_special_path(FileSystemSpecialPath.ROOT, context)
            )
        entries.extend(
            FileSystemPermissionEntry(_lexical_absolute(path), FileSystemAccess.READ)
            for path in readable_roots
        )
        entries.extend(
            FileSystemPermissionEntry(_lexical_absolute(path), FileSystemAccess.DENY)
            for path in denied_read_roots
        )
        return cls(tuple(entries), tuple(str(pattern) for pattern in denied_read_globs))

    @classmethod
    def full_access(cls) -> FileSystemPermissionProfile:
        """Return an unrestricted profile for explicit sandbox-disabled mode."""

        return cls(entries=(), default_access=FileSystemAccess.WRITE)

    def as_read_only(self) -> FileSystemPermissionProfile:
        """Remove every write grant while preserving explicit denied reads."""

        return FileSystemPermissionProfile(
            entries=tuple(
                FileSystemPermissionEntry(
                    entry.path,
                    (
                        FileSystemAccess.DENY
                        if entry.access is FileSystemAccess.DENY
                        else FileSystemAccess.READ
                    ),
                )
                for entry in self.entries
            ),
            denied_read_globs=self.denied_read_globs,
            default_access=(
                FileSystemAccess.READ
                if self.default_access is FileSystemAccess.WRITE
                else self.default_access
            ),
        )

    def resolve(self, path: PurePath) -> FileSystemAccess:
        """Return effective access for ``path`` using canonical longest match."""

        candidate = _canonical(path)
        if any(_matches_denied_read_glob(candidate, pattern) for pattern in self.denied_read_globs):
            return FileSystemAccess.DENY

        matches = (
            (len(root.parts), index, entry.access)
            for index, entry in enumerate(self.entries)
            if candidate.is_relative_to(root := _canonical(entry.path))
        )
        return max(
            matches,
            key=lambda match: (match[0], match[1]),
            default=(0, -1, self.default_access),
        )[2]

    def is_explicitly_denied(self, path: PurePath) -> bool:
        """Distinguish an explicit deny from a path with no matching grant."""

        candidate = _canonical(path)
        if any(_matches_denied_read_glob(candidate, pattern) for pattern in self.denied_read_globs):
            return True
        matches = [
            (len(root.parts), index, entry.access)
            for index, entry in enumerate(self.entries)
            if candidate.is_relative_to(root := _canonical(entry.path))
        ]
        if not matches:
            return False
        return max(matches, key=lambda match: (match[0], match[1]))[2] is FileSystemAccess.DENY

    def protected_metadata_root(self, path: PurePath) -> PurePath | None:
        """Return the matching default metadata carveout, when one applies."""

        candidate = _canonical(path)
        matches = [
            root
            for entry in self.effective_entries
            if entry.access is FileSystemAccess.READ
            and (root := _canonical(entry.path)).name in PROTECTED_METADATA_NAMES
            and candidate.is_relative_to(root)
        ]
        return max(matches, key=lambda item: len(item.parts), default=None)

    @property
    def has_denied_reads(self) -> bool:
        return bool(self.denied_read_globs) or any(
            entry.access is FileSystemAccess.DENY for entry in self.effective_entries
        )

    @property
    def unsandboxed_execution_allowed(self) -> bool:
        """Codex forbids a no-sandbox override when denied reads are active."""

        return not self.has_denied_reads

    @property
    def effective_entries(self) -> tuple[FileSystemPermissionEntry, ...]:
        """Return only each canonical target's final declaration."""

        final_by_target: dict[tuple[str, str], tuple[int, FileSystemPermissionEntry]] = {}
        for index, entry in enumerate(self.entries):
            path = _canonical(entry.path)
            final_by_target[_canonical_key(path)] = (
                index,
                FileSystemPermissionEntry(path, entry.access),
            )
        return tuple(
            entry for _, entry in sorted(final_by_target.values(), key=lambda item: item[0])
        )

    @property
    def readable_roots(self) -> tuple[PurePath, ...]:
        return tuple(
            entry.path for entry in self.effective_entries if entry.access is FileSystemAccess.READ
        )

    @property
    def writable_roots(self) -> tuple[PurePath, ...]:
        return tuple(
            entry.path for entry in self.effective_entries if entry.access is FileSystemAccess.WRITE
        )

    def read_only_subpaths(self, writable_root: PurePath) -> tuple[PurePath, ...]:
        root = _canonical(writable_root)
        return tuple(
            entry.path
            for entry in self.effective_entries
            if entry.access is not FileSystemAccess.WRITE
            and entry.path != root
            and entry.path.is_relative_to(root)
        )

    @property
    def has_full_disk_read_baseline(self) -> bool:
        if self.default_access in (FileSystemAccess.READ, FileSystemAccess.WRITE):
            return True
        return any(
            entry.access in (FileSystemAccess.READ, FileSystemAccess.WRITE)
            and isinstance(entry.path, PurePosixPath)
            and entry.path == PurePosixPath("/")
            for entry in self.effective_entries
        )

    @property
    def denied_read_roots(self) -> tuple[PurePath, ...]:
        return tuple(
            entry.path for entry in self.effective_entries if entry.access is FileSystemAccess.DENY
        )


def _canonical(path: PurePath) -> PurePath:
    if isinstance(path, Path):
        return path.expanduser().resolve(strict=False)
    return path


def _lexical_absolute(path: PurePath) -> PurePath:
    if isinstance(path, Path):
        return path.expanduser().absolute()
    return path


def _canonical_platform_path(
    path: PurePath,
    context: FileSystemPlatformContext,
) -> PurePath:
    same_concrete_flavor = isinstance(context.cwd, Path) and (
        (isinstance(context.cwd, PureWindowsPath) and isinstance(path, PureWindowsPath))
        or (isinstance(context.cwd, PurePosixPath) and isinstance(path, PurePosixPath))
    )
    if same_concrete_flavor:
        return _canonical(Path(str(path)))
    return _canonical(path)


def _canonical_key(path: PurePath) -> tuple[str, str]:
    if isinstance(path, PureWindowsPath):
        return ("windows", path.as_posix().casefold())
    return ("posix", path.as_posix())


def _deduplicate_paths(paths: Iterable[PurePath]) -> tuple[PurePath, ...]:
    unique: list[PurePath] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        key = _canonical_key(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return tuple(unique)


def _canonical_glob(pattern: str) -> str:
    expanded = os.path.expanduser(pattern)
    path = Path(expanded)
    parts = path.parts
    wildcard_index = next(
        (index for index, part in enumerate(parts) if any(char in part for char in "*?[")),
        len(parts),
    )
    if wildcard_index == 0:
        return expanded.replace("\\", "/")
    prefix = Path(*parts[:wildcard_index]).resolve(strict=False)
    if wildcard_index == len(parts):
        return prefix.as_posix()
    suffix = "/".join(parts[wildcard_index:])
    return f"{prefix.as_posix().rstrip('/')}/{suffix}"


def _matches_denied_read_glob(candidate: PurePath, pattern: str) -> bool:
    candidate_text = candidate.as_posix()
    canonical_pattern = _canonical_glob(pattern)
    if isinstance(candidate, PureWindowsPath):
        candidate_text = candidate_text.casefold()
        canonical_pattern = canonical_pattern.casefold()
    return fnmatch.fnmatchcase(candidate_text, canonical_pattern)


__all__ = [
    "FileSystemAccess",
    "FileSystemPermissionEntry",
    "FileSystemPermissionProfile",
    "PROTECTED_METADATA_NAMES",
]
