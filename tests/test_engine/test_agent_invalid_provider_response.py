from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import structlog.testing

from opensquilla.engine import Agent, AgentConfig, ThinkingLevel, ToolResult
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.usage import UsageTracker
from opensquilla.provider import (
    ChatConfig,
    Message,
    ModelCapabilities,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseDeltaEvent as ProviderToolUseDelta
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolContext, ToolSpec


class _SequenceProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        index = len(self.calls)
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


class _FallbackSequenceProvider(_SequenceProvider):
    def __init__(self, streams: list[list[Any]]) -> None:
        super().__init__(streams)
        self.fallback_reasons: list[str] = []

    def fallback_after_invalid_response(self, reason: str) -> bool:
        self.fallback_reasons.append(reason)
        return True


def _large_reasoning_only_done() -> ProviderDone:
    return ProviderDone(
        stop_reason="stop",
        input_tokens=35_000,
        output_tokens=2,
        reasoning_tokens=2,
        reasoning_content="internal",
    )


class _SelectorClone:
    def __init__(self, provider: _SequenceProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model="fake-model")

    def resolve(self) -> _SequenceProvider:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config.model = model

    def next_fallback_after_failure(self, primary_failure: Exception) -> _SequenceProvider:
        raise IndexError("No fallback configured")


class _ProviderSelector:
    def __init__(self, provider: _SequenceProvider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


class _CacheReport:
    break_detected = False

    def to_log_dict(self) -> dict[str, Any]:
        return {}


@pytest.mark.asyncio
async def test_final_done_returns_openrouter_deepseek_reasoning_content() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="ok"),
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=1,
                    reasoning_tokens=4,
                    reasoning_content="I reasoned through the OpenRouter response.",
                    model="deepseek/deepseek-v4-flash",
                ),
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.HIGH,
            model_id="deepseek/deepseek-v4-flash",
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="openrouter",
            ),
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    done = next(event for event in events if event.kind == "done")
    assert done.text == "ok"
    assert done.reasoning_content == "I reasoned through the OpenRouter response."


@pytest.mark.asyncio
async def test_reasoning_only_first_turn_retries_without_disabling_thinking() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=5,
                    reasoning_tokens=5,
                    reasoning_content="internal reasoning",
                    model="z-ai/glm-5.1",
                )
            ],
            [
                ProviderText(text="ok"),
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=11,
                    output_tokens=1,
                    model="z-ai/glm-5.1",
                ),
            ],
        ]
    )
    usage = UsageTracker()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        usage_tracker=usage,
        session_key="agent:test:reasoning-only",
    )

    events = [event async for event in agent.run_turn("hello")]

    assert [event.kind for event in events if event.kind == "error"] == []
    assert any(
        event.kind == "warning" and event.code == "provider_reasoning_only_retry"
        for event in events
    )
    warning = next(
        event
        for event in events
        if event.kind == "warning" and event.code == "provider_reasoning_only_retry"
    )
    assert "thinking disabled" not in warning.message
    done = next(event for event in events if event.kind == "done")
    assert done.text == "ok"
    assert done.input_tokens == 21
    assert done.output_tokens == 6
    assert done.reasoning_tokens == 5
    assert len(provider.calls) == 2
    assert provider.calls[0]["config"].thinking is True
    assert provider.calls[1]["config"].thinking is True
    assert provider.calls[1]["config"].thinking_level == ThinkingLevel.MEDIUM
    assert provider.calls[1]["config"].thinking_budget_tokens > 0
    tracked = usage.get("agent:test:reasoning-only")
    assert tracked is not None
    assert tracked.input_tokens == 21
    assert tracked.output_tokens == 6
    assistant_messages = [msg for msg in agent._history if msg.role == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content[0].text == "ok"
    assert assistant_messages[0].reasoning_content is None


@pytest.mark.asyncio
async def test_reasoning_only_prefill_recovery_cleans_synthetic_history(tmp_path) -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=5,
                    reasoning_tokens=5,
                    reasoning_content="internal reasoning",
                    model="openrouter/test-reasoning",
                )
            ],
            [
                ProviderText(text="ok"),
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=11,
                    output_tokens=1,
                    model="openrouter/test-reasoning",
                ),
            ],
        ]
    )
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="openrouter",
            ),
            reasoning_prefill_recovery_mode="recover",
            runtime_events_path=str(runtime_events_path),
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(
        event.kind == "warning" and event.code == "provider_reasoning_prefill_continue"
        for event in events
    )
    assert not any(
        event.kind == "warning" and event.code == "provider_reasoning_only_retry"
        for event in events
    )
    assert any(event.kind == "done" and event.text == "ok" for event in events)
    assert len(provider.calls) == 2
    assert any(
        msg.role == "assistant" and msg.reasoning_content == "internal reasoning"
        for msg in provider.calls[1]["messages"]
    )
    assistant_messages = [msg for msg in agent._history if msg.role == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0].content[0].text == "ok"
    assert assistant_messages[0].reasoning_content is None
    logged = [json.loads(line) for line in runtime_events_path.read_text().splitlines()]
    recovery_event = next(
        event for event in logged if event.get("mechanism") == "reasoning_prefill_recovery"
    )
    assert recovery_event["injected_to_model"] is True


