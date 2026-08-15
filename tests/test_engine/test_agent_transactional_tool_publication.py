from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.contracts.turn_execution import TurnExecutionContext, TurnIdentity
from opensquilla.engine import (
    Agent,
    AgentConfig,
    ToolResult,
    ToolUseStartEvent,
)
from opensquilla.engine.types import (
    AnswerGenerationResetEvent,
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
)
from opensquilla.engine.usage import UsageTracker
from opensquilla.provider import (
    ChatConfig,
    Message,
    ProviderGenerationResetEvent,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import (
    DoneEvent as ProviderDone,
)
from opensquilla.provider import (
    ErrorEvent as ProviderError,
)
from opensquilla.provider import (
    ReasoningDeltaEvent as ProviderReasoning,
)
from opensquilla.provider import (
    TextDeltaEvent as ProviderText,
)
from opensquilla.provider import (
    ToolUseDeltaEvent as ProviderToolUseDelta,
)
from opensquilla.provider import (
    ToolUseEndEvent as ProviderToolUseEnd,
)
from opensquilla.provider import (
    ToolUseStartEvent as ProviderToolUseStart,
)


class _ScriptedProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls: list[list[Message]] = []
        self.done_calls: set[int] = set()

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        call_number = len(self.calls)
        events = self.streams[min(call_number - 1, len(self.streams) - 1)]
        return self._stream(call_number, events)

    async def _stream(self, call_number: int, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            if isinstance(event, ProviderDone):
                self.done_calls.add(call_number)
            yield event

    async def list_models(self) -> list[Any]:
        return []


def _record_definition() -> ToolDefinition:
    return ToolDefinition(
        name="record",
        description="Record a value.",
        input_schema=ToolInputSchema(
            properties={"value": {"type": "string"}},
            required=["value"],
        ),
    )


def _tool_stream(*, terminal: Any) -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id="tool-1", tool_name="record"),
        ProviderToolUseDelta(tool_use_id="tool-1", json_fragment='{"value":'),
        ProviderToolUseDelta(tool_use_id="tool-1", json_fragment='"ok"}'),
        ProviderToolUseEnd(
            tool_use_id="tool-1",
            tool_name="record",
            arguments={"value": "ok"},
        ),
        terminal,
    ]


@pytest.mark.asyncio
async def test_tool_events_and_side_effect_commit_only_after_done() -> None:
    provider = _ScriptedProvider(
        [
            _tool_stream(terminal=ProviderDone(stop_reason="tool_use")),
            [
                ProviderText(text="finished"),
                ProviderDone(stop_reason="stop"),
            ],
        ]
    )
    side_effects: list[Any] = []

    async def tool_handler(call: Any) -> ToolResult:
        assert provider.done_calls == {1}
        side_effects.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="recorded",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2, max_provider_retries=0),
        tool_definitions=[_record_definition()],
        tool_handler=tool_handler,
    )

    events: list[Any] = []
    async for event in agent.run_turn("record ok"):
        if isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent)):
            assert provider.done_calls == {1}
        events.append(event)

    public_tool_events = [
        event
        for event in events
        if isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
    ]
    assert [event.kind for event in public_tool_events] == [
        "tool_use_start",
        "tool_use_delta",
        "tool_use_delta",
        "tool_use_end",
    ]
    assert [
        event.json_fragment
        for event in public_tool_events
        if isinstance(event, ToolUseDeltaEvent)
    ] == [
        '{"value":',
        '"ok"}',
    ]
    end = next(event for event in public_tool_events if isinstance(event, ToolUseEndEvent))
    assert end.arguments == {"value": "ok"}
    tool_event_indices = [
        index
        for index, event in enumerate(events)
        if isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
    ]
    result_index = next(index for index, event in enumerate(events) if event.kind == "tool_result")
    assert max(tool_event_indices) < result_index
    assert len(side_effects) == 1
    assert side_effects[0].arguments == {"value": "ok"}
    assert provider.done_calls == {1, 2}


@pytest.mark.asyncio
async def test_failed_retry_drops_buffered_tool_events_and_does_not_execute() -> None:
    provider = _ScriptedProvider(
        [
            _tool_stream(
                terminal=ProviderError(message="Request timed out", code="timeout")
            ),
            [
                ProviderText(text="fallback response"),
                ProviderDone(stop_reason="stop"),
            ],
        ]
    )
    side_effects: list[Any] = []

    async def tool_handler(call: Any) -> ToolResult:
        side_effects.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="recorded",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_definitions=[_record_definition()],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("record ok")]

    assert len(provider.calls) == 2
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    assert side_effects == []
    assert any(event.kind == "done" and event.text == "fallback response" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream",
    [
        [
            ProviderToolUseStart(tool_use_id="tool-1", tool_name="record"),
            ProviderToolUseDelta(tool_use_id="tool-1", json_fragment='{"value":"ok"}'),
        ],
        [
            ProviderToolUseStart(tool_use_id="tool-1", tool_name="record"),
            ProviderToolUseDelta(tool_use_id="tool-1", json_fragment='{"value":"ok"}'),
            ProviderToolUseEnd(
                tool_use_id="tool-1",
                tool_name="record",
                arguments={"value": "ok"},
            ),
        ],
        _tool_stream(
            terminal=ProviderError(message="upstream failed", code="request_error")
        ),
    ],
    ids=["missing_end", "missing_done", "error"],
)
async def test_incomplete_or_failed_generation_drops_pending_tool_events(
    stream: list[Any],
) -> None:
    provider = _ScriptedProvider([stream])
    side_effects: list[Any] = []

    async def tool_handler(call: Any) -> ToolResult:
        side_effects.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="recorded",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, max_provider_retries=0),
        tool_definitions=[_record_definition()],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("record ok")]

    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    assert side_effects == []
    assert any(event.kind == "error" for event in events)


