"""Shared onboarding setup path helpers."""

from __future__ import annotations

from opensquilla.gateway.config import GatewayConfig


def _format_url_host(host: str) -> str:
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def _local_web_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def web_setup_url(cfg: GatewayConfig) -> str | None:
    if not cfg.control_ui.enabled:
        return None
    scheme = "https" if cfg.tls.keyfile and cfg.tls.certfile else "http"
    host = _format_url_host(_local_web_host(str(cfg.host)))
    base_path = cfg.control_ui.base_path.rstrip("/")
    setup_path = f"{base_path}/setup" if base_path else "/setup"
    return f"{scheme}://{host}:{cfg.port}{setup_path}"
