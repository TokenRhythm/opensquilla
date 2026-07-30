from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from typer.main import get_command
from typer.testing import CliRunner

from opensquilla.cli.main import app as root_app
from opensquilla.cli.recovery_cmd import recovery_app
from opensquilla.recovery.cleanup import cleanup_inspect


def _workspace(path: Path, marker: str) -> Path:
    path.mkdir(parents=True)
    (path / "SOUL.md").write_text(marker + "\n", encoding="utf-8")
    return path


def _cleanup_approval(report) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "scope_fingerprint": report.scope_fingerprint,
            "items": [
                {"kind": item.kind, "path": str(item.path)}
                for item in report.items
            ],
        },
        ensure_ascii=False,
    ) + "\n"


def test_recovery_command_surface_is_registered_with_complete_offline_actions() -> None:
    root_command = get_command(root_app)
    recovery_command = get_command(recovery_app)

    assert "recovery" in root_command.commands
    assert set(recovery_command.commands) == {
        "inspect",
        "reconcile",
        "choose-workspace",
        "apply-settings",
        "recover-settings",
        "restore-profile",
        "recover-transaction",
        "consolidate-profiles",
        "acknowledge-profile-credential",
        "abandon-cleanup",
        "cleanup-inspect",
        "cleanup-apply",
    }


