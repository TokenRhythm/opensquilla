"""End-to-end Silent Reply coverage through the shared TurnRunner."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.gateway.config import AttachmentsConfig, GatewayConfig, SquillaRouterConfig
from opensquilla.provider import ContentBlockText, Message, ModelInfo
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.silent_reply import (
    SILENT_REPLY_NOT_ALLOWED_CODE,
    SILENT_REPLY_NOT_ALLOWED_MESSAGE,
)
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import CallerKind, ToolContext, ToolSpec


class _ScriptedProvider:
    provider_name = "test"

    def __init__(self, scripts: list[list[str]]) -> None:
        self.model = "test/model"
        self.scripts = scripts
        self.calls: list[list[Message]] = []

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        call_index = len(self.calls)
        self.calls.append(list(messages))
        return self._stream(self.scripts[call_index])

    async def _stream(self, chunks: list[str]) -> AsyncIterator[Any]:
        for chunk in chunks:
            yield ProviderText(text=chunk)
        yield ProviderDone(stop_reason="end_turn", input_tokens=3, output_tokens=2)

    async def list_models(self) -> list[ModelInfo]:
        return []


class _ErroringProvider(_ScriptedProvider):
    def __init__(self, partial_text: str) -> None:
        super().__init__([[partial_text]])

    async def _stream(self, chunks: list[str]) -> AsyncIterator[Any]:
        yield ProviderText(text=chunks[0])
        yield ProviderError(message="provider failed", code="provider_error")


class _ToolBoundaryProvider(_ScriptedProvider):
    def __init__(self, marker_position: str) -> None:
        super().__init__([])
        self.marker_position = marker_position

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        call_index = len(self.calls)
        self.calls.append(list(messages))
        return self._tool_stream(call_index)

    async def _tool_stream(self, call_index: int) -> AsyncIterator[Any]:
        if call_index == 0:
            first_text = "NO_REPLY" if self.marker_position == "before" else "Visible body."
            yield ProviderText(text=first_text)
            yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="lookup")
            yield ProviderToolUseEnd(
                tool_use_id="tool-1",
                tool_name="lookup",
                arguments={},
            )
            yield ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=2)
            return

        final_text = "Visible body." if self.marker_position == "before" else "NO_REPLY"
        yield ProviderText(text=final_text)
        yield ProviderDone(stop_reason="end_turn", input_tokens=3, output_tokens=2)


class _SelectorClone:
    current_config = SimpleNamespace(model="test/model")

    def __init__(self, provider: _ScriptedProvider) -> None:
        self.provider = provider

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)
        self.provider.model = model

    def resolve(self) -> _ScriptedProvider:
        return self.provider


class _ProviderSelector:
    def __init__(self, provider: _ScriptedProvider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


async def _runtime_stack(tmp_path, scripts: list[list[str]]):
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:silent-reply-runtime"
    await manager.create(session_key)
    provider = _ScriptedProvider(scripts)
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        tool_registry=ToolRegistry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )
    return storage, manager, provider, runner, context, session_key


async def _tool_boundary_runtime_stack(tmp_path, marker_position: str):
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = f"agent:main:webchat:silent-reply-tool-{marker_position}"
    await manager.create(session_key)
    provider = _ToolBoundaryProvider(marker_position)
    registry = ToolRegistry()

    async def lookup() -> str:
        return "ok"

    registry.register(
        ToolSpec(name="lookup", description="Synthetic lookup.", parameters={}),
        lookup,
    )
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        tool_registry=registry,
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )
    return storage, manager, runner, context, session_key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "run_kind", "expected_text", "expected_reason"),
    [
        (["NO_", "REPLY"], "goal", "", "no_reply"),
        (["**NO_REPLY**"], "goal", "", "no_reply"),
        (["HEARTBEAT_", "OK"], "heartbeat", "", "heartbeat_ack"),
        (
            ["NO_", "REPLY\r\n", "A real status update."],
            "goal",
            "A real status update.",
            None,
        ),
    ],
)
async def test_system_event_runtime_emits_only_canonical_terminal_text(
    tmp_path,
    chunks: list[str],
    run_kind: str,
    expected_text: str,
    expected_reason: str | None,
) -> None:
    storage, manager, _provider, runner, context, session_key = await _runtime_stack(
        tmp_path,
        [chunks],
    )
    try:
        events = [
            event
            async for event in runner.run(
                "internal event",
                session_key,
                tool_context=context,
                history_has_persisted_user=False,
                input_mode="system_event",
                run_kind=run_kind,
                no_memory_capture=True,
            )
        ]

        text_events = [event.text for event in events if isinstance(event, TextDeltaEvent)]
        assert text_events == ([expected_text] if expected_text else [])
        done = next(event for event in events if isinstance(event, DoneEvent))
        assert done.text == expected_text
        assert done.text_snapshot == expected_text
        assert done.delivery == ("visible" if expected_text else "suppressed")
        assert done.suppression_reason == expected_reason

        transcript = await manager.get_transcript(session_key)
        assistants = [entry for entry in transcript if entry.role == "assistant"]
        if expected_text:
            assert [entry.content for entry in assistants] == [expected_text]
        else:
            assert assistants == []
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [
        list("NO_REPLY"),
        ["HEARTBEAT_", "OK"],
    ],
)
async def test_human_sentinel_runtime_emits_error_and_retains_usage(
    tmp_path,
    chunks: list[str],
) -> None:
    storage, manager, _provider, runner, context, session_key = await _runtime_stack(
        tmp_path,
        [chunks],
    )
    try:
        events = [
            event
            async for event in runner.run(
                "human request",
                session_key,
                tool_context=context,
                history_has_persisted_user=False,
                input_mode="user",
                no_memory_capture=True,
            )
        ]

        assert not any(isinstance(event, TextDeltaEvent) for event in events)
        assert not any(isinstance(event, DoneEvent) for event in events)
        errors = [event for event in events if isinstance(event, ErrorEvent)]
        assert len(errors) == 1
        assert errors[0].code == SILENT_REPLY_NOT_ALLOWED_CODE
        assert errors[0].message == SILENT_REPLY_NOT_ALLOWED_MESSAGE

        session = await manager.get_session(session_key)
        assert session is not None
        assert session.input_tokens == 3
        assert session.output_tokens == 2
        assert session.total_tokens == 5
        transcript = await manager.get_transcript(session_key)
        assert [entry for entry in transcript if entry.role == "assistant"] == []
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_human_mixed_sentinel_runtime_remains_visible(tmp_path) -> None:
    body = "A normal human-facing reply."
    source = f"NO_REPLY\n{body}"
    storage, manager, _provider, runner, context, session_key = await _runtime_stack(
        tmp_path,
        [["NO_", "REPLY\n", body]],
    )
    try:
        events = [
            event
            async for event in runner.run(
                "human request",
                session_key,
                tool_context=context,
                history_has_persisted_user=False,
                input_mode="user",
                no_memory_capture=True,
            )
        ]

        assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
            source
        ]
        done = next(event for event in events if isinstance(event, DoneEvent))
        assert done.text_snapshot == source
        assert done.delivery == "visible"
        assert not any(isinstance(event, ErrorEvent) for event in events)
        transcript = await manager.get_transcript(session_key)
        assistants = [entry for entry in transcript if entry.role == "assistant"]
        assert [entry.content for entry in assistants] == [source]
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marker_position", "expected_segment_types"),
    [
        ("before", ["tool_use", "tool_result", "text"]),
        ("after", ["text", "tool_use", "tool_result"]),
    ],
)
async def test_tool_boundary_marker_live_done_and_transcript_are_identical(
    tmp_path,
    marker_position: str,
    expected_segment_types: list[str],
) -> None:
    storage, manager, runner, context, session_key = await _tool_boundary_runtime_stack(
        tmp_path,
        marker_position,
    )
    try:
        events = [
            event
            async for event in runner.run(
                "internal event",
                session_key,
                tool_context=context,
                history_has_persisted_user=False,
                input_mode="system_event",
                run_kind="goal",
                no_memory_capture=True,
            )
        ]

        visible_text = "Visible body."
        assert [
            event.text for event in events if isinstance(event, TextDeltaEvent)
        ] == [visible_text]
        done = next(event for event in events if isinstance(event, DoneEvent))
        assert done.text == visible_text
        assert done.text_snapshot == visible_text
        assert done.delivery == "visible"

        transcript = await manager.get_transcript(session_key)
        assistants = [entry for entry in transcript if entry.role == "assistant"]
        assert len(assistants) == 1
        assistant = assistants[0]
        assert assistant.content == done.text
        assert [segment["type"] for segment in assistant.tool_calls or []] == (
            expected_segment_types
        )
        assert "NO_REPLY" not in str(assistant.tool_calls)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_canonical_body_is_clean_in_next_provider_history(tmp_path) -> None:
    body = "A real status update."
    storage, _manager, provider, runner, context, session_key = await _runtime_stack(
        tmp_path,
        [["NO_REPLY\n", body], ["Follow-up complete."]],
    )
    try:
        async for _event in runner.run(
            "internal event",
            session_key,
            tool_context=context,
            history_has_persisted_user=False,
            input_mode="system_event",
            run_kind="goal",
            no_memory_capture=True,
        ):
            pass

        async for _event in runner.run(
            "continue",
            session_key,
            tool_context=context,
            history_has_persisted_user=False,
            no_memory_capture=True,
        ):
            pass

        assert len(provider.calls) == 2
        replayed = provider.calls[1]
        assistant_texts = [
            message.content
            if isinstance(message.content, str)
            else "".join(
                block.text
                for block in message.content
                if isinstance(block, ContentBlockText)
            )
            for message in replayed
            if message.role == "assistant"
        ]
        assert body in assistant_texts
        assert "NO_REPLY" not in str(replayed)
        assert "HEARTBEAT_OK" not in str(replayed)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_provider_error_does_not_persist_incomplete_sentinel(tmp_path) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:silent-reply-error"
    await manager.create(session_key)
    provider = _ErroringProvider("NO_REP")
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        tool_registry=ToolRegistry(),
        session_manager=manager,
        config=GatewayConfig(
            attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
            squilla_router=SquillaRouterConfig(enabled=False),
        ),
    )
    context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )
    try:
        events = [
            event
            async for event in runner.run(
                "internal event",
                session_key,
                tool_context=context,
                history_has_persisted_user=False,
                input_mode="system_event",
                run_kind="goal",
                max_provider_retries=0,
                no_memory_capture=True,
            )
        ]

        assert not any(isinstance(event, TextDeltaEvent) for event in events)
        transcript = await manager.get_transcript(session_key)
        assert [entry for entry in transcript if entry.role == "assistant"] == []
    finally:
        await storage.close()
