"""Pin the optional provider-call observer seam on the agent loop.

The gateway injects an observer (``AgentConfig.provider_call_observer``) that
samples per-call TTFT/duration for ``providers.status`` latency. The engine
contract: invoked once per provider call with keyword args, and observer
failures never affect the turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent


class _ScriptedProvider:
    provider_name = "openai"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderTextDeltaEvent(text="observed reply")
        yield ProviderDoneEvent(
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=5,
            billed_cost=0.0,
            model="test-model",
        )

    async def list_models(self) -> list[Any]:
        return []


class _RaisingProvider(_ScriptedProvider):
    """Stream that raises (instead of yielding a ProviderErrorEvent)."""

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderTextDeltaEvent(text="partial")
        raise RuntimeError("connection reset mid-stream")


class _HangingProvider(_ScriptedProvider):
    """Stream that never produces an event, tripping the total deadline."""

    async def _stream(self) -> AsyncIterator[Any]:
        await asyncio.sleep(30.0)
        yield ProviderTextDeltaEvent(text="never reached")


def _run_turn(config: AgentConfig, provider: _ScriptedProvider | None = None) -> list[Any]:
    events: list[Any] = []

    async def run() -> None:
        agent = Agent(
            provider=provider or _ScriptedProvider(),
            config=config,
            tool_definitions=[],
            tool_handler=None,
            session_key="agent:test:webchat:observer",
        )
        async for ev in agent.run_turn("hi"):
            events.append(ev)

    asyncio.run(run())
    return events


def test_observer_receives_one_sample_per_successful_call() -> None:
    calls: list[dict[str, Any]] = []

    def observer(**kwargs: Any) -> None:
        calls.append(kwargs)

    _run_turn(
        AgentConfig(
            max_iterations=2,
            provider_id="vllm",
            model_id="test-model",
            provider_call_observer=observer,
        )
    )

    assert len(calls) == 1
    sample = calls[0]
    assert sample["provider_id"] == "vllm"
    assert sample["model"] == "test-model"
    assert sample["ok"] is True
    assert sample["failure_kind"] == ""
    assert sample["ttft_ms"] is not None
    assert sample["ttft_ms"] >= 0
    assert sample["duration_ms"] >= 0


def test_observer_falls_back_to_adapter_provider_name() -> None:
    calls: list[dict[str, Any]] = []

    def observer(**kwargs: Any) -> None:
        calls.append(kwargs)

    _run_turn(AgentConfig(max_iterations=2, provider_call_observer=observer))

    assert len(calls) == 1
    assert calls[0]["provider_id"] == "openai"


def test_raising_observer_never_affects_the_turn() -> None:
    def observer(**kwargs: Any) -> None:
        raise RuntimeError("observer exploded")

    events = _run_turn(AgentConfig(max_iterations=2, provider_call_observer=observer))

    texts = [getattr(ev, "text", "") for ev in events]
    assert any("observed reply" in text for text in texts)
    assert not any(getattr(ev, "kind", "") == "error" for ev in events)


def test_no_observer_configured_is_a_noop() -> None:
    events = _run_turn(AgentConfig(max_iterations=2))
    texts = [getattr(ev, "text", "") for ev in events]
    assert any("observed reply" in text for text in texts)


def test_observer_records_failed_sample_when_stream_raises() -> None:
    calls: list[dict[str, Any]] = []

    def observer(**kwargs: Any) -> None:
        calls.append(kwargs)

    events = _run_turn(
        AgentConfig(
            max_iterations=2,
            max_provider_retries=0,
            provider_id="vllm",
            model_id="test-model",
            provider_call_observer=observer,
        ),
        provider=_RaisingProvider(),
    )

    assert len(calls) == 1
    sample = calls[0]
    assert sample["ok"] is False
    assert sample["failure_kind"] == "transport_transient"
    assert sample["ttft_ms"] is not None
    assert sample["duration_ms"] >= 0
    terminal = next(event for event in events if getattr(event, "kind", "") == "error")
    assert terminal.code == "response_incomplete"
    assert "connection reset mid-stream" not in repr(events)


def test_observer_records_failed_sample_on_total_deadline_timeout() -> None:
    calls: list[dict[str, Any]] = []

    def observer(**kwargs: Any) -> None:
        calls.append(kwargs)

    events = _run_turn(
        AgentConfig(
            max_iterations=2,
            provider_id="vllm",
            model_id="test-model",
            timeout=0.05,
            iteration_timeout=30.0,
            provider_call_observer=observer,
        ),
        provider=_HangingProvider(),
    )

    assert len(calls) == 1
    sample = calls[0]
    assert sample["ok"] is False
    assert sample["failure_kind"] == "total_timeout"
    assert sample["ttft_ms"] is None
    # The total-deadline timeout must still surface as the same terminal error.
    assert any(getattr(ev, "code", "") == "agent_runtime_timeout" for ev in events)
