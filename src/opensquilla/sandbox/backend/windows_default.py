"""Native Windows default sandbox backend adapter."""

from __future__ import annotations

import asyncio
import base64
import json
import ntpath
import os
import sys
import time
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from opensquilla.sandbox.backend.base import Backend
from opensquilla.sandbox.backend.filesystem_worker_policy import (
    build_filesystem_worker_policy,
)
from opensquilla.sandbox.backend.windows_default_acl import (
    AclAccess,
    AclGrant,
    AclGrantKind,
    plan_acl_refresh,
)
from opensquilla.sandbox.backend.windows_default_cache import (
    build_cache_env,
    ensure_cache_dirs,
)
from opensquilla.sandbox.backend.windows_default_capability import capability_sids_for_command
from opensquilla.sandbox.backend.windows_default_roots import (
    process_executable_rx_roots,
    runtime_rx_roots,
    windows_platform_rx_roots,
    windows_sensitive_marker,
    workspace_cache_root,
)
from opensquilla.sandbox.backend.windows_default_setup import (
    default_setup_marker_path,
    read_setup_marker,
)
from opensquilla.sandbox.backend.windows_default_support import probe_windows_default_support
from opensquilla.sandbox.operation_runtime import (
    SANDBOX_FILESYSTEM_WRITE_KINDS,
    FilesystemOperationRequest,
    SandboxOperation,
    SandboxOperationDomain,
    SandboxOperationResult,
)
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.run_mode import normalize_run_mode
from opensquilla.sandbox.types import SandboxBackendError, SandboxRequest, SandboxResult
from opensquilla.subprocess_encoding import decode_subprocess_output

_HELPER_MODULE = "opensquilla.sandbox.backend.windows_default_runner"
_FILESYSTEM_WORKER_MODULE = "opensquilla.sandbox.filesystem_worker"
_OUTPUT_BYTE_CAP = 1_048_576
_HELPER_PAYLOAD_ENV = "OPENSQUILLA_WINDOWS_DEFAULT_PAYLOAD"
_HELPER_TIMEOUT_GRACE_S = 30.0
_WINDOWS_PROCESS_BASE_ENV_KEYS = (
    "SystemRoot",
    "WINDIR",
    "ComSpec",
)
_WINDOWS_TOOL_PATH_EXECUTABLES = (
    "git.exe",
    "node.exe",
    "npm.cmd",
    "npm.exe",
    "uv.exe",
)
_WINDOWS_APPS_ALIAS_DIR_MARKER = "\\microsoft\\windowsapps"
_WINDOWS_DOS_DEVICE_NAMES = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "nul",
        "prn",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


