from __future__ import annotations

import sys
import time as stdlib_time
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import pytest
import structlog.testing

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.routing.health import get_provider_health_ledger
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.types import DoneEvent
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.provider import ChatConfig, EnsembleProvider, Message
from opensquilla.provider.ranking_router import (
    TASK_ANALYZER_MODEL_ID,
    TASK_ANALYZER_PROVIDER_ID,
    DynamicRankingError,
    TaskAnalysisResult,
    TaskAnalyzerStreamCleanupError,
    fallback_task_profile,
    ranking_config_snapshot,
)
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.tree_baseline_router import TreeBaselineError


class _Provider:
    provider_name = "fake"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        raise AssertionError("credential-guard tests must not start provider chat")

    async def list_models(self) -> list[Any]:
        return []


class _FakeSelector:
    def __init__(self, *, provider: str, api_key: str) -> None:
        self._cfg = ProviderConfig(
            provider=provider,
            model="base-model",
            api_key=api_key,
            base_url="https://example.invalid/api",
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


def _static_b5_config(**ensemble_overrides: Any) -> GatewayConfig:
    return GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm_ensemble={"enabled": True, **ensemble_overrides},
    )


def _successful_chain_analysis(
    kwargs: dict[str, Any],
    *,
    profile_override: dict[str, Any] | None = None,
) -> TaskAnalysisResult:
    candidates = list(kwargs["candidates"])
    selected = candidates[0]
    profile = fallback_task_profile(
        routed_tier=str(kwargs["routed_tier"]),
        request_context=kwargs["request_context"],
    )
    configured_routes = [
        {
            "provider": candidate.provider_id,
            "model": candidate.model_id,
            "upstream_provider": candidate.upstream_provider,
        }
        for candidate in candidates
    ]
    physical_attempt = {
        "attempt": 1,
        "physical_attempt_id": "1" * 32,
        "provider": selected.provider_id,
        "model": selected.model_id,
        "requested_provider": selected.provider_id,
        "requested_model": selected.model_id,
        "input_tokens": 10,
        "output_tokens": 1,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.0,
        "provider_usage": {
            "provider": selected.provider_id,
            "model": selected.model_id,
            "physical_attempt_id": "1" * 32,
        },
    }
    return TaskAnalysisResult(
        profile=profile_override or profile,
        source="llm_provider",
        schema_valid=True,
        confidence=1.0,
        usage={
            "physical_attempts": [physical_attempt],
            "attempt_count": 1,
            "input_tokens": 10,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.0,
        },
        provider_id=selected.provider_id,
        model_id=selected.model_id,
        chain_trace={
            "protocol": "opensquilla.task-analyzer-fallback-chain/v1",
            "configured_routes": configured_routes,
            "attempt_outcomes": [
                {
                    "candidate_index": 0,
                    **configured_routes[0],
                    "outcome": "success",
                    "reason": "",
                    "physical_request_count": 1,
                }
            ],
            "selected_index": 0,
            "exhausted": False,
            "schema_repair_max_retries": kwargs.get(
                "schema_repair_max_retries",
                1,
            ),
            "deadline": {
                "configured_seconds": 1.0,
                "elapsed_seconds": 0.0,
                "remaining_seconds": 1.0,
                "expired": False,
            },
        },
    )


def _router_dynamic_plan(
    decision_id: str,
    *,
    proposer: str,
    aggregator: str,
) -> dict[str, Any]:
    return {
        "strategy": "router_dynamic",
        "selection_mode": "router_dynamic",
        "decision_id": decision_id,
        "selected_P": [proposer],
        "selected_A": aggregator,
        "session": {},
    }


def _turn_with_pending_route(
    session_key: str,
    pending: dict[str, Any],
) -> TurnContext:
    return TurnContext(
        message="test",
        session_key=session_key,
        config=None,
        provider=None,
        model="test",
        tool_defs=[],
        system_prompt="test",
        metadata={"router_dynamic_pending_route_plan": pending},
    )


def test_router_dynamic_route_memory_preserves_thinking_assignment() -> None:
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    assignment = {
        "proposers": {
            "openrouter:model-a": "high",
            "openrouter:model-b": "medium",
        },
        "aggregator": "highest",
        "thinking_policy_version": "thinking-policy-v1",
    }
    executed_assignment = {
        **assignment,
        "proposers": {
            **assignment["proposers"],
            "openrouter:model-b": "low",
        },
    }

    runner._remember_router_dynamic_route(
        "agent:main:thinking-route",
        {
            "selected_P": ["openrouter:model-a", "openrouter:model-b"],
            "selected_A": "openrouter:model-c",
            "session": {"escalation_level": 1},
            "thinking_assignment": assignment,
            "executed_thinking_assignment": executed_assignment,
        },
    )

    remembered = runner._previous_router_dynamic_route(
        "agent:main:thinking-route"
    )
    assert remembered is not None
    assert remembered["thinking_assignment"] == executed_assignment


