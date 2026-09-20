from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.history import decode_assistant_replay, reconstruct_messages_from_entry
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.tool_argument_rejection import rejected_tool_arguments_error
from opensquilla.provider.types import (
    DoneEvent,
    ReasoningDeltaEvent,
    RejectedToolArguments,
    TextDeltaEvent,
    ToolArgumentRejection,
    ToolUseEndEvent,
    ToolUseStartEvent,
)

TOOL = ToolDefinition(
    name="record", description="Record a value",
    input_schema=ToolInputSchema(
        properties={"value": {"type": "string"}}, required=["value"],
    ),
)


def _rejected(call_id: str = "bad"):
    return rejected_tool_arguments_error(
        ToolArgumentRejection(
            calls=(RejectedToolArguments(call_id, "record", "invalid_json"),),
            terminal_reason="tool_calls",
        ),
        usage=DoneEvent(input_tokens=11, output_tokens=5, model="test", provider="fake"),
    )


class _Sequence:
    provider_name = "fake"

    def __init__(self, streams):
        self.streams = streams
        self.calls = []

    async def chat(self, messages, tools=None, config=None):
        index = len(self.calls)
        self.calls.append(list(messages))
        for event in self.streams[index]:
            yield event

    async def list_models(self):
        return []


def _valid(value="ok", call_id="good"):
    return [
        ToolUseStartEvent(tool_use_id=call_id, tool_name="record"),
        ToolUseEndEvent(tool_use_id=call_id, tool_name="record", arguments={"value": value}),
        DoneEvent(stop_reason="tool_use", input_tokens=10, output_tokens=2),
    ]


def _agent(provider, effects, **config):
    async def handle(call):
        effects.append(call.arguments)
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="recorded",
        )

    return Agent(
        provider=provider, tool_definitions=[TOOL], tool_handler=handle,
        config=AgentConfig(
            max_iterations=8, max_provider_retries=0,
            retry_base_backoff_ms=0, retry_max_backoff_ms=0, **config,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["text", "reasoning", "none"])
async def test_rejection_continues_with_feedback_preserving_prose_and_usage(prefix):
    first = {
        "text": [TextDeltaEvent(text="Preparing the page.\n")],
        "reasoning": [ReasoningDeltaEvent(text="Plan the page")],
        "none": [],
    }[prefix]
    provider = _Sequence([
        [*first, _rejected()], _valid(),
        [TextDeltaEvent(text="Finished."), DoneEvent(input_tokens=7, output_tokens=3)],
    ])
    effects = []
    agent = _agent(provider, effects)
    events = [event async for event in agent.run_turn("Make a page")]
    assert effects == [{"value": "ok"}]
    assert len(provider.calls) == 3
    assert not [e for e in events if e.kind == "error"]
    correction = provider.calls[1]
    assert "No tools in THIS batch ran" in correction[-1].content
    assert "invalid_json" in correction[-1].content
    assert not any(
        getattr(block, "type", None) == "tool_use"
        for message in correction for block in message.content
        if isinstance(message.content, list)
    )
    done = next(e for e in events if e.kind == "done")
    expected_prefix = "Preparing the page.\n" if prefix == "text" else ""
    assert done.text == expected_prefix + "Finished."
    assert done.input_tokens == 28
    assert done.output_tokens == 10
    assert "[Runtime tool argument feedback]" in str(done.assistant_replay)
    restored = decode_assistant_replay(done.assistant_replay)
    assert restored[0].role == "assistant"
    if prefix == "reasoning":
        assert done.reasoning_content == "Plan the page"
        assert restored[0].reasoning_content == "Plan the page"
    assert sum(e.kind == "tool_use_start" for e in events) == 1
    history = agent.history_snapshot()
    assert any("[Runtime tool argument feedback]" in str(m.content) for m in history)
    if expected_prefix:
        assert sum(expected_prefix in str(m.content) for m in history) == 1
    # Exercise the persisted v1 envelope through the normal replay loader and
    # a fresh Agent, not just the current turn's in-memory messages.
    reloaded = reconstruct_messages_from_entry(
        role="assistant", content=done.text, tool_calls=None, assistant_replay=json.loads(
            json.dumps(done.assistant_replay)
        ),
    )
    next_provider = _Sequence([[TextDeltaEvent(text="Still complete."), DoneEvent()]])
    next_agent = _agent(next_provider, effects)
    next_agent.set_history(reloaded)
    next_events = [event async for event in next_agent.run_turn("What happened?")]
    assert not [e for e in next_events if e.kind == "error"]
    assert "recorded" in str(next_provider.calls[0])
    assert "[Runtime tool argument feedback]" in str(next_provider.calls[0])
    assert effects == [{"value": "ok"}]


@pytest.mark.asyncio
async def test_correction_budget_is_turn_wide_across_ids_and_successful_calls():
    provider = _Sequence([
        [_rejected("bad1")], _valid("first", "one"),
        [_rejected("bad2")], _valid("second", "two"),
        [_rejected("bad3")],
    ])
    effects = []
    events = [event async for event in _agent(provider, effects).run_turn("Work")]
    assert len(provider.calls) == 5
    assert effects == [{"value": "first"}, {"value": "second"}]
    assert [e.code for e in events if e.kind == "error"] == ["tool_failure_loop_exhausted"]
    # A later correction sees both the earlier success and its explicit receipt.
    assert "recorded" in str(provider.calls[3])


@pytest.mark.asyncio
async def test_correction_still_obeys_turn_request_budget():
    provider = _Sequence([[_rejected()]])
    effects = []
    events = [event async for event in _agent(
        provider, effects, max_turn_llm_calls=1,
    ).run_turn("Work")]
    assert len(provider.calls) == 1
    assert not effects
    assert any(e.kind == "error" and "budget" in e.code for e in events)


@pytest.mark.asyncio
async def test_rejected_identity_must_match_advertised_tools():
    error = _rejected()
    error.tool_argument_rejection = ToolArgumentRejection(
        (RejectedToolArguments("bad", "unadvertised", "invalid_json"),), "tool_calls",
    )
    provider = _Sequence([[TextDeltaEvent(text="Started"), error]])
    events = [event async for event in _agent(provider, []).run_turn("Work")]
    assert len(provider.calls) == 1
    assert [e.code for e in events if e.kind == "error"] == ["incomplete_tool_call"]


@pytest.mark.asyncio
async def test_cancellation_during_correction_preserves_replay_without_executing():
    entered = asyncio.Event()

    class WaitingProvider(_Sequence):
        async def chat(self, messages, tools=None, config=None):
            if self.calls:
                entered.set()
                await asyncio.Event().wait()
            self.calls.append(list(messages))
            yield _rejected()

    provider = WaitingProvider([])
    effects = []
    agent = _agent(provider, effects)

    async def consume():
        return [event async for event in agent.run_turn("Work")]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=20)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert effects == []
    assert "[Runtime tool argument feedback]" in str(agent.current_assistant_replay())
    assert decode_assistant_replay(agent.current_assistant_replay())[0].role == "assistant"


