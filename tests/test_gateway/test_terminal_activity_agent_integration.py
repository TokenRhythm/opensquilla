"""Offline Activity completeness across the agent and public stream boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.turn_runner.stream_consumer_stage import (
    StreamConsumerStage,
    StreamConsumerStageInput,
    _StreamState,
)
from opensquilla.engine.types import AgentEvent, DoneEvent, public_agent_event_payload
from opensquilla.gateway.session_streams import SessionStreamRegistry
from opensquilla.gateway.terminal_activity import terminal_activity_snapshot
from opensquilla.provider import (
    DoneEvent as ProviderDone,
)
from opensquilla.provider import (
    ErrorEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


class _ToolThenAnswerProvider:
    provider_name = "synthetic"

    def __init__(self, *, call_count: int, succeeds: bool) -> None:
        self.call_count = call_count
        self.succeeds = succeeds
        self.calls = 0

    async def chat(self, messages, tools=None, config=None):
        del messages, tools, config
        self.calls += 1
        reasoning = f"Reasoning {self.calls} 😀"
        yield ReasoningDeltaEvent(text=reasoning)
        yield TextDeltaEvent(text=f"Text {self.calls} 😀.")
        if self.calls < self.call_count:
            tool_id = f"call-{self.calls}"
            yield ToolUseStartEvent(tool_use_id=tool_id, tool_name="record")
            yield ToolUseEndEvent(tool_use_id=tool_id, tool_name="record", arguments={})
            yield ProviderDone(stop_reason="tool_use", reasoning_content=reasoning)
        elif self.succeeds:
            yield ProviderDone(reasoning_content=reasoning)
        else:
            yield ErrorEvent(message="Synthetic request failed", code="request_error")


class _AgentStream:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def run_turn(
        self, agent: Agent, *, turn_input: str, **kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        async for event in agent.run_turn(turn_input, **kwargs):
            self.events.append(event)
            yield event


@pytest.mark.parametrize("call_count,succeeds", [(2, False), (3, False), (2, True)])
async def test_terminal_activity_matches_committed_agent_output_across_tool_calls(
    tmp_path, call_count: int, succeeds: bool,
) -> None:
    provider = _ToolThenAnswerProvider(call_count=call_count, succeeds=succeeds)
    executed: list[str] = []

    async def record(call):
        executed.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="Synthetic result",
        )

    definitions = [ToolDefinition(
        name="record", description="Record a synthetic result",
        input_schema=ToolInputSchema(properties={}),
    )]
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=call_count, max_provider_retries=0, workspace_dir=str(tmp_path),
        ),
        tool_definitions=definitions,
        tool_handler=record,
    )
    stream = _AgentStream()
    stage = StreamConsumerStage(
        agent_run=stream,
        compaction_persist=Mock(),
        memory_snapshot_refresh=Mock(),
        system_prompt_refresh=Mock(),
        memory_sync_notify=SimpleNamespace(notify_message_bytes=Mock()),
        warning_transformer=lambda event: event,
    )
    state = _StreamState()
    session_key = "agent:main:webchat:synthetic-activity"
    inp = StreamConsumerStageInput(
        agent=agent, agent_id="main", sync_manager=None, private_memory_allowed=False,
        turn=SimpleNamespace(metadata={}, tool_defs=definitions), tool_defs=definitions,
        turn_input="Run the synthetic task", extra_messages=None,
        semantic_input="Run the synthetic task", effective_runtime_message="Synthetic input",
        session_key=session_key, run_kind="default", heartbeat_ack_max_chars=300,
        bootstrap_context_mode=None, router_cfg=None, session_manager_present=True,
        state=state,
    )
    public_events = [event async for event in stage.run(inp)]
    # The runner emits the held error after finalization. Preserve that public
    # done/error order here rather than feeding raw agent events to the registry.
    if state.pending_error_event is not None:
        public_events.append(state.pending_error_event)
    assert [event.kind for event in public_events if event.kind in {"done", "error"}] == (
        ["done"] if succeeds else ["done", "error"]
    )

    registry = SessionStreamRegistry()
    for event in public_events:
        payload = public_agent_event_payload(event)
        kind = payload.pop("kind")
        registry.record(session_key, f"session.event.{kind}", {
            **payload, "task_id": "task", "turn_id": "task",
        })
    snapshot = registry.take_terminal_activity_snapshot(session_key, "task", turn_id="task")
    assert snapshot is not None
    assert terminal_activity_snapshot(snapshot, task_id="task", turn_id="task") == snapshot
    assert snapshot["complete"] is succeeds
    assert provider.calls == call_count
    assert executed == [f"call-{index}" for index in range(1, call_count)]

    done = next(event for event in stream.events if isinstance(event, DoneEvent))
    committed_calls = call_count if succeeds else call_count - 1
    committed_reasoning = "\n".join(
        f"Reasoning {index} 😀" for index in range(1, committed_calls + 1)
    )
    assert done.reasoning_content == committed_reasoning
    assert done.text_snapshot == "".join(
        f"Text {index} 😀." for index in range(1, committed_calls + 1)
    )
    assert done.assistant_replay is not None
    assistants = [row for row in done.assistant_replay["messages"] if row["role"] == "assistant"]
    assert len(assistants) == committed_calls
    assert "\n".join(row["reasoning_content"] for row in assistants) == committed_reasoning

    # A non-BMP character occupies two UTF-16 units. Each physical call's
    # reasoning contributes once, with a newline only between calls.
    streamed_reasoning = "\n".join(
        f"Reasoning {index} 😀" for index in range(1, call_count + 1)
    )
    assert snapshot["reasoning_utf16_length"] == len(streamed_reasoning) + call_count
    if succeeds:
        text_lengths = [
            entry["text_utf16_length"] for entry in snapshot["entries"]
            if entry.get("segment_type") == "text"
        ]
        assert text_lengths == [
            len(f"Text {index} 😀.") + 1 for index in range(1, call_count + 1)
        ]
    tools = [entry for entry in snapshot["entries"] if entry.get("segment_type") == "tool"]
    assert [(entry["tool_use_id"], entry["is_error"]) for entry in tools] == [
        (tool_id, False) for tool_id in executed
    ]
    reasoning = [entry for entry in snapshot["entries"] if entry["type"] == "reasoning"]
    for index, tool in enumerate(tools):
        assert reasoning[index]["order"] < tool["order"] < reasoning[index + 1]["order"]
