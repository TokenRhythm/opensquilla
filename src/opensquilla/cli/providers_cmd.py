"""CLI: opensquilla providers list/configure."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opensquilla.cli.gateway_rpc import run_gateway_sync
from opensquilla.cli.output import print_json
from opensquilla.onboarding.config_store import (
    default_config_path,
    load_config,
    persist_config,
)
from opensquilla.onboarding.mutations import upsert_llm_provider
from opensquilla.onboarding.provider_specs import (
    list_provider_setup_specs,
    provider_catalog_payload,
)

providers_app = typer.Typer(help="Configure and inspect LLM providers.")


@providers_app.command("list")
def providers_list(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List all known providers (supported and disabled)."""
    if json_output:
        print_json(provider_catalog_payload())
        return

    console = Console(width=200, force_terminal=False)
    table = Table(title="Providers")
    table.add_column("provider", no_wrap=True)
    table.add_column("label", no_wrap=True)
    table.add_column("runtime", no_wrap=True)
    table.add_column("requires key", no_wrap=True)
    table.add_column("requires base url", no_wrap=True)
    table.add_column("default base url")
    for s in list_provider_setup_specs():
        table.add_row(
            s.provider_id,
            s.label,
            "supported" if s.runtime_supported else "unsupported (disabled)",
            "yes" if s.requires_api_key else "no",
            "yes" if s.requires_base_url else "no",
            s.default_base_url or "-",
        )
    console.print(table)


@providers_app.command("status")
def providers_status(
    provider: str | None = typer.Argument(None, help="Optional provider id"),
    probe_models: bool = typer.Option(
        False,
        "--probe-models",
        help="Probe model listing for the active provider",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Show runtime provider diagnostics from the running gateway."""

    async def _run(client):
        params: dict[str, object] = {"probeModels": probe_models}
        if provider:
            params["provider"] = provider
        return await client.call("providers.status", params)

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return

    console = Console(width=180, force_terminal=False)
    table = Table(title="Provider status")
    table.add_column("provider", no_wrap=True)
    table.add_column("active", no_wrap=True)
    table.add_column("configured", no_wrap=True)
    table.add_column("buildable", no_wrap=True)
    table.add_column("model")
    table.add_column("error")
    if probe_models:
        table.add_column("probe")
    for row in payload.get("providers", []):
        values = [
            str(row.get("providerId") or ""),
            "yes" if row.get("active") else "no",
            "yes" if row.get("configured") else "no",
            "yes" if row.get("buildable") else "no",
            str(row.get("model") or ""),
            str(row.get("error") or ""),
        ]
        if probe_models:
            probe = row.get("modelProbe") or {}
            status = str(probe.get("status") or "unavailable")
            if status == "ok":
                probe_text = f"ok ({int(probe.get('count') or 0)} models)"
            elif status in {"error", "degraded"}:
                kind = str(probe.get("failureKind") or status)
                detail = str(probe.get("error") or "")
                probe_text = f"{kind} — {detail}" if detail else kind
            else:
                probe_text = status
            values.append(probe_text)
        table.add_row(*values)
    console.print(table)


@providers_app.command("configure")
def providers_configure(
    provider: str = typer.Argument(..., help="Provider id (e.g. openrouter)."),
    model: str = typer.Option("", "--model", "-m"),
    api_key: str = typer.Option("", "--api-key", "-k"),
    base_url: str = typer.Option("", "--base-url"),
    proxy: str = typer.Option("", "--proxy"),
    config_path: Path | None = typer.Option(
        None, "--config", help="Override config path."
    ),
) -> None:
    """Configure the active LLM provider."""
    target = config_path or default_config_path()
    cfg = load_config(target)
    try:
        result = upsert_llm_provider(
            cfg,
            provider_id=provider,
            model=model,
            api_key=api_key,
            base_url=base_url,
            proxy=proxy,
        )
    except (ValueError, KeyError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    persist = persist_config(
        result.config, path=target, restart_required=result.restart_required
    )
    typer.echo(f"Provider configured: {provider}")
    typer.echo(f"Config: {persist.path}")
    if persist.backup_path:
        typer.echo(f"Backup: {persist.backup_path}")
