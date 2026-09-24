from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.subagent import SubagentManager, SubagentSpec
from opensquilla.engine.types import DoneEvent, ToolCall
from opensquilla.engine.usage_accounting import UsageExecutionContext
from opensquilla.provider.correlation_context import (
    current_provider_request_correlation,
)
from opensquilla.provider.types import DoneEvent as ProviderDoneEvent
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.provider.types import TextDeltaEvent as ProviderTextDeltaEvent
from opensquilla.tools.types import ToolContext, current_tool_context


class _Provider:
    provider_name = "fake"


class _CapturingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.configs = []

    def chat(self, _messages, tools=None, config=None):
        del tools
        self.configs.append(config)

        async def _stream():
            yield ProviderTextDeltaEvent(text="ok")
            yield ProviderDoneEvent()

        return _stream()


def _root_correlation() -> ProviderRequestCorrelation:
    return ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="root-turn-1",
        execution_id="root-execution-1",
        call_kind="agent.chat",
    )


@pytest.mark.asyncio
async def test_tool_callback_binds_correlation_even_with_active_tool_context() -> None:
    observed: list[ProviderRequestCorrelation | None] = []

    async def _tool_handler(call: ToolCall) -> ToolResult:
        observed.append(current_provider_request_correlation())
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    agent = Agent(
        provider=_Provider(),
        config=AgentConfig(),
        tool_handler=_tool_handler,
        tool_context=ToolContext(session_key="agent:main:session-1"),
        provider_request_correlation=_root_correlation(),
    )
    assert agent.tool_handler is not None
    active_context = ToolContext(
        session_key="agent:main:active",
        on_runtime_event=lambda _event: None,
    )
    token = current_tool_context.set(active_context)
    try:
        await agent.tool_handler(
            ToolCall(
                tool_use_id="tool-1",
                tool_name="probe",
                arguments={},
            )
        )
    finally:
        current_tool_context.reset(token)

    assert observed == [_root_correlation()]
    assert current_provider_request_correlation() is None




def test_agent_never_derives_provider_correlation_from_usage_context() -> None:
    usage_context = UsageExecutionContext(
        execution_id="usage-execution",
        agent_run_id="usage-execution",
        turn_id="usage-turn",
        session_id="usage-session",
    )

    agent = Agent(
        provider=_Provider(),
        usage_execution_context=usage_context,
    )

    assert agent._provider_request_correlation is None


@pytest.mark.asyncio
async def test_provider_correlation_does_not_require_usage_sink() -> None:
    provider = _CapturingProvider()
    correlation = _root_correlation()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        provider_request_correlation=correlation,
    )

    async for _event in agent.run_turn("probe"):
        pass

    assert len(provider.configs) == 1
    assert provider.configs[0].provider_request_correlation == correlation


@pytest.mark.asyncio
async def test_subagent_keeps_session_and_root_turn_with_new_execution() -> None:
    observed: list[ProviderRequestCorrelation | None] = []

    async def _tool_handler(call: ToolCall) -> ToolResult:
        observed.append(current_provider_request_correlation())
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    parent = Agent(
        provider=_Provider(),
        tool_handler=_tool_handler,
        provider_request_correlation=_root_correlation(),
    )
    child = parent._make_child_agent(
        SubagentSpec(task="probe"),
        depth=1,
        execution_id="subagent-run-1",
    )

    assert child._usage_event_sink is None
    assert child._provider_request_correlation == ProviderRequestCorrelation(
        session_id="session-1",
        turn_id="root-turn-1",
        execution_id="subagent-run-1",
        call_kind="subagent.chat",
    )
    assert child.tool_handler is not None
    await child.tool_handler(
        ToolCall(
            tool_use_id="tool-2",
            tool_name="probe",
            arguments={},
        )
    )
    assert observed == [child._provider_request_correlation]


class _DoneChildAgent:
    async def run_turn(self, _task: str) -> AsyncIterator[DoneEvent]:
        yield DoneEvent(text="done", text_snapshot="done")


@pytest.mark.asyncio
async def test_subagent_run_id_is_reused_as_execution_id() -> None:
    manager = SubagentManager()
    observed_execution_ids: list[str] = []

    def _factory(
        _spec: SubagentSpec,
        _depth: int,
        execution_id: str,
    ) -> _DoneChildAgent:
        observed_execution_ids.append(execution_id)
        return _DoneChildAgent()

    handle = await manager.spawn(
        SubagentSpec(task="probe", timeout=0),
        _factory,
    )

    assert await handle.task == "done"
    assert observed_execution_ids == [handle.run_id]
    assert "-" in handle.run_id


def test_sibling_and_nested_subagents_keep_root_with_distinct_executions() -> None:
    parent = Agent(
        provider=_Provider(),
        provider_request_correlation=_root_correlation(),
    )

    first = parent._make_child_agent(
        SubagentSpec(task="first"),
        depth=1,
        execution_id="subagent-first",
    )
    second = parent._make_child_agent(
        SubagentSpec(task="second"),
        depth=1,
        execution_id="subagent-second",
    )
    nested = first._make_child_agent(
        SubagentSpec(task="nested"),
        depth=2,
        execution_id="subagent-nested",
    )

    correlations = (
        first._provider_request_correlation,
        second._provider_request_correlation,
        nested._provider_request_correlation,
    )
    assert all(correlation is not None for correlation in correlations)
    assert {correlation.session_id for correlation in correlations if correlation} == {
        "session-1"
    }
    assert {correlation.turn_id for correlation in correlations if correlation} == {
        "root-turn-1"
    }
    assert {correlation.execution_id for correlation in correlations if correlation} == {
        "subagent-first",
        "subagent-second",
        "subagent-nested",
    }
    assert {
        correlation.call_kind for correlation in correlations if correlation
    } == {"subagent.chat"}
