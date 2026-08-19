"""Low-level, handle-bound privacy controls for local runtime paths."""

from __future__ import annotations

import ctypes
import os
import re
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opensquilla.paths import native_io_path

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_REPARSE_NAME_SURROGATE_BIT = 0x20000000
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_ALREADY_EXISTS = 183
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_SE_FILE_OBJECT = 1
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_ID_INFO = 18
_FILE_ALL_ACCESS = 0x001F01FF
_WINDOWS_ACE_RE = re.compile(r"\(([^()]*)\)")
_WINDOWS_HEX_ACCESS_MASK_RE = re.compile(r"0[xX][0-9A-Fa-f]{1,8}")


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


class _WindowsSidAndAttributes(ctypes.Structure):
    _fields_ = [
        ("sid", ctypes.c_void_p),
        ("attributes", ctypes.c_uint32),
    ]


class _WindowsTokenUser(ctypes.Structure):
    _fields_ = [("user", _WindowsSidAndAttributes)]


class _WindowsSecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("security_descriptor", ctypes.c_void_p),
        ("inherit_handle", ctypes.c_int),
    ]


def _private_windows_sddl(user_sid: str, *, directory: bool) -> str:
    inheritance = "OICI" if directory else ""
    entries = [f"(A;{inheritance};FA;;;{user_sid})"]
    if user_sid.casefold() not in {"sy", "s-1-5-18"}:
        entries.append(f"(A;{inheritance};FA;;;SY)")
    return f"D:P{''.join(entries)}"


def _windows_last_error() -> int:
    getter = getattr(ctypes, "get_last_error", None)
    return int(getter()) if callable(getter) else 0


def _reparse_tag_redirects(tag: int) -> bool:
    return tag == 0 or bool(tag & _REPARSE_NAME_SURROGATE_BIT)


def _windows_sddl_is_private(
    sddl: str,
    *,
    user_sid: str,
    directory: bool,
    require_protected: bool,
    canonicalize_sid: Callable[[str], str] | None = None,
) -> bool:
    dacl_start = sddl.find("D:")
    first_ace = sddl.find("(", dacl_start)
    if not sddl.startswith("O:") or dacl_start < 0 or first_ace < 0:
        return False
    def canonical_sid(value: str) -> str | None:
        if canonicalize_sid is not None:
            try:
                return canonicalize_sid(value).casefold()
            except OSError:
                return None
        normalized = value.casefold()
        return {
            "sy": "s-1-5-18",
            "ba": "s-1-5-32-544",
        }.get(normalized, normalized)

    owner = canonical_sid(sddl[2:dacl_start])
    normalized_user_sid = canonical_sid(user_sid)
    system_sid = canonical_sid("SY")
    administrators_sid = canonical_sid("BA")
    if (
        owner is None
        or normalized_user_sid is None
        or system_sid is None
        or administrators_sid is None
        or owner not in {normalized_user_sid, system_sid, administrators_sid}
    ):
        return False
    dacl_flags = sddl[dacl_start + 2 : first_ace]
    if require_protected and "P" not in dacl_flags:
        return False
    entries = _WINDOWS_ACE_RE.findall(sddl[first_ace:])
    expected_principals = {normalized_user_sid, system_sid}
    if len(entries) != len(expected_principals):
        return False
    actual_principals: set[str] = set()
    for entry in entries:
        fields = entry.split(";")
        if len(fields) != 6 or fields[0] != "A":
            return False
        access_mask = fields[2]
        if access_mask != "FA" and (
            _WINDOWS_HEX_ACCESS_MASK_RE.fullmatch(access_mask) is None
            or int(access_mask, 16) != _FILE_ALL_ACCESS
        ):
            return False
        ace_flags = fields[1]
        if (
            directory
            and require_protected
            and not {"OI", "CI"}.issubset(
                {ace_flags[index : index + 2] for index in range(0, len(ace_flags), 2)}
            )
        ):
            return False
        principal = canonical_sid(fields[5])
        if principal is None:
            return False
        actual_principals.add(principal)
    return actual_principals == expected_principals


