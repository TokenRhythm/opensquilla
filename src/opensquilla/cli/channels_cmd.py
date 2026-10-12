"""CLI: opensquilla channels list/add/remove/enable/disable."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from opensquilla.cli.channel_fields import (
    apply_channel_token,
    parse_channel_field_pairs,
)
from opensquilla.cli.gateway_rpc import confirm_or_exit, run_gateway_sync
from opensquilla.cli.output import print_json
from opensquilla.cli.ui import ACCENT_HEADER, ACCENT_MARKUP
from opensquilla.cli.ui import console as ui_console
from opensquilla.onboarding.channel_specs import (
    get_channel_setup_spec,
    list_channel_setup_specs,
)
from opensquilla.onboarding.config_store import (
    load_config,
    persist_config,
    resolve_config_path,
)
from opensquilla.onboarding.mutations import (
    list_channel_entries,
    remove_channel,
    set_channel_enabled,
    upsert_channel,
)

channels_app = typer.Typer(help="Manage messaging channels.")


def _print_restart_notice() -> None:
    typer.secho(
        "Restart the gateway PROCESS to apply (this is not the same as "
        "'opensquilla channels restart <name>', which only restarts an "
        "already-loaded adapter).",
        fg=typer.colors.YELLOW,
    )


def _print_channel_verification_next_step(name: str) -> None:
    typer.echo("Next: opensquilla gateway restart")
    typer.echo(f"Verify: uv run opensquilla channels status {name} --json")


_SOURCE_LABEL = {
    "explicit": "from --config",
    "env": "from OPENSQUILLA_GATEWAY_CONFIG_PATH",
    "cwd": "found in cwd",
    "home": "default in $HOME",
}


def _resolve_and_announce(config_path: Path | None) -> Path:
    target, source = resolve_config_path(config_path)
    ui_console.print(f"[{ACCENT_MARKUP}]Config:[/] {target} ({_SOURCE_LABEL[source]})")
    return target


def _render_channels_table(entries: list[dict[str, Any]], *, title: str) -> None:
    if not entries:
        typer.echo("0 channels configured.")
        return
    console = Console(width=200, force_terminal=False)
    table = Table(title=title)
    table.add_column("name", no_wrap=True)
    table.add_column("type", no_wrap=True)
    table.add_column("enabled", no_wrap=True)
    table.add_column("agent_id", no_wrap=True)
    table.add_column("details")
    for e in entries:
        details = ", ".join(
            f"{k}={v}"
            for k, v in e.items()
            if k not in {"name", "type", "enabled", "agent_id"}
        )
        table.add_row(
            e["name"],
            e["type"],
            str(e.get("enabled", True)),
            e.get("agent_id", "main"),
            details,
        )
    console.print(table)


def _status_diagnostic_text(row: dict[str, Any]) -> str:
    diagnostics = row.get("diagnostics")
    if not isinstance(diagnostics, dict):
        return ""
    last_error = diagnostics.get("last_error")
    if not isinstance(last_error, dict):
        return ""
    message = last_error.get("message")
    if message:
        return str(message)
    error_class = last_error.get("error_class")
    return str(error_class) if error_class else ""


def _render_status_table(payload: dict[str, Any], *, name: str | None = None) -> None:
    rows = _filter_status_rows(payload, name)
    table = Table(title="Channel status", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Name")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Connected")
    table.add_column("Enabled")
    table.add_column("Configured")
    table.add_column("Restart attempts", justify="right")
    table.add_column("Diagnostic")
    for row in rows:
        table.add_row(
            str(row.get("name") or ""),
            str(row.get("type") or ""),
            str(row.get("status") or ""),
            str(row.get("connected") or False),
            str(row.get("enabled") or False),
            str(row.get("configured") or False),
            str(row.get("restart_attempts") or 0),
            _status_diagnostic_text(row),
        )
    Console(width=180, force_terminal=False).print(table)


def _filter_status_rows(payload: dict[str, Any], name: str | None) -> list[dict[str, Any]]:
    rows = payload.get("channels", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    if not name:
        return [row for row in rows if isinstance(row, dict)]
    return [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("name") or "") == name
    ]


@channels_app.command("list")
def channels_list(
    config_path: Path | None = typer.Option(None, "--config"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    target = (
        resolve_config_path(config_path)[0]
        if json_output
        else _resolve_and_announce(config_path)
    )
    cfg = load_config(target)
    entries = list_channel_entries(cfg)
    if json_output:
        print_json(entries)
        return
    _render_channels_table(entries, title=f"Channels in {target}")


@channels_app.command("status")
def channels_status(
    name: str | None = typer.Argument(None, help="Optional channel name to inspect"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Show runtime channel status from the running gateway."""

    async def _run(client):
        return await client.call("channels.status", {})

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if name:
        filtered = {"channels": _filter_status_rows(payload, name)}
        if json_output:
            print_json(filtered)
            return
        _render_status_table(filtered, name=name)
        return
    if json_output:
        print_json(payload)
        return
    _render_status_table(payload)


