"""Tests for agent command no-key three-section error output."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import typer
from typer.testing import CliRunner

from opensquilla.cli.agent_cmd import AgentRunResult

# Provider env vars the project supports
_PROVIDER_ENV_VARS = [
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
]

_NO_KEY_RESULT = AgentRunResult(
    status="error",
    agent_id="main",
    session_key="agent:main:main",
    text="",
    usage={},
    errors=[{"message": "No provider available", "code": "no_provider"}],
)


def _make_app() -> typer.Typer:
    from opensquilla.cli.agent_cmd import run_agent_command

    app = typer.Typer()
    app.command()(run_agent_command)
    return app


def _mock_patch():
    return patch(
        "opensquilla.cli.agent_cmd.run_agent_once",
        new=AsyncMock(return_value=_NO_KEY_RESULT),
    )


def test_tty_message_contains_onboard_and_envvar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Error output contains 'opensquilla onboard' and at least one provider env var."""
    for key in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    runner = CliRunner()
    app = _make_app()

    with _mock_patch():
        result = runner.invoke(app, ["-m", "hi"])

    combined = result.output or ""
    assert "opensquilla onboard" in combined
    assert any(var in combined for var in _PROVIDER_ENV_VARS)


def test_no_color_env_strips_ansi_but_keeps_three_sections(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With NO_COLOR=1 output has no ANSI codes but still shows all three section labels."""
    for key in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    runner = CliRunner()
    app = _make_app()

    with _mock_patch():
        result = runner.invoke(app, ["-m", "hi"])

    combined = result.output or ""

    # No ANSI escape sequences when NO_COLOR is set
    assert not re.search(r"\x1b\[", combined), "ANSI escape codes found with NO_COLOR=1"

    # All three section labels must be present
    assert "Symptom" in combined
    assert "Cause" in combined
    assert "Next" in combined


def test_term_dumb_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """TERM=dumb environment: command completes without raising an unexpected exception."""
    for key in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    runner = CliRunner()
    app = _make_app()

    with _mock_patch():
        result = runner.invoke(app, ["-m", "hi"])

    # No unexpected exception: only SystemExit or clean exit allowed
    assert result.exception is None or isinstance(result.exception, SystemExit)

    combined = result.output or ""
    assert len(combined) > 0, "Expected some output even under TERM=dumb"


def test_exit_code_nonzero_when_no_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exit code is non-zero when no provider key is available."""
    for key in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    runner = CliRunner()
    app = _make_app()

    with _mock_patch():
        result = runner.invoke(app, ["-m", "hi"])

    assert result.exit_code != 0


def _flattened_output(output: str) -> str:
    """Drop panel borders and whitespace so a long path that Rich wrapped still matches."""
    return re.sub(r"[│\s]", "", output)


def test_panel_names_the_state_dir_config_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With OPENSQUILLA_STATE_DIR set, the panel points at the config file the CLI reads."""
    for key in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    state_dir = tmp_path / "state"
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(state_dir))

    with _mock_patch():
        result = CliRunner().invoke(_make_app(), ["-m", "hi"])

    combined = _flattened_output(result.output or "")
    assert str(state_dir / "config.toml").replace(" ", "") in combined
    assert "~/.opensquilla/config.toml" not in combined


def test_panel_names_the_explicit_config_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With OPENSQUILLA_GATEWAY_CONFIG_PATH set, the panel points at that file."""
    for key in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    config_file = tmp_path / "custom" / "my-config.toml"
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(config_file))

    with _mock_patch():
        result = CliRunner().invoke(_make_app(), ["-m", "hi"])

    combined = _flattened_output(result.output or "")
    assert str(config_file).replace(" ", "") in combined
    assert "~/.opensquilla/config.toml" not in combined
