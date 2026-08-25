from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.backend import seatbelt as seatbelt_mod
from opensquilla.sandbox.backend import select_backend
from opensquilla.sandbox.backend.seatbelt import (
    SeatbeltBackend,
    _classify_denial,
    build_seatbelt_argv,
    render_seatbelt_profile,
)
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.operation_runtime import SandboxOperation, SandboxOperationResult
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    NetworkProxySpec,
    ResourceLimits,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
    SecurityLevel,
)

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="Seatbelt backend tests model macOS/POSIX paths",
)

_DISALLOWED_PATH_CHARACTERS = tuple(chr(value) for value in (*range(0x20), 0x7F))
_DISALLOWED_PATH_CHARACTER_IDS = tuple(
    f"0x{ord(character):02x}" for character in _DISALLOWED_PATH_CHARACTERS
)


def _policy(
    workspace: Path,
    *,
    network: NetworkMode = NetworkMode.NONE,
    network_proxy: NetworkProxySpec | None = None,
    workspace_rw: bool = True,
    tmp_writable: bool = True,
    mounts: tuple[MountSpec, ...] | None = None,
) -> SandboxPolicy:
    base_mounts = (
        MountSpec(
            host_path=workspace,
            sandbox_path=Path("/workspace"),
            mode="rw" if workspace_rw else "ro",
            required=True,
        ),
    )
    resolved_mounts = mounts or base_mounts
    if workspace_rw:
        file_system = FileSystemPermissionProfile.workspace(
            workspace=workspace,
            readable_roots=(mount.host_path for mount in resolved_mounts if mount.mode == "ro"),
            writable_roots=(
                mount.host_path
                for mount in resolved_mounts
                if mount.mode == "rw" and mount.host_path != workspace
            ),
            tmp_writable=tmp_writable,
            tmpdir_env_writable=tmp_writable,
        )
    else:
        file_system = FileSystemPermissionProfile.read_only(
            readable_roots=(mount.host_path for mount in resolved_mounts),
        )
    return SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=network,
        mounts=resolved_mounts,
        workspace_rw=workspace_rw,
        tmp_writable=tmp_writable,
        limits=ResourceLimits(wall_timeout_s=0.1),
        env_allowlist=("PATH", "LANG"),
        require_approval=False,
        network_proxy=network_proxy,
        file_system=file_system,
    )


def _request(policy: SandboxPolicy, cwd: Path) -> SandboxRequest:
    return SandboxRequest(
        argv=("sh", "-lc", "echo ok"),
        cwd=cwd,
        action_kind="shell.exec",
        policy=policy,
        env={"PATH": "/bin", "SECRET": "nope"},
    )


def _access_rule(profile: str, action: str, root: Path) -> str:
    root_clause = f'(subpath "{root}")'
    rules = [
        line
        for line in profile.splitlines()
        if line.startswith(f"(allow {action}") and root_clause in line
    ]
    assert rules, f"missing {action} rule for {root}"
    return rules[0]


def _directory_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")


def test_available_false_on_non_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seatbelt_mod.sys, "platform", "linux")
    assert SeatbeltBackend(binary="sandbox-exec").available() is False


def test_available_true_on_macos_when_sandbox_exec_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seatbelt_mod.sys, "platform", "darwin")
    monkeypatch.setattr(seatbelt_mod.shutil, "which", lambda name: "/usr/bin/sandbox-exec")
    assert SeatbeltBackend(binary="sandbox-exec").available() is True


def test_available_false_on_macos_when_sandbox_exec_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seatbelt_mod.sys, "platform", "darwin")
    monkeypatch.setattr(seatbelt_mod.shutil, "which", lambda name: None)
    assert SeatbeltBackend(binary="sandbox-exec").available() is False


def test_auto_selects_seatbelt_on_macos_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import backend as backend_mod

    monkeypatch.setattr(backend_mod.sys, "platform", "darwin")
    monkeypatch.setattr(backend_mod.SeatbeltBackend, "available", lambda self: True)

    backend = select_backend(SandboxSettings(sandbox=True, backend="auto"))

    assert backend.name == "seatbelt"


def test_explicit_seatbelt_fails_closed_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.sandbox import backend as backend_mod

    monkeypatch.setattr(backend_mod.SeatbeltBackend, "available", lambda self: False)

    with pytest.raises(SandboxBackendError, match="seatbelt.*unavailable"):
        select_backend(SandboxSettings(sandbox=True, backend="seatbelt"))


def test_profile_denies_default_and_network_none(tmp_path: Path) -> None:
    profile = render_seatbelt_profile(_request(_policy(tmp_path), tmp_path))

    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert _access_rule(profile, "file-read*", Path("/"))
    assert _access_rule(profile, "file-write*", tmp_path)


def test_profile_fails_closed_without_resolved_filesystem_profile(tmp_path: Path) -> None:
    policy = replace(_policy(tmp_path), file_system=None)

    rendered = render_seatbelt_profile(_request(policy, tmp_path))

    assert not any(
        line.startswith("(allow file-read* (require-all")
        or line.startswith("(allow file-write* (require-all")
        for line in rendered.splitlines()
    )


def test_profile_allows_network_host(tmp_path: Path) -> None:
    profile = render_seatbelt_profile(
        _request(_policy(tmp_path, network=NetworkMode.HOST), tmp_path)
    )

    assert "(allow network-outbound)" in profile
    assert "(allow network-inbound)" in profile


def test_profile_rejects_proxy_allowlist_without_proxy(tmp_path: Path) -> None:
    with pytest.raises(SandboxBackendError, match="network proxy"):
        render_seatbelt_profile(
            _request(_policy(tmp_path, network=NetworkMode.PROXY_ALLOWLIST), tmp_path)
        )


def test_profile_allows_only_proxy_endpoint_for_proxy_allowlist(tmp_path: Path) -> None:
    profile = render_seatbelt_profile(
        _request(
            _policy(
                tmp_path,
                network=NetworkMode.PROXY_ALLOWLIST,
                network_proxy=NetworkProxySpec(host="127.0.0.1", port=18080),
            ),
            tmp_path,
        )
    )

    assert "(allow network-outbound" in profile
    assert "localhost:18080" in profile
    assert "127.0.0.1:18080" not in profile
    assert "(allow network*)" not in profile
    assert "(deny network*)" not in profile


def test_profile_allows_full_disk_read_like_codex(tmp_path: Path) -> None:
    profile = render_seatbelt_profile(_request(_policy(tmp_path), tmp_path))

    assert "; allow read-only file operations" in profile
    assert _access_rule(profile, "file-read*", Path("/"))


def test_profile_full_read_excludes_explicit_denied_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    secret = tmp_path / "secret"
    workspace.mkdir()
    secret.mkdir()
    file_system = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        denied_read_roots=(secret,),
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    policy = replace(_policy(workspace), file_system=file_system)

    rendered = render_seatbelt_profile(_request(policy, workspace))

    root_read = _access_rule(rendered, "file-read*", Path("/"))
    workspace_write = _access_rule(rendered, "file-write*", workspace)
    assert f'(require-not (literal "{secret}"))' in root_read
    assert f'(require-not (subpath "{secret}"))' in root_read
    assert f'(subpath "{workspace}")' in workspace_write


def test_profile_default_read_does_not_reopen_exact_denied_root(
    tmp_path: Path,
) -> None:
    file_system = FileSystemPermissionProfile(
        entries=(FileSystemPermissionEntry(Path("/"), FileSystemAccess.DENY),),
        default_access=FileSystemAccess.READ,
    )
    policy = replace(_policy(tmp_path), file_system=file_system)

    rendered = render_seatbelt_profile(_request(policy, tmp_path))

    root_read = _access_rule(rendered, "file-read*", Path("/"))
    guards = '(require-not (literal "/")) (require-not (subpath "/"))'
    assert root_read.startswith(
        f'(allow file-read* (require-all (literal "/") {guards}) '
        f'(require-all (subpath "/") {guards})'
    )


@pytest.mark.parametrize("carveout", (FileSystemAccess.READ, FileSystemAccess.DENY))
def test_profile_default_write_does_not_reopen_exact_restricted_root(
    carveout: FileSystemAccess,
    tmp_path: Path,
) -> None:
    file_system = FileSystemPermissionProfile(
        entries=(FileSystemPermissionEntry(Path("/"), carveout),),
        default_access=FileSystemAccess.WRITE,
    )
    policy = replace(_policy(tmp_path), file_system=file_system)

    rendered = render_seatbelt_profile(_request(policy, tmp_path))

    root_write = _access_rule(rendered, "file-write*", Path("/"))
    guards = '(require-not (literal "/")) (require-not (subpath "/"))'
    assert root_write.startswith(f'(allow file-write* (require-all (literal "/") {guards} ')
    assert f'(require-all (subpath "/") {guards} ' in root_write


def test_profile_fails_closed_for_unrepresentable_denied_glob(tmp_path: Path) -> None:
    file_system = FileSystemPermissionProfile.workspace(
        workspace=tmp_path,
        denied_read_globs=(str(tmp_path / "**" / "*.pem"),),
    )
    policy = replace(_policy(tmp_path), file_system=file_system)

    with pytest.raises(SandboxBackendError, match="denied read glob"):
        render_seatbelt_profile(_request(policy, tmp_path))


