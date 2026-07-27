from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.types import EnsembleProgressEvent as EngineEnsembleProgressEvent
from opensquilla.engine.types import RunHeartbeatEvent, ToolCall
from opensquilla.provider import (
    ChatConfig,
    Message,
    ProviderHeartbeatEvent,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import (
    DoneEvent as ProviderDone,
)
from opensquilla.provider import (
    ErrorEvent as ProviderErrorEvent,
)
from opensquilla.provider import (
    TextDeltaEvent as ProviderText,
)
from opensquilla.provider.types import (
    EnsembleProgressEvent as ProviderEnsembleProgressEvent,
)


class _EnsembleLikeProvider:
    """A provider that emits mid-stream ensemble_progress deltas, exactly like the
    real EnsembleProvider does for its proposers."""

    provider_name = "fake"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderEnsembleProgressEvent(
            event_type="proposer_start",
            proposer_label="anchor",
            proposer_provider="openrouter",
            proposer_model="qwen/qwen3.7-plus",
        )
        yield ProviderHeartbeatEvent(
            phase="ensemble_proposers_wait",
            message="still generating candidates",
        )
        yield ProviderEnsembleProgressEvent(
            event_type="proposer_finish",
            proposer_label="anchor",
            proposer_provider="openrouter",
            proposer_model="qwen/qwen3.7-plus",
            input_tokens=10,
            output_tokens=5,
        )
        yield ProviderText(text="synthesized answer")
        yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    async def list_models(self) -> list[Any]:
        return []


async def _tool_handler(call: ToolCall) -> ToolResult:
    return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok")


def _agent(provider: Any) -> Agent:
    return Agent(
        provider=provider,
        config=AgentConfig(max_iterations=2),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )
        ],
        tool_handler=_tool_handler,
    )


@pytest.mark.asyncio
async def test_agent_forwards_provider_ensemble_progress_as_engine_event() -> None:
    # This is the previously-unverified link: the ensemble provider yields
    # provider-level EnsembleProgressEvents; the agent loop must re-emit them as
    # engine-level EnsembleProgressEvents so channel_dispatch can broadcast them.
    agent = _agent(_EnsembleLikeProvider())

    events = [event async for event in agent.run_turn("hi")]
    progress = [e for e in events if isinstance(e, EngineEnsembleProgressEvent)]

    assert len(progress) == 2, f"expected 2 engine ensemble_progress events, got {len(progress)}"
    assert {p.event_type for p in progress} == {"proposer_start", "proposer_finish"}
    assert progress[0].proposer_model == "qwen/qwen3.7-plus"
    finish = next(p for p in progress if p.event_type == "proposer_finish")
    assert finish.input_tokens == 10
    assert finish.output_tokens == 5
    assert any(isinstance(event, RunHeartbeatEvent) for event in events)


@pytest.mark.asyncio
async def test_selector_wrapper_and_agent_preserve_live_control_event_order() -> None:
    from opensquilla.engine.runtime import _SelectorFallbackProvider

    class _Selector:
        pass

    agent = _agent(_SelectorFallbackProvider(_EnsembleLikeProvider(), _Selector()))

    events = [event async for event in agent.run_turn("hi")]
    start_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, EngineEnsembleProgressEvent)
        and event.event_type == "proposer_start"
    )
    heartbeat_index = next(
        index for index, event in enumerate(events) if isinstance(event, RunHeartbeatEvent)
    )
    finish_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, EngineEnsembleProgressEvent)
        and event.event_type == "proposer_finish"
    )
    text_index = next(index for index, event in enumerate(events) if event.kind == "text_delta")

    assert start_index < heartbeat_index < finish_index < text_index
    progress = [event for event in events if isinstance(event, EngineEnsembleProgressEvent)]
    assert [event.event_type for event in progress] == ["proposer_start", "proposer_finish"]
    assert progress[1].input_tokens == 10
    assert progress[1].output_tokens == 5


@pytest.mark.asyncio
async def test_selector_fallback_control_events_remain_live_and_do_not_block_fallback() -> None:
    from opensquilla.engine.runtime import _SelectorFallbackProvider

    class _Primary:
        provider_name = "openrouter"

        async def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            yield ProviderEnsembleProgressEvent(
                event_type="proposer_start",
                proposer_label="primary",
                proposer_provider="openrouter",
                proposer_model="primary/model",
            )
            yield ProviderHeartbeatEvent(phase="ensemble_proposers_wait")
            yield ProviderErrorEvent(message="rate limited", code="429")

    class _Fallback(_EnsembleLikeProvider):
        provider_name = "anthropic"

    class _Selector:
        current_config = type("Config", (), {"model": "primary/model"})()

        def next_fallback_after_failure(self, exc: Exception) -> Any:
            del exc
            self.current_config = type("Config", (), {"model": "fallback/model"})()
            return _Fallback()

    agent = _agent(_SelectorFallbackProvider(_Primary(), _Selector()))
    events = [event async for event in agent.run_turn("hi")]

    progress = [event for event in events if isinstance(event, EngineEnsembleProgressEvent)]
    heartbeats = [event for event in events if isinstance(event, RunHeartbeatEvent)]
    assert [(event.event_type, event.proposer_label) for event in progress] == [
        ("proposer_start", "primary"),
        ("proposer_start", "anchor"),
        ("proposer_finish", "anchor"),
    ]
    assert len(heartbeats) == 2
    assert not any(event.kind == "error" for event in events)
    assert any(
        event.kind == "text_delta" and event.text == "synthesized answer"
        for event in events
    )


@pytest.mark.asyncio
async def test_selector_fallback_restores_single_provider_retry_safety() -> None:
    from opensquilla.engine.runtime import _SelectorFallbackProvider

    class _Composite:
        provider_name = "ensemble"
        retry_failed_call_safe = False

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            self.calls += 1
            yield ProviderErrorEvent(message="rate limited", code="429")

    class _Fallback:
        provider_name = "openrouter"

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            self.calls += 1
            if self.calls == 1:
                yield ProviderErrorEvent(message="Request timed out: ", code="timeout")
                return
            yield ProviderText(text="fallback recovered")
            yield ProviderDone(stop_reason="stop", input_tokens=1, output_tokens=1)

    composite = _Composite()
    fallback = _Fallback()

    class _Selector:
        current_config = type("Config", (), {"model": "ensemble/model"})()

        def next_fallback_after_failure(self, exc: Exception) -> Any:
            del exc
            self.current_config = type("Config", (), {"model": "fallback/model"})()
            return fallback

    wrapper = _SelectorFallbackProvider(composite, _Selector())
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(
            max_iterations=2,
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(properties={}, required=[]),
            )
        ],
        tool_handler=_tool_handler,
    )

    events = [event async for event in agent.run_turn("hi")]

    assert composite.calls == 1
    assert fallback.calls == 2
    assert wrapper.retry_failed_call_safe is True
    assert not any(event.kind == "error" for event in events)
    assert any(
        event.kind == "text_delta" and event.text == "fallback recovered"
        for event in events
    )
