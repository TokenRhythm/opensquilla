from __future__ import annotations

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.subagent import SubagentSpec
from opensquilla.engine.types import ToolCall
from opensquilla.sandbox.run_context import MountGrant, RunContext, TemporaryGrant
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


class _Provider:
    provider_name = "fake"

    async def list_models(self) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_direct_subagent_tool_context_inherits_full_host_run_mode() -> None:
    captured: dict[str, object] = {}

    async def parent_tool_handler(call: ToolCall) -> ToolResult:
        child_ctx = current_tool_context.get()
        captured["run_mode"] = getattr(child_ctx, "run_mode", None)
        captured["elevated"] = getattr(child_ctx, "elevated", None)
        captured["sandbox_run_context"] = getattr(child_ctx, "sandbox_run_context", None)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    parent = Agent(
        provider=_Provider(),
        config=AgentConfig(workspace_dir="D:\\workspace"),
        tool_handler=parent_tool_handler,
    )
    parent._session_key = "agent:main:parent"

    parent_ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        agent_id="main",
        workspace_dir="D:\\workspace",
        session_key="agent:main:parent",
        run_mode="full",
        elevated="full",
        sandbox_run_context=RunContext(
            run_mode=RunMode.FULL,
            mounts=(
                MountGrant("/durable", scope="chat"),
                MountGrant("/one-shot", access="rw", scope="once"),
            ),
            temporary_grants=(
                TemporaryGrant("mount", "/temporary", "fingerprint-1"),
            ),
            source="request",
        ),
    )

    token = current_tool_context.set(parent_ctx)
    try:
        child = parent._make_child_agent(SubagentSpec(task="probe"), depth=1)
        assert child.tool_handler is not None
        await child.tool_handler(
            ToolCall(
                tool_use_id="tool-1",
                tool_name="exec_command",
                arguments={"command": "echo ok"},
            )
        )
    finally:
        current_tool_context.reset(token)

    assert captured["run_mode"] == "full"
    assert captured["elevated"] == "full"
    child_run_context = captured["sandbox_run_context"]
    assert isinstance(child_run_context, RunContext)
    assert child_run_context.run_mode == RunMode.FULL
    assert {grant.path for grant in child_run_context.mounts} == {"/durable"}
    assert child_run_context.temporary_grants == ()
