"""CLI tests for terminal-width-aware table output.

Regression tests for the hardcoded ``Console(width=...)`` pins that made
every CLI table render wider than the user's terminal regardless of
``COLUMNS``. The tables must follow the terminal width instead.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from opensquilla.cli.main import app

runner = CliRunner()

_HARDCODED_WIDTH = re.compile(r"Console\([^)]*width=\d+")


def test_providers_list_respects_columns_width(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    monkeypatch.setenv("COLUMNS", "80")
    result = runner.invoke(app, ["providers", "list"])
    assert result.exit_code == 0
    assert result.stdout
    assert max(len(line) for line in result.stdout.splitlines()) <= 80


def test_providers_list_respects_narrow_columns_width(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_GATEWAY_CONFIG_PATH", str(tmp_path / "c.toml"))
    monkeypatch.setenv("COLUMNS", "40")
    result = runner.invoke(app, ["providers", "list"])
    assert result.exit_code == 0
    assert result.stdout
    assert max(len(line) for line in result.stdout.splitlines()) <= 40


def test_cli_modules_have_no_hardcoded_console_width() -> None:
    cli_dir = Path(__file__).resolve().parents[2] / "src" / "opensquilla" / "cli"
    offenders = [
        str(path)
        for path in sorted(cli_dir.rglob("*.py"))
        if _HARDCODED_WIDTH.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
