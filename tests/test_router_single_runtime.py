from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ThinkingLevel, ToolResult
from opensquilla.engine.routing.health import ProviderHealthLedger
from opensquilla.engine.runtime import (
    _ROUTER_SINGLE_FROZEN_CATALOG,
    TurnRunner,
    _RouterSingleDirectProvider,
    _SelectorFallbackProvider,
)
from opensquilla.engine.turn_runner.harness import (
    _TurnRunnerModelCatalogAdapter,
    _TurnRunnerPipelineExecutionAdapter,
)
from opensquilla.engine.turn_runner.prompt_assembler_stage import (
    PromptAssemblerStage,
    PromptAssemblerStageInput,
)
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.provider import (
    ChatConfig,
    Message,
    ProviderFailureKind,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import (
    ErrorEvent as ProviderError,
)
from opensquilla.provider.deployment import ProviderDeploymentResolution
from opensquilla.provider.ensemble import resolve_router_single_route
from opensquilla.provider.ranking_router import (
    DynamicRankingError,
    RankedModel,
    SingleModelRankingDecision,
    TaskAnalysisResult,
    ranking_config_snapshot,
)
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import (
    DoneEvent as ProviderDone,
)
from opensquilla.provider.types import (
    TextDeltaEvent as ProviderText,
)
from opensquilla.provider.types import (
    ToolUseEndEvent as ProviderToolUseEnd,
)
from opensquilla.provider.types import (
    ToolUseStartEvent as ProviderToolUseStart,
)


class _NoChatProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls += 1
        raise AssertionError("routing gate must not start generation")

    async def list_models(self) -> list[Any]:
        return []


class _Selector:
    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._cfg = config or ProviderConfig(
            provider="openrouter",
            model="openai/gpt-5.5",
            api_key="synthetic",
        )

    @property
    def current_config(self) -> ProviderConfig:
        return self._cfg

    @property
    def active_provider_id(self) -> str:
        return self._cfg.provider

    def override_model(self, model: str) -> None:
        self._cfg = replace(self._cfg, model=model)

    def override_provider_config(self, config: ProviderConfig) -> None:
        self._cfg = config

    def resolve(self) -> _NoChatProvider:
        return _NoChatProvider()


def _router_single_config() -> GatewayConfig:
    return GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm_ensemble={
            "enabled": True,
            "mode": "single",
            "selection_mode": "router_dynamic",
        },
    )


async def test_explicit_model_returns_before_router_single_analyzer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    called = False

    async def fail_if_called(**kwargs: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("explicit model must skip router_single Analyzer")

    monkeypatch.setattr(runner, "_resolve_router_single_provider", fail_if_called)
    original = _NoChatProvider()
    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:explicit",
        original,
        _Selector(),
        [],
        "system",
        [],
        explicit_model="openai/gpt-5.6",
    )

    assert called is False
    assert provider is not original
    assert isinstance(provider, _NoChatProvider)
    assert "router_single_decision" not in turn.metadata
    assert "ensemble_enabled" not in turn.metadata


class _StagePromptAssembler:
    def assemble_prompt(self, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        return "system"


class _StageRouterContext:
    async def fetch_router_context(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {}


class _StagePromptConfigResolver:
    def resolve_prompt_config(self, turn: Any) -> tuple[str, None, None]:
        del turn
        return "system", None, None


class _StagePromptReportBuilder:
    def build_prompt_report(self, **kwargs: Any) -> Any:
        return SimpleNamespace(**kwargs)


class _StageSessionIdResolver:
    async def resolve_session_id_for_log(self, session_key: str) -> str:
        del session_key
        return "session-id"


class _StageMemoryFingerprint:
    def memory_mode_fingerprint(self) -> None:
        return None


async def test_prompt_stage_explicit_model_wins_after_router_single_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    analyzer_called = False

    async def fail_if_called(**kwargs: Any) -> Any:
        nonlocal analyzer_called
        analyzer_called = True
        raise AssertionError("explicit model must skip router_single Analyzer")

    monkeypatch.setattr(runner, "_resolve_router_single_provider", fail_if_called)
    selector = _Selector()
    original = _NoChatProvider()
    stage = PromptAssemblerStage(
        prompt_assembler=_StagePromptAssembler(),
        pipeline_executor=_TurnRunnerPipelineExecutionAdapter(runner),
        router_context=_StageRouterContext(),
        prompt_config_resolver=_StagePromptConfigResolver(),
        prompt_report_builder=_StagePromptReportBuilder(),
        session_id_resolver=_StageSessionIdResolver(),
        memory_fingerprint=_StageMemoryFingerprint(),
    )
    explicit_model = "openai/gpt-5.6"

    outcome = await stage.run(
        PromptAssemblerStageInput(
            runtime_message="hello",
            semantic_input="hello",
            extra_prompt_context=None,
            provider=original,
            cloned_selector=selector,
            tool_defs=[],
            effective_tool_context=None,
            tool_metadata={},
            session_key="agent:main:explicit-stage",
            agent_id="main",
            turn_id="turn-explicit-stage",
            attachments=[],
            bootstrap_context_mode=None,
            model=explicit_model,
            history_has_persisted_user=False,
            persist_input=False,
        )
    )

    result = outcome.output
    assert analyzer_called is False
    assert selector.current_config.model == explicit_model
    assert result.resolved_model == explicit_model
    assert result.turn.metadata["executed_model"] == explicit_model
    assert "router_single_decision" not in result.turn.metadata
    assert result.provider.primary is not original


async def test_router_single_early_return_never_enters_fusion_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = TurnRunner(provider_selector=None, config=_router_single_config())
    direct = object()
    calls: list[dict[str, Any]] = []

    async def resolve_direct(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return direct

    monkeypatch.setattr(runner, "_resolve_router_single_provider", resolve_direct)
    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:single",
        _NoChatProvider(),
        _Selector(),
        [],
        "system",
        [],
    )

    assert provider is direct
    assert len(calls) == 1
    assert calls[0]["ensemble_cfg"].mode == "single"
    assert "ensemble_decision_id" not in turn.metadata
    assert "ensemble_enabled" not in turn.metadata


def test_fusion_gate_remains_the_original_unqualified_block() -> None:
    source = inspect.getsource(TurnRunner._run_pipeline)
    original_gate = (
        "if provider is not None and getattr(ensemble_cfg, \"enabled\", False):"
    )

    assert source.count(original_gate) == 1
    assert source.index("if router_single_mode:") < source.index(original_gate)
    assert "if provider is not None and router_single_mode" not in source


def test_router_single_does_not_read_or_write_b5_last_route_memory() -> None:
    single_source = inspect.getsource(TurnRunner._resolve_router_single_provider)
    pipeline_source = inspect.getsource(TurnRunner._run_pipeline)

    assert "_previous_router_dynamic_route" not in single_source
    assert "router_dynamic_last_route" not in single_source
    # The pre-existing B5 branch retains its continuity lookup and commit path.
    assert "_previous_router_dynamic_route" in pipeline_source
    assert "router_dynamic_pending_route_plan" in pipeline_source


def _ranked_model(
    model_id: str = "openai/gpt-5.5",
    *,
    capability: float = 0.9,
) -> RankedModel:
    return RankedModel(
        provider="openrouter",
        model_id=model_id,
        version="test",
        source="test",
        registry_facts={
            "provider": "openrouter",
            "model_id": model_id,
            "status": "enabled",
            "roles": ["proposer"],
            "supports_tools": True,
            "modalities": ["text"],
            "context_window": 200_000,
            "credential_available": True,
        },
        static_profile={"capability": capability},
        online_profile={},
        thinking=None,
    )


class _Catalog:
    def __init__(self) -> None:
        self.output = {
            "openai/gpt-5.5": 4_096,
            "anthropic/claude-sonnet-4.5": 8_192,
        }
        self.context = {
            "openai/gpt-5.5": 128_000,
            "anthropic/claude-sonnet-4.5": 200_000,
        }

    def resolve_max_tokens(
        self,
        model_id: str,
        user_override: int = 0,
        provider: str = "",
    ) -> int:
        del provider
        return user_override if user_override > 0 else self.output[model_id]

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
        del provider
        return self.context[model_id]

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
    ) -> Any:
        from opensquilla.provider import ModelCapabilities

        del model_id, provider_name, base_url
        return ModelCapabilities()


def _resolver_inputs() -> tuple[Any, ProviderConfig, dict[str, Any]]:
    frozen = ranking_config_snapshot()
    ensemble = SimpleNamespace(
        selection_mode="router_dynamic",
        prepared_ranking_config=lambda: frozen,
        ranking_user_profile_enabled=False,
        candidates=[],
        model_options=[],
        ranking_thinking_assignment_enabled=False,
    )
    config = SimpleNamespace(
        llm_ensemble=ensemble,
        llm=SimpleNamespace(
            max_tokens=0,
            context_window_tokens=0,
            temperature=None,
        ),
        squilla_router=SimpleNamespace(tiers={}),
    )
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )
    inputs = {
        "decision_id": "decision-1",
        "ranking_config": frozen,
        "task_analysis": TaskAnalysisResult(
            profile={},
            source="llm_provider",
            schema_valid=True,
            confidence=1.0,
        ),
        "request_context": {
            "conversation": {},
            "tool_state": {},
            "workspace_state": {},
            "intermediate_outputs": {},
            "last_route": {},
            "routing_budget": {
                "estimated_input_tokens": 1,
                "tool_log_tokens": 0,
                "direct_output_tokens": 16_384,
            },
            "input_modalities": ["text"],
            "attachment_refs": [],
            "snapshot_hash": "a" * 64,
        },
    }
    return config, inherited, inputs


