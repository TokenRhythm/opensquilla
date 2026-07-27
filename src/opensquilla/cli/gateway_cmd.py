"""Gateway run command — start ASGI gateway with uvicorn."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
from collections.abc import Callable

import typer

from opensquilla.cli.gateway_lifecycle import (
    DESKTOP_CONFIG_OUTSIDE_PROFILE,
    GatewayLifecycleManager,
    GatewayLifecycleResult,
    desktop_config_path_is_profile_local,
    desktop_profile_lifecycle_active,
    remote_gateway_status,
)
from opensquilla.cli.ui import ACCENT_MARKUP, console
from opensquilla.gateway.boot import (
    gateway_shutdown_deadline,
    start_gateway_server,
)
from opensquilla.gateway.config import GatewayConfig, is_public_bind, resolve_listen_address
from opensquilla.paths import default_opensquilla_home

_SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = tuple(
    sig
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None))
    if sig is not None
)


def _install_shutdown_handlers(
    loop: asyncio.AbstractEventLoop, on_signal: Callable[[str], None]
) -> list[signal.Signals]:
    """Install asyncio SIGINT/SIGTERM handlers that trigger a graceful drain.

    Returns the signals that were actually wired so the caller can remove them.
    ``loop.add_signal_handler`` is unsupported on Windows (NotImplementedError)
    and outside the main thread (ValueError); in those cases SIGINT still
    surfaces as KeyboardInterrupt for the caller to catch, and SIGTERM falls back
    to the platform default (no in-process drain — Windows has no real SIGTERM).
    """
    installed: list[signal.Signals] = []
    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, on_signal, sig.name.lower())
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append(sig)
    return installed


def _remove_shutdown_handlers(
    loop: asyncio.AbstractEventLoop, installed: list[signal.Signals]
) -> None:
    for sig in installed:
        try:
            loop.remove_signal_handler(sig)
        except (NotImplementedError, RuntimeError, ValueError):
            continue


def gateway_startup_guidance(host: str, port: int, scheme: str = "http") -> tuple[str, ...]:
    """Return operator-facing guidance shown after the gateway starts."""

    base_url = f"{scheme}://{host}:{port}"
    return (
        f"[bold]Web UI:[/bold] {base_url}/control/",
        f"[bold]API base:[/bold] {base_url}",
        f"[bold]Debug log:[/bold] {default_opensquilla_home() / 'logs' / 'debug.log'}",
        "[dim]Keep this terminal open. Press Ctrl+C to stop.[/dim]",
    )


def _gateway_bind_available(host: str, port: int) -> bool:
    if port == 0:
        return True
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]

    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in infos:
        with socket.socket(family, socktype, proto) as sock:
            # Mirror how uvicorn/asyncio binds the real listener so the probe
            # never reports a false "in use": asyncio.create_server sets
            # SO_REUSEADDR on POSIX (letting it bind a port still in TIME_WAIT
            # after a restart) but deliberately does not on Windows, where the
            # option allows two live sockets on one port.
            if os.name != "nt":
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                except OSError:
                    pass
            try:
                sock.bind(sockaddr)
            except OSError as exc:
                last_error = exc
                continue
            return True
    return False if last_error is not None else True


def run_gateway(
    port: int | None = typer.Option(18791, "--port", "-p", help="Port to bind"),
    bind: str | None = typer.Option("127.0.0.1", "--bind", "-b", help="Host to bind"),
    listen: str = typer.Option("", "--listen", help="Host to bind (wins over --bind)"),
    debug: bool = typer.Option(False, "--debug", help="Enable debug mode"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
) -> None:
    """Start the ASGI gateway server.

    Precedence: ``--listen`` > ``--bind`` > ``OPENSQUILLA_LISTEN`` >
    ``OPENSQUILLA_GATEWAY_HOST`` > toml ``host`` field > default ``127.0.0.1``.

    The toml ``host`` field was previously silently ignored — operators
    setting ``host = "0.0.0.0"`` in opensquilla.toml then ran the gateway
    expecting public binding and got loopback instead. The toml is now
    honoured as the fallback when no CLI flag or env var is supplied,
    matching what the field name promises.
    """
    requested_config = config_path or os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH")
    if not desktop_config_path_is_profile_local(requested_config):
        console.print("DESKTOP_CONFIG_OUTSIDE_PROFILE")
        console.print(
            "[red]Gateway could not start:[/red] Desktop config is outside the selected "
            f"profile ({DESKTOP_CONFIG_OUTSIDE_PROFILE})."
        )
        raise typer.Exit(code=1)
    # Load config FIRST so its ``host`` field can act as the final
    # fallback below ``OPENSQUILLA_GATEWAY_HOST``.
    config = GatewayConfig.load(config_path or os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"))
    if config_path and not config.config_path:
        config.config_path = str(config_path)
    # Treat the CLI ``--bind`` default as "not explicitly supplied" so the
    # env vars + toml get a chance to participate when the operator only
    # sets one of them.
    explicit_flag: str | None = listen or (bind if bind and bind != "127.0.0.1" else None)
    host = resolve_listen_address(explicit_flag, default=config.host or "127.0.0.1")
    resolved_port = port if port is not None else config.port
    stored_host, stored_port, stored_debug = config.host, config.port, config.debug
    config = config.model_copy(update={"host": host, "port": resolved_port, "debug": debug})
    # CLI-resolved listen/port/debug are runtime state for this process, not
    # operator config edits: record runtime provenance (the same mechanism
    # env application uses for llm.base_url/proxy) so an RPC-surface persist
    # from the Web UI never bakes a one-off ``--listen 0.0.0.0`` / ``--debug``
    # into config.toml. The sparse persister restores the stored value while
    # the field still equals the applied one; an explicit post-boot operator
    # edit wins as usual.
    for field_path, stored, applied in (
        ("host", stored_host, host),
        ("port", stored_port, resolved_port),
        ("debug", stored_debug, debug),
    ):
        if applied != stored:
            config.record_runtime_override(field_path, stored, applied)

    if not _gateway_bind_available(host, resolved_port):
        console.print("OPENSQUILLA_GATEWAY_PORT_IN_USE")
        console.print(
            f"[red]Gateway could not start:[/red] {host}:{resolved_port} is already in use."
        )
        if os.name == "nt":
            find_hint = f"netstat -ano | findstr :{resolved_port}"
        else:
            find_hint = f"lsof -iTCP:{resolved_port} -sTCP:LISTEN -n -P"
        console.print(f"[dim]Find the listener with: {find_hint}[/dim]")
        raise typer.Exit(code=1)

    banner_host = f"[red]{host}[/red]" if is_public_bind(host) else f"[{ACCENT_MARKUP}]{host}[/]"
    console.print(
        f"[bold green]Starting OpenSquilla gateway[/bold green] on {banner_host}:{resolved_port}"
    )
    scheme = "https" if (config.tls.keyfile and config.tls.certfile) else "http"
    for line in gateway_startup_guidance(host, resolved_port, scheme=scheme):
        console.print(line)
    if is_public_bind(host):
        # Use ASCII-only glyphs here so the warning still prints on Windows
        # consoles configured for legacy GBK code pages (where U+26A0 / em-dash
        # crash Rich's legacy renderer with UnicodeEncodeError).
        console.print(
            "[yellow]WARNING: gateway is bound to a wildcard address - "
            "reachable from every interface.[/yellow]"
        )
        if config.auth.mode == "none":
            console.print(
                "[yellow]  auth.mode=none + wildcard bind = LAN-open. "
                "Anyone reachable on this network can use the chat, sessions, "
                "and config surfaces with your provider credentials.[/yellow]"
            )
        console.print(
            "[yellow]  Bypass / elevated mode remains owner-only and "
            "is unreachable from non-loopback peers; the chat UI will "
            "self-disable that pill.[/yellow]"
        )

    async def _run() -> None:
        # Subscription manager is gateway-specific (WS event routing)
        from opensquilla.gateway.websocket import SubscriptionManager

        subscription_mgr = SubscriptionManager()

        # build_services() inside start_gateway_server handles:
        # session_manager, provider_selector, tool_registry, usage_tracker,
        # memory, skills, scheduler, search, MCP discovery.
        server = await start_gateway_server(
            config=config,
            subscription_manager=subscription_mgr,
            run=True,
        )
        assert server._task is not None

        # Trigger OpenSquilla's graceful drain on SIGINT/SIGTERM. uvicorn's own
        # handlers are suppressed in start_gateway_server, so server.close() —
        # the only path that drains in-flight agent turns and background
        # completions — runs whether shutdown comes from a signal (POSIX) or the
        # server task ending on its own.
        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        shutdown_reason = "shutdown"

        def _request_shutdown(reason: str) -> None:
            nonlocal shutdown_reason
            if not shutdown.is_set():
                shutdown_reason = reason
                shutdown.set()

        installed_signals = _install_shutdown_handlers(loop, _request_shutdown)
        # Expose the same trigger to the owner-only HTTP shutdown endpoint so a
        # graceful stop also works where POSIX signals can't drain — notably
        # Windows, where SIGTERM maps to an immediate TerminateProcess.
        app = getattr(server, "app", None)
        if app is not None and hasattr(app, "state"):
            install_shutdown_handler = getattr(
                app.state, "install_shutdown_handler", None
            )
            if callable(install_shutdown_handler):
                install_shutdown_handler(_request_shutdown)
            else:
                app.state.request_shutdown = _request_shutdown
        server_task = server._task
        waiter = asyncio.ensure_future(shutdown.wait())
        try:
            await asyncio.wait(
                {server_task, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            await server.close(shutdown_reason)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Fallback for platforms where add_signal_handler is unavailable
            # (Windows / non-main-thread): SIGINT arrives as KeyboardInterrupt.
            shutdown.set()
            await server.close("keyboard_interrupt")
        finally:
            waiter.cancel()
            _remove_shutdown_handlers(loop, installed_signals)
        if shutdown.is_set():
            console.print("\n[yellow]Gateway stopped.[/yellow]")

    try:
        asyncio.run(_run())
    except ValueError as exc:
        from opensquilla.onboarding.next_steps import env_recovery_commands
        from opensquilla.onboarding.status import get_onboarding_status

        console.print(f"[red]Gateway could not start:[/red] {exc}")
        status = get_onboarding_status(config)
        recovery_entries = env_recovery_commands(status)
        if not recovery_entries:
            embedding = getattr(getattr(config, "memory", None), "embedding", None)
            remote = getattr(embedding, "remote", None)
            env_key = str(getattr(remote, "api_key_env", "") or "").strip()
            if not env_key and config.config_path:
                try:
                    import tomllib

                    with open(config.config_path, "rb") as f:
                        raw_config = tomllib.load(f)
                    env_key = str(
                        raw_config.get("memory", {})
                        .get("embedding", {})
                        .get("remote", {})
                        .get("api_key_env", "")
                        or ""
                    ).strip()
                except (OSError, tomllib.TOMLDecodeError):
                    env_key = ""
            if env_key and not os.environ.get(env_key):
                from opensquilla.onboarding.next_steps import set_env_command

                # ``command`` is the machine-shaped field: keep it the bare
                # command on every platform, matching env_recovery_commands
                # (the primary path feeding this same list).
                recovery_entries.append(
                    {"label": "Set memory key", "command": set_env_command(env_key)}
                )
        for entry in recovery_entries:
            console.print(f"{entry['label']}: {entry['command']}")
        if config.config_path:
            console.print(
                f"Inspect onboarding: opensquilla onboard status --config {config.config_path}"
            )
        raise typer.Exit(code=1) from exc
    except KeyboardInterrupt:
        console.print("\n[yellow]Gateway stopped.[/yellow]")


def _resolve_lifecycle_host(*, bind: str, listen: str) -> str:
    explicit_flag: str | None = listen or (bind if bind and bind != "127.0.0.1" else None)
    return resolve_listen_address(explicit_flag)


def _lifecycle_manager(
    *,
    port: int | None,
    bind: str | None,
    listen: str,
    config_path: str | None = None,
    health_timeout: float = 60.0,
    shutdown_timeout: float = 10.0,
) -> GatewayLifecycleManager:
    config = GatewayConfig.load(
        config_path or os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"),
        read_only=desktop_profile_lifecycle_active(),
    )
    host = _resolve_lifecycle_host(bind=bind or "127.0.0.1", listen=listen)
    if not listen and (bind is None or bind == "127.0.0.1"):
        host = resolve_listen_address(None, default=config.host or "127.0.0.1")
    resolved_port = port if port is not None else config.port
    return GatewayLifecycleManager(
        host=host,
        port=resolved_port,
        config_path=config_path or os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH") or None,
        health_timeout=health_timeout,
        shutdown_timeout=shutdown_timeout,
    )


def _emit_lifecycle_result(result: GatewayLifecycleResult, *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(result.to_payload(), ensure_ascii=False, default=str))
    elif result.ok:
        typer.echo(f"{result.state}: {result.url}")
    else:
        typer.echo(f"Error: {result.message or result.code or result.state}", err=True)

    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


def start_gateway(
    port: int | None = typer.Option(18791, "--port", "-p", help="Port to bind"),
    bind: str | None = typer.Option("127.0.0.1", "--bind", "-b", help="Host to bind"),
    listen: str = typer.Option("", "--listen", help="Host to bind (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
    health_timeout: float = typer.Option(60.0, "--timeout", help="Readiness wait timeout"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Start the gateway in the background and wait for readiness."""

    manager = _lifecycle_manager(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        health_timeout=health_timeout,
    )
    _emit_lifecycle_result(manager.start(), json_output=json_output)


