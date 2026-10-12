"""Bounded, path-safe normalization for Community skill ZIP archives."""

from __future__ import annotations

import io
import re
import stat
import unicodedata
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath


class ArchiveNormalizationError(ValueError):
    """Raised when an archive cannot be normalized into one safe skill root."""


@dataclass(frozen=True)
class ArchiveLimits:
    """Hard limits applied before Community archive contents enter quarantine."""

    max_archive_bytes: int = 50 * 1024 * 1024
    max_entries: int = 2_048
    max_entry_bytes: int = 50 * 1024 * 1024
    max_expanded_bytes: int = 50 * 1024 * 1024
    max_depth: int = 32
    max_compression_ratio: float = 100.0


@dataclass(frozen=True)
class ArchiveNormalizationResult:
    """Normalized bytes plus portable file metadata retained from the ZIP."""

    files: dict[str, str | bytes]
    file_modes: dict[str, int]


DEFAULT_ARCHIVE_LIMITS = ArchiveLimits()
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_DEVICE_RE = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$",
    re.IGNORECASE,
)
_RESERVED_INTERNAL_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".opensquilla",
    ".opensquilla-rollback",
    ".opensquilla-staging",
    ".quarantine",
    ".staging",
    "__macosx",
}
_ASI_UNIX_EXTRA_ID = 0x756E
_MANIFEST_NAMES = frozenset({"skill.md", "skills.md"})


def _is_manifest(path: PurePosixPath) -> bool:
    return path.name.casefold() in _MANIFEST_NAMES


def normalize_relative_path(raw_path: str) -> PurePosixPath:
    """Return a canonical relative POSIX path or reject unsafe spellings."""

    if not isinstance(raw_path, str) or "\x00" in raw_path:
        raise ArchiveNormalizationError("archive entry has an invalid path")
    normalized = raw_path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.startswith("//")
        or _WINDOWS_DRIVE_RE.match(normalized)
    ):
        raise ArchiveNormalizationError(f"archive entry path is not relative: {raw_path!r}")
    segments = [unicodedata.normalize("NFC", segment) for segment in normalized.split("/")]
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ArchiveNormalizationError(f"archive entry path is unsafe: {raw_path!r}")
    if any(
        any(ord(character) < 32 or character in '<>:"|?*' for character in segment)
        or segment.endswith((" ", "."))
        or _WINDOWS_DEVICE_RE.fullmatch(segment)
        for segment in segments
    ):
        raise ArchiveNormalizationError(
            f"archive entry path is not portable to Windows: {raw_path!r}"
        )
    return PurePosixPath(*segments)


def _collision_key(path: PurePosixPath) -> tuple[str, ...]:
    return tuple(unicodedata.normalize("NFC", segment).casefold() for segment in path.parts)


def validate_portable_file_paths(
    raw_paths: Iterable[str | PurePosixPath],
) -> tuple[PurePosixPath, ...]:
    """Validate a set of file paths as one portable directory tree."""

    paths: list[PurePosixPath] = []
    prefix_spellings: dict[tuple[str, ...], tuple[str, ...]] = {}
    file_keys: set[tuple[str, ...]] = set()
    for raw_path in raw_paths:
        path = normalize_relative_path(raw_path) if isinstance(raw_path, str) else raw_path
        key = _collision_key(path)
        for depth in range(1, len(path.parts)):
            prefix = path.parts[:depth]
            prefix_key = key[:depth]
            previous = prefix_spellings.get(prefix_key)
            if previous is not None and previous != prefix:
                raise ArchiveNormalizationError(
                    "artifact directory paths collide across case or Unicode "
                    f"normalization: {'/'.join(previous)} and {'/'.join(prefix)}"
                )
            prefix_spellings[prefix_key] = prefix
        if key in file_keys:
            raise ArchiveNormalizationError(
                f"artifact file paths collide across case or Unicode normalization: {path}"
            )
        file_keys.add(key)
        paths.append(path)

    for path in paths:
        key = _collision_key(path)
        if any(key[:depth] in file_keys for depth in range(1, len(key))):
            raise ArchiveNormalizationError(f"artifact file/directory paths collide: {path}")
    return tuple(paths)


def _validate_archive_path(path: PurePosixPath, limits: ArchiveLimits) -> None:
    if len(path.parts) > limits.max_depth:
        raise ArchiveNormalizationError(f"archive entry exceeds path-depth limit: {path}")
    if any(segment.casefold() in _RESERVED_INTERNAL_DIRS for segment in path.parts):
        raise ArchiveNormalizationError(f"archive entry uses a reserved directory: {path}")


