"""OpenSquilla CLI — Typer app with sub-commands."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer

from opensquilla.cli.stdio import configure_stdio_for_unicode
from opensquilla.env import load_env, warn_if_proxy_ignored
from opensquilla.paths import default_opensquilla_home, is_valid_profile_name

configure_stdio_for_unicode()

_LOADED_ENV_CONTEXTS: set[tuple[Path, Path]] = set()
_LOADED_LOCAL_ENV_CWDS: set[Path] = set()


def _top_level_command_index(argv: list[str]) -> int | None:
    index = 1
    while index < len(argv):
        value = argv[index]
        if value == "--":
            return index + 1 if index + 1 < len(argv) else None
        if value == "--profile":
            index += 2
            continue
        if value.startswith("--profile=") or value.startswith("-"):
            index += 1
            continue
        return index
    return None


def _top_level_command(argv: list[str]) -> str | None:
    """Return the top-level command before ordinary CLI bootstrap runs."""

    index = _top_level_command_index(argv)
    return argv[index] if index is not None else None


def _profile_from_top_level_argv(argv: list[str]) -> str | None:
    """Return a top-level ``--profile`` value without consuming subcommand flags."""
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return None
        if arg.startswith("--profile="):
            return arg.partition("=")[2].strip() or None
        if arg == "--profile":
            if index + 1 < len(argv):
                return argv[index + 1].strip() or None
            return None
        if not arg.startswith("-"):
            return None
        index += 1
    return None


def _is_offline_import_verification(argv: list[str]) -> bool:
    """Recognize the internal receipt verifier before dotenv bootstrap."""

    index = _top_level_command_index(argv)
    return (
        index is not None
        and argv[index] == "migrate"
        and index + 1 < len(argv)
        and argv[index + 1] == "verify-opensquilla-import"
    )


def _activate_profile(profile: str | None) -> None:
    if profile is not None:
        name = profile.strip()
        if name:
            if not is_valid_profile_name(name):
                raise typer.BadParameter(
                    "use lowercase letters, digits, hyphens, or underscores; "
                    "start with a letter or digit; max length 64"
                )
            os.environ["OPENSQUILLA_PROFILE"] = name


def _preactivate_profile_from_argv(argv: list[str]) -> None:
    profile = _profile_from_top_level_argv(argv)
    if profile and is_valid_profile_name(profile):
        os.environ["OPENSQUILLA_PROFILE"] = profile


def _load_env_for_active_home() -> None:
    cwd = Path.cwd()
    if cwd not in _LOADED_LOCAL_ENV_CWDS:
        load_env(cwd=cwd, include_home=False)
        _LOADED_LOCAL_ENV_CWDS.add(cwd)

    try:
        home = default_opensquilla_home()
    except ValueError:
        home = Path.home() / ".opensquilla"
    context = (cwd, home)
    if context in _LOADED_ENV_CONTEXTS:
        return
    load_env(cwd=cwd, home=home)
    _LOADED_ENV_CONTEXTS.add(context)


_preactivate_profile_from_argv(sys.argv)

_RECOVERY_OFFLINE = (
    os.environ.get("OPENSQUILLA_RECOVERY_OFFLINE", "").strip().lower()
    in {"1", "true", "yes", "on"}
    or _top_level_command(sys.argv) == "recovery"
    or _is_offline_import_verification(sys.argv)
)
if _RECOVERY_OFFLINE:
    # Electron also sets this explicitly. argv detection keeps direct CLI use
    # on the same path before dotenv or the selected profile can influence it.
    os.environ["OPENSQUILLA_RECOVERY_OFFLINE"] = "1"

# Populate os.environ from .env files before any submodule import reads keys.
# Precedence: os.environ > $CWD/.env.test during tests > $CWD/.env
# > $CWD/.env.test fallback outside tests > selected OpenSquilla home/.env.
if not _RECOVERY_OFFLINE:
    _load_env_for_active_home()
    warn_if_proxy_ignored()

from opensquilla.cli.agent_cmd import run_agent_command  # noqa: E402
from opensquilla.cli.agents_cmd import agents_app  # noqa: E402
from opensquilla.cli.bundle_cmd import bundle_command  # noqa: E402
from opensquilla.cli.channels_cmd import channels_app  # noqa: E402
from opensquilla.cli.codetask_cmd import codetask_app  # noqa: E402
from opensquilla.cli.config_cmd import app as config_app  # noqa: E402
from opensquilla.cli.cost_cmd import app as cost_app  # noqa: E402
from opensquilla.cli.cron_cmd import cron_app  # noqa: E402
from opensquilla.cli.diagnostics_cmd import diagnostics_app  # noqa: E402
from opensquilla.cli.dist_cmd import app as dist_app  # noqa: E402
from opensquilla.cli.doctor_cmd import doctor_command  # noqa: E402
from opensquilla.cli.ensemble_cmd import ensemble_app  # noqa: E402
from opensquilla.cli.init_cmd import init_command  # noqa: E402
from opensquilla.cli.mcp_server_cmd import app as mcp_server_app  # noqa: E402
from opensquilla.cli.memory_flush_cmd import memory_flush_session_cmd  # noqa: E402
from opensquilla.cli.migrate_cmd import migrate_app  # noqa: E402
from opensquilla.cli.models_cmd import app as models_app  # noqa: E402
from opensquilla.cli.onboard_cmd import configure_command, onboard_app  # noqa: E402
from opensquilla.cli.providers_cmd import providers_app  # noqa: E402
from opensquilla.cli.recovery_cmd import recovery_app  # noqa: E402
from opensquilla.cli.replay import replay_app  # noqa: E402
from opensquilla.cli.router_cmd import router_app  # noqa: E402
from opensquilla.cli.sandbox_cmd import sandbox_app  # noqa: E402
from opensquilla.cli.search_cmd import search_app  # noqa: E402
from opensquilla.cli.sessions_cmd import app as sessions_app  # noqa: E402
from opensquilla.cli.skills_cmd import skills_app  # noqa: E402
from opensquilla.cli.swebench_cmd import swebench_app  # noqa: E402
from opensquilla.cli.uninstall_cmd import uninstall_command  # noqa: E402
from opensquilla.observability.cli_logging import configure_cli_structlog  # noqa: E402

app = typer.Typer(
    name="opensquilla",
    help="OpenSquilla - Python agent runtime with multi-channel support.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)


@app.callback()
def _main_callback(
    profile: str | None = typer.Option(
        None,
        "--profile",
        envvar="OPENSQUILLA_PROFILE",
        help="Use a named OpenSquilla profile home.",
    ),
) -> None:
    # Route structlog output on CLI paths to stderr (WARNING+) so command
    # stdout stays clean; the gateway bridge and interactive TUI install
    # their own richer configurations over this default when they run.
    configure_cli_structlog()
    _activate_profile(profile)
    if not _RECOVERY_OFFLINE:
        _load_env_for_active_home()
        warn_if_proxy_ignored()

# ── Sub-apps ─────────────────────────────────────────────────────────────────

app.add_typer(channels_app, name="channels")
app.add_typer(agents_app, name="agents")
app.add_typer(config_app, name="config")
app.add_typer(cost_app, name="cost")
app.add_typer(diagnostics_app, name="diagnostics")
app.add_typer(cron_app, name="cron")
app.add_typer(dist_app, name="dist")
app.add_typer(mcp_server_app, name="mcp-server")
app.add_typer(migrate_app, name="migrate")
app.add_typer(recovery_app, name="recovery")
app.add_typer(models_app, name="models")
app.add_typer(ensemble_app, name="ensemble")
app.add_typer(providers_app, name="providers")
app.add_typer(router_app, name="router")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(search_app, name="search")
app.add_typer(sessions_app, name="sessions")
app.add_typer(skills_app, name="skills")
app.add_typer(swebench_app, name="swebench")
app.add_typer(codetask_app, name="code-task")

app.command("init")(init_command)
app.command("doctor")(doctor_command)
app.command("bundle")(bundle_command)
app.command("uninstall")(uninstall_command)
app.add_typer(onboard_app, name="onboard")
app.command("configure")(configure_command)


# ── memory sub-app ────────────────────────────────────────────────────────────

memory_app = typer.Typer(help="Memory subsystem commands.")
app.add_typer(memory_app, name="memory")
raw_fallbacks_app = typer.Typer(help="Raw fallback receipt commands.")
memory_app.add_typer(raw_fallbacks_app, name="raw-fallbacks")
repair_app = typer.Typer(help="Compaction memory repair commands.")
memory_app.add_typer(repair_app, name="repair")


def _build_cli_dream(agent: str, *, force: bool = False, need_provider: bool = True):
    """Assemble a Dream instance for CLI runs.

    Uses the same configured agent workspace resolver as gateway Dream runs.
    Unit tests monkeypatch this function to inject a mock Dream without
    touching provider wiring. When ``need_provider`` is False (e.g. ``--status``
    / ``--reset-cursor``), skip provider construction so the command works
    offline.
    """
    import os

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.memory.dream_factory import build_dream_factory

    gw = GatewayConfig.load(os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))

    dream = build_dream_factory(
        config=gw,
        turn_runner=None,
        need_provider=need_provider,
    )
    dream_obj = dream(agent)
    if force:
        dream_obj.cursor.reset()
    return dream_obj


@memory_app.command("status")
def memory_status_cmd(
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    deep: bool = typer.Option(False, "--deep", help="Include detailed retrieval health"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Show read-only memory backend status from the running gateway."""

    from rich.table import Table

    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json
    from opensquilla.cli.ui import console

    async def _run(client):
        params: dict[str, object] = {"agentId": agent_id}
        if deep:
            params["deep"] = True
        return await client.call("doctor.memory.status", params)

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Memory status — agent={agent_id}", show_header=True)
    table.add_column("Backend")
    table.add_column("Status")
    table.add_column("Entries", justify="right")
    table.add_column("Size bytes", justify="right")
    table.add_column("Sources")
    table.add_column("Error")
    source_counts = payload.get("sourceCounts") or {}
    source_summary = ", ".join(
        f"{source} {counts.get('files', 0)} files/{counts.get('chunks', 0)} chunks"
        for source, counts in sorted(source_counts.items())
        if isinstance(counts, dict)
    )
    table.add_row(
        str(payload.get("backend") or ""),
        str(payload.get("status") or ""),
        "" if payload.get("entryCount") is None else str(payload.get("entryCount")),
        "" if payload.get("sizeBytes") is None else str(payload.get("sizeBytes")),
        source_summary,
        str(payload.get("error") or ""),
    )
    console.print(table)


