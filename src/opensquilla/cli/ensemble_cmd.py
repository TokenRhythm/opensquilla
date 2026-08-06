"""Ensemble benchmark CLI commands (measurement only).

``opensquilla ensemble bench`` runs a set of prompts through the ensemble and a
single-model baseline and reports latency / success / cost so a maintainer can
decide whether the ensemble earns its cost. It changes no runtime behavior and
touches no ensemble internals: it drives the ``eval`` harness, which times the
providers' public ``chat`` surface as a black box.

The ``--dry-run`` mode is fully offline — it drives synthetic providers through
a scripted ``FailureInjector``, needs no credentials, and is what the default
test suite exercises. Without it the command is live and builds the configured
ensemble and baseline from config.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.table import Table

from opensquilla.cli.output import emit_error, print_json
from opensquilla.cli.ui import ACCENT_HEADER, console, markup_escape
from opensquilla.eval import (
    ArmReport,
    BenchmarkPrompt,
    BenchmarkReport,
    default_synthetic_prompts,
    run_config_benchmark,
    run_dry_run_benchmark,
)

ensemble_app = typer.Typer(help="Ensemble measurement and benchmark commands.")


def _load_prompts(path: Path) -> list[BenchmarkPrompt]:
    """Load a benchmark prompt set from a JSON array of {id, text, system?}."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("prompts file must be a JSON array of objects")
    prompts: list[BenchmarkPrompt] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"prompt #{index} must be an object")
        text = str(entry.get("text") or "").strip()
        if not text:
            raise ValueError(f"prompt #{index} is missing non-empty 'text'")
        prompts.append(
            BenchmarkPrompt(
                id=str(entry.get("id") or f"prompt-{index}"),
                text=text,
                system=(str(entry["system"]) if entry.get("system") else None),
            )
        )
    if not prompts:
        raise ValueError("prompts file defined no prompts")
    return prompts


def _format_cost(value: float | None) -> str:
    return f"${value:.6f}" if value is not None else "-"


def _render_arm_row(table: Table, arm: ArmReport) -> None:
    proposers = (
        f"{arm.mean_successful_proposers:.1f}/{arm.mean_total_candidates:.1f}"
        if arm.mean_successful_proposers is not None
        and arm.mean_total_candidates is not None
        else "-"
    )
    table.add_row(
        markup_escape(arm.label),
        str(arm.runs),
        f"{arm.success_rate * 100:.0f}%",
        f"{arm.mean_latency_s:.3f}s",
        f"{arm.p95_latency_s:.3f}s",
        _format_cost(arm.total_estimated_cost_usd),
        proposers,
    )


def _render_report(report: BenchmarkReport) -> None:
    table = Table(title="Ensemble benchmark", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Arm")
    table.add_column("Runs", justify="right")
    table.add_column("Success", justify="right")
    table.add_column("Mean lat.", justify="right")
    table.add_column("p95 lat.", justify="right")
    table.add_column("Est. cost", justify="right")
    table.add_column("Proposers", justify="right")
    _render_arm_row(table, report.ensemble)
    _render_arm_row(table, report.baseline)
    console.print(table)

    latency_ratio = (
        f"{report.latency_ratio:.2f}x" if report.latency_ratio is not None else "-"
    )
    cost_ratio = (
        f"{report.estimated_cost_ratio:.2f}x"
        if report.estimated_cost_ratio is not None
        else "-"
    )
    console.print(
        "Ensemble vs baseline: "
        f"latency {report.latency_delta_s:+.3f}s ({latency_ratio}), "
        f"success {report.success_rate_delta * 100:+.0f}%, "
        f"est. cost {_format_cost(report.estimated_cost_delta_usd)} ({cost_ratio})"
    )


@ensemble_app.command("bench")
def ensemble_bench(
    prompts_path: Path | None = typer.Option(
        None, "--prompts", help="JSON array of {id, text, system?}; omit for built-in set."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Offline mode: scripted synthetic providers, no credentials."
    ),
    repeat: int = typer.Option(1, "--repeat", min=1, help="Runs per prompt per arm."),
    cost: bool = typer.Option(True, "--cost/--no-cost", help="Include the pricing cost estimate."),
    baseline_model: str | None = typer.Option(
        None, "--baseline-model", help="Override the single-model baseline (live mode)."
    ),
    max_tokens: int = typer.Option(512, "--max-tokens", min=1, help="Per-run max tokens (live)."),
    timeout: float = typer.Option(120.0, "--timeout", help="Per-run timeout seconds (live)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path (live)."),
) -> None:
    """Benchmark the ensemble against a single-model baseline (measurement only).

    Use ``--dry-run`` for an offline demonstration with scripted synthetic
    outcomes. Without it, the command is live: it builds the configured ensemble
    and a single-model baseline and runs the prompts through both.
    """
    try:
        prompts = _load_prompts(prompts_path) if prompts_path else default_synthetic_prompts()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        emit_error(f"Invalid prompts file: {exc}", json_output=json_output, code="invalid_prompts")
        raise typer.Exit(code=2) from exc

    try:
        if dry_run:
            report = asyncio.run(
                run_dry_run_benchmark(prompts=prompts, repeat=repeat, cost=cost)
            )
        else:
            # Config loading is a CLI concern; the eval harness owns provider coupling.
            from opensquilla.onboarding.config_store import load_config
            from opensquilla.provider.tokenrhythm_correlation import (
                prewarm_tokenrhythm_install_id,
            )

            config = load_config(config_path)
            prewarm_tokenrhythm_install_id(config=config)
            report = asyncio.run(
                run_config_benchmark(
                    prompts=prompts,
                    config=config,
                    baseline_model=baseline_model,
                    repeat=repeat,
                    cost=cost,
                    max_tokens=max_tokens,
                    timeout=timeout,
                )
            )
    except ValueError as exc:
        emit_error(str(exc), json_output=json_output, code="invalid_config")
        raise typer.Exit(code=2) from exc

    if json_output:
        print_json(report.to_dict())
    else:
        _render_report(report)