def status_gateway(
    port: int | None = typer.Option(18791, "--port", "-p", help="Port to inspect"),
    bind: str | None = typer.Option("127.0.0.1", "--bind", "-b", help="Host to inspect"),
    listen: str = typer.Option("", "--listen", help="Host to inspect (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
    gateway_url: str | None = typer.Option(None, "--gateway", help="Remote gateway URL"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Inspect the managed gateway process without mutating state."""

    if gateway_url:
        _emit_lifecycle_result(remote_gateway_status(gateway_url), json_output=json_output)
        return

    manager = _lifecycle_manager(port=port, bind=bind, listen=listen, config_path=config_path)
    _emit_lifecycle_result(manager.status(), json_output=json_output)


def stop_gateway(
    port: int | None = typer.Option(18791, "--port", "-p", help="Port to stop"),
    bind: str | None = typer.Option("127.0.0.1", "--bind", "-b", help="Host to stop"),
    listen: str = typer.Option("", "--listen", help="Host to stop (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
    shutdown_timeout: float | None = typer.Option(
        None,
        "--timeout",
        help="SIGKILL deadline in seconds (default: exceeds the graceful drain budget)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Stop the recorded gateway process."""

    manager = _lifecycle_manager(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        shutdown_timeout=(
            shutdown_timeout if shutdown_timeout is not None else gateway_shutdown_deadline()
        ),
    )
    _emit_lifecycle_result(manager.stop(), json_output=json_output)


def restart_gateway(
    port: int | None = typer.Option(18791, "--port", "-p", help="Port to restart"),
    bind: str | None = typer.Option("127.0.0.1", "--bind", "-b", help="Host to restart"),
    listen: str = typer.Option("", "--listen", help="Host to restart (wins over --bind)"),
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
    health_timeout: float = typer.Option(60.0, "--timeout", help="Readiness wait timeout"),
    shutdown_timeout: float | None = typer.Option(
        None,
        "--shutdown-timeout",
        help="SIGKILL deadline in seconds (default: exceeds the graceful drain budget)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Restart the recorded gateway process."""

    manager = _lifecycle_manager(
        port=port,
        bind=bind,
        listen=listen,
        config_path=config_path,
        health_timeout=health_timeout,
        shutdown_timeout=(
            shutdown_timeout if shutdown_timeout is not None else gateway_shutdown_deadline()
        ),
    )
    _emit_lifecycle_result(manager.restart(), json_output=json_output)


def reload_gateway(
    config_path: str | None = typer.Option(None, "--config", help="Override config path"),
    gateway_url: str | None = typer.Option(
        None, "--gateway", help="Gateway WebSocket URL to call"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Re-read the on-disk config into the running gateway (hot-apply).

    Calls the admin ``config.reload`` RPC. Hand-edited TOML is otherwise
    only read at boot; RPC/Web-UI edits already hot-apply. Channel, memory
    embedding, and sandbox posture changes still need a full
    ``opensquilla gateway restart`` — the summary printed here says so.
    """
    from opensquilla.cli.gateway_rpc import run_gateway_sync
    from opensquilla.cli.output import print_json

    async def _run(client):
        return await client.call("config.reload", {})

    payload = run_gateway_sync(
        _run,
        gateway_url=gateway_url,
        config_path=config_path,
        json_output=json_output,
    )
    if not isinstance(payload, dict):
        payload = {}

    if json_output:
        print_json(payload)
        if payload.get("ok") is not True:
            raise typer.Exit(code=1)
        return

    if payload.get("ok") is not True:
        typer.echo(f"Reload failed: {payload.get('error', 'unknown error')}", err=True)
        if payload.get("path"):
            typer.echo(f"Config file: {payload['path']}", err=True)
        typer.echo("The running config was left unchanged.", err=True)
        raise typer.Exit(code=1)

    if payload.get("path"):
        typer.echo(f"Reloaded config from {payload['path']}")
    live_applied = payload.get("liveApplied") or []
    typer.echo(
        "Applied live: " + (", ".join(live_applied) if live_applied else "(no changes)")
    )
    if payload.get("restartRequired"):
        sections = payload.get("restartSections") or []
        suffix = f" ({', '.join(sections)})" if sections else ""
        typer.echo(
            f"Restart required{suffix}: run `opensquilla gateway restart` "
            "to finish applying these sections."
        )
