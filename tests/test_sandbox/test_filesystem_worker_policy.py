from __future__ import annotations

import json
from pathlib import Path

import pytest

from opensquilla.sandbox.backend.filesystem_worker_policy import (
    build_filesystem_worker_policy,
)
from opensquilla.sandbox.operation_runtime import SandboxOperation
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SecurityLevel,
)


def test_worker_policy_carries_resolved_profile_without_mounting_operation_target(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    runtime_root = tmp_path / "runtime"
    worker_root = tmp_path / "worker"
    profile = FileSystemPermissionProfile.workspace(workspace=workspace)
    operation = SandboxOperation.filesystem(
        kind="list_dir",
        workspace=workspace,
        run_mode="trusted",
        path=Path("/etc"),
        paths=(Path("/etc"),),
        file_system_profile=profile,
    )

    policy = build_filesystem_worker_policy(
        operation,
        private_rw_roots=(worker_root,),
        private_ro_roots=(runtime_root,),
        env_allowlist=("PATH", "HOME"),
        description="filesystem worker test policy",
    )

    assert policy.file_system is profile
    assert policy.mounts == (
        MountSpec(
            host_path=runtime_root,
            sandbox_path=runtime_root,
            mode="ro",
            required=True,
        ),
        MountSpec(
            host_path=worker_root,
            sandbox_path=worker_root,
            mode="rw",
            required=True,
        ),
    )
    assert Path("/etc") not in {mount.host_path for mount in policy.mounts}
    assert policy.level is SecurityLevel.STANDARD
    assert policy.network is NetworkMode.NONE
    assert policy.workspace_rw is False
    assert policy.tmp_writable is True
    assert policy.limits == ResourceLimits(
        cpu_seconds=30,
        memory_mb=1024,
        pids=64,
        wall_timeout_s=30,
    )
    assert policy.require_approval is False
    assert policy.env_allowlist == ("PATH", "HOME")
    assert policy.description == "filesystem worker test policy"


def test_worker_policy_rejects_operation_without_resolved_profile(
    tmp_path: Path,
) -> None:
    operation = SandboxOperation.filesystem(
        kind="list_dir",
        workspace=tmp_path,
        run_mode="trusted",
        path=Path("/etc"),
    )

    with pytest.raises(
        ValueError,
        match="^filesystem operation is missing resolved filesystem profile$",
    ):
        build_filesystem_worker_policy(
            operation,
            private_rw_roots=(tmp_path / "worker",),
            private_ro_roots=(tmp_path / "runtime",),
            env_allowlist=("PATH",),
            description="filesystem worker test policy",
        )


def test_worker_payload_does_not_serialize_resolved_profile(tmp_path: Path) -> None:
    profile = FileSystemPermissionProfile.workspace(workspace=tmp_path)
    other_profile = FileSystemPermissionProfile.full_access()
    operation = SandboxOperation.filesystem(
        kind="list_dir",
        workspace=tmp_path,
        run_mode="trusted",
        path=tmp_path,
        file_system_profile=profile,
    )
    operation_with_other_profile = SandboxOperation.filesystem(
        kind="list_dir",
        workspace=tmp_path,
        run_mode="trusted",
        path=tmp_path,
        file_system_profile=other_profile,
    )

    payload = operation.to_payload()
    operation_repr = repr(operation)

    assert operation.file_system_profile is profile
    assert profile != other_profile
    assert operation == operation_with_other_profile
    assert "file_system_profile" not in operation_repr
    assert repr(profile) not in operation_repr
    assert "file_system_profile" not in payload
    assert "fileSystemProfile" not in payload
    json.dumps(payload)
