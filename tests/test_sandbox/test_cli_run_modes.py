from __future__ import annotations

import json
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from opensquilla.cli.main import app
from opensquilla.onboarding.config_store import load_config

runner = CliRunner()


def _invoke(config_path: Path, *args: str):
    return runner.invoke(app, ["sandbox", *args, "--config", str(config_path)])


def test_sandbox_trust_keeps_runtime_sandbox_enabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    result = _invoke(config_path, "trust")

    assert result.exit_code == 0, result.output
    cfg = load_config(config_path)
    assert cfg.sandbox.run_mode == "trusted"
    assert cfg.sandbox.sandbox is True
    assert cfg.sandbox.security_grading is True
    assert cfg.permissions.default_mode == "off"


def test_sandbox_bypass_fails_without_changing_config(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    before = _invoke(config_path, "on")
    assert before.exit_code == 0, before.output

    result = _invoke(config_path, "bypass")

    assert result.exit_code != 0
    assert "removed" in result.output.lower()
    assert "sandbox trust" in result.output
    cfg = load_config(config_path)
    assert cfg.sandbox.run_mode == "trusted"
    assert cfg.sandbox.sandbox is True


def test_sandbox_on_restores_trusted_sandbox_default(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    assert _invoke(config_path, "full").exit_code == 0

    result = _invoke(config_path, "on")

    assert result.exit_code == 0, result.output
    cfg = load_config(config_path)
    assert cfg.sandbox.run_mode == "trusted"
    assert cfg.sandbox.sandbox is True
    assert cfg.sandbox.security_grading is True
    assert cfg.permissions.default_mode == "off"


def test_sandbox_reset_restores_full_host_access_default(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    assert _invoke(config_path, "full").exit_code == 0

    reset = _invoke(config_path, "reset")

    assert reset.exit_code == 0, reset.output
    cfg = load_config(config_path)
    assert cfg.sandbox.run_mode == "full"
    assert cfg.sandbox.sandbox is False
    assert cfg.sandbox.security_grading is False
    assert cfg.permissions.default_mode == "full"


def test_sandbox_status_reports_run_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    assert _invoke(config_path, "trust").exit_code == 0

    result = runner.invoke(app, ["sandbox", "status", "--config", str(config_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_mode"] == "trusted"
    assert payload["run_mode_label"] == "Managed Execution"
    assert payload["execution_target"] == "sandbox"
    assert payload["project_default_run_mode"] == "trusted"
    assert payload["runtime_capability_run_mode"] == "trusted"
    assert payload["runtime_sandbox_required"] is True

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["sandbox"]["run_mode"] == "trusted"