@pytest.mark.asyncio
async def test_tool_loop_observer_logs_reasoning_only_runtime_event(tmp_path) -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=5,
                    reasoning_tokens=5,
                    reasoning_content="internal reasoning",
                    model="z-ai/glm-5.1",
                )
            ],
            [
                ProviderText(text="ok"),
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=11,
                    output_tokens=1,
                    model="z-ai/glm-5.1",
                ),
            ],
        ]
    )
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            tool_loop_observer_mode="log",
            runtime_events_path=str(runtime_events_path),
        ),
        session_key="agent:test:runtime-observer",
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    logged = [json.loads(line) for line in runtime_events_path.read_text().splitlines()]
    observer_events = [
        event for event in logged if event.get("mechanism") == "tool_loop_observer"
    ]
    assert [event["reason"] for event in observer_events] == ["reasoning_only"]
    assert observer_events[0]["feature"] == "runtime_observer"
    assert observer_events[0]["injected_to_model"] is False
    assert observer_events[0]["iteration"] == 1
    assert observer_events[0]["session_key"] == "agent:test:runtime-observer"
    assert observer_events[0]["evidence"]["post_tool_turn"] is False
    assert observer_events[0]["details"]["post_tool_turn"] is False
    assert observer_events[0]["details"]["reasoning_tokens"] == 5
    assert observer_events[0]["read_files"] == []
    assert observer_events[0]["changed_files"] == []
    assert observer_events[0]["diff_paths"] == []
    assert observer_events[0]["verification_commands"] == []
    assert observer_events[0]["hint_text_sha256"] is None
    assert observer_events[0]["trigger_confidence"] == "observed_runtime_signal"
    assert isinstance(observer_events[0]["created_at"], str)


@pytest.mark.asyncio
async def test_post_tool_empty_recovery_nudges_once_and_cleans_history(tmp_path) -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "ok"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
            ],
            [ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=0)],
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
            post_tool_empty_recovery_mode="warn_model",
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
        event.kind == "warning" and event.code == "post_tool_empty_recovery"
        for event in events
    )
    assert len(provider.calls) == 3
    assert any(
        msg.role == "user"
        and isinstance(msg.content, str)
        and msg.content.startswith("[Runtime recovery]")
        for msg in provider.calls[2]["messages"]
    )
    assert not any(
        msg.role == "user"
        and isinstance(msg.content, str)
        and msg.content.startswith("[Runtime recovery]")
        for msg in agent._history
    )
    assert not any(
        msg.role == "assistant"
        and isinstance(msg.content, list)
        and len(msg.content) == 1
        and getattr(msg.content[0], "type", None) == "text"
        and not msg.content[0].text
        for msg in agent._history
    )
    logged = [json.loads(line) for line in runtime_events_path.read_text().splitlines()]
    recovery_event = next(
        event for event in logged if event.get("mechanism") == "post_tool_empty_recovery"
    )
    assert recovery_event["injected_to_model"] is True


