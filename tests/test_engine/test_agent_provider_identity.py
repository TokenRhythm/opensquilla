"""Pin the configured-provider-id propagation into usage tracking.

Local runtimes (vLLM, LM Studio, Ollama, …) are free, but the openai_compat
adapter class names itself ``"openai"`` for every deployment it serves. The
Agent used to forward that adapter class name into the usage tracker, so a
vLLM deployment was billed with the cloud OpenAI default estimate. The fix
threads the *configured* provider id (``AgentConfig.provider_id``) into both
tracker-add branches, ahead of the adapter class name, so ``SessionUsage``
prices a local runtime as free.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import _SelectorFallbackProvider
from opensquilla.engine.usage import UsageTracker
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDoneEvent
from opensquilla.provider import TextDeltaEvent as ProviderTextDeltaEvent


class _LocalCompatProvider:
    """Fake openai_compat adapter: its class name is the generic ``openai``.

    This mirrors ``provider/openai.py`` where ``provider_name = "openai"`` is
    shared by every openai_compat deployment (vLLM, LM Studio, …). A single
    text-only turn with no per-model breakdown drives the fallback tracker-add
    branch.
    """

    provider_name = "openai"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderTextDeltaEvent(text="hello from a local model")
        yield ProviderDoneEvent(
            stop_reason="end_turn",
            input_tokens=1000,
            output_tokens=50,
            billed_cost=0.0,
            model="qwen3-coder:30b",
        )

    async def list_models(self) -> list[Any]:
        return []


def _run_single_turn(
    config: AgentConfig,
    session_key: str,
    *,
    provider: Any | None = None,
) -> UsageTracker:
    tracker = UsageTracker()

    async def run() -> None:
        agent = Agent(
            provider=provider or _LocalCompatProvider(),
            config=config,
            tool_definitions=[],
            tool_handler=None,
            usage_tracker=tracker,
            session_key=session_key,
        )
        async for _ in agent.run_turn("hi"):
            pass

    asyncio.run(run())
    return tracker


def test_fallback_branch_records_configured_provider_id() -> None:
    """A vLLM-configured agent records ``provider="vllm"`` so the session
    prices the turn as free, even though the adapter class name is
    ``"openai"``."""
    session_key = "agent:test:webchat:vllm"
    tracker = _run_single_turn(
        AgentConfig(max_iterations=2, provider_id="vllm"),
        session_key,
    )

    usage = tracker.get(session_key)
    assert usage is not None
    assert usage._per_model is not None
    mu = usage._per_model["qwen3-coder:30b"]
    assert mu.provider == "vllm"
    # local_free short-circuit in resolve_model_price -> zero cost
    assert mu.cost == 0.0


def test_fallback_branch_without_provider_id_uses_adapter_name() -> None:
    """Backward-compatible default: with no configured provider id the
    adapter class name still flows through (and prices as cloud), so the
    fix is opt-in via ``AgentConfig.provider_id`` rather than a silent
    behavior change for callers that don't set it."""
    session_key = "agent:test:webchat:default"
    tracker = _run_single_turn(
        AgentConfig(max_iterations=2),
        session_key,
    )

    usage = tracker.get(session_key)
    assert usage is not None
    assert usage._per_model is not None
    mu = usage._per_model["qwen3-coder:30b"]
    assert mu.provider == "openai"
    # "openai" is not a local-free provider -> cloud default estimate applies
    assert mu.cost > 0.0


def test_routed_turn_records_selectors_actual_provider_id() -> None:
    """Cross-provider routing must not attribute usage to the primary provider."""

    class _Selector:
        active_provider_id = "deepseek"

    session_key = "agent:test:webchat:routed"
    tracker = _run_single_turn(
        AgentConfig(max_iterations=2, provider_id="openrouter"),
        session_key,
        provider=_SelectorFallbackProvider(_LocalCompatProvider(), _Selector()),
    )

    usage = tracker.get(session_key)
    assert usage is not None
    [deployment] = usage.deployment_breakdown
    assert deployment["provider"] == "deepseek"
    assert deployment["model"] == "qwen3-coder:30b"


def test_routed_turn_cost_budget_prices_the_actual_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live cost gate must not price a routed model as the primary provider."""
    from opensquilla.engine import pricing

    class _Selector:
        active_provider_id = "deepseek"

    calls: list[tuple[str, str]] = []
    original = pricing.resolve_model_price

    def recording_resolver(model_id: str, provider: str = "") -> Any:
        calls.append((model_id, provider))
        return original(model_id, provider)

    monkeypatch.setattr(pricing, "resolve_model_price", recording_resolver)
    _run_single_turn(
        AgentConfig(
            max_iterations=2,
            provider_id="openrouter",
            max_turn_cost_usd=1000.0,
        ),
        "agent:test:webchat:routed-budget",
        provider=_SelectorFallbackProvider(_LocalCompatProvider(), _Selector()),
    )

    assert ("qwen3-coder:30b", "deepseek") in calls
    assert ("qwen3-coder:30b", "openrouter") not in calls
