"""Shared gateway RPC helpers for CLI commands."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import typer

from opensquilla.cli.output import emit_error
from opensquilla.cli.url_utils import normalize_gateway_url


def default_gateway_url() -> str:
    """Return the configured gateway WebSocket URL."""

    if gateway_url := os.environ.get("OPENSQUILLA_GATEWAY_URL"):
        return normalize_gateway_url(gateway_url)
    if config_path := os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"):
        return gateway_url_from_config(config_path)
    try:
        from opensquilla.onboarding.config_store import resolve_config_path

        implicit_config_path, _source = resolve_config_path(None)
        if implicit_config_path.is_file():
            return gateway_url_from_config(implicit_config_path)
    except Exception:  # noqa: BLE001 - fall back to release default.
        pass
    return normalize_gateway_url("ws://localhost:18791/ws")


def _client_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _format_url_host(host: str) -> str:
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def gateway_url_from_config(config_path: str | Path) -> str:
    """Return the WebSocket URL implied by an OpenSquilla config file."""

    from opensquilla.onboarding.config_store import load_config

    config = load_config(config_path)
    host = _format_url_host(_client_host(str(config.host or "127.0.0.1")))
    return normalize_gateway_url(f"ws://{host}:{int(config.port)}/ws")


def _target_gateway_url(
    *,
    gateway_url: str | None,
    config_path: str | Path | None,
) -> str:
    if gateway_url is not None:
        return normalize_gateway_url(gateway_url)
    if config_path is not None:
        return gateway_url_from_config(config_path)
    return default_gateway_url()


from opensquilla.gateway_client import default_gateway_token  # noqa: F401  # re-exported: moved to opensquilla.gateway_client


def rpc_error_exit_code(code: str | None) -> int:
    """Map gateway error codes to the CLI exit-code convention."""

    normalized = (code or "").upper()
    if normalized in {"INVALID_REQUEST", "NOT_FOUND", "METHOD_NOT_FOUND"}:
        return 2
    if normalized in {"CONFLICT", "STATE_CONFLICT", "LIFECYCLE_CONFLICT"}:
        return 3
    return 1


async def run_gateway_call(
    action: Callable[[Any], Awaitable[Any]],
    *,
    gateway_url: str | None = None,
    config_path: str | Path | None = None,
    json_output: bool = False,
) -> Any:
    """Connect to the gateway, run ``action(client)``, and close cleanly."""

    from opensquilla.cli import gateway_client as gateway_client_module

    client = gateway_client_module.GatewayClient()
    try:
        target_url = _target_gateway_url(gateway_url=gateway_url, config_path=config_path)
        await client.connect(target_url, token=default_gateway_token(config_path))
        return await action(client)
    except SystemExit as exc:
        message = str(exc)
        emit_error(message, json_output=json_output, code="GATEWAY_UNAVAILABLE")
        raise typer.Exit(1) from exc
    except gateway_client_module.GatewayRPCError as exc:
        emit_error(
            exc.message,
            json_output=json_output,
            code=exc.code,
            details=exc.data,
        )
        raise typer.Exit(rpc_error_exit_code(exc.code)) from exc
    except (ConnectionError, OSError) as exc:
        emit_error(str(exc), json_output=json_output, code="GATEWAY_UNAVAILABLE")
        raise typer.Exit(1) from exc
    finally:
        await client.close()


def run_gateway_sync(
    action: Callable[[Any], Awaitable[Any]],
    *,
    gateway_url: str | None = None,
    config_path: str | Path | None = None,
    json_output: bool = False,
) -> Any:
    """Synchronous Typer-friendly wrapper around :func:`run_gateway_call`."""

    return asyncio.run(
        run_gateway_call(
            action,
            gateway_url=gateway_url,
            config_path=config_path,
            json_output=json_output,
        )
    )


def confirm_or_exit(prompt: str, *, yes: bool, json_output: bool = False) -> None:
    """Require confirmation unless ``--yes`` was passed."""

    if yes:
        return
    if json_output:
        emit_error(
            "confirmation required; rerun with --yes to execute",
            json_output=True,
            code="CONFIRMATION_REQUIRED",
        )
        raise typer.Exit(2)
    typer.confirm(prompt, abort=True)