def _patch_single_resolver_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], ProviderConfig]:
    import opensquilla.provider.ensemble as ensemble_module
    import opensquilla.provider.ranking_router as ranking_module

    models = [
        _ranked_model("openai/gpt-5.5", capability=0.8),
        _ranked_model("anthropic/claude-sonnet-4.5", capability=0.9),
    ]
    selected = ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4.5",
        api_key="synthetic",
    )
    rank_calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        ranking_module,
        "_legacy_registry_snapshot_projection",
        lambda snapshot: snapshot,
    )
    monkeypatch.setattr(
        ranking_module,
        "build_model_registry_snapshot",
        lambda **kwargs: {
            "snapshot_version": "test",
            "models": [
                {"registry_facts": dict(model.registry_facts)}
                for model in models
            ],
        },
    )

    def rank_single(**kwargs: Any) -> SingleModelRankingDecision:
        rank_calls.append(kwargs)
        selected_facts = kwargs["registry_snapshot"]["models"][1][
            "registry_facts"
        ]
        selected_model = replace(models[1], registry_facts=dict(selected_facts))
        return SingleModelRankingDecision(
            model=selected_model,
            effective_tier=2,
            trace={
                "strategy": "router_dynamic",
                "execution_mode": "router_single",
                "selected_model": selected_model.identity,
                "selected_P": [selected_model.identity],
            },
            thinking_assignment={},
            thinking_assignment_details={},
        )

    monkeypatch.setattr(ranking_module, "rank_single_model", rank_single)
    monkeypatch.setattr(
        ranking_module,
        "dynamic_output_token_budgets",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("single route read fusion budgets")
        ),
    )
    monkeypatch.setattr(
        ranking_module,
        "build_request_context",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("single route built candidate/A context")
        ),
    )
    monkeypatch.setattr(ensemble_module, "_member_from_ref", lambda *a, **k: object())
    monkeypatch.setattr(
        ensemble_module,
        "_member_model_capabilities",
        lambda member: SimpleNamespace(supports_vision=False),
    )

    def deployment(
        ref: Any,
        inherited: ProviderConfig,
        **kwargs: Any,
    ) -> ProviderDeploymentResolution:
        del inherited, kwargs
        deployment_config = replace(selected, model=ref.model)
        return ProviderDeploymentResolution(
            provider=deployment_config.provider,
            model=deployment_config.model,
            ready=True,
            provider_config=deployment_config,
        )

    monkeypatch.setattr(ensemble_module, "_resolve_member_deployment", deployment)
    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        lambda *args, **kwargs: ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        ),
    )
    return rank_calls, selected


def _health_row(*, state: str = "healthy", half_open: bool = False) -> dict[str, Any]:
    return {
        "schema": "opensquilla.provider-health-runtime-facts/v1",
        "provider": "openrouter",
        "model": "model",
        "upstream": "",
        "state": state,
        "fresh": True,
        "half_open_inflight": half_open,
        "eligible": state == "healthy" and not half_open,
    }


def test_single_resolver_freezes_candidate_catalog_budgets_without_fusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rank_calls, selected = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    catalog = _Catalog()
    ledger = SimpleNamespace(runtime_facts=lambda *a, **k: _health_row())

    route = resolve_router_single_route(
        config=config,
        inherited_provider_config=inherited,
        turn_metadata={},
        ranking_inputs=inputs,
        requires_tools=True,
        provider_health_ledger=ledger,
        model_catalog=catalog,
    )

    facts = [
        row["registry_facts"]
        for row in rank_calls[0]["registry_snapshot"]["models"]
    ]
    assert route.provider_config == selected
    assert [row["runtime_direct_output_tokens"] for row in facts] == [4_096, 8_192]
    assert [row["context_window"] for row in facts] == [128_000, 200_000]
    assert route.direct_output_tokens == 8_192
    assert route.context_window_tokens == 200_000
    assert route.trace["selected_P"] == [
        "openrouter:anthropic/claude-sonnet-4.5"
    ]
    assert "selected_A" not in route.trace
    assert "aggregator" not in route.trace


def test_single_resolver_fresh_health_failure_is_fail_closed_without_reselect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rank_calls, _ = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    health_calls = 0

    def runtime_facts(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal health_calls
        del args, kwargs
        health_calls += 1
        return _health_row(state="benched") if health_calls == 3 else _health_row()

    with pytest.raises(DynamicRankingError) as raised:
        resolve_router_single_route(
            config=config,
            inherited_provider_config=inherited,
            turn_metadata={},
            ranking_inputs=inputs,
            requires_tools=False,
            provider_health_ledger=SimpleNamespace(runtime_facts=runtime_facts),
            model_catalog=_Catalog(),
        )

    assert raised.value.reason == "router_single_selected_deployment_unhealthy"
    assert len(rank_calls) == 1
    assert health_calls == 3


def test_agent_bootstrap_consumes_frozen_single_budget_after_catalog_mutation() -> None:
    catalog = _Catalog()
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(llm={"max_tokens": 0, "context_window_tokens": 0}),
        model_catalog=catalog,
    )
    _ROUTER_SINGLE_FROZEN_CATALOG.set(
        {
            "provider": "openrouter",
            "model": "openai/gpt-5.5",
            "max_tokens": 4_096,
            "context_window": 128_000,
        }
    )
    catalog.output["openai/gpt-5.5"] = 99_999
    catalog.context["openai/gpt-5.5"] = 999_999

    resolved = _TurnRunnerModelCatalogAdapter(runner).lookup(
        "openai/gpt-5.5", "openrouter"
    )

    assert resolved.max_tokens == 4_096
    assert resolved.context_window == 128_000
    assert _ROUTER_SINGLE_FROZEN_CATALOG.get() is None


