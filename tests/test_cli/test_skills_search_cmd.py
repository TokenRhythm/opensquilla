from __future__ import annotations

import json

import click
import pytest
from typer.testing import CliRunner

from opensquilla.cli.main import app
from opensquilla.skills.hub.contracts import (
    DiagnosticPhase,
    DiagnosticSeverity,
    SkillDiagnostic,
)
from opensquilla.skills.hub.router import SourceSearchReport
from opensquilla.skills.hub.source import SkillMeta

_RESULT = SkillMeta(
    name="plotter",
    description="Plot charts",
    source_id="healthy",
    identifier="plotter",
)
_DIAGNOSTIC = SkillDiagnostic(
    code="SOURCE_RATE_LIMITED",
    severity=DiagnosticSeverity.ERROR,
    phase=DiagnosticPhase.SOURCE,
    message="The source rate-limited this search.",
    blocking=True,
    details={"source": "limited", "retryAfter": "30"},
)


def _report(kind: str) -> SourceSearchReport:
    if kind == "healthy":
        return SourceSearchReport(
            results=(_RESULT,),
            searched_sources=("healthy",),
            successful_sources=("healthy",),
        )
    if kind == "partial":
        return SourceSearchReport(
            results=(_RESULT,),
            diagnostics=(_DIAGNOSTIC,),
            searched_sources=("healthy", "limited"),
            successful_sources=("healthy",),
        )
    assert kind == "unavailable"
    return SourceSearchReport(
        diagnostics=(_DIAGNOSTIC,),
        searched_sources=("limited",),
    )


def _install_router(monkeypatch: pytest.MonkeyPatch, report: SourceSearchReport) -> None:
    class StaticRouter:
        async def search_with_diagnostics(
            self,
            query: str,
            limit: int = 20,
            source_id: str | None = None,
        ) -> SourceSearchReport:
            assert (query, limit, source_id) == ("plot", 20, None)
            return report

    monkeypatch.setattr(
        "opensquilla.skills.hub.defaults.get_default_skill_router",
        lambda: StaticRouter(),
    )


@pytest.mark.parametrize("kind", ["healthy", "partial", "unavailable"])
def test_skills_search_json_always_preserves_historical_top_level_list(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    _install_router(monkeypatch, _report(kind))

    result = CliRunner().invoke(app, ["skills", "search", "plot", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert [row["name"] for row in payload] == (
        ["plotter"] if kind != "unavailable" else []
    )


@pytest.mark.parametrize("kind", ["healthy", "partial", "unavailable"])
def test_skills_search_json_diagnostic_envelope_is_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    report = _report(kind)
    _install_router(monkeypatch, report)

    result = CliRunner().invoke(
        app,
        ["skills", "search", "plot", "--json", "--include-diagnostics"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [row["name"] for row in payload["results"]] == (
        ["plotter"] if kind != "unavailable" else []
    )
    assert payload["diagnostics"] == [
        item.to_dict() for item in report.diagnostics
    ]
    assert payload["partial"] is (kind == "partial")
    assert payload["allSourcesUnavailable"] is (kind == "unavailable")


def test_skills_search_include_diagnostics_help_and_json_requirement() -> None:
    runner = CliRunner()

    help_result = runner.invoke(app, ["skills", "search", "--help"])
    invalid_result = runner.invoke(
        app,
        ["skills", "search", "plot", "--include-diagnostics"],
    )

    assert help_result.exit_code == 0, help_result.output
    help_output = click.unstyle(help_result.output)
    invalid_output = click.unstyle(invalid_result.output)
    assert "--include-diagnostics" in help_output
    assert "wrap results and source diagnostics" in help_output
    assert invalid_result.exit_code != 0
    assert "--include-diagnostics requires --json" in invalid_output


def test_skills_search_human_does_not_call_source_failure_zero_results(monkeypatch) -> None:
    diagnostic = SkillDiagnostic(
        code="SOURCE_INVALID_RESPONSE",
        severity=DiagnosticSeverity.ERROR,
        phase=DiagnosticPhase.SOURCE,
        message="The source returned an invalid response.",
        blocking=True,
        details={"source": "malformed"},
    )

    class FailedRouter:
        async def search_with_diagnostics(
            self,
            query: str,
            limit: int = 20,
            source_id: str | None = None,
        ) -> SourceSearchReport:
            return SourceSearchReport(
                diagnostics=(diagnostic,),
                searched_sources=("malformed",),
            )

    monkeypatch.setattr(
        "opensquilla.skills.hub.defaults.get_default_skill_router",
        lambda: FailedRouter(),
    )

    result = CliRunner().invoke(app, ["skills", "search", "plot"])

    assert result.exit_code == 0, result.output
    assert "SOURCE_INVALID_RESPONSE" in result.stdout
    assert "No results" not in result.stdout