@memory_app.command("index")
def memory_index_cmd(
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    force: bool = typer.Option(False, "--force", help="Rebuild index rows and rescan sources"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Sync or force-rebuild the memory search index through the gateway."""

    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json
    from opensquilla.cli.ui import console

    async def _run(client):
        return await client.call("memory.index", {"agentId": agent_id, "force": force})

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    console.print(
        f"memory index agent={payload.get('agentId', agent_id)} "
        f"force={bool(payload.get('force'))}"
    )


@memory_app.command("list")
def memory_list_cmd(
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List durable memory source files from the running gateway."""

    from rich.table import Table

    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json
    from opensquilla.cli.ui import console

    async def _run(client):
        return await client.call("memory.list", {"agentId": agent_id})

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Memory sources - agent={agent_id}", show_header=True)
    table.add_column("Path")
    table.add_column("Lines", justify="right")
    table.add_column("Size bytes", justify="right")
    table.add_column("Modified")
    for row in payload.get("files", []):
        table.add_row(
            str(row.get("path") or ""),
            "" if row.get("lineCount") is None else str(row.get("lineCount")),
            "" if row.get("sizeBytes") is None else str(row.get("sizeBytes")),
            str(row.get("modifiedAt") or ""),
        )
    console.print(table)


@memory_app.command("search")
def memory_search_cmd(
    query: str = typer.Argument(..., help="Search query"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results"),
    source: str = typer.Option(
        "memory",
        "--source",
        help="Search source: memory, sessions, or all",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Search durable memory from the running gateway."""

    from rich.table import Table

    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json
    from opensquilla.cli.ui import console

    async def _run(client):
        return await client.call(
            "memory.search",
            {"query": query, "agentId": agent_id, "limit": limit, "source": source},
        )

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Memory search - agent={agent_id}", show_header=True)
    table.add_column("Source")
    table.add_column("Path")
    table.add_column("Lines")
    table.add_column("Score", justify="right")
    table.add_column("Snippet")
    for row in payload.get("results", []):
        table.add_row(
            str(row.get("source") or "memory"),
            str(row.get("path") or ""),
            f"{row.get('startLine', '')}-{row.get('endLine', '')}",
            f"{float(row.get('score') or 0.0):.3f}",
            str(row.get("snippet") or "")[:120],
        )
    console.print(table)


@memory_app.command("show")
def memory_show_cmd(
    path: str = typer.Argument(..., help="Memory source path"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    from_line: int | None = typer.Option(None, "--from-line", help="Start line, 1-indexed"),
    lines: int | None = typer.Option(None, "--lines", help="Number of lines to return"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show one durable memory source from the running gateway."""

    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json
    from opensquilla.cli.ui import console

    async def _run(client):
        params: dict[str, object] = {"path": path, "agentId": agent_id}
        if from_line is not None:
            params["fromLine"] = from_line
        if lines is not None:
            params["lines"] = lines
        return await client.call("memory.show", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    console.print(str(payload.get("content") or ""))
    if payload.get("truncated"):
        console.print("[dim]... truncated[/dim]")


@raw_fallbacks_app.command("list")
def memory_raw_fallbacks_list_cmd(
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List raw fallback receipts from the running gateway."""

    from rich.table import Table

    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json
    from opensquilla.cli.ui import console

    async def _run(client):
        return await client.call("memory.raw_fallbacks.list", {"agentId": agent_id})

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Raw memory fallbacks - agent={agent_id}", show_header=True)
    table.add_column("Path")
    table.add_column("Size bytes", justify="right")
    table.add_column("Reason")
    table.add_column("Modified")
    for row in payload.get("files", []):
        table.add_row(
            str(row.get("path") or ""),
            "" if row.get("sizeBytes") is None else str(row.get("sizeBytes")),
            str(row.get("reason") or ""),
            str(row.get("modifiedAt") or ""),
        )
    console.print(table)


@raw_fallbacks_app.command("show")
def memory_raw_fallbacks_show_cmd(
    path: str = typer.Argument(..., help="Raw fallback path"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    from_line: int | None = typer.Option(None, "--from-line", help="Start line, 1-indexed"),
    lines: int | None = typer.Option(None, "--lines", help="Number of lines to return"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show one raw fallback receipt from the running gateway."""

    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json
    from opensquilla.cli.ui import console

    async def _run(client):
        params: dict[str, object] = {"path": path, "agentId": agent_id}
        if from_line is not None:
            params["fromLine"] = from_line
        if lines is not None:
            params["lines"] = lines
        return await client.call("memory.raw_fallbacks.show", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    console.print(str(payload.get("content") or ""))
    if payload.get("truncated"):
        console.print("[dim]... truncated[/dim]")


@repair_app.command("list")
def memory_repair_list_cmd(
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum pending repairs"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """List degraded compaction records pending repair."""

    from rich.table import Table

    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json
    from opensquilla.cli.ui import console

    async def _run(client):
        return await client.call(
            "memory.repair.list",
            {"agentId": agent_id, "limit": limit},
        )

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Memory repair queue - agent={agent_id}", show_header=True)
    table.add_column("Summary")
    table.add_column("Session")
    table.add_column("Compaction")
    table.add_column("Status")
    table.add_column("Removed", justify="right")
    for row in payload.get("items", []):
        table.add_row(
            str(row.get("summaryId") or ""),
            str(row.get("sessionKey") or ""),
            str(row.get("compactionId") or ""),
            str(row.get("flushReceiptStatus") or ""),
            "" if row.get("removedCount") is None else str(row.get("removedCount")),
        )
    console.print(table)


@repair_app.command("show")
def memory_repair_show_cmd(
    summary_id: int | None = typer.Option(None, "--summary-id", help="Repair summary id"),
    session_key: str = typer.Option("", "--session-key", help="Session key to inspect"),
    compaction_id: str = typer.Option("", "--compaction-id", help="Compaction id to inspect"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    entry_limit: int = typer.Option(
        20,
        "--entry-limit",
        min=1,
        help="Maximum preimage entries",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show archived preimage entries for one degraded compaction."""

    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json
    from opensquilla.cli.ui import console

    async def _run(client):
        params: dict[str, object] = {"agentId": agent_id}
        if summary_id is not None:
            params["summaryId"] = summary_id
        if session_key:
            params["sessionKey"] = session_key
        if compaction_id:
            params["compactionId"] = compaction_id
        if entry_limit != 20:
            params["entryLimit"] = entry_limit
        return await client.call("memory.repair.show", params)

    payload = run_gateway_sync(_run, json_output=json_output)
    if json_output:
        print_json(payload)
        return
    for row in payload.get("entries", []):
        console.print(f"[{row.get('role', '')}] {row.get('content', '')}")


@repair_app.command("run")
def memory_repair_run_cmd(
    summary_id: int | None = typer.Option(None, "--summary-id", help="Repair summary id"),
    session_key: str = typer.Option("", "--session-key", help="Session key to repair"),
    compaction_id: str = typer.Option("", "--compaction-id", help="Compaction id to repair"),
    agent_id: str = typer.Option("main", "--agent", help="Agent id (default: main)"),
    limit: int = typer.Option(50, "--limit", min=1, help="Maximum repairs to run"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    config_path: Path | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Retry extraction from archived compaction preimages."""

    from rich.table import Table

    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json
    from opensquilla.cli.ui import console

    async def _run(client):
        params: dict[str, object] = {"agentId": agent_id, "limit": limit}
        if summary_id is not None:
            params["summaryId"] = summary_id
        if session_key:
            params["sessionKey"] = session_key
        if compaction_id:
            params["compactionId"] = compaction_id
        return await client.call("memory.repair.run", params)

    payload = run_gateway_sync(_run, json_output=json_output, config_path=config_path)
    if json_output:
        print_json(payload)
        return

    table = Table(title=f"Memory repair run - agent={agent_id}", show_header=True)
    table.add_column("Session")
    table.add_column("Compaction")
    table.add_column("Status")
    table.add_column("Reason")
    for row in payload.get("results", []):
        table.add_row(
            str(row.get("sessionKey") or ""),
            str(row.get("compactionId") or ""),
            str(row.get("status") or ""),
            str(row.get("reason") or ""),
        )
    console.print(table)


@memory_app.command("dream")
def memory_dream_cmd(
    agent: str = typer.Option("main", "--agent", "-a", help="Agent ID"),
    force: bool = typer.Option(False, "--force", help="Reset cursor and process all files"),
    status: bool = typer.Option(False, "--status", help="Show cursor + pending file count, no run"),
    reset_cursor: bool = typer.Option(False, "--reset-cursor", help="Clear cursor file, no run"),
) -> None:
    """Run Dream consolidation for an agent."""
    import asyncio

    need_provider = not (status or reset_cursor)
    dream = _build_cli_dream(agent, force=force, need_provider=need_provider)
    if reset_cursor:
        dream.cursor.reset()
        typer.echo(f"reset cursor for agent={agent}")
        return
    if status:
        cursor = dream.cursor.load()
        pending = dream.pending_candidate_count()
        typer.echo(
            f"agent={agent} cursor={cursor} pending={pending} "
            f"memory_md_exists={dream.memory_md.exists()}"
        )
        return
    result = asyncio.run(dream.run())
    typer.echo(
        f"dream agent={agent} "
        f"processed={result.files_processed} "
        f"evidence={result.evidence_status} "
        f"apply={result.apply_status}"
    )
    if result.error:
        typer.echo(f"error: {result.error}", err=True)
        raise typer.Exit(code=1)


memory_app.command("flush-session")(memory_flush_session_cmd)


# ── gateway sub-app ───────────────────────────────────────────────────────────

gateway_app = typer.Typer(
    help="Gateway server commands.",
    pretty_exceptions_enable=False,
)
app.add_typer(gateway_app, name="gateway")

GATEWAY_PROFILE_IN_USE_MARKER = "OPENSQUILLA_PROFILE_IN_USE"


@gateway_app.command("run")
def gateway_run(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Port to bind (default: config port, usually 18791)",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Host to bind (default: config host, usually 127.0.0.1)",
    ),
    listen: str = typer.Option(
        "",
        "--listen",
        help="Host to bind (alias of --bind; wins over --bind when both supplied)",
    ),
    debug: bool = typer.Option(False, "--debug", help="Enable debug mode"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
) -> None:
    """Start the ASGI gateway server.

    Precedence for the bind address: --listen > --bind > OPENSQUILLA_LISTEN >
    OPENSQUILLA_GATEWAY_HOST > default (127.0.0.1). Binding to 0.0.0.0 or :: is
    opt-in only — the gateway's default auth assumes loopback scope.
    """
    from opensquilla.cli.gateway_cmd import run_gateway
    from opensquilla.gateway.desktop_ownership import (
        release_active_desktop_gateway_ownership,
    )
    from opensquilla.recovery import (
        LegacyGatewayRunningError,
        ProfileLockBusyError,
        guarded_desktop_profile,
    )

    # The child that owns the gateway retains both the RC4 profile lock and
    # the legacy gateway lease for its complete write-capable lifetime.
    try:
        try:
            with guarded_desktop_profile():
                run_gateway(
                    port=port,
                    bind=bind,
                    listen=listen,
                    debug=debug,
                    config_path=config_path,
                )
        except (ProfileLockBusyError, LegacyGatewayRunningError):
            # This marker is consumed by the Desktop launcher. Keep both lines
            # independent of the profile path carried by the lock exception: paths
            # may contain user names, and an actionable startup failure does not
            # need to expose them or a traceback.
            typer.echo(GATEWAY_PROFILE_IN_USE_MARKER)
            typer.echo(
                "Gateway could not start: Another OpenSquilla process is still using "
                "this profile. Quit every OpenSquilla app or terminal using it, then "
                "try again; if no process will exit, restart the computer. Do not "
                "delete profile lock files."
            )
            raise typer.Exit(code=1) from None
    finally:
        # The record remains available throughout shutdown and disappears only
        # after guarded_desktop_profile has released the profile writer lease.
        # A permanent sidecar lock serializes exact-record comparison/removal
        # with a successor's atomic replacement in the handoff window.
        release_active_desktop_gateway_ownership()


@gateway_app.command("start")
def gateway_start(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Port to bind (default: config port, usually 18791)",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Host to bind (default: config host, usually 127.0.0.1)",
    ),
    listen: str = typer.Option("", "--listen", help="Host to bind (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
    health_timeout: float = typer.Option(60.0, "--timeout", help="Readiness wait timeout"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Start the gateway in the background and wait for readiness."""
    from opensquilla.cli.gateway_cmd import start_gateway

    start_gateway(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        health_timeout=health_timeout,
        json_output=json_output,
    )


@gateway_app.command("status")
def gateway_status(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Port to inspect (default: config port, usually 18791)",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Host to inspect (default: config host, usually 127.0.0.1)",
    ),
    listen: str = typer.Option("", "--listen", help="Host to inspect (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
    gateway_url: str | None = typer.Option(
        None,
        "--gateway",
        help="Remote gateway URL to inspect instead of local lifecycle state.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Inspect the managed gateway process without mutating state."""
    from opensquilla.cli.gateway_cmd import status_gateway

    status_gateway(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        gateway_url=gateway_url,
        json_output=json_output,
    )


@gateway_app.command("stop")
def gateway_stop(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Port to stop (default: config port, usually 18791)",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Host to stop (default: config host, usually 127.0.0.1)",
    ),
    listen: str = typer.Option("", "--listen", help="Host to stop (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
    shutdown_timeout: float = typer.Option(10.0, "--timeout", help="Shutdown wait timeout"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Stop the recorded gateway process."""
    from opensquilla.cli.gateway_cmd import stop_gateway

    stop_gateway(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        shutdown_timeout=shutdown_timeout,
        json_output=json_output,
    )


@gateway_app.command("restart")
def gateway_restart(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        help="Port to restart (default: config port, usually 18791)",
    ),
    bind: str | None = typer.Option(
        None,
        "--bind",
        "-b",
        help="Host to restart (default: config host, usually 127.0.0.1)",
    ),
    listen: str = typer.Option("", "--listen", help="Host to restart (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
    health_timeout: float = typer.Option(60.0, "--timeout", help="Readiness wait timeout"),
    shutdown_timeout: float = typer.Option(
        10.0, "--shutdown-timeout", help="Shutdown wait timeout"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Restart the recorded gateway process."""
    from opensquilla.cli.gateway_cmd import restart_gateway

    restart_gateway(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        health_timeout=health_timeout,
        shutdown_timeout=shutdown_timeout,
        json_output=json_output,
    )


@gateway_app.command("reload")
def gateway_reload(
    config_path: str | None = typer.Option(None, "--config", help="Override config path."),
    gateway_url: str | None = typer.Option(
        None,
        "--gateway",
        help="Gateway WebSocket URL to call (default: resolved from config).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Re-read the on-disk config into the running gateway without a restart.

    Hand-edited TOML is only read at boot; this hot-applies it via the
    admin `config.reload` RPC. Channel, memory-embedding, and sandbox
    posture changes still require `opensquilla gateway restart`.
    """
    from opensquilla.cli.gateway_cmd import reload_gateway

    reload_gateway(
        config_path=config_path,
        gateway_url=gateway_url,
        json_output=json_output,
    )


# ── replay sub-app ────────────────────────────────────────────────────────────

app.add_typer(replay_app, name="replay")


# ── top-level commands ────────────────────────────────────────────────────────


@app.command("agent")
def agent(
    message: str = typer.Option(..., "--message", "-m", help="Message to send"),
    agent_id: str = typer.Option("main", "--agent", help="Agent identifier"),
    session_id: str = typer.Option("", "--session-id", help="Session key/id to use"),
    model: str = typer.Option("", "--model", help="Model override"),
    workspace: str = typer.Option("", "--workspace", help="Workspace root for this run"),
    workspace_strict: bool | None = typer.Option(
        None,
        "--workspace-strict/--no-workspace-strict",
        help="Restrict read-side file tools to --workspace",
    ),
    workspace_lockdown: bool = typer.Option(
        False,
        "--workspace-lockdown",
        help=(
            "Opt in to automation write containment: writes must stay under "
            "--workspace or --scratch-dir."
        ),
    ),
    workspace_lockdown_deny_paths: list[str] = typer.Option(
        [],
        "--workspace-lockdown-deny-paths",
        help=(
            "Workspace-relative write deny glob(s) for automation containment; "
            "repeat or comma-separate."
        ),
    ),
    scratch_dir: str = typer.Option(
        "",
        "--scratch-dir",
        help="Directory for temporary scripts, logs, debug output, and candidate patches.",
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", "-T", help="Total agent timeout in seconds (0=unlimited)"
    ),
    max_iterations: int | None = typer.Option(
        None,
        "--max-iterations",
        min=0,
        help="Maximum agent model/tool loop iterations (0=unlimited)",
    ),
    iteration_timeout_seconds: float | None = typer.Option(
        None,
        "--iteration-timeout-seconds",
        help="Per-iteration timeout in seconds (one LLM call + its tool executions)",
    ),
    tool_timeout_seconds: float | None = typer.Option(
        None,
        "--tool-timeout-seconds",
        help="Per-tool execution timeout in seconds",
    ),
    request_timeout_seconds: float | None = typer.Option(
        None,
        "--request-timeout-seconds",
        help="Single LLM HTTP/streaming request timeout in seconds",
    ),
    max_provider_retries: int | None = typer.Option(
        None,
        "--max-provider-retries",
        min=0,
        help="Maximum provider-level retries for transient errors",
    ),
    length_capped_continuations: int | None = typer.Option(
        None,
        "--length-capped-continuations",
        min=1,
        help="Maximum automatic continuations after provider output reaches its length limit",
    ),
    thinking: str = typer.Option(
        "",
        "--thinking",
        help="Thinking level override: off|minimal|low|medium|high|xhigh|adaptive",
    ),
    transcript_path: str = typer.Option(
        "", "--transcript-path", help="Write benchmark-compatible JSONL transcript"
    ),
    usage_path: str = typer.Option("", "--usage-path", help="Write usage JSON to this file"),
    session_db_path: str = typer.Option(
        ":memory:",
        "--session-db-path",
        help="Persistent session SQLite path for cross-invocation replay",
    ),
    no_memory_capture: bool = typer.Option(
        False,
        "--no-memory-capture",
        help="Do not write this invocation to durable searchable memory",
    ),
    file_paths: list[str] = typer.Option(
        [],
        "--file",
        "-f",
        help="Attach a local file; repeat for multiple files",
    ),
    unattended: bool = typer.Option(
        True,
        "--unattended/--interactive",
        help=(
            "Run without a live approval surface. Unattended is the default for "
            "single-shot automation."
        ),
    ),
    stateless: bool = typer.Option(
        False,
        "--stateless/--no-stateless",
        help="Use clean-room prompt bootstrap; does not change --unattended semantics.",
    ),
    clean_room: bool = typer.Option(
        False,
        "--clean-room",
        help="Alias for --stateless.",
    ),
    stateless_keep_project_rules: bool = typer.Option(
        False,
        "--stateless-keep-project-rules",
        help="With clean-room bootstrap, keep AGENTS.md project rules only.",
    ),
    permissions: str | None = typer.Option(
        None,
        "--permissions",
        help=(
            "Permission profile for single-shot runs: restricted, bypass, or full. "
            "Defaults to OPENSQUILLA_AGENT_PERMISSIONS, then permissions.default_mode."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Run a single agent turn for automation."""
    from opensquilla.recovery import guarded_desktop_profile

    with guarded_desktop_profile():
        run_agent_command(
            message=message,
            agent_id=agent_id,
            session_id=session_id,
            model=model,
            workspace=workspace,
            workspace_strict=workspace_strict,
            workspace_lockdown=workspace_lockdown,
            workspace_lockdown_deny_paths=workspace_lockdown_deny_paths,
            scratch_dir=scratch_dir,
            thinking=thinking,
            timeout=timeout,
            max_iterations=max_iterations,
            iteration_timeout_seconds=iteration_timeout_seconds,
            tool_timeout_seconds=tool_timeout_seconds,
            request_timeout_seconds=request_timeout_seconds,
            max_provider_retries=max_provider_retries,
            length_capped_continuations=length_capped_continuations,
            transcript_path=transcript_path,
            usage_path=usage_path,
            session_db_path=session_db_path,
            no_memory_capture=no_memory_capture,
            file_paths=file_paths,
            unattended=unattended,
            stateless=stateless,
            clean_room=clean_room,
            stateless_keep_project_rules=stateless_keep_project_rules,
            permissions=permissions,
            json_output=json_output,
        )


@app.command("chat")
def chat(
    model: str = typer.Option("", "--model", "-m", help="Model override"),
    session_id: str = typer.Option("", "--session", "-s", help="Resume session"),
    ui: str | None = typer.Option(
        None,
        "--ui",
        help="Chat UI: auto, tui, or plain (default: auto)",
    ),
    standalone: bool = typer.Option(False, "--standalone", help="Direct Agent without gateway"),
    workspace: str = typer.Option("", "--workspace", help="Workspace root for standalone tools"),
    workspace_strict: bool | None = typer.Option(
        None,
        "--workspace-strict/--no-workspace-strict",
        help="Restrict read-side file tools to --workspace in standalone mode",
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", "-T", help="Total agent timeout in seconds (0=unlimited)"
    ),
) -> None:
    """Start interactive chat mode."""
    from opensquilla.cli.chat_cmd import run_chat

    if standalone:
        from opensquilla.recovery import guarded_desktop_profile

        with guarded_desktop_profile():
            run_chat(
                model=model,
                session_id=session_id,
                ui=ui,
                standalone=True,
                workspace=workspace,
                workspace_strict=workspace_strict,
                timeout=timeout,
            )
        return

    # Gateway-backed chat is a client of the already-locked gateway; taking
    # the same lock here would reject the ordinary interactive client.
    run_chat(
        model=model,
        session_id=session_id,
        ui=ui,
        standalone=False,
        workspace=workspace,
        workspace_strict=workspace_strict,
        timeout=timeout,
    )


@app.command("reset")
def reset_cmd(
    key: str = typer.Option(..., "--key", help="Session key to reset."),
    gateway_url: str = typer.Option(
        "http://localhost:18791", "--gateway", envvar="OPENSQUILLA_GATEWAY_URL"
    ),
) -> None:
    """Reset a session, flushing its memory synchronously.

    Exit codes: 0 on success (including raw-dump fallback),
    1 when flush + raw-dump both fail (session preserved).
    """
    import asyncio

    from opensquilla.cli.gateway_client import GatewayClient, GatewayRPCError
    from opensquilla.cli.url_utils import normalize_gateway_url

    async def _go():
        client = GatewayClient()
        try:
            await client.connect(normalize_gateway_url(gateway_url))
            return await client.reset_session(key)
        finally:
            await client.close()

    try:
        result = asyncio.run(_go())
    except GatewayRPCError as exc:
        data = exc.data or {}
        receipt = data.get("flush_receipt", {}) or {}
        typer.secho(f"\u2717 Reset aborted: {exc.message}", fg=typer.colors.RED)
        typer.echo(f"  Session preserved: {data.get('session_id', '?')}")
        if receipt.get("error"):
            typer.echo(f"  Cause: {receipt['error']}")
        raise typer.Exit(1)

    payload = result
    receipt = payload.get("flush_receipt") or {}
    mode = receipt.get("mode", "?")
    typer.secho(
        f"\u2713 Session reset ({payload.get('previous_session_id', '?')} \u2192 "
        f"{payload.get('session_id', '?')}).",
        fg=typer.colors.GREEN,
    )
    if mode == "llm":
        dur = receipt.get("duration_ms", 0) / 1000
        typer.echo(f"  Flush mode: llm ({dur:.1f}s)")
        for p in receipt.get("flushed_paths") or []:
            typer.echo(f"  Saved to: {p}")
    elif mode == "raw":
        reason = receipt.get("raw_reason", "unknown")
        dur = receipt.get("duration_ms", 0) / 1000
        typer.echo(f"  Flush mode: raw (reason: {reason}, after {dur:.1f}s)")
        for p in receipt.get("flushed_paths") or []:
            typer.echo(f"  Saved to: {p} (raw transcript dump)")
    elif mode == "skipped":
        typer.echo("  Flush mode: skipped (empty transcript)")
    else:
        typer.echo(f"  Flush mode: {mode}")

@app.command("version")
def version_cmd(
    check: bool = typer.Option(
        False, "--check", help="Check GitHub for a newer published release."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show the installed OpenSquilla version, optionally checking for updates.

    ``--check`` performs one network call to the public GitHub Releases API. It
    never downloads or installs anything. A failed check is reported but does not
    return a non-zero exit code.
    """
    from opensquilla import __version__

    if not check:
        if json_output:
            from opensquilla.cli.output import print_json

            print_json({"version": __version__})
        else:
            typer.echo(__version__)
        return

    import os

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.observability.update_check import refresh_update_check

    config = GatewayConfig.load(os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
    info = refresh_update_check(config=config, version=__version__, force=True)

    if json_output:
        from opensquilla.cli.output import print_json

        print_json(
            {
                "current": info.current_version,
                "latest": info.latest_version,
                "updateAvailable": info.update_available,
                "releaseUrl": info.release_url,
                "disabled": info.disabled,
                "error": info.error,
            }
        )
        return

    typer.echo(f"OpenSquilla {info.current_version}")
    if info.error:
        typer.secho(
            f"Could not check for updates: {info.error}",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return
    if info.update_available and info.latest_version:
        typer.secho(
            f"A newer version is available: {info.latest_version}",
            fg=typer.colors.GREEN,
        )
        typer.echo(f"  Release notes: {info.release_url}")
    elif info.latest_version:
        typer.secho("You're on the latest version.", fg=typer.colors.GREEN)
    else:
        typer.echo("Latest version is unknown.")


if __name__ == "__main__":
    app()