@pytest.mark.asyncio
async def test_generation_reset_replaces_answer_and_drops_old_epoch_events() -> None:
    provider = _ScriptedProvider(
        [
            [
                ProviderText(text="old text", generation_epoch=0),
                ProviderReasoning(text="old reasoning", generation_epoch=0),
                ProviderGenerationResetEvent(
                    from_role="primary_aggregator",
                    to_role="fixed_direct",
                    safe_reason="provider takeover",
                ),
                ProviderText(text="new text", generation_epoch=1),
                ProviderReasoning(text="new reasoning", generation_epoch=1),
                ProviderText(text="stale text", generation_epoch=0),
                ProviderDone(stop_reason="stop", generation_epoch=1),
            ]
        ]
    )
    context = TurnExecutionContext.create(
        TurnIdentity("turn-reset", "assistant-reset", "agent:main:reset")
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1, max_provider_retries=0),
        execution_context=context,
    )

    events = [event async for event in agent.run_turn("reset")]
    reset_index = next(
        index for index, event in enumerate(events) if isinstance(event, AnswerGenerationResetEvent)
    )
    text_events = [event for event in events if isinstance(event, TextDeltaEvent)]
    thinking_events = [event for event in events if isinstance(event, ThinkingEvent)]
    done = next(event for event in events if isinstance(event, DoneEvent))

    assert not any(event.kind == "error" for event in events[: reset_index + 1])
    assert [event.text for event in text_events] == ["old text", "new text"]
    assert [event.generation_epoch for event in text_events] == [0, 1]
    assert [event.text for event in thinking_events] == ["old reasoning", "new reasoning"]
    assert thinking_events[-1].generation_epoch == 1
    assert done.text == "new text"
    assert done.generation_epoch == 1


@pytest.mark.asyncio
async def test_terminal_generation_reset_carries_usage_without_second_terminal() -> None:
    terminal_text = "The fixed model could not complete this answer."
    usage_row = {
        "role": "fixed_direct",
        "provider": "fixed",
        "model": "fixed-model",
        "input_tokens": 4,
        "output_tokens": 2,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
    }
    provider = _ScriptedProvider(
        [
            [
                ProviderText(text="partial fixed answer", generation_epoch=0),
                ProviderGenerationResetEvent(
                    from_role="fixed_direct",
                    to_role="fixed_direct",
                    safe_reason="fixed provider final failure",
                    terminal=True,
                    terminal_text_snapshot=terminal_text,
                    terminal_error_message="unauthorized",
                    terminal_error_code="401",
                    model_usage_breakdown=[usage_row],
                    ensemble_trace={"llm_request_count": 1},
                ),
            ]
        ]
    )
    context = TurnExecutionContext.create(
        TurnIdentity(
            "turn-terminal-reset",
            "assistant-terminal-reset",
            "agent:main:terminal-reset",
        )
    )
    usage = UsageTracker()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=0,
            provider_id="openrouter",
        ),
        execution_context=context,
        usage_tracker=usage,
        session_key="agent:main:terminal-reset",
    )

    events = [event async for event in agent.run_turn("reset")]

    terminal_reset = next(
        event
        for event in events
        if isinstance(event, AnswerGenerationResetEvent) and event.terminal
    )
    assert terminal_reset.terminal_text_snapshot == terminal_text
    assert terminal_reset.terminal_error_code == "401"
    assert terminal_reset.terminal_failure_kind == "auth_invalid"
    assert "credentials" in terminal_reset.terminal_error_message.lower()
    assert not any(isinstance(event, ErrorEvent) for event in events)
    accounting_done = next(event for event in events if isinstance(event, DoneEvent))
    assert accounting_done.text_snapshot == ""
    assert accounting_done.input_tokens == 4
    assert accounting_done.output_tokens == 2
    assert accounting_done.generation_epoch == terminal_reset.new_generation_epoch
    tracked = usage.get("agent:main:terminal-reset")
    assert tracked is not None
    assert tracked.input_tokens == 4
    assert tracked.output_tokens == 2
    assert len(provider.calls) == 1
