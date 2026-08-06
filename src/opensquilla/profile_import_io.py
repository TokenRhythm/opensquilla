"""Narrow filesystem and locking facade for profile-import transactions."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from opensquilla.recovery.atomic import (
    _chmod_open_file as chmod_open_file,
)
from opensquilla.recovery.atomic import (
    _native_io_path as native_io_path,
)
from opensquilla.recovery.atomic import (
    is_path_redirecting_stat,
    native_move_no_replace,
    reparse_tag_redirects,
)
from opensquilla.recovery.config_patch import (
    ConfigSnapshot,
)
from opensquilla.recovery.config_patch import (
    _copy_macos_config_metadata as copy_macos_config_metadata,
)
from opensquilla.recovery.config_patch import (
    _copy_windows_config_dacl as copy_windows_config_dacl,
)
from opensquilla.recovery.locking import ProfileOperationLock


class BoundProfileReadError(OSError):
    """A profile path could not be read through a stable no-follow authority."""


@dataclass(frozen=True)
class BoundProfileFile:
    """Bytes and metadata captured from one handle-bound regular file."""

    data: bytes
    mode: int
    mtime_ns: int
    _signature: tuple[int, ...] = field(repr=False)


class _MissingBoundPathError(FileNotFoundError):
    pass


@dataclass(frozen=True)
class _PosixDirectoryComponent:
    parent_fd: int
    name: str
    fd: int
    identity: tuple[int, int, int]


@dataclass
class _PosixDirectoryAuthority:
    anchor_fd: int
    anchor_identity: tuple[int, int, int]
    components: list[_PosixDirectoryComponent]

    @property
    def directory_fd(self) -> int:
        if self.components:
            return self.components[-1].fd
        return self.anchor_fd


def _absolute_lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _relative_below(root: Path, target: Path) -> tuple[Path, Path, Path]:
    root_path = _absolute_lexical(root)
    target_path = _absolute_lexical(target)
    try:
        relative = target_path.relative_to(root_path)
    except ValueError as exc:
        raise BoundProfileReadError(
            f"profile import read target is outside its fixed root: {target_path}"
        ) from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise BoundProfileReadError(
            f"profile import read target is not a safe child path: {target_path}"
        )
    return root_path, target_path, relative


def _directory_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (int(value.st_dev), int(value.st_ino), stat.S_IFMT(value.st_mode))


def _file_signature(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_mode),
        int(value.st_nlink),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _require_directory(value: os.stat_result, *, path: Path) -> None:
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise BoundProfileReadError(
            f"profile import read path is not a real directory: {path}"
        )


def _posix_directory_flags() -> int:
    no_follow = int(getattr(os, "O_NOFOLLOW", 0))
    directory = int(getattr(os, "O_DIRECTORY", 0))
    if not no_follow or not directory:
        raise BoundProfileReadError(
            "profile import no-follow directory handles are unavailable"
        )
    return (
        os.O_RDONLY
        | directory
        | no_follow
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_BINARY", 0))
    )


def _posix_file_flags() -> int:
    no_follow = int(getattr(os, "O_NOFOLLOW", 0))
    if not no_follow:
        raise BoundProfileReadError("profile import no-follow file handles are unavailable")
    return (
        os.O_RDONLY
        | no_follow
        | int(getattr(os, "O_CLOEXEC", 0))
        | int(getattr(os, "O_BINARY", 0))
    )


def _verify_posix_authority(authority: _PosixDirectoryAuthority) -> None:
    if _directory_identity(os.fstat(authority.anchor_fd)) != authority.anchor_identity:
        raise BoundProfileReadError(
            "profile import filesystem anchor changed while it was pinned"
        )
    for component in authority.components:
        try:
            current_entry = os.stat(
                component.name,
                dir_fd=component.parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise BoundProfileReadError(
                "profile import directory chain changed while it was pinned"
            ) from exc
        current_handle = os.fstat(component.fd)
        if (
            _directory_identity(current_entry) != component.identity
            or _directory_identity(current_handle) != component.identity
            or stat.S_ISLNK(current_entry.st_mode)
            or not stat.S_ISDIR(current_entry.st_mode)
        ):
            raise BoundProfileReadError(
                "profile import directory chain changed while it was pinned"
            )


@contextmanager
def _open_posix_directory_authority(
    root: Path,
    directory: Path,
) -> Iterator[_PosixDirectoryAuthority]:
    root_path, directory_path, _relative = _relative_below(root, directory)
    if root_path.anchor != directory_path.anchor or not directory_path.anchor:
        raise BoundProfileReadError("profile import read path has no stable filesystem anchor")
    try:
        root_before = os.lstat(root_path)
    except FileNotFoundError as exc:
        raise _MissingBoundPathError(str(root_path)) from exc
    except OSError as exc:
        raise BoundProfileReadError(
            f"cannot inspect profile import read root: {root_path}"
        ) from exc
    _require_directory(root_before, path=root_path)

    flags = _posix_directory_flags()
    anchor_path = Path(directory_path.anchor)
    opened: list[int] = []
    try:
        anchor_fd = os.open(anchor_path, flags)
        opened.append(anchor_fd)
        anchor_value = os.fstat(anchor_fd)
        _require_directory(anchor_value, path=anchor_path)
        authority = _PosixDirectoryAuthority(
            anchor_fd=anchor_fd,
            anchor_identity=_directory_identity(anchor_value),
            components=[],
        )
        parent_fd = anchor_fd
        root_depth = len(root_path.parts) - 1
        root_identity: tuple[int, int, int] | None = (
            authority.anchor_identity if root_depth == 0 else None
        )
        current = anchor_path
        for index, part in enumerate(directory_path.parts[1:], start=1):
            current /= part
            try:
                fd = os.open(part, flags, dir_fd=parent_fd)
            except FileNotFoundError as exc:
                raise _MissingBoundPathError(str(current)) from exc
            except OSError as exc:
                raise BoundProfileReadError(
                    f"cannot open profile import directory without following links: {current}"
                ) from exc
            opened.append(fd)
            value = os.fstat(fd)
            _require_directory(value, path=current)
            identity = _directory_identity(value)
            authority.components.append(
                _PosixDirectoryComponent(
                    parent_fd=parent_fd,
                    name=part,
                    fd=fd,
                    identity=identity,
                )
            )
            parent_fd = fd
            if index == root_depth:
                root_identity = identity
        if root_identity != _directory_identity(root_before):
            raise BoundProfileReadError(
                "profile import read root changed while it was being opened"
            )
        yield authority
        _verify_posix_authority(authority)
    finally:
        close_error: OSError | None = None
        for fd in reversed(opened):
            try:
                os.close(fd)
            except OSError as exc:
                close_error = close_error or exc
        if close_error is not None:
            raise BoundProfileReadError(
                "cannot close a profile import read handle"
            ) from close_error


def _read_posix_leaf(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
    skip_non_regular: bool,
) -> BoundProfileFile | None:
    try:
        path_before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BoundProfileReadError(
            f"cannot inspect profile import file without following links: {display_path}"
        ) from exc
    if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(path_before.st_mode):
        if skip_non_regular:
            return None
        raise BoundProfileReadError(
            f"profile import read target is not a regular file: {display_path}"
        )

    try:
        fd = os.open(name, _posix_file_flags(), dir_fd=directory_fd)
    except OSError as exc:
        raise BoundProfileReadError(
            f"cannot open profile import file without following links: {display_path}"
        ) from exc
    try:
        before = os.fstat(fd)
        expected = _file_signature(path_before)
        if not stat.S_ISREG(before.st_mode) or _file_signature(before) != expected:
            raise BoundProfileReadError(
                f"profile import file changed while it was being opened: {display_path}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        if _file_signature(after) != expected:
            raise BoundProfileReadError(
                f"profile import file changed while it was being read: {display_path}"
            )
        try:
            path_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise BoundProfileReadError(
                f"profile import file changed while it was being read: {display_path}"
            ) from exc
        if _file_signature(path_after) != expected:
            raise BoundProfileReadError(
                f"profile import file changed while it was being read: {display_path}"
            )
        return BoundProfileFile(
            data=b"".join(chunks),
            mode=stat.S_IMODE(after.st_mode),
            mtime_ns=int(after.st_mtime_ns),
            _signature=expected,
        )
    finally:
        os.close(fd)


def _posix_matching_names(directory_fd: int, suffix: str) -> tuple[str, ...]:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise BoundProfileReadError(
            "cannot enumerate the profile import read directory"
        ) from exc
    return tuple(
        sorted(
            name
            for name in names
            if isinstance(name, str)
            and name.endswith(suffix)
            and name not in {"", ".", ".."}
            and "/" not in name
            and "\x00" not in name
        )
    )


def _capture_posix_file(root: Path, target: Path) -> BoundProfileFile | None:
    root_path, target_path, relative = _relative_below(root, target)
    if not relative.parts:
        raise BoundProfileReadError("profile import read target must be below its root")
    try:
        with _open_posix_directory_authority(root_path, target_path.parent) as authority:
            return _read_posix_leaf(
                authority.directory_fd,
                target_path.name,
                display_path=target_path,
                skip_non_regular=False,
            )
    except _MissingBoundPathError:
        return None


def _capture_posix_directory(
    root: Path,
    directory: Path,
    *,
    suffix: str,
) -> tuple[tuple[str, BoundProfileFile], ...] | None:
    root_path, directory_path, _relative = _relative_below(root, directory)
    try:
        with _open_posix_directory_authority(root_path, directory_path) as authority:
            names_before = _posix_matching_names(authority.directory_fd, suffix)
            captured: list[tuple[str, BoundProfileFile]] = []
            for name in names_before:
                snapshot = _read_posix_leaf(
                    authority.directory_fd,
                    name,
                    display_path=directory_path / name,
                    skip_non_regular=True,
                )
                if snapshot is not None:
                    captured.append((name, snapshot))
            names_after = _posix_matching_names(authority.directory_fd, suffix)
            if names_after != names_before:
                raise BoundProfileReadError(
                    "profile import history changed while it was being enumerated"
                )
            for name, snapshot in captured:
                try:
                    current = os.stat(
                        name,
                        dir_fd=authority.directory_fd,
                        follow_symlinks=False,
                    )
                except OSError as exc:
                    raise BoundProfileReadError(
                        "profile import history changed while it was being read"
                    ) from exc
                if _file_signature(current) != snapshot._signature:
                    raise BoundProfileReadError(
                        "profile import history changed while it was being read"
                    )
            return tuple(captured)
    except _MissingBoundPathError:
        return None


def _capture_windows_leaves(
    root: Path,
    directory: Path,
    *,
    name: str | None = None,
    suffix: str | None = None,
) -> tuple[tuple[str, BoundProfileFile], ...] | None:
    # Keep the Windows implementation behind this top-level facade so the
    # lower-level memory package never imports migration/platform mechanics.
    from opensquilla.migration.source_snapshot_windows import (
        WindowsSourceSnapshotError,
        _new_api,
        _open_child,
        _open_directory_chain,
        _read_bounded_handle,
        _same_information,
        _validate_information,
    )

    _root_path, directory_path, _relative = _relative_below(root, directory)
    if (name is None) == (suffix is None):
        raise ValueError("exactly one Windows profile read selector is required")
    api = _new_api()
    try:
        with _open_directory_chain(api, directory_path) as (_normalized, chain):
            path, directory_handle, initial = chain[-1]
            names_before = api.enumerate_names(directory_handle, path=path)
            selected = (
                tuple(candidate for candidate in names_before if candidate == name)
                if name is not None
                else tuple(
                    candidate
                    for candidate in names_before
                    if candidate.endswith(suffix or "")
                )
            )
            captured: list[tuple[str, BoundProfileFile]] = []
            for candidate in selected:
                child_path = path / candidate
                with _open_child(
                    api,
                    child_path,
                    allow_file_writers=True,
                ) as (handle, information):
                    if information.identity.file_type != stat.S_IFREG:
                        if name is None:
                            continue
                        raise BoundProfileReadError(
                            f"profile import read target is not a regular file: {child_path}"
                        )
                    try:
                        path_before = os.stat(child_path, follow_symlinks=False)
                    except OSError as exc:
                        raise BoundProfileReadError(
                            f"cannot inspect pinned profile import file: {child_path}"
                        ) from exc
                    if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(
                        path_before.st_mode
                    ):
                        raise BoundProfileReadError(
                            f"profile import read target is not a regular file: {child_path}"
                        )
                    data, digest = _read_bounded_handle(
                        api,
                        handle,
                        source=child_path,
                        expected_size=information.size,
                        limit=(1 << 63) - 1,
                    )
                    try:
                        path_after = os.stat(child_path, follow_symlinks=False)
                    except OSError as exc:
                        raise BoundProfileReadError(
                            f"profile import file changed while it was pinned: {child_path}"
                        ) from exc
                    path_signature = (
                        int(path_before.st_dev),
                        int(path_before.st_ino),
                        int(path_before.st_mode),
                    )
                    if path_signature != (
                        int(path_after.st_dev),
                        int(path_after.st_ino),
                        int(path_after.st_mode),
                    ):
                        raise BoundProfileReadError(
                            f"profile import file changed while it was pinned: {child_path}"
                        )
                    captured.append(
                        (
                            candidate,
                            BoundProfileFile(
                                data=data,
                                mode=stat.S_IMODE(path_after.st_mode),
                                mtime_ns=information.mtime_ns,
                                _signature=(
                                    information.identity.device,
                                    information.identity.inode,
                                    information.identity.file_type,
                                    information.mode,
                                    information.size,
                                    information.mtime_ns,
                                    information.attributes,
                                    int(digest, 16),
                                ),
                            ),
                        )
                    )
            names_after = api.enumerate_names(directory_handle, path=path)
            selected_after = (
                tuple(candidate for candidate in names_after if candidate == name)
                if name is not None
                else tuple(
                    candidate
                    for candidate in names_after
                    if candidate.endswith(suffix or "")
                )
            )
            current = api.information(directory_handle, path=path)
            _validate_information(current, path=path)
            if selected_after != selected or not _same_information(current, initial):
                raise BoundProfileReadError(
                    "profile import read directory changed while it was pinned"
                )
            return tuple(captured)
    except WindowsSourceSnapshotError as exc:
        if exc.errno in {2, 3}:
            return None
        raise BoundProfileReadError(
            f"cannot capture profile import path through Windows handles: {directory_path}"
        ) from exc


def capture_bound_profile_file(root: Path, target: Path) -> BoundProfileFile | None:
    """Capture one regular file without following any component of its path."""

    if os.name != "nt":
        return _capture_posix_file(root, target)
    _root_path, target_path, relative = _relative_below(root, target)
    if not relative.parts:
        raise BoundProfileReadError("profile import read target must be below its root")
    captured = _capture_windows_leaves(root, target_path.parent, name=target_path.name)
    if not captured:
        return None
    return captured[0][1]


def capture_bound_profile_directory(
    root: Path,
    directory: Path,
    *,
    suffix: str,
) -> tuple[tuple[str, BoundProfileFile], ...] | None:
    """Capture matching direct children under one pinned directory authority."""

    if not suffix or "/" in suffix or "\\" in suffix or "\x00" in suffix:
        raise BoundProfileReadError("profile import read suffix is unsafe")
    if os.name != "nt":
        return _capture_posix_directory(root, directory, suffix=suffix)
    return _capture_windows_leaves(root, directory, suffix=suffix)


__all__ = [
    "BoundProfileFile",
    "BoundProfileReadError",
    "ConfigSnapshot",
    "ProfileOperationLock",
    "capture_bound_profile_directory",
    "capture_bound_profile_file",
    "chmod_open_file",
    "copy_macos_config_metadata",
    "copy_windows_config_dacl",
    "is_path_redirecting_stat",
    "native_io_path",
    "native_move_no_replace",
    "reparse_tag_redirects",
]