@pytest.mark.parametrize(
    "retry_decision",
    [False, True],
    ids=["same-decision", "valid-retry-lineage"],
)
def test_router_dynamic_route_memory_prefers_valid_effective_selection_plan(
    retry_decision: bool,
) -> None:
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    session_key = "agent:main:effective-route"
    pending = _router_dynamic_plan(
        "initial-decision",
        proposer="openrouter:initial-proposer",
        aggregator="openrouter:initial-aggregator",
    )
    effective = _router_dynamic_plan(
        "retry-decision" if retry_decision else pending["decision_id"],
        proposer=(
            "openrouter:replacement-proposer"
            if retry_decision
            else "openrouter:initial-proposer"
        ),
        aggregator=(
            "openrouter:replacement-aggregator"
            if retry_decision
            else "openrouter:initial-aggregator"
        ),
    )
    if retry_decision:
        effective.update(
            {
                "retry_parent_decision_id": pending["decision_id"],
                "task_analysis_reused": True,
                "retry_routing": {
                    "parent_decision_id": pending["decision_id"],
                },
            }
        )
    else:
        effective["session"] = {"escalation_level": 2}
    turn = _turn_with_pending_route(session_key, pending)

    assert runner._commit_pending_router_dynamic_route(
        turn,
        DoneEvent(
            ensemble_trace={
                "fallback_used": False,
                "effective_selection_plan": effective,
            }
        ),
    )

    remembered = runner._previous_router_dynamic_route(session_key)
    assert remembered is not None
    assert remembered["selected_P"] == effective["selected_P"]
    assert remembered["selected_A"] == effective["selected_A"]
    if not retry_decision:
        assert remembered["escalation_level"] == 2


@pytest.mark.parametrize(
    "override",
    [
        {"strategy": "static_openrouter_b5"},
        {"selection_mode": "static_openrouter_b5"},
        {"decision_id": ""},
        {"selected_P": []},
        {"selected_P": ["replacement-without-provider"]},
        {"selected_P": ["openrouter:"]},
        {
            "selected_P": [
                "openrouter:replacement-proposer",
                "openrouter:replacement-proposer",
            ]
        },
        {"selected_A": " "},
        {"selected_A": "replacement-without-provider"},
        {"selected_A": ":replacement-aggregator"},
        {"retry_parent_decision_id": "unrelated-decision"},
        {"task_analysis_reused": False},
        {"retry_routing": None},
        {"retry_routing": {"parent_decision_id": "unrelated-decision"}},
        {
            "decision_id": "initial-decision",
            "selected_A": "openrouter:initial-aggregator",
        },
        {
            "decision_id": "initial-decision",
            "selected_P": ["openrouter:initial-proposer"],
        },
    ],
    ids=[
        "wrong-strategy",
        "wrong-selection-mode",
        "empty-decision-id",
        "empty-proposer-list",
        "malformed-proposer-identity",
        "empty-proposer-model",
        "duplicate-proposer-identity",
        "blank-aggregator",
        "malformed-aggregator-identity",
        "empty-aggregator-provider",
        "wrong-retry-parent",
        "task-analysis-not-reused",
        "missing-retry-routing",
        "wrong-retry-routing-parent",
        "same-decision-changed-proposer",
        "same-decision-changed-aggregator",
    ],
)
def test_router_dynamic_route_memory_rejects_invalid_effective_selection_plan(
    override: dict[str, Any],
) -> None:
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    session_key = "agent:main:invalid-effective-route"
    pending = _router_dynamic_plan(
        "initial-decision",
        proposer="openrouter:initial-proposer",
        aggregator="openrouter:initial-aggregator",
    )
    effective = {
        **_router_dynamic_plan(
            "retry-decision",
            proposer="openrouter:replacement-proposer",
            aggregator="openrouter:replacement-aggregator",
        ),
        "retry_parent_decision_id": pending["decision_id"],
        "task_analysis_reused": True,
        "retry_routing": {
            "parent_decision_id": pending["decision_id"],
        },
        **override,
    }
    turn = _turn_with_pending_route(session_key, pending)

    assert runner._commit_pending_router_dynamic_route(
        turn,
        DoneEvent(
            ensemble_trace={
                "fallback_used": False,
                "effective_selection_plan": effective,
            }
        ),
    )

    remembered = runner._previous_router_dynamic_route(session_key)
    assert remembered is not None
    assert remembered["selected_P"] == pending["selected_P"]
    assert remembered["selected_A"] == pending["selected_A"]


async def test_static_b5_wrap_skipped_without_openrouter_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    runner = TurnRunner(provider_selector=None, config=_static_b5_config())
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")
    single_provider = _Provider()

    with structlog.testing.capture_logs() as captured:
        turn, provider = await runner._run_pipeline(
            "hello",
            "agent:main:test",
            single_provider,
            selector,
            [],
            "system prompt",
            [],
        )

    # A keyless static profile can never run a member; the turn must keep the
    # plain single-model provider without ensemble labels or fallback budgets.
    assert not isinstance(provider, EnsembleProvider)
    assert turn.metadata["ensemble_wrap_skipped_reason"] == (
        "static_openrouter_b5_no_credential"
    )
    assert "ensemble_enabled" not in turn.metadata
    decision_id = turn.metadata["ensemble_decision_id"]
    routing_events = [
        row
        for row in captured
        if str(row["event"]).startswith("llm_ensemble.routing.")
    ]
    assert routing_events[0]["event"] == "llm_ensemble.routing.decision_started"
    assert routing_events[-1]["event"] == "llm_ensemble.routing.decision_skipped"
    assert not any(
        row["event"] == "llm_ensemble.routing.decision_completed"
        for row in routing_events
    )
    assert all(row["decision_id"] == decision_id for row in routing_events)
    assert [row["sequence"] for row in routing_events] == list(
        range(len(routing_events))
    )


async def test_static_b5_wraps_when_openrouter_env_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    runner = TurnRunner(provider_selector=None, config=_static_b5_config())
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

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
    assert "ensemble_wrap_skipped_reason" not in turn.metadata


