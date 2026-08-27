"""Idempotent direct-update migration for legacy sandbox state."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.lossless_toml import patch_import_config
from opensquilla.sandbox.legacy_codec import (
    LegacyModeContext,
    decode_legacy_config_mode,
    decode_legacy_run_mode,
)

MIGRATION_VERSION = 2
JOURNAL_NAME = ".sandbox-upgrade-v2.json"
SNAPSHOT_NAME = ".sandbox-upgrade-snapshot"
WINDOWS_ACL_STAGE_TIMEOUT_SECONDS = 30.0
_WINDOWS_IDENTITY_FALLBACK_TIMEOUT_SECONDS = 10.0
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_ERROR_INSUFFICIENT_BUFFER = 122
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1


_WINDOWS_PRIVATE_ACL_SCRIPT = r"""
$ErrorActionPreference = "Stop"
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8WithoutBom
$OutputEncoding = $utf8WithoutBom
$userSid = $env:OPENSQUILLA_UPGRADE_ACL_USER_SID
$allowed = @($userSid)
foreach ($trustedSid in @("S-1-5-18", "S-1-5-32-544")) {
    if ($allowed -notcontains $trustedSid) {
        $allowed += $trustedSid
    }
}
$raw = [Console]::In.ReadToEnd()
$parsed = $raw | ConvertFrom-Json
if ($null -eq $parsed) { throw "ACL batch is empty" }
$items = if ($parsed -is [System.Array]) { $parsed } else { @($parsed) }
$itemCount = if ($items -is [System.Array]) { $items.Length } else { 1 }
if ($itemCount -eq 0) { throw "ACL batch is empty" }
$verifiedIds = New-Object 'System.Collections.Generic.List[string]'
$verifiedPathUtf8Base64 = New-Object 'System.Collections.Generic.List[string]'
$verifiedPathHashes = New-Object 'System.Collections.Generic.List[string]'
$seenPaths = New-Object 'System.Collections.Generic.HashSet[string]'
$fullControl = [System.Security.AccessControl.FileSystemRights]::FullControl
foreach ($item in $items) {
    $target = [string]$item.path
    $itemId = [string]$item.id
    if ($null -eq $item.directory -or $item.directory.GetType().Name -ne "Boolean") {
        throw "ACL batch item directory flag is invalid"
    }
    $isDirectory = $item.directory
    if ([string]::IsNullOrWhiteSpace($target) -or [string]::IsNullOrWhiteSpace($itemId)) {
        throw "ACL batch item is invalid"
    }
    if (-not $seenPaths.Add($target)) { throw "ACL batch contains duplicate paths" }
    $attributes = [System.IO.File]::GetAttributes($target)
    if (($attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "snapshot path is a reparse point: $target"
    }
    $actualDirectory = ($attributes -band [System.IO.FileAttributes]::Directory) -ne 0
    if ($actualDirectory -ne $isDirectory) { throw "snapshot path type changed: $target" }
    $acl = if ($isDirectory) {
        [System.Security.AccessControl.DirectorySecurity]::new()
    } else {
        [System.Security.AccessControl.FileSecurity]::new()
    }
    $acl.SetAccessRuleProtection($true, $false)
    $inheritance = [System.Security.AccessControl.InheritanceFlags]::None
    if ($isDirectory) {
        $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    $propagation = [System.Security.AccessControl.PropagationFlags]::None
    foreach ($sidText in $allowed) {
        $sid = [System.Security.Principal.SecurityIdentifier]::new($sidText)
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            $fullControl,
            $inheritance,
            $propagation,
            [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        $verified = Get-Acl -LiteralPath $target
        $needsSet = -not $verified.AreAccessRulesProtected
        $initialRules = @($verified.Access)
        if ($initialRules.Count -ne $allowed.Count) { $needsSet = $true }
        foreach ($rule in $initialRules) {
            $identity = $rule.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
            if ($allowed -notcontains $identity -or
                $rule.AccessControlType -ne
                    [System.Security.AccessControl.AccessControlType]::Allow -or
                $rule.IsInherited -or
                $rule.FileSystemRights -ne $fullControl -or
                $rule.InheritanceFlags -ne $inheritance -or
                $rule.PropagationFlags -ne $propagation) {
                $needsSet = $true
                break
            }
        }
        if ($needsSet) {
            $inheritanceSddl = if ($isDirectory) { "OICI" } else { "" }
            $sddl = "D:P" + (($allowed | ForEach-Object {
                "(A;$inheritanceSddl;FA;;;$_)"
            }) -join "")
            $acl = $verified
            $acl.SetSecurityDescriptorSddlForm($sddl)
            Set-Acl -LiteralPath $target -AclObject $acl
            $verified = Get-Acl -LiteralPath $target
        }
    } elseif ($isDirectory) {
        [System.IO.Directory]::SetAccessControl($target, $acl)
        $verified = [System.IO.Directory]::GetAccessControl($target)
    } else {
        [System.IO.File]::SetAccessControl($target, $acl)
        $verified = [System.IO.File]::GetAccessControl($target)
    }
    $postAttributes = [System.IO.File]::GetAttributes($target)
    if (($postAttributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "snapshot path became a reparse point: $target"
    }
    if ((($postAttributes -band [System.IO.FileAttributes]::Directory) -ne 0) -ne $isDirectory) {
        throw "snapshot path type changed after ACL update: $target"
    }
    if (-not $verified.AreAccessRulesProtected) { throw "DACL inheritance remains enabled" }
    $rules = @($verified.Access)
    if ($rules.Count -ne $allowed.Count) { throw "DACL contains an unexpected rule count" }
    foreach ($rule in $rules) {
        $identity = $rule.IdentityReference.Translate(
            [System.Security.Principal.SecurityIdentifier]
        ).Value
        if ($allowed -notcontains $identity -or
            $rule.AccessControlType -ne
                [System.Security.AccessControl.AccessControlType]::Allow -or
            $rule.IsInherited -or
            $rule.FileSystemRights -ne $fullControl -or
            $rule.InheritanceFlags -ne $inheritance -or
            $rule.PropagationFlags -ne $propagation) {
            throw "DACL verification failed"
        }
    }
    foreach ($sidText in $allowed) {
        $matchCount = 0
        foreach ($rule in $rules) {
            $identity = $rule.IdentityReference.Translate(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
            if ($identity -eq $sidText) { $matchCount += 1 }
        }
        if ($matchCount -ne 1) { throw "DACL principal verification failed" }
    }
    [void]$verifiedIds.Add($itemId)
    $pathBytes = [System.Text.Encoding]::UTF8.GetBytes($target)
    [void]$verifiedPathUtf8Base64.Add(
        [System.Convert]::ToBase64String($pathBytes)
    )
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash(
        $pathBytes
    )
    $pathHash = (
        [System.BitConverter]::ToString($hash) -replace "-", ""
    ).ToLowerInvariant()
    [void]$verifiedPathHashes.Add($pathHash)
}
$resultJson = [ordered]@{
    count = $verifiedIds.Count
    ids = $verifiedIds.ToArray()
    pathUtf8Base64 = $verifiedPathUtf8Base64.ToArray()
    pathHashes = $verifiedPathHashes.ToArray()
} | ConvertTo-Json -Compress
[Console]::Out.WriteLine($resultJson)
"""
_WINDOWS_PRIVATE_ACL_ENCODED = base64.b64encode(
    _WINDOWS_PRIVATE_ACL_SCRIPT.encode("utf-16-le")
).decode("ascii")
_WINDOWS_DLL_DIRECTORY_LOCK = threading.Lock()


def _running_on_windows() -> bool:
    return os.name == "nt"


def _set_windows_dll_directory(path: str | None) -> None:
    import ctypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    setter = kernel32.SetDllDirectoryW
    setter.argtypes = [ctypes.c_wchar_p]
    setter.restype = ctypes.c_bool
    if not setter(path):
        error_code = int(getattr(ctypes, "get_last_error")())
        raise OSError(error_code, "SetDllDirectoryW failed")


@contextmanager
def _system_windows_process_context() -> Iterator[None]:
    bundle_root = getattr(sys, "_MEIPASS", None)
    if not _running_on_windows() or not getattr(sys, "frozen", False) or not bundle_root:
        yield
        return

    with _WINDOWS_DLL_DIRECTORY_LOCK:
        _set_windows_dll_directory(None)
        try:
            yield
        finally:
            _set_windows_dll_directory(str(bundle_root))


def _native_windows_user_sid() -> str:
    import ctypes

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", ctypes.c_uint32)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    loader = getattr(ctypes, "WinDLL")
    advapi32 = loader("advapi32", use_last_error=True)
    kernel32 = loader("kernel32", use_last_error=True)
    open_token = advapi32.OpenProcessToken
    get_token = advapi32.GetTokenInformation
    convert_sid = advapi32.ConvertSidToStringSidW
    get_process = kernel32.GetCurrentProcess
    close_handle = kernel32.CloseHandle
    local_free = kernel32.LocalFree
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
        error_number = int(getattr(ctypes, "get_last_error")())
        raise OSError(error_number, "cannot open the current Windows process token")
    try:
        required = ctypes.c_uint32()
        get_token(token, _TOKEN_USER, None, 0, ctypes.byref(required))
        error_number = int(getattr(ctypes, "get_last_error")())
        if required.value == 0 or error_number != _ERROR_INSUFFICIENT_BUFFER:
            raise OSError(error_number, "cannot size the current Windows token user")
        buffer = ctypes.create_string_buffer(required.value)
        if not get_token(
            token,
            _TOKEN_USER,
            buffer,
            required.value,
            ctypes.byref(required),
        ):
            error_number = int(getattr(ctypes, "get_last_error")())
            raise OSError(error_number, "cannot read the current Windows token user")
        token_user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        string_sid = ctypes.c_void_p()
        if not convert_sid(token_user.user.sid, ctypes.byref(string_sid)):
            error_number = int(getattr(ctypes, "get_last_error")())
            raise OSError(error_number, "cannot format the current Windows user SID")
        try:
            sid = ctypes.wstring_at(string_sid)
        finally:
            local_free(string_sid)
    finally:
        close_handle(token)
    if not sid.startswith("S-"):
        raise OSError("cannot resolve the current Windows user SID")
    return sid


def _current_windows_user_sid(*, deadline: float | None = None) -> str:
    try:
        with _system_windows_process_context():
            return _native_windows_user_sid()
    except (AttributeError, ImportError, OSError):
        remaining = (
            WINDOWS_ACL_STAGE_TIMEOUT_SECONDS
            if deadline is None
            else deadline - time.monotonic()
        )
        if remaining <= 0:
            raise TimeoutError("Windows ACL migration timed out resolving the current user SID")
        try:
            with _system_windows_process_context():
                completed = subprocess.run(
                    ["whoami", "/user", "/fo", "csv", "/nh"],
                    capture_output=True,
                    check=False,
                    timeout=min(_WINDOWS_IDENTITY_FALLBACK_TIMEOUT_SECONDS, remaining),
                    stdin=subprocess.DEVNULL,
                    creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                "Windows ACL migration timed out resolving the current user SID"
            ) from exc
        if completed.returncode != 0:
            raise OSError("cannot resolve the current Windows user SID")
        stdout = completed.stdout
        if not isinstance(stdout, bytes):
            raise OSError("cannot resolve the current Windows user SID")
        sid_matches: list[bytes] = re.findall(
            rb"(?<![A-Za-z0-9-])S-\d+(?:-\d+){2,}(?![A-Za-z0-9-])",
            stdout,
        )
        if len(sid_matches) != 1:
            raise OSError("cannot resolve the current Windows user SID")
        return sid_matches[0].decode("ascii")


def _remaining_windows_acl_time(deadline: float | None) -> float:
    if deadline is None:
        return WINDOWS_ACL_STAGE_TIMEOUT_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Windows ACL migration stage timed out")
    return remaining


def _path_is_directory(path: Path) -> bool:
    value = path.lstat()
    attributes = int(getattr(value, "st_file_attributes", 0))
    if stat.S_ISLNK(value.st_mode) or attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError(f"snapshot path has an unsafe type: {path}")
    if stat.S_ISDIR(value.st_mode):
        return True
    if stat.S_ISREG(value.st_mode):
        return False
    raise OSError(f"snapshot path has an unsafe type: {path}")

def _path_exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _private_tree_entries(
    root: Path,
    *,
    deadline: float | None = None,
) -> tuple[tuple[Path, bool], ...]:
    entries: list[tuple[Path, bool]] = []

    def visit(path: Path, *, expected_directory: bool | None = None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("Windows ACL migration stage timed out")
        is_directory = _path_is_directory(path)
        if expected_directory is not None and is_directory != expected_directory:
            raise OSError(f"snapshot path type changed: {path}")
        entries.append((path, is_directory))
        if not is_directory:
            return
        with os.scandir(path) as iterator:
            children = sorted(iterator, key=lambda item: item.name.casefold())
        for child in children:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Windows ACL migration stage timed out")
            child_path = Path(child.path)
            child_attributes = int(
                getattr(child.stat(follow_symlinks=False), "st_file_attributes", 0)
            )
            if child_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise OSError(f"snapshot path has an unsafe type: {child_path}")
            visit(child_path)

    if not _path_is_directory(root):
        raise OSError(f"snapshot root is not a directory: {root}")
    visit(root)
    return tuple(sorted(entries, key=lambda item: item[0].as_posix().casefold()))


def _acl_batch_payload(entries: tuple[tuple[Path, bool], ...]) -> str:
    return json.dumps(
        [
            {"id": str(index), "path": str(path), "directory": directory}
            for index, (path, directory) in enumerate(entries)
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _acl_path_hash(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _windows_powershell_exe() -> str | None:
    """Return the absolute path to Windows PowerShell 5.1, or ``None``.

    The ACL migration script depends on .NET Framework-only APIs such as
    ``[System.IO.Directory]::SetAccessControl`` and
    ``[System.IO.File]::SetAccessControl``, which do not exist in PowerShell 7
    (.NET Core / ``pwsh``). ``pwsh`` frequently shadows a bare ``powershell``
    PATH lookup on hosts that install it, which is exactly the failure this
    guards against. Instead of trusting PATH, pin the well-known Windows
    PowerShell install locations. Those canonical paths are always Windows
    PowerShell 5.1 on a supported Windows host, so returning them is itself the
    verification that we invoke 5.1 rather than a PATH-resolved ``pwsh``.

    Returns ``None`` only when neither known location exists, so callers fail
    loudly instead of silently invoking an unverified PATH binary.
    """
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidates = (
        # SysNative is the Wow64 redirector view used by 32-bit processes to
        # reach the native 64-bit System32 directory.
        os.path.join(system_root, "SysNative", "WindowsPowerShell", "v1.0", "powershell.exe"),
        os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
    )
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _windows_acl_shells() -> tuple[str, ...]:
    """Resolve the PowerShell shells used for Windows ACL hardening.

    Windows PowerShell 5.1 (the .NET Framework build) is required for the
    ``SetAccessControl`` APIs the ACL script depends on, so it is preferred.
    ``pwsh`` (.NET Core) can only be a last-resort fallback: it will usually
    fail the script's verification, and that failure is surfaced rather than
    masked. If neither a trusted Windows PowerShell 5.1 nor ``pwsh`` is
    available, fail explicitly so the missing dependency is diagnosable instead
    of being hidden behind a PATH-resolved binary that may not be 5.1.
    """
    preferred = _windows_powershell_exe()
    fallback = shutil.which("pwsh")
    shells = tuple(item for item in (preferred, fallback) if item is not None)
    if not shells:
        raise RuntimeError(
            "Windows PowerShell 5.1 was not found at the expected system "
            "locations (System32/SysNative WindowsPowerShell v1.0) and no pwsh "
            "fallback is on PATH; cannot harden upgrade snapshot ACLs."
        )
    return shells


def _protect_windows_acl_batch(
    entries: tuple[tuple[Path, bool], ...],
    *,
    windows_user_sid: str,
    deadline: float | None = None,
) -> None:
    if not entries:
        raise OSError("ACL batch is empty")
    normalized_paths = [os.path.normcase(os.path.abspath(str(path))) for path, _ in entries]
    if len(set(normalized_paths)) != len(normalized_paths):
        raise OSError("ACL batch contains duplicate paths")
    for path, directory in entries:
        _remaining_windows_acl_time(deadline)
        if _path_is_directory(path) != directory:
            raise OSError(f"snapshot path type changed: {path}")
    environment = {
        **os.environ,
        "OPENSQUILLA_UPGRADE_ACL_USER_SID": windows_user_sid,
    }
    completed: subprocess.CompletedProcess[bytes] | None = None
    shells = _windows_acl_shells()
    for shell_index, shell in enumerate(shells):
        try:
            with _system_windows_process_context():
                completed = subprocess.run(
                    [
                        shell,
                        "-NoProfile",
                        "-NonInteractive",
                        "-NoLogo",
                        "-EncodedCommand",
                        _WINDOWS_PRIVATE_ACL_ENCODED,
                    ],
                    env=environment,
                    input=_acl_batch_payload(entries).encode("ascii"),
                    capture_output=True,
                    check=False,
                    timeout=_remaining_windows_acl_time(deadline),
                    creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Windows ACL migration stage timed out") from exc
        if completed is None:
            raise OSError("cannot start Windows ACL helper")
        if completed.returncode == 0:
            break
        detail = " ".join(
            (completed.stderr or completed.stdout or b"")
            .decode("utf-8", errors="replace")
            .strip()
            .split()
        )
        if (
            shell_index + 1 >= len(shells)
            or "securityprivilege" not in detail.casefold()
        ):
            break
    if completed is None:
        raise OSError("cannot start Windows ACL helper")
    if completed.returncode != 0:
        detail = " ".join(
            (completed.stderr or completed.stdout or b"")
            .decode("utf-8", errors="replace")
            .strip()
            .split()
        )
        suffix = f" ({detail[-500:]})" if detail else ""
        raise OSError(f"cannot protect upgrade snapshot path ACL batch{suffix}")
    try:
        result = json.loads(completed.stdout.decode("ascii"))
        if not isinstance(result, dict):
            raise TypeError("ACL helper result is not an object")
        ids = result["ids"]
        encoded_paths = result["pathUtf8Base64"]
        path_hashes = result["pathHashes"]
        count = result["count"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not isinstance(ids, list)
            or not all(isinstance(item, str) for item in ids)
            or not isinstance(encoded_paths, list)
            or not all(isinstance(item, str) for item in encoded_paths)
            or not isinstance(path_hashes, list)
            or not all(isinstance(item, str) for item in path_hashes)
        ):
            raise TypeError("ACL helper result has invalid field types")
        paths = [
            base64.b64decode(item, validate=True).decode("utf-8")
            for item in encoded_paths
        ]
    except (
        AttributeError,
        binascii.Error,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ) as exc:
        raise OSError("Windows ACL helper returned an invalid verification result") from exc
    expected_ids = [str(index) for index in range(len(entries))]
    expected_paths = [str(path) for path, _ in entries]
    expected_hashes = [_acl_path_hash(path) for path, _ in entries]
    if (
        count != len(expected_ids)
        or ids != expected_ids
        or paths != expected_paths
        or path_hashes != expected_hashes
    ):
        raise OSError("Windows ACL helper did not verify the requested paths exactly")
    for path, directory in entries:
        _remaining_windows_acl_time(deadline)
        if _path_is_directory(path) != directory:
            raise OSError(f"snapshot path changed after ACL verification: {path}")


def _protect_private_path(
    path: Path,
    *,
    directory: bool,
    windows_user_sid: str | None,
    deadline: float | None = None,
) -> None:
    if _path_is_directory(path) != directory:
        raise OSError(f"snapshot path has an unsafe type: {path}")

    if _running_on_windows():
        if windows_user_sid is None:
            raise OSError("current Windows user SID is unavailable")
        try:
            _protect_windows_acl_batch(
                ((path, directory),),
                windows_user_sid=windows_user_sid,
                deadline=deadline,
            )
        except TimeoutError as exc:
            raise OSError(f"Windows ACL hardening timed out: {path}") from exc
        return

    mode = 0o700 if directory else 0o600
    path.chmod(mode)
    if stat.S_IMODE(path.stat().st_mode) != mode:
        raise OSError(f"cannot protect upgrade snapshot path: {path}")


def _create_private_directory(
    path: Path,
    *,
    windows_user_sid: str | None,
    protect: bool = True,
    deadline: float | None = None,
) -> None:
    path.mkdir(mode=0o777 if _running_on_windows() else 0o700)
    if protect:
        _protect_private_path(
            path,
            directory=True,
            windows_user_sid=windows_user_sid,
            deadline=deadline,
        )


def _copy_private_file(
    source: Path,
    destination: Path,
    *,
    windows_user_sid: str | None,
    protect: bool = True,
    deadline: float | None = None,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(destination, flags, 0o600)
    try:
        with source.open("rb") as source_handle, os.fdopen(descriptor, "wb") as target_handle:
            descriptor = -1
            shutil.copyfileobj(source_handle, target_handle)
            target_handle.flush()
            os.fsync(target_handle.fileno())
            if not _running_on_windows():
                os.fchmod(target_handle.fileno(), 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if protect:
        _protect_private_path(
            destination,
            directory=False,
            windows_user_sid=windows_user_sid,
            deadline=deadline,
        )


def _protect_private_tree(
    root: Path,
    *,
    windows_user_sid: str | None,
    deadline: float | None = None,
) -> None:
    entries = _private_tree_entries(root, deadline=deadline)
    if _running_on_windows():
        if windows_user_sid is None:
            raise OSError("current Windows user SID is unavailable")
        _protect_windows_acl_batch(
            entries,
            windows_user_sid=windows_user_sid,
            deadline=deadline,
        )
    else:
        for path, directory in entries:
            _protect_private_path(
                path,
                directory=directory,
                windows_user_sid=windows_user_sid,
                deadline=deadline,
            )
    after = _private_tree_entries(root, deadline=deadline)
    before_keys = tuple((str(path), directory) for path, directory in entries)
    after_keys = tuple((str(path), directory) for path, directory in after)
    if before_keys != after_keys:
        raise OSError("snapshot tree changed during ACL migration")


def _remove_failed_snapshot(path: Path, *, original_error: Exception) -> None:
    try:
        if path.exists():
            shutil.rmtree(path)
        if path.exists():
            raise OSError("path still exists after cleanup")
    except Exception:
        raise OSError(f"upgrade snapshot failed and cleanup failed: {path}") from original_error


def _make_snapshot_staging_directory(home: Path) -> Path:
    if not _running_on_windows():
        return Path(
            tempfile.mkdtemp(
                prefix=f".{SNAPSHOT_NAME}.",
                suffix=".tmp",
                dir=home,
            )
        )
    for _ in range(100):
        candidate = home / f".{SNAPSHOT_NAME}.{uuid.uuid4().hex}.tmp"
        try:
            # Let the profile parent provide the initial Windows DACL.  The
            # helper hardens this empty directory before any snapshot bytes
            # are written and rejects any injected entry after hardening.
            candidate.mkdir(mode=0o777)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("cannot create a unique upgrade snapshot staging directory")


@dataclass(frozen=True)
class UpgradeMigrationReport:
    ok: bool
    status: str
    canonical_mode: str | None
    journal_path: Path
    snapshot_path: Path | None
    stores: tuple[str, ...]
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "canonicalMode": self.canonical_mode,
            "journalPath": str(self.journal_path),
            "snapshotPath": str(self.snapshot_path) if self.snapshot_path else None,
            "stores": list(self.stores),
            "error": self.error,
        }


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n",
    )


def _store_candidates(home: Path) -> tuple[Path, ...]:
    candidates = [
        home / "config.toml",
        home / "desktop-preferences.json",
        home / "preferences.json",
        home / "sessions.db",
        home / "state" / "sessions.db",
        home / "data" / "sessions.db",
    ]
    return tuple(path for path in candidates if path.is_file())


def inventory_sandbox_stores(home: str | Path) -> tuple[Path, ...]:
    root = Path(home).expanduser().absolute()
    return _store_candidates(root)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _config_mode(payload: dict[str, Any]):
    sandbox = payload.get("sandbox")
    sandbox_table = sandbox if isinstance(sandbox, dict) else {}
    permissions = payload.get("permissions")
    permissions_table = permissions if isinstance(permissions, dict) else {}
    arguments: dict[str, object] = {}
    if "run_mode" in sandbox_table:
        arguments["run_mode"] = sandbox_table["run_mode"]
    if "default_mode" in permissions_table:
        arguments["permissions_default_mode"] = permissions_table["default_mode"]
    if "sandbox" in sandbox_table:
        arguments["sandbox_enabled"] = sandbox_table["sandbox"]
    elif "enabled" in sandbox_table:
        arguments["sandbox_enabled"] = sandbox_table["enabled"]
    if "security_grading" in sandbox_table:
        arguments["grading_enabled"] = sandbox_table["security_grading"]
    return decode_legacy_config_mode(**arguments)


def lossless_patch_sandbox_fields(raw: bytes) -> tuple[bytes, str]:
    original = tomllib.loads(raw.decode("utf-8"))
    transformed = json.loads(json.dumps(original))
    mode = _config_mode(original)
    sandbox = transformed.setdefault("sandbox", {})
    if not isinstance(sandbox, dict):
        raise ValueError("sandbox config must be a table")
    sandbox["run_mode"] = mode.value
    patched = patch_import_config(raw, original, transformed)
    return patched, mode.value


def _canonicalize_preferences(value: Any) -> Any:
    if isinstance(value, list):
        return [_canonicalize_preferences(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, child in value.items():
        if key in {"runMode", "run_mode", "sandboxMode", "sandbox_mode"} and isinstance(
            child, str
        ):
            result[key] = decode_legacy_run_mode(
                child,
                context=LegacyModeContext.STORED_EVENT,
            ).value
        else:
            result[key] = _canonicalize_preferences(child)
    return result


class SandboxUpgradeCoordinator:
    def __init__(self, home: str | Path) -> None:
        self.home = Path(home).expanduser().absolute()
        self.journal_path = self.home / JOURNAL_NAME
        self.snapshot_path = self.home / SNAPSHOT_NAME

    def _load_journal(self) -> dict[str, Any] | None:
        if not self.journal_path.exists():
            return None
        payload = json.loads(self.journal_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("migrationVersion") != MIGRATION_VERSION:
            raise ValueError("unsupported sandbox upgrade journal")
        return payload

    def _manual_recovery_report(
        self,
        *,
        store_names: tuple[str, ...],
        error: Exception,
    ) -> UpgradeMigrationReport:
        failed = {
            "migrationVersion": MIGRATION_VERSION,
            "status": "prepared",
            "stores": store_names,
            "snapshot": str(self.snapshot_path),
            "error": f"{type(error).__name__}: {error}",
        }
        _write_json(self.journal_path, failed)
        return UpgradeMigrationReport(
            ok=False,
            status="manual_recovery_required",
            canonical_mode=None,
            journal_path=self.journal_path,
            snapshot_path=(
                self.snapshot_path if _path_exists_no_follow(self.snapshot_path) else None
            ),
            stores=store_names,
            error=str(failed["error"]),
        )

    def _snapshot(self, stores: tuple[Path, ...]) -> None:
        deadline = (
            time.monotonic() + WINDOWS_ACL_STAGE_TIMEOUT_SECONDS
            if _running_on_windows()
            else None
        )
        windows_user_sid = (
            _current_windows_user_sid(deadline=deadline) if _running_on_windows() else None
        )
        if _path_exists_no_follow(self.snapshot_path):
            _protect_private_tree(
                self.snapshot_path,
                windows_user_sid=windows_user_sid,
                deadline=deadline,
            )
            return
        staging = _make_snapshot_staging_directory(self.home)
        promoted = False
        try:
            _protect_private_path(
                staging,
                directory=True,
                windows_user_sid=windows_user_sid,
                deadline=deadline,
            )
            if next(staging.iterdir(), None) is not None:
                raise OSError("upgrade snapshot staging is not empty after hardening")
            materialization_started: float | None = None
            if deadline is not None:
                _remaining_windows_acl_time(deadline)
                materialization_started = time.monotonic()
            try:
                manifest: list[dict[str, object]] = []
                for source in stores:
                    relative = source.relative_to(self.home)
                    destination = staging / relative
                    current = staging
                    for part in relative.parts[:-1]:
                        current = current / part
                        if not current.exists():
                            _create_private_directory(
                                current,
                                windows_user_sid=windows_user_sid,
                                protect=False,
                                deadline=deadline,
                            )
                    _copy_private_file(
                        source,
                        destination,
                        windows_user_sid=windows_user_sid,
                        protect=False,
                        deadline=deadline,
                    )
                    manifest.append(
                        {
                            "path": relative.as_posix(),
                            "sha256": _digest(destination),
                            "size": destination.stat().st_size,
                        }
                    )
                manifest_path = staging / "manifest.json"
                _write_json(manifest_path, {"stores": manifest})
            finally:
                if deadline is not None and materialization_started is not None:
                    deadline += time.monotonic() - materialization_started
            _protect_private_tree(
                staging,
                windows_user_sid=windows_user_sid,
                deadline=deadline,
            )
            os.replace(staging, self.snapshot_path)
            promoted = True
            _protect_private_tree(
                self.snapshot_path,
                windows_user_sid=windows_user_sid,
                deadline=deadline,
            )
        except Exception as exc:
            cleanup = self.snapshot_path if promoted else staging
            _remove_failed_snapshot(cleanup, original_error=exc)
            raise

    def run(self) -> UpgradeMigrationReport:
        from opensquilla.recovery.locking import acquire_profile_locks

        self.home.mkdir(parents=True, exist_ok=True)
        with acquire_profile_locks(self.home, timeout=30.0):
            return self._run_locked()

    def _run_locked(self) -> UpgradeMigrationReport:
        self.home.mkdir(parents=True, exist_ok=True)
        stores = inventory_sandbox_stores(self.home)
        store_names = tuple(path.relative_to(self.home).as_posix() for path in stores)
        journal = self._load_journal()
        if journal is not None and journal.get("status") == "committed":
            try:
                if not _path_exists_no_follow(self.snapshot_path):
                    raise OSError("committed sandbox upgrade snapshot is missing")
                self._snapshot(())
            except Exception as exc:
                return self._manual_recovery_report(
                    store_names=store_names,
                    error=exc,
                )
            return UpgradeMigrationReport(
                ok=True,
                status="committed",
                canonical_mode=journal.get("canonicalMode"),
                journal_path=self.journal_path,
                snapshot_path=self.snapshot_path,
                stores=store_names,
            )
        try:
            self._snapshot(stores)
            prepared = {
                "migrationVersion": MIGRATION_VERSION,
                "status": "prepared",
                "preparedAt": int(time.time()),
                "stores": store_names,
                "snapshot": str(self.snapshot_path),
            }
            _write_json(self.journal_path, prepared)
            canonical_mode: str | None = None
            config_path = self.home / "config.toml"
            if config_path.is_file():
                patched, canonical_mode = lossless_patch_sandbox_fields(
                    config_path.read_bytes()
                )
                if patched != config_path.read_bytes():
                    _atomic_write(config_path, patched)
            for name in ("desktop-preferences.json", "preferences.json"):
                preference_path = self.home / name
                if not preference_path.is_file():
                    continue
                original = json.loads(preference_path.read_text(encoding="utf-8"))
                transformed = _canonicalize_preferences(original)
                if transformed != original:
                    _write_json(preference_path, transformed)
            committed = {
                **prepared,
                "status": "committed",
                "committedAt": int(time.time()),
                "canonicalMode": canonical_mode,
            }
            _write_json(self.journal_path, committed)
            return UpgradeMigrationReport(
                ok=True,
                status="committed",
                canonical_mode=canonical_mode,
                journal_path=self.journal_path,
                snapshot_path=self.snapshot_path,
                stores=store_names,
            )
        except Exception as exc:
            return self._manual_recovery_report(
                store_names=store_names,
                error=exc,
            )


def ensure_sandbox_upgrade_migrated(home: str | Path) -> UpgradeMigrationReport:
    return SandboxUpgradeCoordinator(home).run()


def inspect_sandbox_upgrade(home: str | Path) -> UpgradeMigrationReport:
    coordinator = SandboxUpgradeCoordinator(home)
    try:
        journal = coordinator._load_journal()
    except Exception as exc:
        return UpgradeMigrationReport(
            ok=False,
            status="manual_recovery_required",
            canonical_mode=None,
            journal_path=coordinator.journal_path,
            snapshot_path=(
                coordinator.snapshot_path
                if _path_exists_no_follow(coordinator.snapshot_path)
                else None
            ),
            stores=(),
            error=f"{type(exc).__name__}: {exc}",
        )
    if journal is None:
        return UpgradeMigrationReport(
            ok=True,
            status="not_started",
            canonical_mode=None,
            journal_path=coordinator.journal_path,
            snapshot_path=None,
            stores=(),
        )
    return UpgradeMigrationReport(
        ok=journal.get("status") == "committed",
        status=str(journal.get("status") or "manual_recovery_required"),
        canonical_mode=journal.get("canonicalMode"),
        journal_path=coordinator.journal_path,
        snapshot_path=(
            coordinator.snapshot_path
            if _path_exists_no_follow(coordinator.snapshot_path)
            else None
        ),
        stores=tuple(str(item) for item in journal.get("stores", ())),
        error=journal.get("error"),
    )


__all__ = [
    "SandboxUpgradeCoordinator",
    "UpgradeMigrationReport",
    "ensure_sandbox_upgrade_migrated",
    "inspect_sandbox_upgrade",
    "inventory_sandbox_stores",
    "lossless_patch_sandbox_fields",
]