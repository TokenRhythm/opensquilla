"""Retired wrap-up policy cannot interrupt normal provider response recovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ThinkingLevel, ToolResult
from opensquilla.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ReasoningDeltaEvent as ProviderReasoning
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart


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


def _echo_tool_call(tool_use_id: str) -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="echo"),
        ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="echo",
            arguments={"value": "hi"},
        ),
        ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
    ]


def _empty_response() -> list[Any]:
    return [ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=0)]


def _final_text() -> list[Any]:
    return [
        ProviderText(text="done"),
        ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=1),
    ]


def _echo_agent(provider: _SequenceProvider, config: AgentConfig) -> Agent:
    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    return Agent(
        provider=provider,
        config=config,
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


def _user_texts(messages: list[Message]) -> list[str]:
    return [
        message.content
        for message in messages
        if message.role == "user" and isinstance(message.content, str)
    ]


@pytest.mark.asyncio
async def test_retired_wrapup_keeps_post_tool_empty_response_recovery() -> None:
    provider = _SequenceProvider(
        [
            _echo_tool_call("use-1"),
            _empty_response(),
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            max_iterations=5,
            post_tool_empty_recovery_mode="warn_model",
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert any(
        event.kind == "warning" and event.code == "post_tool_empty_recovery" for event in events
    )
    assert len(provider.calls) == 3
    assert any(
        text.startswith("[Runtime recovery]") for text in _user_texts(provider.calls[2]["messages"])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_safe", [False, True])
@pytest.mark.parametrize("legacy_margin", [0, 60])
async def test_retired_wrapup_does_not_preempt_or_reprompt_a_response(
    retry_safe: bool,
    legacy_margin: int,
) -> None:
    provider = _SequenceProvider([[ProviderReasoning(text="thinking"), *_final_text()]])
    provider.retry_failed_call_safe = retry_safe
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=legacy_margin,
            thinking=ThinkingLevel.MEDIUM,
        ),
    )

    events = [event async for event in agent.run_turn("Inspect the workspace")]

    assert not any(event.kind == "error" for event in events)
    assert next(event for event in events if event.kind == "done").text == "done"
    assert len(provider.calls) == 1
    assert provider.calls[0]["config"].thinking is True
    user_texts = _user_texts(provider.calls[0]["messages"])
    assert len(user_texts) == 1
    assert user_texts[0].startswith("Inspect the workspace")
    assert "Time check: roughly " not in user_texts[0]
