"""Windows default sandbox runner helper."""

# ruff: noqa: N801, N806
# mypy: disable-error-code="attr-defined,arg-type,assignment,call-overload"

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any

HELPER_MODULE = "opensquilla.sandbox.backend.windows_default_runner"
DISABLE_MAX_PRIVILEGE = 0x01
LUA_TOKEN = 0x04
WRITE_RESTRICTED = 0x08
RESTRICTED_TOKEN_FLAGS = DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED
GENERIC_ALL = 0x10000000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
DELETE = 0x00010000
FILE_DELETE_CHILD = 0x00000040
FILE_APPEND_DATA = 0x00000004
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_EXECUTE = 0x001200A0
FILE_WRITE_ATTRIBUTES = 0x00000100
FILE_WRITE_DATA = 0x00000002
FILE_WRITE_EA = 0x00000010
INHERIT_ONLY_ACE_FLAG = 0x08
INHERITED_ACE_FLAG = 0x10
FILE_MUTATION_DENY_MASK = (
    FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_WRITE_ATTRIBUTES
    | DELETE
    | FILE_DELETE_CHILD
)
FILE_WRITE_DENY_MASK = FILE_MUTATION_DENY_MASK | FILE_GENERIC_WRITE | GENERIC_WRITE
FILE_READ_DENY_MASK = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE | GENERIC_READ
MANAGED_DENY_MASK = FILE_WRITE_DENY_MASK | FILE_READ_DENY_MASK
WRITE_DAC = 0x00040000
MANAGED_ALLOW_MASK = (
    FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | DELETE | WRITE_DAC
)
TOKEN_ASSIGN_PRIMARY = 0x0001
TOKEN_DUPLICATE = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_ADJUST_DEFAULT = 0x0080
TOKEN_ADJUST_SESSIONID = 0x0100
TOKEN_ADJUST_PRIVILEGES = 0x0020
STARTF_USESHOWWINDOW = 0x00000001
STARTF_USESTDHANDLES = 0x00000100
SW_HIDE = 0
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_NO_WINDOW = 0x08000000
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002
SEM_NOOPENFILEERRORBOX = 0x8000
OFFLINE_PAYLOAD_ENV = "OPENSQUILLA_WINDOWS_DEFAULT_PAYLOAD"
OFFLINE_PAYLOAD_STDIN_ARG = "--payload-stdin"
_ICMP_TOOL_NAMES = frozenset(
    {
        "ping",
        "ping.exe",
        "tracert",
        "tracert.exe",
        "pathping",
        "pathping.exe",
    }
)
_POWERSHELL_NAMES = frozenset({"powershell", "powershell.exe", "pwsh", "pwsh.exe"})
_SHELL_NAMES = _POWERSHELL_NAMES | frozenset({"cmd", "cmd.exe"})
_ICMP_SHELL_COMMAND_RE = re.compile(
    r"(?<![\w.-])(?:pathping|tracert|ping)(?:\.exe)?(?![\w.-])",
    re.IGNORECASE,
)
_ICMP_POWERSHELL_PATTERNS = (
    "test-connection",
    "test-netconnection",
    "system.net.networkinformation.ping",
    "networkinformation.ping",
)


def _base_restricting_sid_specs() -> tuple[tuple[str, str], ...]:
    return (("S-1-1-0", "everyone"),)


def _ordered_restricting_sids(
    *,
    capability_sids: Sequence[object],
    user_sid: object | None,
    logon_sid: object | None,
    base_sids: Sequence[object],
) -> tuple[object, ...]:
    ordered = list(capability_sids)
    if logon_sid is not None:
        ordered.append(logon_sid)
    ordered.extend(base_sids)
    _ = user_sid
    return tuple(ordered)


@dataclass(frozen=True)
class HelperPayload:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    policy: dict[str, Any]
    run_mode: str
    timeout: float
    stdin: bytes | None = None
    offline_child: bool = False


@dataclass(frozen=True)
class OfflineLaunchCredentials:
    sid: str
    username: str
    password: str


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if not sys.platform.startswith("win"):
            raise SystemExit("windows_default runner only runs on native Windows")
        payload = _parse_payload(args)
        _validate_policy_is_enforceable(payload.policy)
        raise SystemExit(_run_windows_default(payload))
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            raise SystemExit(1) from None
        raise