async def test_static_b5_wraps_when_active_provider_is_keyed_openrouter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    runner = TurnRunner(provider_selector=None, config=_static_b5_config())
    selector = _FakeSelector(provider="openrouter", api_key="sk-or-synthetic")

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
    assert "ensemble_wrap_skipped_reason" not in turn.metadata


async def test_static_tokenrhythm_b5_wrap_skipped_without_tokenrhythm_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    # An OpenRouter key must not unlock the tokenrhythm profile.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="static_tokenrhythm_b5"),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:test",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert not isinstance(provider, EnsembleProvider)
    assert turn.metadata["ensemble_wrap_skipped_reason"] == (
        "static_tokenrhythm_b5_no_credential"
    )
    assert "ensemble_enabled" not in turn.metadata


async def test_static_tokenrhythm_b5_wraps_when_active_provider_is_keyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="static_tokenrhythm_b5"),
    )
    selector = _FakeSelector(provider="tokenrhythm", api_key="sk-tr-synthetic")

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
    assert "ensemble_wrap_skipped_reason" not in turn.metadata


async def test_router_dynamic_wrap_is_not_credential_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

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
    assert provider._provider_health_ledger is get_provider_health_ledger()
    assert turn.metadata["ensemble_enabled"] is True
    assert "ensemble_wrap_skipped_reason" not in turn.metadata
    assert turn.metadata["router_dynamic_task_analyzer"]["source"] == "router_fallback"
    assert turn.metadata["router_dynamic_task_analyzer"]["schema_valid"] is False
    assert turn.metadata["router_dynamic_task_analyzer"]["provider"] == (
        TASK_ANALYZER_PROVIDER_ID
    )
    assert turn.metadata["router_dynamic_task_analyzer"]["model"] == (
        "google/gemini-3.1-pro-preview"
    )
    assert turn.metadata["router_dynamic_task_analyzer"]["fallback_reason"] == (
        "provider_unavailable"
    )
    assert turn.metadata["router_dynamic_decision"]["ranking_version"] == "step2-ranking-v2"
    assert len(turn.metadata["router_dynamic_decision"]["registry_snapshot_hash"]) == 64
    assert turn.metadata["router_dynamic_decision"]["selected_P"]
    assert turn.metadata["router_dynamic_decision"]["selected_A"]
    assert not {
        "ranking_thinking_assignment_enabled",
        "thinking_policy_version",
        "thinking_assignment",
        "thinking_assignment_details",
        "assignment_reasons",
        "unsupported_level_fallbacks",
        "policy_versions",
    }.intersection(turn.metadata["router_dynamic_decision"])


async def test_router_dynamic_full_turn_prepares_ranking_config_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import ranking_router

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    config = _static_b5_config(selection_mode="router_dynamic")
    runner = TurnRunner(provider_selector=None, config=config)
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")
    original_validate = ranking_router._validate_ranking_config
    original_prepare = ranking_router._prepare_effective_ranking_config
    validation_count = 0
    prepared_ids: list[int] = []

    def counted_validate(*args: Any, **kwargs: Any) -> Any:
        nonlocal validation_count
        validation_count += 1
        return original_validate(*args, **kwargs)

    def capture_prepare(*args: Any, **kwargs: Any) -> Any:
        prepared = original_prepare(*args, **kwargs)
        prepared_ids.append(id(prepared))
        return prepared

    monkeypatch.setattr(ranking_router, "_validate_ranking_config", counted_validate)
    monkeypatch.setattr(
        ranking_router,
        "_prepare_effective_ranking_config",
        capture_prepare,
    )

    turn, provider = await runner._run_pipeline(
        "exercise the complete dynamic production path",
        "agent:main:ranking-config-prepare-once",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert turn.metadata["ensemble_enabled"] is True
    assert validation_count == 1
    assert len(prepared_ids) >= 3
    assert len(set(prepared_ids)) == 1


async def test_router_dynamic_wraps_with_pinned_skill_loader_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from opensquilla.skills.loader import PinnedSkillLoader, SkillLoader

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    live_skill_loader = SkillLoader(
        bundled_dir=tmp_path,
        snapshot_path=tmp_path / "skills-snapshot.json",
    )
    skill_catalog = live_skill_loader.snapshot()
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
        skill_loader=live_skill_loader,
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:pinned-skill-loader",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
        skill_catalog=skill_catalog,
    )

    pinned_skill_loader = turn.metadata["skill_loader"]
    assert isinstance(pinned_skill_loader, PinnedSkillLoader)
    with pytest.raises(TypeError, match="cannot pickle.*RLock"):
        deepcopy(pinned_skill_loader)
    assert isinstance(provider, EnsembleProvider)
    assert provider.selection_plan["selection_mode"] == "router_dynamic"
    assert provider.selection_plan["selected_P"]
    assert provider.selection_plan["selected_A"]
    assert turn.metadata["ensemble_enabled"] is True
    assert "ensemble_wrap_skipped_reason" not in turn.metadata
    retry_context = provider._router_dynamic_retry_context
    assert retry_context is not None
    retry_factory = retry_context.retry_factory
    assert "skill_loader" not in retry_factory.turn_metadata


