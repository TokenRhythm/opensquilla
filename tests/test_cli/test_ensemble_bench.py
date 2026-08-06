"""CLI tests for `opensquilla ensemble bench` (offline dry-run only).

The dry-run path drives synthetic providers through a scripted FailureInjector,
so these tests never touch the network. The live path is exercised only under a
live invocation and is not covered here.
"""

from __future__ import annotations

import inspect
import json
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from opensquilla.cli import ensemble_cmd
from opensquilla.cli.main import app

runner = CliRunner()


def test_live_bench_prewarms_install_id_after_loading_config() -> None:
    source = inspect.getsource(ensemble_cmd.ensemble_bench)

    config_load = source.index("config = load_config(config_path)")
    prewarm = source.index("prewarm_tokenrhythm_install_id(config=config)")
    benchmark = source.index("run_config_benchmark(")

    assert config_load < prewarm < benchmark


def test_bench_dry_run_json_shape() -> None:
    result = runner.invoke(app, ["ensemble", "bench", "--dry-run", "--json"])

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert set(report) == {"ensemble", "baseline", "deltas"}

    ens = report["ensemble"]
    for key in (
        "label",
        "runs",
        "successes",
        "failures",
        "success_rate",
        "mean_latency_s",
        "p95_latency_s",
        "total_estimated_cost_usd",
        "failure_kinds",
        "mean_successful_proposers",
        "outcomes",
    ):
        assert key in ens, key
    # 5 built-in prompts, repeat=1.
    assert ens["runs"] == 5
    assert len(ens["outcomes"]) == 5
    # The ensemble arm reports public-trace proposer counts; baseline does not.
    assert ens["mean_successful_proposers"] == 3.0
    assert report["baseline"]["mean_successful_proposers"] is None
    # Scripted failures: ensemble 1 overload, baseline 2 rate-limits.
    assert ens["failures"] == 1
    assert report["baseline"]["failures"] == 2
    # Cost estimate resolved offline from the static pricing table.
    assert ens["total_estimated_cost_usd"] is not None
    assert "deltas" in report and "latency_delta_s" in report["deltas"]


def test_bench_dry_run_table_output() -> None:
    result = runner.invoke(app, ["ensemble", "bench", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Ensemble benchmark" in result.output
    assert "ensemble" in result.output
    assert "baseline" in result.output


def test_bench_dry_run_repeat_scales_runs() -> None:
    result = runner.invoke(app, ["ensemble", "bench", "--dry-run", "--repeat", "3", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["ensemble"]["runs"] == 15  # 5 prompts * 3


def test_bench_dry_run_no_cost_disables_estimate() -> None:
    result = runner.invoke(app, ["ensemble", "bench", "--dry-run", "--no-cost", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["ensemble"]["total_estimated_cost_usd"] is None


def test_bench_dry_run_custom_prompts_file(tmp_path: Path) -> None:
    prompts_file = tmp_path / "prompts.json"
    prompts_file.write_text(
        json.dumps(
            [
                {"id": "a", "text": "dummy one"},
                {"id": "b", "text": "dummy two", "system": "be terse"},
            ]
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        ["ensemble", "bench", "--dry-run", "--prompts", str(prompts_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["ensemble"]["runs"] == 2
    ids = {outcome["prompt_id"] for outcome in report["ensemble"]["outcomes"]}
    assert ids == {"a", "b"}


def test_bench_invalid_prompts_file_exits_two(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(textwrap.dedent('{"not": "a list"}'), encoding="utf-8")
    result = runner.invoke(app, ["ensemble", "bench", "--dry-run", "--prompts", str(bad)])
    assert result.exit_code == 2
    combined = result.output + (result.stderr or "")
    assert "Invalid prompts file" in combined