def _parse_payload(args: Sequence[str]) -> HelperPayload:
    if list(args) == ["--payload-env"]:
        env_payload = os.environ.get(OFFLINE_PAYLOAD_ENV)
        if not env_payload:
            raise SystemExit("windows_default runner payload env is missing")
        raw_payload = env_payload
    elif list(args) == [OFFLINE_PAYLOAD_STDIN_ARG]:
        try:
            raw_payload = sys.stdin.buffer.read().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SystemExit(f"windows_default runner payload stdin is unreadable: {exc}") from exc
    elif len(args) == 1:
        raw_payload = args[0]
    else:
        raise SystemExit("windows_default runner expects one JSON payload argument")
    try:
        raw = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid windows_default payload JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit("invalid windows_default payload: expected object")
    if raw.get("backend") != "windows_default":
        raise SystemExit("invalid windows_default payload: expected backend windows_default")

    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise SystemExit("invalid windows_default payload: argv must be a string list")

    cwd_raw = raw.get("cwd")
    if not isinstance(cwd_raw, str) or not cwd_raw:
        raise SystemExit("invalid windows_default payload: cwd is required")
    cwd = Path(cwd_raw)
    if not cwd.exists() or not cwd.is_dir():
        raise SystemExit(f"invalid windows_default cwd: {cwd}")

    env_raw = raw.get("env", {})
    if not isinstance(env_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()
    ):
        raise SystemExit("invalid windows_default payload: env must be string map")

    policy = raw.get("policy")
    if not isinstance(policy, dict):
        raise SystemExit("invalid windows_default payload: policy is required")

    run_mode = raw.get("runMode")
    if run_mode not in {"standard", "trusted"}:
        raise SystemExit("invalid windows_default payload: runMode must be standard or trusted")

    timeout = raw.get("timeout")
    if not isinstance(timeout, int | float) or timeout <= 0:
        raise SystemExit("invalid windows_default payload: timeout must be positive")

    stdin_raw = raw.get("stdinBase64")
    if stdin_raw is None:
        stdin = None
    elif isinstance(stdin_raw, str):
        try:
            stdin = base64.b64decode(stdin_raw.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise SystemExit("invalid windows_default payload: stdinBase64 is invalid") from exc
    else:
        raise SystemExit("invalid windows_default payload: stdinBase64 must be a string or null")
    offline_child = raw.get("offlineChild", False)
    if not isinstance(offline_child, bool):
        raise SystemExit("invalid windows_default payload: offlineChild must be boolean")

    return HelperPayload(
        argv=tuple(argv),
        cwd=cwd,
        env=dict(env_raw),
        policy=policy,
        run_mode=str(run_mode),
        timeout=float(timeout),
        stdin=stdin,
        offline_child=offline_child,
    )


def _validate_policy_is_enforceable(policy: dict[str, Any]) -> None:
    network = policy.get("network")
    if network not in {"none", "host", "proxy_allowlist"}:
        raise SystemExit(f"windows_default runner received unknown network mode: {network!r}")
    if network == "proxy_allowlist":
        _validate_network_proxy(policy)


def _validate_network_proxy(policy: dict[str, Any]) -> None:
    proxy = policy.get("network_proxy")
    if proxy is None:
        proxy = policy.get("networkProxy")
    if not isinstance(proxy, dict):
        raise SystemExit("windows_default PROXY_ALLOWLIST requires network_proxy endpoint")
    host = proxy.get("host")
    port = proxy.get("port")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("windows_default PROXY_ALLOWLIST requires a local network_proxy host")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        raise SystemExit("windows_default PROXY_ALLOWLIST requires a valid network_proxy port")
    _validate_windows_network_boundary(policy)


def _validate_windows_network_boundary(policy: dict[str, Any]) -> None:
    proxy = policy.get("network_proxy") or policy.get("networkProxy")
    boundary = policy.get("windowsNetworkBoundary")
    if not isinstance(proxy, dict):
        raise SystemExit("windows_default PROXY_ALLOWLIST requires network_proxy endpoint")
    if not isinstance(boundary, dict):
        raise SystemExit("windows_default PROXY_ALLOWLIST requires windowsNetworkBoundary")
    ports = boundary.get("allowedProxyPorts")
    sid = boundary.get("offlineUserSid")
    allow_local_binding = boundary.get("allowLocalBinding")
    if not isinstance(sid, str) or not sid:
        raise SystemExit("windows_default windowsNetworkBoundary requires offlineUserSid")
    if not isinstance(ports, list) or not all(isinstance(port, int) for port in ports):
        raise SystemExit("windows_default windowsNetworkBoundary requires allowedProxyPorts")
    if not isinstance(allow_local_binding, bool):
        raise SystemExit("windows_default windowsNetworkBoundary requires allowLocalBinding")
    if proxy.get("port") not in ports:
        raise SystemExit(
            "windows_default network_proxy port is not allowed by windowsNetworkBoundary"
        )


def _run_windows_default(payload: HelperPayload) -> int:
    _reject_proxy_allowlist_icmp_commands(payload)
    acl_plan = _windows_acl_plan(payload.policy)
    if payload.offline_child:
        boundary = _network_boundary(payload.policy)
        if boundary is None:
            raise OSError("windowsNetworkBoundary missing for offline identity launch")
        from opensquilla.sandbox.backend.windows_default_identity import (
            offline_identity_from_boundary,
        )

        offline_identity_from_boundary(boundary)
        return _run_windows_default_with_acl_lease(payload, acl_plan, credentials=None)
    credentials = _resolve_offline_launch_credentials(payload)
    with _windows_acl_execution_lease():
        return _run_windows_default_with_acl_lease(
            payload,
            acl_plan,
            credentials=credentials,
        )


def _run_windows_default_with_acl_lease(
    payload: HelperPayload,
    acl_plan: dict[str, Any],
    *,
    credentials: OfflineLaunchCredentials | None,
) -> int:
    capability_sids = _capability_sids(acl_plan)
    if _should_reexec_as_offline_identity(payload):
        if credentials is None:
            raise SystemExit("windows_default offline identity credentials are missing")
        _prepare_deny_acl_targets(acl_plan)
        _apply_acl_refresh(acl_plan)
        return _run_payload_as_offline_identity(payload, credentials=credentials)
    _prepare_deny_acl_targets(acl_plan)
    if not payload.offline_child:
        _apply_acl_refresh(acl_plan)
    return _run_restricted_process_native(payload, capability_sids)


def _reject_proxy_allowlist_icmp_commands(payload: HelperPayload) -> None:
    if payload.policy.get("network") != "proxy_allowlist":
        return
    reason = _proxy_allowlist_icmp_block_reason(payload.argv)
    if reason is not None:
        raise SystemExit(reason)


def _proxy_allowlist_icmp_block_reason(argv: Sequence[str]) -> str | None:
    if not argv:
        return None
    executable = PureWindowsPath(str(argv[0])).name.lower()
    if executable in _ICMP_TOOL_NAMES:
        return "windows_default PROXY_ALLOWLIST blocks ICMP diagnostic tools"
    if executable not in _SHELL_NAMES:
        embedded_command = _shell_host_embedded_command(argv)
        if embedded_command is not None:
            return _proxy_allowlist_shell_icmp_block_reason(
                embedded_command,
                powershell=True,
            )
        return None
    command_text = " ".join(str(item) for item in argv[1:]).lower()
    return _proxy_allowlist_shell_icmp_block_reason(
        command_text,
        powershell=executable in _POWERSHELL_NAMES,
    )


def _shell_host_embedded_command(argv: Sequence[str]) -> str | None:
    if len(argv) < 5:
        return None
    if str(argv[1]).lower() != "-c":
        return None
    host_source = str(argv[2])
    if "windows sandbox shell host expects powershell path and command" not in host_source:
        return None
    return str(argv[4])


def _proxy_allowlist_shell_icmp_block_reason(
    command_text: str,
    *,
    powershell: bool,
) -> str | None:
    lowered = command_text.lower()
    if _ICMP_SHELL_COMMAND_RE.search(lowered):
        return "windows_default PROXY_ALLOWLIST blocks ICMP diagnostic tools"
    if powershell and any(pattern in lowered for pattern in _ICMP_POWERSHELL_PATTERNS):
        return "windows_default PROXY_ALLOWLIST blocks PowerShell ICMP diagnostics"
    return None


def _windows_acl_plan(policy: dict[str, Any]) -> dict[str, Any]:
    plan = policy.get("windowsAclPlan")
    if not isinstance(plan, dict):
        raise SystemExit("invalid windows_default policy: windowsAclPlan is required")
    auto_grants = plan.get("autoGrants")
    if not isinstance(auto_grants, list):
        raise SystemExit("invalid windows_default policy: autoGrants must be a list")
    for grant in auto_grants:
        if not isinstance(grant, dict):
            raise SystemExit("invalid windows_default ACL grant: grant must be an object")
        path = grant.get("path")
        access = grant.get("access")
        sid = grant.get("capabilitySid")
        kind = grant.get("kind", "required")
        if (
            not isinstance(path, str)
            or not path
            or not Path(path).is_absolute()
            or access not in {"RX", "RWX"}
            or not isinstance(sid, str)
            or not sid
            or kind not in {"required", "policy", "expansion"}
        ):
            raise SystemExit("invalid windows_default ACL grant shape")
    capability_sids = plan.get("capabilitySids")
    if not isinstance(capability_sids, list) or not all(
        isinstance(sid, str) for sid in capability_sids
    ):
        raise SystemExit("invalid windows_default policy: capabilitySids must be a string list")
    deny_write_paths = plan.get("denyWritePaths", [])
    if not isinstance(deny_write_paths, list) or not all(
        isinstance(path, str) and path and Path(path).is_absolute() for path in deny_write_paths
    ):
        raise SystemExit("invalid windows_default policy: denyWritePaths must be a string list")
    deny_read_paths = plan.get("denyReadPaths", [])
    if not isinstance(deny_read_paths, list) or not all(
        isinstance(path, str) and path and Path(path).is_absolute() for path in deny_read_paths
    ):
        raise SystemExit("invalid windows_default policy: denyReadPaths must be a string list")
    grant_current_user_access = plan.get("grantCurrentUserAccess", False)
    if not isinstance(grant_current_user_access, bool):
        raise SystemExit("invalid windows_default policy: grantCurrentUserAccess must be boolean")
    state_path = _trusted_deny_acl_state_path(plan)
    return {
        **plan,
        "denyWritePaths": deny_write_paths,
        "denyReadPaths": deny_read_paths,
        "denyAclStatePath": str(state_path),
        "grantCurrentUserAccess": grant_current_user_access,
    }


def _default_deny_acl_state_path() -> Path:
    from opensquilla.sandbox.backend.windows_default_setup import (
        default_setup_marker_path,
    )

    return default_setup_marker_path().with_name("deny_acl_state.json")


def _default_allow_acl_state_path() -> Path:
    return _default_deny_acl_state_path().with_name("allow_acl_state.json")


def _trusted_deny_acl_state_path(plan: dict[str, Any]) -> Path:
    expected = _default_deny_acl_state_path().expanduser().resolve(strict=False)
    raw = plan.get("denyAclStatePath")
    if raw is None:
        return expected
    if not isinstance(raw, str) or not raw:
        raise SystemExit("invalid windows_default policy: denyAclStatePath must be a string")
    supplied = Path(raw).expanduser().resolve(strict=False)
    if _acl_path_key(supplied) != _acl_path_key(expected):
        raise SystemExit("invalid windows_default policy: denyAclStatePath is not trusted")
    return expected


def _capability_sids(plan: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(sid) for sid in plan["capabilitySids"])


def _apply_acl_refresh(plan: dict[str, Any], *, apply_deny_write: bool = True) -> None:
    if apply_deny_write:
        _prepare_deny_acl_targets(plan, include_read=False)
    normal_access_sid = (
        _current_token_user_sid_string() if plan.get("grantCurrentUserAccess") else None
    )
    normal_access_seen: set[tuple[str, str]] = set()
    for grant in plan["autoGrants"]:
        if not isinstance(grant, dict):
            raise SystemExit("invalid windows_default ACL grant: grant must be an object")
        path = grant.get("path")
        access = grant.get("access")
        sid = grant.get("capabilitySid")
        if not isinstance(path, str) or access not in {"RX", "RWX"} or not isinstance(sid, str):
            raise SystemExit("invalid windows_default ACL grant shape")
        grant_path = Path(path)
        if grant.get("kind") in {"policy", "expansion"} and not grant_path.exists():
            continue
        _grant_path_to_sid(grant_path, access, sid)
        if normal_access_sid is not None and access == "RWX":
            key = (str(grant_path.resolve(strict=False)).casefold(), access)
            if key in normal_access_seen:
                continue
            normal_access_seen.add(key)
            _grant_path_to_sid(grant_path, "HOST_RWX", normal_access_sid)
    if apply_deny_write and "denyAclStatePath" in plan:
        state_path = Path(str(plan["denyAclStatePath"]))
        for sid in _capability_sids(plan):
            _sync_deny_acl_state(
                state_path,
                sid,
                _capability_write_deny_entries(plan, sid),
            )


def _prepare_deny_acl_targets(
    plan: dict[str, Any],
    *,
    include_read: bool = True,
) -> tuple[Path, ...]:
    keys = ["denyWritePaths"]
    if include_read:
        keys.append("denyReadPaths")
    paths: list[Path] = []
    for key in keys:
        raw_paths = plan.get(key, [])
        if not isinstance(raw_paths, list) or not all(
            isinstance(raw, str) and raw for raw in raw_paths
        ):
            raise SystemExit(f"invalid windows_default ACL {key} shape")
        paths.extend(Path(raw).expanduser().absolute() for raw in raw_paths)
    unique = _dedupe_acl_paths(paths)
    for path in unique:
        if path.exists():
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SystemExit(
                f"windows_default ACL deny target could not be materialized: {path}: {exc}"
            ) from exc
    return unique


def _capability_write_deny_entries(
    plan: dict[str, Any],
    sid: str,
) -> dict[Path, int]:
    desired: dict[Path, int] = {}
    for raw_path in plan.get("denyWritePaths", []):
        deny_path = Path(str(raw_path)).expanduser().absolute()
        if sid in _deny_write_capability_sids_for_path(plan, deny_path):
            desired[deny_path] = FILE_WRITE_DENY_MASK
    return desired


def _deny_write_capability_sids_for_path(plan: dict[str, Any], deny_path: Path) -> tuple[str, ...]:
    write_grants: list[tuple[Path, str]] = []
    for grant in plan.get("autoGrants", []):
        if not isinstance(grant, dict) or grant.get("access") != "RWX":
            continue
        path = grant.get("path")
        sid = grant.get("capabilitySid")
        if isinstance(path, str) and isinstance(sid, str):
            write_grants.append((Path(path), sid))
    matching = [
        sid
        for root, sid in write_grants
        if _path_contains_casefold(root.resolve(strict=False), deny_path.resolve(strict=False))
    ]
    return tuple(dict.fromkeys(matching))


def _path_contains_casefold(root: Path, candidate: Path) -> bool:
    root_text = _acl_path_key(root)
    candidate_text = _acl_path_key(candidate)
    return candidate_text == root_text or candidate_text.startswith(root_text + "/")


def _acl_path_key(path: Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()


def _dedupe_acl_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = _acl_path_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def _ace_mask_covers(existing_mask: int, required_mask: int) -> bool:
    return existing_mask & required_mask == required_mask


def _explicit_allow_ace_status(
    *,
    ace_mask: int,
    ace_flags: int,
    required_mask: int,
    cleanup_legacy_delete_child: bool,
) -> tuple[bool, bool]:
    if ace_flags & (INHERIT_ONLY_ACE_FLAG | INHERITED_ACE_FLAG):
        return False, False
    unsafe_managed_legacy = cleanup_legacy_delete_child and bool(ace_mask & FILE_DELETE_CHILD)
    return (
        not unsafe_managed_legacy and _ace_mask_covers(ace_mask, required_mask),
        unsafe_managed_legacy,
    )


def _network_boundary(policy: dict[str, Any]) -> dict[str, object] | None:
    boundary = policy.get("windowsNetworkBoundary")
    return boundary if isinstance(boundary, dict) else None


def _should_reexec_as_offline_identity(payload: HelperPayload) -> bool:
    return (
        not payload.offline_child
        and str(payload.run_mode).strip().lower() != "full"
        and _network_boundary(payload.policy) is not None
    )


def _resolve_offline_launch_credentials(
    payload: HelperPayload,
) -> OfflineLaunchCredentials:
    boundary = _network_boundary(payload.policy)
    if boundary is None:
        raise OSError("windowsNetworkBoundary missing for offline identity launch")
    from opensquilla.sandbox.backend.windows_default_identity import (
        offline_identity_from_boundary,
        unprotect_password,
    )

    identity = offline_identity_from_boundary(boundary)
    password = unprotect_password(identity.protected_password)
    return OfflineLaunchCredentials(
        sid=identity.sid,
        username=identity.username,
        password=password,
    )


def _run_payload_as_offline_identity(
    payload: HelperPayload,
    *,
    credentials: OfflineLaunchCredentials | None = None,
) -> int:
    launch = credentials or _resolve_offline_launch_credentials(payload)
    acl_plan = _windows_acl_plan(payload.policy)
    _prepare_deny_acl_targets(acl_plan)
    _sync_deny_acl_state(
        Path(str(acl_plan["denyAclStatePath"])),
        launch.sid,
        _offline_identity_deny_entries(acl_plan),
    )
    _sync_allow_acl_state(
        _default_allow_acl_state_path(),
        launch.sid,
        _offline_identity_allow_entries(acl_plan),
    )
    return _run_payload_as_offline_identity_native(
        replace(payload, offline_child=True),
        username=launch.username,
        password=launch.password,
    )


def _open_source_token_for_payload(payload: HelperPayload) -> int:
    if payload.offline_child:
        return _open_current_process_token()
    boundary = _network_boundary(payload.policy)
    if payload.policy.get("network") == "proxy_allowlist" and boundary is not None:
        from opensquilla.sandbox.backend.windows_default_identity import (
            logon_offline_identity,
            offline_identity_from_boundary,
        )

        return logon_offline_identity(offline_identity_from_boundary(boundary))
    return _open_current_process_token()


def _open_current_process_token() -> int:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    HANDLE = wintypes.HANDLE
    DWORD = wintypes.DWORD
    BOOL = wintypes.BOOL

    advapi32.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
    advapi32.OpenProcessToken.restype = BOOL
    kernel32.GetCurrentProcess.restype = HANDLE

    desired_access = (
        TOKEN_ASSIGN_PRIMARY
        | TOKEN_DUPLICATE
        | TOKEN_QUERY
        | TOKEN_ADJUST_DEFAULT
        | TOKEN_ADJUST_SESSIONID
        | TOKEN_ADJUST_PRIVILEGES
    )
    source_token = HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        desired_access,
        ctypes.byref(source_token),
    ):
        code = ctypes.get_last_error()
        raise OSError(code, f"OpenProcessToken failed: {ctypes.FormatError(code)}")
    return int(source_token.value)


def _current_token_user_sid_string() -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    HANDLE = wintypes.HANDLE
    DWORD = wintypes.DWORD
    LPVOID = wintypes.LPVOID

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Sid", LPVOID),
            ("Attributes", DWORD),
        ]

    advapi32.GetTokenInformation.argtypes = [HANDLE, DWORD, LPVOID, DWORD, ctypes.POINTER(DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [LPVOID]
    kernel32.LocalFree.restype = LPVOID
    kernel32.CloseHandle.argtypes = [HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    TOKEN_USER_CLASS = 1
    token = HANDLE(_open_current_process_token())
    string_sid = wintypes.LPWSTR()
    try:
        needed = DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise OSError(0, "GetTokenInformation(TokenUser) returned no size")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_USER_CLASS,
            buffer,
            needed,
            ctypes.byref(needed),
        ):
            code = ctypes.get_last_error()
            raise OSError(
                code,
                f"GetTokenInformation(TokenUser) failed: {ctypes.FormatError(code)}",
            )
        user = SID_AND_ATTRIBUTES.from_buffer(buffer)
        if not advapi32.ConvertSidToStringSidW(user.Sid, ctypes.byref(string_sid)):
            code = ctypes.get_last_error()
            raise OSError(code, f"ConvertSidToStringSidW failed: {ctypes.FormatError(code)}")
        sid = string_sid.value
        if sid is None:
            raise OSError(0, "ConvertSidToStringSidW returned no SID")
        return sid
    finally:
        if string_sid:
            kernel32.LocalFree(string_sid)
        if token:
            kernel32.CloseHandle(token)


def _grant_path_to_sid(path: Path, access: str, sid: str) -> None:
    if not path.exists():
        raise SystemExit(f"windows_default ACL grant target does not exist: {path}")
    try:
        _grant_path_to_sid_native(path, access, sid)
    except OSError as exc:
        raise SystemExit(f"windows_default ACL grant failed for {path}: {exc}") from exc


def _grant_path_to_sid_native(path: Path, access: str, sid: str) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    DWORD = wintypes.DWORD
    LPVOID = wintypes.LPVOID

    class TRUSTEE_W(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", LPVOID),
            ("MultipleTrusteeOperation", DWORD),
            ("TrusteeForm", DWORD),
            ("TrusteeType", DWORD),
            ("ptstrName", LPVOID),
        ]

    class EXPLICIT_ACCESS_W(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", DWORD),
            ("grfAccessMode", DWORD),
            ("grfInheritance", DWORD),
            ("Trustee", TRUSTEE_W),
        ]

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", DWORD),
            ("AclBytesInUse", DWORD),
            ("AclBytesFree", DWORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    class ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [
            ("Header", ACE_HEADER),
            ("Mask", DWORD),
            ("SidStart", DWORD),
        ]

    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(LPVOID)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        DWORD,
        DWORD,
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
    ]
    advapi32.GetNamedSecurityInfoW.restype = DWORD
    advapi32.SetEntriesInAclW.argtypes = [
        DWORD,
        ctypes.POINTER(EXPLICIT_ACCESS_W),
        LPVOID,
        ctypes.POINTER(LPVOID),
    ]
    advapi32.SetEntriesInAclW.restype = DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        DWORD,
        DWORD,
        LPVOID,
        LPVOID,
        LPVOID,
        LPVOID,
    ]
    advapi32.SetNamedSecurityInfoW.restype = DWORD
    advapi32.GetAclInformation.argtypes = [LPVOID, LPVOID, DWORD, DWORD]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [LPVOID, DWORD, ctypes.POINTER(LPVOID)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.DeleteAce.argtypes = [LPVOID, DWORD]
    advapi32.DeleteAce.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [LPVOID, LPVOID]
    advapi32.EqualSid.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [LPVOID]
    kernel32.LocalFree.restype = LPVOID

    ERROR_SUCCESS = 0
    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x00000004
    ACL_SIZE_INFORMATION_CLASS = 2
    GRANT_ACCESS = 1
    TRUSTEE_IS_SID = 0
    TRUSTEE_IS_UNKNOWN = 0
    ACCESS_ALLOWED_ACE_TYPE = 0
    NO_INHERITANCE = 0
    OBJECT_INHERIT_ACE = 0x1
    CONTAINER_INHERIT_ACE = 0x2

    DELETE = 0x00010000
    WRITE_DAC = 0x00040000
    FILE_GENERIC_READ = 0x00120089
    FILE_GENERIC_WRITE = 0x00120116
    FILE_GENERIC_EXECUTE = 0x001200A0

    def win32_error(label: str, code: int | None = None) -> OSError:
        error_code = ctypes.get_last_error() if code is None else code
        return OSError(error_code, f"{label} failed: {ctypes.FormatError(error_code)}")

    def explicit_allow_status(
        dacl: object, sid_to_check: object, mask: int
    ) -> tuple[bool, tuple[int, ...]]:
        if not dacl:
            return False, ()
        info = ACL_SIZE_INFORMATION()
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ACL_SIZE_INFORMATION_CLASS,
        ):
            return False, ()
        covers = False
        unsafe_indices: list[int] = []
        for index in range(int(info.AceCount)):
            ace_ptr = LPVOID()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace_ptr)) or not ace_ptr:
                continue
            header = ctypes.cast(ace_ptr, ctypes.POINTER(ACE_HEADER)).contents
            if header.AceType != ACCESS_ALLOWED_ACE_TYPE:
                continue
            ace = ctypes.cast(ace_ptr, ctypes.POINTER(ACCESS_ALLOWED_ACE)).contents
            sid_ptr_value = int(ace_ptr.value) + ctypes.sizeof(ACE_HEADER) + ctypes.sizeof(DWORD)
            ace_sid = LPVOID(sid_ptr_value)
            if not advapi32.EqualSid(ace_sid, sid_to_check):
                continue
            ace_covers, unsafe_legacy = _explicit_allow_ace_status(
                ace_mask=int(ace.Mask),
                ace_flags=int(header.AceFlags),
                required_mask=mask,
                cleanup_legacy_delete_child=access != "HOST_RWX",
            )
            if unsafe_legacy:
                unsafe_indices.append(index)
            elif ace_covers:
                covers = True
        return covers, tuple(unsafe_indices)

    if access == "RX":
        allow_mask = FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
    elif access in {"RWX", "HOST_RWX"}:
        allow_mask = FILE_GENERIC_READ | FILE_GENERIC_WRITE | FILE_GENERIC_EXECUTE | DELETE
        if access == "HOST_RWX":
            allow_mask |= WRITE_DAC
    else:
        raise OSError(0, f"unsupported ACL access mode: {access!r}")

    sid_ptr = LPVOID()
    security_descriptor = LPVOID()
    old_dacl = LPVOID()
    new_dacl = LPVOID()
    path_buffer = ctypes.create_unicode_buffer(str(path))
    inheritance = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE if path.is_dir() else NO_INHERITANCE

    try:
        if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(sid_ptr)):
            raise win32_error("ConvertStringSidToSidW")
        code = advapi32.GetNamedSecurityInfoW(
            path_buffer,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(old_dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        if code != ERROR_SUCCESS:
            raise win32_error("GetNamedSecurityInfoW", code)
        covers, unsafe_indices = explicit_allow_status(old_dacl, sid_ptr, allow_mask)
        for index in reversed(unsafe_indices):
            if not advapi32.DeleteAce(old_dacl, index):
                raise win32_error("DeleteAce(unsafe FILE_DELETE_CHILD allow)")
        if covers and not unsafe_indices:
            return

        explicit = EXPLICIT_ACCESS_W()
        explicit.grfAccessPermissions = allow_mask
        explicit.grfAccessMode = GRANT_ACCESS
        explicit.grfInheritance = inheritance
        explicit.Trustee.pMultipleTrustee = None
        explicit.Trustee.MultipleTrusteeOperation = 0
        explicit.Trustee.TrusteeForm = TRUSTEE_IS_SID
        explicit.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
        explicit.Trustee.ptstrName = sid_ptr

        code = advapi32.SetEntriesInAclW(
            1,
            ctypes.byref(explicit),
            old_dacl,
            ctypes.byref(new_dacl),
        )
        if code != ERROR_SUCCESS:
            raise win32_error("SetEntriesInAclW", code)
        code = advapi32.SetNamedSecurityInfoW(
            path_buffer,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            new_dacl,
            None,
        )
        if code != ERROR_SUCCESS:
            raise win32_error("SetNamedSecurityInfoW", code)
    finally:
        for pointer in (new_dacl, security_descriptor, sid_ptr):
            if pointer:
                kernel32.LocalFree(pointer)


def _revoke_path_for_sid(path: Path, sid: str) -> None:
    if not path.exists():
        return
    try:
        _revoke_path_for_sid_native(path, sid)
    except AttributeError:
        if os.name != "nt":
            return
        raise
    except OSError as exc:
        raise SystemExit(f"windows_default ACL revoke failed for {path}: {exc}") from exc


def _revoke_allow_path_for_sid(path: Path, sid: str) -> None:
    if not path.exists():
        return
    try:
        _revoke_path_for_sid_native(
            path,
            sid,
            ace_type=0,
            blocking_mask=MANAGED_ALLOW_MASK,
        )
    except AttributeError:
        if os.name != "nt":
            return
        raise
    except OSError as exc:
        raise SystemExit(f"windows_default ACL allow revoke failed for {path}: {exc}") from exc


def _revoke_path_for_sid_native(
    path: Path,
    sid: str,
    *,
    ace_type: int = 1,
    blocking_mask: int = MANAGED_DENY_MASK,
) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    DWORD = wintypes.DWORD
    LPVOID = wintypes.LPVOID

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", DWORD),
            ("AclBytesInUse", DWORD),
            ("AclBytesFree", DWORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    class ACCESS_DENIED_ACE(ctypes.Structure):
        _fields_ = [
            ("Header", ACE_HEADER),
            ("Mask", DWORD),
            ("SidStart", DWORD),
        ]

    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(LPVOID)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        DWORD,
        DWORD,
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
    ]
    advapi32.GetNamedSecurityInfoW.restype = DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        DWORD,
        DWORD,
        LPVOID,
        LPVOID,
        LPVOID,
        LPVOID,
    ]
    advapi32.SetNamedSecurityInfoW.restype = DWORD
    advapi32.GetAclInformation.argtypes = [LPVOID, LPVOID, DWORD, DWORD]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [LPVOID, DWORD, ctypes.POINTER(LPVOID)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.DeleteAce.argtypes = [LPVOID, DWORD]
    advapi32.DeleteAce.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [LPVOID, LPVOID]
    advapi32.EqualSid.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [LPVOID]
    kernel32.LocalFree.restype = LPVOID

    ERROR_SUCCESS = 0
    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x00000004
    ACL_SIZE_INFORMATION_CLASS = 2
    INHERITED_ACE = 0x10

    def win32_error(label: str, code: int | None = None) -> OSError:
        error_code = ctypes.get_last_error() if code is None else code
        return OSError(error_code, f"{label} failed: {ctypes.FormatError(error_code)}")

    sid_ptr = LPVOID()
    security_descriptor = LPVOID()
    old_dacl = LPVOID()
    path_buffer = ctypes.create_unicode_buffer(str(path))

    try:
        if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(sid_ptr)):
            raise win32_error("ConvertStringSidToSidW")
        code = advapi32.GetNamedSecurityInfoW(
            path_buffer,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(old_dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        if code != ERROR_SUCCESS:
            raise win32_error("GetNamedSecurityInfoW", code)
        if not old_dacl:
            return
        info = ACL_SIZE_INFORMATION()
        if not advapi32.GetAclInformation(
            old_dacl,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ACL_SIZE_INFORMATION_CLASS,
        ):
            raise win32_error("GetAclInformation")
        removed = False
        for index in range(int(info.AceCount) - 1, -1, -1):
            ace_ptr = LPVOID()
            if not advapi32.GetAce(old_dacl, index, ctypes.byref(ace_ptr)) or not ace_ptr:
                continue
            header = ctypes.cast(ace_ptr, ctypes.POINTER(ACE_HEADER)).contents
            if header.AceType != ace_type or header.AceFlags & INHERITED_ACE:
                continue
            ace = ctypes.cast(ace_ptr, ctypes.POINTER(ACCESS_DENIED_ACE)).contents
            sid_ptr_value = int(ace_ptr.value) + ctypes.sizeof(ACE_HEADER) + ctypes.sizeof(DWORD)
            ace_sid = LPVOID(sid_ptr_value)
            if not (advapi32.EqualSid(ace_sid, sid_ptr) and (ace.Mask & blocking_mask)):
                continue
            if not advapi32.DeleteAce(old_dacl, index):
                raise win32_error("DeleteAce")
            removed = True
        if not removed:
            return
        code = advapi32.SetNamedSecurityInfoW(
            path_buffer,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            old_dacl,
            None,
        )
        if code != ERROR_SUCCESS:
            raise win32_error("SetNamedSecurityInfoW", code)
    finally:
        for pointer in (security_descriptor, sid_ptr):
            if pointer:
                kernel32.LocalFree(pointer)


def _deny_read_path_to_sid(path: Path, sid: str) -> None:
    _deny_path_to_sid(
        path,
        sid,
        mask=FILE_READ_DENY_MASK,
        label="deny-read",
    )


def _deny_write_path_to_sid(
    path: Path,
    sid: str,
    *,
    include_read_control: bool = True,
) -> None:
    _deny_path_to_sid(
        path,
        sid,
        mask=(FILE_WRITE_DENY_MASK if include_read_control else FILE_MUTATION_DENY_MASK),
        label="deny-write",
    )


def _deny_path_to_sid(path: Path, sid: str, *, mask: int, label: str) -> None:
    if not path.exists():
        raise SystemExit(f"windows_default ACL {label} target does not exist: {path}")
    try:
        _deny_path_to_sid_native(path, sid, mask=mask)
    except AttributeError:
        if os.name != "nt":
            return
        raise
    except OSError as exc:
        raise SystemExit(f"windows_default ACL {label} failed for {path}: {exc}") from exc


def _deny_file_mutation_path_to_sid(path: Path, sid: str) -> None:
    if not path.exists():
        raise SystemExit(f"windows_default ACL deny-write target does not exist: {path}")
    _deny_write_path_to_sid(path, sid, include_read_control=False)


def _deny_path_to_sid_native(
    path: Path,
    sid: str,
    *,
    mask: int,
) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    DWORD = wintypes.DWORD
    LPVOID = wintypes.LPVOID

    class TRUSTEE_W(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", LPVOID),
            ("MultipleTrusteeOperation", DWORD),
            ("TrusteeForm", DWORD),
            ("TrusteeType", DWORD),
            ("ptstrName", LPVOID),
        ]

    class EXPLICIT_ACCESS_W(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", DWORD),
            ("grfAccessMode", DWORD),
            ("grfInheritance", DWORD),
            ("Trustee", TRUSTEE_W),
        ]

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", DWORD),
            ("AclBytesInUse", DWORD),
            ("AclBytesFree", DWORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    class ACCESS_DENIED_ACE(ctypes.Structure):
        _fields_ = [
            ("Header", ACE_HEADER),
            ("Mask", DWORD),
            ("SidStart", DWORD),
        ]

    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(LPVOID)]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        DWORD,
        DWORD,
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(LPVOID),
    ]
    advapi32.GetNamedSecurityInfoW.restype = DWORD
    advapi32.SetEntriesInAclW.argtypes = [
        DWORD,
        ctypes.POINTER(EXPLICIT_ACCESS_W),
        LPVOID,
        ctypes.POINTER(LPVOID),
    ]
    advapi32.SetEntriesInAclW.restype = DWORD
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        DWORD,
        DWORD,
        LPVOID,
        LPVOID,
        LPVOID,
        LPVOID,
    ]
    advapi32.SetNamedSecurityInfoW.restype = DWORD
    advapi32.GetAclInformation.argtypes = [LPVOID, LPVOID, DWORD, DWORD]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [LPVOID, DWORD, ctypes.POINTER(LPVOID)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [LPVOID, LPVOID]
    advapi32.EqualSid.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [LPVOID]
    kernel32.LocalFree.restype = LPVOID

    ERROR_SUCCESS = 0
    SE_FILE_OBJECT = 1
    DACL_SECURITY_INFORMATION = 0x00000004
    ACL_SIZE_INFORMATION_CLASS = 2
    DENY_ACCESS = 3
    TRUSTEE_IS_SID = 0
    TRUSTEE_IS_UNKNOWN = 0
    ACCESS_DENIED_ACE_TYPE = 1
    INHERIT_ONLY_ACE = 0x08
    OBJECT_INHERIT_ACE = 0x1
    CONTAINER_INHERIT_ACE = 0x2

    def win32_error(label: str, code: int | None = None) -> OSError:
        error_code = ctypes.get_last_error() if code is None else code
        return OSError(error_code, f"{label} failed: {ctypes.FormatError(error_code)}")

    def dacl_has_deny_for_sid(dacl: object, sid_to_check: object) -> bool:
        if not dacl:
            return False
        info = ACL_SIZE_INFORMATION()
        if not advapi32.GetAclInformation(
            dacl,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ACL_SIZE_INFORMATION_CLASS,
        ):
            return False
        for index in range(int(info.AceCount)):
            ace_ptr = LPVOID()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace_ptr)) or not ace_ptr:
                continue
            header = ctypes.cast(ace_ptr, ctypes.POINTER(ACE_HEADER)).contents
            if header.AceType != ACCESS_DENIED_ACE_TYPE:
                continue
            if header.AceFlags & INHERIT_ONLY_ACE:
                continue
            ace = ctypes.cast(ace_ptr, ctypes.POINTER(ACCESS_DENIED_ACE)).contents
            sid_ptr_value = int(ace_ptr.value) + ctypes.sizeof(ACE_HEADER) + ctypes.sizeof(DWORD)
            ace_sid = LPVOID(sid_ptr_value)
            if advapi32.EqualSid(ace_sid, sid_to_check) and _ace_mask_covers(int(ace.Mask), mask):
                return True
        return False

    sid_ptr = LPVOID()
    security_descriptor = LPVOID()
    old_dacl = LPVOID()
    new_dacl = LPVOID()
    path_buffer = ctypes.create_unicode_buffer(str(path))

    try:
        if not advapi32.ConvertStringSidToSidW(sid, ctypes.byref(sid_ptr)):
            raise win32_error("ConvertStringSidToSidW")
        code = advapi32.GetNamedSecurityInfoW(
            path_buffer,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            ctypes.byref(old_dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        if code != ERROR_SUCCESS:
            raise win32_error("GetNamedSecurityInfoW", code)
        if dacl_has_deny_for_sid(old_dacl, sid_ptr):
            return

        explicit = EXPLICIT_ACCESS_W()
        explicit.grfAccessPermissions = mask
        explicit.grfAccessMode = DENY_ACCESS
        explicit.grfInheritance = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
        explicit.Trustee.pMultipleTrustee = None
        explicit.Trustee.MultipleTrusteeOperation = 0
        explicit.Trustee.TrusteeForm = TRUSTEE_IS_SID
        explicit.Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
        explicit.Trustee.ptstrName = sid_ptr

        code = advapi32.SetEntriesInAclW(
            1,
            ctypes.byref(explicit),
            old_dacl,
            ctypes.byref(new_dacl),
        )
        if code != ERROR_SUCCESS:
            raise win32_error("SetEntriesInAclW", code)
        code = advapi32.SetNamedSecurityInfoW(
            path_buffer,
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION,
            None,
            None,
            new_dacl,
            None,
        )
        if code != ERROR_SUCCESS:
            raise win32_error("SetNamedSecurityInfoW", code)
    finally:
        for pointer in (new_dacl, security_descriptor, sid_ptr):
            if pointer:
                kernel32.LocalFree(pointer)


def _environment_block(env: dict[str, str]) -> str:
    merged = dict(env)
    for key in ("SystemRoot", "WINDIR", "ComSpec"):
        value = os.environ.get(key)
        if value and key not in merged:
            merged[key] = value
    items = [
        f"{key}={value}" for key, value in sorted(merged.items(), key=lambda item: item[0].upper())
    ]
    return "\0".join(items) + "\0\0"


def _payload_to_json(payload: HelperPayload) -> str:
    raw: dict[str, object] = {
        "backend": "windows_default",
        "argv": list(payload.argv),
        "cwd": str(payload.cwd),
        "env": payload.env,
        "policy": payload.policy,
        "runMode": payload.run_mode,
        "timeout": payload.timeout,
        "stdinBase64": (
            base64.b64encode(payload.stdin).decode("ascii") if payload.stdin is not None else None
        ),
        "offlineChild": payload.offline_child,
    }
    return json.dumps(raw, separators=(",", ":"), sort_keys=True)


def _helper_import_root() -> Path:
    path = Path(__file__).resolve()
    package_root = path.parents[2]
    import_root = package_root.parent
    if (import_root / "opensquilla").exists():
        return import_root
    return Path.cwd()


def _helper_child_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop(OFFLINE_PAYLOAD_ENV, None)
    import_root = str(_helper_import_root())
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = import_root if not existing else f"{import_root}{os.pathsep}{existing}"
    return env


def _offline_helper_runtime_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    executable = Path(sys.executable).resolve()
    roots.append(executable.parent)

    pyvenv_cfg = executable.parent.parent / "pyvenv.cfg"
    try:
        for line in pyvenv_cfg.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "home" and value.strip():
                roots.append(Path(value.strip()).resolve())
                break
    except OSError:
        pass

    base_prefix = Path(getattr(sys, "base_prefix", "") or "")
    if str(base_prefix):
        roots.append(base_prefix.resolve())
    roots.append(_helper_import_root().resolve())

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved = root.resolve(strict=False)
        except OSError:
            resolved = root
        key = str(resolved).casefold()
        if key in seen or not resolved.exists():
            continue
        seen.add(key)
        unique.append(resolved)
    return tuple(unique)


def _grant_offline_helper_runtime_access(sid: str) -> None:
    for root in _offline_helper_runtime_roots():
        _grant_path_to_sid(root, "RX", sid)


def _offline_identity_allow_entries(plan: dict[str, Any]) -> dict[Path, str]:
    desired: dict[Path, str] = {
        root.expanduser().absolute(): "RX" for root in _offline_helper_runtime_roots()
    }
    for grant in plan["autoGrants"]:
        path = Path(str(grant["path"])).expanduser().absolute()
        access = str(grant["access"])
        if access == "RWX" or path not in desired:
            desired[path] = access
    return desired


def _sync_allow_acl_state(
    state_path: Path,
    sid: str,
    desired: dict[Path, str],
) -> None:
    with _deny_acl_state_lock(state_path):
        normalized = {
            path.expanduser().absolute(): access
            for path, access in desired.items()
            if access in {"RX", "RWX"}
        }
        state = _read_allow_acl_state(state_path)
        _recover_allow_acl_taint(state_path, state)
        principals = state["principals"]
        previous = {
            Path(item["path"]).expanduser().absolute(): item["access"]
            for item in principals.get(sid, [])
        }
        _mark_acl_state_tainted(
            state_path,
            kind="allow",
            sid=sid,
            paths=(*previous, *normalized),
        )
        try:
            previous_by_key = {
                _acl_path_key(path): (path, access) for path, access in previous.items()
            }
            desired_by_key = {
                _acl_path_key(path): (path, access) for path, access in normalized.items()
            }
            for key, (path, access) in desired_by_key.items():
                old = previous_by_key.get(key)
                if old is not None and old[1] != access:
                    _revoke_allow_path_for_sid(old[0], sid)
                _grant_path_to_sid(path, access, sid)
            for key, (path, _access) in previous_by_key.items():
                if key not in desired_by_key:
                    _revoke_allow_path_for_sid(path, sid)
            updated = dict(principals)
            updated[sid] = [
                {"access": access, "path": str(path)} for path, access in normalized.items()
            ]
            _write_deny_acl_state(state_path, {"version": 1, "principals": updated})
            _clear_acl_state_taint(state_path)
        except (Exception, SystemExit) as exc:
            restore_errors = _rollback_allow_acl_principal(
                sid, previous=previous, desired=normalized
            )
            if not restore_errors:
                _clear_acl_state_taint(state_path)
            raise SystemExit(
                f"windows_default ACL allow desired-state sync failed for {sid}: {exc}; "
                f"rollback_errors={restore_errors!r}"
            ) from exc


def _read_allow_acl_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"version": 1, "principals": {}}
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise SystemExit("invalid windows_default ACL allow desired-state format")
    principals = raw.get("principals")
    if not isinstance(principals, dict):
        raise SystemExit("invalid windows_default ACL allow desired-state principals")
    for sid, entries in principals.items():
        if not isinstance(sid, str) or not isinstance(entries, list):
            raise SystemExit("invalid windows_default ACL allow desired-state principals")
        for item in entries:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or not Path(item["path"]).is_absolute()
                or item.get("access") not in {"RX", "RWX"}
            ):
                raise SystemExit("invalid windows_default ACL allow desired-state entry")
    return {"version": 1, "principals": principals}