class _HealthLedger:
    def __init__(self, admissions: list[bool] | None = None) -> None:
        self.admissions = list(admissions or [True])
        self.begin_calls: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.successes: list[dict[str, Any]] = []
        self.cancels: list[dict[str, Any]] = []

    def begin_attempt(
        self,
        provider: str,
        model: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        allowed = self.admissions.pop(0) if self.admissions else False
        row = {"provider": provider, "model": model, **kwargs}
        self.begin_calls.append(row)
        return {
            "allowed": allowed,
            "reason": "half_open_busy" if not allowed else "",
            "lease_token": f"lease-{len(self.begin_calls)}" if allowed else None,
            "started_at": time.monotonic(),
        }

    def record_failure(
        self,
        provider: str,
        model: str,
        kind: ProviderFailureKind,
        **kwargs: Any,
    ) -> None:
        self.failures.append(
            {"provider": provider, "model": model, "kind": kind, **kwargs}
        )

    def record_success(self, provider: str, model: str, **kwargs: Any) -> None:
        self.successes.append({"provider": provider, "model": model, **kwargs})

    def cancel_attempt(self, provider: str, model: str, **kwargs: Any) -> None:
        self.cancels.append({"provider": provider, "model": model, **kwargs})


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _real_health_ledger(clock: _FakeClock) -> ProviderHealthLedger:
    return ProviderHealthLedger(
        failure_threshold=1,
        cooldown_s=1.0,
        max_cooldown_s=2.0,
        clock=clock,
    )


def _real_health_facts(ledger: ProviderHealthLedger) -> dict[str, Any]:
    cfg = _direct_config()
    return ledger.runtime_facts(
        cfg.provider,
        cfg.model,
        upstream="anthropic",
    )


def _bench_real_health(ledger: ProviderHealthLedger) -> None:
    cfg = _direct_config()
    ledger.record_failure(
        cfg.provider,
        cfg.model,
        ProviderFailureKind.RATE_LIMITED,
        upstream="anthropic",
    )


def _direct_config() -> ProviderConfig:
    model = "anthropic/claude-sonnet-4.5"
    return ProviderConfig(
        provider="openrouter",
        model=model,
        api_key="synthetic",
        provider_routing={model: "anthropic"},
    )


class _BlockingStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    def __aiter__(self) -> _BlockingStream:
        return self

    async def __anext__(self) -> Any:
        self.started.set()
        await self.release.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_started.set()
        await self.close_release.wait()


class _BlockingProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.streams: list[_BlockingStream] = []

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        stream = _BlockingStream()
        self.streams.append(stream)
        return stream


async def test_direct_deadline_records_timeout_failure_and_bounds_stubborn_close() -> None:
    ledger = _HealthLedger([True])
    raw = _BlockingProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 0.03,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    provider._STREAM_CLOSE_TIMEOUT_SECONDS = 0.03
    started = time.monotonic()

    events = [
        event
        async for event in provider.chat([], config=ChatConfig(timeout=30.0))
    ]
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert raw.calls == 1
    assert len(events) == 1
    assert events[0].code == "router_single_absolute_deadline"
    assert events[0].request_started is True
    assert events[0].physical_request_count == 1
    assert len(ledger.failures) == 1
    assert ledger.failures[0]["kind"] is ProviderFailureKind.TRANSPORT_TRANSIENT
    assert ledger.failures[0]["upstream"] == "anthropic"
    assert ledger.failures[0]["lease_token"] == "lease-1"
    assert ledger.cancels == []
    raw.streams[0].close_release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_direct_real_health_ledger_rejects_bench_then_settles_probe_success() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    _bench_real_health(ledger)
    raw = _TimedDoneProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    rejected = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))

    assert raw.calls == []
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0
    assert _real_health_facts(ledger)["state"] == "benched"

    clock.advance(1.1)
    completed = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _real_health_facts(ledger)

    assert completed[-1].kind == "done"
    assert len(raw.calls) == 1
    assert facts["upstream"] == "anthropic"
    assert facts["state"] == "healthy"
    assert facts["half_open_inflight"] is False
    assert facts["recent_successes"] == 1


async def test_direct_real_health_ledger_timeout_rebenches_half_open_probe() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    _bench_real_health(ledger)
    clock.advance(1.1)
    raw = _BlockingProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 0.03,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    provider._STREAM_CLOSE_TIMEOUT_SECONDS = 0.03

    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _real_health_facts(ledger)

    assert raw.calls == 1
    assert events[-1].request_started is True
    assert facts["upstream"] == "anthropic"
    assert facts["state"] == "benched"
    assert facts["half_open_inflight"] is False
    assert facts["last_failure_kind"] == "transport_transient"
    raw.streams[0].close_release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_half_open_atomic_admission_allows_only_one_physical_request() -> None:
    ledger = _HealthLedger([True, False])
    raw = _BlockingProvider()
    first = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 1.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    second = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 1.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    first_task = asyncio.create_task(
        _collect(first.chat([], config=ChatConfig(timeout=30.0)))
    )
    while not raw.streams:
        await asyncio.sleep(0)
    await raw.streams[0].started.wait()

    rejected = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))
    raw.streams[0].close_release.set()
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task

    assert raw.calls == 1
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0
    assert second.retry_failed_call_safe is False
    assert [call["never_strand_exempt"] for call in ledger.begin_calls] == [
        False,
        False,
    ]
    assert all(call["upstream"] == "anthropic" for call in ledger.begin_calls)
    assert len(ledger.cancels) == 1


class _AtomicHalfOpenLedger(_HealthLedger):
    def __init__(self) -> None:
        super().__init__([])
        self.active = False

    def begin_attempt(
        self,
        provider: str,
        model: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        allowed = not self.active
        if allowed:
            self.active = True
        self.begin_calls.append({"provider": provider, "model": model, **kwargs})
        return {
            "allowed": allowed,
            "reason": "half_open_busy" if not allowed else "",
            "lease_token": "half-open-lease" if allowed else None,
            "started_at": time.monotonic(),
        }

    def cancel_attempt(self, provider: str, model: str, **kwargs: Any) -> None:
        super().cancel_attempt(provider, model, **kwargs)
        self.active = False


class _CloseGateStream:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    def __aiter__(self) -> _CloseGateStream:
        return self

    async def __anext__(self) -> Any:
        self.started.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_started.set()
        await self.close_release.wait()


class _CloseGateProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.stream = _CloseGateStream()

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return self.stream


async def test_half_open_lease_is_held_until_cancelled_stream_closes() -> None:
    ledger = _AtomicHalfOpenLedger()
    raw = _CloseGateProvider()
    first = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 5.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    second = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 5.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    first_task = asyncio.create_task(
        _collect(first.chat([], config=ChatConfig(timeout=30.0)))
    )
    await asyncio.wait_for(raw.stream.started.wait(), timeout=0.5)

    first_task.cancel()
    await asyncio.wait_for(raw.stream.close_started.wait(), timeout=0.5)
    rejected = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))

    assert ledger.active is True
    assert raw.calls == 1
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0

    raw.stream.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await first_task
    assert ledger.active is False
    assert len(ledger.cancels) == 1


async def test_direct_real_health_ledger_cancel_releases_probe_after_close() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    _bench_real_health(ledger)
    clock.advance(1.1)
    raw = _CloseGateProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 5.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    task = asyncio.create_task(
        _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    )
    await asyncio.wait_for(raw.stream.started.wait(), timeout=0.5)

    task.cancel()
    await asyncio.wait_for(raw.stream.close_started.wait(), timeout=0.5)
    during_close = _real_health_facts(ledger)
    assert during_close["state"] == "half_open"
    assert during_close["half_open_inflight"] is True

    raw.stream.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    after_close = _real_health_facts(ledger)
    assert after_close["state"] == "half_open"
    assert after_close["half_open_inflight"] is False


