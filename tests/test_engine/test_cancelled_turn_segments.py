"""Cancelled turns persist the same segment timeline a completed turn would."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import TextDeltaEvent, ToolResultEvent
from opensquilla.gateway.config import AttachmentsConfig, GatewayConfig, SquillaRouterConfig
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.gateway.user_input_broker import StructuredUserInputBroker
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import Message, ModelInfo
from opensquilla.provider import ReasoningDeltaEvent as ProviderReasoning
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.provider.types import ProviderReplayState
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.registry import ToolRegistry, ToolSpec
from opensquilla.tools.types import CallerKind, ToolContext

PARTIAL_ANSWER = "Based on the lookup, the answer is 42 and the reasoning is as follows"
PARTIAL_ACTIVITY = "I will inspect another source before answering."


@pytest.fixture(autouse=True)
def _offline_token_estimation(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cancellation tests must not wait for tokenizer downloads.
    monkeypatch.setattr("opensquilla.token_estimation._get_encoding", lambda: None)


class _ToolThenHangingTextProvider:
    """Call 1: emits one tool call. Call 2: streams text, then hangs forever."""

    provider_name = "test"

    def __init__(self, *, native_replay: bool = False) -> None:
        self.calls = 0
        self.model = "test/model"
        self.native_replay = native_replay

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        self.calls += 1
        return self._stream(self.calls)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="lookup")
            yield ProviderToolUseEnd(tool_use_id="tool-1", tool_name="lookup", arguments={})
            yield ProviderDone(
                stop_reason="tool_use", input_tokens=1, output_tokens=1,
                reasoning_content="accepted tool reasoning" if self.native_replay else None,
                provider_replay=(
                    ProviderReplayState(
                        protocol="openai_chat_completions", source="synthetic-origin",
                        model="test/model", reasoning_details=[
                            {"type": "reasoning.encrypted", "data": "synthetic-accepted-state"}
                        ],
                    )
                    if self.native_replay else None
                ),
            )
            return
        yield ProviderText(text=PARTIAL_ANSWER)
        await asyncio.Event().wait()

    async def list_models(self) -> list[ModelInfo]:
        return []


class _ToolThenCompletedTextProvider(_ToolThenHangingTextProvider):
    """Complete the answer stream so cancellation can happen in finalization."""

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="lookup")
            yield ProviderToolUseEnd(tool_use_id="tool-1", tool_name="lookup", arguments={})
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=PARTIAL_ANSWER)
        yield ProviderDone(stop_reason="end_turn", input_tokens=1, output_tokens=1)


class _HangingIntermediateTextProvider(_ToolThenHangingTextProvider):
    """Start a tool and then stream work narration before cancellation."""

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        del call_number
        yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="lookup")
        yield ProviderToolUseEnd(
            tool_use_id="tool-1",
            tool_name="lookup",
            arguments={},
        )
        yield ProviderText(text=PARTIAL_ACTIVITY)
        await asyncio.Event().wait()


class _HangingReasoningProvider:
    """Stream visible reasoning without answer text, then wait for Stop."""

    provider_name = "test"

    def __init__(self) -> None:
        self.model = "test/model"
        self.reasoning_consumed = asyncio.Event()

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderReasoning(text="partial reasoning")
        self.reasoning_consumed.set()
        await asyncio.Event().wait()

    async def list_models(self) -> list[ModelInfo]:
        return []


class _HangingSystemEventProvider:
    """Emit one internal text chunk, then wait so the turn can be stopped."""

    provider_name = "test"

    def __init__(self, text: str) -> None:
        self.text = text
        self.model = "test/model"
        self.text_consumed = asyncio.Event()

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text=self.text)
        # Set only when the consumer requests the next provider event, proving
        # the shared stream stage has already accumulated the held delta.
        self.text_consumed.set()
        await asyncio.Event().wait()

    async def list_models(self) -> list[ModelInfo]:
        return []


class _ToolThenHangingSilentProvider(_ToolThenHangingTextProvider):
    """Complete a tool round, then emit a sentinel and wait for Stop."""

    def __init__(self) -> None:
        super().__init__()
        self.text_consumed = asyncio.Event()

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="lookup")
            yield ProviderToolUseEnd(tool_use_id="tool-1", tool_name="lookup", arguments={})
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text="NO_REPLY")
        self.text_consumed.set()
        await asyncio.Event().wait()


class _TextToolTextHangingProvider(_ToolThenHangingTextProvider):
    """Put caller-provided text on both sides of a completed tool boundary."""

    def __init__(self, before_tool: str, after_tool: str) -> None:
        super().__init__()
        self.before_tool = before_tool
        self.after_tool = after_tool
        self.text_consumed = asyncio.Event()

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderText(text=self.before_tool)
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="lookup")
            yield ProviderToolUseEnd(tool_use_id="tool-1", tool_name="lookup", arguments={})
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
            return
        yield ProviderText(text=self.after_tool)
        self.text_consumed.set()
        await asyncio.Event().wait()


class _SelectorClone:
    current_config = SimpleNamespace(model="test/model")

    def __init__(self, provider: _ToolThenHangingTextProvider) -> None:
        self.provider = provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)
        self.provider.model = model

    def resolve(self) -> _ToolThenHangingTextProvider:
        return self.provider


class _ProviderSelector:
    def __init__(self, provider: _ToolThenHangingTextProvider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def lookup() -> str:
        return "lookup-result-payload"

    registry.register(
        ToolSpec(name="lookup", description="Look something up", parameters={}),
        lookup,
    )
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [False, True], ids=["pending", "answered"])
@pytest.mark.parametrize("repeat_cancel", [False, True], ids=["single-stop", "repeated-stop"])
async def test_cancelled_question_projects_terminal_result_into_next_provider_history(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    answered: bool,
    repeat_cancel: bool,
) -> None:
    from opensquilla.tools.builtin.plan_control import request_user_input

    question = "Which synthetic scope?"

    class Provider(_ToolThenHangingTextProvider):
        def __init__(self) -> None:
            super().__init__(native_replay=True)
            self.requests: list[list[Message]] = []
            self.answer_processing = asyncio.Event()

        def chat(self, messages, tools=None, config=None):
            self.requests.append([message.model_copy(deep=True) for message in messages])
            return super().chat(messages, tools, config)

        async def _stream(self, call_number: int) -> AsyncIterator[Any]:
            if call_number == 1:
                yield ProviderToolUseStart(tool_use_id="question-1", tool_name="request_user_input")
                yield ProviderToolUseEnd(
                    tool_use_id="question-1", tool_name="request_user_input",
                    arguments={"questions": [{"id": "scope", "question": question}]},
                )
                yield ProviderDone(
                    stop_reason="tool_use", input_tokens=1, output_tokens=1,
                    provider_replay=ProviderReplayState(
                        protocol="openai_chat_completions", source="synthetic-origin",
                        model="test/model", reasoning_details=[
                            {"type": "reasoning.encrypted", "data": "synthetic-question-state"},
                        ],
                    ),
                )
            elif answered and call_number == 2:
                self.answer_processing.set()
                await asyncio.Event().wait()
            else:
                yield ProviderText(text="The independent task is complete.")
                yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    storage = await SessionStorage.open(str(tmp_path / "cancelled-question.sqlite"))
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:cancelled-question"
    session = await manager.create(session_key)
    provider = Provider()
    broker = StructuredUserInputBroker()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="request_user_input", description="Ask for input", parameters={}),
        request_user_input,
    )
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider), tool_registry=registry,
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    context = ToolContext(
        is_owner=True, caller_kind=CallerKind.WEB, workspace_dir=str(tmp_path),
        session_key=session_key, task_id="question-task", collaboration_mode="plan",
        user_input_provider=broker,
    )
    published = asyncio.Event()
    persisting = asyncio.Event()
    release_persist = asyncio.Event()
    original_append = runner._append_session_message

    async def append(key, **kwargs):
        if kwargs.get("role") == "assistant" and not persisting.is_set():
            persisting.set()
            await release_persist.wait()
        return await original_append(key, **kwargs)

    monkeypatch.setattr(runner, "_append_session_message", append)

    async def consume(message: str) -> None:
        async for event in runner.run(
            message, session_key, tool_context=context, history_has_persisted_user=False,
            no_memory_capture=True, expected_session_id=session.session_id,
            expected_session_epoch=session.epoch,
        ):
            if isinstance(event, ToolResultEvent):
                if json.loads(event.result).get("status") == "input_required":
                    published.set()

    task = asyncio.create_task(consume("Ask which scope to investigate."))
    try:
        await asyncio.wait_for(published.wait(), 5)
        pending = broker.pending_for_session(session_key)[0]
        if answered:
            broker.resolve(
                session_key=session_key, request_id=pending["request_id"],
                fields={"scope": "synthetic scope"},
            )
            await asyncio.wait_for(provider.answer_processing.wait(), 5)
        task.cancel()
        await asyncio.wait_for(persisting.wait(), 5)
        if repeat_cancel:
            task.cancel()
        release_persist.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert broker.pending_for_session(session_key) == []

        context.task_id = "next-task"
        context.collaboration_mode = "default"
        await consume("Perform a new independent task.")
        # Assert the actual next provider request, not only the UI projection.
        blocks = [
            block for message in provider.requests[-1] if isinstance(message.content, list)
            for block in message.content
        ]
        result = next(
            block for block in blocks
            if block.type == "tool_result" and block.tool_use_id == "question-1"
        )
        payload = json.loads(result.content)
        assert payload["status"] == ("answered" if answered else "cancelled")
        assert payload["paused"] is False
        assert payload["request_id"] == pending["request_id"]
        assert any(
            block.type == "tool_use" and block.id == "question-1"
            and block.input["questions"][0]["question"] == question
            for block in blocks
        )
        transcript = await manager.get_transcript(session_key)
        original = next(entry for entry in transcript if entry.role == "assistant")
        segment = next(
            row for row in original.tool_calls or []
            if row.get("type") == "tool_result" and row.get("tool_use_id") == "question-1"
        )
        assert json.loads(segment["result"]) == payload
        assert segment["user_input_request"]["questions"][0]["question"] == question
        assert segment["user_input_request"]["request_id"] == pending["request_id"]
        assert original.assistant_replay["messages"][0]["provider_replay"]["reasoning_details"] == [
            {"type": "reasoning.encrypted", "data": "synthetic-question-state"},
        ]
    finally:
        release_persist.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("native_replay", [False, True])
async def test_cancelled_turn_persists_trailing_text_segment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    native_replay: bool,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:cancel-trailing-text"
    await storage.initialize_usage_ledger(1)
    session = await manager.create(session_key)
    append_message = AsyncMock(wraps=manager.append_message)
    monkeypatch.setattr(manager, "append_message", append_message)
    reconcile_usage = AsyncMock(
        wraps=storage.reconcile_session_usage_totals_from_ledger
    )
    monkeypatch.setattr(
        storage,
        "reconcile_session_usage_totals_from_ledger",
        reconcile_usage,
    )
    usage_sink = SessionUsageEventSink(storage, start_retry_delays=(), retry_delays=())
    runner = TurnRunner(
        provider_selector=_ProviderSelector(_ToolThenHangingTextProvider(native_replay=native_replay)),
        tool_registry=_registry(),
        session_manager=manager,
        usage_event_sink=usage_sink,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )
    partial_seen = asyncio.Event()

    async def _consume() -> None:
        async for event in runner.run(
            "look it up and explain",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            no_memory_capture=True,
            expected_session_id=session.session_id,
            expected_session_epoch=session.epoch,
        ):
            if isinstance(event, TextDeltaEvent) and PARTIAL_ANSWER in (event.text or ""):
                partial_seen.set()

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(partial_seen.wait(), timeout=5.0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        transcript = await manager.get_transcript(session_key)
        assistants = [entry for entry in transcript if entry.role == "assistant"]
        assert assistants
        assistant = assistants[-1]
        assert PARTIAL_ANSWER in assistant.content
        assert "[interrupted]" not in assistant.content
        assert assistant.assistant_replay is not None
        replay = [Message.model_validate(item) for item in assistant.assistant_replay["messages"]]
        replay_assistants = [message for message in replay if message.role == "assistant"]
        assert len(replay_assistants) == 1
        assert PARTIAL_ANSWER not in str(assistant.assistant_replay)
        assert any("lookup-result-payload" in str(message.content) for message in replay)
        if native_replay:
            assert replay_assistants[0].reasoning_content == "accepted tool reasoning"
            assert replay_assistants[0].provider_replay is not None
            assert replay_assistants[0].provider_replay.reasoning_details == [
                {"type": "reasoning.encrypted", "data": "synthetic-accepted-state"}
            ]

        segments = assistant.tool_calls or []
        segment_types = [str(seg.get("type")) for seg in segments if isinstance(seg, dict)]
        assert "tool_use" in segment_types

        # Transcript-backed views render from the segment timeline, so the text
        # streamed after the last tool boundary must survive as a text segment.
        text_segments = [
            seg for seg in segments if isinstance(seg, dict) and seg.get("type") == "text"
        ]
        assert any(PARTIAL_ANSWER in str(seg.get("text", "")) for seg in text_segments)

        assert assistant.turn_usage is not None
        assert assistant.turn_usage["input_tokens"] == 1
        assert assistant.turn_usage["output_tokens"] == 1
        assert assistant.turn_usage["coverage_status"] == "usage_unknown"
        assert assistant.turn_usage["unknown_usage_events"] == 1
        session = await manager.get_session(session_key)
        assert session is not None
        assert session.input_tokens == 1
        assert session.output_tokens == 1
        assert session.total_tokens == 2
        assert session.missing_cost_entries == 1
        reconcile_usage.assert_awaited()
        assert reconcile_usage.await_args.kwargs["expected_session_id"] == session.session_id
        assert reconcile_usage.await_args.kwargs["expected_epoch"] == session.epoch
        assistant_append = next(
            call
            for call in append_message.await_args_list
            if call.kwargs.get("role") == "assistant"
        )
        assert assistant_append.kwargs["expected_session_id"] == session.session_id
        assert assistant_append.kwargs["expected_session_epoch"] == session.epoch
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await storage.close()


@pytest.mark.asyncio
async def test_cancelled_turn_preserves_intermediate_text_presentation(tmp_path) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:cancel-intermediate-text"
    await manager.create(session_key)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(_HangingIntermediateTextProvider()),
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )
    partial_seen = asyncio.Event()

    async def _consume() -> None:
        async for event in runner.run(
            "inspect it before answering",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            no_memory_capture=True,
        ):
            if isinstance(event, TextDeltaEvent) and PARTIAL_ACTIVITY in (event.text or ""):
                assert event.presentation == "intermediate"
                partial_seen.set()

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(partial_seen.wait(), timeout=5.0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        transcript = await manager.get_transcript(session_key)
        assistant = [entry for entry in transcript if entry.role == "assistant"][-1]
        text_segments = [
            segment
            for segment in (assistant.tool_calls or [])
            if isinstance(segment, dict) and segment.get("type") == "text"
        ]
        assert text_segments == [
            {
                "type": "text",
                "text": PARTIAL_ACTIVITY,
                "presentation": "intermediate",
            }
        ]
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await storage.close()


@pytest.mark.asyncio
async def test_cancelled_reasoning_only_turn_persists_assistant_history(tmp_path) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:cancel-reasoning-only"
    await manager.create(session_key)
    provider = _HangingReasoningProvider()
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),  # type: ignore[arg-type]
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )

    async def _consume() -> None:
        async for _event in runner.run(
            "reason before answering",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            no_memory_capture=True,
        ):
            pass

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(provider.reasoning_consumed.wait(), timeout=5.0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            pytest.fail("Expected CancelledError when awaiting cancelled task")

        transcript = await manager.get_transcript(session_key)
        assistants = [entry for entry in transcript if entry.role == "assistant"]
        assert len(assistants) == 1
        assert assistants[0].content == ""
        assert assistants[0].reasoning_content == "partial reasoning"
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # Cancellation is the expected cleanup outcome for this task.
                pass
        await storage.close()


@pytest.mark.asyncio
async def test_cancel_during_finalizer_does_not_duplicate_text_segment(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:cancel-during-finalizer"
    await manager.create(session_key)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(_ToolThenCompletedTextProvider()),
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    finalizer_entered = asyncio.Event()

    async def _block_finalizer(_input):
        finalizer_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner._turn_finalizer_stage, "run", _block_finalizer)
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )

    async def _consume() -> None:
        async for _event in runner.run(
            "look it up and explain",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            no_memory_capture=True,
        ):
            pass

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(finalizer_entered.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        transcript = await manager.get_transcript(session_key)
        assistant = [entry for entry in transcript if entry.role == "assistant"][-1]
        text_segments = [
            segment
            for segment in (assistant.tool_calls or [])
            if isinstance(segment, dict)
            and segment.get("type") == "text"
            and PARTIAL_ANSWER in str(segment.get("text", ""))
        ]
        assert len(text_segments) == 1
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_marker", ["NO", "NO_REP", "HEARTBEAT_O"])
async def test_cancelled_system_event_does_not_persist_partial_sentinel(
    tmp_path,
    partial_marker: str,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = f"agent:main:webchat:cancel-sentinel-{partial_marker}"
    await storage.initialize_usage_ledger(1)
    await manager.create(session_key)
    provider = _HangingSystemEventProvider(partial_marker)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        tool_registry=_registry(),
        session_manager=manager,
        usage_event_sink=SessionUsageEventSink(
            storage,
            start_retry_delays=(),
            retry_delays=(),
        ),
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )

    async def _consume() -> None:
        async for _event in runner.run(
            "internal continuation",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            input_mode="system_event",
            run_kind="goal",
            no_memory_capture=True,
        ):
            pass

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(provider.text_consumed.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        transcript = await manager.get_transcript(session_key)
        assert [entry for entry in transcript if entry.role == "assistant"] == []
        session = await manager.get_session(session_key)
        assert session is not None
        assert session.total_tokens == 0
        assert session.missing_cost_entries == 1
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "partial_marker",
    ["N", "NO_REP", "HEART", pytest.param("   ", id="whitespace-only")],
)
async def test_cancelled_human_turn_does_not_persist_withheld_sentinel_prefix(
    tmp_path,
    partial_marker: str,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = f"agent:main:webchat:cancel-human-prefix-{partial_marker}"
    await manager.create(session_key)
    provider = _HangingSystemEventProvider(partial_marker)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )

    async def _consume() -> None:
        async for _event in runner.run(
            "ordinary human request",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            input_mode="user",
            no_memory_capture=True,
        ):
            pass

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(provider.text_consumed.wait(), timeout=5.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            _ = await task
        assert task.cancelled()

        transcript = await manager.get_transcript(session_key)
        assert [entry for entry in transcript if entry.role == "assistant"] == []
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                _ = await task
            assert task.cancelled()
        await storage.close()


@pytest.mark.asyncio
async def test_cancelled_human_turn_preserves_released_over_bound_prefix(tmp_path) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:cancel-human-released-prefix"
    await manager.create(session_key)
    released_text = "N" + (" " * 64)
    provider = _HangingSystemEventProvider(released_text)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )
    visible_text: list[str] = []

    async def _consume() -> None:
        async for event in runner.run(
            "ordinary human request",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            input_mode="user",
            no_memory_capture=True,
        ):
            if isinstance(event, TextDeltaEvent):
                visible_text.append(event.text)

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(provider.text_consumed.wait(), timeout=5.0)
        assert "".join(visible_text) == released_text

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            _ = await task
        assert task.cancelled()

        transcript = await manager.get_transcript(session_key)
        assistants = [entry for entry in transcript if entry.role == "assistant"]
        assert len(assistants) == 1
        assert assistants[0].content == "N"
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                _ = await task
            assert task.cancelled()
        await storage.close()


@pytest.mark.asyncio
async def test_cancelled_system_event_persists_body_without_sentinel(tmp_path) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:cancel-mixed-sentinel"
    await manager.create(session_key)
    body = "The external check still needs confirmation."
    provider = _HangingSystemEventProvider(f"NO_REPLY\n{body}")
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )

    async def _consume() -> None:
        async for _event in runner.run(
            "internal continuation",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            input_mode="system_event",
            run_kind="goal",
            no_memory_capture=True,
        ):
            pass

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(provider.text_consumed.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        transcript = await manager.get_transcript(session_key)
        assistants = [entry for entry in transcript if entry.role == "assistant"]
        assert len(assistants) == 1
        assert assistants[0].content == body
        assert "NO_REPLY" not in str(assistants[0].tool_calls)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await storage.close()


@pytest.mark.asyncio
async def test_cancelled_silent_system_event_keeps_completed_tool_audit(tmp_path) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:cancel-sentinel-with-tool"
    await manager.create(session_key)
    provider = _ToolThenHangingSilentProvider()
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )

    async def _consume() -> None:
        async for _event in runner.run(
            "internal continuation",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            input_mode="system_event",
            run_kind="goal",
            no_memory_capture=True,
        ):
            pass

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(provider.text_consumed.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        transcript = await manager.get_transcript(session_key)
        assistants = [entry for entry in transcript if entry.role == "assistant"]
        assert len(assistants) == 1
        assert assistants[0].content == ""
        segments = assistants[0].tool_calls or []
        assert [segment.get("type") for segment in segments] == [
            "tool_use",
            "tool_result",
        ]
        assert "NO_REPLY" not in str(segments)
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before_tool", "after_tool"),
    [
        ("NO_REPLY", "Visible body."),
        ("Visible body.", "NO_REPLY"),
    ],
)
async def test_cancelled_system_event_removes_marker_at_tool_boundary(
    tmp_path,
    before_tool: str,
    after_tool: str,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = f"agent:main:webchat:cancel-tool-boundary-{before_tool}"
    await manager.create(session_key)
    provider = _TextToolTextHangingProvider(before_tool, after_tool)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        tool_registry=_registry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )

    async def _consume() -> None:
        async for _event in runner.run(
            "internal continuation",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            input_mode="system_event",
            run_kind="goal",
            no_memory_capture=True,
        ):
            pass

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(provider.text_consumed.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        transcript = await manager.get_transcript(session_key)
        assistant = [entry for entry in transcript if entry.role == "assistant"][-1]
        assert assistant.content == "Visible body."
        assert "NO_REPLY" not in str(assistant.tool_calls)
        assert [segment.get("type") for segment in assistant.tool_calls or []] == (
            ["tool_use", "tool_result", "text"]
            if before_tool == "NO_REPLY"
            else ["text", "tool_use", "tool_result"]
        )
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await storage.close()
