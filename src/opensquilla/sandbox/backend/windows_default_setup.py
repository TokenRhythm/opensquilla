"""Setup state for the Windows default sandbox."""

# mypy: disable-error-code="attr-defined"

from __future__ import annotations

import csv
import hashlib
import json
import os
import secrets
import string
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opensquilla.sandbox.backend.windows_default_network import WindowsNetworkSetup

SETUP_VERSION = 2
OFFLINE_USERNAME = "OpenSquillaSandbox"
SETUP_HELPER_REPORT = "setup_helper_report.json"
SETUP_PROCESS_LOCK_TIMEOUT_MS = 10 * 60 * 1000


@dataclass(frozen=True)
class WindowsDefaultSetupMarker:
    setup_version: int
    network: WindowsNetworkSetup | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {"setupVersion": self.setup_version}
        if self.network is not None:
            payload["network"] = self.network.to_json()
        return payload


@dataclass(frozen=True)
class _SetupDirectoryLease:
    marker_path: Path
    profile_path: Path
    roots: tuple[Path, ...]
    handles: tuple[object, ...]


def default_setup_marker_path(home: Path | None = None) -> Path:
    if home is not None:
        # An explicit ``home`` retains the support-probe contract: callers are
        # asking about the legacy sandbox state below that Windows user home.
        root = home / ".opensquilla"
    else:
        # Runtime marker state is profile-scoped. In particular, Desktop sets
        # OPENSQUILLA_STATE_DIR to its active profile; falling back to
        # Path.home() here would mutate a CLI profile selected as an import
        # source and violate the migration source-read-only guarantee.
        from opensquilla.paths import default_opensquilla_home

        root = default_opensquilla_home()
    return root / "sandbox" / "setup_marker.json"


def write_setup_marker(
    path: Path,
    *,
    setup_version: int = SETUP_VERSION,
    network: WindowsNetworkSetup | None = None,
) -> None:
    marker = WindowsDefaultSetupMarker(setup_version=setup_version, network=network)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic_no_follow(path, marker.to_json())


def read_setup_marker(path: Path) -> WindowsDefaultSetupMarker | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    version = raw.get("setupVersion")
    if not isinstance(version, int):
        return None
    network = _network_setup_from_json(raw.get("network"))
    return WindowsDefaultSetupMarker(setup_version=version, network=network)


def _network_setup_from_json(raw: object) -> WindowsNetworkSetup | None:
    if not isinstance(raw, dict):
        return None
    sid = raw.get("offlineUserSid")
    ports = raw.get("allowedProxyPorts")
    allow_local_binding = raw.get("allowLocalBinding")
    firewall_version = raw.get("firewallRuleVersion")
    wfp_version = raw.get("wfpRuleVersion")
    network_version = raw.get("networkSetupVersion", 1)
    offline_username = raw.get("offlineUsername")
    protected_password = raw.get("protectedPassword")
    if not isinstance(sid, str) or not sid:
        return None
    if not isinstance(ports, list) or not all(isinstance(port, int) for port in ports):
        return None
    if not isinstance(allow_local_binding, bool):
        return None
    if not isinstance(firewall_version, int) or not isinstance(wfp_version, int):
        return None
    if not isinstance(network_version, int):
        return None
    return WindowsNetworkSetup(
        offline_user_sid=sid,
        allowed_proxy_ports=tuple(sorted(set(ports))),
        allow_local_binding=allow_local_binding,
        firewall_rule_version=firewall_version,
        wfp_rule_version=wfp_version,
        offline_username=offline_username if isinstance(offline_username, str) else None,
        protected_password=protected_password if isinstance(protected_password, str) else None,
        network_setup_version=network_version,
    )


def setup_marker_is_current(path: Path) -> bool:
    marker = read_setup_marker(path)
    return marker is not None and marker.setup_version == SETUP_VERSION


def setup_marker_proxy_allowlist_ready(path: Path, *, ports: tuple[int, ...]) -> bool:
    marker = read_setup_marker(path)
    if marker is None or marker.setup_version != SETUP_VERSION:
        return False
    if marker.network is None:
        return False
    return marker.network.is_current_for_ports(ports)


