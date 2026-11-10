"""A semantically invalid config.toml is reported as one readable error, not a traceback."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opensquilla.cli.main import app

runner = CliRunner()

SLACK_SOCKET_WITHOUT_APP_TOKEN = """\
[channels]
[[channels.channels]]
name = "ops-slack"
type = "slack"
token = "xoxb-placeholder"
connection_mode = "socket"
slack_channel_id = "C12345"
"""


@pytest.fixture
def invalid_config(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text(SLACK_SOCKET_WITHOUT_APP_TOKEN, encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(target))
    return target


@pytest.mark.parametrize(
    "argv",
    [
        ["channels", "list"],
        ["channels", "list", "--json"],
        ["channels", "enable", "ops-slack"],
        ["channels", "remove", "ops-slack"],
        ["agents", "list"],
        ["skills", "list"],
        ["gateway", "status"],
    ],
)
def test_invalid_config_value_exits_with_readable_error(invalid_config, argv):
    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert isinstance(result.exception, SystemExit)
    output = result.output
    assert "OpenSquilla config error" in output
    assert "slack socket channels require app_token" in output
    assert "opensquilla onboard --if-needed" in output
    assert output.count("OpenSquilla config error") == 1
    assert "Traceback" not in output
    assert "errors.pydantic.dev" not in output


@pytest.mark.parametrize("argv", [["skills", "list"], ["gateway", "status"]])
def test_invalid_implicit_config_is_reported_once(tmp_path, monkeypatch, argv):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "config.toml").write_text(SLACK_SOCKET_WITHOUT_APP_TOKEN, encoding="utf-8")
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENSQUILLA_GATEWAY_URL", raising=False)
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(state_dir))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, argv)

    assert result.exit_code == 2
    assert result.output.count("OpenSquilla config error") == 1
    assert "slack socket channels require app_token" in result.output
    assert "Traceback" not in result.output