def _has_extra_field(info: zipfile.ZipInfo, expected_id: int) -> bool:
    offset = 0
    extra = info.extra
    while offset + 4 <= len(extra):
        field_id = int.from_bytes(extra[offset : offset + 2], "little")
        length = int.from_bytes(extra[offset + 2 : offset + 4], "little")
        offset += 4
        if offset + length > len(extra):
            raise ArchiveNormalizationError(
                f"archive entry has malformed metadata: {info.filename}"
            )
        if field_id == expected_id:
            return True
        offset += length
    if offset != len(extra):
        raise ArchiveNormalizationError(f"archive entry has malformed metadata: {info.filename}")
    return False


def _selected_skill_root(
    paths: set[PurePosixPath],
    selected_subpath: str,
) -> PurePosixPath:
    selected = selected_subpath.strip()
    if selected:
        selected_path = normalize_relative_path(selected)
        if _is_manifest(selected_path):
            selected_path = selected_path.parent
        selected_parts = () if str(selected_path) == "." else selected_path.parts
        roots: set[PurePosixPath] = set()
        for path in paths:
            if not _is_manifest(path):
                continue
            if selected_parts and path.parent.parts[-len(selected_parts) :] != selected_parts:
                continue
            prefix = (
                path.parent.parts[: -len(selected_parts)]
                if selected_parts
                else path.parent.parts
            )
            # GitHub and registry archives are accepted either flat or with one
            # packaging wrapper. Deeper implicit roots are intentionally not guessed.
            if len(prefix) <= 1:
                roots.add(PurePosixPath(*prefix, *selected_parts))
        if len(roots) != 1:
            raise ArchiveNormalizationError(
                "archive does not contain exactly one selected SKILL.md root"
            )
        return next(iter(roots))

    root_markers = {path for path in paths if len(path.parts) == 1 and _is_manifest(path)}
    if len(root_markers) == 1:
        return PurePosixPath()
    if len(root_markers) > 1:
        raise ArchiveNormalizationError("archive contains multiple root Skill manifests")

    wrapper_roots = {
        path.parent for path in paths if _is_manifest(path) and len(path.parts) == 2
    }
    if len(wrapper_roots) != 1:
        raise ArchiveNormalizationError(
            "archive must contain SKILL.md at its root or inside one wrapper directory"
        )
    return next(iter(wrapper_roots))


def _relative_to_root(path: PurePosixPath, root: PurePosixPath) -> PurePosixPath | None:
    if not root.parts:
        return path
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def _validated_mode(info: zipfile.ZipInfo) -> int:
    """Return POSIX permission bits, rejecting link and special-file metadata."""

    if _has_extra_field(info, _ASI_UNIX_EXTRA_ID):
        # ASi Unix metadata can encode link targets.  ZIP has no portable
        # hardlink contract, so fail closed instead of materializing a link.
        raise ArchiveNormalizationError(
            f"archive link metadata is unsupported: {info.filename}"
        )
    if info.create_system != 3:
        return 0
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if info.is_dir():
        if file_type not in {0, stat.S_IFDIR}:
            raise ArchiveNormalizationError(
                f"archive directory has unsupported type: {info.filename}"
            )
    elif file_type not in {0, stat.S_IFREG}:
        label = "symlink" if file_type == stat.S_IFLNK else "special file"
        raise ArchiveNormalizationError(f"archive {label} is unsupported: {info.filename}")
    return stat.S_IMODE(unix_mode) if unix_mode else 0


def _decode_entry(path: PurePosixPath, content: bytes) -> str | bytes:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        if _is_manifest(path):
            raise ArchiveNormalizationError("Skill manifest is not valid UTF-8") from None
        return content


