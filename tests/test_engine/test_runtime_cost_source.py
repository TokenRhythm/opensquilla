"""Integration tests for turn runtime cost source persistence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import Message, ModelInfo
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import CallerKind, ToolContext


class _CostProvider:
    provider_name = "test"

    def __init__(self, done_events: list[ProviderDone]) -> None:
        self._done_events = list(done_events)
        self._model = done_events[0].model if done_events else "claude-opus-4-7"

    @property
    def model(self) -> str:
        return self._model

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator:
        return self._stream()

    async def _stream(self) -> AsyncIterator:
        done = self._done_events.pop(0)
        self._model = done.model
        yield ProviderText(text="ok")
        yield done

    async def list_models(self) -> list[ModelInfo]:
        return []


class _ReasoningOnlyCostProvider(_CostProvider):
    async def _stream(self) -> AsyncIterator:
        done = self._done_events.pop(0)
        self._model = done.model
        yield done


class _SelectorClone:
    def __init__(self, provider: _CostProvider) -> None:
        self.provider = provider
        self.current_config = SimpleNamespace(model=provider.model)

    def override_model(self, model: str) -> None:
        self.current_config = SimpleNamespace(model=model)
        self.provider._model = model

    def resolve(self) -> _CostProvider:
        return self.provider


class _ProviderSelector:
    def __init__(self, provider: _CostProvider) -> None:
        self.provider = provider

    def clone(self) -> _SelectorClone:
        return _SelectorClone(self.provider)


@pytest.mark.asyncio
async def test_runtime_session_id_log_lookup_uses_private_storage_fallback() -> None:
    class _Storage:
        async def get_session(self, session_key: str) -> SimpleNamespace:
            assert session_key == "agent:main:private-storage"
            return SimpleNamespace(session_id="session-123")

    manager = SimpleNamespace(_storage=_Storage())
    runner = TurnRunner(provider_selector=None, session_manager=manager)

    assert (
        await runner._resolve_session_id_for_log("agent:main:private-storage")
        == "session-123"
    )


@pytest.mark.asyncio
async def test_runtime_persists_billed_and_estimated_cost_source_rollup() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:cost-source"
    await manager.create(session_key)
    provider = _CostProvider(
        [
            ProviderDone(
                input_tokens=100,
                output_tokens=10,
                billed_cost=0.004,
                model="claude-opus-4-7",
            ),
            ProviderDone(
                input_tokens=1000,
                output_tokens=0,
                billed_cost=0.0,
                model="claude-opus-4-7",
            ),
        ]
    )
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        session_manager=manager,
    )
    tool_context = ToolContext(is_owner=True, caller_kind=CallerKind.CLI)

    try:
        async for _ in runner.run(
            "first",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            no_memory_capture=True,
        ):
            pass
        first = await manager.get_session(session_key)
        assert first is not None
        assert first.total_cost_usd == pytest.approx(0.004)
        assert first.billed_cost_usd == pytest.approx(0.004)
        assert first.estimated_cost_component_usd == 0.0
        assert first.cost_source == "provider_billed"

        async for _ in runner.run(
            "second",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            no_memory_capture=True,
        ):
            pass
        second = await manager.get_session(session_key)
        assert second is not None
        assert second.total_cost_usd == pytest.approx(0.019)
        assert second.billed_cost_usd == pytest.approx(0.004)
        assert second.estimated_cost_component_usd == pytest.approx(0.015)
        assert second.cost_source == "mixed"
        assert second.estimated_cost_usd == pytest.approx(0.019)
        assert second.missing_cost_entries == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_runtime_reuses_supplied_assistant_id_without_duplicate_transcript_rows() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:assistant-identity-runtime"
    await manager.create(session_key)
    provider = _CostProvider(
        [
            ProviderDone(model="claude-opus-4-7"),
            ProviderDone(model="claude-opus-4-7"),
        ]
    )
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        session_manager=manager,
    )
    tool_context = ToolContext(is_owner=True, caller_kind=CallerKind.CLI)

    try:
        for message in ("first", "second"):
            async for _event in runner.run(
                message,
                session_key,
                tool_context=tool_context,
                history_has_persisted_user=False,
                no_memory_capture=True,
                assistant_message_id="assistant-runtime-fixed",
            ):
                pass

        entries = await manager.get_transcript(session_key)
        assert len(entries) == 1
        assert entries[0].message_id == "assistant-runtime-fixed"
        assert entries[0].content == "ok"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_runtime_persists_usage_before_yielding_terminal_error() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:error-usage"
    await manager.create(session_key)
    provider = _ReasoningOnlyCostProvider(
        [
            ProviderDone(
                input_tokens=40,
                output_tokens=2,
                reasoning_tokens=2,
                reasoning_content="internal reasoning",
                billed_cost=0.004,
                model="claude-opus-4-7",
            )
        ]
    )
    runner = TurnRunner(
        provider_selector=_ProviderSelector(provider),
        session_manager=manager,
    )
    tool_context = ToolContext(is_owner=True, caller_kind=CallerKind.CLI)

    try:
        events = []
        async for event in runner.run(
            "fail after usage",
            session_key,
            tool_context=tool_context,
            history_has_persisted_user=False,
            no_memory_capture=True,
        ):
            events.append(event)
            if getattr(event, "kind", "") == "error":
                break

        session = await manager.get_session(session_key)
        assert session is not None
        assert any(event.kind == "done" for event in events)
        assert events[-1].kind == "error"
        assert session.input_tokens == 40
        assert session.output_tokens == 2
        assert session.total_tokens == 42
        assert session.total_cost_usd == pytest.approx(0.004)
        assert session.billed_cost_usd == pytest.approx(0.004)
        assert session.cost_source == "provider_billed"
    finally:
        await storage.close()
