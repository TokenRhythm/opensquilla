"""Proxy routing helpers for Linux sandbox networking."""

from __future__ import annotations

from opensquilla.sandbox.managed_proxy_env import (
    DEFAULT_NO_PROXY_VALUE,
    NO_PROXY_ENV_KEYS,
    PROXY_CONTROL_ENV,
    PROXY_ENV_KEYS,
    managed_proxy_env,
)


def proxy_env_for_inner_port(*, base_env: dict[str, str], port: int) -> dict[str, str]:
    env = dict(base_env)
    env.update(managed_proxy_env("127.0.0.1", port))
    return env


__all__ = [
    "DEFAULT_NO_PROXY_VALUE",
    "NO_PROXY_ENV_KEYS",
    "PROXY_CONTROL_ENV",
    "PROXY_ENV_KEYS",
    "proxy_env_for_inner_port",
]