def test_tool_loop_observer_diff_paths_preserve_status_path_prefix(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    src = tmp_path / "src"
    src.mkdir()
    file_path = src / "foo.py"
    file_path.write_text("print('old')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    file_path.write_text("print('new')\n", encoding="utf-8")
    agent = Agent(
        provider=_SequenceProvider([[ProviderText(text="ok"), ProviderDone(stop_reason="stop")]]),
        tool_context=ToolContext(workspace_dir=str(tmp_path)),
    )

    assert agent._workspace_diff_paths_for_runtime_event() == ["src/foo.py"]


@pytest.mark.asyncio
async def test_final_diff_contract_warn_model_reaches_next_provider_request(
    tmp_path,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    scratch = tmp_path / "debug_case.php"
    scratch.write_text("<?php echo 'scratch';\n", encoding="utf-8")

    provider = _SequenceProvider(
        [
            [
                ProviderText(text="ready"),
                ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=1),
            ],
            [
                ProviderText(text="final"),
                ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=1),
            ],
        ]
    )
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            final_diff_contract_mode="warn_model",
            runtime_events_path=str(runtime_events_path),
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        session_key="agent:test:final-diff-proof",
        tool_context=ToolContext(workspace_dir=str(tmp_path)),
    )

    events = [event async for event in agent.run_turn("finish")]

    assert any(event.kind == "done" and event.text == "final" for event in events)
    assert any(
        event.kind == "warning" and event.code == "final_diff_contract_recovery"
        for event in events
    )
    assert len(provider.calls) == 2
    warning_messages = [
        msg
        for msg in provider.calls[1]["messages"]
        if msg.role == "user"
        and isinstance(msg.content, str)
        and msg.content.startswith("[Runtime final-diff check]")
    ]
    assert len(warning_messages) == 1
    assert "debug_case.php" in warning_messages[0].content
    assert "repository diff looks suspicious" in warning_messages[0].content

    logged = [json.loads(line) for line in runtime_events_path.read_text().splitlines()]
    contract_event = next(
        event for event in logged if event.get("feature") == "final_diff_contract"
    )
    assert contract_event["injected_to_model"] is True
    assert contract_event["diff_paths"] == ["debug_case.php"]


@pytest.mark.asyncio
async def test_missing_required_shape_guidance_reaches_next_provider_request() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="write_file"),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="write_file",
                    arguments={"content": "print('ok')\n"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
            ],
            [
                ProviderText(text="fixed"),
                ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=1),
            ],
        ]
    )
    registry = ToolRegistry()

    async def write_file(path: str, content: str) -> str:
        raise AssertionError("missing required argument preflight should stop dispatch")

    registry.register(
        ToolSpec(
            name="write_file",
            description="Write a file.",
            parameters={
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            required=["path", "content"],
        ),
        write_file,
    )
    tool_context = ToolContext(missing_required_argument_shape_guidance=True)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="write_file",
                description="Write a file.",
                input_schema=ToolInputSchema(
                    properties={
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    required=["path", "content"],
                ),
            )
        ],
        tool_handler=build_tool_handler(registry, tool_context),
        tool_context=tool_context,
    )

    events = [event async for event in agent.run_turn("write the file")]

    assert any(event.kind == "done" and event.text == "fixed" for event in events)
    assert len(provider.calls) == 2
    tool_result_contents = [
        block.content
        for msg in provider.calls[1]["messages"]
        if msg.role == "user" and isinstance(msg.content, list)
        for block in msg.content
        if getattr(block, "tool_use_id", "") == "tool-1"
    ]
    assert len(tool_result_contents) == 1
    tool_result_payload = json.loads(tool_result_contents[0])
    assert tool_result_payload["error_class"] == "InvalidToolArgumentsError"
    assert "You supplied argument(s): `content`." in tool_result_payload["user_message"]
    assert "Missing argument(s): `path`." in tool_result_payload["user_message"]
    assert 'Valid write_file shape: {"path":"...","content":"..."}' in tool_result_payload[
        "user_message"
    ]


@pytest.mark.asyncio
async def test_reasoning_only_post_tool_turn_retries_without_disabling_thinking() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "ok"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
            ],
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=4,
                    output_tokens=2,
                    reasoning_tokens=2,
                    reasoning_content="internal reasoning",
                )
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

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            max_iterations=2,
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
        event.kind == "warning" and event.code == "provider_reasoning_only_retry"
        for event in events
    )
    warning = next(
        event
        for event in events
        if event.kind == "warning" and event.code == "provider_reasoning_only_retry"
    )
    assert "thinking disabled" not in warning.message
    assert len(provider.calls) == 3
    assert provider.calls[1]["config"].thinking is True
    assert provider.calls[2]["config"].thinking is True
    assert provider.calls[2]["config"].thinking_level == ThinkingLevel.MEDIUM


