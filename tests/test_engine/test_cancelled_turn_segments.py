"""Cancelled turns persist the same segment timeline a completed turn would."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.routing.fixed_four_tier_v2 import (
    INTENTS,
    TIERS,
    ClassifierPrediction,
    FixedFourTierV2Router,
)
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import TextDeltaEvent
from opensquilla.gateway.config import AttachmentsConfig, GatewayConfig, SquillaRouterConfig
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import Message, ModelCapabilities, ModelInfo
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart
from opensquilla.provider.selector import ModelSelector, ProviderConfig, SelectorConfig
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


class _ZeroThenCompletedProvider:
    """First call hangs before output; the next call completes normally."""

    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.model = ""
        self.first_call_started = asyncio.Event()

    def chat(self, messages: list[Message], tools=None, config=None) -> AsyncIterator[Any]:
        del messages, tools, config
        self.calls += 1
        call_number = self.calls
        model = self.model

        async def _stream() -> AsyncIterator[Any]:
            if call_number == 1:
                self.first_call_started.set()
                await asyncio.Event().wait()
                return
            yield ProviderText(text="continued response")
            yield ProviderDone(
                stop_reason="end_turn",
                input_tokens=3,
                output_tokens=2,
                provider="openrouter",
                model=model,
            )

        return _stream()

    async def list_models(self) -> list[ModelInfo]:
        return []


class _FixedV2Catalog:
    def resolve_max_tokens(
        self,
        model_id: str,
        user_override: int = 0,
        provider: str = "",
    ) -> int:
        del model_id, provider
        return user_override if user_override > 0 else 8_192

    def resolve_max_tokens_with_source(
        self,
        model_id: str,
        user_override: int = 0,
        provider: str = "",
    ) -> tuple[int, str]:
        return (
            self.resolve_max_tokens(model_id, user_override, provider),
            "override" if user_override > 0 else "catalog",
        )

    def resolve_context_window(self, model_id: str, provider: str = "") -> int:
        del model_id, provider
        return 128_000

    def resolve_context_window_with_source(
        self,
        model_id: str,
        provider: str = "",
    ) -> tuple[int, str]:
        return self.resolve_context_window(model_id, provider), "catalog"

    def get_capabilities(
        self,
        model_id: str,
        provider_name: str = "",
        base_url: str = "",
    ) -> ModelCapabilities:
        del model_id, provider_name, base_url
        return ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            supports_vision=True,
        )


class _ContinueIntentClassifier:
    version = "continue-v1"

    def predict(self, snapshot: Any) -> ClassifierPrediction:
        del snapshot
        return ClassifierPrediction(
            label="continue",
            probabilities={label: 1.0 if label == "continue" else 0.0 for label in INTENTS},
            confidence=1.0,
            version=self.version,
        )


class _C1TierClassifier:
    version = "c1-v1"

    def predict(self, snapshot: Any, allowed_tiers: Any) -> ClassifierPrediction:
        del snapshot
        assert "c1" in allowed_tiers
        return ClassifierPrediction(
            label="c1",
            probabilities={label: 1.0 if label == "c1" else 0.0 for label in TIERS},
            confidence=1.0,
            version=self.version,
        )


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
        assert "[interrupted]" in assistant.content

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


@pytest.mark.asyncio
async def test_fixed_v2_zero_output_cancel_preserves_anchor_and_next_continue(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:fixed-zero-output-cancel"
    session = await manager.create(session_key)
    provider = _ZeroThenCompletedProvider()
    selector = ModelSelector(
        SelectorConfig(
            primary=ProviderConfig(
                provider="openrouter",
                model="openai/gpt-5.5",
                api_key="synthetic",
            )
        )
    )

    def _resolve_physical(active_selector: ModelSelector) -> Any:
        provider.model = active_selector.current_config.model
        return provider

    monkeypatch.setattr(ModelSelector, "resolve", _resolve_physical)
    config = GatewayConfig(
        attachments=AttachmentsConfig(media_root=str(tmp_path / "media")),
        squilla_router=SquillaRouterConfig(enabled=False),
        llm_profiles={"openrouter": {"api_key": "synthetic"}},
        llm_ensemble={
            "enabled": True,
            "mode": "single",
            "selection_mode": "four_tier_mapping",
            "four_tier_mapping": {"mock_seed": 1},
        },
    )
    runner = TurnRunner(
        provider_selector=selector,
        session_manager=manager,
        config=config,
        model_catalog=_FixedV2Catalog(),
    )
    scripted_router = FixedFourTierV2Router(
        intent_classifier=_ContinueIntentClassifier(),
        tier_classifier=_C1TierClassifier(),
        policy_config={"test": "zero-output-cancel"},
    )
    monkeypatch.setattr(
        runner,
        "_fixed_four_tier_v2_router_for_config",
        lambda _ensemble: scripted_router,
    )
    tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        workspace_dir=str(tmp_path),
    )

    async def _consume_first() -> None:
        async for _event in runner.run(
            "start durable fixed task",
            session_key,
            tool_context=tool_context,
            bound_user_message_id=first_input.message_id,
            no_memory_capture=True,
        ):
            pass

    first_input = await manager.append_message(
        session_key,
        role="user",
        content="start durable fixed task",
    )
    first = asyncio.create_task(_consume_first())
    try:
        await asyncio.wait_for(provider.first_call_started.wait(), timeout=5.0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        first_transcript = await manager.get_transcript(session_key)
        first_users = [entry for entry in first_transcript if entry.role == "user"]
        first_assistants = [entry for entry in first_transcript if entry.role == "assistant"]
        assert len(first_users) == 1
        assert len(first_assistants) == 1
        assert "[interrupted]" in first_assistants[0].content
        assert first_assistants[0].turn_context is not None
        assert first_assistants[0].turn_context["schema"] == (
            "fixed_four_tier_v2_response_binding_v1"
        )
        assert first_assistants[0].turn_context["execution_status"] == "cancelled"

        state_after_cancel = await manager.get_fixed_four_tier_state(session.session_id)
        assert state_after_cancel is not None
        assert state_after_cancel.task_start_input_message_id == first_users[0].message_id
        first_task_id = state_after_cancel.task_id

        second_input = await manager.append_message(
            session_key,
            role="user",
            content="continue the durable fixed task",
        )
        second_events = [
            event
            async for event in runner.run(
                "continue the durable fixed task",
                session_key,
                tool_context=tool_context,
                bound_user_message_id=second_input.message_id,
                no_memory_capture=True,
            )
        ]
        assert any(
            isinstance(event, TextDeltaEvent) and "continued response" in (event.text or "")
            for event in second_events
        )
        state_after_continue = await manager.get_fixed_four_tier_state(session.session_id)
        assert state_after_continue is not None
        assert state_after_continue.task_id == first_task_id
        assert state_after_continue.task_turn_count == 2
        assert state_after_continue.task_start_input_message_id == first_users[0].message_id
    finally:
        if not first.done():
            first.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await first
        await storage.close()