def _rollback_allow_acl_principal(
    sid: str,
    *,
    previous: dict[Path, str],
    desired: dict[Path, str],
) -> tuple[str, ...]:
    errors: list[str] = []
    for path in (*desired, *previous):
        try:
            _revoke_allow_path_for_sid(path, sid)
        except BaseException as exc:
            errors.append(str(exc))
    for path, access in previous.items():
        try:
            _grant_path_to_sid(path, access, sid)
        except BaseException as exc:
            errors.append(str(exc))
    return tuple(errors)


def _offline_identity_deny_entries(plan: dict[str, Any]) -> dict[Path, int]:
    desired: dict[Path, int] = {}
    for raw_path in plan.get("denyWritePaths", []):
        path = Path(str(raw_path)).expanduser().absolute()
        desired[path] = desired.get(path, 0) | FILE_MUTATION_DENY_MASK
    for raw_path in plan.get("denyReadPaths", []):
        path = Path(str(raw_path)).expanduser().absolute()
        desired[path] = desired.get(path, 0) | FILE_READ_DENY_MASK
    return desired


def _sync_deny_acl_state(
    state_path: Path,
    sid: str,
    desired: dict[Path, int],
) -> None:
    try:
        with _deny_acl_state_lock(state_path):
            _sync_deny_acl_state_locked(state_path, sid, desired)
    except (OSError, TimeoutError) as exc:
        raise SystemExit(
            f"windows_default ACL desired-state lock failed: {state_path}: {exc}"
        ) from exc


