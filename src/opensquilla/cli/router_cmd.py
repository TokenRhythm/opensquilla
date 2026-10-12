"""``opensquilla router ...`` subcommand tree.

Currently exposes ``calibrate`` — the on-device router calibration job as a
one-shot CLI. It reads local, prompt-free decision records (the V017
``router_decisions`` table inside ``sessions.db``), runs the same pure
aggregation the 24h in-process job uses, and writes the clamped adjustment to
``<state>/router_calibration.json``. Offline and deterministic; never touches
the router savings/cost math.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import typer

from opensquilla.engine.routing.calibration import (
    CalibrationState,
    aggregate_calibration,
    calibration_path,
    load_calibration,
    save_calibration,
)
from opensquilla.engine.routing.calibration_service import collect_decision_records
from opensquilla.paths import state_dir
from opensquilla.persistence.router_decision_writer import open_router_decision_writer

router_app = typer.Typer(help="Router calibration and inspection.")


def _resolve_decisions_db_path() -> str:
    """Resolve the ``sessions.db`` holding the V017 ``router_decisions`` table.

    Resolution order mirrors ``opensquilla skills meta`` so the CLI reads the
    same rows the running gateway writes:

      1. ``OPENSQUILLA_ROUTER_DECISIONS_DB`` env var (explicit override)
      2. ``GatewayConfig.state_dir`` / ``sessions.db``
      3. ``~/.opensquilla/state/sessions.db`` (built-in default)
    """
    env = os.environ.get("OPENSQUILLA_ROUTER_DECISIONS_DB", "").strip()
    if env:
        return env
    try:
        from opensquilla.gateway.config import GatewayConfig

        config_path_env = os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip()
        cfg = GatewayConfig.load(config_path_env or None)
        configured = (cfg.state_dir or "").strip()
        if configured:
            return os.path.join(configured, "sessions.db")
    except Exception:  # noqa: BLE001 — fall back to default on any load failure
        pass
    return str(state_dir("sessions.db"))


def _read_records(max_records: int) -> list[dict]:
    """Gather decision records; a missing DB yields an empty list (neutral)."""
    db_path = _resolve_decisions_db_path()
    if db_path != ":memory:" and not Path(db_path).exists():
        return []
    writer = open_router_decision_writer(db_path)
    try:
        return collect_decision_records(writer, max_records=max_records)
    finally:
        writer.close()


def _print_state(state: CalibrationState, *, path: Path | None, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "wrote": path is not None,
                    "path": str(path) if path is not None else None,
                    "calibration": state.to_dict(),
                },
                sort_keys=True,
                indent=2,
            )
        )
        return
    typer.echo(f"samples:          {state.sample_count}")
    typer.echo(f"threshold_adjust: {state.threshold_adjust:+.4f}")
    if state.per_class_bias:
        typer.echo("per_class_bias:")
        for tier in sorted(state.per_class_bias):
            typer.echo(f"  {tier}: {state.per_class_bias[tier]:+.4f}")
    else:
        typer.echo("per_class_bias:   (none)")
    if path is not None:
        typer.echo(f"wrote:            {path}")
    else:
        typer.echo("wrote:            (dry-run, not written)")


@router_app.command("calibrate")
def calibrate(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compute and print the calibration without writing the file."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the calibration state as JSON instead of a summary."
    ),
    max_records: int = typer.Option(
        5000, "--max-records", min=1, help="Maximum decision records to read."
    ),
) -> None:
    """Recompute the router calibration adjustment from local decision records.

    Offline and deterministic. Blends the existing calibration file as a prior
    for run-to-run stability. Adjustments are hard-clamped
    (``|per_class_bias| <= 0.15``; effective threshold in ``[0.3, 0.7]``).
    """
    records = _read_records(max_records)
    now = int(time.time() * 1000)
    prior = load_calibration()
    state = aggregate_calibration(records, now=now, prior=prior)
    if dry_run:
        _print_state(state, path=None, json_output=json_output)
        return
    written_path = save_calibration(state)
    _print_state(state, path=written_path, json_output=json_output)


@router_app.command("calibration-show")
def calibration_show(
    json_output: bool = typer.Option(
        False, "--json", help="Emit the calibration state as JSON instead of a summary."
    ),
) -> None:
    """Print the active calibration state (neutral if no file exists)."""
    state = load_calibration()
    path = calibration_path()
    _print_state(state, path=path if path.exists() else None, json_output=json_output)
