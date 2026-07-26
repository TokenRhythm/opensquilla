"""macOS Seatbelt backend.

Executes requests through ``sandbox-exec`` with a generated SBPL profile.
Seatbelt is not a Linux namespace equivalent: paths stay as host paths, there
is no PID/user namespace, and V1 intentionally supports only host network or
no network. The profile is still deny-by-default for filesystem and network
access, with read/write allowances compiled from the shared filesystem
profile plus private worker runtime transport and backend-owned temporary
directories.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import sysconfig
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import Any, cast

from opensquilla.sandbox.backend.base import Backend
from opensquilla.sandbox.backend.filesystem_worker_policy import (
    build_filesystem_worker_policy,
)
from opensquilla.sandbox.managed_proxy_env import managed_proxy_env
from opensquilla.sandbox.operation_runtime import (
    SANDBOX_FILESYSTEM_WRITE_KINDS,
    FilesystemOperationRequest,
    SandboxOperation,
    SandboxOperationDomain,
    SandboxOperationResult,
)
from opensquilla.sandbox.permissions import FileSystemAccess, FileSystemPermissionProfile
from opensquilla.sandbox.run_mode import normalize_run_mode
from opensquilla.sandbox.types import (
    NetworkMode,
    NetworkProxySpec,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
)

log = logging.getLogger(__name__)

_SANDBOX_EXEC_NAME = "sandbox-exec"
_SANDBOX_EXEC_SYSTEM_PATH = Path("/usr/bin/sandbox-exec")
_FILESYSTEM_WORKER_MODULE = "opensquilla.sandbox.filesystem_worker"
_FILESYSTEM_PATH_OPERATION_KINDS = frozenset(
    {
        "read_file",
        "list_dir",
        "glob_search",
        "grep_search",
        "write_text",
        "edit_text",
    }
)
_FILESYSTEM_OPERATION_KINDS = _FILESYSTEM_PATH_OPERATION_KINDS | {"apply_patch"}
_FILESYSTEM_WORKER_ENV_ALLOWLIST = (
    "PATH",
    "PYTHONPATH",
    "PYTHONDONTWRITEBYTECODE",
    "LANG",
    "LC_ALL",
)
_OUTPUT_BYTE_CAP = 1_048_576
_TERMINATE_GRACE_S = 2.0

# This mirrors Codex's macOS Seatbelt posture for workspace-write:
# deny by default, allow ordinary macOS runtime services, allow full-disk reads,
# and constrain writes to explicit writable roots.
_SEATBELT_BASE_POLICY = """(version 1)

; start with closed-by-default
(deny default)

; child processes inherit the policy of their parent
(allow process-exec)
(allow process-fork)
(allow signal (target same-sandbox))

; process-info
(allow process-info* (target same-sandbox))

(allow file-write-data
  (require-all
    (path "/dev/null")
    (vnode-type CHARACTER-DEVICE)))

; sysctls permitted.
(allow sysctl-read)

; IOKit and common macOS runtime services.
(allow iokit-open
  (iokit-registry-entry-class "RootDomainUserClient"))

(allow mach-lookup
  (global-name "com.apple.system.opendirectoryd.libinfo"))

; Needed for python multiprocessing on macOS for SemLock.
(allow ipc-posix-sem)

