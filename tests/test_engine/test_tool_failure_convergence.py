"""Deterministic shared-loop recovery boundaries without a live model."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, DoneEvent, ErrorEvent, ToolCall, ToolResult
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolStart


class ScriptedProvider:
    provider_name = "offline"

    def __init__(self, calls: list[list[tuple[str, dict[str, Any]]]], *, honors_stop=True):
        self.calls = calls
        self.honors_stop = honors_stop
        self.requests: list[dict[str, Any]] = []
        self.fallbacks = 0

    async def chat(self, messages, **kwargs) -> AsyncIterator[Any]:
        index = len(self.requests)
        self.requests.append({"messages": messages, **kwargs})
        if (self.honors_stop and not kwargs.get("tools")) or index >= len(self.calls):
            yield ProviderText(text="const twice = (n: number): number => n * 2; // unverified")
            yield ProviderDone(stop_reason="stop")
            return
        for offset, (name, arguments) in enumerate(self.calls[index]):
            call_id = f"call-{index}-{offset}"
            yield ProviderToolStart(tool_use_id=call_id, tool_name=name)
            yield ProviderToolEnd(tool_use_id=call_id, tool_name=name, arguments=arguments)
        yield ProviderDone(stop_reason="tool_calls")

    def fallback_after_invalid_response(self, reason):
        self.fallbacks += 1
        return True

    async def list_models(self):
        return []


def make_agent(provider, handler):
    return Agent(
        provider=provider,
        config=AgentConfig(timeout=2, max_provider_retries=0),
        tool_definitions=[
            ToolDefinition(name=name, description="Synthetic tool", input_schema=ToolInputSchema())
            for name in ("probe", "repair", "exec_command")
        ],
        tool_handler=handler,
    )


def failure(call, **fields):
    return ToolResult(call.tool_use_id, call.tool_name, json.dumps(fields), is_error=True)


@pytest.mark.parametrize("honors_stop", [False, True])
@pytest.mark.parametrize("batched", [False, True])
async def test_repeated_failures_have_a_bound_and_a_final_response(honors_stop, batched):
    repetitions = [("probe", {"target": "offline"})] * 12
    provider = ScriptedProvider(
        [repetitions] if batched else [[call] for call in repetitions], honors_stop=honors_stop,
    )
    executed = []

    async def handler(call):
        executed.append(call)
        return failure(call, error="unavailable", attempt=len(executed))

    events = [event async for event in make_agent(provider, handler).run_turn("Provide code.")]
    assert len(executed) == 3
    assert len(provider.requests) <= 4
    assert provider.requests[-1]["tools"] is None
    assert provider.fallbacks == 0
    assert any(isinstance(event, DoneEvent) for event in events)
    if honors_stop or batched:
        assert "const twice" in next(event.text for event in events if isinstance(event, DoneEvent))
    else:
        assert any(
            isinstance(event, ErrorEvent) and event.code == "tool_failure_loop_exhausted"
            for event in events
        )


async def test_runtime_failure_variants_share_recovery_bound():
    provider = ScriptedProvider([
        [("exec_command", {"command": "node --version"})],
        [("exec_command", {"command": "npm --version"})],
        [("exec_command", {"command": "npx tsc --version"})],
    ])
    executed = []

    async def handler(call):
        executed.append(call)
        return failure(call, code="RUNTIME_UNAVAILABLE", componentId="node", retryable=False)

    events = [event async for event in make_agent(provider, handler).run_turn("Provide code.")]
    assert len(executed) == 2
    assert provider.requests[-1]["tools"] is None
    assert any(isinstance(event, DoneEvent) and "const twice" in event.text for event in events)


async def test_nonretryable_exact_call_is_not_dispatched_twice():
    provider = ScriptedProvider([[('probe', {"target": "offline"})]] * 12)
    executed = []

    async def handler(call):
        executed.append(call)
        return failure(call, code="MISSING_DEPENDENCY", retryable=False)

    events = [event async for event in make_agent(provider, handler).run_turn("Provide code.")]
    assert len(executed) == 1
    assert len(provider.requests) == 3
    assert any(isinstance(event, DoneEvent) for event in events)


async def test_different_calls_and_successful_repair_remain_available():
    sequence = [
        ("probe", {"target": "a"}), ("repair", {"target": "a"}),
        ("probe", {"target": "a"}), ("probe", {"target": "b"}),
        ("repair", {"target": "b"}), ("probe", {"target": "b"}),
    ]
    provider = ScriptedProvider([[call] for call in sequence])
    executed = []
    repaired = set()

    async def handler(call: ToolCall):
        executed.append((call.tool_name, call.arguments))
        target = call.arguments["target"]
        if call.tool_name == "repair":
            repaired.add(target)
        if target not in repaired:
            return failure(call, error="missing")
        return ToolResult(call.tool_use_id, call.tool_name, "ready")

    events = [event async for event in make_agent(provider, handler).run_turn("Repair both.")]
    assert executed == sequence
    assert not any(isinstance(event, ErrorEvent) for event in events)