@pytest.mark.parametrize("single_route", [False, True], ids=["fallbacks", "single"])
async def test_router_dynamic_uses_ordered_task_analyzer_chain(
    monkeypatch: pytest.MonkeyPatch,
    single_route: bool,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    fixed_analyzer_provider = _Provider()
    provider_builds: list[ProviderConfig] = []
    analyzer_calls: list[dict[str, Any]] = []

    def fake_resolve(selector: Any) -> _Provider:
        provider_builds.append(selector.current_config)
        return fixed_analyzer_provider

    async def fake_analyze_task_with_fallback_chain(**kwargs: Any) -> TaskAnalysisResult:
        analyzer_calls.append(kwargs)
        return _successful_chain_analysis(kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.selector.ModelSelector.resolve",
        fake_resolve,
    )
    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        fake_analyze_task_with_fallback_chain,
    )
    ensemble_overrides: dict[str, Any] = {
        "selection_mode": "router_dynamic",
        "ranking_user_profile_generation_enabled": False,
        "ranking_user_profile_enabled": True,
    }
    if single_route:
        ensemble_overrides["ranking_config_override"] = {
            "task_analyzer": {"fallback_chain": []}
        }
    config = _static_b5_config(
        **ensemble_overrides,
    )
    config.llm.provider_routing = {TASK_ANALYZER_MODEL_ID: "anthropic"}
    runner = TurnRunner(provider_selector=None, config=config)
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "analyze with the dedicated model",
        "agent:main:fixed-task-analyzer",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    expected_models = [
        TASK_ANALYZER_MODEL_ID,
        "openai/gpt-5.6-sol",
        "google/gemini-3.1-pro-preview",
    ]
    expected_upstreams = ["anthropic", "azure", "google-ai-studio"]
    if single_route:
        expected_models = expected_models[:1]
        expected_upstreams = expected_upstreams[:1]
    assert [analyzer_config.model for analyzer_config in provider_builds] == expected_models
    assert [
        analyzer_config.provider_routing[analyzer_config.model]
        for analyzer_config in provider_builds
    ] == expected_upstreams
    assert all(
        analyzer_config.provider == TASK_ANALYZER_PROVIDER_ID
        and analyzer_config.api_key == "sk-or-synthetic"
        and analyzer_config.base_url == "https://openrouter.ai/api/v1"
        and analyzer_config.replay_provider_state is False
        for analyzer_config in provider_builds
    )
    assert len(analyzer_calls) == 1
    candidates = analyzer_calls[0]["candidates"]
    assert [candidate.provider for candidate in candidates] == [
        fixed_analyzer_provider
    ] * len(expected_models)
    assert [candidate.model_id for candidate in candidates] == [
        analyzer_config.model for analyzer_config in provider_builds
    ]
    assert analyzer_calls[0]["user_profile_enabled"] is True
    assert "user_profile" not in analyzer_calls[0]
    assert turn.metadata["routed_model_before_ensemble"] != TASK_ANALYZER_MODEL_ID
    assert turn.metadata["router_dynamic_task_analyzer"]["provider"] == (
        TASK_ANALYZER_PROVIDER_ID
    )
    assert turn.metadata["router_dynamic_task_analyzer"]["model"] == (
        TASK_ANALYZER_MODEL_ID
    )


@pytest.mark.parametrize(
    ("configured_chain", "expected_admission_seconds"),
    [(True, 60.0), (False, 20.0)],
    ids=["configured-chain", "legacy-single"],
)
async def test_router_dynamic_analyzer_admission_deadline_matches_execution_budget(
    monkeypatch: pytest.MonkeyPatch,
    configured_chain: bool,
    expected_admission_seconds: float,
) -> None:
    from opensquilla.eval.draco_task_analyzer_execution import (
        task_analyzer_execution_contract_from_ranking_config,
    )
    from opensquilla.provider.ranking_router import (
        TaskAnalysisResult,
        task_analyzer_chain_policy,
        task_analyzer_policy,
    )

    fixed_now = 1_000_000_000.0

    class FixedRuntimeTime:
        @staticmethod
        def monotonic() -> float:
            return fixed_now

        def __getattr__(self, name: str) -> Any:
            return getattr(stdlib_time, name)

    config = _static_b5_config(selection_mode="router_dynamic")
    resolution = config.llm_ensemble.ranking_config_resolution_snapshot()
    ranking_config = deepcopy(resolution["effective_config"])
    if not configured_chain:
        analyzer_config = ranking_config["task_analyzer"]
        for field in (
            "fallback_chain",
            "schema_repair_max_retries",
            "total_timeout_seconds",
            "payload_max_chars",
            "payload_max_bytes",
            "payload_max_estimated_tokens",
        ):
            analyzer_config.pop(field)
    resolution["effective_config"] = ranking_config
    monkeypatch.setattr(
        type(config.llm_ensemble),
        "ranking_config_resolution_snapshot",
        lambda _self: deepcopy(resolution),
    )
    monkeypatch.setattr(
        "opensquilla.engine.runtime.time",
        FixedRuntimeTime(),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")

    captured: dict[str, Any] = {}

    async def fake_chain(**kwargs: Any) -> TaskAnalysisResult:
        captured.update(kwargs)
        captured["executor"] = "chain"
        return _successful_chain_analysis(kwargs)

    async def fake_legacy(**kwargs: Any) -> TaskAnalysisResult:
        captured.update(kwargs)
        captured["executor"] = "legacy"
        provider_id = str(kwargs["analyzer_provider_id"])
        model_id = str(kwargs["analyzer_model_id"])
        attempt_id = "2" * 32
        physical_attempt = {
            "attempt": 1,
            "physical_attempt_id": attempt_id,
            "provider": provider_id,
            "model": model_id,
            "requested_provider": provider_id,
            "requested_model": model_id,
            "input_tokens": 10,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.0,
            "provider_usage": {
                "provider": provider_id,
                "model": model_id,
                "physical_attempt_id": attempt_id,
            },
        }
        return TaskAnalysisResult(
            profile=fallback_task_profile(
                routed_tier=str(kwargs["routed_tier"]),
                request_context=kwargs["request_context"],
            ),
            source="llm_provider",
            schema_valid=True,
            confidence=1.0,
            usage={
                "physical_attempts": [physical_attempt],
                "attempt_count": 1,
                "input_tokens": 10,
                "output_tokens": 1,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
            },
            provider_id=provider_id,
            model_id=model_id,
        )

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        fake_chain,
    )
    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_provider",
        fake_legacy,
    )
    runner = TurnRunner(provider_selector=None, config=config)
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")
    turn_deadline = fixed_now + 1_000.0

    turn, provider = await runner._run_pipeline(
        "capture analyzer deadline",
        f"agent:main:analyzer-deadline-{configured_chain}",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
        turn_absolute_deadline=turn_deadline,
    )

    assert isinstance(provider, EnsembleProvider), turn.metadata.get(
        "router_dynamic_ranking_error"
    )
    assert captured["executor"] == ("chain" if configured_chain else "legacy")
    assert captured["admission_deadline"] == (
        fixed_now + expected_admission_seconds
    )
    absolute_key = "absolute_deadline" if configured_chain else "_absolute_deadline"
    assert captured[absolute_key] == turn_deadline
    execution_contract = task_analyzer_execution_contract_from_ranking_config(
        ranking_config
    )
    assert execution_contract["total_timeout_seconds"] == (
        expected_admission_seconds
    )
    if configured_chain:
        assert task_analyzer_chain_policy(ranking_config)[
            "total_timeout_seconds"
        ] == expected_admission_seconds
    else:
        assert task_analyzer_policy(ranking_config)["timeout_seconds"] == (
            expected_admission_seconds
        )


