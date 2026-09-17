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

    ``path`` is the canonical target used for host access.  ``logical_path``
    preserves the caller's normalized spelling when following a symlink would
    otherwise erase a path that the sandbox must protect.

    More-specific paths win.  Repeating the same lexical/canonical spelling
    replaces its earlier declaration; distinct aliases of one target remain
    separate and the most restrictive matching alias wins.
    """

    path: PurePath
    access: FileSystemAccess
    logical_path: PurePath | None = None

    @property
    def lexical_path(self) -> PurePath:
        """Return the normalized caller-visible spelling of this rule."""

        return self.logical_path or self.path


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

        workspace_entry = _permission_entry(workspace, FileSystemAccess.WRITE)
        workspace = workspace_entry.path
        declared_writable = [
            workspace_entry,
            *(_permission_entry(path, FileSystemAccess.WRITE) for path in writable_roots),
        ]
        context = platform_context or current_platform_context(
            cwd=workspace,  # type: ignore[arg-type]
            writable_roots=tuple(entry.path for entry in declared_writable),
        )
        declared_writable.extend(
            _permission_entry(
                path,
                FileSystemAccess.WRITE,
                canonical_path=_canonical_platform_path(path, context),
            )
            for path in resolve_temp_write_paths(
                context,
                include_slash_tmp=tmp_writable,
                include_tmpdir=tmpdir_env_writable,
            )
        )
        declared_writable = list(_deduplicate_permission_entries(declared_writable))

        windows_host_read_baseline = host_root_readonly and context.platform == "windows"
        entries: list[FileSystemPermissionEntry] = []
        if host_root_readonly and not windows_host_read_baseline:
            entries.extend(
                _permission_entry(
                    path,
                    FileSystemAccess.READ,
                    canonical_path=_canonical_platform_path(path, context),
                )
                for path in resolve_special_path(FileSystemSpecialPath.ROOT, context)
            )
        entries.extend(_permission_entry(path, FileSystemAccess.READ) for path in readable_roots)
        entries.extend(declared_writable)
        if protect_metadata:
            entries.extend(
                _permission_entry(root.lexical_path / name, FileSystemAccess.READ)
                for root in declared_writable
                for name in PROTECTED_METADATA_NAMES
            )
        entries.extend(_permission_entry(path, FileSystemAccess.DENY) for path in denied_read_roots)
        return cls(
            entries=tuple(entries),
            denied_read_globs=tuple(str(pattern) for pattern in denied_read_globs),
            default_access=(
                FileSystemAccess.READ if windows_host_read_baseline else FileSystemAccess.DENY
            ),
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
        windows_host_read_baseline = host_root_readonly and context.platform == "windows"
        entries: list[FileSystemPermissionEntry] = []
        if host_root_readonly and not windows_host_read_baseline:
            entries.extend(
                _permission_entry(
                    path,
                    FileSystemAccess.READ,
                    canonical_path=_canonical_platform_path(path, context),
                )
                for path in resolve_special_path(FileSystemSpecialPath.ROOT, context)
            )
        entries.extend(_permission_entry(path, FileSystemAccess.READ) for path in readable_roots)
        entries.extend(_permission_entry(path, FileSystemAccess.DENY) for path in denied_read_roots)
        return cls(
            tuple(entries),
            tuple(str(pattern) for pattern in denied_read_globs),
            (FileSystemAccess.READ if windows_host_read_baseline else FileSystemAccess.DENY),
        )

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
                    logical_path=entry.logical_path,
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
        """Return access after evaluating the lexical and canonical spellings."""

        candidates = _path_variants(path)
        if any(
            _matches_denied_read_glob(candidate, pattern)
            for candidate in candidates
            for pattern in self.denied_read_globs
        ):
            return FileSystemAccess.DENY

        winners = (self._winning_rule(candidate)[0] for candidate in candidates)
        return min(winners, key=_access_restrictiveness)

    def is_explicitly_denied(self, path: PurePath) -> bool:
        """Distinguish an explicit deny from a path with no matching grant."""

        candidates = _path_variants(path)
        if any(
            _matches_denied_read_glob(candidate, pattern)
            for candidate in candidates
            for pattern in self.denied_read_globs
        ):
            return True

        winners = tuple(self._winning_rule(candidate) for candidate in candidates)
        access = min((winner[0] for winner in winners), key=_access_restrictiveness)
        return access is FileSystemAccess.DENY and any(
            winner_access is FileSystemAccess.DENY and explicit
            for winner_access, explicit in winners
        )

    def _winning_rule(self, candidate: PurePath) -> tuple[FileSystemAccess, bool]:
        matches = [
            (len(root.parts), entry.access)
            for entry in self.effective_entries
            for root in _entry_path_variants(entry)
            if _is_relative_to(candidate, root)
        ]
        if not matches:
            return (self.default_access, False)
        specificity = max(match[0] for match in matches)
        return (
            min(
                (
                    access
                    for match_specificity, access in matches
                    if match_specificity == specificity
                ),
                key=_access_restrictiveness,
            ),
            True,
        )

    def protected_metadata_root(self, path: PurePath) -> PurePath | None:
        """Return the matching default metadata carveout, when one applies."""

        for candidate in _path_variants(path):
            matches = [
                (len(root.parts), index, root)
                for index, entry in enumerate(self.effective_entries)
                if _is_protected_metadata_entry(entry)
                for root in _entry_path_variants(entry)
                if _is_relative_to(candidate, root)
            ]
            if matches:
                return max(matches, key=lambda item: (item[0], item[1]))[2]
        return None

    def protected_path_variants(self, path: PurePath) -> tuple[PurePath, ...]:
        """Return both spellings of the protected non-write rule matching ``path``."""

        for candidate in _path_variants(path):
            matches: list[tuple[int, int, tuple[PurePath, ...]]] = []
            for index, entry in enumerate(self.effective_entries):
                if entry.access is FileSystemAccess.WRITE:
                    continue
                roots = _entry_path_variants(entry)
                matching_roots = tuple(root for root in roots if _is_relative_to(candidate, root))
                if matching_roots:
                    matches.append(
                        (
                            max(len(root.parts) for root in matching_roots),
                            index,
                            roots,
                        )
                    )
            if matches:
                return max(matches, key=lambda item: (item[0], item[1]))[2]
        return ()

    def writable_path_variants(self, path: PurePath) -> tuple[PurePath, ...]:
        """Return only write spellings that still resolve to frozen authority."""

        candidate_key = _canonical_key(logical_absolute_path(path))
        for entry in self.effective_entries:
            if entry.access is not FileSystemAccess.WRITE:
                continue
            if candidate_key not in {
                _canonical_key(entry.lexical_path),
                _canonical_key(entry.path),
            }:
                continue
            return _entry_path_variants(entry)
        return ()

    @property
    def retargeted_writable_roots(self) -> tuple[PurePath, ...]:
        """Return WRITE roots whose frozen canonical spelling now resolves elsewhere."""

        return tuple(
            entry.path
            for entry in self.effective_entries
            if entry.access is FileSystemAccess.WRITE and not _entry_path_variants(entry)
        )

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
        """Return final declarations without collapsing distinct path spellings."""

        identity = tuple[
            tuple[str, str],
            tuple[str, str],
            FileSystemAccess,
        ]
        spelling = tuple[tuple[str, str], tuple[str, str]]
        final_by_identity: dict[
            identity,
            tuple[int, FileSystemPermissionEntry],
        ] = {}
        identity_by_spelling: dict[spelling, identity] = {}
        for index, entry in enumerate(self.entries):
            normalized = _normalized_permission_entry(entry)
            lexical_key = _canonical_key(normalized.lexical_path)
            canonical_key = _canonical_key(normalized.path)
            spelling_key = (lexical_key, canonical_key)
            prior_identity = identity_by_spelling.get(spelling_key)
            if prior_identity is not None:
                final_by_identity.pop(prior_identity, None)
            entry_identity = (lexical_key, canonical_key, normalized.access)
            identity_by_spelling[spelling_key] = entry_identity
            final_by_identity[entry_identity] = (
                index,
                normalized,
            )
        return tuple(
            entry for _, entry in sorted(final_by_identity.values(), key=lambda item: item[0])
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
        roots = _path_variants(writable_root)
        subpaths: list[PurePath] = []
        seen: set[tuple[str, str]] = set()
        for entry in self.effective_entries:
            if entry.access is FileSystemAccess.WRITE:
                continue
            for variant in _entry_path_variants(entry):
                key = _canonical_key(variant)
                if key in seen or not any(
                    variant != root and _is_relative_to(variant, root) for root in roots
                ):
                    continue
                seen.add(key)
                subpaths.append(variant)
        return tuple(subpaths)

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


def logical_absolute_path(path: PurePath) -> PurePath:
    """Make a concrete path absolute without following any symlink."""

    if isinstance(path, Path):
        return Path(os.path.abspath(os.fspath(path.expanduser())))
    return path


def _lexical_absolute(path: PurePath) -> PurePath:
    return logical_absolute_path(path)


def _permission_entry(
    path: PurePath,
    access: FileSystemAccess,
    *,
    canonical_path: PurePath | None = None,
) -> FileSystemPermissionEntry:
    logical = logical_absolute_path(path)
    canonical = _canonical(logical if canonical_path is None else canonical_path)
    return FileSystemPermissionEntry(
        path=canonical,
        access=access,
        logical_path=(logical if _canonical_key(logical) != _canonical_key(canonical) else None),
    )


def _normalized_permission_entry(
    entry: FileSystemPermissionEntry,
) -> FileSystemPermissionEntry:
    logical = logical_absolute_path(entry.lexical_path)
    # ``entry.path`` is the canonical identity captured when the profile was
    # created.  Keep that spelling frozen: resolving it again here would let a
    # later symlink replacement silently retarget the stored authority.
    canonical = logical_absolute_path(entry.path)
    return FileSystemPermissionEntry(
        path=canonical,
        access=entry.access,
        logical_path=(
            logical
            if entry.logical_path is not None
            or _canonical_key(logical) != _canonical_key(canonical)
            else None
        ),
    )


def _path_variants(path: PurePath) -> tuple[PurePath, ...]:
    logical = logical_absolute_path(path)
    return _deduplicate_paths((logical, _canonical(logical)))


def _entry_path_variants(
    entry: FileSystemPermissionEntry,
) -> tuple[PurePath, ...]:
    normalized = _normalized_permission_entry(entry)
    if normalized.access is FileSystemAccess.WRITE:
        return _write_entry_path_variants(normalized)
    return _deduplicate_paths(
        (
            normalized.lexical_path,
            normalized.path,
            _canonical(normalized.lexical_path),
            _canonical(normalized.path),
        )
    )


def _write_entry_path_variants(
    entry: FileSystemPermissionEntry,
) -> tuple[PurePath, ...]:
    """Return stable spellings without expanding write authority on retarget."""

    frozen = entry.path
    logical = entry.lexical_path
    if not isinstance(frozen, (Path, PurePosixPath, PureWindowsPath)):
        return _deduplicate_paths((logical, frozen))
    try:
        current_frozen = _current_platform_canonical(frozen)
        current_logical = _current_platform_canonical(logical)
    except (OSError, RuntimeError, ValueError):
        # Backend path validation owns malformed-path diagnostics.
        return _deduplicate_paths((logical, frozen))
    if _canonical_key(current_frozen) != _canonical_key(frozen):
        return ()
    variants: list[PurePath] = [frozen]
    if _canonical_key(current_logical) == _canonical_key(frozen):
        variants.insert(0, logical)
    return _deduplicate_paths(variants)


def _current_platform_canonical(path: PurePath) -> PurePath:
    if isinstance(path, Path):
        return _canonical(path)
    if os.name == "nt" and isinstance(path, PureWindowsPath):
        return _canonical(Path(str(path)))
    if os.name != "nt" and isinstance(path, PurePosixPath):
        return _canonical(Path(str(path)))
    return path


def _is_relative_to(path: PurePath, root: PurePath) -> bool:
    try:
        return path.is_relative_to(root)
    except (TypeError, ValueError):
        return False


def _is_protected_metadata_entry(entry: FileSystemPermissionEntry) -> bool:
    variants = _entry_path_variants(entry)
    return entry.access is FileSystemAccess.READ and any(
        variant.name in PROTECTED_METADATA_NAMES for variant in variants
    )


def _access_restrictiveness(access: FileSystemAccess) -> int:
    return {
        FileSystemAccess.DENY: 0,
        FileSystemAccess.READ: 1,
        FileSystemAccess.WRITE: 2,
    }[access]


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


def _deduplicate_permission_entries(
    entries: Iterable[FileSystemPermissionEntry],
) -> tuple[FileSystemPermissionEntry, ...]:
    unique: list[FileSystemPermissionEntry] = []
    seen: set[tuple[tuple[str, str], tuple[str, str], FileSystemAccess]] = set()
    for entry in entries:
        normalized = _normalized_permission_entry(entry)
        key = (
            _canonical_key(normalized.lexical_path),
            _canonical_key(normalized.path),
            normalized.access,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(normalized)
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
    "logical_absolute_path",
]