def setup_payload(path: Path) -> dict[str, Any]:
    return {
        "setupVersion": SETUP_VERSION,
        "markerPath": str(path),
        "sandboxStateRoot": str(path.parent),
        "sandboxSecretsRoot": str(path.parent.parent / "sandbox-secrets"),
        "sandboxBinRoot": str(path.parent.parent / "sandbox-bin"),
    }


def setup_helper_report_path(marker_path: Path) -> Path:
    return marker_path.parent / SETUP_HELPER_REPORT


def write_setup_helper_report(
    marker_path: Path,
    *,
    state: str,
    detail: str | None = None,
) -> None:
    report: dict[str, object] = {"state": state}
    if detail:
        report["detail"] = detail
    path = setup_helper_report_path(marker_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic_no_follow(path, report)


def read_setup_helper_report(marker_path: Path) -> dict[str, str] | None:
    try:
        raw = json.loads(setup_helper_report_path(marker_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    if not isinstance(state, str) or not state:
        return None
    detail = raw.get("detail")
    report = {"state": state}
    if isinstance(detail, str) and detail:
        report["detail"] = detail
    return report


def establish_windows_network_setup(path: Path) -> WindowsNetworkSetup:
    from opensquilla.sandbox.backend.windows_default_firewall import (
        firewall_rule_specs,
        install_firewall_rules,
    )
    from opensquilla.sandbox.backend.windows_default_network import (
        FIREWALL_RULE_VERSION,
        WFP_RULE_VERSION,
    )
    from opensquilla.sandbox.backend.windows_default_wfp import install_wfp_filters_for_user

    identity = ensure_offline_sandbox_user(path.parent)
    allowed_ports = (48123,)
    rules = firewall_rule_specs(
        offline_sid=identity["sid"],
        allowed_proxy_ports=allowed_ports,
        allow_local_binding=False,
    )
    install_firewall_rules(rules)
    install_wfp_filters_for_user(identity["sid"], allowed_proxy_ports=allowed_ports)
    return WindowsNetworkSetup(
        offline_user_sid=identity["sid"],
        allowed_proxy_ports=allowed_ports,
        allow_local_binding=False,
        firewall_rule_version=FIREWALL_RULE_VERSION,
        wfp_rule_version=WFP_RULE_VERSION,
        offline_username=identity["username"],
        protected_password=identity["protectedPassword"],
    )


def run_elevated_setup_helper(path: Path) -> None:
    try:
        setup_helper_report_path(path).unlink()
    except FileNotFoundError:
        pass
    payload = _encode_setup_helper_payload(path, user_sid=_current_windows_user_sid())
    parameters = subprocess.list2cmdline(
        [
            "-m",
            "opensquilla.sandbox.backend.windows_default_setup",
            "--elevated-helper",
            payload,
        ]
    )
    exit_code = _shell_execute_runas_and_wait(
        executable=sys.executable,
        parameters=parameters,
        directory=str(_setup_helper_import_root()),
    )
    if exit_code != 0:
        detail = _setup_helper_report_detail(path)
        message = f"windows_setup_helper_failed: exit={exit_code}"
        if detail:
            message = f"{message}: {detail}"
        raise OSError(message)


def elevated_setup_helper_main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or args[0] != "--elevated-helper":
        print("windows_default_setup helper expects --elevated-helper payload", file=sys.stderr)
        return 2
    try:
        payload = _decode_setup_helper_payload(args[1])
        marker_path, profile_path = _validated_elevated_setup_target(payload)
    except Exception as exc:
        print(f"windows_default_setup helper failed: {exc}", file=sys.stderr)
        return 2
    try:
        with _windows_setup_process_lock(marker_path):
            with _secure_setup_directory_lease(marker_path, profile_path) as lease:
                if _windows_setup_is_ready(marker_path, profile_path):
                    write_setup_helper_report(
                        marker_path,
                        state="ready",
                        detail="already_ready",
                    )
                    return 0
                write_setup_helper_report(marker_path, state="running")
                try:
                    network = establish_windows_network_setup(marker_path)
                    lock_persistent_sandbox_dirs(
                        marker_path,
                        offline_sid=network.offline_user_sid,
                        real_user_sid=payload["userSid"],
                        lease=lease,
                    )
                    _validate_setup_directory_lease(lease, recursive=True)
                    write_setup_marker(marker_path, network=network)
                    write_setup_helper_report(marker_path, state="ready", detail="setup_complete")
                    return 0
                except Exception as exc:
                    try:
                        _validate_setup_directory_lease(lease, recursive=True)
                    except Exception as lease_exc:
                        print(
                            f"windows_default_setup helper failed without report: {lease_exc}",
                            file=sys.stderr,
                        )
                        return 2
                    write_setup_helper_report(marker_path, state="failed", detail=str(exc))
                    print(f"windows_default_setup helper failed: {exc}", file=sys.stderr)
                    return 1
    except Exception as exc:
        print(f"windows_default_setup helper failed: {exc}", file=sys.stderr)
        return 2


def _setup_helper_report_detail(marker_path: Path) -> str | None:
    report = read_setup_helper_report(marker_path)
    if report is None:
        return None
    return report.get("detail") or report.get("state")


def _setup_helper_import_root() -> Path:
    path = Path(__file__).resolve()
    package_root = path.parents[2]
    import_root = package_root.parent
    if (import_root / "opensquilla").exists():
        return import_root
    return Path.cwd()


def _encode_setup_helper_payload(path: Path, *, user_sid: str | None = None) -> str:
    import base64

    data = {"markerPath": str(path)}
    if user_sid:
        data["userSid"] = user_sid
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_setup_helper_payload(value: str) -> dict[str, str]:
    import base64

    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError("windows_setup_helper_payload_invalid") from exc
    if not isinstance(payload, dict):
        raise OSError("windows_setup_helper_payload_invalid")
    marker_path = payload.get("markerPath")
    if not isinstance(marker_path, str) or not marker_path:
        raise OSError("windows_setup_helper_payload_invalid")
    result = {"markerPath": marker_path}
    user_sid = payload.get("userSid")
    if user_sid is not None:
        if not isinstance(user_sid, str) or not user_sid.startswith("S-1-"):
            raise OSError("windows_setup_helper_payload_invalid")
        result["userSid"] = user_sid
    return result


def _validated_elevated_setup_target(payload: dict[str, str]) -> tuple[Path, Path]:
    sid = payload.get("userSid")
    if not sid:
        raise OSError("windows_setup_helper_real_user_sid_missing")
    profile_path = _windows_profile_path_for_sid(sid).expanduser().absolute()
    expected = default_setup_marker_path(profile_path).absolute()
    supplied = Path(payload["markerPath"]).expanduser().absolute()
    if _setup_path_key(supplied) != _setup_path_key(expected):
        raise OSError("windows_setup_helper_marker_path_mismatch")
    _validate_existing_non_reparse_components(expected)
    profile_canonical = profile_path.resolve(strict=True)
    marker_parent_canonical = expected.parent.resolve(strict=False)
    try:
        marker_parent_canonical.relative_to(profile_canonical)
    except ValueError as exc:
        raise OSError("windows_setup_helper_marker_outside_profile") from exc
    return expected, profile_path


def _windows_profile_path_for_sid(sid: str) -> Path:
    if not sys.platform.startswith("win"):
        raise OSError("windows_setup_helper_profile_lookup_requires_windows")
    import winreg

    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList" + rf"\{sid}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _kind = winreg.QueryValueEx(key, "ProfileImagePath")
    except OSError as exc:
        raise OSError("windows_setup_helper_profile_lookup_failed") from exc
    if not isinstance(value, str) or not value:
        raise OSError("windows_setup_helper_profile_lookup_failed")
    return Path(os.path.expandvars(value))


def _setup_path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path))).replace("\\", "/").rstrip("/")