@pytest.mark.asyncio
async def test_reasoning_only_retry_restores_thinking_after_retry_call() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "first"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
            ],
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=35_000,
                    output_tokens=2,
                    reasoning_tokens=2,
                    reasoning_content="internal reasoning",
                )
            ],
            [
                ProviderToolUseStart(tool_use_id="tool-2", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="tool-2",
                    tool_name="echo",
                    arguments={"value": "second"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=5, output_tokens=1),
            ],
            [
                ProviderText(text="done"),
                ProviderDone(stop_reason="stop", input_tokens=6, output_tokens=1),
            ],
        ]
    )

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=f"tool ok: {call.arguments['value']}",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            max_iterations=3,
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
    assert len(provider.calls) == 4
    assert provider.calls[0]["config"].thinking is True
    assert provider.calls[1]["config"].thinking is True
    assert provider.calls[2]["config"].thinking is False
    assert provider.calls[3]["config"].thinking is True


@pytest.mark.asyncio
async def test_reasoning_only_with_thinking_disabled_surfaces_empty_response() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=4,
                    output_tokens=2,
                    reasoning_tokens=2,
                    reasoning_content="internal reasoning",
                )
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(thinking=False, retry_base_backoff_ms=0, retry_max_backoff_ms=0),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)
    done = next(event for event in events if event.kind == "done")
    assert done.input_tokens == 4
    assert done.output_tokens == 2
    assert done.reasoning_tokens == 2


@pytest.mark.asyncio
async def test_clean_empty_done_retries_once_then_errors() -> None:
    provider = _SequenceProvider(
        [
            [ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=0)],
            [ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=0)],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "warning" and event.code == "provider_empty_retry" for event in events)
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)
    done = next(event for event in events if event.kind == "done")
    assert done.input_tokens == 7
    assert done.output_tokens == 0


