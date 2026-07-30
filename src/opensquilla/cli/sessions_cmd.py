"""Sessions command — list/show/resume/delete/export sessions."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from opensquilla.cli.chat.session_state import messages_to_markdown
from opensquilla.cli.gateway_client import session_history_all
from opensquilla.cli.gateway_rpc import run_gateway_sync
from opensquilla.cli.output import print_json
from opensquilla.cli.ui import ACCENT, ACCENT_HEADER, console, error_panel
from opensquilla.cli.url_utils import normalize_gateway_url

app = typer.Typer(help="Manage chat sessions.")

_CLIENT_UNAVAILABLE = object()
_ACTION_FAILED = object()


def _resolved_key(payload: dict[str, Any], fallback: str) -> str:
    value = payload.get("session_key") or payload.get("key") or fallback
    return str(value)


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        if raw.isdigit():
            number = float(int(raw))
            if number > 10_000_000_000:
                number = number / 1000
            return datetime.fromtimestamp(number, tz=UTC)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError as exc:
        raise typer.BadParameter("--since must be an ISO date/datetime or epoch timestamp") from exc


def _row_datetime(row: dict[str, Any]) -> datetime | None:
    value = row.get("updated_at", row.get("updatedAt"))
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    if isinstance(value, str):
        try:
            if value.isdigit():
                return _parse_since(value)
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError:
            return None
    return None


def _filter_sessions(
    rows: list[dict[str, Any]],
    *,
    agent: str | None,
    status: str | None,
    channel: str | None,
    since: datetime | None,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if agent and str(row.get("agent_id") or row.get("agentId") or "") != agent:
            continue
        if status and str(row.get("status") or "").lower() != status.lower():
            continue
        if channel:
            channel_values = {
                str(row.get("channel") or ""),
                str(row.get("last_channel") or ""),
                str(row.get("lastChannel") or ""),
                str(row.get("source_channel") or ""),
                str(row.get("sourceChannel") or ""),
            }
            if channel not in channel_values:
                continue
        if since:
            updated = _row_datetime(row)
            if updated is None or updated < since:
                continue
        filtered.append(row)
    return filtered


async def _with_client(action):
    from opensquilla.cli.gateway_client import GatewayClient, GatewayRPCError

    client = GatewayClient()
    try:
        await client.connect(
            normalize_gateway_url(os.environ.get("OPENSQUILLA_GATEWAY_URL", "ws://localhost:18791/ws"))
        )
        return await action(client)
    except SystemExit as exc:
        console.print(f"[dim]{exc}[/dim]")
        return _CLIENT_UNAVAILABLE
    except GatewayRPCError as exc:
        console.print(error_panel(str(exc)))
        return _ACTION_FAILED
    finally:
        await client.close()


@app.command("list")
def sessions_list(
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum rows"),
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent id"),
    status: str | None = typer.Option(None, "--status", help="Filter by session status"),
    channel: str | None = typer.Option(None, "--channel", help="Filter by channel/source"),
    since: str | None = typer.Option(None, "--since", help="ISO date/datetime or epoch timestamp"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List recent sessions."""
    since_dt = _parse_since(since)

    async def _run(client):
        return await client.list_sessions(limit=limit)

    result = run_gateway_sync(_run, json_output=json_output)
    raw_rows = result.get("sessions", []) if isinstance(result, dict) else []
    rows = _filter_sessions(
        [row for row in raw_rows if isinstance(row, dict)],
        agent=agent,
        status=status,
        channel=channel,
        since=since_dt,
    )
    if json_output:
        payload = dict(result) if isinstance(result, dict) else {}
        payload["sessions"] = rows
        payload["count"] = len(rows)
        print_json(payload)
        return

    table = Table(title="Sessions", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Key")
    table.add_column("Agent")
    table.add_column("Status")
    table.add_column("Model")
    table.add_column("Messages", justify="right")
    for row in rows:
        table.add_row(
            str(row.get("key") or ""),
            str(row.get("agent_id") or row.get("agentId") or ""),
            str(row.get("status") or ""),
            str(row.get("model") or ""),
            str(row.get("message_count") or row.get("entry_count") or 0),
        )
    console.print(table)


@app.command("show")
def sessions_show(
    session_id: str = typer.Argument(..., help="Session ID to inspect"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show details of a specific session."""

    async def _run(client):
        resolved = await client.resolve_session(session_id)
        preview = await client.preview_sessions(keys=[_resolved_key(resolved, session_id)])
        return {"resolved": resolved, "preview": preview}

    result = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(result)
        return

    resolved = result.get("resolved", {}) if isinstance(result, dict) else {}
    previews = result.get("preview", {}).get("previews", []) if isinstance(result, dict) else []
    preview = previews[0] if previews else {}
    key = _resolved_key(resolved, session_id)
    table = Table(title=f"Session {key}", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Field", style=ACCENT)
    table.add_column("Value")
    for field, value in (
        ("session_key", key),
        ("session_id", resolved.get("session_id")),
        ("agent_id", resolved.get("agent_id")),
        ("status", resolved.get("status")),
        ("model", resolved.get("model")),
        ("updated_at", resolved.get("updated_at") or preview.get("updatedAt")),
        ("title", preview.get("title")),
    ):
        if value not in (None, ""):
            table.add_row(field, str(value))
    console.print(table)
    last_message = str(preview.get("lastMessage") or "")
    if last_message:
        console.print(last_message)


@app.command("resume")
def sessions_resume(session_id: str = typer.Argument(..., help="Session ID to resume")) -> None:
    """Resume a session in interactive chat."""
    from opensquilla.cli.chat_cmd import run_chat

    async def _run(client):
        return await client.resolve_session(session_id)

    result = asyncio.run(_with_client(_run))
    if result is _CLIENT_UNAVAILABLE:
        console.print(f"[dim]Session {session_id!r} requires a running gateway.[/dim]")
        return
    if result is _ACTION_FAILED:
        return
    run_chat(session_id=_resolved_key(result, session_id))


@app.command("abort")
def sessions_abort(
    session_id: str = typer.Argument(..., help="Session ID to abort"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Abort a running session turn."""

    async def _run(client):
        resolved = await client.resolve_session(session_id)
        key = _resolved_key(resolved, session_id)
        result = await client.abort_session(key)
        if isinstance(result, dict):
            return {"resolved": resolved, **result}
        return {"resolved": resolved, "result": result}

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    key = payload.get("key") or session_id
    aborted = bool(payload.get("aborted", False))
    console.print(f"{'Aborted' if aborted else 'No running task for'} session {key!r}")


@app.command("delete")
def sessions_delete(
    session_id: str = typer.Argument(..., help="Session ID to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a session."""
    if not yes:
        confirmed = typer.confirm(f"Delete session {session_id!r}?")
        if not confirmed:
            raise typer.Abort()

    async def _run(client):
        resolved = await client.resolve_session(session_id)
        key = _resolved_key(resolved, session_id)
        return await client.delete_sessions([key])

    result = asyncio.run(_with_client(_run))
    if result is _CLIENT_UNAVAILABLE:
        console.print("[dim]Session deletion requires a running gateway.[/dim]")
        return
    if result is _ACTION_FAILED:
        return
    console.print_json(data=result)


@app.command("export")
def sessions_export(
    session_id: str = typer.Argument(..., help="Session ID to export"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Output file"),
    format: str = typer.Option("md", "--format", help="Export format: md|json"),
) -> None:
    """Export session transcript and metadata.

    Uses the existing chat.history RPC for persisted transcript messages and
    falls back to session preview when no messages are available.
    """
    if format not in {"md", "json"}:
        console.print("[red]--format must be md or json[/red]")
        raise typer.Exit(2)

    async def _run(client):
        resolved = await client.resolve_session(session_id)
        key = _resolved_key(resolved, session_id)
        preview = await client.preview_sessions(keys=[key])
        history = await session_history_all(client.session_history, key)
        return {"resolved": resolved, "preview": preview, "history": history}

    result: dict[str, Any] | None = asyncio.run(_with_client(_run))
    if result is _CLIENT_UNAVAILABLE:
        console.print("[dim]Session export requires a running gateway.[/dim]")
        return
    if result is _ACTION_FAILED:
        return
    if result is None:
        console.print("[red]Session export returned no data.[/red]")
        return
    target = output or Path(f"{session_id.replace(':', '-')}.{format}")
    if format == "json":
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        resolved = result.get("resolved", {})
        key = _resolved_key(resolved, session_id)
        previews = result.get("preview", {}).get("previews", [])
        preview = previews[0] if previews else {}
        messages = result.get("history", {}).get("messages", [])
        transcript = messages_to_markdown(messages) if isinstance(messages, list) else ""
        if not transcript.strip():
            transcript = f"## Preview\n\n{preview.get('lastMessage', '')}\n"
        body = (
            f"# Session {key}\n\n"
            f"- Status: {resolved.get('status', '')}\n"
            f"- Model: {resolved.get('model') or ''}\n"
            f"- Updated: {resolved.get('updated_at', '')}\n\n"
            f"{transcript}"
        )
        target.write_text(body, encoding="utf-8")
    console.print(f"[green]Exported:[/green] {target}")