def _windows_setup_is_ready(marker_path: Path, profile_path: Path) -> bool:
    marker = read_setup_marker(marker_path)
    if marker is None or marker.network is None:
        return False

    from opensquilla.sandbox.backend.windows_default_support import (
        probe_windows_default_support,
    )

    support = probe_windows_default_support(
        home=profile_path,
        proxy_ports=marker.network.allowed_proxy_ports,
    )
    return support.default_backend_available and support.proxy_allowlist_enforced


@contextmanager
def _windows_setup_process_lock(marker_path: Path) -> Iterator[None]:
    """Serialize elevated setup helpers for one Windows profile.

    Gateway restarts can leave an already-elevated helper running. A named
    mutex prevents a replacement gateway from concurrently changing the
    account, ACL, firewall, and WFP state. Windows releases the mutex if a
    helper crashes; the bounded wait avoids an unbounded second helper.
    """

    if not sys.platform.startswith("win"):
        yield
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    digest = hashlib.sha256(_setup_path_key(marker_path).encode("utf-8")).hexdigest()[:32]
    mutex_name = rf"Local\OpenSquillaSandboxSetup-{digest}"
    handle = kernel32.CreateMutexW(None, False, mutex_name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "windows_setup_mutex_create_failed")

    wait_object_0 = 0x00000000
    wait_abandoned = 0x00000080
    wait_timeout = 0x00000102
    acquired = False
    try:
        wait_result = int(kernel32.WaitForSingleObject(handle, SETUP_PROCESS_LOCK_TIMEOUT_MS))
        if wait_result in (wait_object_0, wait_abandoned):
            acquired = True
        elif wait_result == wait_timeout:
            raise OSError("windows_setup_mutex_timeout")
        else:
            raise OSError(ctypes.get_last_error(), "windows_setup_mutex_wait_failed")
        yield
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


