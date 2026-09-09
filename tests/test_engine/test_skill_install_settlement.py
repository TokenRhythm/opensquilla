"""A settled install result must survive the Agent tool deadline."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.types import ToolCall, ToolResultEvent
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart


class InstallProvider:
    provider_name = "fake"

    async def chat(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        yield ProviderToolUseStart(tool_use_id="install-1", tool_name="skill_install_community")
        yield ProviderToolUseEnd(
            tool_use_id="install-1", tool_name="skill_install_community",
            arguments={"identifier": "demo"},
        )
        yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)


@pytest.mark.asyncio
async def test_install_timeout_preserves_settled_commit_receipt() -> None:
    settled = asyncio.Event()

    async def handler(call: ToolCall) -> ToolResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            settled.set()
            return ToolResult(
                tool_use_id=call.tool_use_id, tool_name=call.tool_name,
                content='{"success":true,"installed":true,"effectiveFrom":"next_turn"}',
            )

    agent = Agent(
        provider=InstallProvider(),
        config=AgentConfig(max_iterations=1, tool_timeout=0.01),
        tool_definitions=[ToolDefinition(
            name="skill_install_community", description="Install",
            input_schema=ToolInputSchema(properties={}, required=[]),
            cancellation_policy="must_settle",
        )],
        tool_handler=handler,
    )
    events = [event async for event in agent.run_turn("install demo")]
    result = next(event for event in events if isinstance(event, ToolResultEvent))
    assert settled.is_set()
    assert not result.is_error
    assert '"success":true' in result.result
    assert "timed out" not in result.result