def _sync_deny_acl_state_locked(
    state_path: Path,
    sid: str,
    desired: dict[Path, int],
) -> None:
    if not sid:
        raise SystemExit("windows_default ACL state sync requires a principal SID")
    normalized_desired = _normalize_deny_acl_entries(desired)
    state = _read_deny_acl_state(state_path)
    _recover_deny_acl_taint(state_path, state)
    _materialize_deny_paths(tuple(normalized_desired))
    principals = state["principals"]
    previous = _principal_deny_acl_entries(principals.get(sid, []), sid=sid)
    previous_by_key = {_acl_path_key(path): (path, mask) for path, mask in previous.items()}
    desired_by_key = {
        _acl_path_key(path): (path, mask) for path, mask in normalized_desired.items()
    }

    _mark_acl_state_tainted(
        state_path,
        kind="deny",
        sid=sid,
        paths=(*previous, *normalized_desired),
    )
    try:
        for key, (path, mask) in desired_by_key.items():
            old = previous_by_key.get(key)
            if old is not None and old[1] != mask:
                _revoke_path_for_sid(old[0], sid)
            _deny_path_to_sid(path, sid, mask=mask, label="desired-state")
        for key, (path, _mask) in previous_by_key.items():
            if key not in desired_by_key:
                _revoke_path_for_sid(path, sid)

        updated_principals = dict(principals)
        if normalized_desired:
            updated_principals[sid] = [
                {"mask": mask, "path": str(path)} for path, mask in normalized_desired.items()
            ]
        else:
            updated_principals.pop(sid, None)
        _write_deny_acl_state(
            state_path,
            {"version": 1, "principals": updated_principals},
        )
        _clear_acl_state_taint(state_path)
    except (Exception, SystemExit) as exc:
        restore_errors = _rollback_deny_acl_principal(
            sid,
            previous=previous,
            desired=normalized_desired,
        )
        if not restore_errors:
            _clear_acl_state_taint(state_path)
        raise SystemExit(
            f"windows_default ACL desired-state sync failed for {sid}: {exc}; "
            f"rollback_errors={restore_errors!r}"
        ) from exc


