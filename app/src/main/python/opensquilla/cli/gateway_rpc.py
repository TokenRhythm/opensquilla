"""Shared gateway RPC helpers for CLI commands."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from opensquilla.cli.output import emit_error
from opensquilla.cli.url_utils import normalize_gateway_url


@dataclass(frozen=True)
class GatewayTarget:
    """A Gateway URL paired with the config used for its credentials."""

    url: str
    config_path: str | Path | None = None
    config_owns_target: bool = False


def default_gateway_target() -> GatewayTarget:
    """Resolve the default Gateway target and its configuration provenance."""

    if gateway_url := os.environ.get("OPENSQUILLA_GATEWAY_URL"):
        return GatewayTarget(
            normalize_gateway_url(gateway_url),
            os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH") or None,
        )
    if config_path := os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH"):
        return GatewayTarget(
            gateway_url_from_config(config_path),
            config_path,
            config_owns_target=True,
        )
    try:
        from opensquilla.cli.gateway_lifecycle import active_managed_gateway_target

        if managed_target := active_managed_gateway_target():
            return GatewayTarget(managed_target.url, managed_target.config_path)
    except Exception:  # noqa: BLE001 - fall back to config-derived target.
        pass
    try:
        from opensquilla.onboarding.config_store import resolve_config_path

        implicit_config_path, _source = resolve_config_path(None)
        if implicit_config_path.is_file():
            return GatewayTarget(
                gateway_url_from_config(implicit_config_path),
                implicit_config_path,
                config_owns_target=True,
            )
    except Exception:  # noqa: BLE001 - fall back to release default.
        pass
    return GatewayTarget(normalize_gateway_url("ws://localhost:18791/ws"))


def default_gateway_url() -> str:
    """Return the configured gateway WebSocket URL."""

    return default_gateway_target().url


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


def _target_gateway(
    *,
    gateway_url: str | None,
    config_path: str | Path | None,
) -> GatewayTarget:
    if gateway_url is not None:
        return GatewayTarget(normalize_gateway_url(gateway_url), config_path)
    if config_path is not None:
        return GatewayTarget(
            gateway_url_from_config(config_path),
            config_path,
            config_owns_target=True,
        )
    return default_gateway_target()


def default_gateway_token(
    config_path: str | Path | None = None,
    *,
    discover_target: bool = True,
) -> str | None:
    """Resolve the auth token used to connect to the gateway.

    Resolution order (matches the gateway's own config-loading
    precedence, so a single ``opensquilla.toml`` works for both ends):

      1. ``OPENSQUILLA_GATEWAY_TOKEN`` env var (explicit override)
      2. ``GatewayConfig.auth.token`` (from the explicit CLI config path,
         ``OPENSQUILLA_GATEWAY_CONFIG_PATH`` env var,
         ``./opensquilla.toml``, or ``~/.opensquilla/config.toml``)
      3. ``None`` — the connect handshake omits ``auth`` and only
         works against ``[auth] mode = "none"`` deployments.

    Returns ``None`` instead of raising on any load failure so the
    CLI still tries to connect (UNAUTHORIZED is more informative than
    a config-loader crash).
    """
    env = os.environ.get("OPENSQUILLA_GATEWAY_TOKEN", "").strip()
    if env:
        return env
    try:
        from opensquilla.gateway.config import GatewayConfig

        effective_config_path = str(config_path) if config_path is not None else ""
        if not effective_config_path:
            effective_config_path = os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip()
        if (
            discover_target
            and not effective_config_path
            and not os.environ.get("OPENSQUILLA_GATEWAY_URL")
        ):
            target_config_path = default_gateway_target().config_path
            if target_config_path is not None:
                effective_config_path = str(target_config_path)
        cfg = GatewayConfig.load(effective_config_path or None)
        token = getattr(getattr(cfg, "auth", None), "token", None)
        if isinstance(token, str) and token.strip():
            return token.strip()
    except Exception:  # noqa: BLE001 — config-loader robustness
        pass
    return None


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
        target = _target_gateway(gateway_url=gateway_url, config_path=config_path)
        await client.connect(
            target.url,
            token=default_gateway_token(target.config_path, discover_target=False),
        )
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
