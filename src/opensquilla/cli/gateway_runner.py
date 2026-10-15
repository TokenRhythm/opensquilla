"""Shared implementation for the Desktop ``gateway run`` command."""

from __future__ import annotations

import typer

from opensquilla.startup_timing import startup_phase_end, startup_phase_start

GATEWAY_PROFILE_IN_USE_MARKER = "OPENSQUILLA_PROFILE_IN_USE"


def run_gateway_with_profile(
    *,
    port: int | None,
    bind: str | None,
    listen: str,
    debug: bool,
    config_path: str | None,
) -> None:
    """Run Gateway with the existing profile lease and ownership semantics."""

    gateway_import_started = startup_phase_start("gateway_command_import")
    try:
        from opensquilla.cli.gateway_cmd import run_gateway
        from opensquilla.gateway.desktop_ownership import (
            release_active_desktop_gateway_ownership,
        )
        from opensquilla.recovery import (
            LegacyGatewayRunningError,
            ProfileLockBusyError,
            guarded_desktop_profile,
        )
    except BaseException:
        startup_phase_end("gateway_command_import", gateway_import_started, failed=True)
        raise
    else:
        startup_phase_end("gateway_command_import", gateway_import_started)

    try:
        try:
            with guarded_desktop_profile(lock_timeout=5.0):
                run_gateway(
                    port=port,
                    bind=bind,
                    listen=listen,
                    debug=debug,
                    config_path=config_path,
                )
        except (ProfileLockBusyError, LegacyGatewayRunningError):
            # This marker is consumed by the Desktop launcher. Keep the output
            # independent of the profile path carried by the lock exception.
            typer.echo(GATEWAY_PROFILE_IN_USE_MARKER)
            typer.echo(
                "Gateway could not start: Another OpenSquilla process is still using "
                "this profile. Quit every OpenSquilla app or terminal using it, then "
                "try again; if no process will exit, restart the computer. Do not "
                "delete profile lock files."
            )
            raise typer.Exit(code=1) from None
    finally:
        release_active_desktop_gateway_ownership()
