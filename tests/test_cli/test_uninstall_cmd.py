"""CLI surface for `opensquilla uninstall` — flags, guards, confirmation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opensquilla.cli import uninstall_cmd
from opensquilla.cli.main import app
from opensquilla.uninstall import actions as actions_module
from opensquilla.uninstall import inventory as inventory_module
from opensquilla.uninstall.actions import ActionResult, ExecutionResult
from opensquilla.uninstall.inventory import DataBucket, Inventory

runner = CliRunner()


def _fake_inventory(home: Path) -> Inventory:
    state = home / "state"
    state.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text("x")
    return Inventory(
        method="pip",
        home=home,
        state_root=state,
        config_path=None,
        entrypoints=[],
        program_paths=[],
        package_uninstall=["pip", "uninstall", "-y", "opensquilla"],
        buckets=[
            DataBucket("config.toml", home / "config.toml", "config", "config"),
            DataBucket("state directory", state, "user-data", "state"),
        ],
        services=[],
        receipt=None,
        notes=[],
        home_recognized=True,
    )


def _patch_discover(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(inventory_module, "discover", lambda: _fake_inventory(home))


def test_dry_run_json_emits_plan_and_does_nothing(monkeypatch, tmp_path: Path) -> None:
    _patch_discover(monkeypatch, tmp_path / "home")

    def _boom(*_a, **_k):
        raise AssertionError("execute must not run during --dry-run")

    monkeypatch.setattr(actions_module, "execute", _boom)

    result = runner.invoke(app, ["uninstall", "--dry-run", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["plan"]["method"] == "pip"


def test_dry_run_json_subprocess_stdout_stays_machine_readable(tmp_path: Path) -> None:
    """Import-time env loading must not pollute stdout before JSON output."""
    home = tmp_path / "home"
    env_home = home / ".opensquilla"
    env_home.mkdir(parents=True)
    (env_home / ".env").write_text("OPENSQUILLA_ENV_JSON_TEST=1\n", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "HTTP_PROXY": "http://127.0.0.1:9",
        }
    )
    env.pop("OPENSQUILLA_TRUST_ENV", None)

    completed = subprocess.run(
        [sys.executable, "-m", "opensquilla.cli.main", "uninstall", "--dry-run", "--json"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert completed.stdout.lstrip().startswith("{")
    payload = json.loads(completed.stdout)
    assert payload["dry_run"] is True


def test_non_interactive_without_yes_refuses(monkeypatch, tmp_path: Path) -> None:
    _patch_discover(monkeypatch, tmp_path / "home")

    def _boom(*_a, **_k):
        raise AssertionError("execute must not run without confirmation")

    monkeypatch.setattr(actions_module, "execute", _boom)

    result = runner.invoke(app, ["uninstall", "--json"])
    assert result.exit_code == 2, result.stdout
    assert "CONFIRMATION_REQUIRED" in (result.stdout + (result.stderr or ""))


def test_yes_json_executes(monkeypatch, tmp_path: Path) -> None:
    _patch_discover(monkeypatch, tmp_path / "home")
    captured = {}

    def _fake_execute(plan, inventory, **_kwargs):
        captured["ran"] = True
        return ExecutionResult(
            results=[ActionResult("run-package-uninstall", "ok", ok=True)], ok=True
        )

    monkeypatch.setattr(actions_module, "execute", _fake_execute)

    result = runner.invoke(app, ["uninstall", "--yes", "--json"])
    assert result.exit_code == 0, result.stdout
    assert captured.get("ran") is True
    payload = json.loads(result.stdout)
    assert payload["ok"] is True


@pytest.mark.parametrize("purge_flag", ["--purge-state", "--purge-config", "--purge-all"])
def test_desktop_profile_routes_data_deletion_to_complete_desktop_cleanup(
    monkeypatch,
    tmp_path: Path,
    purge_flag: str,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_PROFILE_KIND", "desktop-primary")

    def _boom(*_args, **_kwargs):
        raise AssertionError("generic Desktop purge must not inspect or delete partial data")

    monkeypatch.setattr(inventory_module, "discover", _boom)
    monkeypatch.setattr(actions_module, "execute", _boom)

    result = runner.invoke(app, ["uninstall", purge_flag, "--yes", "--json"])

    assert result.exit_code == 2, result.stdout
    assert "DESKTOP_CLEANUP_REQUIRED" in (result.stdout + (result.stderr or ""))


def test_purge_all_requires_confirmation_phrase(monkeypatch, tmp_path: Path) -> None:
    _patch_discover(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(uninstall_cmd, "_stdin_is_tty", lambda: True)  # allow interactive path

    def _boom(*_a, **_k):
        raise AssertionError("execute must not run on a mismatched phrase")

    monkeypatch.setattr(actions_module, "execute", _boom)

    result = runner.invoke(app, ["uninstall", "--purge-all"], input="nope\n")
    assert result.exit_code == 2, result.stdout
    assert "requires confirmation" in (result.stdout + (result.stderr or ""))


def test_yes_purge_all_without_phrase_is_refused(monkeypatch, tmp_path: Path) -> None:
    """`--yes --purge-all` must NOT wipe without the explicit second-factor phrase."""
    _patch_discover(monkeypatch, tmp_path / "home")

    def _boom(*_a, **_k):
        raise AssertionError("execute must not run without the purge-all phrase")

    monkeypatch.setattr(actions_module, "execute", _boom)

    result = runner.invoke(app, ["uninstall", "--yes", "--purge-all", "--json"])
    assert result.exit_code == 2, result.stdout
    assert "CONFIRMATION_REQUIRED" in (result.stdout + (result.stderr or ""))


def test_yes_purge_all_with_phrase_executes(monkeypatch, tmp_path: Path) -> None:
    _patch_discover(monkeypatch, tmp_path / "home")
    captured = {}

    def _fake_execute(plan, inventory, **_kwargs):
        captured["ran"] = True
        return ExecutionResult(results=[], ok=True)

    monkeypatch.setattr(actions_module, "execute", _fake_execute)

    result = runner.invoke(
        app,
        ["uninstall", "--yes", "--purge-all", "--json", "--confirm-purge-all", "delete everything"],
    )
    assert result.exit_code == 0, result.stdout
    assert captured.get("ran") is True


def test_purge_all_proceeds_on_correct_phrase(monkeypatch, tmp_path: Path) -> None:
    _patch_discover(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(uninstall_cmd, "_stdin_is_tty", lambda: True)
    captured = {}

    def _fake_execute(plan, inventory, **_kwargs):
        captured["ran"] = True
        return ExecutionResult(results=[], ok=True)

    monkeypatch.setattr(actions_module, "execute", _fake_execute)

    result = runner.invoke(app, ["uninstall", "--purge-all"], input="delete everything\n")
    assert result.exit_code == 0, result.stdout
    assert captured.get("ran") is True
