"""Cron scheduler CLI commands backed by OpenSquilla gateway RPCs."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.table import Table

from opensquilla.cli.gateway_rpc import confirm_or_exit, run_gateway_sync
from opensquilla.cli.output import print_json
from opensquilla.cli.ui import ACCENT_HEADER, console

cron_app = typer.Typer(help="Inspect and manage scheduled OpenSquilla runs.")

_SESSION_TARGETS = {"isolated", "main", "current", "session"}
_JOB_KINDS = {"auto", "reminder", "agent_turn", "system_event"}
_WAKE_MODES = {"now", "next-heartbeat"}


def _validate_session_target(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in _SESSION_TARGETS:
        raise typer.BadParameter(
            "--session-target must be one of isolated, main, current, session"
        )
    return normalized


def _validate_job_kind(value: Any) -> str:
    if not isinstance(value, str):
        return "auto"
    normalized = value.strip().lower()
    if normalized not in _JOB_KINDS:
        raise typer.BadParameter(
            "--job-kind must be one of auto, reminder, agent_turn, system_event"
        )
    return normalized


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h|sec|secs|min|mins|hr|hrs)?\s*$")
_DURATION_UNIT_SECONDS = {
    "": 1.0,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
}


def _parse_duration_seconds(value: str | float | None) -> float | None:
    """Accept '30s', '5m', '1h', or a plain numeric string/float. Return seconds.

    None / empty string → None (caller decides default). Anything ambiguous
    raises typer.BadParameter so the user sees the failure at parse time.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip()
    if not raw:
        return None
    match = _DURATION_RE.match(raw.lower())
    if not match:
        raise typer.BadParameter(
            f"invalid duration {value!r}: expected '30s', '5m', '1h', or seconds"
        )
    qty = float(match.group(1))
    unit = match.group(2) or ""
    return qty * _DURATION_UNIT_SECONDS[unit]


def _build_schedule_param(
    *,
    expression: str | None,
    cron: str | None,
    every: str | int | float | None,
    at: str | None,
    tz: str | None,
) -> dict[str, Any]:
    sources = [
        ("expression", expression.strip() if isinstance(expression, str) else expression),
        ("cron", cron.strip() if isinstance(cron, str) else cron),
        ("every", every),
        ("at", at.strip() if isinstance(at, str) else at),
    ]
    provided = [(name, value) for name, value in sources if value not in (None, "")]
    if len(provided) != 1:
        raise typer.BadParameter(
            "provide exactly one schedule source: --expression, --cron, --every, or --at"
        )
    name, value = provided[0]
    if name == "expression":
        return {"expression": str(value)}
    if name == "cron":
        schedule: dict[str, Any] = {"kind": "cron", "expr": str(value)}
        if tz:
            schedule["tz"] = tz
        return {"schedule": schedule}
    if name == "every":
        seconds = _parse_duration_seconds(value)
        if seconds is None or seconds < 1:
            raise typer.BadParameter("--every must be a duration >= 1 second")
        if not seconds.is_integer():
            raise typer.BadParameter("--every must resolve to whole seconds")
        return {"schedule": {"kind": "every", "every_seconds": int(seconds)}}
    return {"schedule": {"kind": "at", "at": str(value)}}


def _build_optional_schedule_param(
    *,
    expression: str | None,
    cron: str | None,
    every: str | int | float | None,
    at: str | None,
    tz: str | None,
) -> dict[str, Any]:
    sources = [
        expression.strip() if isinstance(expression, str) else expression,
        cron.strip() if isinstance(cron, str) else cron,
        every,
        at.strip() if isinstance(at, str) else at,
    ]
    if all(value in (None, "") for value in sources):
        return {}
    if sum(1 for value in sources if value not in (None, "")) > 1:
        raise typer.BadParameter(
            "provide at most one schedule source: --expression, --cron, --every, or --at"
        )
    return _build_schedule_param(
        expression=expression,
        cron=cron,
        every=every,
        at=at,
        tz=tz,
    )


