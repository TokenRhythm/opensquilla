"""Handle-pinned, no-follow source snapshots for Windows profile imports.

This module deliberately has no path-based fallback.  Every source path
component is opened with ``CreateFileW`` while its parent handle is still held,
without ``FILE_SHARE_DELETE`` and with ``FILE_FLAG_OPEN_REPARSE_POINT``. Source
files permit an existing writer so committed SQLite WAL bundles can be read;
their identity, metadata, and digest are checked again before publication. A
reparse point, non-disk object, unsupported file type, sharing conflict, or
identity change fails the import closed.

The public dataclasses mirror the migration engine's private manifest shape so
the Windows implementation can be adapted without weakening the cross-platform
protocol.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

_GENERIC_READ = 0x80000000
_FILE_READ_DATA = 0x0001
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_READONLY = 0x00000001
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_ATTRIBUTE_DEVICE = 0x00000040
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_TYPE_DISK = 0x0001
_FILE_ID_BOTH_DIRECTORY_INFO = 10
_FILE_ID_BOTH_DIRECTORY_RESTART_INFO = 11
_FILE_ID_INFO = 18
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PATH_NOT_FOUND = 3
_ERROR_NO_MORE_FILES = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_WINDOWS_EPOCH_TICKS = 116_444_736_000_000_000


class WindowsSourceSnapshotError(OSError):
    """The source could not be proven stable through pinned Windows handles."""


class WindowsSourceSnapshotUnavailableError(WindowsSourceSnapshotError):
    """The native Windows primitive is unavailable on this platform."""


@dataclass(frozen=True)
class WindowsPathIdentity:
    device: int
    inode: int
    file_type: int

    def as_json(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "file_type": self.file_type,
        }


@dataclass(frozen=True)
class WindowsManifestEntry:
    source: Path
    relative: Path
    entry_type: str
    identity: WindowsPathIdentity
    mode: int
    size: int
    mtime_ns: int
    digest: str | None
    attributes: int


@dataclass(frozen=True)
class WindowsSourceSnapshot:
    root: Path
    destination_prefix: Path
    identity: WindowsPathIdentity
    root_mode: int
    root_mtime_ns: int
    entries: tuple[WindowsManifestEntry, ...]
    role: str
    excluded: frozenset[Path]
    excluded_leaf_suffixes: tuple[str, ...]
    root_attributes: int


@dataclass(frozen=True)
class WindowsBoundedFileSnapshot:
    entry: WindowsManifestEntry
    data: bytes


@dataclass(frozen=True)
class WindowsDirectoryAuthoritySnapshot:
    root: Path
    identity: WindowsPathIdentity
    root_mode: int
    root_mtime_ns: int
    root_attributes: int
    files: tuple[tuple[str, WindowsBoundedFileSnapshot | None], ...]

    def file(self, name: str) -> WindowsBoundedFileSnapshot | None:
        return next((value for key, value in self.files if key == name), None)


@dataclass(frozen=True)
class _HandleInformation:
    identity: WindowsPathIdentity
    mode: int
    size: int
    mtime_ns: int
    attributes: int


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32),
        ("creation_time", _FILETIME),
        ("last_access_time", _FILETIME),
        ("last_write_time", _FILETIME),
        ("volume_serial_number", ctypes.c_uint32),
        ("file_size_high", ctypes.c_uint32),
        ("file_size_low", ctypes.c_uint32),
        ("number_of_links", ctypes.c_uint32),
        ("file_index_high", ctypes.c_uint32),
        ("file_index_low", ctypes.c_uint32),
    ]


class _FileId128(ctypes.Structure):
    _fields_ = [("identifier", ctypes.c_ubyte * 16)]


class _FileIdInfo(ctypes.Structure):
    _fields_ = [
        ("volume_serial_number", ctypes.c_uint64),
        ("file_id", _FileId128),
    ]


class _FileIdBothDirInfoHeader(ctypes.Structure):
    _fields_ = [
        ("next_entry_offset", ctypes.c_uint32),
        ("file_index", ctypes.c_uint32),
        ("creation_time", ctypes.c_int64),
        ("last_access_time", ctypes.c_int64),
        ("last_write_time", ctypes.c_int64),
        ("change_time", ctypes.c_int64),
        ("end_of_file", ctypes.c_int64),
        ("allocation_size", ctypes.c_int64),
        ("file_attributes", ctypes.c_uint32),
        ("file_name_length", ctypes.c_uint32),
        ("ea_size", ctypes.c_uint32),
        ("short_name_length", ctypes.c_byte),
        # Fixed-width WCHAR storage keeps the documented layout testable on
        # non-Windows hosts where ctypes.c_wchar is four bytes.
        ("short_name", ctypes.c_uint16 * 12),
        ("file_id", ctypes.c_int64),
        ("file_name", ctypes.c_uint16 * 1),
    ]


def _extended_path(path: Path) -> str:
    value = str(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _filetime_to_unix_ns(value: _FILETIME) -> int:
    ticks = (int(value.high) << 32) | int(value.low)
    return (ticks - _WINDOWS_EPOCH_TICKS) * 100


def _mode_from_attributes(attributes: int) -> int:
    if attributes & _FILE_ATTRIBUTE_DIRECTORY:
        permissions = 0o500 if attributes & _FILE_ATTRIBUTE_READONLY else 0o700
        return stat.S_IFDIR | permissions
    permissions = 0o400 if attributes & _FILE_ATTRIBUTE_READONLY else 0o600
    return stat.S_IFREG | permissions


def _entry_type(attributes: int) -> str:
    return "directory" if attributes & _FILE_ATTRIBUTE_DIRECTORY else "file"


def _raise_windows_error(operation: str, path: Path | None = None) -> None:
    get_last_error = getattr(ctypes, "get_last_error", None)
    code = int(get_last_error()) if get_last_error is not None else 0
    message = f"{operation} failed (WinError {code})"
    if path is None:
        raise WindowsSourceSnapshotError(code, message)
    raise WindowsSourceSnapshotError(code, message, str(path))


class _Win32SourceApi:
    """Small Win32 boundary kept injectable for non-Windows contract tests."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsSourceSnapshotUnavailableError(
                "native Windows source snapshot handles are unavailable"
            )
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:  # pragma: no cover - defensive Windows runtime gate
            raise WindowsSourceSnapshotUnavailableError("ctypes.WinDLL is unavailable")
        self._kernel32 = win_dll("kernel32", use_last_error=True)

        self._create_file = self._kernel32.CreateFileW
        self._create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self._create_file.restype = ctypes.c_void_p

        self._close_handle = self._kernel32.CloseHandle
        self._close_handle.argtypes = [ctypes.c_void_p]
        self._close_handle.restype = ctypes.c_int

        self._get_file_type = self._kernel32.GetFileType
        self._get_file_type.argtypes = [ctypes.c_void_p]
        self._get_file_type.restype = ctypes.c_uint32

        self._get_information = self._kernel32.GetFileInformationByHandle
        self._get_information.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ByHandleFileInformation),
        ]
        self._get_information.restype = ctypes.c_int

        self._get_information_ex = self._kernel32.GetFileInformationByHandleEx
        self._get_information_ex.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self._get_information_ex.restype = ctypes.c_int

        self._read_file = self._kernel32.ReadFile
        self._read_file.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self._read_file.restype = ctypes.c_int

    def normalize_root(self, path: Path) -> Path:
        candidate = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
        if not candidate.is_absolute() or not candidate.anchor:
            raise WindowsSourceSnapshotError(f"source path is not absolute: {path}")
        if any(part in {".", ".."} for part in candidate.parts[1:]):
            raise WindowsSourceSnapshotError(f"source path is not normalized: {path}")
        return candidate

    def open_path(self, path: Path, *, allow_writers: bool) -> int:
        # FILE_READ_DATA and FILE_LIST_DIRECTORY have the same access bit.  The
        # share mode never permits delete access.  Directory handles permit
        # writes so pinning C:\Users and other ancestors does not block unrelated
        # sibling activity. Callers disable write sharing for the selected
        # source root and every entry below it, so an active source writer fails
        # the offline snapshot closed.
        share_mode = _FILE_SHARE_READ
        if allow_writers:
            share_mode |= _FILE_SHARE_WRITE
        handle = self._create_file(
            _extended_path(path),
            _FILE_READ_DATA | _FILE_READ_ATTRIBUTES,
            share_mode,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if handle in {None, _INVALID_HANDLE_VALUE}:
            _raise_windows_error("CreateFileW", path)
        return int(handle)

    def close(self, handle: int) -> None:
        if not self._close_handle(ctypes.c_void_p(handle)):
            _raise_windows_error("CloseHandle")

    def information(self, handle: int, *, path: Path) -> _HandleInformation:
        if int(self._get_file_type(ctypes.c_void_p(handle))) != _FILE_TYPE_DISK:
            raise WindowsSourceSnapshotError(f"source is not a disk file: {path}")

        basic = _ByHandleFileInformation()
        if not self._get_information(ctypes.c_void_p(handle), ctypes.byref(basic)):
            _raise_windows_error("GetFileInformationByHandle", path)

        identifier = _FileIdInfo()
        if not self._get_information_ex(
            ctypes.c_void_p(handle),
            _FILE_ID_INFO,
            ctypes.byref(identifier),
            ctypes.sizeof(identifier),
        ):
            _raise_windows_error("GetFileInformationByHandleEx(FileIdInfo)", path)

        attributes = int(basic.attributes)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise WindowsSourceSnapshotError(f"source reparse point is not allowed: {path}")
        if attributes & _FILE_ATTRIBUTE_DEVICE:
            raise WindowsSourceSnapshotError(f"source device file is not allowed: {path}")

        file_type = (
            stat.S_IFDIR if attributes & _FILE_ATTRIBUTE_DIRECTORY else stat.S_IFREG
        )
        file_id = int.from_bytes(bytes(identifier.file_id.identifier), "little")
        if file_id == 0:
            raise WindowsSourceSnapshotError(f"source file identity is unavailable: {path}")
        size = (int(basic.file_size_high) << 32) | int(basic.file_size_low)
        return _HandleInformation(
            identity=WindowsPathIdentity(
                device=int(identifier.volume_serial_number),
                inode=file_id,
                file_type=file_type,
            ),
            mode=_mode_from_attributes(attributes),
            size=size,
            mtime_ns=_filetime_to_unix_ns(basic.last_write_time),
            attributes=attributes,
        )

    def enumerate_names(self, handle: int, *, path: Path) -> tuple[str, ...]:
        buffer_size = 64 * 1024
        buffer = ctypes.create_string_buffer(buffer_size)
        names: list[str] = []
        information_class = _FILE_ID_BOTH_DIRECTORY_RESTART_INFO
        while True:
            if not self._get_information_ex(
                ctypes.c_void_p(handle),
                information_class,
                buffer,
                buffer_size,
            ):
                get_last_error = getattr(ctypes, "get_last_error", None)
                code = int(get_last_error()) if get_last_error is not None else 0
                if code == _ERROR_NO_MORE_FILES:
                    break
                _raise_windows_error(
                    "GetFileInformationByHandleEx(FileIdBothDirectoryInfo)", path
                )
            information_class = _FILE_ID_BOTH_DIRECTORY_INFO
            offset = 0
            while True:
                header_size = _FileIdBothDirInfoHeader.file_name.offset
                if offset < 0 or offset + header_size > buffer_size:
                    raise WindowsSourceSnapshotError(
                        f"invalid Windows directory enumeration buffer: {path}"
                    )
                header = _FileIdBothDirInfoHeader.from_buffer_copy(
                    buffer.raw[offset : offset + ctypes.sizeof(_FileIdBothDirInfoHeader)]
                )
                name_size = int(header.file_name_length)
                name_offset = offset + header_size
                if name_size % 2 or name_offset + name_size > buffer_size:
                    raise WindowsSourceSnapshotError(
                        f"invalid Windows directory entry name: {path}"
                    )
                raw_name = ctypes.string_at(ctypes.addressof(buffer) + name_offset, name_size)
                name = raw_name.decode("utf-16-le", errors="strict")
                if name not in {".", ".."}:
                    if not name or "\x00" in name or "\\" in name or "/" in name:
                        raise WindowsSourceSnapshotError(
                            f"unsafe Windows directory entry returned for: {path}"
                        )
                    names.append(name)
                next_offset = int(header.next_entry_offset)
                if next_offset == 0:
                    break
                if next_offset < header_size or offset + next_offset >= buffer_size:
                    raise WindowsSourceSnapshotError(
                        f"invalid Windows directory entry offset: {path}"
                    )
                offset += next_offset
        return tuple(sorted(names, key=lambda name: (os.path.normcase(name), name)))

    def read(self, handle: int, size: int, *, path: Path) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_uint32()
        if not self._read_file(
            ctypes.c_void_p(handle),
            buffer,
            size,
            ctypes.byref(read),
            None,
        ):
            _raise_windows_error("ReadFile", path)
        return bytes(buffer.raw[: int(read.value)])


def _new_api() -> _Win32SourceApi:
    return _Win32SourceApi()


def windows_source_snapshot_available() -> bool:
    return os.name == "nt" and getattr(ctypes, "WinDLL", None) is not None


def _same_information(left: _HandleInformation, right: _HandleInformation) -> bool:
    return (
        left.identity == right.identity
        and left.mode == right.mode
        and left.size == right.size
        and left.mtime_ns == right.mtime_ns
        and left.attributes == right.attributes
    )


def _same_ancestor_information(
    left: _HandleInformation,
    right: _HandleInformation,
) -> bool:
    """Compare a pinned ancestor without treating sibling activity as a swap."""

    return (
        left.identity == right.identity
        and left.mode == right.mode
        and left.attributes == right.attributes
        and left.identity.file_type == stat.S_IFDIR
    )


def _validate_information(information: _HandleInformation, *, path: Path) -> None:
    if information.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise WindowsSourceSnapshotError(f"source reparse point is not allowed: {path}")
    if information.attributes & _FILE_ATTRIBUTE_DEVICE:
        raise WindowsSourceSnapshotError(f"source device file is not allowed: {path}")
    expected_type = (
        stat.S_IFDIR
        if information.attributes & _FILE_ATTRIBUTE_DIRECTORY
        else stat.S_IFREG
    )
    if information.identity.file_type != expected_type:
        raise WindowsSourceSnapshotError(f"source file type is inconsistent: {path}")


def _matches_entry(
    information: _HandleInformation,
    entry: WindowsManifestEntry,
    *,
    ignore_directory_metadata: bool = False,
) -> bool:
    metadata_matches = (
        ignore_directory_metadata
        and entry.entry_type == "directory"
    ) or (
        information.size == entry.size
        and information.mtime_ns == entry.mtime_ns
    )
    return (
        information.identity == entry.identity
        and information.mode == entry.mode
        and metadata_matches
        and information.attributes == entry.attributes
        and _entry_type(information.attributes) == entry.entry_type
    )


def _matches_root(information: _HandleInformation, snapshot: WindowsSourceSnapshot) -> bool:
    return (
        information.identity == snapshot.identity
        and information.mode == snapshot.root_mode
        and (
            bool(snapshot.excluded_leaf_suffixes)
            or information.mtime_ns == snapshot.root_mtime_ns
        )
        and information.attributes == snapshot.root_attributes
        and information.identity.file_type == stat.S_IFDIR
    )


@contextmanager
def _open_directory_chain(
    api: _Win32SourceApi,
    root: Path,
) -> Iterator[tuple[Path, list[tuple[Path, int, _HandleInformation]]]]:
    normalized = api.normalize_root(root)
    anchor = Path(normalized.anchor)
    if not anchor:
        raise WindowsSourceSnapshotError(f"source path has no Windows anchor: {root}")
    component_paths = [anchor]
    current = anchor
    for part in normalized.parts[1:]:
        current /= part
        component_paths.append(current)

    opened: list[tuple[Path, int, _HandleInformation]] = []
    try:
        for index, component in enumerate(component_paths):
            # Ancestors above the selected root remain usable by unrelated
            # applications. The selected root itself is opened without write
            # sharing so the offline source directory cannot be mutated while
            # it is enumerated or copied.
            handle = api.open_path(
                component,
                allow_writers=index < len(component_paths) - 1,
            )
            try:
                information = api.information(handle, path=component)
                _validate_information(information, path=component)
                if information.identity.file_type != stat.S_IFDIR:
                    raise WindowsSourceSnapshotError(
                        f"source path component is not a directory: {component}"
                    )
            except BaseException:
                api.close(handle)
                raise
            opened.append((component, handle, information))
        yield normalized, opened
        for index, (component, handle, expected) in enumerate(opened):
            current_information = api.information(handle, path=component)
            selected_root = index == len(opened) - 1
            matches = (
                _same_information(current_information, expected)
                if selected_root
                else _same_ancestor_information(current_information, expected)
            )
            if not matches:
                raise WindowsSourceSnapshotError(
                    f"source path component changed while pinned: {component}"
                )
    finally:
        close_error: BaseException | None = None
        for _component, handle, _information in reversed(opened):
            try:
                api.close(handle)
            except BaseException as exc:  # pragma: no cover - exceptional OS teardown
                close_error = close_error or exc
        if close_error is not None:
            raise close_error


@contextmanager
def _open_child(
    api: _Win32SourceApi,
    path: Path,
    *,
    allow_file_writers: bool = False,
    ignore_directory_metadata: bool = False,
) -> Iterator[tuple[int, _HandleInformation]]:
    handle = api.open_path(path, allow_writers=allow_file_writers)
    try:
        information = api.information(handle, path=path)
        _validate_information(information, path=path)
        if allow_file_writers and information.identity.file_type == stat.S_IFDIR:
            # We do not know the child type until it is opened. Keep the first
            # no-delete handle pinned while replacing a permissive directory
            # handle with the restrictive handle used for recursive traversal.
            restrictive_handle = api.open_path(path, allow_writers=False)
            try:
                restrictive_information = api.information(restrictive_handle, path=path)
                _validate_information(restrictive_information, path=path)
                information_matches = (
                    _same_ancestor_information(restrictive_information, information)
                    if ignore_directory_metadata
                    else _same_information(restrictive_information, information)
                )
                if not information_matches:
                    raise WindowsSourceSnapshotError(
                        f"source changed while its directory handle was tightened: {path}"
                    )
            except BaseException:
                api.close(restrictive_handle)
                raise
            permissive_handle = handle
            handle = restrictive_handle
            information = restrictive_information
            api.close(permissive_handle)
        yield handle, information
        current = api.information(handle, path=path)
        information_matches = (
            _same_ancestor_information(current, information)
            if ignore_directory_metadata
            and information.identity.file_type == stat.S_IFDIR
            else _same_information(current, information)
        )
        if not information_matches:
            raise WindowsSourceSnapshotError(f"source changed while pinned: {path}")
    finally:
        api.close(handle)


def _digest_handle(
    api: _Win32SourceApi,
    handle: int,
    *,
    source: Path,
    destination: Path | None = None,
) -> str:
    descriptor: int | None = None
    digest = hashlib.sha256()
    try:
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0))
            descriptor = os.open(destination, flags, 0o600)
        while True:
            chunk = api.read(handle, 1024 * 1024, path=source)
            if not chunk:
                break
            digest.update(chunk)
            if descriptor is not None:
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("destination write made no progress")
                    view = view[written:]
        if descriptor is not None:
            os.fsync(descriptor)
    except BaseException:
        if destination is not None:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()


