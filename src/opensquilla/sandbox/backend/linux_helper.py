"""Command-line entry point for Linux sandbox helper execution."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from opensquilla.sandbox.backend.linux_bwrap import (
    BwrapOptions,
    BwrapPlan,
    build_bwrap_plan,
)
from opensquilla.sandbox.backend.linux_filesystem import run_filesystem_payload
from opensquilla.sandbox.backend.linux_paths import canonical_linux_mount
from opensquilla.sandbox.backend.linux_payload import HelperPayload, decode_payload
from opensquilla.sandbox.backend.linux_permissions import (
    LinuxPermissions,
    LinuxRoot,
    compile_linux_permissions,
)
from opensquilla.sandbox.backend.linux_process import run_process_payload
from opensquilla.sandbox.backend.linux_protected_create import (
    cleanup_protected_create_registrations,
    cleanup_synthetic_mount_registrations,
    register_protected_create_targets,
    register_synthetic_mount_targets,
)
from opensquilla.sandbox.backend.linux_proxy_bridge import ENV_PROXY_PORT, ENV_PROXY_UDS
from opensquilla.sandbox.backend.linux_proxy_routing import proxy_env_for_inner_port
from opensquilla.sandbox.backend.linux_readiness import is_proc_mount_failure, probe_bwrap
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SecurityLevel,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--inner", action="store_true")
    args = parser.parse_args(argv)
    payload = decode_payload(Path(args.payload).read_text(encoding="utf-8"))
    coro = _run_inner(payload) if args.inner else _run_outer(payload, Path(args.payload))
    result = asyncio.run(coro)
    print(json.dumps(result, ensure_ascii=False))
    return 0


async def _run_inner(payload: HelperPayload) -> dict[str, Any]:
    if payload.operation_type == "process":
        return await run_process_payload(payload)
    if payload.operation_type == "filesystem":
        return await run_filesystem_payload(payload)
    raise ValueError(f"unknown operation type: {payload.operation_type}")


async def _run_outer(payload: HelperPayload, payload_path: Path) -> dict[str, Any]:
    probe = probe_bwrap()
    if not probe.available:
        raise RuntimeError(probe.message)
    supports_proc = bool(getattr(probe, "supports_proc", True))
    plan = _build_outer_plan(
        payload=payload,
        payload_path=payload_path,
        bwrap_path=probe.path or "bwrap",
        mount_proc=supports_proc,
    )
    returncode, stdout, stderr, protected_create_messages = await _run_outer_plan(plan)
    if (
        supports_proc
        and returncode != 0
        and not protected_create_messages
        and is_proc_mount_failure(stderr)
    ):
        plan = _build_outer_plan(
            payload=payload,
            payload_path=payload_path,
            bwrap_path=probe.path or "bwrap",
            mount_proc=False,
        )
        returncode, stdout, stderr, protected_create_messages = await _run_outer_plan(plan)
    if returncode != 0 and not protected_create_messages:
        raise RuntimeError(stderr.strip() or stdout.strip() or "linux helper failed")
    result = json.loads(stdout)
    if not isinstance(result, dict):
        raise RuntimeError("linux helper returned invalid result")
    if protected_create_messages:
        if "returncode" not in result:
            raise RuntimeError("\n".join(protected_create_messages))
        result["returncode"] = 1
        result["stderr"] = _append_stderr(
            str(result.get("stderr", "")),
            "\n".join(protected_create_messages),
        )
    return result


def _build_outer_plan(
    *,
    payload: HelperPayload,
    payload_path: Path,
    bwrap_path: str,
    mount_proc: bool,
) -> BwrapPlan:
    return build_outer_bwrap_plan(
        payload=payload,
        payload_path=payload_path,
        bwrap_path=bwrap_path,
        mount_proc=mount_proc,
    )


async def _run_outer_plan(plan: BwrapPlan) -> tuple[int, str, str, list[str]]:
    synthetic_registrations = register_synthetic_mount_targets(plan.synthetic_mount_targets)
    protected_create_registrations = register_protected_create_targets(
        plan.protected_create_targets,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *plan.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=tuple(file.fileno() for file in plan.preserved_files),
        )
        stdout_raw, stderr_raw = await proc.communicate()
    except BaseException:
        cleanup_synthetic_mount_registrations(synthetic_registrations)
        cleanup_protected_create_registrations(protected_create_registrations)
        raise
    finally:
        for file in plan.preserved_files:
            file.close()
    stdout = stdout_raw.decode("utf-8", errors="replace")
    stderr = stderr_raw.decode("utf-8", errors="replace")
    cleanup_synthetic_mount_registrations(synthetic_registrations)
    protected_create_messages = cleanup_protected_create_registrations(
        protected_create_registrations,
    )
    return int(proc.returncode or 0), stdout, stderr, protected_create_messages


def build_outer_bwrap_command(
    *,
    payload: HelperPayload,
    payload_path: Path,
    bwrap_path: str,
    mount_proc: bool,
) -> list[str]:
    return build_outer_bwrap_plan(
        payload=payload,
        payload_path=payload_path,
        bwrap_path=bwrap_path,
        mount_proc=mount_proc,
    ).argv


def build_outer_bwrap_plan(
    *,
    payload: HelperPayload,
    payload_path: Path,
    bwrap_path: str,
    mount_proc: bool,
) -> BwrapPlan:
    policy = _policy_from_payload(payload.policy)
    permissions = _with_payload_mount(compile_linux_permissions(policy), payload_path)
    inner = [
        sys.executable,
        "-m",
        "opensquilla.sandbox.backend.linux_helper",
        "--inner",
        "--payload",
        str(payload_path),
    ]
    env: dict[str, str] = {}
    bridge = payload.policy.get("linuxProxyBridge")
    if isinstance(bridge, dict):
        uds_path = Path(str(bridge["udsPath"]))
        script_path = Path(str(bridge["scriptPath"]))
        port = int(bridge["port"])
        inner = [
            sys.executable,
            str(script_path),
            "--",
            *inner,
        ]
        env = proxy_env_for_inner_port(
            base_env={
                ENV_PROXY_UDS: str(uds_path),
                ENV_PROXY_PORT: str(port),
            },
            port=port,
        )
    return build_bwrap_plan(
        command=inner,
        command_cwd=Path(payload.cwd),
        permissions=permissions,
        options=BwrapOptions(
            bwrap_path=bwrap_path,
            mount_proc=mount_proc,
            env=env,
        ),
    )


def _append_stderr(current: str, message: str) -> str:
    if not current:
        return f"{message}\n"
    if current.endswith("\n"):
        return f"{current}{message}\n"
    return f"{current}\n{message}\n"


def _with_payload_mount(
    permissions: LinuxPermissions,
    payload_path: Path,
) -> LinuxPermissions:
    payload_root = LinuxRoot(
        host_path=payload_path.parent,
        sandbox_path=payload_path.parent,
        required=True,
    )
    return LinuxPermissions(
        read_roots=(*permissions.read_roots, payload_root),
        write_roots=permissions.write_roots,
        denied_roots=permissions.denied_roots,
        denied_globs=permissions.denied_globs,
        protected_subpaths=permissions.protected_subpaths,
        env_allowlist=permissions.env_allowlist,
        network=permissions.network,
        tmp_writable=permissions.tmp_writable,
        wall_timeout_s=permissions.wall_timeout_s,
        read_all=permissions.read_all,
    )


def _policy_from_payload(policy_payload: dict[str, object]) -> SandboxPolicy:
    mounts = []
    raw_mounts = policy_payload.get("mounts", [])
    if not isinstance(raw_mounts, list):
        raw_mounts = []
    for item in raw_mounts:
        if not isinstance(item, dict):
            continue
        mount = MountSpec(
            host_path=Path(str(item["host"])),
            sandbox_path=Path(str(item["sandbox"])),
            mode="rw" if item.get("mode") == "rw" else "ro",
            required=bool(item.get("required", False)),
        )
        mounts.append(canonical_linux_mount(mount))
    mounts.extend(_helper_runtime_mounts())
    bridge = policy_payload.get("linuxProxyBridge")
    if isinstance(bridge, dict):
        uds_path = Path(str(bridge["udsPath"]))
        script_path = Path(str(bridge["scriptPath"]))
        bridge_dir = uds_path.parent
        mounts.append(
            MountSpec(
                host_path=bridge_dir,
                sandbox_path=bridge_dir,
                mode="rw",
                required=True,
            )
        )
        if script_path.parent != bridge_dir:
            mounts.append(
                MountSpec(
                    host_path=script_path.parent,
                    sandbox_path=script_path.parent,
                    mode="ro",
                    required=True,
                )
            )
    network = NetworkMode(str(policy_payload.get("network", "none")))
    raw_env_allowlist = policy_payload.get("envAllowlist", [])
    env_allowlist = raw_env_allowlist if isinstance(raw_env_allowlist, list) else []
    raw_unreadable_globs = policy_payload.get("unreadableGlobs", [])
    unreadable_globs = raw_unreadable_globs if isinstance(raw_unreadable_globs, list) else []
    file_system = _filesystem_profile_from_payload(policy_payload.get("fileSystem"))
    limits = _resource_limits_from_payload(policy_payload)
    return SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=network,
        mounts=tuple(mounts),
        workspace_rw=any(mount.mode == "rw" for mount in mounts),
        tmp_writable=bool(policy_payload.get("tmpWritable", False)),
        limits=limits,
        env_allowlist=tuple(str(item) for item in env_allowlist),
        require_approval=False,
        unreadable_globs=tuple(str(item) for item in unreadable_globs),
        file_system=file_system,
    )


def _filesystem_profile_from_payload(
    raw_profile: object,
) -> FileSystemPermissionProfile | None:
    if not isinstance(raw_profile, dict):
        return None
    raw_entries = raw_profile.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("linux helper filesystem profile entries are invalid")
    entries: list[FileSystemPermissionEntry] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("linux helper filesystem profile entry is invalid")
        try:
            path = Path(str(raw_entry["path"]))
            access = FileSystemAccess(str(raw_entry["access"]))
        except (KeyError, ValueError) as exc:
            raise ValueError("linux helper filesystem profile entry is invalid") from exc
        if not path.is_absolute():
            raise ValueError("linux helper filesystem profile path must be absolute")
        entries.append(FileSystemPermissionEntry(path=path, access=access))
    raw_globs = raw_profile.get("deniedReadGlobs", [])
    if not isinstance(raw_globs, list):
        raise ValueError("linux helper denied-read globs are invalid")
    try:
        default_access = FileSystemAccess(
            str(raw_profile.get("defaultAccess", FileSystemAccess.DENY.value))
        )
    except ValueError as exc:
        raise ValueError("linux helper filesystem profile default access is invalid") from exc
    return FileSystemPermissionProfile(
        entries=tuple(entries),
        denied_read_globs=tuple(str(item) for item in raw_globs),
        default_access=default_access,
    )


def _resource_limits_from_payload(policy_payload: dict[str, object]) -> ResourceLimits:
    return ResourceLimits(
        cpu_seconds=_int_payload(policy_payload.get("cpuSeconds"), default=30),
        memory_mb=_int_payload(policy_payload.get("memoryMb"), default=1024),
        pids=_int_payload(policy_payload.get("pids"), default=256),
        wall_timeout_s=_float_payload(policy_payload.get("wallTimeoutS"), default=60.0),
    )


def _int_payload(value: object, *, default: int) -> int:
    if isinstance(value, (str, bytes, int, float)):
        return int(value)
    return default


def _float_payload(value: object, *, default: float) -> float:
    if isinstance(value, (str, bytes, int, float)):
        return float(value)
    return default


def _helper_runtime_mounts() -> list[MountSpec]:
    roots = [
        Path(sys.prefix),
        Path(sys.base_prefix),
        Path(sys.executable).resolve().parents[1],
        *_python_symlink_runtime_roots(Path(sys.executable)),
        Path(__file__).resolve().parents[3],
    ]
    mounts: list[MountSpec] = []
    seen: set[Path] = set()
    for root in roots:
        if root in seen or not root.exists():
            continue
        seen.add(root)
        mounts.append(
            MountSpec(
                host_path=root,
                sandbox_path=root,
                mode="ro",
                required=True,
            )
        )
    return mounts

def _python_symlink_runtime_roots(executable: Path) -> list[Path]:
    roots: list[Path] = []
    current = executable
    seen: set[Path] = set()
    for _ in range(4):
        if current in seen or not current.is_symlink():
            break
        seen.add(current)
        target_raw = os.readlink(current)
        target = Path(target_raw)
        if not target.is_absolute():
            target = current.parent / target
        roots.append(target.parent.parent)
        current = target
    return roots


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
