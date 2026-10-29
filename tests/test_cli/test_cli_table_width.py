"""CLI tests for terminal-width-aware table output.

Regression tests for the hardcoded ``Console(width=...)`` pins that made
every CLI table render wider than the user's terminal regardless of
``COLUMNS``. The tables must follow the terminal width instead.
"""

from __future__ import annotations

import re
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len
from rich.console import Console
from typer.testing import CliRunner

from opensquilla.cli import agents_cmd, channels_cmd, doctor_cmd, providers_cmd, search_cmd
from opensquilla.cli.main import app
from opensquilla.onboarding.provider_specs import list_provider_setup_specs

runner = CliRunner()

_HARDCODED_WIDTH = re.compile(r"Console\([^)]*width=\d+")


@pytest.fixture(params=[False, True], ids=["modern", "legacy-windows"])
def terminal_style(monkeypatch, request) -> None:
    # Exercise Windows' safe-box fallback even when running on Linux or macOS.
    for module in (agents_cmd, channels_cmd, doctor_cmd, providers_cmd, search_cmd):
        monkeypatch.setattr(module, "Console", partial(Console, legacy_windows=request.param))


def _assert_fits(output: str, width: int) -> None:
    assert output
    assert max(cell_len(line) for line in output.splitlines()) <= width


def _column_text(output: str, index: int) -> str:
    """Reassemble a displayed column across folded lines, ignoring whitespace."""
    cells = []
    in_body = False
    for line in output.splitlines():
        # Windows' safe box uses the same vertical border for header and body.
        # Start after the header divider in either box style.
        if line.startswith(("┡", "├")):
            in_body = True
        elif in_body and line.startswith("│"):
            cells.append("".join(line.split("│")[index + 1].split()))
    assert in_body, output
    return "".join(cells)


@pytest.mark.parametrize("width", [40, 80])
def test_providers_list_preserves_ids_within_terminal_width(
    tmp_path: Path, monkeypatch, terminal_style, width: int,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    monkeypatch.setenv("COLUMNS", str(width))
    result = runner.invoke(app, ["providers", "list"])
    assert result.exit_code == 0
    _assert_fits(result.stdout, width)
    assert _column_text(result.stdout, 0) == "".join(
        spec.provider_id for spec in list_provider_setup_specs()
    )


@pytest.mark.parametrize("width", [40, 80])
def test_agents_list_preserves_identifiers_and_workspace(
    monkeypatch, capsys, terminal_style, width: int,
) -> None:
    agent = {
        "id": "support-assistant",
        "name": "演示助手",
        "type": "custom",
        "enabled": True,
        "model": "example/model-with-a-long-name",
        "workspace": "/workspace/project-with-a-long-name",
    }

    async def list_agents(**_kwargs):
        return [agent]

    monkeypatch.setenv("COLUMNS", str(width))
    monkeypatch.setattr(
        agents_cmd, "_load_registry",
        lambda _path: (Path("config.toml"), None, SimpleNamespace(list_agents=list_agents)),
    )
    agents_cmd.agents_list(json_output=False, config_path=None)
    output = capsys.readouterr().out
    _assert_fits(output, width)
    for index, field in [(0, "id"), (1, "name"), (4, "model"), (5, "workspace")]:
        assert _column_text(output, index) == agent[field]


@pytest.mark.parametrize("width", [40, 80])
def test_pairings_preserve_complete_code(monkeypatch, capsys, terminal_style, width: int) -> None:
    monkeypatch.setenv("COLUMNS", str(width))
    channels_cmd._render_pairings_table([{
        "pairingCode": "abcdef12",
        "senderName": "Example User",
        "status": "pending",
        "createdAt": "2026-01-01T09:00:00+00:00",
        "approvedAt": None,
    }], channel="example")
    output = capsys.readouterr().out
    _assert_fits(output, width)
    assert _column_text(output, 0) == "abcdef12"
    assert _column_text(output, 1) == "ExampleUser"
    assert _column_text(output, 3) == "2026-01-01T09:00:00+00:00"


@pytest.mark.parametrize("width", [40, 80])
def test_doctor_preserves_complete_recovery_command(
    monkeypatch, capsys, terminal_style, width: int,
) -> None:
    command = (
        "opensquilla providers configure example --api-key YOUR_API_KEY "
        "--config /workspace/project-with-a-long-name/config.toml"
    )
    monkeypatch.setenv("COLUMNS", str(width))
    doctor_cmd._render_report({
        "summary": "Needs setup",
        "findings": [{
            "severity": "error",
            "surface": "provider",
            "impact": "blocks_ready",
            "title": "Provider is not configured",
            "detail": "The configured provider requires a key.",
            "fixSteps": [{"command": command}],
        }],
    })
    output = capsys.readouterr().out
    _assert_fits(output, width)
    assert _column_text(output, 4) == "".join(command.split())


@pytest.mark.parametrize("width", [40, 80])
def test_research_results_preserve_complete_url(
    monkeypatch, capsys, terminal_style, width: int,
) -> None:
    url = "https://example.com/research/a-long-result-path"
    monkeypatch.setenv("COLUMNS", str(width))
    search_cmd._print_research_results("example", {"results": [{
        "rank": 1,
        "title": "Example research result",
        "domain": "example.com",
        "provider": "example-search",
        "fetched": False,
        "url": url,
        "excerpt": "Example result summary.",
    }]})
    output = capsys.readouterr().out
    _assert_fits(output, width)
    assert _column_text(output, 3) == "example-search"
    assert _column_text(output, 5) == url


def test_cli_modules_have_no_hardcoded_console_width() -> None:
    cli_dir = Path(__file__).resolve().parents[2] / "src" / "opensquilla" / "cli"
    offenders = [
        str(path)
        for path in sorted(cli_dir.rglob("*.py"))
        if _HARDCODED_WIDTH.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
