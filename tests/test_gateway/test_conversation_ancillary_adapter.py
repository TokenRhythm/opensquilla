from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from opensquilla.application.conversation_ancillary import PromptCachePolicy
from opensquilla.gateway.adapters.conversation_ancillary import (
    GatewayConversationAncillaryAdapter,
    GatewayConversationAncillaryCallbacks,
)
from opensquilla.gateway.rpc import RpcContext


def _adapter() -> tuple[
    GatewayConversationAncillaryAdapter,
    RpcContext,
    GatewayConversationAncillaryCallbacks,
]:
    context = cast(RpcContext, SimpleNamespace(conn_id="test"))
    callbacks = GatewayConversationAncillaryCallbacks(
        usage_status=AsyncMock(return_value={"sessions": []}),
        usage_query=AsyncMock(return_value={"rows": []}),
        usage_cost=AsyncMock(return_value={"totalCostUsd": 1.5}),
        command_catalog=AsyncMock(return_value={"surface": "web", "commands": []}),
        route_feedback=AsyncMock(return_value={"accepted": True}),
        prompt_cache_status=AsyncMock(return_value={"enabled": False}),
        prompt_cache_set=AsyncMock(return_value={"enabled": True}),
        clarification=AsyncMock(return_value={"accepted": True}),
    )
    return (
        GatewayConversationAncillaryAdapter(
            context,
            callbacks,
            prompt_cache_policy=PromptCachePolicy(
                default_ttl_seconds=300,
                minimum_ttl_seconds=60,
                maximum_ttl_seconds=3600,
                default_idle_timeout_seconds=600,
                minimum_idle_timeout_seconds=60,
                maximum_idle_timeout_seconds=7200,
            ),
        ),
        context,
        callbacks,
    )


async def test_adapter_projects_usage_commands_feedback_and_prompt_cache() -> None:
    adapter, context, callbacks = _adapter()

    await adapter.usage_status({"session_key": "agent:main:webchat:test"})
    await adapter.list_commands({"surface": " web "})
    await adapter.submit_feedback({"decision_id": "d-1", "rating": "down"})
    await adapter.prompt_cache_set(
        {"key": "agent:main:webchat:test", "enabled": True}
    )

    cast(AsyncMock, callbacks.usage_status).assert_awaited_once_with(
        {
            "session_key": "agent:main:webchat:test",
            "sessionKey": "agent:main:webchat:test",
        },
        context,
    )
    cast(AsyncMock, callbacks.command_catalog).assert_awaited_once_with(
        {"surface": "web"}, context
    )
    cast(AsyncMock, callbacks.route_feedback).assert_awaited_once_with(
        {"decisionId": "d-1", "rating": "down"}, context
    )
    cast(AsyncMock, callbacks.prompt_cache_set).assert_awaited_once_with(
        {
            "key": "agent:main:webchat:test",
            "enabled": True,
            "ttlSeconds": 300,
            "idleTimeoutSeconds": 600,
        },
        context,
    )


async def test_adapter_projects_clarification_aliases_to_domain_command() -> None:
    adapter, context, callbacks = _adapter()

    result = await adapter.submit_clarification(
        {
            "key": "agent:main:webchat:test",
            "fields": {"choice": "continue"},
            "request_id": "request-1",
            "run_id": "run-1",
        }
    )

    assert result == {"accepted": True}
    cast(AsyncMock, callbacks.clarification).assert_awaited_once_with(
        {
            "sessionKey": "agent:main:webchat:test",
            "fields": {"choice": "continue"},
            "requestId": "request-1",
            "run_id": "run-1",
        },
        context,
    )
