"""Stable, bounded-memory file reads for Skill tree fingerprints."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Never, Protocol

_HASH_CHUNK_SIZE = 1024 * 1024
_IS_WINDOWS = os.name == "nt"
_PATH_CHANGED_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
_PATH_CHANGED_WINERRORS = frozenset(
    {
        2,  # ERROR_FILE_NOT_FOUND
        3,  # ERROR_PATH_NOT_FOUND
        1921,  # ERROR_CANT_RESOLVE_FILENAME
    }
)
_READLINK_CHANGED_ERRNOS = _PATH_CHANGED_ERRNOS | {errno.EINVAL}
_READLINK_CHANGED_WINERRORS = _PATH_CHANGED_WINERRORS | {
    4390,  # ERROR_NOT_A_REPARSE_POINT
}


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object: ...


class _TreeChangedDuringHashError(OSError):
    """Raised when a filesystem entry changes while its digest is calculated."""


def _entry_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return the metadata that must remain stable across one file read."""

    return (
        stat.S_IFMT(info.st_mode),
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _path_descriptor_identity_matches(
    path_info: os.stat_result,
    descriptor_info: os.stat_result,
) -> bool:
    """Compare path and descriptor metadata using fields reliable on each OS.

    On Windows, CPython maps path-based ``st_ctime_ns`` to creation time while
    descriptor-based ``fstat()`` retains change time.  Device and inode remain
    the file identity and must match along with type, size, and modification
    time.  Same-source path/path and descriptor/descriptor comparisons continue
    to use :func:`_entry_identity`, including change time.
    """

    if not _IS_WINDOWS:
        return _entry_identity(path_info) == _entry_identity(descriptor_info)
    return (
        stat.S_IFMT(path_info.st_mode),
        path_info.st_dev,
        path_info.st_ino,
        path_info.st_size,
        path_info.st_mtime_ns,
    ) == (
        stat.S_IFMT(descriptor_info.st_mode),
        descriptor_info.st_dev,
        descriptor_info.st_ino,
        descriptor_info.st_size,
        descriptor_info.st_mtime_ns,
    )


def _changed(path: Path, detail: str, cause: OSError | None = None) -> Never:
    error = _TreeChangedDuringHashError(f"Skill tree entry changed while hashing {path}: {detail}")
    if cause is None:
        raise error
    raise error from cause


def _raise_if_path_operation_proves_change(
    path: Path,
    detail: str,
    error: OSError,
    *,
    readlink: bool = False,
) -> None:
    """Classify path-operation errors that prove a captured entry changed.

    These error codes are authoritative even if a follow-up identity probe
    observes the original entry again after an ABA replacement.  Permission
    and device errors deliberately remain their original ``OSError``.
    """

    errnos = _READLINK_CHANGED_ERRNOS if readlink else _PATH_CHANGED_ERRNOS
    winerrors = (
        _READLINK_CHANGED_WINERRORS if readlink else _PATH_CHANGED_WINERRORS
    )
    if error.errno in errnos or getattr(error, "winerror", None) in winerrors:
        _changed(path, detail, error)


def _path_stat(path: Path, *, follow_symlinks: bool) -> os.stat_result:
    try:
        return path.stat() if follow_symlinks else path.lstat()
    except OSError as exc:
        _raise_if_path_operation_proves_change(
            path,
            "entry is no longer available",
            exc,
        )
        raise


def _path_matches_snapshot(
    path: Path,
    snapshot: os.stat_result,
    *,
    follow_symlinks: bool,
) -> bool:
    """Return whether a path still resolves to the captured entry metadata."""

    current = _path_stat(path, follow_symlinks=follow_symlinks)
    return _entry_identity(current) == _entry_identity(snapshot)


def _raise_if_file_changed(
    path: Path,
    *,
    path_entry_before: os.stat_result,
    path_before: os.stat_result,
    follow_symlinks: bool,
    descriptor: int | None = None,
    descriptor_before: os.stat_result | None = None,
) -> None:
    """Raise only when an operation failure coincides with an observed mutation.

    Permission and device I/O failures are not evidence that the tree changed.
    Keeping those as their original ``OSError`` lets the loader retain its
    historical per-Skill partial-failure behavior.  A missing/replaced path or
    changed descriptor identity is an integrity race and invalidates the whole
    catalog candidate.
    """

    if descriptor is not None:
        try:
            current_open = os.fstat(descriptor)
        except OSError:
            current_open = None
        if current_open is not None:
            if descriptor_before is not None:
                descriptor_matches = _entry_identity(current_open) == _entry_identity(
                    descriptor_before
                )
            else:
                descriptor_matches = _path_descriptor_identity_matches(
                    path_before,
                    current_open,
                )
            if not descriptor_matches:
                _changed(path, "opened entry metadata changed during hashing")

    try:
        if not _path_matches_snapshot(
            path,
            path_entry_before,
            follow_symlinks=False,
        ):
            _changed(path, "pathname entry changed during hashing")
        if follow_symlinks and not _path_matches_snapshot(
            path,
            path_before,
            follow_symlinks=True,
        ):
            _changed(path, "symbolic-link target changed during hashing")
    except _TreeChangedDuringHashError:
        raise
    except OSError:
        # The verification probe may itself encounter a stable permission or
        # device error.  It cannot turn the original operation failure into a
        # tree-race diagnosis without positive identity evidence.
        return


def _read_chunk(descriptor: int, size: int) -> bytes:
    """Read one bounded chunk; kept separate for deterministic race injection tests."""

    return os.read(descriptor, size)


def _stream_file_into_digest(
    path: Path,
    digest: _Digest,
    *,
    follow_symlinks: bool,
    expected_stat: os.stat_result | None = None,
    expected_path_stat: os.stat_result | None = None,
) -> None:
    """Hash one stable regular file without materializing it in memory.

    ``follow_symlinks`` is false for the complete v2 tree digest and true for
    the historical digest, whose file predicate and reads followed links. The
    path, opened descriptor, and metadata are compared before and after the
    bounded read so a concurrent replacement or mutation fails closed.
    """

    path_entry_before = (
        expected_path_stat
        if expected_path_stat is not None
        else (
            _path_stat(path, follow_symlinks=False)
            if follow_symlinks or expected_stat is None
            else expected_stat
        )
    )
    before = (
        expected_stat
        if expected_stat is not None
        else (
            _path_stat(path, follow_symlinks=True)
            if follow_symlinks
            else path_entry_before
        )
    )
    if not stat.S_ISREG(before.st_mode):
        _changed(path, "entry is no longer a regular file")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_BINARY", 0)
    if not follow_symlinks:
        flags |= getattr(os, "O_NOFOLLOW", 0)

    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _raise_if_path_operation_proves_change(
            path,
            "entry could not be opened consistently",
            exc,
        )
        _raise_if_file_changed(
            path,
            path_entry_before=path_entry_before,
            path_before=before,
            follow_symlinks=follow_symlinks,
        )
        raise

    try:
        try:
            opened = os.fstat(descriptor)
        except OSError:
            _raise_if_file_changed(
                path,
                path_entry_before=path_entry_before,
                path_before=before,
                follow_symlinks=follow_symlinks,
                descriptor=descriptor,
            )
            raise
        if not stat.S_ISREG(opened.st_mode):
            _changed(path, "opened entry is no longer a regular file")
        if not _path_descriptor_identity_matches(before, opened):
            _changed(path, "opened entry does not match the path metadata")

        remaining = opened.st_size
        while remaining:
            try:
                chunk = _read_chunk(descriptor, min(_HASH_CHUNK_SIZE, remaining))
            except OSError:
                _raise_if_file_changed(
                    path,
                    path_entry_before=path_entry_before,
                    path_before=before,
                    follow_symlinks=follow_symlinks,
                    descriptor=descriptor,
                    descriptor_before=opened,
                )
                raise
            if not chunk:
                _changed(path, "entry was truncated during hashing")
            digest.update(chunk)
            remaining -= len(chunk)

        try:
            extra = _read_chunk(descriptor, 1)
        except OSError:
            _raise_if_file_changed(
                path,
                path_entry_before=path_entry_before,
                path_before=before,
                follow_symlinks=follow_symlinks,
                descriptor=descriptor,
                descriptor_before=opened,
            )
            raise
        if extra:
            _changed(path, "entry grew during hashing")

        try:
            after_open = os.fstat(descriptor)
        except OSError:
            _raise_if_file_changed(
                path,
                path_entry_before=path_entry_before,
                path_before=before,
                follow_symlinks=follow_symlinks,
                descriptor=descriptor,
                descriptor_before=opened,
            )
            raise
        if _entry_identity(after_open) != _entry_identity(opened):
            _changed(path, "opened entry metadata changed during hashing")
        if follow_symlinks:
            if not _path_matches_snapshot(
                path,
                path_entry_before,
                follow_symlinks=False,
            ):
                _changed(path, "pathname entry changed during hashing")
            if not _path_matches_snapshot(
                path,
                before,
                follow_symlinks=True,
            ):
                _changed(path, "symbolic-link target changed during hashing")
            followed_after = _path_stat(path, follow_symlinks=True)
            if not _path_descriptor_identity_matches(followed_after, after_open):
                _changed(path, "opened entry no longer matches the symbolic-link target")
        else:
            if not _path_matches_snapshot(
                path,
                path_entry_before,
                follow_symlinks=False,
            ):
                _changed(path, "path was replaced during hashing")
            path_after = _path_stat(path, follow_symlinks=False)
            if not _path_descriptor_identity_matches(path_after, after_open):
                _changed(path, "opened entry no longer matches the path metadata")
    finally:
        os.close(descriptor)


def _read_stable_symlink(
    path: Path,
    *,
    expected_stat: os.stat_result | None = None,
) -> str:
    """Read one link target while verifying that the link remains unchanged."""

    before = _path_stat(path, follow_symlinks=False)
    if expected_stat is not None and _entry_identity(before) != _entry_identity(expected_stat):
        _changed(path, "symbolic-link metadata changed before it could be read")
    if not stat.S_ISLNK(before.st_mode):
        _changed(path, "entry is no longer a symbolic link")
    try:
        target = os.readlink(path)
    except OSError as exc:
        _raise_if_path_operation_proves_change(
            path,
            "symbolic link could not be read consistently",
            exc,
            readlink=True,
        )
        _raise_if_file_changed(
            path,
            path_entry_before=before,
            path_before=before,
            follow_symlinks=False,
        )
        raise
    after = _path_stat(path, follow_symlinks=False)
    if _entry_identity(after) != _entry_identity(before):
        _changed(path, "symbolic link changed while its target was read")
    return target


__all__: list[str] = []