def _resolve_webhook_token(
    *,
    inline: str | None,
    env: str | None,
    path: str | None,
) -> str | None:
    """Resolve a webhook bearer token from the safest available source.

    Priority: --webhook-token-env > --webhook-token-file > --webhook-token.
    Multiple sources is a ValueError so scripts fail loud instead of guessing.
    Inline --webhook-token is supported but emits a warning because it leaks
    via shell history and process listings.
    """
    sources = [bool(env), bool(path), bool(inline)]
    if sum(sources) > 1:
        raise typer.BadParameter(
            "specify at most one of --webhook-token, --webhook-token-env, "
            "--webhook-token-file"
        )
    if env:
        value = os.environ.get(env)
        if not value:
            raise typer.BadParameter(
                f"--webhook-token-env: environment variable {env!r} is unset or empty"
            )
        return value
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise typer.BadParameter(f"--webhook-token-file: {exc}") from exc
    if inline:
        typer.echo(
            "warning: --webhook-token is visible in shell history and process "
            "listings; prefer --webhook-token-env NAME or --webhook-token-file PATH.",
            err=True,
        )
        return inline
    return None


def _build_failure_destination_dict(
    *,
    mode: str | None,
    channel: str | None,
    to: str | None,
    account: str | None,
    webhook_url: str | None,
    webhook_token: str | None,
) -> dict[str, Any] | None:
    """Translate --failure-* flags into a delivery.failureDestination dict.

    Returns None when no failure-* flag is set. Raises BadParameter when the
    selected mode is missing required fields (webhook URL for webhook mode,
    channel + recipient for channel mode).
    """
    any_failure_flag = any(
        bool(v) for v in (mode, channel, to, account, webhook_url, webhook_token)
    )
    if not any_failure_flag:
        return None
    if not mode:
        raise typer.BadParameter(
            "--failure-* flags require --failure-mode (channel or webhook)"
        )
    mode_norm = mode.strip().lower()
    if mode_norm not in ("channel", "webhook"):
        raise typer.BadParameter("--failure-mode must be 'channel' or 'webhook'")

    if mode_norm == "webhook":
        if not webhook_url:
            raise typer.BadParameter(
                "--failure-mode=webhook requires --failure-webhook-url"
            )
        fd: dict[str, Any] = {"mode": "webhook", "webhookUrl": webhook_url}
        if webhook_token:
            fd["webhookToken"] = webhook_token
        return fd

    # channel mode
    if webhook_url or webhook_token:
        raise typer.BadParameter(
            "--failure-webhook-* requires --failure-mode=webhook"
        )
    if not (channel or to):
        raise typer.BadParameter(
            "--failure-mode=channel requires --failure-channel and/or --failure-to"
        )
    fd = {"mode": "channel"}
    if channel:
        fd["channelName"] = channel.strip().lower()
    if to:
        fd["to"] = to
    if account:
        fd["accountId"] = account
    return fd


