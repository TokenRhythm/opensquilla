"""Linux backend path normalization helpers."""

from __future__ import annotations

from dataclasses import replace

from opensquilla.sandbox.types import (
    SANDBOX_WORKSPACE_PATH,
    MountSpec,
    SandboxPolicy,
    sandbox_path_text,
)


def canonical_linux_mount(mount: MountSpec) -> MountSpec:
    if sandbox_path_text(mount.sandbox_path) != SANDBOX_WORKSPACE_PATH.as_posix():
        return mount
    return MountSpec(
        host_path=mount.host_path,
        sandbox_path=mount.host_path,
        mode=mount.mode,
        required=mount.required,
    )


def canonical_linux_policy(policy: SandboxPolicy) -> SandboxPolicy:
    mounts = tuple(canonical_linux_mount(mount) for mount in policy.mounts)
    if mounts == policy.mounts:
        return policy
    return replace(policy, mounts=mounts)


__all__ = ["canonical_linux_mount", "canonical_linux_policy"]
