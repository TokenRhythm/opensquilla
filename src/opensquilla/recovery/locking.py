"""Cross-process profile operation locks for Desktop-owned writers."""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import os
import stat
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from opensquilla.recovery.atomic import _chmod_open_file, _windows_extended_path
from opensquilla.recovery.errors import (
    AtomicStateUnknownError,
    LegacyGatewayRunningError,
    ProfileLockBusyError,
    UnsafePathError,
)

_LOCKS_GUARD = threading.RLock()
_PROCESS_LOCKS: dict[str, _HeldLock] = {}
_PROCESS_LEGACY_LOCKS: dict[str, _HeldLegacyLock] = {}
_PROCESS_LOCKS_PID = os.getpid()

_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_GENERIC_WRITE = 0x40000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_FILE_SHARE_DELETE = 0x00000004
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_OPEN_ALWAYS = 4
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_ERROR_FILE_NOT_FOUND = 2
_WINDOWS_ERROR_PATH_NOT_FOUND = 3
_WINDOWS_FILE_LIST_DIRECTORY = 0x00000001
_WINDOWS_FILE_ADD_FILE = 0x00000002
_WINDOWS_FILE_ADD_SUBDIRECTORY = 0x00000004
_WINDOWS_FILE_TRAVERSE = 0x00000020
_WINDOWS_FILE_READ_ATTRIBUTES = 0x00000080
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_OPEN_IF = 3
_WINDOWS_FILE_DIRECTORY_FILE = 0x00000001
_WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_WINDOWS_FILE_NON_DIRECTORY_FILE = 0x00000040
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_ATTRIBUTE_TAG_INFO = 9
_WINDOWS_FILE_ID_INFO = 18
_WINDOWS_OBJ_CASE_INSENSITIVE = 0x00000040


class _WindowsUnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint16),
        ("maximum_length", ctypes.c_uint16),
        ("buffer", ctypes.c_wchar_p),
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


class _WindowsIoStatusBlock(ctypes.Structure):
    _fields_ = [
        ("status_or_pointer", ctypes.c_void_p),
        ("information", ctypes.c_size_t),
    ]


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


@dataclass
class _HeldLock:
    fd: int
    count: int
    owner_thread: int
    path: Path


@dataclass
class _HeldLegacyLock:
    fd: int | None
    compat_count: int
    compat_owner_thread: int | None
    gateway_owner_thread: int | None
    path: Path
    state_identity: tuple[int, int, int]


@dataclass(frozen=True)
class LegacyGatewayLockFileSnapshot:
    """Metadata-only identity plus an ephemeral digest for one held lock leaf."""

    path: Path
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    digest: str


@dataclass(frozen=True)
class _LegacyLockMove:
    held: _HeldLegacyLock
    source_state: Path
    destination_state: Path
    lock_identity: tuple[int, int]
    lock_digest: bytes


@dataclass(frozen=True)
class GatewayLegacyLease:
    """One gateway's claim on the persistent pre-RC4 lock inode.

    A guarded RC4 process may already hold a compatibility lease before the
    gateway bootstrap reaches :class:`GatewayPidLock`.  This token lets that
    gateway adopt the same process-owned descriptor without briefly dropping
    the old-version exclusion.  A second gateway claim is still rejected.
    """

    key: str
    owner_thread: int


def _normalized_path(path: str | Path) -> str:
    candidate = Path(path).expanduser()
    try:
        normalized = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        normalized = candidate.absolute()
    return os.path.normcase(os.path.normpath(str(normalized)))


def profile_lock_key(home: str | Path) -> str:
    """Return the stable lock ordering/hash key for a normalized profile home."""
    return hashlib.sha256(_normalized_path(home).encode("utf-8", "surrogatepass")).hexdigest()


def user_state_dir() -> Path:
    """Return the OS user-state root without consulting profile dotenv files."""
    test_override = os.environ.get("OPENSQUILLA_USER_STATE_DIR", "").strip()
    test_gate = os.environ.get("OPENSQUILLA_TEST_PROFILE_LOCK_ROOT", "").strip() == "1"
    if test_override and test_gate:
        return Path(test_override).expanduser()
    if os.name == "nt" or sys.platform == "win32":
        value = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(value).expanduser() if value else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    value = os.environ.get("XDG_STATE_HOME", "").strip()
    return Path(value).expanduser() if value else Path.home() / ".local" / "state"


def profile_lock_path(home: str | Path) -> Path:
    return user_state_dir() / "OpenSquilla" / "profile-locks" / f"{profile_lock_key(home)}.lock"


def _refresh_after_fork() -> None:
    global _PROCESS_LOCKS_PID
    current_pid = os.getpid()
    if current_pid == _PROCESS_LOCKS_PID:
        return
    # Inherited descriptors refer to the parent's open-file descriptions. A
    # child must close its copies and acquire independently instead of treating
    # the parent's in-memory reentrancy record as its own.
    for held in _PROCESS_LOCKS.values():
        with contextlib.suppress(OSError):
            os.close(held.fd)
    _PROCESS_LOCKS.clear()
    closed_legacy: set[int] = set()
    for legacy_held in _PROCESS_LEGACY_LOCKS.values():
        if id(legacy_held) in closed_legacy:
            continue
        closed_legacy.add(id(legacy_held))
        if legacy_held.fd is not None:
            with contextlib.suppress(OSError):
                os.close(legacy_held.fd)
    _PROCESS_LEGACY_LOCKS.clear()
    _PROCESS_LOCKS_PID = current_pid


def _assert_real_lock_directory(path: Path) -> tuple[int, int]:
    try:
        value = path.lstat()
    except OSError as exc:
        raise UnsafePathError(f"profile lock directory is unavailable: {path}") from exc
    attributes = int(getattr(value, "st_file_attributes", 0))
    if stat.S_ISLNK(value.st_mode) or attributes & 0x400 or not stat.S_ISDIR(value.st_mode):
        raise UnsafePathError(f"profile lock directory must not be a link: {path}")
    return int(value.st_dev), int(value.st_ino)