def test_profile_workspace_write_excludes_metadata_and_profile_carveouts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    readonly = workspace / "readonly"
    secret = workspace / "secret"
    workspace.mkdir()
    file_system = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(Path("/"), FileSystemAccess.READ),
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(readonly, FileSystemAccess.READ),
            FileSystemPermissionEntry(secret, FileSystemAccess.DENY),
        )
    )
    policy = replace(_policy(workspace), file_system=file_system)

    rendered = render_seatbelt_profile(_request(policy, workspace))

    workspace_write = _access_rule(rendered, "file-write*", workspace)
    assert f'(require-not (literal "{readonly}"))' in workspace_write
    assert f'(require-not (subpath "{readonly}"))' in workspace_write
    assert f'(require-not (literal "{secret}"))' in workspace_write
    assert f'(require-not (subpath "{secret}"))' in workspace_write
    for name in (".git", ".agents", ".codex"):
        assert name.replace(".", "\\.") in workspace_write


@pytest.mark.parametrize("access", (FileSystemAccess.READ, FileSystemAccess.DENY))
def test_profile_expands_logical_and_canonical_carveout_variants(
    access: FileSystemAccess,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "restricted-target"
    logical = workspace / "restricted"
    workspace.mkdir()
    target.mkdir()
    _directory_symlink(logical, target)
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
    policy = replace(_policy(workspace), file_system=profile)

    rendered = render_seatbelt_profile(_request(policy, workspace))

    workspace_write = _access_rule(rendered, "file-write*", workspace)
    for variant in (logical, target):
        assert f'(require-not (literal "{variant}"))' in workspace_write
        assert f'(require-not (subpath "{variant}"))' in workspace_write


@pytest.mark.parametrize(
    "restricted_access",
    (FileSystemAccess.READ, FileSystemAccess.DENY),
)
def test_profile_exact_conflicting_alias_write_uses_restrictive_rule(
    tmp_path: Path,
    restricted_access: FileSystemAccess,
) -> None:
    target = tmp_path / "shared-target"
    writable_alias = tmp_path / "writable-alias"
    restricted_alias = tmp_path / "restricted-alias"
    target.mkdir()
    _directory_symlink(writable_alias, target)
    _directory_symlink(restricted_alias, target)
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
    policy = replace(_policy(tmp_path), file_system=profile)

    rendered = render_seatbelt_profile(_request(policy, tmp_path))

    assert profile.resolve(target) is restricted_access
    target_write = _access_rule(rendered, "file-write*", target)
    assert f'(require-not (literal "{target}"))' in target_write
    assert f'(require-not (subpath "{target}"))' in target_write


@pytest.mark.parametrize("mutation", ("create", "retarget"))
def test_profile_tracks_current_metadata_symlink_target_after_profile_freeze(
    mutation: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    original_target = workspace / "original-target"
    current_target = workspace / "current-target"
    metadata = workspace / ".git"
    workspace.mkdir()
    original_target.mkdir()
    current_target.mkdir()
    if mutation == "retarget":
        _directory_symlink(metadata, original_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    if metadata.is_symlink():
        metadata.unlink()
    _directory_symlink(metadata, current_target)
    policy = replace(_policy(workspace), file_system=profile)

    rendered = render_seatbelt_profile(_request(policy, workspace))

    workspace_write = _access_rule(rendered, "file-write*", workspace)
    expected = (
        (metadata, current_target)
        if mutation == "create"
        else (metadata, original_target, current_target)
    )
    for variant in expected:
        assert f'(require-not (literal "{variant}"))' in workspace_write
        assert f'(require-not (subpath "{variant}"))' in workspace_write
    assert r"\.git" in workspace_write


def test_profile_write_roots_do_not_follow_retargeted_alias(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    frozen_target = tmp_path / "frozen-target"
    current_target = tmp_path / "current-target"
    alias = tmp_path / "writable-alias"
    workspace.mkdir()
    frozen_target.mkdir()
    current_target.mkdir()
    _directory_symlink(alias, frozen_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        writable_roots=(alias,),
        tmp_writable=False,
        tmpdir_env_writable=False,
        protect_metadata=False,
    )
    alias.unlink()
    _directory_symlink(alias, current_target)
    policy = replace(_policy(workspace), file_system=profile)

    rendered = render_seatbelt_profile(_request(policy, workspace))

    assert f'(literal "{frozen_target}")' in rendered
    assert f'(literal "{current_target}")' not in rendered


def test_profile_validates_logical_entry_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(
                workspace / "canonical",
                FileSystemAccess.READ,
                logical_path=workspace / "logical\x01path",
            ),
        )
    )
    policy = replace(_policy(workspace), file_system=profile)

    with pytest.raises(SandboxBackendError, match="control character"):
        render_seatbelt_profile(_request(policy, workspace))


def test_profile_rejects_retargeted_writable_root(tmp_path: Path) -> None:
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
    _directory_symlink(writable_root, current_target)
    policy = replace(_policy(workspace), file_system=profile)

    with pytest.raises(
        SandboxBackendError,
        match="retargeted writable filesystem root",
    ):
        render_seatbelt_profile(_request(policy, workspace))


def test_profile_more_specific_read_reopens_denied_parent(tmp_path: Path) -> None:
    denied_parent = tmp_path / "home"
    readable_child = denied_parent / "user" / "project"
    denied_grandchild = readable_child / "private"
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(Path("/"), FileSystemAccess.READ),
            FileSystemPermissionEntry(denied_parent, FileSystemAccess.DENY),
            FileSystemPermissionEntry(readable_child, FileSystemAccess.READ),
            FileSystemPermissionEntry(denied_grandchild, FileSystemAccess.DENY),
        )
    )
    policy = replace(_policy(tmp_path), file_system=profile)

    rendered = render_seatbelt_profile(_request(policy, tmp_path))

    root_read = _access_rule(rendered, "file-read*", Path("/"))
    child_read = _access_rule(rendered, "file-read*", readable_child)
    assert f'(require-not (subpath "{denied_parent}"))' in root_read
    assert f'(require-not (subpath "{denied_grandchild}"))' in child_read


def test_profile_more_specific_write_reopens_readonly_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    readonly = workspace / "readonly"
    writable_child = readonly / "generated"
    denied_grandchild = writable_child / "secret"
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(readonly, FileSystemAccess.READ),
            FileSystemPermissionEntry(writable_child, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(denied_grandchild, FileSystemAccess.DENY),
        )
    )
    policy = replace(_policy(workspace), file_system=profile)

    rendered = render_seatbelt_profile(_request(policy, workspace))

    workspace_write = _access_rule(rendered, "file-write*", workspace)
    child_write = _access_rule(rendered, "file-write*", writable_child)
    assert f'(require-not (subpath "{readonly}"))' in workspace_write
    assert f'(require-not (subpath "{denied_grandchild}"))' in child_write


def test_profile_missing_readonly_path_blocks_creation_but_writable_child_reopens(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    readonly = workspace / "readonly"
    writable_child = readonly / "generated"
    readonly.mkdir(parents=True)
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(readonly, FileSystemAccess.READ),
            FileSystemPermissionEntry(writable_child, FileSystemAccess.WRITE),
        )
    )
    policy = replace(_policy(workspace), file_system=profile)

    rendered = render_seatbelt_profile(_request(policy, workspace))

    workspace_write = _access_rule(rendered, "file-write*", workspace)
    child_write = _access_rule(rendered, "file-write*", writable_child)
    assert f'(require-not (literal "{readonly}"))' in workspace_write
    assert f'(require-not (subpath "{readonly}"))' in workspace_write
    assert child_write.startswith(f'(allow file-write* (require-all (literal "{writable_child}") ')
    assert f'(require-all (subpath "{writable_child}")' in child_write


def test_profile_scoped_workspace_write_also_grants_read_without_ambient_access(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    file_system = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    policy = replace(_policy(workspace), file_system=file_system)

    rendered = render_seatbelt_profile(_request(policy, workspace))

    assert _access_rule(rendered, "file-read*", workspace)
    assert _access_rule(rendered, "file-write*", workspace)
    assert f'(subpath "{outside}")' not in rendered
    assert "(allow file-read*)\n" not in rendered


def test_profile_does_not_add_ambient_tmp_write_outside_shared_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    env_tmp = tmp_path / "ambient-tmp"
    workspace.mkdir()
    env_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(env_tmp))
    file_system = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    policy = replace(
        _policy(workspace, tmp_writable=True),
        file_system=file_system,
    )

    rendered = render_seatbelt_profile(_request(policy, workspace))

    assert f'(subpath "{Path("/tmp")}")' not in rendered
    assert f'(subpath "{env_tmp}")' not in rendered


def test_profile_adds_ambient_tmp_write_only_once_from_shared_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TMPDIR", "/tmp")
    policy = _policy(tmp_path)

    rendered = render_seatbelt_profile(_request(policy, tmp_path))

    canonical_tmp = Path("/tmp").resolve(strict=False)
    tmp_write_rules = [
        line
        for line in rendered.splitlines()
        if line.startswith("(allow file-write*")
        and f'(subpath "{canonical_tmp}")' in line
    ]
    assert len(tmp_write_rules) == 1


