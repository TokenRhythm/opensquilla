"""Pre-alternate-screen readiness checks for Gateway-backed chat."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ChatGatewayPreflight:
    gateway_url: str
    lifecycle_state: str
    managed: bool
    started: bool


class ChatGatewayPreflightError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _resolved_config_path() -> str | None:
    explicit = os.environ.get("OPENSQUILLA_GATEWAY_CONFIG_PATH", "").strip()
    if explicit:
        return explicit
    from opensquilla.onboarding.config_store import resolve_config_path

    path, _source = resolve_config_path(None)
    return str(path) if Path(path).is_file() else None


async def _probe_rpc(gateway_url: str, config_path: str | None) -> None:
    from opensquilla.cli.gateway_client import GatewayClient
    from opensquilla.cli.gateway_rpc import default_gateway_token

    client = GatewayClient()
    try:
        await client.connect(gateway_url, token=default_gateway_token(config_path))
    finally:
        await client.close()


def _verify_rpc(gateway_url: str, config_path: str | None) -> None:
    try:
        asyncio.run(_probe_rpc(gateway_url, config_path))
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - stable preflight error
        raise ChatGatewayPreflightError(
            "GATEWAY_AUTH_OR_RPC_FAILED",
            f"Gateway is reachable but chat authentication failed: {exc}",
        ) from exc


def preflight_gateway_chat() -> ChatGatewayPreflight:
    """Validate or start the configured Gateway before terminal takeover.

    An explicit ``OPENSQUILLA_GATEWAY_URL`` is always operator-owned, even when
    it points at localhost.  Chat probes that target but never starts a
    different local process as a fallback.  Only the implicit config-backed
    local target may be lifecycle-managed here.
    """

    from opensquilla.cli.gateway_lifecycle import (
        GatewayLifecycleManager,
        remote_gateway_status,
    )
    from opensquilla.cli.gateway_rpc import default_gateway_url

    target_url = default_gateway_url()
    explicit_url = bool(os.environ.get("OPENSQUILLA_GATEWAY_URL", "").strip())
    config_path = _resolved_config_path()

    if explicit_url:
        result = remote_gateway_status(target_url)
        if not result.ok:
            raise ChatGatewayPreflightError(
                result.code or "GATEWAY_UNAVAILABLE",
                f"Configured gateway is unavailable at {target_url}. "
                "Inspect that target; chat will not start a local replacement.",
            )
        _verify_rpc(target_url, config_path=None)
        return ChatGatewayPreflight(
            gateway_url=target_url,
            lifecycle_state=result.state,
            managed=False,
            started=False,
        )

    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.onboarding.status import get_onboarding_status

    config = GatewayConfig.load(config_path)
    onboarding = get_onboarding_status(config)
    if onboarding.needs_onboarding:
        blocking = [
            name
            for name, detail in onboarding.section_details.items()
            if bool(detail.get("blocking"))
        ]
        suffix = f" Blocking sections: {', '.join(blocking)}." if blocking else ""
        raise ChatGatewayPreflightError(
            "ONBOARDING_REQUIRED",
            "OpenSquilla setup is incomplete."
            f"{suffix} Run `opensquilla onboard --if-needed` before chat.",
        )

    manager = GatewayLifecycleManager(
        host=str(config.host or "127.0.0.1"),
        port=int(config.port or 18791),
        config_path=config_path,
        health_timeout=60.0,
    )
    before = manager.status()
    if before.state in {"running", "unmanaged"} and before.ok:
        result = before
        started = False
    else:
        result = manager.start()
        started = result.ok and result.state == "running"
    if not result.ok:
        detail = result.message or result.code or result.state
        raise ChatGatewayPreflightError(
            result.code or "GATEWAY_START_FAILED",
            f"Gateway preflight failed: {detail} Run `opensquilla gateway status` for details.",
        )

    _verify_rpc(target_url, config_path=config_path)
    return ChatGatewayPreflight(
        gateway_url=target_url,
        lifecycle_state=result.state,
        managed=result.managed,
        started=started,
    )