def test_recovery_subprocess_routes_before_cwd_or_profile_dotenv(
    tmp_path: Path,
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    home = tmp_path / "selected-home"
    workspace = _workspace(home / "workspace", "selected identity")
    (home / "state").mkdir(parents=True)
    (home / "config.toml").write_text(
        'state_dir = "state"\nworkspace_dir = "workspace"\n',
        encoding="utf-8",
    )
    (cwd / ".env").write_text(
        "OPENSQUILLA_RECOVERY_DOTENV_SENTINEL=loaded-from-cwd\n",
        encoding="utf-8",
    )
    (home / ".env").write_text(
        "OPENSQUILLA_RECOVERY_DOTENV_SENTINEL=loaded-from-profile\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("OPENSQUILLA_RECOVERY_OFFLINE", None)
    environment.pop("OPENSQUILLA_RECOVERY_DOTENV_SENTINEL", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import os, sys\n"
                "sys.argv = ['opensquilla', *sys.argv[1:]]\n"
                "from opensquilla.cli.main import app\n"
                "assert 'OPENSQUILLA_RECOVERY_DOTENV_SENTINEL' not in os.environ\n"
                "app()\n"
            ),
            "recovery",
            "inspect",
            "--home",
            str(home),
            "--json",
        ],
        check=False,
        text=True,
        capture_output=True,
        cwd=cwd,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["primary_home"] == str(home)
    assert payload["effective_workspace"] == str(workspace)


def test_inspect_command_emits_fixed_json_protocol(tmp_path: Path) -> None:
    home = tmp_path / "opensquilla"
    workspace = _workspace(home / "workspace", "current identity")
    (home / "state").mkdir(parents=True)
    (home / "config.toml").write_text(
        'state_dir = "state"\nworkspace_dir = "workspace"\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        recovery_app,
        ["inspect", "--home", str(home), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "schema_version",
        "outcome",
        "stable_code",
        "primary_home",
        "effective_workspace",
        "candidates",
        "allowed_actions",
        "transaction_id",
        "revision",
    }
    assert payload["outcome"] == "ready"
    assert payload["effective_workspace"] == str(workspace)


def test_cleanup_inspect_command_emits_complete_read_only_inventory(tmp_path: Path) -> None:
    user_data = tmp_path / "user-data"
    home = user_data / "opensquilla"
    home.mkdir(parents=True)
    credential = user_data / "desktop-credential.json"
    credential.write_text("{}\n", encoding="utf-8")

    result = CliRunner().invoke(
        recovery_app,
        [
            "cleanup-inspect",
            "--user-data",
            str(user_data),
            "--mode",
            "reset-current-settings",
            "--profile-kind",
            "primary",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload) == {
        "schema_version",
        "outcome",
        "stable_code",
        "mode",
        "items",
        "transaction_id",
        "revision",
        "scope_fingerprint",
    }
    assert payload["outcome"] == "ready"
    assert any(item["path"] == str(credential) for item in payload["items"])
    assert credential.is_file()


def test_delete_all_can_reinspect_only_after_offline_parent_exit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_data = tmp_path / "user-data"
    home = user_data / "opensquilla"
    (home / "state").mkdir(parents=True)
    credential = user_data / "desktop-credential.json"
    credential.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_RECOVERY_OFFLINE", "1")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "lock-state"))
    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )
    fingerprint = inspected.scope_fingerprint

    result = CliRunner().invoke(
        recovery_app,
        [
            "cleanup-apply",
            "--user-data",
            str(user_data),
            "--mode",
            "delete-all-user-data",
            "--profile-kind",
            "primary",
            "--transaction-id",
            "post-exit-reinspect",
            "--expected-revision",
            "0",
            "--expected-scope-fingerprint",
            fingerprint,
            "--confirm-user-data",
            str(user_data),
            "--after-parent-exit",
            "--json",
        ],
        input=_cleanup_approval(inspected),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["outcome"] == "complete"
    assert not credential.exists()
    assert not home.exists()


def test_parent_exit_cleanup_handoff_is_rejected_outside_offline_desktop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    user_data = tmp_path / "user-data"
    (user_data / "opensquilla").mkdir(parents=True)
    monkeypatch.delenv("OPENSQUILLA_RECOVERY_OFFLINE", raising=False)

    result = CliRunner().invoke(
        recovery_app,
        [
            "cleanup-apply",
            "--user-data",
            str(user_data),
            "--mode",
            "delete-all-user-data",
            "--profile-kind",
            "primary",
            "--transaction-id",
            "post-exit-reinspect",
            "--expected-revision",
            "0",
            "--confirm-user-data",
            str(user_data),
            "--after-parent-exit",
            "--json",
        ],
        input="",
    )

    assert result.exit_code == 2
    assert (user_data / "opensquilla").is_dir()


def test_delete_all_helper_waits_for_parent_pipe_eof_before_reinspection(
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    home = user_data / "opensquilla"
    (home / "state").mkdir(parents=True)
    credential = user_data / "desktop-credential.json"
    credential.write_text("{}\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["OPENSQUILLA_RECOVERY_OFFLINE"] = "1"
    environment["OPENSQUILLA_USER_STATE_DIR"] = str(tmp_path / "lock-state")
    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )
    fingerprint = inspected.scope_fingerprint
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "sys.argv = ['opensquilla', *sys.argv[1:]]\n"
                "from opensquilla.cli.main import app\n"
                "app()\n"
            ),
            "recovery",
            "cleanup-apply",
            "--user-data",
            str(user_data),
            "--mode",
            "delete-all-user-data",
            "--profile-kind",
            "primary",
            "--transaction-id",
            "post-exit-reinspect",
            "--expected-revision",
            "0",
            "--expected-scope-fingerprint",
            fingerprint,
            "--confirm-user-data",
            str(user_data),
            "--after-parent-exit",
            "--json",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        time.sleep(0.2)
        assert process.poll() is None
        assert credential.is_file(), "the helper must not delete while Electron's pipe is open"
        assert process.stdin is not None
        process.stdin.write(_cleanup_approval(inspected))
        process.stdin.flush()
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.stdout is not None
        payload = json.loads(process.stdout.read())
        assert payload["outcome"] == "complete"
        assert not credential.exists()
        assert not home.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_delete_all_helper_allows_confirmed_chromium_entry_to_disappear_at_exit(
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    home = user_data / "opensquilla"
    (home / "state").mkdir(parents=True)
    credential = user_data / "desktop-credential.json"
    credential.write_text("{}\n", encoding="utf-8")
    chromium_lock = user_data / "SingletonLock"
    chromium_lock.write_text("synthetic transient lock\n", encoding="utf-8")
    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )
    environment = os.environ.copy()
    environment["OPENSQUILLA_RECOVERY_OFFLINE"] = "1"
    environment["OPENSQUILLA_USER_STATE_DIR"] = str(tmp_path / "lock-state")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "sys.argv = ['opensquilla', *sys.argv[1:]]\n"
                "from opensquilla.cli.main import app\n"
                "app()\n"
            ),
            "recovery",
            "cleanup-apply",
            "--user-data",
            str(user_data),
            "--mode",
            "delete-all-user-data",
            "--profile-kind",
            "primary",
            "--transaction-id",
            "post-exit-reinspect",
            "--expected-revision",
            "0",
            "--expected-scope-fingerprint",
            inspected.scope_fingerprint,
            "--confirm-user-data",
            str(user_data),
            "--after-parent-exit",
            "--json",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        time.sleep(0.2)
        chromium_lock.unlink()
        assert process.stdin is not None
        process.stdin.write(_cleanup_approval(inspected))
        process.stdin.flush()
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.stdout is not None
        payload = json.loads(process.stdout.read())
        assert payload["outcome"] == "complete"
        assert not home.exists()
        assert not credential.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_delete_all_helper_refuses_a_new_unconfirmed_scope_after_parent_exit(
    tmp_path: Path,
) -> None:
    user_data = tmp_path / "user-data"
    home = user_data / "opensquilla"
    (home / "state").mkdir(parents=True)
    credential = user_data / "desktop-credential.json"
    credential.write_text("{}\n", encoding="utf-8")
    inspected = cleanup_inspect(
        user_data,
        mode="delete-all-user-data",
        profile_kind="primary",
    )
    fingerprint = inspected.scope_fingerprint
    environment = os.environ.copy()
    environment["OPENSQUILLA_RECOVERY_OFFLINE"] = "1"
    environment["OPENSQUILLA_USER_STATE_DIR"] = str(tmp_path / "lock-state")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "sys.argv = ['opensquilla', *sys.argv[1:]]\n"
                "from opensquilla.cli.main import app\n"
                "app()\n"
            ),
            "recovery",
            "cleanup-apply",
            "--user-data",
            str(user_data),
            "--mode",
            "delete-all-user-data",
            "--profile-kind",
            "primary",
            "--transaction-id",
            "post-exit-reinspect",
            "--expected-revision",
            "0",
            "--expected-scope-fingerprint",
            fingerprint,
            "--confirm-user-data",
            str(user_data),
            "--after-parent-exit",
            "--json",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        time.sleep(0.2)
        added_after_confirmation = user_data / "new-unconfirmed-root"
        added_after_confirmation.mkdir()
        assert process.stdin is not None
        process.stdin.write(_cleanup_approval(inspected))
        process.stdin.flush()
        process.stdin.close()
        assert process.wait(timeout=10) == 2
        assert process.stdout is not None
        payload = json.loads(process.stdout.read())
        assert payload["outcome"] == "blocked"
        assert payload["stable_code"] == "cleanup_scope_changed"
        assert home.is_dir()
        assert credential.is_file()
        assert added_after_confirmation.is_dir()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