@contextmanager
def _deny_acl_state_lock(state_path: Path) -> Iterator[None]:
    lock_path = state_path.with_name(f".{state_path.name}.lock")
    with _cross_process_file_lock(lock_path):
        yield


def _default_execution_lease_path() -> Path:
    return _default_deny_acl_state_path().with_name("execution.lock")


@contextmanager
def _windows_acl_execution_lease() -> Iterator[None]:
    with _cross_process_file_lock(_default_execution_lease_path()):
        yield


@contextmanager
def _cross_process_file_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _normalize_deny_acl_entries(desired: dict[Path, int]) -> dict[Path, int]:
    normalized: dict[str, tuple[Path, int]] = {}
    for raw_path, mask in desired.items():
        if (
            not isinstance(raw_path, Path)
            or not isinstance(mask, int)
            or mask <= 0
            or mask & ~MANAGED_DENY_MASK
        ):
            raise SystemExit("invalid windows_default ACL desired-state entry")
        path = raw_path.expanduser().absolute()
        key = _acl_path_key(path)
        previous = normalized.get(key)
        normalized[key] = (path, mask | (previous[1] if previous else 0))
    return {path: mask for path, mask in normalized.values()}


def _materialize_deny_paths(paths: Sequence[Path]) -> None:
    for path in paths:
        if path.exists():
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SystemExit(
                f"windows_default ACL deny target could not be materialized: {path}: {exc}"
            ) from exc


def _read_deny_acl_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"version": 1, "principals": {}}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"windows_default ACL desired-state is unreadable: {state_path}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise SystemExit("invalid windows_default ACL desired-state format")
    principals = raw.get("principals")
    if not isinstance(principals, dict) or not all(
        isinstance(key, str) and isinstance(value, list) for key, value in principals.items()
    ):
        raise SystemExit("invalid windows_default ACL desired-state principals")
    for principal, entries in principals.items():
        _principal_deny_acl_entries(entries, sid=principal)
    return {"version": 1, "principals": principals}


def _principal_deny_acl_entries(
    raw_entries: object,
    *,
    sid: str,
) -> dict[Path, int]:
    if not isinstance(raw_entries, list):
        raise SystemExit(f"invalid windows_default ACL desired-state entries for {sid}")
    result: dict[Path, int] = {}
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise SystemExit(f"invalid windows_default ACL desired-state entry for {sid}")
        raw_path = entry.get("path")
        mask = entry.get("mask")
        if (
            not isinstance(raw_path, str)
            or not raw_path
            or not Path(raw_path).is_absolute()
            or not isinstance(mask, int)
        ):
            raise SystemExit(f"invalid windows_default ACL desired-state entry for {sid}")
        result[Path(raw_path).expanduser().absolute()] = mask
    return _normalize_deny_acl_entries(result)


