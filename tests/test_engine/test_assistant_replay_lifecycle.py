"""Replay state follows accepted provider calls, not public stream fragments."""

from __future__ import annotations

import json

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.session_sanitize import sanitize_session_messages
from opensquilla.engine.types import DoneEvent, ThinkingLevel, public_agent_event_payload
from opensquilla.provider import (
    ContentBlockToolUse,
    Message,
    ModelCapabilities,
    ProviderGenerationResetEvent,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolStart
from opensquilla.provider.types import ProviderReplayState


def _state(label: str) -> ProviderReplayState:
    return ProviderReplayState(
        protocol="openai_chat_completions",
        source="synthetic-origin",
        model="synthetic-model",
        reasoning_details=[{"type": "reasoning.encrypted", "data": label, "index": 0}],
    )


class _ScriptedProvider:
    provider_name = "synthetic"

    def __init__(self, streams):
        self.streams = streams
        self.calls = []

    async def chat(self, messages, tools=None, config=None):
        index = len(self.calls)
        self.calls.append([message.model_copy(deep=True) for message in messages])
        assert index < len(self.streams), "unexpected additional provider call"
        for event in self.streams[index]:
            yield event


@pytest.mark.asyncio
async def test_output_limit_continuation_keeps_native_state_in_request_and_final_record():
    first, last = _state("partial-state"), _state("final-state")
    provider = _ScriptedProvider(
        [
            [
                ProviderText(text="partial answer"),
                ProviderDone(
                    stop_reason="length",
                    input_tokens=7,
                    output_tokens=9,
                    reasoning_content="partial reasoning",
                    provider_replay=first,
                ),
            ],
            [
                ProviderText(text=" finished"),
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=8,
                    output_tokens=1,
                    reasoning_content="final reasoning",
                    provider_replay=last,
                ),
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
    events = [event async for event in agent.run_turn("synthetic output-limit test")]
    assert not any(event.kind == "error" for event in events)
    assistant = next(message for message in provider.calls[1] if message.role == "assistant")
    assert assistant.provider_replay == first
    assert assistant.reasoning_content == "partial reasoning"
    done = next(event for event in events if isinstance(event, DoneEvent))
    saved = [Message.model_validate(item) for item in done.assistant_replay["messages"]]
    assert [message.provider_replay for message in saved if message.role == "assistant"] == [
        first,
        last,
    ]


@pytest.mark.asyncio
async def test_generation_reset_cannot_commit_stale_native_state():
    new = _state("accepted-state")
    provider = _ScriptedProvider(
        [
            [
                ProviderText(text="abandoned prefix", generation_epoch=0),
                ProviderGenerationResetEvent(
                    from_role="primary_aggregator",
                    to_role="fixed_direct",
                    safe_reason="takeover",
                ),
                ProviderText(text="accepted answer", generation_epoch=1),
                ProviderDone(
                    stop_reason="stop",
                    generation_epoch=0,
                    provider_replay=_state("stale-state"),
                    reasoning_content="stale reasoning",
                ),
                ProviderDone(
                    stop_reason="stop",
                    generation_epoch=1,
                    provider_replay=new,
                    reasoning_content="accepted reasoning",
                ),
            ]
        ]
    )
    agent = Agent(provider=provider, config=AgentConfig(max_iterations=1, max_provider_retries=0))
    events = [event async for event in agent.run_turn("synthetic reset test")]
    done = next(event for event in events if isinstance(event, DoneEvent))
    saved = [Message.model_validate(item) for item in done.assistant_replay["messages"]]
    assert len(saved) == 1 and saved[0].provider_replay == new
    assert saved[0].reasoning_content == "accepted reasoning"
    assert "stale-state" not in json.dumps(done.assistant_replay)
    assert "abandoned prefix" not in json.dumps(done.assistant_replay)


@pytest.mark.asyncio
@pytest.mark.parametrize("input_tokens", [0, 10])
async def test_failed_followup_preserves_committed_tool_call_without_partial_assistant(
    input_tokens,
):
    state = _state("completed-tool-state")
    provider = _ScriptedProvider(
        [
            [
                ProviderToolStart(tool_use_id="call-1", tool_name="record"),
                ProviderToolEnd(tool_use_id="call-1", tool_name="record", arguments={}),
                ProviderDone(
                    stop_reason="tool_use",
                    input_tokens=input_tokens,
                    output_tokens=0,
                    provider_replay=state,
                    reasoning_content="tool reasoning",
                ),
            ],
            [
                ProviderText(text="unfinished text"),
                ProviderError(message="synthetic error", code="401"),
            ],
        ]
    )

    async def tool_handler(call):
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="recorded"
        )

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2, max_provider_retries=0),
        tool_definitions=[
            ToolDefinition(
                name="record",
                description="pure synthetic tool",
                input_schema=ToolInputSchema(properties={}),
            )
        ],
        tool_handler=tool_handler,
    )
    events = [event async for event in agent.run_turn("synthetic failed followup")]
    assert any(event.kind == "error" for event in events)
    done = next(event for event in events if isinstance(event, DoneEvent))
    saved = [Message.model_validate(item) for item in done.assistant_replay["messages"]]
    assistants = [message for message in saved if message.role == "assistant"]
    assert len(assistants) == 1 and assistants[0].provider_replay == state
    assert isinstance(assistants[0].content[0], ContentBlockToolUse)
    assert any(message.role == "user" for message in saved)
    assert "unfinished text" not in json.dumps(done.assistant_replay)


def test_sanitizing_message_metadata_retains_opaque_replay_state():
    state = _state("synthetic-opaque")
    original = Message.model_construct(
        role="assistant",
        content=[
            {
                "type": "text",
                "text": "answer",
                "diagnostic_metadata": "not-model-content",
            }
        ],
        reasoning_content="reasoning",
        provider_replay=state,
    )
    sanitized, result = sanitize_session_messages([original])
    assert result.metadata_keys_removed == 1
    assert sanitized[0].provider_replay == state
    assert sanitized[0].reasoning_content == "reasoning"
    assert "diagnostic_metadata" in original.content[0]


@pytest.mark.asyncio
async def test_reasoning_prefill_recovery_passes_native_state_to_consumer():
    state = _state("reasoning-prefill-state")
    provider = _ScriptedProvider(
        [
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=5,
                    reasoning_tokens=5,
                    reasoning_content="unfinished reasoning",
                    provider_replay=state,
                )
            ],
            [
                ProviderText(text="completed answer"),
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=11,
                    output_tokens=1,
                    provider_replay=_state("completed-answer-state"),
                ),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True, supports_tools=True, reasoning_format="openrouter"
            ),
            reasoning_prefill_recovery_mode="recover",
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )
    events = [event async for event in agent.run_turn("synthetic prefill")]
    assert not any(event.kind == "error" for event in events)
    assert any(
        event.kind == "warning" and event.code == "provider_reasoning_prefill_continue"
        for event in events
    )
    assistant = next(message for message in provider.calls[1] if message.role == "assistant")
    assert assistant.provider_replay == state
    assert assistant.reasoning_content == "unfinished reasoning"


def test_public_done_serialization_does_not_copy_or_expose_native_state():
    class NeverCopy:
        def __deepcopy__(self, memo):
            raise AssertionError(
                "private native state must not be traversed by public serialization"
            )

    done = DoneEvent(text="public answer", assistant_replay={"private": NeverCopy()})
    public = public_agent_event_payload(done)
    assert public["text"] == "public answer"
    assert "assistant_replay" not in public
    assert done.assistant_replay is not None
    assert "NeverCopy" not in repr(done)