def test_internal_profile_process_local_tmp_is_private_readwrite_transport(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private_tmp = tmp_path / "seatbelt-private-tmp"
    workspace.mkdir()
    private_tmp.mkdir()
    file_system = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    policy = replace(_policy(workspace), file_system=file_system)

    private_transport = seatbelt_mod._SeatbeltPrivateTransport(
        read_roots=(private_tmp,),
        write_roots=(private_tmp,),
    )
    rendered = seatbelt_mod._render_seatbelt_profile(
        _request(policy, workspace),
        private_transport=private_transport,
    )

    assert _access_rule(rendered, "file-read*", private_tmp)
    assert _access_rule(rendered, "file-write*", private_tmp)


def test_profile_explicit_backend_tmp_uses_private_transport(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    private_tmp = tmp_path / "seatbelt-private-tmp"
    workspace.mkdir()
    private_tmp.mkdir()
    file_system = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    policy = replace(_policy(workspace), file_system=file_system)

    rendered = render_seatbelt_profile(
        _request(policy, workspace),
        tmp_dir=private_tmp,
    )

    assert _access_rule(rendered, "file-read*", private_tmp)
    assert _access_rule(rendered, "file-write*", private_tmp)


def test_profile_private_runtime_read_preserves_denied_descendant(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    runtime_root = tmp_path / "runtime"
    denied = runtime_root / "secret"
    workspace.mkdir()
    denied.mkdir(parents=True)
    file_system = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(denied, FileSystemAccess.DENY),
        )
    )
    policy = replace(
        _policy(workspace),
        mounts=(
            MountSpec(
                host_path=runtime_root,
                sandbox_path=runtime_root,
                mode="ro",
                required=True,
            ),
        ),
        file_system=file_system,
    )
    private_transport = seatbelt_mod._SeatbeltPrivateTransport(
        read_roots=(runtime_root,),
        write_roots=(),
    )

    rendered = seatbelt_mod._render_seatbelt_profile(
        _request(policy, workspace),
        private_transport=private_transport,
    )

    runtime_read = _access_rule(rendered, "file-read*", runtime_root)
    assert f'(require-not (subpath "{denied}"))' in runtime_read


def test_profile_private_runtime_fails_closed_below_explicit_denied_ancestor(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    denied_parent = tmp_path / "denied"
    runtime_root = denied_parent / "runtime"
    workspace.mkdir()
    runtime_root.mkdir(parents=True)
    file_system = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(denied_parent, FileSystemAccess.DENY),
        )
    )
    policy = replace(
        _policy(workspace),
        mounts=(
            MountSpec(
                host_path=runtime_root,
                sandbox_path=runtime_root,
                mode="ro",
                required=True,
            ),
        ),
        file_system=file_system,
    )
    private_transport = seatbelt_mod._SeatbeltPrivateTransport(
        read_roots=(runtime_root,),
        write_roots=(),
    )

    with pytest.raises(SandboxBackendError, match="private.*explicitly denied"):
        seatbelt_mod._render_seatbelt_profile(
            _request(policy, workspace),
            private_transport=private_transport,
        )


def test_profile_private_worker_write_preserves_read_and_deny_carveouts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = tmp_path / "worker"
    readonly = worker_root / "readonly"
    denied = worker_root / "secret"
    workspace.mkdir()
    readonly.mkdir(parents=True)
    denied.mkdir()
    file_system = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(readonly, FileSystemAccess.READ),
            FileSystemPermissionEntry(denied, FileSystemAccess.DENY),
        )
    )
    policy = replace(
        _policy(workspace),
        mounts=(
            MountSpec(
                host_path=worker_root,
                sandbox_path=worker_root,
                mode="rw",
                required=True,
            ),
        ),
        file_system=file_system,
    )
    private_transport = seatbelt_mod._SeatbeltPrivateTransport(
        read_roots=(worker_root,),
        write_roots=(worker_root,),
    )

    rendered = seatbelt_mod._render_seatbelt_profile(
        _request(policy, workspace),
        private_transport=private_transport,
    )

    worker_write = _access_rule(rendered, "file-write*", worker_root)
    assert f'(require-not (subpath "{readonly}"))' in worker_write
    assert f'(require-not (subpath "{denied}"))' in worker_write


def test_profile_rejects_non_loopback_proxy_endpoint(tmp_path: Path) -> None:
    policy = _policy(
        tmp_path,
        network=NetworkMode.PROXY_ALLOWLIST,
        network_proxy=NetworkProxySpec(host="192.0.2.10", port=18080),
    )

    with pytest.raises(SandboxBackendError, match="loopback"):
        render_seatbelt_profile(_request(policy, tmp_path))


def test_profile_keeps_workspace_ro_when_policy_ro(tmp_path: Path) -> None:
    profile = render_seatbelt_profile(_request(_policy(tmp_path, workspace_rw=False), tmp_path))

    assert _access_rule(profile, "file-read*", Path("/"))
    assert not any(
        line.startswith("(allow file-write*") and f'(subpath "{tmp_path}")' in line
        for line in profile.splitlines()
    )


def test_profile_denies_writes_to_protected_metadata_under_workspace(tmp_path: Path) -> None:
    for name in (".git", ".codex", ".agents"):
        (tmp_path / name).mkdir()

    profile = render_seatbelt_profile(_request(_policy(tmp_path), tmp_path))
    workspace_write = _access_rule(profile, "file-write*", tmp_path)

    for name in (".git", ".codex", ".agents"):
        assert name.replace(".", "\\.") in profile
        assert "(require-not (regex" in profile
        assert workspace_write.count(name.replace(".", "\\.")) == 2


