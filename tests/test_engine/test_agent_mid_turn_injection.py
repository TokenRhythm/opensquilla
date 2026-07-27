from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.agent_injection import ListPendingInputProvider
from opensquilla.engine.types import ToolCall, ToolResultEvent
from opensquilla.gateway.user_input_broker import StructuredUserInputBroker
from opensquilla.provider import (
    ChatConfig,
    ContentBlockText,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import (
    DoneEvent as ProviderDone,
)
from opensquilla.provider import (
    TextDeltaEvent as ProviderText,
)
from opensquilla.provider import (
    ToolUseEndEvent as ProviderToolUseEnd,
)
from opensquilla.provider import (
    ToolUseStartEvent as ProviderToolUseStart,
)
from opensquilla.tools.types import ToolContext


class _ToolBoundaryProvider:
    provider_name = "fake"

    def __init__(self, *, tool_iterations: int = 1) -> None:
        self.tool_iterations = tool_iterations
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(list(messages))
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number <= self.tool_iterations:
            tool_use_id = f"tool-{call_number}"
            yield ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="echo")
            yield ProviderToolUseEnd(
                tool_use_id=tool_use_id,
                tool_name="echo",
                arguments={"value": call_number},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return

        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


class _NoToolProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(list(messages))
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="done")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _tool_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Mock {name}.",
        input_schema=ToolInputSchema(properties={}, required=[]),
    )


async def _tool_handler(call: ToolCall) -> ToolResult:
    return ToolResult(
        tool_use_id=call.tool_use_id,
        tool_name=call.tool_name,
        content=f"result from {call.tool_name} {call.tool_use_id}",
    )


def _agent(provider: Any, *, max_iterations: int = 3) -> Agent:
    return Agent(
        provider=provider,
        config=AgentConfig(max_iterations=max_iterations),
        tool_definitions=[_tool_def("echo")],
        tool_handler=_tool_handler,
    )


def _text_block_texts(message: Message) -> list[str]:
    if not isinstance(message.content, list):
        return []
    return [block.text for block in message.content if isinstance(block, ContentBlockText)]


def _is_tool_result_message(message: Message) -> bool:
    return message.role == "user" and isinstance(message.content, list) and any(
        getattr(block, "type", None) == "tool_result" for block in message.content
    )


def _message_text(message: Message) -> str:
    if isinstance(message.content, str):
        return message.content
    if not isinstance(message.content, list):
        return ""
    return "\n".join(
        block.text for block in message.content if isinstance(block, ContentBlockText)
    )


def _tool_result_index(messages: list[Message]) -> int:
    return next(index for index, message in enumerate(messages) if _is_tool_result_message(message))


def _text_message_index(messages: list[Message], texts: list[str]) -> int:
    return next(
        index
        for index, message in enumerate(messages)
        if message.role == "user" and _text_block_texts(message) == texts
    )


def _text_messages(messages: list[Message], texts: list[str]) -> list[Message]:
    return [
        message
        for message in messages
        if message.role == "user" and _text_block_texts(message) == texts
    ]


@pytest.mark.asyncio
async def test_pending_input_is_injected_after_tool_result_and_seen_by_next_model_request() -> None:
    provider = _ToolBoundaryProvider()
    pending = ListPendingInputProvider()
    pending.append("INJECTED")
    agent = _agent(provider)

    events = [event async for event in agent.run_turn("run echo", pending_input_provider=pending)]

    assert len(provider.calls) == 2
    second_request = provider.calls[1]
    assert _text_message_index(second_request, ["INJECTED"]) == (
        _tool_result_index(second_request) + 1
    )
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.asyncio
async def test_multiple_pending_inputs_are_merged_into_one_user_message() -> None:
    provider = _ToolBoundaryProvider()
    pending = ListPendingInputProvider()
    pending.append("A")
    pending.append("B")
    agent = _agent(provider)

    _events = [event async for event in agent.run_turn("run echo", pending_input_provider=pending)]

    second_request = provider.calls[1]
    injected_messages = _text_messages(second_request, ["A", "B"])
    assert len(injected_messages) == 1
    assert isinstance(injected_messages[0].content, list)
    assert _text_block_texts(injected_messages[0]) == ["A", "B"]
    assert not _text_messages(second_request, ["A"])
    assert not _text_messages(second_request, ["B"])


