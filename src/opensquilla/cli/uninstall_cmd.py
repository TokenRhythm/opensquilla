"""``opensquilla uninstall`` — inventory-driven uninstaller CLI.

Default posture: remove the program, keep user data. Deletion of data is opt-in
via ``--purge-state`` / ``--purge-config`` / ``--purge-all``. ``--dry-run`` and
``--json`` render the exact plan without acting. A total wipe (``--purge-all``)
requires typing a confirmation phrase in interactive mode, and any non-TTY /
``--json`` surface refuses to act without ``--yes``.
"""

from __future__ import annotations

import os

import typer


def _lifecycle_stop(
    host: str, port: int, config_path: str | None, shutdown_timeout: float
) -> tuple[str, int, str]:
    """Stop the lifecycle-managed gateway; injected into the uninstall core so the
    `uninstall` package never imports `cli` (avoids a cli<->uninstall cycle).

    Returns (state, exit_code, message). The core treats unmanaged/target_mismatch
    as "refuse" and a non-zero exit on a running gateway as "could not stop".
    """
    from opensquilla.cli.gateway_lifecycle import GatewayLifecycleManager

    mgr = GatewayLifecycleManager(
        host=host, port=port, config_path=config_path, shutdown_timeout=shutdown_timeout
    )
    status = mgr.status()
    if status.state in ("not_started", "stale", "unmanaged", "target_mismatch"):
        return (status.state, status.exit_code, status.message or "")
    stop = mgr.stop()
    return (stop.state, stop.exit_code, stop.message or "")


def uninstall_command(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be removed and kept; do nothing."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the interactive confirmation prompt."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit a machine-readable plan/result."),
    purge_state: bool = typer.Option(
        False,
        "--purge-state",
        help="Also delete runtime state (sessions, scheduler, memory, logs, cache).",
    ),
    purge_config: bool = typer.Option(
        False,
        "--purge-config",
        help="Also delete configuration and secrets (config.toml, .env).",
    ),
    purge_all: bool = typer.Option(
        False,
        "--purge-all",
        help="Delete ALL OpenSquilla user data on this machine (implies state+config).",
    ),
    remove_source_dir: bool = typer.Option(
        False,
        "--remove-source-dir",
        help="(source installs) surface the checkout path as a manual removal step.",
    ),
    confirm_purge_all: str = typer.Option(
        "",
        "--confirm-purge-all",
        help="Confirmation phrase required for --purge-all on non-interactive surfaces.",
    ),
) -> None:
    """Uninstall OpenSquilla. By default removes the program and keeps your data."""
    from opensquilla.cli.codetask_cmd import _stdin_is_tty
    from opensquilla.cli.output import emit_error, print_json
    from opensquilla.cli.ui import console
    from opensquilla.uninstall.actions import execute
    from opensquilla.uninstall.inventory import discover
    from opensquilla.uninstall.plan import PlanOptions, build_plan
    from opensquilla.uninstall.safety import PURGE_ALL_CONFIRM_PHRASE

    options = PlanOptions(
        purge_state=purge_state,
        purge_config=purge_config,
        purge_all=purge_all,
        remove_source_dir=remove_source_dir,
    )
    profile_kind = os.environ.get("OPENSQUILLA_PROFILE_KIND", "").strip().lower()
    if profile_kind.startswith("desktop-") and options.any_purge:
        emit_error(
            "Desktop data spans the primary profile, recovery profiles, credentials, "
            "logs, and backups. Use Desktop Settings → Data cleanup so every item is "
            "listed and confirmed; the generic uninstall command cannot safely perform "
            "a partial Desktop purge.",
            json_output=json_output,
            code="DESKTOP_CLEANUP_REQUIRED",
        )
        raise typer.Exit(2)
    inventory = discover()
    plan = build_plan(inventory, options)

    # --dry-run: render the plan and stop. Pure; touches nothing.
    if dry_run:
        if json_output:
            print_json({"dry_run": True, "plan": plan.to_payload(), "receipt": inventory.receipt})
        else:
            _render_plan(console, plan)
        return

    if purge_all:
        # A total wipe demands an explicit second factor on EVERY surface — the
        # typed phrase — so a stray `--yes --purge-all` (CI, scripts) can never
        # trigger an irreversible wipe. --yes alone is not sufficient here.
        provided = confirm_purge_all.strip()
        if provided != PURGE_ALL_CONFIRM_PHRASE and not yes and _stdin_is_tty():
            _render_plan(console, plan)
            provided = typer.prompt(
                f"This permanently deletes ALL OpenSquilla data. "
                f"Type '{PURGE_ALL_CONFIRM_PHRASE}' to proceed"
            ).strip()
        if provided != PURGE_ALL_CONFIRM_PHRASE:
            emit_error(
                "--purge-all requires confirmation: pass "
                f'--confirm-purge-all "{PURGE_ALL_CONFIRM_PHRASE}", or run '
                "interactively and type the phrase when prompted.",
                json_output=json_output,
                code="CONFIRMATION_REQUIRED",
            )
            raise typer.Exit(2)
    else:
        # Non-interactive safety gate (mirrors code-task): never act without --yes
        # on a --json or non-TTY surface where we cannot prompt.
        if not yes and (json_output or not _stdin_is_tty()):
            emit_error(
                "Refusing to uninstall without --yes on a non-interactive surface. "
                "Re-run with --yes (and --purge-* if you want data removed).",
                json_output=json_output,
                code="CONFIRMATION_REQUIRED",
            )
            raise typer.Exit(2)
        if not yes:
            _render_plan(console, plan)
            if options.any_purge:
                typer.confirm("Proceed with uninstall AND delete the selected data?", abort=True)
            else:
                typer.confirm("Proceed with uninstall (your data is kept)?", abort=True)

    result = execute(plan, inventory, lifecycle_stop=_lifecycle_stop)

    if json_output:
        print_json(
            {
                "ok": result.ok,
                "aborted": result.aborted,
                "plan": plan.to_payload(),
                "result": result.to_payload(),
            }
        )
    else:
        _render_result(console, result)

    if result.aborted:
        raise typer.Exit(3)
    if not result.ok:
        raise typer.Exit(1)


