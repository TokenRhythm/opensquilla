"""Retired endgame configuration cannot arm workspace mutation guards."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.tools.types import CallerKind, ToolContext


class _FinalProvider:
    provider_name = "fake"

    async def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_retired_freeze_config_does_not_arm_tool_context() -> None:
    ctx = ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test"
    )
    agent = Agent(
        provider=_FinalProvider(),
        config=AgentConfig(
            timeout=30.0,
            endgame_git_freeze_margin_seconds=60,
            endgame_git_freeze_instrumentation_exempt=True,
        ),
        tool_context=ctx,
    )

    events = [event async for event in agent.run_turn("Inspect the workspace")]

    assert any(event.kind == "done" for event in events)
    assert ctx.endgame_git_freeze_active is False
    assert ctx.endgame_git_freeze_instrumentation_exempt is False
