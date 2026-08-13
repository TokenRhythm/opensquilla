from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.action_completion import ACTION_COMPLETION_TOOL_NAME
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart


class _SequenceProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, config=None) -> AsyncIterator[Any]:  # noqa: ANN001
        index = len(self.calls)
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_text_only_recovery_warns_then_records_next_tool_action(tmp_path) -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="I should inspect the repository."),
                ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=1),
            ],
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "ok"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=4, output_tokens=1),
            ],
            [
                ProviderText(text="done"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=1),
            ],
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="tool ok",
        )

    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=3,
            text_only_tool_recovery_mode="warn_model",
            runtime_events_path=str(runtime_events_path),
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(
                    properties={"value": {"type": "string"}},
                    required=["value"],
                ),
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" and event.text == "done" for event in events)
    assert any(
        event.kind == "warning" and event.code == "text_only_tool_recovery"
        for event in events
    )
    assert len(provider.calls) == 3
    assert any(
        msg.role == "user"
        and isinstance(msg.content, str)
        and "Previous assistant turn had text only" in msg.content
        for msg in provider.calls[1]["messages"]
    )
    assert not any(
        msg.role == "user"
        and isinstance(msg.content, str)
        and "Previous assistant turn had text only" in msg.content
        for msg in agent._history
    )
    logged = [json.loads(line) for line in runtime_events_path.read_text().splitlines()]
    recovery = [
        event for event in logged if event.get("mechanism") == "text_only_tool_recovery"
    ]
    assert [event["action"] for event in recovery] == ["nudge", "observe"]
    assert recovery[0]["injected_to_model"] is True
    assert recovery[1]["details"]["next_action"] == "tool_call"


@pytest.mark.asyncio
async def test_text_only_recovery_log_mode_does_not_inject(tmp_path) -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="I should inspect the repository."),
                ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=1),
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            text_only_tool_recovery_mode="log",
            runtime_events_path=str(tmp_path / "runtime_events.jsonl"),
        ),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )
        ],
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 1


def _tool_definition(
    name: str,
    *,
    completion_effect: Literal["unknown", "read_only", "action", "control"],
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} test tool.",
        input_schema=ToolInputSchema(properties={}, required=[]),
        completion_effect=completion_effect,
    )