def _write_deny_acl_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(f".{state_path.name}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, state_path)
        _fsync_parent_directory(state_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _acl_state_taint_path(state_path: Path) -> Path:
    return state_path.with_name(f"{state_path.name}.tainted")


def _mark_acl_state_tainted(
    state_path: Path,
    *,
    kind: str,
    sid: str,
    paths: Sequence[Path],
) -> None:
    if kind not in {"allow", "deny"} or not sid:
        raise SystemExit("invalid windows_default ACL taint intent")
    unique_paths = _dedupe_acl_paths(tuple(path.expanduser().absolute() for path in paths))
    taint_path = _acl_state_taint_path(state_path)
    _write_deny_acl_state(
        taint_path,
        {
            "version": 1,
            "kind": kind,
            "sid": sid,
            "paths": [str(path) for path in unique_paths],
        },
    )


def _clear_acl_state_taint(state_path: Path) -> None:
    _acl_state_taint_path(state_path).unlink(missing_ok=True)
    _fsync_parent_directory(state_path)


def _read_acl_taint_intent(state_path: Path) -> dict[str, Any] | None:
    path = _acl_state_taint_path(state_path)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"windows_default ACL taint intent is unreadable and remains fail-closed: {path}"
        ) from exc
    if (
        not isinstance(raw, dict)
        or raw.get("version") != 1
        or raw.get("kind") not in {"allow", "deny"}
        or not isinstance(raw.get("sid"), str)
        or not raw["sid"]
        or not isinstance(raw.get("paths"), list)
        or not all(
            isinstance(item, str) and item and Path(item).is_absolute() for item in raw["paths"]
        )
    ):
        raise SystemExit(
            f"windows_default ACL taint intent is invalid and remains fail-closed: {path}"
        )
    return raw


def _recover_allow_acl_taint(state_path: Path, state: dict[str, Any]) -> None:
    intent = _read_acl_taint_intent(state_path)
    if intent is None:
        return
    if intent["kind"] != "allow":
        raise SystemExit("windows_default ACL taint kind does not match allow state")
    sid = str(intent["sid"])
    persisted = {
        Path(item["path"]).expanduser().absolute(): str(item["access"])
        for item in state["principals"].get(sid, [])
    }
    paths = _dedupe_acl_paths(
        tuple(Path(item).expanduser().absolute() for item in intent["paths"]) + tuple(persisted)
    )
    try:
        for path in paths:
            _revoke_allow_path_for_sid(path, sid)
        for path, access in persisted.items():
            _grant_path_to_sid(path, access, sid)
    except BaseException as exc:
        raise SystemExit(
            f"windows_default ACL allow taint repair failed and remains fail-closed: {exc}"
        ) from exc
    _clear_acl_state_taint(state_path)


def _recover_deny_acl_taint(state_path: Path, state: dict[str, Any]) -> None:
    intent = _read_acl_taint_intent(state_path)
    if intent is None:
        return
    if intent["kind"] != "deny":
        raise SystemExit("windows_default ACL taint kind does not match deny state")
    sid = str(intent["sid"])
    persisted = _principal_deny_acl_entries(state["principals"].get(sid, []), sid=sid)
    paths = _dedupe_acl_paths(
        tuple(Path(item).expanduser().absolute() for item in intent["paths"]) + tuple(persisted)
    )
    try:
        for path in paths:
            _revoke_path_for_sid(path, sid)
        for path, mask in persisted.items():
            _deny_path_to_sid(path, sid, mask=mask, label="taint-repair")
    except BaseException as exc:
        raise SystemExit(
            f"windows_default ACL deny taint repair failed and remains fail-closed: {exc}"
        ) from exc
    _clear_acl_state_taint(state_path)


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rollback_deny_acl_principal(
    sid: str,
    *,
    previous: dict[Path, int],
    desired: dict[Path, int],
) -> tuple[str, ...]:
    errors: list[str] = []
    for path in (*desired, *previous):
        try:
            _revoke_path_for_sid(path, sid)
        except BaseException as exc:
            errors.append(str(exc))
    for path, mask in previous.items():
        try:
            _deny_path_to_sid(path, sid, mask=mask, label="rollback")
        except BaseException as exc:
            errors.append(str(exc))
    return tuple(errors)


def _path_and_parents(path: Path) -> tuple[Path, ...]:
    paths = [path]
    current = path
    while True:
        parent = current.parent
        if parent == current or _is_drive_root(parent):
            break
        paths.append(parent)
        current = parent
    return tuple(paths)


def _is_drive_root(path: Path) -> bool:
    try:
        return bool(path.anchor) and path == type(path)(path.anchor)
    except (OSError, RuntimeError, ValueError):
        return False


def _grant_acl_plan_to_sid(plan: dict[str, Any], sid: str) -> None:
    seen: set[tuple[str, str]] = set()
    for grant in plan["autoGrants"]:
        if not isinstance(grant, dict):
            raise SystemExit("invalid windows_default ACL grant: grant must be an object")
        path = grant.get("path")
        access = grant.get("access")
        if not isinstance(path, str) or access not in {"RX", "RWX"}:
            raise SystemExit("invalid windows_default ACL grant shape")
        key = (str(Path(path)).casefold(), access)
        if key in seen:
            continue
        seen.add(key)
        _grant_path_to_sid(Path(path), access, sid)


def _runner_error_mode_flags() -> int:
    return SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX


def _restricted_process_creation_flags() -> int:
    return CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW


def _restricted_process_application_name(argv: Sequence[str]) -> str | None:
    if not argv:
        return None
    executable = argv[0]
    if PureWindowsPath(executable).is_absolute():
        return executable
    return None


def _offline_helper_creation_flags() -> int:
    return CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW


def _restricted_process_startup_flags() -> int:
    return STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW


def _clear_handle_inheritance(kernel32: object, handle: object, label: str) -> None:
    if not kernel32.SetHandleInformation(handle, 0x00000001, 0):
        raise OSError(f"SetHandleInformation({label}) failed")


def _require_reader_threads_stopped(
    threads: Sequence[threading.Thread],
    *,
    label: str,
) -> None:
    if any(thread.is_alive() for thread in threads):
        raise OSError(f"{label} pipe reader did not terminate")


def _start_child_stdin_writer(
    kernel32: object,
    stdin_write: object,
    stdin: bytes | None,
    *,
    on_error: Callable[[], None],
) -> tuple[threading.Thread, list[BaseException], Callable[[], None]]:
    errors: list[BaseException] = []
    close_lock = threading.Lock()
    closed = False

    def close_once() -> None:
        nonlocal closed
        with close_lock:
            if not closed:
                closed = True
                kernel32.CloseHandle(stdin_write)

    def write() -> None:
        try:
            _write_child_stdin(kernel32, stdin_write, stdin, close_handle=False)
        except BaseException as exc:
            errors.append(exc)
            on_error()
        finally:
            close_once()

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    return thread, errors, close_once


def _finish_child_io(
    *,
    writer_thread: threading.Thread,
    reader_threads: Sequence[threading.Thread],
    writer_errors: Sequence[BaseException],
    close_writer: Callable[[], None],
    label: str,
    terminate: Callable[[], None],
    cancel_io: Callable[[], None] | None = None,
    force_cancel: bool = False,
    ignore_writer_errors: bool = False,
) -> None:
    cancelled = False

    def cancel_and_close() -> None:
        nonlocal cancelled
        terminate()
        if cancel_io is not None:
            cancel_io()
        close_writer()
        cancelled = True

    if force_cancel:
        cancel_and_close()
    writer_thread.join(timeout=5)
    if writer_errors and not cancelled:
        cancel_and_close()
    for thread in reader_threads:
        thread.join(timeout=5)
    if writer_thread.is_alive() or any(thread.is_alive() for thread in reader_threads):
        cancel_and_close()
        writer_thread.join(timeout=1)
        for thread in reader_threads:
            thread.join(timeout=1)
    if writer_thread.is_alive() or any(thread.is_alive() for thread in reader_threads):
        raise OSError(f"{label} pipe I/O thread did not terminate")
    if writer_errors and not ignore_writer_errors:
        if not cancelled:
            cancel_and_close()
        raise OSError(f"{label} stdin writer failed: {writer_errors[0]}") from writer_errors[0]


def _cancel_child_pipe_io(kernel32: object, handles: Sequence[object]) -> None:
    cancel = getattr(kernel32, "CancelIoEx", None)
    if cancel is None:
        return
    for handle in handles:
        raw_handle = getattr(handle, "value", handle)
        if raw_handle:
            cancel(handle, None)


def _run_payload_as_offline_identity_native(
    payload: HelperPayload,
    *,
    username: str,
    password: str,
) -> int:
    if not sys.platform.startswith("win"):
        raise OSError("offline_identity_launch_requires_windows")

    import ctypes
    import msvcrt
    import threading
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LPVOID = wintypes.LPVOID
    HANDLE = wintypes.HANDLE
    DWORD = wintypes.DWORD
    BOOL = wintypes.BOOL

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", DWORD),
            ("lpSecurityDescriptor", LPVOID),
            ("bInheritHandle", BOOL),
        ]

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", DWORD),
            ("dwY", DWORD),
            ("dwXSize", DWORD),
            ("dwYSize", DWORD),
            ("dwXCountChars", DWORD),
            ("dwYCountChars", DWORD),
            ("dwFillAttribute", DWORD),
            ("dwFlags", DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", HANDLE),
            ("hStdOutput", HANDLE),
            ("hStdError", HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", HANDLE),
            ("hThread", HANDLE),
            ("dwProcessId", DWORD),
            ("dwThreadId", DWORD),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", DWORD),
            ("SchedulingClass", DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    advapi32.CreateProcessWithLogonW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        DWORD,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        DWORD,
        LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFO),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessWithLogonW.restype = BOOL
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(HANDLE),
        ctypes.POINTER(HANDLE),
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        DWORD,
    ]
    kernel32.CreatePipe.restype = BOOL
    kernel32.SetHandleInformation.argtypes = [HANDLE, DWORD, DWORD]
    kernel32.SetHandleInformation.restype = BOOL
    kernel32.CloseHandle.argtypes = [HANDLE]
    kernel32.CloseHandle.restype = BOOL
    kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
    kernel32.WaitForSingleObject.restype = DWORD
    kernel32.TerminateProcess.argtypes = [HANDLE, DWORD]
    kernel32.TerminateProcess.restype = BOOL
    kernel32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
    kernel32.GetExitCodeProcess.restype = BOOL
    kernel32.CreateJobObjectW.argtypes = [LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = HANDLE
    kernel32.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, LPVOID, DWORD]
    kernel32.SetInformationJobObject.restype = BOOL
    kernel32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
    kernel32.AssignProcessToJobObject.restype = BOOL
    kernel32.ResumeThread.argtypes = [HANDLE]
    kernel32.ResumeThread.restype = DWORD
    kernel32.CancelIoEx.argtypes = [HANDLE, LPVOID]
    kernel32.CancelIoEx.restype = BOOL
    kernel32.TerminateJobObject.argtypes = [HANDLE, DWORD]
    kernel32.TerminateJobObject.restype = BOOL
    kernel32.WriteFile.argtypes = [HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID]
    kernel32.WriteFile.restype = BOOL
    kernel32.SetErrorMode.argtypes = [DWORD]
    kernel32.SetErrorMode.restype = DWORD

    LOGON_WITHOUT_PROFILE = 0
    WAIT_TIMEOUT = 0x00000102
    WAIT_FAILED = 0xFFFFFFFF
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    def win_error(label: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{label} failed: {ctypes.FormatError(code)}")

    def close(handle: int) -> None:
        if handle:
            kernel32.CloseHandle(handle)

    stdin_read = HANDLE()
    stdin_write = HANDLE()
    stdout_read = HANDLE()
    stdout_write = HANDLE()
    stderr_read = HANDLE()
    stderr_write = HANDLE()
    process_info = PROCESS_INFORMATION()
    job = HANDLE()
    job_assigned = False
    reader_threads: list[threading.Thread] = []
    outputs: dict[str, bytes] = {"stdout": b"", "stderr": b""}
    try:
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = None
        sa.bInheritHandle = True
        if not kernel32.CreatePipe(
            ctypes.byref(stdin_read),
            ctypes.byref(stdin_write),
            ctypes.byref(sa),
            0,
        ):
            raise win_error("CreatePipe(stdin)")
        _clear_handle_inheritance(kernel32, stdin_write, "stdin")
        if not kernel32.CreatePipe(
            ctypes.byref(stdout_read),
            ctypes.byref(stdout_write),
            ctypes.byref(sa),
            0,
        ):
            raise win_error("CreatePipe(stdout)")
        if not kernel32.CreatePipe(
            ctypes.byref(stderr_read),
            ctypes.byref(stderr_write),
            ctypes.byref(sa),
            0,
        ):
            raise win_error("CreatePipe(stderr)")
        _clear_handle_inheritance(kernel32, stdout_read, "stdout")
        _clear_handle_inheritance(kernel32, stderr_read, "stderr")

        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(STARTUPINFO)
        startup.dwFlags = _restricted_process_startup_flags()
        startup.wShowWindow = SW_HIDE
        startup.hStdInput = stdin_read
        startup.hStdOutput = stdout_write
        startup.hStdError = stderr_write

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise win_error("CreateJobObjectW")
        limit_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limit_info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limit_info),
            ctypes.sizeof(limit_info),
        ):
            raise win_error("SetInformationJobObject")

        command_line = ctypes.create_unicode_buffer(
            subprocess.list2cmdline(
                [
                    sys.executable,
                    "-m",
                    HELPER_MODULE,
                    OFFLINE_PAYLOAD_STDIN_ARG,
                ]
            )
        )
        child_env = _helper_child_env()
        env_block = ctypes.create_unicode_buffer(_environment_block(child_env))
        previous_error_mode = kernel32.SetErrorMode(_runner_error_mode_flags())
        try:
            created = advapi32.CreateProcessWithLogonW(
                username,
                ".",
                password,
                LOGON_WITHOUT_PROFILE,
                sys.executable,
                command_line,
                _offline_helper_creation_flags(),
                env_block,
                str(_helper_import_root()),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            )
        finally:
            kernel32.SetErrorMode(previous_error_mode)
        if not created:
            raise win_error("CreateProcessWithLogonW")
        if not kernel32.AssignProcessToJobObject(job, process_info.hProcess):
            raise win_error("AssignProcessToJobObject")
        job_assigned = True
        if kernel32.ResumeThread(process_info.hThread) == WAIT_FAILED:
            raise win_error("ResumeThread")

        pipe_io_handles = (stdin_write, stdout_read, stderr_read)

        close(stdin_read)
        stdin_read = HANDLE()
        close(stdout_write)
        stdout_write = HANDLE()
        close(stderr_write)
        stderr_write = HANDLE()

        def read_pipe(name: str, handle: object) -> None:
            raw_handle = getattr(handle, "value", handle)
            fd = msvcrt.open_osfhandle(int(raw_handle), os.O_RDONLY | os.O_BINARY)
            with os.fdopen(fd, "rb", closefd=True) as stream:
                outputs[name] = stream.read()

        for name, handle in (("stdout", stdout_read), ("stderr", stderr_read)):
            thread = threading.Thread(target=read_pipe, args=(name, handle), daemon=True)
            thread.start()
            reader_threads.append(thread)
        stdout_read = HANDLE()
        stderr_read = HANDLE()

        writer_thread, writer_errors, close_writer = _start_child_stdin_writer(
            kernel32,
            stdin_write,
            _payload_to_json(payload).encode("utf-8"),
            on_error=lambda: kernel32.TerminateJobObject(job, 125),
        )
        stdin_write = HANDLE()

        wait_ms = max(1, int(payload.timeout * 1000))
        wait_result = kernel32.WaitForSingleObject(process_info.hProcess, wait_ms)
        wait_error: OSError | None = None
        if wait_result == WAIT_TIMEOUT:
            kernel32.TerminateJobObject(job, 124)
            kernel32.WaitForSingleObject(process_info.hProcess, 5000)
            exit_code = 124
        elif wait_result == WAIT_FAILED:
            wait_error = win_error("WaitForSingleObject")
            kernel32.TerminateJobObject(job, 125)
            exit_code = 125
        else:
            code = DWORD()
            if not kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(code)):
                wait_error = win_error("GetExitCodeProcess")
                kernel32.TerminateJobObject(job, 125)
                exit_code = 125
            else:
                exit_code = int(code.value)

        _finish_child_io(
            writer_thread=writer_thread,
            reader_threads=reader_threads,
            writer_errors=writer_errors,
            close_writer=close_writer,
            label="offline helper",
            terminate=lambda: kernel32.TerminateJobObject(job, 125),
            cancel_io=lambda: _cancel_child_pipe_io(kernel32, pipe_io_handles),
            force_cancel=wait_result in {WAIT_TIMEOUT, WAIT_FAILED} or wait_error is not None,
            ignore_writer_errors=wait_result in {WAIT_TIMEOUT, WAIT_FAILED}
            or wait_error is not None,
        )
        if wait_error is not None:
            raise wait_error
        sys.stdout.buffer.write(outputs["stdout"])
        sys.stderr.buffer.write(outputs["stderr"])
        return exit_code
    finally:
        if process_info.hProcess and not job_assigned:
            kernel32.TerminateProcess(process_info.hProcess, 125)
        close(stdin_write)
        close(stdin_read)
        close(stdout_write)
        close(stderr_write)
        close(stdout_read)
        close(stderr_read)
        close(process_info.hThread)
        close(process_info.hProcess)
        close(job)