class _CtypesWindowsPrivateAcl:
    """Thin handle-bound Win32 ACL adapter; injectable in platform-neutral tests."""

    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL")
        self.advapi32 = loader("advapi32", use_last_error=True)
        self.kernel32 = loader("kernel32", use_last_error=True)

    def current_user_sid(self) -> str:
        open_token = self.advapi32.OpenProcessToken
        get_token = self.advapi32.GetTokenInformation
        convert_sid = self.advapi32.ConvertSidToStringSidW
        get_process = self.kernel32.GetCurrentProcess
        close_handle = self.kernel32.CloseHandle
        local_free = self.kernel32.LocalFree
        open_token.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        open_token.restype = ctypes.c_int
        get_token.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_token.restype = ctypes.c_int
        convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        convert_sid.restype = ctypes.c_int
        get_process.argtypes = []
        get_process.restype = ctypes.c_void_p
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p

        token = ctypes.c_void_p()
        if not open_token(get_process(), _TOKEN_QUERY, ctypes.byref(token)):
            error_number = _windows_last_error()
            raise OSError(error_number, "cannot open the current Windows process token")
        try:
            required = ctypes.c_uint32()
            first = get_token(token, _TOKEN_USER, None, 0, ctypes.byref(required))
            error_number = _windows_last_error()
            if first or required.value == 0 or error_number != _ERROR_INSUFFICIENT_BUFFER:
                raise OSError(
                    error_number,
                    "cannot size the current Windows token user",
                )
            buffer = ctypes.create_string_buffer(required.value)
            if not get_token(
                token,
                _TOKEN_USER,
                buffer,
                required.value,
                ctypes.byref(required),
            ):
                error_number = _windows_last_error()
                raise OSError(error_number, "cannot read the current Windows token user")
            token_user = ctypes.cast(buffer, ctypes.POINTER(_WindowsTokenUser)).contents
            string_sid = ctypes.c_void_p()
            if not convert_sid(token_user.user.sid, ctypes.byref(string_sid)):
                error_number = _windows_last_error()
                raise OSError(error_number, "cannot format the current Windows user SID")
            try:
                return ctypes.wstring_at(string_sid)
            finally:
                local_free(string_sid)
        finally:
            close_handle(token)

    def canonical_sid(self, value: str) -> str:
        convert_descriptor = (
            self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        )
        get_owner = self.advapi32.GetSecurityDescriptorOwner
        convert_to_string = self.advapi32.ConvertSidToStringSidW
        local_free = self.kernel32.LocalFree
        convert_descriptor.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        convert_descriptor.restype = ctypes.c_int
        get_owner.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int),
        ]
        get_owner.restype = ctypes.c_int
        convert_to_string.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        convert_to_string.restype = ctypes.c_int
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p

        descriptor = ctypes.c_void_p()
        descriptor_size = ctypes.c_uint32()
        if not convert_descriptor(
            f"O:{value}",
            _SDDL_REVISION_1,
            ctypes.byref(descriptor),
            ctypes.byref(descriptor_size),
        ):
            error_number = _windows_last_error()
            raise OSError(error_number, "cannot parse a Windows SDDL principal")
        try:
            sid = ctypes.c_void_p()
            defaulted = ctypes.c_int()
            if (
                not get_owner(descriptor, ctypes.byref(sid), ctypes.byref(defaulted))
                or not sid.value
            ):
                error_number = _windows_last_error()
                raise OSError(error_number, "Windows SDDL principal has no owner SID")
            canonical = ctypes.c_void_p()
            if not convert_to_string(sid, ctypes.byref(canonical)):
                error_number = _windows_last_error()
                raise OSError(error_number, "cannot canonicalize a Windows SDDL principal")
            try:
                return ctypes.wstring_at(canonical)
            finally:
                local_free(canonical)
        finally:
            local_free(descriptor)

    @contextmanager
    def open_bound(
        self,
        path: Path,
        *,
        directory: bool,
        expected_device: int,
        expected_inode: int,
    ) -> Iterator[object]:
        create_file = self.kernel32.CreateFileW
        get_information = self.kernel32.GetFileInformationByHandleEx
        close_handle = self.kernel32.CloseHandle
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

        flags = _FILE_FLAG_OPEN_REPARSE_POINT
        if directory:
            flags |= _FILE_FLAG_BACKUP_SEMANTICS
        handle = create_file(
            os.fspath(native_io_path(path)),
            _READ_CONTROL | _WRITE_DAC | _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            flags,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle in {None, invalid_handle}:
            error_number = _windows_last_error()
            raise OSError(error_number, "cannot bind private Windows path")
        try:
            attributes = _WindowsFileAttributeTagInfo()
            if not get_information(
                handle,
                _FILE_ATTRIBUTE_TAG_INFO,
                ctypes.byref(attributes),
                ctypes.sizeof(attributes),
            ):
                error_number = _windows_last_error()
                raise OSError(error_number, "cannot inspect bound private Windows path")
            file_id = _WindowsFileIdInfo()
            if not get_information(
                handle,
                _FILE_ID_INFO,
                ctypes.byref(file_id),
                ctypes.sizeof(file_id),
            ):
                error_number = _windows_last_error()
                raise OSError(
                    error_number,
                    "cannot inspect bound private Windows path identity",
                )
            file_attributes = int(attributes.file_attributes)
            is_directory = bool(file_attributes & _FILE_ATTRIBUTE_DIRECTORY)
            identity = (
                int(file_id.volume_serial_number),
                int.from_bytes(bytes(file_id.file_id.identifier), "little"),
            )
            if (
                (
                    file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                    and _reparse_tag_redirects(int(attributes.reparse_tag))
                )
                or is_directory != directory
                or (expected_inode and identity != (expected_device, expected_inode))
            ):
                raise OSError(0, "private Windows path changed or is a reparse point")
            yield handle
        finally:
            close_handle(handle)

    @contextmanager
    def _security_descriptor(self, sddl: str) -> Iterator[tuple[object, object]]:
        convert = self.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        get_dacl = self.advapi32.GetSecurityDescriptorDacl
        local_free = self.kernel32.LocalFree
        convert.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        convert.restype = ctypes.c_int
        get_dacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int),
        ]
        get_dacl.restype = ctypes.c_int
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p

        descriptor = ctypes.c_void_p()
        descriptor_size = ctypes.c_uint32()
        if not convert(
            sddl,
            _SDDL_REVISION_1,
            ctypes.byref(descriptor),
            ctypes.byref(descriptor_size),
        ):
            error_number = _windows_last_error()
            raise OSError(
                error_number,
                "cannot create a private Windows security descriptor",
            )
        try:
            present = ctypes.c_int()
            defaulted = ctypes.c_int()
            dacl = ctypes.c_void_p()
            if (
                not get_dacl(
                    descriptor,
                    ctypes.byref(present),
                    ctypes.byref(dacl),
                    ctypes.byref(defaulted),
                )
                or not present.value
                or not dacl.value
            ):
                error_number = _windows_last_error()
                raise OSError(error_number, "private Windows DACL is missing")
            yield descriptor, dacl
        finally:
            local_free(descriptor)

    def set_protected_dacl(self, handle: object, sddl: str) -> None:
        set_security = self.advapi32.SetSecurityInfo
        set_security.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        set_security.restype = ctypes.c_uint32
        with self._security_descriptor(sddl) as (_descriptor, dacl):
            result = set_security(
                handle,
                _SE_FILE_OBJECT,
                _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
            if result:
                raise OSError(int(result), "cannot restrict bound private Windows path")

    def read_dacl_sddl(self, handle: object) -> str:
        get_security = self.advapi32.GetSecurityInfo
        convert = self.advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        local_free = self.kernel32.LocalFree
        get_security.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        get_security.restype = ctypes.c_uint32
        convert.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        convert.restype = ctypes.c_int
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p

        descriptor = ctypes.c_void_p()
        result = get_security(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise OSError(int(result), "cannot read bound private Windows DACL")
        try:
            sddl = ctypes.c_void_p()
            length = ctypes.c_uint32()
            if not convert(
                descriptor,
                _SDDL_REVISION_1,
                _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
                ctypes.byref(sddl),
                ctypes.byref(length),
            ):
                error_number = _windows_last_error()
                raise OSError(error_number, "cannot format bound private Windows DACL")
            try:
                return ctypes.wstring_at(sddl)
            finally:
                local_free(sddl)
        finally:
            local_free(descriptor)

    def create_directory(self, path: Path, sddl: str) -> None:
        create_directory = self.kernel32.CreateDirectoryW
        create_directory.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(_WindowsSecurityAttributes),
        ]
        create_directory.restype = ctypes.c_int
        with self._security_descriptor(sddl) as (descriptor, _dacl):
            attributes = _WindowsSecurityAttributes(
                length=ctypes.sizeof(_WindowsSecurityAttributes),
                security_descriptor=descriptor,
                inherit_handle=0,
            )
            if create_directory(os.fspath(native_io_path(path)), ctypes.byref(attributes)):
                return
            error_number = _windows_last_error()
            if error_number == _ERROR_ALREADY_EXISTS:
                raise FileExistsError(error_number, "directory already exists")
            raise OSError(error_number, "cannot create private Windows directory")


def apply_windows_private_dacl(
    path: Path,
    *,
    directory: bool,
    expected_device: int,
    expected_inode: int,
    native: Any | None = None,
) -> None:
    """Install and verify a protected current-user-and-SYSTEM DACL."""

    api = native or _CtypesWindowsPrivateAcl()
    user_sid = api.current_user_sid()
    sddl = _private_windows_sddl(user_sid, directory=directory)
    with api.open_bound(
        path,
        directory=directory,
        expected_device=expected_device,
        expected_inode=expected_inode,
    ) as handle:
        api.set_protected_dacl(handle, sddl)
        actual = api.read_dacl_sddl(handle)
        if not _windows_sddl_is_private(
            actual,
            user_sid=user_sid,
            directory=directory,
            require_protected=True,
            canonicalize_sid=getattr(api, "canonical_sid", None),
        ):
            raise OSError(0, "bound Windows path did not retain a private DACL")


def create_windows_private_directory(
    path: Path,
    *,
    native: Any | None = None,
) -> None:
    """Create a directory with a protected inheritable DACL at publication."""

    api = native or _CtypesWindowsPrivateAcl()
    sddl = _private_windows_sddl(api.current_user_sid(), directory=True)
    api.create_directory(path, sddl)


def windows_path_has_private_dacl(
    path: Path,
    *,
    directory: bool,
    require_protected: bool,
    native: Any | None = None,
) -> bool:
    """Inspect a bound Windows path without trusting pathname-only ACL state."""

    if os.name != "nt" and native is None:
        raise OSError("Windows DACL inspection is unavailable on this platform")
    metadata = os.lstat(native_io_path(path))
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        return False
    api = native or _CtypesWindowsPrivateAcl()
    user_sid = api.current_user_sid()
    with api.open_bound(
        path,
        directory=directory,
        expected_device=int(metadata.st_dev),
        expected_inode=int(metadata.st_ino),
    ) as handle:
        return _windows_sddl_is_private(
            api.read_dacl_sddl(handle),
            user_sid=user_sid,
            directory=directory,
            require_protected=require_protected,
            canonicalize_sid=getattr(api, "canonical_sid", None),
        )