async def test_router_dynamic_profile_application_defaults_off_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    analyzer_calls: list[dict[str, Any]] = []

    async def fake_analyze_task_with_fallback_chain(**kwargs: Any) -> TaskAnalysisResult:
        analyzer_calls.append(kwargs)
        return _successful_chain_analysis(kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        fake_analyze_task_with_fallback_chain,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(
            selection_mode="router_dynamic",
            ranking_user_profile_generation_enabled=True,
        ),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    with structlog.testing.capture_logs() as captured:
        turn, provider = await runner._run_pipeline(
            "route without a user profile",
            "agent:main:no-ranking-profile",
            _Provider(),
            selector,
            [],
            "system prompt",
            [],
        )

    assert isinstance(provider, EnsembleProvider)
    assert len(analyzer_calls) == 1
    assert analyzer_calls[0]["user_profile_enabled"] is False
    assert "user_profile" not in analyzer_calls[0]
    decision_id = turn.metadata["ensemble_decision_id"]
    assert analyzer_calls[0]["decision_id"] == decision_id
    assert turn.metadata["router_dynamic_user_profile"] == {
        "enabled": False,
        "source": "",
        "version": "",
    }
    assert provider.selection_plan["decision_id"] == decision_id
    assert provider.selection_plan["user_profile_enabled"] is False
    assert turn.metadata["router_dynamic_decision"]["decision_id"] == decision_id

    routing_events = [
        row
        for row in captured
        if str(row["event"]).startswith("llm_ensemble.routing.")
    ]
    assert routing_events[0]["event"] == "llm_ensemble.routing.decision_started"
    assert routing_events[-1]["event"] == "llm_ensemble.routing.decision_completed"
    assert all(row["decision_id"] == decision_id for row in routing_events)
    assert [row["sequence"] for row in routing_events] == list(
        range(len(routing_events))
    )
    assert {
        "llm_ensemble.routing.task_analysis_recorded",
        "llm_ensemble.routing.hard_filter_recorded",
        "llm_ensemble.routing.model_score_recorded",
        "llm_ensemble.routing.proposer_step_recorded",
        "llm_ensemble.routing.aggregator_score_recorded",
    }.issubset({row["event"] for row in routing_events})


async def test_router_tree_baseline_never_calls_the_remote_task_analyzer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_analyzer_call(**kwargs: Any) -> TaskAnalysisResult:
        raise AssertionError(f"tree baseline invoked analyzer with {kwargs!r}")

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_provider",
        unexpected_analyzer_call,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_tree_baseline"),
    )
    selector = _FakeSelector(provider="openrouter", api_key="sk-or-synthetic")

    turn, provider = await runner._run_pipeline(
        "use the local tree baseline",
        "agent:main:tree-baseline",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert provider.profile_name == "router_tree_baseline/c1"
    assert provider.selection_plan["router_source"] == "compatibility_default"
    assert "router_dynamic_task_analyzer" not in turn.metadata
    assert turn.metadata["router_tree_baseline_decision"]["routed_tier"] == "c1"
    assert turn.metadata["router_tree_baseline_decision"]["selected_P"]
    assert turn.metadata["router_tree_baseline_decision"]["selected_A"]


async def test_router_tree_baseline_runs_without_the_ranking_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "opensquilla.provider.ranking_router",
        None,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_tree_baseline"),
    )
    selector = _FakeSelector(provider="openrouter", api_key="sk-or-synthetic")

    turn, provider = await runner._run_pipeline(
        "run the isolated local tree baseline",
        "agent:main:tree-baseline-no-ranking",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, EnsembleProvider)
    assert provider.profile_name == "router_tree_baseline/c1"
    assert turn.metadata["router_tree_baseline_decision"]["selected_P"]


