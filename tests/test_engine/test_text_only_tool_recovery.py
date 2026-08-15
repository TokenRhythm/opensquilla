from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

import pytest

import opensquilla.engine.action_completion as action_completion
from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.action_completion import (
    ACTION_COMPLETION_TOOL_NAME,
    resolve_tool_completion_effect,
    tool_result_confirms_success,
)
from opensquilla.execution_status import runtime_execution_status
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
                completion_effect="read_only",
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
    completion_effect_resolver: (
        Literal["exec_command", "process", "http_request", "cron", "subagents"]
        | None
    ) = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} test tool.",
        input_schema=ToolInputSchema(properties={}, required=[]),
        completion_effect=completion_effect,
        completion_effect_resolver=completion_effect_resolver,
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


def test_mixed_tool_effect_resolvers_classify_each_call() -> None:
    exec_definition = _tool_definition(
        "exec_command",
        completion_effect="unknown",
        completion_effect_resolver="exec_command",
    )
    process_definition = _tool_definition(
        "process",
        completion_effect="unknown",
        completion_effect_resolver="process",
    )
    http_definition = _tool_definition(
        "http_request",
        completion_effect="action",
        completion_effect_resolver="http_request",
    )
    cron_definition = _tool_definition(
        "cron",
        completion_effect="action",
        completion_effect_resolver="cron",
    )
    subagents_definition = _tool_definition(
        "subagents",
        completion_effect="action",
        completion_effect_resolver="subagents",
    )

    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "git status --short && rg -n TODO src"},
    ) == "read_only"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "printf done > result.txt"},
    ) == "action"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "rg --pre 'touch marker' TODO src"},
    ) == "action"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "/tmp/git status --short"},
    ) == "action"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "/tmp/cat README.md"},
    ) == "action"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "./git status --short"},
    ) == "action"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "/usr/bin/git status --short"},
    ) == "read_only"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "git status", "env": {"PATH": "/tmp/shadow-bin"}},
    ) == "action"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "sh -c 'cat README.md; touch marker'"},
    ) == "action"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "sh -c 'cat README.md'"},
    ) == "read_only"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "cat <(touch marker)"},
    ) == "action"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "date -s 2030-01-01"},
    ) == "action"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "date --utc +%FT%TZ"},
    ) == "read_only"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "rg 'left > right' src"},
    ) == "read_only"
    assert resolve_tool_completion_effect(
        exec_definition,
        {"command": "rg left src > matches.txt"},
    ) == "action"
    assert resolve_tool_completion_effect(
        process_definition,
        {"action": "log", "session_id": "p1"},
    ) == "read_only"
    assert resolve_tool_completion_effect(
        process_definition,
        {"action": "write", "session_id": "p1", "data": "yes"},
    ) == "action"
    assert resolve_tool_completion_effect(
        http_definition,
        {"method": "GET", "url": "https://example.test"},
    ) == "read_only"
    assert resolve_tool_completion_effect(
        http_definition,
        {"method": "GET", "url": "https://example.test", "output_path": "out"},
    ) == "action"
    assert resolve_tool_completion_effect(cron_definition, {"action": "list"}) == "read_only"
    assert resolve_tool_completion_effect(cron_definition, {"action": "run"}) == "action"
    assert resolve_tool_completion_effect(
        subagents_definition,
        {"action": "list"},
    ) == "read_only"
    assert resolve_tool_completion_effect(
        subagents_definition,
        {"action": "steer"},
    ) == "action"


def test_nested_shell_payload_quotes_are_platform_independent(monkeypatch: Any) -> None:
    definition = _tool_definition(
        "exec_command",
        completion_effect="unknown",
        completion_effect_resolver="exec_command",
    )

    for platform_name in ("posix", "nt"):
        monkeypatch.setattr(action_completion.os, "name", platform_name)
        assert resolve_tool_completion_effect(
            definition,
            {"command": "sh -c 'cat README.md'"},
        ) == "read_only"
        assert resolve_tool_completion_effect(
            definition,
            {"command": "sh -c 'cat README.md; touch marker'"},
        ) == "action"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("file README.md", "read_only"),
        ("file --mime-type README.md", "read_only"),
        ("file -C -m custom.magic", "action"),
        ("file --compile --magic-file custom.magic", "action"),
        ("file --unknown-mode README.md", "action"),
        ("arch", "read_only"),
        ("arch --version", "read_only"),
        ("arch touch marker", "action"),
        ("cat -n README.md", "read_only"),
        ("cat --unknown-mode README.md", "action"),
        ("grep -R TODO src", "read_only"),
        ("grep -Rn TODO src", "read_only"),
        ("grep --unknown-mode TODO src", "action"),
        ("rg -n TODO src", "read_only"),
        ("rg -z TODO archive.gz", "action"),
        ("rg --unknown-mode TODO src", "action"),
        ("tree -L 2 src", "read_only"),
        ("tree -o report.txt src", "action"),
        ("head -20 README.md", "read_only"),
        ("echo left -x", "read_only"),
        ("printf '%s' -x", "read_only"),
    ],
)
def test_exec_simple_command_option_grammars_fail_closed(
    command: str,
    expected: Literal["read_only", "action"],
) -> None:
    definition = _tool_definition(
        "exec_command",
        completion_effect="unknown",
        completion_effect_resolver="exec_command",
    )

    assert resolve_tool_completion_effect(definition, {"command": command}) == expected


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
            execution_status=runtime_execution_status("success", reason=None),
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
            execution_status=runtime_execution_status("success", reason=None),
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
            execution_status=runtime_execution_status("success", reason=None),
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


