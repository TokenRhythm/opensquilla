from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from opensquilla.sandbox.backend.linux_permissions import compile_linux_permissions
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.policy import build_policy
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SecurityLevel,
)


def _make_directory_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlink unsupported/unavailable: {exc}")


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
    assert denied in compiled.protected_subpaths
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


@pytest.mark.parametrize(
    "access",
    (FileSystemAccess.READ, FileSystemAccess.DENY),
)
def test_compile_linux_permissions_protects_all_symlinked_carveout_variants(
    tmp_path: Path,
    access: FileSystemAccess,
) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "outside-target"
    workspace.mkdir()
    target.mkdir()
    logical = workspace / "carveout-link"
    _make_directory_symlink(logical, target)
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(
                target,
                access,
                logical_path=logical,
            ),
        )
    )
    policy = replace(
        _policy(tmp_path),
        mounts=(),
        workspace_rw=False,
        file_system=profile,
    )

    compiled = compile_linux_permissions(policy)

    assert logical in compiled.protected_subpaths
    assert target in compiled.protected_subpaths
    if access is FileSystemAccess.READ:
        assert target in {root.host_path for root in compiled.read_roots}
        assert logical not in {root.host_path for root in compiled.read_roots}
        assert compiled.denied_roots == ()
    else:
        assert compiled.denied_roots == (logical, target)


def test_compile_linux_permissions_protects_retargeted_metadata_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    frozen_target = tmp_path / "frozen-target"
    current_target = tmp_path / "current-target"
    workspace.mkdir()
    frozen_target.mkdir()
    current_target.mkdir()
    logical = workspace / ".git"
    _make_directory_symlink(logical, frozen_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    logical.unlink()
    _make_directory_symlink(logical, current_target)
    policy = replace(
        _policy(tmp_path),
        mounts=(),
        workspace_rw=False,
        file_system=profile,
    )

    compiled = compile_linux_permissions(policy)

    assert logical in compiled.protected_subpaths
    assert frozen_target in compiled.protected_subpaths
    assert current_target in compiled.protected_subpaths


def test_compile_linux_permissions_denies_all_retargeted_alias_variants(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    frozen_target = tmp_path / "frozen-secret"
    current_target = tmp_path / "current-secret"
    logical = tmp_path / "secret-alias"
    workspace.mkdir()
    frozen_target.mkdir()
    current_target.mkdir()
    _make_directory_symlink(logical, frozen_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        denied_read_roots=(logical,),
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    logical.unlink()
    _make_directory_symlink(logical, current_target)
    policy = replace(
        _policy(tmp_path),
        mounts=(),
        workspace_rw=False,
        file_system=profile,
    )

    compiled = compile_linux_permissions(policy)

    assert compiled.denied_roots == (
        logical,
        frozen_target,
        current_target,
    )


def test_compile_linux_permissions_rejects_retargeted_write_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    writable_root = tmp_path / "writable-root"
    current_target = tmp_path / "current-target"
    workspace.mkdir()
    writable_root.mkdir()
    current_target.mkdir()
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        writable_roots=(writable_root,),
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    writable_root.rmdir()
    _make_directory_symlink(writable_root, current_target)
    policy = replace(
        _policy(tmp_path),
        mounts=(),
        workspace_rw=False,
        file_system=profile,
    )

    with pytest.raises(ValueError, match="retargeted writable filesystem root"):
        compile_linux_permissions(policy)


def test_compile_linux_permissions_does_not_follow_retargeted_write_alias(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    frozen_target = tmp_path / "frozen-target"
    current_target = tmp_path / "current-target"
    logical = tmp_path / "writable-alias"
    workspace.mkdir()
    frozen_target.mkdir()
    current_target.mkdir()
    _make_directory_symlink(logical, frozen_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        writable_roots=(logical,),
        tmp_writable=False,
        tmpdir_env_writable=False,
        protect_metadata=False,
    )
    logical.unlink()
    _make_directory_symlink(logical, current_target)
    policy = replace(
        _policy(tmp_path),
        mounts=(),
        workspace_rw=False,
        file_system=profile,
    )

    compiled = compile_linux_permissions(policy)

    write_roots = {root.host_path for root in compiled.write_roots}
    assert frozen_target in write_roots
    assert logical not in write_roots
    assert current_target not in write_roots


def test_compile_linux_permissions_freezes_retargeted_built_policy_rw_mount(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    frozen_target = tmp_path / "frozen-target"
    current_target = tmp_path / "current-target"
    logical = tmp_path / "writable-alias"
    workspace.mkdir()
    frozen_target.mkdir()
    current_target.mkdir()
    _make_directory_symlink(logical, frozen_target)
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(
            host_root_readonly=False,
            network_default="none",
            extra_rw_mounts=[str(logical)],
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
    )
    logical.unlink()
    _make_directory_symlink(logical, current_target)

    compiled = compile_linux_permissions(policy)

    assert any(
        root.host_path == frozen_target and root.sandbox_path == logical
        for root in compiled.write_roots
    )
    assert logical not in {root.host_path for root in compiled.write_roots}
    assert current_target not in {root.host_path for root in compiled.write_roots}


def test_compile_linux_permissions_preserves_stable_symlink_mount_mapping(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    frozen_target = tmp_path / "frozen-target"
    logical = tmp_path / "writable-alias"
    sandbox_path = Path("/mounted/generated")
    workspace.mkdir()
    frozen_target.mkdir()
    _make_directory_symlink(logical, frozen_target)
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(
            host_root_readonly=False,
            network_default="none",
            extra_rw_mounts=[str(logical)],
            exclude_slash_tmp=True,
            exclude_tmpdir_env_var=True,
        ),
    )
    policy = replace(
        policy,
        mounts=tuple(
            replace(mount, sandbox_path=sandbox_path) if mount.host_path == logical else mount
            for mount in policy.mounts
        ),
    )

    compiled = compile_linux_permissions(policy)

    assert any(
        root.host_path == frozen_target and root.sandbox_path == sandbox_path
        for root in compiled.write_roots
    )


@pytest.mark.parametrize(
    "restricted_access",
    (FileSystemAccess.READ, FileSystemAccess.DENY),
)
def test_compile_linux_permissions_removes_exact_conflicting_alias_write_root(
    tmp_path: Path,
    restricted_access: FileSystemAccess,
) -> None:
    target = tmp_path / "shared-target"
    writable_alias = tmp_path / "writable-alias"
    restricted_alias = tmp_path / "restricted-alias"
    sandbox_path = Path("/mounted/shared")
    target.mkdir()
    _make_directory_symlink(writable_alias, target)
    _make_directory_symlink(restricted_alias, target)
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(
                target,
                FileSystemAccess.WRITE,
                logical_path=writable_alias,
            ),
            FileSystemPermissionEntry(
                target,
                restricted_access,
                logical_path=restricted_alias,
            ),
        )
    )
    policy = replace(
        _policy(tmp_path),
        mounts=(
            MountSpec(
                host_path=writable_alias,
                sandbox_path=sandbox_path,
                mode="rw",
                required=True,
            ),
        ),
        file_system=profile,
    )

    compiled = compile_linux_permissions(policy)

    assert profile.resolve(target) is restricted_access
    assert compiled.write_roots == ()
    if restricted_access is FileSystemAccess.READ:
        assert any(
            root.host_path == target and root.sandbox_path == sandbox_path
            for root in compiled.read_roots
        )
    else:
        assert target not in {root.host_path for root in compiled.read_roots}
        assert {restricted_alias, target} <= set(compiled.denied_roots)


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
