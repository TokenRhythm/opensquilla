from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from opensquilla.application.conversation_ancillary import (
    ClarificationSubmissionPort,
    CommandCatalogPort,
    PromptCacheLeasePort,
    PromptCachePolicy,
    RouteFeedbackPort,
    UsageReportingPort,
)
from opensquilla.gateway.adapters.conversation_ancillary import (
    GatewayConversationAncillaryAdapter,
)


def _adapter() -> tuple[GatewayConversationAncillaryAdapter, dict[str, AsyncMock]]:
    calls = {
        name: AsyncMock(return_value=value)
        for name, value in {
            "usage_status": {"sessions": []},
            "usage_query": {"rows": []},
            "usage_cost": {"totalCostUsd": 1.5},
            "commands": {"surface": "web", "commands": []},
            "feedback": {"accepted": True},
            "prompt_status": {"enabled": False},
            "prompt_set": {"enabled": True},
            "clarification": {"accepted": True},
        }.items()
    }
    usage = cast(
        UsageReportingPort,
        SimpleNamespace(
            status=calls["usage_status"],
            query=calls["usage_query"],
            cost_breakdown=calls["usage_cost"],
        ),
    )
    commands = cast(CommandCatalogPort, SimpleNamespace(list=calls["commands"]))
    feedback = cast(RouteFeedbackPort, SimpleNamespace(submit=calls["feedback"]))
    prompt_cache = cast(
        PromptCacheLeasePort,
        SimpleNamespace(status=calls["prompt_status"], set_policy=calls["prompt_set"]),
    )
    clarification = cast(
        ClarificationSubmissionPort,
        SimpleNamespace(submit=calls["clarification"]),
    )
    return (
        GatewayConversationAncillaryAdapter(
            usage=usage,
            commands=commands,
            feedback=feedback,
            prompt_cache=prompt_cache,
            clarification=clarification,
            prompt_cache_policy=PromptCachePolicy(
                default_ttl_seconds=300,
                minimum_ttl_seconds=60,
                maximum_ttl_seconds=3600,
                default_idle_timeout_seconds=600,
                minimum_idle_timeout_seconds=60,
                maximum_idle_timeout_seconds=7200,
            ),
        ),
        calls,
    )


async def test_adapter_projects_usage_commands_feedback_and_prompt_cache() -> None:
    adapter, calls = _adapter()

    await adapter.usage_status({"session_key": "agent:main:webchat:test"})
    await adapter.list_commands({"surface": " web "})
    await adapter.submit_feedback({"decision_id": "d-1", "rating": "down"})
    await adapter.prompt_cache_set(
        {"key": "agent:main:webchat:test", "enabled": True}
    )

    usage_query = calls["usage_status"].await_args.args[0]
    assert usage_query.session_key == "agent:main:webchat:test"
    assert dict(usage_query.filters) == {"session_key": "agent:main:webchat:test"}
    command_query = calls["commands"].await_args.args[0]
    assert command_query.surface == "web"
    feedback = calls["feedback"].await_args.args[0]
    assert (feedback.decision_id, feedback.rating) == ("d-1", "down")
    prompt = calls["prompt_set"].await_args.args[0]
    assert (
        prompt.session_key,
        prompt.enabled,
        prompt.ttl_seconds,
        prompt.idle_timeout_seconds,
    ) == ("agent:main:webchat:test", True, 300, 600)


async def test_adapter_projects_clarification_aliases_to_domain_command() -> None:
    adapter, calls = _adapter()

    result = await adapter.submit_clarification(
        {
            "key": "agent:main:webchat:test",
            "fields": {"choice": "continue"},
            "request_id": "request-1",
            "run_id": "run-1",
        }
    )

    assert result == {"accepted": True}
    command = calls["clarification"].await_args.args[0]
    assert cast(Any, command).session_key == "agent:main:webchat:test"
    assert dict(cast(Any, command).fields) == {"choice": "continue"}
    assert cast(Any, command).request_id == "request-1"
    assert cast(Any, command).run_id == "run-1"
