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
from opensquilla.engine.runtime import TurnRunner, accepted_turn_config_scope
from opensquilla.engine.steps import squilla_router as squilla_router_step
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.gateway.model_routing import capture_model_routing_config
from opensquilla.provider import ChatConfig, EnsembleProvider, Message
from opensquilla.provider.selector import ProviderConfig


@pytest.fixture(autouse=True)
def isolated_router_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    # These deadline tests substitute the classifier; its optional ML bundle
    # must not be loaded as an unrelated prerequisite. Readiness tests below
    # replace this stub with a controlled cold initializer.
    monkeypatch.setattr(squilla_router_step, "preload_strategy", lambda _config: object())


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


@pytest.mark.asyncio
async def test_router_worker_does_not_clone_live_services_or_bound_callbacks(monkeypatch):
    """Copying a bound callback must not recursively clone its gateway/event loop."""
    copied: list[str] = []

    class LiveService:
        def __init__(self, name):
            self.name = name

        def __deepcopy__(self, _memo):
            copied.append(self.name)
            raise TypeError("live gateway services cannot be copied")

        def observe_metaskill_usage(self, _run_id):
            pass

    sink = LiveService("growth_sink")
    writer = LiveService("meta_writer")
    hold_store = LiveService("router_hold_store")
    observed = {}

    async def inspect_router(ctx):
        observed.update({
            "writer": ctx.metadata["meta_run_writer"],
            "sink": ctx.metadata["metaskill_usage_recorder"].__self__,
            "hold": ctx.metadata["router_control_hold_store"],
        })
        # Mutable per-turn facts still need isolation from a timed-out worker.
        ctx.metadata["router_prev_assistant_usage"]["nested"]["tokens"] = 99
        return ctx

    inspect_router.__name__ = "apply_squilla_router"
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", inspect_router)
    runner = TurnRunner(
        provider_selector=None, config=GatewayConfig(), meta_run_writer=writer,
        growth_event_sink=sink,
    )
    runner._router_control_hold_store = hold_store
    facts = {"nested": {"tokens": 7}}
    await runner._run_pipeline(
        "hello", "agent:main:live-service-copy", _Provider(), None, [], "system", [],
        prev_assistant_usage=facts,
    )
    assert observed == {"writer": writer, "sink": sink, "hold": hold_store}
    assert facts == {"nested": {"tokens": 7}}
    assert copied == []


@pytest.mark.asyncio
async def test_router_readiness_loads_offloop_before_classification_budget(monkeypatch):
    import asyncio
    import threading

    from opensquilla.engine.steps.squilla_router import prepare_model_routing_runtime

    config = GatewayConfig(squilla_router={"enabled": False, "routing_timeout_seconds": 5})
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    caller_thread = threading.get_ident()
    seen = []

    def preload(router_config):
        seen.append((router_config, threading.get_ident()))
        loop.call_soon_threadsafe(entered.set)
        release.wait(2)
        return object()

    monkeypatch.setattr("opensquilla.engine.steps.squilla_router.preload_strategy", preload)
    accepted = capture_model_routing_config(config, session_mode="router")
    task = asyncio.create_task(prepare_model_routing_runtime(accepted))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert not task.done()
        assert seen[0][1] != caller_thread
        assert config.squilla_router.enabled is False
        assert config.squilla_router.routing_timeout_seconds == 5
    finally:
        release.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["direct", "ensemble", None])
async def test_non_router_readiness_does_not_load_local_models(monkeypatch, mode):
    from opensquilla.engine.steps.squilla_router import prepare_model_routing_runtime

    calls = []
    monkeypatch.setattr(
        "opensquilla.engine.steps.squilla_router.preload_strategy",
        lambda config: calls.append(config),
    )
    accepted = capture_model_routing_config(
        GatewayConfig(squilla_router={"enabled": False}), session_mode=mode,
    )
    await prepare_model_routing_runtime(accepted)
    assert calls == []


@pytest.mark.asyncio
async def test_router_initialization_failure_keeps_routing_pipeline_authoritative(monkeypatch):
    from opensquilla.engine.steps import squilla_router as router_step
    from opensquilla.engine.steps.squilla_router import prepare_model_routing_runtime

    def fail(_config):
        raise RuntimeError("local router readiness failed")

    warnings = []
    monkeypatch.setattr(router_step.log, "warning", lambda *a, **kw: warnings.append(kw))
    monkeypatch.setattr("opensquilla.engine.steps.squilla_router.preload_strategy", fail)
    accepted = capture_model_routing_config(GatewayConfig(), session_mode="router")
    await prepare_model_routing_runtime(accepted)
    assert accepted.squilla_router.enabled is True
    assert warnings == [{"reason": "initialization_failed", "error_type": "RuntimeError"}]