def _validate_relative_path(path: Path, *, label: str) -> None:
    if path.is_absolute() or ".." in path.parts:
        raise WindowsSourceSnapshotError(f"{label} is unsafe: {path}")


def _scan_with_api(
    api: _Win32SourceApi,
    root: Path,
    *,
    destination_prefix: Path,
    role: str,
    excluded: set[Path] | frozenset[Path],
    excluded_leaf_suffixes: tuple[str, ...],
) -> WindowsSourceSnapshot:
    _validate_relative_path(destination_prefix, label="import destination prefix")
    normalized_excluded = frozenset(Path(value) for value in excluded)
    normalized_suffixes = tuple(dict.fromkeys(excluded_leaf_suffixes))
    if any(not suffix or "\\" in suffix or "/" in suffix for suffix in normalized_suffixes):
        raise WindowsSourceSnapshotError("excluded source leaf suffix is unsafe")
    for value in normalized_excluded:
        _validate_relative_path(value, label="excluded source path")

    with _open_directory_chain(api, root) as (normalized_root, chain):
        root_path, root_handle, root_information = chain[-1]
        entries: list[WindowsManifestEntry] = []

        def visit(directory_path: Path, directory_handle: int, relative: Path) -> None:
            before = api.information(directory_handle, path=directory_path)
            names_before = tuple(
                name
                for name in api.enumerate_names(directory_handle, path=directory_path)
                if not name.endswith(normalized_suffixes)
            )
            for name in names_before:
                child_relative = relative / name
                if child_relative in normalized_excluded or any(
                    parent in normalized_excluded for parent in child_relative.parents
                ):
                    continue
                child_path = directory_path / name
                with _open_child(
                    api,
                    child_path,
                    allow_file_writers=True,
                    ignore_directory_metadata=bool(normalized_suffixes),
                ) as (child_handle, information):
                    entry_type = _entry_type(information.attributes)
                    digest: str | None = None
                    if entry_type == "file":
                        digest = _digest_handle(api, child_handle, source=child_path)
                    entry = WindowsManifestEntry(
                        source=normalized_root / child_relative,
                        relative=child_relative,
                        entry_type=entry_type,
                        identity=information.identity,
                        mode=information.mode,
                        size=information.size,
                        mtime_ns=information.mtime_ns,
                        digest=digest,
                        attributes=information.attributes,
                    )
                    entries.append(entry)
                    if entry_type == "directory":
                        visit(child_path, child_handle, child_relative)
            names_after = tuple(
                name
                for name in api.enumerate_names(directory_handle, path=directory_path)
                if not name.endswith(normalized_suffixes)
            )
            after = api.information(directory_handle, path=directory_path)
            directory_matches = (
                _same_ancestor_information(after, before)
                if normalized_suffixes
                else _same_information(after, before)
            )
            if names_after != names_before or not directory_matches:
                raise WindowsSourceSnapshotError(
                    f"source directory changed during enumeration: {directory_path}"
                )

        visit(root_path, root_handle, Path())
        current_root = api.information(root_handle, path=root_path)
        root_matches = (
            _same_ancestor_information(current_root, root_information)
            if normalized_suffixes
            else _same_information(current_root, root_information)
        )
        if not root_matches:
            raise WindowsSourceSnapshotError(
                f"source root changed during enumeration: {normalized_root}"
            )
        entries.sort(key=lambda entry: (len(entry.relative.parts), entry.relative.as_posix()))
        return WindowsSourceSnapshot(
            root=normalized_root,
            destination_prefix=destination_prefix,
            identity=root_information.identity,
            root_mode=root_information.mode,
            root_mtime_ns=root_information.mtime_ns,
            entries=tuple(entries),
            role=role,
            excluded=normalized_excluded,
            excluded_leaf_suffixes=normalized_suffixes,
            root_attributes=root_information.attributes,
        )


