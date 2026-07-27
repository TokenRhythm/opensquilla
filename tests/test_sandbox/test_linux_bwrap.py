from __future__ import annotations

import sys
from pathlib import Path

import pytest

from opensquilla.sandbox.backend import bubblewrap as bubblewrap_mod
from opensquilla.sandbox.backend.bubblewrap import BubblewrapBackend
from opensquilla.sandbox.backend.linux_bwrap import (
    HOST_RUNTIME_READONLY_PATHS,
    BwrapOptions,
    build_bwrap_argv,
    build_bwrap_plan,
)
from opensquilla.sandbox.backend.linux_permissions import (
    LinuxPermissions,
    LinuxRoot,
    compile_linux_permissions,
)
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.operation_runtime import SandboxOperation
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.policy import build_policy
from opensquilla.sandbox.types import (
    NetworkMode,
    ResourceLimits,
    SandboxBackendError,
    SandboxPolicy,
    SecurityLevel,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="Linux bubblewrap backend tests require POSIX path and process semantics",
)


def _permissions(tmp_path: Path, *, network: NetworkMode = NetworkMode.NONE) -> LinuxPermissions:
    return LinuxPermissions(
        read_roots=(LinuxRoot(tmp_path / "docs", Path("/workspace/docs"), required=False),),
        write_roots=(LinuxRoot(tmp_path, Path("/workspace"), required=True),),
        denied_roots=(),
        protected_subpaths=(tmp_path / ".git",),
        env_allowlist=("PATH",),
        network=network,
        tmp_writable=True,
        wall_timeout_s=30,
    )


@pytest.mark.asyncio
async def test_filesystem_operation_carries_profile_without_mounting_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = FileSystemPermissionProfile.workspace(workspace=workspace)
    captured: dict[str, object] = {}

    async def fake_run_helper(payload: object) -> dict[str, object]:
        captured["payload"] = payload
        return {"message": "listed"}

    monkeypatch.setattr(bubblewrap_mod, "_run_linux_helper_payload", fake_run_helper)

    result = await BubblewrapBackend().run_operation(
        SandboxOperation.filesystem(
            kind="list_dir",
            workspace=workspace,
            run_mode="trusted",
            path=Path("/etc"),
            paths=(Path("/etc"),),
            file_system_profile=profile,
        )
    )

    assert result.message == "listed"
    payload = captured["payload"]
    assert isinstance(payload, bubblewrap_mod.HelperPayload)
    file_system = payload.policy["fileSystem"]
    assert isinstance(file_system, dict)
    entries = {entry["path"]: entry["access"] for entry in file_system["entries"]}
    assert entries["/"] == "read"
    assert entries[str(workspace)] == "write"
    mounts = payload.policy["mounts"]
    assert all(mount["host"] != "/etc" and mount["sandbox"] != "/etc" for mount in mounts)
    assert payload.filesystem is not None
    assert "file_system_profile" not in payload.filesystem.worker_payload
    assert "fileSystemProfile" not in payload.filesystem.worker_payload


