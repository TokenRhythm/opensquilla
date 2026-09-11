"""Tool execution facts survive retries without heuristic intervention."""

from __future__ import annotations

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