@pytest.mark.asyncio
async def test_clean_empty_done_can_switch_to_selector_fallback() -> None:
    provider = _FallbackSequenceProvider(
        [
            [ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=0)],
            [
                ProviderText(text="ok"),
                ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=1),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert provider.fallback_reasons == ["malformed_empty"]
    assert len(provider.calls) == 2
    assert any(event.kind == "done" and event.text == "ok" for event in events)
    assert not any(event.kind == "error" for event in events)


@pytest.mark.asyncio
async def test_large_reasoning_only_uses_fallback_before_same_model_retry() -> None:
    provider = _FallbackSequenceProvider(
        [
            [_large_reasoning_only_done()],
            [
                ProviderText(text="ok"),
                ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=1),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert provider.fallback_reasons == ["reasoning_only"]
    assert len(provider.calls) == 2
    assert not any(
        event.kind == "warning" and event.code == "provider_reasoning_only_retry"
        for event in events
    )
    assert any(
        event.kind == "warning" and event.code == "provider_large_context_fallback"
        for event in events
    )
    assert any(event.kind == "done" and event.text == "ok" for event in events)


@pytest.mark.asyncio
async def test_large_reasoning_only_without_fallback_retries_once_with_thinking_disabled() -> None:
    provider = _SequenceProvider(
        [
            [_large_reasoning_only_done()],
            [
                ProviderText(text="ok"),
                ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=1),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert provider.calls[0]["config"].thinking is True
    assert provider.calls[1]["config"].thinking is False
    assert provider.calls[1]["config"].thinking_level is None
    assert provider.calls[1]["config"].thinking_budget_tokens == 0
    assert any(
        event.kind == "warning" and event.code == "provider_large_context_visible_retry"
        for event in events
    )
    assert not any(
        event.kind == "warning" and event.code == "provider_reasoning_only_retry"
        for event in events
    )
    assert not any(event.kind == "error" for event in events)
    done = next(event for event in events if event.kind == "done")
    assert done.text == "ok"
    assert done.input_tokens == 35_004
    assert done.output_tokens == 3
    assert done.reasoning_tokens == 2


@pytest.mark.asyncio
async def test_large_dashscope_reasoning_only_nudges_before_hard_fail(tmp_path) -> None:
    provider = _SequenceProvider(
        [
            [_large_reasoning_only_done()],
            [
                ProviderText(text="ok"),
                ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=1),
            ],
        ]
    )
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="dashscope",
            ),
            reasoning_prefill_recovery_mode="recover",
            runtime_events_path=str(runtime_events_path),
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(
        event.kind == "warning" and event.code == "provider_reasoning_continuation"
        for event in events
    )
    assert not any(
        event.kind == "warning" and event.code == "provider_reasoning_prefill_continue"
        for event in events
    )
    assert not any(event.kind == "error" for event in events)
    assert any(event.kind == "done" and event.text == "ok" for event in events)
    assert not any(
        msg.reasoning_content == "internal" for msg in provider.calls[1]["messages"]
    )
    assert any(
        msg.role == "user"
        and isinstance(msg.content, str)
        and "Continue now with the next concrete tool call" in msg.content
        for msg in provider.calls[1]["messages"]
    )
    logged = [json.loads(line) for line in runtime_events_path.read_text().splitlines()]
    recovery_event = next(
        event for event in logged if event.get("mechanism") == "reasoning_continuation_recovery"
    )
    assert recovery_event["injected_to_model"] is True
    assert recovery_event["details"]["provider_reasoning_format"] == "dashscope"


@pytest.mark.asyncio
async def test_repeated_large_dashscope_reasoning_only_disables_thinking_after_nudge() -> None:
    provider = _SequenceProvider(
        [
            [_large_reasoning_only_done()],
            [_large_reasoning_only_done()],
            [
                ProviderText(text="ok"),
                ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=1),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="dashscope",
            ),
            reasoning_prefill_recovery_mode="recover",
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 3
    assert any(
        event.kind == "warning" and event.code == "provider_reasoning_continuation"
        for event in events
    )
    assert any(
        event.kind == "warning" and event.code == "provider_large_context_visible_retry"
        for event in events
    )
    assert not any(event.kind == "error" for event in events)
    assert any(event.kind == "done" and event.text == "ok" for event in events)
    assert provider.calls[2]["config"].thinking is False


@pytest.mark.asyncio
async def test_ensemble_usage_total_does_not_misclassify_small_final_aggregator_as_large_input(
) -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderDone(
                    stop_reason="length",
                    input_tokens=92_213,
                    output_tokens=12_209,
                    reasoning_tokens=10_580,
                    reasoning_content="internal reasoning only",
                    model_usage_breakdown=[
                        {"role": "proposer", "input_tokens": 17_700},
                        {"role": "proposer", "input_tokens": 14_378},
                        {"role": "proposer", "input_tokens": 14_380},
                        {"role": "proposer", "input_tokens": 14_379},
                        {
                            "role": "aggregator",
                            "input_tokens": 26_399,
                            "output_tokens": 12_209,
                            "reasoning_tokens": 10_580,
                        },
                    ],
                )
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    error = next(event for event in events if event.kind == "error")
    assert error.code == "empty_response"
    assert "large input" not in error.message
    assert len(provider.calls) == 1



@pytest.mark.asyncio
async def test_large_empty_response_without_fallback_surfaces_clear_error() -> None:
    provider = _SequenceProvider(
        [[ProviderDone(stop_reason="stop", input_tokens=35_000, output_tokens=0)]]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    error = next(event for event in events if event.kind == "error")
    assert error.code == "empty_response"
    assert "large input" in error.message
    assert "attachment" in error.message
    assert "summarize" in error.message or "shorten" in error.message
    assert "stronger model" in error.message
    assert not any(
        event.kind == "warning" and event.code == "provider_empty_retry"
        for event in events
    )


@pytest.mark.asyncio
async def test_incomplete_tool_stream_errors_without_running_tool() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderDone(stop_reason="tool_use", input_tokens=5, output_tokens=1),
            ]
        ]
    )
    called = False

    async def tool_handler(call: Any) -> ToolResult:
        nonlocal called
        called = True
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="tool ok")

    agent = Agent(
        provider=provider,
        config=AgentConfig(retry_base_backoff_ms=0, retry_max_backoff_ms=0),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert called is False
    assert any(event.kind == "tool_use_start" for event in events)
    assert any(event.kind == "error" and event.code == "incomplete_tool_stream" for event in events)
    done = next(event for event in events if event.kind == "done")
    assert done.input_tokens == 5
    assert done.output_tokens == 1
    assert agent._history == []


@pytest.mark.asyncio
async def test_turn_runner_drops_unpaired_tool_use_from_incomplete_stream_transcript() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:incomplete-tool-stream"
    await manager.create(session_key)
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderDone(stop_reason="tool_use", input_tokens=5, output_tokens=1),
            ]
        ]
    )
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        session_manager=manager,
    )

    try:
        events = [
            event
            async for event in runner.run(
                "hello",
                session_key,
                ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
                history_has_persisted_user=False,
                no_memory_capture=True,
            )
        ]
        transcript = await manager.get_transcript(session_key)
    finally:
        await storage.close()

    assert any(event.kind == "error" and event.code == "incomplete_tool_stream" for event in events)
    assert all(entry.role != "assistant" for entry in transcript)
    assert any(
        entry.role == "system"
        and "Provider stream ended with an incomplete tool call" in entry.content
        for entry in transcript
    )


