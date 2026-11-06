"""CLI tests for `opensquilla agents`."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from opensquilla.cli.main import app
from opensquilla.onboarding.config_store import load_config

runner = CliRunner()


def _setenv(monkeypatch, tmp_path: Path) -> Path:
    target = tmp_path / "c.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    return target


def test_agents_list_json_includes_builtin_main(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)

    result = runner.invoke(app, ["agents", "list", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload[0]["id"] == "main"
    assert payload[0]["isBuiltin"] is True


def test_agents_add_json_persists_config_entry(tmp_path, monkeypatch):
    target = _setenv(monkeypatch, tmp_path)

    result = runner.invoke(app, ["agents", "add", "ops", "--model", "openai/test", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["id"] == "ops"
    assert payload["model"] == "openai/test"
    cfg = load_config(target)
    assert [entry.id for entry in cfg.agents] == ["ops"]
    text = target.read_text(encoding="utf-8")
    assert "agents = [" in text
    assert 'id = "ops"' in text


def test_agents_add_duplicate_fails(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)
    first = runner.invoke(app, ["agents", "add", "ops", "--json"])
    assert first.exit_code == 0, first.stdout

    result = runner.invoke(app, ["agents", "add", "ops", "--json"])

    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "already exists" in combined


def test_agents_delete_main_rejected(tmp_path, monkeypatch):
    _setenv(monkeypatch, tmp_path)

    result = runner.invoke(app, ["agents", "delete", "main", "--force", "--json"])

    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "builtin agent" in combined


def test_agents_delete_force_json_removes_config_entry(tmp_path, monkeypatch):
    target = _setenv(monkeypatch, tmp_path)
    add_result = runner.invoke(app, ["agents", "add", "ops", "--json"])
    assert add_result.exit_code == 0, add_result.stdout

    result = runner.invoke(app, ["agents", "delete", "ops", "--force", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload == {
        "id": "ops",
        "deleted": True,
        "workspaceDeleted": False,
        "stateDeleted": False,
    }
    assert load_config(target).agents == []


def test_agents_delete_json_emits_structured_error_for_missing_agent(tmp_path, monkeypatch):
    """--json must emit one JSON document on the failure path.

    `sessions show --json` already does this; agents/channels bypassed it and
    printed the human renderer's "Error: ..." line instead.
    """
    _setenv(monkeypatch, tmp_path)

    result = runner.invoke(app, ["agents", "delete", "qa-does-not-exist", "--force", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "NOT_FOUND"
    assert "qa-does-not-exist" in payload["error"]["message"]


def test_agents_add_json_emits_structured_error_for_duplicate(tmp_path, monkeypatch):
    """The same failure path in `agents add`, which also advertises --json."""
    _setenv(monkeypatch, tmp_path)
    first = runner.invoke(app, ["agents", "add", "ops", "--json"])
    assert first.exit_code == 0, first.stdout

    result = runner.invoke(app, ["agents", "add", "ops", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stderr)
    assert "already exists" in payload["error"]["message"]


def test_agents_delete_without_json_keeps_plain_text_error(tmp_path, monkeypatch):
    """Control: the human path must be byte-for-byte what it always was."""
    _setenv(monkeypatch, tmp_path)

    result = runner.invoke(app, ["agents", "delete", "qa-does-not-exist", "--force"])

    assert result.exit_code == 2
    combined = result.stdout + (result.stderr or "")
    assert "Error: " in combined
    assert "{" not in combined