class _TimedDoneProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ChatConfig]] = []

    def chat(
        self,
        messages: list[Any],
        tools: Any = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages
        assert config is not None
        self.calls.append((tools, config))
        call_number = len(self.calls)

        async def stream() -> AsyncIterator[Any]:
            if call_number == 1:
                await asyncio.sleep(0.04)
            yield ProviderDone(
                stop_reason="stop",
                input_tokens=10,
                output_tokens=1,
            )

        return stream()


async def test_direct_tool_loop_calls_share_one_absolute_deadline() -> None:
    deadline = time.monotonic() + 0.15
    ledger = _HealthLedger([True, True])
    raw = _TimedDoneProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config(),
        health_ledger=ledger,
        absolute_deadline=deadline,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    tools = [object()]

    first = await _collect(
        provider.chat([], tools=tools, config=ChatConfig(timeout=30.0))
    )
    second = await _collect(
        provider.chat([], tools=tools, config=ChatConfig(timeout=30.0))
    )

    assert first[-1].kind == "done"
    assert second[-1].kind == "done"
    assert len(raw.calls) == 2
    assert raw.calls[0][0] is tools
    assert raw.calls[1][0] is tools
    first_timeout = float(raw.calls[0][1].timeout)
    second_timeout = float(raw.calls[1][1].timeout)
    assert 0 < second_timeout < first_timeout - 0.02

    await asyncio.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
    rejected = await _collect(
        provider.chat([], tools=tools, config=ChatConfig(timeout=30.0))
    )
    assert len(raw.calls) == 2
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0
    assert provider.retry_failed_call_safe is False


async def _collect(stream: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in stream]


class _SequenceProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls: list[ChatConfig] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools
        assert config is not None
        self.calls.append(config)
        index = len(self.calls)

        async def stream() -> AsyncIterator[Any]:
            if index == 1:
                yield ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=5,
                    reasoning_tokens=5,
                    reasoning_content="reasoning only",
                )
                return
            yield ProviderText(text="ok")
            yield ProviderDone(stop_reason="stop", input_tokens=11, output_tokens=1)

        return stream()

    async def list_models(self) -> list[Any]:
        return []


class _OneModelSelector:
    active_provider_id = "openrouter"
    current_config = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )

    def __init__(self) -> None:
        self.fallback_calls = 0

    def has_fallback(self) -> bool:
        return False

    def remaining_chain(self) -> list[ProviderConfig]:
        return [self.current_config]

    def next_fallback_after_failure(self, error: Exception) -> Any:
        self.fallback_calls += 1
        raise IndexError("single route has no fallback")


async def test_managed_single_uses_provider_native_max_and_cannot_downgrade() -> None:
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(llm={"thinking": "high"}),
    )
    turn = SimpleNamespace(
        metadata={
            "thinking_requested": True,
            "thinking_level": "max",
            "_router_single_managed_provider_thinking_level": "max",
        }
    )
    resolved = runner._resolve_turn_thinking(turn)
    assert resolved is ThinkingLevel.MAX

    raw = _SequenceProvider()
    direct = _RouterSingleDirectProvider(
        raw,
        _OneModelSelector.current_config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=True,
    )
    wrapped = _SelectorFallbackProvider(direct, _OneModelSelector())
    agent = Agent(
        provider=wrapped,
        config=AgentConfig(
            thinking=resolved,
            reasoning_only_thinking_fallback=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert wrapped._routed_thinking_policy_blocks_fallback() is True
    assert len(raw.calls) == 1
    assert [config.thinking_level for config in raw.calls] == [
        ThinkingLevel.MAX,
    ]
    assert all(config.thinking is True for config in raw.calls)


class _ToolLoopProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls: list[ChatConfig] = []
        self.tools_by_call: list[list[Any] | None] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages
        assert config is not None
        self.calls.append(config)
        self.tools_by_call.append(tools)
        call_number = len(self.calls)

        async def stream() -> AsyncIterator[Any]:
            if call_number == 1:
                yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="echo")
                yield ProviderToolUseEnd(
                    tool_use_id="tool-1",
                    tool_name="echo",
                    arguments={"value": "again"},
                )
                yield ProviderDone(
                    stop_reason="tool_use",
                    input_tokens=10,
                    output_tokens=1,
                )
                return
            yield ProviderText(text="ok")
            yield ProviderDone(stop_reason="stop", input_tokens=11, output_tokens=1)

        return stream()


async def test_managed_single_tool_loop_keeps_model_tools_thinking_and_usage() -> None:
    raw = _ToolLoopProvider()
    direct = _RouterSingleDirectProvider(
        raw,
        _OneModelSelector.current_config,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=True,
    )
    selector = _OneModelSelector()
    wrapped = _SelectorFallbackProvider(direct, selector)
    tool_definition = ToolDefinition(
        name="echo",
        description="Echo.",
        input_schema=ToolInputSchema(),
    )

    async def echo_tool(call: Any) -> ToolResult:
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    agent = Agent(
        provider=wrapped,
        config=AgentConfig(thinking=ThinkingLevel.MAX),
        tool_definitions=[tool_definition],
        tool_handler=echo_tool,
    )

    events = [event async for event in agent.run_turn("hello")]

    done = next(event for event in events if event.kind == "done")
    assert len(raw.calls) == 2
    assert [config.thinking_level for config in raw.calls] == [
        ThinkingLevel.MAX,
        ThinkingLevel.MAX,
    ]
    assert all(config.thinking is True for config in raw.calls)
    assert raw.tools_by_call == [[tool_definition], [tool_definition]]
    assert selector.fallback_calls == 0
    assert done.input_tokens == 21
    assert done.output_tokens == 2
    assert not any(event.kind == "ensemble_progress" for event in events)


def test_single_realigns_routed_model_and_clears_stale_savings() -> None:
    metadata = {
        "routed_model": "old/model",
        "savings_pct": 25.0,
        "savings_max_price_per_m": 10.0,
        "savings_routed_price_per_m": 1.0,
    }
    wrapped = _SelectorFallbackProvider(
        _NoChatProvider(),
        _OneModelSelector(),
        turn_metadata=metadata,
    )

    wrapped._realign_routed_model_after_fallback()

    assert metadata["routed_model"] == "openai/gpt-5.5"
    assert metadata["executed_provider"] == "openrouter"
    assert metadata["executed_model"] == "openai/gpt-5.5"
    assert metadata["savings_pct"] == 0.0
    assert metadata["savings_max_price_per_m"] == 0.0
    assert metadata["savings_routed_price_per_m"] == 0.0


class _ErrorProvider:
    provider_name = "openai"

    def __init__(self, code: str) -> None:
        self.code = code

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config

        async def stream() -> AsyncIterator[Any]:
            yield ProviderError(message="provider rejected request", code=self.code)

        return stream()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("429", ProviderFailureKind.RATE_LIMITED),
        ("401", ProviderFailureKind.AUTH_INVALID),
        ("402", ProviderFailureKind.INSUFFICIENT_CREDITS),
    ],
)
async def test_single_pool_failure_uses_configured_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected: ProviderFailureKind,
) -> None:
    import opensquilla.gateway.llm_runtime as llm_runtime

    reports: list[tuple[str, str, ProviderFailureKind]] = []
    pool = SimpleNamespace(
        report_failure=lambda provider, session, kind, retry_after_seconds=None: reports.append(
            (provider, session, kind)
        )
    )
    monkeypatch.setattr(llm_runtime, "profile_credential_pools", lambda: pool)
    metadata = {
        "_router_single_provider_finalized": True,
        "routed_provider_applied": "openrouter",
        "credential_pool": {
            "provider": "openrouter",
            "session_key": "agent:main:pool",
        },
    }
    wrapped = _SelectorFallbackProvider(
        _ErrorProvider(code),
        _OneModelSelector(),
        turn_metadata=metadata,
    )

    await _collect(wrapped.chat([], config=ChatConfig()))

    assert reports == [("openrouter", "agent:main:pool", expected)]