def _effective_child_env(payload: HelperPayload) -> dict[str, str]:
    env = dict(payload.env)
    if payload.policy.get("network") == "proxy_allowlist":
        proxy = payload.policy.get("network_proxy") or payload.policy.get("networkProxy")
        if isinstance(proxy, dict):
            from opensquilla.sandbox.backend.windows_default_network import network_proxy_env

            env.update(network_proxy_env(str(proxy["host"]), int(proxy["port"])))
    _inject_git_safe_directory(env, payload.cwd)
    return env


def _inject_git_safe_directory(env: dict[str, str], cwd: Path) -> None:
    root = _find_git_worktree_root_for_safe_directory(cwd)
    if root is None:
        return
    _append_git_config(env, "safe.directory", str(root).replace("\\", "/"))


def _find_git_worktree_root_for_safe_directory(start: Path) -> Path | None:
    try:
        current = start.resolve(strict=False)
    except OSError:
        current = start
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _append_git_config(env: dict[str, str], key: str, value: str) -> None:
    try:
        index = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        index = 0
    env[f"GIT_CONFIG_KEY_{index}"] = key
    env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(index + 1)


def _run_restricted_process_native(
    payload: HelperPayload,
    capability_sids: tuple[str, ...],
) -> int:
    if not sys.platform.startswith("win"):
        raise SystemExit("windows_default runner only runs on native Windows")

    try:
        return _run_restricted_process_native_impl(payload, capability_sids)
    except OSError as exc:
        raise SystemExit(f"windows_default process launch failed: {exc}") from exc


def _finalize_restricted_token(token: int, dacl_sids: Sequence[object]) -> None:
    _set_token_default_dacl(token, dacl_sids)
    _enable_token_privilege(token, "SeChangeNotifyPrivilege")


def _set_token_default_dacl(token: int, dacl_sids: Sequence[object]) -> None:
    if not dacl_sids:
        return
    _set_token_default_dacl_native(token, dacl_sids)


def _enable_token_privilege(token: int, name: str) -> None:
    _enable_token_privilege_native(token, name)


def _set_token_default_dacl_native(token: int, dacl_sids: Sequence[object]) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    DWORD = wintypes.DWORD
    LPVOID = wintypes.LPVOID

    class TRUSTEE_W(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", LPVOID),
            ("MultipleTrusteeOperation", DWORD),
            ("TrusteeForm", DWORD),
            ("TrusteeType", DWORD),
            ("ptstrName", LPVOID),
        ]

    class EXPLICIT_ACCESS_W(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", DWORD),
            ("grfAccessMode", DWORD),
            ("grfInheritance", DWORD),
            ("Trustee", TRUSTEE_W),
        ]

    class TOKEN_DEFAULT_DACL(ctypes.Structure):
        _fields_ = [("DefaultDacl", LPVOID)]

    advapi32.SetEntriesInAclW.argtypes = [
        DWORD,
        ctypes.POINTER(EXPLICIT_ACCESS_W),
        LPVOID,
        ctypes.POINTER(LPVOID),
    ]
    advapi32.SetEntriesInAclW.restype = DWORD
    advapi32.SetTokenInformation.argtypes = [wintypes.HANDLE, DWORD, LPVOID, DWORD]
    advapi32.SetTokenInformation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [LPVOID]
    kernel32.LocalFree.restype = LPVOID

    ERROR_SUCCESS = 0
    GRANT_ACCESS = 1
    TRUSTEE_IS_SID = 0
    TRUSTEE_IS_UNKNOWN = 0
    TOKEN_DEFAULT_DACL_CLASS = 6

    entries = (EXPLICIT_ACCESS_W * len(dacl_sids))()
    for index, sid in enumerate(dacl_sids):
        entries[index].grfAccessPermissions = GENERIC_ALL
        entries[index].grfAccessMode = GRANT_ACCESS
        entries[index].grfInheritance = 0
        entries[index].Trustee.pMultipleTrustee = None
        entries[index].Trustee.MultipleTrusteeOperation = 0
        entries[index].Trustee.TrusteeForm = TRUSTEE_IS_SID
        entries[index].Trustee.TrusteeType = TRUSTEE_IS_UNKNOWN
        entries[index].Trustee.ptstrName = sid

    new_dacl = LPVOID()
    code = advapi32.SetEntriesInAclW(
        len(dacl_sids),
        entries,
        None,
        ctypes.byref(new_dacl),
    )
    if code != ERROR_SUCCESS:
        raise OSError(code, f"SetEntriesInAclW failed: {ctypes.FormatError(code)}")
    try:
        info = TOKEN_DEFAULT_DACL(new_dacl)
        if not advapi32.SetTokenInformation(
            token,
            TOKEN_DEFAULT_DACL_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error_code = ctypes.get_last_error()
            raise OSError(
                error_code,
                f"SetTokenInformation(TokenDefaultDacl) failed: {ctypes.FormatError(error_code)}",
            )
    finally:
        if new_dacl:
            kernel32.LocalFree(new_dacl)


def _enable_token_privilege_native(token: int, name: str) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    DWORD = wintypes.DWORD
    LPVOID = wintypes.LPVOID

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", DWORD), ("HighPart", ctypes.c_long)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", LUID), ("Attributes", DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

    SE_PRIVILEGE_ENABLED = 0x00000002

    advapi32.LookupPrivilegeValueW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.POINTER(LUID),
    ]
    advapi32.LookupPrivilegeValueW.restype = wintypes.BOOL
    advapi32.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE,
        wintypes.BOOL,
        ctypes.POINTER(TOKEN_PRIVILEGES),
        DWORD,
        LPVOID,
        LPVOID,
    ]
    advapi32.AdjustTokenPrivileges.restype = wintypes.BOOL

    luid = LUID()
    if not advapi32.LookupPrivilegeValueW(None, name, ctypes.byref(luid)):
        error_code = ctypes.get_last_error()
        raise OSError(
            error_code,
            f"LookupPrivilegeValueW({name}) failed: {ctypes.FormatError(error_code)}",
        )
    privileges = TOKEN_PRIVILEGES()
    privileges.PrivilegeCount = 1
    privileges.Privileges[0].Luid = luid
    privileges.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
    if not advapi32.AdjustTokenPrivileges(
        token,
        False,
        ctypes.byref(privileges),
        0,
        None,
        None,
    ):
        error_code = ctypes.get_last_error()
        raise OSError(
            error_code,
            f"AdjustTokenPrivileges({name}) failed: {ctypes.FormatError(error_code)}",
        )


def _write_child_stdin(
    kernel32: object,
    stdin_write: object,
    stdin: bytes | None,
    *,
    close_handle: bool = True,
) -> None:
    import ctypes
    from ctypes import wintypes

    try:
        if stdin:
            offset = 0
            while offset < len(stdin):
                chunk = stdin[offset:]
                written = wintypes.DWORD()
                buffer = ctypes.create_string_buffer(chunk)
                if not kernel32.WriteFile(
                    stdin_write,
                    buffer,
                    len(chunk),
                    ctypes.byref(written),
                    None,
                ):
                    raise OSError(ctypes.get_last_error(), "WriteFile(stdin) failed")
                if written.value == 0:
                    raise OSError(0, "WriteFile(stdin) wrote zero bytes")
                offset += written.value
    finally:
        if close_handle:
            kernel32.CloseHandle(stdin_write)