def scan_windows_source_tree(
    root: Path,
    *,
    destination_prefix: Path,
    role: str,
    excluded: set[Path] | frozenset[Path] = frozenset(),
    excluded_leaf_suffixes: tuple[str, ...] = (),
) -> WindowsSourceSnapshot:
    """Build a stable manifest through pinned no-reparse Win32 handles."""

    return _scan_with_api(
        _new_api(),
        root,
        destination_prefix=destination_prefix,
        role=role,
        excluded=excluded,
        excluded_leaf_suffixes=excluded_leaf_suffixes,
    )


def _read_bounded_handle(
    api: _Win32SourceApi,
    handle: int,
    *,
    source: Path,
    expected_size: int,
    limit: int,
) -> tuple[bytes, str]:
    if expected_size > limit:
        raise WindowsSourceSnapshotError("bounded source file exceeds its read limit")
    remaining = limit + 1
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    while remaining > 0:
        chunk = api.read(handle, min(64 * 1024, remaining), path=source)
        if not chunk:
            break
        chunks.append(chunk)
        digest.update(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != expected_size or len(data) > limit:
        raise WindowsSourceSnapshotError("bounded source file changed while read")
    return data, digest.hexdigest()


def _capture_bounded_with_api(
    api: _Win32SourceApi,
    root: Path,
    *,
    names: tuple[str, ...],
    max_bytes: int,
) -> WindowsDirectoryAuthoritySnapshot | None:
    if max_bytes < 0:
        raise ValueError("max_bytes must not be negative")
    if len(set(names)) != len(names) or any(
        not name or name in {".", ".."} or "\\" in name or "/" in name or "\x00" in name
        for name in names
    ):
        raise WindowsSourceSnapshotError("bounded source names must be unique leaf names")
    try:
        chain_context = _open_directory_chain(api, root)
        with chain_context as (normalized_root, chain):
            root_path, root_handle, root_information = chain[-1]
            names_before = api.enumerate_names(root_handle, path=root_path)
            captured: list[tuple[str, WindowsBoundedFileSnapshot | None]] = []
            for name in names:
                if name not in names_before:
                    captured.append((name, None))
                    continue
                path = root_path / name
                with _open_child(api, path) as (handle, information):
                    if information.identity.file_type != stat.S_IFREG:
                        raise WindowsSourceSnapshotError(
                            f"bounded source authority is not a regular file: {path}"
                        )
                    data, digest = _read_bounded_handle(
                        api,
                        handle,
                        source=path,
                        expected_size=information.size,
                        limit=max_bytes,
                    )
                    captured.append(
                        (
                            name,
                            WindowsBoundedFileSnapshot(
                                entry=WindowsManifestEntry(
                                    source=normalized_root / name,
                                    relative=Path(name),
                                    entry_type="file",
                                    identity=information.identity,
                                    mode=information.mode,
                                    size=information.size,
                                    mtime_ns=information.mtime_ns,
                                    digest=digest,
                                    attributes=information.attributes,
                                ),
                                data=data,
                            ),
                        )
                    )
            names_after = api.enumerate_names(root_handle, path=root_path)
            current_root = api.information(root_handle, path=root_path)
            _validate_information(current_root, path=root_path)
            if names_after != names_before or not _same_information(
                current_root, root_information
            ):
                raise WindowsSourceSnapshotError(
                    f"source authority directory changed during inspection: {root_path}"
                )
            return WindowsDirectoryAuthoritySnapshot(
                root=normalized_root,
                identity=root_information.identity,
                root_mode=root_information.mode,
                root_mtime_ns=root_information.mtime_ns,
                root_attributes=root_information.attributes,
                files=tuple(captured),
            )
    except WindowsSourceSnapshotError as exc:
        if exc.errno in {_ERROR_FILE_NOT_FOUND, _ERROR_PATH_NOT_FOUND}:
            return None
        raise


def capture_windows_bounded_files(
    root: Path,
    *,
    names: tuple[str, ...],
    max_bytes: int,
) -> WindowsDirectoryAuthoritySnapshot | None:
    """Read selected small leaf files while their full parent chain is pinned."""

    return _capture_bounded_with_api(
        _new_api(),
        root,
        names=names,
        max_bytes=max_bytes,
    )


def _copy_with_api(
    api: _Win32SourceApi,
    snapshot: WindowsSourceSnapshot,
    entry: WindowsManifestEntry,
    destination: Path,
) -> str:
    if entry.entry_type != "file" or entry.digest is None:
        raise WindowsSourceSnapshotError(f"source manifest entry is not a file: {entry.source}")
    _validate_relative_path(entry.relative, label="source manifest path")
    expected_entry = next(
        (candidate for candidate in snapshot.entries if candidate.relative == entry.relative),
        None,
    )
    if expected_entry != entry:
        raise WindowsSourceSnapshotError(f"source manifest entry is not frozen: {entry.source}")

    directories = {
        candidate.relative: candidate
        for candidate in snapshot.entries
        if candidate.entry_type == "directory"
    }
    opened_children: list[tuple[Path, int, _HandleInformation]] = []
    try:
        with _open_directory_chain(api, snapshot.root) as (normalized_root, chain):
            root_path, _root_handle, root_information = chain[-1]
            if normalized_root != snapshot.root or not _matches_root(root_information, snapshot):
                raise WindowsSourceSnapshotError(
                    f"source root changed before copy: {snapshot.root}"
                )

            parent_path = root_path
            relative_parent = Path()
            for part in entry.relative.parent.parts:
                relative_parent /= part
                expected_directory = directories.get(relative_parent)
                if expected_directory is None:
                    raise WindowsSourceSnapshotError(
                        f"source manifest parent is missing: {relative_parent}"
                    )
                parent_path /= part
                handle = api.open_path(parent_path, allow_writers=False)
                try:
                    information = api.information(handle, path=parent_path)
                    _validate_information(information, path=parent_path)
                    if not _matches_entry(
                        information,
                        expected_directory,
                        ignore_directory_metadata=bool(snapshot.excluded_leaf_suffixes),
                    ):
                        raise WindowsSourceSnapshotError(
                            f"source directory changed before copy: {expected_directory.source}"
                        )
                except BaseException:
                    api.close(handle)
                    raise
                opened_children.append((parent_path, handle, information))

            file_path = parent_path / entry.relative.name
            with _open_child(
                api,
                file_path,
                allow_file_writers=True,
            ) as (file_handle, information):
                if not _matches_entry(information, entry):
                    raise WindowsSourceSnapshotError(
                        f"source file changed before copy: {entry.source}"
                    )
                digest = _digest_handle(
                    api,
                    file_handle,
                    source=file_path,
                    destination=destination,
                )
                if digest != entry.digest:
                    destination.unlink(missing_ok=True)
                    raise WindowsSourceSnapshotError(
                        f"source file content changed during copy: {entry.source}"
                    )

            for directory_path, handle, expected in opened_children:
                current = api.information(handle, path=directory_path)
                directory_matches = (
                    _same_ancestor_information(current, expected)
                    if snapshot.excluded_leaf_suffixes
                    else _same_information(current, expected)
                )
                if not directory_matches:
                    destination.unlink(missing_ok=True)
                    raise WindowsSourceSnapshotError(
                        f"source directory changed during copy: {directory_path}"
                    )
            return digest
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        close_error: BaseException | None = None
        for _path, handle, _information in reversed(opened_children):
            try:
                api.close(handle)
            except BaseException as exc:  # pragma: no cover - exceptional OS teardown
                close_error = close_error or exc
        if close_error is not None:
            raise close_error


def copy_windows_snapshot_file(
    snapshot: WindowsSourceSnapshot,
    entry: WindowsManifestEntry,
    destination: Path,
) -> str:
    """Copy one frozen file without resolving source path components."""

    return _copy_with_api(_new_api(), snapshot, entry, destination)


__all__ = [
    "WindowsBoundedFileSnapshot",
    "WindowsDirectoryAuthoritySnapshot",
    "WindowsManifestEntry",
    "WindowsPathIdentity",
    "WindowsSourceSnapshot",
    "WindowsSourceSnapshotError",
    "WindowsSourceSnapshotUnavailableError",
    "capture_windows_bounded_files",
    "copy_windows_snapshot_file",
    "scan_windows_source_tree",
    "windows_source_snapshot_available",
]
