from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from opensquilla.application.conversation_ancillary import (
    ClarificationSubmission,
    CommandCatalog,
    CommandCatalogQuery,
    PromptCacheLease,
    PromptCachePolicy,
    SetPromptCacheLease,
    SubmitClarification,
    UsageQuery,
    UsageReporting,
)


async def test_usage_and_command_modules_normalize_domain_queries() -> None:
    usage_port = SimpleNamespace(
        status=AsyncMock(return_value={"sessions": []}),
        query=AsyncMock(return_value={"rows": []}),
        cost_breakdown=AsyncMock(return_value={"totalCostUsd": 0}),
    )
    command_port = SimpleNamespace(list=AsyncMock(return_value={"commands": []}))

    await UsageReporting(cast(Any, usage_port)).status(
        UsageQuery(session_key=" agent:main:webchat:test ")
    )
    await CommandCatalog(cast(Any, command_port)).list(CommandCatalogQuery(" web "))

    assert usage_port.status.await_args.args[0].session_key == "agent:main:webchat:test"
    assert command_port.list.await_args.args[0].surface == "web"


async def test_prompt_cache_module_applies_defaults_before_mutation() -> None:
    port = SimpleNamespace(
        status=AsyncMock(return_value={"enabled": False}),
        set_policy=AsyncMock(return_value={"enabled": True}),
    )
    module = PromptCacheLease(
        cast(Any, port),
        PromptCachePolicy(
            default_ttl_seconds=300,
            minimum_ttl_seconds=60,
            maximum_ttl_seconds=3600,
            default_idle_timeout_seconds=600,
            minimum_idle_timeout_seconds=60,
            maximum_idle_timeout_seconds=7200,
        ),
    )

    await module.set_policy(
        SetPromptCacheLease(session_key=" agent:main:webchat:test ", enabled=True)
    )

    command = port.set_policy.await_args.args[0]
    assert command.session_key == "agent:main:webchat:test"
    assert command.ttl_seconds == 300
    assert command.idle_timeout_seconds == 600


async def test_prompt_cache_module_rejects_invalid_policy_without_mutation() -> None:
    port = SimpleNamespace(
        status=AsyncMock(),
        set_policy=AsyncMock(),
    )
    module = PromptCacheLease(
        cast(Any, port),
        PromptCachePolicy(
            default_ttl_seconds=300,
            minimum_ttl_seconds=60,
            maximum_ttl_seconds=3600,
            default_idle_timeout_seconds=600,
            minimum_idle_timeout_seconds=60,
            maximum_idle_timeout_seconds=7200,
        ),
    )

    with pytest.raises(ValueError, match="ttl_seconds"):
        await module.set_policy(
            SetPromptCacheLease(
                session_key="agent:main:webchat:test",
                enabled=True,
                ttl_seconds=30,
            )
        )

    port.set_policy.assert_not_awaited()


async def test_clarification_validates_before_port() -> None:
    clarification_port = SimpleNamespace(submit=AsyncMock(return_value={"accepted": True}))

    await ClarificationSubmission(cast(Any, clarification_port)).submit(
        SubmitClarification(
            session_key=" agent:main:webchat:test ",
            fields={"choice": "continue"},
            request_id=" request-1 ",
        )
    )

    clarification = clarification_port.submit.await_args.args[0]
    assert clarification.session_key == "agent:main:webchat:test"
    assert clarification.request_id == "request-1"
