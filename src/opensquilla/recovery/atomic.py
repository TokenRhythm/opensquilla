"""Fail-closed filesystem primitives used by recovery transactions."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import ntpath
import os
import stat
import struct
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
_ALLOWED_LINK_REPARSE_TAGS = frozenset({_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK})
_REPARSE_NAME_SURROGATE_BIT = 0x20000000
_WINDOWS_DELETE_ACCESS = 0x00010000
_WINDOWS_FILE_ADD_SUBDIRECTORY = 0x00000004
_WINDOWS_FILE_TRAVERSE = 0x00000020
_WINDOWS_FILE_READ_ATTRIBUTES = 0x00000080
_WINDOWS_FILE_WRITE_ATTRIBUTES = 0x00000100
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_FILE_SHARE_DELETE = 0x00000004
_WINDOWS_FILE_CREATE = 2
_WINDOWS_FILE_CREATED = 2
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_DIRECTORY_FILE = 0x00000001
_WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_ATTRIBUTE_TAG_INFO = 9
_WINDOWS_FILE_ID_INFO = 18
_WINDOWS_FILE_RENAME_INFORMATION = 10
_WINDOWS_FILE_DISPOSITION_INFO = 4
_WINDOWS_FSCTL_GET_REPARSE_POINT = 0x000900A8
_WINDOWS_FSCTL_SET_REPARSE_POINT = 0x000900A4
_WINDOWS_MAXIMUM_REPARSE_DATA_BUFFER_SIZE = 16 * 1024
_WINDOWS_OBJ_CASE_INSENSITIVE = 0x00000040
_WINDOWS_STATUS_OBJECT_NAME_EXISTS = 0x40000000
_WINDOWS_STATUS_OBJECT_NAME_COLLISION = 0xC0000035

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


class _WindowsUnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint16),
        ("maximum_length", ctypes.c_uint16),
        ("buffer", ctypes.POINTER(ctypes.c_uint16)),
    ]


class _WindowsObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("root_directory", ctypes.c_void_p),
        ("object_name", ctypes.POINTER(_WindowsUnicodeString)),
        ("attributes", ctypes.c_uint32),
        ("security_descriptor", ctypes.c_void_p),
        ("security_quality_of_service", ctypes.c_void_p),
    ]


class _WindowsFileDispositionInfo(ctypes.Structure):
    _fields_ = [("delete_file", ctypes.c_ubyte)]


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
        if observed_tag is not None and not reparse_tag_redirects(observed_tag):
            # Data-only reparse tags (cloud placeholders, WOF compression) are
            # hydration state, not filesystem identity: fstat cannot observe
            # them and sync clients toggle them at will.
            observed_tag = None
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


def reparse_tag_redirects(tag: int) -> bool:
    """Return True when a Windows reparse tag redirects path resolution.

    Name surrogates (symlinks, junctions) substitute another path during
    lookups and stay rejected everywhere. Data-only reparse points - OneDrive
    Files On-Demand placeholders, WOF-compressed files - keep resolution
    intact and behave as plain entries. An unobservable tag fails closed.
    """

    if tag == 0:
        return True
    return bool(tag & _REPARSE_NAME_SURROGATE_BIT)


def is_path_redirecting_stat(value: os.stat_result) -> bool:
    """Symlink, or a Windows reparse point that redirects path resolution."""

    if stat.S_ISLNK(value.st_mode):
        return True
    if not _is_reparse_point(value):
        return False
    return reparse_tag_redirects(int(getattr(value, "st_reparse_tag", 0)))


def _is_link_or_reparse(value: os.stat_result) -> bool:
    return is_path_redirecting_stat(value)


def path_identity(path: str | Path, *, follow_symlinks: bool = False) -> PathIdentity:
    candidate = Path(path)
    native_candidate = _native_io_path(candidate)
    value = os.stat(native_candidate) if follow_symlinks else os.lstat(native_candidate)
    if follow_symlinks or not _is_link_or_reparse(value):
        return PathIdentity.from_stat(value)
    return _link_leaf_identity(candidate, value)


def _link_leaf_identity(path: Path, value: os.stat_result) -> PathIdentity:
    """Return no-follow identity for an approved link/reparse manifest leaf."""

    tag = int(getattr(value, "st_reparse_tag", 0)) or None
    if tag is not None and tag not in _ALLOWED_LINK_REPARSE_TAGS:
        raise UnsafePathError(f"unsupported reparse point in recovery source: {path}")
    try:
        target = os.readlink(_native_io_path(path))
    except OSError as exc:
        raise UnsafePathError(f"cannot inspect recovery link target: {path}") from exc
    return PathIdentity.from_stat(value, reparse_tag=tag, link_target=target)


def _assert_plain_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        value = os.lstat(_native_io_path(path))
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
    are recorded by their no-follow identity and are not traversed. Data-only
    Windows reparse points (cloud sync placeholders, WOF compression) do not
    redirect path resolution and are manifested as the plain entries they are.
    """

    root_path = Path(root)
    root_stat = os.lstat(_native_io_path(root_path))
    if _is_link_or_reparse(root_stat):
        raise UnsafePathError(f"automatic operations refuse links or reparse points: {root_path}")
    if not (stat.S_ISDIR(root_stat.st_mode) or stat.S_ISREG(root_stat.st_mode)):
        raise UnsafePathError(f"automatic operations refuse special files: {root_path}")

    result = {".": PathIdentity.from_stat(root_stat)}
    if stat.S_ISREG(root_stat.st_mode):
        return result

    def visit(directory: Path, relative: Path) -> None:
        try:
            with os.scandir(_native_io_path(directory)) as iterator:
                entry_names = sorted(entry.name for entry in iterator)
        except OSError as exc:
            raise UnsafePathError(f"cannot enumerate recovery source: {directory}") from exc
        for entry_name in entry_names:
            child_relative = relative / entry_name
            child_key = child_relative.as_posix()
            child_path = directory / entry_name
            try:
                value = os.lstat(_native_io_path(child_path))
            except OSError as exc:
                raise UnsafePathError(f"cannot inspect recovery source: {child_path}") from exc
            if _is_link_or_reparse(value):
                if not any(
                    child_relative != Path(link_root)
                    and child_relative.is_relative_to(Path(link_root))
                    for link_root in link_leaf_directories
                ):
                    raise UnsafePathError(
                        f"automatic operations refuse links or reparse points: {child_path}"
                    )
                result[child_key] = _link_leaf_identity(child_path, value)
                continue
            if not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)):
                raise UnsafePathError(f"automatic operations refuse special files: {child_path}")
            result[child_key] = PathIdentity.from_stat(value)
            if stat.S_ISDIR(value.st_mode) and child_key not in opaque_directories:
                visit(child_path, child_relative)

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


