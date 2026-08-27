"""Agent re-emit hop: provider ReasoningDeltaEvent -> engine ThinkingEvent.

The agent layer translates provider-level stream events into engine-level
events. Reasoning must cross this hop as a first-class ThinkingEvent (not folded
into assistant text), so the TUI can render it as a distinct thinking stream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.types import ThinkingEndEvent, ThinkingEvent, ThinkingStartEvent
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ReasoningDeltaEvent as ProviderReasoning
from opensquilla.provider import TextDeltaEvent as ProviderText


class _ReasoningProvider:
    provider_name = "fake"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderReasoning(text="Let me ")
        yield ProviderReasoning(text="think.")
        yield ProviderText(text="The answer.")
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=5,
            output_tokens=3,
            reasoning_content="Let me think.",
        )

    async def list_models(self) -> list[Any]:
        return []


class _EmptyThenReasoningProvider(_ReasoningProvider):
    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderReasoning(text="")
        yield ProviderReasoning(text="first")
        yield ProviderReasoning(text=" second")
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=2,
            output_tokens=2,
            reasoning_content="first second",
        )


class _ReasoningThenRaiseProvider(_ReasoningProvider):
    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderReasoning(text="partial")
        raise RuntimeError("synthetic provider failure")


def test_agent_reemits_reasoning_as_thinking_event() -> None:
    import asyncio

    async def run() -> list[Any]:
        agent = Agent(provider=_ReasoningProvider(), config=AgentConfig(max_iterations=1))
        return [event async for event in agent.run_turn("hi")]

    events = asyncio.run(run())

    thinking = [e for e in events if isinstance(e, ThinkingEvent)]
    assert [t.text for t in thinking] == ["Let me ", "think."]
    assert thinking[0].started_at > 0
    assert thinking[1].started_at == thinking[0].started_at
    starts = [e for e in events if isinstance(e, ThinkingStartEvent)]
    ends = [e for e in events if isinstance(e, ThinkingEndEvent)]
    assert len(starts) == len(ends) == 1
    assert starts[0].block_id == thinking[0].block_id == ends[0].block_id
    assert starts[0].block_index == thinking[0].block_index == ends[0].block_index == 0
    assert ends[0].status == "completed"

    # reasoning must NOT be folded into the assistant answer text
    answer = "".join(
        e.text for e in events if type(e).__name__ == "TextDeltaEvent"
    )
    assert answer == "The answer."
    assert "think" not in answer


def test_reasoning_timer_starts_on_first_nonempty_delta() -> None:
    import asyncio

    async def run() -> list[Any]:
        agent = Agent(
            provider=_EmptyThenReasoningProvider(),
            config=AgentConfig(max_iterations=1, max_provider_retries=0),
        )
        return [event async for event in agent.run_turn("hi")]

    thinking = [
        event
        for event in asyncio.run(run())
        if isinstance(event, ThinkingEvent)
    ]

    assert [event.text for event in thinking] == ["first", " second"]
    assert thinking[0].started_at > 0
    assert thinking[1].started_at == thinking[0].started_at


def test_thinking_event_keeps_legacy_default_start_time() -> None:
    assert ThinkingEvent(text="legacy").started_at == 0
    assert ThinkingEvent(text="legacy").block_id == ""
    assert ThinkingEvent(text="legacy").block_index == -1


def test_reasoning_block_ends_when_provider_stream_raises() -> None:
    import asyncio

    async def run() -> list[Any]:
        events: list[Any] = []
        agent = Agent(
            provider=_ReasoningThenRaiseProvider(),
            config=AgentConfig(max_iterations=1),
        )
        try:
            async for event in agent.run_turn("hi"):
                events.append(event)
        except RuntimeError as exc:
            assert str(exc) == "synthetic provider failure"
        return events

    events = asyncio.run(run())
    starts = [event for event in events if isinstance(event, ThinkingStartEvent)]
    ends = [event for event in events if isinstance(event, ThinkingEndEvent)]

    assert len(starts) == len(ends) == 1
    assert ends[0].block_id == starts[0].block_id
    assert ends[0].status == "error"


from opensquilla.engine import ToolResult  # noqa: E402
from opensquilla.engine.types import TextDeltaEvent, ToolCall, ToolUseStartEvent  # noqa: E402
from opensquilla.provider import (  # noqa: E402
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd  # noqa: E402
from opensquilla.provider import ToolUseStartEvent as ProviderToolStart  # noqa: E402


class _TextThenToolThenAnswerProvider:
    """Round 1: narration text + a tool call. Round 2: final answer text."""

    provider_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls += 1
        return self._stream(self.calls)

    async def _stream(self, call: int) -> AsyncIterator[Any]:
        if call == 1:
            # text the model speaks before deciding to call a tool
            yield ProviderText(text="Let me look ")
            yield ProviderText(text="at the file.")
            yield ProviderToolStart(tool_use_id="t1", tool_name="read")
            yield ProviderToolEnd(tool_use_id="t1", tool_name="read", arguments={})
            yield ProviderDone(stop_reason="tool_use", input_tokens=5, output_tokens=2)
            return
        # final answer round: no tool
        yield ProviderText(text="Here is the answer.")
        yield ProviderDone(stop_reason="end_turn", input_tokens=6, output_tokens=3)


class _ReasoningToolThenReasoningAnswerProvider(_TextThenToolThenAnswerProvider):
    async def _stream(self, call: int) -> AsyncIterator[Any]:
        if call == 1:
            yield ProviderReasoning(text="choose tool")
            yield ProviderToolStart(tool_use_id="t1", tool_name="read")
            yield ProviderToolEnd(tool_use_id="t1", tool_name="read", arguments={})
            yield ProviderDone(
                stop_reason="tool_use",
                input_tokens=5,
                output_tokens=2,
                reasoning_content="choose tool",
            )
            return
        yield ProviderReasoning(text="review result")
        yield ProviderText(text="Here is the answer.")
        yield ProviderDone(
            stop_reason="end_turn",
            input_tokens=6,
            output_tokens=3,
            reasoning_content="review result",
        )


class _InterleavedReasoningBoundaryProvider(_ReasoningProvider):
    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderReasoning(text="before text")
        yield ProviderText(text="answer prefix")
        yield ProviderReasoning(text="before tool")
        yield ProviderToolStart(tool_use_id="ignored", tool_name="unavailable")
        yield ProviderToolEnd(
            tool_use_id="ignored",
            tool_name="unavailable",
            arguments={},
        )
        yield ProviderReasoning(text="after tool")
        yield ProviderText(text="answer suffix")
        yield ProviderDone(
            stop_reason="end_turn",
            input_tokens=6,
            output_tokens=3,
            reasoning_content="before textbefore toolafter tool",
        )


def test_reasoning_blocks_close_before_text_and_tool_boundaries() -> None:
    import asyncio

    async def run() -> list[Any]:
        agent = Agent(
            provider=_InterleavedReasoningBoundaryProvider(),
            config=AgentConfig(max_iterations=1),
        )
        return [event async for event in agent.run_turn("hi")]

    events = asyncio.run(run())
    lifecycle = [
        event
        for event in events
        if isinstance(
            event,
            (ThinkingStartEvent, ThinkingEvent, ThinkingEndEvent, TextDeltaEvent),
        )
    ]

    expected_first_call = [
        ThinkingStartEvent,
        ThinkingEvent,
        ThinkingEndEvent,
        TextDeltaEvent,
        ThinkingStartEvent,
        ThinkingEvent,
        ThinkingEndEvent,
        ThinkingStartEvent,
        ThinkingEvent,
        ThinkingEndEvent,
        TextDeltaEvent,
    ]
    assert [type(event) for event in lifecycle[: len(expected_first_call)]] == (
        expected_first_call
    )
    assert [
        event.block_index
        for event in lifecycle
        if isinstance(event, ThinkingStartEvent)
    ][:3] == [0, 1, 2]
    assert [
        event.status
        for event in lifecycle
        if isinstance(event, ThinkingEndEvent)
    ][:3] == ["completed", "completed", "completed"]


def test_reasoning_blocks_follow_physical_provider_call_boundaries() -> None:
    import asyncio

    async def tool_handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="{}",
        )

    async def run() -> list[Any]:
        agent = Agent(
            provider=_ReasoningToolThenReasoningAnswerProvider(),
            config=AgentConfig(max_iterations=3),
            tool_definitions=[
                ToolDefinition(
                    name="read",
                    description="read a file",
                    input_schema=ToolInputSchema(properties={}, required=[]),
                )
            ],
            tool_handler=tool_handler,
        )
        return [event async for event in agent.run_turn("hi")]

    events = asyncio.run(run())
    starts = [event for event in events if isinstance(event, ThinkingStartEvent)]
    deltas = [event for event in events if isinstance(event, ThinkingEvent)]
    ends = [event for event in events if isinstance(event, ThinkingEndEvent)]

    assert [event.block_index for event in starts] == [0, 1]
    assert [event.block_id for event in starts] == [event.block_id for event in deltas]
    assert [event.block_id for event in starts] == [event.block_id for event in ends]
    assert [event.status for event in ends] == ["completed", "completed"]


def test_text_streams_live_as_answer_until_a_tool_appears() -> None:
    """Issue #358 regression guard: assistant text streams live, token by token, the
    moment it arrives instead of being buffered until the provider call ends. Until a
    tool appears the running text is the answer (the common plain-Q&A case), so nothing
    is held back and the UI updates incrementally rather than freezing then dumping the
    whole reply at once."""
    import asyncio

    async def tool_handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="{}",
        )

    async def run() -> list[Any]:
        agent = Agent(
            provider=_TextThenToolThenAnswerProvider(),
            config=AgentConfig(max_iterations=3),
            tool_definitions=[
                ToolDefinition(
                    name="read",
                    description="read a file",
                    input_schema=ToolInputSchema(properties={}, required=[]),
                )
            ],
            tool_handler=tool_handler,
        )
        return [event async for event in agent.run_turn("hi")]

    events = asyncio.run(run())
    text_events = [e for e in events if isinstance(e, TextDeltaEvent)]

    # Every provider text delta is surfaced as its own live event — no buffering and
    # no end-of-call flush that lumps the reply into one blob.
    assert [e.text for e in text_events] == [
        "Let me look ",
        "at the file.",
        "Here is the answer.",
    ]
    # No tool has appeared when any of this text arrives, so all of it streams as the
    # answer; nothing is withheld as intermediate.
    intermediate = "".join(e.text for e in text_events if e.presentation == "intermediate")
    answer = "".join(e.text for e in text_events if e.presentation == "answer")
    assert intermediate == ""
    assert answer == "Let me look at the file.Here is the answer."


class _ToolThenTextThenAnswerProvider:
    """Round 1: a tool call, then narration text *after* it. Round 2: final answer."""

    provider_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls += 1
        return self._stream(self.calls)

    async def _stream(self, call: int) -> AsyncIterator[Any]:
        if call == 1:
            yield ProviderToolStart(tool_use_id="t1", tool_name="read")
            yield ProviderToolEnd(tool_use_id="t1", tool_name="read", arguments={})
            # narration the model speaks between tool calls
            yield ProviderText(text="Now let me ")
            yield ProviderText(text="explain.")
            yield ProviderDone(stop_reason="tool_use", input_tokens=5, output_tokens=2)
            return
        yield ProviderText(text="Final answer.")
        yield ProviderDone(stop_reason="end_turn", input_tokens=6, output_tokens=3)


def test_text_after_a_tool_is_intermediate_narration() -> None:
    """Once a tool has appeared in a round, later text in that round is intermediate
    narration between tools (purple), while the final no-tool round is the answer."""
    import asyncio

    async def tool_handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="{}",
        )

    async def run() -> list[Any]:
        agent = Agent(
            provider=_ToolThenTextThenAnswerProvider(),
            config=AgentConfig(max_iterations=3),
            tool_definitions=[
                ToolDefinition(
                    name="read",
                    description="read a file",
                    input_schema=ToolInputSchema(properties={}, required=[]),
                )
            ],
            tool_handler=tool_handler,
        )
        return [event async for event in agent.run_turn("hi")]

    events = asyncio.run(run())
    text_events = [e for e in events if isinstance(e, TextDeltaEvent)]

    intermediate = "".join(e.text for e in text_events if e.presentation == "intermediate")
    answer = "".join(e.text for e in text_events if e.presentation == "answer")

    assert intermediate == "Now let me explain."
    assert answer == "Final answer."


def test_tool_use_start_carries_server_start_timestamp() -> None:
    """Issue #329: the engine stamps ToolUseStartEvent with a server wall-clock start
    time (epoch ms). A client seeds the running tool's elapsed timer from this instead
    of its own clock, so the timer survives page switches / stream replay (where the
    component remounts) rather than restarting from zero on every remount."""
    import asyncio
    import time

    async def tool_handler(call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="{}",
        )

    async def run() -> list[Any]:
        agent = Agent(
            provider=_ToolThenTextThenAnswerProvider(),
            config=AgentConfig(max_iterations=3),
            tool_definitions=[
                ToolDefinition(
                    name="read",
                    description="read a file",
                    input_schema=ToolInputSchema(properties={}, required=[]),
                )
            ],
            tool_handler=tool_handler,
        )
        return [event async for event in agent.run_turn("hi")]

    before_ms = int(time.time() * 1000)
    events = asyncio.run(run())
    after_ms = int(time.time() * 1000)

    starts = [e for e in events if isinstance(e, ToolUseStartEvent)]
    assert starts, "expected at least one tool_use_start event"
    for ev in starts:
        # Stamped with a real server time within the turn's wall-clock window, not the
        # 0 "unstamped" sentinel.
        assert ev.started_at > 0
        assert before_ms <= ev.started_at <= after_ms