def test_bwrap_argv_uses_readonly_baseline_and_write_layers(tmp_path: Path) -> None:
    argv = build_bwrap_argv(
        command=["/bin/echo", "ok"],
        command_cwd=tmp_path,
        permissions=_permissions(tmp_path),
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    assert argv[:2] == ["bwrap", "--new-session"]
    assert "--ro-bind" in argv
    assert "/" in argv
    assert "--bind" in argv
    assert str(tmp_path) in argv
    assert "/workspace" in argv
    assert "--unshare-user" in argv
    assert "--unshare-pid" in argv
    assert "--unshare-net" in argv
    assert "--proc" in argv
    assert argv[-2:] == ["/bin/echo", "ok"]


def test_linux_runtime_readonly_paths_match_codex_platform_defaults() -> None:
    paths = {path.as_posix() for path in HOST_RUNTIME_READONLY_PATHS}

    assert {
        "/bin",
        "/sbin",
        "/usr",
        "/etc",
        "/lib",
        "/lib64",
        "/nix/store",
        "/run/current-system/sw",
    }.issubset(paths)


def test_bwrap_argv_does_not_expand_legacy_root_without_read_all(tmp_path: Path) -> None:
    permissions = LinuxPermissions(
        read_roots=(LinuxRoot(Path("/"), Path("/"), required=True),),
        write_roots=(LinuxRoot(tmp_path, tmp_path, required=True),),
        denied_roots=(),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=tmp_path,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    triples = [argv[index : index + 3] for index in range(len(argv) - 2)]
    pairs = [argv[index : index + 2] for index in range(len(argv) - 1)]
    assert ["--ro-bind", "/", "/"] not in triples
    assert ["--tmpfs", "/"] in pairs


def test_default_workspace_profile_binds_host_tmp_writable(tmp_path: Path) -> None:
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        SandboxSettings(),
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=tmp_path,
        permissions=compile_linux_permissions(policy),
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    triples = [argv[index : index + 3] for index in range(len(argv) - 2)]
    pairs = [argv[index : index + 2] for index in range(len(argv) - 1)]
    assert ["--bind", "/tmp", "/tmp"] in triples
    assert ["--tmpfs", "/tmp"] not in pairs


def test_compiled_workspace_profile_orders_read_baseline_write_and_protection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    protected = workspace / ".git"
    protected.mkdir(parents=True)
    (protected / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(),
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=workspace,
        permissions=compile_linux_permissions(policy),
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    root_bind_index = next(
        index
        for index in range(len(argv) - 2)
        if argv[index : index + 3] == ["--ro-bind", "/", "/"]
    )
    workspace_bind_index = next(
        index
        for index in range(len(argv) - 2)
        if argv[index : index + 3] == ["--bind", str(workspace), str(workspace)]
    )
    protected_bind_indices = [
        index
        for index in range(len(argv) - 2)
        if argv[index : index + 3]
        == ["--ro-bind", str(protected), str(protected)]
    ]

    assert root_bind_index < workspace_bind_index < max(protected_bind_indices)


def test_readonly_root_skips_redundant_identity_mounts(tmp_path: Path) -> None:
    identity_root = Path.home()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    permissions = LinuxPermissions(
        read_roots=(
            LinuxRoot(Path("/"), Path("/"), required=True),
            LinuxRoot(identity_root, identity_root, required=True),
        ),
        write_roots=(LinuxRoot(workspace, workspace, required=True),),
        denied_roots=(),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
        read_all=True,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=workspace,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    assert ["--ro-bind", str(identity_root), str(identity_root)] not in [
        argv[index : index + 3] for index in range(len(argv) - 2)
    ]


def test_readonly_root_keeps_identity_mount_below_tmpfs(tmp_path: Path) -> None:
    payload_dir = tmp_path / "payload"
    payload_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    permissions = LinuxPermissions(
        read_roots=(
            LinuxRoot(Path("/"), Path("/"), required=True),
            LinuxRoot(payload_dir, payload_dir, required=True),
        ),
        write_roots=(LinuxRoot(workspace, workspace, required=True),),
        denied_roots=(),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=workspace,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    assert ["--ro-bind", str(payload_dir), str(payload_dir)] in [
        argv[index : index + 3] for index in range(len(argv) - 2)
    ]


def test_readonly_root_canonicalizes_denied_symlink_aliases(tmp_path: Path) -> None:
    real_dir = tmp_path / "run"
    real_dir.mkdir()
    secret = real_dir / "service.sock"
    secret.write_text("secret", encoding="utf-8")
    alias_dir = tmp_path / "var-run"
    alias_dir.symlink_to(real_dir, target_is_directory=True)
    alias = alias_dir / secret.name
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    permissions = LinuxPermissions(
        read_roots=(LinuxRoot(Path("/"), Path("/"), required=True),),
        write_roots=(LinuxRoot(workspace, workspace, required=True),),
        denied_roots=(alias, secret),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=workspace,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    assert str(alias) not in argv
    assert argv.count(str(secret)) == 1


def test_bwrap_argv_keeps_host_network_when_network_mode_host(tmp_path: Path) -> None:
    argv = build_bwrap_argv(
        command=["/bin/echo", "ok"],
        command_cwd=tmp_path,
        permissions=_permissions(tmp_path, network=NetworkMode.HOST),
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    assert "--unshare-net" not in argv


def test_bwrap_argv_can_skip_proc(tmp_path: Path) -> None:
    argv = build_bwrap_argv(
        command=["/bin/echo", "ok"],
        command_cwd=tmp_path,
        permissions=_permissions(tmp_path),
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=False),
    )

    assert "--proc" not in argv


def test_bwrap_argv_creates_parent_dirs_for_file_read_roots(tmp_path: Path) -> None:
    target = tmp_path / "pyproject.toml"
    target.write_text("[project]\n", encoding="utf-8")
    permissions = LinuxPermissions(
        read_roots=(LinuxRoot(target, target, required=True),),
        write_roots=(),
        denied_roots=(),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    argv = build_bwrap_argv(
        command=["/bin/cat", str(target)],
        command_cwd=tmp_path,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    dir_targets = [argv[index + 1] for index, item in enumerate(argv) if item == "--dir"]
    assert str(target) not in dir_targets
    assert str(target.parent) in dir_targets
    bind_index = argv.index(str(target))
    assert argv[bind_index - 1] == "--ro-bind"
    assert argv[bind_index + 1] == str(target)


def test_bwrap_argv_masks_missing_protected_metadata_paths(tmp_path: Path) -> None:
    protected = tmp_path / ".codex"
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(tmp_path, Path("/workspace"), required=True),),
        denied_roots=(),
        protected_subpaths=(protected,),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=tmp_path,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    protected_target = str(protected)
    assert protected_target in argv
    protected_index = argv.index(protected_target)
    assert argv[protected_index - 1] == "--tmpfs"
    assert "--remount-ro" in argv


def test_bwrap_plan_tracks_missing_protected_metadata_synthetic_mount(
    tmp_path: Path,
) -> None:
    protected = tmp_path / ".codex"
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(tmp_path, tmp_path, required=True),),
        denied_roots=(),
        protected_subpaths=(protected,),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    plan = build_bwrap_plan(
        command=["/bin/true"],
        command_cwd=tmp_path,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    assert [(target.path, target.kind) for target in plan.synthetic_mount_targets] == [
        (protected, "empty_directory")
    ]


def test_bwrap_plan_treats_existing_empty_protected_file_as_synthetic_mount(
    tmp_path: Path,
) -> None:
    protected = tmp_path / ".git"
    protected.write_text("", encoding="utf-8")
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(tmp_path, tmp_path, required=True),),
        denied_roots=(),
        protected_subpaths=(protected,),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    plan = build_bwrap_plan(
        command=["/bin/true"],
        command_cwd=tmp_path,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    protected_target = str(protected)
    assert any(
        window[0] == "--ro-bind" and window[2] == protected_target
        for window in (plan.argv[index : index + 3] for index in range(len(plan.argv) - 2))
    )
    assert ["--ro-bind", protected_target, protected_target] not in [
        plan.argv[index : index + 3] for index in range(len(plan.argv) - 2)
    ]
    assert [(target.path, target.kind) for target in plan.synthetic_mount_targets] == [
        (protected, "empty_file")
    ]
    assert plan.synthetic_mount_targets[0].pre_existing_identity is not None


def test_bwrap_argv_rejects_symlinked_protected_metadata_paths(tmp_path: Path) -> None:
    protected_target = tmp_path / "agents-target"
    protected_target.mkdir()
    protected = tmp_path / ".agents"
    protected.symlink_to(protected_target, target_is_directory=True)
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(tmp_path, tmp_path, required=True),),
        denied_roots=(),
        protected_subpaths=(protected,),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    with pytest.raises(SandboxBackendError, match="cannot enforce sandbox read-only path"):
        build_bwrap_argv(
            command=["/bin/true"],
            command_cwd=tmp_path,
            permissions=permissions,
            options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
        )


def test_bwrap_plan_leaves_missing_child_git_for_parent_repo_discovery(
    tmp_path: Path,
) -> None:
    parent_git = tmp_path / ".git"
    parent_git.mkdir()
    (parent_git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    workspace = tmp_path / "child"
    workspace.mkdir()
    protected = workspace / ".git"
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(workspace, workspace, required=True),),
        denied_roots=(),
        protected_subpaths=(protected,),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    plan = build_bwrap_plan(
        command=["/bin/true"],
        command_cwd=workspace,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    protected_target = str(protected)
    assert ["--perms", "555", "--tmpfs", protected_target] not in [
        plan.argv[index : index + 4]
        for index in range(len(plan.argv) - 3)
    ]
    assert plan.protected_create_targets == (protected,)


def test_bwrap_argv_does_not_recreate_protected_parent_after_bind(
    tmp_path: Path,
) -> None:
    protected = tmp_path / ".git"
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(tmp_path, tmp_path, required=True),),
        denied_roots=(),
        protected_subpaths=(protected,),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=tmp_path,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    bind_index = argv.index(str(tmp_path))
    protected_index = argv.index(str(protected))
    parent_dirs_after_bind = [
        argv[index + 1]
        for index, item in enumerate(argv[bind_index + 1 : protected_index], start=bind_index + 1)
        if item == "--dir"
    ]
    assert str(tmp_path) not in parent_dirs_after_bind


def test_bwrap_argv_masks_denied_roots(tmp_path: Path) -> None:
    denied = tmp_path / "private"
    denied.mkdir()
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(tmp_path, tmp_path, required=True),),
        denied_roots=(denied,),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=tmp_path,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    denied_target = str(denied)
    assert denied_target in argv
    denied_index = argv.index(denied_target)
    assert argv[denied_index - 1] == "--tmpfs"
    assert ["--perms", "000", "--tmpfs", denied_target] == argv[
        denied_index - 3 : denied_index + 1
    ]


def test_bwrap_plan_masks_missing_denied_root_with_empty_file_bind(
    tmp_path: Path,
) -> None:
    denied = tmp_path / "missing" / "secret.txt"
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(tmp_path, tmp_path, required=True),),
        denied_roots=(denied,),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    plan = build_bwrap_plan(
        command=["/bin/true"],
        command_cwd=tmp_path,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    missing_component = tmp_path / "missing"
    assert plan.preserved_files
    assert any(
        window[0] == "--ro-bind" and window[2] == str(missing_component)
        for window in (plan.argv[index : index + 3] for index in range(len(plan.argv) - 2))
    )
    assert ["--perms", "000", "--ro-bind"] not in [
        plan.argv[index : index + 3] for index in range(len(plan.argv) - 2)
    ]
    assert ["--tmpfs", str(denied)] not in [
        plan.argv[index : index + 2] for index in range(len(plan.argv) - 1)
    ]
    assert [(target.path, target.kind) for target in plan.synthetic_mount_targets] == [
        (missing_component, "empty_file")
    ]


def test_bwrap_plan_expands_denied_globs_to_file_masks(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("secret", encoding="utf-8")
    permissions = LinuxPermissions(
        read_roots=(LinuxRoot(tmp_path, tmp_path, required=True),),
        write_roots=(),
        denied_roots=(),
        denied_globs=(str(tmp_path / "**" / ".env"),),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    plan = build_bwrap_plan(
        command=["/bin/true"],
        command_cwd=tmp_path,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    secret_target = str(secret)
    assert plan.preserved_files
    assert any(
        window[0] == "--ro-bind"
        and window[2] == secret_target
        for window in (plan.argv[index : index + 3] for index in range(len(plan.argv) - 2))
    )


def test_bwrap_plan_denied_globs_mask_symlink_targets(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    link_root = tmp_path / "link"
    real_root.mkdir()
    secret = real_root / "secret.env"
    secret.write_text("secret", encoding="utf-8")
    link_root.symlink_to(real_root, target_is_directory=True)
    permissions = LinuxPermissions(
        read_roots=(LinuxRoot(tmp_path, tmp_path, required=True),),
        write_roots=(),
        denied_roots=(),
        denied_globs=(str(link_root / "**" / "*.env"),),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    plan = build_bwrap_plan(
        command=["/bin/true"],
        command_cwd=tmp_path,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    assert str(secret) in plan.argv


def test_compiled_profile_reopens_read_grant_below_denied_parent(
    tmp_path: Path,
) -> None:
    denied_parent = tmp_path / "home"
    readable_child = denied_parent / "user" / "project"
    denied_grandchild = readable_child / "private"
    denied_grandchild.mkdir(parents=True)
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(Path("/"), FileSystemAccess.READ),
            FileSystemPermissionEntry(denied_parent, FileSystemAccess.DENY),
            FileSystemPermissionEntry(readable_child, FileSystemAccess.READ),
            FileSystemPermissionEntry(denied_grandchild, FileSystemAccess.DENY),
        )
    )
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(),
        workspace_rw=False,
        tmp_writable=False,
        limits=ResourceLimits(wall_timeout_s=30),
        env_allowlist=("PATH",),
        require_approval=False,
        file_system=profile,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=readable_child,
        permissions=compile_linux_permissions(policy),
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    parent_mask_indices = [
        index
        for index in range(len(argv) - 1)
        if argv[index : index + 2] == ["--tmpfs", str(denied_parent)]
    ]
    child_bind_indices = [
        index
        for index in range(len(argv) - 2)
        if argv[index : index + 3]
        == ["--ro-bind", str(readable_child), str(readable_child)]
    ]
    grandchild_mask_indices = [
        index
        for index in range(len(argv) - 1)
        if argv[index : index + 2] == ["--tmpfs", str(denied_grandchild)]
    ]

    assert len(parent_mask_indices) == 1
    assert len(child_bind_indices) == 1
    assert len(grandchild_mask_indices) == 1
    parent_mask_index = parent_mask_indices[0]
    child_bind_index = child_bind_indices[0]
    grandchild_mask_index = grandchild_mask_indices[0]
    assert argv[parent_mask_index - 2 : parent_mask_index + 2] == [
        "--perms",
        "111",
        "--tmpfs",
        str(denied_parent),
    ]
    assert parent_mask_index < child_bind_index < grandchild_mask_index


def test_compiled_profile_reopens_read_carveout_inside_write_root(
    tmp_path: Path,
) -> None:
    denied_parent = tmp_path / "home"
    readable_child = denied_parent / "user" / "project"
    denied_grandchild = readable_child / "private"
    denied_grandchild.mkdir(parents=True)
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(Path("/"), FileSystemAccess.READ),
            FileSystemPermissionEntry(tmp_path, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(denied_parent, FileSystemAccess.DENY),
            FileSystemPermissionEntry(readable_child, FileSystemAccess.READ),
            FileSystemPermissionEntry(denied_grandchild, FileSystemAccess.DENY),
        )
    )
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(),
        workspace_rw=False,
        tmp_writable=False,
        limits=ResourceLimits(wall_timeout_s=30),
        env_allowlist=("PATH",),
        require_approval=False,
        file_system=profile,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=readable_child,
        permissions=compile_linux_permissions(policy),
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    workspace_bind_index = next(
        index
        for index in range(len(argv) - 2)
        if argv[index : index + 3] == ["--bind", str(tmp_path), str(tmp_path)]
    )
    parent_mask_index = max(
        index
        for index in range(len(argv) - 1)
        if argv[index : index + 2] == ["--tmpfs", str(denied_parent)]
    )
    child_bind_index = max(
        index
        for index in range(len(argv) - 2)
        if argv[index : index + 3]
        == ["--ro-bind", str(readable_child), str(readable_child)]
    )
    grandchild_mask_index = max(
        index
        for index in range(len(argv) - 1)
        if argv[index : index + 2] == ["--tmpfs", str(denied_grandchild)]
    )

    assert (
        workspace_bind_index
        < parent_mask_index
        < child_bind_index
        < grandchild_mask_index
    )


def test_bwrap_argv_binds_symlinked_writable_root_real_target_and_remaps_denied_child(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real"
    link_root = tmp_path / "link"
    blocked = real_root / "blocked"
    blocked.mkdir(parents=True)
    link_root.symlink_to(real_root, target_is_directory=True)
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(link_root, link_root, required=True),),
        denied_roots=(link_root / "blocked",),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=link_root,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    assert ["--bind", str(real_root), str(real_root)] in [
        argv[index : index + 3] for index in range(len(argv) - 2)
    ]
    assert ["--bind", str(link_root), str(link_root)] not in [
        argv[index : index + 3] for index in range(len(argv) - 2)
    ]
    assert ["--chdir", str(real_root)] in [
        argv[index : index + 2] for index in range(len(argv) - 1)
    ]
    assert ["--perms", "000", "--tmpfs", str(blocked)] in [
        argv[index : index + 4] for index in range(len(argv) - 3)
    ]


def test_bwrap_argv_reopens_writable_directory_under_denied_parent(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "blocked"
    allowed = blocked / "allowed"
    allowed.mkdir(parents=True)
    permissions = LinuxPermissions(
        read_roots=(LinuxRoot(Path("/"), Path("/"), required=True),),
        write_roots=(LinuxRoot(allowed, allowed, required=True),),
        denied_roots=(blocked,),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=allowed,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    blocked_mask_index = next(
        index
        for index in range(len(argv) - 3)
        if argv[index : index + 4] == ["--perms", "111", "--tmpfs", str(blocked)]
    )
    allowed_dir_index = next(
        index
        for index in range(len(argv) - 1)
        if argv[index : index + 2] == ["--dir", str(allowed)]
    )
    blocked_remount_index = next(
        index
        for index in range(len(argv) - 1)
        if argv[index : index + 2] == ["--remount-ro", str(blocked)]
    )
    allowed_bind_index = next(
        index
        for index in range(len(argv) - 2)
        if argv[index : index + 3] == ["--bind", str(allowed), str(allowed)]
    )

    assert blocked_mask_index < allowed_dir_index < blocked_remount_index < allowed_bind_index


def test_bwrap_argv_reopens_writable_file_under_denied_parent(
    tmp_path: Path,
) -> None:
    blocked = tmp_path / "blocked"
    allowed_dir = blocked / "allowed"
    allowed_file = allowed_dir / "note.txt"
    allowed_dir.mkdir(parents=True)
    allowed_file.write_text("ok", encoding="utf-8")
    permissions = LinuxPermissions(
        read_roots=(LinuxRoot(Path("/"), Path("/"), required=True),),
        write_roots=(LinuxRoot(allowed_file, allowed_file, required=True),),
        denied_roots=(blocked,),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    argv = build_bwrap_argv(
        command=["/bin/true"],
        command_cwd=allowed_dir,
        permissions=permissions,
        options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
    )

    assert ["--perms", "111", "--tmpfs", str(blocked)] in [
        argv[index : index + 4] for index in range(len(argv) - 3)
    ]
    assert ["--dir", str(allowed_dir)] in [
        argv[index : index + 2] for index in range(len(argv) - 1)
    ]
    assert ["--dir", str(allowed_file)] not in [
        argv[index : index + 2] for index in range(len(argv) - 1)
    ]
    assert ["--bind", str(allowed_file), str(allowed_file)] in [
        argv[index : index + 3] for index in range(len(argv) - 2)
    ]


def test_bwrap_argv_rejects_relative_mount_paths(tmp_path: Path) -> None:
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(Path("relative"), tmp_path, required=True),),
        denied_roots=(),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    with pytest.raises(SandboxBackendError, match="host mount path must be absolute"):
        build_bwrap_argv(
            command=["/bin/true"],
            command_cwd=tmp_path,
            permissions=permissions,
            options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
        )


def test_bwrap_argv_rejects_traversal_mount_paths(tmp_path: Path) -> None:
    permissions = LinuxPermissions(
        read_roots=(),
        write_roots=(LinuxRoot(tmp_path / ".." / "workspace", tmp_path, required=True),),
        denied_roots=(),
        protected_subpaths=(),
        env_allowlist=("PATH",),
        network=NetworkMode.NONE,
        tmp_writable=True,
        wall_timeout_s=30,
    )

    with pytest.raises(SandboxBackendError, match="host mount path contains '..'"):
        build_bwrap_argv(
            command=["/bin/true"],
            command_cwd=tmp_path,
            permissions=permissions,
            options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
        )


def test_bwrap_argv_rejects_relative_command_cwd(tmp_path: Path) -> None:
    with pytest.raises(SandboxBackendError, match="command cwd must be absolute"):
        build_bwrap_argv(
            command=["/bin/true"],
            command_cwd=Path("relative"),
            permissions=_permissions(tmp_path),
            options=BwrapOptions(bwrap_path="bwrap", mount_proc=True),
        )
