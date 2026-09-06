from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import ErrorEvent
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.session.storage import StaleEpochError
from opensquilla.tools.types import CallerKind, ToolContext


class _RecordingSessionManager:
    def __init__(self) -> None:
        self.compact_calls: list[tuple[str, int]] = []
        self.messages: list[tuple[str, str, str]] = []
        self.append_calls: list[dict[str, object]] = []

    async def compact(self, session_key: str, budget: int) -> str:
        self.compact_calls.append((session_key, budget))
        return "summary"

    async def append_message(
        self,
        session_key: str,
        *,
        role: str,
        content: str,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
        **kwargs: object,
    ) -> None:
        self.messages.append((session_key, role, content))
        self.append_calls.append(
            {
                "session_key": session_key,
                "role": role,
                "content": content,
                "expected_session_id": expected_session_id,
                "expected_session_epoch": expected_session_epoch,
                **kwargs,
            }
        )

    async def get_transcript(self, session_key: str) -> list[object]:
        del session_key
        return []

    async def get_session(self, session_key: str) -> SimpleNamespace:
        del session_key
        return SimpleNamespace(
            session_id="session-old",
            epoch=7,
            workspace_id=None,
            model_provider=None,
            model_override=None,
            model=None,
            provider_override=None,
        )


class _StaleAssistantSessionManager(_RecordingSessionManager):
    async def append_message(self, session_key: str, **kwargs: Any) -> None:
        await super().append_message(session_key, **kwargs)
        if kwargs.get("role") == "assistant":
            raise StaleEpochError("session owner rotated")


class _SingleReplyProvider:
    provider_name = "test"
    model = "fake-model"

    def chat(self, messages: list[object], tools=None, config=None) -> AsyncIterator[object]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[object]:
        yield ProviderText(text="old task reply")
        yield ProviderDone(stop_reason="end_turn", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[object]:
        return []


class _SelectorClone:
    def __init__(self, provider: _SingleReplyProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model=provider.model)

    def resolve(self) -> _SingleReplyProvider:
        return self.provider

    def override_model(self, model: str) -> None:
        self.current_config.model = model
        self.provider.model = model


class _ProviderSelector:
    def __init__(self, provider: _SingleReplyProvider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


@pytest.mark.asyncio
async def test_provider_request_too_large_error_persistence_does_not_compact_transcript() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message=(
                "The request is too large for the provider context window after "
                "automatic context compaction and payload reduction."
            ),
            code="provider_request_too_large",
        ),
    )

    assert manager.compact_calls == []
    assert manager.messages == [
        (
            "agent:main:webchat:test",
            "system",
            "Error: The request is too large for the provider context window after "
            "automatic context compaction and payload reduction. OpenSquilla "
            "preserved the recoverable state; retry with a narrower request "
            "or a larger-context model.",
        )
    ]


@pytest.mark.asyncio
async def test_provider_output_truncation_error_persistence_uses_terminal_reply() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="Provider output limit reached before completion",
            code="provider_output_truncated",
        ),
    )

    assert manager.compact_calls == []
    assert manager.messages == [
        (
            "agent:main:webchat:test",
            "system",
            "The provider stopped because the output limit was reached before the task finished.",
        )
    ]


@pytest.mark.asyncio
async def test_provider_output_truncation_error_persistence_uses_message_fallback() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    with patch("opensquilla.engine.runtime.log") as log:
        await runner._persist_turn_error(
            "agent:main:webchat:test",
            ErrorEvent(
                message="Provider output limit reached before completion",
                code="agent_error",
            ),
        )

    assert manager.messages == [
        (
            "agent:main:webchat:test",
            "system",
            "The provider stopped because the output limit was reached before the task finished.",
        )
    ]
    log.info.assert_called_once()
    assert log.info.call_args.kwargs["code"] == "provider_output_truncated"
    assert log.info.call_args.kwargs["turn_outcome"]["kind"] == "partial"