class WindowsDefaultBackend(Backend):
    """Windows backend used by Standard-Sandbox and Managed Execution."""

    name = "windows_default"

    def available(self) -> bool:
        return _support_ready()

    def operation_domains_supported(self) -> frozenset[SandboxOperationDomain]:
        return frozenset({"filesystem"})

    async def run_operation(
        self,
        operation: SandboxOperation,
    ) -> SandboxOperationResult:
        if operation.domain != "filesystem":
            raise SandboxBackendError(
                f"windows_default backend does not implement {operation.domain} operations"
            )
        _filesystem_request(operation)
        if not _support_ready():
            raise SandboxBackendError(
                "windows_default backend unavailable: administrator setup or Windows "
                "support checks are not ready"
            )
        if operation.workspace is None:
            raise SandboxBackendError("filesystem operation is missing workspace")
        request = _filesystem_operation_request(operation)
        result = await self._run(
            request,
            prepare_cache=False,
            rehome_user_state=False,
            private_mounts_are_required=True,
        )
        if result.returncode != 0:
            _raise_filesystem_worker_failure(result)
        return SandboxOperationResult.from_worker_stdout(result.stdout)

    async def run(self, request: SandboxRequest) -> SandboxResult:
        cache_writable = _request_allows_cache_write(request)
        return await self._run(
            request,
            prepare_cache=cache_writable,
            rehome_user_state=cache_writable,
            private_mounts_are_required=False,
        )

    async def _run(
        self,
        request: SandboxRequest,
        *,
        prepare_cache: bool,
        rehome_user_state: bool,
        private_mounts_are_required: bool,
    ) -> SandboxResult:
        if not _support_ready():
            raise SandboxBackendError(
                "windows_default backend unavailable: administrator setup or Windows "
                "support checks are not ready"
            )

        payload = _payload_for_request(
            request,
            rehome_user_state=rehome_user_state,
            private_mounts_are_required=private_mounts_are_required,
        )
        if prepare_cache:
            ensure_cache_dirs(request.cwd)
        helper_env = dict(os.environ)
        helper_env[_HELPER_PAYLOAD_ENV] = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        )
        helper_argv = (sys.executable, "-m", _HELPER_MODULE, "--payload-env")
        wall = request.policy.limits.wall_timeout_s
        helper_wall = _helper_supervision_timeout(wall)
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *helper_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=helper_env,
            )
        except (FileNotFoundError, OSError) as exc:
            raise SandboxBackendError(f"windows_default helper launch failed: {exc}") from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=helper_wall,
            )
        except TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
            elapsed = time.monotonic() - started
            return SandboxResult(
                returncode=124,
                stdout="",
                stderr="windows_default helper timed out",
                wall_time_s=elapsed,
                backend_used=self.name,
                policy_used=request.policy.summary(),
                timed_out=True,
            )

        elapsed = time.monotonic() - started
        stdout, trunc_out = _decode_capped(stdout_bytes)
        stderr, trunc_err = _decode_capped(stderr_bytes)
        return SandboxResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            wall_time_s=elapsed,
            backend_used=self.name,
            policy_used=request.policy.summary(),
            truncated_stdout=trunc_out,
            truncated_stderr=trunc_err,
            timed_out=False,
        )


def _support_ready() -> bool:
    return probe_windows_default_support().default_backend_available


def _helper_supervision_timeout(command_timeout_s: float) -> float:
    return max(0.01, float(command_timeout_s)) + _HELPER_TIMEOUT_GRACE_S


def _request_allows_cache_write(request: SandboxRequest) -> bool:
    if normalize_run_mode(request.run_mode).value == "full":
        return True
    profile = request.policy.file_system
    if profile is None:
        return False
    return profile.resolve(workspace_cache_root(request.cwd)) is FileSystemAccess.WRITE


def _payload_for_request(
    request: SandboxRequest,
    *,
    rehome_user_state: bool = True,
    private_mounts_are_required: bool = False,
) -> dict[str, Any]:
    base_env = _process_base_env(request)
    env = build_cache_env(request.cwd, base_env=base_env) if rehome_user_state else base_env
    policy = request.policy.summary()
    policy["windowsAclPlan"] = _acl_plan_payload(
        request,
        private_mounts_are_required=private_mounts_are_required,
    )
    network_boundary = _windows_network_boundary_payload(request)
    if network_boundary is not None:
        policy["windowsNetworkBoundary"] = network_boundary
    stdin_b64 = (
        base64.b64encode(request.stdin).decode("ascii") if request.stdin is not None else None
    )
    return {
        "backend": "windows_default",
        "argv": list(request.argv),
        "cwd": str(request.cwd),
        "env": env,
        "policy": policy,
        "runMode": request.run_mode,
        "timeout": request.policy.limits.wall_timeout_s,
        "stdinBase64": stdin_b64,
    }


def _windows_network_boundary_payload(request: SandboxRequest) -> dict[str, object] | None:
    marker = read_setup_marker(default_setup_marker_path())
    if marker is None or marker.network is None:
        return None
    return marker.network.to_json()


