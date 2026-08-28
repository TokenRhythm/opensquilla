"""Offline sandbox identity helpers for windows_default."""

# mypy: disable-error-code="attr-defined,arg-type"

from __future__ import annotations

import base64
import sys
from dataclasses import dataclass

LOGON32_LOGON_INTERACTIVE = 2
LOGON32_LOGON_BATCH = 4
LOGON32_PROVIDER_DEFAULT = 0


@dataclass(frozen=True)
class OfflineSandboxIdentity:
    sid: str
    username: str
    protected_password: str


def offline_identity_from_boundary(boundary: dict[str, object]) -> OfflineSandboxIdentity:
    sid = boundary.get("offlineUserSid")
    username = boundary.get("offlineUsername")
    protected_password = boundary.get("protectedPassword")
    if not isinstance(sid, str) or not sid:
        raise ValueError("windowsNetworkBoundary requires offlineUserSid")
    if not isinstance(username, str) or not username:
        raise ValueError("windowsNetworkBoundary requires offlineUsername")
    if not isinstance(protected_password, str) or not protected_password:
        raise ValueError("windowsNetworkBoundary requires protectedPassword")
    return OfflineSandboxIdentity(
        sid=sid,
        username=username,
        protected_password=protected_password,
    )


def logon_offline_identity(identity: OfflineSandboxIdentity) -> int:
    password = _unprotect_password(identity.protected_password)
    return _logon_user_native(identity.username, password)


def protect_password(password: str) -> str:
    return _protect_password_native(password)


def unprotect_password(protected_password: str) -> str:
    return _unprotect_password_native(protected_password)


def _unprotect_password(protected_password: str) -> str:
    return unprotect_password(protected_password)


def _protect_password_native(password: str) -> str:
    if not sys.platform.startswith("win"):
        raise OSError("offline_user_password_unavailable")
    raw = password.encode("utf-8")
    protected = _crypt_protect_data(raw)
    return base64.b64encode(protected).decode("ascii")


def _unprotect_password_native(protected_password: str) -> str:
    if not sys.platform.startswith("win"):
        raise OSError("offline_user_password_unavailable")
    try:
        raw = base64.b64decode(protected_password.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise OSError("offline_user_password_unavailable") from exc
    return _crypt_unprotect_data(raw).decode("utf-8")


def _crypt_protect_data(raw: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    kernel32.LocalFree.restype = wintypes.LPVOID

    in_buffer = ctypes.create_string_buffer(raw)
    in_blob = DataBlob(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = DataBlob()
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        code = ctypes.get_last_error()
        raise OSError(code, f"offline_user_password_unavailable: {ctypes.FormatError(code)}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)


def _crypt_unprotect_data(raw: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.LPVOID]
    kernel32.LocalFree.restype = wintypes.LPVOID

    in_buffer = ctypes.create_string_buffer(raw)
    in_blob = DataBlob(len(raw), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        code = ctypes.get_last_error()
        raise OSError(code, f"offline_user_password_unavailable: {ctypes.FormatError(code)}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)


def _logon_user_native(username: str, password: str) -> int:
    if not sys.platform.startswith("win"):
        raise OSError("process_launch_as_offline_user_failed")
    if not username or not password:
        raise OSError("process_launch_as_offline_user_failed")

    batch_token, batch_error = _try_logon_user(
        username,
        password,
        LOGON32_LOGON_BATCH,
    )
    if batch_token:
        return batch_token

    interactive_token, interactive_error = _try_logon_user(
        username,
        password,
        LOGON32_LOGON_INTERACTIVE,
    )
    if interactive_token:
        return interactive_token

    detail = (
        "process_launch_as_offline_user_failed: "
        f"batch={batch_error}; interactive={interactive_error}"
    )
    raise OSError(detail)


def _try_logon_user(
    username: str,
    password: str,
    logon_type: int,
) -> tuple[int | None, str]:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    handle_type = wintypes.HANDLE
    dword_type = wintypes.DWORD
    bool_type = wintypes.BOOL

    advapi32.LogonUserW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        dword_type,
        dword_type,
        ctypes.POINTER(handle_type),
    ]
    advapi32.LogonUserW.restype = bool_type

    token = handle_type()
    if advapi32.LogonUserW(
        username,
        ".",
        password,
        logon_type,
        LOGON32_PROVIDER_DEFAULT,
        ctypes.byref(token),
    ):
        return int(token.value), ""
    code = ctypes.get_last_error()
    return None, f"{code}: {ctypes.FormatError(code)}"


__all__ = [
    "OfflineSandboxIdentity",
    "LOGON32_LOGON_BATCH",
    "LOGON32_LOGON_INTERACTIVE",
    "LOGON32_PROVIDER_DEFAULT",
    "logon_offline_identity",
    "offline_identity_from_boundary",
    "protect_password",
    "unprotect_password",
]
