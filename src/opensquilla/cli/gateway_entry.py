"""Minimal dispatcher for the packaged Desktop ``gateway run`` path."""

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


def _profile_from_argv(argv: list[str]) -> str | None:
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


def _activate_profile(profile: str | None) -> None:
    if profile is None:
        return
    name = profile.strip()
    if not name:
        return
    if not is_valid_profile_name(name):
        raise typer.BadParameter(
            "use lowercase letters, digits, hyphens, or underscores; "
            "start with a letter or digit; max length 64"
        )
    os.environ["OPENSQUILLA_PROFILE"] = name


_profile = _profile_from_argv(sys.argv)
if _profile and is_valid_profile_name(_profile):
    os.environ["OPENSQUILLA_PROFILE"] = _profile

_RECOVERY_OFFLINE = os.environ.get("OPENSQUILLA_RECOVERY_OFFLINE", "").strip().lower() in {
    "1", "true", "yes", "on",
}
if not _RECOVERY_OFFLINE:
    _load_env_for_active_home()
    warn_if_proxy_ignored()


gateway_app = typer.Typer(
    help="Gateway server commands.",
    pretty_exceptions_enable=False,
)


@gateway_app.command("run")
def gateway_run(
    port: int | None = typer.Option(
        None,
        "--port",
        "-p",
        min=0,
        max=65535,
        metavar="PORT",
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
    from opensquilla.cli.gateway_runner import run_gateway_with_profile

    run_gateway_with_profile(
        port=port,
        bind=bind,
        listen=listen,
        debug=debug,
        config_path=config_path,
    )


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
    from opensquilla.observability.cli_logging import configure_cli_structlog

    configure_cli_structlog()
    _activate_profile(profile)
    if not _RECOVERY_OFFLINE:
        _load_env_for_active_home()
        warn_if_proxy_ignored()


app.add_typer(gateway_app, name="gateway")