@pytest.mark.asyncio
async def test_cold_session_router_prepares_accepted_config_before_classification(monkeypatch):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    completed = threading.Event()
    classifications = []
    preparations = []
    classifier_timeouts = []
    real_wait_for = asyncio.wait_for

    async def classification_wait_for(awaitable, timeout):
        assert completed.is_set()
        classifier_timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(runtime_module, "asyncio", SimpleNamespace(
        **(vars(asyncio) | {"wait_for": classification_wait_for}),
    ))
    config = GatewayConfig(squilla_router={"enabled": False, "routing_timeout_seconds": 5})
    accepted = capture_model_routing_config(config, session_mode="router")

    class ReadyStrategy:
        async def classify(self, _message, _tiers, **kwargs):
            assert completed.is_set()
            classifications.append(True)
            return "c2", 0.95, "v4_phase3", {
                "route_class": "R2", "thinking_mode": "T2", "prompt_policy": "P1",
            }

    strategy = ReadyStrategy()

    def preload(router_config):
        assert router_config is accepted.squilla_router
        preparations.append(router_config)
        loop.call_soon_threadsafe(entered.set)
        release.wait(2)
        completed.set()
        return strategy

    monkeypatch.setattr(squilla_router_step, "preload_strategy", preload)
    monkeypatch.setattr(squilla_router_step, "_get_strategy", lambda _config: strategy)
    runner = TurnRunner(provider_selector=None, config=config)
    with accepted_turn_config_scope(accepted):
        task = asyncio.create_task(runner._run_pipeline(
            "Review this implementation", "agent:main:cold-session-router", _Provider(),
            None, [], "system", [],
        ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # Readiness starts before the classification timer exists.
        assert classifier_timeouts == []
        assert not task.done()
        assert classifications == []
        assert config.squilla_router.enabled is False
        release.set()
        turn, _provider = await asyncio.wait_for(task, 2)
        assert turn.config.squilla_router is accepted.squilla_router
        assert classifier_timeouts == [5]
        assert preparations == [accepted.squilla_router]
        assert classifications == [True]
        assert turn.metadata["routed_tier"] == "c2"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_router_initialization_timeout_does_not_reject_accepted_turn(monkeypatch):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    completed = threading.Event()
    real_prepare = squilla_router_step.prepare_model_routing_runtime
    real_wait_for = asyncio.wait_for
    warnings = []
    calls = []

    def blocked_preload(_config):
        try:
            loop.call_soon_threadsafe(entered.set)
            release.wait(5)
        finally:
            completed.set()

    async def wait_for_started_preload(awaitable, timeout):
        task = asyncio.ensure_future(awaitable)
        await real_wait_for(entered.wait(), 2)
        return await real_wait_for(task, timeout)

    async def bounded_prepare(config, **kwargs):
        await real_prepare(config, **kwargs, initialization_timeout=0.01)

    async def route(turn):
        calls.append(turn.config)
        return turn

    monkeypatch.setattr(squilla_router_step, "preload_strategy", blocked_preload)
    monkeypatch.setattr(squilla_router_step, "asyncio", SimpleNamespace(
        **(vars(asyncio) | {"wait_for": wait_for_started_preload}),
    ))
    monkeypatch.setattr(squilla_router_step.log, "warning", lambda *a, **kw: warnings.append(kw))
    monkeypatch.setattr(squilla_router_step, "prepare_model_routing_runtime", bounded_prepare)
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", route)
    config = GatewayConfig(squilla_router={"enabled": False})
    accepted = capture_model_routing_config(config, session_mode="router")
    runner = TurnRunner(provider_selector=None, config=config)
    try:
        with accepted_turn_config_scope(accepted):
            await real_wait_for(runner._run_pipeline(
                "hello", "agent:main:router-warmup-timeout", _Provider(), None, [], "system", [],
            ), 3)
        assert len(calls) == 1
        assert calls[0].squilla_router is accepted.squilla_router
        assert accepted.squilla_router.enabled is True
        assert warnings == [{"reason": "timeout", "timeout_seconds": 0.01}]
    finally:
        release.set()
        assert await asyncio.to_thread(completed.wait, 2)


@pytest.mark.asyncio
async def test_cancelled_router_readiness_never_starts_classification(monkeypatch):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    completed = threading.Event()
    calls = []

    def preload(_config):
        try:
            loop.call_soon_threadsafe(entered.set)
            release.wait(2)
        finally:
            completed.set()

    async def route(turn):
        calls.append(turn)
        return turn

    monkeypatch.setattr(squilla_router_step, "preload_strategy", preload)
    monkeypatch.setattr("opensquilla.engine.steps.apply_squilla_router", route)
    runner = TurnRunner(provider_selector=None, config=GatewayConfig())
    task = asyncio.create_task(runner._run_pipeline(
        "hello", "agent:main:cancel-warmup", _Provider(), None, [], "system", [],
    ))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert calls == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(completed.wait, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("ineligible", ["no_tiers", "subagent"])
async def test_router_readiness_skips_nonrouting_pipeline_inputs(monkeypatch, ineligible):
    config = GatewayConfig()
    session_key = "agent:main:ordinary"
    if ineligible == "no_tiers":
        config.squilla_router.tiers = {}
    else:
        session_key = "agent:main:subagent:worker"
    calls = []
    monkeypatch.setattr(squilla_router_step, "preload_strategy", lambda cfg: calls.append(cfg))
    runner = TurnRunner(provider_selector=None, config=config)
    await runner._run_pipeline("hello", session_key, _Provider(), None, [], "system", [])
    assert calls == []