def test_profile_escapes_paths(tmp_path: Path) -> None:
    hostile = tmp_path / 'quote"path'
    hostile.mkdir()
    policy = _policy(
        hostile,
        mounts=(
            MountSpec(
                host_path=hostile,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
        ),
    )

    profile = render_seatbelt_profile(_request(policy, hostile))

    assert '\\"' in profile
    assert '"quote"path"' not in profile


@pytest.mark.parametrize(
    "control",
    _DISALLOWED_PATH_CHARACTERS,
    ids=_DISALLOWED_PATH_CHARACTER_IDS,
)
def test_profile_rejects_control_characters_in_paths(
    control: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    unsafe = workspace / f"unsafe{control}path"
    workspace.mkdir()
    profile = FileSystemPermissionProfile(
        entries=(FileSystemPermissionEntry(unsafe, FileSystemAccess.WRITE),)
    )
    policy = replace(_policy(workspace), file_system=profile)

    with pytest.raises(SandboxBackendError, match="control character"):
        render_seatbelt_profile(_request(policy, workspace))


def test_profile_does_not_trust_forged_worker_action_for_private_mounts(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    forged_private_root = tmp_path / "forged-private"
    workspace.mkdir()
    forged_private_root.mkdir()
    policy = replace(
        _policy(workspace),
        mounts=(
            MountSpec(
                host_path=forged_private_root,
                sandbox_path=forged_private_root,
                mode="rw",
                required=True,
            ),
        ),
        file_system=FileSystemPermissionProfile(entries=()),
    )
    request = replace(
        _request(policy, workspace),
        action_kind="fs.worker.write_text",
    )

    rendered = render_seatbelt_profile(request)

    assert f'(literal "{forged_private_root}")' not in rendered
    assert f'(subpath "{forged_private_root}")' not in rendered


def test_missing_required_mount_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    policy = _policy(
        tmp_path,
        mounts=(
            MountSpec(
                host_path=missing,
                sandbox_path=Path("/workspace"),
                mode="ro",
                required=True,
            ),
        ),
    )

    with pytest.raises(SandboxBackendError, match="required mount missing"):
        seatbelt_mod._validate_request(_request(policy, tmp_path))


def test_missing_optional_mount_is_skipped(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    policy = _policy(
        tmp_path,
        mounts=(
            MountSpec(
                host_path=tmp_path,
                sandbox_path=Path("/workspace"),
                mode="rw",
                required=True,
            ),
            MountSpec(
                host_path=missing,
                sandbox_path=missing,
                mode="rw",
                required=False,
            ),
        ),
    )

    policy = replace(
        policy,
        file_system=FileSystemPermissionProfile.workspace(
            workspace=tmp_path,
            tmp_writable=False,
            tmpdir_env_writable=False,
        ),
    )

    profile = render_seatbelt_profile(_request(policy, tmp_path))

    assert str(missing) not in profile


@pytest.mark.parametrize(
    "control",
    _DISALLOWED_PATH_CHARACTERS,
    ids=_DISALLOWED_PATH_CHARACTER_IDS,
)
def test_request_rejects_control_characters_in_transport_mounts_before_render(
    control: str,
    tmp_path: Path,
) -> None:
    unsafe = tmp_path / f"optional{control}transport"
    policy = replace(
        _policy(tmp_path),
        mounts=(
            MountSpec(
                host_path=unsafe,
                sandbox_path=unsafe,
                mode="ro",
                required=False,
            ),
        ),
        file_system=FileSystemPermissionProfile(entries=()),
    )

    with pytest.raises(SandboxBackendError, match="control character"):
        seatbelt_mod._validate_request(_request(policy, tmp_path))


def test_build_argv_uses_sandbox_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        seatbelt_mod,
        "_sandbox_exec_binary",
        lambda binary=None: "/usr/bin/sandbox-exec",
    )

    argv = build_seatbelt_argv(
        _request(_policy(tmp_path), tmp_path),
        tmp_path / "profile.sb",
    )

    assert argv[:3] == ["/usr/bin/sandbox-exec", "-f", str(tmp_path / "profile.sb")]
    assert argv[3:] == ["sh", "-lc", "echo ok"]


def test_filesystem_worker_runtime_roots_cover_python_and_import_closure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    venv = tmp_path / "venv"
    base_prefix = tmp_path / "python"
    base_alias = tmp_path / "python-current"
    python_dir = venv / "bin"
    base_bin = base_prefix / "bin"
    stdlib = tmp_path / "python" / "lib" / "stdlib"
    purelib = venv / "site-packages"
    package = tmp_path / "checkout" / "src" / "opensquilla"
    source = package.parent
    for root in (python_dir, base_bin, stdlib, purelib, package):
        root.mkdir(parents=True, exist_ok=True)
    base_alias.symlink_to(base_prefix, target_is_directory=True)
    base_executable = base_alias / "bin" / "python"
    (base_prefix / "bin" / "python").touch()
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    executable = python_dir / "python"
    executable.touch()
    monkeypatch.setattr(seatbelt_mod, "_python_executable", lambda: executable)
    monkeypatch.setattr(seatbelt_mod.sys, "prefix", str(venv))
    monkeypatch.setattr(seatbelt_mod.sys, "base_prefix", str(base_prefix))
    monkeypatch.setattr(
        seatbelt_mod.sys,
        "_base_executable",
        str(base_executable),
        raising=False,
    )
    monkeypatch.setattr(
        seatbelt_mod,
        "sysconfig",
        SimpleNamespace(
            get_paths=lambda: {
                "stdlib": str(stdlib),
                "platstdlib": str(stdlib),
                "purelib": str(purelib),
                "platlib": "/",
            }
        ),
        raising=False,
    )
    monkeypatch.setattr(
        seatbelt_mod,
        "_opensquilla_import_roots",
        lambda: (package, source),
    )

    roots = seatbelt_mod._runtime_readonly_roots()

    assert roots == (
        python_dir.resolve(),
        venv.resolve(),
        base_alias.absolute(),
        base_prefix.resolve(),
        stdlib.resolve(),
        purelib.resolve(),
        package.resolve(),
        source.resolve(),
    )
    assert Path("/") not in roots
    assert len(roots) == len(set(roots))


def test_filesystem_worker_argv_uses_python_module_outside_frozen_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seatbelt_mod.sys, "frozen", False, raising=False)

    assert seatbelt_mod._filesystem_worker_argv() == (
        str(seatbelt_mod._python_executable()),
        "-m",
        "opensquilla.sandbox.filesystem_worker",
        "-",
    )


def test_filesystem_worker_argv_uses_internal_entrypoint_when_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(seatbelt_mod.sys, "frozen", True, raising=False)

    assert seatbelt_mod._filesystem_worker_argv() == (
        str(seatbelt_mod._python_executable()),
        "--internal-child",
        "filesystem-worker",
        "-",
    )


@pytest.mark.asyncio
async def test_public_run_never_supplies_private_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    async def fake_run_request(
        self: SeatbeltBackend,
        request: SandboxRequest,
        *,
        private_transport: object,
    ) -> SandboxResult:
        captured["private_transport"] = private_transport
        return SandboxResult(
            returncode=0,
            stdout="",
            stderr="",
            wall_time_s=0.0,
            backend_used=self.name,
        )

    monkeypatch.setattr(
        SeatbeltBackend,
        "_run_request",
        fake_run_request,
        raising=False,
    )

    result = await SeatbeltBackend().run(_request(_policy(tmp_path), tmp_path))

    assert result.returncode == 0
    assert captured["private_transport"] is None


@pytest.mark.asyncio
async def test_run_filters_env_and_returns_nonzero_without_raise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345
        returncode = 7

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return b"", b"nope"

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> FakeProcess:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]
        return FakeProcess()

    monkeypatch.setattr(seatbelt_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        seatbelt_mod,
        "_sandbox_exec_binary",
        lambda binary=None: "/usr/bin/sandbox-exec",
    )
    monkeypatch.setattr(
        seatbelt_mod,
        "create_owned_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = await SeatbeltBackend().run(_request(_policy(tmp_path), tmp_path))

    assert result.returncode == 7
    assert result.stderr == "nope"
    assert result.backend_used == "seatbelt"
    assert result.timed_out is False
    assert captured["cwd"] == str(tmp_path)
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PATH"] == "/bin"
    assert "SECRET" not in env
    assert "TMPDIR" in env
    tmpdir = Path(env["TMPDIR"])
    assert env["XDG_CACHE_HOME"] == str(tmpdir / "cache" / "xdg")
    assert env["npm_config_cache"] == str(tmpdir / "cache" / "npm")
    assert env["PIP_CACHE_DIR"] == str(tmpdir / "cache" / "pip")
    assert env["UV_CACHE_DIR"] == str(tmpdir / "cache" / "uv")


@pytest.mark.asyncio
async def test_run_injects_proxy_env_for_proxy_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345
        returncode = 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return b"ok\n", b""

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> FakeProcess:
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(seatbelt_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        seatbelt_mod,
        "_sandbox_exec_binary",
        lambda binary=None: "/usr/bin/sandbox-exec",
    )
    monkeypatch.setattr(
        seatbelt_mod,
        "create_owned_subprocess_exec",
        fake_create_subprocess_exec,
    )

    policy = _policy(
        tmp_path,
        network=NetworkMode.PROXY_ALLOWLIST,
        network_proxy=NetworkProxySpec(host="127.0.0.1", port=18080),
    )
    request = _request(policy, tmp_path)
    request.env["HTTP_PROXY"] = "http://attacker.invalid:1"

    result = await SeatbeltBackend().run(request)

    assert result.returncode == 0
    env = captured["env"]
    assert isinstance(env, dict)
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "npm_config_proxy",
        "PIP_PROXY",
    ):
        assert env[key] == "http://127.0.0.1:18080"
    assert env["NODE_USE_ENV_PROXY"] == "1"
    assert env["OPENSQUILLA_SANDBOX_NETWORK"] == "proxy_allowlist"
    assert "http://attacker.invalid:1" not in env.values()


@pytest.mark.asyncio
async def test_run_timeout_returns_timed_out_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeProcess:
        pid = 12345
        returncode = -15
        stdout = None
        stderr = None

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            await asyncio.sleep(1)
            return b"", b""

        async def wait(self) -> None:
            return None

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(seatbelt_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        seatbelt_mod,
        "_sandbox_exec_binary",
        lambda binary=None: "/usr/bin/sandbox-exec",
    )
    monkeypatch.setattr(
        seatbelt_mod,
        "create_owned_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(seatbelt_mod.os, "killpg", lambda pid, sig: None)

    result = await SeatbeltBackend().run(_request(_policy(tmp_path), tmp_path))

    assert result.timed_out is True
    assert result.returncode == -15


@pytest.mark.asyncio
async def test_run_caller_cancel_terminates_process_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = asyncio.Event()
    terminated: list[int] = []

    class FakeProcess:
        pid = 12346
        returncode = None
        stdout = None
        stderr = None

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Event().wait()

    async def fake_create_subprocess_exec(*argv: str, **kwargs: object) -> FakeProcess:
        assert "start_new_session" not in kwargs
        return FakeProcess()

    async def fake_terminate(proc: FakeProcess) -> tuple[bytes, bytes]:
        terminated.append(proc.pid)
        return b"", b""

    monkeypatch.setattr(seatbelt_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        seatbelt_mod,
        "_sandbox_exec_binary",
        lambda binary=None: "/usr/bin/sandbox-exec",
    )
    monkeypatch.setattr(
        seatbelt_mod,
        "create_owned_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setattr(seatbelt_mod, "_terminate_process_group", fake_terminate)

    running = asyncio.create_task(
        SeatbeltBackend().run(_request(_policy(tmp_path), tmp_path))
    )
    await asyncio.wait_for(started.wait(), timeout=1.0)
    running.cancel()
    cancelled = await asyncio.gather(running, return_exceptions=True)
    assert isinstance(cancelled[0], asyncio.CancelledError)

    assert terminated == [12346]


@pytest.mark.asyncio
async def test_real_seatbelt_runs_python_when_available(tmp_path: Path) -> None:
    if not SeatbeltBackend().available():
        pytest.skip("requires macOS sandbox-exec")
    policy = _policy(tmp_path)
    request = SandboxRequest(
        argv=(sys.executable, "-c", "print('ok')"),
        cwd=tmp_path,
        action_kind="code.exec",
        policy=policy,
        env={"PATH": "/bin:/usr/bin"},
    )

    result = await SeatbeltBackend().run(request)

    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert result.stderr == ""


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
async def test_real_seatbelt_scoped_filesystem_worker_runtime_closure(
    tmp_path: Path,
) -> None:
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("requires macOS sandbox-exec")
    workspace = tmp_path / "workspace"
    target = workspace / "notes.txt"
    workspace.mkdir()
    target.write_text("hello\n", encoding="utf-8")
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )

    result = await backend.run_operation(
        SandboxOperation.filesystem(
            kind="read_file",
            workspace=workspace,
            run_mode="trusted",
            path=target,
            file_system_profile=profile,
        )
    )

    assert "hello" in result.message


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
async def test_real_seatbelt_frozen_filesystem_worker_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    binary = os.environ.get("OPENSQUILLA_PACKAGED_GATEWAY_BINARY", "")
    if not binary:
        pytest.skip("requires OPENSQUILLA_PACKAGED_GATEWAY_BINARY")
    packaged_gateway = Path(binary).resolve()
    if not packaged_gateway.is_file():
        pytest.skip("packaged gateway binary is unavailable")
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("requires macOS sandbox-exec")

    workspace = tmp_path / "workspace"
    target = workspace / "notes.txt"
    workspace.mkdir()
    target.write_text("frozen-worker-ok\n", encoding="utf-8")
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    monkeypatch.setattr(seatbelt_mod.sys, "executable", str(packaged_gateway))
    monkeypatch.setattr(seatbelt_mod.sys, "frozen", True, raising=False)

    result = await backend.run_operation(
        SandboxOperation.filesystem(
            kind="read_file",
            workspace=workspace,
            run_mode="trusted",
            path=target,
            file_system_profile=profile,
        )
    )

    assert result.message == "1\tfrozen-worker-ok\n"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
async def test_real_seatbelt_exact_carveouts_and_missing_writable_reopen(
    tmp_path: Path,
) -> None:
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("requires macOS sandbox-exec")
    workspace = tmp_path / "workspace"
    readonly = workspace / "readonly"
    writable_child = readonly / "generated"
    denied_file = workspace / "denied.txt"
    missing_readonly = workspace / "missing-readonly"
    missing_denied = workspace / "missing-denied"
    readonly.mkdir(parents=True)
    denied_file.write_text("secret", encoding="utf-8")
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(Path("/"), FileSystemAccess.READ),
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(readonly, FileSystemAccess.READ),
            FileSystemPermissionEntry(writable_child, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(denied_file, FileSystemAccess.DENY),
            FileSystemPermissionEntry(missing_readonly, FileSystemAccess.READ),
            FileSystemPermissionEntry(missing_denied, FileSystemAccess.DENY),
        )
    )
    policy = replace(_policy(workspace), file_system=profile)
    script = """
import sys
from pathlib import Path

writable_child, readonly, denied_file, missing_readonly, missing_denied = map(
    Path, sys.argv[1:]
)
writable_child.mkdir()
(writable_child / "ok.txt").write_text("ok", encoding="utf-8")
for action in (
    lambda: (readonly / "blocked.txt").write_text("blocked", encoding="utf-8"),
    lambda: denied_file.read_text(encoding="utf-8"),
    missing_readonly.mkdir,
    missing_denied.mkdir,
):
    try:
        action()
    except PermissionError:
        pass
    else:
        raise SystemExit("Seatbelt exact carveout unexpectedly allowed access")
print("ok")
"""
    request = SandboxRequest(
        argv=(
            sys.executable,
            "-c",
            script,
            str(writable_child),
            str(readonly),
            str(denied_file),
            str(missing_readonly),
            str(missing_denied),
        ),
        cwd=workspace,
        action_kind="code.exec",
        policy=policy,
        env={"PATH": "/bin:/usr/bin"},
    )

    result = await backend.run(request)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    assert (writable_child / "ok.txt").read_text(encoding="utf-8") == "ok"
    assert not (readonly / "blocked.txt").exists()
    assert not missing_readonly.exists()
    assert not missing_denied.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
async def test_real_seatbelt_blocks_retargeted_metadata_symlink_mutations(
    tmp_path: Path,
) -> None:
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("requires macOS sandbox-exec")
    workspace = tmp_path / "workspace"
    original_target = workspace / "original-target"
    current_target = workspace / "current-target"
    metadata = workspace / ".git"
    replacement = workspace / "replacement"
    ordinary = workspace / "ordinary.txt"
    workspace.mkdir()
    original_target.mkdir()
    current_target.mkdir()
    replacement.write_text("replacement", encoding="utf-8")
    _directory_symlink(metadata, original_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    metadata.unlink()
    _directory_symlink(metadata, current_target)
    policy = replace(_policy(workspace), file_system=profile)
    script = """
import os
import sys
from pathlib import Path

workspace, metadata, replacement = map(Path, sys.argv[1:])
(workspace / "ordinary.txt").write_text("ok", encoding="utf-8")
actions = (
    lambda: (metadata / "config").write_text("blocked", encoding="utf-8"),
    metadata.unlink,
    lambda: metadata.rename(workspace / ".git-renamed"),
    lambda: os.replace(replacement, metadata),
)
for action in actions:
    try:
        action()
    except PermissionError:
        pass
    else:
        raise SystemExit("Seatbelt unexpectedly allowed protected metadata mutation")
print("ok")
"""
    request = SandboxRequest(
        argv=(
            sys.executable,
            "-c",
            script,
            str(workspace),
            str(metadata),
            str(replacement),
        ),
        cwd=workspace,
        action_kind="code.exec",
        policy=policy,
        env={"PATH": "/bin:/usr/bin"},
    )

    result = await backend.run(request)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok\n"
    assert metadata.is_symlink()
    assert metadata.resolve() == current_target
    assert not (current_target / "config").exists()
    assert replacement.read_text(encoding="utf-8") == "replacement"
    assert ordinary.read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_real_seatbelt_shell_can_write_slash_tmp_when_available(
    tmp_path: Path,
) -> None:
    if not SeatbeltBackend().available():
        pytest.skip("requires macOS sandbox-exec")
    target = Path("/tmp") / f"opensquilla_sandbox_shell_probe_{os.getpid()}.txt"
    policy = _policy(tmp_path)
    request = SandboxRequest(
        argv=(
            "sh",
            "-c",
            f"printf '%s\\n' shell-temp-ok > {target} && cat {target} && rm {target}",
        ),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        env={"PATH": "/bin:/usr/bin"},
    )

    try:
        result = await SeatbeltBackend().run(request)
    finally:
        target.unlink(missing_ok=True)

    assert result.returncode == 0
    assert result.stdout == "shell-temp-ok\n"
    assert result.stderr == ""
    assert result.backend_notes == ()


@pytest.mark.asyncio
async def test_real_seatbelt_blocks_write_outside_workspace_when_available(
    tmp_path: Path,
) -> None:
    if not SeatbeltBackend().available():
        pytest.skip("requires macOS sandbox-exec")
    outside = Path.home() / f"opensquilla-seatbelt-outside-{os.getpid()}.txt"
    policy = _policy(tmp_path)
    request = SandboxRequest(
        argv=(
            sys.executable,
            "-c",
            f"open({str(outside)!r}, 'w').write('blocked')",
        ),
        cwd=tmp_path,
        action_kind="code.exec",
        policy=policy,
        env={"PATH": "/bin:/usr/bin"},
    )

    result = await SeatbeltBackend().run(request)

    assert result.returncode != 0
    assert "PermissionError" in result.stderr
    assert not outside.exists()


@pytest.mark.asyncio
async def test_real_seatbelt_proxy_allowlist_allows_loopback_proxy_port_when_available(
    tmp_path: Path,
) -> None:
    if not SeatbeltBackend().available():
        pytest.skip("requires macOS sandbox-exec")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _ = await reader.read(16)
        writer.write(b"ok\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        sock = server.sockets[0]
        host, port = sock.getsockname()[:2]
        policy = _policy(
            tmp_path,
            network=NetworkMode.PROXY_ALLOWLIST,
            network_proxy=NetworkProxySpec(host=str(host), port=int(port)),
        )
        code = (
            "import socket\n"
            f"s = socket.create_connection(('127.0.0.1', {int(port)}), timeout=2)\n"
            "s.sendall(b'hi')\n"
            "print(s.recv(16).decode(), end='')\n"
            "s.close()\n"
        )
        request = SandboxRequest(
            argv=(sys.executable, "-c", code),
            cwd=tmp_path,
            action_kind="network.http",
            policy=policy,
            env={"PATH": "/bin:/usr/bin"},
        )

        result = await SeatbeltBackend().run(request)
    finally:
        server.close()
        await server.wait_closed()

    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_real_seatbelt_blocks_loopback_tcp_when_network_none(
    tmp_path: Path,
) -> None:
    if not SeatbeltBackend().available():
        pytest.skip("requires macOS sandbox-exec")

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        _ = await reader.read(16)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        sock = server.sockets[0]
        _host, port = sock.getsockname()[:2]
        policy = _policy(tmp_path, network=NetworkMode.NONE, network_proxy=None)
        code = (
            "import socket\n"
            f"socket.create_connection(('127.0.0.1', {int(port)}), timeout=2)\n"
            "print('connected')\n"
        )
        request = SandboxRequest(
            argv=(sys.executable, "-c", code),
            cwd=tmp_path,
            action_kind="network.http",
            policy=policy,
            env={"PATH": "/bin:/usr/bin"},
        )

        result = await SeatbeltBackend().run(request)
    finally:
        server.close()
        await server.wait_closed()

    assert result.returncode != 0
    assert "connected" not in result.stdout
    assert "PermissionError" in result.stderr or "Operation not permitted" in result.stderr


# ─── _classify_denial tests ───────────────────────────────────────────────


def test_classify_denial_execvp_blocked() -> None:
    stderr = "sandbox-exec: execvp() of '/opt/homebrew/bin/uv' failed: Operation not permitted"
    notes = _classify_denial(("sh",), stderr)
    assert len(notes) == 1
    assert notes[0].category == "execve.denied"
    assert "/opt/homebrew/bin/uv" in notes[0].hint


def test_classify_denial_filesystem_read_blocked() -> None:
    stderr = "/etc/ssl/cert.pem: Operation not permitted"
    notes = _classify_denial(("python",), stderr)
    assert len(notes) == 1
    assert notes[0].category == "filesystem.read"
    assert "/etc/ssl/cert.pem" in notes[0].hint


def test_classify_denial_ping_sendto_blocked() -> None:
    stderr = "ping: sendto: Operation not permitted\n"

    notes = _classify_denial(("sh", "-lc", "/sbin/ping -c 1 1.1.1.1"), stderr)

    assert len(notes) == 1
    assert notes[0].category == "network.denied"
    assert "ICMP" in notes[0].hint


def test_classify_denial_ping_packet_loss_under_restricted_network() -> None:
    stdout = (
        "PING 1.1.1.1 (1.1.1.1): 56 data bytes\n\n"
        "--- 1.1.1.1 ping statistics ---\n"
        "1 packets transmitted, 0 packets received, 100.0% packet loss\n"
    )

    notes = _classify_denial(
        ("sh", "-lc", "ping -c 1 -W 3000 1.1.1.1"),
        "",
        stdout=stdout,
        network=NetworkMode.PROXY_ALLOWLIST,
    )

    assert len(notes) == 1
    assert notes[0].category == "network.denied"
    assert "ICMP" in notes[0].hint


def test_classify_denial_ping_packet_loss_ignored_on_host_network() -> None:
    stdout = "1 packets transmitted, 0 packets received, 100.0% packet loss\n"

    notes = _classify_denial(
        ("sh", "-lc", "ping -c 1 1.1.1.1"),
        "",
        stdout=stdout,
        network=NetworkMode.HOST,
    )

    assert notes == ()


def test_classify_denial_dyld_library_not_loaded() -> None:
    stderr = "dyld[123]: Library not loaded: /opt/homebrew/opt/openssl/lib/libssl.dylib"
    notes = _classify_denial(("python",), stderr)
    assert len(notes) == 1
    assert notes[0].category == "filesystem.read"
    assert "libssl.dylib" in notes[0].hint


def test_classify_denial_empty_stderr_returns_empty() -> None:
    assert _classify_denial(("sh",), "") == ()


def test_classify_denial_unrelated_stderr_returns_empty() -> None:
    assert _classify_denial(("sh",), "syntax error near unexpected token") == ()


def test_classify_denial_deduplicates_same_path() -> None:
    stderr = (
        "/etc/ssl/cert.pem: Operation not permitted\n/etc/ssl/cert.pem: Operation not permitted\n"
    )
    notes = _classify_denial(("python",), stderr)
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_run_populates_backend_notes_on_denial(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    denial_stderr = (
        "sandbox-exec: execvp() of '/opt/homebrew/bin/uv' failed: Operation not permitted"
    )

    class FakeProcess:
        pid = 12345
        returncode = 1

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return b"", denial_stderr.encode()

    async def fake_create(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(seatbelt_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        seatbelt_mod, "_sandbox_exec_binary", lambda binary=None: "/usr/bin/sandbox-exec"
    )
    monkeypatch.setattr(seatbelt_mod, "create_owned_subprocess_exec", fake_create)

    result = await SeatbeltBackend().run(_request(_policy(tmp_path), tmp_path))

    assert len(result.backend_notes) == 1
    assert result.backend_notes[0].startswith("execve.denied:")


@pytest.mark.asyncio
async def test_run_populates_backend_notes_for_zero_exit_ping_packet_loss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ping_stdout = (
        "PING 1.1.1.1 (1.1.1.1): 56 data bytes\n\n"
        "--- 1.1.1.1 ping statistics ---\n"
        "1 packets transmitted, 0 packets received, 100.0% packet loss\n"
    )

    class FakeProcess:
        pid = 12345
        returncode = 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return ping_stdout.encode(), b""

    async def fake_create(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(seatbelt_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        seatbelt_mod, "_sandbox_exec_binary", lambda binary=None: "/usr/bin/sandbox-exec"
    )
    monkeypatch.setattr(seatbelt_mod, "create_owned_subprocess_exec", fake_create)
    policy = _policy(
        tmp_path,
        network=NetworkMode.PROXY_ALLOWLIST,
        network_proxy=NetworkProxySpec(host="127.0.0.1", port=18080),
    )
    request = SandboxRequest(
        argv=("sh", "-lc", "ping -c 1 -W 3000 1.1.1.1"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
    )

    result = await SeatbeltBackend().run(request)

    assert result.returncode == 0
    assert len(result.backend_notes) == 1
    assert result.backend_notes[0].startswith("network.denied:")


@pytest.mark.asyncio
async def test_run_backend_notes_empty_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeProcess:
        pid = 12345
        returncode = 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return b"ok", b""

    async def fake_create(*args: object, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(seatbelt_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        seatbelt_mod, "_sandbox_exec_binary", lambda binary=None: "/usr/bin/sandbox-exec"
    )
    monkeypatch.setattr(seatbelt_mod, "create_owned_subprocess_exec", fake_create)

    result = await SeatbeltBackend().run(_request(_policy(tmp_path), tmp_path))

    assert result.backend_notes == ()


@pytest.mark.asyncio
async def test_run_operation_delegates_filesystem_to_seatbelt_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    profile = FileSystemPermissionProfile.workspace(workspace=workspace)
    captured: dict[str, object] = {}

    async def fake_run_request(
        self: SeatbeltBackend,
        request: SandboxRequest,
        *,
        private_transport: object,
    ) -> object:
        captured["request"] = request
        captured["private_transport"] = private_transport
        captured["payload"] = json.loads((request.stdin or b"").decode("utf-8"))
        return SandboxResult(
            returncode=0,
            stdout=json.dumps({"message": f"Written 5 bytes to {target}", "created": True}),
            stderr="",
            wall_time_s=0.0,
            backend_used=self.name,
        )

    monkeypatch.setattr(
        SeatbeltBackend,
        "_run_request",
        fake_run_request,
        raising=False,
    )

    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        paths=(target,),
        content="hello",
        file_system_profile=profile,
    )
    result = await SeatbeltBackend().run_operation(operation)

    assert result == SandboxOperationResult(
        message=f"Written 5 bytes to {target}",
        created=True,
    )
    request = captured["request"]
    assert isinstance(request, SandboxRequest)
    assert request.action_kind == "fs.worker.write_text"
    assert request.cwd == workspace.resolve()
    assert request.argv == (
        str(seatbelt_mod._python_executable()),
        "-m",
        "opensquilla.sandbox.filesystem_worker",
        "-",
    )
    assert request.stdin == json.dumps(operation.to_payload(), ensure_ascii=False).encode("utf-8")
    assert request.policy.network == NetworkMode.NONE
    assert request.policy.tmp_writable is False
    assert request.policy.file_system is profile
    assert {"HOME", "TMP", "TEMP", "TMPDIR"}.isdisjoint(request.policy.env_allowlist)
    assert target not in {mount.host_path for mount in request.policy.mounts}
    runtime_roots = set(seatbelt_mod._runtime_readonly_roots())
    assert {
        mount.host_path for mount in request.policy.mounts if mount.mode == "ro"
    } == runtime_roots
    assert {mount.host_path for mount in request.policy.mounts if mount.mode == "rw"} == set()
    assert set(request.env) == {"PATH", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
    assert request.env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert {"HOME", "TMP", "TEMP", "TMPDIR"}.isdisjoint(request.env)
    private_transport = captured["private_transport"]
    assert private_transport is not None
    assert set(getattr(private_transport, "read_roots")) == runtime_roots
    assert getattr(private_transport, "write_roots") == ()
    assert captured["payload"] == {
        "domain": "filesystem",
        "kind": "write_text",
        "workspace": str(workspace),
        "runMode": "trusted",
        "toolName": "filesystem",
        "operationId": "",
        "summary": "",
        "permissions": {
            "filesystem": {},
            "network": {},
            "process": {},
            "artifact": {},
            "media": {},
        },
        "approval": {
            "required": False,
            "reason": "",
            "namespace": "sandbox",
            "payload": {},
        },
        "request": {
            "path": str(target),
            "paths": [str(target)],
            "displayPath": "",
            "content": "hello",
            "oldText": "",
            "newText": "",
            "patch": "",
            "root": None,
            "offset": None,
            "limit": None,
            "pattern": "",
            "include": None,
            "maxResults": None,
        },
        "path": str(target),
        "paths": [str(target)],
        "displayPath": "",
        "content": "hello",
        "oldText": "",
        "newText": "",
        "patch": "",
        "root": None,
        "offset": None,
        "limit": None,
        "pattern": "",
        "include": None,
        "maxResults": None,
    }
    assert not (workspace / ".opensquilla-cache").exists()


def test_filesystem_worker_transport_validator_binds_launch_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    runtime_root = tmp_path / "runtime"
    workspace.mkdir()
    runtime_root.mkdir()
    target = workspace / "notes.txt"
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=FileSystemPermissionProfile.workspace(workspace=workspace),
    )
    runtime_roots = (runtime_root.resolve(),)
    monkeypatch.setattr(seatbelt_mod, "_runtime_readonly_roots", lambda: runtime_roots)
    request = seatbelt_mod._filesystem_operation_request(operation)
    writable_mount = MountSpec(
        host_path=workspace.resolve(),
        sandbox_path=workspace.resolve(),
        mode="rw",
    )
    tampered_requests = (
        (replace(request, argv=(*request.argv[:-1], "payload.json")), "argv"),
        (replace(request, cwd=tmp_path.resolve()), "cwd"),
        (replace(request, action_kind="fs.worker.read_file"), "action kind"),
        (replace(request, stdin=b"[]"), "stdin must be a JSON object"),
        (replace(request, stdin=b"{}"), "stdin is inconsistent"),
        (
            replace(request, env={**request.env, "TMPDIR": "/tmp"}),
            "environment",
        ),
        (
            replace(
                request,
                policy=replace(
                    request.policy,
                    env_allowlist=(*request.policy.env_allowlist, "TMPDIR"),
                ),
            ),
            "environment",
        ),
        (
            replace(request, policy=replace(request.policy, tmp_writable=True)),
            "must not create backend tmp",
        ),
        (
            replace(request, policy=replace(request.policy, mounts=())),
            "transport roots",
        ),
        (
            replace(
                request,
                policy=replace(
                    request.policy,
                    mounts=(*request.policy.mounts, writable_mount),
                ),
            ),
            "transport roots",
        ),
    )

    for tampered, message in tampered_requests:
        with pytest.raises(SandboxBackendError, match=message):
            seatbelt_mod._filesystem_worker_private_transport(
                operation,
                tampered,
                workspace.resolve(),
                runtime_roots,
            )

    with pytest.raises(SandboxBackendError, match="unsupported filesystem operation"):
        seatbelt_mod._filesystem_worker_private_transport(
            replace(operation, kind="delete_file"),
            request,
            workspace.resolve(),
            runtime_roots,
        )


@pytest.mark.asyncio
async def test_run_operation_does_not_touch_inserted_cache_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()
    cache_link = workspace / ".opensquilla-cache"
    try:
        probe = tmp_path / "symlink-probe"
        probe.symlink_to(replacement, target_is_directory=True)
        probe.unlink()
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    target = workspace / "notes.txt"
    profile = FileSystemPermissionProfile.workspace(workspace=workspace)
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=profile,
    )
    captured: dict[str, object] = {}
    real_launch = seatbelt_mod._filesystem_operation_launch

    def launch_then_insert_cache_symlink(
        launched_operation: SandboxOperation,
        *args: object,
    ) -> object:
        launch = real_launch(launched_operation, *args)
        cache_link.symlink_to(replacement, target_is_directory=True)
        return launch

    async def fake_run_request(
        self: SeatbeltBackend,
        request: SandboxRequest,
        *,
        private_transport: object,
    ) -> SandboxResult:
        captured["request"] = request
        captured["private_transport"] = private_transport
        return SandboxResult(
            returncode=0,
            stdout=json.dumps({"message": "Written 5 bytes", "created": True}),
            stderr="",
            wall_time_s=0.0,
            backend_used=self.name,
        )

    monkeypatch.setattr(
        seatbelt_mod,
        "_filesystem_operation_launch",
        launch_then_insert_cache_symlink,
    )
    monkeypatch.setattr(SeatbeltBackend, "_run_request", fake_run_request)

    await SeatbeltBackend().run_operation(operation)

    request = captured["request"]
    assert isinstance(request, SandboxRequest)
    assert request.argv[-1] == "-"
    assert request.stdin == json.dumps(operation.to_payload(), ensure_ascii=False).encode("utf-8")
    assert getattr(captured["private_transport"], "write_roots") == ()
    assert cache_link.is_symlink()
    assert list(replacement.iterdir()) == []


@pytest.mark.asyncio
async def test_run_operation_fails_closed_without_resolved_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"

    with pytest.raises(
        ValueError,
        match="^filesystem operation is missing resolved filesystem profile$",
    ):
        await SeatbeltBackend().run_operation(
            SandboxOperation.filesystem(
                kind="write_text",
                workspace=workspace,
                run_mode="trusted",
                path=target,
                content="hello",
            )
        )
    assert not worker_root.exists()


def test_filesystem_operation_request_construction_has_no_host_side_effects(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    target = workspace / "notes.txt"
    workspace.mkdir()
    profile = FileSystemPermissionProfile.workspace(workspace=workspace)
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=profile,
    )

    request = seatbelt_mod._filesystem_operation_request(operation)

    assert request.policy.file_system is profile
    assert request.cwd == workspace.resolve()
    assert request.stdin is not None
    assert not {mount.host_path for mount in request.policy.mounts if mount.mode == "rw"}
    assert not worker_root.exists()


@pytest.mark.asyncio
async def test_run_operation_preflights_denied_globs_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    target = workspace / "notes.txt"
    workspace.mkdir()
    target.write_text("hello", encoding="utf-8")
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        denied_read_globs=(str(workspace / "**" / "*.pem"),),
    )
    operation = SandboxOperation.filesystem(
        kind="read_file",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        file_system_profile=profile,
    )
    monkeypatch.setattr(SeatbeltBackend, "available", lambda self: True)

    with pytest.raises(SandboxBackendError, match="denied read glob"):
        await SeatbeltBackend().run_operation(operation)
    assert not worker_root.exists()


@pytest.mark.asyncio
async def test_run_operation_preflights_unrelated_invalid_profile_path_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    target = workspace / "notes.txt"
    unsafe = tmp_path / "unrelated\x01path"
    workspace.mkdir()
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(unsafe, FileSystemAccess.READ),
        )
    )
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=profile,
    )
    monkeypatch.setattr(SeatbeltBackend, "available", lambda self: True)

    with pytest.raises(SandboxBackendError, match="control character"):
        await SeatbeltBackend().run_operation(operation)
    assert not worker_root.exists()


@pytest.mark.parametrize(
    "control",
    _DISALLOWED_PATH_CHARACTERS,
    ids=_DISALLOWED_PATH_CHARACTER_IDS,
)
def test_filesystem_preflight_rejects_every_control_character_in_profile_paths(
    control: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    target = workspace / "notes.txt"
    unsafe = tmp_path / f"unrelated{control}path"
    workspace.mkdir()
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(unsafe, FileSystemAccess.READ),
        )
    )
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=profile,
    )

    with pytest.raises(SandboxBackendError, match="control character"):
        seatbelt_mod._filesystem_operation_request(operation)
    assert not worker_root.exists()


