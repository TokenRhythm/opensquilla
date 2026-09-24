"""Packaged runtime capability diagnostics remain available without task runners."""

import pytest
from typer.testing import CliRunner

from opensquilla.cli import diagnostics_cmd
from opensquilla.cli.diagnostics_cmd import diagnostics_app

runner = CliRunner()


def test_smoke_imports_accepts_explicit_modules():
    result = runner.invoke(diagnostics_app, ["smoke-imports", "--module", "json"])
    assert result.exit_code == 0, result.output
    assert '"success": true' in result.stdout
    assert '"json"' in result.stdout


def test_smoke_imports_fails_on_missing_module():
    result = runner.invoke(
        diagnostics_app,
        ["smoke-imports", "--module", "opensquilla_missing_smoke_module_x"],
    )
    assert result.exit_code == 1
    assert '"success": false' in result.stdout
    assert "opensquilla_missing_smoke_module_x" in result.stdout


def test_smoke_router_reports_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        diagnostics_cmd,
        "_smoke_router_runtime",
        lambda: {
            "success": True,
            "available": True,
            "tier": "c1",
            "confidence": 0.88,
            "source": "v4_phase3",
            "route_class": "R1",
            "model_version": "test",
        },
    )

    result = runner.invoke(diagnostics_app, ["smoke-router"])

    assert result.exit_code == 0, result.output
    assert '"success": true' in result.stdout
    assert '"source": "v4_phase3"' in result.stdout


def test_smoke_router_fails_when_runtime_unavailable(monkeypatch: pytest.MonkeyPatch):
    def fail() -> dict[str, object]:
        raise RuntimeError("failed to initialize V4 Phase 3 router: No module named 'sklearn'")

    monkeypatch.setattr(diagnostics_cmd, "_smoke_router_runtime", fail)

    result = runner.invoke(diagnostics_app, ["smoke-router"])

    assert result.exit_code == 1
    assert '"success": false' in result.stdout
    assert "No module named" in result.stdout