def _is_reparse_path(path: Path) -> bool:
    try:
        stat_result = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or bool(is_junction()) or bool(attributes & 0x400)


def _validate_existing_non_reparse_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor) if absolute.anchor else Path()
    parts = absolute.parts[1:] if absolute.anchor else absolute.parts
    for part in parts:
        current /= part
        if not os.path.lexists(current):
            continue
        if _is_reparse_path(current):
            raise OSError(f"windows_setup_helper_reparse_component: {current}")


def _open_directory_no_follow(path: Path) -> object:
    if not sys.platform.startswith("win"):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags)

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    file_read_attributes = 0x0080
    file_share_read = 0x00000001
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    invalid_handle_value = wintypes.HANDLE(-1).value
    file_attribute_tag_info = 9

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        str(path),
        file_read_attributes,
        file_share_read,
        None,
        open_existing,
        file_flag_backup_semantics | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle_value:
        code = ctypes.get_last_error()
        raise OSError(code, f"windows_setup_helper_open_directory_failed: {path}")
    tag_info = FileAttributeTagInfo()
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        file_attribute_tag_info,
        ctypes.byref(tag_info),
        ctypes.sizeof(tag_info),
    ):
        code = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise OSError(code, f"windows_setup_helper_inspect_directory_failed: {path}")
    if tag_info.FileAttributes & 0x400:
        kernel32.CloseHandle(handle)
        raise OSError(f"windows_setup_helper_reparse_component: {path}")
    return handle


def _close_directory_handle(handle: Any) -> None:
    if not sys.platform.startswith("win"):
        os.close(int(handle))
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


@contextmanager
def _secure_setup_directory_lease(
    marker_path: Path,
    profile_path: Path,
) -> Iterator[_SetupDirectoryLease]:
    opensquilla_root = profile_path / ".opensquilla"
    roots = (
        opensquilla_root / "sandbox",
        opensquilla_root / "sandbox-secrets",
        opensquilla_root / "sandbox-bin",
    )
    handles: list[object] = []
    try:
        _validate_existing_non_reparse_components(marker_path)
        handles.append(_open_directory_no_follow(profile_path))
        for directory in (opensquilla_root, *roots):
            directory.mkdir(exist_ok=True)
            if _is_reparse_path(directory):
                raise OSError(f"windows_setup_helper_reparse_component: {directory}")
            handles.append(_open_directory_no_follow(directory))
        lease = _SetupDirectoryLease(
            marker_path=marker_path,
            profile_path=profile_path,
            roots=roots,
            handles=tuple(handles),
        )
        _validate_setup_directory_lease(lease, recursive=True)
        yield lease
    finally:
        for handle in reversed(handles):
            _close_directory_handle(handle)


def _validate_setup_directory_lease(
    lease: _SetupDirectoryLease,
    *,
    recursive: bool,
) -> None:
    _validate_existing_non_reparse_components(lease.marker_path)
    profile = lease.profile_path.resolve(strict=True)
    for root in lease.roots:
        if _is_reparse_path(root):
            raise OSError(f"windows_setup_helper_reparse_component: {root}")
        try:
            root.resolve(strict=True).relative_to(profile)
        except ValueError as exc:
            raise OSError(f"windows_setup_helper_root_outside_profile: {root}") from exc
        if not recursive:
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            for name in (*directories, *files):
                candidate = Path(current) / name
                if _is_reparse_path(candidate):
                    raise OSError(f"windows_setup_helper_reparse_component: {candidate}")


