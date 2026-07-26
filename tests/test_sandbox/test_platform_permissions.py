from __future__ import annotations

from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

import pytest

from opensquilla.sandbox import platform_permissions as platform_permissions_module
from opensquilla.sandbox import policy as policy_module
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.platform_permissions import (
    WINDOWS_PLATFORM_READ_ROOTS,
    WINDOWS_PROFILE_READ_EXCLUSIONS,
    FileSystemPlatform,
    FileSystemPlatformContext,
    FileSystemSpecialPath,
    current_platform_context,
    resolve_special_path,
    resolve_temp_write_paths,
)
from opensquilla.sandbox.policy import build_policy
from opensquilla.sandbox.types import SecurityLevel


def _context(
    platform: FileSystemPlatform,
    *,
    cwd: PurePath,
    home: PurePath,
    helper_roots: tuple[PurePath, ...] = (),
    writable_roots: tuple[PurePath, ...] = (),
    user_profile_children: tuple[PurePath, ...] | None = None,
    env: dict[str, str] | None = None,
) -> FileSystemPlatformContext:
    return FileSystemPlatformContext(
        platform=platform,
        cwd=cwd,
        home=home,
        helper_roots=helper_roots,
        writable_roots=writable_roots,
        user_profile_children=user_profile_children,
        env=env or {},
    )


def test_platform_context_optional_fields_have_safe_defaults() -> None:
    context = FileSystemPlatformContext(
        platform="linux",
        cwd=PurePosixPath("/work/repo"),
        home=PurePosixPath("/home/codex"),
    )

    assert context.helper_roots == ()
    assert context.writable_roots == ()
    assert context.user_profile_children is None
    assert context.env == {}


def test_linux_policy_tolerates_unavailable_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"

    def unavailable_home(cls: type[Path]) -> Path:
        raise RuntimeError("home is unavailable")

    monkeypatch.setattr(platform_permissions_module.sys, "platform", "linux")
    monkeypatch.setattr(
        platform_permissions_module.Path,
        "home",
        classmethod(unavailable_home),
    )

    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(exclude_slash_tmp=True, exclude_tmpdir_env_var=True),
    )

    assert policy.file_system is not None
    assert policy.file_system.resolve(PurePosixPath("/etc/hosts")) is FileSystemAccess.READ
    assert policy.file_system.resolve(workspace / "src" / "app.py") is FileSystemAccess.WRITE


def test_windows_context_does_not_enumerate_fallback_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper_root = PureWindowsPath(r"C:\Codex\bin")
    writable_root = PureWindowsPath(r"D:\cache")

    def unavailable_home(cls: type[Path]) -> Path:
        raise OSError("home is unavailable")

    monkeypatch.setattr(platform_permissions_module.sys, "platform", "win32")
    monkeypatch.setattr(
        platform_permissions_module.Path,
        "home",
        classmethod(unavailable_home),
    )

    context = current_platform_context(
        cwd=tmp_path,
        helper_roots=(helper_root,),
        writable_roots=(writable_root,),
    )

    assert context.home == tmp_path
    assert context.user_profile_children == ()
    assert resolve_special_path(FileSystemSpecialPath.ROOT, context) == (
        *WINDOWS_PLATFORM_READ_ROOTS,
        helper_root,
        PureWindowsPath(str(tmp_path)),
        writable_root,
    )


def test_unknown_host_platform_falls_back_to_linux(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform_permissions_module.sys, "platform", "freebsd14")

    context = current_platform_context(cwd=tmp_path)

    assert context.platform == "linux"


def test_windows_codex_projection_constants_are_exact() -> None:
    assert WINDOWS_PROFILE_READ_EXCLUSIONS == frozenset(
        {
            ".ssh",
            ".tsh",
            ".brev",
            ".gnupg",
            ".aws",
            ".azure",
            ".kube",
            ".docker",
            ".config",
            ".npm",
            ".pki",
            ".terraform.d",
        }
    )
    assert WINDOWS_PLATFORM_READ_ROOTS == (
        PureWindowsPath(r"C:\Windows"),
        PureWindowsPath(r"C:\Program Files"),
        PureWindowsPath(r"C:\Program Files (x86)"),
        PureWindowsPath(r"C:\ProgramData"),
    )


def test_posix_root_resolves_to_slash() -> None:
    context = _context(
        "linux",
        cwd=PurePosixPath("/work/repo"),
        home=PurePosixPath("/home/codex"),
    )

    assert resolve_special_path(FileSystemSpecialPath.ROOT, context) == (PurePosixPath("/"),)


def test_windows_root_projects_codex_read_roots_in_stable_order() -> None:
    home = PureWindowsPath(r"C:\Users\codex")
    workspace = PureWindowsPath(r"C:\work\repo")
    context = _context(
        "windows",
        cwd=workspace,
        home=home,
        helper_roots=(PureWindowsPath(r"C:\Codex\bin"),),
        writable_roots=(workspace, PureWindowsPath(r"D:\cache")),
        user_profile_children=(
            home / "Desktop",
            home / ".ssh",
            home / "Documents",
            home / ".CONFIG",
        ),
    )

    assert resolve_special_path(FileSystemSpecialPath.ROOT, context) == (
        *WINDOWS_PLATFORM_READ_ROOTS,
        PureWindowsPath(r"C:\Codex\bin"),
        home / "Desktop",
        home / "Documents",
        workspace,
        PureWindowsPath(r"D:\cache"),
    )