@pytest.mark.asyncio
async def test_turn_runner_persists_no_provider_error_to_transcript() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:no-provider"
    await manager.create(session_key)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(None),  # type: ignore[arg-type]
        session_manager=manager,
    )

    try:
        events = [
            event
            async for event in runner.run(
                "hello",
                session_key,
                ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
                history_has_persisted_user=False,
                no_memory_capture=True,
            )
        ]
        transcript = await manager.get_transcript(session_key)
    finally:
        await storage.close()

    assert any(event.kind == "error" and event.code == "no_provider" for event in events)
    assert any(
        entry.role == "system" and entry.content == "Error: No provider available"
        for entry in transcript
    )


@pytest.mark.asyncio
async def test_no_done_without_visible_output_retries_once_then_errors() -> None:
    provider = _SequenceProvider([[], []])
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(
        event.kind == "error" and event.code == "provider_stream_incomplete"
        for event in events
    )
    assert not any(event.kind == "done" for event in events)


@pytest.mark.asyncio
async def test_no_done_after_text_does_not_retry() -> None:
    provider = _SequenceProvider([[ProviderText(text="partial")]])
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(event.kind == "text_delta" and event.text == "partial" for event in events)
    assert any(
        event.kind == "error" and event.code == "provider_stream_incomplete"
        for event in events
    )
    assert not any(event.kind == "done" for event in events)


@pytest.mark.asyncio
async def test_length_capped_visible_text_continues_once_before_terminal() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="partial answer"),
                ProviderDone(stop_reason="length", input_tokens=7, output_tokens=9),
            ],
            [
                ProviderText(text=" finished"),
                ProviderDone(stop_reason="stop", input_tokens=8, output_tokens=1),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "text_delta" and event.text == "partial answer" for event in events)
    assert any(event.kind == "text_delta" and event.text == " finished" for event in events)
    assert any(
        event.kind == "warning" and event.code == "provider_output_continue"
        for event in events
    )
    assert not any(event.kind == "error" for event in events)
    done = next(event for event in events if event.kind == "done")
    assert done.text == "partial answer finished"
    assert done.input_tokens == 15
    assert done.output_tokens == 10