def _write_json_atomic_no_follow(path: Path, payload: dict[str, object]) -> None:
    data = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    if os.name == "nt":
        try:
            _write_json_windows_no_follow(temporary, data)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_json_windows_no_follow(path: Path, data: bytes) -> None:
    if os.name != "nt":
        raise OSError("windows_setup_helper_output_requires_windows")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    generic_write = 0x40000000
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    open_always = 4
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    file_attribute_tag_info = 9
    file_begin = 0
    invalid_handle_value = wintypes.HANDLE(-1).value

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        ]

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.SetFilePointerEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    ]
    kernel32.SetFilePointerEx.restype = wintypes.BOOL
    kernel32.SetEndOfFile.argtypes = [wintypes.HANDLE]
    kernel32.SetEndOfFile.restype = wintypes.BOOL
    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL
    kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateFileW(
        str(path),
        generic_write,
        file_share_read | file_share_write,
        None,
        open_always,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle == invalid_handle_value:
        code = ctypes.get_last_error()
        raise OSError(code, f"windows_setup_helper_open_output_failed: {path}")
    try:
        tag_info = FileAttributeTagInfo()
        if not kernel32.GetFileInformationByHandleEx(
            handle,
            file_attribute_tag_info,
            ctypes.byref(tag_info),
            ctypes.sizeof(tag_info),
        ):
            code = ctypes.get_last_error()
            raise OSError(code, f"windows_setup_helper_inspect_output_failed: {path}")
        if tag_info.FileAttributes & 0x400:
            raise OSError(f"windows_setup_helper_reparse_output: {path}")
        if not kernel32.SetFilePointerEx(handle, 0, None, file_begin):
            code = ctypes.get_last_error()
            raise OSError(code, f"windows_setup_helper_seek_output_failed: {path}")
        if not kernel32.SetEndOfFile(handle):
            code = ctypes.get_last_error()
            raise OSError(code, f"windows_setup_helper_truncate_output_failed: {path}")
        written = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(data)
        if not kernel32.WriteFile(
            handle,
            buffer,
            len(data),
            ctypes.byref(written),
            None,
        ) or written.value != len(data):
            code = ctypes.get_last_error()
            raise OSError(code, f"windows_setup_helper_write_output_failed: {path}")
        if not kernel32.FlushFileBuffers(handle):
            code = ctypes.get_last_error()
            raise OSError(code, f"windows_setup_helper_flush_output_failed: {path}")
    finally:
        kernel32.CloseHandle(handle)


def _shell_execute_runas_and_wait(
    *,
    executable: str,
    parameters: str,
    directory: str,
) -> int:
    if not sys.platform.startswith("win"):
        raise OSError("windows_setup_helper_requires_windows")
    if not executable:
        raise OSError("windows_setup_helper_missing_python")

    import ctypes
    from ctypes import wintypes

    see_mask_nocloseprocess = 0x00000040
    sw_hide = 0
    infinite = 0xFFFFFFFF
    wait_failed = 0xFFFFFFFF
    error_cancelled = 1223

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", wintypes.LPVOID),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
    info.fMask = see_mask_nocloseprocess
    info.lpVerb = "runas"
    info.lpFile = executable
    info.lpParameters = parameters
    info.lpDirectory = directory
    info.nShow = sw_hide

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == error_cancelled:
            raise OSError("windows_setup_helper_cancelled")
        raise OSError(code, f"windows_setup_helper_launch_failed: {ctypes.FormatError(code)}")
    try:
        wait_result = kernel32.WaitForSingleObject(info.hProcess, infinite)
        if wait_result == wait_failed:
            code = ctypes.get_last_error()
            raise OSError(code, f"windows_setup_helper_wait_failed: {ctypes.FormatError(code)}")
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(exit_code)):
            code = ctypes.get_last_error()
            raise OSError(code, f"windows_setup_helper_exit_failed: {ctypes.FormatError(code)}")
        return int(exit_code.value)
    finally:
        if info.hProcess:
            kernel32.CloseHandle(info.hProcess)


