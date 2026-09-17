from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from opensquilla.sandbox.file_policy import (
    GuestWorkspacePolicyError,
    builtin_deny_write_paths,
    compile_safe_file_profile,
    compile_web_guest_file_profile,
    decide_file_access,
    validate_web_guest_workspace,
)
from opensquilla.sandbox.permissions import FileSystemAccess
from opensquilla.sandbox.policy_models import SandboxPolicy


def test_windows_builtin_deny_write_contains_requested_credentials() -> None:
    env = {
        "USERPROFILE": r"C:\Users\alice",
        "APPDATA": r"C:\Users\alice\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
    }

    roots = builtin_deny_write_paths("win32", env=env)

    assert PureWindowsPath(r"C:\Users\alice\.ssh") in roots
    assert PureWindowsPath(r"C:\Users\alice\.aws") in roots
    assert PureWindowsPath(r"C:\Users\alice\.kube\config") in roots
    assert PureWindowsPath(r"C:\Users\alice\.docker\config.json") in roots
    assert PureWindowsPath(r"C:\Users\alice\.config\gh\hosts.yml") in roots
    assert PureWindowsPath(r"C:\Users\alice\.terraform.d\credentials.tfrc.json") in roots


def test_windows_safe_profile_projects_user_home_write_with_read_baseline() -> None:
    home = PureWindowsPath(r"C:\Users\alice")
    profile = compile_safe_file_profile(
        SandboxPolicy(),
        platform="win32",
        home=home,
        env={
            "USERPROFILE": str(home),
            "APPDATA": r"C:\Users\alice\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        },
    )

    assert profile.default_access is FileSystemAccess.READ
    assert profile.resolve(home / "Documents" / "ordinary.txt") is FileSystemAccess.WRITE
    assert profile.resolve(home / ".ssh" / "config") is FileSystemAccess.READ
    assert any(
        entry.path == home and entry.access is FileSystemAccess.WRITE
        for entry in profile.entries
    )


def test_safe_ordinary_read_and_write_are_automatic(tmp_path: Path) -> None:
    policy = SandboxPolicy()
    target = tmp_path / "ordinary.txt"

    read = decide_file_access("read", target, policy, platform="linux")
    write = decide_file_access("write", target, policy, platform="linux")

    assert read.allowed is True and read.approval_required is False
    assert write.allowed is True and write.approval_required is False


def test_custom_deny_write_requires_approval_but_read_stays_allowed(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "protected"
    target = protected / "nested" / "credential.txt"
    policy = SandboxPolicy.model_validate(
        {"files": {"customDenyWritePaths": [f"{protected}/**"]}}
    )

    read = decide_file_access("read", target, policy, platform="linux")
    write = decide_file_access("write", target, policy, platform="linux")

    assert read.allowed is True
    assert write.allowed is False
    assert write.approval_required is True
    assert write.code == "sensitive_file_mutation_requires_approval"
    assert write.rule_source == "custom"


def test_builtin_rules_cannot_be_removed_by_empty_custom_policy(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = home / ".ssh" / "config"

    decision = decide_file_access(
        "delete",
        target,
        SandboxPolicy(),
        platform="linux",
        home=home,
    )

    assert decision.approval_required is True
    assert decision.rule_source == "builtin"


def test_safe_profile_compiles_write_baseline_and_read_only_carveouts(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    authority = tmp_path / "state"
    custom = tmp_path / "custom-secret"
    profile = compile_safe_file_profile(
        SandboxPolicy.model_validate(
            {"files": {"customDenyWritePaths": [f"{custom}/**"]}}
        ),
        authority_roots=(authority,),
        platform="linux",
        home=home,
        env={"HOME": str(home)},
    )

    assert profile.resolve(tmp_path / "ordinary.txt") is FileSystemAccess.WRITE
    assert profile.resolve(home / ".ssh" / "config") is FileSystemAccess.READ
    assert profile.resolve(custom / "credential") is FileSystemAccess.READ
    assert profile.resolve(authority / "sessions.db") is FileSystemAccess.DENY


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX alias canonicalization requires native POSIX path semantics",
)
def test_safe_profile_freezes_posix_alias_and_canonical_protected_paths(
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    authority = alias_root / "state"
    authority.mkdir()
    custom = alias_root / "custom-secret"
    custom.mkdir()

    profile = compile_safe_file_profile(
        SandboxPolicy.model_validate(
            {"files": {"customDenyWritePaths": [f"{custom}/**"]}}
        ),
        authority_roots=(authority,),
        platform="linux",
        home=tmp_path / "home",
        env={"HOME": str(tmp_path / "home")},
    )

    assert profile.resolve(real_root / "state" / "sessions.db") is FileSystemAccess.DENY
    assert profile.resolve(real_root / "custom-secret" / "token") is FileSystemAccess.READ
    authority_variants = {str(path) for path in profile.protected_path_variants(authority)}
    assert str(authority) in authority_variants
    assert str(authority.resolve()) in authority_variants


def test_windows_web_guest_profile_denies_credentials_and_writes_only_workspace() -> None:
    home = PureWindowsPath(r"C:\Users\alice")
    workspace = PureWindowsPath(r"D:\OpenSquilla\workspace")
    authority = PureWindowsPath(r"C:\Users\alice\.opensquilla\state")
    guest_home = workspace.parent / "guest-home"
    guest_temp = workspace.parent / "guest-temp"
    runtime = PureWindowsPath(r"C:\Program Files\OpenSquilla\runtime\python")
    custom = PureWindowsPath(r"C:\Company\protected")
    profile = compile_web_guest_file_profile(
        SandboxPolicy.model_validate(
            {"files": {"customDenyWritePaths": [rf"{custom}\**"]}}
        ),
        workspace=workspace,
        writable_roots=(guest_home, guest_temp),
        runtime_roots=(runtime,),
        authority_roots=(authority,),
        platform="win32",
        home=home,
        env={
            "USERPROFILE": str(home),
            "APPDATA": r"C:\Users\alice\AppData\Roaming",
            "LOCALAPPDATA": r"C:\Users\alice\AppData\Local",
        },
    )

    assert profile.default_access is FileSystemAccess.DENY
    assert profile.resolve(home / "Documents" / "ordinary.txt") is FileSystemAccess.DENY
    assert profile.resolve(workspace / "nested" / "new.txt") is FileSystemAccess.WRITE
    assert profile.resolve(guest_home / "notes.txt") is FileSystemAccess.WRITE
    assert profile.resolve(guest_temp / "scratch.txt") is FileSystemAccess.WRITE
    assert profile.resolve(runtime / "python.exe") is FileSystemAccess.READ
    assert profile.resolve(PureWindowsPath(r"D:\outside.txt")) is FileSystemAccess.DENY
    assert profile.resolve(home / ".ssh" / "id_ed25519") is FileSystemAccess.DENY
    assert profile.resolve(authority / "sessions.db") is FileSystemAccess.DENY
    assert profile.resolve(custom / "file.txt") is FileSystemAccess.DENY


def test_posix_web_guest_profile_denies_credentials_and_writes_only_workspace(
) -> None:
    home = PurePosixPath("/home/alice")
    workspace = PurePosixPath("/srv/opensquilla/workspace")
    authority = PurePosixPath("/srv/opensquilla/state")
    guest_home = workspace.parent / "guest-home"
    guest_temp = workspace.parent / "guest-temp"
    runtime = PurePosixPath("/opt/opensquilla/runtime/python")
    custom = PurePosixPath("/srv/company/protected")
    profile = compile_web_guest_file_profile(
        SandboxPolicy.model_validate(
            {"files": {"customDenyWritePaths": [f"{custom}/**"]}}
        ),
        workspace=workspace,
        writable_roots=(guest_home, guest_temp),
        runtime_roots=(runtime,),
        authority_roots=(authority,),
        platform="linux",
        home=home,
        env={"HOME": str(home)},
    )

    assert profile.default_access is FileSystemAccess.DENY
    assert profile.resolve(home / "Documents" / "ordinary.txt") is FileSystemAccess.DENY
    assert profile.resolve(workspace / "nested" / "new.txt") is FileSystemAccess.WRITE
    assert profile.resolve(guest_home / "notes.txt") is FileSystemAccess.WRITE
    assert profile.resolve(guest_temp / "scratch.txt") is FileSystemAccess.WRITE
    assert profile.resolve(runtime / "bin" / "python") is FileSystemAccess.READ
    assert profile.resolve(PurePosixPath("/srv/outside.txt")) is FileSystemAccess.DENY
    assert profile.resolve(home / ".ssh" / "id_ed25519") is FileSystemAccess.DENY
    assert profile.resolve(authority / "sessions.db") is FileSystemAccess.DENY
    assert profile.resolve(custom / "file.txt") is FileSystemAccess.DENY


def test_web_guest_workspace_beneath_sensitive_root_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"

    with pytest.raises(GuestWorkspacePolicyError) as raised:
        validate_web_guest_workspace(
            home / ".ssh" / "project",
            platform="linux",
            home=home,
            env={"HOME": str(home)},
        )

    assert raised.value.code == "GUEST_DEFAULT_WORKSPACE_UNSAFE"


def test_web_guest_workspace_alias_to_sensitive_root_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sensitive = home / ".ssh"
    sensitive.mkdir(parents=True)
    alias = tmp_path / "workspace"
    try:
        alias.symlink_to(sensitive, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlinks unavailable: {exc}")
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(alias), str(sensitive)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"directory aliases unavailable: {result.stderr or result.stdout}")

    with pytest.raises(GuestWorkspacePolicyError) as raised:
        validate_web_guest_workspace(
            alias,
            platform="win32",
            home=home,
            env={
                "USERPROFILE": str(home),
                "APPDATA": str(home / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(home / "AppData" / "Local"),
            },
        )

    assert raised.value.code == "GUEST_DEFAULT_WORKSPACE_UNSAFE"


@pytest.mark.parametrize("grant_kind", ["workspace", "writable", "runtime"])
def test_web_guest_grant_alias_to_authority_root_is_rejected(
    tmp_path: Path,
    grant_kind: str,
) -> None:
    authority = tmp_path / "state"
    authority.mkdir()
    alias = tmp_path / f"{grant_kind}-alias"
    try:
        alias.symlink_to(authority, target_is_directory=True)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory aliases unavailable: {exc}")
        result = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(alias), str(authority)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"directory aliases unavailable: {result.stderr or result.stdout}")
    workspace = tmp_path / "guest" / "workspace"
    workspace.mkdir(parents=True)
    kwargs: dict[str, object] = {
        "workspace": workspace,
        "authority_roots": (authority,),
    }
    if grant_kind == "workspace":
        kwargs["workspace"] = alias
    elif grant_kind == "writable":
        kwargs["writable_roots"] = (alias,)
    else:
        kwargs["runtime_roots"] = (alias,)

    with pytest.raises(GuestWorkspacePolicyError):
        compile_web_guest_file_profile(SandboxPolicy(), **kwargs)
