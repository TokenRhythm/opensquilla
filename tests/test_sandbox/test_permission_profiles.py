from __future__ import annotations

import os
import subprocess
from pathlib import Path, PureWindowsPath

import pytest

from opensquilla.sandbox import permissions as permissions_module
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.permissions import (
    PROTECTED_METADATA_NAMES,
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.platform_permissions import FileSystemPlatformContext
from opensquilla.sandbox.policy import build_policy
from opensquilla.sandbox.types import SecurityLevel


def _directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        if os.name != "nt" or getattr(exc, "winerror", None) != 1314:
            raise
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"directory links unavailable: {result.stderr or result.stdout}")


def _remove_directory_link(link: Path) -> None:
    try:
        link.unlink()
    except (IsADirectoryError, PermissionError):
        link.rmdir()


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


@pytest.mark.parametrize("name", PROTECTED_METADATA_NAMES)
def test_protected_metadata_symlink_preserves_lexical_and_target(
    tmp_path: Path,
    name: str,
) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "metadata-target"
    workspace.mkdir()
    target.mkdir()
    lexical = workspace / name
    _directory_link(lexical, target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )

    assert profile.protected_path_variants(lexical) == (lexical, target)
    assert (
        FileSystemPermissionEntry(
            target,
            FileSystemAccess.READ,
            logical_path=lexical,
        )
        in profile.effective_entries
    )
    assert profile.resolve(lexical / "config") is FileSystemAccess.READ
    assert profile.resolve(target / "config") is FileSystemAccess.READ
    assert profile.protected_metadata_root(lexical / "config") == lexical
    assert profile.protected_metadata_root(target / "config") == target


def test_logical_absolute_path_collapses_dots_without_following_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "metadata-target"
    workspace.mkdir()
    target.mkdir()
    _directory_link(workspace / ".git", target)
    dotted = workspace / "src" / ".." / ".git" / "config"
    logical = workspace / ".git" / "config"
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )

    assert permissions_module.logical_absolute_path(dotted) == logical
    assert profile.resolve(dotted) is FileSystemAccess.READ
    assert profile.protected_metadata_root(dotted) == workspace / ".git"


