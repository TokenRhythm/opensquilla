from __future__ import annotations

import json
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from opensquilla.cli.main import app
from opensquilla.onboarding.config_store import load_config, persist_config

runner = CliRunner()


def _invoke(config_path: Path, *args: str):
    return runner.invoke(app, ["sandbox", *args, "--config", str(config_path)])


def test_sandbox_status_reports_default_full(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    result = runner.invoke(
        app,
        ["sandbox", "status", "--config", str(config_path), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_mode"] == "full"
    assert payload["run_mode_label"] == "Full Host Access"
    assert payload["execution_target"] == "host"
    assert payload["posture"] == "full"
    assert payload["sandbox"]["sandbox"] is False
    assert payload["sandbox"]["security_grading"] is False
    assert payload["permissions"]["default_mode"] == "off"
    assert payload["permissions"]["effective_mode"] == "full"
    assert payload["owner_execution_target"] == "host"
    assert payload["sandbox_required_for_owner_default"] is False
    assert payload["sandbox_backend_configured"] == "auto"
    assert payload["restart_required"] is False


def test_sandbox_trust_persists_safe_run_mode(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    result = _invoke(config_path, "trust")

    assert result.exit_code == 0, result.output
    cfg = load_config(config_path)
    assert cfg.sandbox.run_mode == "safe"
    assert cfg.sandbox.sandbox is True
    assert cfg.sandbox.security_grading is True
    assert cfg.sandbox.network_default == "proxy_allowlist"
    assert cfg.permissions.default_mode == "off"
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["sandbox"]["run_mode"] == "safe"
    # Sparse persistence omits values equal to the built-in defaults
    # (sandbox on, grading on, proxy allowlist, permissions off); the
    # effective posture is asserted via load_config above. Any value that
    # is written must match the trusted posture.
    assert data["sandbox"].get("sandbox", True) is True
    assert data["sandbox"].get("security_grading", True) is True
    assert data["sandbox"].get("network_default", "proxy_allowlist") == "proxy_allowlist"
    assert data.get("permissions", {}).get("default_mode", "off") == "off"
    assert "restart" in result.output.lower()


def test_sandbox_trust_repairs_legacy_disabled_network_default(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    full = _invoke(config_path, "full")
    assert full.exit_code == 0, full.output
    cfg = load_config(config_path)
    cfg.sandbox.network_default = "none"
    persist_config(cfg, path=config_path)

    result = _invoke(config_path, "trust")

    assert result.exit_code == 0, result.output
    cfg = load_config(config_path)
    assert cfg.sandbox.run_mode == "safe"
    assert cfg.sandbox.network_default == "proxy_allowlist"


def test_sandbox_bypass_restores_owner_full_with_restricted_runtime_available(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    before = _invoke(config_path, "on")
    assert before.exit_code == 0, before.output

    result = _invoke(config_path, "bypass")

    assert result.exit_code == 0, result.output
    cfg = load_config(config_path)
    assert cfg.sandbox.run_mode == "full"
    assert cfg.sandbox.sandbox is False
    assert cfg.sandbox.security_grading is False
    assert cfg.sandbox.network_default == "none"
    assert cfg.permissions.default_mode == "full"


def test_sandbox_full_and_on_are_reversible(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    full = _invoke(config_path, "full")
    assert full.exit_code == 0, full.output
    cfg = load_config(config_path)
    assert cfg.sandbox.run_mode == "full"
    assert cfg.sandbox.sandbox is False
    assert cfg.sandbox.security_grading is False
    assert cfg.sandbox.network_default == "none"
    assert cfg.permissions.default_mode == "full"

    on = _invoke(config_path, "on")
    assert on.exit_code == 0, on.output
    cfg = load_config(config_path)
    assert cfg.sandbox.run_mode == "safe"
    assert cfg.sandbox.sandbox is True
    assert cfg.sandbox.security_grading is True
    assert cfg.sandbox.network_default == "proxy_allowlist"
    assert cfg.permissions.default_mode == "off"


def test_sandbox_reset_restores_safe(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"

    full = _invoke(config_path, "full")
    reset = _invoke(config_path, "reset")

    assert full.exit_code == 0, full.output
    assert reset.exit_code == 0, reset.output
    cfg = load_config(config_path)
    assert cfg.sandbox.run_mode == "safe"
    assert cfg.sandbox.sandbox is True
    assert cfg.sandbox.security_grading is True
    assert cfg.sandbox.network_default == "proxy_allowlist"
    assert cfg.permissions.default_mode == "off"