async def test_router_tree_baseline_selection_failure_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_selection(**kwargs: Any) -> None:
        raise TreeBaselineError(f"invalid frozen config: {sorted(kwargs)}")

    monkeypatch.setattr(
        "opensquilla.provider.tree_baseline_router.select_tree_baseline",
        fail_selection,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_tree_baseline"),
    )
    selector = _FakeSelector(provider="openrouter", api_key="sk-or-synthetic")

    turn, provider = await runner._run_pipeline(
        "use the local tree baseline",
        "agent:main:tree-baseline-fail-open",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, _Provider)
    assert not isinstance(provider, EnsembleProvider)
    assert "ensemble_enabled" not in turn.metadata
    assert turn.metadata["ensemble_wrap_skipped_reason"] == (
        "router_tree_baseline_unavailable"
    )
    assert "invalid frozen config" in turn.metadata["router_tree_baseline_error"]


def test_router_dynamic_task_analyzer_resolution_failure_uses_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_resolution(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("credential pool unavailable")

    monkeypatch.setattr(
        "opensquilla.engine.selector_override.resolve_tier_provider_config",
        fail_resolution,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(
            llm={"provider": "groq", "model": "base-model", "api_key": "fake"},
            squilla_router=SquillaRouterConfig(enabled=False),
            llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
        ),
    )

    provider = runner._router_dynamic_task_analyzer_provider(
        ProviderConfig(provider="groq", model="base-model", api_key="fake"),
        session_key="agent:main:analyzer-resolution-failure",
        ranking_config=ranking_config_snapshot(),
    )

    assert provider is None


def test_live_router_dynamic_analyzer_canary_is_blocked_before_provider_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.provider import ranking_router

    real_loader = ranking_router.load_model_registry_snapshot

    def canary_registry(*, base_version: str | None = None) -> dict[str, Any]:
        snapshot = real_loader(base_version=base_version)
        for row in snapshot["models"]:
            facts = row["registry_facts"]
            if (
                facts["provider"] == TASK_ANALYZER_PROVIDER_ID
                and facts["model_id"] == TASK_ANALYZER_MODEL_ID
            ):
                facts["status"] = "canary"
                break
        else:
            raise AssertionError("task analyzer route is absent from registry")
        return snapshot

    sentinel = object()
    provider_builds: list[ProviderConfig] = []

    def capture_resolution(selector: Any) -> object:
        provider_builds.append(selector.current_config)
        return sentinel

    monkeypatch.setattr(
        ranking_router,
        "load_model_registry_snapshot",
        canary_registry,
    )
    monkeypatch.setattr(
        "opensquilla.provider.selector.ModelSelector.resolve",
        capture_resolution,
    )
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "openrouter-key",
        },
        squilla_router=SquillaRouterConfig(enabled=False),
        llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
    )
    runner = TurnRunner(provider_selector=None, config=config)
    inherited = ProviderConfig(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_key="openrouter-key",
    )

    live_provider = runner._router_dynamic_task_analyzer_provider(
        inherited,
        session_key="agent:main:live-canary-analyzer",
        ranking_config=ranking_config_snapshot(),
    )
    experiment_provider = runner._router_dynamic_task_analyzer_provider(
        inherited,
        session_key="agent:main:experiment-canary-analyzer",
        ranking_config=ranking_config_snapshot(),
        allow_canary_route=True,
    )

    assert live_provider is None
    assert experiment_provider is sentinel
    assert len(provider_builds) == 1


def test_router_dynamic_task_analyzer_rejects_misbound_resolver_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.engine.selector_override.resolve_tier_provider_config",
        lambda *_args, **_kwargs: ProviderConfig(
            provider="anthropic",
            model=TASK_ANALYZER_MODEL_ID,
            api_key="anthropic-key",
        ),
    )
    runner = TurnRunner(
        provider_selector=None,
        config=GatewayConfig(
            llm={"provider": "groq", "model": "base-model", "api_key": "groq-key"},
            squilla_router=SquillaRouterConfig(enabled=False),
            llm_ensemble={"enabled": True, "selection_mode": "router_dynamic"},
        ),
    )

    provider = runner._router_dynamic_task_analyzer_provider(
        ProviderConfig(provider="groq", model="base-model", api_key="groq-key"),
        session_key="agent:main:misbound-analyzer-credential",
        ranking_config=ranking_config_snapshot(),
    )

    assert provider is None


