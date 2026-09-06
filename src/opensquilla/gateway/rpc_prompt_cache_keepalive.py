"""RPC surface for the opt-in session prompt-cache keepalive lease."""

from __future__ import annotations

from typing import Any, cast

from opensquilla.application.conversation_ancillary import (
    PromptCacheLeasePort,
    PromptCacheLeaseResult,
    PromptCachePolicy,
    SetPromptCacheLease,
)
from opensquilla.gateway.adapters.conversation_ancillary import (
    GatewayConversationAncillaryAdapter,
)
from opensquilla.gateway.adapters.conversation_ancillary_contract import (
    register_conversation_ancillary_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.prompt_cache_keepalive import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_TTL_SECONDS,
    MAX_IDLE_TIMEOUT_SECONDS,
    MAX_TTL_SECONDS,
    MIN_IDLE_TIMEOUT_SECONDS,
    MIN_TTL_SECONDS,
)
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.gateway.session_services import get_session_storage

_d = get_dispatcher()


async def _stored_session_key(key: str, storage: Any) -> str:
    if storage is None:
        raise RpcUnavailableError("Session storage is not available")
    session = await storage.get_session(key)
    if session is None:
        raise KeyError(f"Session not found: {key}")
    return str(session.session_key)


def _require_service(service: Any) -> Any:
    if service is None:
        raise RpcUnavailableError("Prompt-cache keepalive is not available")
    return service


_PROMPT_CACHE_POLICY = PromptCachePolicy(
    default_ttl_seconds=DEFAULT_TTL_SECONDS,
    minimum_ttl_seconds=MIN_TTL_SECONDS,
    maximum_ttl_seconds=MAX_TTL_SECONDS,
    default_idle_timeout_seconds=DEFAULT_IDLE_TIMEOUT_SECONDS,
    minimum_idle_timeout_seconds=MIN_IDLE_TIMEOUT_SECONDS,
    maximum_idle_timeout_seconds=MAX_IDLE_TIMEOUT_SECONDS,
)


class _GatewayPromptCacheLeasePort(PromptCacheLeasePort):
    def __init__(self, context: RpcContext) -> None:
        self._storage = get_session_storage(context.session_manager)
        self._keepalive = context.prompt_cache_keepalive_service

    async def status(self, session_key: str) -> PromptCacheLeaseResult:
        key = await _stored_session_key(session_key, self._storage)
        return cast(PromptCacheLeaseResult, _require_service(self._keepalive).status(key))

    async def set_policy(self, command: SetPromptCacheLease) -> PromptCacheLeaseResult:
        key = await _stored_session_key(command.session_key, self._storage)
        assert command.ttl_seconds is not None
        assert command.idle_timeout_seconds is not None
        return cast(
            PromptCacheLeaseResult,
            await _require_service(self._keepalive).set_enabled(
                key,
                enabled=command.enabled,
                ttl_seconds=command.ttl_seconds,
                idle_timeout_seconds=command.idle_timeout_seconds,
            ),
        )


def _prompt_cache_adapter(ctx: RpcContext) -> GatewayConversationAncillaryAdapter:
    return GatewayConversationAncillaryAdapter(
        prompt_cache=_GatewayPromptCacheLeasePort(ctx),
        prompt_cache_policy=_PROMPT_CACHE_POLICY,
    )


async def _status_contract(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return await _prompt_cache_adapter(ctx).prompt_cache_status(params)


async def _set_contract(params: Any, ctx: RpcContext) -> dict[str, Any]:
    return await _prompt_cache_adapter(ctx).prompt_cache_set(params)


_status_generated_contract = register_conversation_ancillary_contract(
    _d,
    "sessions.promptCacheKeepalive.status",
    _status_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_set_generated_contract = register_conversation_ancillary_contract(
    _d,
    "sessions.promptCacheKeepalive.set",
    _set_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


__all__: list[str] = []
