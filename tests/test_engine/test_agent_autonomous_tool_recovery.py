"""Tool execution facts survive retries without heuristic intervention."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, DoneEvent, ErrorEvent, ToolCall, ToolResult
from opensquilla.provider import (
    ContentBlockToolResult,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolStart


class RepeatedCallProvider:
    provider_name = "fake"

    def __init__(self, rounds: int) -> None:
        self.rounds = rounds
        self.requests: list[list[Message]] = []

    async def chat(self, messages: list[Message], **kwargs: Any) -> AsyncIterator[Any]:
        index = len(self.requests)
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if index < self.rounds:
            yield ProviderToolStart(tool_use_id=f"probe-{index}", tool_name="probe")
            yield ProviderToolEnd(
                tool_use_id=f"probe-{index}", tool_name="probe", arguments={"target": "service"}
            )
            yield ProviderDone(stop_reason="tool_calls", input_tokens=1, output_tokens=1)
        else:
            yield ProviderText(text="Finished after checking the results.")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _probe_definition(**kwargs: Any) -> ToolDefinition:
    return ToolDefinition(
        name="probe", description="Inspect service state.", input_schema=ToolInputSchema(), **kwargs
    )


def _result(request: list[Message], call_id: str) -> ContentBlockToolResult:
    return next(
        block
        for message in request
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult) and block.tool_use_id == call_id
    )


@pytest.mark.parametrize("succeeds_on_third", [False, True])
async def test_identical_calls_execute_and_each_real_result_reaches_next_request(
    succeeds_on_third: bool,
) -> None:
    rounds = 3 if succeeds_on_third else 5
    provider = RepeatedCallProvider(rounds)
    calls: list[dict[str, Any]] = []

    async def handler(call: ToolCall) -> ToolResult:
        calls.append(dict(call.arguments))
        success = succeeds_on_third and len(calls) == 3
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ready" if success else f"service unavailable, attempt {len(calls)}",
            is_error=not success,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=5,
            flush_enabled=False,
            tool_failure_loop_block_threshold=1,
            repeated_tool_call_recovery_threshold=1,
            repeated_tool_call_recovery_extra_tools=("probe",),
            progress_watchdog_mode="block",
            identical_request_loop_break_threshold=1,
            finalize_evidence_gate_enabled=True,
            final_diff_contract_mode="warn_model",
            final_diff_salvage=True,
        ),
        tool_definitions=[_probe_definition()],
        tool_handler=handler,
    )
    events = [event async for event in agent.run_turn("Check the service until it is ready.")]

    assert calls == [{"target": "service"}] * rounds
    assert len(provider.requests) == rounds + 1
    for index in range(rounds):
        result = _result(provider.requests[index + 1], f"probe-{index}")
        success = succeeds_on_third and index == 2
        assert result.is_error is not success
        expected = "ready" if success else f"service unavailable, attempt {index + 1}"
        assert expected in str(result.content)
    assert sum(isinstance(event, DoneEvent) for event in events) == 1
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize("legacy_iteration_timeout", [0.0, 0.001])
@pytest.mark.parametrize("legacy_tool_timeout", [0.0, 0.001, 60.0])
async def test_retired_generic_and_iteration_budgets_do_not_cancel_tools(
    legacy_iteration_timeout: float,
    legacy_tool_timeout: float,
) -> None:
    provider = RepeatedCallProvider(2)
    calls = 0

    async def handler(call: ToolCall) -> ToolResult:
        nonlocal calls
        await asyncio.sleep(0.02)
        calls += 1
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ready")

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=2,
            tool_timeout=legacy_tool_timeout,
            iteration_timeout=legacy_iteration_timeout,
        ),
        tool_definitions=[_probe_definition()],
        tool_handler=handler,
    )
    assert agent._tool_execution_timeout(ToolCall("probe", "probe", {})) is None
    events = [event async for event in agent.run_turn("Check the service twice.")]
    assert calls == 2
    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_declared_tool_timeout_returns_error_and_allows_recovery() -> None:
    provider = RepeatedCallProvider(2)
    cancelled = asyncio.Event()
    calls = 0

    async def handler(call: ToolCall) -> ToolResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ready")

    agent = Agent(
        provider=provider,
        config=AgentConfig(timeout=2, tool_timeout=0),
        tool_definitions=[_probe_definition(execution_timeout_seconds=0.01)],
        tool_handler=handler,
    )
    events = [event async for event in agent.run_turn("Inspect and recover the service.")]
    assert cancelled.is_set()
    assert calls == 2
    assert _result(provider.requests[1], "probe-0").is_error
    assert not _result(provider.requests[2], "probe-1").is_error
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)


def test_output_store_operator_limits_reach_ingress_context(tmp_path) -> None:
    from opensquilla.tools.types import ToolContext

    context = ToolContext(workspace_dir=str(tmp_path))
    agent = Agent(
        provider=RepeatedCallProvider(0),
        tool_context=context,
        config=AgentConfig(
            tool_result_store_dir=str(tmp_path / "results"),
            tool_result_store_session_id="synthetic-session",
            tool_result_store_max_bytes=1024,
            tool_result_store_disk_budget_bytes=4096,
            tool_result_store_retention_seconds=60,
        ),
    )
    for actual in (context, agent._tool_context):
        assert actual is not None
        assert actual.tool_result_store_max_bytes == 1024
        assert actual.tool_result_store_disk_budget_bytes == 4096
        assert actual.tool_result_store_retention_seconds == 60
        assert actual.tool_result_store_dir == str(tmp_path / "results")


@pytest.mark.parametrize("generic_timeout", [0.0, 60.0])
def test_registered_long_tool_budgets_survive_agent_dispatch(generic_timeout: float) -> None:
    from typing import cast

    from opensquilla.mcp.client import MCPClient
    from opensquilla.mcp.discovery import _make_tool_handler
    from opensquilla.mcp.types import MCPToolDef
    from opensquilla.tools.builtin import code_exec  # noqa: F401
    from opensquilla.tools.registry import ToolRegistry, get_default_registry

    registry = ToolRegistry()
    _make_tool_handler(
        cast(MCPClient, object()), "test", "slow", MCPToolDef("slow", "slow", {}),
        registry, timeout_seconds=120,
    )
    mcp = registry.to_tool_definitions()[0]
    execute_code = next(
        tool for tool in get_default_registry().to_tool_definitions()
        if tool.name == "execute_code"
    )
    agent = Agent(
        provider=RepeatedCallProvider(0),
        config=AgentConfig(tool_timeout=generic_timeout),
        tool_definitions=[mcp, execute_code],
    )
    for name in (mcp.name, "execute_code"):
        budget = agent._tool_execution_timeout(
            ToolCall(tool_use_id="long-call", tool_name=name, arguments={"timeout": 120}),
        )
        required = 125 if name == mcp.name else 120 + code_exec._EXECUTION_TIMEOUT_PADDING
        assert budget is not None and budget >= required