def test_router_dynamic_task_analyzer_uses_frozen_override_and_upstream_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[ProviderConfig] = []
    sentinel = object()

    def capture_resolution(selector: Any) -> object:
        captured.append(selector.current_config)
        return sentinel

    monkeypatch.setattr(
        "opensquilla.provider.selector.ModelSelector.resolve",
        capture_resolution,
    )
    config = GatewayConfig(
        llm={
            "provider": "openrouter",
            "model": "deepseek/deepseek-v4-pro",
            "api_key": "openrouter-key",
            "base_url": "https://openrouter.ai/api/v1",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "router_dynamic",
            "ranking_config_override": {
                "task_analyzer": {
                    "model": "openai/gpt-5.5",
                    "upstream_provider": "openai",
                }
            },
        },
    )
    ranking_config = config.llm_ensemble.ranking_config_effective_snapshot()
    runner = TurnRunner(provider_selector=None, config=config)

    provider = runner._router_dynamic_task_analyzer_provider(
        ProviderConfig(
            provider="openrouter",
            model="deepseek/deepseek-v4-pro",
            api_key="openrouter-key",
            base_url="https://openrouter.ai/api/v1",
        ),
        session_key="agent:main:analyzer-override",
        ranking_config=ranking_config,
    )

    assert provider is sentinel
    assert len(captured) == 1
    assert captured[0].provider == "openrouter"
    assert captured[0].model == "openai/gpt-5.5"
    assert captured[0].api_key == "openrouter-key"
    assert captured[0].provider_routing["openai/gpt-5.5"] == "openai"
    assert captured[0].replay_provider_state is False