def _filesystem_operation_request(
    operation: SandboxOperation,
) -> SandboxRequest:
    if operation.workspace is None:
        raise SandboxBackendError("filesystem operation is missing workspace")
    workspace = operation.workspace.expanduser().resolve(strict=False)
    if not workspace.exists():
        raise SandboxBackendError(f"filesystem operation workspace is missing: {workspace}")
    if not workspace.is_dir():
        raise NotADirectoryError(f"filesystem operation workspace is not a directory: {workspace}")
    request = _filesystem_request(operation)
    profile = operation.file_system_profile
    if profile is None:
        raise ValueError("filesystem operation is missing resolved filesystem profile")
    _validate_profile_is_windows_compilable(profile)
    targets = _filesystem_operation_targets(operation, request)
    runtime_roots = _runtime_readonly_roots()
    _validate_filesystem_operation_targets(operation, request, targets, runtime_roots)
    _validate_filesystem_private_transport_roots(profile, runtime_roots)
    policy = replace(
        build_filesystem_worker_policy(
            operation,
            private_rw_roots=(),
            private_ro_roots=runtime_roots,
            env_allowlist=(
                "PATH",
                "PYTHONPATH",
                "SystemRoot",
                "WINDIR",
                "ComSpec",
            ),
            description=f"Windows filesystem worker policy for {operation.kind}",
        ),
        tmp_writable=False,
    )
    env = {
        "PATH": str(_python_executable().parent),
        "PYTHONPATH": _pythonpath_for_worker(),
    }
    return SandboxRequest(
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
        stdin=json.dumps(operation.to_payload(), ensure_ascii=False).encode("utf-8"),
        env=env,
        reason="sandboxed filesystem side-effect worker",
        run_mode=normalize_run_mode(operation.run_mode).value,
    )


def _python_executable() -> Path:
    return Path(sys.executable)


def _capability_store_path() -> Path:
    return default_setup_marker_path().with_name("cap_sids.json")


def _deny_acl_state_path() -> Path:
    return default_setup_marker_path().with_name("deny_acl_state.json")


