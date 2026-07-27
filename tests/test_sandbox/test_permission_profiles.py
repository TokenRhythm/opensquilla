from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

import pytest

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.platform_permissions import FileSystemPlatformContext
from opensquilla.sandbox.policy import build_policy
from opensquilla.sandbox.types import SecurityLevel


def test_workspace_profile_reads_root_and_writes_declared_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    cache = tmp_path / "cache"
    tmpdir = tmp_path / "tmpdir"
    monkeypatch.setenv("TMPDIR", str(tmpdir))

    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        writable_roots=(cache,),
    )

    readable_root = profile.readable_roots[0]
    assert profile.resolve(readable_root / "probe") is FileSystemAccess.READ
    assert profile.resolve(workspace / "src" / "a.py") is FileSystemAccess.WRITE
    assert profile.resolve(cache / "artifact") is FileSystemAccess.WRITE
    if os.name != "nt":
        assert profile.resolve(Path("/tmp") / "probe") is FileSystemAccess.WRITE
    assert profile.resolve(tmpdir / "probe") is FileSystemAccess.WRITE


@pytest.mark.parametrize("name", [".git", ".agents", ".codex"])
def test_workspace_profile_reprotects_metadata(tmp_path: Path, name: str) -> None:
    profile = FileSystemPermissionProfile.workspace(workspace=tmp_path)

    assert profile.resolve(tmp_path / name / "config") is FileSystemAccess.READ
    assert profile.protected_metadata_root(tmp_path / name / "config") == tmp_path / name


def test_explicit_denied_read_prevents_unsandboxed_execution(tmp_path: Path) -> None:
    profile = FileSystemPermissionProfile.workspace(
        workspace=tmp_path,
        denied_read_roots=(tmp_path / "secret",),
    )

    assert profile.resolve(tmp_path / "secret" / "token") is FileSystemAccess.DENY
    assert profile.is_explicitly_denied(tmp_path / "secret" / "token")
    assert profile.has_denied_reads
    assert not profile.unsandboxed_execution_allowed


def test_unmatched_path_is_not_an_explicit_denied_read(tmp_path: Path) -> None:
    profile = FileSystemPermissionProfile.workspace(
        workspace=tmp_path / "workspace",
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )
    outside = tmp_path / "outside"

    assert profile.resolve(outside) is FileSystemAccess.DENY
    assert not profile.is_explicitly_denied(outside)


def test_denied_read_glob_takes_precedence_over_writable_root(tmp_path: Path) -> None:
    profile = FileSystemPermissionProfile.workspace(
        workspace=tmp_path,
        denied_read_globs=(str(tmp_path / "**" / "*.pem"),),
    )

    assert profile.resolve(tmp_path / "keys" / "identity.pem") is FileSystemAccess.DENY
    assert profile.resolve(tmp_path / "keys" / "identity.pub") is FileSystemAccess.WRITE


def test_denied_read_glob_matches_canonical_symlink_path(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    profile = FileSystemPermissionProfile.workspace(
        workspace=tmp_path / "workspace",
        denied_read_globs=(str(alias_root / "**" / "*.pem"),),
    )

    assert profile.resolve(real_root / "keys" / "identity.pem") is FileSystemAccess.DENY


def test_build_policy_carries_the_canonical_workspace_profile(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    cache = tmp_path / "cache"
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(extra_rw_mounts=[str(cache)]),
    )

    assert policy.file_system is not None
    readable_root = policy.file_system.readable_roots[0]
    assert policy.file_system.resolve(readable_root / "probe") is FileSystemAccess.READ
    assert policy.file_system.resolve(workspace / "a.py") is FileSystemAccess.WRITE
    assert policy.file_system.resolve(cache / "artifact") is FileSystemAccess.WRITE


def test_build_policy_applies_configured_denied_reads(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    secret = tmp_path / "secret"
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(
            denied_read_roots=[str(secret)],
            denied_read_globs=[str(tmp_path / "**" / "*.pem")],
        ),
    )

    assert policy.file_system is not None
    assert policy.file_system.resolve(secret / "token") is FileSystemAccess.DENY
    assert policy.file_system.resolve(workspace / "identity.pem") is FileSystemAccess.DENY
    assert not policy.file_system.unsandboxed_execution_allowed


def test_non_linux_workspace_profile_does_not_add_posix_tmp(
    tmp_path: Path,
) -> None:
    platform_context = FileSystemPlatformContext(
        platform="windows",
        cwd=PureWindowsPath(r"C:\work\repo"),
        home=PureWindowsPath(r"C:\Users\codex"),
        helper_roots=(),
        writable_roots=(),
        user_profile_children=(),
        env={},
    )

    profile = FileSystemPermissionProfile.workspace(
        workspace=tmp_path,
        platform_context=platform_context,
    )

    assert profile.resolve(Path("/tmp/guardian-probe")) is not FileSystemAccess.WRITE


def test_codex_tmp_exclusion_flags_remove_only_requested_write_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmpdir = tmp_path / "custom-tmp"
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path / "repo",
        SandboxSettings(exclude_slash_tmp=True, exclude_tmpdir_env_var=True),
    )

    assert policy.file_system.resolve(Path("/tmp/probe")) is not FileSystemAccess.WRITE
    assert policy.file_system.resolve(tmpdir / "probe") is not FileSystemAccess.WRITE


def test_disabled_policy_is_the_only_full_access_profile(tmp_path: Path) -> None:
    policy = build_policy(
        SecurityLevel.DISABLED,
        "shell.exec",
        tmp_path,
        SandboxSettings(default_level=SecurityLevel.DISABLED, allow_legacy_mode=True),
    )

    assert policy.file_system is not None
    assert policy.file_system.resolve(Path("/etc/hosts")) is FileSystemAccess.WRITE