@pytest.mark.asyncio
async def test_terminal_reset_error_records_row_without_duplicate_transcript_message() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )
    record_error = AsyncMock(return_value="error-terminal-reset")
    runner._record_turn_error = record_error

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="The fallback model also failed.",
            code="ensemble_fixed_error",
            failure_kind="provider_error",
        ),
        append_transcript=False,
    )

    record_error.assert_awaited_once()
    assert manager.messages == []


@pytest.mark.asyncio
async def test_task_owned_no_provider_error_append_keeps_frozen_session_owner() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    events = [
        event
        async for event in runner.run(
            "hello",
            "agent:main:webchat:test",
            ToolContext(is_owner=True, caller_kind=CallerKind.WEB),
            history_has_persisted_user=False,
            no_memory_capture=True,
            expected_session_id="session-old",
            expected_session_epoch=7,
        )
    ]

    assert any(isinstance(event, ErrorEvent) and event.code == "no_provider" for event in events)
    assert manager.append_calls == [
        {
            "session_key": "agent:main:webchat:test",
            "role": "system",
            "content": "Error: No provider available",
            "expected_session_id": "session-old",
            "expected_session_epoch": 7,
        }
    ]


@pytest.mark.asyncio
async def test_standalone_turn_rejects_stale_owner_before_provider_dispatch() -> None:
    class ReplacementSessionManager(_RecordingSessionManager):
        async def get_session(self, session_key: str) -> SimpleNamespace:
            current = await super().get_session(session_key)
            current.session_id = "session-new"
            current.epoch = 8
            return current

    class RecordingProvider(_SingleReplyProvider):
        def __init__(self) -> None:
            self.chat_calls = 0

        def chat(self, messages, tools=None, config=None):
            self.chat_calls += 1
            return super().chat(messages, tools=tools, config=config)

    manager = ReplacementSessionManager()
    provider = RecordingProvider()
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    with pytest.raises(StaleEpochError, match="before provider dispatch"):
        async for _event in runner.run(
            "hello",
            "agent:main:webchat:test",
            ToolContext(is_owner=True, caller_kind=CallerKind.WEB),
            history_has_persisted_user=False,
            no_memory_capture=True,
            expected_session_id="session-old",
            expected_session_epoch=7,
        ):
            pass

    assert provider.chat_calls == 0
    assert manager.append_calls == []


@pytest.mark.asyncio
async def test_task_owned_context_exhaustion_skips_compaction_and_keeps_owner() -> None:
    manager = _RecordingSessionManager()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    await runner._persist_turn_error(
        "agent:main:webchat:test",
        ErrorEvent(
            message="The accepted turn no longer fits in the current context.",
            code="current_turn_context_exhausted",
        ),
        expected_session_id="session-old",
        expected_session_epoch=7,
    )

    assert manager.compact_calls == []
    assert len(manager.append_calls) == 1
    assert manager.append_calls[0]["session_key"] == "agent:main:webchat:test"
    assert manager.append_calls[0]["role"] == "system"
    assert str(manager.append_calls[0]["content"]).startswith("Error:")
    assert manager.append_calls[0]["expected_session_id"] == "session-old"
    assert manager.append_calls[0]["expected_session_epoch"] == 7


@pytest.mark.asyncio
async def test_stale_finalizer_append_does_not_fall_back_to_unfenced_error_row() -> None:
    manager = _StaleAssistantSessionManager()
    runner = TurnRunner(
        provider_selector=_ProviderSelector(_SingleReplyProvider()),
        session_manager=manager,
        config=SimpleNamespace(context_window_tokens=100_000),
    )

    with pytest.raises(StaleEpochError, match="session owner rotated"):
        async for _event in runner.run(
            "hello",
            "agent:main:webchat:test",
            ToolContext(is_owner=True, caller_kind=CallerKind.WEB),
            history_has_persisted_user=False,
            no_memory_capture=True,
            expected_session_id="session-old",
            expected_session_epoch=7,
        ):
            pass

    assert len(manager.append_calls) == 1
    assert manager.append_calls[0]["role"] == "assistant"
    assert manager.append_calls[0]["expected_session_id"] == "session-old"
    assert manager.append_calls[0]["expected_session_epoch"] == 7
    assert not str(manager.append_calls[0]["content"]).startswith("Error:")
