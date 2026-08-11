from __future__ import annotations

import sys
from pathlib import Path

import pytest

from opensquilla.cli.tui.opentui import bridge as opentui_bridge
from opensquilla.cli.tui.renderers.selection import (
    RendererBackendAvailability,
    RendererBackendUnavailableReason,
)
from tui_real_terminal import targets as targets_module
from tui_real_terminal.driver import TerminalSize
from tui_real_terminal.targets import (
    PACKAGED_GATE_ENV,
    TargetContext,
    build_tui_target,
    opentui_host_capability_gate,
    opentui_host_skip_reason,
)

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


def test_opentui_host_probe_uses_source_switch_and_path_from_target_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_which(command: str, *, path: str | None = None) -> str:
        calls["which"] = (command, path)
        return "/target/bin/bun"

    def fake_check(
        *,
        runtime_bin: str | None = None,
        use_source_host: bool | None = None,
    ) -> RendererBackendAvailability:
        calls["check"] = (runtime_bin, use_source_host)
        return RendererBackendAvailability(available=True)

    monkeypatch.setattr(targets_module.shutil, "which", fake_which)
    monkeypatch.setattr(opentui_bridge, "check_opentui_host_available", fake_check)

    reason = opentui_host_skip_reason(
        {
            "OPENSQUILLA_TUI_DEV_SOURCE_HOST": "1",
            "PATH": "/target/bin",
        }
    )

    assert reason is None
    assert calls == {
        "which": ("bun", "/target/bin"),
        "check": ("/target/bin/bun", True),
    }


def test_opentui_host_probe_returns_expected_missing_host_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_check(**_kwargs: object) -> RendererBackendAvailability:
        calls.append(_kwargs)
        return RendererBackendAvailability(
            available=False,
            reason="OpenTUI companion is not installed",
            reason_code=RendererBackendUnavailableReason.MISSING,
        )

    monkeypatch.setattr(opentui_bridge, "check_opentui_host_available", fake_check)

    assert opentui_host_skip_reason({}) == "OpenTUI companion is not installed"
    assert calls == [{"runtime_bin": None, "use_source_host": False}]


def test_opentui_host_probe_uses_target_path_for_missing_bun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_which(command: str, *, path: str | None = None) -> None:
        calls.append((command, path))

    monkeypatch.setattr(targets_module.shutil, "which", fake_which)

    reason = opentui_host_skip_reason(
        {
            "OPENSQUILLA_TUI_DEV_SOURCE_HOST": "1",
            "PATH": "/target/without-bun",
        }
    )

    assert reason == "Bun is not installed or is not on PATH"
    assert calls == [("bun", "/target/without-bun")]


def test_opentui_host_probe_does_not_hide_version_regressions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_check(**_kwargs: object) -> RendererBackendAvailability:
        return RendererBackendAvailability(
            available=False,
            reason="host version mismatch",
            reason_code=RendererBackendUnavailableReason.VERSION_MISMATCH,
        )

    monkeypatch.setattr(opentui_bridge, "check_opentui_host_available", fake_check)

    with pytest.raises(AssertionError, match="version_mismatch.*host version mismatch"):
        opentui_host_skip_reason({})


def test_opentui_host_probe_does_not_hide_unexpected_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_check(**_kwargs: object) -> RendererBackendAvailability:
        raise RuntimeError("probe implementation broke")

    monkeypatch.setattr(opentui_bridge, "check_opentui_host_available", broken_check)

    with pytest.raises(RuntimeError, match="probe implementation broke"):
        opentui_host_skip_reason({})


def test_opentui_host_probe_does_not_hide_missing_source_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(opentui_bridge, "DEFAULT_HOST_PACKAGE_DIR", tmp_path)

    with pytest.raises(AssertionError, match="source host entrypoint is missing"):
        opentui_host_skip_reason({"OPENSQUILLA_TUI_DEV_SOURCE_HOST": "1"})


def test_opentui_host_capability_gate_skips_optional_missing_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        targets_module,
        "opentui_host_skip_reason",
        lambda _env: "OpenTUI host is missing",
    )

    with pytest.raises(pytest.skip.Exception, match="OpenTUI host is missing"):
        opentui_host_capability_gate({}, require_capabilities=False)


def test_opentui_host_capability_gate_fails_required_missing_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        targets_module,
        "opentui_host_skip_reason",
        lambda _env: "OpenTUI host is missing",
    )

    with pytest.raises(
        pytest.fail.Exception,
        match="required real-terminal capability is unavailable: OpenTUI host is missing",
    ):
        opentui_host_capability_gate({}, require_capabilities=True)