; Needed for PyTorch/libomp on macOS to register OpenMP runtimes.
(allow ipc-posix-shm-read-data
  ipc-posix-shm-write-create
  ipc-posix-shm-write-unlink
  (ipc-posix-name-regex #"^/__KMP_REGISTERED_LIB_[0-9]+$"))

(allow mach-lookup
  (global-name "com.apple.PowerManagement.control"))

; allow openpty()
(allow pseudo-tty)
(allow file-read* file-write* file-ioctl (literal "/dev/ptmx"))
(allow file-read* file-write*
  (require-all
    (regex #"^/dev/ttys[0-9]+")
    (extension "com.apple.sandbox.pty")))
(allow file-ioctl (regex #"^/dev/ttys[0-9]+"))

; allow readonly user preferences
(allow ipc-posix-shm-read* (ipc-posix-name-prefix "apple.cfprefs."))
(allow mach-lookup
  (global-name "com.apple.cfprefsd.daemon")
  (global-name "com.apple.cfprefsd.agent")
  (local-name "com.apple.cfprefsd.agent"))
(allow user-preference-read)
"""

_SEATBELT_NETWORK_POLICY = """; allow safe AF_SYSTEM sockets used for local platform services.
(allow system-socket
  (require-all
    (socket-domain AF_SYSTEM)
    (socket-protocol 2)))

(allow mach-lookup
  (global-name "com.apple.bsd.dirhelper")
  (global-name "com.apple.system.opendirectoryd.membership")
  (global-name "com.apple.SecurityServer")
  (global-name "com.apple.networkd")
  (global-name "com.apple.ocspd")
  (global-name "com.apple.trustd.agent")
  (global-name "com.apple.SystemConfiguration.DNSConfiguration")
  (global-name "com.apple.SystemConfiguration.configd"))

(allow sysctl-read
  (sysctl-name-regex #"^net.routetable"))
"""

_PROTECTED_SUBPATH_NAMES = (".git", ".codex", ".agents")
_SEATBELT_LOOPBACK_PROXY_HOSTS = {"127.0.0.1", "::1", "localhost"}


@dataclass(frozen=True)
class _SeatbeltPrivateTransport:
    read_roots: tuple[Path, ...] = ()
    write_roots: tuple[Path, ...] = ()

    def extended(
        self,
        *,
        read_roots: tuple[Path, ...] = (),
        write_roots: tuple[Path, ...] = (),
    ) -> _SeatbeltPrivateTransport:
        return _SeatbeltPrivateTransport(
            read_roots=(*self.read_roots, *read_roots),
            write_roots=(*self.write_roots, *write_roots),
        )


@dataclass(frozen=True)
class _FilesystemWorkerLaunch:
    request: SandboxRequest
    private_transport: _SeatbeltPrivateTransport


def _sandbox_exec_binary(binary: str | None = None) -> str | None:
    if binary is not None:
        return shutil.which(binary)
    if _SANDBOX_EXEC_SYSTEM_PATH.exists():
        return str(_SANDBOX_EXEC_SYSTEM_PATH)
    return shutil.which(_SANDBOX_EXEC_NAME)


def _validate_mount_path(path: PurePath, *, kind: str) -> None:
    if _has_disallowed_path_character(path):
        raise SandboxBackendError(f"{kind} path contains a control character: {path!r}")
    if not path.is_absolute():
        raise SandboxBackendError(f"{kind} path must be absolute: {path!r}")
    if any(part == ".." for part in path.parts):
        raise SandboxBackendError(f"{kind} path contains '..': {path!r}")


def _has_disallowed_path_character(path: PurePath) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in str(path))


def _validate_request(request: SandboxRequest) -> None:
    if not request.argv:
        raise SandboxBackendError("seatbelt request argv must not be empty")
    _validate_mount_path(request.cwd, kind="cwd")
    if not request.cwd.exists():
        raise SandboxBackendError(f"cwd missing on host: {request.cwd!r}")
    for spec in request.policy.mounts:
        _validate_mount_path(spec.host_path, kind="host mount")
        _validate_mount_path(spec.sandbox_path, kind="sandbox mount")
        if spec.required and not spec.host_path.exists():
            raise SandboxBackendError(f"required mount missing on host: {spec.host_path!r}")


def _scheme_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _literal(path: Path) -> str:
    return f"(literal {_scheme_string(str(path))})"


def _subpath(path: Path) -> str:
    return f"(subpath {_scheme_string(str(path))})"


def seatbelt_env_for_policy(
    policy: SandboxPolicy,
    override_env: dict[str, str],
    *,
    tmp_dir: Path | None,
) -> dict[str, str]:
    allowlist = set(policy.env_allowlist)
    resolved: dict[str, str] = {}
    for key in policy.env_allowlist:
        value = os.environ.get(key)
        if value is not None:
            resolved[key] = value
    for key, value in override_env.items():
        if key not in allowlist:
            log.debug("sandbox.seatbelt_env_override_rejected: key=%s", key)
            continue
        resolved[key] = value
    if tmp_dir is not None:
        resolved["TMPDIR"] = str(tmp_dir)
        resolved.update(_tool_cache_env(tmp_dir))
    if policy.network == NetworkMode.PROXY_ALLOWLIST:
        if policy.network_proxy is None:
            raise SandboxBackendError(
                "NetworkMode.PROXY_ALLOWLIST requires a network proxy "
                "for the seatbelt backend"
            )
        resolved.update(
            managed_proxy_env(
                policy.network_proxy.host,
                policy.network_proxy.port,
            )
        )
    return resolved


_env_for_policy = seatbelt_env_for_policy


def _tool_cache_env(tmp_dir: Path) -> dict[str, str]:
    cache_root = tmp_dir / "cache"
    return {
        "XDG_CACHE_HOME": str(cache_root / "xdg"),
        "npm_config_cache": str(cache_root / "npm"),
        "NPM_CONFIG_CACHE": str(cache_root / "npm"),
        "PIP_CACHE_DIR": str(cache_root / "pip"),
        "UV_CACHE_DIR": str(cache_root / "uv"),
    }


def _regex_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _protected_metadata_regex(root: Path, name: str) -> str:
    root_text = str(root).rstrip("/")
    if not root_text:
        root_text = "/"
    escaped_root = re.escape(root_text)
    escaped_name = re.escape(name)
    if root_text == "/":
        return f"^/{escaped_name}(/.*)?$"
    return f"^{escaped_root}/{escaped_name}(/.*)?$"


def _seatbelt_path(path: PurePath) -> Path:
    candidate = Path(str(path))
    if _has_disallowed_path_character(candidate):
        raise SandboxBackendError(
            f"filesystem profile path contains a control character: {candidate!r}"
        )
    _validate_mount_path(candidate, kind="filesystem profile")
    return candidate


def _unique_seatbelt_paths(paths: tuple[PurePath, ...]) -> tuple[Path, ...]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        candidate = _seatbelt_path(path)
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return tuple(unique)


def _seatbelt_access_rule(
    action: str,
    root: Path,
    excluded: tuple[Path, ...],
) -> str:
    if action not in {"file-read*", "file-write*"}:
        raise ValueError(f"unsupported Seatbelt filesystem action: {action!r}")
    root = _seatbelt_path(root)
    excluded = _unique_seatbelt_paths(excluded)
    guards: list[str] = []
    for path in excluded:
        guards.append(f"(require-not {_literal(path)})")
        guards.append(f"(require-not {_subpath(path)})")
    if action == "file-write*":
        for name in _PROTECTED_SUBPATH_NAMES:
            regex = _regex_string(_protected_metadata_regex(root, name))
            guards.append(f'(require-not (regex #"{regex}"))')
    guard_suffix = f" {' '.join(guards)}" if guards else ""
    exact = f"(require-all {_literal(root)}{guard_suffix})"
    descendants = f"(require-all {_subpath(root)}{guard_suffix})"
    return f"(allow {action} {exact} {descendants})"


def _seatbelt_proxy_endpoint(proxy: NetworkProxySpec) -> str:
    host = proxy.host.strip().lower()
    if host not in _SEATBELT_LOOPBACK_PROXY_HOSTS:
        raise SandboxBackendError(
            "seatbelt proxy allowlist requires a loopback network_proxy host "
            f"(got {proxy.host!r})"
        )
    if not 1 <= proxy.port <= 65535:
        raise SandboxBackendError(
            f"seatbelt proxy allowlist requires a valid network_proxy port (got {proxy.port!r})"
        )
    return f"localhost:{proxy.port}"


def _network_proxy_rule(proxy: NetworkProxySpec) -> str:
    endpoint = _seatbelt_proxy_endpoint(proxy)
    return f"(allow network-outbound (remote ip {_scheme_string(endpoint)}))"


def _filesystem_profile(policy: SandboxPolicy) -> FileSystemPermissionProfile:
    profile = policy.file_system
    if profile is None:
        log.error("sandbox.seatbelt_filesystem_profile_missing: deny all filesystem access")
        return FileSystemPermissionProfile(entries=())
    _validate_filesystem_profile_paths(profile)
    if profile.denied_read_globs:
        log.error(
            "sandbox.seatbelt_denied_glob_unsupported: patterns=%r",
            profile.denied_read_globs,
        )
        raise SandboxBackendError("seatbelt cannot safely enforce denied read glob rules")
    return profile


def _validate_filesystem_profile_paths(profile: FileSystemPermissionProfile) -> None:
    for entry in profile.entries:
        _seatbelt_path(entry.path)


def _profile_roots(
    profile: FileSystemPermissionProfile,
    accesses: frozenset[FileSystemAccess],
) -> tuple[Path, ...]:
    return _unique_seatbelt_paths(
        tuple(entry.path for entry in profile.effective_entries if entry.access in accesses)
    )


def _profile_exclusions(
    profile: FileSystemPermissionProfile,
    root: Path,
    accesses: frozenset[FileSystemAccess],
) -> tuple[Path, ...]:
    excluded: list[PurePath] = []
    for entry in profile.effective_entries:
        if entry.access not in accesses:
            continue
        path = _seatbelt_path(entry.path)
        if path.is_relative_to(root):
            excluded.append(path)
    return _unique_seatbelt_paths(tuple(excluded))


def _private_transport_exclusions(
    profile: FileSystemPermissionProfile,
    root: Path,
    accesses: frozenset[FileSystemAccess],
) -> tuple[Path, ...]:
    if profile.is_explicitly_denied(root):
        raise SandboxBackendError(
            f"seatbelt private transport root is explicitly denied by filesystem profile: {root}"
        )
    return _profile_exclusions(profile, root, accesses)


def _profile_read_rules(profile: FileSystemPermissionProfile) -> list[str]:
    read_roots = list(
        _profile_roots(
            profile,
            frozenset({FileSystemAccess.READ, FileSystemAccess.WRITE}),
        )
    )
    root = Path("/")
    if profile.has_full_disk_read_baseline and root not in read_roots:
        read_roots.insert(0, root)
    denied = frozenset({FileSystemAccess.DENY})
    return [
        _seatbelt_access_rule(
            "file-read*",
            read_root,
            _profile_exclusions(profile, read_root, denied),
        )
        for read_root in read_roots
    ]


def _profile_write_rules(profile: FileSystemPermissionProfile) -> list[str]:
    write_roots = list(_profile_roots(profile, frozenset({FileSystemAccess.WRITE})))
    root = Path("/")
    if profile.default_access is FileSystemAccess.WRITE and root not in write_roots:
        write_roots.insert(0, root)
    carveouts = frozenset({FileSystemAccess.READ, FileSystemAccess.DENY})
    return [
        _seatbelt_access_rule(
            "file-write*",
            write_root,
            _profile_exclusions(profile, write_root, carveouts),
        )
        for write_root in write_roots
    ]


def _private_transport_roots(
    private_transport: _SeatbeltPrivateTransport | None,
    *,
    require_exists: bool,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    if private_transport is None:
        return (), ()
    readable = _unique_seatbelt_paths(private_transport.read_roots)
    writable = _unique_seatbelt_paths(private_transport.write_roots)
    if require_exists:
        for root in (*readable, *writable):
            if not root.exists():
                raise SandboxBackendError(
                    f"seatbelt private transport root is missing on host: {root!r}"
                )
    return readable, writable


def _render_seatbelt_profile(
    request: SandboxRequest,
    *,
    private_transport: _SeatbeltPrivateTransport | None,
    require_private_roots: bool = True,
) -> str:
    """Render a deny-by-default SBPL profile for ``request``."""
    policy = request.policy
    file_system = _filesystem_profile(policy)
    if policy.network == NetworkMode.PROXY_ALLOWLIST:
        if policy.network_proxy is None:
            raise SandboxBackendError(
                "NetworkMode.PROXY_ALLOWLIST requires a network proxy "
                "for the seatbelt backend"
            )

    lines: list[str] = [
        _SEATBELT_BASE_POLICY.rstrip(),
        "; allow read-only file operations",
    ]
    lines.extend(_profile_read_rules(file_system))
    lines.extend(_profile_write_rules(file_system))

    private_read_roots, private_write_roots = _private_transport_roots(
        private_transport,
        require_exists=require_private_roots,
    )
    denied = frozenset({FileSystemAccess.DENY})
    lines.extend(
        _seatbelt_access_rule(
            "file-read*",
            root,
            _private_transport_exclusions(file_system, root, denied),
        )
        for root in private_read_roots
    )
    carveouts = frozenset({FileSystemAccess.READ, FileSystemAccess.DENY})
    for root in private_write_roots:
        exclusions = _private_transport_exclusions(file_system, root, carveouts)
        if root in exclusions:
            raise SandboxBackendError(
                f"seatbelt private transport root is explicitly read-only: {root}"
            )
        lines.append(_seatbelt_access_rule("file-write*", root, exclusions))

    if policy.network == NetworkMode.NONE:
        lines.append("(deny network*)")
    elif policy.network == NetworkMode.HOST:
        lines.append("(allow network-outbound)")
        lines.append("(allow network-inbound)")
        lines.append(_SEATBELT_NETWORK_POLICY.rstrip())
    elif policy.network == NetworkMode.PROXY_ALLOWLIST:
        if policy.network_proxy is None:  # pragma: no cover - guarded above
            raise SandboxBackendError(
                "NetworkMode.PROXY_ALLOWLIST requires a network proxy "
                "for the seatbelt backend"
            )
        lines.append(_network_proxy_rule(policy.network_proxy))
        lines.append(_SEATBELT_NETWORK_POLICY.rstrip())
    else:  # pragma: no cover - exhaustive guard for future enum values
        raise SandboxBackendError(f"unsupported seatbelt network mode: {policy.network!r}")

    return "\n".join(lines) + "\n"


def render_seatbelt_profile(
    request: SandboxRequest,
    *,
    tmp_dir: Path | None = None,
) -> str:
    """Render a public request without backend-private transport grants."""
    private_transport = (
        _SeatbeltPrivateTransport(read_roots=(tmp_dir,), write_roots=(tmp_dir,))
        if tmp_dir is not None
        else None
    )
    return _render_seatbelt_profile(
        request,
        private_transport=private_transport,
    )


def _render_sbpl_skeleton(policy: SandboxPolicy) -> str:
    """Compatibility helper for existing tests.

    New code should call :func:`render_seatbelt_profile` with a full request so
    the renderer can include cwd, executable, and temporary-directory rules.
    """
    cwd = Path.cwd()
    return render_seatbelt_profile(
        SandboxRequest(
            argv=("sh", "-c", "true"),
            cwd=cwd,
            action_kind="seatbelt.profile",
            policy=policy,
        )
    )


def build_seatbelt_argv(
    request: SandboxRequest,
    profile_path: Path,
    *,
    binary: str | None = None,
) -> list[str]:
    resolved = _sandbox_exec_binary(binary)
    if resolved is None:
        label = binary or _SANDBOX_EXEC_NAME
        raise SandboxBackendError(f"seatbelt backend unavailable: missing {label!r} binary")
    _validate_mount_path(profile_path, kind="profile")
    return [resolved, "-f", str(profile_path), *request.argv]


def _filesystem_request(operation: SandboxOperation) -> FilesystemOperationRequest:
    if not isinstance(operation.request, FilesystemOperationRequest):
        raise SandboxBackendError("filesystem operation is missing filesystem request")
    return operation.request


def _canonical_filesystem_target(path: Path, *, kind: str) -> Path:
    if _has_disallowed_path_character(path):
        raise SandboxBackendError(
            f"{kind} path contains a control character: {path!r}"
        )
    _validate_mount_path(path, kind=kind)
    try:
        return path.expanduser().resolve(strict=False)
    except OSError as exc:
        raise SandboxBackendError(f"could not canonicalize {kind} path {path}: {exc}") from exc


def _filesystem_operation_targets(
    operation: SandboxOperation,
    request: FilesystemOperationRequest,
) -> tuple[Path, ...]:
    targets: tuple[Path, ...]
    if operation.kind in _FILESYSTEM_PATH_OPERATION_KINDS:
        if request.path is None:
            raise SandboxBackendError(
                f"filesystem operation {operation.kind} requires path"
            )
        targets = (
            _canonical_filesystem_target(request.path, kind="filesystem target"),
        )
    elif operation.kind == "apply_patch":
        if request.root is None:
            raise SandboxBackendError("filesystem operation apply_patch requires root")
        root = _canonical_filesystem_target(request.root, kind="apply_patch root")
        try:
            from opensquilla.tools.builtin import patch as patch_tool

            targets = tuple(
                dict.fromkeys(
                    patch_tool._validate_path(patch_op.path, root)
                    for patch_op in patch_tool._parse_patch(request.patch)
                )
            )
        except Exception as exc:
            raise SandboxBackendError(f"invalid apply_patch targets: {exc}") from exc
    else:
        raise SandboxBackendError(
            f"unsupported filesystem operation: {operation.kind!r}"
        )

    declared = tuple(
        dict.fromkeys(
            _canonical_filesystem_target(path, kind="declared filesystem target")
            for path in request.paths
        )
    )
    if set(declared) != set(targets):
        raise SandboxBackendError(
            "declared filesystem paths do not match derived operation targets: "
            f"declared={tuple(str(path) for path in declared)!r}, "
            f"derived={tuple(str(path) for path in targets)!r}"
        )
    return targets


def _filesystem_operation_launch(
    operation: SandboxOperation,
) -> _FilesystemWorkerLaunch:
    if operation.workspace is None:
        raise SandboxBackendError("filesystem operation is missing workspace")
    workspace = _canonical_filesystem_target(operation.workspace, kind="workspace")
    if not workspace.exists():
        raise SandboxBackendError(f"filesystem operation workspace is missing: {workspace}")
    if not workspace.is_dir():
        raise NotADirectoryError(f"filesystem operation workspace is not a directory: {workspace}")
    request = _filesystem_request(operation)
    if operation.file_system_profile is None:
        raise ValueError("filesystem operation is missing resolved filesystem profile")
    _validate_filesystem_profile_paths(operation.file_system_profile)
    targets = _filesystem_operation_targets(operation, request)
    runtime_roots = _runtime_readonly_roots()
    _validate_filesystem_operation_targets(
        operation,
        request,
        targets,
        runtime_roots,
    )
    _validate_filesystem_private_transport_roots(
        operation.file_system_profile,
        runtime_roots,
    )
    policy = replace(
        build_filesystem_worker_policy(
            operation,
            private_rw_roots=(),
            private_ro_roots=runtime_roots,
            env_allowlist=_FILESYSTEM_WORKER_ENV_ALLOWLIST,
            description=f"macOS filesystem worker policy for {operation.kind}",
        ),
        tmp_writable=False,
    )
    env = _filesystem_worker_env()
    stdin = json.dumps(operation.to_payload(), ensure_ascii=False).encode("utf-8")
    sandbox_request = SandboxRequest(
        argv=(
            str(_python_executable()),
            "-B",
            "-m",
            _FILESYSTEM_WORKER_MODULE,
            "-",
        ),
        cwd=workspace,
        action_kind=f"fs.worker.{operation.kind}",
        policy=policy,
        env=env,
        stdin=stdin,
        reason="sandboxed filesystem side-effect worker",
        run_mode=normalize_run_mode(operation.run_mode).value,
    )
    private_transport = _filesystem_worker_private_transport(
        operation,
        sandbox_request,
        workspace,
        runtime_roots,
    )
    _render_seatbelt_profile(
        sandbox_request,
        private_transport=private_transport,
        require_private_roots=False,
    )
    return _FilesystemWorkerLaunch(
        request=sandbox_request,
        private_transport=private_transport,
    )


def _filesystem_operation_request(
    operation: SandboxOperation,
) -> SandboxRequest:
    return _filesystem_operation_launch(operation).request


def _filesystem_worker_private_transport(
    operation: SandboxOperation,
    request: SandboxRequest,
    workspace: Path,
    runtime_roots: tuple[Path, ...],
) -> _SeatbeltPrivateTransport:
    if operation.kind not in _FILESYSTEM_OPERATION_KINDS:
        raise SandboxBackendError(
            f"unsupported filesystem operation: {operation.kind!r}"
        )
    expected_argv = (
        str(_python_executable()),
        "-B",
        "-m",
        _FILESYSTEM_WORKER_MODULE,
        "-",
    )
    if request.argv != expected_argv:
        raise SandboxBackendError("seatbelt filesystem worker argv is inconsistent")
    if request.action_kind != f"fs.worker.{operation.kind}":
        raise SandboxBackendError("seatbelt filesystem worker action kind is inconsistent")
    if request.cwd != workspace:
        raise SandboxBackendError("seatbelt filesystem worker cwd is inconsistent")
    if (
        request.env != _filesystem_worker_env()
        or request.policy.env_allowlist != _FILESYSTEM_WORKER_ENV_ALLOWLIST
    ):
        raise SandboxBackendError("seatbelt filesystem worker environment is inconsistent")
    if request.stdin is None:
        raise SandboxBackendError("seatbelt filesystem worker stdin must be a JSON object")
    try:
        stdin_payload = json.loads(request.stdin.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxBackendError(
            "seatbelt filesystem worker stdin must be a JSON object"
        ) from exc
    if not isinstance(stdin_payload, dict):
        raise SandboxBackendError("seatbelt filesystem worker stdin must be a JSON object")
    expected_stdin = json.dumps(operation.to_payload(), ensure_ascii=False).encode("utf-8")
    if request.stdin != expected_stdin:
        raise SandboxBackendError("seatbelt filesystem worker stdin is inconsistent")
    if request.policy.tmp_writable:
        raise SandboxBackendError("seatbelt filesystem worker must not create backend tmp")
    readonly_mounts = tuple(
        mount.host_path for mount in request.policy.mounts if mount.mode == "ro"
    )
    writable_mounts = tuple(
        mount.host_path for mount in request.policy.mounts if mount.mode == "rw"
    )
    if readonly_mounts != runtime_roots or writable_mounts:
        raise SandboxBackendError("seatbelt filesystem worker transport roots are inconsistent")
    return _SeatbeltPrivateTransport(
        read_roots=runtime_roots,
        write_roots=(),
    )


def _validate_filesystem_operation_targets(
    operation: SandboxOperation,
    request: FilesystemOperationRequest,
    targets: tuple[Path, ...],
    runtime_roots: tuple[Path, ...],
) -> None:
    target = targets[0] if targets else None
    if operation.kind == "read_file" and target is not None:
        display = request.display_path or str(target)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {display}")
        if not target.is_file():
            raise IsADirectoryError(f"Path is a directory: {display}")
    elif operation.kind == "list_dir" and target is not None:
        display = request.display_path or str(target)
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {display}")
        if not target.is_dir():
            raise NotADirectoryError(f"Not a directory: {display}")
    elif operation.kind in {"glob_search", "grep_search"} and target is not None:
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {target}")
    elif operation.kind == "edit_text" and target is not None:
        if not target.exists():
            raise FileNotFoundError(f"File not found: {target}")
        if not target.is_file():
            raise IsADirectoryError(f"Path is a directory: {target}")
    if operation.kind in SANDBOX_FILESYSTEM_WRITE_KINDS:
        for path in targets:
            for root in runtime_roots:
                if _is_relative_to(path, root):
                    raise SandboxBackendError(
                        f"seatbelt denied read-only runtime filesystem target: {path}"
                    )
    _validate_filesystem_operation_profile_targets(operation, targets)


def _validate_filesystem_operation_profile_targets(
    operation: SandboxOperation,
    targets: tuple[Path, ...],
) -> None:
    profile = operation.file_system_profile
    if profile is None:
        return
    write_required = operation.kind in SANDBOX_FILESYSTEM_WRITE_KINDS
    for path in targets:
        access = profile.resolve(path)
        if write_required and access is not FileSystemAccess.WRITE:
            raise SandboxBackendError(
                "seatbelt filesystem profile requires write access for "
                f"{operation.kind} target: {path} (resolved {access.value})"
            )
        if not write_required and access is FileSystemAccess.DENY:
            raise SandboxBackendError(
                "seatbelt filesystem profile denies read access for "
                f"{operation.kind} target: {path}"
            )


def _validate_filesystem_private_transport_roots(
    profile: FileSystemPermissionProfile,
    runtime_roots: tuple[Path, ...],
) -> None:
    denied = frozenset({FileSystemAccess.DENY})
    for root in runtime_roots:
        _private_transport_exclusions(profile, root, denied)


def _runtime_readonly_roots() -> tuple[Path, ...]:
    executable = _python_executable().expanduser()
    prefix = Path(sys.prefix).expanduser().resolve(strict=False)
    base_prefix = Path(sys.base_prefix).expanduser().resolve(strict=False)
    configured = sysconfig.get_paths()
    candidates = [
        executable.parent,
        executable.resolve(strict=False).parent,
        *((prefix,) if prefix != base_prefix else ()),
        *(
            Path(configured[name])
            for name in ("stdlib", "platstdlib", "purelib", "platlib")
            if configured.get(name)
        ),
        *_opensquilla_import_roots(),
    ]
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        root = candidate.expanduser().resolve(strict=False)
        if root == Path(root.anchor) or not root.exists():
            continue
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return tuple(roots)


def _opensquilla_import_roots() -> tuple[Path, ...]:
    import opensquilla

    package_root = Path(opensquilla.__file__).resolve().parent
    roots = [package_root]
    if package_root.parent.name.lower() == "src":
        roots.append(package_root.parent)
    return tuple(roots)


def _pythonpath_for_worker() -> str:
    roots = _opensquilla_import_roots()
    if not roots:
        return ""
    return str(roots[-1] if roots[-1].name.lower() == "src" else roots[0].parent)


def _python_executable() -> Path:
    return Path(sys.executable)


def _filesystem_worker_env() -> dict[str, str]:
    return {
        "PATH": str(_python_executable().parent),
        "PYTHONPATH": _pythonpath_for_worker(),
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        canonical_candidate = candidate.expanduser().resolve(strict=False)
        canonical_root = root.expanduser().resolve(strict=False)
    except OSError as exc:
        raise SandboxBackendError(
            f"seatbelt could not canonicalize filesystem target {candidate}: {exc}"
        ) from exc
    return canonical_candidate.is_relative_to(canonical_root)


class SeatbeltBackend(Backend):
    """macOS ``sandbox-exec`` backend."""

    name = "seatbelt"

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary

    def available(self) -> bool:
        if sys.platform != "darwin":
            return False
        return _sandbox_exec_binary(self._binary) is not None

    def operation_domains_supported(self) -> frozenset[SandboxOperationDomain]:
        return frozenset({"filesystem"})

    async def run_operation(
        self,
        operation: SandboxOperation,
    ) -> SandboxOperationResult:
        if operation.domain != "filesystem":
            raise SandboxBackendError(
                f"seatbelt backend does not implement {operation.domain} operations"
            )
        if operation.workspace is None:
            raise SandboxBackendError("filesystem operation is missing workspace")
        _filesystem_request(operation)
        launch = _filesystem_operation_launch(operation)
        result = await self._run_request(
            launch.request,
            private_transport=launch.private_transport,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "filesystem worker failed"
            raise SandboxBackendError(f"seatbelt filesystem worker failed: {detail}")
        return SandboxOperationResult.from_worker_stdout(result.stdout)

    async def run(self, request: SandboxRequest) -> SandboxResult:
        return await self._run_request(request, private_transport=None)

    async def _run_request(
        self,
        request: SandboxRequest,
        *,
        private_transport: _SeatbeltPrivateTransport | None,
    ) -> SandboxResult:
        if not self.available():
            raise SandboxBackendError(
                "seatbelt backend unavailable: missing 'sandbox-exec' binary on macOS"
            )
        _validate_request(request)

        tmp_ctx: tempfile.TemporaryDirectory[str] | None = None
        profile_path: Path | None = None
        try:
            tmp_dir: Path | None = None
            if request.policy.tmp_writable:
                tmp_ctx = tempfile.TemporaryDirectory(prefix="opensquilla-seatbelt-tmp-")
                tmp_dir = Path(tmp_ctx.name)

            effective_private_transport = private_transport
            if tmp_dir is not None:
                effective_private_transport = (
                    effective_private_transport or _SeatbeltPrivateTransport()
                ).extended(
                    read_roots=(tmp_dir,),
                    write_roots=(tmp_dir,),
                )
            profile = _render_seatbelt_profile(
                request,
                private_transport=effective_private_transport,
            )
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                prefix="opensquilla-seatbelt-",
                suffix=".sb",
                delete=False,
            ) as profile_file:
                profile_file.write(profile)
                profile_file.flush()
                profile_path = Path(profile_file.name)

            argv = build_seatbelt_argv(request, profile_path, binary=self._binary)
            env = _env_for_policy(request.policy, request.env, tmp_dir=tmp_dir)

            log.info(
                "sandbox.seatbelt_spawn: action=%s level=%s network=%s argv_len=%d",
                request.action_kind,
                request.policy.level.label,
                request.policy.network.value,
                len(argv),
            )

            wall = request.policy.limits.wall_timeout_s
            started = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE if request.stdin is not None else None,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(request.cwd),
                    env=env,
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise SandboxBackendError(f"seatbelt launch failed: {exc}") from exc
            except OSError as exc:
                raise SandboxBackendError(f"seatbelt launch failed: {exc}") from exc

            timed_out = False
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=request.stdin), timeout=wall
                )
            except TimeoutError:
                timed_out = True
                stdout_bytes, stderr_bytes = await _terminate_process_group(proc)

            elapsed = time.monotonic() - started
            stdout, trunc_out = _decode_capped(stdout_bytes)
            stderr, trunc_err = _decode_capped(stderr_bytes)
            returncode = proc.returncode if proc.returncode is not None else -1
            notes: tuple[_SeatbeltNote, ...] = ()
            if not timed_out:
                notes = _classify_denial(
                    request.argv,
                    stderr,
                    stdout=stdout,
                    network=request.policy.network,
                )
                if returncode == 0:
                    notes = tuple(note for note in notes if note.category == "network.denied")
                for note in notes:
                    log.info(
                        "sandbox.seatbelt_note: category=%s argv0=%s blocked_path=%s action=%s",
                        note.category,
                        Path(request.argv[0]).name if request.argv else "",
                        note.blocked_path,
                        request.action_kind,
                    )
            return SandboxResult(
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                wall_time_s=elapsed,
                backend_used=self.name,
                policy_used=request.policy.summary(),
                truncated_stdout=trunc_out,
                truncated_stderr=trunc_err,
                timed_out=timed_out,
                backend_notes=tuple(n.to_user_string() for n in notes),
            )
        finally:
            if profile_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(profile_path)
            if tmp_ctx is not None:
                tmp_ctx.cleanup()


def _decode_capped(raw: bytes | None) -> tuple[str, bool]:
    if not raw:
        return "", False
    if len(raw) <= _OUTPUT_BYTE_CAP:
        return raw.decode("utf-8", errors="replace"), False
    return raw[:_OUTPUT_BYTE_CAP].decode("utf-8", errors="replace"), True


async def _terminate_process_group(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    pid = proc.pid
    os_mod = cast(Any, os)
    signal_mod = cast(Any, signal)
    try:
        os_mod.killpg(pid, signal_mod.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_S)
    except TimeoutError:
        try:
            os_mod.killpg(pid, signal_mod.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await proc.wait()
        except ProcessLookupError:
            pass

    stdout = b""
    stderr = b""
    if proc.stdout is not None:
        try:
            stdout = await proc.stdout.read()
        except Exception:  # noqa: BLE001
            stdout = b""
    if proc.stderr is not None:
        try:
            stderr = await proc.stderr.read()
        except Exception:  # noqa: BLE001
            stderr = b""
    return stdout, stderr


# ─── Denial classifier ───────────────────────────────────────────────────


@dataclass(frozen=True)
class _SeatbeltNote:
    """One classified denial extracted from sandbox-exec stderr."""

    category: str
    hint: str
    blocked_path: Path | None = None

    def to_user_string(self) -> str:
        return f"{self.category}: {self.hint}"


_STDERR_SCAN_BYTES = 8192

_EXECVP_RE = re.compile(
    r"sandbox-exec:\s+execvp\(\)\s+of\s+'([^']+)'\s+failed:\s+Operation not permitted"
)
_DYLD_RE = re.compile(r"dyld(?:\[\d+\])?:\s*Library not loaded:\s*(\S+)")
_OPNOTPERM_RE = re.compile(
    r"(?:at\s+'([^']+)'[^\n]*\(Operation not permitted\))"
    r"|(/[^\s:]+):\s*Operation not permitted"
)
_TMP_RE = re.compile(r"\b(mkstemp|mkdtemp|tmpfile)\b.*(?:permitted|denied|failed)")
_PING_SENDTO_DENIED_RE = re.compile(
    r"\bping6?:\s+sendto:\s+Operation not permitted\b",
    re.IGNORECASE,
)
_PING_PACKET_LOSS_RE = re.compile(
    r"\b100(?:\.0+)?%\s+packet loss\b",
    re.IGNORECASE,
)
_PING_COMMAND_RE = re.compile(
    r"(?:(?<=^)|(?<=[\s;&|]))(?:/[^\s;&|]*/)?ping6?(?=$|[\s;&|])"
)


def _looks_like_ping_invocation(argv: tuple[str, ...]) -> bool:
    return bool(_PING_COMMAND_RE.search(" ".join(argv)))


def _network_is_restricted(network: NetworkMode | None) -> bool:
    return network is not NetworkMode.HOST


def _classify_denial(
    argv: tuple[str, ...],
    stderr: str,
    *,
    stdout: str = "",
    network: NetworkMode | None = None,
) -> tuple[_SeatbeltNote, ...]:
    """Scan the tail of ``stderr`` for known Seatbelt denial signatures."""
    if not stderr and not stdout:
        return ()
    tail = stderr[-_STDERR_SCAN_BYTES:]
    stdout_tail = stdout[-_STDERR_SCAN_BYTES:]
    notes: list[_SeatbeltNote] = []
    seen: set[tuple[str, str]] = set()

    def _add(note: _SeatbeltNote) -> None:
        key = (note.category, str(note.blocked_path))
        if key in seen:
            return
        seen.add(key)
        notes.append(note)

    for match in _EXECVP_RE.finditer(tail):
        path = Path(match.group(1))
        _add(_SeatbeltNote(
            category="execve.denied",
            hint=f"sandbox blocked execve of {path}",
            blocked_path=path,
        ))

    for match in _DYLD_RE.finditer(tail):
        path = Path(match.group(1))
        _add(_SeatbeltNote(
            category="filesystem.read",
            hint=f"dyld could not load {path}",
            blocked_path=path,
        ))

    for match in _OPNOTPERM_RE.finditer(tail):
        raw_path = match.group(1) or match.group(2)
        if not raw_path:
            continue
        path = Path(raw_path)
        if any(n.blocked_path == path for n in notes):
            continue
        _add(_SeatbeltNote(
            category="filesystem.read",
            hint=f"sandbox blocked access to {path}",
            blocked_path=path,
        ))

    if _TMP_RE.search(tail):
        _add(_SeatbeltNote(category="tmp.denied", hint="sandbox denied a tmp-directory operation"))

    if _looks_like_ping_invocation(argv):
        if _PING_SENDTO_DENIED_RE.search(tail):
            _add(_SeatbeltNote(
                category="network.denied",
                hint="sandbox blocked raw ICMP ping traffic",
            ))
        elif _network_is_restricted(network) and _PING_PACKET_LOSS_RE.search(stdout_tail):
            _add(_SeatbeltNote(
                category="network.denied",
                hint="sandbox blocked raw ICMP ping traffic",
            ))

    return tuple(notes)


__all__ = [
    "SeatbeltBackend",
    "build_seatbelt_argv",
    "render_seatbelt_profile",
    "seatbelt_env_for_policy",
    "_render_sbpl_skeleton",
]