@pytest.mark.asyncio
async def test_failed_action_result_does_not_arm_completion_contract() -> None:
    provider = _SequenceProvider(
        [
            _tool_call_stream("action-1", "write_file"),
            _text_stream("The write failed; no file was changed."),
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="permission denied",
            is_error=True,
            execution_status=runtime_execution_status("error", reason="denied"),
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[_tool_definition("write_file", completion_effect="action")],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("write the file")]

    assert len(provider.calls) == 2
    assert any(event.kind == "done" for event in events)
    assert not any(event.kind == "warning" for event in events)
    assert all(
        tool.name != ACTION_COMPLETION_TOOL_NAME
        for tool in provider.calls[1]["tools"]
    )
    assert agent.config.metadata.get("action_completion_contracts_armed", 0) == 0


@pytest.mark.asyncio
async def test_unexecuted_unknown_result_does_not_arm_completion_contract() -> None:
    provider = _SequenceProvider(
        [
            _tool_call_stream("unknown-1", "dynamic_tool"),
            _text_stream("Approval is still required."),
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="approval pending",
            execution_status=runtime_execution_status(
                "unknown",
                reason="approval_pending",
            ),
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[_tool_definition("dynamic_tool", completion_effect="unknown")],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("run the dynamic action")]

    assert len(provider.calls) == 2
    assert any(event.kind == "done" for event in events)
    assert not any(event.kind == "warning" for event in events)
    assert agent.config.metadata.get("action_completion_contracts_armed", 0) == 0


@pytest.mark.asyncio
async def test_unknown_tool_without_success_sidecar_cannot_arm_completion() -> None:
    provider = _SequenceProvider(
        [
            _tool_call_stream("unknown-1", "dynamic_tool"),
            _text_stream("The dynamic operation completed."),
            _text_stream("The dynamic operation completed."),
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=4),
        tool_definitions=[_tool_definition("dynamic_tool", completion_effect="unknown")],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("run the dynamic action")]

    assert len(provider.calls) == 2
    assert any(event.kind == "done" for event in events)
    assert not any(event.kind == "warning" for event in events)
    assert agent.config.metadata.get("action_completion_contracts_armed", 0) == 0


@pytest.mark.asyncio
async def test_dynamic_tool_with_trusted_success_sidecar_fails_closed_as_action() -> None:
    provider = _SequenceProvider(
        [
            _tool_call_stream("unknown-1", "dynamic_tool"),
            _text_stream("The dynamic operation completed."),
            _text_stream("The dynamic operation completed."),
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
            execution_status=runtime_execution_status("success", reason=None),
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=4),
        tool_definitions=[_tool_definition("dynamic_tool", completion_effect="unknown")],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("run the dynamic action")]

    assert len(provider.calls) == 3
    assert sum(event.kind == "warning" for event in events) == 1
    assert any(
        event.kind == "error" and event.code == "action_completion_incomplete"
        for event in events
    )
    assert agent.config.metadata["action_completion_contracts_armed"] == 1


def test_soft_success_without_execution_sidecar_is_not_a_receipt() -> None:
    result = ToolResult(
        tool_use_id="media-1",
        tool_name="tts",
        content='{"status":"not_available"}',
        is_error=False,
    )

    assert tool_result_confirms_success(result) is False

    untrusted = ToolResult(
        tool_use_id="plugin-1",
        tool_name="dynamic_tool",
        content="ok",
        execution_status={
            "version": 1,
            "status": "success",
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "reason": None,
            "source": "unknown",
            "preservation_class": "normal",
        },
    )
    assert tool_result_confirms_success(untrusted) is False


@pytest.mark.asyncio
async def test_read_only_exec_command_call_does_not_arm_completion_contract() -> None:
    provider = _SequenceProvider(
        [
            _tool_call_stream(
                "read-1",
                "exec_command",
                arguments={"command": "git status --short && rg -n TODO src"},
            ),
            _text_stream("There are no pending changes."),
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="exit_code=0",
            execution_status=runtime_execution_status("success", reason=None),
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[
            _tool_definition(
                "exec_command",
                completion_effect="unknown",
                completion_effect_resolver="exec_command",
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("inspect the repository")]

    assert len(provider.calls) == 2
    assert any(event.kind == "done" for event in events)
    assert not any(event.kind == "warning" for event in events)
    assert agent.config.metadata.get("action_completion_contracts_armed", 0) == 0


@pytest.mark.asyncio
async def test_read_only_process_poll_does_not_arm_completion_contract() -> None:
    provider = _SequenceProvider(
        [
            _tool_call_stream(
                "read-1",
                "process",
                arguments={"action": "poll", "session_id": "p1"},
            ),
            _text_stream("The process is still running."),
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="running",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=3),
        tool_definitions=[
            _tool_definition(
                "process",
                completion_effect="unknown",
                completion_effect_resolver="process",
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("check the process")]

    assert len(provider.calls) == 2
    assert any(event.kind == "done" for event in events)
    assert not any(event.kind == "warning" for event in events)
    assert agent.config.metadata.get("action_completion_contracts_armed", 0) == 0
