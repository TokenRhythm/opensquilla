"""``opensquilla router ...`` calibration and rollout operations.

Calibration reads local, prompt-free decision records and writes the bounded
on-device adjustment. Canary commands inspect or reset one exact persistent
rollout scope; reset remains offline-only and holds the gateway process lock.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer

from opensquilla.canary_rollout import (
    CanaryRolloutLedger,
    CanaryRolloutRole,
    CanaryRolloutScope,
    CanaryRolloutSnapshot,
    CanaryRolloutState,
    canary_rollout_policy_sha256,
)
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
from opensquilla.recovery.locking import (
    acquire_gateway_legacy_lease,
    release_gateway_legacy_lease,
)

router_app = typer.Typer(help="Router calibration and canary rollout operations.")


def _load_canary_admin_config() -> Any:
    from opensquilla.gateway.config import GatewayConfig

    config_path = os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip()
    return GatewayConfig.load(config_path or None)


def _canary_admin_target(
    *,
    role: str,
    provider: str,
    model: str,
    upstream: str,
    policy_sha256: str | None,
) -> tuple[Path, CanaryRolloutScope]:
    config = _load_canary_admin_config()
    raw_state_dir = str(getattr(config, "state_dir", "") or "").strip()
    if not raw_state_dir:
        raise typer.BadParameter("Gateway state_dir is required")
    try:
        state_root = Path(raw_state_dir).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise typer.BadParameter("Gateway state_dir cannot be resolved") from exc
    normalized_policy_sha256 = str(policy_sha256 or "").strip()
    if normalized_policy_sha256:
        if (
            len(normalized_policy_sha256) != 64
            or normalized_policy_sha256 != normalized_policy_sha256.lower()
            or any(character not in "0123456789abcdef" for character in normalized_policy_sha256)
        ):
            raise typer.BadParameter("--policy-sha256 must be lowercase SHA-256 hex")
    else:
        rollout = getattr(getattr(config, "llm_ensemble", None), "canary_rollout", None)
        auto_rollback = getattr(rollout, "auto_rollback", None)
        if getattr(auto_rollback, "enabled", False) is not True:
            raise typer.BadParameter(
                "Current config does not enable persistent canary auto rollback; "
                "supply --policy-sha256 for a historical scope"
            )
        dump = getattr(rollout, "model_dump", None)
        raw_policy = dump(mode="json") if callable(dump) else None
        if not isinstance(raw_policy, dict):
            raise typer.BadParameter("Current canary rollout policy is unavailable")
        normalized_policy_sha256 = canary_rollout_policy_sha256(raw_policy)
    try:
        normalized_role = CanaryRolloutRole(str(role or "").strip().casefold())
        scope = CanaryRolloutScope.from_identity(
            policy_sha256=normalized_policy_sha256,
            role=normalized_role,
            provider=provider,
            model=model,
            upstream=upstream,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return state_root, scope


def _canary_snapshot_payload(
    scope: CanaryRolloutScope,
    snapshot: CanaryRolloutSnapshot,
) -> dict[str, Any]:
    payload = asdict(snapshot)
    payload["state"] = snapshot.state.value
    payload["latch_reason"] = (
        snapshot.latch_reason.value if snapshot.latch_reason is not None else None
    )
    payload.update(
        {
            "role": scope.role.value,
            "policy_sha256_prefix": scope.policy_sha256[:12],
            "deployment_sha256_prefix": scope.deployment_sha256[:12],
        }
    )
    return payload


def _print_canary_snapshot(
    scope: CanaryRolloutScope,
    snapshot: CanaryRolloutSnapshot,
    *,
    json_output: bool,
) -> None:
    payload = _canary_snapshot_payload(scope, snapshot)
    if json_output:
        typer.echo(json.dumps(payload, sort_keys=True, indent=2))
        return
    typer.echo(f"available:          {str(snapshot.available).lower()}")
    typer.echo(f"found:              {str(snapshot.found).lower()}")
    typer.echo(f"state:              {snapshot.state.value}")
    typer.echo(
        "latch_reason:       "
        + (snapshot.latch_reason.value if snapshot.latch_reason is not None else "none")
    )
    typer.echo(f"window_attempts:    {snapshot.window_attempts}")
    typer.echo(f"recovery_successes: {snapshot.recovery_successes}")
    typer.echo(f"role:               {scope.role.value}")
    typer.echo(f"policy:             {scope.policy_sha256[:12]}")
    typer.echo(f"deployment:         {scope.deployment_sha256[:12]}")


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


@router_app.command("canary-rollout-status")
def canary_rollout_status(
    provider: str = typer.Option(..., "--provider", help="Exact provider id."),
    model: str = typer.Option(..., "--model", help="Exact model id."),
    role: str = typer.Option("proposer", "--role", help="proposer or aggregator."),
    upstream: str = typer.Option("", "--upstream", help="Canonical upstream discriminator."),
    policy_sha256: str | None = typer.Option(
        None,
        "--policy-sha256",
        help="Historical policy hash; current configured policy is the default.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Inspect one exact persistent canary rollout scope."""

    state_root, scope = _canary_admin_target(
        role=role,
        provider=provider,
        model=model,
        upstream=upstream,
        policy_sha256=policy_sha256,
    )
    database = CanaryRolloutLedger.default_path(state_root)
    try:
        database.lstat()
    except FileNotFoundError:
        snapshot = CanaryRolloutSnapshot(
            available=True,
            found=False,
            state=CanaryRolloutState.ACTIVE,
        )
    except OSError as exc:
        typer.echo(f"Canary rollout ledger is unavailable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    else:
        try:
            snapshot = CanaryRolloutLedger(database).admin_snapshot(scope)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            typer.echo(f"Canary rollout ledger is unavailable: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    _print_canary_snapshot(scope, snapshot, json_output=json_output)
    if not snapshot.available:
        raise typer.Exit(code=1)


@router_app.command("canary-rollout-reset")
def canary_rollout_reset(
    provider: str = typer.Option(..., "--provider", help="Exact provider id."),
    model: str = typer.Option(..., "--model", help="Exact model id."),
    role: str = typer.Option("proposer", "--role", help="proposer or aggregator."),
    upstream: str = typer.Option("", "--upstream", help="Canonical upstream discriminator."),
    policy_sha256: str | None = typer.Option(
        None,
        "--policy-sha256",
        help="Historical policy hash; current configured policy is the default.",
    ),
    yes: bool = typer.Option(False, "--yes", help="Confirm the exact offline reset."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Reset one exact scope while holding the gateway's process lock."""

    state_root, scope = _canary_admin_target(
        role=role,
        provider=provider,
        model=model,
        upstream=upstream,
        policy_sha256=policy_sha256,
    )
    if not yes and not typer.confirm(
        "Reset this exact canary scope? The gateway must remain stopped"
    ):
        raise typer.Abort()
    database = CanaryRolloutLedger.default_path(state_root)
    try:
        database.lstat()
    except FileNotFoundError:
        typer.echo("Canary rollout scope was not found.", err=True)
        raise typer.Exit(code=1) from None
    except OSError as exc:
        typer.echo(f"Canary rollout ledger is unavailable: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    try:
        lease = acquire_gateway_legacy_lease(state_root)
    except Exception as exc:  # noqa: BLE001 - unsafe state paths fail closed
        typer.echo(f"Cannot acquire the gateway lock safely: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if lease is None:
        typer.echo(
            "Gateway is running or its lock is busy; stop it before reset.",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        try:
            result = CanaryRolloutLedger(database).admin_manual_reset(scope)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            typer.echo(f"Canary rollout ledger is unavailable: {exc}", err=True)
            raise typer.Exit(code=1) from exc
    finally:
        release_gateway_legacy_lease(lease)
    _print_canary_snapshot(scope, result.snapshot, json_output=json_output)
    if not result.available or not result.applied:
        if result.available:
            typer.echo(f"Reset was not applied: {result.reason.value}", err=True)
        raise typer.Exit(code=1)
    if not json_output:
        typer.echo("reset:               applied")
        typer.echo("restart_required:    true")