def _prepare_posix_lock_file(path: Path, root: Path) -> int:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_identity = _assert_real_lock_directory(root)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise UnsafePathError(f"cannot bind profile lock root: {root}") from exc
    root_fd = directory_fd
    opened: list[tuple[Path, int, tuple[int, int]]] = []
    try:
        root_open = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(root_open.st_mode)
            or (int(root_open.st_dev), int(root_open.st_ino)) != root_identity
        ):
            raise UnsafePathError(f"profile lock root changed while opening: {root}")
        current_path = root
        for name in ("OpenSquilla", "profile-locks"):
            try:
                os.mkdir(name, 0o700, dir_fd=directory_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise UnsafePathError(
                    f"cannot create profile lock directory safely: {current_path / name}"
                ) from exc
            try:
                child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
            except OSError as exc:
                raise UnsafePathError(
                    f"cannot bind profile lock directory safely: {current_path / name}"
                ) from exc
            child_stat = os.fstat(child_fd)
            if not stat.S_ISDIR(child_stat.st_mode):
                os.close(child_fd)
                raise UnsafePathError(
                    f"profile lock directory must be real: {current_path / name}"
                )
            current_path /= name
            child_identity = (int(child_stat.st_dev), int(child_stat.st_ino))
            opened.append((current_path, child_fd, child_identity))
            directory_fd = child_fd
            with contextlib.suppress(OSError):
                _chmod_open_file(directory_fd, 0o700)

        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            raise UnsafePathError(
                f"cannot open profile lock without following links: {path}"
            ) from exc
        try:
            value = os.fstat(fd)
            lock_path_stat = os.stat(
                path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(value.st_mode)
                or value.st_nlink != 1
                or stat.S_ISLNK(lock_path_stat.st_mode)
                or not stat.S_ISREG(lock_path_stat.st_mode)
                or lock_path_stat.st_nlink != 1
                or (int(lock_path_stat.st_dev), int(lock_path_stat.st_ino))
                != (int(value.st_dev), int(value.st_ino))
            ):
                raise UnsafePathError(
                    f"profile lock is not a regular single-link file: {path}"
                )
            with contextlib.suppress(OSError):
                _chmod_open_file(fd, 0o600)
            os.lseek(fd, 0, os.SEEK_SET)
        except BaseException:
            os.close(fd)
            raise
        # A pathname swap after a dirfd was opened must be detected before the
        # lock can become authoritative for a different directory object.
        for opened_path, _opened_fd, expected in opened:
            if _assert_real_lock_directory(opened_path) != expected:
                os.close(fd)
                raise UnsafePathError(
                    f"profile lock directory identity changed: {opened_path}"
                )
        return fd
    finally:
        closed: set[int] = set()
        for _opened_path, opened_fd, _identity in reversed(opened):
            if opened_fd not in closed:
                os.close(opened_fd)
                closed.add(opened_fd)
        if root_fd not in closed:
            os.close(root_fd)


def _windows_nt_create_relative(
    nt_create_file: Callable[..., int],
    *,
    parent_handle: int,
    name: str,
    desired_access: int,
    file_attributes: int,
    share_access: int,
    create_disposition: int,
    create_options: int,
) -> int:
    """Create/open one leaf relative to an already-bound Windows directory handle."""

    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise UnsafePathError("Windows profile lock component name is invalid")
    name_buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    unicode_name = _WindowsUnicodeString(
        length=encoded_length,
        maximum_length=encoded_length + 2,
        buffer=ctypes.cast(name_buffer, ctypes.c_wchar_p),
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
    status = nt_create_file(
        ctypes.byref(handle),
        desired_access,
        ctypes.byref(object_attributes),
        ctypes.byref(io_status),
        None,
        file_attributes,
        share_access,
        create_disposition,
        create_options,
        None,
        0,
    )
    status_value = int(status)
    handle_value = getattr(handle, "value", None)
    invalid_handle = ctypes.c_void_p(-1).value
    if (
        status_value < 0
        or status_value & 0x80000000
        or handle_value in {None, 0, invalid_handle}
    ):
        raise UnsafePathError(
            "cannot create profile lock component safely "
            f"(NTSTATUS 0x{status_value & 0xFFFFFFFF:08x}): {name}"
        )
    if not isinstance(handle_value, int):
        raise UnsafePathError("Windows returned an invalid profile lock component handle")
    return handle_value


def _windows_assert_lock_handle(
    get_information: Callable[..., int],
    handle: int,
    *,
    label: str,
    require_directory: bool,
    expected_identity: tuple[int, int],
) -> None:
    attributes = _WindowsFileAttributeTagInfo()
    if not get_information(
        handle,
        _WINDOWS_FILE_ATTRIBUTE_TAG_INFO,
        ctypes.byref(attributes),
        ctypes.sizeof(attributes),
    ):
        error_number = getattr(ctypes, "get_last_error")()
        raise UnsafePathError(
            f"cannot inspect {label} handle (Windows error {error_number})"
        )
    if attributes.file_attributes & 0x400:
        raise UnsafePathError(f"{label} must not be a reparse point")
    is_directory = bool(attributes.file_attributes & _WINDOWS_FILE_ATTRIBUTE_DIRECTORY)
    if require_directory != is_directory:
        expected_kind = "directory" if require_directory else "regular file"
        raise UnsafePathError(f"{label} must be a {expected_kind}")

    file_id = _WindowsFileIdInfo()
    if not get_information(
        handle,
        _WINDOWS_FILE_ID_INFO,
        ctypes.byref(file_id),
        ctypes.sizeof(file_id),
    ):
        error_number = getattr(ctypes, "get_last_error")()
        raise UnsafePathError(
            f"cannot inspect {label} identity (Windows error {error_number})"
        )
    actual = (
        int(file_id.volume_serial_number),
        int.from_bytes(bytes(file_id.file_id.identifier), "little"),
    )
    if actual != expected_identity:
        raise UnsafePathError(f"{label} identity changed while opening")


def _windows_open_profile_lock_file(path: Path, root: Path) -> int:
    """Bind the complete app-owned lock path without path-based child traversal."""

    import msvcrt

    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise UnsafePathError(f"cannot create profile lock root safely: {root}") from exc
    root_identity = _assert_real_lock_directory(root)
    expected_parent = root / "OpenSquilla" / "profile-locks"
    if os.path.normcase(os.path.normpath(str(path.parent))) != os.path.normcase(
        os.path.normpath(str(expected_parent))
    ):
        raise UnsafePathError("profile lock path escaped the bound Windows state root")

    win_dll = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    ntdll = win_dll("ntdll", use_last_error=True)
    try:
        create_file = kernel32.CreateFileW
        get_information = kernel32.GetFileInformationByHandleEx
        close_handle = kernel32.CloseHandle
        nt_create_file = ntdll.NtCreateFile
    except AttributeError as exc:
        raise UnsafePathError("handle-relative Windows lock APIs are unavailable") from exc
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
    nt_create_file.restype = ctypes.c_long

    directory_access = (
        _WINDOWS_FILE_LIST_DIRECTORY
        | _WINDOWS_FILE_ADD_FILE
        | _WINDOWS_FILE_ADD_SUBDIRECTORY
        | _WINDOWS_FILE_TRAVERSE
        | _WINDOWS_FILE_READ_ATTRIBUTES
        | _WINDOWS_SYNCHRONIZE
    )
    # Omitting FILE_SHARE_DELETE pins each app-owned component while its child
    # is opened. The final lock handle keeps this exclusion for the lock's life.
    share_access = _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE
    directory_options = (
        _WINDOWS_FILE_DIRECTORY_FILE
        | _WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT
        | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
    )
    file_options = (
        _WINDOWS_FILE_NON_DIRECTORY_FILE
        | _WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT
        | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT
    )
    invalid_handle = ctypes.c_void_p(-1).value
    root_handle_raw = create_file(
        _windows_extended_path(root),
        directory_access,
        share_access,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    root_handle = getattr(root_handle_raw, "value", root_handle_raw)
    if not isinstance(root_handle, int) or root_handle in {0, invalid_handle}:
        error_number = getattr(ctypes, "get_last_error")()
        raise UnsafePathError(
            f"cannot bind Windows profile lock root (Windows error {error_number}): {root}"
        )

    directory_handles = [root_handle]
    leaf_handle: int | None = None
    fd: int | None = None
    try:
        _windows_assert_lock_handle(
            get_information,
            root_handle,
            label="profile lock root",
            require_directory=True,
            expected_identity=root_identity,
        )
        parent_handle = root_handle
        current_path = root
        for name in ("OpenSquilla", "profile-locks"):
            current_path /= name
            child_handle = _windows_nt_create_relative(
                nt_create_file,
                parent_handle=parent_handle,
                name=name,
                desired_access=directory_access,
                file_attributes=_WINDOWS_FILE_ATTRIBUTE_DIRECTORY,
                share_access=share_access,
                create_disposition=_WINDOWS_FILE_OPEN_IF,
                create_options=directory_options,
            )
            directory_handles.append(child_handle)
            child_identity = _assert_real_lock_directory(current_path)
            _windows_assert_lock_handle(
                get_information,
                child_handle,
                label=f"profile lock directory {name}",
                require_directory=True,
                expected_identity=child_identity,
            )
            parent_handle = child_handle

        leaf_handle = _windows_nt_create_relative(
            nt_create_file,
            parent_handle=parent_handle,
            name=path.name,
            desired_access=_WINDOWS_GENERIC_READ | _WINDOWS_GENERIC_WRITE | _WINDOWS_SYNCHRONIZE,
            file_attributes=_WINDOWS_FILE_ATTRIBUTE_NORMAL,
            share_access=share_access,
            create_disposition=_WINDOWS_FILE_OPEN_IF,
            create_options=file_options,
        )
        try:
            path_value = path.lstat()
        except OSError as exc:
            raise UnsafePathError(f"profile lock path disappeared while opening: {path}") from exc
        attributes = int(getattr(path_value, "st_file_attributes", 0))
        if (
            stat.S_ISLNK(path_value.st_mode)
            or attributes & 0x400
            or not stat.S_ISREG(path_value.st_mode)
            or path_value.st_nlink != 1
        ):
            raise UnsafePathError(f"profile lock is not a regular single-link file: {path}")
        _windows_assert_lock_handle(
            get_information,
            leaf_handle,
            label="profile lock file",
            require_directory=False,
            expected_identity=(int(path_value.st_dev), int(path_value.st_ino)),
        )
        try:
            fd = int(
                getattr(msvcrt, "open_osfhandle")(
                    leaf_handle,
                    os.O_RDWR | getattr(os, "O_BINARY", 0),
                )
            )
        except BaseException:
            close_handle(leaf_handle)
            leaf_handle = None
            raise
        leaf_handle = None  # The CRT descriptor now owns the native handle.
        value = os.fstat(fd)
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise UnsafePathError(f"profile lock is not a regular single-link file: {path}")
        with contextlib.suppress(OSError):
            _chmod_open_file(fd, 0o600)
        if value.st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        result = fd
        fd = None
        return result
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        if leaf_handle is not None:
            close_handle(leaf_handle)
        for handle in reversed(directory_handles):
            close_handle(handle)


def _prepare_windows_lock_file(path: Path, root: Path) -> int:
    return _windows_open_profile_lock_file(path, root)


def _prepare_lock_file(path: Path) -> int:
    root = user_state_dir().expanduser().absolute()
    expected_parent = root / "OpenSquilla" / "profile-locks"
    if os.path.normcase(os.path.normpath(str(path.parent))) != os.path.normcase(
        os.path.normpath(str(expected_parent))
    ):
        raise UnsafePathError("profile lock path escaped the OS user-state root")
    if os.name == "nt":
        return _prepare_windows_lock_file(path, root)
    return _prepare_posix_lock_file(path, root)


def _state_directory_identity(path: Path) -> tuple[int, int, int]:
    try:
        value = path.lstat()
    except OSError as exc:
        raise UnsafePathError(f"cannot inspect legacy state directory safely: {path}") from exc
    attributes = int(getattr(value, "st_file_attributes", 0))
    if stat.S_ISLNK(value.st_mode) or attributes & 0x400 or not stat.S_ISDIR(value.st_mode):
        raise UnsafePathError(f"legacy state directory must not be a link: {path}")
    return int(value.st_dev), int(value.st_ino), attributes


def _lockable_state_path(path: Path, *, allow_state_symlink: bool) -> Path:
    """Return the real state directory used solely for runtime lock placement."""

    try:
        value = path.lstat()
    except OSError as exc:
        raise UnsafePathError(f"cannot inspect legacy state directory safely: {path}") from exc
    attributes = int(getattr(value, "st_file_attributes", 0))
    if not (stat.S_ISLNK(value.st_mode) or attributes & 0x400):
        return path
    if not allow_state_symlink:
        raise UnsafePathError(f"legacy state directory must not be a link: {path}")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise UnsafePathError(f"configured state link cannot be resolved safely: {path}") from exc
    # The final resolved leaf must itself be a real directory. Intermediate
    # links are part of the user's explicit runtime path and are not traversed
    # for any migration/copy operation here.
    _state_directory_identity(resolved)
    return resolved


def _windows_open_legacy_lock_file(
    path: Path,
    *,
    create_if_missing: bool,
) -> int | None:
    """Open the byte lock with delete sharing for verified Windows handoff."""

    import msvcrt

    win_dll = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
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
    handle_raw = create_file(
        _windows_extended_path(path),
        _WINDOWS_GENERIC_READ | _WINDOWS_GENERIC_WRITE,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE | _WINDOWS_FILE_SHARE_DELETE,
        None,
        _WINDOWS_OPEN_ALWAYS if create_if_missing else _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_ATTRIBUTE_NORMAL | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    handle_value = getattr(handle_raw, "value", handle_raw)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle_value in {None, 0, invalid_handle}:
        error_number = getattr(ctypes, "get_last_error")()
        if not create_if_missing and error_number in {
            _WINDOWS_ERROR_FILE_NOT_FOUND,
            _WINDOWS_ERROR_PATH_NOT_FOUND,
        }:
            return None
        raise UnsafePathError(
            f"cannot open legacy gateway lock safely (Windows error {error_number}): {path}"
        )
    if not isinstance(handle_value, int):
        raise UnsafePathError(f"Windows returned an invalid legacy lock handle: {path}")
    try:
        return int(
            getattr(msvcrt, "open_osfhandle")(
                handle_value,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
        )
    except BaseException:
        kernel32.CloseHandle(handle_value)
        raise


def _prepare_legacy_lock_file(state_path: Path, *, create_if_missing: bool) -> int | None:
    """Open/create the stable legacy lock without ever creating ``state_path``."""

    parent_identity = _state_directory_identity(state_path)
    path = state_path / "gateway.pid.lock"
    if os.name == "nt":
        fd = _windows_open_legacy_lock_file(path, create_if_missing=create_if_missing)
        if fd is None:
            return None
    else:
        flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        if create_if_missing:
            flags |= os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except FileNotFoundError:
            if not create_if_missing:
                return None
            raise
        except OSError as exc:
            raise UnsafePathError(
                f"cannot open legacy gateway lock without following links: {path}"
            ) from exc
    try:
        value = os.fstat(fd)
        attributes = int(getattr(value, "st_file_attributes", 0))
        if attributes & 0x400 or not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise UnsafePathError(f"legacy gateway lock is not a regular file: {path}")
        current_path = path.lstat()
        current_attributes = int(getattr(current_path, "st_file_attributes", 0))
        if (
            stat.S_ISLNK(current_path.st_mode)
            or current_attributes & 0x400
            or not stat.S_ISREG(current_path.st_mode)
            or current_path.st_nlink != 1
            or (int(current_path.st_dev), int(current_path.st_ino))
            != (int(value.st_dev), int(value.st_ino))
        ):
            raise UnsafePathError(f"legacy gateway lock changed while it was opened: {path}")
        if _state_directory_identity(state_path) != parent_identity:
            raise UnsafePathError(
                f"legacy state directory changed while its lock was opened: {state_path}"
            )
        if create_if_missing:
            with contextlib.suppress(OSError):
                _chmod_open_file(fd, 0o600)
            if os.name == "nt" and value.st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _remove_legacy_lock_aliases(held: _HeldLegacyLock) -> None:
    for alias, candidate in tuple(_PROCESS_LEGACY_LOCKS.items()):
        if candidate is held:
            _PROCESS_LEGACY_LOCKS.pop(alias, None)


def rebind_legacy_gateway_lock(
    source_state_root: str | Path,
    destination_state_root: str | Path,
) -> None:
    """Bind a held lock to its verified path after an atomic directory move.

    The original descriptor remains locked throughout.  This only updates the
    process-local path index after proving that both the moved state directory
    and its lock leaf retain the identities captured at acquisition.
    """

    source_state = Path(source_state_root).expanduser().absolute()
    destination_state = Path(destination_state_root).expanduser().absolute()
    source_path = source_state / "gateway.pid.lock"
    destination_path = destination_state / "gateway.pid.lock"
    source_key = _normalized_path(source_path)
    destination_key = _normalized_path(destination_path)
    if source_key == destination_key:
        raise UnsafePathError("legacy gateway lock handoff paths are identical")
    if os.path.lexists(source_state):
        raise UnsafePathError("legacy gateway lock source still exists after handoff")

    destination_state_identity = _state_directory_identity(destination_state)
    try:
        path_value = destination_path.lstat()
    except OSError as exc:
        raise UnsafePathError("moved legacy gateway lock is missing at destination") from exc
    path_attributes = int(getattr(path_value, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(path_value.st_mode)
        or path_attributes & 0x400
        or not stat.S_ISREG(path_value.st_mode)
        or path_value.st_nlink != 1
    ):
        raise UnsafePathError("moved legacy gateway lock destination is unsafe")

    owner_thread = threading.get_ident()
    with _LOCKS_GUARD:
        _refresh_after_fork()
        held = _PROCESS_LEGACY_LOCKS.get(source_key)
        if (
            held is None
            or held.fd is None
            or held.compat_count <= 0
            or held.compat_owner_thread != owner_thread
            or held.gateway_owner_thread is not None
        ):
            raise UnsafePathError("legacy gateway lock handoff has no owned source lease")
        if destination_state_identity != held.state_identity:
            raise UnsafePathError("moved legacy gateway lock directory identity changed")
        try:
            held_value = os.fstat(held.fd)
        except OSError as exc:
            raise UnsafePathError("cannot verify moved legacy gateway lock handle") from exc
        held_attributes = int(getattr(held_value, "st_file_attributes", 0))
        if (
            held_attributes & 0x400
            or not stat.S_ISREG(held_value.st_mode)
            or held_value.st_nlink != 1
            or (int(held_value.st_dev), int(held_value.st_ino))
            != (int(path_value.st_dev), int(path_value.st_ino))
        ):
            raise UnsafePathError("moved legacy gateway lock file identity changed")

        displaced = _PROCESS_LEGACY_LOCKS.get(destination_key)
        if displaced is not None and displaced is not held:
            if (
                displaced.fd is None
                or displaced.compat_owner_thread != owner_thread
                or displaced.gateway_owner_thread is not None
            ):
                raise UnsafePathError("destination legacy gateway lock is owned elsewhere")
            try:
                displaced_value = os.fstat(displaced.fd)
            except OSError as exc:
                raise UnsafePathError(
                    "cannot verify displaced destination legacy gateway lock"
                ) from exc
            if (int(displaced_value.st_dev), int(displaced_value.st_ino)) == (
                int(path_value.st_dev),
                int(path_value.st_ino),
            ):
                raise UnsafePathError("destination legacy gateway lock handoff is ambiguous")
            # The prior target lease follows target -> backup. Its owning
            # LegacyGatewayLock keeps a direct claim, so replacing only this
            # stale path alias cannot release or lose that descriptor. Keep an
            # unaddressable registry entry as well so fork refresh can close the
            # inherited descriptor even though its new backup path is unknown.
            # Register the displaced descriptor first. Replacing an existing
            # destination-key value is then one atomic dict-slot update; if the
            # registry expansion fails, the original destination mapping is
            # still intact and the harmless detached alias can be removed.
            detached_key = f"\0moved:{id(displaced)}"
            _PROCESS_LEGACY_LOCKS[detached_key] = displaced
            try:
                _PROCESS_LEGACY_LOCKS[destination_key] = held
            except BaseException:
                if _PROCESS_LEGACY_LOCKS.get(detached_key) is displaced:
                    _PROCESS_LEGACY_LOCKS.pop(detached_key, None)
                raise
            held.path = destination_path
            held.state_identity = destination_state_identity
            return
        _PROCESS_LEGACY_LOCKS[destination_key] = held
        held.path = destination_path
        held.state_identity = destination_state_identity


def _profile_relative_path(path: Path, root: Path) -> Path | None:
    path_absolute = os.path.normpath(str(path.absolute()))
    root_absolute = os.path.normpath(str(root.absolute()))
    path_value = os.path.normcase(path_absolute)
    root_value = os.path.normcase(root_absolute)
    try:
        if os.path.commonpath((path_value, root_value)) != root_value:
            return None
    except ValueError:
        return None
    relative = os.path.relpath(path_absolute, root_absolute)
    if relative in {"", os.curdir} or relative == os.pardir:
        return None
    if relative.startswith(os.pardir + os.sep):
        return None
    return Path(relative)


def _legacy_lock_file_identity(path: Path) -> tuple[int, int]:
    try:
        value = path.lstat()
    except OSError as exc:
        raise UnsafePathError(f"legacy gateway lock is unavailable: {path}") from exc
    attributes = int(getattr(value, "st_file_attributes", 0))
    if (
        stat.S_ISLNK(value.st_mode)
        or attributes & 0x400
        or not stat.S_ISREG(value.st_mode)
        or value.st_nlink != 1
    ):
        raise UnsafePathError(f"legacy gateway lock is not a regular file: {path}")
    return int(value.st_dev), int(value.st_ino)


def _legacy_lock_digest(fd: int) -> bytes:
    """Hash the small authority file in memory so mtime tolerance cannot hide writes."""

    limit = 64 * 1024
    position = os.lseek(fd, 0, os.SEEK_CUR)
    digest = hashlib.sha256()
    total = 0
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            chunk = os.read(fd, min(8192, limit + 1 - total))
            if not chunk:
                return digest.digest()
            total += len(chunk)
            if total > limit:
                raise UnsafePathError("legacy gateway lock authority is unexpectedly large")
            digest.update(chunk)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def _held_legacy_lock_moves(source: Path, destination: Path) -> tuple[_LegacyLockMove, ...]:
    owner_thread = threading.get_ident()
    moves: list[_LegacyLockMove] = []
    seen: set[int] = set()
    for held in _PROCESS_LEGACY_LOCKS.values():
        if id(held) in seen:
            continue
        seen.add(id(held))
        relative = _profile_relative_path(held.path.parent, source)
        if relative is None:
            continue
        if (
            held.fd is None
            or held.compat_count <= 0
            or held.compat_owner_thread != owner_thread
            or held.gateway_owner_thread is not None
        ):
            raise LegacyGatewayRunningError(
                f"profile contains a legacy gateway lock not owned by this operation: {source}"
            )
        source_state = held.path.parent
        if _state_directory_identity(source_state) != held.state_identity:
            raise UnsafePathError("legacy state directory changed before profile move")
        held_value = os.fstat(held.fd)
        held_identity = (int(held_value.st_dev), int(held_value.st_ino))
        if _legacy_lock_file_identity(held.path) != held_identity:
            raise UnsafePathError("legacy gateway lock changed before profile move")
        moves.append(
            _LegacyLockMove(
                held=held,
                source_state=source_state,
                destination_state=destination / relative,
                lock_identity=held_identity,
                lock_digest=_legacy_lock_digest(held.fd),
            )
        )
    return tuple(sorted(moves, key=lambda item: _normalized_path(item.source_state)))


def _legacy_lock_move_location_matches(
    moves: tuple[_LegacyLockMove, ...],
    *,
    destination: bool,
) -> bool:
    for item in moves:
        state = item.destination_state if destination else item.source_state
        try:
            if _state_directory_identity(state) != item.held.state_identity:
                return False
            if _legacy_lock_file_identity(state / "gateway.pid.lock") != item.lock_identity:
                return False
        except (OSError, UnsafePathError):
            return False
    return True


def _reacquire_suspended_legacy_locks(
    moves: tuple[_LegacyLockMove, ...],
    *,
    destination: bool,
) -> None:
    for item in moves:
        held = item.held
        if held.fd is not None:
            continue
        state = item.destination_state if destination else item.source_state
        if _state_directory_identity(state) != held.state_identity:
            raise UnsafePathError("legacy state directory identity changed during lock handoff")
        fd = _prepare_legacy_lock_file(state, create_if_missing=False)
        if fd is None:
            raise UnsafePathError("legacy gateway lock disappeared during profile move")
        try:
            if not _try_lock(fd):
                raise LegacyGatewayRunningError(
                    "legacy gateway acquired its state lock during profile move"
                )
            held_value = os.fstat(fd)
            if (int(held_value.st_dev), int(held_value.st_ino)) != item.lock_identity:
                raise UnsafePathError("legacy gateway lock identity changed during handoff")
            if _legacy_lock_digest(fd) != item.lock_digest:
                raise UnsafePathError("legacy gateway lock content changed during handoff")
        except BaseException:
            with contextlib.suppress(OSError):
                _unlock(fd)
            os.close(fd)
            raise
        _remove_legacy_lock_aliases(held)
        held.fd = fd
        held.path = state / "gateway.pid.lock"
        held.state_identity = _state_directory_identity(state)
        _PROCESS_LEGACY_LOCKS[_normalized_path(held.path)] = held


def _windows_requires_legacy_lock_handoff() -> bool:
    return os.name == "nt" or sys.platform == "win32"


@contextlib.contextmanager
def _windows_legacy_lock_mutation_guard(
    source: Path,
    destination: Path,
) -> Iterator[None]:
    """Release descendant compatibility handles for one native rename call."""

    with _LOCKS_GUARD:
        _refresh_after_fork()
        moves = _held_legacy_lock_moves(source, destination)
        if not moves:
            yield
            return
        try:
            for item in moves:
                held = item.held
                assert held.fd is not None
                fd = held.fd
                _unlock(fd)
                held.fd = None
                os.close(fd)
        except BaseException as exc:
            try:
                _reacquire_suspended_legacy_locks(moves, destination=False)
            except BaseException as reacquire_exc:
                raise AtomicStateUnknownError(
                    "legacy gateway exclusion was lost before the profile move"
                ) from reacquire_exc
            raise AtomicStateUnknownError(
                "legacy gateway lock could not be prepared for the profile move"
            ) from exc

        mutation_error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            mutation_error = exc

        source_matches = _legacy_lock_move_location_matches(moves, destination=False)
        destination_matches = _legacy_lock_move_location_matches(moves, destination=True)
        if source_matches == destination_matches:
            raise AtomicStateUnknownError(
                "profile move completed with ambiguous legacy lock locations"
            ) from mutation_error
        try:
            _reacquire_suspended_legacy_locks(
                moves,
                destination=destination_matches,
            )
        except BaseException as exc:
            raise AtomicStateUnknownError(
                "profile path changed but legacy gateway exclusion could not be reacquired"
            ) from exc
        if mutation_error is not None:
            if destination_matches:
                raise AtomicStateUnknownError(
                    "profile move changed the directory before reporting failure"
                ) from mutation_error
            raise mutation_error


def move_profile_no_replace(
    source: str | Path,
    destination: str | Path,
    *,
    move: Callable[..., None] | None = None,
) -> None:
    """Move a locked profile without dropping old-gateway exclusion silently.

    Windows refuses to rename a directory while any descendant handle remains
    open. The external profile-operation lock stays held by the caller; only
    compatibility lock handles inside the profile are briefly released and
    reacquired by verified file identity. No copy/delete or replacement
    fallback is permitted.

    Whole-profile moves preserve the profile contract without dereferencing
    links: descendants of a real ``workspace`` are verified as link leaves,
    while a real ``code-task`` directory is moved opaquely. The generic native
    move primitive remains strict unless this wrapper selects that policy.
    """

    if move is None:
        from opensquilla.recovery.atomic import native_move_no_replace

        move = native_move_no_replace
    source_path = Path(source).expanduser().absolute()
    destination_path = Path(destination).expanduser().absolute()
    if _windows_requires_legacy_lock_handoff():
        with _LOCKS_GUARD:
            _refresh_after_fork()
            allowed_mtime_paths: set[str] = set()
            for item in _held_legacy_lock_moves(source_path, destination_path):
                relative = _profile_relative_path(item.held.path, source_path)
                if relative is None:
                    continue
                allowed_mtime_paths.add(relative.as_posix())
            allowed_mtime_changes = frozenset(allowed_mtime_paths)
        move(
            source_path,
            destination_path,
            _mutation_guard=lambda: _windows_legacy_lock_mutation_guard(
                source_path,
                destination_path,
            ),
            _allowed_manifest_mtime_changes=allowed_mtime_changes,
            _use_profile_manifest_policy=True,
        )
        return
    with _LOCKS_GUARD:
        _refresh_after_fork()
        moves = _held_legacy_lock_moves(source_path, destination_path)
        move(
            source_path,
            destination_path,
            _use_profile_manifest_policy=True,
        )
        rebound: list[_LegacyLockMove] = []
        try:
            for item in moves:
                rebind_legacy_gateway_lock(item.source_state, item.destination_state)
                rebound.append(item)
        except BaseException:
            try:
                move(
                    destination_path,
                    source_path,
                    _use_profile_manifest_policy=True,
                )
                for item in reversed(rebound):
                    rebind_legacy_gateway_lock(
                        item.destination_state,
                        item.source_state,
                    )
            except BaseException as rollback_exc:
                raise AtomicStateUnknownError(
                    "profile moved but legacy lock binding could not be restored"
                ) from rollback_exc
            raise


def _try_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            locking = getattr(msvcrt, "locking")
            locking(fd, getattr(msvcrt, "LK_NBLCK"), 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        locking = getattr(msvcrt, "locking")
        locking(fd, getattr(msvcrt, "LK_UNLCK"), 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


class ProfileOperationLock:
    """Exclusive, same-process-reentrant lock for one normalized profile home."""

    def __init__(self, home: str | Path, *, timeout: float = 0.0) -> None:
        self.home = Path(home).expanduser()
        self.timeout = max(0.0, float(timeout))
        self.key = profile_lock_key(self.home)
        self.path = profile_lock_path(self.home)
        self._acquired = False
        self._owner_thread: int | None = None

    def acquire(self) -> ProfileOperationLock:
        if self._acquired:
            if self._owner_thread != threading.get_ident():
                raise ProfileLockBusyError("profile lock is owned by another thread")
            return self
        owner_thread = threading.get_ident()
        deadline = time.monotonic() + self.timeout
        while True:
            with _LOCKS_GUARD:
                _refresh_after_fork()
                held = _PROCESS_LOCKS.get(self.key)
                if held is not None:
                    if held.owner_thread == owner_thread:
                        held.count += 1
                        self._acquired = True
                        self._owner_thread = owner_thread
                        return self
                    if time.monotonic() >= deadline:
                        raise ProfileLockBusyError(
                            f"profile is in use by another writer: {self.home}"
                        )
            if held is not None:
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                continue

            fd = _prepare_lock_file(self.path)
            try:
                if not _try_lock(fd):
                    if time.monotonic() >= deadline:
                        raise ProfileLockBusyError(
                            f"profile is in use by another writer: {self.home}"
                        )
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                    continue
                with _LOCKS_GUARD:
                    _refresh_after_fork()
                    held = _PROCESS_LOCKS.get(self.key)
                    if held is None:
                        _PROCESS_LOCKS[self.key] = _HeldLock(
                            fd=fd,
                            count=1,
                            owner_thread=owner_thread,
                            path=self.path,
                        )
                        fd = -1
                        self._acquired = True
                        self._owner_thread = owner_thread
                        return self
                # Some platforms treat process-local locks as reentrant. The
                # in-memory owner is still authoritative between threads.
                _unlock(fd)
            finally:
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            if time.monotonic() >= deadline:
                raise ProfileLockBusyError(f"profile is in use by another writer: {self.home}")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        if not self._acquired:
            return
        with _LOCKS_GUARD:
            _refresh_after_fork()
            held = _PROCESS_LOCKS.get(self.key)
            if held is None:
                self._acquired = False
                self._owner_thread = None
                return
            if self._owner_thread != held.owner_thread:
                raise RuntimeError("profile lock must be released by its owning thread")
            held.count -= 1
            if held.count == 0:
                _PROCESS_LOCKS.pop(self.key, None)
                try:
                    _unlock(held.fd)
                finally:
                    os.close(held.fd)
            self._acquired = False
            self._owner_thread = None

    def __enter__(self) -> ProfileOperationLock:
        return self.acquire()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()


class LegacyGatewayLock:
    """Probe/hold the pre-RC4 gateway PID lock during offline mutations.

    Older gateways do not know the external profile lock. Acquiring every
    effective persistent ``gateway.pid.lock`` closes that compatibility race
    without trusting a stale PID file. Missing state directories are skipped;
    this guard must never create a new data root merely to lock it.
    """

    def __init__(
        self,
        home: str | Path,
        *,
        state_roots: Iterable[str | Path] | None = None,
        create_if_missing: bool = True,
        allow_state_symlinks: bool = False,
        timeout: float = 0.0,
    ) -> None:
        self.home = Path(home).expanduser()
        self.timeout = max(0.0, float(timeout))
        self.create_if_missing = create_if_missing
        if state_roots is None:
            specs = _effective_state_root_specs(self.home)
        else:
            specs = tuple(
                (Path(root).expanduser().absolute(), allow_state_symlinks)
                for root in state_roots
            )
        unique: dict[str, tuple[Path, bool]] = {}
        for root, explicit in specs:
            key = _normalized_path(root)
            previous = unique.get(key)
            unique[key] = (
                root,
                explicit if previous is None else previous[1] or explicit,
            )
        self.state_roots = tuple(unique[key][0] for key in sorted(unique))
        self._allow_symlink_by_key = {
            key: unique[key][1] for key in sorted(unique)
        }
        self.paths = tuple(root / "gateway.pid.lock" for root in self.state_roots)
        # Compatibility with callers/tests that historically inspected the one
        # canonical lock path.
        self.path = self.paths[0] if self.paths else self.home / "state" / "gateway.pid.lock"
        self._claims: list[_HeldLegacyLock] = []
        self._owner_thread: int | None = None

    def acquire(self) -> LegacyGatewayLock:
        if self._claims:
            if self._owner_thread != threading.get_ident():
                raise LegacyGatewayRunningError("legacy lock is owned by another thread")
            return self
        self._owner_thread = threading.get_ident()
        try:
            for root in self.state_roots:
                if not os.path.lexists(root):
                    continue
                self._acquire_one(root)
            return self
        except BaseException:
            self.release()
            raise

    def holds_state_root(self, state_root: str | Path) -> bool:
        """Return whether this lease owns the existing lock for ``state_root``.

        This is intentionally an observation only: callers that treat a
        profile as a read-only source can prove that an authority file was
        already present and locked without creating it as a side effect.
        """

        expected = _normalized_path(
            Path(state_root).expanduser().absolute() / "gateway.pid.lock"
        )
        owner_thread = threading.get_ident()
        with _LOCKS_GUARD:
            _refresh_after_fork()
            return any(
                claim.fd is not None
                and claim.compat_count > 0
                and claim.compat_owner_thread == owner_thread
                and _normalized_path(claim.path) == expected
                for claim in self._claims
            )

    def snapshot_state_root(
        self,
        state_root: str | Path,
    ) -> LegacyGatewayLockFileSnapshot | None:
        """Snapshot a lock through the descriptor that owns its byte-range lock.

        Windows rejects reading the locked byte through a second handle. The
        owning descriptor remains readable, so callers can verify the exact
        leaf without releasing the compatibility lock or opening a race for an
        older gateway.
        """

        expected = _normalized_path(
            Path(state_root).expanduser().absolute() / "gateway.pid.lock"
        )
        owner_thread = threading.get_ident()
        with _LOCKS_GUARD:
            _refresh_after_fork()
            claim = next(
                (
                    item
                    for item in self._claims
                    if item.fd is not None
                    and item.compat_count > 0
                    and item.compat_owner_thread == owner_thread
                    and _normalized_path(item.path) == expected
                ),
                None,
            )
            if claim is None or claim.fd is None:
                return None
            value = os.fstat(claim.fd)
            identity = (int(value.st_dev), int(value.st_ino))
            if (
                not stat.S_ISREG(value.st_mode)
                or int(value.st_nlink) != 1
                or _legacy_lock_file_identity(claim.path) != identity
            ):
                raise UnsafePathError("held legacy gateway lock identity changed")
            digest = _legacy_lock_digest(claim.fd).hex()
            current = os.fstat(claim.fd)
            if (
                int(current.st_dev),
                int(current.st_ino),
                int(current.st_mode),
                int(current.st_size),
                int(current.st_mtime_ns),
            ) != (
                identity[0],
                identity[1],
                int(value.st_mode),
                int(value.st_size),
                int(value.st_mtime_ns),
            ):
                raise UnsafePathError("held legacy gateway lock changed while read")
            return LegacyGatewayLockFileSnapshot(
                path=claim.path,
                device=identity[0],
                inode=identity[1],
                mode=int(value.st_mode),
                size=int(value.st_size),
                mtime_ns=int(value.st_mtime_ns),
                digest=digest,
            )

    def _acquire_one(self, state_path: Path) -> None:
        try:
            state_path = _lockable_state_path(
                state_path,
                allow_state_symlink=self._allow_symlink_by_key.get(
                    _normalized_path(state_path),
                    False,
                ),
            )
        except UnsafePathError:
            if not self.create_if_missing:
                return
            raise
        path = state_path / "gateway.pid.lock"
        key = _normalized_path(path)
        owner_thread = threading.get_ident()
        deadline = time.monotonic() + self.timeout
        while True:
            with _LOCKS_GUARD:
                _refresh_after_fork()
                held = _PROCESS_LEGACY_LOCKS.get(key)
                if (
                    held is not None
                    and held.fd is not None
                    and held.gateway_owner_thread is None
                    and held.compat_owner_thread == owner_thread
                ):
                    held.compat_count += 1
                    self._claims.append(held)
                    return
                busy_in_process = held is not None
            if busy_in_process:
                if time.monotonic() >= deadline:
                    raise LegacyGatewayRunningError(
                        f"another writer still holds its state lock: {state_path}"
                    )
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                continue

            try:
                fd = _prepare_legacy_lock_file(
                    state_path,
                    create_if_missing=self.create_if_missing,
                )
            except UnsafePathError:
                # Existing-only source probes never modify authority files,
                # but an authority path in a valid state directory that cannot
                # be locked is not safe to ignore. An invalid configured state
                # root is left to the importer's more specific data-root
                # preflight, which also blocks publication without mutation.
                if not self.create_if_missing:
                    try:
                        _state_directory_identity(state_path)
                    except UnsafePathError:
                        return
                raise
            if fd is None:
                return
            try:
                if not _try_lock(fd):
                    if time.monotonic() >= deadline:
                        raise LegacyGatewayRunningError(
                            f"legacy gateway still holds its state lock: {state_path}"
                        )
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                    continue
                with _LOCKS_GUARD:
                    _refresh_after_fork()
                    held = _PROCESS_LEGACY_LOCKS.get(key)
                    if held is None:
                        held = _HeldLegacyLock(
                            fd=fd,
                            compat_count=1,
                            compat_owner_thread=owner_thread,
                            gateway_owner_thread=None,
                            path=path,
                            state_identity=_state_directory_identity(state_path),
                        )
                        _PROCESS_LEGACY_LOCKS[key] = held
                        fd = -1
                        self._claims.append(held)
                        return
                    _unlock(fd)
            finally:
                if fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            if time.monotonic() >= deadline:
                raise LegacyGatewayRunningError(
                    f"another writer still holds its state lock: {state_path}"
                )
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def release(self) -> None:
        with _LOCKS_GUARD:
            _refresh_after_fork()
            for held in reversed(self._claims):
                if held.compat_owner_thread != self._owner_thread:
                    raise RuntimeError("legacy lock must be released by its owning thread")
                held.compat_count -= 1
                if held.compat_count == 0:
                    held.compat_owner_thread = None
                if held.compat_count == 0 and held.gateway_owner_thread is None:
                    _remove_legacy_lock_aliases(held)
                    if held.fd is not None:
                        try:
                            _unlock(held.fd)
                        finally:
                            os.close(held.fd)
                        held.fd = None
            self._claims.clear()
            self._owner_thread = None

    def __enter__(self) -> LegacyGatewayLock:
        return self.acquire()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()


def _effective_state_root_specs(
    home: str | Path,
    *,
    include_process_environment: bool = True,
) -> tuple[tuple[Path, bool], ...]:
    """Return candidate roots with whether a user explicitly selected each.

    Explicitly configured links may be followed solely to place an exclusion
    lock. The implicit canonical root is never granted that exception.
    """

    home_path = Path(home).expanduser().absolute()
    candidates: list[tuple[Path, bool]] = [(home_path / "state", False)]
    config_path = home_path / "config.toml"
    if os.path.lexists(config_path):
        from opensquilla.recovery.config_patch import ConfigSnapshot, state_override

        snapshot = ConfigSnapshot.capture(config_path)
        try:
            payload = tomllib.loads(snapshot.data.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            # The caller's recovery/import preflight owns the diagnostic and
            # blocks mutation. Keep the canonical old-version root covered in
            # the meantime without turning a config error into a lock error.
            payload = {}
        raw_state = payload.get("state_dir")
        if raw_state is not None:
            if not isinstance(raw_state, str) or not raw_state.strip():
                raise UnsafePathError("top-level state_dir must be a non-empty path string")
            configured = Path(raw_state).expanduser()
            if not configured.is_absolute():
                configured = home_path / configured
            candidates.append((configured.absolute(), True))
        override = state_override(
            home_path,
            include_legacy_dotenv=True,
            include_process_environment=include_process_environment,
        )
    else:
        from opensquilla.recovery.config_patch import state_override

        override = state_override(
            home_path,
            include_legacy_dotenv=True,
            include_process_environment=include_process_environment,
        )
    if override is not None:
        overridden = Path(override[1]).expanduser()
        if not overridden.is_absolute():
            overridden = home_path / overridden
        candidates.append((overridden.absolute(), True))
    unique: dict[str, tuple[Path, bool]] = {}
    for path, explicit in candidates:
        key = _normalized_path(path)
        previous = unique.get(key)
        unique[key] = (path, explicit if previous is None else previous[1] or explicit)
    return tuple(unique[key] for key in sorted(unique))


def effective_state_roots(
    home: str | Path,
    *,
    include_process_environment: bool = True,
) -> tuple[Path, ...]:
    """Return every state root that an old or current profile may write."""

    return tuple(
        path
        for path, _explicit in _effective_state_root_specs(
            home,
            include_process_environment=include_process_environment,
        )
    )


def acquire_gateway_legacy_lease(state_root: str | Path) -> GatewayLegacyLease | None:
    """Acquire/adopt one gateway lock, returning ``None`` when it is busy."""

    state_path = _lockable_state_path(
        Path(state_root).expanduser().absolute(),
        allow_state_symlink=True,
    )
    path = state_path / "gateway.pid.lock"
    key = _normalized_path(path)
    owner_thread = threading.get_ident()
    with _LOCKS_GUARD:
        _refresh_after_fork()
        held = _PROCESS_LEGACY_LOCKS.get(key)
        if held is not None:
            if held.fd is None or held.gateway_owner_thread is not None:
                return None
            if held.compat_owner_thread != owner_thread:
                return None
            held.gateway_owner_thread = owner_thread
            return GatewayLegacyLease(key, owner_thread)
    fd = _prepare_legacy_lock_file(state_path, create_if_missing=True)
    assert fd is not None
    try:
        if not _try_lock(fd):
            return None
        with _LOCKS_GUARD:
            _refresh_after_fork()
            held = _PROCESS_LEGACY_LOCKS.get(key)
            if held is not None:
                _unlock(fd)
                os.close(fd)
                fd = -1
                if (
                    held.fd is None
                    or held.gateway_owner_thread is not None
                    or held.compat_owner_thread != owner_thread
                ):
                    return None
                held.gateway_owner_thread = owner_thread
            else:
                _PROCESS_LEGACY_LOCKS[key] = _HeldLegacyLock(
                    fd=fd,
                    compat_count=0,
                    compat_owner_thread=None,
                    gateway_owner_thread=owner_thread,
                    path=path,
                    state_identity=_state_directory_identity(state_path),
                )
                fd = -1
            return GatewayLegacyLease(key, owner_thread)
    finally:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)


def release_gateway_legacy_lease(lease: GatewayLegacyLease | None) -> None:
    if lease is None:
        return
    with _LOCKS_GUARD:
        _refresh_after_fork()
        held = _PROCESS_LEGACY_LOCKS.get(lease.key)
        if held is None or held.gateway_owner_thread is None:
            return
        if held.gateway_owner_thread != lease.owner_thread:
            raise RuntimeError("gateway lock must be released by its owning thread")
        held.gateway_owner_thread = None
        if held.compat_count == 0:
            _remove_legacy_lock_aliases(held)
            if held.fd is not None:
                try:
                    _unlock(held.fd)
                finally:
                    os.close(held.fd)
                held.fd = None


@contextlib.contextmanager
def acquire_legacy_gateway_locks(
    *homes: str | Path,
    read_only_homes: Iterable[str | Path] = (),
    timeout: float = 0.0,
) -> Iterator[tuple[LegacyGatewayLock, ...]]:
    """Acquire all source/target legacy leases in globally sorted root order."""

    read_only_keys = {_normalized_path(home) for home in read_only_homes}
    roots: dict[str, tuple[Path, bool, bool]] = {}
    for home in homes:
        home_is_read_only = _normalized_path(home) in read_only_keys
        for root, explicit in _effective_state_root_specs(
            home,
            include_process_environment=not home_is_read_only,
        ):
            key = _normalized_path(root)
            create = not home_is_read_only
            previous = roots.get(key)
            # If any source owns this root, preserving source immutability wins.
            roots[key] = (
                root,
                create if previous is None else previous[1] and create,
                explicit if previous is None else previous[2] or explicit,
            )
    if not homes:
        yield ()
        return
    locks = tuple(
        LegacyGatewayLock(
            homes[0],
            state_roots=(roots[key][0],),
            create_if_missing=roots[key][1],
            allow_state_symlinks=roots[key][2],
            timeout=timeout,
        )
        for key in sorted(roots)
    )
    acquired: list[LegacyGatewayLock] = []
    try:
        for lock in locks:
            lock.acquire()
            acquired.append(lock)
        yield locks
    finally:
        for lock in reversed(acquired):
            lock.release()


@contextlib.contextmanager
def acquire_profile_locks(
    *homes: str | Path,
    timeout: float = 0.0,
) -> Iterator[tuple[ProfileOperationLock, ...]]:
    """Acquire multiple profile locks in normalized key order to avoid deadlocks."""
    unique: dict[str, Path] = {}
    for home in homes:
        path = Path(home).expanduser()
        unique.setdefault(profile_lock_key(path), path)
    locks = tuple(ProfileOperationLock(unique[key], timeout=timeout) for key in sorted(unique))
    acquired: list[ProfileOperationLock] = []
    try:
        for lock in locks:
            lock.acquire()
            acquired.append(lock)
        yield locks
    finally:
        for lock in reversed(acquired):
            lock.release()


def replacement_history_lock_scope(target: str | Path) -> Path:
    """Return the shared lock key for every sibling target's backup index."""

    home = Path(target).expanduser().absolute()
    return home.parent


__all__ = [
    "GatewayLegacyLease",
    "LegacyGatewayLock",
    "ProfileOperationLock",
    "acquire_gateway_legacy_lease",
    "acquire_legacy_gateway_locks",
    "acquire_profile_locks",
    "effective_state_roots",
    "move_profile_no_replace",
    "profile_lock_key",
    "profile_lock_path",
    "replacement_history_lock_scope",
    "rebind_legacy_gateway_lock",
    "release_gateway_legacy_lease",
    "user_state_dir",
]
