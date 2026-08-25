from __future__ import annotations

from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolCall, ToolResult
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDoneEvent


class _Provider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[Any]] = []

    def chat(self, messages, tools=None, config=None):
        self.calls.append(messages)
        return self._stream()

    async def _stream(self):
        yield TextDeltaEvent(text="done")
        yield ProviderDoneEvent(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


def _tool_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Mock tool {name}",
        input_schema=ToolInputSchema(properties={}, required=[]),
    )


async def _unused_tool_handler(tool_call: ToolCall) -> ToolResult:
    raise AssertionError(f"unexpected tool call: {tool_call.tool_name}")


def _declare_available_tools(handler: Any, *tool_names: str) -> Any:
    setattr(handler, "_opensquilla_available_tools", frozenset(tool_names))
    return handler


_declare_available_tools(_unused_tool_handler, "retrieve_tool_result")


def _config(tmp_path: Any, **overrides: Any) -> AgentConfig:
    values: dict[str, Any] = {
        "tool_result_store_dir": str(tmp_path / "tool-results"),
        "tool_result_store_session_id": "session-1",
        "tool_result_store_session_key": "agent:main:session-1",
        "tool_result_store_agent_id": "main",
        "tool_result_provider_request_max_chars": 4_000,
        "tool_result_history_projection_keep_recent_turns": 1,
    }
    values.update(overrides)
    return AgentConfig(**values)


def _history_messages(big_content: str, *, is_error: bool = False) -> list[Message]:
    """Two completed turns; the bulky result sits in the older exchange."""

    return [
        Message(role="user", content="older question"),
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="tool-old",
                    name="exec_command",
                    input={"command": "pytest -q"},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="tool-old",
                    content=big_content,
                    is_error=is_error,
                )
            ],
        ),
        Message(role="assistant", content="older answer"),
        Message(role="user", content="latest question"),
    ]


def _assemble(agent: Agent, messages: list[Message]) -> list[Message]:
    request_messages, _sanitize = agent._provider_request_messages_with_sanitize(
        messages,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[runtime context]"),
        runtime_context_insert_index=len(messages),
    )
    return request_messages


def _request_texts(messages: list[Any]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            texts.append(content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            block_content = getattr(block, "content", None)
            if isinstance(block_content, str):
                texts.append(block_content)
    return texts


_BIG_CONTENT = "pytest output\n" + ("x" * 50_000)


@pytest.mark.asyncio
async def test_history_waterline_projects_bulky_older_results(tmp_path: Any) -> None:
    agent = Agent(
        provider=_Provider(),
        config=_config(tmp_path),
        tool_definitions=[_tool_def("retrieve_tool_result")],
        tool_handler=_unused_tool_handler,
    )
    messages = _history_messages(_BIG_CONTENT)

    # Production state: every block delivered full-text once gets frozen, so
    # pressure-triggered passes skip it forever. Only the waterline may
    # unlock it.
    agent._remember_provider_visible_tool_results(list(messages))

    request_messages = _assemble(agent, messages)
    texts = _request_texts(request_messages)

    projected = [text for text in texts if text.startswith("[tool_result_projection]")]
    assert projected, "bulky historical tool result should be projected"
    assert any("tool_result_handle:" in text for text in projected)
    # Persisted source history must stay untouched.
    source_block = messages[2].content[0]
    assert source_block.content == _BIG_CONTENT
    assert agent.config.metadata["history_waterline_projection_applied"] is True
    assert agent.config.metadata["history_waterline_projection_blocks"] == 1
    assert agent.config.metadata["history_waterline_projection_chars_saved"] > 0


@pytest.mark.asyncio
async def test_history_waterline_leaves_recent_turns_intact(tmp_path: Any) -> None:
    agent = Agent(
        provider=_Provider(),
        config=_config(tmp_path),
        tool_definitions=[_tool_def("retrieve_tool_result")],
        tool_handler=_unused_tool_handler,
    )
    messages = [
        Message(role="user", content="older question"),
        Message(role="assistant", content="older answer"),
        Message(role="user", content="latest question"),
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="tool-new",
                    name="exec_command",
                    input={"command": "pytest -q"},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="tool-new",
                    content=_BIG_CONTENT,
                )
            ],
        ),
    ]

    agent._remember_provider_visible_tool_results(list(messages))

    request_messages = _assemble(agent, messages)

    for text in _request_texts(request_messages):
        assert not text.startswith("[tool_result_projection]")
    assert "history_waterline_projection_applied" not in agent.config.metadata


@pytest.mark.asyncio
async def test_history_waterline_disabled_with_zero_keep_turns(tmp_path: Any) -> None:
    agent = Agent(
        provider=_Provider(),
        config=_config(tmp_path, tool_result_history_projection_keep_recent_turns=0),
        tool_definitions=[_tool_def("retrieve_tool_result")],
        tool_handler=_unused_tool_handler,
    )
    messages = _history_messages(_BIG_CONTENT)

    agent._remember_provider_visible_tool_results(list(messages))

    request_messages = _assemble(agent, messages)

    for text in _request_texts(request_messages):
        assert not text.startswith("[tool_result_projection]")
    assert "history_waterline_projection_applied" not in agent.config.metadata


@pytest.mark.asyncio
async def test_history_waterline_skips_small_results_and_short_history(
    tmp_path: Any,
) -> None:
    agent = Agent(
        provider=_Provider(),
        config=_config(tmp_path),
        tool_definitions=[_tool_def("retrieve_tool_result")],
        tool_handler=_unused_tool_handler,
    )
    small_content = "small result\n" + ("x" * 100)
    messages = [
        Message(role="user", content="question"),
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="tool-small",
                    name="exec_command",
                    input={"command": "pytest -q"},
                )
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(tool_use_id="tool-small", content=small_content)
            ],
        ),
    ]

    request_messages = _assemble(agent, messages)

    for text in _request_texts(request_messages):
        assert not text.startswith("[tool_result_projection]")
    assert "history_waterline_projection_applied" not in agent.config.metadata