def test_single_resolver_requires_authoritative_catalog_before_ranking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rank_calls, _ = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()

    with pytest.raises(DynamicRankingError) as raised:
        resolve_router_single_route(
            config=config,
            inherited_provider_config=inherited,
            turn_metadata={},
            ranking_inputs=inputs,
            requires_tools=False,
            provider_health_ledger=None,
            model_catalog=None,
        )

    assert raised.value.reason == "router_single_model_catalog_unavailable"
    assert rank_calls == []


async def test_missing_catalog_fails_before_analyzer_admission_or_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.admission as admission_module
    import opensquilla.provider.ranking_router as ranking_module

    analyzer_calls = 0
    admission_calls = 0

    async def analyzer_spy(*args: Any, **kwargs: Any) -> Any:
        nonlocal analyzer_calls
        del args, kwargs
        analyzer_calls += 1
        raise AssertionError("missing catalog must fail before Analyzer")

    def admission_spy(*args: Any, **kwargs: Any) -> Any:
        nonlocal admission_calls
        del args, kwargs
        admission_calls += 1
        raise AssertionError("missing catalog must fail before admission")

    monkeypatch.setattr(ranking_module, "analyze_task_with_provider", analyzer_spy)
    monkeypatch.setattr(
        ranking_module,
        "analyze_task_with_fallback_chain",
        analyzer_spy,
    )
    monkeypatch.setattr(
        admission_module,
        "get_shared_provider_admission_controller",
        admission_spy,
    )
    config = _router_single_config()
    runner = TurnRunner(
        provider_selector=None,
        config=config,
        model_catalog=None,
    )
    provider = _NoChatProvider()
    turn = SimpleNamespace(
        metadata={},
        semantic_message="hello",
        attachments=[],
        session_key="agent:main:missing-catalog",
    )

    with pytest.raises(DynamicRankingError) as raised:
        await runner._resolve_router_single_provider(
            turn=turn,
            provider=provider,
            cloned_selector=_Selector(),
            turn_config=config,
            ensemble_cfg=config.llm_ensemble,
            turn_absolute_deadline=None,
        )

    assert raised.value.reason == "router_single_model_catalog_unavailable"
    assert analyzer_calls == 0
    assert admission_calls == 0
    assert provider.calls == 0


@pytest.mark.parametrize(
    ("inherited_provider", "inherited_model", "inherited_replay", "expected"),
    [
        ("OPENROUTER", "anthropic/claude-sonnet-4.5", True, True),
        ("openrouter", "anthropic/claude-sonnet-4.5", False, False),
        ("openrouter", "openai/gpt-5.5", True, False),
        ("anthropic", "anthropic/claude-sonnet-4.5", True, False),
    ],
)
def test_single_replays_private_provider_state_only_for_exact_identity(
    monkeypatch: pytest.MonkeyPatch,
    inherited_provider: str,
    inherited_model: str,
    inherited_replay: bool,
    expected: bool,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module

    _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    inherited = replace(
        inherited,
        provider=inherited_provider,
        model=inherited_model,
        replay_provider_state=inherited_replay,
    )
    replay_arguments: list[bool | None] = []

    def resolve_selected(*args: Any, **kwargs: Any) -> ProviderDeploymentResolution:
        del args
        replay = kwargs.get("replay_provider_state")
        replay_arguments.append(replay)
        selected = ProviderConfig(
            provider="openrouter",
            model="anthropic/claude-sonnet-4.5",
            api_key="synthetic",
            replay_provider_state=bool(replay),
        )
        return ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        )

    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        resolve_selected,
    )
    route = resolve_router_single_route(
        config=config,
        inherited_provider_config=inherited,
        turn_metadata={},
        ranking_inputs=inputs,
        requires_tools=False,
        model_catalog=_Catalog(),
    )

    assert replay_arguments == [expected]
    assert route.provider_config.replay_provider_state is expected


def test_single_freezes_capabilities_from_selected_endpoint_without_leaking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    from opensquilla.provider import ModelCapabilities

    _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    endpoint = "https://selected-endpoint.invalid/v1"
    selected = ProviderConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4.5",
        api_key="secret-never-project",
        base_url=endpoint,
        replay_provider_state=False,
    )
    capability = ModelCapabilities(
        supports_reasoning=True,
        supports_tools=False,
        supports_streaming=True,
        reasoning_format="openrouter",
    )
    capability_calls: list[tuple[str, str, str]] = []

    class EndpointCatalog(_Catalog):
        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "",
            base_url: str = "",
        ) -> ModelCapabilities:
            capability_calls.append((model_id, provider_name, base_url))
            return capability

    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        lambda *args, **kwargs: ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        ),
    )
    route = resolve_router_single_route(
        config=config,
        inherited_provider_config=inherited,
        turn_metadata={},
        ranking_inputs=inputs,
        requires_tools=False,
        model_catalog=EndpointCatalog(),
    )

    assert route.model_capabilities is capability
    assert capability_calls == [(selected.model, selected.provider, endpoint)]
    assert endpoint not in repr(route)
    assert endpoint not in repr(route.trace)
    assert selected.api_key not in repr(route)


@pytest.mark.parametrize(
    ("requires_tools", "input_modalities", "capabilities", "expected_reason"),
    [
        (
            True,
            ["text"],
            {"supports_tools": False, "supports_vision": True},
            "router_single_selected_model_tools_unavailable",
        ),
        (
            False,
            ["text", "image"],
            {"supports_tools": True, "supports_vision": False},
            "router_single_selected_model_vision_unavailable",
        ),
    ],
)
def test_selected_endpoint_capability_mismatch_fails_before_generation(
    monkeypatch: pytest.MonkeyPatch,
    requires_tools: bool,
    input_modalities: list[str],
    capabilities: dict[str, bool],
    expected_reason: str,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    from opensquilla.provider import ModelCapabilities

    rank_calls, selected = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    inputs["request_context"] = {
        **inputs["request_context"],
        "input_modalities": input_modalities,
    }

    class EndpointCatalog(_Catalog):
        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "",
            base_url: str = "",
        ) -> ModelCapabilities:
            del model_id, provider_name, base_url
            return ModelCapabilities(**capabilities)

    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        lambda *args, **kwargs: ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        ),
    )

    with pytest.raises(DynamicRankingError) as raised:
        resolve_router_single_route(
            config=config,
            inherited_provider_config=inherited,
            turn_metadata={},
            ranking_inputs=inputs,
            requires_tools=requires_tools,
            model_catalog=EndpointCatalog(),
        )

    assert raised.value.reason == expected_reason
    assert len(rank_calls) == 1


def test_anthropic_capabilities_become_authoritative_when_migration_gate_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    import opensquilla.provider.model_catalog as model_catalog_module
    from opensquilla.provider import ModelCapabilities

    rank_calls, _ = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    inputs["request_context"] = {
        **inputs["request_context"],
        "input_modalities": ["text", "image"],
    }
    selected = ProviderConfig(
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_key="synthetic",
    )
    monkeypatch.setattr(
        model_catalog_module,
        "CATALOG_CAPABILITIES_FOR_ANTHROPIC_OLLAMA",
        True,
    )
    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        lambda *args, **kwargs: ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        ),
    )

    class EndpointCatalog(_Catalog):
        def get_capabilities(
            self,
            model_id: str,
            provider_name: str = "",
            base_url: str = "",
        ) -> ModelCapabilities:
            del model_id, provider_name, base_url
            return ModelCapabilities(supports_vision=False)

    with pytest.raises(DynamicRankingError) as raised:
        resolve_router_single_route(
            config=config,
            inherited_provider_config=inherited,
            turn_metadata={},
            ranking_inputs=inputs,
            requires_tools=False,
            model_catalog=EndpointCatalog(),
        )

    assert raised.value.reason == "router_single_selected_model_vision_unavailable"
    assert len(rank_calls) == 1


