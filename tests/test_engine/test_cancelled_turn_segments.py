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
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import Message, ModelInfo
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.registry import ToolRegistry, ToolSpec
from opensquilla.tools.types import CallerKind, ToolContext

PARTIAL_ANSWER = "Based on the lookup, the answer is 42 and the reasoning is as follows"


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
    await manager.create(session_key)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(_ToolThenHangingTextProvider()),
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
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