@pytest.mark.parametrize(
    "control",
    _DISALLOWED_PATH_CHARACTERS,
    ids=_DISALLOWED_PATH_CHARACTER_IDS,
)
def test_filesystem_operation_rejects_control_characters_in_targets_before_side_effects(
    control: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    target = workspace / f"unsafe{control}path"
    workspace.mkdir()
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=FileSystemPermissionProfile.workspace(workspace=workspace),
    )

    with pytest.raises(SandboxBackendError, match="control character"):
        seatbelt_mod._filesystem_operation_request(operation)
    assert not worker_root.exists()


def test_filesystem_operation_preflights_logical_target_before_canonicalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    original_target = workspace / "original-target"
    current_target = workspace / "current-target"
    metadata = workspace / ".git"
    workspace.mkdir()
    original_target.mkdir()
    current_target.mkdir()
    _directory_symlink(metadata, original_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    metadata.unlink()
    _directory_symlink(metadata, current_target)
    logical_target = metadata / "config"
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=logical_target,
        content="blocked",
        file_system_profile=profile,
    )
    real_canonicalize = seatbelt_mod._canonical_filesystem_target

    def reject_target_canonicalization(path: Path, *, kind: str) -> Path:
        if kind == "filesystem target":
            raise AssertionError("canonicalized before logical profile preflight")
        return real_canonicalize(path, kind=kind)

    monkeypatch.setattr(
        seatbelt_mod,
        "_canonical_filesystem_target",
        reject_target_canonicalization,
    )

    with pytest.raises(
        SandboxBackendError,
        match=rf"requires write access.*{re.escape(str(logical_target))}",
    ):
        seatbelt_mod._filesystem_operation_request(operation)


