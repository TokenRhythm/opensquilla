from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.contracts.turn_execution import TurnExecutionContext, TurnIdentity
from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.types import ProviderActivityEvent
from opensquilla.engine.types import ToolUseStartEvent as PublicToolStart
from opensquilla.gateway.boot import _emit_task_runtime_stream_events
from opensquilla.gateway.session_streams import SessionStreamRegistry
from opensquilla.provider import (
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputPreviewEvent,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


class _Provider:
    provider_name = "openrouter"

    def __init__(self, *, failed: bool = False, code: str = "400") -> None:
        self.failed = failed
        self.code = code
        self.calls = 0
        self.done_seen = False

    async def chat(self, messages, tools=None, config=None):
        self.calls += 1
        if self.calls > 1:
            yield TextDeltaEvent(text="done")
            yield DoneEvent()
            return
        yield ToolUseStartEvent(tool_use_id="file", tool_name="write_file")
        yield ToolUseDeltaEvent(
            tool_use_id="file", json_fragment='{"path":"example.html","content":"<html>',
        )
        yield ToolUseDeltaEvent(tool_use_id="file", json_fragment='hello</html>"}')
        if self.failed:
            yield ErrorEvent(code=self.code)
            return
        yield ToolUseEndEvent(
            tool_use_id="file", tool_name="write_file",
            arguments={"path": "example.html", "content": "<html>hello</html>"},
        )
        self.done_seen = True
        yield DoneEvent(stop_reason="tool_calls")


def _agent(provider: Any, executions: list[Any], context=None) -> Agent:
    async def handler(call):
        executions.append(call.arguments)
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="written")

    return Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0),
        tool_definitions=[ToolDefinition(
            name="write_file", description="write a file", input_schema=ToolInputSchema(),
        )],
        tool_handler=handler,
        execution_context=context,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_draft_precedes_commit_and_never_executes_failed_attempt(wrapped, failed) -> None:
    provider = _Provider(failed=failed)

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="test")

        def next_fallback_after_failure(self, exc):
            return None

    selected = _SelectorFallbackProvider(provider, _Selector()) if wrapped else provider
    executions = []
    events = []
    async for event in _agent(selected, executions).run_turn("write a file"):
        events.append(event)
        if isinstance(event, ToolInputPreviewEvent) and event.state == "generating":
            assert not provider.done_seen
            assert executions == []
            assert not any(isinstance(item, PublicToolStart) for item in events)
            assert event.generation_epoch == 0

    previews = [event for event in events if isinstance(event, ToolInputPreviewEvent)]
    assert len({event.preview_id for event in previews}) == 1
    assert previews[0].content == "<html>"
    assert previews[-1].state == ("discarded" if failed else "committed")
    if failed:
        assert executions == [] and previews[-1].content == ""
        assert not any(isinstance(event, PublicToolStart) for event in events)
    else:
        assert executions == [{"path": "example.html", "content": "<html>hello</html>"}]
        assert previews[-1].content == "<html>hello</html>"
        commit = next(index for index, event in enumerate(events) if event is previews[-1])
        assert isinstance(events[commit + 1], PublicToolStart)


@pytest.mark.asyncio
async def test_preview_emits_real_gateway_wire_and_snapshot_coalesces() -> None:
    registry = SessionStreamRegistry()
    observed = []

    async def source():
        for revision in range(1, 20):
            yield ToolInputPreviewEvent(
                preview_id="draft", attempt_id="attempt", tool_use_id="file",
                generation_epoch=0, revision=revision, path="a.txt", content=str(revision),
            )

    async def emit(session_key, event_name, payload):
        observed.append((event_name, payload))
        registry.record(session_key, event_name, payload)

    await _emit_task_runtime_stream_events(
        source(), "agent:main:webchat:draft", emit,
        task_id="task", session_id="session", session_epoch=1,
    )
    assert len(observed) == 19
    assert all(name == "session.event.tool_input_preview" for name, _ in observed)
    assert all(payload["task_id"] == "task" for _, payload in observed)
    snapshot = registry.live_snapshot("agent:main:webchat:draft")
    drafts = [event for event in snapshot.events if event.event_name.endswith("tool_input_preview")]
    assert len(drafts) == 1
    assert drafts[0].payload["revision"] == 19
    assert drafts[0].payload["content"] == "19"
    assert "tool_results" not in json.dumps(drafts[0].payload)
    registry.record(
        "agent:main:webchat:draft", "session.event.done", {"task_id": "task"},
    )
    assert not registry.live_snapshot("agent:main:webchat:draft").events
    terminal = registry.take_terminal_activity_snapshot(
        "agent:main:webchat:draft", "task", turn_id="task",
    )
    assert "tool_input_preview" not in json.dumps(terminal)