def _tool_call_stream(
    tool_use_id: str,
    tool_name: str,
    *,
    arguments: dict[str, Any] | None = None,
    text: str = "",
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> list[Any]:
    events: list[Any] = []
    if text:
        events.append(ProviderText(text=text))
    events.extend(
        [
            ProviderToolUseStart(tool_use_id=tool_use_id, tool_name=tool_name),
            ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name=tool_name,
                arguments=arguments or {},
            ),
            ProviderDone(
                stop_reason="tool_use",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        ]
    )
    return events


def _text_stream(
    text: str,
    *,
    input_tokens: int = 1,
    output_tokens: int = 1,
) -> list[Any]:
    return [
        ProviderText(text=text),
        ProviderDone(
            stop_reason="stop",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    ]


@pytest.mark.asyncio
async def test_action_tool_text_only_recovers_once_then_accepts_completion_evidence() -> None:
    provider = _SequenceProvider(
        [
            _tool_call_stream("action-1", "exec_command", input_tokens=2),
            _text_stream("The check passed; I will start the service now.", input_tokens=3),
            [
                ProviderText(text="The requested service is running."),
                ProviderToolUseStart(
                    tool_use_id="complete-1",
                    tool_name=ACTION_COMPLETION_TOOL_NAME,
                ),
                ProviderToolUseEnd(
                    tool_use_id="complete-1",
                    tool_name=ACTION_COMPLETION_TOOL_NAME,
                    arguments={"summary": "The requested service is running."},
                ),
                ProviderDone(
                    stop_reason="tool_use",
                    input_tokens=5,
                    output_tokens=1,
                ),
            ],
        ]
    )
    handled_tools: list[str] = []

    async def tool_handler(call: Any) -> ToolResult:
        handled_tools.append(call.tool_name)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="command ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=5),
        tool_definitions=[_tool_definition("exec_command", completion_effect="action")],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("start the service")]

    assert handled_tools == ["exec_command"]
    assert len(provider.calls) == 3
    assert all(
        tool.name != ACTION_COMPLETION_TOOL_NAME for tool in provider.calls[0]["tools"]
    )
    assert any(
        tool.name == ACTION_COMPLETION_TOOL_NAME for tool in provider.calls[1]["tools"]
    )
    assert any(
        event.kind == "warning" and event.code == "action_completion_recovery"
        for event in events
    )
    done = next(event for event in events if event.kind == "done")
    assert done.text == "The requested service is running."
    assert done.input_tokens == 10
    assert done.output_tokens == 3
    assert agent.config.metadata["action_completion_contracts_armed"] == 1
    assert agent.config.metadata["action_completion_recoveries"] == 1
    assert agent.config.metadata["action_completion_evidence"] == 1


@pytest.mark.asyncio
async def test_summary_only_completion_evidence_requires_a_final_visible_answer() -> None:
    provider = _SequenceProvider(
        [
            _tool_call_stream("action-1", "exec_command"),
            _tool_call_stream(
                "complete-1",
                ACTION_COMPLETION_TOOL_NAME,
                arguments={"summary": "The requested service is running."},
            ),
            _text_stream("The requested service is running."),
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="command ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=5),
        tool_definitions=[_tool_definition("exec_command", completion_effect="action")],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("start the service")]

    assert len(provider.calls) == 3
    assert any(
        event.kind == "done" and event.text == "The requested service is running."
        for event in events
    )
    assert not any(event.kind == "warning" for event in events)


@pytest.mark.asyncio
async def test_action_tool_repeated_text_only_is_incomplete_without_replay() -> None:
    provider = _SequenceProvider(
        [
            _tool_call_stream("action-1", "exec_command", input_tokens=2),
            _text_stream("I will perform the remaining action now.", input_tokens=3),
            _text_stream("I will perform the remaining action now.", input_tokens=5),
        ]
    )
    handled_tools: list[str] = []

    async def tool_handler(call: Any) -> ToolResult:
        handled_tools.append(call.tool_name)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="inspection complete",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=5),
        tool_definitions=[_tool_definition("exec_command", completion_effect="action")],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("start the service")]

    assert handled_tools == ["exec_command"]
    assert len(provider.calls) == 3
    assert sum(event.kind == "warning" for event in events) == 1
    error = next(event for event in events if event.kind == "error")
    assert error.code == "action_completion_incomplete"
    done = next(event for event in events if event.kind == "done")
    assert done.input_tokens == 10
    assert done.output_tokens == 3
    assert agent.config.metadata["action_completion_recoveries"] == 1
    assert agent.config.metadata["action_completion_incomplete"] == 1


@pytest.mark.asyncio
async def test_plain_qa_with_action_tools_available_is_not_recovered() -> None:
    provider = _SequenceProvider([_text_stream("Paris.")])
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[_tool_definition("exec_command", completion_effect="action")],
    )

    events = [event async for event in agent.run_turn("What is France's capital?")]

    assert len(provider.calls) == 1
    assert any(event.kind == "done" and event.text == "Paris." for event in events)
    assert not any(event.kind == "warning" for event in events)


@pytest.mark.asyncio
async def test_read_only_tool_qa_is_not_recovered() -> None:
    provider = _SequenceProvider(
        [
            _tool_call_stream("read-1", "web_search"),
            _text_stream("The cited release date is August 13."),
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="release date: August 13",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[_tool_definition("web_search", completion_effect="read_only")],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("When was the release?")]

    assert len(provider.calls) == 2
    assert any(
        event.kind == "done" and event.text == "The cited release date is August 13."
        for event in events
    )
    assert not any(event.kind == "warning" for event in events)


@pytest.mark.asyncio
async def test_terminating_action_with_visible_output_needs_no_completion_retry() -> None:
    provider = _SequenceProvider(
        [_tool_call_stream("action-1", "background_process", text="Started service.")]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="session: service-1",
            terminates_turn=True,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[
            _tool_definition("background_process", completion_effect="action")
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("start the service")]

    assert len(provider.calls) == 1
    assert any(event.kind == "done" and event.text == "Started service." for event in events)
    assert not any(event.kind == "warning" for event in events)