def _run_restricted_process_native_impl(
    payload: HelperPayload,
    capability_sids: tuple[str, ...],
) -> int:
    import ctypes
    import msvcrt
    import threading
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    LPVOID = wintypes.LPVOID
    HANDLE = wintypes.HANDLE
    DWORD = wintypes.DWORD
    BOOL = wintypes.BOOL

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", DWORD),
            ("lpSecurityDescriptor", LPVOID),
            ("bInheritHandle", BOOL),
        ]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Sid", LPVOID),
            ("Attributes", DWORD),
        ]

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", DWORD),
            ("dwY", DWORD),
            ("dwXSize", DWORD),
            ("dwYSize", DWORD),
            ("dwXCountChars", DWORD),
            ("dwYCountChars", DWORD),
            ("dwFillAttribute", DWORD),
            ("dwFlags", DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", HANDLE),
            ("hStdOutput", HANDLE),
            ("hStdError", HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", HANDLE),
            ("hThread", HANDLE),
            ("dwProcessId", DWORD),
            ("dwThreadId", DWORD),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", DWORD),
            ("SchedulingClass", DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    advapi32.OpenProcessToken.argtypes = [HANDLE, DWORD, ctypes.POINTER(HANDLE)]
    advapi32.OpenProcessToken.restype = BOOL
    advapi32.GetTokenInformation.argtypes = [HANDLE, DWORD, LPVOID, DWORD, ctypes.POINTER(DWORD)]
    advapi32.GetTokenInformation.restype = BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(LPVOID)]
    advapi32.ConvertStringSidToSidW.restype = BOOL
    advapi32.CreateRestrictedToken.argtypes = [
        HANDLE,
        DWORD,
        DWORD,
        LPVOID,
        DWORD,
        LPVOID,
        DWORD,
        ctypes.POINTER(SID_AND_ATTRIBUTES),
        ctypes.POINTER(HANDLE),
    ]
    advapi32.CreateRestrictedToken.restype = BOOL
    advapi32.CreateProcessAsUserW.argtypes = [
        HANDLE,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        LPVOID,
        LPVOID,
        BOOL,
        DWORD,
        LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFO),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessAsUserW.restype = BOOL
    advapi32.CreateProcessWithTokenW.argtypes = [
        HANDLE,
        DWORD,
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        DWORD,
        LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFO),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessWithTokenW.restype = BOOL

    kernel32.GetCurrentProcess.restype = HANDLE
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(HANDLE),
        ctypes.POINTER(HANDLE),
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        DWORD,
    ]
    kernel32.CreatePipe.restype = BOOL
    kernel32.SetHandleInformation.argtypes = [HANDLE, DWORD, DWORD]
    kernel32.SetHandleInformation.restype = BOOL
    kernel32.CloseHandle.argtypes = [HANDLE]
    kernel32.CloseHandle.restype = BOOL
    kernel32.CreateJobObjectW.argtypes = [LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = HANDLE
    kernel32.SetInformationJobObject.argtypes = [HANDLE, ctypes.c_int, LPVOID, DWORD]
    kernel32.SetInformationJobObject.restype = BOOL
    kernel32.AssignProcessToJobObject.argtypes = [HANDLE, HANDLE]
    kernel32.AssignProcessToJobObject.restype = BOOL
    kernel32.ResumeThread.argtypes = [HANDLE]
    kernel32.ResumeThread.restype = DWORD
    kernel32.CancelIoEx.argtypes = [HANDLE, LPVOID]
    kernel32.CancelIoEx.restype = BOOL
    kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
    kernel32.WaitForSingleObject.restype = DWORD
    kernel32.TerminateJobObject.argtypes = [HANDLE, DWORD]
    kernel32.TerminateJobObject.restype = BOOL
    kernel32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
    kernel32.GetExitCodeProcess.restype = BOOL
    kernel32.WriteFile.argtypes = [HANDLE, LPVOID, DWORD, ctypes.POINTER(DWORD), LPVOID]
    kernel32.WriteFile.restype = BOOL
    kernel32.SetErrorMode.argtypes = [DWORD]
    kernel32.SetErrorMode.restype = DWORD

    TOKEN_GROUPS_CLASS = 2
    SE_GROUP_LOGON_ID = 0xC0000000
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    WAIT_TIMEOUT = 0x00000102
    WAIT_FAILED = 0xFFFFFFFF

    def win_error(label: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{label} failed: {ctypes.FormatError(code)}")

    def close(handle: int) -> None:
        if handle:
            kernel32.CloseHandle(handle)

    def convert_sid(value: str, label: str) -> object:
        sid = LPVOID()
        if not advapi32.ConvertStringSidToSidW(value, ctypes.byref(sid)):
            raise win_error(f"ConvertStringSidToSidW({label})")
        return sid

    def logon_sid_from_token(token: int) -> tuple[object | None, object | None]:
        needed = DWORD()
        advapi32.GetTokenInformation(token, TOKEN_GROUPS_CLASS, None, 0, ctypes.byref(needed))
        if not needed.value:
            return None, None
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_GROUPS_CLASS,
            buffer,
            needed,
            ctypes.byref(needed),
        ):
            return None, None
        group_count = ctypes.cast(buffer, ctypes.POINTER(DWORD)).contents.value
        offset = ctypes.sizeof(DWORD)
        align = ctypes.alignment(SID_AND_ATTRIBUTES)
        offset = (offset + align - 1) & ~(align - 1)
        groups_type = SID_AND_ATTRIBUTES * group_count
        groups = groups_type.from_buffer(buffer, offset)
        for group in groups:
            if group.Attributes & SE_GROUP_LOGON_ID == SE_GROUP_LOGON_ID:
                return group.Sid, buffer
        return None, buffer

    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [LPVOID]
    local_free.restype = LPVOID

    source_token = HANDLE()
    restricted_token = HANDLE()
    allocated_sids: list[object] = []
    logon_sid_buffer: object | None = None
    stdin_read = HANDLE()
    stdin_write = HANDLE()
    stdout_read = HANDLE()
    stdout_write = HANDLE()
    stderr_read = HANDLE()
    stderr_write = HANDLE()
    job = HANDLE()
    process_info = PROCESS_INFORMATION()
    reader_threads: list[threading.Thread] = []
    outputs: dict[str, bytes] = {"stdout": b"", "stderr": b""}
    job_assigned = False

    try:
        source_token = HANDLE(_open_source_token_for_payload(payload))

        capability_sid_ptrs = []
        for index, capability_sid in enumerate(capability_sids):
            sid = convert_sid(capability_sid, f"capability-{index}")
            allocated_sids.append(sid)
            capability_sid_ptrs.append(sid)

        logon_sid, logon_sid_buffer = logon_sid_from_token(source_token)
        base_sid_ptrs = []
        for sid_value, sid_label in _base_restricting_sid_specs():
            sid = convert_sid(sid_value, sid_label)
            allocated_sids.append(sid)
            base_sid_ptrs.append(sid)
        restricting_sids = _ordered_restricting_sids(
            capability_sids=tuple(capability_sid_ptrs),
            user_sid=None,
            logon_sid=logon_sid,
            base_sids=tuple(base_sid_ptrs),
        )
        restricting_entries = (SID_AND_ATTRIBUTES * len(restricting_sids))()
        for index, sid in enumerate(restricting_sids):
            restricting_entries[index].Sid = sid
            restricting_entries[index].Attributes = 0

        if not advapi32.CreateRestrictedToken(
            source_token,
            RESTRICTED_TOKEN_FLAGS,
            0,
            None,
            0,
            None,
            len(restricting_sids),
            restricting_entries,
            ctypes.byref(restricted_token),
        ):
            raise win_error("CreateRestrictedToken")
        dacl_sids = []
        if logon_sid:
            dacl_sids.append(logon_sid)
        dacl_sids.extend(base_sid_ptrs)
        dacl_sids.extend(capability_sid_ptrs)
        _finalize_restricted_token(restricted_token, dacl_sids)

        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = None
        sa.bInheritHandle = True
        if not kernel32.CreatePipe(
            ctypes.byref(stdin_read),
            ctypes.byref(stdin_write),
            ctypes.byref(sa),
            0,
        ):
            raise win_error("CreatePipe(stdin)")
        _clear_handle_inheritance(kernel32, stdin_write, "stdin")
        if not kernel32.CreatePipe(
            ctypes.byref(stdout_read),
            ctypes.byref(stdout_write),
            ctypes.byref(sa),
            0,
        ):
            raise win_error("CreatePipe(stdout)")
        if not kernel32.CreatePipe(
            ctypes.byref(stderr_read),
            ctypes.byref(stderr_write),
            ctypes.byref(sa),
            0,
        ):
            raise win_error("CreatePipe(stderr)")
        _clear_handle_inheritance(kernel32, stdout_read, "stdout")
        _clear_handle_inheritance(kernel32, stderr_read, "stderr")

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise win_error("CreateJobObjectW")
        limit_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limit_info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limit_info),
            ctypes.sizeof(limit_info),
        ):
            raise win_error("SetInformationJobObject")

        startup = STARTUPINFO()
        startup.cb = ctypes.sizeof(STARTUPINFO)
        startup.lpDesktop = "winsta0\\default"
        startup.dwFlags = _restricted_process_startup_flags()
        startup.wShowWindow = SW_HIDE
        startup.hStdInput = stdin_read
        startup.hStdOutput = stdout_write
        startup.hStdError = stderr_write

        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(payload.argv))
        application_name = _restricted_process_application_name(payload.argv)
        env_block = ctypes.create_unicode_buffer(_environment_block(_effective_child_env(payload)))
        creation_flags = _restricted_process_creation_flags()
        previous_error_mode = kernel32.SetErrorMode(_runner_error_mode_flags())
        create_failures: list[tuple[str, int, str]] = []
        try:
            created = advapi32.CreateProcessAsUserW(
                restricted_token,
                application_name,
                command_line,
                None,
                None,
                True,
                creation_flags,
                env_block,
                str(payload.cwd),
                ctypes.byref(startup),
                ctypes.byref(process_info),
            )
            if not created:
                error_code = ctypes.get_last_error()
                create_failures.append(
                    (
                        "CreateProcessAsUserW",
                        error_code,
                        ctypes.FormatError(error_code).strip(),
                    )
                )
                command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(payload.argv))
                created = advapi32.CreateProcessWithTokenW(
                    restricted_token,
                    0,
                    application_name,
                    command_line,
                    creation_flags,
                    env_block,
                    str(payload.cwd),
                    ctypes.byref(startup),
                    ctypes.byref(process_info),
                )
                if not created:
                    error_code = ctypes.get_last_error()
                    create_failures.append(
                        (
                            "CreateProcessWithTokenW",
                            error_code,
                            ctypes.FormatError(error_code).strip(),
                        )
                    )
        finally:
            kernel32.SetErrorMode(previous_error_mode)
        if not created:
            if create_failures:
                code = create_failures[-1][1]
                details = "; ".join(
                    f"{name}={error_code} {message}"
                    for name, error_code, message in create_failures
                )
                raise OSError(
                    code,
                    f"CreateProcessAsUserW/CreateProcessWithTokenW failed: {details}",
                )
            raise win_error("CreateProcessAsUserW/CreateProcessWithTokenW")

        close(stdin_read)
        stdin_read = HANDLE()
        close(stdout_write)
        stdout_write = HANDLE()
        close(stderr_write)
        stderr_write = HANDLE()

        if not kernel32.AssignProcessToJobObject(job, process_info.hProcess):
            raise win_error("AssignProcessToJobObject")
        job_assigned = True
        if kernel32.ResumeThread(process_info.hThread) == WAIT_FAILED:
            raise win_error("ResumeThread")

        pipe_io_handles = (stdin_write, stdout_read, stderr_read)

        def read_pipe(name: str, handle: object) -> None:
            raw_handle = getattr(handle, "value", handle)
            fd = msvcrt.open_osfhandle(int(raw_handle), os.O_RDONLY | os.O_BINARY)
            with os.fdopen(fd, "rb", closefd=True) as stream:
                outputs[name] = stream.read()

        for name, handle in (("stdout", stdout_read), ("stderr", stderr_read)):
            thread = threading.Thread(target=read_pipe, args=(name, handle), daemon=True)
            thread.start()
            reader_threads.append(thread)
        stdout_read = HANDLE()
        stderr_read = HANDLE()

        writer_thread, writer_errors, close_writer = _start_child_stdin_writer(
            kernel32,
            stdin_write,
            payload.stdin,
            on_error=lambda: kernel32.TerminateJobObject(job, 125),
        )
        stdin_write = HANDLE()

        wait_ms = max(1, int(payload.timeout * 1000))
        wait_result = kernel32.WaitForSingleObject(process_info.hProcess, wait_ms)
        wait_error: OSError | None = None
        if wait_result == WAIT_TIMEOUT:
            kernel32.TerminateJobObject(job, 124)
            kernel32.WaitForSingleObject(process_info.hProcess, 5000)
            exit_code = 124
        elif wait_result == WAIT_FAILED:
            wait_error = win_error("WaitForSingleObject")
            kernel32.TerminateJobObject(job, 125)
            exit_code = 125
        else:
            code = DWORD()
            if not kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(code)):
                wait_error = win_error("GetExitCodeProcess")
                kernel32.TerminateJobObject(job, 125)
                exit_code = 125
            else:
                exit_code = int(code.value)

        _finish_child_io(
            writer_thread=writer_thread,
            reader_threads=reader_threads,
            writer_errors=writer_errors,
            close_writer=close_writer,
            label="restricted process",
            terminate=lambda: kernel32.TerminateJobObject(job, 125),
            cancel_io=lambda: _cancel_child_pipe_io(kernel32, pipe_io_handles),
            force_cancel=wait_result in {WAIT_TIMEOUT, WAIT_FAILED} or wait_error is not None,
            ignore_writer_errors=wait_result in {WAIT_TIMEOUT, WAIT_FAILED}
            or wait_error is not None,
        )
        if wait_error is not None:
            raise wait_error
        sys.stdout.buffer.write(outputs["stdout"])
        sys.stderr.buffer.write(outputs["stderr"])
        return exit_code
    finally:
        if process_info.hProcess and not job_assigned:
            kernel32.TerminateProcess(process_info.hProcess, 125)
        close(stdin_read)
        close(stdin_write)
        close(stdout_write)
        close(stderr_write)
        close(stdout_read)
        close(stderr_read)
        close(process_info.hThread)
        close(process_info.hProcess)
        close(job)
        close(restricted_token)
        close(source_token)
        for sid in allocated_sids:
            if sid:
                local_free(sid)
        _ = logon_sid_buffer


if __name__ == "__main__":
    main()
