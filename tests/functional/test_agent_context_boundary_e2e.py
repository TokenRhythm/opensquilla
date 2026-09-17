from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.types import ToolCall
from opensquilla.execution_status import runtime_execution_status
from opensquilla.provider import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart


class _BoundaryProvider:
    provider_name = "synthetic"

    def __init__(self, large_local_argument: str) -> None:
        self.large_local_argument = large_local_argument
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append(messages)
        return self._stream(len(self.calls))

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            calls = [
                (
                    "local-large-1",
                    "local_context_builder",
                    {"content": self.large_local_argument, "label": "local"},
                ),
                ("fetch-large-0", "web_fetch", {"url": "https://example.com/0"}),
                ("fetch-large-1", "web_fetch", {"url": "https://example.com/1"}),
                ("fetch-error-2", "web_fetch", {"url": "https://example.com/2"}),
            ]
            for tool_use_id, tool_name, arguments in calls:
                yield ProviderToolUseStart(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                )
                yield ProviderToolUseEnd(
                    tool_use_id=tool_use_id,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            yield ProviderDone(stop_reason="tool_use", input_tokens=500, output_tokens=50)
            return
        yield ProviderText(text="BOUNDARY_E2E_OK")
        yield ProviderDone(stop_reason="stop", input_tokens=500, output_tokens=10)

    async def list_models(self) -> list[Any]:
        return []


def _tool_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Synthetic {name}.",
        input_schema=ToolInputSchema(properties={"content": {}, "url": {}, "label": {}}),
    )


def _tool_blocks(messages: list[Message]) -> list[ContentBlockToolUse]:
    blocks: list[ContentBlockToolUse] = []
    for message in messages:
        if not isinstance(message.content, list):
            continue
        blocks.extend(
            block for block in message.content if isinstance(block, ContentBlockToolUse)
        )
    return blocks


def _tool_result_blocks(messages: list[Message]) -> list[ContentBlockToolResult]:
    blocks: list[ContentBlockToolResult] = []
    for message in messages:
        if not isinstance(message.content, list):
            continue
        blocks.extend(
            block for block in message.content if isinstance(block, ContentBlockToolResult)
        )
    return blocks


@pytest.mark.asyncio
async def test_agent_multi_turn_boundary_e2e_keeps_critical_context() -> None:
    large_local_argument = "LOCAL_ARGUMENT_START\n" + ("x" * 24_000)
    provider = _BoundaryProvider(large_local_argument)

    async def tool_handler(call: ToolCall) -> ToolResult:
        if call.tool_use_id == "local-large-1":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="local context stored",
                is_error=False,
            )
        if call.tool_use_id == "fetch-error-2":
            return ToolResult(
                tool_use_id=call.tool_use_id,
                tool_name=call.tool_name,
                content="FETCH_FAILURE_MARKER " + ("e" * 8_000),
                is_error=True,
                execution_status=runtime_execution_status(
                    "error",
                    reason="runtime_error",
                ),
            )
        suffix = call.tool_use_id.rsplit("-", 1)[-1]
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content=f"FETCH_RESULT_{suffix}_START\n" + ("r" * 48_000),
            is_error=False,
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            context_window_tokens=200_000,
            max_tokens=8192,

            max_iterations=4,
        ),
        tool_definitions=[_tool_def("local_context_builder"), _tool_def("web_fetch")],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("exercise context boundaries")]

    assert any(event.kind == "done" and event.text == "BOUNDARY_E2E_OK" for event in events)
    assert len(provider.calls) == 2

    replay = provider.calls[1]
    tool_uses = _tool_blocks(replay)
    local_tool_use = next(block for block in tool_uses if block.id == "local-large-1")
    assert local_tool_use.input["content"] == large_local_argument

    result_content_by_id = {
        block.tool_use_id: block.content for block in _tool_result_blocks(replay)
    }
    assert str(result_content_by_id["fetch-large-0"]).startswith("FETCH_RESULT_0_START")
    assert "external_tool_result_compacted" not in str(
        result_content_by_id["fetch-large-0"]
    )
    assert "FETCH_FAILURE_MARKER" in str(result_content_by_id["fetch-error-2"])
    assert all(
        "opensquilla_compacted" not in str(block.input)
        and "tool_use_argument_projection" not in str(block.input)
        for block in tool_uses
    )
