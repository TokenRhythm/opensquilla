from __future__ import annotations

from pathlib import Path

from opensquilla.sandbox.sensitive_paths import (
    is_sensitive_path,
    linux_runtime_sensitive_deny_roots,
    sensitive_path_in_text,
    sensitive_path_marker,
    sensitive_target_in_command,
)


def test_sensitive_path_matches_nested_home_prefixes_with_native_separators() -> None:
    assert is_sensitive_path(str(Path.home() / ".ssh" / "id_rsa")) == "~/.ssh"
    assert is_sensitive_path(str(Path.home() / ".aws" / "credentials")) == "~/.aws"


def test_sensitive_path_in_text_matches_native_separator_paths() -> None:
    key_path = Path.home() / ".ssh" / "id_rsa"

    assert sensitive_path_in_text(f"type {key_path}") == "~/.ssh"


def test_active_workspace_under_root_is_not_blocked_by_root_prefix() -> None:
    workspace = Path("/root/.opensquilla/workspace")

    assert (
        sensitive_path_marker(
            str(workspace / "notes" / "plan.md"),
            workspace=workspace,
        )
        is None
    )
    assert (
        sensitive_path_in_text(
            f"cat {workspace / 'notes' / 'plan.md'}",
            workspace=workspace,
        )
        is None
    )


def test_active_workspace_exception_keeps_leaf_secret_blocks() -> None:
    workspace = Path("/root/.opensquilla/workspace")

    assert sensitive_path_marker(str(workspace / ".env"), workspace=workspace) in {
        "/.env",
        "/.env*",
    }
    assert sensitive_path_marker(str(workspace / "id_rsa"), workspace=workspace) == "/id_rsa"
    assert (
        sensitive_path_in_text(
            f"cat {workspace / '.env.local'}",
            workspace=workspace,
        )
        in {"/.env.local", "/.env*"}
    )


def test_sensitive_command_targets_honor_active_workspace_exception() -> None:
    workspace = Path("/root/.opensquilla/workspace")

    assert (
        sensitive_target_in_command(
            f"rm {workspace / 'scratch.txt'}",
            workspace=workspace,
        )
        is None
    )
    assert (
        sensitive_target_in_command(
            f"rm {workspace / '.env'}",
            workspace=workspace,
        )
        in {"/.env", "/.env*"}
    )


def test_windows_rooted_workspace_targets_keep_leaf_secret_blocks() -> None:
    workspace = Path("/root/.opensquilla/workspace")

    assert (
        sensitive_target_in_command(
            r"rm \root\.opensquilla\workspace\scratch.txt",
            workspace=workspace,
        )
        is None
    )
    assert (
        sensitive_target_in_command(
            r"rm \root\.opensquilla\workspace\.env",
            workspace=workspace,
        )
        in {"/.env", "/.env*"}
    )


def test_posix_sensitive_paths_stay_blocked_on_windows_runners() -> None:
    workspace = Path("/root/.opensquilla/workspace")

    assert sensitive_path_in_text("cat /dev/sda 2>/dev/null") == "/dev"
    assert (
        sensitive_path_in_text("cat /root/.ssh/id_rsa", workspace=workspace)
        == "~/.ssh"
    )


def test_ordinary_etc_files_are_readable_but_sensitive_entries_stay_blocked() -> None:
    assert sensitive_path_marker("/etc/hosts") is None
    assert sensitive_path_marker("/etc/passwd") is None
    assert sensitive_path_marker("/etc/shadow") == "/etc/shadow"
    assert sensitive_path_marker("/etc/ssh/sshd_config") == "/etc/ssh"


def test_linux_runtime_sensitive_deny_roots_excludes_workspace_parent() -> None:
    workspace = Path("/root/.opensquilla/workspace")

    roots = {path.as_posix() for path in linux_runtime_sensitive_deny_roots(workspace=workspace)}

    assert "/etc" not in roots
    assert "/etc/shadow" in roots
    assert "/root" not in roots
    assert "/root/.ssh" in roots
    assert workspace.as_posix() not in roots
