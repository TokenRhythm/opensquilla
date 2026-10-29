"""CLI boundary for configuration load failures: one readable error, no traceback."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import NoReturn

import typer
from pydantic import ValidationError

from opensquilla.cli.ui import ACCENT_SOFT, error_console, markup_escape
from opensquilla.gateway.config_migration import ConfigParseError
from opensquilla.onboarding.config_store import load_config, resolve_config_path
from opensquilla.onboarding.next_steps import quote_cli_arg

CONFIG_LOAD_ERRORS = (OSError, tomllib.TOMLDecodeError, ConfigParseError, ValidationError)


def format_config_load_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = ".".join(str(part) for part in first.get("loc", ()))
            msg = str(first.get("msg") or "invalid value")
            return f"{loc}: {msg}" if loc else msg
    return str(exc).splitlines()[0]


def exit_config_load_error(exc: Exception, path: str | Path | None = None) -> NoReturn:
    target, _ = resolve_config_path(path)
    error_console.print(f"[red]OpenSquilla config error:[/red] {markup_escape(str(target))}")
    error_console.print(f"[dim]Reason: {markup_escape(format_config_load_error(exc))}[/dim]")
    error_console.print("[dim]Fix: edit or move this config, then rerun onboarding:[/dim]")
    error_console.print(
        f"  [{ACCENT_SOFT}]opensquilla onboard --if-needed"
        f" --config {markup_escape(quote_cli_arg(target))}[/]"
    )
    raise typer.Exit(code=2) from exc


def load_config_or_exit(path: str | Path | None = None):
    try:
        return load_config(path)
    except CONFIG_LOAD_ERRORS as exc:
        exit_config_load_error(exc, path)
