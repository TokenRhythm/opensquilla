"""Lightweight entry point for the offline Desktop recovery CLI.

This module is intentionally kept below the ordinary CLI bootstrap.  Desktop
starts recovery commands before the Gateway, so importing the full command
tree here would also import the runtime and its optional numerical stack.
"""

from __future__ import annotations

import os
import sys

import typer

# Set this before importing *any* recovery implementation.  Recovery code must
# never read cwd/profile dotenv files, even when called directly (rather than
# through Electron, which also sets the variable in its child environment).
os.environ["OPENSQUILLA_RECOVERY_OFFLINE"] = "1"

from opensquilla.cli.stdio import configure_stdio_for_unicode  # noqa: E402
from opensquilla.paths import is_valid_profile_name  # noqa: E402

configure_stdio_for_unicode()


def _top_level_command_index(argv: list[str]) -> int | None:
    """Return the first positional command in a CLI argv vector."""

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


def _profile_from_top_level_argv(argv: list[str]) -> str | None:
    """Read a root ``--profile`` option without consuming subcommand flags."""

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


def _activate_profile(profile: str | None) -> None:
    """Match the public CLI's profile validation before command dispatch."""

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


# Preserve explicit profile selection before the recovery package resolves any
# paths, while still avoiding all dotenv loading.  Invalid values are left for
# the Typer callback below so import-time behavior matches the public CLI.
_initial_profile = _profile_from_top_level_argv(sys.argv)
if _initial_profile and is_valid_profile_name(_initial_profile):
    os.environ["OPENSQUILLA_PROFILE"] = _initial_profile

from opensquilla.cli.recovery_cmd import recovery_app  # noqa: E402

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
    # Typer invokes the callback after parsing.  Keep this validation in place
    # for CliRunner callers that pass argv directly instead of process argv.
    _activate_profile(profile)
    # Import the observability package only after Typer has selected the
    # recovery command.  Its broad package initializer is outside the shared
    # offline recovery dependency budget, but command diagnostics still need
    # the normal stderr-only structlog contract.
    from opensquilla.observability.cli_logging import configure_cli_structlog

    configure_cli_structlog()


app.add_typer(recovery_app, name="recovery")

__all__ = ["app", "recovery_app"]


if __name__ == "__main__":
    app()