@pytest.mark.asyncio
async def test_length_capped_visible_text_uses_configured_continuation_budget() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="part one "),
                ProviderDone(stop_reason="length", input_tokens=1, output_tokens=2),
            ],
            [
                ProviderText(text="part two "),
                ProviderDone(stop_reason="length", input_tokens=3, output_tokens=4),
            ],
            [
                ProviderText(text="part three "),
                ProviderDone(stop_reason="length", input_tokens=5, output_tokens=6),
            ],
            [
                ProviderText(text="done"),
                ProviderDone(stop_reason="stop", input_tokens=7, output_tokens=8),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            length_capped_continuations=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 4
    assert sum(
        1
        for event in events
        if event.kind == "warning" and event.code == "provider_output_continue"
    ) == 3
    assert not any(event.kind == "error" for event in events)
    done = next(event for event in events if event.kind == "done")
    assert done.text == "part one part two part three done"
    assert done.input_tokens == 16
    assert done.output_tokens == 20


@pytest.mark.asyncio
async def test_length_capped_visible_text_uses_default_three_continuation_budget() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="part one "),
                ProviderDone(stop_reason="length", input_tokens=1, output_tokens=2),
            ],
            [
                ProviderText(text="part two "),
                ProviderDone(stop_reason="length", input_tokens=3, output_tokens=4),
            ],
            [
                ProviderText(text="part three "),
                ProviderDone(stop_reason="length", input_tokens=5, output_tokens=6),
            ],
            [
                ProviderText(text="done"),
                ProviderDone(stop_reason="stop", input_tokens=7, output_tokens=8),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 4
    assert sum(
        1
        for event in events
        if event.kind == "warning" and event.code == "provider_output_continue"
    ) == 3
    assert not any(event.kind == "error" for event in events)
    done = next(event for event in events if event.kind == "done")
    assert done.text == "part one part two part three done"
    assert done.input_tokens == 16
    assert done.output_tokens == 20


@pytest.mark.asyncio
async def test_length_capped_reasoning_only_does_not_continue_empty_output() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderDone(
                    stop_reason="length",
                    input_tokens=17_000,
                    output_tokens=32_768,
                    reasoning_tokens=32_000,
                ),
            ]
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            length_capped_continuations=3,
            max_provider_retries=0,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert not any(
        event.kind == "warning" and event.code == "provider_output_continue"
        for event in events
    )
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)


@pytest.mark.asyncio
async def test_length_capped_exhaustion_records_partial_diagnostics() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="first partial "),
                ProviderDone(stop_reason="length", input_tokens=1, output_tokens=2),
            ],
            [
                ProviderText(text="second partial "),
                ProviderDone(stop_reason="length", input_tokens=3, output_tokens=4),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            length_capped_continuations=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    with structlog.testing.capture_logs() as captured:
        events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 2
    assert any(event.kind == "text_delta" and event.text == "first partial " for event in events)
    assert any(event.kind == "text_delta" and event.text == "second partial " for event in events)
    assert any(
        event.kind == "warning" and event.code == "provider_output_continue"
        for event in events
    )
    assert any(
        event.kind == "error" and event.code == "provider_output_truncated"
        for event in events
    )
    exhausted = [
        event
        for event in captured
        if event.get("event") == "provider.output_truncated_exhausted"
    ]
    assert exhausted
    assert exhausted[-1]["attempt"] == 1
    assert exhausted[-1]["budget"] == 1
    assert exhausted[-1]["visible_chars"] == len("second partial ")
    assert exhausted[-1]["partial_preserved"] is True


@pytest.mark.asyncio
async def test_length_capped_tool_call_is_not_executed() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "x"},
                ),
                ProviderDone(stop_reason="length", input_tokens=7, output_tokens=9),
            ]
        ]
    )
    called = False

    async def tool_handler(call: Any) -> ToolResult:
        nonlocal called
        called = True
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="tool ok")

    agent = Agent(
        provider=provider,
        config=AgentConfig(retry_base_backoff_ms=0, retry_max_backoff_ms=0),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert called is False
    assert any(event.kind == "tool_use_start" for event in events)
    assert any(
        event.kind == "error" and event.code == "provider_output_truncated"
        for event in events
    )
    assert not any(event.kind == "tool_result" for event in events)
    assert agent._history == []


@pytest.mark.asyncio
async def test_discarded_empty_attempt_counts_usage_but_skips_cache_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SequenceProvider(
        [
            [ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=0)],
            [
                ProviderText(text="ok"),
                ProviderDone(stop_reason="stop", input_tokens=4, output_tokens=1),
            ],
        ]
    )
    cache_checks: list[Any] = []

    def fake_cache_check(*args: Any, **kwargs: Any) -> _CacheReport:
        cache_checks.append((args, kwargs))
        return _CacheReport()

    monkeypatch.setattr("opensquilla.engine.agent.check_response_for_cache_break", fake_cache_check)
    usage = UsageTracker()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        usage_tracker=usage,
        session_key="agent:test:empty-retry",
    )

    events = [event async for event in agent.run_turn("hello")]

    done = next(event for event in events if event.kind == "done")
    assert done.input_tokens == 7
    assert done.output_tokens == 1
    tracked = usage.get("agent:test:empty-retry")
    assert tracked is not None
    assert tracked.input_tokens == 7
    assert tracked.output_tokens == 1
    assert len(cache_checks) == 1
    assert len([msg for msg in agent._history if msg.role == "assistant"]) == 1


