from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from opensquilla.sandbox.backend.linux_permissions import compile_linux_permissions
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


def _policy(tmp_path: Path, *, network: NetworkMode = NetworkMode.NONE) -> SandboxPolicy:
    return SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=network,
        mounts=(
            MountSpec(
                host_path=tmp_path,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
            MountSpec(
                host_path=tmp_path / "docs",
                sandbox_path=Path("/workspace/docs"),
                mode="ro",
                required=False,
            ),
        ),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=30),
        env_allowlist=("PATH", "HOME"),
        require_approval=False,
    )


def test_compile_linux_permissions_splits_mount_modes(tmp_path: Path) -> None:
    compiled = compile_linux_permissions(_policy(tmp_path))

    assert str(tmp_path) in [str(root.host_path) for root in compiled.write_roots]
    assert str(tmp_path / "docs") in [str(root.host_path) for root in compiled.read_roots]
    assert compiled.env_allowlist == ("PATH", "HOME")
    assert compiled.tmp_writable is True


def test_compile_linux_permissions_adds_protected_subpaths_under_writable_roots(
    tmp_path: Path,
) -> None:
    compiled = compile_linux_permissions(_policy(tmp_path))

    protected = {path.as_posix() for path in compiled.protected_subpaths}

    assert (tmp_path / ".git").as_posix() in protected
    assert (tmp_path / ".codex").as_posix() in protected
    assert (tmp_path / ".agents").as_posix() in protected


def test_compile_linux_permissions_upgrades_duplicate_host_aliases_to_writable(
    tmp_path: Path,
) -> None:
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(
            MountSpec(
                host_path=tmp_path,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
            MountSpec(
                host_path=tmp_path,
                sandbox_path=tmp_path,
                mode="ro",
                required=False,
            ),
        ),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=30),
        env_allowlist=("PATH", "HOME"),
        require_approval=False,
    )

    compiled = compile_linux_permissions(policy)

    write_targets = {root.sandbox_path.as_posix() for root in compiled.write_roots}
    read_targets = {root.sandbox_path.as_posix() for root in compiled.read_roots}
    assert tmp_path.as_posix() in write_targets
    assert tmp_path.as_posix() not in read_targets


def test_compile_linux_permissions_preserves_network_mode(tmp_path: Path) -> None:
    compiled = compile_linux_permissions(_policy(tmp_path, network=NetworkMode.PROXY_ALLOWLIST))

    assert compiled.network == NetworkMode.PROXY_ALLOWLIST


def test_compile_linux_permissions_does_not_infer_read_all_from_private_mount(
    tmp_path: Path,
) -> None:
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(
            MountSpec(
                host_path=Path("/"),
                sandbox_path=Path("/"),
                mode="ro",
                required=True,
            ),
            MountSpec(
                host_path=tmp_path,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
        ),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=30),
        env_allowlist=("PATH",),
        require_approval=False,
        file_system=FileSystemPermissionProfile(entries=()),
    )

    compiled = compile_linux_permissions(policy)

    assert compiled.read_all is False


def test_compile_linux_permissions_rejects_default_write_profile(tmp_path: Path) -> None:
    policy = replace(
        _policy(tmp_path),
        mounts=(),
        workspace_rw=False,
        file_system=FileSystemPermissionProfile.full_access(),
    )

    with pytest.raises(
        ValueError,
        match="unrestricted/default-write.*must bypass Bubblewrap",
    ):
        compile_linux_permissions(policy)


def test_compile_linux_permissions_accepts_default_read_profile(tmp_path: Path) -> None:
    policy = replace(
        _policy(tmp_path),
        mounts=(),
        workspace_rw=False,
        file_system=FileSystemPermissionProfile(
            entries=(),
            default_access=FileSystemAccess.READ,
        ),
    )

    compiled = compile_linux_permissions(policy)

    assert compiled.read_all is True


def test_compile_linux_permissions_compiles_effective_profile_entries(
    tmp_path: Path,
) -> None:
    root = Path(tmp_path.anchor)
    workspace = tmp_path / "workspace"
    readonly = workspace / "readonly"
    denied = workspace / "secret"
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(root, FileSystemAccess.READ),
            FileSystemPermissionEntry(workspace, FileSystemAccess.READ),
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(readonly, FileSystemAccess.READ),
            FileSystemPermissionEntry(denied, FileSystemAccess.DENY),
        ),
        denied_read_globs=(str(workspace / "**" / ".env"),),
    )
    policy = replace(
        _policy(tmp_path),
        mounts=(),
        workspace_rw=False,
        file_system=profile,
    )

    compiled = compile_linux_permissions(policy)

    assert [entry.host_path for entry in compiled.read_roots] == [root, readonly]
    assert [root.host_path for root in compiled.write_roots] == [workspace]
    assert compiled.denied_roots == (denied,)
    assert compiled.denied_globs == (str(workspace / "**" / ".env"),)
    assert readonly in compiled.protected_subpaths
    assert denied not in compiled.protected_subpaths
    assert workspace / ".git" in compiled.protected_subpaths
    assert compiled.read_all is profile.has_full_disk_read_baseline
    assert profile.unsandboxed_execution_allowed is False


def test_compile_linux_permissions_compiles_workspace_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    policy = replace(
        _policy(tmp_path),
        mounts=(),
        workspace_rw=False,
        file_system=profile,
    )

    compiled = compile_linux_permissions(policy)

    assert compiled.read_all is profile.has_full_disk_read_baseline
    assert workspace in {root.host_path for root in compiled.write_roots}
    assert workspace / ".git" in compiled.protected_subpaths


def test_compile_linux_permissions_has_no_builtin_sensitive_deny_roots(
    tmp_path: Path,
) -> None:
    compiled = compile_linux_permissions(_policy(tmp_path))

    assert compiled.denied_roots == ()


def test_compile_linux_permissions_preserves_explicit_denied_roots(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy = SandboxPolicy(
        **{
            **policy.__dict__,
            "file_system": FileSystemPermissionProfile.workspace(
                workspace=tmp_path,
                denied_read_roots=(tmp_path / "secret",),
            ),
        }
    )

    compiled = compile_linux_permissions(policy)

    assert compiled.denied_roots == ((tmp_path / "secret").resolve(),)
