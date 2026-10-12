"""Stable, link-safe fingerprints for Skill directory trees."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from opensquilla.skills.file_hash import (
    _read_stable_symlink,
    _stream_file_into_digest,
)


def _tree_entries(directory: Path) -> list[Path]:
    """Return all descendants in a platform-independent path order."""

    return sorted(
        directory.rglob("*"),
        key=lambda item: item.relative_to(directory).as_posix(),
    )


def compute_tree_state(directory: Path) -> str:
    """Fingerprint tree metadata cheaply enough for catalog change probes.

    Content is deliberately not read here. A state change causes the loader to
    rebuild the candidate and calculate :func:`compute_tree_sha256`; consumers
    that cross a pinned-turn boundary verify that full digest before returning
    bytes. Nanosecond mtime and size preserve the loader's historical manifest
    behavior while extending it to every non-directory entry in the Skill.
    """

    hasher = hashlib.sha256()
    for path in _tree_entries(directory):
        relative = path.relative_to(directory).as_posix()
        info = path.lstat()
        mode = info.st_mode
        if stat.S_ISDIR(mode):
            continue
        hasher.update(relative.encode("utf-8", errors="surrogateescape"))
        hasher.update(b"\0")
        hasher.update(str(stat.S_IFMT(mode)).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(str(info.st_size).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(str(info.st_mtime_ns).encode("ascii"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def compute_tree_sha256(directory: Path) -> str:
    """Hash the complete Skill tree without following filesystem links.

    Paths use POSIX separators so the same logical tree hashes identically on
    every supported OS. Hidden files and file types are included; POSIX mode
    bits are excluded because they are not a readiness requirement on Windows.
    """

    hasher = hashlib.sha256()
    for path in _tree_entries(directory):
        relative = path.relative_to(directory).as_posix()
        info = path.lstat()
        mode = info.st_mode
        if stat.S_ISDIR(mode):
            continue
        hasher.update(relative.encode("utf-8", errors="surrogateescape"))
        hasher.update(b"\0")
        if stat.S_ISREG(mode):
            hasher.update(b"file\0")
            hasher.update(info.st_size.to_bytes(8, "big"))
            _stream_file_into_digest(
                path,
                hasher,
                follow_symlinks=False,
                expected_stat=info,
            )
        elif stat.S_ISLNK(mode):
            target = _read_stable_symlink(path, expected_stat=info).encode(
                "utf-8", errors="surrogateescape"
            )
            hasher.update(b"symlink\0")
            hasher.update(len(target).to_bytes(8, "big"))
            hasher.update(target)
        else:
            hasher.update(f"special:{stat.S_IFMT(mode):o}".encode("ascii"))
            hasher.update(b"\0")
    return hasher.hexdigest()


__all__ = ["compute_tree_sha256", "compute_tree_state"]