@pytest.mark.asyncio
async def test_no_pending_provider_anchors_current_request_after_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_TURN_OBJECTIVE_REMINDER", "on")
    provider = _ToolBoundaryProvider()
    agent = _agent(provider)

    _events = [event async for event in agent.run_turn("run echo")]

    assert len(provider.calls) == 2
    second_request = provider.calls[1]
    tool_result_index = _tool_result_index(second_request)
    reminder_text = _message_text(second_request[tool_result_index + 1])
    assert "Current user request" in reminder_text
    assert "run echo" in reminder_text
    assert not any(
        "Current user request" in _message_text(message) for message in agent._history
    )


@pytest.mark.asyncio
async def test_no_pending_default_sends_no_reminder_after_tool_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENSQUILLA_TURN_OBJECTIVE_REMINDER", raising=False)
    provider = _ToolBoundaryProvider()
    agent = _agent(provider)

    _events = [event async for event in agent.run_turn("run echo")]

    assert len(provider.calls) == 2
    second_request = provider.calls[1]
    assert not any(
        "Current user request" in _message_text(message) for message in second_request
    )


@pytest.mark.asyncio
async def test_drained_pending_input_is_not_injected_again_at_later_tool_boundaries() -> None:
    provider = _ToolBoundaryProvider(tool_iterations=2)
    pending = ListPendingInputProvider()
    pending.append("ONCE")
    agent = _agent(provider, max_iterations=3)

    _events = [event async for event in agent.run_turn("run echo", pending_input_provider=pending)]

    assert len(provider.calls) == 3
    assert len(pending) == 0
    assert len(_text_messages(agent._history, ["ONCE"])) == 1


@pytest.mark.asyncio
async def test_injected_pending_input_is_persisted_to_successful_turn_history() -> None:
    provider = _ToolBoundaryProvider()
    pending = ListPendingInputProvider()
    pending.append("HISTORY")
    agent = _agent(provider)

    _events = [event async for event in agent.run_turn("run echo", pending_input_provider=pending)]

    assert _text_message_index(agent._history, ["HISTORY"]) == (
        _tool_result_index(agent._history) + 1
    )


@pytest.mark.asyncio
async def test_pending_input_is_not_drained_without_a_tool_completion_boundary() -> None:
    provider = _NoToolProvider()
    pending = ListPendingInputProvider()
    pending.append("NO_BOUNDARY")
    agent = _agent(provider)

    _events = [
        event async for event in agent.run_turn("just answer", pending_input_provider=pending)
    ]

    assert len(provider.calls) == 1
    assert len(pending) == 1
    assert not _text_messages(agent._history, ["NO_BOUNDARY"])


class _DeferredUserInputProvider(_ToolBoundaryProvider):
    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(
                tool_use_id="request-1",
                tool_name="request_user_input",
            )
            yield ProviderToolUseEnd(
                tool_use_id="request-1",
                tool_name="request_user_input",
                arguments={"questions": [{"id": "scope"}]},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="plan complete")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


class _ControlBoundaryProvider(_ToolBoundaryProvider):
    def __init__(self, tool_calls: list[tuple[str, str, dict[str, Any]]]) -> None:
        super().__init__()
        self._tool_calls = tool_calls

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            for tool_use_id, tool_name, arguments in self._tool_calls:
                yield ProviderToolUseStart(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                )
                yield ProviderToolUseEnd(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="continued")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


@pytest.mark.asyncio
async def test_terminal_control_tool_prevents_later_batch_calls_from_dispatching() -> None:
    provider = _ControlBoundaryProvider(
        [
            (
                "checkpoint-1",
                "plan_run_checkpoint",
                {"step_id": "verify", "step_status": "blocked"},
            ),
            ("write-1", "write_file", {"path": "must-not-run"}),
        ]
    )
    dispatched: list[str] = []

    async def _handler(call: ToolCall) -> ToolResult:
        dispatched.append(call.tool_name)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=json.dumps({"status": "blocked"}),
            terminates_turn=call.tool_name == "plan_run_checkpoint",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[
            _tool_def("plan_run_checkpoint"),
            _tool_def("write_file"),
        ],
        tool_handler=_handler,
    )

    events = [event async for event in agent.run_turn("implement the plan")]

    assert dispatched == ["plan_run_checkpoint"]
    assert len(provider.calls) == 1
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert [event.tool_use_id for event in results] == ["checkpoint-1", "write-1"]
    assert json.loads(results[1].result) == {
        "status": "not_executed",
        "reason": "prior_tool_dispatch_boundary",
        "boundary_tool": "plan_run_checkpoint",
        "boundary_tool_use_id": "checkpoint-1",
    }
    assert results[1].is_error is True


