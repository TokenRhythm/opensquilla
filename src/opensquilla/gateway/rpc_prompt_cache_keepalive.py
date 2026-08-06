"""RPC surface for the opt-in session prompt-cache keepalive lease."""

from __future__ import annotations

from typing import Any, cast

from opensquilla.gateway.prompt_cache_keepalive import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_TTL_SECONDS,
    MAX_IDLE_TIMEOUT_SECONDS,
    MAX_TTL_SECONDS,
    MIN_IDLE_TIMEOUT_SECONDS,
    MIN_TTL_SECONDS,
)
from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.session_services import get_session_storage

_d = get_dispatcher()


async def _resolve_session(params: Any, ctx: RpcContext) -> str:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    key = params.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("params.key must be a complete session key")
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise RpcUnavailableError("Session storage is not available")
    session = await storage.get_session(key)
    if session is None:
        raise KeyError(f"Session not found: {key}")
    return str(session.session_key)


def _service(ctx: RpcContext) -> Any:
    service = ctx.prompt_cache_keepalive_service
    if service is None:
        raise RpcUnavailableError("Prompt-cache keepalive is not available")
    return service


@_d.method("sessions.promptCacheKeepalive.status", scope="operator.read")
async def _status(params: Any, ctx: RpcContext) -> dict[str, Any]:
    key = await _resolve_session(params, ctx)
    return cast(dict[str, Any], _service(ctx).status(key))


@_d.method("sessions.promptCacheKeepalive.set", scope="operator.write")
async def _set(params: Any, ctx: RpcContext) -> dict[str, Any]:
    key = await _resolve_session(params, ctx)
    assert isinstance(params, dict)
    enabled = params.get("enabled")
    if type(enabled) is not bool:
        raise ValueError("params.enabled must be a boolean")
    ttl = params.get("ttlSeconds", DEFAULT_TTL_SECONDS)
    if type(ttl) is not int:
        raise ValueError("params.ttlSeconds must be an integer")
    if enabled and not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
        raise ValueError(
            f"params.ttlSeconds must be between {MIN_TTL_SECONDS} and "
            f"{MAX_TTL_SECONDS}"
        )
    minimum_useful_idle_timeout = int(ttl * 0.8) + 1
    idle_timeout = params.get("idleTimeoutSeconds")
    if idle_timeout is None:
        idle_timeout = max(DEFAULT_IDLE_TIMEOUT_SECONDS, minimum_useful_idle_timeout)
    if type(idle_timeout) is not int:
        raise ValueError("params.idleTimeoutSeconds must be an integer")
    if enabled and not MIN_IDLE_TIMEOUT_SECONDS <= idle_timeout <= MAX_IDLE_TIMEOUT_SECONDS:
        raise ValueError(
            "params.idleTimeoutSeconds must be between "
            f"{MIN_IDLE_TIMEOUT_SECONDS} and {MAX_IDLE_TIMEOUT_SECONDS}"
        )
    if enabled and idle_timeout < minimum_useful_idle_timeout:
        raise ValueError(
            "params.idleTimeoutSeconds must be longer than the probe interval"
        )
    return cast(
        dict[str, Any],
        await _service(ctx).set_enabled(
            key,
            enabled=enabled,
            ttl_seconds=ttl,
            idle_timeout_seconds=idle_timeout,
        ),
    )


__all__: list[str] = []