@pytest.mark.asyncio
async def test_duplicate_tool_use_id_in_one_provider_attempt_never_executes() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="duplicate", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="duplicate",
                    tool_name="echo",
                    arguments={"value": "first"},
                ),
                ProviderToolUseStart(tool_use_id="duplicate", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="duplicate",
                    tool_name="echo",
                    arguments={"value": "second"},
                ),
                ProviderDone(stop_reason="tool_use"),
            ]
        ]
    )
    executed: list[Any] = []

    async def tool_handler(call: Any) -> ToolResult:
        executed.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="unexpected",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=0,
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

    assert executed == []
    assert not any(event.kind == "tool_result" for event in events)
    assert not any(event.kind == "done" for event in events)
    assert any(
        event.kind == "error" and event.code == "provider_protocol_error"
        for event in events
    )


@pytest.mark.parametrize(
    "stream",
    [
        [
            ProviderToolUseDelta(tool_use_id="unknown", json_fragment='{"value":"x"}'),
            ProviderDone(stop_reason="tool_use"),
        ],
        [
            ProviderToolUseStart(tool_use_id="closed", tool_name="echo"),
            ProviderToolUseEnd(
                tool_use_id="closed",
                tool_name="echo",
                arguments={"value": "first"},
            ),
            ProviderToolUseDelta(tool_use_id="closed", json_fragment='{"value":"late"}'),
            ProviderDone(stop_reason="tool_use"),
        ],
    ],
    ids=["unknown-id", "closed-id"],
)
@pytest.mark.asyncio
async def test_tool_delta_without_active_start_never_executes(stream: list[Any]) -> None:
    provider = _SequenceProvider([stream])
    executed: list[Any] = []

    async def tool_handler(call: Any) -> ToolResult:
        executed.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="unexpected",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert executed == []
    assert not any(event.kind == "tool_result" for event in events)
    assert not any(event.kind == "done" for event in events)
    assert any(
        event.kind == "error" and event.code == "provider_protocol_error"
        for event in events
    )


@pytest.mark.parametrize(
    "malformed_event",
    [
        ProviderToolUseDelta(
            tool_use_id=["unhashable"],  # type: ignore[arg-type]
            json_fragment='{"value":"x"}',
        ),
        ProviderToolUseEnd(
            tool_use_id=["unhashable"],  # type: ignore[arg-type]
            tool_name="echo",
            arguments={"value": "x"},
        ),
    ],
    ids=["delta", "end"],
)
@pytest.mark.asyncio
async def test_unhashable_tool_event_id_is_a_protocol_error(
    malformed_event: Any,
) -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="call-1", tool_name="echo"),
                malformed_event,
                ProviderDone(stop_reason="tool_use"),
            ]
        ]
    )
    executed: list[Any] = []

    async def tool_handler(call: Any) -> ToolResult:
        executed.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="unexpected",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert executed == []
    assert not any(event.kind in {"tool_result", "done"} for event in events)
    assert any(
        event.kind == "error" and event.code == "provider_protocol_error"
        for event in events
    )


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("other", {"value": "x"}),
        ("", {"value": "x"}),
        ("echo", ["not", "an", "object"]),
        ("echo", {"value": float("nan")}),
        ("echo", {"nested": {"value": float("inf")}}),
        ("echo", {"value": {"not-json"}}),
    ],
    ids=[
        "name-mismatch",
        "empty-name",
        "non-object",
        "nan",
        "nested-infinity",
        "non-json-value",
    ],
)
@pytest.mark.asyncio
async def test_tool_end_requires_matching_name_and_finite_json_object(
    tool_name: str,
    arguments: Any,
) -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(tool_use_id="call-1", tool_name="echo"),
                ProviderToolUseEnd(
                    tool_use_id="call-1",
                    tool_name=tool_name,
                    arguments=arguments,
                ),
                ProviderDone(stop_reason="tool_use"),
            ]
        ]
    )
    executed: list[Any] = []

    async def tool_handler(call: Any) -> ToolResult:
        executed.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="unexpected",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("hello")]

    assert executed == []
    assert not any(event.kind == "tool_result" for event in events)
    assert not any(event.kind == "done" for event in events)
    assert any(
        event.kind == "error" and event.code == "provider_protocol_error"
        for event in events
    )
