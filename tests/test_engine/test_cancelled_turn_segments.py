"""Cancelled turns persist the same segment timeline a completed turn would."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import TextDeltaEvent
from opensquilla.gateway.config import AttachmentsConfig, GatewayConfig, SquillaRouterConfig
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import Message, ModelInfo
from opensquilla.provider import ReasoningDeltaEvent as ProviderReasoning
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.registry import ToolRegistry, ToolSpec
from opensquilla.tools.types import CallerKind, ToolContext

PARTIAL_ANSWER = "Based on the lookup, the answer is 42 and the reasoning is as follows"
PARTIAL_ACTIVITY = "I will inspect another source before answering."


class _ToolThenHangingTextProvider:
    """Call 1: emits one tool call. Call 2: streams text, then hangs forever."""

    provider_name = "test"

    def __init__(self) -> None:
        self.calls = 0
        self.model = "test/model"

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        self.calls += 1
        return self._stream(self.calls)

    async def _stream(self, call_number: int) -> AsyncIterator[Any]:
        if call_number == 1:
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="lookup")
            yield ProviderToolUseEnd(tool_use_id="tool-1", tool_name="lookup", arguments={})
            yield ProviderDone(stop_reason="tool_use", input_tokens=1, output_tokens=1)
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
async def test_cancelled_turn_persists_trailing_text_segment(tmp_path) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:cancel-trailing-text"
    await storage.initialize_usage_ledger(1)
    await manager.create(session_key)
    usage_sink = SessionUsageEventSink(storage, start_retry_delays=(), retry_delays=())
    runner = TurnRunner(
        provider_selector=_ProviderSelector(_ToolThenHangingTextProvider()),
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