def _protected_opensquilla_state_roots() -> tuple[Path, ...]:
    sandbox_root = default_setup_marker_path().parent
    opensquilla_root = sandbox_root.parent
    return (
        sandbox_root,
        opensquilla_root / "sandbox-secrets",
        opensquilla_root / "sandbox-bin",
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
                if _is_relative_to_casefold(path, root):
                    raise SandboxBackendError(
                        f"windows_default denied read-only runtime filesystem target: {path}"
                    )
    _validate_filesystem_operation_profile_targets(operation, targets)


def _filesystem_operation_targets(
    operation: SandboxOperation,
    request: FilesystemOperationRequest,
) -> tuple[Path, ...]:
    targets: tuple[Path, ...]
    if operation.kind in {
        "read_file",
        "list_dir",
        "write_text",
        "edit_text",
        "glob_search",
        "grep_search",
    }:
        if request.path is None:
            raise SandboxBackendError(f"filesystem operation {operation.kind} requires path")
        targets = (_canonical_filesystem_target(request.path),)
    elif operation.kind == "apply_patch":
        if request.root is None:
            raise SandboxBackendError("filesystem operation apply_patch requires root")
        root = _canonical_filesystem_target(request.root)
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
        raise SandboxBackendError(f"unsupported filesystem operation: {operation.kind!r}")
    declared = tuple(dict.fromkeys(_canonical_filesystem_target(path) for path in request.paths))
    if set(declared) != set(targets):
        raise SandboxBackendError(
            "declared filesystem paths do not match derived operation targets: "
            f"declared={tuple(str(path) for path in declared)!r}, "
            f"derived={tuple(str(path) for path in targets)!r}"
        )
    return targets


def _canonical_filesystem_target(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


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
                "windows_default filesystem profile requires write access for "
                f"{operation.kind} target: {path} (resolved {access.value})"
            )
        if not write_required and access is FileSystemAccess.DENY:
            raise SandboxBackendError(
                "windows_default filesystem profile denies read access for "
                f"{operation.kind} target: {path}"
            )


def _validate_filesystem_private_transport_roots(
    profile: FileSystemPermissionProfile,
    runtime_roots: tuple[Path, ...],
) -> None:
    for root in runtime_roots:
        if profile.is_explicitly_denied(root):
            raise SandboxBackendError(
                f"windows_default filesystem profile denies private runtime root: {root}"
            )


def _validate_profile_is_windows_compilable(profile: FileSystemPermissionProfile) -> None:
    if profile.default_access is not FileSystemAccess.DENY:
        raise SandboxBackendError(
            "windows_default cannot compile non-deny default filesystem access; "
            "the Windows platform profile must project explicit roots"
        )
    if profile.denied_read_globs:
        raise SandboxBackendError(
            "windows_default cannot reliably enforce denied filesystem read globs"
        )
    entries = _effective_raw_profile_entries(profile)
    writable = tuple(
        Path(entry.path).resolve(strict=False)
        for entry in entries
        if entry.access is FileSystemAccess.WRITE
    )
    for entry in entries:
        if entry.access is not FileSystemAccess.WRITE:
            continue
        candidate = Path(entry.path).resolve(strict=False)
        for restriction in entries:
            if restriction.access is FileSystemAccess.WRITE:
                continue
            restricted = Path(restriction.path).resolve(strict=False)
            if (
                _windows_acl_path_key(candidate) != _windows_acl_path_key(restricted)
                and _is_relative_to_casefold(candidate, restricted)
                and any(
                    _windows_acl_path_key(restricted) != _windows_acl_path_key(root)
                    and _is_relative_to_casefold(restricted, root)
                    for root in writable
                )
            ):
                raise SandboxBackendError(
                    "windows_default cannot reopen a writable descendant below a "
                    f"READ/DENY ACL carveout: {entry.path}"
                )


def _filesystem_request(operation: SandboxOperation) -> FilesystemOperationRequest:
    if not isinstance(operation.request, FilesystemOperationRequest):
        raise SandboxBackendError("filesystem operation is missing filesystem request")
    return operation.request


def _runtime_readonly_roots() -> tuple[Path, ...]:
    roots = [
        *runtime_rx_roots(_python_executable()),
        *_opensquilla_import_roots(),
    ]
    return tuple(dict.fromkeys(root for root in roots if root))


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


def _is_relative_to_casefold(candidate: Path, root: Path) -> bool:
    c = str(candidate).replace("\\", "/").rstrip("/").lower()
    r = str(root).replace("\\", "/").rstrip("/").lower()
    return c == r or c.startswith(r + "/")


def _is_filesystem_root(path: Path) -> bool:
    try:
        return bool(path.anchor) and path == type(path)(path.anchor)
    except (OSError, RuntimeError, ValueError):
        return False


def _raise_filesystem_worker_failure(result: SandboxResult) -> None:
    detail = result.stderr.strip() or result.stdout.strip() or "filesystem worker failed"
    payload = _filesystem_worker_error_payload(result.stderr) or _filesystem_worker_error_payload(
        result.stdout
    )
    if payload is not None:
        message = payload["error"]
        exc_type = payload["type"]
        if exc_type == "FileNotFoundError":
            raise FileNotFoundError(message)
        if exc_type == "IsADirectoryError":
            raise IsADirectoryError(message)
        if exc_type == "NotADirectoryError":
            raise NotADirectoryError(message)
        if exc_type == "PermissionError":
            raise PermissionError(message)
        if exc_type == "ValueError":
            raise ValueError(message)
        if exc_type == "ToolError":
            from opensquilla.tools.types import ToolError

            raise ToolError(message)
    raise SandboxBackendError(f"windows_default filesystem worker failed: {detail}")


def _filesystem_worker_error_payload(raw: str) -> dict[str, str] | None:
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    exc_type = payload.get("type")
    if isinstance(error, str) and isinstance(exc_type, str):
        return {"error": error, "type": exc_type}
    return None


def _acl_plan_payload(
    request: SandboxRequest,
    *,
    private_mounts_are_required: bool = False,
) -> dict[str, object]:
    mode = normalize_run_mode(request.run_mode)
    if mode.value == "full":
        return {
            "autoGrants": [],
            "approvalRequired": [],
            "denied": [],
            "capabilitySids": [],
            "denyWritePaths": [],
            "denyReadPaths": [],
            "grantCurrentUserAccess": True,
        }
    profile = request.policy.file_system
    if profile is None:
        raise SandboxBackendError("windows_default requires a resolved filesystem profile")
    _validate_profile_is_windows_compilable(profile)
    process_rx_roots = tuple(
        root for root in process_executable_rx_roots(request.argv, request.env) if root.exists()
    )
    tool_rx_roots = tuple(
        root
        for root in _windows_tool_path_roots(
            _process_base_env(request),
            host_env=_host_tool_env(request),
        )
        if _acl_sensitive_marker(root) is None
    )
    tool_traversal_roots = _windows_tool_traversal_roots(
        tool_rx_roots,
        host_env=_host_tool_env(request),
    )
    runtime_acl_roots = tuple(
        root
        for root in runtime_rx_roots(_python_executable())
        if _rx_root_needs_acl_grant(root, request.env)
    )
    process_acl_roots = tuple(
        root for root in process_rx_roots if _rx_root_needs_acl_grant(root, request.env)
    )
    required: list[AclGrant] = [
        *(
            AclGrant(root, AclAccess.RX, AclGrantKind.REQUIRED)
            for root in _workspace_traversal_roots(request.cwd)
        ),
        *(AclGrant(root, AclAccess.RX, AclGrantKind.REQUIRED) for root in tool_traversal_roots),
        *(AclGrant(root, AclAccess.RX, AclGrantKind.REQUIRED) for root in runtime_acl_roots),
        *(AclGrant(root, AclAccess.RX, AclGrantKind.REQUIRED) for root in tool_rx_roots),
        *(AclGrant(root, AclAccess.RX, AclGrantKind.REQUIRED) for root in process_acl_roots),
        *(
            AclGrant(
                mount.host_path,
                AclAccess.RWX if mount.mode == "rw" else AclAccess.RX,
                AclGrantKind.REQUIRED,
            )
            for mount in request.policy.mounts
            if private_mounts_are_required and mount.host_path.exists()
        ),
    ]
    policy_grants = _profile_acl_grants(request, profile)
    plan = plan_acl_refresh(
        run_mode=mode,
        required=required,
        policy=policy_grants,
        expansion=_expansion_grants_from_env(request),
        sensitive_marker=_acl_sensitive_marker,
        required_policy_sensitive_marker=lambda _path: None,
    )
    if plan.denied:
        denied = plan.denied[0]
        raise SandboxBackendError(
            f"windows_default denied sensitive ACL grant for {denied.grant.path}: {denied.reason}"
        )
    if plan.approval_required:
        grant = plan.approval_required[0]
        raise SandboxBackendError(
            f"windows_default ACL approval is required before granting {grant.path}"
        )

    deny_write_paths = _deny_write_paths_for_request(
        request,
        profile,
        include_private_mounts=private_mounts_are_required,
    )
    deny_read_paths = _profile_denied_read_paths(profile)
    merged_grants = _merge_acl_grants(plan.auto_grants)
    roots = tuple(grant.path for grant in merged_grants)
    accesses = tuple(grant.access.value for grant in merged_grants)
    sids = capability_sids_for_command(
        _capability_store_path(),
        roots,
        accesses=accesses,
    )
    grants: list[dict[str, str]] = []
    for grant, sid in zip(merged_grants, sids, strict=True):
        grants.append(
            {
                "path": str(grant.path),
                "access": grant.access.value,
                "kind": grant.kind.value,
                "capabilitySid": sid,
            }
        )
    return {
        "autoGrants": grants,
        "approvalRequired": [],
        "denied": [],
        "capabilitySids": list(dict.fromkeys(item["capabilitySid"] for item in grants)),
        "denyWritePaths": [str(path) for path in deny_write_paths],
        "denyReadPaths": [str(path) for path in deny_read_paths],
        "denyAclStatePath": str(_deny_acl_state_path()),
        "grantCurrentUserAccess": True,
    }


def _profile_acl_grants(
    request: SandboxRequest,
    profile: FileSystemPermissionProfile,
) -> tuple[AclGrant, ...]:
    grants: list[AclGrant] = []
    for entry in _effective_raw_profile_entries(profile):
        path = Path(entry.path)
        if entry.access is FileSystemAccess.DENY or not path.exists():
            continue
        access = AclAccess.RWX if entry.access is FileSystemAccess.WRITE else AclAccess.RX
        if access is AclAccess.RX and not _rx_root_needs_acl_grant(path, request.env):
            continue
        grants.append(AclGrant(path, access, AclGrantKind.POLICY))
    return tuple(grants)


def _deny_write_paths_for_request(
    request: SandboxRequest,
    profile: FileSystemPermissionProfile,
    *,
    include_private_mounts: bool,
) -> tuple[Path, ...]:
    writable_roots = tuple(Path(path) for path in profile.writable_roots)
    writable_acl_roots = _dedupe_acl_paths(
        variant
        for entry in _effective_raw_profile_entries(profile)
        if entry.access is FileSystemAccess.WRITE
        for variant in _acl_path_variants(Path(entry.path))
    )
    paths: list[Path] = [
        root
        for root in _runtime_readonly_roots()
        if root.exists() and not _is_filesystem_root(root) and _acl_sensitive_marker(root) is None
    ]
    for entry in _effective_raw_profile_entries(profile):
        if entry.access is FileSystemAccess.WRITE:
            continue
        entry_path = Path(entry.path)
        if not _acl_target_is_nested_under_writable_root(entry_path, writable_acl_roots):
            continue
        variants = _acl_path_variants(entry_path)
        paths.extend(variants)
        for variant in variants:
            paths.extend(_writable_reparse_ancestors(variant, writable_acl_roots))
    paths.extend(
        protected_root
        for protected_root in _protected_opensquilla_state_roots()
        if any(
            _is_relative_to_casefold(
                protected_root.resolve(strict=False),
                writable_root.resolve(strict=False),
            )
            for writable_root in writable_roots
        )
    )
    paths.extend(
        mount.host_path
        for mount in request.policy.mounts
        if include_private_mounts
        and mount.mode == "ro"
        and mount.host_path.exists()
        and not _is_filesystem_root(mount.host_path)
        and _acl_sensitive_marker(mount.host_path) is None
    )
    return _dedupe_acl_paths(paths)


def _acl_target_is_nested_under_writable_root(
    path: Path,
    writable_roots: tuple[Path, ...],
) -> bool:
    return any(
        _windows_acl_path_key(target) != _windows_acl_path_key(writable_root)
        and _is_relative_to_casefold(target, writable_root)
        for target in _acl_path_variants(path)
        for writable_root in writable_roots
    )


def _writable_reparse_ancestors(
    target: Path,
    writable_roots: tuple[Path, ...],
) -> tuple[Path, ...]:
    result: list[Path] = []
    for root in writable_roots:
        if not _is_relative_to_casefold(target, root):
            continue
        try:
            relative = target.relative_to(root)
        except ValueError:
            continue
        current = root
        for part in relative.parts[:-1]:
            current = current / part
            is_junction = bool(getattr(os.path, "isjunction", lambda _path: False)(current))
            if current.is_symlink() or is_junction:
                result.extend(_acl_path_variants(current))
    return _dedupe_acl_paths(result)


def _profile_denied_read_paths(
    profile: FileSystemPermissionProfile,
) -> tuple[Path, ...]:
    return _dedupe_acl_paths(
        path
        for entry in _effective_raw_profile_entries(profile)
        if entry.access is FileSystemAccess.DENY
        for path in _acl_path_variants(Path(entry.path))
    )


def _effective_raw_profile_entries(
    profile: FileSystemPermissionProfile,
) -> tuple[FileSystemPermissionEntry, ...]:
    latest: dict[str, tuple[int, FileSystemPermissionEntry]] = {}
    for index, entry in enumerate(profile.entries):
        canonical = Path(entry.path).expanduser().resolve(strict=False)
        latest[_windows_acl_path_key(canonical)] = (index, entry)
    return tuple(entry for _index, entry in sorted(latest.values()))


def _acl_path_variants(path: Path) -> tuple[Path, ...]:
    lexical = path.expanduser().absolute()
    variants = [lexical]
    if lexical.exists():
        variants.append(lexical.resolve(strict=False))
    return _dedupe_acl_paths(variants)


def _dedupe_acl_paths(paths: Iterable[Path | str]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().absolute()
        key = _windows_acl_path_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def _merge_acl_grants(grants: Iterable[AclGrant]) -> tuple[AclGrant, ...]:
    merged: dict[str, tuple[int, AclGrant]] = {}
    for index, grant in enumerate(grants):
        key = _windows_acl_path_key(grant.path.resolve(strict=False))
        previous = merged.get(key)
        if previous is None:
            merged[key] = (index, grant)
            continue
        previous_index, previous_grant = previous
        access = (
            AclAccess.RWX
            if AclAccess.RWX in {previous_grant.access, grant.access}
            else AclAccess.RX
        )
        kind = (
            AclGrantKind.REQUIRED
            if AclGrantKind.REQUIRED in {previous_grant.kind, grant.kind}
            else grant.kind
        )
        merged[key] = (
            previous_index,
            AclGrant(path=grant.path, access=access, kind=kind),
        )
    return tuple(grant for _index, grant in sorted(merged.values()))


def _windows_acl_path_key(path: Path) -> str:
    return str(path).replace("\\", "/").rstrip("/").casefold()


def _rx_root_needs_acl_grant(path: Path, env: dict[str, str]) -> bool:
    return not any(_is_relative_to_casefold(path, root) for root in windows_platform_rx_roots(env))


def _workspace_traversal_roots(cwd: Path) -> tuple[Path, ...]:
    parent = cwd.parent
    if parent.name.lower() != ".opensquilla":
        return ()
    return (parent,)


def _acl_sensitive_marker(path: Path) -> str | None:
    return windows_sensitive_marker(path)


def _is_windows_dos_device_path(path: Path) -> bool:
    basename = ntpath.basename(str(path).strip().strip("'\"").rstrip("\\/"))
    if not basename:
        return False
    stem = basename.split(":", 1)[0].split(".", 1)[0].lower()
    return stem in _WINDOWS_DOS_DEVICE_NAMES


def _expansion_grants_from_env(request: SandboxRequest) -> tuple[AclGrant, ...]:
    raw = request.env.get("OPENSQUILLA_WINDOWS_SANDBOX_EXPANSION_ROOTS", "")
    roots = [item.strip() for item in raw.split(";") if item.strip()]
    return tuple(
        AclGrant(Path(root), AclAccess.RWX, AclGrantKind.EXPANSION)
        for root in roots
        if Path(root).exists() and not _is_windows_dos_device_path(Path(root))
    )


def _allowed_env(request: SandboxRequest) -> dict[str, str]:
    return {
        key: value
        for key in request.policy.env_allowlist
        if isinstance((value := request.env.get(key)), str)
    }


def _process_base_env(request: SandboxRequest) -> dict[str, str]:
    env = _allowed_env(request)
    for key in _WINDOWS_PROCESS_BASE_ENV_KEYS:
        value = request.env.get(key) or os.environ.get(key)
        if isinstance(value, str) and value:
            env[key] = value
    _prepend_windows_tool_paths(env, host_env=_host_tool_env(request))
    return env


def _host_tool_env(request: SandboxRequest) -> dict[str, str]:
    env = dict(os.environ)
    env.update(request.env)
    return env


def _prepend_windows_tool_paths(
    env: dict[str, str],
    *,
    host_env: Mapping[str, str] | None = None,
) -> None:
    if "PATH" not in env:
        return
    roots = _windows_tool_path_roots(env, host_env=host_env)
    if not roots:
        return
    existing = _split_windows_path(env.get("PATH", ""))
    merged = [str(root) for root in roots]
    merged.extend(part for part in existing if part)
    env["PATH"] = ";".join(_dedupe_path_texts(merged))


def _windows_tool_path_roots(
    env: Mapping[str, str],
    *,
    host_env: Mapping[str, str] | None = None,
) -> tuple[Path, ...]:
    source = host_env or os.environ
    candidates: list[Path] = []
    candidates.extend(_common_windows_tool_dirs(source))
    for value in (env.get("PATH"), source.get("PATH")):
        for entry in _split_windows_path(value or ""):
            path = Path(entry).expanduser()
            if _windows_path_is_apps_alias_dir(path):
                continue
            candidates.append(path)
    return tuple(_dedupe_paths(path for path in candidates if _directory_has_windows_tool(path)))


def _common_windows_tool_dirs(env: Mapping[str, str]) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for root in _program_files_roots(env):
        candidates.extend(
            (
                root / "Git" / "cmd",
                root / "Git" / "bin",
                root / "nodejs",
            )
        )
    local_appdata = _env_path(env, "LOCALAPPDATA")
    if local_appdata is not None:
        candidates.extend(
            (
                local_appdata / "Programs" / "nodejs",
                local_appdata / "OpenAI" / "Codex" / "bin",
            )
        )
        candidates.extend(
            sorted(
                local_appdata.glob("OpenAI/Codex/runtimes/cua_node/*/bin"),
                key=lambda path: str(path).casefold(),
            )
        )
    appdata = _env_path(env, "APPDATA")
    if appdata is not None:
        candidates.append(appdata / "npm")
    userprofile = _env_path(env, "USERPROFILE")
    if userprofile is not None:
        candidates.extend(
            (
                userprofile / ".local" / "bin",
                userprofile
                / ".cache"
                / "codex-runtimes"
                / "codex-primary-runtime"
                / "dependencies"
                / "native"
                / "git"
                / "cmd",
                userprofile
                / ".cache"
                / "codex-runtimes"
                / "codex-primary-runtime"
                / "dependencies"
                / "native"
                / "git"
                / "bin",
                userprofile
                / ".cache"
                / "codex-runtimes"
                / "codex-primary-runtime"
                / "dependencies"
                / "node"
                / "bin",
                userprofile
                / ".cache"
                / "codex-runtimes"
                / "codex-primary-runtime"
                / "dependencies"
                / "bin",
            )
        )
        for pattern in (
            ".cache/codex-runtimes/*/dependencies/native/git/cmd",
            ".cache/codex-runtimes/*/dependencies/native/git/bin",
            ".cache/codex-runtimes/*/dependencies/node/bin",
            ".cache/codex-runtimes/*/dependencies/bin",
        ):
            candidates.extend(
                sorted(
                    userprofile.glob(pattern),
                    key=lambda path: str(path).casefold(),
                )
            )
    return tuple(candidates)


def _windows_tool_traversal_roots(
    tool_roots: tuple[Path, ...],
    *,
    host_env: Mapping[str, str],
) -> tuple[Path, ...]:
    anchors: list[Path] = []
    for key in ("LOCALAPPDATA", "APPDATA"):
        value = _env_path(host_env, key)
        if value is not None and value.parent != value:
            anchors.append(value.parent)
    userprofile = _env_path(host_env, "USERPROFILE")
    if userprofile is not None:
        anchors.append(userprofile / ".local")
        anchors.append(userprofile / ".cache")

    roots: list[Path] = []
    for tool_root in tool_roots:
        resolved_tool = tool_root.resolve(strict=False)
        for anchor in anchors:
            resolved_anchor = anchor.resolve(strict=False)
            if not _is_relative_to_casefold(resolved_tool, resolved_anchor):
                continue
            roots.extend(_path_chain(resolved_anchor, resolved_tool.parent))
            break
    return tuple(_dedupe_paths(path for path in roots if _acl_sensitive_marker(path) is None))


def _path_chain(start: Path, stop: Path) -> tuple[Path, ...]:
    start = start.resolve(strict=False)
    stop = stop.resolve(strict=False)
    if not _is_relative_to_casefold(stop, start):
        return ()
    roots: list[Path] = []
    current = stop
    while True:
        roots.append(current)
        if current == start:
            break
        parent = current.parent
        if parent == current:
            return ()
        current = parent
    roots.reverse()
    return tuple(roots)


def _program_files_roots(env: Mapping[str, str]) -> tuple[Path, ...]:
    roots = []
    for key, fallback in (
        ("ProgramFiles", r"C:\Program Files"),
        ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ):
        roots.append(Path(env.get(key) or fallback))
    return tuple(_dedupe_paths(root for root in roots if str(root)))


def _env_path(env: Mapping[str, str], key: str) -> Path | None:
    raw = env.get(key) or os.environ.get(key)
    if not raw:
        return None
    return Path(raw)


def _split_windows_path(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def _directory_has_windows_tool(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    return any((path / name).exists() for name in _WINDOWS_TOOL_PATH_EXECUTABLES)


def _windows_path_is_apps_alias_dir(path: Path) -> bool:
    return _WINDOWS_APPS_ALIAS_DIR_MARKER in str(path).replace("/", "\\").casefold()


def _dedupe_paths(paths: Iterable[Path | str]) -> tuple[Path, ...]:
    seen: set[str] = set()
    result: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve(strict=False)
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return tuple(result)


def _dedupe_path_texts(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        key = path.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _decode_capped(raw: bytes | None) -> tuple[str, bool]:
    if not raw:
        return "", False
    if len(raw) <= _OUTPUT_BYTE_CAP:
        return decode_subprocess_output(raw), False
    return decode_subprocess_output(raw[:_OUTPUT_BYTE_CAP]), True


__all__ = ["WindowsDefaultBackend"]