def test_filesystem_operation_relative_target_uses_workspace_for_logical_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside_cwd = tmp_path / "outside-cwd"
    metadata_target = workspace / "metadata-target"
    metadata = workspace / ".git"
    workspace.mkdir()
    outside_cwd.mkdir()
    metadata_target.mkdir()
    _directory_symlink(metadata, metadata_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    monkeypatch.chdir(outside_cwd)
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=Path(".git/config"),
        content="blocked",
        file_system_profile=profile,
    )

    with pytest.raises(
        SandboxBackendError,
        match=rf"requires write access.*{re.escape(str(metadata / 'config'))}",
    ):
        seatbelt_mod._filesystem_operation_request(operation)


def test_apply_patch_preflights_each_logical_path_before_resolving_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import patch as patch_tool

    workspace = tmp_path / "workspace"
    original_target = workspace / "original-target"
    current_target = workspace / "current-target"
    metadata = workspace / ".git"
    workspace.mkdir()
    original_target.mkdir()
    current_target.mkdir()
    _directory_symlink(metadata, original_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    metadata.unlink()
    _directory_symlink(metadata, current_target)
    patch = """*** Begin Patch
*** Add File: ordinary.txt
+ordinary
*** Add File: .git/config
+blocked
*** End Patch"""
    declared = (workspace / "ordinary.txt", current_target / "config")
    operation = SandboxOperation.filesystem(
        kind="apply_patch",
        workspace=workspace,
        run_mode="trusted",
        paths=declared,
        patch=patch,
        root=workspace,
        file_system_profile=profile,
    )
    real_validate_path = patch_tool._validate_path
    validated: list[str] = []

    def record_validate_path(path: str, root: Path) -> Path:
        validated.append(path)
        return real_validate_path(path, root)

    monkeypatch.setattr(patch_tool, "_validate_path", record_validate_path)

    with pytest.raises(
        SandboxBackendError,
        match=rf"requires write access.*{re.escape(str(metadata / 'config'))}",
    ):
        seatbelt_mod._filesystem_operation_request(operation)
    assert validated == ["ordinary.txt"]


def test_seatbelt_source_target_maps_execution_workspace_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "new.py"
    operation = SandboxOperation.filesystem(
        kind="create_source",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        logical_path=Path("/workspace/new.py"),
        paths=(target,),
        content="value = 1\n",
        file_system_profile=FileSystemPermissionProfile.workspace(workspace=workspace),
    )
    checked: list[tuple[Path, ...]] = []
    monkeypatch.setattr(
        seatbelt_mod,
        "_validate_filesystem_operation_profile_targets",
        lambda _operation, targets: checked.append(targets),
    )

    assert seatbelt_mod._filesystem_operation_targets(operation, operation.request) == (target,)
    assert checked == [(target,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_kind", ("missing", "extra", "different"))
async def test_run_operation_rejects_inconsistent_declared_paths_without_side_effects(
    declared_kind: str,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    actual = workspace / "actual.txt"
    other = workspace / "other.txt"
    workspace.mkdir()
    actual.write_text("actual", encoding="utf-8")
    other.write_text("other", encoding="utf-8")
    operation = SandboxOperation.filesystem(
        kind="read_file",
        workspace=workspace,
        run_mode="trusted",
        path=actual,
        file_system_profile=FileSystemPermissionProfile.read_only(),
    )
    declared_paths = {
        "missing": (),
        "extra": (actual, other),
        "different": (other,),
    }[declared_kind]
    operation = replace(
        operation,
        request=replace(operation.request, paths=declared_paths),
    )

    with pytest.raises(SandboxBackendError, match="declared filesystem paths"):
        await SeatbeltBackend().run_operation(operation)
    assert not worker_root.exists()


@pytest.mark.asyncio
async def test_run_operation_rejects_missing_path_without_side_effects(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    workspace.mkdir()
    operation = SandboxOperation.filesystem(
        kind="read_file",
        workspace=workspace,
        run_mode="trusted",
        file_system_profile=FileSystemPermissionProfile.read_only(),
    )

    with pytest.raises(SandboxBackendError, match="read_file.*requires path"):
        await SeatbeltBackend().run_operation(operation)
    assert not worker_root.exists()


@pytest.mark.asyncio
async def test_run_operation_derives_apply_patch_targets_from_patch_text(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    actual = workspace / "created.txt"
    falsely_declared = workspace / "other.txt"
    workspace.mkdir()
    patch = """*** Begin Patch
*** Add File: created.txt
+hello
*** End Patch"""
    operation = SandboxOperation.filesystem(
        kind="apply_patch",
        workspace=workspace,
        run_mode="trusted",
        paths=(falsely_declared,),
        patch=patch,
        root=workspace,
        file_system_profile=FileSystemPermissionProfile.workspace(workspace=workspace),
    )

    with pytest.raises(SandboxBackendError, match="declared filesystem paths"):
        await SeatbeltBackend().run_operation(operation)
    assert not actual.exists()
    assert not worker_root.exists()


@pytest.mark.asyncio
async def test_run_operation_rejects_apply_patch_without_root_before_side_effects(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    workspace.mkdir()
    patch = """*** Begin Patch
*** Add File: created.txt
+hello
*** End Patch"""
    operation = SandboxOperation.filesystem(
        kind="apply_patch",
        workspace=workspace,
        run_mode="trusted",
        patch=patch,
        file_system_profile=FileSystemPermissionProfile.workspace(workspace=workspace),
    )

    with pytest.raises(SandboxBackendError, match="apply_patch.*requires root"):
        await SeatbeltBackend().run_operation(operation)
    assert not worker_root.exists()


def test_filesystem_operation_ignores_denied_legacy_cache_without_transport(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    target = workspace / "notes.txt"
    workspace.mkdir()
    target.write_text("hello", encoding="utf-8")
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(Path("/"), FileSystemAccess.READ),
            FileSystemPermissionEntry(worker_root, FileSystemAccess.DENY),
        )
    )
    operation = SandboxOperation.filesystem(
        kind="read_file",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        file_system_profile=profile,
    )

    request = seatbelt_mod._filesystem_operation_request(operation)

    assert worker_root not in {mount.host_path for mount in request.policy.mounts}
    assert not worker_root.exists()


def test_filesystem_operation_ignores_readonly_legacy_cache_without_transport(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    target = workspace / "notes.txt"
    workspace.mkdir()
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(worker_root, FileSystemAccess.READ),
        )
    )
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=profile,
    )
    request = seatbelt_mod._filesystem_operation_request(operation)

    assert worker_root not in {mount.host_path for mount in request.policy.mounts}
    assert not worker_root.exists()


def test_filesystem_operation_validates_missing_read_target(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = workspace / "missing.txt"
    operation = SandboxOperation.filesystem(
        kind="read_file",
        workspace=workspace,
        run_mode="trusted",
        path=missing,
        file_system_profile=FileSystemPermissionProfile.workspace(workspace=workspace),
    )

    with pytest.raises(FileNotFoundError, match="File not found"):
        seatbelt_mod._filesystem_operation_request(operation)


def test_filesystem_operation_validates_list_target_type(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    operation = SandboxOperation.filesystem(
        kind="list_dir",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        file_system_profile=FileSystemPermissionProfile.workspace(workspace=workspace),
    )

    with pytest.raises(NotADirectoryError, match="Not a directory"):
        seatbelt_mod._filesystem_operation_request(operation)


def test_filesystem_operation_rejects_denied_read_target_in_private_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    runtime_root = tmp_path / "runtime"
    target = runtime_root / "secret.txt"
    workspace.mkdir()
    runtime_root.mkdir()
    target.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(
        seatbelt_mod,
        "_runtime_readonly_roots",
        lambda: (runtime_root,),
    )
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(Path("/"), FileSystemAccess.READ),
            FileSystemPermissionEntry(target, FileSystemAccess.DENY),
        )
    )
    operation = SandboxOperation.filesystem(
        kind="read_file",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        file_system_profile=profile,
    )

    with pytest.raises(SandboxBackendError, match="profile denies read access"):
        seatbelt_mod._filesystem_operation_request(operation)
    assert not worker_root.exists()


def test_filesystem_operation_rejects_write_target_in_readonly_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    runtime_root = tmp_path / "runtime"
    target = runtime_root / "generated.txt"
    workspace.mkdir()
    runtime_root.mkdir()
    monkeypatch.setattr(
        seatbelt_mod,
        "_runtime_readonly_roots",
        lambda: (runtime_root,),
    )
    profile = FileSystemPermissionProfile(
        entries=(FileSystemPermissionEntry(runtime_root, FileSystemAccess.WRITE),)
    )
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=profile,
    )

    with pytest.raises(SandboxBackendError, match="read-only runtime"):
        seatbelt_mod._filesystem_operation_request(operation)
    assert not worker_root.exists()


def test_filesystem_operation_rejects_readonly_write_target_in_legacy_cache(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    target = worker_root / "notes.txt"
    workspace.mkdir()
    profile = FileSystemPermissionProfile.read_only()
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=profile,
    )

    with pytest.raises(SandboxBackendError, match="profile requires write access"):
        seatbelt_mod._filesystem_operation_request(operation)
    assert not worker_root.exists()


def test_filesystem_operation_allows_profile_write_target_in_legacy_cache(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    worker_root = workspace / ".opensquilla-cache" / "fs-worker"
    target = worker_root / "notes.txt"
    workspace.mkdir()
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(Path("/"), FileSystemAccess.READ),
            FileSystemPermissionEntry(worker_root, FileSystemAccess.WRITE),
        )
    )
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=profile,
    )

    request = seatbelt_mod._filesystem_operation_request(operation)

    assert request.policy.file_system is profile


def test_filesystem_operation_allows_etc_with_root_read_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = FileSystemPermissionProfile.read_only()
    operation = SandboxOperation.filesystem(
        kind="list_dir",
        workspace=workspace,
        run_mode="trusted",
        path=Path("/etc"),
        file_system_profile=profile,
    )

    request = seatbelt_mod._filesystem_operation_request(operation)

    assert request.policy.file_system is profile


def test_filesystem_operation_runtime_containment_is_case_sensitive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    runtime_root = tmp_path / "Runtime"
    target = tmp_path / "runtime" / "notes.txt"
    workspace.mkdir()
    runtime_root.mkdir()
    if target.parent.exists():
        pytest.skip("requires a case-sensitive filesystem")
    target.parent.mkdir()
    monkeypatch.setattr(
        seatbelt_mod,
        "_runtime_readonly_roots",
        lambda: (runtime_root,),
    )
    profile = FileSystemPermissionProfile(
        entries=(FileSystemPermissionEntry(target, FileSystemAccess.WRITE),)
    )
    operation = SandboxOperation.filesystem(
        kind="write_text",
        workspace=workspace,
        run_mode="trusted",
        path=target,
        content="hello",
        file_system_profile=profile,
    )

    request = seatbelt_mod._filesystem_operation_request(operation)

    assert request.policy.file_system is profile