def _windows_nt_create_relative_directory(
    nt_create_file: Callable[..., int],
    status_to_dos_error: Callable[[int], int],
    close_handle: Callable[[int], int],
    *,
    parent_handle: int,
    name: str,
    share_access: int = 0,
) -> int:
    """Create and bind one directory leaf beneath a pinned Windows parent."""

    if not name or name in {".", ".."} or "\x00" in name or "/" in name or "\\" in name:
        raise UnsafePathError("Windows junction destination leaf name is invalid")
    encoded_name = name.encode("utf-16-le")
    if len(encoded_name) + 2 > 0xFFFF:
        raise UnsafePathError("Windows junction destination leaf name is too long")
    name_type = ctypes.c_uint16 * (len(encoded_name) // 2 + 1)
    name_buffer = name_type()
    ctypes.memmove(name_buffer, encoded_name, len(encoded_name))
    unicode_name = _WindowsUnicodeString(
        length=len(encoded_name),
        maximum_length=len(encoded_name) + 2,
        buffer=ctypes.cast(name_buffer, ctypes.POINTER(ctypes.c_uint16)),
    )
    object_attributes = _WindowsObjectAttributes(
        length=ctypes.sizeof(_WindowsObjectAttributes),
        root_directory=parent_handle,
        object_name=ctypes.pointer(unicode_name),
        attributes=_WINDOWS_OBJ_CASE_INSENSITIVE,
        security_descriptor=None,
        security_quality_of_service=None,
    )
    io_status = _WindowsIoStatusBlock()
    handle = ctypes.c_void_p()
    status = int(
        nt_create_file(
            ctypes.byref(handle),
            _WINDOWS_DELETE_ACCESS
            | _WINDOWS_FILE_READ_ATTRIBUTES
            | _WINDOWS_FILE_WRITE_ATTRIBUTES
            | _WINDOWS_SYNCHRONIZE,
            ctypes.byref(object_attributes),
            ctypes.byref(io_status),
            None,
            _FILE_ATTRIBUTE_DIRECTORY,
            share_access,
            _WINDOWS_FILE_CREATE,
            _WINDOWS_FILE_DIRECTORY_FILE
            | _WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT
            | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
            0,
        )
    )
    status_code = status & 0xFFFFFFFF
    handle_value = _windows_handle_value(handle)
    invalid_handle = ctypes.c_void_p(-1).value
    handle_is_valid = handle_value not in {0, invalid_handle}
    if status_code in {
        _WINDOWS_STATUS_OBJECT_NAME_EXISTS,
        _WINDOWS_STATUS_OBJECT_NAME_COLLISION,
    }:
        if handle_is_valid and not close_handle(handle_value):
            raise AtomicStateUnknownError(
                "Windows returned an uncloseable junction handle on name collision"
            )
        raise FileExistsError(errno.EEXIST, "destination already exists", name)
    if status_code != 0:
        if ctypes.c_int32(status_code).value < 0:
            windows_error = int(status_to_dos_error(ctypes.c_int32(status_code).value))
            if handle_is_valid and not close_handle(handle_value):
                raise AtomicStateUnknownError(
                    "Windows junction creation failed and returned an uncloseable handle"
                )
            raise UnsafePathError(
                "cannot create junction destination safely "
                f"(NTSTATUS 0x{status_code:08x}, Windows error {windows_error}): {name}"
            )
        if handle_is_valid and not close_handle(handle_value):
            raise AtomicStateUnknownError(
                "Windows junction creation returned an uncloseable ambiguous handle"
            )
        raise AtomicStateUnknownError(
            "Windows junction creation returned an ambiguous informational status "
            f"0x{status_code:08x}"
        )
    if not handle_is_valid:
        raise AtomicStateUnknownError(
            "Windows reported junction destination creation without returning its handle"
        )
    if int(io_status.information) != _WINDOWS_FILE_CREATED:
        if not close_handle(handle_value):
            raise AtomicStateUnknownError(
                "Windows junction creation provenance was unknown and its handle did not close"
            )
        raise AtomicStateUnknownError(
            "Windows junction creation did not report a newly created directory"
        )
    return handle_value


def _validated_windows_mount_point_buffer(raw: bytes) -> bytes:
    """Validate one native mount-point payload before reproducing it."""

    if len(raw) < 16 or len(raw) > _WINDOWS_MAXIMUM_REPARSE_DATA_BUFFER_SIZE:
        raise UnsafePathError("Windows mount-point reparse data has an invalid size")
    tag, data_length, _reserved = struct.unpack_from("<IHH", raw)
    if tag != _IO_REPARSE_TAG_MOUNT_POINT:
        raise UnsafePathError("Windows reparse point is not a directory junction")
    if data_length < 8 or 8 + data_length != len(raw):
        raise UnsafePathError("Windows mount-point reparse data length is invalid")

    substitute_offset, substitute_length, print_offset, print_length = struct.unpack_from(
        "<HHHH",
        raw,
        8,
    )
    path_buffer = raw[16:]

    def validated_name(offset: int, length: int, *, label: str) -> str:
        if offset % 2 or length % 2 or offset + length > len(path_buffer):
            raise UnsafePathError(f"Windows mount-point {label} bounds are invalid")
        end = offset + length
        try:
            value = path_buffer[offset:end].decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise UnsafePathError(f"Windows mount-point {label} is not valid UTF-16") from exc
        if "\0" in value:
            raise UnsafePathError(f"Windows mount-point {label} contains an embedded NUL")
        return value

    substitute_name = validated_name(
        substitute_offset,
        substitute_length,
        label="substitute name",
    )
    validated_name(print_offset, print_length, label="print name")
    if (
        len(substitute_name) < 7
        or not substitute_name.startswith("\\??\\")
        or not substitute_name[4].isascii()
        or not substitute_name[4].isalpha()
        or substitute_name[5:7] != ":\\"
        or "/" in substitute_name[7:]
    ):
        raise UnsafePathError("Windows junction target must be a local drive-absolute path")
    target_parts = substitute_name[7:].split("\\")
    if any(part in {".", ".."} for part in target_parts) or any(
        not part and index != len(target_parts) - 1 for index, part in enumerate(target_parts)
    ):
        raise UnsafePathError("Windows junction target contains unsafe path components")

    normalized = bytearray(raw)
    normalized[6:8] = b"\0\0"
    return bytes(normalized)


def _copy_windows_mount_point_no_follow(
    source: str | Path,
    destination: str | Path,
    *,
    publish_destination: str | Path | None = None,
) -> None:
    """Copy one Windows directory junction without traversing or retagging it."""

    if os.name != "nt":
        raise UnsafePathError("Windows mount-point copying is unavailable on this platform")

    source_path = Path(source)
    destination_path = Path(destination)
    publish_path = Path(publish_destination) if publish_destination is not None else None
    if publish_path is not None and ntpath.normcase(
        ntpath.abspath(os.fspath(publish_path.parent))
    ) != ntpath.normcase(ntpath.abspath(os.fspath(destination_path.parent))):
        raise UnsafePathError("junction publish destination must share its temporary parent")
    source_expected = path_identity(source_path)
    if source_expected.reparse_tag != _IO_REPARSE_TAG_MOUNT_POINT or not stat.S_ISDIR(
        source_expected.mode
    ):
        raise UnsafePathError("recovery link is not a Windows directory junction")
    destination_parent_expected = path_identity(destination_path.parent)
    if destination_parent_expected.reparse_tag is not None or not stat.S_ISDIR(
        destination_parent_expected.mode
    ):
        raise UnsafePathError("junction destination parent must be a real directory")

    win_dll = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    ntdll = win_dll("ntdll")
    try:
        create_file = kernel32.CreateFileW
        device_io_control = kernel32.DeviceIoControl
        get_information = kernel32.GetFileInformationByHandleEx
        set_information = kernel32.SetFileInformationByHandle
        close_handle = kernel32.CloseHandle
        nt_create_file = ntdll.NtCreateFile
        nt_set_information = ntdll.NtSetInformationFile
        status_to_dos_error = ntdll.RtlNtStatusToDosError
    except AttributeError as exc:
        raise UnsafePathError("Windows reparse-point APIs are unavailable") from exc

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
    device_io_control.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    device_io_control.restype = ctypes.c_int
    get_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    get_information.restype = ctypes.c_int
    set_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    set_information.restype = ctypes.c_int
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    nt_create_file.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint32,
        ctypes.POINTER(_WindowsObjectAttributes),
        ctypes.POINTER(_WindowsIoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    nt_create_file.restype = ctypes.c_int32
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

    invalid_handle = ctypes.c_void_p(-1).value
    open_flags = _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
    destination_parent_share_mode = (
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE
        if publish_path is not None
        else _WINDOWS_FILE_SHARE_READ
    )

    def open_handle(
        path: Path,
        access: int,
        *,
        share_mode: int,
        label: str,
    ) -> int:
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
            raise UnsafePathError(f"cannot bind {label} (Windows error {error_number})")
        return handle

    def inspect_handle(
        handle: int,
        *,
        label: str,
        expected_tag: int | None,
    ) -> tuple[int, int]:
        attributes = _WindowsFileAttributeTagInfo()
        if not get_information(
            handle,
            _WINDOWS_FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(attributes),
            ctypes.sizeof(attributes),
        ):
            error_number = getattr(ctypes, "get_last_error")()
            raise UnsafePathError(
                f"cannot verify {label} attributes (Windows error {error_number})"
            )
        observed_tag = int(attributes.reparse_tag)
        is_reparse = bool(attributes.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT)
        if expected_tag is None:
            if (is_reparse or observed_tag) and reparse_tag_redirects(observed_tag):
                raise UnsafePathError(f"{label} became a reparse point")
        elif not is_reparse or observed_tag != expected_tag:
            raise UnsafePathError(f"{label} reparse tag changed")
        if not attributes.file_attributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise UnsafePathError(f"{label} is not a directory")

        file_id = _WindowsFileIdInfo()
        if not get_information(
            handle,
            _WINDOWS_FILE_ID_INFO,
            ctypes.byref(file_id),
            ctypes.sizeof(file_id),
        ):
            error_number = getattr(ctypes, "get_last_error")()
            raise UnsafePathError(f"cannot verify {label} identity (Windows error {error_number})")
        return (
            int(file_id.volume_serial_number),
            int.from_bytes(bytes(file_id.file_id.identifier), "little"),
        )

    def assert_handle(
        handle: int,
        expected: PathIdentity | tuple[int, int],
        *,
        label: str,
        expected_tag: int | None,
    ) -> None:
        actual = inspect_handle(handle, label=label, expected_tag=expected_tag)
        expected_identity = (
            (expected.device, expected.inode) if isinstance(expected, PathIdentity) else expected
        )
        if actual != expected_identity:
            raise UnsafePathError(f"{label} identity changed")

    def read_payload(handle: int, *, label: str) -> bytes:
        output = ctypes.create_string_buffer(_WINDOWS_MAXIMUM_REPARSE_DATA_BUFFER_SIZE)
        returned = ctypes.c_uint32()
        if not device_io_control(
            handle,
            _WINDOWS_FSCTL_GET_REPARSE_POINT,
            None,
            0,
            output,
            len(output),
            ctypes.byref(returned),
            None,
        ):
            error_number = getattr(ctypes, "get_last_error")()
            raise UnsafePathError(
                f"cannot read {label} reparse data (Windows error {error_number})"
            )
        return _validated_windows_mount_point_buffer(output.raw[: returned.value])

    destination_created = False
    destination_published = False
    publish_state_unknown = False
    destination_identity: tuple[int, int] | None = None
    source_handle = 0
    destination_parent_handle = 0
    destination_handle = 0
    try:
        source_handle = open_handle(
            source_path,
            _WINDOWS_FILE_READ_ATTRIBUTES | _WINDOWS_SYNCHRONIZE,
            share_mode=_WINDOWS_FILE_SHARE_READ,
            label="source junction",
        )
        assert_handle(
            source_handle,
            source_expected,
            label="source junction",
            expected_tag=_IO_REPARSE_TAG_MOUNT_POINT,
        )
        source_payload = read_payload(source_handle, label="source junction")

        destination_parent_handle = open_handle(
            destination_path.parent,
            _WINDOWS_FILE_ADD_SUBDIRECTORY
            | _WINDOWS_FILE_TRAVERSE
            | _WINDOWS_FILE_READ_ATTRIBUTES
            | _WINDOWS_SYNCHRONIZE,
            share_mode=destination_parent_share_mode,
            label="junction destination parent",
        )
        assert_handle(
            destination_parent_handle,
            destination_parent_expected,
            label="junction destination parent",
            expected_tag=None,
        )
        assert_handle(
            source_handle,
            source_expected,
            label="source junction",
            expected_tag=_IO_REPARSE_TAG_MOUNT_POINT,
        )
        destination_handle = _windows_nt_create_relative_directory(
            nt_create_file,
            status_to_dos_error,
            close_handle,
            parent_handle=destination_parent_handle,
            name=destination_path.name,
            share_access=(
                _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_DELETE
                if publish_path is not None
                else 0
            ),
        )
        destination_created = True
        destination_identity = inspect_handle(
            destination_handle,
            label="junction destination",
            expected_tag=None,
        )
        assert_handle(
            destination_parent_handle,
            destination_parent_expected,
            label="junction destination parent",
            expected_tag=None,
        )
        assert_handle(
            source_handle,
            source_expected,
            label="source junction",
            expected_tag=_IO_REPARSE_TAG_MOUNT_POINT,
        )

        input_buffer = ctypes.create_string_buffer(source_payload, len(source_payload))
        returned = ctypes.c_uint32()
        if not device_io_control(
            destination_handle,
            _WINDOWS_FSCTL_SET_REPARSE_POINT,
            input_buffer,
            len(source_payload),
            None,
            0,
            ctypes.byref(returned),
            None,
        ):
            error_number = getattr(ctypes, "get_last_error")()
            raise UnsafePathError(
                f"cannot create destination junction (Windows error {error_number})"
            )
        assert_handle(
            destination_parent_handle,
            destination_parent_expected,
            label="junction destination parent",
            expected_tag=None,
        )
        assert_handle(
            source_handle,
            source_expected,
            label="source junction",
            expected_tag=_IO_REPARSE_TAG_MOUNT_POINT,
        )
        assert_handle(
            destination_handle,
            destination_identity,
            label="junction destination",
            expected_tag=_IO_REPARSE_TAG_MOUNT_POINT,
        )
        if read_payload(destination_handle, label="destination junction") != source_payload:
            raise UnsafePathError("destination junction reparse data differs from its source")
        if read_payload(source_handle, label="source junction") != source_payload:
            raise UnsafePathError("source junction changed while it was copied")

        source_after = path_identity(source_path)
        destination_parent_after = path_identity(destination_path.parent)
        destination_after = path_identity(destination_path)
        if (
            source_after.token != source_expected.token
            or source_after.reparse_tag != source_expected.reparse_tag
            or source_after.link_target != source_expected.link_target
        ):
            raise UnsafePathError("source junction changed while it was copied")
        if (
            destination_parent_after.token != destination_parent_expected.token
            or destination_parent_after.reparse_tag is not None
        ):
            raise UnsafePathError("junction destination parent changed while it was copied")
        if (
            (destination_after.device, destination_after.inode) != destination_identity
            or destination_after.reparse_tag != _IO_REPARSE_TAG_MOUNT_POINT
            or destination_after.link_target != source_expected.link_target
        ):
            raise UnsafePathError("destination junction identity could not be verified")
        assert_handle(
            destination_parent_handle,
            destination_parent_expected,
            label="junction destination parent",
            expected_tag=None,
        )
        assert_handle(
            source_handle,
            source_expected,
            label="source junction",
            expected_tag=_IO_REPARSE_TAG_MOUNT_POINT,
        )
        assert_handle(
            destination_handle,
            destination_identity,
            label="junction destination",
            expected_tag=_IO_REPARSE_TAG_MOUNT_POINT,
        )
        if read_payload(destination_handle, label="destination junction") != source_payload:
            raise UnsafePathError("destination junction changed after verification")
        if read_payload(source_handle, label="source junction") != source_payload:
            raise UnsafePathError("source junction changed after verification")

        if publish_path is not None:
            rename_info = _windows_rename_info(
                publish_path.name,
                destination_parent_handle,
            )
            rename_io_status = _WindowsIoStatusBlock()
            rename_status = int(
                nt_set_information(
                    destination_handle,
                    ctypes.byref(rename_io_status),
                    ctypes.byref(rename_info),
                    ctypes.sizeof(rename_info),
                    _WINDOWS_FILE_RENAME_INFORMATION,
                )
            )
            rename_status_code = rename_status & 0xFFFFFFFF
            if rename_status_code != 0:
                if ctypes.c_int32(rename_status_code).value < 0:
                    windows_error = int(
                        status_to_dos_error(ctypes.c_int32(rename_status_code).value)
                    )
                    if windows_error in {
                        _WINDOWS_ERROR_ALREADY_EXISTS,
                        _WINDOWS_ERROR_FILE_EXISTS,
                    }:
                        raise FileExistsError(
                            errno.EEXIST,
                            "junction publish destination already exists",
                            os.fspath(publish_path),
                        )
                    raise UnsafePathError(
                        "cannot publish junction destination safely "
                        f"(NTSTATUS 0x{rename_status_code:08x}, "
                        f"Windows error {windows_error}): {publish_path.name}"
                    )
                publish_state_unknown = True
                raise AtomicStateUnknownError(
                    "Windows junction publish returned an ambiguous informational status "
                    f"0x{rename_status_code:08x}"
                )
            destination_published = True
            if os.path.lexists(_native_io_path(destination_path)):
                raise UnsafePathError("junction temporary still exists after atomic publish")
            source_after_publish = path_identity(source_path)
            destination_parent_after_publish = path_identity(destination_path.parent)
            destination_after_publish = path_identity(publish_path)
            if (
                source_after_publish.token != source_expected.token
                or source_after_publish.reparse_tag != source_expected.reparse_tag
                or source_after_publish.link_target != source_expected.link_target
            ):
                raise UnsafePathError("source junction changed during atomic publish")
            if (
                destination_parent_after_publish.token != destination_parent_expected.token
                or destination_parent_after_publish.reparse_tag is not None
            ):
                raise UnsafePathError("junction destination parent changed during atomic publish")
            if (
                (destination_after_publish.device, destination_after_publish.inode)
                != destination_identity
                or destination_after_publish.reparse_tag != _IO_REPARSE_TAG_MOUNT_POINT
                or destination_after_publish.link_target != source_expected.link_target
            ):
                raise UnsafePathError("published junction identity could not be verified")
            assert_handle(
                destination_parent_handle,
                destination_parent_expected,
                label="junction destination parent",
                expected_tag=None,
            )
            assert_handle(
                source_handle,
                source_expected,
                label="source junction",
                expected_tag=_IO_REPARSE_TAG_MOUNT_POINT,
            )
            assert_handle(
                destination_handle,
                destination_identity,
                label="published junction destination",
                expected_tag=_IO_REPARSE_TAG_MOUNT_POINT,
            )
            if read_payload(destination_handle, label="published junction") != source_payload:
                raise UnsafePathError("published junction payload changed after verification")
            if read_payload(source_handle, label="source junction") != source_payload:
                raise UnsafePathError("source junction changed after atomic publish")
    except BaseException as copy_exc:
        if destination_created and not destination_published and not publish_state_unknown:
            try:
                if not destination_handle:
                    raise AtomicStateUnknownError(
                        "created junction destination handle is unavailable"
                    )
                disposition = _WindowsFileDispositionInfo(delete_file=1)
                if not set_information(
                    destination_handle,
                    _WINDOWS_FILE_DISPOSITION_INFO,
                    ctypes.byref(disposition),
                    ctypes.sizeof(disposition),
                ):
                    error_number = getattr(ctypes, "get_last_error")()
                    raise UnsafePathError(
                        f"cannot remove failed junction destination (Windows error {error_number})"
                    )
                if not close_handle(destination_handle):
                    raise UnsafePathError(
                        "cannot close failed junction destination after marking it for deletion"
                    )
                destination_handle = 0
            except BaseException as cleanup_exc:
                raise AtomicStateUnknownError(
                    "junction copy failed and its exact destination handle could not be removed"
                ) from cleanup_exc
        if destination_published or publish_state_unknown:
            if isinstance(copy_exc, AtomicStateUnknownError):
                raise
            raise AtomicStateUnknownError(
                "junction publish completed or became ambiguous before final verification"
            ) from copy_exc
        raise
    finally:
        if destination_handle:
            close_handle(destination_handle)
        if destination_parent_handle:
            close_handle(destination_parent_handle)
        if source_handle:
            close_handle(source_handle)


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
                raise NoReplaceUnavailableError("Windows handle attribute query is unavailable")
            raise UnsafePathError(f"cannot verify {label} handle (Windows error {error_number})")
        if attributes.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT and reparse_tag_redirects(
            int(attributes.reparse_tag)
        ):
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
            raise UnsafePathError(f"cannot verify {label} identity (Windows error {error_number})")
        actual_inode = int.from_bytes(bytes(file_id.file_id.identifier), "little")
        if int(file_id.volume_serial_number) != expected.device or actual_inode != expected.inode:
            raise UnsafePathError(f"{label} identity changed before native move")

    source_expected = source_identity or path_identity(source)
    source_parent_expected = source_parent_identity or path_identity(source.parent)
    destination_parent_expected = destination_parent_identity or path_identity(destination.parent)
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
                    _mutation_guard() if _mutation_guard is not None else contextlib.nullcontext()
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


def _native_io_path(path: str | Path) -> str:
    """Return an OS-call spelling without changing persisted logical paths."""

    if os.name == "nt":
        return _windows_extended_path(path)
    return os.fspath(path)


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
        os.lstat(_native_io_path(destination_path))
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