@pytest.mark.asyncio
async def test_selector_fallback_revokes_draft_and_reused_tool_id_gets_new_identity() -> None:
    primary = _Provider(failed=True, code="503")
    fallback = _Provider()

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary")

        def next_fallback_after_failure(self, exc):
            self.current_config = SimpleNamespace(provider="openrouter", model="fallback")
            return fallback

    executions = []
    agent = _agent(_SelectorFallbackProvider(primary, _Selector()), executions)
    events = [event async for event in agent.run_turn("write")]
    previews = [event for event in events if isinstance(event, ToolInputPreviewEvent)]
    discarded = next(event for event in previews if event.state == "discarded")
    committed = next(event for event in previews if event.state == "committed")
    assert discarded.preview_id != committed.preview_id
    assert discarded.attempt_id != committed.attempt_id
    assert discarded.tool_use_id == committed.tool_use_id == "file"
    assert discarded.content == ""
    assert len(executions) == 1
    assert primary.calls == 1


@pytest.mark.asyncio
async def test_closing_stream_during_preview_never_executes_tool() -> None:
    executions = []
    stream = _agent(_Provider(), executions).run_turn("write")
    async for event in stream:
        if isinstance(event, ToolInputPreviewEvent):
            break
    assert executions == []
    await stream.aclose()
    assert executions == []


@pytest.mark.asyncio
async def test_terminal_context_rejects_late_generating_preview() -> None:
    context = TurnExecutionContext.create(TurnIdentity("turn", "assistant", "draft-test"))

    class _CancelledProvider(_Provider):
        async def chat(self, messages, tools=None, config=None):
            async for event in super().chat(messages, tools, config):
                if isinstance(event, ToolUseDeltaEvent) and "hello" in event.json_fragment:
                    context.begin_control_terminal("user_cancelled")
                yield event

    executions = []
    events = [
        event async for event in _agent(_CancelledProvider(), executions, context).run_turn("write")
    ]
    generated = [
        event for event in events
        if isinstance(event, ToolInputPreviewEvent) and event.state == "generating"
    ]
    assert [event.content for event in generated] == ["<html>"]
    assert executions == []


@pytest.mark.asyncio
async def test_selector_local_protocol_failure_discards_before_fallback_notice() -> None:
    class _InvalidInterleave(_Provider):
        async def chat(self, messages, tools=None, config=None):
            yield ToolUseStartEvent(tool_use_id="file", tool_name="write_file")
            yield ToolUseDeltaEvent(
                tool_use_id="file", json_fragment='{"path":"abandoned.txt","content":"old',
            )
            yield TextDeltaEvent(text="invalid interleave")

    fallback = _Provider()

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary")

        def next_fallback_after_failure(self, exc):
            self.current_config = SimpleNamespace(provider="openrouter", model="fallback")
            return fallback

    executions = []
    agent = _agent(_SelectorFallbackProvider(_InvalidInterleave(), _Selector()), executions)
    events = [event async for event in agent.run_turn("write")]
    discard_index = next(
        index for index, event in enumerate(events)
        if isinstance(event, ToolInputPreviewEvent) and event.state == "discarded"
    )
    fallback_index = next(
        index for index, event in enumerate(events)
        if isinstance(event, ProviderActivityEvent) and event.phase == "fallback"
    )
    assert events[discard_index].path == "abandoned.txt"
    assert discard_index < fallback_index
    assert len(executions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    {"state": []}, {"generation_epoch": []}, {"revision": True}, {"content": "\ud800"},
])
async def test_malformed_optional_preview_cannot_break_the_real_answer(patch) -> None:
    class _MalformedPreview(_Provider):
        async def chat(self, messages, tools=None, config=None):
            preview = ToolInputPreviewEvent(
                preview_id="preview", attempt_id="attempt", tool_use_id="file",
                path="a.txt", content="draft", revision=1,
            )
            preview.__dict__.update(patch)
            yield preview
            yield TextDeltaEvent(text="real answer")
            yield DoneEvent()

    events = [event async for event in _agent(_MalformedPreview(), []).run_turn("hello")]
    assert not any(isinstance(event, ToolInputPreviewEvent) for event in events)
    assert any(getattr(event, "text", "") == "real answer" for event in events)
