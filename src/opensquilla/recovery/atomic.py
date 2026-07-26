"""Fail-closed filesystem primitives used by recovery transactions."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import ntpath
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from opensquilla.recovery.errors import (
    AtomicStateUnknownError,
    CrossDeviceMoveError,
    DestinationExistsError,
    NoReplaceUnavailableError,
    RecoveryError,
    UnsafePathError,
)

_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_WINDOWS_ERROR_ALREADY_EXISTS = 183
_WINDOWS_ERROR_FILE_EXISTS = 80
_WINDOWS_ERROR_NOT_SAME_DEVICE = 17
_WINDOWS_ERROR_INVALID_FUNCTION = 1
_WINDOWS_ERROR_NOT_SUPPORTED = 50
_WINDOWS_ERROR_INVALID_PARAMETER = 87
_WINDOWS_ERROR_CALL_NOT_IMPLEMENTED = 120
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_ALLOWED_LINK_REPARSE_TAGS = frozenset(
    {_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK}
)
_WINDOWS_DELETE_ACCESS = 0x00010000
_WINDOWS_FILE_TRAVERSE = 0x00000020
_WINDOWS_FILE_READ_ATTRIBUTES = 0x00000080
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_FILE_SHARE_DELETE = 0x00000004
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_ATTRIBUTE_TAG_INFO = 9
_WINDOWS_FILE_ID_INFO = 18
_WINDOWS_FILE_RENAME_INFORMATION = 10

_PROFILE_LINK_LEAF_DIRECTORIES = frozenset({"workspace", "state/workspace"})
_PROFILE_OPAQUE_DIRECTORIES = frozenset({"code-task"})
_WINDOWS_PROFILE_OPAQUE_DIRECTORIES = frozenset({"sandbox"})


def _chmod_open_file(fd: int, mode: int) -> None:
    """Apply a POSIX mode when the host exposes descriptor chmod.

    Windows CPython does not expose ``os.fchmod``. Recovery files still request
    restrictive creation modes where the host supports them, while
    Windows-specific config paths preserve their DACL separately. Descriptor
    mode hardening is an additional POSIX capability, not a portable
    prerequisite.
    """

    fchmod = getattr(os, "fchmod", None)
    if callable(fchmod):
        fchmod(fd, mode)


class _WindowsFileAttributeTagInfo(ctypes.Structure):
    _fields_ = [
        ("file_attributes", ctypes.c_uint32),
        ("reparse_tag", ctypes.c_uint32),
    ]


class _WindowsFileId128(ctypes.Structure):
    _fields_ = [("identifier", ctypes.c_ubyte * 16)]


class _WindowsFileIdInfo(ctypes.Structure):
    _fields_ = [
        ("volume_serial_number", ctypes.c_uint64),
        ("file_id", _WindowsFileId128),
    ]


class _WindowsIoStatusBlock(ctypes.Structure):
    _fields_ = [
        ("status_or_pointer", ctypes.c_void_p),
        ("information", ctypes.c_size_t),
    ]


@dataclass(frozen=True)
class PathIdentity:
    """Non-content filesystem identity used for CAS and transaction receipts."""

    device: int
    inode: int
    mode: int
    size: int
    modified_at_ns: int
    reparse_tag: int | None = None
    link_target: str | None = None

    @classmethod
    def from_stat(
        cls,
        value: os.stat_result,
        *,
        reparse_tag: int | None = None,
        link_target: str | None = None,
    ) -> PathIdentity:
        observed_tag = int(getattr(value, "st_reparse_tag", 0)) or None
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            mode=int(value.st_mode),
            size=int(value.st_size),
            modified_at_ns=int(value.st_mtime_ns),
            reparse_tag=reparse_tag if reparse_tag is not None else observed_tag,
            link_target=link_target,
        )

    @property
    def token(self) -> str:
        return f"{self.device}:{self.inode}"

    def metadata_tuple(self) -> tuple[int, int, int, int, int, int | None, str | None]:
        return (
            self.device,
            self.inode,
            self.mode,
            self.size,
            self.modified_at_ns,
            self.reparse_tag,
            self.link_target,
        )


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_link_or_reparse(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or _is_reparse_point(value)


def path_identity(path: str | Path, *, follow_symlinks: bool = False) -> PathIdentity:
    candidate = Path(path)
    value = candidate.stat() if follow_symlinks else candidate.lstat()
    if follow_symlinks or not _is_link_or_reparse(value):
        return PathIdentity.from_stat(value)
    return _link_leaf_identity(candidate, value)


def _link_leaf_identity(path: Path, value: os.stat_result) -> PathIdentity:
    """Return no-follow identity for an approved link/reparse manifest leaf."""

    tag = int(getattr(value, "st_reparse_tag", 0)) or None
    if tag is not None and tag not in _ALLOWED_LINK_REPARSE_TAGS:
        raise UnsafePathError(f"unsupported reparse point in recovery source: {path}")
    try:
        target = os.readlink(path)
    except OSError as exc:
        raise UnsafePathError(f"cannot inspect recovery link target: {path}") from exc
    return PathIdentity.from_stat(value, reparse_tag=tag, link_target=target)


def _assert_plain_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise UnsafePathError(f"{label} is not accessible: {path}") from exc
    if _is_link_or_reparse(value) or not stat.S_ISDIR(value.st_mode):
        raise UnsafePathError(f"{label} must be a real directory: {path}")
    return value


def _assert_bound_directory(
    fd: int,
    expected: PathIdentity,
    *,
    label: str,
) -> None:
    """Verify a no-follow directory handle still names the preflight object."""

    try:
        value = os.fstat(fd)
    except OSError as exc:
        raise UnsafePathError(f"cannot verify opened {label}") from exc
    if not stat.S_ISDIR(value.st_mode):
        raise UnsafePathError(f"opened {label} is not a directory")
    if (int(value.st_dev), int(value.st_ino)) != (expected.device, expected.inode):
        raise UnsafePathError(f"{label} identity changed before native move")


def no_follow_manifest(
    root: str | Path,
    *,
    opaque_directories: frozenset[str] = frozenset(),
    link_leaf_directories: frozenset[str] = frozenset(),
) -> dict[str, PathIdentity]:
    """Enumerate a regular file/directory tree without following links.

    The manifest intentionally contains metadata only. Recovery diagnostics and
    receipts must never persist hashes of user-authored Markdown or transcripts.
    Links are rejected by default. When a real directory is explicitly named in
    ``link_leaf_directories``, links below (but never in place of) that directory
    are recorded by their no-follow identity and are not traversed.
    """

    root_path = Path(root)
    root_stat = root_path.lstat()
    if _is_link_or_reparse(root_stat):
        raise UnsafePathError(f"automatic operations refuse links or reparse points: {root_path}")
    if not (stat.S_ISDIR(root_stat.st_mode) or stat.S_ISREG(root_stat.st_mode)):
        raise UnsafePathError(f"automatic operations refuse special files: {root_path}")

    result = {".": PathIdentity.from_stat(root_stat)}
    if stat.S_ISREG(root_stat.st_mode):
        return result

    def visit(directory: Path, relative: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise UnsafePathError(f"cannot enumerate recovery source: {directory}") from exc
        for entry in entries:
            child_relative = relative / entry.name
            child_key = child_relative.as_posix()
            child_path = Path(entry.path)
            try:
                value = child_path.lstat()
            except OSError as exc:
                raise UnsafePathError(f"cannot inspect recovery source: {entry.path}") from exc
            if _is_link_or_reparse(value):
                if not any(
                    child_relative != Path(link_root)
                    and child_relative.is_relative_to(Path(link_root))
                    for link_root in link_leaf_directories
                ):
                    raise UnsafePathError(
                        f"automatic operations refuse links or reparse points: {entry.path}"
                    )
                result[child_key] = _link_leaf_identity(child_path, value)
                continue
            if not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)):
                raise UnsafePathError(f"automatic operations refuse special files: {entry.path}")
            result[child_key] = PathIdentity.from_stat(value)
            if (
                stat.S_ISDIR(value.st_mode)
                and child_key not in opaque_directories
            ):
                visit(Path(entry.path), child_relative)

    visit(root_path, Path())
    return result


def _profile_opaque_manifest_directories() -> frozenset[str]:
    opaque = _PROFILE_OPAQUE_DIRECTORIES
    if os.name == "nt" or sys.platform == "win32":
        opaque |= _WINDOWS_PROFILE_OPAQUE_DIRECTORIES
    return opaque


def profile_no_follow_manifest(root: str | Path) -> dict[str, PathIdentity]:
    """Manifest one whole profile without dereferencing profile-owned links.

    ``workspace`` (including the historical ``state/workspace`` layout) is
    persistent user data and may contain Git/npm links, which are verified as
    no-follow leaves across a native rename. ``code-task`` is machine-local
    execution state and is kept opaque while a complete existing profile is
    parked or restored. Their policy roots must still be real directories.
    Windows runtime ``sandbox`` data retains its established opaque-directory
    contract.
    """

    return no_follow_manifest(
        root,
        opaque_directories=_profile_opaque_manifest_directories(),
        link_leaf_directories=_PROFILE_LINK_LEAF_DIRECTORIES,
    )


def _manifest_matches_after_move(
    before: dict[str, PathIdentity],
    after: dict[str, PathIdentity],
    *,
    allowed_mtime_changes: frozenset[str],
    allow_directory_mtime_changes: bool = False,
) -> bool:
    """Compare a move manifest with explicit, metadata-only exceptions."""

    if before.keys() != after.keys():
        return False
    for relative, expected in before.items():
        current = after[relative]
        if current == expected:
            continue
        if relative not in allowed_mtime_changes and not (
            allow_directory_mtime_changes and stat.S_ISDIR(expected.mode)
        ):
            return False
        if (
            current.device,
            current.inode,
            current.mode,
            current.size,
            current.reparse_tag,
            current.link_target,
        ) != (
            expected.device,
            expected.inode,
            expected.mode,
            expected.size,
            expected.reparse_tag,
            expected.link_target,
        ):
            return False
    return True


def _manifest_difference_summary(
    before: dict[str, PathIdentity],
    after: dict[str, PathIdentity],
    *,
    allowed_mtime_changes: frozenset[str],
) -> str:
    """Describe only changed metadata field counts, never profile paths or contents."""

    counts: dict[str, int] = {}
    removed = before.keys() - after.keys()
    added = after.keys() - before.keys()
    if removed:
        counts["removed_entries"] = len(removed)
    if added:
        counts["added_entries"] = len(added)
    fields = (
        "device",
        "inode",
        "mode",
        "size",
        "modified_at_ns",
        "reparse_tag",
        "link_target",
    )
    for relative in before.keys() & after.keys():
        expected = before[relative]
        current = after[relative]
        for field in fields:
            if getattr(expected, field) != getattr(current, field):
                counts[field] = counts.get(field, 0) + 1
                if field == "modified_at_ns" and relative not in allowed_mtime_changes:
                    if relative == ".":
                        category = "unallowed_root_mtime"
                    elif stat.S_ISDIR(expected.mode):
                        category = "unallowed_directory_mtime"
                    else:
                        category = "unallowed_file_mtime"
                    counts[category] = counts.get(category, 0) + 1
    return ",".join(f"{field}={counts[field]}" for field in sorted(counts)) or "none"


def _linux_rename_no_replace(
    source: Path,
    destination: Path,
    *,
    source_parent_identity: PathIdentity | None = None,
    destination_parent_identity: PathIdentity | None = None,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise NoReplaceUnavailableError("renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    source_expected = source_parent_identity or PathIdentity.from_stat(
        _assert_plain_directory(source.parent, label="source parent")
    )
    destination_expected = destination_parent_identity or PathIdentity.from_stat(
        _assert_plain_directory(destination.parent, label="destination parent")
    )
    source_fd = os.open(source.parent, flags)
    try:
        _assert_bound_directory(source_fd, source_expected, label="source parent")
        destination_fd = os.open(destination.parent, flags)
        try:
            _assert_bound_directory(
                destination_fd,
                destination_expected,
                label="destination parent",
            )
            result = renameat2(
                source_fd,
                os.fsencode(source.name),
                destination_fd,
                os.fsencode(destination.name),
                _RENAME_NOREPLACE,
            )
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise DestinationExistsError(f"destination already exists: {destination}")
    if error_number == errno.EXDEV:
        raise CrossDeviceMoveError("cross-filesystem recovery moves are not allowed")
    if error_number in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
        raise NoReplaceUnavailableError("renameat2(RENAME_NOREPLACE) is unavailable")
    raise RecoveryError(
        f"native no-replace move failed: {os.strerror(error_number)}",
        stable_code="no_replace_move_failed",
    )


def _macos_rename_no_replace(
    source: Path,
    destination: Path,
    *,
    source_parent_identity: PathIdentity | None = None,
    destination_parent_identity: PathIdentity | None = None,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameatx_np = getattr(libc, "renameatx_np", None)
    if renameatx_np is None:
        raise NoReplaceUnavailableError("renameatx_np(RENAME_EXCL) is unavailable")
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int

    # Bind both parents before the mutation.  A path-only renamex_np call can
    # be redirected if either parent is exchanged after preflight.  dirfd-based
    # renameatx_np keeps the no-replace destination inside the directories we
    # actually inspected, matching the Linux renameat2 contract.
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    source_expected = source_parent_identity or PathIdentity.from_stat(
        _assert_plain_directory(source.parent, label="source parent")
    )
    destination_expected = destination_parent_identity or PathIdentity.from_stat(
        _assert_plain_directory(destination.parent, label="destination parent")
    )
    try:
        source_fd = os.open(source.parent, flags)
    except OSError as exc:
        raise UnsafePathError(f"source parent changed before native move: {source.parent}") from exc
    try:
        _assert_bound_directory(source_fd, source_expected, label="source parent")
        try:
            destination_fd = os.open(destination.parent, flags)
        except OSError as exc:
            raise UnsafePathError(
                f"destination parent changed before native move: {destination.parent}"
            ) from exc
        try:
            _assert_bound_directory(
                destination_fd,
                destination_expected,
                label="destination parent",
            )
            result = renameatx_np(
                source_fd,
                os.fsencode(source.name),
                destination_fd,
                os.fsencode(destination.name),
                _RENAME_EXCL,
            )
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise DestinationExistsError(f"destination already exists: {destination}")
    if error_number == errno.EXDEV:
        raise CrossDeviceMoveError("cross-filesystem recovery moves are not allowed")
    if error_number in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP):
        raise NoReplaceUnavailableError("renameatx_np(RENAME_EXCL) is unavailable")
    raise RecoveryError(
        f"native no-replace move failed: {os.strerror(error_number)}",
        stable_code="no_replace_move_failed",
    )


def _windows_rename_info(destination_name: str, destination_parent_handle: int):
    """Build handle-relative FILE_RENAME_INFORMATION with replacement disabled."""

    if not destination_name or destination_name in {".", ".."} or "\x00" in destination_name:
        raise UnsafePathError("destination leaf name is invalid")
    encoded_name = destination_name.encode("utf-16-le")
    # Use explicit UTF-16 code units so the layout remains the Windows ABI even
    # when contract tests construct the buffer on a non-Windows host. The native
    # structure carries a trailing WCHAR placeholder; FileNameLength excludes
    # the required NUL terminator.
    # The native length contract is sizeof(FILE_RENAME_INFORMATION) plus the
    # visible FileName bytes. Reserve both its WCHAR placeholder and the NUL.
    name_type = ctypes.c_uint16 * (len(encoded_name) // 2 + 2)

    class _WindowsFileRenameInformation(ctypes.Structure):
        _fields_ = [
            ("replace_or_flags", ctypes.c_uint32),
            ("root_directory", ctypes.c_void_p),
            ("file_name_length", ctypes.c_uint32),
            ("file_name", name_type),
        ]

    info = _WindowsFileRenameInformation()
    # FileRenameInformation reads the first byte as ReplaceIfExists. Clearing
    # the complete union storage keeps replacement disabled on every ABI.
    info.replace_or_flags = 0
    info.root_directory = destination_parent_handle
    info.file_name_length = len(encoded_name)
    for index in range(0, len(encoded_name), 2):
        info.file_name[index // 2] = int.from_bytes(encoded_name[index : index + 2], "little")
    return info


def _windows_handle_value(handle: object) -> int:
    value = getattr(handle, "value", handle)
    if value is None:
        return 0
    if not isinstance(value, int):
        raise UnsafePathError("Windows returned an invalid native handle")
    return value


def _windows_move_no_replace(
    source: Path,
    destination: Path,
    *,
    source_identity: PathIdentity | None = None,
    source_parent_identity: PathIdentity | None = None,
    destination_parent_identity: PathIdentity | None = None,
    _before_mutation: Callable[[], None] | None = None,
    _mutation_guard: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
) -> None:
    win_dll = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    ntdll = win_dll("ntdll")
    try:
        create_file = kernel32.CreateFileW
        get_information = kernel32.GetFileInformationByHandleEx
        close_handle = kernel32.CloseHandle
        nt_set_information = ntdll.NtSetInformationFile
        status_to_dos_error = ntdll.RtlNtStatusToDosError
    except AttributeError as exc:
        raise NoReplaceUnavailableError(
            "handle-relative Windows rename APIs are unavailable"
        ) from exc
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    get_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    get_information.restype = ctypes.c_int
    nt_set_information.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_WindowsIoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    nt_set_information.restype = ctypes.c_int32
    status_to_dos_error.argtypes = [ctypes.c_int32]
    status_to_dos_error.restype = ctypes.c_uint32
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    unsupported_errors = {
        _WINDOWS_ERROR_INVALID_FUNCTION,
        _WINDOWS_ERROR_NOT_SUPPORTED,
        _WINDOWS_ERROR_INVALID_PARAMETER,
        _WINDOWS_ERROR_CALL_NOT_IMPLEMENTED,
    }
    # Parent handles exclude DELETE sharing so their path identities cannot be
    # exchanged before the handle-relative mutation. The source handle itself
    # must share DELETE: Windows otherwise rejects the rename requested through
    # that same open file object. Its handle still binds the exact source inode,
    # while profile locks and the post-move manifest detect outside mutation.
    pinned_parent_share_mode = _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE
    source_share_mode = pinned_parent_share_mode | _WINDOWS_FILE_SHARE_DELETE
    open_flags = _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
    invalid_handle = ctypes.c_void_p(-1).value

    def open_handle(path: Path, access: int, share_mode: int, *, label: str) -> int:
        handle = _windows_handle_value(
            create_file(
                _windows_extended_path(path),
                access,
                share_mode,
                None,
                _WINDOWS_OPEN_EXISTING,
                open_flags,
                None,
            )
        )
        if handle in {0, invalid_handle}:
            error_number = getattr(ctypes, "get_last_error")()
            raise UnsafePathError(
                f"cannot bind {label} for native move (Windows error {error_number})"
            )
        return handle

    def assert_handle(
        handle: int,
        expected: PathIdentity,
        *,
        label: str,
        require_directory: bool,
    ) -> None:
        attributes = _WindowsFileAttributeTagInfo()
        if not get_information(
            handle,
            _WINDOWS_FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(attributes),
            ctypes.sizeof(attributes),
        ):
            error_number = getattr(ctypes, "get_last_error")()
            if error_number in unsupported_errors:
                raise NoReplaceUnavailableError(
                    "Windows handle attribute query is unavailable"
                )
            raise UnsafePathError(
                f"cannot verify {label} handle (Windows error {error_number})"
            )
        if attributes.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise UnsafePathError(f"{label} became a reparse point before native move")
        if require_directory and not attributes.file_attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise UnsafePathError(f"{label} is not a directory")

        file_id = _WindowsFileIdInfo()
        if not get_information(
            handle,
            _WINDOWS_FILE_ID_INFO,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            error_number = getattr(ctypes, "get_last_error")()
            if error_number in unsupported_errors:
                raise NoReplaceUnavailableError("Windows file identity query is unavailable")
            raise UnsafePathError(
                f"cannot verify {label} identity (Windows error {error_number})"
            )
        actual_inode = int.from_bytes(bytes(file_id.file_id.identifier), "little")
        if (
            int(file_id.volume_serial_number) != expected.device
            or actual_inode != expected.inode
        ):
            raise UnsafePathError(f"{label} identity changed before native move")

    source_expected = source_identity or path_identity(source)
    source_parent_expected = source_parent_identity or path_identity(source.parent)
    destination_parent_expected = destination_parent_identity or path_identity(
        destination.parent
    )
    # RootDirectory is used to resolve the destination leaf relative to the
    # already-inspected directory handle. Windows requires traversal access
    # for that relative lookup; FILE_LIST_DIRECTORY only permits enumeration.
    parent_access = _WINDOWS_FILE_TRAVERSE | _WINDOWS_FILE_READ_ATTRIBUTES | _WINDOWS_SYNCHRONIZE
    source_parent_handle = open_handle(
        source.parent,
        parent_access,
        pinned_parent_share_mode,
        label="source parent",
    )
    error_number = 0
    native_status = 0
    try:
        assert_handle(
            source_parent_handle,
            source_parent_expected,
            label="source parent",
            require_directory=True,
        )
        source_handle = open_handle(
            source,
            _WINDOWS_DELETE_ACCESS | _WINDOWS_FILE_READ_ATTRIBUTES | _WINDOWS_SYNCHRONIZE,
            source_share_mode,
            label="source",
        )
        try:
            assert_handle(
                source_handle,
                source_expected,
                label="source",
                require_directory=stat.S_ISDIR(source_expected.mode),
            )
            destination_parent_handle = open_handle(
                destination.parent,
                parent_access,
                pinned_parent_share_mode,
                label="destination parent",
            )
            try:
                assert_handle(
                    destination_parent_handle,
                    destination_parent_expected,
                    label="destination parent",
                    require_directory=True,
                )
                guard = (
                    _mutation_guard()
                    if _mutation_guard is not None
                    else contextlib.nullcontext()
                )
                with guard:
                    if _before_mutation is not None:
                        _before_mutation()
                    rename_info = _windows_rename_info(destination.name, destination_parent_handle)
                    io_status = _WindowsIoStatusBlock()
                    # The Win32 FILE_RENAME_INFO contract requires RootDirectory to
                    # be NULL. FileRenameInformation is the native contract that
                    # supports resolving a leaf against our bound directory handle.
                    status = int(
                        nt_set_information(
                            source_handle,
                            ctypes.byref(io_status),
                            ctypes.byref(rename_info),
                            ctypes.sizeof(rename_info),
                            _WINDOWS_FILE_RENAME_INFORMATION,
                        )
                    )
                if status == 0:
                    return
                if status > 0:
                    raise AtomicStateUnknownError(
                        "handle-relative Windows rename returned ambiguous "
                        f"NTSTATUS 0x{status & 0xFFFFFFFF:08X}"
                    )
                native_status = status & 0xFFFFFFFF
                error_number = int(status_to_dos_error(status))
            finally:
                close_handle(destination_parent_handle)
        finally:
            close_handle(source_handle)
    finally:
        close_handle(source_parent_handle)
    try:
        source_after_failure = path_identity(source)
    except (OSError, RecoveryError) as exc:
        raise AtomicStateUnknownError(
            "Windows rename reported failure but the source identity is no longer provable"
        ) from exc
    if source_after_failure.token != source_expected.token:
        raise AtomicStateUnknownError(
            "Windows rename reported failure after the source identity changed"
        )
    if error_number in (_WINDOWS_ERROR_ALREADY_EXISTS, _WINDOWS_ERROR_FILE_EXISTS):
        raise DestinationExistsError(f"destination already exists: {destination}")
    if error_number == _WINDOWS_ERROR_NOT_SAME_DEVICE:
        raise CrossDeviceMoveError("cross-filesystem recovery moves are not allowed")
    if error_number in unsupported_errors:
        raise NoReplaceUnavailableError(
            "handle-relative Windows rename is unavailable "
            f"(NTSTATUS 0x{native_status:08X}; Windows error {error_number})"
        )
    raise RecoveryError(
        "native no-replace move failed "
        f"(NTSTATUS 0x{native_status:08X}; Windows error {error_number})",
        stable_code="no_replace_move_failed",
    )


def _windows_extended_path(path: str | Path) -> str:
    """Return a MoveFileW-compatible extended-length absolute spelling."""
    value = ntpath.abspath(str(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return f"\\\\?\\UNC\\{value[2:]}"
    return f"\\\\?\\{value}"


def native_move_no_replace(
    source: str | Path,
    destination: str | Path,
    *,
    _mutation_guard: Callable[[], contextlib.AbstractContextManager[None]] | None = None,
    _allowed_manifest_mtime_changes: frozenset[str] = frozenset(),
    _opaque_manifest_directories: frozenset[str] = frozenset(),
    _link_leaf_manifest_directories: frozenset[str] = frozenset(),
    _use_profile_manifest_policy: bool = False,
) -> None:
    """Atomically move ``source`` without ever replacing ``destination``.

    There is deliberately no check-then-rename or copy/delete fallback. If the
    required platform primitive is missing, the operation stops for recovery UI.
    """

    source_path = Path(source).expanduser().absolute()
    destination_path = Path(destination).expanduser().absolute()
    if source_path == destination_path:
        raise UnsafePathError("source and destination are the same path")

    source_parent_before = PathIdentity.from_stat(
        _assert_plain_directory(source_path.parent, label="source parent")
    )
    destination_parent_before = PathIdentity.from_stat(
        _assert_plain_directory(destination_path.parent, label="destination parent")
    )
    if source_parent_before.device != destination_parent_before.device:
        raise CrossDeviceMoveError("cross-filesystem recovery moves are not allowed")

    try:
        destination_path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise DestinationExistsError(f"destination already exists: {destination_path}")

    opaque_manifest_directories = _opaque_manifest_directories
    link_leaf_manifest_directories = _link_leaf_manifest_directories
    if _use_profile_manifest_policy:
        opaque_manifest_directories |= _profile_opaque_manifest_directories()
        link_leaf_manifest_directories |= _PROFILE_LINK_LEAF_DIRECTORIES

    manifest_before = no_follow_manifest(
        source_path,
        opaque_directories=opaque_manifest_directories,
        link_leaf_directories=link_leaf_manifest_directories,
    )
    if sys.platform.startswith("linux"):
        _linux_rename_no_replace(
            source_path,
            destination_path,
            source_parent_identity=source_parent_before,
            destination_parent_identity=destination_parent_before,
        )
    elif sys.platform == "darwin":
        _macos_rename_no_replace(
            source_path,
            destination_path,
            source_parent_identity=source_parent_before,
            destination_parent_identity=destination_parent_before,
        )
    elif os.name == "nt" or sys.platform == "win32":
        _windows_move_no_replace(
            source_path,
            destination_path,
            source_identity=manifest_before["."],
            source_parent_identity=source_parent_before,
            destination_parent_identity=destination_parent_before,
            _mutation_guard=_mutation_guard,
        )
    else:
        raise NoReplaceUnavailableError(f"no native no-replace move for {sys.platform}")

    try:
        source_parent_after = path_identity(source_path.parent)
        destination_parent_after = path_identity(destination_path.parent)
        manifest_after = no_follow_manifest(
            destination_path,
            opaque_directories=opaque_manifest_directories,
            link_leaf_directories=link_leaf_manifest_directories,
        )
    except AtomicStateUnknownError:
        raise
    except (OSError, RecoveryError) as exc:
        # The native rename has already reported success.  From this point on,
        # even a normally precise unsafe-path error describes an unverifiable
        # *post-mutation* tree, not a harmless preflight refusal.  Preserve that
        # distinction so callers can never reinterpret the destination as ready
        # or stamp a compatibility marker after verification failed.
        raise AtomicStateUnknownError(
            "move completed but post-move filesystem state could not be verified"
        ) from exc
    manifest_matches = _manifest_matches_after_move(
        manifest_before,
        manifest_after,
        allowed_mtime_changes=_allowed_manifest_mtime_changes,
        allow_directory_mtime_changes=os.name == "nt" or sys.platform == "win32",
    )
    if (
        source_parent_after.token != source_parent_before.token
        or destination_parent_after.token != destination_parent_before.token
        or not manifest_matches
    ):
        manifest_difference = _manifest_difference_summary(
            manifest_before,
            manifest_after,
            allowed_mtime_changes=_allowed_manifest_mtime_changes,
        )
        raise AtomicStateUnknownError(
            "move completed but parent or source metadata changed during verification "
            f"(source_parent_changed="
            f"{source_parent_after.token != source_parent_before.token}, "
            f"destination_parent_changed="
            f"{destination_parent_after.token != destination_parent_before.token}, "
            f"manifest_fields={manifest_difference})"
        )


__all__ = [
    "PathIdentity",
    "native_move_no_replace",
    "no_follow_manifest",
    "path_identity",
    "profile_no_follow_manifest",
]