@pytest.mark.parametrize(
    (
        "provider_id",
        "model_id",
        "supports_reasoning",
        "reasoning_format",
        "expect_failure",
    ),
    [
        ("openrouter", "anthropic/claude-sonnet-4.5", False, "none", True),
        ("anthropic", "claude-sonnet-4-6", False, "none", False),
        ("openai_codex", "gpt-5.5-codex", False, "none", False),
        ("deepseek", "deepseek-v4-pro", False, "none", True),
        ("deepseek", "deepseek-v4-pro", False, "deepseek", False),
        ("openrouter", "openai/gpt-5.5", True, "none", True),
        ("openrouter", "openai/gpt-5.5", True, "unknown", True),
        ("openrouter", "deepseek/deepseek-r1", True, "deepseek", False),
        ("ollama", "llama3", False, "none", True),
        ("openai_responses", "gpt-5.5", False, "none", True),
    ],
)
def test_managed_thinking_uses_selected_adapter_execution_contract(
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    model_id: str,
    supports_reasoning: bool,
    reasoning_format: str,
    expect_failure: bool,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module
    import opensquilla.provider.ranking_router as ranking_module
    from opensquilla.provider import ModelCapabilities

    rank_calls, _ = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    if provider_id == "anthropic":
        inputs["request_context"] = {
            **inputs["request_context"],
            "input_modalities": ["text", "image"],
        }
    original_rank = ranking_module.rank_single_model

    def managed_rank(**kwargs: Any) -> SingleModelRankingDecision:
        decision = original_rank(**kwargs)
        selected_model = replace(
            decision.model,
            provider=provider_id,
            model_id=model_id,
            registry_facts={
                **decision.model.registry_facts,
                "provider": provider_id,
                "model_id": model_id,
            },
            thinking="high",
            requested_thinking_level="high",
            effective_thinking_level="high",
            thinking_policy_version="router-single-test/v1",
        )
        return replace(
            decision,
            model=selected_model,
            trace={
                **decision.trace,
                "selected_model": selected_model.identity,
                "selected_P": [selected_model.identity],
            },
        )

    monkeypatch.setattr(ranking_module, "rank_single_model", managed_rank)
    selected = ProviderConfig(
        provider=provider_id,
        model=model_id,
        api_key="synthetic",
    )
    monkeypatch.setattr(
        ensemble_module,
        "resolve_provider_deployment",
        lambda *args, **kwargs: ProviderDeploymentResolution(
            provider=selected.provider,
            model=selected.model,
            ready=True,
            provider_config=selected,
        ),
    )

    class EndpointCatalog(_Catalog):
        def get_capabilities(
            self,
            resolved_model_id: str,
            provider_name: str = "",
            base_url: str = "",
        ) -> ModelCapabilities:
            del resolved_model_id, provider_name, base_url
            return ModelCapabilities(
                supports_reasoning=supports_reasoning,
                reasoning_format=reasoning_format,
            )

    if expect_failure:
        with pytest.raises(DynamicRankingError) as raised:
            resolve_router_single_route(
                config=config,
                inherited_provider_config=inherited,
                turn_metadata={},
                ranking_inputs=inputs,
                requires_tools=False,
                model_catalog=EndpointCatalog(),
            )
        assert (
            raised.value.reason
            == "router_single_selected_model_reasoning_unavailable"
        )
    else:
        route = resolve_router_single_route(
            config=config,
            inherited_provider_config=inherited,
            turn_metadata={},
            ranking_inputs=inputs,
            requires_tools=False,
            model_catalog=EndpointCatalog(),
        )
        assert route.provider_config == selected
        assert route.model_capabilities.supports_reasoning is supports_reasoning
    assert len(rank_calls) == 1


def test_frozen_single_capabilities_bypass_mutated_live_catalog() -> None:
    from opensquilla.provider import ModelCapabilities

    capability = ModelCapabilities(
        supports_reasoning=True,
        supports_tools=False,
        supports_streaming=True,
        reasoning_format="deepseek",
    )

    class ExplodingCatalog:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"live catalog access after freeze: {name}")

    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(llm={"max_tokens": 0, "context_window_tokens": 0}),
        model_catalog=ExplodingCatalog(),
    )
    token = _ROUTER_SINGLE_FROZEN_CATALOG.set(
        {
            "provider": "openrouter",
            "model": "deepseek/deepseek-r1",
            "max_tokens": 8_192,
            "context_window": 128_000,
            "capabilities": capability,
        }
    )
    try:
        resolved = _TurnRunnerModelCatalogAdapter(runner).lookup(
            "deepseek/deepseek-r1",
            "openrouter",
        )
    finally:
        _ROUTER_SINGLE_FROZEN_CATALOG.reset(token)

    assert resolved.max_tokens == 8_192
    assert resolved.context_window == 128_000
    assert resolved.capabilities is capability


def test_single_allowlist_candidates_still_receive_rank_time_health_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.provider.ensemble as ensemble_module

    rank_calls, _ = _patch_single_resolver_dependencies(monkeypatch)
    config, inherited, inputs = _resolver_inputs()
    health_calls: list[str] = []

    monkeypatch.setattr(
        ensemble_module,
        "_apply_router_dynamic_registry_allowlist",
        lambda *args, **kwargs: {
            "candidate_scope": "configured",
            "expected_identities": [],
        },
    )

    def runtime_facts(
        provider: str,
        model: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        health_calls.append(model)
        row = _health_row(
            state="benched" if model == "openai/gpt-5.5" else "healthy"
        )
        row.update({"provider": provider, "model": model})
        return row

    resolve_router_single_route(
        config=config,
        inherited_provider_config=inherited,
        turn_metadata={},
        ranking_inputs={**inputs, "registry_allowlist": {"enabled": True}},
        requires_tools=False,
        provider_health_ledger=SimpleNamespace(runtime_facts=runtime_facts),
        model_catalog=_Catalog(),
    )

    ranked_rows = rank_calls[0]["registry_snapshot"]["models"]
    first_facts = ranked_rows[0]["registry_facts"]
    second_facts = ranked_rows[1]["registry_facts"]
    assert health_calls == [
        "openai/gpt-5.5",
        "anthropic/claude-sonnet-4.5",
        "anthropic/claude-sonnet-4.5",
    ]
    assert first_facts["runtime_health"]["state"] == "benched"
    assert first_facts["runtime_hard_filter_reasons_by_role"]["proposer"] == [
        "runtime_deployment_benched"
    ]
    assert second_facts["runtime_health"]["state"] == "healthy"


def _direct_config_for(model: str) -> ProviderConfig:
    return ProviderConfig(
        provider="openrouter",
        model=model,
        api_key="synthetic",
        provider_routing={model: "anthropic"},
    )


def _facts_for(
    ledger: ProviderHealthLedger,
    config: ProviderConfig,
) -> dict[str, Any]:
    return ledger.runtime_facts(
        config.provider,
        config.model,
        upstream="anthropic",
    )


class _DoneWithoutCloseStream:
    def __init__(self) -> None:
        self.sent = False

    def __aiter__(self) -> _DoneWithoutCloseStream:
        return self

    async def __anext__(self) -> Any:
        if self.sent:
            raise StopAsyncIteration
        self.sent = True
        return ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=1)


