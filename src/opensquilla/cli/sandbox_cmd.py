"""CLI: opensquilla sandbox posture controls."""

from __future__ import annotations

from pathlib import Path

import typer

from opensquilla.cli.output import print_json
from opensquilla.onboarding.config_store import (
    load_config,
    persist_config,
    resolve_config_path,
)
from opensquilla.sandbox.run_mode import RunMode, run_mode_config_patch
from opensquilla.sandbox.status import status_payload as _status_payload

sandbox_app = typer.Typer(help="Show or change the default sandbox posture.")


_SOURCE_LABEL = {
    "explicit": "from --config",
    "env": "from OPENSQUILLA_GATEWAY_CONFIG_PATH",
    "cwd": "found in cwd",
    "home": "default in $HOME",
}


def _resolve_path(config_path: Path | None) -> Path:
    target, source = resolve_config_path(config_path)
    typer.echo(f"Config: {target} ({_SOURCE_LABEL[source]})")
    return target


def _apply_run_mode(config, run_mode: RunMode):
    patch = run_mode_config_patch(run_mode)
    config.sandbox.run_mode = patch.run_mode.value
    config.sandbox.sandbox = patch.sandbox
    config.sandbox.security_grading = patch.security_grading
    config.sandbox.network_default = patch.network_default
    config.permissions.default_mode = patch.permissions_default_mode
    return config


def _write_run_mode(config_path: Path | None, run_mode: RunMode) -> None:
    target = _resolve_path(config_path)
    config = _apply_run_mode(load_config(target), run_mode)
    persist_config(config, path=target, restart_required=True)
    payload = _status_payload(config, restart_required=True)
    typer.echo(
        "Sandbox run mode set to "
        f"{payload['run_mode_label']}. Restart the gateway for running processes to apply it."
    )


@sandbox_app.command("status")
def sandbox_status(
    config_path: Path | None = typer.Option(None, "--config"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show the configured default sandbox posture."""

    target, _source = resolve_config_path(config_path)
    config = load_config(target)
    payload = _status_payload(config, restart_required=False)
    if json_output:
        print_json(payload)
        return
    typer.echo(f"Config: {target}")
    typer.echo(f"Run mode: {payload['run_mode_label']} ({payload['run_mode']})")
    typer.echo(f"Execution target: {payload['execution_target']}")
    typer.echo(
        "Sandbox: "
        f"sandbox={payload['sandbox']['sandbox']} "
        f"security_grading={payload['sandbox']['security_grading']}"
    )
    typer.echo(f"Permissions default: {payload['permissions']['default_mode']}")


@sandbox_app.command("bypass", hidden=True)
def sandbox_bypass(
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Use Full Host Access for owner calls while retaining sandbox capability."""

    target = _resolve_path(config_path)
    config = load_config(target)
    capability = run_mode_config_patch(RunMode.STANDARD)
    config.sandbox.run_mode = None
    config.sandbox.sandbox = capability.sandbox
    config.sandbox.security_grading = capability.security_grading
    config.sandbox.network_default = capability.network_default
    config.permissions.default_mode = "bypass"
    persist_config(config, path=target, restart_required=True)
    payload = _status_payload(config, restart_required=True)
    typer.echo(
        "Sandbox run mode set to "
        f"{payload['run_mode_label']}. Restart the gateway for running processes to apply it."
    )


@sandbox_app.command("full")
def sandbox_full(
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Disable runtime sandboxing and skip approval and sensitive-path gates."""

    _write_run_mode(config_path, RunMode.FULL)


@sandbox_app.command("on")
def sandbox_on(
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Restore the default sandboxed posture."""

    _write_run_mode(config_path, RunMode.TRUSTED)


@sandbox_app.command("trust")
def sandbox_trust(
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Keep sandboxing enabled while using Managed Execution."""

    _write_run_mode(config_path, RunMode.TRUSTED)


@sandbox_app.command("reset")
def sandbox_reset(
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Reset sandbox posture to OpenSquilla defaults."""

    sandbox_bypass(config_path)
