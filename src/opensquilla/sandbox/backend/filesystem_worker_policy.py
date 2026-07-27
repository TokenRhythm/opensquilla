"""Build backend-worker transport policy for filesystem operations."""

from __future__ import annotations

from pathlib import Path

from opensquilla.sandbox.operation_runtime import SandboxOperation
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SecurityLevel,
)


def build_filesystem_worker_policy(
    operation: SandboxOperation,
    *,
    private_rw_roots: tuple[Path, ...],
    private_ro_roots: tuple[Path, ...],
    env_allowlist: tuple[str, ...],
    description: str,
) -> SandboxPolicy:
    """Carry resolved user permissions plus worker-private transport mounts."""

    profile = operation.file_system_profile
    if profile is None:
        raise ValueError("filesystem operation is missing resolved filesystem profile")

    mounts = tuple(
        MountSpec(
            host_path=root,
            sandbox_path=root,
            mode="ro",
            required=True,
        )
        for root in private_ro_roots
    ) + tuple(
        MountSpec(
            host_path=root,
            sandbox_path=root,
            mode="rw",
            required=True,
        )
        for root in private_rw_roots
    )
    return SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=mounts,
        workspace_rw=False,
        tmp_writable=True,
        limits=ResourceLimits(
            cpu_seconds=30,
            memory_mb=1024,
            pids=64,
            wall_timeout_s=30,
        ),
        env_allowlist=env_allowlist,
        require_approval=False,
        description=description,
        file_system=profile,
    )


__all__ = ["build_filesystem_worker_policy"]
