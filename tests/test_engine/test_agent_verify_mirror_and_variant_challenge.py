"""Retired scratch mirror compatibility preserves ordinary write policy."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, DoneEvent
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.tools import write_policy
from opensquilla.tools.types import ToolContext


@pytest.mark.parametrize("retired_mirror_active", [False, True])
def test_retired_mirror_slot_does_not_change_write_denial(
    tmp_path, monkeypatch, retired_mirror_active
) -> None:
    monkeypatch.delenv("OPENSQUILLA_WORKSPACE_WRITE_DENY_GUIDANCE", raising=False)
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    ctx = ToolContext(
        workspace_dir=str(workspace),
        scratch_dir=str(scratch),
        workspace_write_deny_globs=["tests/**"],
        scratch_verify_mirror_active=retired_mirror_active,
    )
    token = write_policy.current_tool_context.set(ctx)
    try:
        match = write_policy.match_workspace_write_deny(
            workspace / "tests" / "test_a.py", workspace=workspace, ctx=ctx
        )
        assert match is not None
        block = write_policy.workspace_write_deny_block("write_file", match)
    finally:
        write_policy.current_tool_context.reset(token)

    assert block["reason"] == "workspace_write_deny"
    assert block["retryable"] is False
    assert str(scratch) in str(block["message"])
    assert "verify-mirror" not in str(block["message"])




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
async def test_retired_mirror_config_does_not_arm_tool_context(tmp_path) -> None:
    tool_context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=_FinalProvider(),
        config=AgentConfig(scratch_verify_mirror=True),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("Inspect the workspace")]

    assert any(isinstance(event, DoneEvent) for event in events)
    assert tool_context.scratch_verify_mirror_active is False
