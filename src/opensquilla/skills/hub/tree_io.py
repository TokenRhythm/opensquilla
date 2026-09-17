"""Portable staging I/O and bounded Skill tree accounting."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from opensquilla.skills.io_worker import check_staging_cancelled

if TYPE_CHECKING:
    from opensquilla.skills.hub.source import SkillBundle

MAX_TREE_ENTRIES = 4_096
CHUNK_SIZE = 64 * 1024


def exceeds_limit(size: int, limit: int | None) -> bool:
    return limit is not None and size > limit


def validate_entry_count(count: int, limit: int = MAX_TREE_ENTRIES) -> None:
    if count > limit:
        raise ValueError(f"Skill tree contains more than {limit} entries")


def validate_tree_entry_count(
    paths: Iterable[str | PurePosixPath],
    *,
    limit: int = MAX_TREE_ENTRIES,
) -> None:
    entries: set[PurePosixPath] = set()
    for raw in paths:
        path = PurePosixPath(raw)
        while path.parts:
            entries.add(path)
            validate_entry_count(len(entries), limit)
            path = path.parent


def artifact_tree_digest(directory: Path, *, include_lengths: bool = False) -> str:
    """Hash original artifact bytes using the source's historical encoding."""
    digest = hashlib.sha256()
    for path in sorted(
        directory.rglob("*"), key=lambda item: item.relative_to(directory).as_posix()
    ):
        if path.is_symlink():
            raise ValueError("Skill artifact contains a symbolic link")
        if not path.is_file():
            continue
        digest.update(path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        if include_lengths:
            digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
                check_staging_cancelled()
                digest.update(chunk)
    return digest.hexdigest()


def write_legacy_bundle(bundle: SkillBundle, destination: Path) -> None:
    from opensquilla.skills.hub.archive import (
        DEFAULT_ARCHIVE_LIMITS,
        _validate_archive_path,
        validate_portable_file_paths,
    )

    files = bundle.files
    paths = validate_portable_file_paths(files)
    validate_tree_entry_count(paths)
    for path in paths:
        _validate_archive_path(path, DEFAULT_ARCHIVE_LIMITS)
    destination.mkdir(parents=True, exist_ok=False)
    for name, path in zip(files, paths, strict=True):
        target = destination.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = files[name]
        content = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        with target.open("xb") as output:
            for offset in range(0, len(content), CHUNK_SIZE):
                check_staging_cancelled()
                output.write(content[offset : offset + CHUNK_SIZE])
        mode = bundle.file_modes.get(name)
        if mode and os.name != "nt":
            target.chmod(mode & 0o777)


def validate_portable_tree(files: Iterable[str], directories: Iterable[str]) -> None:
    from opensquilla.skills.hub.archive import (
        ArchiveNormalizationError,
        normalize_relative_path,
        validate_portable_file_paths,
    )

    paths = validate_portable_file_paths(files)
    file_keys = {tuple(part.casefold() for part in path.parts) for path in paths}
    spellings: dict[tuple[str, ...], tuple[str, ...]] = {}
    for path in (*paths, *(normalize_relative_path(value) for value in directories)):
        key = tuple(part.casefold() for part in path.parts)
        for depth in range(1, len(path.parts) + 1):
            prefix = key[:depth]
            spelling = path.parts[:depth]
            previous = spellings.get(prefix)
            if previous is not None and previous != spelling:
                raise ArchiveNormalizationError("Skill directory paths collide")
            spellings[prefix] = spelling
            if depth < len(path.parts) and prefix in file_keys:
                raise ArchiveNormalizationError("Skill file/directory paths collide")
    for value in directories:
        if tuple(part.casefold() for part in normalize_relative_path(value).parts) in file_keys:
            raise ArchiveNormalizationError("Skill file/directory paths collide")