def ensure_offline_sandbox_user(state_root: Path) -> dict[str, str]:
    from opensquilla.sandbox.backend.windows_default_identity import protect_password

    state_root.mkdir(parents=True, exist_ok=True)
    password = _generate_offline_user_password()
    username = OFFLINE_USERNAME
    if len(username) > 20:
        raise OSError("offline_user_name_too_long")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$name = '{username}'; "
        "$plain = $env:OPENSQUILLA_SANDBOX_PASSWORD; "
        "$password = ConvertTo-SecureString $plain -AsPlainText -Force; "
        "$user = Get-LocalUser -Name $name -ErrorAction SilentlyContinue; "
        "if ($null -eq $user) { "
        "New-LocalUser -Name $name -Password $password "
        "-Description 'OpenSquilla offline sandbox network identity' | Out-Null "
        "} else { Set-LocalUser -Name $name -Password $password }; "
        "$user = Get-LocalUser -Name $name; "
        "$user.SID.Value"
    )
    env = {**os.environ, "OPENSQUILLA_SANDBOX_PASSWORD": password}
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise OSError(detail or "offline_user_missing")
    sid = completed.stdout.strip().splitlines()[-1].strip()
    if not sid:
        raise OSError("offline_user_missing")
    return {
        "sid": sid,
        "username": OFFLINE_USERNAME,
        "protectedPassword": protect_password(password),
    }


def lock_persistent_sandbox_dirs(
    marker_path: Path,
    *,
    offline_sid: str,
    real_user_sid: str | None = None,
    lease: _SetupDirectoryLease | None = None,
) -> None:
    opensquilla_root = marker_path.parent.parent
    roots = (
        marker_path.parent,
        opensquilla_root / "sandbox-secrets",
        opensquilla_root / "sandbox-bin",
    )
    real_sid = real_user_sid or _current_windows_user_sid()
    profile_path = marker_path.parent.parent.parent
    lease_context = (
        nullcontext(lease)
        if lease is not None
        else _secure_setup_directory_lease(marker_path, profile_path)
    )
    with lease_context as active_lease:
        assert active_lease is not None
        for root in roots:
            _validate_setup_directory_lease(active_lease, recursive=True)
            commands = (
                ["icacls", str(root), "/reset", "/t", "/c"],
                ["icacls", str(root), "/inheritance:r", "/t", "/c"],
                [
                    "icacls",
                    str(root),
                    "/grant:r",
                    f"*{real_sid}:(OI)(CI)F",
                    "*S-1-5-18:(OI)(CI)F",
                    "*S-1-5-32-544:(OI)(CI)F",
                    "/t",
                    "/c",
                ],
                ["icacls", str(root), "/remove:g", f"*{offline_sid}", "/t", "/c"],
            )
            for command in commands:
                _validate_setup_directory_lease(active_lease, recursive=True)
                command.append("/L")
                completed = subprocess.run(command, capture_output=True, text=True, check=False)
                if completed.returncode != 0:
                    detail = completed.stderr.strip() or completed.stdout.strip()
                    raise OSError(detail or f"persistent_sandbox_acl_failed: {root}")


def _current_windows_user_sid() -> str:
    completed = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError("persistent_sandbox_user_sid_unavailable")
    rows = list(csv.reader(completed.stdout.splitlines()))
    if not rows or len(rows[-1]) < 2 or not rows[-1][1].startswith("S-1-"):
        raise OSError("persistent_sandbox_user_sid_unavailable")
    return rows[-1][1]


def _generate_offline_user_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    prefix = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*()-_=+"),
    ]
    rest = [secrets.choice(alphabet) for _ in range(36)]
    return "".join(prefix + rest)


__all__ = [
    "SETUP_VERSION",
    "OFFLINE_USERNAME",
    "SETUP_HELPER_REPORT",
    "WindowsDefaultSetupMarker",
    "default_setup_marker_path",
    "ensure_offline_sandbox_user",
    "elevated_setup_helper_main",
    "establish_windows_network_setup",
    "read_setup_marker",
    "read_setup_helper_report",
    "run_elevated_setup_helper",
    "setup_marker_is_current",
    "setup_marker_proxy_allowlist_ready",
    "setup_payload",
    "setup_helper_report_path",
    "write_setup_helper_report",
    "write_setup_marker",
]


if __name__ == "__main__":
    raise SystemExit(elevated_setup_helper_main())