def test_effective_entries_retain_distinct_lexical_aliases(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "shared-target"
    workspace.mkdir()
    target.mkdir()
    aliases = (workspace / "first", workspace / "second")
    for alias in aliases:
        _directory_link(alias, target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        readable_roots=aliases,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
        protect_metadata=False,
    )

    target_entries = tuple(
        entry
        for entry in profile.effective_entries
        if entry.path == target and entry.access is FileSystemAccess.READ
    )

    assert tuple(entry.lexical_path for entry in target_entries) == aliases
    assert set(profile.read_only_subpaths(workspace)) == set(aliases)


@pytest.mark.parametrize(
    "access",
    (FileSystemAccess.READ, FileSystemAccess.DENY),
)
def test_symlinked_explicit_carveout_keeps_both_path_views(
    tmp_path: Path,
    access: FileSystemAccess,
) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "restricted-target"
    workspace.mkdir()
    target.mkdir()
    lexical = workspace / "restricted"
    _directory_link(lexical, target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        readable_roots=((lexical,) if access is FileSystemAccess.READ else ()),
        denied_read_roots=((lexical,) if access is FileSystemAccess.DENY else ()),
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
        protect_metadata=False,
    )

    assert (
        FileSystemPermissionEntry(
            target,
            access,
            logical_path=lexical,
        )
        in profile.effective_entries
    )
    assert profile.resolve(lexical / "item") is access
    assert profile.resolve(target / "item") is access
    assert profile.is_explicitly_denied(lexical / "item") is (access is FileSystemAccess.DENY)
    assert profile.is_explicitly_denied(target / "item") is (access is FileSystemAccess.DENY)
    assert profile.protected_path_variants(lexical) == (lexical, target)
    assert lexical in profile.read_only_subpaths(workspace)


def test_read_only_subpaths_include_lexical_and_canonical_protected_paths(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target = workspace / "metadata-target"
    workspace.mkdir()
    target.mkdir()
    lexical = workspace / ".git"
    _directory_link(lexical, target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )

    assert {lexical, target} <= set(profile.read_only_subpaths(workspace))


def test_protected_path_variants_follow_retargeted_symlink(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    original_target = tmp_path / "original-target"
    replacement_target = tmp_path / "replacement-target"
    workspace.mkdir()
    original_target.mkdir()
    replacement_target.mkdir()
    lexical = workspace / ".git"
    _directory_link(lexical, original_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )

    _remove_directory_link(lexical)
    _directory_link(lexical, replacement_target)

    assert profile.protected_path_variants(lexical) == (
        lexical,
        original_target,
        replacement_target,
    )
    assert profile.resolve(replacement_target / "config") is FileSystemAccess.READ


def test_protected_path_variants_preserve_frozen_canonical_spelling(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    frozen_target = tmp_path / "frozen-target"
    current_target = tmp_path / "current-target"
    workspace.mkdir()
    frozen_target.mkdir()
    current_target.mkdir()
    lexical = workspace / ".git"
    _directory_link(lexical, frozen_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )

    frozen_target.rmdir()
    _directory_link(frozen_target, current_target)

    assert profile.protected_path_variants(lexical) == (
        lexical,
        frozen_target,
        current_target,
    )
    assert profile.resolve(frozen_target / "config") is FileSystemAccess.READ
    assert profile.resolve(current_target / "config") is FileSystemAccess.READ


def test_retargeted_writable_symlink_does_not_expand_write_authority(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    frozen_target = tmp_path / "frozen-target"
    current_target = tmp_path / "current-target"
    workspace.mkdir()
    frozen_target.mkdir()
    current_target.mkdir()
    writable_alias = workspace / "generated"
    _directory_link(writable_alias, frozen_target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace / "unrelated",
        writable_roots=(writable_alias,),
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
        protect_metadata=False,
    )

    _remove_directory_link(writable_alias)
    _directory_link(writable_alias, current_target)

    assert profile.resolve(frozen_target / "artifact") is FileSystemAccess.WRITE
    assert profile.resolve(writable_alias / "artifact") is FileSystemAccess.DENY
    assert profile.resolve(current_target / "artifact") is FileSystemAccess.DENY
    assert profile.writable_path_variants(writable_alias) == (frozen_target,)


def test_replaced_frozen_write_root_is_marked_retargeted(
    tmp_path: Path,
) -> None:
    writable_root = tmp_path / "writable-root"
    current_target = tmp_path / "current-target"
    writable_root.mkdir()
    current_target.mkdir()
    profile = FileSystemPermissionProfile.workspace(
        workspace=tmp_path / "unrelated",
        writable_roots=(writable_root,),
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
        protect_metadata=False,
    )

    writable_root.rmdir()
    _directory_link(writable_root, current_target)

    assert profile.retargeted_writable_roots == (writable_root,)
    assert profile.resolve(writable_root / "artifact") is FileSystemAccess.DENY
    assert profile.resolve(current_target / "artifact") is FileSystemAccess.DENY


def test_as_read_only_preserves_logical_path(tmp_path: Path) -> None:
    target = tmp_path / "target"
    lexical = tmp_path / "alias"
    entry = FileSystemPermissionEntry(
        target,
        FileSystemAccess.WRITE,
        logical_path=lexical,
    )

    assert FileSystemPermissionProfile(entries=(entry,)).as_read_only().entries == (
        FileSystemPermissionEntry(
            target,
            FileSystemAccess.READ,
            logical_path=lexical,
        ),
    )


def test_same_spelling_later_declaration_still_overrides(tmp_path: Path) -> None:
    target = tmp_path / "target"
    lexical = tmp_path / "alias"
    target.mkdir()
    _directory_link(lexical, target)
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(
                target,
                FileSystemAccess.DENY,
                logical_path=lexical,
            ),
            FileSystemPermissionEntry(
                target,
                FileSystemAccess.WRITE,
                logical_path=lexical,
            ),
        )
    )

    assert profile.effective_entries == (
        FileSystemPermissionEntry(
            target,
            FileSystemAccess.WRITE,
            logical_path=lexical,
        ),
    )
    assert profile.resolve(lexical / "item") is FileSystemAccess.WRITE
    assert profile.resolve(target / "item") is FileSystemAccess.WRITE
    assert not profile.is_explicitly_denied(lexical / "item")


def test_distinct_spellings_for_same_target_choose_most_restrictive(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    denied_alias = tmp_path / "denied-alias"
    writable_alias = tmp_path / "writable-alias"
    target.mkdir()
    _directory_link(denied_alias, target)
    _directory_link(writable_alias, target)
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(
                target,
                FileSystemAccess.DENY,
                logical_path=denied_alias,
            ),
            FileSystemPermissionEntry(
                target,
                FileSystemAccess.WRITE,
                logical_path=writable_alias,
            ),
        )
    )

    assert profile.resolve(target / "item") is FileSystemAccess.DENY
    assert profile.resolve(writable_alias / "item") is FileSystemAccess.DENY
    assert profile.is_explicitly_denied(target / "item")
    assert profile.is_explicitly_denied(writable_alias / "item")


def test_more_specific_write_reopens_both_views_of_readonly_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "metadata-target"
    workspace.mkdir()
    target.mkdir()
    lexical = workspace / ".git"
    _directory_link(lexical, target)
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        writable_roots=(lexical / "objects",),
        host_root_readonly=False,
        tmp_writable=False,
        tmpdir_env_writable=False,
    )

    assert profile.resolve(lexical / "objects" / "pack") is FileSystemAccess.WRITE
    assert profile.resolve(target / "objects" / "pack") is FileSystemAccess.WRITE


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


def test_windows_workspace_profile_reads_all_drives_without_read_approval() -> None:
    workspace = PureWindowsPath(r"D:\projects\opensquilla")
    denied = PureWindowsPath(r"C:\Users\lrk\.ssh")
    context = FileSystemPlatformContext(
        platform="windows",
        cwd=workspace,
        home=PureWindowsPath(r"C:\Users\lrk"),
        writable_roots=(workspace,),
        env={},
    )

    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        denied_read_roots=(denied,),
        tmp_writable=False,
        tmpdir_env_writable=False,
        platform_context=context,
    )

    assert profile.default_access is FileSystemAccess.READ
    assert profile.has_full_disk_read_baseline
    assert (
        profile.resolve(PureWindowsPath(r"C:\Windows\System32\drivers\etc\hosts"))
        is FileSystemAccess.READ
    )
    assert profile.resolve(PureWindowsPath(r"D:\lrk\notes.txt")) is FileSystemAccess.READ
    assert profile.resolve(PureWindowsPath(r"E:\archive\data.json")) is FileSystemAccess.READ
    assert profile.resolve(workspace / "src" / "app.py") is FileSystemAccess.WRITE
    assert profile.resolve(denied / "id_ed25519") is FileSystemAccess.DENY


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
    _directory_link(alias_root, real_root)
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
