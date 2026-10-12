"""Diagnostics CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import typer
from rich.table import Table

from opensquilla.cli.gateway_rpc import run_gateway_sync
from opensquilla.cli.output import print_json
from opensquilla.cli.ui import console

diagnostics_app = typer.Typer(help="Manage runtime diagnostics logging.")


def _print_status(payload: dict[str, Any]) -> None:
    raw = payload.get("raw_turn_call") or {}
    runtime = payload.get("runtime") or {}
    configured = payload.get("configured") or {}
    table = Table(title="Diagnostics", show_header=True)
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("enabled", str(bool(payload.get("enabled"))).lower())
    table.add_row("detail", str(payload.get("detail") or "off"))
    table.add_row("raw", str(bool(raw.get("enabled"))).lower())
    table.add_row("raw source", str(raw.get("source") or "off"))
    table.add_row("runtime enabled", str(runtime.get("enabled")))
    table.add_row("runtime raw", str(bool(runtime.get("raw"))).lower())
    table.add_row(
        "config diagnostics_enabled",
        str(bool(configured.get("diagnostics_enabled"))).lower(),
    )
    if payload.get("warning"):
        table.add_row("warning", str(payload["warning"]))
    console.print(table)


@diagnostics_app.command("status")
def diagnostics_status(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Show effective diagnostics and raw-capture state."""

    async def _run(client) -> dict[str, Any]:
        return cast(dict[str, Any], await client.call("diagnostics.status", {}))

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return
    _print_status(payload)


@diagnostics_app.command("on")
def diagnostics_on(
    raw: bool = typer.Option(False, "--raw", help="Also enable raw turn-call capture."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Enable runtime diagnostics; --raw also enables raw turn-call capture."""

    async def _run(client) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await client.call("diagnostics.set", {"enabled": True, "raw": raw}),
        )

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    _print_status(payload)


@diagnostics_app.command("off")
def diagnostics_off(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Disable runtime diagnostics and runtime raw capture."""

    async def _run(client) -> dict[str, Any]:
        return cast(dict[str, Any], await client.call("diagnostics.set", {"enabled": False}))

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    _print_status(payload)