class _DoneWithoutCloseProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return _DoneWithoutCloseStream()


async def test_direct_terminal_without_aclose_is_a_valid_stream_boundary() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for("test/done-without-close")
    raw = _DoneWithoutCloseProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _facts_for(ledger, config)

    assert [event.kind for event in events] == ["done"]
    assert raw.calls == 1
    assert facts["state"] == "healthy"
    assert facts["recent_successes"] == 1


class _TerminalThenBlockingStream:
    def __init__(self) -> None:
        self.sent = False
        self.second_next_called = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    def __aiter__(self) -> _TerminalThenBlockingStream:
        return self

    async def __anext__(self) -> Any:
        if not self.sent:
            self.sent = True
            return ProviderDone(stop_reason="stop", input_tokens=3, output_tokens=1)
        self.second_next_called.set()
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_started.set()
        await self.close_release.wait()


class _TerminalThenBlockingProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.stream = _TerminalThenBlockingStream()

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return self.stream


async def test_direct_stops_at_terminal_and_withholds_it_until_close_returns() -> None:
    ledger = _HealthLedger([True])
    raw = _TerminalThenBlockingProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        _direct_config_for("test/terminal-close"),
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    task = asyncio.create_task(
        _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    )

    await asyncio.wait_for(raw.stream.close_started.wait(), timeout=0.5)
    assert task.done() is False
    assert raw.stream.second_next_called.is_set() is False
    assert ledger.successes == []
    raw.stream.close_release.set()
    events = await asyncio.wait_for(task, timeout=0.5)

    assert [event.kind for event in events] == ["done"]
    assert len(ledger.successes) == 1


async def test_cleanup_gate_blocks_past_health_cooldown_until_close_finishes() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for("test/pending-cleanup")
    first_raw = _BlockingProvider()
    first = _RouterSingleDirectProvider(
        first_raw,
        config,
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 0.02,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    first._STREAM_CLOSE_TIMEOUT_SECONDS = 0.02

    first_events = await _collect(first.chat([], config=ChatConfig(timeout=30.0)))
    assert first_events[-1].request_started is True
    assert _facts_for(ledger, config)["state"] == "benched"

    clock.advance(1.1)
    eligible = _facts_for(ledger, config)
    assert eligible["state"] == "half_open"
    assert eligible["eligible"] is True

    second_raw = _TimedDoneProvider()
    second = _RouterSingleDirectProvider(
        second_raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    rejected = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))

    assert second_raw.calls == []
    assert rejected[0].code == "router_single_cleanup_pending"
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0

    first_raw.streams[0].close_release.set()
    for _ in range(4):
        await asyncio.sleep(0)
    completed = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))

    assert completed[-1].kind == "done"
    assert len(second_raw.calls) == 1
    assert _facts_for(ledger, config)["state"] == "healthy"


class _FailingCloseStream:
    def __init__(self) -> None:
        self.close_calls = 0

    def __aiter__(self) -> _FailingCloseStream:
        return self

    async def __anext__(self) -> Any:
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        raise RuntimeError("synthetic close failure")


class _FailingCloseProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.stream = _FailingCloseStream()

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return self.stream


async def test_failed_required_close_sticky_poison_blocks_later_dispatch() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for("test/poisoned-cleanup")
    raw = _FailingCloseProvider()
    first = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 0.02,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    await _collect(first.chat([], config=ChatConfig(timeout=30.0)))
    assert raw.stream.close_calls == 1
    clock.advance(10.0)

    second_raw = _TimedDoneProvider()
    second = _RouterSingleDirectProvider(
        second_raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    rejected = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))

    assert second_raw.calls == []
    assert rejected[0].code == "router_single_cleanup_poisoned"
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0


class _LazyNeverStartedStream:
    def __init__(self) -> None:
        self.next_calls = 0
        self.close_calls = 0

    def __aiter__(self) -> _LazyNeverStartedStream:
        return self

    async def __anext__(self) -> Any:
        self.next_calls += 1
        await asyncio.Event().wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_calls += 1
        await asyncio.Event().wait()


class _LazyNeverStartedProvider:
    provider_name = "openrouter"

    def __init__(self) -> None:
        self.calls = 0
        self.stream = _LazyNeverStartedStream()

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return self.stream


async def test_deadline_before_first_anext_does_not_poison_cleanup_gate() -> None:
    ledger = _HealthLedger([True, True])
    config = _direct_config_for("test/pre-anext-deadline")
    raw = _LazyNeverStartedProvider()
    first = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=time.monotonic() + 30.0,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    first._STREAM_CLOSE_TIMEOUT_SECONDS = 0.01
    remaining = iter([1.0, 1.0, 0.0])
    first._remaining_seconds = lambda: next(remaining)  # type: ignore[method-assign]

    rejected = await _collect(first.chat([], config=ChatConfig(timeout=30.0)))

    assert raw.calls == 1
    assert raw.stream.next_calls == 0
    assert rejected[0].request_started is False
    assert rejected[0].physical_request_count == 0
    assert len(ledger.cancels) == 1

    second_raw = _TimedDoneProvider()
    second = _RouterSingleDirectProvider(
        second_raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )
    completed = await _collect(second.chat([], config=ChatConfig(timeout=30.0)))

    assert completed[-1].kind == "done"
    assert len(second_raw.calls) == 1


class _ErrorEventProvider:
    provider_name = "openrouter"

    def __init__(self, event: ProviderError) -> None:
        self.event = event
        self.calls = 0

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1

        async def stream() -> AsyncIterator[Any]:
            yield self.event

        return stream()


@pytest.mark.parametrize("request_started", [False, True])
async def test_provider_error_zero_request_cancels_probe_but_started_error_benches(
    request_started: bool,
) -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for(f"test/error-started-{request_started}")
    ledger.record_failure(
        config.provider,
        config.model,
        ProviderFailureKind.RATE_LIMITED,
        upstream="anthropic",
    )
    clock.advance(1.1)
    event = ProviderError(
        message="synthetic provider error",
        code="429",
        request_started=request_started,
        physical_request_count=1 if request_started else 0,
    )
    raw = _ErrorEventProvider(event)
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    observed = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _facts_for(ledger, config)

    assert observed == [event]
    assert facts["half_open_inflight"] is False
    assert facts["state"] == ("benched" if request_started else "half_open")


class _MissingTerminalStream:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.close_called = False

    def __aiter__(self) -> _MissingTerminalStream:
        return self

    async def __anext__(self) -> Any:
        if self.error is not None:
            raise self.error
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_called = True


class _MissingTerminalProvider:
    provider_name = "openrouter"

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.stream = _MissingTerminalStream(error)

    def chat(self, messages: list[Any], tools: Any = None, config: Any = None) -> Any:
        del messages, tools, config
        self.calls += 1
        return self.stream


async def test_empty_stream_records_transport_failure_in_real_ledger() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for("test/empty-stream")
    raw = _MissingTerminalProvider()
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    events = await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _facts_for(ledger, config)

    assert events == []
    assert raw.stream.close_called is True
    assert facts["state"] == "benched"
    assert facts["last_failure_kind"] == "transport_transient"


async def test_iterator_exception_records_transport_failure_in_real_ledger() -> None:
    clock = _FakeClock()
    ledger = _real_health_ledger(clock)
    config = _direct_config_for("test/iterator-exception")
    raw = _MissingTerminalProvider(RuntimeError("synthetic iterator failure"))
    provider = _RouterSingleDirectProvider(
        raw,
        config,
        health_ledger=ledger,
        absolute_deadline=None,
        frozen_catalog={},
        enforces_routed_thinking_policy=False,
    )

    with pytest.raises(RuntimeError, match="synthetic iterator failure"):
        await _collect(provider.chat([], config=ChatConfig(timeout=30.0)))
    facts = _facts_for(ledger, config)

    assert raw.stream.close_called is True
    assert facts["state"] == "benched"
    assert facts["last_failure_kind"] == "transport_transient"