def _build_delivery_params(
    *,
    announce: bool,
    no_deliver: bool,
    channel: str | None,
    to: str | None,
    account: str | None,
    best_effort: bool,
    webhook_url: str | None,
    webhook_token: str | None,
    failure_destination: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Translate CLI delivery flags into a delivery dict for the cron.add RPC.

    Returns None when the user did not request any delivery override AND no
    failure_destination was provided — the backend then falls back to its
    own inference (session last_channel or none). When only a failure
    destination is set, returns ``{"failureDestination": {...}}`` so the
    backend attaches it to the inferred or default primary delivery.
    """
    declared = sum([announce, no_deliver, bool(webhook_url)])
    if declared > 1:
        raise typer.BadParameter(
            "choose at most one delivery mode: --announce, --no-deliver, "
            "or --webhook-url"
        )

    if webhook_url:
        delivery: dict[str, Any] = {"mode": "webhook", "webhookUrl": webhook_url}
        if webhook_token:
            delivery["webhookToken"] = webhook_token
        if best_effort:
            delivery["bestEffort"] = True
        if failure_destination is not None:
            delivery["failureDestination"] = failure_destination
        return delivery

    if webhook_token and not webhook_url:
        raise typer.BadParameter(
            "--webhook-token* requires --webhook-url"
        )

    if no_deliver:
        result: dict[str, Any] = {"mode": "none"}
        if failure_destination is not None:
            result["failureDestination"] = failure_destination
        return result

    # Channel-mode announce. 'last' is a CLI sentinel that means "let the
    # backend infer from the session's last route"; do not forward it as an
    # explicit channelName because the RPC's _parse_delivery_overrides treats
    # any channelName as an explicit override.
    channel_norm = (channel or "").strip().lower()
    has_target = bool(to) or (channel_norm not in ("", "last"))
    if not announce and not has_target and not best_effort and not account:
        if failure_destination is not None:
            # FD-only — backend keeps inferred primary delivery and attaches FD.
            return {"failureDestination": failure_destination}
        return None  # nothing requested

    delivery = {"mode": "announce"}
    if channel_norm and channel_norm != "last":
        delivery["channelName"] = channel_norm
    if to:
        delivery["to"] = to
    if account:
        delivery["accountId"] = account
    if best_effort:
        delivery["bestEffort"] = True
    if failure_destination is not None:
        delivery["failureDestination"] = failure_destination
    return delivery


def _job_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("jobs", [])
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _render_jobs(rows: list[dict[str, Any]], *, title: str = "Cron jobs") -> None:
    if not rows:
        typer.echo("No cron jobs.")
        return
    table = Table(title=title, show_header=True, header_style=ACCENT_HEADER)
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Enabled")
    table.add_column("Expression")
    table.add_column("Agent")
    table.add_column("Next run")
    table.add_column("Last run")
    table.add_column("Errors", justify="right")
    for row in rows:
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("name") or ""),
            str(row.get("enabled") or False),
            str(row.get("expression") or row.get("schedule_raw") or ""),
            str(row.get("agentId") or row.get("agent_id") or ""),
            str(row.get("next_run") or ""),
            str(row.get("last_run") or ""),
            str(row.get("error_count") or row.get("consecutive_errors") or 0),
        )
    console.print(table)


def _render_mapping(payload: dict[str, Any], *, title: str) -> None:
    table = Table(title=title, show_header=True, header_style=ACCENT_HEADER)
    table.add_column("Field")
    table.add_column("Value")
    for key, value in payload.items():
        table.add_row(str(key), str(value))
    console.print(table)


def _render_runs(rows: list[dict[str, Any]]) -> None:
    if not rows:
        typer.echo("No cron runs.")
        return
    table = Table(title="Cron runs", show_header=True, header_style=ACCENT_HEADER)
    table.add_column("ID")
    table.add_column("Started")
    table.add_column("Finished")
    table.add_column("Status")
    table.add_column("Duration ms", justify="right")
    table.add_column("Error")
    for row in rows:
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("started_at") or ""),
            str(row.get("finished_at") or ""),
            str(row.get("status") or ("ok" if row.get("success") else "error")),
            str(row.get("duration_ms") or ""),
            str(row.get("error") or ""),
        )
    console.print(table)


def _emit_success(payload: Any, *, json_output: bool, title: str) -> None:
    if json_output:
        print_json(payload)
    elif isinstance(payload, dict):
        _render_mapping(payload, title=title)
    else:
        typer.echo(str(payload))


@cron_app.command("list")
def cron_list(
    agent: str | None = typer.Option(None, "--agent", help="Filter by agent id"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List scheduled cron jobs."""

    async def _run(client):
        params: dict[str, Any] = {}
        if agent:
            params["agentId"] = agent
        return await client.call("cron.list", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    _render_jobs(_job_rows(payload))


@cron_app.command("status")
def cron_status(
    job_id: str = typer.Argument(..., help="Cron job id"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show one cron job."""

    async def _run(client):
        return await client.call("cron.status", {"id": job_id})

    payload = run_gateway_sync(_run, json_output=json_output)
    _emit_success(payload, json_output=json_output, title=f"Cron job {job_id}")


@cron_app.command("add")
def cron_add(
    expression: Annotated[
        str | None, typer.Option("--expression", help="Cron expression")
    ] = None,
    cron: Annotated[
        str | None, typer.Option("--cron", help="Cron expression schedule")
    ] = None,
    every: Annotated[
        str | None, typer.Option("--every", help="Fixed interval, e.g. 30s, 5m, 1h")
    ] = None,
    at: Annotated[
        str | None, typer.Option("--at", help="One-time ISO-8601 time with timezone")
    ] = None,
    text: str = typer.Option(..., "--text", help="Prompt text to run"),
    name: str | None = typer.Option(None, "--name", help="Display name"),
    agent: str | None = typer.Option(None, "--agent", help="Agent id"),
    job_kind: str = typer.Option(
        "auto",
        "--job-kind",
        help=(
            "Cron payload kind: auto, reminder, agent_turn, or system_event. "
            "auto creates static reminders for non-main targets and system events for main."
        ),
    ),
    session_target: str = typer.Option(
        "isolated",
        "--session-target",
        help="Target session mode: isolated, main, current, or session",
    ),
    timeout: float | None = typer.Option(None, "--timeout", help="Run timeout in seconds"),
    tz: str | None = typer.Option(
        None,
        "--tz",
        help=(
            "IANA timezone for cron expressions (e.g. 'America/Los_Angeles'). "
            "Empty/omitted keeps UTC."
        ),
    ),
    wake: str | None = typer.Option(
        None,
        "--wake",
        help="Wake mode for main-session jobs: now or next-heartbeat",
    ),
    exact: bool = typer.Option(
        False,
        "--exact",
        help="Fire exactly on schedule (jitter_seconds=0); overrides default stagger.",
    ),
    jitter: str | None = typer.Option(
        None,
        "--jitter",
        help=(
            "Explicit stagger (per-job). Accepts '30s', '5m', '1h', or a numeric "
            "second count. 0 == --exact; takes precedence over --exact."
        ),
    ),
    announce: bool = typer.Option(
        False, "--announce", help="Announce summary delivery (channel mode)."
    ),
    no_deliver: bool = typer.Option(
        False,
        "--no-deliver",
        help="Disable any delivery for this job.",
    ),
    channel: str | None = typer.Option(
        None,
        "--channel",
        help=(
            "Delivery channel (e.g. slack, feishu). 'last' or unset → let the "
            "backend infer from the session's last route."
        ),
    ),
    to: str | None = typer.Option(
        None, "--to", help="Delivery destination (channel-specific recipient)"
    ),
    account: str | None = typer.Option(
        None, "--account", help="Channel account id for delivery (multi-account setups)"
    ),
    best_effort_deliver: bool = typer.Option(
        False,
        "--best-effort-deliver",
        help="Do not fail the job when delivery fails",
    ),
    webhook_url: str | None = typer.Option(
        None,
        "--webhook-url",
        help="Webhook delivery URL (http/https); mutually exclusive with --announce/--no-deliver.",
    ),
    webhook_token: str | None = typer.Option(
        None,
        "--webhook-token",
        help=(
            "Webhook bearer token (visible in shell history; prefer "
            "--webhook-token-env or --webhook-token-file)."
        ),
    ),
    webhook_token_env: str | None = typer.Option(
        None,
        "--webhook-token-env",
        help="Read webhook bearer token from this environment variable.",
    ),
    webhook_token_file: str | None = typer.Option(
        None,
        "--webhook-token-file",
        help="Read webhook bearer token from this file (whitespace-trimmed).",
    ),
    failure_mode: str | None = typer.Option(
        None,
        "--failure-mode",
        help=(
            "Route failure alerts separately from primary delivery. "
            "One of: 'channel', 'webhook'."
        ),
    ),
    failure_channel: str | None = typer.Option(
        None,
        "--failure-channel",
        help="Failure-destination channel name (slack, feishu, …) for --failure-mode=channel.",
    ),
    failure_to: str | None = typer.Option(
        None,
        "--failure-to",
        help="Failure-destination recipient (channel-specific) for --failure-mode=channel.",
    ),
    failure_account: str | None = typer.Option(
        None,
        "--failure-account",
        help="Failure-destination channel account id (multi-account setups).",
    ),
    failure_webhook_url: str | None = typer.Option(
        None,
        "--failure-webhook-url",
        help="Failure-destination webhook URL (http/https) for --failure-mode=webhook.",
    ),
    failure_webhook_token: str | None = typer.Option(
        None,
        "--failure-webhook-token",
        help=(
            "Failure-destination webhook bearer token (visible in shell history; "
            "prefer --failure-webhook-token-env or --failure-webhook-token-file)."
        ),
    ),
    failure_webhook_token_env: str | None = typer.Option(
        None,
        "--failure-webhook-token-env",
        help="Read failure-destination webhook token from this environment variable.",
    ),
    failure_webhook_token_file: str | None = typer.Option(
        None,
        "--failure-webhook-token-file",
        help="Read failure-destination webhook token from this file (trimmed).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Add a scheduled cron job."""

    target = _validate_session_target(session_target)
    payload_kind = _validate_job_kind(job_kind)
    if payload_kind == "auto":
        payload_kind = "system_event" if target == "main" else "reminder"
    if payload_kind == "reminder" and target == "main":
        raise typer.BadParameter("--job-kind reminder cannot use --session-target main")
    if payload_kind == "agent_turn" and target == "main":
        raise typer.BadParameter("--job-kind agent_turn cannot use --session-target main")
    if payload_kind == "system_event" and target != "main":
        raise typer.BadParameter("--job-kind system_event requires --session-target main")
    if target == "current":
        raise typer.BadParameter(
            "--session-target current is only available from session-bound clients; "
            "use the WebUI/current chat surface or choose --session-target isolated"
        )
    params: dict[str, Any] = {
        **_build_schedule_param(
            expression=expression,
            cron=cron,
            every=every,
            at=at,
            tz=tz,
        ),
        "text": text,
        "payloadKind": payload_kind,
        "sessionTarget": target,
    }
    if name:
        params["name"] = name
    if agent:
        params["agentId"] = agent
    if timeout is not None:
        params["timeout"] = timeout
    if tz:
        params["tz"] = tz
    if wake is not None:
        wake_norm = wake.strip().lower()
        if wake_norm not in _WAKE_MODES:
            raise typer.BadParameter("--wake must be now or next-heartbeat")
        params["wakeMode"] = wake_norm

    jitter_seconds = _parse_duration_seconds(jitter)
    if jitter_seconds is not None:
        params["jitterSeconds"] = max(0.0, jitter_seconds)
    elif exact:
        params["exact"] = True

    token = _resolve_webhook_token(
        inline=webhook_token,
        env=webhook_token_env,
        path=webhook_token_file,
    )
    failure_token = _resolve_webhook_token(
        inline=failure_webhook_token,
        env=failure_webhook_token_env,
        path=failure_webhook_token_file,
    )
    failure_destination = _build_failure_destination_dict(
        mode=failure_mode,
        channel=failure_channel,
        to=failure_to,
        account=failure_account,
        webhook_url=failure_webhook_url,
        webhook_token=failure_token,
    )
    delivery = _build_delivery_params(
        announce=announce,
        no_deliver=no_deliver,
        channel=channel,
        to=to,
        account=account,
        best_effort=best_effort_deliver,
        webhook_url=webhook_url,
        webhook_token=token,
        failure_destination=failure_destination,
    )
    if delivery is not None:
        params["delivery"] = delivery

    async def _run(client):
        return await client.call("cron.add", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    _emit_success(payload, json_output=json_output, title="Cron job added")


@cron_app.command("update")
def cron_update(
    job_id: str = typer.Argument(..., help="Cron job id"),
    expression: Annotated[
        str | None, typer.Option("--expression", help="Cron expression")
    ] = None,
    cron: Annotated[
        str | None, typer.Option("--cron", help="Cron expression schedule")
    ] = None,
    every: Annotated[
        str | None, typer.Option("--every", help="Fixed interval, e.g. 30s, 5m, 1h")
    ] = None,
    at: Annotated[
        str | None, typer.Option("--at", help="One-time ISO-8601 time with timezone")
    ] = None,
    text: str | None = typer.Option(None, "--text", help="Prompt text to run"),
    name: str | None = typer.Option(None, "--name", help="Display name"),
    enabled: bool | None = typer.Option(None, "--enabled/--disabled", help="Enable/disable job"),
    timeout: float | None = typer.Option(None, "--timeout", help="Run timeout in seconds"),
    tz: str | None = typer.Option(
        None,
        "--tz",
        help=(
            "Update IANA timezone for cron expressions. Pass an empty string to clear "
            "(revert to UTC matching)."
        ),
    ),
    wake: str | None = typer.Option(
        None,
        "--wake",
        help="Wake mode: now or next-heartbeat",
    ),
    failure_mode: str | None = typer.Option(
        None,
        "--failure-mode",
        help=(
            "Patch the failure-alert route. One of: 'channel', 'webhook'. "
            "Other delivery fields (primary channel/webhook) are not patchable "
            "from this CLI — remove + re-add to repoint primary delivery."
        ),
    ),
    failure_channel: str | None = typer.Option(
        None,
        "--failure-channel",
        help="Failure-destination channel name for --failure-mode=channel.",
    ),
    failure_to: str | None = typer.Option(
        None,
        "--failure-to",
        help="Failure-destination recipient for --failure-mode=channel.",
    ),
    failure_account: str | None = typer.Option(
        None,
        "--failure-account",
        help="Failure-destination channel account id (multi-account setups).",
    ),
    failure_webhook_url: str | None = typer.Option(
        None,
        "--failure-webhook-url",
        help="Failure-destination webhook URL for --failure-mode=webhook.",
    ),
    failure_webhook_token: str | None = typer.Option(
        None,
        "--failure-webhook-token",
        help=(
            "Failure-destination webhook bearer token (prefer "
            "--failure-webhook-token-env or --failure-webhook-token-file)."
        ),
    ),
    failure_webhook_token_env: str | None = typer.Option(
        None,
        "--failure-webhook-token-env",
        help="Read failure-destination webhook token from this environment variable.",
    ),
    failure_webhook_token_file: str | None = typer.Option(
        None,
        "--failure-webhook-token-file",
        help="Read failure-destination webhook token from this file (trimmed).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Update a scheduled cron job.

    Primary delivery (channel / webhook URL) is intentionally NOT patchable
    from this CLI — remove + re-add when those need to change. Failure
    destination IS patchable via the --failure-* flags.
    """

    params: dict[str, Any] = {"id": job_id}
    params.update(
        _build_optional_schedule_param(
            expression=expression,
            cron=cron,
            every=every,
            at=at,
            tz=tz,
        )
    )
    if text is not None:
        params["text"] = text
    if name is not None:
        params["name"] = name
    if enabled is not None:
        params["enabled"] = enabled
    if timeout is not None:
        params["timeout"] = timeout
    if tz is not None:
        params["tz"] = tz
    if wake is not None:
        wake_norm = wake.strip().lower()
        if wake_norm not in _WAKE_MODES:
            raise typer.BadParameter("--wake must be now or next-heartbeat")
        params["wakeMode"] = wake_norm

    failure_token = _resolve_webhook_token(
        inline=failure_webhook_token,
        env=failure_webhook_token_env,
        path=failure_webhook_token_file,
    )
    failure_destination = _build_failure_destination_dict(
        mode=failure_mode,
        channel=failure_channel,
        to=failure_to,
        account=failure_account,
        webhook_url=failure_webhook_url,
        webhook_token=failure_token,
    )
    if failure_destination is not None:
        params["delivery"] = {"failureDestination": failure_destination}

    if len(params) == 1:
        raise typer.BadParameter("provide at least one field to update")

    async def _run(client):
        return await client.call("cron.update", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    _emit_success(payload, json_output=json_output, title="Cron job updated")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Cron job id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Remove a scheduled cron job."""

    confirm_or_exit(f"Remove cron job {job_id!r}?", yes=yes, json_output=json_output)

    async def _run(client):
        await client.call("cron.remove", {"id": job_id})
        return {"id": job_id, "removed": True}

    payload = run_gateway_sync(_run, json_output=json_output)
    _emit_success(payload, json_output=json_output, title="Cron job removed")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Cron job id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Run a scheduled cron job now."""

    confirm_or_exit(
        f"Run cron job {job_id!r} now? This may post into a live session or channel.",
        yes=yes,
        json_output=json_output,
    )

    async def _run(client):
        return await client.call("cron.run", {"id": job_id})

    payload = run_gateway_sync(_run, json_output=json_output)
    _emit_success(payload, json_output=json_output, title="Cron run result")


@cron_app.command("runs")
def cron_runs(
    job_id: str = typer.Argument(..., help="Cron job id"),
    limit: int = typer.Option(20, "--limit", "-n", help="Maximum rows"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List recent runs for a cron job."""

    async def _run(client):
        return await client.call("cron.runs", {"id": job_id, "limit": limit})

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    _render_runs(_job_rows(payload))