def _render_plan(console: object, plan: object) -> None:
    """Human-readable preview of the plan (stdout)."""
    p = console.print  # type: ignore[attr-defined]
    p(f"\n[bold]Uninstall plan[/bold] — install method: [bold]{plan.method}[/bold]")  # type: ignore[attr-defined]
    p(f"OpenSquilla home: {plan.home}")  # type: ignore[attr-defined]

    p("\n[bold]Will do:[/bold]")  # type: ignore[attr-defined]
    for action in plan.actions:  # type: ignore[attr-defined]
        p(f"  • {action.summary}")
        for path in action.paths:
            p(f"      [dim]{path}[/dim]")
        for command in action.commands:
            p(f"      [dim]$ {' '.join(command)}[/dim]")

    if plan.keep:  # type: ignore[attr-defined]
        p("\n[bold]Will keep:[/bold]")  # type: ignore[attr-defined]
        for item in plan.keep:  # type: ignore[attr-defined]
            p(f"  • [green]{item}[/green]")

    if plan.manual:  # type: ignore[attr-defined]
        p("\n[bold]Manual (not done automatically):[/bold]")  # type: ignore[attr-defined]
        for action in plan.manual:  # type: ignore[attr-defined]
            p(f"  • {action.summary}: [yellow]{action.reason}[/yellow]")
            for path in action.paths:
                p(f"      [dim]{path}[/dim]")

    if plan.warnings:  # type: ignore[attr-defined]
        p("\n[bold yellow]Warnings:[/bold yellow]")  # type: ignore[attr-defined]
        for warning in plan.warnings:  # type: ignore[attr-defined]
            p(f"  • [yellow]{warning}[/yellow]")
    p("")


def _render_result(console: object, result: object) -> None:
    p = console.print  # type: ignore[attr-defined]
    if result.aborted:  # type: ignore[attr-defined]
        p("\n[bold red]Uninstall aborted before deleting anything.[/bold red]")  # type: ignore[attr-defined]
    for r in result.results:  # type: ignore[attr-defined]
        mark = "[green]✓[/green]" if r.ok else "[red]✗[/red]"
        p(f"  {mark} {r.summary}" + (f" — [dim]{r.detail}[/dim]" if r.detail else ""))
    status = "[green]done[/green]" if result.ok else "[red]completed with errors[/red]"  # type: ignore[attr-defined]
    p(f"\nUninstall {status}.\n")