def test_b5_pipeline_does_not_touch_router_single_contextvar() -> None:
    sentinel = {
        "provider": "sentinel",
        "model": "sentinel",
        "max_tokens": 1,
        "context_window": 1,
    }
    config = GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm_ensemble={"enabled": False, "mode": "multiple"},
    )
    runner = TurnRunner(provider_selector=None, config=config)

    async def run() -> None:
        token = _ROUTER_SINGLE_FROZEN_CATALOG.set(sentinel)
        try:
            await runner._run_pipeline(
                "hello",
                "agent:main:b5-contextvar",
                _NoChatProvider(),
                _Selector(),
                [],
                "system",
                [],
            )
            assert _ROUTER_SINGLE_FROZEN_CATALOG.get() is sentinel
        finally:
            _ROUTER_SINGLE_FROZEN_CATALOG.reset(token)

    asyncio.run(run())


def test_real_router_single_analyzer_provider_accepts_frozen_call_contract() -> None:
    config = _router_single_config()
    runner = TurnRunner(provider_selector=None, config=config)
    inherited = ProviderConfig(
        provider="openrouter",
        model="openai/gpt-5.5",
        api_key="synthetic",
    )

    provider = runner._router_dynamic_task_analyzer_provider(
        inherited,
        session_key="agent:main:analyzer-signature",
        ranking_config=ranking_config_snapshot(),
        analyzer_route={
            "provider": "openrouter",
            "model": "openai/gpt-5.5",
            "upstream_provider": "openai",
        },
        allow_canary_route=True,
    )

    assert provider is not None


async def test_run_turn_scopes_private_freeze_after_router_event_and_uses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.pipeline import TurnContext
    from opensquilla.engine.types import RouterDecisionEvent
    from opensquilla.provider import ModelCapabilities
    from opensquilla.tools.types import CallerKind, ToolContext

    selected = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-r1",
        api_key="private-api-key-never-project",
        base_url="https://selected-private-endpoint.invalid/v1",
        provider_routing={"deepseek/deepseek-r1": "deepseek"},
        replay_provider_state=False,
    )
    capabilities = ModelCapabilities(
        supports_reasoning=True,
        supports_tools=True,
        supports_streaming=True,
        reasoning_format="deepseek",
    )

    class RunSelector(_Selector):
        def clone(self) -> RunSelector:
            return RunSelector(self._cfg)

    class RunProvider:
        provider_name = "openrouter"

        def chat(
            self,
            messages: list[Any],
            tools: Any = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config

            async def stream() -> AsyncIterator[Any]:
                yield ProviderText(text="ok")
                yield ProviderDone(
                    stop_reason="stop",
                    input_tokens=4,
                    output_tokens=1,
                )

            return stream()

        async def list_models(self) -> list[Any]:
            return []

    class ExplodingLiveCatalog:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"live catalog accessed after freeze: {name}")

    raw = RunProvider()
    direct = _RouterSingleDirectProvider(
        raw,
        selected,
        health_ledger=None,
        absolute_deadline=None,
        frozen_catalog={
            "provider": selected.provider,
            "model": selected.model,
            "max_tokens": 6_144,
            "context_window": 131_072,
            "capabilities": capabilities,
        },
        enforces_routed_thinking_policy=False,
    )

    async def routed_pipeline(
        self: TurnRunner,
        message: str,
        session_key: str,
        provider: Any,
        cloned_selector: Any,
        tool_defs: list[Any],
        base_prompt: str | tuple[str, str],
        attachments: list[dict[str, Any]],
        **kwargs: Any,
    ) -> tuple[TurnContext, Any]:
        del provider, cloned_selector, kwargs
        return (
            TurnContext(
                message=message,
                session_key=session_key,
                config=self._config,
                provider=direct,
                model=selected.model,
                tool_defs=tool_defs,
                system_prompt=base_prompt,
                attachments=attachments,
                metadata={
                    "_router_single_provider_finalized": True,
                    "_router_single_frozen_catalog": {
                        "provider": selected.provider,
                        "model": selected.model,
                        "max_tokens": 6_144,
                        "context_window": 131_072,
                    },
                    "routed_tier": "c2",
                    "routed_model": selected.model,
                    "routing_source": "router_single",
                    "routing_confidence": 0.91,
                    "executed_provider": selected.provider,
                    "executed_model": selected.model,
                },
            ),
            direct,
        )

    monkeypatch.setattr(TurnRunner, "_run_pipeline", routed_pipeline)
    runner = TurnRunner(
        provider_selector=RunSelector(selected),
        config=GatewayConfig(
            squilla_router=SquillaRouterConfig(enabled=False),
            llm={"max_tokens": 0, "context_window_tokens": 0},
        ),
        model_catalog=ExplodingLiveCatalog(),
    )
    frozen_during_bootstrap: list[dict[str, Any] | None] = []
    resolved_capabilities: list[Any] = []
    original_bootstrap = runner._agent_bootstrap_stage.run

    async def observe_bootstrap(inp: Any) -> Any:
        frozen = _ROUTER_SINGLE_FROZEN_CATALOG.get()
        frozen_during_bootstrap.append(dict(frozen) if frozen is not None else None)
        outcome = await original_bootstrap(inp)
        resolved_capabilities.append(outcome.output.model_capabilities)
        return outcome

    monkeypatch.setattr(runner._agent_bootstrap_stage, "run", observe_bootstrap)
    stream = runner.run(
        "hello",
        "agent:main:single-freeze-scope",
        tool_context=ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
        history_has_persisted_user=False,
        no_memory_capture=True,
    )

    router_event = await anext(stream)
    assert isinstance(router_event, RouterDecisionEvent)
    assert router_event.context_window == 131_072
    # The private freeze is not installed across this externally visible yield.
    assert _ROUTER_SINGLE_FROZEN_CATALOG.get() is None

    # Same-task unrelated consumption cannot clear the pending single freeze,
    # because the authoritative payload still lives on the direct wrapper.
    unrelated_token = _ROUTER_SINGLE_FROZEN_CATALOG.set(
        {
            "provider": "other",
            "model": "other-model",
            "max_tokens": 1,
            "context_window": 1,
            "capabilities": None,
        }
    )
    try:
        unrelated = TurnRunner(
            provider_selector=None,
            config=GatewayConfig(llm={"max_tokens": 0, "context_window_tokens": 0}),
            model_catalog=ExplodingLiveCatalog(),
        )
        unrelated_result = _TurnRunnerModelCatalogAdapter(unrelated).lookup(
            "other-model",
            "other",
        )
        assert unrelated_result.max_tokens == 1
    finally:
        _ROUTER_SINGLE_FROZEN_CATALOG.reset(unrelated_token)

    remaining_events = [event async for event in stream]

    assert frozen_during_bootstrap == [
        {
            "provider": selected.provider,
            "model": selected.model,
            "max_tokens": 6_144,
            "context_window": 131_072,
            "capabilities": capabilities,
        }
    ]
    assert resolved_capabilities == [capabilities]
    assert _ROUTER_SINGLE_FROZEN_CATALOG.get() is None
    assert any(getattr(event, "kind", "") == "done" for event in remaining_events)
    projected = repr([router_event, *remaining_events]) + repr(
        frozen_during_bootstrap
    )
    assert selected.base_url not in projected
    assert selected.api_key not in projected
