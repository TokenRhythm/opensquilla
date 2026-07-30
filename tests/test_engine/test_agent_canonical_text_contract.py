"""Regression coverage for canonical provider text through Agent paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import structlog.testing

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseDeltaEvent as ProviderToolUseDelta
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart


class _SequenceProvider:
    provider_name = "synthetic"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls: list[list[Message]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del tools, config
        index = len(self.calls)
        self.calls.append(messages)
        return self._stream(self.streams[index])

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


def _synthetic_tool_stream(text: str, *, stop_reason: str) -> list[Any]:
    return [
        ProviderText(text=text),
        ProviderToolUseStart(
            tool_use_id="tool-1",
            tool_name="echo",
            synthetic_from_text=True,
        ),
        ProviderToolUseEnd(
            tool_use_id="tool-1",
            tool_name="echo",
            arguments={"value": "x"},
            synthetic_from_text=True,
        ),
        ProviderDone(stop_reason=stop_reason, input_tokens=1, output_tokens=1),
    ]


def _make_agent(
    provider: _SequenceProvider,
    *,
    length_capped_continuations: int = 3,
    tool_calls: list[str] | None = None,
    captured_tool_calls: list[Any] | None = None,
) -> Agent:
    async def tool_handler(call: Any) -> ToolResult:
        if tool_calls is not None:
            tool_calls.append(call.tool_name)
        if captured_tool_calls is not None:
            captured_tool_calls.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    return Agent(
        provider=provider,
        config=AgentConfig(
            length_capped_continuations=length_capped_continuations,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Synthetic echo tool.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=tool_handler,
    )


@pytest.mark.asyncio
async def test_normal_tool_loop_does_not_strip_canonical_synthetic_text() -> None:
    canonical = 'Literal example remains.\n\necho{"value": "x"}'
    tool_calls: list[str] = []
    provider = _SequenceProvider(
        [
            _synthetic_tool_stream(canonical, stop_reason="tool_use"),
            [
                ProviderText(text="After tool."),
                ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1),
            ],
        ]
    )
    agent = _make_agent(provider, tool_calls=tool_calls)

    events = [event async for event in agent.run_turn("test")]

    done = next(event for event in events if event.kind == "done")
    assert done.text == canonical + "After tool."
    assert done.text_snapshot == done.text
    assert tool_calls == ["echo"]


@pytest.mark.asyncio
async def test_length_continuation_does_not_strip_canonical_synthetic_text() -> None:
    canonical = 'Literal example remains.\n\necho{"value": "x"}'
    tool_calls: list[str] = []
    provider = _SequenceProvider(
        [
            _synthetic_tool_stream(canonical, stop_reason="length"),
            [
                ProviderText(text="Continued."),
                ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1),
            ],
        ]
    )
    agent = _make_agent(
        provider,
        length_capped_continuations=1,
        tool_calls=tool_calls,
    )

    events = [event async for event in agent.run_turn("test")]

    done = next(event for event in events if event.kind == "done")
    assert done.text == canonical + "Continued."
    assert tool_calls == []


@pytest.mark.asyncio
async def test_length_exhaustion_accounts_for_full_canonical_synthetic_text() -> None:
    canonical = 'Literal example remains.\n\necho{"value": "x"}'
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="First partial."),
                ProviderDone(stop_reason="length", input_tokens=1, output_tokens=1),
            ],
            _synthetic_tool_stream(canonical, stop_reason="length"),
        ]
    )
    agent = _make_agent(provider, length_capped_continuations=1)

    with structlog.testing.capture_logs() as captured:
        events = [event async for event in agent.run_turn("test")]

    exhausted = [
        event
        for event in captured
        if event.get("event") == "provider.output_truncated_exhausted"
    ]
    assert exhausted[-1]["visible_chars"] == len(canonical)
    assert any(
        event.kind == "error" and event.code == "provider_output_truncated"
        for event in events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provisional_delta",
    [
        pytest.param('{"value": "unfinished', id="malformed"),
        pytest.param('{"value": "stale"}', id="stale-valid"),
        pytest.param('["not", "an", "object"]', id="non-object"),
        pytest.param('{"value": NaN}', id="non-finite"),
    ],
)
async def test_provider_terminal_arguments_override_provisional_delta_bytes(
    provisional_delta: str,
) -> None:
    captured: list[Any] = []
    provider = _SequenceProvider(
        [
            [
                ProviderToolUseStart(
                    tool_use_id="tool-1",
                    tool_name="echo",
                ),
                ProviderToolUseDelta(
                    tool_use_id="tool-1",
                    json_fragment=provisional_delta,
                ),
                ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "provider-repaired"},
                ),
                ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1),
            ],
            [
                ProviderText(text="Finished."),
                ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1),
            ],
        ]
    )
    agent = _make_agent(provider, captured_tool_calls=captured)

    events = [event async for event in agent.run_turn("test")]

    assert len(captured) == 1
    assert captured[0].arguments == {"value": "provider-repaired"}
    assert next(event for event in events if event.kind == "done").text == "Finished."


@pytest.mark.asyncio
async def test_recovery_failure_emits_authoritative_empty_terminal_snapshot() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="superseded answer"),
                ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1),
            ],
            [ProviderError(message="fatal retry failure", code="400")],
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
        config=AgentConfig(
            text_only_tool_recovery_mode="warn_model",
            max_provider_retries=0,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Synthetic echo tool.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=tool_handler,
    )

    events = [event async for event in agent.run_turn("test")]
    done = next(event for event in events if event.kind == "done")

    assert done.text == ""
    assert done.text_snapshot == ""
    assert any(
        event.kind == "warning" and event.code == "text_only_tool_recovery"
        for event in events
    )