def test_windows_workspace_profile_matches_codex_projection() -> None:
    home = PureWindowsPath(r"C:\Users\codex")
    workspace = PureWindowsPath(r"C:\work\repo")
    context = _context(
        "windows",
        cwd=workspace,
        home=home,
        writable_roots=(workspace,),
        user_profile_children=(
            home / "Desktop",
            home / ".ssh",
            home / "Documents",
        ),
    )

    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        tmp_writable=False,
        tmpdir_env_writable=False,
        platform_context=context,
    )

    assert profile.resolve(PureWindowsPath(r"C:\Windows\System32\cmd.exe")) is FileSystemAccess.READ
    assert profile.resolve(home / "Desktop" / "notes.txt") is FileSystemAccess.READ
    assert profile.resolve(home / ".ssh" / "id_ed25519") is FileSystemAccess.DENY
    assert not profile.is_explicitly_denied(home / ".ssh" / "id_ed25519")
    assert profile.resolve(workspace / "src" / "app.py") is FileSystemAccess.WRITE
    assert not profile.has_full_disk_read_baseline


def test_supplied_windows_context_preserves_special_root_flavor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    context = FileSystemPlatformContext(
        platform="windows",
        cwd=workspace,
        home=PureWindowsPath(r"C:\Users\codex"),
        user_profile_children=(),
    )

    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        tmp_writable=False,
        tmpdir_env_writable=False,
        platform_context=context,
    )

    assert PureWindowsPath(r"C:\Windows") in profile.readable_roots
    windows_root = Path(r"C:\Windows")
    expected_access = (
        FileSystemAccess.READ if windows_root.is_absolute() else FileSystemAccess.WRITE
    )
    assert profile.resolve(workspace / windows_root / "System32") is expected_access
    assert profile.resolve(workspace / "src" / "app.py") is FileSystemAccess.WRITE


def test_build_policy_uses_windows_context_for_host_root_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "repo"
    home = PureWindowsPath(r"C:\Users\codex")
    context = _context(
        "windows",
        cwd=PureWindowsPath(r"C:\work\repo"),
        home=home,
        writable_roots=(workspace,),
        user_profile_children=(home / "Desktop", home / ".ssh"),
    )
    calls: list[tuple[Path, tuple[PurePath, ...]]] = []

    def fake_context(
        *,
        cwd: Path,
        writable_roots: tuple[PurePath, ...],
    ) -> FileSystemPlatformContext:
        calls.append((cwd, writable_roots))
        return context

    monkeypatch.setattr(policy_module.sys, "platform", "win32")
    monkeypatch.setattr(policy_module, "current_platform_context", fake_context)

    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(exclude_slash_tmp=True, exclude_tmpdir_env_var=True),
    )

    assert calls == [(workspace, (workspace,))]
    assert policy.file_system is not None
    assert (
        policy.file_system.resolve(PureWindowsPath(r"C:\Windows\System32\cmd.exe"))
        is FileSystemAccess.READ
    )
    assert policy.file_system.resolve(home / ".ssh" / "id_ed25519") is FileSystemAccess.DENY


def test_macos_root_read_is_overridden_by_explicit_denied_root() -> None:
    context = _context(
        "macos",
        cwd=PurePosixPath("/Users/codex/repo"),
        home=PurePosixPath("/Users/codex"),
    )

    profile = FileSystemPermissionProfile.read_only(
        denied_read_roots=(PurePosixPath("/"),),
        platform_context=context,
    )

    assert profile.resolve(PurePosixPath("/System/Library")) is FileSystemAccess.DENY
    assert profile.effective_entries == (
        FileSystemPermissionEntry(PurePosixPath("/"), FileSystemAccess.DENY),
    )
    assert not profile.unsandboxed_execution_allowed


def test_macos_temp_writes_include_slash_tmp_and_absolute_tmpdir() -> None:
    context = _context(
        "macos",
        cwd=PurePosixPath("/Users/codex/repo"),
        home=PurePosixPath("/Users/codex"),
        env={"TMPDIR": "/var/folders/codex/T"},
    )

    assert resolve_temp_write_paths(
        context,
        include_slash_tmp=True,
        include_tmpdir=True,
    ) == (
        PurePosixPath("/tmp"),
        PurePosixPath("/var/folders/codex/T"),
    )


def test_posix_tmpdir_ignores_relative_value() -> None:
    context = _context(
        "linux",
        cwd=PurePosixPath("/work/repo"),
        home=PurePosixPath("/home/codex"),
        env={"TMPDIR": "relative/tmp"},
    )

    assert resolve_special_path(FileSystemSpecialPath.TMPDIR, context) == ()


