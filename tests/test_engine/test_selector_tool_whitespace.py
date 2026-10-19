"""Mixed SSE tool/text deltas keep their terminal gate through selector buffering."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.fallback import FallbackPolicy
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from tests.test_engine.test_provider_activity import _SequenceProvider
from tests.test_provider_stream_contract import _SEARCH_TOOL, _openai_sse, _patch_stream_body

MODEL = "qwen/qwen-2.5-72b-instruct"


def frame(delta, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def tool_prefix():
    return [frame({"tool_calls": [{
        "index": 0, "id": "call-local", "type": "function",
        "function": {"name": "search", "arguments": '{"que'},
    }]}), frame({"tool_calls": [{
        "index": 0, "function": {"arguments": 'ry":"x"}'},
    }]})]


def wrapper(*, fallback, physical=None):
    if physical is None:
        physical = OpenAIProvider(api_key="offline-only", model=MODEL, provider_kind="openrouter")

    class Selector:
        current_config = SimpleNamespace(provider="openrouter", model=MODEL)
        selected = False

        def next_fallback_after_failure(self, exc):
            if not fallback or self.selected:
                raise IndexError("No configured fallback")
            self.selected = True
            return physical

    primary = _SequenceProvider([[ErrorEvent(code="503", message="busy")]]) \
        if fallback else physical
    return _SelectorFallbackProvider(primary, Selector())


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True], ids=["primary", "fallback"])
@pytest.mark.parametrize("text", ["\n\n", "{\n", "A tool call and an explanation."])
async def test_mixed_tool_text_waits_for_valid_terminal_then_executes_once(
    monkeypatch, fallback, text,
):
    prefix_received = asyncio.Event()
    release_terminal = asyncio.Event()
    terminal_read = False
    requests = []
    effects = []
    events = []

    class ToolStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            nonlocal terminal_read
            for part in tool_prefix():
                yield f"data: {json.dumps(part)}\n\n".encode()
            yield f"data: {json.dumps(frame({'content': text}))}\n\n".encode()
            prefix_received.set()
            await release_terminal.wait()
            terminal_read = True
            yield _openai_sse([frame({}, "tool_calls")])

    def respond(request):
        requests.append(json.loads(request.content))
        assert len(requests) <= 2
        if len(requests) == 1:
            assert effects == []
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=ToolStream())
        assert effects == [{"query": "x"}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=_openai_sse([frame({"content": "Finished."}, "stop")]))

    client = httpx.AsyncClient
    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", lambda **kwargs: client(
        **{**kwargs, "transport": httpx.MockTransport(respond)},
    ))

    async def execute(call):
        assert terminal_read
        effects.append(call.arguments)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="found")

    agent = Agent(provider=wrapper(fallback=fallback), tool_definitions=[_SEARCH_TOOL],
                  tool_handler=execute, config=AgentConfig(
                      context_window_tokens=32768, context_window_known=True,
                      max_tokens=2048, max_provider_retries=0, max_iterations=3,
                  ))

    async def consume():
        async for event in agent.run_turn("Search once, then report the result."):
            events.append(event)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(prefix_received.wait(), timeout=5)
        await asyncio.sleep(0)
        assert effects == []
        assert not any(
            event.kind in {"text_delta", "tool_use_start", "tool_use_end"} for event in events
        )
        release_terminal.set()
        await asyncio.wait_for(task, timeout=10)
        assert effects == [{"query": "x"}]
        assert len(requests) == 2
        assert not [event for event in events if event.kind == "error"]
        assert any(event.kind == "done" for event in events)
        assert "".join(event.text for event in events if event.kind == "text_delta") == (
            text + "Finished."
        )
    finally:
        release_terminal.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True], ids=["primary", "fallback"])
@pytest.mark.parametrize("fault", ["missing-terminal", "post-terminal-mutation"])
@pytest.mark.parametrize("text", ["\n\n", "{\n"])
async def test_mixed_tool_text_does_not_relax_invalid_streams(monkeypatch, fallback, fault, text):
    parts = tool_prefix() + [frame({"content": text})]
    if fault == "post-terminal-mutation":
        parts += [frame({}, "tool_calls"), frame({"content": "late mutation"})]
    _patch_stream_body(monkeypatch, "openai", _openai_sse(parts))
    events = [event async for event in wrapper(fallback=fallback).chat(
        [Message(role="user", content="Search once.")], tools=[_SEARCH_TOOL],
        config=ChatConfig(context_window_tokens=32768, max_tokens=2048),
    )]
    errors = [event for event in events if event.kind == "error"]
    expected = "incomplete_stream" if fault == "missing-terminal" else "invalid_stream_order"
    assert [event.code for event in errors] == [expected]
    assert not any(
        event.kind in {"text_delta", "done", "tool_use_start", "tool_use_end"} for event in events
    )


@pytest.mark.asyncio
async def test_visible_text_whitespace_remains_unchanged(monkeypatch):
    _patch_stream_body(monkeypatch, "openai", _openai_sse([
        frame({"content": "Answer"}), frame({"content": "\n\n"}), frame({}, "stop"),
    ]))
    events = [event async for event in wrapper(fallback=False).chat(
        [Message(role="user", content="Answer.")],
        config=ChatConfig(context_window_tokens=32768, max_tokens=2048),
    )]
    assert [event.text for event in events if event.kind == "text_delta"] == ["Answer", "\n\n"]
    assert any(event.kind == "done" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True], ids=["primary", "fallback"])
@pytest.mark.parametrize("character", [" ", "x"])
async def test_provisional_text_still_respects_existing_buffer_bound(
    monkeypatch, fallback, character,
):
    _patch_stream_body(monkeypatch, "openai", _openai_sse(
        tool_prefix() + [frame({"content": character * (2 * 1024 * 1024)}),
                         frame({}, "tool_calls")],
    ))
    events = [event async for event in wrapper(fallback=fallback).chat(
        [Message(role="user", content="Search once.")], tools=[_SEARCH_TOOL],
        config=ChatConfig(context_window_tokens=32768, max_tokens=2048),
    )]
    assert [event.code for event in events if event.kind == "error"] == [
        "provider_pretext_buffer_exhausted",
    ]
    assert not any(event.kind in {"done", "tool_use_start", "tool_use_end"} for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True], ids=["primary", "fallback"])
@pytest.mark.parametrize("terminal", ["done", "error", "cancel"])
async def test_text_after_closed_tool_stays_provisional_until_done(fallback, terminal):
    prefix_received = asyncio.Event()
    release_terminal = asyncio.Event()
    terminal_read = False
    effects = []
    events = []
    mixed_text = "Explanation after ToolUseEnd."

    class HeldProvider(_SequenceProvider):
        async def _stream(self, stream):
            nonlocal terminal_read
            for event in stream:
                if isinstance(event, (DoneEvent, ErrorEvent)):
                    terminal_read = True
                yield event
                if isinstance(event, TextDeltaEvent) and event.text == mixed_text:
                    prefix_received.set()
                    await release_terminal.wait()

    physical = HeldProvider([[
        ToolUseStartEvent(tool_use_id="call-local", tool_name="search"),
        ToolUseDeltaEvent(tool_use_id="call-local", json_fragment='{"query":"x"}'),
        ToolUseEndEvent(tool_use_id="call-local", tool_name="search", arguments={"query": "x"}),
        TextDeltaEvent(text=mixed_text),
        ErrorEvent(code="synthetic_failure", message="failed before Done")
        if terminal == "error" else DoneEvent(stop_reason="tool_use"),
    ], [TextDeltaEvent(text="Finished."), DoneEvent(stop_reason="stop")]])

    async def execute(call):
        assert terminal_read
        effects.append(call.arguments)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="found")

    agent = Agent(provider=wrapper(fallback=fallback, physical=physical),
                  tool_definitions=[_SEARCH_TOOL], tool_handler=execute, config=AgentConfig(
                      context_window_tokens=32768, context_window_known=True,
                      max_tokens=2048, max_provider_retries=0, max_iterations=3,
                  ))

    async def consume():
        async for event in agent.run_turn("Search once, then report the result."):
            events.append(event)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(prefix_received.wait(), timeout=5)
        assert not effects and not terminal_read
        assert not any(event.kind in {"text_delta", "tool_use_start", "tool_use_end"}
                       for event in events)
        if terminal == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release_terminal.set()
            await asyncio.wait_for(task, timeout=10)
        if terminal == "done":
            assert effects == [{"query": "x"}] and physical.calls == 2
            assert any(event.kind == "done" for event in events)
            assert not any(event.kind == "error" for event in events)
            assert "".join(event.text for event in events if event.kind == "text_delta") == (
                mixed_text + "Finished."
            )
        else:
            assert not effects and physical.calls == 1
            assert not any(event.kind in {"text_delta", "tool_use_start", "tool_use_end", "done"}
                           for event in events)
            if terminal == "error":
                assert any(event.kind == "error" for event in events)
    finally:
        release_terminal.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True], ids=["primary", "fallback"])
@pytest.mark.parametrize("late_error", ["503", "429"])
async def test_published_mixed_tool_done_cannot_retry_or_fallback(
    monkeypatch, fallback, late_error,
):
    physical = _SequenceProvider([[
        ToolUseStartEvent(tool_use_id="call-local", tool_name="search"),
        ToolUseEndEvent(tool_use_id="call-local", tool_name="search", arguments={"query": "x"}),
        TextDeltaEvent(text="Published with the completed tool."),
        DoneEvent(stop_reason="tool_use"),
        ErrorEvent(code=late_error, message="late error after Done"),
    ]])
    other = _SequenceProvider([[TextDeltaEvent(text="Must not replace the published answer."),
                                DoneEvent(stop_reason="stop")]])

    class Selector:
        current_config = SimpleNamespace(provider="openrouter", model=MODEL)
        calls = 0

        def next_fallback_after_failure(self, exc):
            self.calls += 1
            return physical if fallback and self.calls == 1 else other

    async def forbidden_retry(*args, **kwargs):
        raise AssertionError("A published tool batch must not be retried")

    monkeypatch.setattr("opensquilla.engine.runtime.sleep_before_retry", forbidden_retry)
    selector = Selector()
    primary = _SequenceProvider([[ErrorEvent(code="503", message="busy")]]) \
        if fallback else physical
    wrapped = _SelectorFallbackProvider(primary, selector)
    wrapped.configure_retry_policy(FallbackPolicy(max_retries=2))
    events = [event async for event in wrapped.chat(
        [Message(role="user", content="Search once.")], tools=[_SEARCH_TOOL],
        config=ChatConfig(context_window_tokens=32768, max_tokens=2048),
    )]
    assert selector.calls == int(fallback)
    assert physical.calls == 1 and other.calls == 0
    assert [event.text for event in events if event.kind == "text_delta"] == [
        "Published with the completed tool.",
    ]
    assert sum(event.kind == "tool_use_end" for event in events) == 1
    assert sum(event.kind == "done" for event in events) == 1
    assert any(event.kind == "error" for event in events)
