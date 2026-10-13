"""Diagnostics CLI commands."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, cast

import typer
from rich.table import Table

from opensquilla.cli.gateway_rpc import run_gateway_sync
from opensquilla.cli.output import print_json
from opensquilla.cli.ui import console

diagnostics_app = typer.Typer(help="Inspect runtime diagnostics and packaged capabilities.")


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


_DEFAULT_SMOKE_IMPORTS = (
    "joblib",
    "sklearn",
    "lightgbm",
    "tokenizers",
    "tiktoken",
    "onnxruntime",
    "mcp",
)


def _smoke_import_modules(modules: list[str] | tuple[str, ...]) -> dict[str, object]:
    ok: list[str] = []
    missing: dict[str, str] = {}
    for module in modules:
        try:
            importlib.import_module(module)
            ok.append(module)
        except Exception as exc:
            missing[module] = f"{type(exc).__name__}: {exc}"
    return {"ok": ok, "missing": missing, "success": not missing}


def _smoke_router_runtime() -> dict[str, object]:
    import asyncio

    router_module = importlib.import_module("opensquilla.squilla_router.v4_phase3")
    strategy_cls = getattr(router_module, "V4Phase3Strategy")

    async def _run() -> dict[str, object]:
        strategy = strategy_cls(require_router_runtime=True)
        tier, confidence, source, metadata = await strategy.classify(
            "Summarize this short note.",
            valid_tiers=["c0", "c1", "c2", "c3"],
        )
        success = source == "v4_phase3" and bool(strategy._available)
        return {
            "success": success,
            "available": bool(strategy._available),
            "tier": tier,
            "confidence": confidence,
            "source": source,
            "route_class": metadata.get("route_class"),
            "model_version": metadata.get("model_version"),
        }

    return asyncio.run(_run())


@diagnostics_app.command("smoke-imports")
def smoke_imports(
    module: list[str] | None = typer.Option(
        None,
        "--module",
        help="Module to import; defaults to desktop packaged runtime capability smoke set.",
    ),
) -> None:
    """Import optional desktop capability modules and exit non-zero on gaps."""
    modules = module or list(_DEFAULT_SMOKE_IMPORTS)
    result = _smoke_import_modules(modules)
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result["success"]:
        raise typer.Exit(1)


@diagnostics_app.command("smoke-router")
def smoke_router() -> None:
    """Initialize the bundled V4 router and run one deterministic classification."""
    try:
        result = _smoke_router_runtime()
    except Exception as exc:
        result = {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    typer.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not result.get("success"):
        raise typer.Exit(1)