@pytest.mark.asyncio
async def test_deferred_user_input_resumes_same_tool_call_without_user_injection() -> None:
    provider = _DeferredUserInputProvider()
    broker = StructuredUserInputBroker()

    async def _request_user_input(call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=json.dumps(
                {
                    "status": "input_required",
                    "kind": "user_input",
                    "paused": True,
                    "clarify_schema": {
                        "fields": [
                            {
                                "name": "scope",
                                "type": "enum",
                                "required": True,
                                "choices": ["Core", "Full"],
                            }
                        ]
                    },
                }
            ),
            terminates_turn=True,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[_tool_def("request_user_input")],
        tool_handler=_request_user_input,
        session_key="agent:main:webchat:deferred-input",
        tool_context=ToolContext(
            session_key="agent:main:webchat:deferred-input",
            task_id="task-1",
            user_input_provider=broker,
        ),
    )

    stream = agent.run_turn("make a plan")
    events = []
    async for event in stream:
        events.append(event)
        if isinstance(event, ToolResultEvent):
            payload = json.loads(event.result)
            if payload.get("status") == "input_required":
                assert len(provider.calls) == 1
                assert payload["request_id"]
                broker.resolve(
                    session_key="agent:main:webchat:deferred-input",
                    request_id=payload["request_id"],
                    fields={"scope": "Core"},
                )

    tool_results = [
        event for event in events if isinstance(event, ToolResultEvent)
    ]
    assert [event.tool_use_id for event in tool_results] == [
        "request-1",
        "request-1",
    ]
    assert json.loads(tool_results[0].result)["status"] == "input_required"
    assert json.loads(tool_results[1].result) == {
        "status": "answered",
        "kind": "user_input",
        "paused": False,
        "request_id": json.loads(tool_results[0].result)["request_id"],
        "answers": {"scope": "Core"},
    }
    assert len(provider.calls) == 2
    second_request = provider.calls[1]
    result_message = next(
        message for message in second_request if _is_tool_result_message(message)
    )
    result_block = next(
        block
        for block in result_message.content
        if getattr(block, "type", None) == "tool_result"
    )
    assert json.loads(result_block.content)["answers"] == {"scope": "Core"}
    assert not any(
        message.role == "user" and _text_block_texts(message) == ["Core"]
        for message in second_request
    )


@pytest.mark.asyncio
async def test_deferred_user_input_defers_later_batch_calls_until_after_answer() -> None:
    provider = _ControlBoundaryProvider(
        [
            (
                "request-1",
                "request_user_input",
                {"questions": [{"id": "scope"}]},
            ),
            ("write-1", "write_file", {"path": "must-not-run-before-answer"}),
        ]
    )
    broker = StructuredUserInputBroker()
    dispatched: list[str] = []

    async def _handler(call: ToolCall) -> ToolResult:
        dispatched.append(call.tool_name)
        if call.tool_name != "request_user_input":
            raise AssertionError("tail tool crossed the user-input dispatch boundary")
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=json.dumps(
                {
                    "status": "input_required",
                    "kind": "user_input",
                    "paused": True,
                    "clarify_schema": {
                        "fields": [
                            {
                                "name": "scope",
                                "type": "enum",
                                "required": True,
                                "choices": ["Core", "Full"],
                            }
                        ]
                    },
                }
            ),
            terminates_turn=True,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[
            _tool_def("request_user_input"),
            _tool_def("write_file"),
        ],
        tool_handler=_handler,
        session_key="agent:main:webchat:deferred-boundary",
        tool_context=ToolContext(
            session_key="agent:main:webchat:deferred-boundary",
            task_id="task-boundary",
            user_input_provider=broker,
        ),
    )

    events = []
    async for event in agent.run_turn("make a plan"):
        events.append(event)
        if isinstance(event, ToolResultEvent):
            payload = json.loads(event.result)
            if payload.get("status") == "input_required":
                broker.resolve(
                    session_key="agent:main:webchat:deferred-boundary",
                    request_id=payload["request_id"],
                    fields={"scope": "Core"},
                )

    assert dispatched == ["request_user_input"]
    assert len(provider.calls) == 2
    results = [event for event in events if isinstance(event, ToolResultEvent)]
    assert [event.tool_use_id for event in results] == [
        "request-1",
        "request-1",
        "write-1",
    ]
    assert json.loads(results[2].result)["status"] == "not_executed"
    second_result_message = next(
        message for message in provider.calls[1] if _is_tool_result_message(message)
    )
    second_results = {
        block.tool_use_id: json.loads(block.content)
        for block in second_result_message.content
        if getattr(block, "type", None) == "tool_result"
    }
    assert second_results["request-1"]["answers"] == {"scope": "Core"}
    assert second_results["write-1"]["status"] == "not_executed"
