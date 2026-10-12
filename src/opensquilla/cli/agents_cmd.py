"""CLI: opensquilla agents list/add/delete."""

from __future__ import annotations

import asyncio
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from opensquilla.agents.registry import AgentRegistry
from opensquilla.onboarding.config_store import default_config_path, load_config, persist_config
from opensquilla.session.keys import normalize_agent_id

agents_app = typer.Typer(help="Manage durable agents.")


def _print_json(payload: Any) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, default=str))


def _print_restart_notice() -> None:
    typer.secho(
        "Agent changes require restarting the gateway to take effect.",
        fg=typer.colors.YELLOW,
    )


def _load_registry(config_path: Path | None) -> tuple[Path, Any, AgentRegistry]:
    target = config_path or default_config_path()
    cfg = load_config(target)
    return target, cfg, AgentRegistry(cfg, config_path=target, persist_changes=False)


def _fail(exc: Exception) -> None:
    typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2) from exc


def _persist_agents_config(cfg: Any, target: Path, *, quiet: bool) -> Any:
    if not quiet:
        return persist_config(cfg, path=target, restart_required=True)
    with redirect_stdout(StringIO()):
        return persist_config(cfg, path=target, restart_required=True)


@agents_app.command("list")
def agents_list(
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """List configured agents."""
    target, _, registry = _load_registry(config_path)
    agents = asyncio.run(registry.list_agents(include_builtin=True))

    if json_output:
        _print_json(agents)
        return

    console = Console(width=200, force_terminal=False)
    table = Table(title=f"Agents in {target}")
    table.add_column("id", no_wrap=True)
    table.add_column("name", no_wrap=True)
    table.add_column("type", no_wrap=True)
    table.add_column("enabled", no_wrap=True)
    table.add_column("model", no_wrap=True)
    table.add_column("workspace")
    for agent in agents:
        table.add_row(
            str(agent.get("id", "")),
            str(agent.get("name", "")),
            str(agent.get("type", "")),
            str(agent.get("enabled", True)),
            str(agent.get("model") or ""),
            str(agent.get("workspace") or ""),
        )
    console.print(table)


@agents_app.command("add")
def agents_add(
    agent_id: str = typer.Argument(..., help="Agent id to add."),
    model: str | None = typer.Option(None, "--model", help="Default model for this agent."),
    workspace: Path | None = typer.Option(None, "--workspace", help="Workspace directory."),
    name: str | None = typer.Option(None, "--name", help="Display name."),
    description: str | None = typer.Option(None, "--description", help="Agent description."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Add a durable agent entry to config."""
    target, cfg, registry = _load_registry(config_path)
    try:
        agent = asyncio.run(
            registry.create_agent(
                agent_id=agent_id,
                name=name,
                description=description,
                model=model,
                workspace=str(workspace) if workspace is not None else None,
            )
        )
    except (ValueError, KeyError) as exc:
        _fail(exc)

    persist = _persist_agents_config(cfg, target, quiet=json_output)
    if json_output:
        _print_json(agent)
        return

    typer.echo(f"Agent saved: {agent['id']}")
    typer.echo(f"Config: {persist.path}")
    if persist.backup_path:
        typer.echo(f"Backup: {persist.backup_path}")
    _print_restart_notice()


@agents_app.command("delete")
def agents_delete(
    agent_id: str = typer.Argument(..., help="Agent id to delete."),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation."),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Delete a durable agent entry from config."""
    target, cfg, registry = _load_registry(config_path)
    if not force:
        typer.confirm(f"Delete agent {agent_id!r} from config?", abort=True)

    try:
        asyncio.run(registry.delete_agent(agent_id))
    except (ValueError, KeyError) as exc:
        _fail(exc)

    persist = _persist_agents_config(cfg, target, quiet=json_output)
    payload = {
        "id": normalize_agent_id(agent_id),
        "deleted": True,
        "workspaceDeleted": False,
        "stateDeleted": False,
    }
    if json_output:
        _print_json(payload)
        return

    typer.echo(f"Agent deleted: {agent_id}")
    typer.echo(f"Config: {persist.path}")
    typer.echo("Workspace and state were left untouched.")
    _print_restart_notice()