async def test_router_dynamic_carries_the_previous_route_into_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_last_routes: list[dict[str, Any]] = []

    async def fake_analyze_task_with_fallback_chain(**kwargs: Any) -> TaskAnalysisResult:
        request_context = kwargs["request_context"]
        last_route = dict(request_context.get("last_route") or {})
        seen_last_routes.append(last_route)
        profile = fallback_task_profile(
            routed_tier=str(kwargs["routed_tier"]),
            request_context=request_context,
        )
        if last_route:
            profile["session_intent"] = {"type": "continue", "confidence": 1.0}
        return _successful_chain_analysis(
            kwargs,
            profile_override=profile,
        )

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        fake_analyze_task_with_fallback_chain,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")
    session_key = "agent:main:router-dynamic-continuity"

    first_turn, _ = await runner._run_pipeline(
        "first",
        session_key,
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )
    assert runner._previous_router_dynamic_route(session_key) is None
    assert first_turn.metadata["router_dynamic_pending_route_plan"]["selected_P"]
    assert runner._commit_pending_router_dynamic_route(
        first_turn,
        DoneEvent(ensemble_trace={"fallback_used": False}),
    )
    second_turn, _ = await runner._run_pipeline(
        "continue",
        session_key,
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert seen_last_routes[0] == {}
    assert seen_last_routes[1]["selected_P"] == first_turn.metadata[
        "router_dynamic_decision"
    ]["selected_P"]
    assert seen_last_routes[1]["selected_A"] == first_turn.metadata[
        "router_dynamic_decision"
    ]["selected_A"]
    assert second_turn.metadata["router_dynamic_last_route"]["strategy_mode"] == (
        "B5_fuse"
    )
    assert second_turn.metadata["router_dynamic_decision"]["session"][
        "sticky_applied"
    ] is True


async def test_router_dynamic_failed_turn_does_not_replace_previous_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def analyzed_task(**kwargs: Any) -> TaskAnalysisResult:
        return _successful_chain_analysis(kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        analyzed_task,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")
    session_key = "agent:main:router-dynamic-failed-route"
    previous = {
        "selected_P": ["old-provider:old-proposer"],
        "selected_A": "old-provider:old-aggregator",
        "session": {},
    }
    runner._remember_router_dynamic_route(session_key, previous)

    turn, _ = await runner._run_pipeline(
        "new route that later fails",
        session_key,
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )
    assert turn.metadata["router_dynamic_pending_route_plan"]["selected_P"]
    assert not runner._commit_pending_router_dynamic_route(turn, None)

    remembered = runner._previous_router_dynamic_route(session_key)
    assert remembered is not None
    assert remembered["selected_P"] == previous["selected_P"]
    assert remembered["selected_A"] == previous["selected_A"]


async def test_router_dynamic_fallback_turn_does_not_replace_previous_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def analyzed_task(**kwargs: Any) -> TaskAnalysisResult:
        return _successful_chain_analysis(kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        analyzed_task,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")
    session_key = "agent:main:router-dynamic-fallback-route"
    previous = {
        "selected_P": ["old-provider:old-proposer"],
        "selected_A": "old-provider:old-aggregator",
        "session": {},
    }
    runner._remember_router_dynamic_route(session_key, previous)

    turn, _ = await runner._run_pipeline(
        "new route that falls back",
        session_key,
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )
    assert not runner._commit_pending_router_dynamic_route(
        turn,
        DoneEvent(ensemble_trace={"fallback_used": True}),
    )

    remembered = runner._previous_router_dynamic_route(session_key)
    assert remembered is not None
    assert remembered["selected_P"] == previous["selected_P"]
    assert remembered["selected_A"] == previous["selected_A"]


@pytest.mark.parametrize("failure_type", [DynamicRankingError, RuntimeError])
async def test_router_dynamic_selection_failure_fails_open_to_the_single_provider(
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    async def analyzed_task(**kwargs: Any) -> TaskAnalysisResult:
        return _successful_chain_analysis(kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        analyzed_task,
    )

    def fail_ranking(**_kwargs: Any) -> None:
        raise failure_type("router_dynamic has no proposer after hard filtering")

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.rank_models",
        fail_ranking,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "an extra-long request",
        "agent:main:router-dynamic-fail-open",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, _Provider)
    assert not isinstance(provider, EnsembleProvider)
    assert "ensemble_enabled" not in turn.metadata
    assert turn.metadata["ensemble_wrap_skipped_reason"] == (
        "router_dynamic_ranking_unavailable"
    )
    assert "no proposer" in turn.metadata["router_dynamic_ranking_error"]


async def test_router_dynamic_thinking_unavailable_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def analyzed_task(**kwargs: Any) -> TaskAnalysisResult:
        return _successful_chain_analysis(kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        analyzed_task,
    )

    def fail_thinking_assignment(**_kwargs: Any) -> None:
        raise DynamicRankingError(
            "router_dynamic has no proposer: thinking_level_unavailable"
        )

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.rank_models",
        fail_thinking_assignment,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(
            selection_mode="router_dynamic",
            ranking_thinking_assignment_enabled=True,
        ),
    )

    with pytest.raises(
        DynamicRankingError,
        match="thinking_level_unavailable",
    ):
        await runner._run_pipeline(
            "high-risk request",
            "agent:main:router-dynamic-thinking-fail-closed",
            _Provider(),
            _FakeSelector(provider="groq", api_key="sk-groq-synthetic"),
            [],
            "system prompt",
            [],
        )


async def test_router_dynamic_analyzer_cleanup_failure_aborts_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_analysis(**_kwargs: Any) -> TaskAnalysisResult:
        raise TaskAnalyzerStreamCleanupError("cleanup not proven")

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_fallback_chain",
        fail_analysis,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    with pytest.raises(TaskAnalyzerStreamCleanupError, match="cleanup not proven"):
        await runner._run_pipeline(
            "hello",
            "agent:main:router-dynamic-cleanup-fail-closed",
            _Provider(),
            selector,
            [],
            "system prompt",
            [],
        )


async def test_router_dynamic_unbounded_candidates_fail_open_before_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def analysis_must_not_start(**_kwargs: Any) -> TaskAnalysisResult:
        raise AssertionError("unbounded candidate config must fail before analysis")

    monkeypatch.setattr(
        "opensquilla.provider.ranking_router.analyze_task_with_provider",
        analysis_must_not_start,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(
            selection_mode="router_dynamic",
            candidate_max_chars=0,
        ),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:router-dynamic-unbounded-candidate",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, _Provider)
    assert not isinstance(provider, EnsembleProvider)
    assert "ensemble_enabled" not in turn.metadata
    assert turn.metadata["ensemble_wrap_skipped_reason"] == (
        "router_dynamic_ranking_unavailable"
    )
    assert "candidate_max_chars > 0" in turn.metadata["router_dynamic_ranking_error"]


async def test_router_dynamic_config_load_failure_fails_open_before_analysis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_config_load(_self: Any) -> dict[str, Any]:
        raise DynamicRankingError("ranking config is malformed")

    monkeypatch.setattr(
        (
            "opensquilla.gateway.config.LlmEnsembleConfig."
            "ranking_config_resolution_snapshot"
        ),
        fail_config_load,
    )
    runner = TurnRunner(
        provider_selector=None,
        config=_static_b5_config(selection_mode="router_dynamic"),
    )
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

    turn, provider = await runner._run_pipeline(
        "hello",
        "agent:main:router-dynamic-bad-config",
        _Provider(),
        selector,
        [],
        "system prompt",
        [],
    )

    assert isinstance(provider, _Provider)
    assert not isinstance(provider, EnsembleProvider)
    assert "ensemble_enabled" not in turn.metadata
    assert turn.metadata["ensemble_wrap_skipped_reason"] == (
        "router_dynamic_ranking_unavailable"
    )
    assert turn.metadata["router_dynamic_ranking_error"] == (
        "ranking config is malformed"
    )
    assert "router_dynamic_task_analyzer" not in turn.metadata


def _custom_b5_guard_config(candidates: list[dict[str, Any]]) -> GatewayConfig:
    return GatewayConfig(
        squilla_router=SquillaRouterConfig(enabled=False),
        llm={
            "provider": "groq",
            "model": "base-model",
            "api_key": "sk-groq-synthetic",
            "base_url": "https://example.invalid/api",
        },
        llm_ensemble={
            "enabled": True,
            "selection_mode": "custom_b5",
            "candidates": candidates,
        },
    )


async def test_custom_b5_tracks_missing_member_and_preserves_quorum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = _custom_b5_guard_config(
        [
            {"provider": "groq", "model": "candidate-a"},
            {"provider": "openrouter", "model": "z-ai/glm-5.2"},
        ]
    )
    runner = TurnRunner(provider_selector=None, config=cfg)
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

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
    by_provider = {
        member.provider_config.provider: member for member in provider.proposers
    }
    assert by_provider["groq"].ready is True
    assert by_provider["openrouter"].ready is False
    assert by_provider["openrouter"].unavailable_reason == "missing_credential"
    assert turn.metadata["ensemble_enabled"] is True
    assert "ensemble_wrap_skipped_reason" not in turn.metadata


async def test_custom_b5_wraps_when_every_member_resolves_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-synthetic")
    cfg = _custom_b5_guard_config(
        [
            {"provider": "groq", "model": "candidate-a", "role": "primary"},
            {"provider": "openrouter", "model": "z-ai/glm-5.2", "role": "contrast"},
            {"provider": "groq", "model": "fuser", "role": "aggregator"},
        ]
    )
    runner = TurnRunner(provider_selector=None, config=cfg)
    selector = _FakeSelector(provider="groq", api_key="sk-groq-synthetic")

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
    assert provider.profile_name == "custom_b5"
    assert [member.label for member in provider.proposers] == ["primary", "contrast"]
    assert provider.aggregator.provider_config.model == "fuser"
    assert turn.metadata["ensemble_enabled"] is True
    assert "ensemble_wrap_skipped_reason" not in turn.metadata