@pytest.mark.asyncio
async def test_exhaustion_preserves_latest_visible_text_and_next_turn_has_new_budget():
    provider = _Sequence([
        [TextDeltaEvent(text="One."), _rejected("one")],
        [TextDeltaEvent(text="Two."), _rejected("two")],
        [TextDeltaEvent(text="Three."), _rejected("three")],
        [_rejected("new-turn")], _valid(),
        [TextDeltaEvent(text="Done."), DoneEvent()],
    ])
    agent = _agent(provider, [])
    events = [event async for event in agent.run_turn("Work")]
    assert [e.text for e in events if e.kind == "done"] == ["One.Two.Three."]
    events = [event async for event in agent.run_turn("Try again")]
    assert not [e for e in events if e.kind == "error"]
    assert len(provider.calls) == 6


@pytest.mark.asyncio
async def test_rejection_receipt_budget_keeps_visible_text_without_next_request():
    provider = _Sequence([[TextDeltaEvent(text="Already displayed."), _rejected()]])
    effects = []
    agent = _agent(provider, effects, max_turn_output_tokens=1)
    events = [event async for event in agent.run_turn("Work")]
    assert len(provider.calls) == 1
    assert effects == []
    assert any(e.kind == "error" and "budget" in e.code for e in events)
    done = next(e for e in events if e.kind == "done")
    assert done.text == "Already displayed."
    assert decode_assistant_replay(done.assistant_replay)[0].content == "Already displayed."


@pytest.mark.asyncio
async def test_unknown_rejected_usage_remains_missing_after_billed_success():
    error = _rejected()
    error.model_usage_breakdown = []
    error.usage_missing_count = 1
    provider = _Sequence([
        [error],
        [TextDeltaEvent(text="Done"), DoneEvent(
            input_tokens=10, output_tokens=2, billed_cost=0.2,
            cost_source="provider_billed", model="test", provider="fake",
        )],
    ])
    events = [event async for event in _agent(provider, []).run_turn("Work")]
    done = next(e for e in events if e.kind == "done")
    assert done.missing_cost_entries == 1
    assert done.cost_source == "mixed"
    assert done.billed_cost == 0.2
    assert done.input_tokens == 10


def _sse(*deltas: dict[str, Any], finish="tool_calls", done=True):
    chunks = [{"choices": [{"delta": delta, "finish_reason": None}]} for delta in deltas]
    chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    return b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks) + (
        b"data: [DONE]\n\n" if done else b""
    )


def _call(arguments, *, call_id="bad", index=0):
    return {"index": index, "id": call_id, "function": {"name": "record", "arguments": arguments}}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["text", "reasoning", "none"])
async def test_real_adapter_batch_rejection_feedback_and_exactly_once_execution(
    monkeypatch, prefix,
):
    requests = []
    effects = []
    prefix_deltas = {
        "text": [{"content": "Preparing.\n"}],
        "reasoning": [{"reasoning_content": "Planning"}], "none": [],
    }[prefix]
    responses = [
        _sse(*prefix_deltas, {"tool_calls": [
            _call('{"value":"unterminated'),
            _call('{"value":"sibling"}', call_id="sibling", index=1),
        ]}),
        _sse({"tool_calls": [_call('{"value":"corrected"}', call_id="good")]}),
        _sse({"content": "Finished."}, finish="stop"),
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            assert effects == []
            assert "No tools in THIS batch ran" in str(requests[-1]["messages"])
            assert "batch_not_executed" in str(requests[-1]["messages"])
        return httpx.Response(200, content=responses[len(requests) - 1])

    client = httpx.AsyncClient

    def patched_client(*args, **kwargs):
        return client(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", patched_client)
    provider = OpenAIProvider(
        api_key="offline-test", base_url="https://offline.invalid", proxy="",
        provider_kind="tokenrhythm", model="kimi-k2.7-code",
    )
    agent = _agent(provider, effects)
    events = [event async for event in agent.run_turn("Make a page")]
    assert len(requests) == 3
    assert effects == [{"value": "corrected"}]
    assert not [e for e in events if e.kind == "error"]
    assert [e.text for e in events if e.kind == "done"] == [
        ("Preparing.\n" if prefix == "text" else "") + "Finished."
    ]
