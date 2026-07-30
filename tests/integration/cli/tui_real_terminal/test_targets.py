from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tui_real_terminal.driver import TerminalSize
from tui_real_terminal.targets import PACKAGED_GATE_ENV, TargetContext, build_tui_target

REMOVED_TEXT_BACKEND = "text" + "ual"
REMOVED_BACKEND_IDS = ("terminal", REMOVED_TEXT_BACKEND, f"live-{REMOVED_TEXT_BACKEND}")


def test_removed_backend_targets_fail_clearly(tmp_path: Path) -> None:
    context = TargetContext(
        project_root=Path.cwd(),
        artifact_dir=tmp_path,
        scenario_id="launch_input_loop",
        size=TerminalSize(cols=100, rows=30),
    )

    for backend_id in REMOVED_BACKEND_IDS:
        with pytest.raises(ValueError, match="only opentui is supported"):
            build_tui_target(backend_id, context)


def test_opentui_target_builds_fake_footer_app_command(tmp_path: Path) -> None:
    context = TargetContext(
        project_root=Path.cwd(),
        artifact_dir=tmp_path,
        scenario_id="launch_input_loop",
        size=TerminalSize(cols=100, rows=30),
    )

    target = build_tui_target("opentui", context)

    assert target.backend_id == "opentui"
    assert target.available is True
    assert target.skip_reason is None
    assert target.command[:2] == [sys.executable, "-u"]
    assert target.command[2].endswith("fake_opentui_app.py")
    assert target.env["OPENSQUILLA_TUI_FAKE_SCENARIO"] == "launch_input_loop"
    assert target.env["OPENSQUILLA_TUI_READY_MARKER"] == "OPEN_SQUILLA_TUI_READY"
    assert target.env["OPENSQUILLA_TUI_BACKEND"] == "opentui"
    assert target.env["OPENSQUILLA_TUI_DEV_SOURCE_HOST"] == "1"
    assert target.readiness_markers == ("OPEN_SQUILLA_TUI_READY",)
    assert target.log_paths == (tmp_path / "opentui-app.log",)
    assert "opentui-footer" in target.capability_requirements


def test_live_opentui_target_builds_real_cli_command(tmp_path: Path) -> None:
    context = TargetContext(
        project_root=Path.cwd(),
        artifact_dir=tmp_path,
        scenario_id="live_architecture_prompt",
        size=TerminalSize(cols=112, rows=34),
    )

    target = build_tui_target("live-opentui", context)

    assert target.backend_id == "live-opentui"
    assert target.command[:3] == [sys.executable, "-u", "-m"]
    assert target.command[3:6] == ["opensquilla.cli.main", "chat", "--standalone"]
    assert "--ui" not in target.command
    assert target.command[6] == "--workspace"
    assert "--workspace" in target.command
    assert str(Path.cwd()) in target.command
    assert "--workspace-strict" in target.command
    assert "OPENSQUILLA_TUI_BACKEND" not in target.env
    assert target.env["OPENSQUILLA_TUI_READY_MARKER"] == "OPEN_SQUILLA_TUI_READY"
    assert "real-cli" in target.capability_requirements
    assert "tmux" in target.capability_requirements
    assert "fake-provider" not in target.capability_requirements


def test_live_opentui_target_preserves_user_config_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    user_config = home / ".opensquilla" / "config.toml"
    user_config.parent.mkdir(parents=True)
    user_config.write_text("[llm]\nprovider = 'openrouter'\n", encoding="utf-8")
    project_root = tmp_path / "project"
    project_root.mkdir()
    artifact_dir = tmp_path / "artifacts"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENSQUILLA_STATE_DIR", raising=False)
    context = TargetContext(
        project_root=project_root,
        artifact_dir=artifact_dir,
        scenario_id="live_architecture_prompt",
        size=TerminalSize(cols=112, rows=34),
    )

    target = build_tui_target("live-opentui", context)

    assert "OPENSQUILLA_STATE_DIR" not in target.env
    assert target.env["OPENSQUILLA_GATEWAY_CONFIG_PATH"] == str(user_config)


def test_packaged_gate_removes_source_resolution_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PACKAGED_GATE_ENV, "1")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "source"))
    monkeypatch.setenv("OPENSQUILLA_TUI_DEV_SOURCE_HOST", "1")
    monkeypatch.setenv("BUN_INSTALL", str(tmp_path / "bun"))
    context = TargetContext(
        project_root=Path.cwd(),
        artifact_dir=tmp_path / "artifacts",
        scenario_id="launch_input_loop",
        size=TerminalSize(cols=100, rows=30),
    )

    target = build_tui_target("opentui", context)

    assert "PYTHONPATH" not in target.env
    assert "OPENSQUILLA_TUI_DEV_SOURCE_HOST" not in target.env
    assert "BUN_INSTALL" not in target.env