def test_windows_temp_writes_use_temp_without_slash_tmp() -> None:
    context = _context(
        "windows",
        cwd=PureWindowsPath(r"C:\work\repo"),
        home=PureWindowsPath(r"C:\Users\codex"),
        env={"TEMP": r"C:\Users\codex\AppData\Local\Temp"},
    )

    assert resolve_special_path(FileSystemSpecialPath.SLASH_TMP, context) == ()
    assert resolve_temp_write_paths(
        context,
        include_slash_tmp=True,
        include_tmpdir=True,
    ) == (PureWindowsPath(r"C:\Users\codex\AppData\Local\Temp"),)


def test_windows_temp_roots_ignore_relative_values() -> None:
    context = _context(
        "windows",
        cwd=PureWindowsPath(r"C:\work\repo"),
        home=PureWindowsPath(r"C:\Users\codex"),
        env={
            "TEMP": r"relative\temp",
            "TMP": r"D:\absolute\temp",
            "TMPDIR": r"..\other-relative-temp",
        },
    )

    assert resolve_special_path(FileSystemSpecialPath.TMPDIR, context) == (
        PureWindowsPath(r"D:\absolute\temp"),
    )


def test_windows_glob_with_backslashes_denies_pem_file() -> None:
    workspace = PureWindowsPath(r"C:\work\repo")
    profile = FileSystemPermissionProfile(
        entries=(FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),),
        denied_read_globs=(r"C:\work\repo\**\*.pem",),
    )

    assert profile.resolve(workspace / "keys" / "identity.pem") is FileSystemAccess.DENY
    assert profile.resolve(workspace / "keys" / "identity.pub") is FileSystemAccess.WRITE


def test_windows_denied_glob_matching_is_case_insensitive() -> None:
    workspace = PureWindowsPath(r"C:\work\repo")
    candidate = PureWindowsPath(r"c:\WORK\REPO\Keys\Identity.PEM")
    profile = FileSystemPermissionProfile(
        entries=(FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),),
        denied_read_globs=(r"C:\work\repo\**\*.pem",),
    )

    assert profile.resolve(candidate) is FileSystemAccess.DENY
    assert profile.is_explicitly_denied(candidate)


def test_same_windows_target_deny_then_write_has_only_effective_write() -> None:
    denied = PureWindowsPath(r"C:\work\repo")
    writable = PureWindowsPath(r"c:\WORK\REPO")
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(denied, FileSystemAccess.DENY),
            FileSystemPermissionEntry(writable, FileSystemAccess.WRITE),
        )
    )

    assert profile.resolve(denied / "src" / "app.py") is FileSystemAccess.WRITE
    assert profile.effective_entries == (
        FileSystemPermissionEntry(writable, FileSystemAccess.WRITE),
    )
    assert profile.writable_roots == (writable,)
    assert profile.denied_read_roots == ()
    assert not profile.has_denied_reads


def test_full_access_uses_default_write_and_becomes_full_read_only() -> None:
    profile = FileSystemPermissionProfile.full_access()

    assert profile.entries == ()
    assert profile.default_access is FileSystemAccess.WRITE
    assert profile.resolve(PureWindowsPath(r"Z:\anywhere\file.txt")) is FileSystemAccess.WRITE

    read_only = profile.as_read_only()

    assert read_only.entries == ()
    assert read_only.default_access is FileSystemAccess.READ
    assert read_only.resolve(PurePosixPath("/anywhere/file.txt")) is FileSystemAccess.READ
    assert read_only.has_full_disk_read_baseline


@pytest.mark.parametrize(
    "path",
    (
        PurePosixPath("."),
        PureWindowsPath("C:"),
        PureWindowsPath("C:/"),
    ),
)
def test_non_posix_root_does_not_create_full_disk_read_baseline(path: PurePath) -> None:
    profile = FileSystemPermissionProfile(
        entries=(FileSystemPermissionEntry(path, FileSystemAccess.READ),)
    )

    assert not profile.has_full_disk_read_baseline


def test_profile_root_views_use_effective_entries() -> None:
    workspace = PurePosixPath("/work/repo")
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(PurePosixPath("/"), FileSystemAccess.READ),
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(workspace / ".git", FileSystemAccess.READ),
            FileSystemPermissionEntry(workspace / "secret", FileSystemAccess.DENY),
        )
    )

    assert profile.readable_roots == (PurePosixPath("/"), workspace / ".git")
    assert profile.writable_roots == (workspace,)
    assert profile.read_only_subpaths(workspace) == (
        workspace / ".git",
        workspace / "secret",
    )
    assert profile.has_full_disk_read_baseline


def test_read_only_subpaths_include_denied_carveouts() -> None:
    workspace = PurePosixPath("/work/repo")
    denied = workspace / "secret"
    profile = FileSystemPermissionProfile(
        entries=(
            FileSystemPermissionEntry(workspace, FileSystemAccess.WRITE),
            FileSystemPermissionEntry(denied, FileSystemAccess.DENY),
        )
    )

    assert profile.read_only_subpaths(workspace) == (denied,)
