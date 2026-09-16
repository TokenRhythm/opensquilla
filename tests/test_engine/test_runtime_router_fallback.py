from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import runtime as runtime_module
from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.steps import squilla_router as squilla_router_step
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.provider import ChatConfig, EnsembleProvider, Message
from opensquilla.provider.selector import ProviderConfig


class _Provider:
    provider_name = "fake"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        raise AssertionError("pipeline test should not start provider chat")

    async def list_models(self) -> list[Any]:
        return []


class _FakeSelector:
    def __init__(self) -> None:
        self._cfg = ProviderConfig(
            provider="openrouter",
            model="base-model",
            api_key="sk-test",
            base_url="https://openrouter.ai/api",
        )

    @property
    def current_config(self) -> ProviderConfig:
        return self._cfg

    def override_model(self, model: str) -> None:
        self._cfg = ProviderConfig(
            provider=self._cfg.provider,
            model=model,
            api_key=self._cfg.api_key,
            base_url=self._cfg.base_url,
            proxy=self._cfg.proxy,
            provider_routing=self._cfg.provider_routing,
        )

    def resolve(self) -> _Provider:
        return _Provider()


class _SlowHistoryStrategy:
    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict]:
        time.sleep(0.08)
        return (
            "c2",
            0.95,
            "v4_phase3",
            {
                "route_class": "R2",
                "thinking_mode": "T2",
                "prompt_policy": "P1",
            },
        )


class _MutatingHistoryStrategy:
    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict]:
        time.sleep(0.08)
        assert routing_history
        routing_history[0]["final_tier"] = "poisoned"
        return (
            "c2",
            0.95,
            "v4_phase3",
            {
                "route_class": "R2",
                "thinking_mode": "T2",
                "prompt_policy": "P1",
            },
        )


def _config_with_router_timeout() -> GatewayConfig:
    return GatewayConfig(
        squilla_router=SquillaRouterConfig(routing_timeout_seconds=0.01)
    )


@pytest.mark.asyncio
async def test_run_pipeline_wraps_provider_when_llm_ensemble_enabled() -> None:
    config = GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm_ensemble={"enabled": True},
    )
    runner = TurnRunner(provider_selector=None, config=config)
    selector = _FakeSelector()

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:test",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert turn.metadata["ensemble_enabled"] is True
    assert turn.metadata["routed_model_before_ensemble"]


@pytest.mark.asyncio
async def test_squilla_router_timeout_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_router(ctx: TurnContext) -> TurnContext:
        await asyncio.sleep(1.0)
        ctx.model = "should-not-route"
        return ctx

    slow_router.__name__ = "apply_squilla_router"
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", slow_router)
    runner = TurnRunner(
        provider_selector=None,
        config=_config_with_router_timeout(),
    )
    provider = _Provider()

    turn, resolved_provider = await asyncio.wait_for(
        runner._run_pipeline(
            "hello",
            "agent:main:test",
            provider,
            None,
            [],
            "system prompt",
            [],
        ),
        timeout=0.25,
    )

    assert resolved_provider is provider
    assert turn.model != "should-not-route"
    router_record = next(
        record
        for record in turn.metadata["pipeline_steps"]
        if record.step_name == "apply_squilla_router"
    )
    assert router_record.applied is False
    assert "timed out" in (router_record.fallback_reason or "")


@pytest.mark.asyncio
async def test_squilla_router_timeout_does_not_late_append_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:main:test-router-timeout-history"
    squilla_router_step._history_store.clear()
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: _SlowHistoryStrategy(),
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_config_with_router_timeout(),
    )
    provider = _Provider()

    turn, resolved_provider = await asyncio.wait_for(
        runner._run_pipeline(
            "hello",
            session_key,
            provider,
            None,
            [],
            "system prompt",
            [],
        ),
        timeout=0.25,
    )
    await asyncio.sleep(0.1)

    assert resolved_provider is provider
    assert squilla_router_step._history_store.get(session_key) is None
    router_record = next(
        record
        for record in turn.metadata["pipeline_steps"]
        if record.step_name == "apply_squilla_router"
    )
    assert router_record.applied is False
    assert "timed out" in (router_record.fallback_reason or "")


@pytest.mark.asyncio
async def test_squilla_router_timeout_does_not_late_mutate_history_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "agent:main:test-router-timeout-history-mutation"
    original_history = [
        {
            "turn_index": 0,
            "_ts": time.monotonic(),
            "text": "previous",
            "final_tier": "c1",
        }
    ]
    squilla_router_step._history_store.clear()
    squilla_router_step._history_store.set(session_key, original_history)
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: _MutatingHistoryStrategy(),
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_config_with_router_timeout(),
    )
    provider = _Provider()

    turn, resolved_provider = await asyncio.wait_for(
        runner._run_pipeline(
            "hello",
            session_key,
            provider,
            None,
            [],
            "system prompt",
            [],
        ),
        timeout=0.25,
    )
    await asyncio.sleep(0.1)

    assert resolved_provider is provider
    stored_history = squilla_router_step._history_store.get(session_key)
    assert stored_history == original_history
    assert stored_history is not None
    assert stored_history[0]["final_tier"] == "c1"
    router_record = next(
        record
        for record in turn.metadata["pipeline_steps"]
        if record.step_name == "apply_squilla_router"
    )
    assert router_record.applied is False
    assert "timed out" in (router_record.fallback_reason or "")


@pytest.mark.asyncio
async def test_squilla_router_timeout_fails_open_for_blocking_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    completed = threading.Event()
    observed_timeouts: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def blocking_router(ctx: TurnContext) -> TurnContext:
        try:
            loop.call_soon_threadsafe(entered.set)
            # The finite wait also bounds a regression that runs this directly
            # on the event loop, where an asyncio watchdog cannot fire.
            release.wait(5)
            ctx.model = "should-not-route"
            return ctx
        finally:
            completed.set()

    async def wait_for_started_router(awaitable: Any, timeout: float | None) -> Any:
        observed_timeouts.append(timeout)
        # Start the real timeout only once the worker is blocked. Host thread
        # scheduling is not part of the configured router deadline contract.
        await real_wait_for(entered.wait(), timeout=5)
        return await real_wait_for(awaitable, timeout=timeout)

    blocking_router.__name__ = "apply_squilla_router"
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", blocking_router)
    monkeypatch.setattr(
        runtime_module,
        "asyncio",
        SimpleNamespace(**(vars(asyncio) | {"wait_for": wait_for_started_router})),
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_config_with_router_timeout(),
    )
    provider = _Provider()
    try:
        turn, resolved_provider = await real_wait_for(
            runner._run_pipeline(
                "hello",
                "agent:main:test",
                provider,
                None,
                [],
                "system prompt",
                [],
            ),
            timeout=5,
        )

        assert observed_timeouts == [0.01]
        assert not completed.is_set()
        assert resolved_provider is provider
        assert turn.model != "should-not-route"
    finally:
        release.set()
        assert await asyncio.to_thread(completed.wait, 5)

    # The timed-out worker has now mutated its copy; it cannot change the
    # already returned turn even after it completes.
    assert turn.model != "should-not-route"
    router_record = next(
        record
        for record in turn.metadata["pipeline_steps"]
        if record.step_name == "apply_squilla_router"
    )
    assert router_record.applied is False
    assert "timed out" in (router_record.fallback_reason or "")