@channels_app.command("restart")
def channels_restart(
    name: str = typer.Argument(..., help="Channel name to restart"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Restart a live messaging channel."""

    confirm_or_exit(
        f"Restart channel {name!r}? Message delivery may be interrupted.",
        yes=yes,
        json_output=json_output,
    )

    async def _run(client):
        return await client.call("channels.restart", {"name": name})

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return
    typer.echo(f"Channel restarted: {payload.get('channel', name)}")


@channels_app.command("logout")
def channels_logout(
    name: str = typer.Argument(..., help="Channel name to log out"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Log out and disconnect a live messaging channel."""

    confirm_or_exit(
        f"Log out channel {name!r}? Live channel session state will be dropped.",
        yes=yes,
        json_output=json_output,
    )

    async def _run(client):
        return await client.call("channels.logout", {"name": name})

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return
    typer.echo(f"Channel logged out: {payload.get('channel', name)}")


@channels_app.command("add")
def channels_add(
    type_name: str = typer.Argument(..., help="Channel type (e.g. slack)."),
    name: str = typer.Option(..., "--name"),
    token: str = typer.Option("", "--token"),
    enabled: bool = typer.Option(True, "--enabled/--disabled"),
    agent_id: str = typer.Option("main", "--agent-id"),
    fields: list[str] = typer.Option(
        [], "--field", "-f", help="Repeatable key=value channel field."
    ),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Add or update a channel entry."""
    target = _resolve_and_announce(config_path)
    payload: dict[str, Any] = {
        "type": type_name,
        "name": name,
        "enabled": enabled,
        "agent_id": agent_id,
    }
    apply_channel_token(payload, type_name, token)
    payload.update(parse_channel_field_pairs(fields, type_name))

    cfg = load_config(target)
    try:
        result = upsert_channel(cfg, entry_payload=payload)
    except (ValueError, KeyError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    persist = persist_config(result.config, path=target, restart_required=True)
    typer.echo(f"Channel saved: {name} ({type_name})")
    if persist.backup_path:
        typer.echo(f"Backup: {persist.backup_path}")
    _print_restart_notice()
    _print_channel_verification_next_step(name)


@channels_app.command("remove")
def channels_remove(
    name: str = typer.Argument(...),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    target = _resolve_and_announce(config_path)
    cfg = load_config(target)
    try:
        result = remove_channel(cfg, name=name)
    except KeyError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    persist_config(result.config, path=target, restart_required=True)
    typer.echo(f"Channel removed: {name}")
    _print_restart_notice()


@channels_app.command("enable")
def channels_enable(
    name: str = typer.Argument(...),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    target = _resolve_and_announce(config_path)
    cfg = load_config(target)
    try:
        result = set_channel_enabled(cfg, name=name, enabled=True)
    except KeyError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    persist_config(result.config, path=target, restart_required=True)
    typer.echo(f"Channel enabled: {name}")
    _print_restart_notice()


@channels_app.command("disable")
def channels_disable(
    name: str = typer.Argument(...),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    target = _resolve_and_announce(config_path)
    cfg = load_config(target)
    try:
        result = set_channel_enabled(cfg, name=name, enabled=False)
    except KeyError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    persist_config(result.config, path=target, restart_required=True)
    typer.echo(f"Channel disabled: {name}")
    _print_restart_notice()


@channels_app.command("edit")
def channels_edit(
    name: str = typer.Argument(..., help="Existing channel name."),
    token: str = typer.Option("", "--token"),
    enabled: bool | None = typer.Option(None, "--enabled/--disabled"),
    agent_id: str = typer.Option("", "--agent-id"),
    fields: list[str] = typer.Option(
        [], "--field", "-f", help="Repeatable key=value channel field."
    ),
    config_path: Path | None = typer.Option(None, "--config"),
) -> None:
    """Edit an existing channel; blank fields keep current values."""
    target = _resolve_and_announce(config_path)
    cfg = load_config(target)
    existing = next(
        (
            e.model_dump(mode="python")
            for e in cfg.channels.channels
            if e.name == name
        ),
        None,
    )
    if existing is None:
        typer.secho(f"Error: no channel named {name!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    type_name = existing["type"]

    overrides: dict[str, Any] = {"type": type_name, "name": name}
    if enabled is not None:
        overrides["enabled"] = enabled
    if agent_id:
        overrides["agent_id"] = agent_id
    apply_channel_token(overrides, type_name, token)
    overrides.update(parse_channel_field_pairs(fields, type_name))
    # Patch semantics: every field not explicitly overridden retains its
    # existing value. upsert_channel's secret-merge guards against blanks
    # in the add path; this seeding handles non-secret partial updates
    # in the edit path.
    payload = {**existing, **overrides}

    try:
        result = upsert_channel(cfg, entry_payload=payload)
    except (ValueError, KeyError) as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    persist = persist_config(result.config, path=target, restart_required=True)
    typer.echo(f"Channel updated: {name} ({type_name})")
    if persist.backup_path:
        typer.echo(f"Backup: {persist.backup_path}")
    _print_restart_notice()
    _print_channel_verification_next_step(name)


@channels_app.command("types")
def channels_types(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List supported channel types."""
    specs = list_channel_setup_specs()
    if json_output:
        print_json([
            {
                "type": s.type,
                "label": s.label,
                "transport": s.transport,
                "requires_public_url": s.requires_public_url,
                "dependency_extra": s.dependency_extra,
            }
            for s in specs
        ])
        return
    table = Table(title="Supported channel types")
    table.add_column("type", no_wrap=True)
    table.add_column("label")
    table.add_column("transport", no_wrap=True)
    table.add_column("public URL", no_wrap=True)
    table.add_column("extras", no_wrap=True)
    for s in specs:
        table.add_row(
            s.type, s.label, s.transport,
            "yes" if s.requires_public_url else "no",
            s.dependency_extra or "—",
        )
    Console(width=140, force_terminal=False).print(table)


@channels_app.command("describe")
def channels_describe(
    type_name: str = typer.Argument(..., help="Channel type, e.g. slack."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show the field schema, transport, and docs hint for a channel type."""
    try:
        spec = get_channel_setup_spec(type_name)
    except KeyError as exc:
        typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if json_output:
        print_json({
            "type": spec.type,
            "label": spec.label,
            "description": spec.description,
            "transport": spec.transport,
            "requires_public_url": spec.requires_public_url,
            "dependency_extra": spec.dependency_extra,
            "restart_required": spec.restart_required,
            "docs_hint": spec.docs_hint,
            "fields": [
                {
                    "name": f.name, "label": f.label, "type": f.field_type,
                    "required": f.required, "default": f.default,
                    "choices": list(f.choices), "secret": f.secret,
                    "description": f.description,
                }
                for f in spec.fields
            ],
        })
        return

    console = Console(width=160, force_terminal=False)
    typer.echo(f"{spec.label} ({spec.type})")
    typer.echo(spec.description)
    typer.echo(
        f"transport={spec.transport}  "
        f"public_url={'yes' if spec.requires_public_url else 'no'}  "
        f"extras={spec.dependency_extra or '—'}  "
        f"docs={spec.docs_hint}"
    )
    table = Table(title="Fields")
    table.add_column("name", no_wrap=True)
    table.add_column("type", no_wrap=True)
    table.add_column("required", no_wrap=True)
    table.add_column("secret", no_wrap=True)
    table.add_column("default")
    table.add_column("choices")
    for f in spec.fields:
        table.add_row(
            f.name,
            f.field_type,
            "yes" if f.required else "no",
            "yes" if f.secret else "no",
            "" if f.default is None else str(f.default),
            ",".join(f.choices) if f.choices else "—",
        )
    console.print(table)


@channels_app.command("certify")
def channels_certify(
    providers: list[str] = typer.Option(
        [],
        "--provider",
        "-p",
        help="Channel type to certify; repeat to select multiple (default: all).",
    ),
    timeout: float = typer.Option(
        15.0,
        "--timeout",
        min=0.1,
        help="Per-provider operation timeout in seconds.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit redacted machine-readable evidence.",
    ),
    send_test_message: bool = typer.Option(
        False,
        "--send-test-message",
        help="Send one fixed test message after a successful safe auth probe.",
    ),
    allow_side_effects: bool = typer.Option(
        False,
        "--allow-side-effects",
        help="Required acknowledgement for side-effecting certification.",
    ),
    targets: list[str] = typer.Option(
        [],
        "--target",
        help="Explicit provider=destination; required for every delivery test.",
    ),
) -> None:
    """Run ephemeral, environment-driven live channel certification.

    Credentials are accepted only through ``OPENSQUILLA_CHANNEL_CERT_*``
    environment variables. The default mode calls adapter-specific safe auth
    probes and never starts ingress or sends messages. No credential values
    are written to config or included in the evidence output.
    """
    from opensquilla.onboarding.channel_certification import (
        CertificationUsageError,
        certify_channels,
        evidence_passed,
        parse_targets,
    )

    try:
        target_map = parse_targets(targets)
        evidence = asyncio.run(
            certify_channels(
                providers,
                environ=os.environ,
                timeout=timeout,
                send_test_message=send_test_message,
                allow_side_effects=allow_side_effects,
                targets=target_map,
            )
        )
    except CertificationUsageError as exc:
        if json_output:
            print_json({"error": {"code": "invalid_certification_request", "message": str(exc)}})
        else:
            typer.secho(f"Error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    if json_output:
        print_json(evidence)
    else:
        table = Table(title="Channel certification", show_header=True)
        table.add_column("Provider")
        table.add_column("Operation")
        table.add_column("Status")
        table.add_column("Authenticated")
        table.add_column("Latency", justify="right")
        table.add_column("Detail")
        for row in evidence["providers"]:
            detail = str(row.get("detail") or "")
            missing = row.get("missingEnvironment")
            if isinstance(missing, list) and missing:
                detail = "missing: " + ", ".join(str(value) for value in missing)
            table.add_row(
                str(row.get("provider") or ""),
                str(row.get("operation") or ""),
                str(row.get("status") or ""),
                str(bool(row.get("authenticated"))),
                f"{row.get('latencyMs', 0)} ms",
                detail,
            )
        Console(width=180, force_terminal=False).print(table)

    if not evidence_passed(evidence):
        raise typer.Exit(code=1)


pairings_app = typer.Typer(help="Review and decide channel pairing requests.")
channels_app.add_typer(pairings_app, name="pairings")


def _render_pairings_table(records: list[dict[str, Any]], *, channel: str) -> None:
    table = Table(title=f"Pairing requests: {channel}", header_style=ACCENT_HEADER)
    table.add_column("Code")
    table.add_column("Sender")
    table.add_column("Status")
    table.add_column("Requested")
    table.add_column("Approved")
    for record in records:
        sender = str(record.get("senderName") or record.get("senderId") or "")
        table.add_row(
            str(record.get("pairingCode") or ""),
            sender,
            str(record.get("status") or ""),
            str(record.get("createdAt") or ""),
            str(record.get("approvedAt") or ""),
        )
    Console(width=140, force_terminal=False).print(table)
    if not records:
        typer.echo("No pairing requests.")


@pairings_app.command("list")
def pairings_list(
    channel: str = typer.Argument(..., help="Channel name to inspect"),
    status: str | None = typer.Option(
        None, "--status", help="Filter by status: pending, approved, or revoked"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """List pairing requests for a channel, newest first."""

    async def _run(client):
        params: dict[str, Any] = {"channelName": channel}
        if status:
            params["status"] = status
        return await client.call("channels.pairings", params)

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return
    _render_pairings_table(list(payload.get("pairings") or []), channel=channel)


@pairings_app.command("approve")
def pairings_approve(
    channel: str = typer.Argument(..., help="Channel name"),
    pairing: str = typer.Argument(..., help="Pairing code (8 chars) or full pairing id"),
    admin: bool = typer.Option(
        False,
        "--admin",
        help=(
            "Also mark this sender as a channel admin (full tool surface; "
            "privileged commands still confirm per call). Use for yourself "
            "or someone you trust with the host."
        ),
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Approve a pairing request; the sender is notified they can start."""

    access = (
        "The sender gains conversational access AND channel-admin privileges."
        if admin
        else "The sender gains conversational access."
    )
    confirm_or_exit(
        f"Approve pairing {pairing!r} on channel {channel!r}? {access}",
        yes=yes,
        json_output=json_output,
    )

    async def _run(client):
        request: dict[str, object] = {"channelName": channel, "pairingCode": pairing}
        if admin:
            request["asAdmin"] = True
        return await client.call("channels.pairing.approve", request)

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return
    record = payload.get("pairing") or {}
    sender = str(record.get("senderName") or record.get("senderId") or "")
    suffix = " [admin]" if payload.get("adminGranted") else ""
    typer.echo(f"Pairing approved: {record.get('pairingCode', pairing)} ({sender}){suffix}")


@pairings_app.command("revoke")
def pairings_revoke(
    channel: str = typer.Argument(..., help="Channel name"),
    pairing: str = typer.Argument(..., help="Pairing code (8 chars) or full pairing id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Revoke a pairing; the sender loses conversational access."""

    confirm_or_exit(
        f"Revoke pairing {pairing!r} on channel {channel!r}? "
        "The sender loses conversational access.",
        yes=yes,
        json_output=json_output,
    )

    async def _run(client):
        return await client.call(
            "channels.pairing.revoke",
            {"channelName": channel, "pairingCode": pairing},
        )

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return
    record = payload.get("pairing") or {}
    typer.echo(f"Pairing revoked: {record.get('pairingCode', pairing)}")
