from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ThinkingLevel, ToolResult
from opensquilla.provider import ChatConfig, Message, ToolDefinition, ToolInputSchema
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
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
        self.calls.append({"messages": messages, "tools": tools})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


class _CompositeSequenceProvider(_SequenceProvider):
    provider_name = "ensemble"
    retry_failed_call_safe = False


def _reasoning_only_done() -> ProviderDone:
    return ProviderDone(
        stop_reason="stop",
        input_tokens=4,
        output_tokens=2,
        reasoning_tokens=2,
        reasoning_content="internal",
    )


def _empty_done() -> ProviderDone:
    return ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=0)


def _ok_done() -> ProviderDone:
    return ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=1)


@pytest.mark.asyncio
async def test_reasoning_only_retries_once_then_errors() -> None:
    provider = _SequenceProvider(
        [
            [_reasoning_only_done()],
            [_reasoning_only_done()],
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
    assert any(
        event.kind == "warning" and event.code == "provider_reasoning_only_retry"
        for event in events
    )
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)


@pytest.mark.asyncio
async def test_reasoning_only_resolves_on_retry() -> None:
    provider = _SequenceProvider(
        [
            [_reasoning_only_done()],
            [ProviderText(text="ok"), _ok_done()],
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
    assert any(event.kind == "done" and event.text == "ok" for event in events)


@pytest.mark.asyncio
async def test_malformed_empty_retries_once_then_errors() -> None:
    provider = _SequenceProvider(
        [
            [_empty_done()],
            [_empty_done()],
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


@pytest.mark.asyncio
async def test_malformed_empty_resolves_on_retry() -> None:
    provider = _SequenceProvider(
        [
            [_empty_done()],
            [ProviderText(text="ok"), _ok_done()],
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
    assert any(event.kind == "done" and event.text == "ok" for event in events)


@pytest.mark.asyncio
async def test_stream_incomplete_retries_once_then_errors() -> None:
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
    assert any(event.kind == "warning" and event.code == "provider_empty_retry" for event in events)
    assert any(
        event.kind == "error" and event.code == "provider_stream_incomplete"
        for event in events
    )


@pytest.mark.asyncio
async def test_stream_incomplete_resolves_on_retry() -> None:
    provider = _SequenceProvider(
        [
            [],
            [ProviderText(text="ok"), _ok_done()],
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
    assert any(event.kind == "done" and event.text == "ok" for event in events)


@pytest.mark.asyncio
async def test_timeout_error_code_retries_when_message_lacks_timeout_token() -> None:
    provider = _SequenceProvider(
        [
            [ProviderError(message="Request timed out: ", code="timeout")],
            [ProviderText(text="ok"), _ok_done()],
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
    assert any(event.kind == "done" and event.text == "ok" for event in events)
    assert not any(event.kind == "error" and event.code == "timeout" for event in events)


@pytest.mark.asyncio
async def test_composite_timeout_surfaces_without_replaying_full_call() -> None:
    provider = _CompositeSequenceProvider(
        [
            [ProviderError(message="Request timed out: ", code="timeout")],
            [ProviderText(text="should-not-run"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    assert any(event.kind == "error" and event.code == "timeout" for event in events)
    assert not any(
        event.kind == "text_delta" and event.text == "should-not-run"
        for event in events
    )


@pytest.mark.asyncio
async def test_composite_partial_timeout_does_not_duplicate_visible_text() -> None:
    provider = _CompositeSequenceProvider(
        [
            [
                ProviderText(text="partial"),
                ProviderError(message="Request timed out: ", code="timeout"),
            ],
            [ProviderText(text="duplicate"), _ok_done()],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert len(provider.calls) == 1
    visible = [event.text for event in events if event.kind == "text_delta"]
    assert visible == ["partial"]
    assert any(event.kind == "error" and event.code == "timeout" for event in events)


@pytest.mark.asyncio
async def test_first_turn_provider_empty_response_error_surfaces_without_retry() -> None:
    provider = _SequenceProvider(
        [
            [ProviderError(message="Provider returned an empty response", code="empty_response")],
            [ProviderText(text="should-not-run"), _ok_done()],
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

    assert len(provider.calls) == 1
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)


@pytest.mark.asyncio
async def test_post_tool_provider_empty_response_error_retries_once_and_recovers() -> None:
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
            [ProviderError(message="Provider returned an empty response", code="empty_response")],
            [ProviderText(text="done"), _ok_done()],
        ]
    )

    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=2,
            max_provider_retries=1,
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

    assert len(provider.calls) == 3
    assert any(event.kind == "warning" and event.code == "provider_empty_retry" for event in events)
    assert any(event.kind == "done" and event.text == "done" for event in events)


@pytest.mark.asyncio
async def test_post_tool_provider_empty_response_error_retries_once_with_default_budget() -> None:
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
            [ProviderError(message="Provider returned an empty response", code="empty_response")],
            [ProviderError(message="Provider returned an empty response", code="empty_response")],
            [ProviderText(text="should-not-run"), _ok_done()],
        ]
    )

    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(
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

    assert len(provider.calls) == 3
    assert (
        len(
            [
                event
                for event in events
                if event.kind == "warning" and event.code == "provider_empty_retry"
            ]
        )
        == 1
    )
    assert any(event.kind == "error" and event.code == "empty_response" for event in events)
