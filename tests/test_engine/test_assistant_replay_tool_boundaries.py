"""Accepted tool outcomes survive interruption before batch delivery finishes."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.history import (
    decode_assistant_replay,
    limit_turns,
    reconstruct_messages_from_entry,
)
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent, ToolResultEvent
from opensquilla.gateway.config import AttachmentsConfig, GatewayConfig, SquillaRouterConfig
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
    ModelCapabilities,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolStart
from opensquilla.provider.image_projection import count_image_blocks
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.types import ProviderReplayState
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.registry import ToolRegistry, ToolSpec
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


class _ToolProvider:
    provider_name = "synthetic"

    def __init__(self, count: int = 1):
        self.count = count
        self.calls = 0

    async def chat(self, messages, tools=None, config=None):
        self.calls += 1
        assert self.calls == 1, "a completed tool must not be rerun during recovery"
        for index in range(self.count):
            tool_id = f"call-{index}"
            yield ProviderToolStart(tool_use_id=tool_id, tool_name="read_file")
            yield ProviderToolEnd(tool_use_id=tool_id, tool_name="read_file", arguments={})
        yield ProviderDone(
            stop_reason="tool_use",
            reasoning_content="accepted reasoning",
            provider_replay=ProviderReplayState(
                protocol="openai_chat_completions",
                source="synthetic-origin",
                model="synthetic-model",
                reasoning_details=[{"type": "reasoning.encrypted", "data": "dummy-state"}],
            ),
        )


def _agent(provider, handler, **config):
    return Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2, max_provider_retries=0, **config),
        tool_definitions=[
            ToolDefinition(
                name="read_file",
                description="Pure synthetic fixture",
                input_schema=ToolInputSchema(properties={}),
            )
        ],
        tool_handler=handler,
    )


def _results(messages):
    return [
        block
        for message in messages
        if isinstance(message.content, list)
        for block in message.content
        if isinstance(block, ContentBlockToolResult)
    ]


@pytest.mark.asyncio
async def test_replay_snapshot_is_complete_at_first_public_tool_result():
    executed = []

    async def handler(call):
        executed.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="recorded outcome"
        )

    provider = _ToolProvider()
    agent = _agent(provider, handler)
    stream = agent.run_turn("synthetic request")
    async for event in stream:
        if isinstance(event, ToolResultEvent):
            before_close = agent.current_assistant_replay()
            break
    else:
        pytest.fail("tool result was not delivered")
    await stream.aclose()
    saved = agent.current_assistant_replay()
    assert saved == before_close
    messages = decode_assistant_replay(saved)
    assert messages[0].provider_replay.reasoning_details[0]["data"] == "dummy-state"
    assert [result.content for result in _results(messages)] == ["recorded outcome"]
    assert (
        reconstruct_messages_from_entry("assistant", "", None, assistant_replay=saved) == messages
    )
    assert executed == ["call-0"]
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_partial_parallel_batch_keeps_completed_result_after_cancellation():
    first_finished = asyncio.Event()
    second_started = asyncio.Event()
    executed = []

    async def handler(call):
        executed.append(call.tool_use_id)
        if call.tool_use_id == "call-1":
            second_started.set()
            await asyncio.Event().wait()
        await second_started.wait()
        first_finished.set()
        return ToolResult(
            tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="first recorded outcome"
        )

    provider = _ToolProvider(count=2)
    agent = _agent(provider, handler)

    async def consume():
        async for _event in agent.run_turn("synthetic concurrent request"):
            pass

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(first_finished.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        saved = agent.current_assistant_replay()
        raw = decode_assistant_replay(saved)
        assert [result.tool_use_id for result in _results(raw)] == ["call-0"]
        assert _results(raw)[0].content == "first recorded outcome"
        assert raw[0].provider_replay.reasoning_details[0]["data"] == "dummy-state"
        restored = reconstruct_messages_from_entry("assistant", "", None, assistant_replay=saved)
        assert len(restored) == 1 and restored[0].role == "user"
        assert "first recorded outcome" in restored[0].content
        assert 'Completion was not recorded for tool IDs ["call-1"]' in restored[0].content
        assert "Missing results do not establish whether execution occurred" in restored[0].content
        assert not _results(restored)
        assert "dummy-state" not in restored[0].content
        assert executed == ["call-0", "call-1"]
        assert provider.calls == 1
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_tool_error_budget_exit_keeps_result_in_final_envelope():
    async def handler(call):
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="recorded error outcome",
            is_error=True,
        )

    agent = _agent(_ToolProvider(), handler, max_turn_tool_errors=1)
    events = [event async for event in agent.run_turn("synthetic budget request")]
    assert any(
        event.kind == "error" and event.code == "turn_tool_error_budget_exceeded"
        for event in events
    )
    done = next(event for event in events if isinstance(event, DoneEvent))
    messages = decode_assistant_replay(done.assistant_replay)
    results = _results(messages)
    assert len(results) == 1 and results[0].is_error
    assert results[0].content == "recorded error outcome"
    assert agent.current_assistant_replay() == done.assistant_replay


def test_incomplete_batch_projection_preserves_following_native_call_and_media():
    records = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(id="known", name="read_file", input={}),
                ContentBlockToolUse(id="unknown", name="read_file", input={}),
            ],
            reasoning_content="local reasoning",
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="known",
                    content=[
                        {"type": "image", "media_type": "image/png", "data": "synthetic-base64"}
                    ],
                )
            ],
        ),
        Message(
            role="assistant",
            content="later completed answer",
            provider_replay=ProviderReplayState(
                protocol="openai_chat_completions",
                source="synthetic-origin",
                model="synthetic-model",
                reasoning_details=[{"type": "reasoning.encrypted", "data": "later-state"}],
            ),
        ),
    ]
    envelope = {"version": 1, "messages": [record.model_dump(mode="json") for record in records]}
    projected = reconstruct_messages_from_entry("assistant", "", None, assistant_replay=envelope)
    assert len(projected) == 2 and projected[1] == records[2]
    facts, media = projected[0].content
    assert "synthetic-base64" not in facts.text
    assert media.type == "image" and media.data == "synthetic-base64"
    assert "local reasoning" not in facts.text
    assert json.loads(facts.text.split("\n", 1)[1])[1]["content"][0]["tool_use_id"] == "known"
    assert decode_assistant_replay(envelope) == records


class _MediaReplayProvider(_ToolProvider):
    def __init__(self, *, cancel: bool):
        super().__init__()
        self.cancel = cancel
        self.requests = []
        self.second_call_started = asyncio.Event()
        self.current_config = SimpleNamespace(model="synthetic-vision")

    def clone(self):
        return self

    def override_model(self, model):
        self.current_config = SimpleNamespace(model=model)

    def resolve(self):
        return self

    async def chat(self, messages, tools=None, config=None):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if len(self.requests) == 1:
            async for event in super().chat(messages, tools, config):
                yield event
            return
        assert len(self.requests) == 2
        self.second_call_started.set()
        yield ProviderText(text="Synthetic screenshot examined.")
        if self.cancel:
            await asyncio.Event().wait()
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True], ids=["complete", "cancel"])
@pytest.mark.parametrize("persist", [False, True], ids=["no-retention", "default-retention"])
async def test_runner_replay_respects_image_retention_after_sqlite_reload(
    tmp_path,
    monkeypatch,
    cancel,
    persist,
):
    payload = base64.b64encode(b"synthetic-transient-screenshot").decode("ascii")
    path = tmp_path / "sessions.db"
    storage = SessionStorage(str(path))
    await storage.connect()
    manager = SessionManager(storage)
    key = "agent:main:webchat:synthetic-media-replay"
    session = await manager.create(key)
    registry = ToolRegistry()

    async def read_file():
        context = current_tool_context.get()
        assert context is not None
        context.tool_result_media["call-0"] = [{"mime": "image/png", "data": payload}]
        return "recorded screenshot outcome"

    registry.register(
        ToolSpec(name="read_file", description="Pure synthetic fixture", parameters={}),
        read_file,
    )
    catalog = ModelCatalog()
    monkeypatch.setattr(
        catalog,
        "get_capabilities",
        lambda *args, **kwargs: ModelCapabilities(supports_vision=True),
    )
    monkeypatch.setattr(
        catalog, "resolve_deployment_vision_support", lambda *args, **kwargs: "supported"
    )
    provider = _MediaReplayProvider(cancel=cancel)
    runner = TurnRunner(
        provider_selector=provider,
        tool_registry=registry,
        session_manager=manager,
        model_catalog=catalog,
        config=GatewayConfig(
            llm={"provider": "synthetic", "model": "synthetic-vision"},
            attachments=(
                AttachmentsConfig(media_root=str(tmp_path / "media"))
                if persist
                else AttachmentsConfig(
                    persist_transcripts=False, media_root=str(tmp_path / "media")
                )
            ),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )
    events = []

    async def consume():
        async for event in runner.run(
            "Examine the synthetic screenshot.",
            key,
            tool_context=tool_context,
            model="synthetic-vision",
            max_iterations=2,
            history_has_persisted_user=False,
            no_memory_capture=True,
            expected_session_id=session.session_id,
            expected_session_epoch=session.epoch,
        ):
            events.append(event)

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(provider.second_call_started.wait(), timeout=5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
        assert not any(event.kind == "error" for event in events)
        # Retention is a disk policy; even the no-retention turn sent the real
        # typed image and untouched native state to its vision-capable consumer.
        outbound = provider.requests[1]
        assert count_image_blocks(outbound) == 1
        assert payload in json.dumps([message.model_dump() for message in outbound])
        original_assistant = next(message for message in outbound if message.role == "assistant")
        assert original_assistant.provider_replay.source == "synthetic-origin"

        await storage.close()
        storage = SessionStorage(str(path))
        await storage.connect()
        manager = SessionManager(storage)
        row = next(
            entry for entry in await manager.get_transcript(key) if entry.role == "assistant"
        )
        raw = decode_assistant_replay(row.assistant_replay)
        restored = reconstruct_messages_from_entry(
            row.role,
            row.content,
            row.tool_calls,
            assistant_replay=row.assistant_replay,
        )
        assert restored == raw
        assert raw[0] == original_assistant
        assert _results(raw)[0].tool_use_id == "call-0"
        assert _results(raw)[0].content == "recorded screenshot outcome"
        saved = json.dumps(row.assistant_replay, ensure_ascii=False)
        assert (payload in saved) is persist
        assert count_image_blocks(raw) == int(persist)
        if not persist:
            assert "历史图片不可用" in saved and "请重新上传" in saved
        # Old public transcript fields never contained the tool's image bytes.
        assert payload not in json.dumps([row.content, row.tool_calls])
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await storage.close()


_PRIVATE_PATH = "/synthetic/workspace/private.txt"
_PRIVATE_OUTPUT = "synthetic private tool output"
_MEDIA_BYTES = base64.b64encode(b"synthetic-private-tool-image").decode("ascii")


def _replay(*, orphan: bool, media: bool) -> dict:
    call = Message(
        role="assistant",
        content="Earlier visible answer." if orphan else [
            ContentBlockToolUse(
                id="finished", name="read_file", input={"path": _PRIVATE_PATH},
            ),
            ContentBlockToolUse(
                id="unfinished", name="exec_command", input={"cmd": f"cat {_PRIVATE_PATH}"},
            ),
        ],
    )
    result = Message(
        role="user",
        content=[ContentBlockToolResult(
            tool_use_id="finished",
            content=[
                {"type": "text", "text": f"{_PRIVATE_OUTPUT}: {_PRIVATE_PATH}"},
                {"type": "image", "media_type": "image/png", "data": _MEDIA_BYTES},
            ] if media else f"{_PRIVATE_OUTPUT}: {_PRIVATE_PATH}",
        )],
    )
    return {"version": 1, "messages": [message.model_dump() for message in [call, result]]}


@pytest.mark.parametrize("orphan", [False, True], ids=["interrupted-batch", "orphan-outcome"])
@pytest.mark.parametrize("media", [False, True], ids=["text", "media"])
def test_recorded_tool_evidence_does_not_count_as_a_user_turn(orphan, media):
    envelope = _replay(orphan=orphan, media=media)
    restored = reconstruct_messages_from_entry(
        "assistant", "Earlier visible answer.", None, assistant_replay=envelope,
    )
    current_turn = [Message(role="user", content="Current historical request."), *restored]
    history = [
        Message(role="user", content="Older request."),
        Message(role="assistant", content="Older answer."),
        *current_turn,
    ]

    assert limit_turns(history, 1) == current_turn
    assert envelope == _replay(orphan=orphan, media=media)


class _RecordingProvider:
    provider_name = "synthetic"

    def __init__(self):
        self.requests = []

    async def chat(self, messages, tools=None, config=None):
        self.requests.append([message.model_copy(deep=True) for message in messages])
        yield ProviderText(text="Synthetic annotation applied.")
        yield ProviderDone(stop_reason="end_turn")


@pytest.mark.asyncio
@pytest.mark.parametrize("orphan", [False, True], ids=["interrupted-batch", "orphan-outcome"])
@pytest.mark.parametrize("media", [False, True], ids=["text", "media"])
@pytest.mark.parametrize("restricted", [False, True], ids=["ordinary", "restricted"])
async def test_reloaded_tool_evidence_is_filtered_from_restricted_provider_requests(
    tmp_path, monkeypatch, orphan, media, restricted,
):
    monkeypatch.setenv("OPENSQUILLA_TURN_CALL_LOG", "0")
    path = tmp_path / "sessions.db"
    key = "agent:main:webchat:synthetic-restricted-replay"
    envelope = _replay(orphan=orphan, media=media)
    storage = SessionStorage(str(path))
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        await manager.create(key)
        await manager.append_message(key, "user", "Earlier ordinary request.")
        await manager.append_message(
            key, "assistant", "Earlier visible answer.", assistant_replay=envelope,
        )
    finally:
        await storage.close()

    storage = SessionStorage(str(path))
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
        provider = _RecordingProvider()
        agent = Agent(
            provider=provider,
            config=AgentConfig(
                restricted_turn=restricted,
                context_window_tokens=200_000,
                materialize_historical_attachments=False,
                preserve_historical_images=True,
                model_capabilities=ModelCapabilities(supports_vision=True),
                model_vision_support="supported",
            ),
            tool_context=ToolContext(
                exclusive_tools={"document_inspect"}, allowed_tools={"document_inspect"},
            ) if restricted else None,
        )
        await runner._load_history(
            agent, key, trim_last_user=False, restricted_turn=restricted,
        )
        events = [event async for event in agent.run_turn("Apply the synthetic annotation.")]

        assert not [event for event in events if event.kind == "error"]
        assert len(provider.requests) == 1
        payload = json.dumps([message.model_dump() for message in provider.requests[0]])
        assert "Earlier ordinary request." in payload
        assert "Apply the synthetic annotation." in payload
        assert (_PRIVATE_PATH in payload) is not restricted
        assert (_PRIVATE_OUTPUT in payload) is not restricted
        assert (_MEDIA_BYTES in payload) is (media and not restricted)
        if orphan:
            assert "Earlier visible answer." in payload
        # Request projection must not erase the durable record for normal turns.
        transcript = await manager.get_transcript(key)
        assert transcript[1].assistant_replay == envelope
    finally:
        await storage.close()