def normalize_skill_archive(
    archive: bytes,
    *,
    selected_subpath: str = "",
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> dict[str, str | bytes]:
    """Extract one skill tree without writing or trusting archive paths.

    Both canonical layouts are supported: a flat archive whose root contains
    ``SKILL.md`` and an archive with one packaging wrapper. ``selected_subpath``
    additionally selects a known skill directory from a repository archive.
    """

    return normalize_skill_archive_result(
        archive,
        selected_subpath=selected_subpath,
        limits=limits,
    ).files


def normalize_skill_archive_result(
    archive: bytes,
    *,
    selected_subpath: str = "",
    limits: ArchiveLimits = DEFAULT_ARCHIVE_LIMITS,
) -> ArchiveNormalizationResult:
    """Normalize an archive and retain safe POSIX permission metadata."""

    if len(archive) > limits.max_archive_bytes:
        raise ArchiveNormalizationError("archive exceeds the compressed-size limit")

    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            infos = zf.infolist()
            if len(infos) > limits.max_entries:
                raise ArchiveNormalizationError("archive contains too many files")

            normalized_infos: list[tuple[PurePosixPath, zipfile.ZipInfo, int]] = []
            seen_paths: set[PurePosixPath] = set()
            seen_collision_keys: dict[tuple[str, ...], PurePosixPath] = {}
            declared_total = 0
            for info in infos:
                path = normalize_relative_path(info.filename)
                _validate_archive_path(path, limits)
                collision_key = _collision_key(path)
                previous = seen_collision_keys.get(collision_key)
                if previous is not None:
                    raise ArchiveNormalizationError(
                        f"archive paths collide across case or Unicode normalization: "
                        f"{previous} and {path}"
                    )
                if path in seen_paths:
                    raise ArchiveNormalizationError(f"archive contains duplicate path: {path}")
                seen_paths.add(path)
                seen_collision_keys[collision_key] = path
                if info.flag_bits & 0x1:
                    raise ArchiveNormalizationError(
                        f"encrypted archive entry is unsupported: {path}"
                    )
                mode = _validated_mode(info)
                if info.is_dir():
                    normalized_infos.append((path, info, mode))
                    continue
                if info.file_size > limits.max_entry_bytes:
                    raise ArchiveNormalizationError(f"archive entry exceeds size limit: {path}")
                if info.file_size and (
                    info.compress_size <= 0
                    or info.file_size / info.compress_size > limits.max_compression_ratio
                ):
                    raise ArchiveNormalizationError(
                        f"archive entry exceeds compression-ratio limit: {path}"
                    )
                declared_total += info.file_size
                if declared_total > limits.max_expanded_bytes:
                    raise ArchiveNormalizationError("archive exceeds the expanded-size limit")
                normalized_infos.append((path, info, mode))

            file_collision_keys = {
                _collision_key(path)
                for path, info, _mode in normalized_infos
                if not info.is_dir()
            }
            for path, _info, _mode in normalized_infos:
                collision_key = _collision_key(path)
                if any(
                    collision_key[:depth] in file_collision_keys
                    for depth in range(1, len(collision_key))
                ):
                    raise ArchiveNormalizationError(
                        f"archive file/directory paths collide: {path}"
                    )

            file_paths = {path for path, info, _mode in normalized_infos if not info.is_dir()}
            validate_portable_file_paths(file_paths)
            root = _selected_skill_root(file_paths, selected_subpath)
            root_markers = {
                path
                for path in file_paths
                if _is_manifest(path) and path.parent == root
            }
            skill_markers = {path for path in file_paths if _is_manifest(path)}
            if len(root_markers) != 1 or skill_markers != root_markers:
                raise ArchiveNormalizationError(
                    "archive contains multiple or misplaced Skill manifests"
                )

            for path, info, _mode in normalized_infos:
                relative = _relative_to_root(path, root)
                is_root_ancestor = info.is_dir() and (
                    path == root or (path.parts and root.parts[: len(path.parts)] == path.parts)
                )
                if relative is None and not is_root_ancestor:
                    raise ArchiveNormalizationError(
                        f"archive contains an entry outside the selected skill root: {path}"
                    )

            files: dict[str, str | bytes] = {}
            file_modes: dict[str, int] = {}
            actual_total = 0
            for path, info, mode in normalized_infos:
                if info.is_dir():
                    continue
                relative = _relative_to_root(path, root)
                if relative is None or not relative.parts:
                    continue
                with zf.open(info, "r") as handle:
                    content = handle.read(limits.max_entry_bytes + 1)
                if len(content) > limits.max_entry_bytes:
                    raise ArchiveNormalizationError(f"archive entry exceeds size limit: {path}")
                actual_total += len(content)
                if actual_total > limits.max_expanded_bytes:
                    raise ArchiveNormalizationError("archive exceeds the expanded-size limit")
                relative_name = relative.as_posix()
                if relative_name in files:
                    raise ArchiveNormalizationError(
                        f"archive contains duplicate normalized path: {relative_name}"
                    )
                files[relative_name] = _decode_entry(relative, content)
                if mode:
                    file_modes[relative_name] = mode
    except zipfile.BadZipFile as exc:
        raise ArchiveNormalizationError("download is not a valid ZIP archive") from exc
    except RuntimeError as exc:
        raise ArchiveNormalizationError(f"archive extraction failed: {exc}") from exc

    if sum(PurePosixPath(path).name.casefold() in _MANIFEST_NAMES for path in files) != 1:
        raise ArchiveNormalizationError("normalized archive root has no unique Skill manifest")
    return ArchiveNormalizationResult(files=files, file_modes=file_modes)
