from __future__ import annotations

import ast
import asyncio
import gc
import hashlib
import inspect
import json
import threading
import time
import weakref
from collections.abc import AsyncIterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import copy, deepcopy
from datetime import date
from importlib import resources
from typing import Any

import pytest
import structlog.testing

import opensquilla.provider.ranking_router as ranking_router
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageExecutionContext,
    bind_usage_accounting_scope,
)
from opensquilla.provider.admission import (
    ProviderAdmissionController,
    ProviderAdmissionSettings,
)
from opensquilla.provider.cache_affinity import (
    CacheAffinityEvidenceInput,
    CachePriceQuote,
)
from opensquilla.provider.ranking_router import (
    CAPABILITIES,
    DOMAINS,
    TASK_ANALYZER_FALLBACK_CHAIN_PROTOCOL,
    TASK_ANALYZER_MODEL_ID,
    TASK_ANALYZER_PROVIDER_ID,
    TASK_ANALYZER_VERSION,
    THINKING_LEVELS,
    DynamicRankingError,
    TaskAnalysisResult,
    TaskAnalyzerPhysicalEvidenceError,
    TaskAnalyzerStreamCleanupError,
    analyze_task_with_fallback_chain,
    analyze_task_with_provider,
    build_model_registry_snapshot,
    build_request_context,
    build_single_model_request_context,
    canonical_json_sha256,
    dynamic_output_token_budgets,
    fallback_task_profile,
    load_model_registry_snapshot,
    load_ranking_config,
    mock_user_profile,
    normalize_task_profile,
    rank_models,
    rank_single_model,
    ranking_config_resolution,
    ranking_config_snapshot,
    ranking_trace_replay_reasons,
    single_ranking_trace_replay_reasons,
    task_analyzer_chain_policy,
    task_analyzer_policy,
)
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
)

_MISTRAL_MODEL_IDS = frozenset(
    {
        "mistralai/mistral-large-2512",
        "mistralai/mistral-medium-3-5",
        "mistralai/mistral-small-2603",
        "mistralai/ministral-14b-2512",
        "mistralai/voxtral-small-24b-2507",
    }
)


def _task_profile(
    *,
    tier: int = 3,
    risk: str = "medium",
    cost: str = "medium",
    latency: str = "normal",
    context: str = "short",
    modalities: list[str] | None = None,
    intent: str = "new_task",
    intent_confidence: float = 1.0,
) -> dict[str, Any]:
    return {
        "capability_dist": {"reasoning": 0.6, "code_generation": 0.4},
        "domain_dist": {"software_engineering": 1.0},
        "tier_dist": {str(tier): 1.0},
        "constraints": {
            "cost": cost,
            "latency": latency,
            "context": context,
            "modality": modalities or ["text"],
            "risk": risk,
        },
        "optional_constraints": {"format": "patch"},
        "session_intent": {"type": intent, "confidence": intent_confidence},
    }


def _analysis(**kwargs: Any) -> TaskAnalysisResult:
    return TaskAnalysisResult(
        profile=_task_profile(**kwargs),
        source="test",
        schema_valid=True,
        confidence=1.0,
    )


def _context(
    *,
    input_tokens: int = 1_000,
    candidate_tokens: int = 1_000,
    aggregator_tokens: int = 1_000,
    last_route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "routing_budget": {
            "estimated_input_tokens": input_tokens,
            "tool_log_tokens": 0,
            "candidate_output_tokens": candidate_tokens,
            "aggregator_output_tokens": aggregator_tokens,
        },
        "input_modalities": ["text"],
        "last_route": last_route or {},
        "snapshot_hash": "request-context-test",
    }


def _model(
    model_id: str,
    *,
    provider: str = "test-provider",
    vendor: str | None = None,
    family: str | None = None,
    roles: list[str] | None = None,
    status: str = "enabled",
    health: str = "healthy",
    credential_available: bool = True,
    context_window: int = 128_000,
    modalities: list[str] | None = None,
    is_open_source: bool = False,
    is_chinese_model: bool = False,
    capability: float = 0.8,
    aggregator_fit: float = 0.8,
    price: float = 1.0,
    price_source: str | None = None,
    latency_ms: int = 2_000,
) -> dict[str, Any]:
    return {
        "source": "test_registry",
        "runtime": {"thinking": "off"},
        "registry_facts": {
            "model_id": model_id,
            "version": "test-v1",
            "provider": provider,
            "vendor": vendor or provider,
            "family": family or model_id,
            "is_open_source": is_open_source,
            "is_chinese_model": is_chinese_model,
            "status": status,
            "roles": roles or ["proposer", "aggregator"],
            "context_window": context_window,
            "effective_context_bucket": "extra_long",
            "modalities": modalities or ["text"],
            "tools": [],
            "price": {
                "input_per_million": price,
                "output_per_million": price,
                **({"price_source": price_source} if price_source is not None else {}),
            },
            "latency_p50_ms": latency_ms // 2,
            "latency_p95_ms": latency_ms,
            "quota": "available",
            "rate_limit": "available",
            "health": health,
            "credential_available": credential_available,
        },
        "static_profile": {
            "capability_dist_prior": {
                "reasoning": capability,
                "code_generation": capability,
                "format_following": capability,
            },
            "domain_dist_prior": {"software_engineering": capability},
            "tier_dist_prior": {
                "1": capability,
                "2": capability,
                "3": capability,
                "4": capability,
            },
            "role_fit_prior": {
                "proposer": capability,
                "aggregator": aggregator_fit,
            },
        },
        "online_profile": {
            "error_rates": {
                "hallucination": max(0.0, 1.0 - capability),
                "omission": max(0.0, 0.9 - capability),
            }
        },
    }


def _with_role_reliability(
    model: dict[str, Any],
    *,
    proposer: tuple[int, int] = (0, 0),
    aggregator: tuple[int, int] = (0, 0),
    window_size: int = 50,
    source: str = "aef_experiment_artifacts",
) -> dict[str, Any]:
    model["online_profile"]["role_reliability"] = {
        "window_size": window_size,
        "proposer": {"success": proposer[0], "failure": proposer[1]},
        "aggregator": {"success": aggregator[0], "failure": aggregator[1]},
        "source": source,
    }
    return model


def _thinking_model(
    model_id: str,
    *,
    thinking_levels: list[str] | None = None,
    thinking_level_mapping: dict[str, str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    model = _model(model_id, **kwargs)
    levels = (
        thinking_levels if thinking_levels is not None else ["low", "medium", "high", "highest"]
    )
    mapping = (
        thinking_level_mapping
        if thinking_level_mapping is not None
        else {
            "low": "low",
            "medium": "medium",
            "high": "high",
            "highest": "xhigh",
        }
    )
    facts = model["registry_facts"]
    facts.update(
        {
            "supports_reasoning": True,
            "supported_thinking_levels": sorted(set(mapping.values())),
            "thinking_levels": list(levels),
            "thinking_level_mapping": dict(mapping),
        }
    )
    return model


def _snapshot(*models: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "test",
        "snapshot_version": "test-snapshot-v1",
        "models": list(models),
    }


def _cache_ranking_config(
    *,
    strategy: str = "bonus",
    topologies: list[str] | None = None,
) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "strategy": strategy,
        "topologies": topologies or ["single", "multiple"],
        "ttl_seconds": 300,
        "age_decay": "linear",
    }
    if strategy == "bonus":
        policy["bonus_by_evidence"] = {
            "read_hit": 0.05,
            "write_only": 0.025,
        }
    else:
        policy["hit_probability_by_evidence"] = {
            "read_hit": 0.8,
            "write_only": 0.5,
        }
    return ranking_config_snapshot(override={"session": {"kv_cache_affinity": policy}})


def _calibration_ranking_config(
    *,
    enabled: bool = True,
    activation_model_identities: list[str] | None = None,
    prior_weight: float = 0.0,
    priors: dict[str, float] | None = None,
    residual_weight: float = 0.0,
    residuals: dict[str, dict[str, Any]] | None = None,
    residual_clip: float = 0.5,
    quality_guard_enabled: bool = False,
    max_quality_drop: float = 0.1,
    predicted_total_cost_enabled: bool = False,
    predicted_cost_reference_usd: float = 1.0,
    input_tokens_by_tier: dict[str, int] | None = None,
    output_tokens_by_tier: dict[str, int] | None = None,
) -> dict[str, Any]:
    policy = {
        "enabled": enabled,
        "activation_model_identities": activation_model_identities or [],
        "model_prior_weight": prior_weight,
        "model_quality_priors": priors or {},
        "residual_weight": residual_weight,
        "residual_clip": residual_clip,
        "model_residual_coefficients": residuals or {},
        "quality_guard_enabled": quality_guard_enabled,
        "max_quality_drop": max_quality_drop,
        "predicted_total_cost_enabled": predicted_total_cost_enabled,
        "predicted_cost_reference_usd": predicted_cost_reference_usd,
        "input_tokens_by_tier": input_tokens_by_tier
        or {str(tier): 1_000 for tier in range(1, 5)},
        "output_tokens_by_tier": output_tokens_by_tier
        or {str(tier): 1_000 for tier in range(1, 5)},
    }
    return ranking_config_snapshot(
        override={"single_route_calibration": policy}
    )


def _calibration_residual(intercept: float) -> dict[str, Any]:
    return {
        "intercept": intercept,
        "centers": {
            "capability": 0.0,
            "domain": 0.0,
            "tier": 0.0,
        },
        "coefficients": {
            "capability": 0.0,
            "domain": 0.0,
            "tier": 0.0,
        },
    }


def _cache_evidence(
    identity: str,
    *,
    role: str,
    price_quote: CachePriceQuote | None = None,
    decay_factor: float = 1.0,
) -> CacheAffinityEvidenceInput:
    return CacheAffinityEvidenceInput(
        identity=identity,
        role=role,  # type: ignore[arg-type]
        evidence_kind="read_hit",
        cached_tokens=1_000,
        cache_write_tokens=1_000,
        decay_factor=decay_factor,
        price_quote=price_quote,
        ranking_price_source=(price_quote.price_source if price_quote else ""),
        endpoint_scope=(price_quote.endpoint_scope if price_quote else ""),
        upstream_scope=(price_quote.upstream_scope if price_quote else ""),
    )


def _decision(
    *models: dict[str, Any],
    analysis: TaskAnalysisResult | None = None,
    context: dict[str, Any] | None = None,
    user_profile: dict[str, Any] | None = None,
    ranking_config: dict[str, Any] | None = None,
    thinking_assignment_enabled: bool = False,
    proposer_recovery_quorum: int | None = None,
    user_profile_enabled: bool = True,
    stage_observability_out: dict[str, Any] | None = None,
    cache_continuity_available: bool = False,
    cache_affinity_inputs: Any = None,
    cache_affinity_unavailable_reasons: Any = None,
):
    return rank_models(
        task_analysis=analysis or _analysis(),
        user_profile=(user_profile or mock_user_profile() if user_profile_enabled else None),
        request_context=context or _context(),
        registry_snapshot=_snapshot(*models),
        routed_tier="c2",
        routing_confidence=0.9,
        ranking_config=ranking_config,
        ranking_thinking_assignment_enabled=thinking_assignment_enabled,
        proposer_recovery_quorum=proposer_recovery_quorum,
        cache_continuity_available=cache_continuity_available,
        cache_affinity_inputs=cache_affinity_inputs,
        _cache_affinity_unavailable_reasons=(cache_affinity_unavailable_reasons),
        _stage_observability_out=stage_observability_out,
    )


def _replayable_decision():
    return rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _model("alpha", capability=0.95, aggregator_fit=0.82),
            _model("beta", capability=0.90, aggregator_fit=0.97),
            _model("gamma", capability=0.85, aggregator_fit=0.88),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="replay-decision",
    )


def test_ranking_trace_embeds_public_frozen_replay_evidence() -> None:
    trace = _replayable_decision().trace

    assert trace["registry_snapshot"]["models"]
    assert trace["request_context"]["snapshot_hash"] == trace["request_context_hash"]
    assert trace["task_profile_pre_escalation"]
    assert ranking_trace_replay_reasons(trace) == []


@pytest.mark.parametrize(
    ("schema_valid", "selected_index", "exhausted", "source"),
    [
        (True, 1, False, "provider"),
        (False, None, True, "router_fallback"),
    ],
)
def test_ranking_trace_replay_preserves_task_analyzer_chain(
    schema_valid: bool,
    selected_index: int | None,
    exhausted: bool,
    source: str,
) -> None:
    configured_routes = [
        {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "upstream_provider": "anthropic",
        },
        {
            "provider": "openrouter",
            "model": "openai/gpt-5.6-sol",
            "upstream_provider": "azure",
        },
        {
            "provider": "openrouter",
            "model": "google/gemini-3.1-pro-preview",
            "upstream_provider": "google-ai-studio",
        },
    ]
    outcome_count = len(configured_routes) if exhausted else int(selected_index or 0) + 1
    attempt_outcomes = [
        {
            "candidate_index": index,
            **route,
            "outcome": (
                "success" if selected_index is not None and index == selected_index else "failed"
            ),
            "reason": (
                "" if selected_index is not None and index == selected_index else "provider_error"
            ),
            "physical_request_count": 1,
        }
        for index, route in enumerate(configured_routes[:outcome_count])
    ]
    chain_trace = {
        "protocol": TASK_ANALYZER_FALLBACK_CHAIN_PROTOCOL,
        "configured_routes": configured_routes,
        "attempt_outcomes": attempt_outcomes,
        "selected_index": selected_index,
        "exhausted": exhausted,
    }
    terminal_route = configured_routes[
        selected_index if selected_index is not None else len(configured_routes) - 1
    ]
    analysis = TaskAnalysisResult(
        profile=_task_profile(tier=3),
        source=source,
        schema_valid=schema_valid,
        confidence=0.9,
        fallback_reason="" if schema_valid else "provider_error",
        provider_id=terminal_route["provider"],
        model_id=terminal_route["model"],
        chain_trace=chain_trace,
    )

    trace = rank_models(
        task_analysis=analysis,
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _model("alpha", capability=0.95, aggregator_fit=0.82),
            _model("beta", capability=0.90, aggregator_fit=0.97),
            _model("gamma", capability=0.85, aggregator_fit=0.88),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id=f"chain-replay-{source}",
    ).trace

    assert trace["task_analyzer"]["chain"] == chain_trace
    assert ranking_trace_replay_reasons(trace) == []


def test_v3_replay_binds_frozen_aggregator_candidate_chain() -> None:
    decision = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _thinking_model("alpha", provider="provider-a", capability=0.95),
            _thinking_model("beta", provider="provider-b", capability=0.90),
            _thinking_model("gamma", provider="provider-c", capability=0.85),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="aggregator-chain-replay",
        ranking_thinking_assignment_enabled=True,
    )
    trace = decision.trace
    assert trace["ranking_version"] == "step2-ranking-v4"

    tampered = json.loads(json.dumps(trace))
    tampered["aggregator_candidates"] = list(reversed(tampered["aggregator_candidates"]))

    assert "g1_frozen_ranker_replay_mismatch_aggregator_candidates" in ranking_trace_replay_reasons(
        tampered
    )


def test_legacy_v4_trace_without_embedded_enabled_switch_remains_replayable() -> None:
    trace = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _thinking_model("alpha", provider="provider-a", capability=0.95),
            _thinking_model("beta", provider="provider-b", capability=0.90),
            _thinking_model("gamma", provider="provider-c", capability=0.85),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="legacy-v4-switch-replay",
        ranking_thinking_assignment_enabled=True,
    ).trace
    legacy = deepcopy(trace)
    legacy["ranking_parameters"]["thinking_assignment"].pop("enabled")
    legacy["ranking_config_hash"] = canonical_json_sha256(legacy["ranking_parameters"])

    assert ranking_trace_replay_reasons(legacy) == []


def test_v3_managed_thinking_trace_requires_explicit_compatibility() -> None:
    trace = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _thinking_model(
                "alpha",
                provider="provider-a",
                capability=0.95,
            ),
            _thinking_model(
                "beta",
                provider="provider-b",
                capability=0.90,
            ),
            _thinking_model(
                "gamma",
                provider="provider-c",
                capability=0.85,
            ),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="legacy-managed-thinking-replay",
        ranking_thinking_assignment_enabled=True,
    ).trace
    legacy = deepcopy(trace)
    legacy["ranking_version"] = ranking_router.LEGACY_THINKING_RANKING_VERSION
    legacy.pop("thinking_physical_evidence_schema")
    legacy["thinking_assignment_details"].pop("aggregator_candidates")
    legacy["policy_versions"]["ranking"] = ranking_router.LEGACY_THINKING_RANKING_VERSION

    assert "g1_frozen_ranker_replay_mismatch_ranking_version" in (
        ranking_trace_replay_reasons(legacy)
    )
    assert (
        ranking_trace_replay_reasons(
            legacy,
            allow_legacy_managed_v3=True,
        )
        == []
    )

    tampered = deepcopy(legacy)
    tampered["policy_versions"]["thinking"] = "tampered"
    assert "g1_frozen_ranker_replay_mismatch_policy_versions" in ranking_trace_replay_reasons(
        tampered,
        allow_legacy_managed_v3=True,
    )

    missing_recovery_chain = deepcopy(legacy)
    missing_recovery_chain.pop("aggregator_candidates")
    assert "g1_frozen_ranker_replay_mismatch_aggregator_candidates" in ranking_trace_replay_reasons(
        missing_recovery_chain,
        allow_legacy_managed_v3=True,
    )


def test_legacy_v2_replay_allows_missing_aggregator_candidate_chain() -> None:
    trace = json.loads(json.dumps(_replayable_decision().trace))
    assert trace["ranking_version"] == "step2-ranking-v2"
    trace.pop("aggregator_candidates")

    assert ranking_trace_replay_reasons(trace) == []


@pytest.mark.parametrize("selection_field", ["selected_P", "selected_A"])
def test_frozen_replay_rejects_valid_pool_selection_swap(
    selection_field: str,
) -> None:
    trace = _replayable_decision().trace
    tampered = json.loads(json.dumps(trace))
    pool = [row["identity"] for row in trace["candidate_pool"]]
    if selection_field == "selected_P":
        tampered[selection_field] = list(reversed(trace[selection_field]))
    else:
        tampered[selection_field] = next(
            identity for identity in pool if identity != trace[selection_field]
        )

    assert f"g1_frozen_ranker_replay_mismatch_{selection_field}" in ranking_trace_replay_reasons(
        tampered
    )


@pytest.mark.parametrize(
    ("evidence", "needle"),
    [
        ({"api_key": "redacted"}, "secret-like field"),
        ({"nested": {"Authorization": "redacted"}}, "secret-like field"),
        ({"public_note": "sk-test-secret"}, "secret-like value"),
    ],
)
def test_ranking_trace_rejects_secret_like_replay_evidence(
    evidence: dict[str, Any],
    needle: str,
) -> None:
    context = _context()
    context["replay_evidence"] = evidence

    with pytest.raises(DynamicRankingError, match=needle):
        rank_models(
            task_analysis=_analysis(tier=3),
            user_profile=None,
            request_context=context,
            registry_snapshot=_snapshot(
                _model("alpha"),
                _model("beta"),
                _model("gamma"),
            ),
            routed_tier="c2",
            routing_confidence=0.9,
        )


def test_packaged_ranking_config_is_versioned_validated_and_isolated() -> None:
    first = load_ranking_config()
    second = load_ranking_config()

    assert first["schema_version"] == "step2-ranking-config-v4"
    assert first["config_version"].startswith("step2-ranking-")
    assert first["task_analyzer"]["max_output_tokens"] == 1_200
    assert first["task_analyzer"]["provider"] == "openrouter"
    assert first["task_analyzer"]["model"] == TASK_ANALYZER_MODEL_ID
    assert first["task_analyzer"]["upstream_provider"] == "together"
    assert first["task_analyzer"]["stream_close_timeout_seconds"] == 1.0
    assert first["routing_tiers"]["mapping"] == {"c0": 1, "c1": 2, "c2": 3, "c3": 4}
    assert first["context"]["bucket_min_tokens"]["extra_long"] == 128_000
    assert first["context"]["token_estimation"]["dense_chars_per_token"] == 1
    assert first["validation"]["task_profile_sum_tolerance"] == pytest.approx(0.02)
    assert first["fallback_task_profile"]["capability_dist"]["reasoning"] == 0.50
    assert first["synthetic_model"]["context_window"] == 128_000
    assert first["hard_filter"]["eligible_statuses"] == ["enabled", "canary"]
    assert first["exploration"] == {"enabled": False, "decision_propensity": 1.0}
    assert first["thinking_assignment"]["enabled"] is False
    assert first["rerank"]["similarity_penalty_weight"] == pytest.approx(0.25)
    assert first["role_reliability"] == {
        "penalty_weight": pytest.approx(0.40),
        "prior_success": 10,
        "prior_failure": 1,
    }
    assert first["proposer_count"]["backup_count"] == 2
    assert first["aggregator"]["candidate_count"] == 3
    first["rerank"]["similarity_penalty_weight"] = 99.0
    assert second["rerank"]["similarity_penalty_weight"] == pytest.approx(0.25)


def test_prepared_ranking_config_is_recursive_immutable_json_and_identity_copy() -> None:
    source = load_ranking_config()
    prepared = ranking_router._prepare_ranking_config(source)

    assert ranking_router._is_validated_ranking_config(prepared) is True
    assert prepared == source
    assert prepared["task_analyzer"] == source["task_analyzer"]
    assert (
        prepared["task_analyzer"]["fallback_chain"] == (source["task_analyzer"]["fallback_chain"])
    )
    assert copy(prepared) is prepared
    assert deepcopy(prepared) is prepared
    assert deepcopy(prepared["task_analyzer"]) is prepared["task_analyzer"]
    assert (
        deepcopy(prepared["task_analyzer"]["fallback_chain"])
        is (prepared["task_analyzer"]["fallback_chain"])
    )
    assert json.loads(ranking_router.canonical_json_bytes(prepared)) == source
    assert canonical_json_sha256(prepared) == canonical_json_sha256(source)

    rejected_mutations = (
        lambda: prepared.__setattr__("forged", True),
        lambda: prepared.__delattr__("forged"),
        lambda: prepared.__setitem__("config_version", "forged"),
        lambda: prepared.__delitem__("trace"),
        lambda: prepared.__ior__({"forged": True}),
        lambda: prepared.update({"config_version": "forged"}),
        lambda: prepared.pop("config_version"),
        lambda: prepared.popitem(),
        lambda: prepared.clear(),
        lambda: prepared["task_analyzer"].__setitem__("max_retries", 99),
        lambda: prepared["task_analyzer"].setdefault("forged", True),
        lambda: prepared["task_analyzer"]["fallback_chain"].__setitem__(0, {}),
        lambda: prepared["task_analyzer"]["fallback_chain"].__delitem__(0),
        lambda: prepared["task_analyzer"]["fallback_chain"].__iadd__([{}]),
        lambda: prepared["task_analyzer"]["fallback_chain"].__imul__(2),
        lambda: prepared["task_analyzer"]["fallback_chain"].append({}),
        lambda: prepared["task_analyzer"]["fallback_chain"].clear(),
        lambda: prepared["task_analyzer"]["fallback_chain"].extend([{}]),
        lambda: prepared["task_analyzer"]["fallback_chain"].insert(0, {}),
        lambda: prepared["task_analyzer"]["fallback_chain"].pop(),
        lambda: prepared["task_analyzer"]["fallback_chain"].remove(
            prepared["task_analyzer"]["fallback_chain"][0]
        ),
        lambda: prepared["task_analyzer"]["fallback_chain"].reverse(),
        lambda: prepared["task_analyzer"]["fallback_chain"].sort(),
    )
    for mutate in rejected_mutations:
        with pytest.raises(TypeError, match="immutable"):
            mutate()
    with pytest.raises(TypeError):
        dict.__setitem__(prepared, "config_version", "base-class-bypass")
    with pytest.raises(TypeError):
        list.__setitem__(
            prepared["task_analyzer"]["fallback_chain"],
            0,
            {},
        )
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(prepared, "_values", {})


def test_canonical_json_only_thaws_registered_immutable_containers() -> None:
    class CustomSequence(Sequence[int]):
        def __getitem__(self, index: int) -> int:
            return (1, 2, 3)[index]

        def __len__(self) -> int:
            return 3

    source = load_ranking_config()
    prepared = ranking_router._prepare_ranking_config(source)
    ordinary = {"tuple": (1, 2), "list": [3, 4]}

    assert json.loads(ranking_router.canonical_json_bytes(prepared)) == source
    assert ranking_router.canonical_json_bytes(ordinary) == (b'{"list":[3,4],"tuple":[1,2]}')
    with pytest.raises(TypeError, match="not JSON serializable"):
        ranking_router.canonical_json_bytes(range(3))
    with pytest.raises(TypeError, match="not JSON serializable"):
        ranking_router.canonical_json_bytes(CustomSequence())

    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="Circular reference"):
        ranking_router.canonical_json_bytes(cyclic)


def test_external_ranking_config_does_not_coerce_tuple_for_json_array_field() -> None:
    external = load_ranking_config()
    external["task_analyzer"]["fallback_chain"] = tuple(external["task_analyzer"]["fallback_chain"])

    with pytest.raises(DynamicRankingError):
        ranking_router._prepare_ranking_config(external)


def test_prepared_ranking_config_marker_cannot_be_forged() -> None:
    source = load_ranking_config()
    marker_type = ranking_router._ValidatedRankingConfig

    with pytest.raises(TypeError, match="factory-created"):
        marker_type(source, _factory_token=object())
    with pytest.raises(TypeError):
        dict.__new__(marker_type)
    unregistered = object.__new__(marker_type)
    assert ranking_router._is_validated_ranking_config(unregistered) is False
    with pytest.raises(TypeError, match="unregistered"):
        ranking_router._prepare_ranking_config(unregistered)

    class ForgedMarker(dict[str, Any]):
        pass

    forged = ForgedMarker(source)
    prepared = ranking_router._prepare_ranking_config(forged)
    assert prepared == source
    assert prepared is not forged
    assert ranking_router._is_validated_ranking_config(prepared) is True
    assert not hasattr(prepared, "_factory_token")
    assert not hasattr(ranking_router, "_freeze_validated_ranking_config")


def test_prepared_ranking_config_registry_releases_roots_and_children() -> None:
    source = load_ranking_config()
    gc.collect()
    baseline_size = ranking_router._ranking_config_registry_size()
    prepared = ranking_router._prepare_ranking_config(source)
    root_reference = weakref.ref(prepared)
    child_reference = weakref.ref(prepared["task_analyzer"])
    released_id = id(prepared)

    assert ranking_router._ranking_config_registry_size() > baseline_size
    del prepared
    gc.collect()
    assert root_reference() is None
    assert child_reference() is None
    assert ranking_router._ranking_config_registry_size() == baseline_size

    # A same-layout, unregistered allocation must never inherit authenticity
    # even if CPython reuses the released address.
    unregistered = object.__new__(ranking_router._ValidatedRankingConfig)
    reused_address = id(unregistered) == released_id
    assert ranking_router._is_validated_ranking_config(unregistered) is False
    if reused_address:
        assert ranking_router._ranking_config_registry_size() == baseline_size


def test_public_ranking_config_snapshots_are_detached_plain_json() -> None:
    prepared = ranking_router._prepare_ranking_config(load_ranking_config())
    snapshot = ranking_router._detached_ranking_config(prepared)
    public_snapshot = ranking_config_snapshot(thinking_assignment_enabled=True)

    assert type(snapshot) is dict
    assert type(snapshot["task_analyzer"]) is dict
    assert type(snapshot["task_analyzer"]["fallback_chain"]) is list
    assert type(public_snapshot) is dict
    assert type(public_snapshot["task_analyzer"]) is dict
    snapshot["task_analyzer"]["max_retries"] = 99
    assert prepared["task_analyzer"]["max_retries"] != 99


def test_prepared_ranking_config_skips_revalidation_across_hot_path_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = ranking_config_snapshot(thinking_assignment_enabled=True)
    original_validate = ranking_router._validate_ranking_config
    original_detach = ranking_router._detached_ranking_config
    validation_count = 0
    detach_count = 0

    def counted_validate(*args: Any, **kwargs: Any) -> Any:
        nonlocal validation_count
        validation_count += 1
        return original_validate(*args, **kwargs)

    def counted_detach(*args: Any, **kwargs: Any) -> Any:
        nonlocal detach_count
        detach_count += 1
        return original_detach(*args, **kwargs)

    monkeypatch.setattr(ranking_router, "_validate_ranking_config", counted_validate)
    monkeypatch.setattr(ranking_router, "_detached_ranking_config", counted_detach)
    prepared = ranking_router._prepare_effective_ranking_config(
        source,
        thinking_assignment_enabled=True,
    )
    for _ in range(100):
        assert (
            ranking_router._prepare_effective_ranking_config(
                prepared,
                thinking_assignment_enabled=True,
            )
            is prepared
        )
        task_analyzer_policy(prepared)
        task_analyzer_chain_policy(prepared)
        dynamic_output_token_budgets(
            configured_output_tokens=8_192,
            candidate_max_chars=24_000,
            ranking_config=prepared,
        )
        fallback_task_profile(
            routed_tier="c2",
            request_context=_context(),
            ranking_config=prepared,
        )

    assert validation_count == 1
    assert detach_count == 1


def test_analyzer_candidate_fast_freeze_requires_prevalidated_contracts() -> None:
    candidate = ranking_router.TaskAnalyzerCandidate(
        provider=None,
        provider_id="openrouter",
        model_id="openai/gpt-5.6-sol",
        upstream_provider="azure",
    )
    external = load_ranking_config()
    with pytest.raises(DynamicRankingError, match="requires a prepared base"):
        ranking_router._task_analyzer_candidate_ranking_config(
            external,
            candidate,
            schema_repair_max_retries=0,
        )

    prepared = ranking_router._prepare_ranking_config(external)
    for invalid_repair_count in (-1, True, 2):
        with pytest.raises(ValueError, match="must be 0 or 1"):
            ranking_router._task_analyzer_candidate_ranking_config(
                prepared,
                candidate,
                schema_repair_max_retries=invalid_repair_count,
            )

    rejected_provider = ranking_router.TaskAnalyzerCandidate(
        provider=None,
        provider_id="not-openrouter",
        model_id="vendor/model",
        upstream_provider="auto",
    )
    with pytest.raises(DynamicRankingError, match="must be openrouter"):
        ranking_router._task_analyzer_candidate_ranking_config(
            prepared,
            rejected_provider,
            schema_repair_max_retries=0,
        )

    for model_id, upstream_provider in (
        ("Vendor/Model", "auto"),
        ("vendor/model", "Invalid Provider"),
    ):
        malformed = object.__new__(ranking_router.TaskAnalyzerCandidate)
        object.__setattr__(malformed, "provider", None)
        object.__setattr__(malformed, "provider_id", "openrouter")
        object.__setattr__(malformed, "model_id", model_id)
        object.__setattr__(malformed, "upstream_provider", upstream_provider)
        with pytest.raises(ValueError):
            ranking_router._task_analyzer_candidate_ranking_config(
                prepared,
                malformed,
                schema_repair_max_retries=0,
            )

    for model_id, upstream_provider, repair_count in (
        ("openai/gpt-5.6-sol", "azure", 0),
        ("google/gemini-3.1-pro-preview", "auto", 1),
    ):
        accepted_candidate = ranking_router.TaskAnalyzerCandidate(
            provider=None,
            provider_id="openrouter",
            model_id=model_id,
            upstream_provider=upstream_provider,
        )
        derived = ranking_router._task_analyzer_candidate_ranking_config(
            prepared,
            accepted_candidate,
            schema_repair_max_retries=repair_count,
        )
        detached = ranking_router._detached_ranking_config(derived)
        fully_validated = ranking_router._validate_ranking_config(detached)
        assert ranking_router._is_validated_ranking_config(derived) is True
        assert derived == fully_validated
        assert detached["task_analyzer"]["fallback_chain"] == []
        assert detached["task_analyzer"]["max_retries"] == repair_count

    forged = object.__new__(ranking_router.TaskAnalyzerCandidate)
    object.__setattr__(forged, "provider", None)
    object.__setattr__(forged, "provider_id", "OpenRouter")
    object.__setattr__(forged, "model_id", "openai/gpt-5.6-sol")
    object.__setattr__(forged, "upstream_provider", "azure")
    with pytest.raises(ValueError, match="lowercase"):
        ranking_router._normalize_task_analyzer_chain_candidates([forged])


def test_ranking_snapshot_none_tracks_the_packaged_thinking_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packaged = load_ranking_config()
    packaged["thinking_assignment"]["enabled"] = True
    validated = ranking_router._validate_ranking_config(packaged)
    monkeypatch.setattr(
        ranking_router,
        "_packaged_ranking_config",
        lambda: validated,
    )
    monkeypatch.setattr(
        ranking_router,
        "_packaged_enabled_ranking_config",
        lambda: validated,
    )

    snapshot = ranking_config_snapshot()
    resolution = ranking_config_resolution()

    assert snapshot == resolution["effective_config"]
    assert snapshot["thinking_assignment"]["enabled"] is True
    assert resolution["thinking_assignment_enabled"] is True


@pytest.mark.parametrize(
    ("thinking_assignment_enabled", "schema_version", "config_version", "sha256"),
    [
        (
            False,
            "step2-ranking-config-v3",
            "step2-ranking-2026-08-25.1",
            "268ebb0c002994a9434eeecaef6b76571d0bd803db6eb2e7c8a788f3e2210e96",
        ),
        (
            True,
            "step2-ranking-config-v4",
            "step2-ranking-2026-08-25.1",
            "799b95e6426b243aff7672f0df3bdba58034a5b39a0b01fe5fb69f648456b31c",
        ),
    ],
)
def test_ranking_config_resolution_without_override_preserves_packaged_identity(
    thinking_assignment_enabled: bool,
    schema_version: str,
    config_version: str,
    sha256: str,
) -> None:
    snapshot = ranking_config_snapshot(thinking_assignment_enabled=thinking_assignment_enabled)
    resolution = ranking_config_resolution(thinking_assignment_enabled=thinking_assignment_enabled)
    empty_resolution = ranking_config_resolution(
        thinking_assignment_enabled=thinking_assignment_enabled,
        override={},
    )

    assert resolution["override"] is None
    assert resolution["override_sha256"] is None
    assert resolution["base_config"] == snapshot
    assert resolution["effective_config"] == snapshot
    assert resolution["base_sha256"] == sha256
    assert resolution["effective_sha256"] == sha256
    assert resolution["effective_config"]["schema_version"] == schema_version
    assert resolution["effective_config"]["config_version"] == config_version
    assert resolution["effective_config"]["task_analyzer"]["max_retries"] == 1
    assert empty_resolution == resolution
    assert (
        ranking_config_snapshot(
            thinking_assignment_enabled=thinking_assignment_enabled,
            override={},
        )
        == snapshot
    )


def test_task_analyzer_chain_policy_preserves_historical_single_route() -> None:
    current = task_analyzer_chain_policy()
    historical_config = ranking_config_snapshot(base_version="step2-ranking-2026-08-10.1")
    historical = task_analyzer_chain_policy(historical_config)
    sparse_primary_override = task_analyzer_chain_policy(
        ranking_config_snapshot(
            override={
                "task_analyzer": {
                    "model": "openai/gpt-5.6-sol",
                    "upstream_provider": "azure",
                }
            }
        )
    )

    assert current["configured"] is True
    assert current["routes"] == [
        {
            "provider": provider,
            "model": model,
            "upstream_provider": upstream,
        }
        for provider, model, upstream in _TASK_ANALYZER_CHAIN_ROUTES
    ]
    assert current["total_timeout_seconds"] == 60.0
    assert current["schema_repair_max_retries"] == 0
    assert historical["configured"] is False
    assert historical["routes"] == [
        {
            "provider": "openrouter",
            "model": "anthropic/claude-opus-4.8",
            "upstream_provider": "anthropic",
        }
    ]
    assert historical["total_timeout_seconds"] == 20.0
    assert historical["schema_repair_max_retries"] == 0
    assert canonical_json_sha256(historical_config) == (
        "4dbefbec7ebae151e68937b73acef8b8e173862e84af474e143a6b013264879e"
    )
    assert [route["model"] for route in sparse_primary_override["routes"]] == [
        "openai/gpt-5.6-sol",
        "google/gemini-3.1-pro-preview",
    ]


def test_historical_ranking_base_reconstructs_frozen_draco_identity() -> None:
    historical = ranking_config_resolution(
        thinking_assignment_enabled=False,
        base_version="step2-ranking-2026-08-02.2",
    )
    default = ranking_config_resolution(thinking_assignment_enabled=False)

    assert historical["base_config"]["schema_version"] == "step2-ranking-config-v3"
    assert historical["base_config"]["config_version"] == ("step2-ranking-2026-08-02.2")
    assert "thinking_assignment" not in historical["base_config"]
    assert "role_reliability" not in historical["base_config"]
    assert historical["base_sha256"] == (
        "71be283f94095bc3ced34d39ae9ed58abbaa7e4d273b0a074e7e8a4a6e4b5fc6"
    )
    assert default["base_config"]["config_version"] == "step2-ranking-2026-08-25.1"
    assert "role_reliability" in default["base_config"]
    assert default["base_config"]["normalization"][
        "price_reference_usd_per_million"
    ] == pytest.approx(5.5)
    assert default["base_config"]["penalties"]["latency_penalty_enabled"] is False
    assert default["base_config"]["penalties"]["task_cost_weights"] == {
        "low": pytest.approx(0.45),
        "medium": pytest.approx(0.10),
        "high": pytest.approx(0.04),
        "hard_limit": pytest.approx(0.28),
    }
    assert default["base_config"]["rerank"]["top_l_min"] == 8
    assert default["base_config"]["task_analyzer"]["max_retries"] == 1
    assert default["base_config"]["task_analyzer"]["schema_repair_max_retries"] == 0

    previous_c1_baseline = ranking_config_resolution(
        thinking_assignment_enabled=False,
        base_version="step2-ranking-2026-08-21.2",
    )
    previous_c1_baseline_thinking = ranking_config_resolution(
        thinking_assignment_enabled=True,
        base_version="step2-ranking-2026-08-21.2",
    )
    assert previous_c1_baseline["base_sha256"] == (
        "2b89c0769b67e5b0cdaaf8da5f4de36db893b636e7c7552c2f08537bde97b7bc"
    )
    assert previous_c1_baseline_thinking["base_sha256"] == (
        "d0d8b213346a40951985ff750ab69c4c22ad931acaa8f23b64f1a0e084bdfc05"
    )
    assert "single_route_calibration" not in previous_c1_baseline["base_config"]

    previous_upstream_policy = ranking_config_resolution(
        thinking_assignment_enabled=False,
        base_version="step2-ranking-2026-08-21.1",
    )
    previous_upstream_policy_thinking = ranking_config_resolution(
        thinking_assignment_enabled=True,
        base_version="step2-ranking-2026-08-21.1",
    )
    assert previous_upstream_policy["base_config"]["task_analyzer"]["model"] == (
        "deepseek/deepseek-v4-pro"
    )
    assert previous_upstream_policy["base_config"]["task_analyzer"][
        "upstream_provider"
    ] == "deepseek"
    assert previous_upstream_policy["base_sha256"] == (
        "a578f9fc4abfaf09cc64254328cf4a74d7cbfd8323e199982088e8cdcf9914c4"
    )
    assert previous_upstream_policy_thinking["base_sha256"] == (
        "03fed14a08ad3424c0c77d06f7cbe15e3ac07b95d66992e99a0a10efb8246ca8"
    )

    previous_task_analyzer_policy = ranking_config_resolution(
        thinking_assignment_enabled=False,
        base_version="step2-ranking-2026-08-18.4",
    )
    previous_task_analyzer_policy_thinking = ranking_config_resolution(
        thinking_assignment_enabled=True,
        base_version="step2-ranking-2026-08-18.4",
    )
    assert previous_task_analyzer_policy["base_config"]["task_analyzer"]["model"] == (
        "anthropic/claude-opus-4.8"
    )
    assert previous_task_analyzer_policy["base_config"]["task_analyzer"][
        "upstream_provider"
    ] == "anthropic"
    assert previous_task_analyzer_policy["base_sha256"] == (
        "a81e82aa1ac60e7fcaee3ebb6a084a8c20576526c11171aa65ca316064755693"
    )
    assert previous_task_analyzer_policy_thinking["base_sha256"] == (
        "ca3778b791caaa4719388330cc8c2721ae16b1a1f6ba5d7e4310b929ace93dcf"
    )

    previous_schema_repair_policy = ranking_config_resolution(
        thinking_assignment_enabled=False,
        base_version="step2-ranking-2026-08-18.3",
    )
    previous_schema_repair_policy_thinking = ranking_config_resolution(
        thinking_assignment_enabled=True,
        base_version="step2-ranking-2026-08-18.3",
    )
    assert (
        previous_schema_repair_policy["base_config"]["task_analyzer"]["schema_repair_max_retries"]
        == 1
    )
    assert (
        previous_schema_repair_policy["base_config"]["normalization"]
        == (previous_c1_baseline["base_config"]["normalization"])
    )
    assert (
        previous_schema_repair_policy["base_config"]["penalties"]
        == (previous_c1_baseline["base_config"]["penalties"])
    )
    assert previous_schema_repair_policy["base_sha256"] == (
        "667eb6057b2d261a2cb6a82b1073c87896e5b3f7b25124f18ade488900792e16"
    )
    assert previous_schema_repair_policy_thinking["base_sha256"] == (
        "07fe68f66881107f73166a6276cd61cab5712658c542f8ef56379292669eadb9"
    )

    previous_resource_policy = ranking_config_resolution(
        thinking_assignment_enabled=False,
        base_version="step2-ranking-2026-08-18.2",
    )
    previous_resource_policy_thinking = ranking_config_resolution(
        thinking_assignment_enabled=True,
        base_version="step2-ranking-2026-08-18.2",
    )
    assert previous_resource_policy["base_config"]["normalization"][
        "price_reference_usd_per_million"
    ] == pytest.approx(40.0)
    assert "latency_penalty_enabled" not in previous_resource_policy["base_config"]["penalties"]
    assert (
        previous_resource_policy["base_config"]["penalties"]["task_cost_weights"]
        == previous_c1_baseline["base_config"]["penalties"]["task_cost_weights"]
    )
    assert previous_resource_policy["base_config"]["rerank"]["top_l_min"] == 8
    assert previous_resource_policy["base_config"]["task_analyzer"]["max_retries"] == 1
    assert previous_resource_policy["base_sha256"] == (
        "96a8c24133d22cc5af204d212379193d6a7a8839048139fd41a83d58384b56bf"
    )
    assert previous_resource_policy_thinking["base_sha256"] == (
        "5b9039dd50ec5c46aacd710214673a65e833f439ab7c1343c2f07205c3325281"
    )

    previous_packaged = ranking_config_resolution(
        thinking_assignment_enabled=False,
        base_version="step2-ranking-2026-08-18.1",
    )
    assert previous_packaged["base_config"]["penalties"]["task_cost_weights"] == {
        "low": pytest.approx(0.28),
        "medium": pytest.approx(0.20),
        "high": pytest.approx(0.10),
        "hard_limit": pytest.approx(0.40),
    }
    assert previous_packaged["base_config"]["rerank"]["top_l_min"] == 15
    assert previous_packaged["base_config"]["task_analyzer"]["max_retries"] == 3
    assert previous_packaged["base_sha256"] == (
        "2e04d910089c772e6001076b406ebc56b83471bef8041078134c18c5b81b1d71"
    )

    previous_cost_balance = ranking_config_resolution(
        thinking_assignment_enabled=False,
        base_version="step2-ranking-2026-08-11.2",
    )
    assert previous_cost_balance["base_config"]["penalties"]["task_cost_weights"] == {
        "low": pytest.approx(0.20),
        "medium": pytest.approx(0.10),
        "high": pytest.approx(0.04),
        "hard_limit": pytest.approx(0.28),
    }
    assert previous_cost_balance["base_config"]["rerank"]["top_l_min"] == 8
    assert previous_cost_balance["base_sha256"] == (
        "cdb2727e95533d9c49579b9bb82b40ad801f816e2f151b8bc4e8f1540bbb7fbe"
    )

    previous_reliability = ranking_config_resolution(
        thinking_assignment_enabled=False,
        base_version="step2-ranking-2026-08-05.1",
    )
    assert previous_reliability["base_config"]["role_reliability"] == {
        "penalty_weight": pytest.approx(0.40),
        "prior_success": 9,
        "prior_failure": 1,
    }


@pytest.mark.parametrize(
    ("thinking_assignment_enabled", "schema_version", "sha256"),
    [
        (
            False,
            "step2-ranking-config-v3",
            "8e78140419906b29c5c8e4e80e5057ee5a87d308c950e36b49e7d9995b2e338f",
        ),
        (
            True,
            "step2-ranking-config-v4",
            "44085643f6272e143a6200fe222462ac8e3943c9eb381de121bf1ca9d3ffc132",
        ),
    ],
)
def test_previous_zero_failure_prior_base_reconstructs_frozen_identity(
    thinking_assignment_enabled: bool,
    schema_version: str,
    sha256: str,
) -> None:
    resolution = ranking_config_resolution(
        thinking_assignment_enabled=thinking_assignment_enabled,
        base_version="step2-ranking-2026-08-11.1",
    )

    assert resolution["base_config"]["schema_version"] == schema_version
    assert resolution["base_config"]["config_version"] == ("step2-ranking-2026-08-11.1")
    assert resolution["base_config"]["role_reliability"] == {
        "penalty_weight": pytest.approx(0.40),
        "prior_success": 10,
        "prior_failure": 0,
    }
    assert resolution["base_sha256"] == sha256
    assert resolution["effective_sha256"] == sha256


@pytest.mark.parametrize(
    ("thinking_assignment_enabled", "effective_sha256"),
    [
        (
            False,
            "ab2c6f116c24a66efc5a2aa2fc162f3a4b3a24976a6cd230913794bade6a98db",
        ),
        (
            True,
            "0f4c53386ffa303ae3bc2200c42b64e913492d282fc61f4c92c499e3a16e0ee7",
        ),
    ],
)
def test_previous_zero_failure_prior_override_remains_hash_bound(
    thinking_assignment_enabled: bool,
    effective_sha256: str,
) -> None:
    resolution = ranking_config_resolution(
        thinking_assignment_enabled=thinking_assignment_enabled,
        base_version="step2-ranking-2026-08-11.1",
        override={"penalties": {"task_cost_weights": {"medium": 0.17}}},
    )

    assert resolution["override_sha256"] == (
        "c2ac5b1a3c8ed7e88dcebefdabb4bcffcc3dd5daeead645def2ce9c847cb9a89"
    )
    assert resolution["effective_config"]["config_version"] == (
        "step2-ranking-2026-08-11.1+override.c2ac5b1a3c8e"
    )
    assert resolution["effective_sha256"] == effective_sha256


def test_ranking_base_selector_rejects_unallowlisted_version() -> None:
    with pytest.raises(DynamicRankingError, match="base_version .* is not available"):
        ranking_config_resolution(base_version="step2-ranking-2026-08-03.1")


def test_ranking_config_resolution_deep_merges_sparse_nested_override() -> None:
    override = {"penalties": {"task_cost_weights": {"medium": 0.17}}}

    resolution = ranking_config_resolution(override=override)
    snapshot = ranking_config_snapshot(override=override)

    assert resolution["base_config"]["penalties"]["task_cost_weights"]["medium"] == 0.10
    assert resolution["effective_config"]["penalties"]["task_cost_weights"] == {
        "low": 0.45,
        "medium": 0.17,
        "high": 0.04,
        "hard_limit": 0.28,
    }
    assert snapshot == resolution["effective_config"]
    assert resolution["effective_config"]["config_version"] == (
        f"step2-ranking-2026-08-25.1+override.{resolution['override_sha256'][:12]}"
    )
    assert resolution["effective_sha256"] != resolution["base_sha256"]
    override["penalties"]["task_cost_weights"]["medium"] = 99
    assert resolution["override"]["penalties"]["task_cost_weights"]["medium"] == 0.17


def test_task_analyzer_policy_is_public_overrideable_and_protocol_pinned() -> None:
    resolution = ranking_config_resolution(
        override={
            "task_analyzer": {
                "model": "openai/gpt-5.2",
                "upstream_provider": "openai",
                "stream_close_timeout_seconds": 2.5,
                "max_retries": 2,
            }
        }
    )

    assert task_analyzer_policy(resolution["effective_config"]) == {
        "protocol_version": TASK_ANALYZER_VERSION,
        "provider": "openrouter",
        "model": "openai/gpt-5.2",
        "upstream_provider": "openai",
        "stream_close_timeout_seconds": 2.5,
        "timeout_seconds": 20.0,
        "max_retries": 2,
    }


def test_task_analyzer_policy_replays_authenticated_legacy_shape() -> None:
    legacy = load_ranking_config()
    legacy["config_version"] = "step2-ranking-2026-08-02.1"
    legacy["penalties"].pop("latency_penalty_enabled")
    for key in (
        "provider",
        "model",
        "upstream_provider",
        "stream_close_timeout_seconds",
    ):
        legacy["task_analyzer"].pop(key)

    assert task_analyzer_policy(legacy)["model"] == TASK_ANALYZER_MODEL_ID
    current = deepcopy(legacy)
    current["config_version"] = "step2-ranking-2026-08-02.2"
    with pytest.raises(DynamicRankingError, match="lacks the versioned task_analyzer"):
        task_analyzer_policy(current)


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (["not", "an", "object"], "must be a JSON object"),
        ({"schema_version": "other"}, "cannot override"),
        ({"config_version": "caller-controlled"}, "cannot override"),
        ({"penalties": {"unknown": 1}}, "unknown or missing keys"),
        (
            {"penalties": {"task_cost_weights": {"low": "0.20"}}},
            "must be numeric",
        ),
        (
            {"proposer_count": {"backup_count": 3}},
            "backup_count must be between 0 and 2",
        ),
        (
            {"aggregator": {"candidate_count": 0}},
            "candidate_count must be between 1 and 3",
        ),
        (
            {"task_analyzer": {"provider": "anthropic"}},
            "provider currently must be openrouter",
        ),
        (
            {"task_analyzer": {"model": "OpenAI/GPT-5.2"}},
            "model must be lowercase",
        ),
        (
            {"task_analyzer": {"model": "invalid-model"}},
            "contain '/'",
        ),
        (
            {"task_analyzer": {"upstream_provider": "Google AI"}},
            "upstream_provider must be a lowercase",
        ),
        (
            {"task_analyzer": {"stream_close_timeout_seconds": 0.0}},
            "stream_close_timeout_seconds must be positive",
        ),
        (
            {"task_analyzer": {"stream_close_timeout_seconds": 21.0}},
            "no greater than timeout_seconds",
        ),
        ({"penalties": {"api_key": "redacted"}}, "secret-like field"),
        ({"penalties": {"credential_available": True}}, "secret-like field"),
    ],
)
def test_ranking_config_resolution_rejects_invalid_sparse_override(
    override: Any,
    message: str,
) -> None:
    with pytest.raises(DynamicRankingError, match=message):
        ranking_config_resolution(override=override)


def test_ranking_config_override_hash_and_version_are_key_order_stable() -> None:
    first = {
        "penalties": {"task_cost_weights": {"medium": 0.17, "low": 0.31}},
        "rerank": {"similarity_penalty_weight": 0.20},
    }
    second = {
        "rerank": {"similarity_penalty_weight": 0.20},
        "penalties": {"task_cost_weights": {"low": 0.31, "medium": 0.17}},
    }

    first_resolution = ranking_config_resolution(override=first)
    second_resolution = ranking_config_resolution(override=second)

    assert first_resolution["override_sha256"] == second_resolution["override_sha256"]
    assert first_resolution["effective_sha256"] == second_resolution["effective_sha256"]
    assert (
        first_resolution["effective_config"]["config_version"]
        == second_resolution["effective_config"]["config_version"]
    )


def test_ranking_config_override_unicode_hash_uses_canonical_utf8() -> None:
    override = {"mock_user_profile": {"profile_source": "实验-中文"}}

    resolution = ranking_config_resolution(override=override)
    normalized_override = resolution["override"]
    expected = hashlib.sha256(
        json.dumps(
            normalized_override,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    ascii_escaped = hashlib.sha256(
        json.dumps(
            normalized_override,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()

    assert resolution["override_sha256"] == expected
    assert canonical_json_sha256(normalized_override) == expected
    assert expected != ascii_escaped
    assert resolution["effective_config"]["config_version"].endswith(expected[:12])


def test_ranking_config_override_resolves_against_selected_thinking_base() -> None:
    override = {"penalties": {"task_cost_weights": {"medium": 0.17}}}

    legacy = ranking_config_resolution(override=override)
    thinking = ranking_config_resolution(
        thinking_assignment_enabled=True,
        override=override,
    )

    assert legacy["override_sha256"] == thinking["override_sha256"]
    assert legacy["effective_sha256"] != thinking["effective_sha256"]
    assert legacy["effective_config"]["schema_version"] == "step2-ranking-config-v3"
    assert "thinking_assignment" not in legacy["effective_config"]
    assert thinking["effective_config"]["schema_version"] == "step2-ranking-config-v4"
    assert "thinking_assignment" in thinking["effective_config"]
    suffix = legacy["override_sha256"][:12]
    assert legacy["effective_config"]["config_version"] == (
        f"step2-ranking-2026-08-25.1+override.{suffix}"
    )
    assert thinking["effective_config"]["config_version"] == (
        f"step2-ranking-2026-08-25.1+override.{suffix}"
    )


def test_ranking_config_override_is_the_authoritative_thinking_switch() -> None:
    override = {"thinking_assignment": {"enabled": True}}

    resolution = ranking_config_resolution(override=override)

    assert resolution["base_config"]["schema_version"] == "step2-ranking-config-v3"
    assert "thinking_assignment" not in resolution["base_config"]
    assert resolution["effective_config"]["schema_version"] == "step2-ranking-config-v4"
    assert resolution["effective_config"]["thinking_assignment"]["enabled"] is True
    assert resolution["thinking_assignment_enabled"] is True
    assert ranking_config_snapshot(override=override) == resolution["effective_config"]


@pytest.mark.parametrize(
    ("legacy_switch", "override_switch"),
    [(False, True), (True, False)],
)
def test_ranking_config_override_rejects_conflicting_legacy_thinking_switch(
    legacy_switch: bool,
    override_switch: bool,
) -> None:
    with pytest.raises(DynamicRankingError, match="conflicts with the legacy"):
        ranking_config_resolution(
            thinking_assignment_enabled=legacy_switch,
            override={"thinking_assignment": {"enabled": override_switch}},
        )


def test_invalid_ranking_config_fails_before_selection() -> None:
    config = load_ranking_config()
    config["quality"]["task_match_weight"] = 0.90

    with pytest.raises(DynamicRankingError, match="sum to 1"):
        _decision(_model("only"), analysis=_analysis(tier=1), ranking_config=config)


def test_ranking_config_rejects_ambiguous_or_inactive_settings() -> None:
    duplicate_errors = load_ranking_config()
    duplicate_errors["rerank"]["error_dimensions"].append("timeout")

    ambiguous_tiers = load_ranking_config()
    ambiguous_tiers["routing_tiers"]["mapping"]["c3"] = 3

    bool_penalty = load_ranking_config()
    bool_penalty["penalties"]["task_cost_weights"]["low"] = True

    non_boolean_latency_policy = load_ranking_config()
    non_boolean_latency_policy["penalties"]["latency_penalty_enabled"] = 0

    missing_latency_policy = load_ranking_config()
    missing_latency_policy["penalties"].pop("latency_penalty_enabled")

    historical_latency_policy = load_ranking_config(base_version="step2-ranking-2026-08-18.2")
    historical_latency_policy["penalties"]["latency_penalty_enabled"] = False

    inactive_exploration = load_ranking_config()
    inactive_exploration["exploration"]["enabled"] = True

    for config, message in (
        (duplicate_errors, "cannot contain duplicates"),
        (ambiguous_tiers, "one-to-one"),
        (bool_penalty, "must be numeric"),
        (non_boolean_latency_policy, "latency_penalty_enabled must be boolean"),
        (missing_latency_policy, "lacks the versioned latency penalty policy"),
        (historical_latency_policy, "historical ranking config cannot declare"),
        (inactive_exploration, "exploration is reserved"),
    ):
        with pytest.raises(DynamicRankingError, match=message):
            _decision(
                _model("only"),
                analysis=_analysis(tier=1),
                ranking_config=config,
            )


@pytest.mark.parametrize("invalid_value", [0, 1, "false", None, [], {}])
def test_latency_penalty_policy_requires_a_json_boolean(invalid_value: Any) -> None:
    config = load_ranking_config()
    config["penalties"]["latency_penalty_enabled"] = invalid_value

    with pytest.raises(DynamicRankingError, match="latency_penalty_enabled must be boolean"):
        ranking_router._validate_ranking_config(config)


def test_ranking_config_rejects_unknown_or_missing_nested_parameters() -> None:
    unknown = load_ranking_config()
    unknown["rerank"]["similarity"]["capabilty_weight"] = 0.5

    missing = load_ranking_config()
    missing["task_analyzer"].pop("temperature")

    unsupported_protocol_value = load_ranking_config()
    unsupported_protocol_value["penalties"]["task_cost_weights"]["economy"] = 0.1

    for config, message in (
        (unknown, "unknown or missing keys"),
        (missing, "unknown or missing keys"),
        (unsupported_protocol_value, "supported protocol values"),
    ):
        with pytest.raises(DynamicRankingError, match=message):
            _decision(
                _model("only"),
                analysis=_analysis(tier=1),
                ranking_config=config,
            )


@pytest.mark.parametrize(
    ("role_reliability", "message"),
    [
        (
            {
                "window_size": 49,
                "proposer": {"success": 0, "failure": 0},
                "aggregator": {"success": 0, "failure": 0},
                "source": "aef_experiment_artifacts",
            },
            "window_size must be 50",
        ),
        (
            {
                "window_size": 50,
                "proposer": {"success": -1, "failure": 0},
                "aggregator": {"success": 0, "failure": 0},
                "source": "aef_experiment_artifacts",
            },
            "role_reliability.proposer.success",
        ),
        (
            {
                "window_size": 50,
                "proposer": {"success": 50, "failure": 1},
                "aggregator": {"success": 0, "failure": 0},
                "source": "aef_experiment_artifacts",
            },
            "proposer exceeds window_size",
        ),
        (
            {
                "window_size": 50,
                "proposer": {"success": 1.0, "failure": 0},
                "aggregator": {"success": 0, "failure": 0},
                "source": "aef_experiment_artifacts",
            },
            "role_reliability.proposer.success",
        ),
        (
            {
                "window_size": 50,
                "proposer": {"success": 0, "failure": 0},
                "aggregator": {"success": 0, "failure": 0},
            },
            "invalid role_reliability",
        ),
    ],
)
def test_role_reliability_profile_is_strictly_validated(
    role_reliability: dict[str, Any],
    message: str,
) -> None:
    model = _model("invalid-reliability")
    model["online_profile"]["role_reliability"] = role_reliability

    with pytest.raises(DynamicRankingError, match=message):
        _decision(model, analysis=_analysis(tier=1))


def test_role_reliability_config_is_strictly_validated() -> None:
    excessive_penalty = load_ranking_config()
    excessive_penalty["role_reliability"]["penalty_weight"] = 1.01
    negative_prior = load_ranking_config()
    negative_prior["role_reliability"]["prior_success"] = -1
    empty_prior = load_ranking_config()
    empty_prior["role_reliability"].update({"prior_success": 0, "prior_failure": 0})
    missing_policy = load_ranking_config()
    missing_policy.pop("role_reliability")

    for config, message in (
        (excessive_penalty, "penalty_weight must be between 0 and 1"),
        (negative_prior, "priors must be non-negative integers"),
        (empty_prior, "priors must have a positive total"),
        (missing_policy, "lacks the versioned role_reliability policy"),
    ):
        with pytest.raises(DynamicRankingError, match=message):
            _decision(_model("only"), analysis=_analysis(tier=1), ranking_config=config)


def test_packaged_curated_registry_has_versioned_step2_profiles() -> None:
    snapshot = load_model_registry_snapshot()
    model_ids = [model["registry_facts"]["model_id"] for model in snapshot["models"]]
    mistral_statuses = {
        model["registry_facts"]["model_id"]: model["registry_facts"]["status"]
        for model in snapshot["models"]
        if model["registry_facts"]["vendor"] == "mistralai"
    }

    assert snapshot["snapshot_version"].startswith("curated-openrouter-step2-")
    assert len(snapshot["models"]) == 80
    assert len(set(model_ids)) == len(model_ids)
    assert mistral_statuses == {model_id: "disabled" for model_id in _MISTRAL_MODEL_IDS}
    assert {
        "poolside/laguna-xs-2.1",
        "tencent/hy3",
        "kwaipilot/kat-coder-air-v2.5",
        "meta-llama/llama-4-scout",
        "kwaipilot/kat-coder-pro-v2.5",
        "minimax/minimax-m3",
        "mistralai/mistral-medium-3-5",
        "openai/gpt-5.6-luna",
        "anthropic/claude-sonnet-5",
        "x-ai/grok-4.5",
        "google/gemini-3.1-pro-preview",
        "anthropic/claude-fable-5",
        "moonshotai/kimi-k3",
        "thinkingmachines/inkling",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "openai/gpt-oss-120b",
        "qwen/qwen3.6-27b",
        "inclusionai/ling-2.6-1t",
        "openai/gpt-oss-20b",
        "qwen/qwen3.5-9b",
        "qwen/qwen3.5-122b-a10b",
    }.issubset(model_ids)
    for model in snapshot["models"]:
        facts = model["registry_facts"]
        assert facts["model_id"]
        assert facts["provider"] == "openrouter"
        assert facts["roles"]
        assert facts["context_window"] > 0
        assert type(facts["is_open_source"]) is bool
        assert type(facts["is_chinese_model"]) is bool
        assert type(facts["supports_reasoning"]) is bool
        assert type(facts["supports_tools"]) is bool
        thinking_levels = facts["supported_thinking_levels"]
        assert thinking_levels
        assert len(thinking_levels) == len(set(thinking_levels))
        assert set(thinking_levels) <= set(THINKING_LEVELS)
        assert model["runtime"]["thinking"] == thinking_levels[0]
        assert facts["supports_reasoning"] is any(level != "off" for level in thinking_levels)
        verified_at = date.fromisoformat(facts["catalog_verified_at"])
        assert date(2026, 7, 24) <= verified_at <= date(2026, 8, 20)
        assert facts["latency_source"] == "curated_estimate"
        assert set(model["static_profile"]["capability_dist_prior"]) == set(CAPABILITIES)
        assert set(model["static_profile"]["domain_dist_prior"]) == set(DOMAINS)
        assert model["static_profile"]["tier_dist_prior"]
        assert model["static_profile"]["role_fit_prior"]["aggregator"] >= 0
        assert model["online_profile"]["source"] == "curated_estimate"

    curated_models = [
        model for model in snapshot["models"] if model["source"] == "curated_openrouter_profile"
    ]
    assert len(curated_models) == 80
    by_model_id = {model["registry_facts"]["model_id"]: model for model in curated_models}
    qwen_9b = by_model_id["qwen/qwen3.5-9b"]
    qwen_122b = by_model_id["qwen/qwen3.5-122b-a10b"]
    expected_qwen_9b_facts = {
        "version": "qwen/qwen3.5-9b-20260310",
        "provider": "openrouter",
        "context_window": 262144,
        "modalities": ["text", "image", "video"],
        "price": {"input_per_million": 0.1, "output_per_million": 0.15},
        "supports_reasoning": True,
        "supports_tools": True,
        "supported_thinking_levels": ["high", "off"],
        "catalog_verified_at": "2026-08-20",
    }
    assert {
        key: qwen_9b["registry_facts"][key] for key in expected_qwen_9b_facts
    } == expected_qwen_9b_facts
    expected_qwen_122b_facts = {
        "version": "qwen/qwen3.5-122b-a10b-20260224",
        "provider": "openrouter",
        "context_window": 262144,
        "price": {"input_per_million": 0.26, "output_per_million": 2.08},
        "supports_reasoning": True,
        "supports_tools": True,
        "supported_thinking_levels": ["high", "off"],
        "catalog_verified_at": "2026-08-20",
    }
    assert {
        key: qwen_122b["registry_facts"][key] for key in expected_qwen_122b_facts
    } == expected_qwen_122b_facts
    assert qwen_9b["runtime"]["thinking"] == qwen_122b["runtime"]["thinking"] == "high"
    for prior_name in (
        "capability_dist_prior",
        "domain_dist_prior",
        "tier_dist_prior",
        "role_fit_prior",
    ):
        assert all(
            value <= qwen_122b["static_profile"][prior_name][key]
            for key, value in qwen_9b["static_profile"][prior_name].items()
        )
    assert qwen_9b["registry_facts"]["latency_p95_ms"] > qwen_9b["registry_facts"]["latency_p50_ms"]
    assert by_model_id["z-ai/glm-5.2"]["registry_facts"]["price"] == {
        "input_per_million": 1.4,
        "output_per_million": 4.4,
    }
    assert by_model_id["deepseek/deepseek-v4-pro"]["registry_facts"]["price"] == {
        "input_per_million": 1.32,
        "output_per_million": 3.96,
    }
    assert by_model_id["moonshotai/kimi-k2.6"]["registry_facts"]["price"] == {
        "input_per_million": 0.95,
        "output_per_million": 4.0,
    }
    assert by_model_id["nvidia/nemotron-3-ultra-550b-a55b"]["registry_facts"]["price"] == {
        "input_per_million": 0.6,
        "output_per_million": 3.6,
    }
    assert by_model_id["moonshotai/kimi-k3"]["registry_facts"]["modalities"] == [
        "text",
        "image",
        "video",
    ]
    assert by_model_id["moonshotai/kimi-k3"]["static_profile"]["tier_dist_prior"][
        "4"
    ] == pytest.approx(0.85)
    codex_profile = by_model_id["openai/gpt-5.3-codex"]["static_profile"]
    assert codex_profile["capability_dist_prior"] == {
        **codex_profile["capability_dist_prior"],
        "summarization": pytest.approx(0.89),
        "writing": pytest.approx(0.86),
    }
    assert {
        key: codex_profile["domain_dist_prior"][key]
        for key in (
            "business_analysis",
            "creative_writing",
            "education",
            "customer_support",
            "general",
        )
    } == {
        "business_analysis": pytest.approx(0.90),
        "creative_writing": pytest.approx(0.89),
        "education": pytest.approx(0.89),
        "customer_support": pytest.approx(0.88),
        "general": pytest.approx(0.91),
    }
    assert codex_profile["tier_dist_prior"]["4"] == pytest.approx(0.88)
    assert codex_profile["role_fit_prior"] == {
        "proposer": pytest.approx(0.97),
        "aggregator": pytest.approx(0.86),
    }
    sonnet_profile = by_model_id["anthropic/claude-sonnet-5"]["static_profile"]
    assert sonnet_profile["role_fit_prior"] == {
        "proposer": pytest.approx(0.94),
        "aggregator": pytest.approx(0.95),
    }
    assert {
        model_id
        for model_id, model in by_model_id.items()
        if model["registry_facts"]["status"] == "disabled"
    } == {
        "anthropic/claude-fable-5",
        "arcee-ai/trinity-large-thinking",
        "deepseek/deepseek-v3.1-terminus",
        "google/gemini-3.5-flash",
        "google/gemma-3-27b-it",
        "google/gemma-4-26b-a4b-it",
        "inclusionai/ling-2.6-1t",
        "inclusionai/ring-2.6-1t",
        "kwaipilot/kat-coder-air-v2.5",
        "kwaipilot/kat-coder-pro-v2.5",
        "meituan/longcat-2.0",
        "meta-llama/llama-3.3-70b-instruct",
        "mistralai/mistral-large-2512",
        "mistralai/mistral-medium-3-5",
        "mistralai/mistral-small-2603",
        "mistralai/ministral-14b-2512",
        "mistralai/voxtral-small-24b-2507",
        "moonshotai/kimi-k3",
        "nex-agi/nex-n2-pro",
        "poolside/laguna-s-2.1",
        "poolside/laguna-xs-2.1",
        "qwen/qwen3.6-27b",
        "tencent/hy3",
        "tencent/hy3-preview",
        "z-ai/glm-5.1",
    }
    assert by_model_id["deepseek/deepseek-v4-flash"]["registry_facts"][
        "supported_thinking_levels"
    ] == ["xhigh", "high", "off"]
    assert by_model_id["anthropic/claude-opus-4.8"]["runtime"]["thinking"] == "max"
    assert by_model_id["kwaipilot/kat-coder-pro-v2.5"]["registry_facts"][
        "supported_thinking_levels"
    ] == ["off"]
    assert (
        min(model["registry_facts"]["price"]["input_per_million"] for model in curated_models)
        <= 0.05
    )
    assert (
        max(model["static_profile"]["role_fit_prior"]["proposer"] for model in curated_models)
        >= 0.94
    )


def test_historical_registry_base_reconstructs_frozen_draco_identity() -> None:
    current = load_model_registry_snapshot()
    historical = load_model_registry_snapshot(base_version="curated-openrouter-step2-2026-07-31.1")
    frozen = ranking_router._legacy_registry_snapshot_projection(historical)

    assert current["snapshot_version"].startswith(
        "curated-openrouter-step2-2026-08-20.1-reliability-"
    )
    assert "role_reliability_snapshot" in current
    provenance = current["role_reliability_snapshot"]
    assert provenance["schema_version"] == "role-reliability-snapshot-v2"
    assert provenance["base_snapshot_version"] == ("curated-openrouter-step2-2026-08-20.1")
    assert provenance["observation_policy"] == "aef-physical-model-calls-v5"
    assert provenance["completion_gate"] == "manual_thresholded_draco_audit"
    assert set(provenance) == {
        "schema_version",
        "generated_at",
        "base_snapshot_version",
        "window_size",
        "observation_policy",
        "completion_gate",
        "content_sha256",
    }
    assert provenance["content_sha256"] == (
        ranking_router._role_reliability_snapshot_content_sha256(current["models"], provenance)
    )
    assert current["snapshot_version"].endswith(f"-{provenance['content_sha256'][:12]}")
    assert all("role_reliability" in row["online_profile"] for row in current["models"])
    selected_rows = {
        row["registry_facts"]["model_id"]: row["online_profile"]["role_reliability"]
        for row in current["models"]
        if any(
            row["online_profile"]["role_reliability"][role]["success"]
            or row["online_profile"]["role_reliability"][role]["failure"]
            for role in ("proposer", "aggregator")
        )
    }
    assert set(selected_rows) == {
        "anthropic/claude-fable-5",
        "anthropic/claude-opus-4.8",
        "anthropic/claude-sonnet-5",
        "google/gemini-3.1-pro-preview",
        "inclusionai/ring-2.6-1t",
        "moonshotai/kimi-k3",
        "openai/gpt-5.3-codex",
        "openai/gpt-5.5",
        "openai/gpt-5.6-sol",
        "qwen/qwen3.7-max",
        "x-ai/grok-4.5",
        "z-ai/glm-5.2",
    }
    assert selected_rows["anthropic/claude-sonnet-5"] == {
        "window_size": 50,
        "proposer": {"success": 27, "failure": 23},
        "aggregator": {"success": 48, "failure": 2},
        "source": "aef_experiment_artifacts_thresholded",
    }
    assert selected_rows["qwen/qwen3.7-max"] == {
        "window_size": 50,
        "proposer": {"success": 8, "failure": 0},
        "aggregator": {"success": 0, "failure": 0},
        "source": "aef_experiment_artifacts_thresholded",
    }
    assert selected_rows["z-ai/glm-5.2"] == {
        "window_size": 50,
        "proposer": {"success": 13, "failure": 1},
        "aggregator": {"success": 4, "failure": 0},
        "source": "aef_experiment_artifacts_thresholded",
    }
    assert selected_rows["moonshotai/kimi-k3"] == {
        "window_size": 50,
        "proposer": {"success": 17, "failure": 0},
        "aggregator": {"success": 0, "failure": 0},
        "source": "aef_experiment_artifacts_thresholded",
    }
    assert selected_rows["inclusionai/ring-2.6-1t"] == {
        "window_size": 50,
        "proposer": {"success": 0, "failure": 0},
        "aggregator": {"success": 14, "failure": 0},
        "source": "aef_experiment_artifacts_thresholded",
    }
    current_model = ranking_router._normalize_model(
        current["models"][0],
        load_ranking_config(),
    )
    current_reliability = ranking_router._role_reliability_score(
        current_model,
        "proposer",
        load_ranking_config(),
    )
    assert current_reliability["failure_rate"] == pytest.approx(1 / 11)
    assert current_reliability["penalty"] == pytest.approx(0.40 / 11)
    assert historical["snapshot_version"] == "curated-openrouter-step2-2026-07-31.1"
    assert "role_reliability_snapshot" not in historical
    assert all("role_reliability" not in row["online_profile"] for row in historical["models"])
    historical_by_model = {row["registry_facts"]["model_id"]: row for row in historical["models"]}
    assert historical_by_model["z-ai/glm-5.2"]["registry_facts"]["price"] == {
        "input_per_million": 0.8246,
        "output_per_million": 2.5916,
    }
    assert historical_by_model["deepseek/deepseek-v4-pro"]["registry_facts"]["price"] == {
        "input_per_million": 0.435,
        "output_per_million": 0.87,
    }
    assert historical_by_model["moonshotai/kimi-k2.6"]["registry_facts"]["price"] == {
        "input_per_million": 0.684,
        "output_per_million": 3.42,
    }
    assert historical_by_model["nvidia/nemotron-3-ultra-550b-a55b"]["registry_facts"]["price"] == {
        "input_per_million": 0.5,
        "output_per_million": 2.2,
    }
    assert historical_by_model["moonshotai/kimi-k3"]["registry_facts"]["modalities"] == [
        "text",
        "image",
    ]
    assert historical_by_model["moonshotai/kimi-k3"]["static_profile"]["tier_dist_prior"][
        "4"
    ] == pytest.approx(0.79)
    historical_codex_profile = historical_by_model["openai/gpt-5.3-codex"]["static_profile"]
    assert {
        key: historical_codex_profile["capability_dist_prior"][key]
        for key in ("summarization", "writing")
    } == {
        "summarization": pytest.approx(0.93),
        "writing": pytest.approx(0.93),
    }
    assert {
        key: historical_codex_profile["domain_dist_prior"][key]
        for key in (
            "business_analysis",
            "creative_writing",
            "education",
            "customer_support",
            "general",
        )
    } == {
        "business_analysis": pytest.approx(0.95),
        "creative_writing": pytest.approx(0.94),
        "education": pytest.approx(0.95),
        "customer_support": pytest.approx(0.93),
        "general": pytest.approx(0.97),
    }
    assert historical_codex_profile["tier_dist_prior"]["4"] == pytest.approx(0.94)
    assert historical_codex_profile["role_fit_prior"]["proposer"] == pytest.approx(0.99)
    assert historical_by_model["anthropic/claude-sonnet-5"]["static_profile"]["role_fit_prior"] == {
        "proposer": pytest.approx(0.96),
        "aggregator": pytest.approx(0.95),
    }
    assert all(
        historical_by_model[model_id]["registry_facts"]["status"] == "enabled"
        for model_id in {
            "anthropic/claude-fable-5",
            "arcee-ai/trinity-large-thinking",
            "deepseek/deepseek-v3.1-terminus",
            "google/gemini-3.5-flash",
            "google/gemma-3-27b-it",
            "google/gemma-4-26b-a4b-it",
            "inclusionai/ling-2.6-1t",
            "inclusionai/ring-2.6-1t",
            "kwaipilot/kat-coder-air-v2.5",
            "kwaipilot/kat-coder-pro-v2.5",
            "meituan/longcat-2.0",
            "meta-llama/llama-3.3-70b-instruct",
            "moonshotai/kimi-k3",
            "nex-agi/nex-n2-pro",
            "poolside/laguna-s-2.1",
            "poolside/laguna-xs-2.1",
            "qwen/qwen3.6-27b",
            "tencent/hy3",
            "tencent/hy3-preview",
            "z-ai/glm-5.1",
        }
    )
    assert canonical_json_sha256(historical) == (
        "b51b64d7880472e47f8a5f954b1a76eaee440d6cd59d28f9dc2579f876bac1ea"
    )
    assert frozen["schema_version"] == "step2-model-registry-v1"
    assert canonical_json_sha256(frozen) == (
        "9f76c7f96e5cb22c05b615f69b71ca633965e5039fbec9673f0a5edf9b45078a"
    )
    current_base = load_model_registry_snapshot(
        base_version="curated-openrouter-step2-2026-08-20.1"
    )
    assert current_base["snapshot_version"] == "curated-openrouter-step2-2026-08-20.1"
    assert current_base["models"] == [
        {
            **row,
            "online_profile": {
                key: value
                for key, value in row["online_profile"].items()
                if key != "role_reliability"
            },
        }
        for row in current["models"]
    ]
    pre_qwen35_9b_base = load_model_registry_snapshot(
        base_version="curated-openrouter-step2-2026-08-19.1"
    )
    assert pre_qwen35_9b_base["snapshot_version"] == ("curated-openrouter-step2-2026-08-19.1")
    assert len(pre_qwen35_9b_base["models"]) == 79
    assert canonical_json_sha256(pre_qwen35_9b_base) == (
        "3f17f3169034af15829c5162e5590a03316eff821eac9e1dd9d31e745fe84681"
    )
    assert "qwen/qwen3.5-9b" not in {
        row["registry_facts"]["model_id"] for row in pre_qwen35_9b_base["models"]
    }
    pre_qwen35_9b_full = load_model_registry_snapshot(
        base_version=(
            "curated-openrouter-step2-2026-08-19.1-reliability-20260818T121632Z-01580c6982b4"
        )
    )
    archived_pre_qwen35_9b = json.loads(
        resources.files("opensquilla.provider")
        .joinpath("router_dynamic_model_profiles_20260819_01580c6982b4.json")
        .read_text(encoding="utf-8")
    )
    assert pre_qwen35_9b_full == archived_pre_qwen35_9b
    assert canonical_json_sha256(pre_qwen35_9b_full) == (
        "2ed00a4d7053ac771e31b4651b55f3a0c2b53e7217fe3c76006e42e5181d9beb"
    )
    pre_qwen35_9b_by_model = {
        row["registry_facts"]["model_id"]: row for row in pre_qwen35_9b_base["models"]
    }
    assert (
        pre_qwen35_9b_by_model["qwen/qwen3.5-122b-a10b"]["registry_facts"]["catalog_verified_at"]
        == "2026-07-24"
    )
    pre_status_base = load_model_registry_snapshot(
        base_version="curated-openrouter-step2-2026-08-18.2"
    )
    assert pre_status_base["snapshot_version"] == "curated-openrouter-step2-2026-08-18.2"
    assert canonical_json_sha256(pre_status_base) == (
        "1eb4ebbf3cfb5903b99f104a05b52f3037b772a03de9493072ddad67d1ce970e"
    )
    previous_base = load_model_registry_snapshot(
        base_version="curated-openrouter-step2-2026-08-18.1"
    )
    assert previous_base["snapshot_version"] == ("curated-openrouter-step2-2026-08-18.1")
    assert canonical_json_sha256(previous_base) == (
        "f23130040c4ccbe5e2e3bff74edd16a03cc928e1a957a055fec23f02f8f1b62b"
    )
    current_base_by_model = {
        row["registry_facts"]["model_id"]: row for row in current_base["models"]
    }
    pre_status_base_by_model = {
        row["registry_facts"]["model_id"]: row for row in pre_status_base["models"]
    }
    previous_by_model = {row["registry_facts"]["model_id"]: row for row in previous_base["models"]}
    assert all(
        current_base_by_model[model_id]["registry_facts"]["status"] == "disabled"
        for model_id in _MISTRAL_MODEL_IDS
    )
    for registry_by_model in (
        historical_by_model,
        pre_status_base_by_model,
        previous_by_model,
    ):
        assert all(
            registry_by_model[model_id]["registry_facts"]["status"] == "enabled"
            for model_id in _MISTRAL_MODEL_IDS
        )
    assert (
        previous_by_model["openai/gpt-5.3-codex"]["static_profile"]
        == (historical_by_model["openai/gpt-5.3-codex"]["static_profile"])
    )
    assert (
        previous_by_model["anthropic/claude-sonnet-5"]["static_profile"]
        == (historical_by_model["anthropic/claude-sonnet-5"]["static_profile"])
    )
    for model_id in set(previous_by_model) - {
        "openai/gpt-5.3-codex",
        "anthropic/claude-sonnet-5",
    }:
        assert previous_by_model[model_id] == pre_status_base_by_model[model_id]


def test_pre_status_disable_full_registry_snapshot_remains_replayable() -> None:
    archived = load_model_registry_snapshot(
        base_version=(
            "curated-openrouter-step2-2026-08-18.2-reliability-20260818T121632Z-a04f8e2167bf"
        )
    )
    assert archived["snapshot_version"] == (
        "curated-openrouter-step2-2026-08-18.2-reliability-20260818T121632Z-a04f8e2167bf"
    )
    assert canonical_json_sha256(archived) == (
        "b656ee2f7cdbcefa73c6263f9cf34c25d655bba10d32229058fc6e0373d84611"
    )
    by_model_id = {row["registry_facts"]["model_id"]: row for row in archived["models"]}
    assert all(
        by_model_id[model_id]["registry_facts"]["status"] == "enabled"
        for model_id in _MISTRAL_MODEL_IDS
    )
    projected = ranking_router._legacy_registry_snapshot_projection(archived)
    assert canonical_json_sha256(projected) == (
        "686144c33d57bf57ec9b1785f604d10c061928eaf993e09bf6beae54ab985cd3"
    )


def test_static_profile_calibration_preserves_code_and_cn_controls() -> None:
    current = load_model_registry_snapshot()
    historical = load_model_registry_snapshot(base_version="curated-openrouter-step2-2026-07-31.1")
    current_by_model = {row["registry_facts"]["model_id"]: row for row in current["models"]}
    historical_by_model = {row["registry_facts"]["model_id"]: row for row in historical["models"]}
    config = load_ranking_config()
    current_codex = ranking_router._normalize_model(
        current_by_model["openai/gpt-5.3-codex"], config
    )
    historical_codex = ranking_router._normalize_model(
        historical_by_model["openai/gpt-5.3-codex"], config
    )
    code_profile = _task_profile(tier=4)
    general_writing_profile = _task_profile(tier=4)
    general_writing_profile["capability_dist"] = {"writing": 1.0}
    general_writing_profile["domain_dist"] = {"general": 1.0}

    assert ranking_router._task_match(current_codex, code_profile, config, role="proposer") > 0.95
    assert ranking_router._task_match(
        current_codex, general_writing_profile, config, role="proposer"
    ) < ranking_router._task_match(
        historical_codex, general_writing_profile, config, role="proposer"
    )
    assert ranking_router._task_match(
        ranking_router._normalize_model(current_by_model["anthropic/claude-sonnet-5"], config),
        code_profile,
        config,
        role="aggregator",
    ) == pytest.approx(
        ranking_router._task_match(
            ranking_router._normalize_model(
                historical_by_model["anthropic/claude-sonnet-5"], config
            ),
            code_profile,
            config,
            role="aggregator",
        )
    )
    for model_id in ("qwen/qwen3.7-max", "z-ai/glm-5.2"):
        assert (
            current_by_model[model_id]["static_profile"]
            == (historical_by_model[model_id]["static_profile"])
        )


def test_packaged_registry_rejects_obsolete_or_tampered_statistics() -> None:
    current = load_model_registry_snapshot()
    obsolete = deepcopy(current)
    obsolete["role_reliability_snapshot"]["observation_policy"] = "aef-physical-model-calls-v3"
    with pytest.raises(DynamicRankingError, match="observation policy is obsolete"):
        ranking_router._validate_packaged_role_reliability_provenance(obsolete)

    nonzero = deepcopy(current)
    nonzero["models"][0]["online_profile"]["role_reliability"]["proposer"]["success"] = 1
    with pytest.raises(DynamicRankingError, match="content_sha256 differs"):
        ranking_router._validate_packaged_role_reliability_provenance(nonzero)


def test_packaged_registry_rejects_nonzero_invalidated_statistics() -> None:
    invalidated = load_model_registry_snapshot()
    for row in invalidated["models"]:
        row["online_profile"]["role_reliability"] = {
            "window_size": 50,
            "proposer": {"success": 0, "failure": 0},
            "aggregator": {"success": 0, "failure": 0},
            "source": "legacy_statistics_invalidated",
        }
    provenance = invalidated["role_reliability_snapshot"]
    provenance.clear()
    provenance.update(
        {
            "schema_version": "role-reliability-snapshot-v2",
            "observation_policy": "legacy-statistics-invalidated-v1",
            "completion_gate": "legacy_statistics_invalidated",
        }
    )
    provenance["content_sha256"] = ranking_router._role_reliability_snapshot_content_sha256(
        invalidated["models"], provenance
    )
    ranking_router._validate_packaged_role_reliability_provenance(invalidated)

    invalidated["models"][0]["online_profile"]["role_reliability"]["proposer"]["success"] = 1
    with pytest.raises(DynamicRankingError, match="nonzero counts"):
        ranking_router._validate_packaged_role_reliability_provenance(invalidated)


def test_packaged_v5_reliability_statistics_are_content_bound() -> None:
    current = load_model_registry_snapshot()
    trusted = deepcopy(current)
    provenance = trusted["role_reliability_snapshot"]
    provenance["observation_policy"] = "aef-physical-model-calls-v5"
    provenance["completion_gate"] = "clean_final_audit"
    provenance.pop("invalidated_observation_policy", None)
    provenance.pop("invalidated_snapshot_version", None)
    provenance.pop("invalidation_reason", None)
    provenance["content_sha256"] = ranking_router._role_reliability_snapshot_content_sha256(
        trusted["models"], provenance
    )
    ranking_router._validate_packaged_role_reliability_provenance(trusted)

    tampered = deepcopy(trusted)
    tampered["models"][0]["online_profile"]["role_reliability"]["proposer"]["success"] = 1
    with pytest.raises(DynamicRankingError, match="content_sha256 differs"):
        ranking_router._validate_packaged_role_reliability_provenance(tampered)

    missing_hash = deepcopy(trusted)
    missing_hash["role_reliability_snapshot"].pop("content_sha256")
    with pytest.raises(DynamicRankingError, match="valid content_sha256"):
        ranking_router._validate_packaged_role_reliability_provenance(missing_hash)

    unbound_v1 = deepcopy(trusted)
    unbound_v1["role_reliability_snapshot"]["schema_version"] = "role-reliability-snapshot-v1"
    with pytest.raises(DynamicRankingError, match="content-bound provenance"):
        ranking_router._validate_packaged_role_reliability_provenance(unbound_v1)


@pytest.mark.parametrize(
    "schema_version",
    ["role-reliability-snapshot-v1", "role-reliability-snapshot-v2"],
)
def test_historical_registry_base_accepts_versioned_reliability_provenance(
    monkeypatch: pytest.MonkeyPatch,
    schema_version: str,
) -> None:
    current = load_model_registry_snapshot()
    current["role_reliability_snapshot"]["schema_version"] = schema_version
    monkeypatch.setattr(ranking_router, "_packaged_registry_snapshot", lambda: current)

    historical = load_model_registry_snapshot(base_version="curated-openrouter-step2-2026-08-20.1")
    assert historical["snapshot_version"] == "curated-openrouter-step2-2026-08-20.1"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("unsupported_schema", "provenance schema is unsupported"),
        ("missing_base", "provenance has a different base"),
        ("wrong_snapshot_prefix", "cannot reconstruct"),
    ],
)
def test_historical_registry_base_rejects_unauthenticated_provenance(
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    current = load_model_registry_snapshot()
    if mutation == "unsupported_schema":
        current["role_reliability_snapshot"]["schema_version"] = "unknown"
    elif mutation == "missing_base":
        current["role_reliability_snapshot"].pop("base_snapshot_version")
    else:
        current["snapshot_version"] = "unrelated-snapshot"
    monkeypatch.setattr(ranking_router, "_packaged_registry_snapshot", lambda: current)

    with pytest.raises(DynamicRankingError, match=message):
        load_model_registry_snapshot(base_version="curated-openrouter-step2-2026-08-20.1")


def test_registry_base_selector_rejects_unallowlisted_version() -> None:
    with pytest.raises(DynamicRankingError, match="base_version .* is not available"):
        load_model_registry_snapshot(base_version="curated-openrouter-step2-unknown")


def test_normalize_task_profile_falls_back_on_missing_required_distributions() -> None:
    profile, valid, errors = normalize_task_profile(
        {"constraints": {"risk": "low"}},
        routed_tier="c3",
        request_context=_context(),
    )

    assert valid is False
    assert "invalid_capability_dist" in errors
    assert profile["tier_dist"] == {"4": 1.0}
    assert profile["session_intent"] == {"type": "new_task", "confidence": 0.0}


@pytest.mark.parametrize(
    ("mutate", "expected_error"),
    [
        (
            lambda profile: profile.update(
                capability_dist={"reasoning": 0.5, "code_generation": 0.3}
            ),
            "invalid_capability_dist",
        ),
        (
            lambda profile: profile.update(
                capability_dist={"reasoning": "0.6", "code_generation": 0.4}
            ),
            "invalid_capability_dist",
        ),
        (
            lambda profile: profile["session_intent"].update(confidence=True),
            "invalid_session_intent_confidence",
        ),
    ],
)
def test_normalize_task_profile_rejects_invalid_required_numeric_fields(
    mutate: Any,
    expected_error: str,
) -> None:
    raw_profile = _task_profile(tier=2)
    mutate(raw_profile)

    _, valid, issues = normalize_task_profile(
        raw_profile,
        routed_tier="c1",
        request_context=_context(),
    )

    assert valid is False
    assert expected_error in issues


def test_normalize_task_profile_accepts_configured_distribution_rounding() -> None:
    raw_profile = _task_profile(tier=2)
    raw_profile["capability_dist"] = {"reasoning": 0.60, "code_generation": 0.39}

    profile, valid, issues = normalize_task_profile(
        raw_profile,
        routed_tier="c1",
        request_context=_context(),
    )

    assert valid is True
    assert issues == []
    assert sum(profile["capability_dist"].values()) == pytest.approx(1.0)


def test_normalize_task_profile_repairs_domain_distribution_format() -> None:
    raw_profile = _task_profile(tier=2)
    raw_profile["domain_dist"] = {
        "Software Engineering": "2",
        "unsupported-domain": 1.0,
    }

    profile, valid, issues = normalize_task_profile(
        raw_profile,
        routed_tier="c1",
        request_context=_context(),
    )

    assert valid is True
    assert issues == ["repaired_domain_dist"]
    assert profile["domain_dist"] == {"software_engineering": 1.0}
    assert sum(profile["domain_dist"].values()) == pytest.approx(1.0)
    assert set(profile["domain_dist"]).issubset(DOMAINS)


def test_request_context_uses_bounded_history_and_attachment_facts() -> None:
    context = build_request_context(
        message="current request",
        turn_metadata={
            "router_history_user_texts": ["old-1", "old-2"],
            "router_prev_assistant_text": "previous answer",
        },
        attachments=[{"name": "diagram.png", "media_type": "image/png"}],
        candidate_output_tokens=2_000,
        aggregator_output_tokens=3_000,
    )

    assert context["conversation"]["recent_turns"] == [
        "user: old-1",
        "user: old-2",
        "assistant: previous answer",
    ]
    assert context["input_modalities"] == ["text", "image"]
    assert context["workspace_state"]["referenced_files"] == ["diagram.png"]
    assert len(context["snapshot_hash"]) == 64


def _legacy_request_context_golden_kwargs() -> dict[str, Any]:
    config = load_ranking_config()
    config["context"]["request_limits"].update(
        {
            "role_max_chars": 32,
            "max_recent_turns": 6,
            "fallback_history_max_turns": 4,
            "turn_max_chars": 2_000,
            "summary_max_chars": 4_000,
            "state_max_items": 32,
            "item_max_chars": 512,
            "tool_summary_max_chars": 4_000,
            "test_results_max_chars": 2_000,
            "intermediate_max_items": 8,
            "intermediate_max_chars": 2_000,
            "attachment_max_items": 32,
            "last_route_max_models": 8,
            "max_scanned_items_multiplier": 4,
        }
    )
    config["context"]["output_budget"]["minimum_tokens"] = 1
    config["context"]["token_estimation"].update(
        {
            "utf8_bytes_per_token": 4,
            "dense_chars_per_token": 1,
        }
    )
    config["hard_filter"]["default_required_modalities"] = ["text"]
    return {
        "message": "ship 修复",
        "turn_metadata": {
            "router_history_user_texts": ["old-1", "old-2"],
            "router_prev_assistant_text": "previous answer",
            "input_tokens": 321,
            "tool_log_tokens": 77,
            "router_dynamic_request_context": {
                "tool_state": {
                    "called_tools": ["shell"],
                    "tool_results_summary": "ok",
                },
                "workspace_state": {
                    "changed_files": ["a.py"],
                    "test_results": "pass",
                },
            },
        },
        "attachments": [
            {"name": "diagram.JPG", "media_type": "IMAGE/JPG; charset=binary"},
            {"name": "brief.pdf", "mime": "application/pdf"},
        ],
        "candidate_output_tokens": 2_000,
        "aggregator_output_tokens": 3_000,
        "ranking_config": config,
    }


def test_request_context_refactor_preserves_legacy_canonical_bytes() -> None:
    context = build_request_context(**_legacy_request_context_golden_kwargs())

    expected = (
        b'{"attachment_refs":["diagram.JPG","brief.pdf"],'
        b'"conversation":{"recent_turns":["user: old-1","user: old-2",'
        b'"assistant: previous answer"],"summary":""},'
        b'"input_modalities":["text","image"],'
        b'"intermediate_outputs":{"current_errors":[],"previous_candidates":[]},'
        b'"last_route":{},"routing_budget":{"aggregator_output_tokens":3000,'
        b'"candidate_output_tokens":2000,"estimated_input_tokens":321,'
        b'"tool_log_tokens":77},'
        b'"snapshot_hash":"d8ef9ac869db0dec23192d2bbdabbdbd065d382e211df3f619aed1150ebc614a",'
        b'"tool_state":{"called_tools":["shell"],"failed_tools":[],'
        b'"tool_results_summary":"ok"},'
        b'"workspace_state":{"changed_files":["a.py"],'
        b'"referenced_files":["diagram.JPG","brief.pdf"],"test_results":"pass"}}'
    )

    assert ranking_router.canonical_json_bytes(context) == expected


@pytest.mark.parametrize(
    ("candidate_output_tokens", "aggregator_output_tokens"),
    [(1, 1), (2_000, 3_000), (0, -7)],
)
def test_fusion_request_context_is_role_neutral_base_plus_legacy_budgets(
    candidate_output_tokens: int,
    aggregator_output_tokens: int,
) -> None:
    kwargs = _legacy_request_context_golden_kwargs()
    kwargs["candidate_output_tokens"] = candidate_output_tokens
    kwargs["aggregator_output_tokens"] = aggregator_output_tokens
    actual = build_request_context(**kwargs)
    effective_config = ranking_router._resolve_ranking_config(kwargs["ranking_config"])
    expected = ranking_router._build_request_context_base(
        message=kwargs["message"],
        turn_metadata=kwargs["turn_metadata"],
        attachments=kwargs["attachments"],
        last_route=ranking_router._legacy_fusion_last_route(
            turn_metadata=kwargs["turn_metadata"],
            effective_config=effective_config,
        ),
        include_previous_candidates=True,
        effective_config=effective_config,
    )
    minimum_tokens = effective_config["context"]["output_budget"]["minimum_tokens"]
    expected["routing_budget"].update(
        {
            "candidate_output_tokens": max(minimum_tokens, candidate_output_tokens),
            "aggregator_output_tokens": max(minimum_tokens, aggregator_output_tokens),
        }
    )
    expected["snapshot_hash"] = ranking_router._request_context_hash(expected)

    assert actual == expected
    assert list(actual["routing_budget"]) == [
        "estimated_input_tokens",
        "tool_log_tokens",
        "candidate_output_tokens",
        "aggregator_output_tokens",
    ]


@pytest.mark.parametrize(
    "media_type",
    [
        "image/gif",
        "image/jpg",
        "IMAGE/PNG; charset=binary",
        "image/webp",
    ],
)
def test_request_context_normalizes_native_image_mime(media_type: str) -> None:
    context = build_request_context(
        message="review the image",
        turn_metadata={},
        attachments=[{"name": "diagram", "media_type": media_type}],
        candidate_output_tokens=2_000,
        aggregator_output_tokens=3_000,
    )

    assert context["input_modalities"] == ["text", "image"]


def test_request_context_bounds_history_and_projects_attachments_like_runtime() -> None:
    context = build_request_context(
        message="current request",
        turn_metadata={
            "material_estimated_tokens": 12_345,
            "router_dynamic_request_context": {
                "conversation": {
                    "summary": "s" * 5_000,
                    "recent_turns": [f"turn-{index}-" + ("x" * 3_000) for index in range(9)],
                }
            },
        },
        attachments=[
            {"filename": "voice.wav", "mime": "audio/wav"},
            {"name": "clip.mp4", "type": "video/mp4"},
            {"name": "brief.pdf", "media_type": "application/pdf"},
        ],
        candidate_output_tokens=2_000,
        aggregator_output_tokens=3_000,
    )

    assert len(context["conversation"]["summary"]) == 4_000
    assert len(context["conversation"]["recent_turns"]) == 6
    assert context["conversation"]["recent_turns"][0].startswith("turn-3-")
    assert all(len(turn) <= 2_000 for turn in context["conversation"]["recent_turns"])
    assert context["input_modalities"] == ["text"]
    assert context["attachment_refs"] == ["voice.wav", "clip.mp4", "brief.pdf"]
    assert context["routing_budget"]["estimated_input_tokens"] >= 12_345


def test_request_context_limits_and_token_estimation_are_config_driven() -> None:
    config = load_ranking_config()
    config["context"]["request_limits"]["max_recent_turns"] = 2
    config["context"]["request_limits"]["turn_max_chars"] = 12
    config["context"]["token_estimation"]["utf8_bytes_per_token"] = 1

    context = build_request_context(
        message="abcdefghij",
        turn_metadata={
            "router_dynamic_request_context": {
                "conversation": {
                    "recent_turns": ["first-long-turn", "second-long-turn", "third-long-turn"]
                }
            }
        },
        attachments=[],
        candidate_output_tokens=10,
        aggregator_output_tokens=10,
        ranking_config=config,
    )

    assert context["conversation"]["recent_turns"] == ["second-long-", "third-long-t"]
    assert context["routing_budget"]["estimated_input_tokens"] >= 10


def test_request_context_uses_a_conservative_dense_script_token_estimate() -> None:
    ascii_context = build_request_context(
        message="a" * 400,
        turn_metadata={},
        attachments=[],
        candidate_output_tokens=10,
        aggregator_output_tokens=10,
    )
    dense_context = build_request_context(
        message="中" * 400,
        turn_metadata={},
        attachments=[],
        candidate_output_tokens=10,
        aggregator_output_tokens=10,
    )

    ascii_tokens = ascii_context["routing_budget"]["estimated_input_tokens"]
    dense_tokens = dense_context["routing_budget"]["estimated_input_tokens"]
    assert dense_tokens >= ascii_tokens + 250


def test_dynamic_output_token_budgets_do_not_assume_ascii_density() -> None:
    assert dynamic_output_token_budgets(
        configured_output_tokens=0,
        candidate_max_chars=24_000,
    ) == (24_000, 8_192)
    assert dynamic_output_token_budgets(
        configured_output_tokens=20_000,
        candidate_max_chars=6_000,
    ) == (6_000, 20_000)
    assert dynamic_output_token_budgets(
        configured_output_tokens=4_096,
        candidate_max_chars=24_000,
    ) == (24_000, 4_096)
    assert dynamic_output_token_budgets(
        configured_output_tokens=4_096,
        candidate_max_chars=0,
    ) == (4_096, 4_096)

    config = load_ranking_config()
    config["context"]["output_budget"]["default_tokens"] = 77
    config["context"]["token_estimation"]["candidate_chars_per_token"] = 2
    assert dynamic_output_token_budgets(
        configured_output_tokens=0,
        candidate_max_chars=100,
        ranking_config=config,
    ) == (50, 77)


def test_request_context_sanitizes_supplied_state_and_estimates_tool_tokens() -> None:
    context = build_request_context(
        message="review the workspace",
        turn_metadata={
            "router_dynamic_request_context": {
                "secret_unbounded_field": "do-not-forward",
                "tool_state": {
                    "called_tools": [
                        {"name": f"tool-{index}", "arguments": [index]} for index in range(40)
                    ],
                    "tool_results_summary": "result" * 2_000,
                    "failed_tools": [["nested", index] for index in range(40)],
                },
                "workspace_state": {
                    "referenced_files": [{"path": f"src/file-{index}.py"} for index in range(40)],
                    "changed_files": ["changed.py", "changed.py"],
                    "test_results": "failed" * 1_000,
                },
                "intermediate_outputs": {
                    "previous_candidates": [
                        f"candidate-{index}:" + ("candidate" * 500) for index in range(12)
                    ],
                    "current_errors": [f"error-{index}:" + ("error" * 500) for index in range(12)],
                },
                "last_route": {
                    "selected_P": [f"provider:model-{index}" for index in range(20)],
                    "selected_A": "provider:aggregator",
                    "quality_feedback": 2.0,
                    "escalation_level": 99,
                    "raw_prompt": "must-not-survive",
                },
            }
        },
        attachments=[{"name": {"path": "diagram.png"}, "media_type": "image/png"}],
        candidate_output_tokens=8_192,
        aggregator_output_tokens=8_192,
    )

    assert "secret_unbounded_field" not in context
    assert len(context["tool_state"]["called_tools"]) == 32
    assert len(context["tool_state"]["tool_results_summary"]) == 4_000
    assert len(context["workspace_state"]["referenced_files"]) == 32
    assert context["workspace_state"]["changed_files"] == ["changed.py"]
    assert len(context["intermediate_outputs"]["previous_candidates"]) == 8
    assert len(context["last_route"]["selected_P"]) == 8
    assert context["last_route"]["quality_feedback"] == 1.0
    assert context["last_route"]["escalation_level"] == 2
    assert "raw_prompt" not in context["last_route"]
    assert context["routing_budget"]["tool_log_tokens"] > 0
    assert all(isinstance(value, str) for value in context["workspace_state"]["referenced_files"])


def test_fallback_context_bucket_uses_boundary_token_as_the_larger_bucket() -> None:
    profile = fallback_task_profile(
        routed_tier="c1",
        request_context=_context(input_tokens=8_000),
    )

    assert profile["constraints"]["context"] == "medium"


def test_fallback_profile_and_mock_user_are_loaded_from_ranking_config() -> None:
    config = load_ranking_config()
    config["context"]["bucket_min_tokens"]["medium"] = 100
    config["fallback_task_profile"]["capability_dist"] = {"writing": 1.0}
    config["fallback_task_profile"]["risk_by_tier"]["2"] = "high"
    config["mock_user_profile"]["preference"]["cost_sensitivity"] = "high"

    profile = fallback_task_profile(
        routed_tier="c1",
        request_context=_context(input_tokens=100),
        ranking_config=config,
    )
    user = mock_user_profile(config)

    assert profile["capability_dist"] == {"writing": 1.0}
    assert profile["constraints"]["context"] == "medium"
    assert profile["constraints"]["risk"] == "high"
    assert user["preference"]["cost_sensitivity"] == "high"


def test_runtime_anchor_does_not_inherit_unverified_task_modalities() -> None:
    snapshot = build_model_registry_snapshot(
        inherited_provider="test-provider",
        inherited_model="test-vendor/unknown-model",
        routed_tier="c2",
        anchor_modalities=["text"],
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [],
        },
    )

    assert snapshot["models"][0]["registry_facts"]["modalities"] == ["text"]


def test_unknown_model_synthesis_is_config_driven() -> None:
    config = load_ranking_config()
    config["synthetic_model"]["context_window"] = 77_777
    config["synthetic_model"]["price_input_per_million"] = 1.25
    config["synthetic_model"]["base_strength_by_tier"]["3"] = 0.42

    snapshot = build_model_registry_snapshot(
        inherited_provider="test-provider",
        inherited_model="vendor/unknown-model-v1",
        routed_tier="c2",
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [],
        },
        ranking_config=config,
    )

    anchor = snapshot["models"][0]
    assert anchor["registry_facts"]["context_window"] == 77_777
    assert anchor["registry_facts"]["price"]["input_per_million"] == 1.25
    assert anchor["static_profile"]["capability_dist_prior"]["reasoning"] == 0.42


def test_vendor_qualified_model_does_not_reuse_another_vendor_template() -> None:
    google_template = _model(
        "google/shared-model",
        provider="openrouter",
        vendor="google",
        family="google-shared",
        capability=0.99,
    )
    snapshot = build_model_registry_snapshot(
        inherited_provider="openrouter",
        inherited_model="acme/shared-model",
        routed_tier="c2",
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [google_template],
        },
    )

    anchor = snapshot["models"][0]
    assert anchor["registry_facts"]["model_id"] == "acme/shared-model"
    assert anchor["registry_facts"]["vendor"] == "acme"
    assert anchor["registry_facts"]["family"] == "shared-model"


def test_ambiguous_bare_model_name_uses_synthesized_profile() -> None:
    snapshot = build_model_registry_snapshot(
        inherited_provider="openrouter",
        inherited_model="shared-model",
        routed_tier="c2",
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [
                _model("google/shared-model", capability=0.99),
                _model("acme/shared-model", capability=0.10),
            ],
        },
    )

    anchor = snapshot["models"][0]
    assert anchor["registry_facts"]["model_id"] == "shared-model"
    assert anchor["static_profile"]["capability_dist_prior"]["reasoning"] == 0.74


def test_packaged_registry_template_index_preserves_exact_and_basename_semantics() -> None:
    first_exact = _model("vendor/shared", provider="provider-a", capability=0.91)
    second_exact = _model("vendor/shared", provider="provider-b", capability=0.11)
    first_basename = _model("other/duplicate", provider="provider-c")
    second_basename = _model("another/duplicate", provider="provider-d")
    unique_basename = _model("vendor/unique", provider="provider-e", capability=0.67)
    index = ranking_router._compile_packaged_registry_template_index(
        {
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [
                first_exact,
                second_exact,
                first_basename,
                second_basename,
                unique_basename,
            ],
        }
    )

    exact = ranking_router._template_for_packaged_model(index, "VENDOR/SHARED")
    unique = ranking_router._template_for_packaged_model(index, "unique")

    assert exact is not None
    assert exact["registry_facts"]["provider"] == "provider-a"
    assert unique is not None
    assert unique["static_profile"]["capability_dist_prior"]["reasoning"] == 0.67
    assert ranking_router._template_for_packaged_model(index, "duplicate") is None

    exact["static_profile"]["capability_dist_prior"]["reasoning"] = 0.0
    reread = ranking_router._template_for_packaged_model(index, "vendor/shared")
    assert reread is not None
    assert reread["static_profile"]["capability_dist_prior"]["reasoning"] == 0.91


def test_packaged_registry_template_cache_reports_one_atomic_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ranking_router,
        "_PACKAGED_REGISTRY_TEMPLATE_INDEX",
        None,
    )
    original_compile = ranking_router._compile_packaged_registry_template_index
    compile_count = 0
    compile_count_lock = threading.Lock()

    def counted_compile(snapshot: Any) -> Any:
        nonlocal compile_count
        with compile_count_lock:
            compile_count += 1
        return original_compile(snapshot)

    monkeypatch.setattr(
        ranking_router,
        "_compile_packaged_registry_template_index",
        counted_compile,
    )
    worker_count = 8
    barrier = threading.Barrier(worker_count)

    def lookup() -> tuple[Any, bool]:
        barrier.wait(timeout=5)
        return ranking_router._packaged_registry_template_index_lookup()

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(lambda _: lookup(), range(worker_count)))

    assert compile_count == 1
    assert sum(cache_hit is False for _, cache_hit in results) == 1
    assert sum(cache_hit is True for _, cache_hit in results) == worker_count - 1
    assert len({id(index) for index, _ in results}) == 1

    inherited_lock = ranking_router._PACKAGED_REGISTRY_TEMPLATE_INDEX_LOCK
    inherited_lock.acquire()
    try:
        ranking_router._PACKAGED_REGISTRY_TEMPLATE_INDEX = None
        ranking_router._reset_packaged_registry_template_index_lock_after_fork()
        reset_index, reset_hit = ranking_router._packaged_registry_template_index_lookup()
    finally:
        inherited_lock.release()
    assert reset_hit is False
    assert reset_index is not results[0][0]
    assert compile_count == 2


def test_snapshot_build_reports_request_level_cache_evidence_without_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ranking_router,
        "_PACKAGED_REGISTRY_TEMPLATE_INDEX",
        None,
    )
    kwargs = {
        "inherited_provider": "openrouter",
        "inherited_model": "openai/gpt-5.6-sol",
        "routed_tier": "c2",
    }
    first_observability: dict[str, Any] = {}
    second_observability: dict[str, Any] = {}

    first = build_model_registry_snapshot(
        **kwargs,
        _observability_out=first_observability,
    )
    second = build_model_registry_snapshot(
        **kwargs,
        _observability_out=second_observability,
    )

    assert canonical_json_sha256(first) == canonical_json_sha256(second)
    assert first_observability == {"packaged_template_cache_hit": False}
    assert second_observability == {"packaged_template_cache_hit": True}

    explicit_observability = {"packaged_template_cache_hit": True}
    explicit = build_model_registry_snapshot(
        **kwargs,
        packaged_snapshot=load_model_registry_snapshot(),
        _observability_out=explicit_observability,
    )
    assert canonical_json_sha256(explicit) == canonical_json_sha256(first)
    assert explicit_observability == {}


def test_default_packaged_template_index_matches_linear_snapshot_build() -> None:
    kwargs = {
        "inherited_provider": "openrouter",
        "inherited_model": "openai/gpt-5.6-sol",
        "routed_tier": "c2",
        "operator_candidates": [
            {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-5",
                "role": "aggregator",
                "source": "test-operator",
            }
        ],
        "legacy_model_options": ["google/gemini-3.1-pro-preview"],
        "router_tiers": {
            "c3": {
                "provider": "openrouter",
                "model": "x-ai/grok-4.5",
                "thinking_level": "high",
            }
        },
    }
    expected = build_model_registry_snapshot(
        **kwargs,
        packaged_snapshot=load_model_registry_snapshot(),
    )
    actual = build_model_registry_snapshot(**kwargs)

    assert canonical_json_sha256(actual) == canonical_json_sha256(expected)
    actual["models"][0]["static_profile"]["capability_dist_prior"]["reasoning"] = 0.0
    assert canonical_json_sha256(build_model_registry_snapshot(**kwargs)) == (
        canonical_json_sha256(expected)
    )


def test_explicit_legacy_registry_snapshot_skips_packaged_template_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical = load_model_registry_snapshot(base_version="curated-openrouter-step2-2026-07-31.1")
    legacy = ranking_router._legacy_registry_snapshot_projection(historical)
    monkeypatch.setattr(
        ranking_router,
        "_packaged_registry_template_index_lookup",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected packaged fast path")),
    )

    replay = build_model_registry_snapshot(
        inherited_provider="openrouter",
        inherited_model="openai/gpt-5.6-sol",
        routed_tier="c2",
        operator_candidates=[
            {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-5",
                "role": "aggregator",
                "source": "legacy-golden",
            }
        ],
        legacy_model_options=["google/gemini-3.1-pro-preview"],
        router_tiers={
            "c3": {
                "provider": "openrouter",
                "model": "x-ai/grok-4.5",
                "thinking_level": "high",
            }
        },
        packaged_snapshot=legacy,
    )

    assert replay["schema_version"] == "step2-model-registry-v1"
    assert canonical_json_sha256(replay) == (
        "f7ec984706f8c6d7f5c691630d41c5735e7d5b0ebd8e79e7cf175b24fda6093a"
    )


def test_ranker_records_monotonic_stage_timings_outside_decision_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = (
        _model("alpha", capability=0.95, aggregator_fit=0.82),
        _model("beta", capability=0.90, aggregator_fit=0.97),
        _model("gamma", capability=0.85, aggregator_fit=0.88),
    )
    expected = _decision(*models)
    monotonic_values = iter(
        [
            0,
            5_000_000,
            10_000_000,
            17_000_000,
            20_000_000,
            23_000_000,
        ]
    )
    monkeypatch.setattr(
        ranking_router.time,
        "monotonic_ns",
        lambda: next(monotonic_values),
    )
    stage_observability: dict[str, Any] = {}

    actual = _decision(
        *models,
        stage_observability_out=stage_observability,
    )

    assert stage_observability == {
        "hard_filter_ms": 8,
        "score_ms": 7,
    }
    assert actual == expected
    with pytest.raises(StopIteration):
        next(monotonic_values)

    monkeypatch.setattr(
        ranking_router.time,
        "monotonic_ns",
        lambda: (_ for _ in ()).throw(AssertionError("unrequested ranking timing")),
    )
    assert _decision(*models) == expected


def test_operator_candidates_only_use_explicit_aggregator_role_for_aggregation() -> None:
    snapshot = build_model_registry_snapshot(
        inherited_provider="anchor-provider",
        inherited_model="anchor-model",
        routed_tier="c2",
        operator_candidates=[
            {"provider": "provider-a", "model": "model-a", "role": ""},
            {"provider": "provider-b", "model": "model-b", "role": "critic"},
            {
                "provider": "provider-c",
                "model": "model-c",
                "role": "aggregator",
            },
        ],
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [],
        },
    )
    by_model = {
        row["registry_facts"]["model_id"]: row["registry_facts"]["roles"]
        for row in snapshot["models"]
    }

    assert by_model["model-a"] == ["proposer"]
    assert by_model["model-b"] == ["proposer"]
    assert by_model["model-c"] == ["aggregator"]


def test_operator_role_overrides_duplicate_routed_anchor_role() -> None:
    snapshot = build_model_registry_snapshot(
        inherited_provider="anchor-provider",
        inherited_model="anchor-model",
        routed_tier="c2",
        operator_candidates=[
            {
                "provider": "ANCHOR-PROVIDER",
                "model": "ANCHOR-MODEL",
                "role": "aggregator",
            }
        ],
        packaged_snapshot={
            "schema_version": "test",
            "snapshot_version": "test-v1",
            "models": [],
        },
    )

    assert len(snapshot["models"]) == 1
    assert snapshot["models"][0]["source"] == "router_anchor"
    assert snapshot["models"][0]["registry_facts"]["roles"] == ["aggregator"]


def test_registry_builder_rejects_malformed_or_duplicate_profile_rows() -> None:
    malformed = {
        "schema_version": "test",
        "snapshot_version": "test-v1",
        "models": ["not-a-model"],
    }
    duplicate = {
        "schema_version": "test",
        "snapshot_version": "test-v1",
        "models": [_model("Vendor/Model"), _model("vendor/model")],
    }

    for snapshot, message in (
        (malformed, "row 0 must be an object"),
        (duplicate, "duplicate model identities"),
    ):
        with pytest.raises(DynamicRankingError, match=message):
            build_model_registry_snapshot(
                inherited_provider="test-provider",
                inherited_model="anchor",
                routed_tier="c1",
                packaged_snapshot=snapshot,
            )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("price", -1.0, "negative price"),
        ("latency", -1, "invalid latency bounds"),
        ("strength", 1.1, "out-of-range capability_dist_prior.reasoning"),
    ],
)
def test_ranking_rejects_malformed_numeric_model_profiles(
    field: str,
    value: float,
    message: str,
) -> None:
    model = _model("malformed")
    if field == "price":
        model["registry_facts"]["price"]["output_per_million"] = value
    elif field == "latency":
        model["registry_facts"]["latency_p95_ms"] = value
    else:
        model["static_profile"]["capability_dist_prior"]["reasoning"] = value

    with pytest.raises(DynamicRankingError, match=message):
        _decision(model, analysis=_analysis(tier=1))


@pytest.mark.parametrize(
    "field",
    [
        "is_open_source",
        "is_chinese_model",
        "supports_reasoning",
        "supports_tools",
    ],
)
def test_ranking_rejects_non_boolean_model_boolean_fact(field: str) -> None:
    model = _model("malformed")
    model["registry_facts"][field] = "false"

    with pytest.raises(DynamicRankingError, match=f"invalid {field}"):
        _decision(model, analysis=_analysis(tier=1))


@pytest.mark.parametrize(
    ("supports_reasoning", "levels", "message"),
    [
        (True, ["high", "turbo"], "invalid supported_thinking_levels"),
        (True, ["high", "high"], "duplicate supported_thinking_levels"),
        (True, ["off"], "no enabled supported_thinking_levels"),
        (False, ["high", "off"], "without reasoning support"),
    ],
)
def test_ranking_rejects_invalid_supported_thinking_levels(
    supports_reasoning: bool,
    levels: list[str],
    message: str,
) -> None:
    model = _model("malformed")
    model["registry_facts"]["supports_reasoning"] = supports_reasoning
    model["registry_facts"]["supported_thinking_levels"] = levels

    with pytest.raises(DynamicRankingError, match=message):
        _decision(model, analysis=_analysis(tier=1))


class _AnalyzerProvider:
    provider_name = "analyzer-test"

    def __init__(
        self,
        response: str | list[str],
        *,
        include_done: bool | list[bool] = True,
    ) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.include_done = include_done if isinstance(include_done, list) else [include_done]
        self.calls: list[tuple[list[Message], ChatConfig | None]] = []

    async def _stream(
        self,
        response: str,
        *,
        response_id: str,
        include_done: bool,
    ) -> AsyncIterator[Any]:
        yield TextDeltaEvent(text=response)
        if include_done:
            yield DoneEvent(
                model="analyzer-test",
                input_tokens=11,
                output_tokens=7,
                billed_cost=0.012,
                cost_source="provider_billed",
                provider_usage={
                    "is_byok": False,
                    "provider_reported_cost": 0.012,
                    "response_ids": [response_id],
                    "router_metadata": {"is_byok": False},
                },
            )

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append((messages, config))
        attempt = len(self.calls)
        response = self.responses[min(attempt - 1, len(self.responses) - 1)]
        include_done = self.include_done[min(attempt - 1, len(self.include_done) - 1)]
        return self._stream(
            response,
            response_id=f"analyzer-{attempt}",
            include_done=include_done,
        )

    async def list_models(self) -> list[Any]:
        return []


class _AnalyzerTerminalProvider:
    provider_name = "analyzer-test"
    model = "analyzer-test"
    accounts_physical_usage = True

    def __init__(self, terminals: list[DoneEvent | ErrorEvent]) -> None:
        self.terminals = terminals
        self.calls: list[tuple[list[Message], ChatConfig | None]] = []

    async def _stream(
        self,
        terminal: DoneEvent | ErrorEvent,
    ) -> AsyncIterator[Any]:
        if isinstance(terminal, DoneEvent):
            yield TextDeltaEvent(text=json.dumps(_task_profile(tier=2)))
        yield terminal

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append((messages, config))
        index = min(len(self.calls) - 1, len(self.terminals) - 1)
        return self._stream(self.terminals[index])

    async def list_models(self) -> list[Any]:
        return []


_TASK_ANALYZER_CHAIN_ROUTES = (
    ("openrouter", "deepseek/deepseek-v4-pro", "together"),
    ("openrouter", "openai/gpt-5.6-sol", "azure"),
    (
        "openrouter",
        "google/gemini-3.1-pro-preview",
        "google-ai-studio",
    ),
)


def _task_analyzer_chain_candidates(
    providers: list[Any | None],
) -> list[dict[str, Any]]:
    return [
        {
            "provider": provider,
            "provider_id": provider_id,
            "model_id": model_id,
            "upstream_provider": upstream_provider,
        }
        for provider, (provider_id, model_id, upstream_provider) in zip(
            providers,
            _TASK_ANALYZER_CHAIN_ROUTES,
            strict=True,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("success_index", [0, 1, 2])
async def test_task_analyzer_fallback_chain_selects_first_valid_candidate(
    success_index: int,
) -> None:
    providers = [
        _AnalyzerProvider(
            json.dumps(_task_profile(tier=2)) if index == success_index else "not-json"
        )
        for index in range(3)
    ]

    result = await analyze_task_with_fallback_chain(
        candidates=_task_analyzer_chain_candidates(providers),
        message="implement a parser",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        decision_id="1" * 32,
    )

    assert result.source == "llm_provider"
    assert result.schema_valid is True
    assert result.provider_id == _TASK_ANALYZER_CHAIN_ROUTES[success_index][0]
    assert result.model_id == _TASK_ANALYZER_CHAIN_ROUTES[success_index][1]
    expected_call_counts = [1 if index <= success_index else 0 for index in range(3)]
    assert [len(provider.calls) for provider in providers] == expected_call_counts
    assert all(
        config is not None and config.allow_provider_stream_fallback is False
        for provider in providers
        for _, config in provider.calls
    )
    attempted_configs = [
        config
        for provider in providers[: success_index + 1]
        for _, config in provider.calls
    ]
    assert attempted_configs[0] is not None
    assert attempted_configs[0].model_capabilities is not None
    assert (
        attempted_configs[0].model_capabilities.reasoning_format
        == "openrouter_explicit_off"
    )
    assert all(
        config is not None and config.model_capabilities is None
        for config in attempted_configs[1:]
    )
    expected_models = [route[1] for route in _TASK_ANALYZER_CHAIN_ROUTES[: success_index + 1]]
    assert result.usage["attempt_count"] == len(expected_models)
    attempts = result.usage["physical_attempts"]
    assert [attempt["attempt"] for attempt in attempts] == list(range(1, len(attempts) + 1))
    assert [attempt["requested_model"] for attempt in attempts] == expected_models
    assert len({attempt["physical_attempt_id"] for attempt in attempts}) == len(attempts)
    assert result.usage["input_tokens"] == 11 * len(attempts)
    assert result.usage["billed_cost"] == pytest.approx(0.012 * len(attempts))
    chain = result.trace()["chain"]
    assert chain["protocol"] == TASK_ANALYZER_FALLBACK_CHAIN_PROTOCOL
    assert chain["configured_routes"] == [
        {
            "provider": provider_id,
            "model": model_id,
            "upstream_provider": upstream_provider,
        }
        for provider_id, model_id, upstream_provider in _TASK_ANALYZER_CHAIN_ROUTES
    ]
    assert chain["selected_index"] == success_index
    assert chain["exhausted"] is False
    assert chain["schema_repair_max_retries"] == 0
    assert [attempt["outcome"] for attempt in chain["attempt_outcomes"]] == [
        *(["failed"] * success_index),
        "success",
    ]


@pytest.mark.asyncio
async def test_task_analyzer_fallback_chain_can_disable_schema_repair() -> None:
    first = _AnalyzerProvider("not-json")
    second = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    third = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    result = await analyze_task_with_fallback_chain(
        candidates=_task_analyzer_chain_candidates([first, second, third]),
        message="implement a parser",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        schema_repair_max_retries=0,
        decision_id="3" * 32,
    )

    assert [len(provider.calls) for provider in (first, second, third)] == [1, 1, 0]
    assert result.model_id == _TASK_ANALYZER_CHAIN_ROUTES[1][1]
    chain = result.trace()["chain"]
    assert chain["schema_repair_max_retries"] == 0
    assert [outcome["outcome"] for outcome in chain["attempt_outcomes"]] == [
        "failed",
        "success",
    ]


@pytest.mark.asyncio
async def test_historical_explicit_analyzer_chain_keeps_per_route_total_budget() -> None:
    providers = [_AnalyzerProvider(json.dumps(_task_profile(tier=2))) for _ in range(3)]
    historical_config = ranking_config_snapshot(base_version="step2-ranking-2026-08-10.1")

    result = await analyze_task_with_fallback_chain(
        candidates=_task_analyzer_chain_candidates(providers),
        message="implement a parser",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        ranking_config=historical_config,
    )

    chain = result.trace()["chain"]
    assert chain["deadline"]["configured_seconds"] == 60.0
    assert chain["schema_repair_max_retries"] == 0
    assert [len(provider.calls) for provider in providers] == [1, 0, 0]


@pytest.mark.asyncio
async def test_task_analyzer_fallback_chain_advances_past_unavailable_provider() -> None:
    second = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    third = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    result = await analyze_task_with_fallback_chain(
        candidates=_task_analyzer_chain_candidates([None, second, third]),
        message="implement a parser",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
    )

    assert result.model_id == _TASK_ANALYZER_CHAIN_ROUTES[1][1]
    assert len(second.calls) == 1
    assert third.calls == []
    assert result.usage["attempt_count"] == 1
    chain_attempts = result.trace()["chain"]["attempt_outcomes"]
    assert chain_attempts[0]["reason"] == "provider_unavailable"
    assert chain_attempts[0]["physical_request_count"] == 0
    assert chain_attempts[1]["outcome"] == "success"


@pytest.mark.asyncio
async def test_task_analyzer_fallback_chain_all_unavailable_has_zero_usage() -> None:
    result = await analyze_task_with_fallback_chain(
        candidates=_task_analyzer_chain_candidates([None, None, None]),
        message="implement a parser",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
    )

    assert result.source == "router_fallback"
    assert result.schema_valid is False
    assert result.usage == {"physical_attempts": [], "attempt_count": 0}
    chain = result.trace()["chain"]
    assert chain["selected_index"] is None
    assert chain["exhausted"] is True
    assert [outcome["physical_request_count"] for outcome in chain["attempt_outcomes"]] == [
        0,
        0,
        0,
    ]


@pytest.mark.asyncio
async def test_task_analyzer_fallback_chain_advances_after_closed_timeout() -> None:
    class _TimeoutStream:
        def __init__(self) -> None:
            self.closed = False

        def __aiter__(self) -> _TimeoutStream:
            return self

        async def __anext__(self) -> Any:
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.closed = True

    class _TimeoutProvider:
        accounts_physical_usage = True

        def __init__(self) -> None:
            self.calls = 0
            self.stream = _TimeoutStream()

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            self.calls += 1
            return self.stream

    first = _TimeoutProvider()
    second = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    third = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    result = await analyze_task_with_fallback_chain(
        candidates=_task_analyzer_chain_candidates([first, second, third]),
        message="classify this task",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        timeout_seconds=0.01,
    )

    assert first.calls == 1
    assert first.stream.closed is True
    assert len(second.calls) == 1
    assert third.calls == []
    assert result.model_id == _TASK_ANALYZER_CHAIN_ROUTES[1][1]
    chain_attempts = result.trace()["chain"]["attempt_outcomes"]
    assert chain_attempts[0]["reason"] == "transient"
    assert chain_attempts[1]["outcome"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "public_reason"),
    [
        ("unauthorized", "auth"),
        ("unsupported", "unsupported"),
        ("401", "auth"),
        ("403", "auth"),
        ("404", "unsupported"),
        ("429", "transient"),
        ("503", "transient"),
    ],
)
async def test_task_analyzer_fallback_chain_classifies_provider_errors(
    error_code: str,
    public_reason: str,
) -> None:
    first = _AnalyzerTerminalProvider(
        [
            ErrorEvent(
                message=error_code,
                code=error_code,
                request_started=True,
                physical_request_count=1,
            )
        ]
    )
    second = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    third = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    result = await analyze_task_with_fallback_chain(
        candidates=_task_analyzer_chain_candidates([first, second, third]),
        message="classify this task",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
    )

    assert len(first.calls) == 1
    assert len(second.calls) == 1
    assert third.calls == []
    assert result.model_id == _TASK_ANALYZER_CHAIN_ROUTES[1][1]
    assert result.usage["attempt_count"] == 2
    assert result.trace()["chain"]["attempt_outcomes"][0]["reason"] == public_reason
    assert [
        outcome["physical_request_count"] for outcome in result.trace()["chain"]["attempt_outcomes"]
    ] == [1, 1]


@pytest.mark.asyncio
async def test_task_analyzer_fallback_chain_uses_one_total_deadline() -> None:
    class _SlowProvider:
        accounts_physical_usage = True

        def __init__(self) -> None:
            self.timeouts: list[float] = []

        async def _stream(self) -> AsyncIterator[Any]:
            await asyncio.Event().wait()
            yield  # pragma: no cover

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            assert config is not None and config.timeout is not None
            self.timeouts.append(float(config.timeout))
            return self._stream()

    providers = [_SlowProvider() for _ in range(3)]
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    result = await analyze_task_with_fallback_chain(
        candidates=_task_analyzer_chain_candidates(providers),
        message="classify this task",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        timeout_seconds=0.03,
    )
    elapsed = loop.time() - started_at

    observed_timeouts = [timeout for provider in providers for timeout in provider.timeouts]
    assert elapsed < 0.15
    assert observed_timeouts
    assert observed_timeouts == sorted(observed_timeouts, reverse=True)
    assert sum(observed_timeouts) <= 0.031
    assert result.trace()["chain"]["deadline"]["expired"] is True
    assert result.usage["attempt_count"] == len(observed_timeouts)


@pytest.mark.asyncio
async def test_task_analyzer_rechecks_deadline_immediately_before_provider_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    original_serialize = ranking_router._serialize_task_analyzer_input

    def delayed_serialize(*args: Any, **kwargs: Any):
        payload = original_serialize(*args, **kwargs)
        time.sleep(0.02)
        return payload

    monkeypatch.setattr(
        ranking_router,
        "_serialize_task_analyzer_input",
        delayed_serialize,
    )
    result = await analyze_task_with_fallback_chain(
        candidates=[
            {
                "provider": provider,
                "provider_id": _TASK_ANALYZER_CHAIN_ROUTES[0][0],
                "model_id": _TASK_ANALYZER_CHAIN_ROUTES[0][1],
                "upstream_provider": _TASK_ANALYZER_CHAIN_ROUTES[0][2],
            }
        ],
        message="classify this task",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        timeout_seconds=0.01,
    )

    assert provider.calls == []
    assert result.usage["attempt_count"] == 0
    outcome = result.trace()["chain"]["attempt_outcomes"][0]
    assert outcome["reason"] == "transient"
    assert outcome["physical_request_count"] == 0


@pytest.mark.asyncio
async def test_task_analyzer_schema_repair_reuses_chain_deadline() -> None:
    class _SchemaThenSlowProvider:
        accounts_physical_usage = True

        def __init__(self) -> None:
            self.calls = 0
            self.timeouts: list[float] = []

        async def _stream(self, attempt: int) -> AsyncIterator[Any]:
            if attempt == 1:
                await asyncio.sleep(0.01)
                yield TextDeltaEvent(text="not-json")
                yield DoneEvent(
                    model="analyzer-test",
                    input_tokens=11,
                    output_tokens=7,
                    billed_cost=0.012,
                    cost_source="provider_billed",
                )
                return
            await asyncio.Event().wait()
            yield  # pragma: no cover

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            assert config is not None and config.timeout is not None
            self.calls += 1
            self.timeouts.append(float(config.timeout))
            return self._stream(self.calls)

    primary = _SchemaThenSlowProvider()
    second = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    third = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    result = await asyncio.wait_for(
        analyze_task_with_fallback_chain(
            candidates=_task_analyzer_chain_candidates([primary, second, third]),
            message="classify this task",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c2",
            routing_confidence=0.77,
            timeout_seconds=0.05,
            schema_repair_max_retries=1,
        ),
        timeout=0.2,
    )

    assert primary.calls == 2
    assert len(primary.timeouts) == 2
    assert 0.0 < primary.timeouts[1] < primary.timeouts[0] <= 0.05
    assert result.trace()["chain"]["deadline"]["configured_seconds"] == 0.05
    assert result.usage["attempt_count"] >= 2


@pytest.mark.asyncio
async def test_task_analyzer_fallback_chain_returns_fallback_after_exhaustion() -> None:
    providers = [_AnalyzerProvider("not-json") for _ in range(3)]

    result = await analyze_task_with_fallback_chain(
        candidates=_task_analyzer_chain_candidates(providers),
        message="classify this task",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        decision_id="2" * 32,
    )

    assert result.source == "router_fallback"
    assert result.schema_valid is False
    assert result.profile["tier_dist"] == {"3": 1.0}
    assert result.provider_id == _TASK_ANALYZER_CHAIN_ROUTES[0][0]
    assert result.model_id == _TASK_ANALYZER_CHAIN_ROUTES[-1][1]
    assert [len(provider.calls) for provider in providers] == [1, 1, 1]
    assert result.usage["attempt_count"] == 3
    assert [attempt["requested_model"] for attempt in result.usage["physical_attempts"]] == [
        route[1] for route in _TASK_ANALYZER_CHAIN_ROUTES
    ]
    chain = result.trace()["chain"]
    assert chain["selected_index"] is None
    assert chain["exhausted"] is True
    assert [attempt["outcome"] for attempt in chain["attempt_outcomes"]] == [
        "failed",
        "failed",
        "failed",
    ]
    assert [attempt["reason"] for attempt in chain["attempt_outcomes"]] == [
        "invalid_json",
        "invalid_json",
        "invalid_json",
    ]


@pytest.mark.asyncio
async def test_task_analyzer_fallback_chain_cleanup_failure_does_not_advance() -> None:
    class _HangingStream:
        def __aiter__(self) -> _HangingStream:
            return self

        async def __anext__(self) -> Any:
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            await asyncio.Event().wait()

    class _HangingProvider:
        accounts_physical_usage = True

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            self.calls += 1
            return _HangingStream()

    first = _HangingProvider()
    second = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    third = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    with pytest.raises(TaskAnalyzerStreamCleanupError):
        await asyncio.wait_for(
            analyze_task_with_fallback_chain(
                candidates=_task_analyzer_chain_candidates([first, second, third]),
                message="classify this task",
                user_profile_enabled=False,
                request_context=_context(),
                routed_tier="c2",
                routing_confidence=0.77,
                timeout_seconds=0.01,
            ),
            timeout=0.2,
        )

    assert first.calls == 1
    assert second.calls == []
    assert third.calls == []


@pytest.mark.asyncio
async def test_task_analyzer_fallback_chain_physical_evidence_failure_does_not_advance() -> None:
    first = _AnalyzerTerminalProvider(
        [
            ErrorEvent(
                message="conflicting request identities",
                code="response_invalid",
                model_usage_breakdown=[
                    {
                        "provider": "openrouter",
                        "model": "analyzer-test",
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "billed_cost": 0.01,
                        "cost_source": "provider_billed",
                        "physical_attempt_id": "c" * 32,
                        "provider_usage": {
                            "response_ids": ["paid-response"],
                            "physical_attempt_id": "d" * 32,
                        },
                    }
                ],
                diagnostic_done=DoneEvent(
                    provider="openrouter",
                    model="analyzer-test",
                    input_tokens=3,
                    output_tokens=1,
                    billed_cost=0.01,
                    cost_source="provider_billed",
                    provider_usage={
                        "response_ids": ["paid-response"],
                        "physical_attempt_id": "d" * 32,
                    },
                ),
                request_started=True,
                physical_request_count=1,
            )
        ]
    )
    second = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    third = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    with pytest.raises(TaskAnalyzerPhysicalEvidenceError):
        await analyze_task_with_fallback_chain(
            candidates=_task_analyzer_chain_candidates([first, second, third]),
            message="classify this task",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c2",
            routing_confidence=0.77,
        )

    assert len(first.calls) == 1
    assert second.calls == []
    assert third.calls == []


@pytest.mark.asyncio
async def test_task_analyzer_hanging_stream_close_is_bounded() -> None:
    class _HangingStream:
        def __init__(self) -> None:
            self.close_started = False

        def __aiter__(self) -> _HangingStream:
            return self

        async def __anext__(self) -> Any:
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            self.close_started = True
            await asyncio.Event().wait()

    class _HangingProvider:
        provider_name = "hanging-analyzer"
        model = "hanging-model"
        accounts_physical_usage = True

        def __init__(self) -> None:
            self.stream = _HangingStream()

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            return self.stream

    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 0
    config["task_analyzer"]["stream_close_timeout_seconds"] = 0.01
    provider = _HangingProvider()

    with pytest.raises(TaskAnalyzerStreamCleanupError):
        await asyncio.wait_for(
            analyze_task_with_provider(
                provider=provider,
                message="classify this",
                user_profile_enabled=False,
                request_context=_context(),
                routed_tier="c1",
                routing_confidence=0.8,
                timeout_seconds=0.01,
                ranking_config=config,
            ),
            timeout=0.2,
        )

    assert provider.stream.close_started is True


@pytest.mark.asyncio
async def test_task_analyzer_missing_aclose_requires_a_terminal_stream() -> None:
    stream = object()

    assert (
        await ranking_router._bounded_close_task_analyzer_stream(
            stream,
            timeout_seconds=0.01,
            require_aclose=False,
        )
        is True
    )
    assert (
        await ranking_router._bounded_close_task_analyzer_stream(
            stream,
            timeout_seconds=0.01,
            require_aclose=True,
        )
        is False
    )


@pytest.mark.asyncio
async def test_task_analyzer_uses_provider_interface_and_validates_json() -> None:
    expected = _task_profile(tier=2)
    provider = _AnalyzerProvider(f"```json\n{json.dumps(expected)}\n```")

    class _UsageTracker:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def add(self, session_key: str, **kwargs: Any) -> None:
            self.calls.append((session_key, kwargs))

    usage_tracker = _UsageTracker()

    result = await analyze_task_with_provider(
        provider=provider,
        message="implement a parser",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        usage_tracker=usage_tracker,
        session_key="agent:main:test",
        analyzer_provider_id=TASK_ANALYZER_PROVIDER_ID,
        analyzer_model_id=TASK_ANALYZER_MODEL_ID,
    )

    assert result.source == "llm_provider"
    assert result.schema_valid is True
    assert result.profile["tier_dist"] == {"2": 1.0}
    assert len(provider.calls) == 1
    assert provider.calls[0][1] is not None
    assert provider.calls[0][1].temperature == 0.0
    assert provider.calls[0][1].thinking is False
    assert provider.calls[0][1].model_capabilities is not None
    assert (
        provider.calls[0][1].model_capabilities.reasoning_format
        == "openrouter_explicit_off"
    )
    assert provider.calls[0][1].allow_provider_stream_fallback is True
    assert '"modality":["<allowed modality>"]' in provider.calls[0][1].system
    assert '"session_intent":{"type":"<allowed intent>"' in provider.calls[0][1].system
    assert "research is a domain, not a capability" in provider.calls[0][1].system
    assert provider.calls[0][1].output_json_schema_strict is True
    output_schema = provider.calls[0][1].output_json_schema
    assert output_schema is not None
    assert output_schema["properties"]["domain_dist"]["additionalProperties"] is False
    assert output_schema["properties"]["domain_dist"]["required"] == list(DOMAINS)
    assert result.usage["input_tokens"] == 11
    assert result.usage["billed_cost"] == pytest.approx(0.012)
    assert result.usage["attempt_count"] == 1
    assert result.provider_id == TASK_ANALYZER_PROVIDER_ID
    assert result.model_id == TASK_ANALYZER_MODEL_ID
    assert result.trace()["provider"] == TASK_ANALYZER_PROVIDER_ID
    assert result.trace()["model"] == TASK_ANALYZER_MODEL_ID
    assert usage_tracker.calls[0][0] == "agent:main:test"
    assert usage_tracker.calls[0][1]["output_tokens"] == 7
    analyzer_payload = json.loads(str(provider.calls[0][0][0].content))
    assert analyzer_payload["allowed_constraints"]["risk"] == ["low", "medium", "high"]
    assert analyzer_payload["allowed_session_intents"] == ["new_task", "continue", "redo"]
    # The profile never reaches the analyzer provider, even when one is supplied.
    assert "user_profile" not in analyzer_payload


@pytest.mark.asyncio
async def test_task_analyzer_admission_timeout_is_zero_request_fallback() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=1,
            provider_default_max_in_flight=1,
            deployment_default_max_in_flight=1,
            queue_timeout_seconds=0.01,
        )
    )
    holder = await controller.acquire(
        provider=TASK_ANALYZER_PROVIDER_ID,
        model=TASK_ANALYZER_MODEL_ID,
        role="analyzer",
    )
    try:
        result = await analyze_task_with_provider(
            provider=provider,
            message="implement a parser",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c1",
            routing_confidence=0.8,
            analyzer_provider_id=TASK_ANALYZER_PROVIDER_ID,
            analyzer_model_id=TASK_ANALYZER_MODEL_ID,
            admission_controller=controller,
            admission_deadline=time.monotonic() + 1,
        )
    finally:
        holder.release()

    assert result.source == "router_fallback"
    assert result.schema_valid is False
    assert result.fallback_reason == "ProviderAdmissionTimeoutError"
    assert result.usage["attempt_count"] == 0
    assert result.usage["physical_attempts"] == []
    assert provider.calls == []
    assert controller.active_leases == 0


@pytest.mark.asyncio
async def test_task_analyzer_late_admission_grant_never_starts_request() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    class _LateLease:
        wait_ms = 2
        weight = 1

        def __init__(self) -> None:
            self.released = False

        def release(self) -> None:
            self.released = True

    class _LateController:
        def __init__(self) -> None:
            self.lease = _LateLease()

        async def acquire(self, **_: object) -> _LateLease:
            await asyncio.sleep(0.002)
            return self.lease

    controller = _LateController()
    result = await analyze_task_with_provider(
        provider=provider,
        message="implement a parser",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        analyzer_provider_id=TASK_ANALYZER_PROVIDER_ID,
        analyzer_model_id=TASK_ANALYZER_MODEL_ID,
        admission_controller=controller,
        admission_deadline=time.monotonic() + 0.001,
    )

    assert result.source == "router_fallback"
    assert result.fallback_reason == "ProviderAdmissionTimeoutError"
    assert result.usage["attempt_count"] == 0
    assert provider.calls == []
    assert controller.lease.released is True


@pytest.mark.asyncio
async def test_task_analyzer_absolute_deadline_covers_queue_stream_and_chain() -> None:
    class _SlowProvider:
        accounts_physical_usage = True

        def __init__(self) -> None:
            self.calls = 0
            self.configs: list[ChatConfig] = []
            self.closed = asyncio.Event()

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools
            self.calls += 1
            assert config is not None
            self.configs.append(config)

            async def _stream() -> AsyncIterator[Any]:
                try:
                    await asyncio.sleep(10)
                    yield DoneEvent(model="unreachable")
                finally:
                    self.closed.set()

            return _stream()

    first = _SlowProvider()
    second = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    third = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=1,
            provider_default_max_in_flight=1,
            deployment_default_max_in_flight=1,
            queue_timeout_seconds=1,
        )
    )
    holder = await controller.acquire(
        provider="holder",
        model="holder",
        role="another_turn",
    )

    async def delayed_release() -> None:
        await asyncio.sleep(0.015)
        holder.release()

    release_task = asyncio.create_task(delayed_release())
    started = time.monotonic()
    absolute_deadline = started + 0.05
    result = await analyze_task_with_fallback_chain(
        candidates=_task_analyzer_chain_candidates([first, second, third]),
        message="classify this task",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        timeout_seconds=1,
        admission_controller=controller,
        admission_deadline=absolute_deadline,
    )
    elapsed = time.monotonic() - started
    await release_task

    assert elapsed < 0.12
    assert first.calls == 1
    assert first.closed.is_set() is True
    assert first.configs[0].timeout < 0.05
    assert second.calls == []
    assert third.calls == []
    assert result.source == "router_fallback"
    assert result.fallback_reason == "transient"
    assert result.usage["attempt_count"] == 1
    assert len(result.usage["physical_attempts"]) == 1
    chain = result.trace()["chain"]
    assert chain["exhausted"] is True
    assert chain["deadline"]["expired"] is True
    for _ in range(10):
        await asyncio.sleep(0)
        if controller.active_leases == 0:
            break
    assert controller.active_leases == 0


@pytest.mark.asyncio
async def test_task_analyzer_pre_stream_retry_releases_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    controller = ProviderAdmissionController(
        ProviderAdmissionSettings(
            global_max_in_flight=1,
            provider_default_max_in_flight=1,
            deployment_default_max_in_flight=1,
            queue_timeout_seconds=0.05,
        )
    )
    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 1
    original_model_copy = ChatConfig.model_copy
    projection_calls = 0

    def flaky_model_copy(
        self: ChatConfig,
        *args: Any,
        **kwargs: Any,
    ) -> ChatConfig:
        nonlocal projection_calls
        projection_calls += 1
        if projection_calls == 1:
            raise ValueError("synthetic config projection failure")
        return original_model_copy(self, *args, **kwargs)

    monkeypatch.setattr(ChatConfig, "model_copy", flaky_model_copy)
    result = await analyze_task_with_provider(
        provider=provider,
        message="classify this task",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        ranking_config=config,
        admission_controller=controller,
        admission_deadline=time.monotonic() + 0.2,
    )

    assert result.source == "llm_provider"
    assert len(provider.calls) == 1
    assert controller.active_leases == 0
    assert controller.snapshot()["total_acquired"] == 2
    assert controller.snapshot()["total_released"] == 2


@pytest.mark.asyncio
async def test_task_analyzer_payload_keeps_unicode_without_ascii_expansion() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    message = "请分析这个中文任务：实现可靠的解析器。" * 20
    config = load_ranking_config()

    result = await analyze_task_with_provider(
        provider=provider,
        message=message,
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        ranking_config=config,
    )

    assert result.schema_valid is True
    payload = str(provider.calls[0][0][0].content)
    assert "中文任务" in payload
    assert "\\u4e2d" not in payload
    assert len(payload) <= config["task_analyzer"]["payload_max_chars"]
    assert len(payload.encode("utf-8")) <= config["task_analyzer"]["payload_max_bytes"]
    assert (
        ranking_router._estimated_tokens_from_text(payload, config)
        <= (config["task_analyzer"]["payload_max_estimated_tokens"])
    )


@pytest.mark.asyncio
async def test_task_analyzer_payload_compacts_full_oversized_context() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    config = load_ranking_config()
    config["task_analyzer"].update(
        {
            "payload_max_chars": 2200,
            "payload_max_bytes": 5000,
            "payload_max_estimated_tokens": 1500,
        }
    )
    request_context = {
        **_context(input_tokens=90_000),
        "large_nested_context": {"中文材料": "非常长" * 100_000},
    }
    message = "中文任务正文" * 10_000

    with structlog.testing.capture_logs() as captured:
        result = await analyze_task_with_provider(
            provider=provider,
            message=message,
            user_profile_enabled=False,
            request_context=request_context,
            routed_tier="c1",
            routing_confidence=0.8,
            ranking_config=config,
        )

    assert result.schema_valid is True
    payload = str(provider.calls[0][0][0].content)
    decoded = json.loads(payload)
    compact_context = decoded["request_context"]
    assert compact_context["payload_context_truncated"] is True
    assert compact_context["routing_budget"]["estimated_input_tokens"] == 90_000
    assert "large_nested_context" not in compact_context
    assert "中文" in decoded["task"]
    assert len(decoded["task"]) < 24_000
    assert len(payload) <= 2200
    assert len(payload.encode("utf-8")) <= 5000
    assert ranking_router._estimated_tokens_from_text(payload, config) <= 1500
    started = next(
        row
        for row in captured
        if row["event"] == "llm_ensemble.router_dynamic.task_analyzer_started"
    )
    assert started["input_truncated"] is True
    assert started["payload_task_truncated"] is True


@pytest.mark.asyncio
async def test_task_analyzer_impossible_payload_budget_starts_no_request() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    config = load_ranking_config()
    config["task_analyzer"].update(
        {
            "payload_max_chars": 1,
            "payload_max_bytes": 1,
            "payload_max_estimated_tokens": 1,
        }
    )

    result = await analyze_task_with_provider(
        provider=provider,
        message="中文任务",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        ranking_config=config,
    )

    assert result.source == "router_fallback"
    assert result.fallback_reason == "DynamicRankingError"
    assert result.usage == {"physical_attempts": [], "attempt_count": 0}
    assert provider.calls == []


@pytest.mark.asyncio
async def test_historical_task_analyzer_keeps_single_route_serialization() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    invalid_provider = _AnalyzerProvider("not-json")
    historical_config = ranking_config_snapshot(base_version="step2-ranking-2026-08-10.1")

    result = await analyze_task_with_provider(
        provider=provider,
        message="中文任务",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        ranking_config=historical_config,
    )
    invalid_result = await analyze_task_with_provider(
        provider=invalid_provider,
        message="中文任务",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        ranking_config=historical_config,
    )

    assert result.schema_valid is True
    assert len(provider.calls) == 1
    payload = str(provider.calls[0][0][0].content)
    assert "\\u4e2d" in payload
    assert provider.calls[0][1] is not None
    assert provider.calls[0][1].allow_provider_stream_fallback is True
    assert invalid_result.schema_valid is False
    assert invalid_result.fallback_reason == "ValueError"
    assert len(invalid_provider.calls) == 4


@pytest.mark.asyncio
async def test_task_analyzer_override_controls_request_usage_and_trace_identity() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    ranking_config = ranking_config_snapshot(
        override={
            "task_analyzer": {
                "model": "openai/gpt-5.2",
                "upstream_provider": "openai",
            }
        }
    )

    result = await analyze_task_with_provider(
        provider=provider,
        message="implement a parser",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        ranking_config=ranking_config,
    )

    assert result.provider_id == "openrouter"
    assert result.model_id == "openai/gpt-5.2"
    assert result.trace(ranking_config)["model"] == "openai/gpt-5.2"
    assert result.usage["provider"] == "openrouter"
    assert result.usage["model"] == "analyzer-test"
    assert result.usage["requested_provider"] == "openrouter"
    assert result.usage["requested_model"] == "openai/gpt-5.2"


@pytest.mark.asyncio
async def test_task_analyzer_explicit_identity_mismatch_fails_before_request() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    ranking_config = ranking_config_snapshot(
        override={"task_analyzer": {"model": "openai/gpt-5.2"}}
    )

    with pytest.raises(DynamicRankingError, match="caller model identity conflicts"):
        await analyze_task_with_provider(
            provider=provider,
            message="implement a parser",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c1",
            routing_confidence=0.8,
            analyzer_model_id=TASK_ANALYZER_MODEL_ID,
            ranking_config=ranking_config,
        )

    assert provider.calls == []


@pytest.mark.asyncio
async def test_task_analyzer_uses_durable_accounting_and_retains_provider_evidence() -> None:
    class _Sink:
        def __init__(self) -> None:
            self.started: list[Any] = []
            self.finalized: list[tuple[Any, Any]] = []
            self.unknown: list[tuple[Any, str]] = []

        async def start(self, call: Any) -> None:
            self.started.append(call)

        async def finalize(self, call: Any, result: Any) -> None:
            self.finalized.append((call, result))

        async def mark_unknown(self, call: Any, reason: str) -> None:
            self.unknown.append((call, reason))

    sink = _Sink()
    scope = UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="routing-decision-1",
            agent_run_id="routing-decision-1",
            turn_id="turn-1",
            session_id="session-1",
            agent_id="main",
            run_kind="routing",
        ),
    )
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    with bind_usage_accounting_scope(scope):
        result = await analyze_task_with_provider(
            provider=provider,
            message="implement a parser",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c1",
            routing_confidence=0.8,
            analyzer_provider_id=TASK_ANALYZER_PROVIDER_ID,
            analyzer_model_id=TASK_ANALYZER_MODEL_ID,
        )

    assert len(sink.started) == 1
    assert len(sink.finalized) == 1
    assert sink.unknown == []
    assert sink.started[0].provider == TASK_ANALYZER_PROVIDER_ID
    assert sink.finalized[0][1].cost_source == "provider_billed"
    assert result.usage["provider_usage"]["is_byok"] is False
    assert result.usage["provider_usage"]["response_ids"] == ["analyzer-1"]


@pytest.mark.asyncio
async def test_task_analyzer_omits_user_profile_and_correlates_logs() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    with structlog.testing.capture_logs() as captured:
        result = await analyze_task_with_provider(
            provider=provider,
            message="implement a parser",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c1",
            routing_confidence=0.8,
            decision_id="decision-without-profile",
        )

    assert result.source == "llm_provider"
    analyzer_payload = json.loads(str(provider.calls[0][0][0].content))
    assert "user_profile" not in analyzer_payload
    analyzer_events = [
        row
        for row in captured
        if str(row["event"]).startswith("llm_ensemble.router_dynamic.task_analyzer_")
    ]
    assert [row["event"] for row in analyzer_events] == [
        "llm_ensemble.router_dynamic.task_analyzer_started",
        "llm_ensemble.router_dynamic.task_analyzer_completed",
    ]
    assert all(row["decision_id"] == "decision-without-profile" for row in analyzer_events)
    assert all(row["user_profile_enabled"] is False for row in analyzer_events)


@pytest.mark.asyncio
async def test_task_analyzer_logs_profile_enabled_without_receiving_profile() -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    with structlog.testing.capture_logs() as captured:
        await analyze_task_with_provider(
            provider=provider,
            message="implement a parser",
            user_profile_enabled=True,
            request_context=_context(),
            routed_tier="c1",
            routing_confidence=0.8,
            decision_id="decision-with-profile",
        )

    assert "user_profile" not in json.loads(str(provider.calls[0][0][0].content))
    analyzer_events = [
        row
        for row in captured
        if str(row["event"]).startswith("llm_ensemble.router_dynamic.task_analyzer_")
    ]
    assert analyzer_events
    assert all(row["user_profile_enabled"] is True for row in analyzer_events)


@pytest.mark.asyncio
async def test_task_analyzer_chat_parameters_are_loaded_from_ranking_config() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_output_tokens"] = 321
    config["task_analyzer"]["temperature"] = 0.2
    config["task_analyzer"]["thinking"] = True
    config["task_analyzer"]["timeout_seconds"] = 7.5
    config["task_analyzer"]["input_max_chars"] = 80
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))

    result = await analyze_task_with_provider(
        provider=provider,
        message="implement a parser " * 100,
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.8,
        ranking_config=config,
    )

    chat_config = provider.calls[0][1]
    assert result.source == "llm_provider"
    assert chat_config is not None
    assert chat_config.max_tokens == 321
    assert chat_config.temperature == 0.2
    assert chat_config.thinking is True
    assert chat_config.timeout == 7.5
    analyzer_payload = json.loads(str(provider.calls[0][0][0].content))
    assert len(analyzer_payload["task"]) == 80
    assert "truncated for classification" in analyzer_payload["task"]


@pytest.mark.asyncio
async def test_task_analyzer_incomplete_stream_falls_back_even_with_valid_json() -> None:
    result = await analyze_task_with_provider(
        provider=_AnalyzerProvider(json.dumps(_task_profile(tier=2)), include_done=False),
        message="hello",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
    )

    assert result.source == "router_fallback"
    assert result.schema_valid is False
    assert result.fallback_reason == "RuntimeError"


@pytest.mark.asyncio
async def test_task_analyzer_legacy_provider_error_keeps_runtime_error_reason() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 0
    result = await analyze_task_with_provider(
        provider=_AnalyzerTerminalProvider(
            [
                ErrorEvent(
                    message="legacy preflight failure",
                    code="unauthorized",
                    request_started=False,
                    physical_request_count=0,
                )
            ]
        ),
        message="hello",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        ranking_config=config,
    )

    assert result.source == "router_fallback"
    assert result.fallback_reason == "RuntimeError"
    assert result.usage["attempt_count"] == 0


@pytest.mark.asyncio
async def test_task_analyzer_retries_three_times_before_succeeding() -> None:
    malformed = _task_profile(tier=2)
    malformed["domain_dist"] = {"unsupported-domain": 1.0}
    provider = _AnalyzerProvider(
        [
            json.dumps(malformed),
            "not-json",
            json.dumps(malformed),
            json.dumps(_task_profile(tier=2)),
        ]
    )

    with structlog.testing.capture_logs() as captured:
        result = await analyze_task_with_provider(
            provider=provider,
            message="hello",
            user_profile_enabled=True,
            request_context=_context(),
            routed_tier="c2",
            routing_confidence=0.77,
            ranking_config=load_ranking_config(base_version="step2-ranking-2026-08-18.1"),
        )

    assert result.source == "llm_provider"
    assert result.schema_valid is True
    assert len(provider.calls) == 4
    assert result.usage["attempt_count"] == 4
    assert result.usage["input_tokens"] == 44
    assert result.usage["billed_cost"] == pytest.approx(0.048)
    physical_attempts = result.usage["physical_attempts"]
    assert [row["attempt"] for row in physical_attempts] == [1, 2, 3, 4]
    assert [row["provider_usage"]["response_ids"][0] for row in physical_attempts] == [
        "analyzer-1",
        "analyzer-2",
        "analyzer-3",
        "analyzer-4",
    ]
    assert len({row["physical_attempt_id"] for row in physical_attempts}) == 4
    retry_events = [row for row in captured if row["event"].endswith("task_analyzer_retry")]
    assert [row["attempt"] for row in retry_events] == [1, 2, 3]
    assert not any(row["event"].endswith("task_analyzer_fallback") for row in captured)
    assert "chain" not in result.trace()


@pytest.mark.asyncio
async def test_task_analyzer_preserves_unknown_then_exact_retry_attempts() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 1
    provider = _AnalyzerProvider(
        [
            json.dumps(_task_profile(tier=2)),
            json.dumps(_task_profile(tier=2)),
        ],
        include_done=[False, True],
    )

    result = await analyze_task_with_provider(
        provider=provider,
        message="hello",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        analyzer_provider_id=TASK_ANALYZER_PROVIDER_ID,
        analyzer_model_id=TASK_ANALYZER_MODEL_ID,
        ranking_config=config,
        decision_id="a" * 32,
    )

    assert result.source == "llm_provider"
    assert result.usage["attempt_count"] == 2
    attempts = result.usage["physical_attempts"]
    assert [row["attempt"] for row in attempts] == [1, 2]
    assert attempts[0]["usage_unknown"] is True
    assert attempts[0]["provider"] == ""
    assert attempts[0]["requested_provider"] == TASK_ANALYZER_PROVIDER_ID
    assert attempts[0]["requested_model"] == TASK_ANALYZER_MODEL_ID
    assert attempts[0]["cost_source"] == "none"
    assert attempts[0]["billed_cost"] == 0.0
    assert attempts[1]["provider_usage"]["response_ids"] == ["analyzer-2"]
    assert attempts[1].get("usage_unknown") is not True
    assert len({row["physical_attempt_id"] for row in attempts}) == 2


@pytest.mark.asyncio
async def test_task_analyzer_explicit_no_request_retry_has_no_physical_gap() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 1
    provider = _AnalyzerTerminalProvider(
        [
            ErrorEvent(
                message="local preflight",
                code="local_preflight",
                request_started=False,
                physical_request_count=0,
            ),
            DoneEvent(
                provider="openrouter",
                model="analyzer-test",
                input_tokens=11,
                output_tokens=7,
                billed_cost=0.012,
                cost_source="provider_billed",
                provider_usage={
                    "response_ids": ["analyzer-2"],
                    "physical_attempt_id": "e" * 32,
                },
            ),
        ]
    )

    result = await analyze_task_with_provider(
        provider=provider,
        message="hello",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        ranking_config=config,
        decision_id="b" * 32,
    )

    assert len(provider.calls) == 2
    assert result.usage["attempt_count"] == 1
    assert [row["attempt"] for row in result.usage["physical_attempts"]] == [1]
    assert result.usage["physical_attempts"][0]["provider_usage"]["response_ids"] == ["analyzer-2"]
    assert (
        result.usage["physical_attempts"][0]["provider_usage"]["physical_attempt_id"]
        == result.usage["physical_attempts"][0]["physical_attempt_id"]
    )
    assert result.usage["physical_attempts"][0]["physical_attempt_id"] == "e" * 32


@pytest.mark.asyncio
async def test_task_analyzer_explicit_no_request_fallback_is_zero_usage() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 0

    result = await analyze_task_with_provider(
        provider=_AnalyzerTerminalProvider(
            [
                ErrorEvent(
                    message="local preflight",
                    code="local_preflight",
                    request_started=False,
                    physical_request_count=0,
                )
            ]
        ),
        message="hello",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        ranking_config=config,
    )

    assert result.source == "router_fallback"
    assert result.usage == {"physical_attempts": [], "attempt_count": 0}


@pytest.mark.asyncio
async def test_task_analyzer_error_preserves_known_diagnostic_receipt() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 0
    diagnostic = DoneEvent(
        provider="openrouter",
        model="analyzer-test",
        input_tokens=13,
        output_tokens=5,
        billed_cost=0.025,
        cost_source="provider_billed",
        provider_usage={
            "response_ids": ["diagnostic-response"],
            "physical_attempt_id": "c" * 32,
        },
    )

    result = await analyze_task_with_provider(
        provider=_AnalyzerTerminalProvider(
            [
                ErrorEvent(
                    message="response metadata invalid",
                    code="response_invalid",
                    diagnostic_done=diagnostic,
                    request_started=True,
                    physical_request_count=1,
                )
            ]
        ),
        message="hello",
        user_profile_enabled=False,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
        ranking_config=config,
    )

    attempt = result.usage["physical_attempts"][0]
    assert result.source == "router_fallback"
    assert result.usage["attempt_count"] == 1
    assert attempt["input_tokens"] == 13
    assert attempt["billed_cost"] == pytest.approx(0.025)
    assert attempt["provider_usage"]["response_ids"] == ["diagnostic-response"]
    assert attempt["physical_attempt_id"] == "c" * 32
    assert attempt["provider_usage"]["physical_attempt_id"] == "c" * 32
    assert attempt.get("usage_unknown") is not True


@pytest.mark.asyncio
async def test_task_analyzer_conflicting_receipt_ids_fail_closed() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 3
    provider = _AnalyzerTerminalProvider(
        [
            ErrorEvent(
                message="conflicting request identities",
                code="response_invalid",
                model_usage_breakdown=[
                    {
                        "provider": "openrouter",
                        "model": "analyzer-test",
                        "input_tokens": 3,
                        "output_tokens": 1,
                        "billed_cost": 0.01,
                        "cost_source": "provider_billed",
                        "physical_attempt_id": "c" * 32,
                        "provider_usage": {
                            "response_ids": ["paid-response"],
                            "physical_attempt_id": "d" * 32,
                        },
                    }
                ],
                diagnostic_done=DoneEvent(
                    provider="openrouter",
                    model="analyzer-test",
                    input_tokens=3,
                    output_tokens=1,
                    billed_cost=0.01,
                    cost_source="provider_billed",
                    provider_usage={
                        "response_ids": ["paid-response"],
                        "physical_attempt_id": "d" * 32,
                    },
                ),
                request_started=True,
                physical_request_count=1,
            )
        ]
    )

    with pytest.raises(TaskAnalyzerPhysicalEvidenceError):
        await analyze_task_with_provider(
            provider=provider,
            message="hello",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c2",
            routing_confidence=0.77,
            ranking_config=config,
        )

    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_task_analyzer_contradictory_receipt_fails_closed_with_usage() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 3
    provider = _AnalyzerTerminalProvider(
        [
            ErrorEvent(
                message="contradictory adapter evidence",
                code="response_invalid",
                diagnostic_done=DoneEvent(
                    provider="openrouter",
                    model="analyzer-test",
                    input_tokens=3,
                    output_tokens=1,
                    billed_cost=0.01,
                    cost_source="provider_billed",
                    provider_usage={"response_ids": ["paid-response"]},
                ),
                request_started=False,
                physical_request_count=0,
            )
        ]
    )

    with pytest.raises(TaskAnalyzerPhysicalEvidenceError) as caught:
        await analyze_task_with_provider(
            provider=provider,
            message="hello",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c2",
            routing_confidence=0.77,
            ranking_config=config,
        )

    assert len(provider.calls) == 1
    assert caught.value.usage["attempt_count"] == 1
    assert caught.value.usage["physical_attempts"][0]["provider_usage"]["response_ids"] == [
        "paid-response"
    ]
    assert (
        caught.value.usage["physical_attempts"][0]["provider_usage"]["physical_attempt_id"]
        == caught.value.usage["physical_attempts"][0]["physical_attempt_id"]
    )


@pytest.mark.asyncio
async def test_task_analyzer_multiple_physical_requests_fail_closed() -> None:
    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 3

    with pytest.raises(TaskAnalyzerPhysicalEvidenceError) as caught:
        await analyze_task_with_provider(
            provider=_AnalyzerTerminalProvider(
                [
                    ErrorEvent(
                        message="fallback wrapper made two calls",
                        code="all_routes_failed",
                        request_started=True,
                        physical_request_count=2,
                        usage_missing_count=2,
                    )
                ]
            ),
            message="hello",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c2",
            routing_confidence=0.77,
            ranking_config=config,
        )

    assert caught.value.usage["attempt_count"] == 2
    assert [row["attempt"] for row in caught.value.usage["physical_attempts"]] == [
        1,
        2,
    ]
    assert all(row["usage_unknown"] is True for row in caught.value.usage["physical_attempts"])


@pytest.mark.asyncio
async def test_task_analyzer_logs_do_not_store_exception_body() -> None:
    class _ExplodingProvider:
        provider_name = "analyzer-test"
        model = "analyzer-test"
        accounts_physical_usage = True

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            async def stream() -> AsyncIterator[Any]:
                raise RuntimeError("sensitive upstream response body")
                yield  # pragma: no cover

            return stream()

        async def list_models(self) -> list[Any]:
            return []

    config = load_ranking_config()
    config["task_analyzer"]["max_retries"] = 0
    with structlog.testing.capture_logs() as captured:
        await analyze_task_with_provider(
            provider=_ExplodingProvider(),
            message="hello",
            user_profile_enabled=False,
            request_context=_context(),
            routed_tier="c2",
            routing_confidence=0.77,
            ranking_config=config,
        )

    serialized_logs = json.dumps(captured)
    assert "sensitive upstream response body" not in serialized_logs
    assert all("details" not in row for row in captured)


@pytest.mark.asyncio
async def test_task_analyzer_malformed_output_falls_back_to_tree_router_profile() -> None:
    provider = _AnalyzerProvider("not-json")
    result = await analyze_task_with_provider(
        provider=provider,
        message="hello",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
    )

    assert result.source == "router_fallback"
    assert result.schema_valid is False
    assert result.profile["tier_dist"] == {"3": 1.0}
    assert result.confidence == pytest.approx(0.77)
    assert len(provider.calls) == 2
    assert result.usage["attempt_count"] == 2
    assert result.usage["billed_cost"] == pytest.approx(0.024)
    assert len(result.usage["physical_attempts"]) == 2


@pytest.mark.asyncio
async def test_task_analyzer_invalid_required_constraint_uses_fallback() -> None:
    malformed = _task_profile(tier=2)
    malformed["constraints"]["risk"] = "catastrophic"

    result = await analyze_task_with_provider(
        provider=_AnalyzerProvider(json.dumps(malformed)),
        message="hello",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c2",
        routing_confidence=0.77,
    )

    assert result.source == "router_fallback"
    assert result.schema_valid is False
    assert result.profile["tier_dist"] == {"3": 1.0}


@pytest.mark.asyncio
async def test_task_analyzer_drops_invalid_optional_fields_without_full_fallback() -> None:
    profile = _task_profile(tier=2)
    profile["optional_constraints"] = {"format": "unsupported-format"}
    profile["analysis_confidence"] = "high"

    result = await analyze_task_with_provider(
        provider=_AnalyzerProvider(json.dumps(profile)),
        message="hello",
        user_profile_enabled=True,
        request_context=_context(),
        routed_tier="c1",
        routing_confidence=0.77,
    )

    assert result.source == "llm_provider"
    assert result.schema_valid is True
    assert result.profile["optional_constraints"] == {}
    assert result.confidence == pytest.approx(0.80)
    assert result.normalization_warnings == (
        "invalid_optional_format",
        "invalid_analysis_confidence",
    )
    assert result.trace()["normalization_warnings"] == [
        "invalid_optional_format",
        "invalid_analysis_confidence",
    ]


@pytest.mark.asyncio
async def test_task_analyzer_cannot_drop_an_actual_input_modality() -> None:
    incomplete = _task_profile(tier=2, modalities=["text"])
    request_context = _context()
    request_context["input_modalities"] = ["text", "image"]

    result = await analyze_task_with_provider(
        provider=_AnalyzerProvider(json.dumps(incomplete)),
        message="review the attached diagram",
        user_profile_enabled=True,
        request_context=request_context,
        routed_tier="c2",
        routing_confidence=0.77,
    )

    assert result.source == "router_fallback"
    assert result.profile["constraints"]["modality"] == ["text", "image"]


def test_hard_filter_records_availability_permission_modality_and_context_reasons() -> None:
    eligible = _model("eligible", roles=["proposer", "aggregator"], modalities=["text", "image"])
    unavailable = _model("unavailable", credential_available=False, modalities=["text", "image"])
    denied = _model("denied", modalities=["text", "image"])
    text_only = _model("text-only", modalities=["text"])
    short_context = _model("short-context", context_window=1_500, modalities=["image"])
    user = mock_user_profile()
    user["permission"]["deny_models"] = ["denied"]

    decision = _decision(
        eligible,
        unavailable,
        denied,
        text_only,
        short_context,
        analysis=_analysis(tier=1, modalities=["image"]),
        context=_context(input_tokens=1_000, candidate_tokens=1_000),
        user_profile=user,
    )
    by_model = {row["model"]: row for row in decision.trace["hard_filter"]["proposer_results"]}

    assert "credential_unavailable" in by_model["unavailable"]["reasons"]
    assert "no_permission" in by_model["denied"]["reasons"]
    assert "modality_mismatch" in by_model["text-only"]["reasons"]
    assert "context_exceeded" in by_model["short-context"]["reasons"]
    assert decision.proposers[0].model_id == "eligible"


def test_runtime_generation_policy_reason_excludes_before_ranking() -> None:
    primary = _model("primary", capability=0.95)
    backup = _model("backup", capability=0.90)
    contrast = _model("contrast", capability=0.85)
    blocked = _model("blocked", capability=1.0)
    blocked["registry_facts"]["runtime_hard_filter_reasons"] = [
        "generation_policy_reasoning_unsupported"
    ]

    decision = _decision(
        primary,
        backup,
        contrast,
        blocked,
        analysis=_analysis(tier=3, latency="interactive"),
    )
    selected = {
        *(model.model_id for model in decision.proposers),
        decision.aggregator.model_id,
    }
    proposer_row = next(
        row
        for row in decision.trace["hard_filter"]["proposer_results"]
        if row["model"] == "blocked"
    )
    aggregator_row = next(
        row
        for row in decision.trace["hard_filter"]["aggregator_results"]
        if row["model"] == "blocked"
    )

    assert "blocked" not in selected
    assert proposer_row["eligible"] is False
    assert aggregator_row["eligible"] is False
    assert proposer_row["reasons"] == ["generation_policy_reasoning_unsupported"]
    assert aggregator_row["reasons"] == ["generation_policy_reasoning_unsupported"]


def test_runtime_deployment_reason_is_hard_filtered_only_for_bound_role() -> None:
    primary = _model("primary", capability=0.95)
    backup = _model("backup", capability=0.90)
    blocked = _model("blocked", capability=1.0, aggregator_fit=1.0)
    blocked["registry_facts"]["runtime_hard_filter_reasons_by_role"] = {
        "proposer": ["runtime_deployment_benched"],
    }

    decision = _decision(
        primary,
        backup,
        blocked,
        analysis=_analysis(tier=2, latency="interactive"),
        user_profile_enabled=False,
    )
    proposer_row = next(
        row
        for row in decision.trace["hard_filter"]["proposer_results"]
        if row["model"] == "blocked"
    )
    aggregator_row = next(
        row
        for row in decision.trace["hard_filter"]["aggregator_results"]
        if row["model"] == "blocked"
    )

    assert proposer_row["eligible"] is False
    assert proposer_row["reasons"] == ["runtime_deployment_benched"]
    assert aggregator_row["eligible"] is True
    assert "runtime_deployment_benched" not in aggregator_row["reasons"]
    assert ranking_trace_replay_reasons(decision.trace) == []


def test_generation_policy_filter_fails_clearly_when_below_n_min() -> None:
    eligible = _model("eligible", capability=0.95)
    blocked_one = _model("blocked-one", capability=0.90)
    blocked_two = _model("blocked-two", capability=0.85)
    for blocked in (blocked_one, blocked_two):
        blocked["registry_facts"]["runtime_hard_filter_reasons"] = [
            "generation_policy_reasoning_unsupported"
        ]

    with pytest.raises(
        DynamicRankingError,
        match=r"generation-policy filtering left 1 eligible proposer\(s\), fewer than N_min=2",
    ):
        _decision(
            eligible,
            blocked_one,
            blocked_two,
            analysis=_analysis(tier=3, latency="interactive"),
        )


def _profile_with_history(*, positive: list[str], negative: list[str], count: int) -> dict:
    profile = mock_user_profile()
    profile["history"]["positive_model_ids"] = positive
    profile["history"]["negative_model_ids"] = negative
    profile["history"]["feedback_count"] = count
    return profile


def test_history_reorders_candidates_that_task_match_alone_would_not() -> None:
    """The regression that matters: history must be able to change the order.

    With an empty history every model gets the same neutral S_user, so the
    0.15 * S_user term is a uniform offset and cannot reorder anything — the
    profile is inert rather than approximate. A saturated history splits
    S_user across models and the weaker-but-liked model wins.
    """
    liked = _model("liked", capability=0.80, aggregator_fit=0.80)
    disliked = _model("disliked", capability=0.85, aggregator_fit=0.85)

    neutral = _decision(liked, disliked, analysis=_analysis(tier=2))
    assert [m.model_id for m in neutral.proposers][0] == "disliked"

    opinionated = _decision(
        liked,
        disliked,
        analysis=_analysis(tier=2),
        user_profile=_profile_with_history(positive=["liked"], negative=["disliked"], count=20),
    )
    assert [m.model_id for m in opinionated.proposers][0] == "liked"


def test_history_confidence_ramps_in_with_feedback_count() -> None:
    """One click must not swing the ranking to an extreme.

    confidence = min(1, feedback_count / 20), so a single rating moves S_user
    by 1/20th of the full signal — not enough to overturn a task-match gap
    that a saturated history does overturn.
    """
    liked = _model("liked", capability=0.80, aggregator_fit=0.80)
    disliked = _model("disliked", capability=0.85, aggregator_fit=0.85)

    barely = _decision(
        liked,
        disliked,
        analysis=_analysis(tier=2),
        user_profile=_profile_with_history(positive=["liked"], negative=["disliked"], count=1),
    )
    assert [m.model_id for m in barely.proposers][0] == "disliked"


def test_ranking_without_user_profile_bypasses_all_profile_effects() -> None:
    preferred = _model("preferred", capability=0.95, aggregator_fit=0.95)
    backup = _model("backup", capability=0.80, aggregator_fit=0.80)
    contrast = _model("contrast", capability=0.70, aggregator_fit=0.70)
    profile = mock_user_profile()
    profile["permission"]["deny_models"] = ["preferred"]
    profile["permission"]["risk_allowlist"] = ["medium"]
    profile["preference"]["cost_sensitivity"] = "hard_limit"
    profile["preference"]["quality_latency_tradeoff"] = "latency_first"

    enabled = _decision(
        preferred,
        backup,
        contrast,
        analysis=_analysis(tier=3, risk="medium"),
        user_profile=profile,
    )
    risk_blocked_profile = mock_user_profile()
    risk_blocked_profile["permission"]["risk_allowlist"] = ["low"]
    with pytest.raises(DynamicRankingError, match="no proposer"):
        _decision(
            preferred,
            backup,
            contrast,
            analysis=_analysis(tier=3, risk="medium"),
            user_profile=risk_blocked_profile,
        )
    disabled = rank_models(
        task_analysis=_analysis(tier=3, risk="medium"),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(preferred, backup, contrast),
        routed_tier="c2",
        routing_confidence=0.9,
        decision_id="ranking-without-profile",
    )

    assert enabled.trace["user_profile_enabled"] is True
    assert enabled.trace["N_max"] == 2
    assert disabled.trace["decision_id"] == "ranking-without-profile"
    assert disabled.trace["user_profile_enabled"] is False
    assert disabled.trace["user_profile_version"] == ""
    assert disabled.trace["user_profile_source"] == ""
    assert disabled.trace["N_max"] == 3
    disabled_filters = [
        *disabled.trace["hard_filter"]["proposer_results"],
        *disabled.trace["hard_filter"]["aggregator_results"],
    ]
    assert all("no_permission" not in row["reasons"] for row in disabled_filters)
    assert all("risk_not_allowed" not in row["reasons"] for row in disabled_filters)
    assert {row["model"] for row in disabled.trace["model_scores"]} == {
        "preferred",
        "backup",
        "contrast",
    }
    for row in disabled.trace["model_scores"]:
        assert row["S_user"] == 0.0
        assert row["S_qual_before_reliability"] == row["S_match"]
        assert row["S_qual_clean"] == pytest.approx(
            row["S_match"] - row["role_reliability"]["penalty"],
            abs=1e-6,
        )
        assert row["cost_weight"] == pytest.approx(0.10)
        assert row["latency_weight"] == pytest.approx(0.0)


def test_availability_filter_covers_registry_health_quota_rate_and_role() -> None:
    healthy = _model("healthy")
    disabled = _model("disabled", status="disabled")
    unhealthy = _model("unhealthy", health="unavailable")
    no_quota = _model("no-quota")
    no_quota["registry_facts"]["quota"] = 0
    limited = _model("limited")
    limited["registry_facts"]["rate_limit"] = "limited"
    aggregator_only = _model("aggregator-only", roles=["aggregator"])

    decision = _decision(
        healthy,
        disabled,
        unhealthy,
        no_quota,
        limited,
        aggregator_only,
        analysis=_analysis(tier=1),
    )
    by_model = {row["model"]: row for row in decision.trace["hard_filter"]["proposer_results"]}

    assert "status_unavailable" in by_model["disabled"]["reasons"]
    assert "health_unavailable" in by_model["unhealthy"]["reasons"]
    assert "quota_exhausted" in by_model["no-quota"]["reasons"]
    assert "rate_limited" in by_model["limited"]["reasons"]
    assert "role_proposer_unsupported" in by_model["aggregator-only"]["reasons"]


def test_hard_filter_availability_states_are_config_driven() -> None:
    config = load_ranking_config()
    config["hard_filter"]["eligible_statuses"].append("maintenance")

    decision = _decision(
        _model("maintenance-model", status="maintenance"),
        analysis=_analysis(tier=1),
        ranking_config=config,
    )

    assert decision.proposers[0].model_id == "maintenance-model"


def test_canary_status_is_eligible_by_default() -> None:
    decision = _decision(
        _model("canary-model", status="canary"),
        analysis=_analysis(tier=1),
    )

    proposer_row = decision.trace["hard_filter"]["proposer_results"][0]
    aggregator_row = decision.trace["hard_filter"]["aggregator_results"][0]
    assert "status_unavailable" not in proposer_row["reasons"]
    assert "status_unavailable" not in aggregator_row["reasons"]
    assert decision.proposers[0].model_id == "canary-model"


def test_user_risk_permission_is_a_hard_filter() -> None:
    user = mock_user_profile()
    user["permission"]["risk_allowlist"] = ["low", "medium"]

    with pytest.raises(DynamicRankingError, match="no proposer"):
        _decision(
            _model("eligible-by-model"),
            analysis=_analysis(tier=4, risk="high"),
            user_profile=user,
        )


def test_greedy_selection_prefers_cross_family_complement_over_duplicate_family() -> None:
    primary = _model(
        "primary",
        provider="openrouter",
        vendor="vendor-a",
        family="family-a",
        capability=0.88,
    )
    duplicate = _model(
        "duplicate",
        provider="openrouter",
        vendor="vendor-a",
        family="family-a",
        capability=0.87,
    )
    complement = _model(
        "complement",
        provider="openrouter",
        vendor="vendor-b",
        family="family-b",
        capability=0.86,
    )

    decision = _decision(
        primary,
        duplicate,
        complement,
        analysis=_analysis(tier=3, latency="interactive"),
    )

    assert decision.trace["N_min"] == 2
    assert decision.trace["N_max"] == 2
    assert [model.model_id for model in decision.proposers] == ["primary", "complement"]
    assert decision.trace["selection_steps"][1]["max_similarity"] < 0.75
    assert [row["proposer_count"] for row in decision.trace["aggregator_feasibility"]] == [1, 2]
    assert all(row["eligible_aggregator_ids"] for row in decision.trace["aggregator_feasibility"])
    assert decision.trace["selection_steps"][0]["eligible_aggregator_count"] == 3


def test_rerank_trace_records_quality_floor_exclusions_and_stop_detail() -> None:
    strong = _model("strong", capability=0.90)
    weak = _model("weak", provider="provider-b", capability=0.10)

    decision = _decision(
        strong,
        weak,
        analysis=_analysis(tier=2, risk="low"),
    )

    assert [model.model_id for model in decision.proposers] == ["strong"]
    assert decision.trace["quality_floor_excluded_ids"] == ["provider-b:weak"]
    assert decision.trace["stop_reason"] == "quality_floor_or_pool_exhausted"
    assert decision.trace["stop_detail"] == {
        "quality_floor_excluded_count": 1,
        "remaining_candidate_count": 0,
    }


def test_aggregator_feasibility_filters_once_per_prospective_set_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = tuple(
        _model(
            f"model-{index}",
            provider=f"provider-{index}",
            family=f"family-{index}",
        )
        for index in range(4)
    )
    original = ranking_router._hard_filter_reasons
    aggregator_filter_calls = 0

    def counted_hard_filter(*args: Any, **kwargs: Any):
        nonlocal aggregator_filter_calls
        if kwargs.get("role") == "aggregator":
            aggregator_filter_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(ranking_router, "_hard_filter_reasons", counted_hard_filter)

    decision = _decision(*models, analysis=_analysis(tier=3))

    prospective_counts = len(decision.trace["aggregator_feasibility"])
    assert prospective_counts == len(decision.proposers)
    assert aggregator_filter_calls == len(models) * (prospective_counts + 1)


def test_rerank_weights_from_json_change_the_selected_proposer_set() -> None:
    primary = _model(
        "primary",
        provider="openrouter",
        vendor="vendor-a",
        family="family-a",
        capability=0.88,
    )
    duplicate = _model(
        "duplicate",
        provider="openrouter",
        vendor="vendor-a",
        family="family-a",
        capability=0.87,
    )
    complement = _model(
        "complement",
        provider="openrouter",
        vendor="vendor-b",
        family="family-b",
        capability=0.86,
    )
    config = load_ranking_config()
    config["config_version"] = "test-no-similarity-penalty-v1"
    config["rerank"]["similarity_penalty_weight"] = 0.0

    decision = _decision(
        primary,
        duplicate,
        complement,
        analysis=_analysis(tier=3, latency="interactive"),
        ranking_config=config,
    )

    assert [model.model_id for model in decision.proposers] == ["primary", "duplicate"]
    assert decision.trace["ranking_config_version"] == config["config_version"]
    assert decision.trace["ranking_parameters"] == (
        ranking_router._legacy_ranking_config_projection(config)
    )
    assert len(decision.trace["ranking_config_hash"]) == 64


def test_cost_breaks_quality_ties_in_proposer_selection() -> None:
    expensive = _model(
        "a-expensive",
        capability=0.82,
        price=40.0,
        latency_ms=30_000,
    )
    efficient = _model(
        "z-efficient",
        provider="provider-b",
        capability=0.82,
        price=0.1,
        latency_ms=1_000,
    )

    decision = _decision(
        expensive,
        efficient,
        analysis=_analysis(tier=1, cost="low", latency="interactive"),
    )

    assert decision.proposers[0].model_id == "z-efficient"
    efficient_score = next(
        row for row in decision.trace["model_scores"] if row["model"] == "z-efficient"
    )
    expensive_score = next(
        row for row in decision.trace["model_scores"] if row["model"] == "a-expensive"
    )
    assert efficient_score["S_base_clean"] > expensive_score["S_base_clean"]


def test_latency_penalty_switch_defaults_off_and_controls_all_score_paths() -> None:
    slow = _model("a-slow", capability=0.82, price=1.0, latency_ms=30_000)
    fast = _model(
        "z-fast",
        capability=0.82,
        price=1.0,
        latency_ms=1_000,
    )
    analysis = _analysis(tier=1, latency="interactive")

    default_single = _single_decision(slow, fast, analysis=analysis)
    enabled_config = load_ranking_config()
    enabled_config["penalties"]["latency_penalty_enabled"] = True
    enabled_config["config_version"] = "test-latency-penalty-enabled-v1"
    enabled_single = _single_decision(
        slow,
        fast,
        analysis=analysis,
        ranking_config=enabled_config,
    )

    assert default_single.model.model_id == "a-slow"
    assert enabled_single.model.model_id == "z-fast"
    assert all(
        row["latency_weight"] == pytest.approx(0.0) for row in default_single.trace["model_scores"]
    )
    assert all(
        row["latency_weight"] == pytest.approx(0.22) for row in enabled_single.trace["model_scores"]
    )

    common = {"provider": "openrouter", "vendor": "shared", "family": "shared"}
    primary = _model(
        "primary",
        roles=["proposer"],
        capability=0.99,
        price=1.0,
        **common,
    )
    slow_proposer = _model(
        "a-slow-proposer",
        roles=["proposer"],
        capability=0.82,
        price=1.0,
        latency_ms=30_000,
        **common,
    )
    fast_proposer = _model(
        "z-fast-proposer",
        roles=["proposer"],
        capability=0.82,
        price=1.0,
        latency_ms=1_000,
        **common,
    )
    proposer_aggregator = _model(
        "proposer-aggregator",
        provider="aggregator-provider",
        roles=["aggregator"],
        capability=0.90,
        aggregator_fit=0.95,
        price=1.0,
    )
    proposer_analysis = _analysis(tier=3, latency="interactive")
    default_proposers = _decision(
        primary,
        slow_proposer,
        fast_proposer,
        proposer_aggregator,
        analysis=proposer_analysis,
        user_profile_enabled=False,
    )
    enabled_proposers = _decision(
        primary,
        slow_proposer,
        fast_proposer,
        proposer_aggregator,
        analysis=proposer_analysis,
        ranking_config=enabled_config,
        user_profile_enabled=False,
    )

    assert [model.model_id for model in default_proposers.proposers] == [
        "primary",
        "a-slow-proposer",
    ]
    assert [model.model_id for model in enabled_proposers.proposers] == [
        "primary",
        "z-fast-proposer",
    ]
    assert (
        default_proposers.trace["N_min"],
        default_proposers.trace["N_max"],
    ) == (
        enabled_proposers.trace["N_min"],
        enabled_proposers.trace["N_max"],
    )

    proposer = _model("proposer", roles=["proposer"], capability=0.99)
    slow_aggregator = _model(
        "a-slow-aggregator",
        roles=["aggregator"],
        capability=0.82,
        aggregator_fit=0.82,
        latency_ms=30_000,
    )
    fast_aggregator = _model(
        "z-fast-aggregator",
        roles=["aggregator"],
        capability=0.82,
        aggregator_fit=0.82,
        latency_ms=1_000,
    )
    default_multi = _decision(
        proposer,
        slow_aggregator,
        fast_aggregator,
        analysis=analysis,
        user_profile_enabled=False,
    )
    enabled_multi = _decision(
        proposer,
        slow_aggregator,
        fast_aggregator,
        analysis=analysis,
        ranking_config=enabled_config,
        user_profile_enabled=False,
    )

    assert default_multi.aggregator.model_id == "a-slow-aggregator"
    assert enabled_multi.aggregator.model_id == "z-fast-aggregator"
    assert default_multi.trace["aggregator"]["selected"]["latency_weight"] == pytest.approx(0.0)
    assert enabled_multi.trace["aggregator"]["selected"]["latency_weight"] == pytest.approx(0.22)


def test_resource_aware_proposer_rerank_retains_cost_penalty_inside_top_l() -> None:
    common = {"provider": "openrouter", "vendor": "shared", "family": "shared"}
    primary = _model(
        "primary",
        roles=["proposer"],
        capability=0.99,
        price=0.1,
        **common,
    )
    expensive = _model(
        "expensive",
        roles=["proposer"],
        capability=0.95,
        price=8.0,
        **common,
    )
    efficient = _model(
        "efficient",
        roles=["proposer"],
        capability=0.89,
        price=0.1,
        **common,
    )
    aggregator = _model(
        "aggregator",
        provider="aggregator-provider",
        roles=["aggregator"],
        capability=0.90,
        aggregator_fit=0.95,
        price=0.1,
    )
    analysis = _analysis(tier=3, cost="low", latency="normal")

    current = _decision(
        primary,
        expensive,
        efficient,
        aggregator,
        analysis=analysis,
        user_profile_enabled=False,
    )
    historical = _decision(
        primary,
        expensive,
        efficient,
        aggregator,
        analysis=analysis,
        ranking_config=load_ranking_config(base_version="step2-ranking-2026-08-18.2"),
        user_profile_enabled=False,
    )

    assert [model.model_id for model in current.proposers] == ["primary", "efficient"]
    assert [model.model_id for model in historical.proposers] == ["primary", "expensive"]
    assert (
        ranking_router._resource_aware_proposer_rerank_enabled(current.trace["ranking_parameters"])
        is True
    )
    assert (
        ranking_router._resource_aware_proposer_rerank_enabled(
            historical.trace["ranking_parameters"]
        )
        is False
    )


def test_zero_role_reliability_observations_use_cold_start_prior() -> None:
    model = _with_role_reliability(_model("zero-observations"))

    decision = _decision(
        model,
        analysis=_analysis(tier=1),
        user_profile_enabled=False,
    )

    expected_failure_rate = 1 / 11
    expected_penalty = 0.40 * expected_failure_rate
    proposer_score = decision.trace["model_scores"][0]
    aggregator_score = decision.trace["aggregator"]["selected"]
    assert proposer_score["role_reliability"]["failure_rate"] == pytest.approx(
        expected_failure_rate,
        abs=1e-6,
    )
    assert proposer_score["role_reliability"]["penalty"] == pytest.approx(
        expected_penalty,
        abs=1e-6,
    )
    assert proposer_score["S_qual_before_reliability"] > proposer_score["S_qual_clean"]
    assert aggregator_score["role_reliability"]["failure_rate"] == pytest.approx(
        expected_failure_rate,
        abs=1e-6,
    )
    assert aggregator_score["role_reliability"]["penalty"] == pytest.approx(
        expected_penalty,
        abs=1e-6,
    )
    assert aggregator_score["S_agg_qual_before_reliability"] > aggregator_score["S_agg_qual"]


def test_reliability_cold_start_penalty_is_monotonic() -> None:
    def failure_rate(*, success: int, failure: int) -> float:
        model = _with_role_reliability(
            _model(f"model-{success}-{failure}"),
            proposer=(success, failure),
        )
        decision = _decision(
            model,
            analysis=_analysis(tier=1),
            user_profile_enabled=False,
        )
        return decision.trace["model_scores"][0]["role_reliability"]["failure_rate"]

    cold_start = failure_rate(success=0, failure=0)
    after_success = failure_rate(success=1, failure=0)
    after_failure = failure_rate(success=0, failure=1)
    after_recovery = failure_rate(success=1, failure=1)

    assert cold_start == pytest.approx(1 / 11, abs=1e-6)
    assert after_success == pytest.approx(1 / 12, abs=1e-6)
    assert after_failure == pytest.approx(1 / 6, abs=1e-6)
    assert after_recovery == pytest.approx(2 / 13, abs=1e-6)
    assert after_success < cold_start < after_recovery < after_failure


def test_cold_start_does_not_outrank_high_confidence_reliability() -> None:
    cold_start = _with_role_reliability(
        _model("cold-start", provider="provider-cold", roles=["proposer"]),
    )
    failure_free = _with_role_reliability(
        _model("failure-free", provider="provider-success", roles=["proposer"]),
        proposer=(50, 0),
    )
    one_failure = _with_role_reliability(
        _model("one-failure", provider="provider-observed", roles=["proposer"]),
        proposer=(49, 1),
    )
    aggregator = _model(
        "aggregator",
        provider="provider-aggregator",
        roles=["aggregator"],
    )

    decision = _decision(
        cold_start,
        failure_free,
        one_failure,
        aggregator,
        analysis=_analysis(tier=1),
        user_profile_enabled=False,
    )
    scores = {row["model"]: row for row in decision.trace["model_scores"]}

    assert scores["cold-start"]["role_reliability"]["failure_rate"] == (
        pytest.approx(1 / 11, abs=1e-6)
    )
    assert scores["failure-free"]["role_reliability"]["failure_rate"] == (
        pytest.approx(1 / 61, abs=1e-6)
    )
    assert scores["one-failure"]["role_reliability"]["failure_rate"] == (
        pytest.approx(2 / 61, abs=1e-6)
    )
    assert scores["failure-free"]["S_qual_clean"] > scores["cold-start"]["S_qual_clean"]
    assert scores["one-failure"]["S_qual_clean"] > scores["cold-start"]["S_qual_clean"]


def test_archived_config_without_reliability_policy_keeps_legacy_trace_shape() -> None:
    config = load_ranking_config()
    config["config_version"] = "step2-ranking-2026-08-02.2"
    config["penalties"].pop("latency_penalty_enabled")
    config.pop("role_reliability")
    unreliable = _with_role_reliability(
        _model("unreliable", capability=0.90),
        proposer=(0, 50),
        aggregator=(0, 50),
    )
    reliable = _with_role_reliability(_model("reliable", provider="provider-b", capability=0.80))

    decision = _decision(
        unreliable,
        reliable,
        analysis=_analysis(tier=1),
        ranking_config=config,
        user_profile_enabled=False,
    )

    assert decision.proposers[0].model_id == "unreliable"
    assert "role_reliability" not in decision.trace["model_scores"][0]
    assert "S_qual_before_reliability" not in decision.trace["model_scores"][0]
    assert "role_reliability" not in decision.trace["aggregator"]["selected"]
    assert "reliability_penalty" not in decision.trace["selection_steps"][0]


def test_role_reliability_is_isolated_between_proposer_and_aggregator() -> None:
    proposer_reliable = _with_role_reliability(
        _model("proposer-reliable", provider="provider-a", capability=0.80),
        proposer=(50, 0),
        aggregator=(0, 50),
    )
    aggregator_reliable = _with_role_reliability(
        _model("aggregator-reliable", provider="provider-b", capability=0.80),
        proposer=(0, 50),
        aggregator=(50, 0),
    )

    decision = _decision(
        proposer_reliable,
        aggregator_reliable,
        analysis=_analysis(tier=1),
        user_profile_enabled=False,
    )

    assert decision.proposers[0].model_id == "proposer-reliable"
    assert decision.aggregator.model_id == "aggregator-reliable"
    proposer_trace = next(
        row for row in decision.trace["model_scores"] if row["model"] == "proposer-reliable"
    )
    aggregator_trace = next(
        row
        for row in decision.trace["aggregator"]["scores"]
        if row["model"] == "aggregator-reliable"
    )
    assert proposer_trace["role_reliability"]["role"] == "proposer"
    assert proposer_trace["role_reliability"]["failure_rate"] == pytest.approx(
        1 / 61,
        abs=1e-6,
    )
    assert aggregator_trace["role_reliability"]["role"] == "aggregator"
    assert aggregator_trace["role_reliability"]["failure_rate"] == pytest.approx(
        1 / 61,
        abs=1e-6,
    )


def test_reliability_penalty_changes_initial_order_and_quality_floor() -> None:
    unreliable = _with_role_reliability(
        _model("unreliable", roles=["proposer"], capability=0.90),
        proposer=(0, 50),
    )
    reliable = _with_role_reliability(
        _model("reliable", provider="provider-b", roles=["proposer"], capability=0.70),
        proposer=(50, 0),
    )
    aggregator = _model("aggregator", provider="provider-c", roles=["aggregator"])
    no_penalty_config = load_ranking_config()
    no_penalty_config["role_reliability"]["penalty_weight"] = 0.0
    no_penalty_config["rerank"]["quality_floor_margin_by_risk"]["low"] = 0.10
    penalty_config = deepcopy(no_penalty_config)
    penalty_config["role_reliability"]["penalty_weight"] = 0.40

    baseline = _decision(
        unreliable,
        reliable,
        aggregator,
        analysis=_analysis(tier=1, risk="low"),
        ranking_config=no_penalty_config,
        user_profile_enabled=False,
    )
    penalized = _decision(
        unreliable,
        reliable,
        aggregator,
        analysis=_analysis(tier=1, risk="low"),
        ranking_config=penalty_config,
        user_profile_enabled=False,
    )

    assert baseline.proposers[0].model_id == "unreliable"
    assert penalized.proposers[0].model_id == "reliable"
    assert "test-provider:unreliable" in penalized.trace["quality_floor_excluded_ids"]
    unreliable_trace = next(
        row for row in penalized.trace["model_scores"] if row["model"] == "unreliable"
    )
    assert unreliable_trace["role_reliability"]["failure_rate"] == pytest.approx(51 / 61)
    assert unreliable_trace["role_reliability"]["penalty"] == pytest.approx(0.40 * 51 / 61)


def test_reliability_penalty_changes_greedy_marginal_selection() -> None:
    primary = _with_role_reliability(
        _model("primary", provider="provider-a", roles=["proposer"], capability=0.95)
    )
    unreliable = _with_role_reliability(
        _model("unreliable", provider="provider-b", roles=["proposer"], capability=0.90),
        proposer=(0, 50),
    )
    reliable = _with_role_reliability(
        _model("reliable", provider="provider-c", roles=["proposer"], capability=0.75)
    )
    aggregator = _model("aggregator", provider="provider-d", roles=["aggregator"])
    penalty_config = load_ranking_config()
    penalty_config["rerank"]["quality_floor_margin_by_risk"]["low"] = 1.0
    no_penalty_config = deepcopy(penalty_config)
    no_penalty_config["role_reliability"]["penalty_weight"] = 0.0

    baseline = _decision(
        primary,
        unreliable,
        reliable,
        aggregator,
        analysis=_analysis(tier=3, risk="low", latency="interactive"),
        ranking_config=no_penalty_config,
        user_profile_enabled=False,
    )
    penalized = _decision(
        primary,
        unreliable,
        reliable,
        aggregator,
        analysis=_analysis(tier=3, risk="low", latency="interactive"),
        ranking_config=penalty_config,
        user_profile_enabled=False,
    )

    assert [model.model_id for model in baseline.proposers] == ["primary", "unreliable"]
    assert [model.model_id for model in penalized.proposers] == ["primary", "reliable"]
    unstable_candidate = next(
        row
        for row in penalized.trace["selection_steps"][1]["top_candidates"]
        if row["identity"] == "provider-b:unreliable"
    )
    assert unstable_candidate["reliability_penalty"] == pytest.approx(0.40 * 51 / 61)


def test_reliability_penalty_orders_proposer_and_aggregator_fallbacks() -> None:
    primary = _with_role_reliability(
        _model("primary", provider="provider-p1", roles=["proposer"], capability=0.95)
    )
    unreliable_backup = _with_role_reliability(
        _model(
            "unreliable-backup",
            provider="provider-p2",
            roles=["proposer"],
            capability=0.90,
        ),
        proposer=(0, 50),
    )
    reliable_backup = _with_role_reliability(
        _model(
            "reliable-backup",
            provider="provider-p3",
            roles=["proposer"],
            capability=0.80,
        )
    )
    unreliable_aggregator = _with_role_reliability(
        _model("unreliable-a", provider="provider-a1", roles=["aggregator"]),
        aggregator=(0, 50),
    )
    mixed_aggregator = _with_role_reliability(
        _model("mixed-a", provider="provider-a2", roles=["aggregator"]),
        aggregator=(25, 25),
    )
    reliable_aggregator = _with_role_reliability(
        _model("reliable-a", provider="provider-a3", roles=["aggregator"])
    )
    config = load_ranking_config()
    config["rerank"]["quality_floor_margin_by_risk"]["low"] = 1.0

    decision = _decision(
        primary,
        unreliable_backup,
        reliable_backup,
        unreliable_aggregator,
        mixed_aggregator,
        reliable_aggregator,
        analysis=_analysis(tier=1, risk="low"),
        ranking_config=config,
        user_profile_enabled=False,
    )

    assert [model.model_id for model in decision.backup_proposers] == [
        "reliable-backup",
        "unreliable-backup",
    ]
    assert [model.model_id for model in decision.aggregator_candidates] == [
        "reliable-a",
        "mixed-a",
        "unreliable-a",
    ]
    assert [
        row["role_reliability"]["failure_rate"] for row in decision.trace["aggregator"]["scores"]
    ] == sorted(
        row["role_reliability"]["failure_rate"] for row in decision.trace["aggregator"]["scores"]
    )


def test_aggregator_is_ranked_after_proposers_with_full_context_need() -> None:
    proposer_a = _model("proposer-a", roles=["proposer"], capability=0.9)
    proposer_b = _model(
        "proposer-b",
        provider="provider-b",
        family="family-b",
        roles=["proposer"],
        capability=0.88,
    )
    short_aggregator = _model(
        "short-aggregator",
        roles=["aggregator"],
        context_window=4_500,
        capability=0.98,
        aggregator_fit=0.99,
    )
    long_aggregator = _model(
        "long-aggregator",
        roles=["aggregator"],
        context_window=20_000,
        capability=0.80,
        aggregator_fit=0.85,
    )

    decision = _decision(
        proposer_a,
        proposer_b,
        short_aggregator,
        long_aggregator,
        analysis=_analysis(tier=3, latency="interactive"),
        context=_context(input_tokens=1_000, candidate_tokens=2_000, aggregator_tokens=1_000),
    )

    assert len(decision.proposers) == 2
    assert decision.aggregator.model_id == "long-aggregator"
    short_filter = next(
        row
        for row in decision.trace["hard_filter"]["aggregator_results"]
        if row["model"] == "short-aggregator"
    )
    assert short_filter["context_need_tokens"] == 6_000
    assert "context_exceeded" in short_filter["reasons"]


def test_continue_applies_weak_stickiness_between_near_equal_models() -> None:
    first = _model("first", capability=0.80)
    previous = _model("previous", provider="provider-b", capability=0.79)
    context = _context(
        last_route={
            "selected_P": ["previous"],
            "selected_A": "previous",
            "quality_feedback": 1.0,
            "escalation_level": 0,
        }
    )

    decision = _decision(
        first,
        previous,
        analysis=_analysis(tier=1, intent="continue"),
        context=context,
    )

    assert decision.proposers[0].model_id == "previous"
    assert decision.trace["session"]["sticky_applied"] is True
    previous_score = next(
        row for row in decision.trace["model_scores"] if row["model"] == "previous"
    )
    assert previous_score["S_session"] == pytest.approx(0.1)


def test_low_confidence_continue_does_not_apply_stickiness() -> None:
    previous = _model("previous", capability=0.79)
    stronger = _model("stronger", provider="provider-b", capability=0.80)
    context = _context(
        last_route={
            "selected_P": ["previous"],
            "selected_A": "previous",
            "quality_feedback": 1.0,
            "escalation_level": 0,
        }
    )

    decision = _decision(
        previous,
        stronger,
        analysis=_analysis(tier=1, intent="continue", intent_confidence=0.4),
        context=context,
    )

    assert decision.proposers[0].model_id == "stronger"
    assert decision.trace["session"]["intent"] == "new_task"
    assert decision.trace["session"]["sticky_applied"] is False


def test_continue_without_a_previous_route_is_treated_as_a_new_task() -> None:
    decision = _decision(
        _model("first"),
        _model("second", provider="provider-b"),
        analysis=_analysis(tier=1, intent="continue"),
        context=_context(),
    )

    assert decision.trace["session"]["intent"] == "new_task"
    assert decision.trace["session"]["sticky_applied"] is False


def test_continue_does_not_claim_stickiness_when_previous_models_are_ineligible() -> None:
    context = _context(
        last_route={
            "selected_P": ["unavailable"],
            "selected_A": "unavailable",
            "quality_feedback": 1.0,
            "escalation_level": 0,
        }
    )

    decision = _decision(
        _model("available"),
        _model("unavailable", provider="provider-b", credential_available=False),
        analysis=_analysis(tier=1, intent="continue"),
        context=context,
    )

    assert decision.trace["session"]["intent"] == "continue"
    assert decision.trace["session"]["sticky_applied"] is False
    assert decision.trace["session"]["adjusted_model_ids"] == []


def test_session_adjustment_applies_to_aggregator_selection() -> None:
    proposer = _model("proposer", roles=["proposer"], capability=0.90)
    previous = _model(
        "previous-aggregator",
        provider="provider-b",
        roles=["aggregator"],
        capability=0.80,
        aggregator_fit=0.80,
    )
    alternative = _model(
        "alternative-aggregator",
        provider="provider-c",
        roles=["aggregator"],
        capability=0.82,
        aggregator_fit=0.82,
    )
    context = _context(
        last_route={
            "selected_P": ["proposer"],
            "selected_A": "previous-aggregator",
            "quality_feedback": 1.0,
            "escalation_level": 0,
        }
    )

    continued = _decision(
        proposer,
        previous,
        alternative,
        analysis=_analysis(tier=1, intent="continue"),
        context=context,
    )
    redone = _decision(
        proposer,
        previous,
        alternative,
        analysis=_analysis(tier=1, intent="redo"),
        context=context,
    )

    assert continued.aggregator.model_id == "previous-aggregator"
    continued_score = next(
        row
        for row in continued.trace["aggregator"]["scores"]
        if row["model"] == "previous-aggregator"
    )
    assert continued_score["S_session"] == pytest.approx(0.1)
    assert redone.aggregator.model_id == "alternative-aggregator"


def test_redo_demotes_previous_models_and_shifts_tier_once() -> None:
    previous = _model("previous", capability=0.86)
    alternative = _model("alternative", provider="provider-b", capability=0.84)
    context = _context(
        last_route={
            "selected_P": ["previous"],
            "selected_A": "previous",
            "quality_feedback": 0.2,
            "escalation_level": 0,
        }
    )

    decision = _decision(
        previous,
        alternative,
        analysis=_analysis(tier=2, intent="redo"),
        context=context,
    )

    assert decision.effective_tier == 3
    assert decision.trace["session"]["tier_shifted"] is True
    assert decision.trace["session"]["escalation_level"] == 1
    assert decision.trace["task_profile_pre_escalation"]["tier_dist"] == {"2": 1.0}
    assert decision.trace["task_profile_post_escalation"]["tier_dist"] == {"3": 1.0}
    previous_score = next(
        row for row in decision.trace["model_scores"] if row["model"] == "previous"
    )
    assert previous_score["S_session"] == pytest.approx(-0.1)
    assert decision.proposers[0].model_id == "alternative"


def test_redo_stops_escalating_at_the_configured_ceiling() -> None:
    context = _context(
        last_route={
            "selected_P": ["previous"],
            "selected_A": "previous",
            "quality_feedback": 0.1,
            "escalation_level": 2,
        }
    )

    decision = _decision(
        _model("previous"),
        _model("alternative", provider="provider-b"),
        analysis=_analysis(tier=3, intent="redo"),
        context=context,
    )

    assert decision.effective_tier == 3
    assert decision.trace["session"]["tier_shifted"] is False
    assert decision.trace["session"]["escalation_level"] == 2


def test_explicit_proposer_recovery_quorum_one_preserves_ranked_selection() -> None:
    models = (
        _model("primary", provider="provider-a", capability=0.90),
        _model("secondary", provider="provider-b", capability=0.85),
    )
    baseline = _decision(
        *models,
        analysis=_analysis(tier=3, latency="interactive"),
        user_profile_enabled=False,
    )
    explicit = _decision(
        *models,
        analysis=_analysis(tier=3, latency="interactive"),
        proposer_recovery_quorum=1,
        user_profile_enabled=False,
    )

    assert explicit.trace["selected_P"] == baseline.trace["selected_P"]
    assert explicit.trace["N_min"] == baseline.trace["N_min"] == 2
    assert explicit.trace["N_max"] == baseline.trace["N_max"] == 2
    assert explicit.trace["proposer_recovery_policy"]["quorum_required"] == 1
    assert ranking_trace_replay_reasons(explicit.trace) == []


def test_explicit_proposer_recovery_quorum_one_allows_original_bound_shortfall() -> None:
    decision = _decision(
        _model("only"),
        analysis=_analysis(tier=3),
        proposer_recovery_quorum=1,
    )

    assert decision.trace["N_min"] == 2
    assert len(decision.proposers) == 1
    assert decision.trace["coverage_shortfall"] is True
    assert decision.trace["proposer_recovery_policy"]["quorum_required"] == 1


def test_explicit_proposer_recovery_quorum_lifts_c0_selection_bounds() -> None:
    decision = _decision(
        _model("primary", provider="provider-a", capability=0.90),
        _model("secondary", provider="provider-b", capability=0.85),
        analysis=_analysis(tier=1),
        proposer_recovery_quorum=2,
        user_profile_enabled=False,
    )

    assert decision.trace["N_min"] == 2
    assert decision.trace["N_max"] == 2
    assert len(decision.proposers) == 2
    assert decision.trace["coverage_shortfall"] is False
    assert decision.trace["proposer_recovery_policy"]["quorum_required"] == 2
    assert ranking_trace_replay_reasons(decision.trace) == []


def test_explicit_proposer_recovery_quorum_allows_global_ceiling() -> None:
    ranking_config = load_ranking_config()
    ceiling = ranking_router._proposer_global_ceiling(ranking_config)
    models = tuple(
        _model(
            f"model-{index}",
            provider=f"provider-{index}",
            capability=0.90,
        )
        for index in range(ceiling)
    )

    decision = _decision(
        *models,
        analysis=_analysis(tier=1),
        ranking_config=ranking_config,
        proposer_recovery_quorum=ceiling,
        user_profile_enabled=False,
    )

    assert ceiling == 5
    assert decision.trace["N_min"] == ceiling
    assert decision.trace["N_max"] == ceiling
    assert len(decision.proposers) == ceiling
    assert decision.trace["proposer_recovery_policy"]["quorum_required"] == ceiling
    assert ranking_trace_replay_reasons(decision.trace) == []


def test_explicit_proposer_recovery_quorum_above_global_ceiling_fails_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ranking_config = load_ranking_config()
    ceiling = ranking_router._proposer_global_ceiling(ranking_config)

    def unexpected_normalization(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("registry normalization must not start")

    monkeypatch.setattr(ranking_router, "_normalize_model", unexpected_normalization)
    with pytest.raises(
        DynamicRankingError,
        match=(
            f"configured proposer recovery quorum {ceiling + 1} "
            f"exceeds the global proposer ceiling {ceiling}"
        ),
    ) as exc_info:
        _decision(
            _model("unused"),
            analysis=_analysis(tier=1),
            ranking_config=ranking_config,
            proposer_recovery_quorum=ceiling + 1,
        )

    assert exc_info.value.reason == "proposer_recovery_quorum_unreachable"


@pytest.mark.parametrize(
    ("models", "context", "message"),
    [
        pytest.param(
            (
                _model("eligible"),
                _model("filtered", credential_available=False),
            ),
            _context(),
            "hard filtering left 1 eligible",
            id="hard-filter",
        ),
        pytest.param(
            (
                _model("strong", capability=0.90),
                _model("weak", provider="provider-b", capability=0.10),
            ),
            _context(),
            "quality floor left 1 eligible",
            id="quality-floor",
        ),
        pytest.param(
            (
                _model("proposer-a", provider="provider-a", roles=["proposer"]),
                _model("proposer-b", provider="provider-b", roles=["proposer"]),
                _model(
                    "aggregator",
                    provider="provider-c",
                    roles=["aggregator"],
                    context_window=3_500,
                ),
            ),
            _context(input_tokens=1_000, candidate_tokens=1_000, aggregator_tokens=1_000),
            "only 1 have a feasible aggregator .*stop_reason=aggregator_infeasible",
            id="aggregator-feasibility",
        ),
    ],
)
def test_explicit_proposer_recovery_quorum_unreachable_fails_closed(
    models: tuple[dict[str, Any], ...],
    context: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(DynamicRankingError, match=message) as exc_info:
        _decision(
            *models,
            analysis=_analysis(tier=1, risk="low"),
            context=context,
            proposer_recovery_quorum=2,
        )

    assert exc_info.value.reason == "proposer_recovery_quorum_unreachable"


def test_high_risk_shortfall_is_recorded_without_violating_filters() -> None:
    decision = _decision(
        _model("one", provider="provider-a"),
        _model("two", provider="provider-b"),
        analysis=_analysis(tier=4, risk="high"),
        proposer_recovery_quorum=None,
    )

    assert decision.trace["N_min"] == 4
    assert len(decision.proposers) == 2
    assert decision.trace["coverage_shortfall"] is True
    assert decision.trace["stop_reason"] == "candidate_pool_exhausted"
    assert decision.trace["proposer_recovery_policy"]["quorum_required"] == 2


def test_no_feasible_aggregator_fails_with_explicit_error() -> None:
    with pytest.raises(DynamicRankingError, match="feasible aggregator"):
        _decision(
            _model("proposer", roles=["proposer"]),
            analysis=_analysis(tier=1),
        )


def test_duplicate_registry_identity_is_rejected_before_scoring() -> None:
    duplicate = _model("Vendor/Duplicate")
    duplicate_case_variant = _model("vendor/duplicate")

    with pytest.raises(DynamicRankingError, match="duplicate model identities"):
        _decision(duplicate, duplicate_case_variant, analysis=_analysis(tier=1))


def test_malformed_registry_row_is_not_silently_dropped() -> None:
    with pytest.raises(DynamicRankingError, match="malformed model row"):
        rank_models(
            task_analysis=_analysis(tier=1),
            user_profile=mock_user_profile(),
            request_context=_context(),
            registry_snapshot={
                "snapshot_version": "test",
                "models": [_model("valid"), "not-a-model"],
            },
            routed_tier="c0",
            routing_confidence=1.0,
        )


def test_ranking_is_deterministic_for_the_same_snapshot() -> None:
    models = (
        _model("a", provider="provider-a"),
        _model("b", provider="provider-b"),
        _model("c", provider="provider-c"),
    )

    first = _decision(*models, analysis=_analysis(tier=3))
    second = _decision(*models, analysis=_analysis(tier=3))

    assert [model.identity for model in first.proposers] == [
        model.identity for model in second.proposers
    ]
    assert first.aggregator.identity == second.aggregator.identity
    assert first.trace["selection_steps"] == second.trace["selection_steps"]
    assert first.trace["registry_snapshot_hash"] == second.trace["registry_snapshot_hash"]
    assert len(first.trace["registry_snapshot_hash"]) == 64
    assert all(len(row["profile_hash"]) == 64 for row in first.trace["candidate_pool"])
    assert all(
        type(row["is_open_source"]) is bool and type(row["is_chinese_model"]) is bool
        for row in first.trace["candidate_pool"]
    )


def test_ranking_emits_the_required_debug_lifecycle_events() -> None:
    with structlog.testing.capture_logs() as captured:
        rank_models(
            task_analysis=_analysis(tier=2),
            user_profile=mock_user_profile(),
            request_context=_context(),
            registry_snapshot=_snapshot(
                _model("a", provider="provider-a"),
                _model("b", provider="provider-b"),
            ),
            routed_tier="c1",
            routing_confidence=0.9,
            decision_id="ranking-log-decision",
        )

    event_names = {row["event"] for row in captured}
    assert {
        "llm_ensemble.router_dynamic.candidate_pool_recorded",
        "llm_ensemble.router_dynamic.model_scores_recorded",
        "llm_ensemble.router_dynamic.proposer_selection_recorded",
        "llm_ensemble.router_dynamic.aggregator_selection_recorded",
        "llm_ensemble.router_dynamic.router_decision_recorded",
    }.issubset(event_names)
    lifecycle = [
        row for row in captured if str(row["event"]).startswith("llm_ensemble.router_dynamic.")
    ]
    assert all(row["decision_id"] == "ranking-log-decision" for row in lifecycle)


def test_enabled_thinking_assignment_emits_dedicated_router_event() -> None:
    with structlog.testing.capture_logs() as captured:
        rank_models(
            task_analysis=_analysis(tier=3),
            user_profile=mock_user_profile(),
            request_context=_context(),
            registry_snapshot=_snapshot(
                _thinking_model("a", provider="provider-a"),
                _thinking_model("b", provider="provider-b"),
            ),
            routed_tier="c2",
            routing_confidence=0.9,
            decision_id="thinking-log-decision",
            ranking_thinking_assignment_enabled=True,
        )

    assignment_event = next(
        row
        for row in captured
        if row["event"] == "llm_ensemble.router_dynamic.thinking_assignment_recorded"
    )
    assert assignment_event["decision_id"] == "thinking-log-decision"
    assert assignment_event["thinking_assignment"]["proposers"]
    assert assignment_event["thinking_assignment"]["aggregator"]
    assert assignment_event["policy_versions"]["thinking"] == ("thinking-policy-v1")


def test_thinking_assignment_is_default_off_and_selection_is_unchanged() -> None:
    models = (
        _thinking_model("alpha", provider="provider-a", capability=0.95),
        _thinking_model("beta", provider="provider-b", capability=0.90),
        _thinking_model("gamma", provider="provider-c", capability=0.85),
    )

    disabled = _decision(*models, analysis=_analysis(tier=3))
    enabled = _decision(
        *models,
        analysis=_analysis(tier=3),
        thinking_assignment_enabled=True,
    )

    assert "ranking_thinking_assignment_enabled" not in disabled.trace
    assert "thinking_assignment" not in disabled.trace
    assert "assignment_reasons" not in disabled.trace
    assert all(model.requested_thinking_level is None for model in disabled.proposers)
    assert disabled.aggregator.requested_thinking_level is None
    assert [model.identity for model in disabled.proposers] == [
        model.identity for model in enabled.proposers
    ]
    assert disabled.aggregator.identity == enabled.aggregator.identity
    assert disabled.trace["model_scores"] == enabled.trace["model_scores"]
    assert disabled.trace["selection_steps"] == enabled.trace["selection_steps"]


def test_disabled_thinking_assignment_preserves_exact_legacy_trace_shape() -> None:
    current_config = load_ranking_config()
    current_snapshot = {
        "schema_version": "step2-model-registry-v2",
        "snapshot_version": "curated-openrouter-step2-2026-07-27.1",
        "models": [
            _thinking_model("alpha", provider="provider-a", capability=0.95),
            _thinking_model("beta", provider="provider-b", capability=0.90),
            _thinking_model("gamma", provider="provider-c", capability=0.85),
        ],
    }
    legacy_config = ranking_router._legacy_ranking_config_projection(current_config)
    legacy_snapshot = ranking_router._legacy_registry_snapshot_projection(current_snapshot)
    common = {
        "task_analysis": _analysis(tier=3),
        "user_profile": mock_user_profile(),
        "request_context": _context(),
        "routed_tier": "c2",
        "routing_confidence": 0.9,
        "decision_id": "legacy-shape",
    }

    disabled = rank_models(
        **common,
        registry_snapshot=current_snapshot,
        ranking_config=current_config,
    )
    legacy = rank_models(
        **common,
        registry_snapshot=legacy_snapshot,
        ranking_config=legacy_config,
    )

    assert disabled.trace == legacy.trace
    assert disabled.trace["ranking_version"] == "step2-ranking-v2"
    assert (
        disabled.trace["ranking_config_hash"]
        == "268ebb0c002994a9434eeecaef6b76571d0bd803db6eb2e7c8a788f3e2210e96"
    )
    for field in (
        "ranking_thinking_assignment_enabled",
        "thinking_policy_version",
        "thinking_assignment",
        "thinking_assignment_details",
        "assignment_reasons",
        "unsupported_level_fallbacks",
        "policy_versions",
    ):
        assert field not in disabled.trace
    assert all(
        "thinking_levels" not in row and "thinking_level_mapping" not in row
        for row in disabled.trace["candidate_pool"]
    )


def test_default_off_keeps_request_filters_off_but_honors_retry_exclusions() -> None:
    models = [
        _thinking_model("alpha", provider="provider-a", capability=0.95),
        _thinking_model("beta", provider="provider-b", capability=0.90),
        _thinking_model("gamma", provider="provider-c", capability=0.85),
    ]
    models[0]["registry_facts"].update(
        {
            "supports_tools": False,
            "retry_excluded_proposer": True,
        }
    )
    for model in models[1:]:
        model["registry_facts"]["supports_tools"] = True
    context = {
        **_context(),
        "required_parameters_by_role": {
            "proposer": ["tools"],
            "aggregator": ["tools"],
        },
    }

    disabled = _decision(
        *deepcopy(models),
        analysis=_analysis(tier=3),
        context=context,
        thinking_assignment_enabled=False,
    )
    enabled = _decision(
        *deepcopy(models),
        analysis=_analysis(tier=3),
        context=context,
        thinking_assignment_enabled=True,
    )
    disabled_alpha = [
        row
        for role in ("proposer_results", "aggregator_results")
        for row in disabled.trace["hard_filter"][role]
        if row["model"] == "alpha"
    ]
    enabled_alpha = [
        row
        for role in ("proposer_results", "aggregator_results")
        for row in enabled.trace["hard_filter"][role]
        if row["model"] == "alpha"
    ]

    assert all(
        "required_parameter_tools_unsupported" not in row["reasons"] for row in disabled_alpha
    )
    assert any(
        "prior_attempt_reasoning_only_length" in row["reasons"]
        for row in disabled_alpha
        if row["role"] == "proposer"
    )
    assert all(
        "prior_attempt_reasoning_only_length" not in row["reasons"]
        for row in disabled_alpha
        if row["role"] == "aggregator"
    )
    assert any("required_parameter_tools_unsupported" in row["reasons"] for row in enabled_alpha)
    assert any(
        "prior_attempt_reasoning_only_length" in row["reasons"]
        for row in enabled_alpha
        if row["role"] == "proposer"
    )


def test_legacy_v3_ranking_config_remains_usable_only_when_assignment_is_off() -> None:
    legacy = load_ranking_config()
    legacy["schema_version"] = "step2-ranking-config-v3"
    legacy["config_version"] = "step2-ranking-legacy-test"
    legacy.pop("thinking_assignment")
    models = (
        _thinking_model("alpha", provider="provider-a"),
        _thinking_model("beta", provider="provider-b"),
    )

    decision = _decision(
        *models,
        analysis=_analysis(tier=1),
        ranking_config=legacy,
    )

    assert decision.trace["ranking_config_schema_version"] == "step2-ranking-config-v3"
    with pytest.raises(DynamicRankingError, match="requires step2-ranking-config-v4"):
        _decision(
            *models,
            analysis=_analysis(tier=1),
            ranking_config=legacy,
            thinking_assignment_enabled=True,
        )


@pytest.mark.parametrize(
    ("tier", "proposer_level", "aggregator_level"),
    [
        (1, "low", "medium"),
        (2, "medium", "high"),
        (3, "high", "highest"),
        (4, "highest", "highest"),
    ],
)
def test_thinking_policy_maps_tiers_and_aggregator_step(
    tier: int,
    proposer_level: str,
    aggregator_level: str,
) -> None:
    policy = ranking_router._thinking_assignment_policy(load_ranking_config())
    profile = _task_profile(tier=tier)

    proposer, _, _ = ranking_router._thinking_target_for_role(
        role="proposer",
        effective_tier=tier,
        task_profile=profile,
        session_trace={"intent": "new_task"},
        policy=policy,
    )
    aggregator, _, _ = ranking_router._thinking_target_for_role(
        role="aggregator",
        effective_tier=tier,
        task_profile=profile,
        session_trace={"intent": "new_task"},
        policy=policy,
    )

    assert proposer == proposer_level
    assert aggregator == aggregator_level


@pytest.mark.parametrize(
    ("tier_dist", "expected"),
    [
        ({"1": 0.51, "2": 0.49}, 1),
        ({"1": 0.50, "2": 0.50}, 2),
    ],
)
def test_effective_tier_uses_half_up_rounding(
    tier_dist: dict[str, float],
    expected: int,
) -> None:
    profile = _task_profile(tier=1)
    profile["tier_dist"] = tier_dist

    assert ranking_router._effective_tier(profile, load_ranking_config()) == expected


def test_enabled_thinking_policy_rejects_non_half_up_tier_rounding() -> None:
    config = load_ranking_config()
    config["proposer_count"]["effective_tier_rounding_offset"] = 0.25
    models = (
        _thinking_model("alpha", provider="provider-a"),
        _thinking_model("beta", provider="provider-b"),
    )

    disabled = _decision(*models, ranking_config=config)
    assert disabled.trace["ranking_version"] == "step2-ranking-v2"
    with pytest.raises(
        DynamicRankingError,
        match="effective_tier_rounding_offset to be 0.5",
    ):
        _decision(
            *models,
            ranking_config=config,
            thinking_assignment_enabled=True,
        )


def test_thinking_policy_applies_risk_floor_before_single_resource_downshift() -> None:
    policy = ranking_router._thinking_assignment_policy(load_ranking_config())
    both_constrained = _task_profile(
        tier=4,
        cost="hard_limit",
        latency="interactive",
    )
    high_risk = _task_profile(
        tier=2,
        risk="high",
        cost="low",
        latency="hard_timeout",
    )

    constrained_level, constrained_reasons, _ = ranking_router._thinking_target_for_role(
        role="proposer",
        effective_tier=4,
        task_profile=both_constrained,
        session_trace={"intent": "new_task"},
        policy=policy,
    )
    risk_level, risk_reasons, risk_floor = ranking_router._thinking_target_for_role(
        role="proposer",
        effective_tier=2,
        task_profile=high_risk,
        session_trace={"intent": "new_task"},
        policy=policy,
    )

    assert constrained_level == "high"
    assert sum("resource_" in reason for reason in constrained_reasons) == 1
    assert risk_floor == "high"
    assert risk_level == "high"
    assert any("risk_high_floor_high" in reason for reason in risk_reasons)
    assert any("downshift_blocked" in reason for reason in risk_reasons)


def test_redo_does_not_apply_a_second_thinking_level_shift() -> None:
    policy = ranking_router._thinking_assignment_policy(load_ranking_config())
    profile = _task_profile(tier=3, intent="redo")

    new_level, _, _ = ranking_router._thinking_target_for_role(
        role="proposer",
        effective_tier=3,
        task_profile=profile,
        session_trace={"intent": "new_task"},
        policy=policy,
    )
    redo_level, redo_reasons, _ = ranking_router._thinking_target_for_role(
        role="proposer",
        effective_tier=3,
        task_profile=profile,
        session_trace={"intent": "redo"},
        policy=policy,
    )

    assert redo_level == new_level == "high"
    assert "redo_uses_session_adjusted_tier_only" in redo_reasons


def test_unsupported_thinking_level_uses_deterministic_nearest_tie_breaks() -> None:
    policy = ranking_router._thinking_assignment_policy(load_ranking_config())
    model = ranking_router._normalize_model(
        _thinking_model(
            "partial",
            thinking_levels=["low", "high"],
            thinking_level_mapping={"low": "low", "high": "high"},
        ),
        load_ranking_config(),
        thinking_policy=policy,
    )

    normal, normal_detail, _ = ranking_router._resolve_model_thinking_level(
        model,
        role="proposer",
        requested_level="medium",
        reasons=[],
        risk_floor=None,
        policy=policy,
    )
    high_risk, high_risk_detail, _ = ranking_router._resolve_model_thinking_level(
        model,
        role="proposer",
        requested_level="medium",
        reasons=[],
        risk_floor="low",
        policy=policy,
    )

    assert normal.effective_thinking_level == "low"
    assert normal_detail["fallback_reason"].endswith("_lower")
    assert high_risk.effective_thinking_level == "high"
    assert high_risk_detail["fallback_reason"].endswith("_higher")


def test_provider_rejection_fallbacks_recompute_nearest_remaining_level() -> None:
    policy = ranking_router._thinking_assignment_policy(load_ranking_config())
    model = ranking_router._normalize_model(
        _thinking_model("all-levels"),
        load_ranking_config(),
        thinking_policy=policy,
    )

    assigned, detail, _ = ranking_router._resolve_model_thinking_level(
        model,
        role="proposer",
        requested_level="high",
        reasons=[],
        risk_floor=None,
        policy=policy,
    )

    assert assigned.effective_thinking_level == "high"
    assert [row["unified_level"] for row in assigned.thinking_fallbacks] == [
        "medium",
        "low",
        "highest",
    ]
    assert [row["unified_level"] for row in detail["provider_rejection_fallbacks"]] == [
        "medium",
        "low",
        "highest",
    ]


def test_normal_risk_partial_thinking_support_falls_back_without_hard_filter() -> None:
    decision = _decision(
        _thinking_model(
            "partial",
            thinking_levels=["low"],
            thinking_level_mapping={"low": "low"},
        ),
        analysis=_analysis(tier=1, risk="medium"),
        thinking_assignment_enabled=True,
    )

    assert decision.proposers[0].effective_thinking_level == "low"
    assert decision.aggregator.effective_thinking_level == "low"
    assert decision.trace["unsupported_level_fallbacks"]
    assert (
        decision.trace["hard_filter"]["filter_reason_counts"].get("thinking_level_unavailable")
        is None
    )


def test_high_risk_requires_at_least_high_thinking_support() -> None:
    with pytest.raises(
        DynamicRankingError,
        match="thinking_level_unavailable",
    ) as caught:
        _decision(
            _thinking_model(
                "partial",
                thinking_levels=["low", "medium"],
                thinking_level_mapping={"low": "low", "medium": "medium"},
            ),
            analysis=_analysis(tier=3, risk="high"),
            thinking_assignment_enabled=True,
        )
    assert caught.value.reason == "thinking_level_unavailable"


@pytest.mark.parametrize(
    "mapping",
    [
        {"low": "off"},
        {"low": "high"},
        {"highest": "high"},
    ],
)
def test_registry_v2_rejects_semantically_invalid_thinking_mapping(
    mapping: dict[str, str],
) -> None:
    model = _thinking_model(
        "invalid",
        thinking_levels=list(mapping),
        thinking_level_mapping=mapping,
    )
    model["registry_facts"]["supported_thinking_levels"] = sorted(set(mapping.values()))

    with pytest.raises(
        DynamicRankingError,
        match="no enabled supported_thinking_levels|semantically invalid",
    ):
        ranking_router._validate_registry_snapshot(
            {
                "schema_version": "step2-model-registry-v2",
                "snapshot_version": "invalid-test",
                "models": [model],
            }
        )


def test_registry_v2_requires_explicit_thinking_contract_fields() -> None:
    with pytest.raises(DynamicRankingError, match="requires thinking_levels"):
        ranking_router._validate_registry_snapshot(
            {
                "schema_version": "step2-model-registry-v2",
                "snapshot_version": "missing-test",
                "models": [_model("missing")],
            }
        )


def test_enabled_assignment_is_complete_auditable_and_role_specific() -> None:
    decision = _decision(
        _thinking_model("alpha", provider="provider-a", capability=0.95),
        _thinking_model("beta", provider="provider-b", capability=0.90),
        _thinking_model("gamma", provider="provider-c", capability=0.85),
        analysis=_analysis(tier=3),
        thinking_assignment_enabled=True,
    )
    assignment = decision.trace["thinking_assignment"]

    assert assignment == {
        "proposers": {model.identity: "high" for model in decision.proposers},
        "aggregator": "highest",
        "thinking_policy_version": "thinking-policy-v1",
    }
    assert decision.aggregator.effective_thinking_level == "highest"
    assert all(model.effective_thinking_level == "high" for model in decision.proposers)
    assert decision.trace["assignment_reasons"]["proposers"]
    assert decision.trace["assignment_reasons"]["aggregator"]
    assert decision.trace["policy_versions"] == {
        "ranking": "step2-ranking-v4",
        "thinking": "thinking-policy-v1",
    }


def test_enabled_thinking_assignment_is_replayable_and_tamper_evident() -> None:
    decision = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _thinking_model("alpha", provider="provider-a", capability=0.95),
            _thinking_model("beta", provider="provider-b", capability=0.90),
            _thinking_model("gamma", provider="provider-c", capability=0.85),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="thinking-replay-decision",
        ranking_thinking_assignment_enabled=True,
    )
    trace = decision.trace

    assert ranking_trace_replay_reasons(trace) == []
    tampered = json.loads(json.dumps(trace))
    tampered["thinking_assignment"]["aggregator"] = "low"
    assert "g1_frozen_ranker_replay_mismatch_thinking_assignment" in ranking_trace_replay_reasons(
        tampered
    )


def test_ranker_freezes_ordered_disjoint_proposer_backups_and_replays_exactly() -> None:
    models = [
        _thinking_model(
            f"model-{index}",
            provider=f"provider-{index}",
            capability=0.95,
        )
        for index in range(9)
    ]
    decision = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(*models),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="backup-replay-decision",
        ranking_thinking_assignment_enabled=True,
        proposer_recovery_max_additional_calls=3,
        proposer_max_tokens_cap=65_536,
        proposer_visible_answer_reserve_tokens=4_096,
        proposer_recovery_quorum=2,
    )

    selected = {model.identity for model in decision.proposers}
    aggregators = {model.identity for model in decision.aggregator_candidates}
    backups = [model.identity for model in decision.backup_proposers]
    assert len(backups) == 2
    assert len(set(backups)) == 2
    assert not selected.intersection(backups)
    assert not aggregators.intersection(backups)
    assert decision.trace["backup_P"] == backups
    assert decision.trace["proposer_recovery_policy"] == {
        "schema": "opensquilla.router-dynamic-proposer-recovery/v1",
        "configured_backup_count": 2,
        "effective_backup_count": 2,
        "max_additional_physical_requests": 3,
        "quorum_required": 2,
        "max_tokens_cap": 65_536,
        "visible_answer_reserve_tokens": 4_096,
        "thinking_downgrade_order": ["one_strictly_lower"],
        "transient_same_model_retries": 1,
        "backup_reasoning_downgrades": 1,
    }
    assert ranking_trace_replay_reasons(decision.trace) == []

    tampered = deepcopy(decision.trace)
    tampered["proposer_recovery_policy"]["configured_backup_count"] = 1
    assert (
        "g1_frozen_ranker_replay_mismatch_proposer_recovery_policy"
        in ranking_trace_replay_reasons(tampered)
    )


def test_ranker_roster_counts_are_owned_by_effective_ranking_config() -> None:
    models = [
        _thinking_model(
            f"roster-{index}",
            provider=f"provider-{index}",
            capability=0.99 - index * 0.02,
        )
        for index in range(9)
    ]
    ranking_config = ranking_config_snapshot(
        thinking_assignment_enabled=True,
        override={
            "proposer_count": {"backup_count": 1},
            "aggregator": {"candidate_count": 2},
        },
    )

    decision = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(*models),
        routed_tier="c2",
        routing_confidence=0.91,
        ranking_config=ranking_config,
        decision_id="ranking-owned-roster",
        ranking_thinking_assignment_enabled=True,
        proposer_recovery_max_additional_calls=3,
        proposer_recovery_quorum=2,
    )

    assert len(decision.backup_proposers) == 1
    assert len(decision.aggregator_candidates) == 2
    assert decision.trace["configured_proposer_backup_count"] == 1
    assert decision.trace["effective_proposer_backup_count"] == 1
    assert decision.trace["configured_aggregator_candidate_count"] == 2
    assert decision.trace["effective_aggregator_candidate_count"] == 2
    assert ranking_trace_replay_reasons(decision.trace) == []

    tampered = deepcopy(decision.trace)
    tampered["configured_aggregator_candidate_count"] = 3
    assert (
        "g1_frozen_ranker_replay_mismatch_configured_aggregator_candidate_count"
        in ranking_trace_replay_reasons(tampered)
    )


def test_pre_roster_trace_replay_keeps_archived_gateway_backup_count() -> None:
    ranking_config = load_ranking_config()
    ranking_config["config_version"] = "step2-ranking-2026-07-27.1"
    ranking_config["penalties"].pop("latency_penalty_enabled")
    ranking_config["proposer_count"].pop("backup_count")
    ranking_config["aggregator"].pop("candidate_count")
    decision = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            *[
                _thinking_model(
                    f"archived-{index}",
                    provider=f"provider-{index}",
                    capability=0.99 - index * 0.03,
                )
                for index in range(9)
            ]
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        ranking_config=ranking_config,
        decision_id="pre-roster-replay",
        ranking_thinking_assignment_enabled=True,
        legacy_proposer_backup_count=1,
    )
    archived_trace = deepcopy(decision.trace)
    archived_trace.pop("configured_aggregator_candidate_count")
    archived_trace.pop("effective_aggregator_candidate_count")

    assert archived_trace["configured_proposer_backup_count"] == 1
    assert ranking_trace_replay_reasons(archived_trace) == []


def test_ranker_legacy_replay_allows_no_recovery_projection_but_rejects_partial() -> None:
    decision = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            *[
                _thinking_model(
                    f"legacy-{index}",
                    provider=f"provider-{index}",
                    capability=0.99 - index * 0.04,
                )
                for index in range(8)
            ]
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="legacy-backup-replay",
        ranking_thinking_assignment_enabled=True,
    )
    legacy = deepcopy(decision.trace)
    for field_name in (
        "backup_P",
        "configured_proposer_backup_count",
        "effective_proposer_backup_count",
        "proposer_recovery_policy",
    ):
        legacy.pop(field_name, None)
    assert ranking_trace_replay_reasons(legacy) == []

    partial = deepcopy(legacy)
    partial["backup_P"] = []
    assert "incomplete_g1_replay_proposer_recovery_policy" in (
        ranking_trace_replay_reasons(partial)
    )


def test_enabled_thinking_assignment_replay_rejects_switch_downgrade() -> None:
    decision = rank_models(
        task_analysis=_analysis(tier=3),
        user_profile=None,
        request_context=_context(),
        registry_snapshot=_snapshot(
            _thinking_model("alpha", provider="provider-a", capability=0.95),
            _thinking_model("beta", provider="provider-b", capability=0.90),
            _thinking_model("gamma", provider="provider-c", capability=0.85),
        ),
        routed_tier="c2",
        routing_confidence=0.91,
        decision_id="thinking-replay-downgrade",
        ranking_thinking_assignment_enabled=True,
    )
    tampered = json.loads(json.dumps(decision.trace))
    tampered.pop("ranking_thinking_assignment_enabled")
    for field_name in (
        "thinking_policy_version",
        "thinking_assignment",
        "thinking_assignment_details",
        "assignment_reasons",
        "unsupported_level_fallbacks",
        "policy_versions",
    ):
        tampered.pop(field_name)

    assert "missing_g1_replay_thinking_assignment_switch" in ranking_trace_replay_reasons(tampered)


def test_registry_builder_never_reuses_native_mapping_across_providers() -> None:
    openrouter_template = _thinking_model(
        "vendor/shared-model",
        provider="openrouter",
    )

    snapshot = build_model_registry_snapshot(
        inherited_provider="direct-provider",
        inherited_model="vendor/shared-model",
        routed_tier="c2",
        packaged_snapshot={
            "schema_version": "step2-model-registry-v2",
            "snapshot_version": "provider-isolation-test",
            "models": [openrouter_template],
        },
    )
    anchor = snapshot["models"][0]

    assert anchor["registry_facts"]["provider"] == "direct-provider"
    assert anchor["registry_facts"]["thinking_levels"] == []
    assert anchor["registry_facts"]["thinking_level_mapping"] == {}
    assert "supported_thinking_levels" not in anchor["registry_facts"]
    assert anchor["static_profile"] == openrouter_template["static_profile"]
    ranking_router._validate_registry_snapshot(snapshot)


def test_packaged_registry_v2_has_valid_unified_thinking_contracts() -> None:
    snapshot = load_model_registry_snapshot()

    assert snapshot["schema_version"] == "step2-model-registry-v2"
    for row in snapshot["models"]:
        facts = row["registry_facts"]
        levels = facts["thinking_levels"]
        mapping = facts["thinking_level_mapping"]
        assert set(mapping) == set(levels)
        assert all(level in {"low", "medium", "high", "highest"} for level in levels)
        assert mapping.get("highest") != "high"


def test_aggregator_recovery_candidates_preserve_frozen_top_three_order() -> None:
    decision = _decision(
        _model(
            "proposer-only",
            roles=["proposer"],
            capability=0.95,
            aggregator_fit=0.1,
        ),
        _model(
            "aggregator-alpha",
            roles=["aggregator"],
            capability=0.92,
            aggregator_fit=0.99,
            price=1.0,
        ),
        _model(
            "aggregator-beta",
            roles=["aggregator"],
            capability=0.90,
            aggregator_fit=0.96,
            price=1.1,
        ),
        _model(
            "aggregator-gamma",
            roles=["aggregator"],
            capability=0.88,
            aggregator_fit=0.93,
            price=1.2,
        ),
        _model(
            "aggregator-delta",
            roles=["aggregator"],
            capability=0.86,
            aggregator_fit=0.90,
            price=1.3,
        ),
        user_profile=None,
    )

    ranked_identities = [row["identity"] for row in decision.trace["aggregator"]["scores"]]
    candidate_identities = [model.identity for model in decision.aggregator_candidates]

    assert len(candidate_identities) == 3
    assert candidate_identities == ranked_identities[:3]
    assert candidate_identities[0] == decision.aggregator.identity
    assert len(set(candidate_identities)) == 3


def test_aggregator_recovery_candidates_do_not_pad_a_small_eligible_pool() -> None:
    decision = _decision(
        _model(
            "proposer-only",
            roles=["proposer"],
            capability=0.95,
            aggregator_fit=0.1,
        ),
        _model(
            "aggregator-alpha",
            roles=["aggregator"],
            capability=0.92,
            aggregator_fit=0.99,
        ),
        _model(
            "aggregator-beta",
            roles=["aggregator"],
            capability=0.90,
            aggregator_fit=0.96,
        ),
        user_profile=None,
    )

    ranked_identities = [row["identity"] for row in decision.trace["aggregator"]["scores"]]
    candidate_identities = [model.identity for model in decision.aggregator_candidates]

    assert len(candidate_identities) == 2
    assert candidate_identities == ranked_identities
    assert len(set(candidate_identities)) == 2


def _single_context(
    *,
    input_tokens: int = 1_000,
    output_tokens: int = 1_000,
) -> dict[str, Any]:
    context = _context(input_tokens=input_tokens)
    context["routing_budget"] = {
        "estimated_input_tokens": input_tokens,
        "tool_log_tokens": 0,
        "direct_output_tokens": output_tokens,
    }
    context["snapshot_hash"] = ranking_router._request_context_hash(context)
    return context


_SINGLE_CONTEXT_FUSION_FIELDS = {
    "aggregator_output_tokens",
    "candidate_output_tokens",
    "previous_candidates",
    "selected_A",
    "selected_P",
}


def _nested_mapping_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_nested_mapping_keys(child) for child in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(_nested_mapping_keys(child) for child in value))
    return set()


def _single_decision(
    *models: dict[str, Any],
    analysis: TaskAnalysisResult | None = None,
    context: dict[str, Any] | None = None,
    ranking_config: dict[str, Any] | None = None,
    requires_tools: bool = False,
    thinking_assignment_enabled: bool = False,
    cache_continuity_available: bool = False,
    cache_affinity_inputs: Any = None,
    cache_affinity_unavailable_reasons: Any = None,
):
    return rank_single_model(
        task_analysis=analysis or _analysis(),
        user_profile=None,
        request_context=context or _single_context(),
        registry_snapshot=_snapshot(*models),
        routed_tier="c2",
        routing_confidence=0.9,
        requires_tools=requires_tools,
        ranking_config=ranking_config,
        decision_id="single-test",
        ranking_thinking_assignment_enabled=thinking_assignment_enabled,
        cache_continuity_available=cache_continuity_available,
        cache_affinity_inputs=cache_affinity_inputs,
        _cache_affinity_unavailable_reasons=(cache_affinity_unavailable_reasons),
    )


def test_build_single_model_request_context_has_only_direct_output_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("fusion budget helper must not be called")

    monkeypatch.setattr(
        ranking_router,
        "dynamic_output_token_budgets",
        fail_if_called,
    )
    monkeypatch.setattr(ranking_router, "build_request_context", fail_if_called)
    monkeypatch.setattr(ranking_router, "_sanitize_last_route", fail_if_called)
    context = build_single_model_request_context(
        message="direct request",
        turn_metadata={"input_tokens": 123, "tool_log_tokens": 9},
        attachments=[],
        output_tokens=2_048,
    )

    assert context["routing_budget"] == {
        "estimated_input_tokens": 123,
        "tool_log_tokens": 9,
        "direct_output_tokens": 2_048,
    }
    assert context["last_route"] == {}
    assert _nested_mapping_keys(context).isdisjoint(_SINGLE_CONTEXT_FUSION_FIELDS)
    assert context["snapshot_hash"] == ranking_router._request_context_hash(context)


def test_single_context_ignores_stale_fusion_route_in_hash_and_score() -> None:
    clean = build_single_model_request_context(
        message="direct request",
        turn_metadata={"input_tokens": 123, "tool_log_tokens": 9},
        attachments=[],
        output_tokens=2_048,
    )
    stale = build_single_model_request_context(
        message="direct request",
        turn_metadata={
            "input_tokens": 123,
            "tool_log_tokens": 9,
            "router_dynamic_request_context": {
                "last_route": {
                    "selected_P": ["test-provider:alpha"],
                    "selected_A": "provider:stale-aggregator",
                    "quality_feedback": 1.0,
                    "escalation_level": 3,
                },
                "intermediate_outputs": {"previous_candidates": ["stale candidate answer"]},
            },
            "router_dynamic_last_route": {
                "selected_P": ["provider:other-stale-proposer"],
                "selected_A": "provider:other-stale-aggregator",
            },
            "last_route": {
                "selected_P": ["provider:third-stale-proposer"],
                "selected_A": "provider:third-stale-aggregator",
            },
        },
        attachments=[],
        output_tokens=2_048,
    )

    assert stale == clean
    assert stale["last_route"] == {}
    assert _nested_mapping_keys(stale).isdisjoint(_SINGLE_CONTEXT_FUSION_FIELDS)
    assert "stale" not in ranking_router.canonical_json_bytes(stale).decode()

    models = (
        _model("alpha", capability=0.90),
        _model("beta", capability=0.80),
    )
    analysis = _analysis(intent="continue", intent_confidence=1.0)
    clean_decision = _single_decision(*models, analysis=analysis, context=clean)
    stale_decision = _single_decision(*models, analysis=analysis, context=stale)
    assert stale_decision.model.identity == clean_decision.model.identity
    assert stale_decision.trace["model_scores"] == clean_decision.trace["model_scores"]


def test_cache_affinity_absent_is_a_true_ranking_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models = (
        _model("alpha", capability=0.9),
        _model("beta", capability=0.9),
    )
    baseline = _single_decision(*models)

    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("disabled cache affinity must not call its scorer")

    monkeypatch.setattr(
        ranking_router,
        "cache_affinity_score_adjustment",
        fail_if_called,
    )
    with_unused_private_input = _single_decision(
        *models,
        cache_affinity_inputs="malformed-but-disabled",
    )

    assert with_unused_private_input == baseline
    assert "cache_affinity_inputs" not in baseline.trace
    assert "cache_affinity" not in baseline.trace["model_scores"][0]


def test_packaged_ranking_config_without_affinity_preserves_golden_bytes_and_hashes() -> None:
    packaged = resources.files("opensquilla.provider").joinpath(
        "router_dynamic_ranking_config.json"
    )
    raw_payload = packaged.read_bytes()
    loaded = load_ranking_config()
    resolution = ranking_config_resolution()

    assert hashlib.sha256(raw_payload).hexdigest() == (
        "c4e2f727aee8438a348029dcc4fa95ba45320b4272ced48dda3c7fc48e4529d8"
    )
    assert canonical_json_sha256(loaded) == (
        "b94b7bba4de316c4dbdedc2578de15df8d42438879482c7d7e23172bc7775df6"
    )
    assert resolution["base_sha256"] == (
        "268ebb0c002994a9434eeecaef6b76571d0bd803db6eb2e7c8a788f3e2210e96"
    )
    assert resolution["effective_sha256"] == resolution["base_sha256"]
    assert "kv_cache_affinity" not in loaded["session"]
    assert "kv_cache_affinity" not in resolution["effective_config"]["session"]


def test_cache_continuity_is_ignored_when_policy_is_absent_or_topology_off() -> None:
    models = (
        _model("alpha", capability=0.9),
        _model("beta", capability=0.8),
    )
    analysis = _analysis(intent="continue", intent_confidence=1.0)
    absent_false = _single_decision(*models, analysis=analysis)
    absent_true = _single_decision(
        *models,
        analysis=analysis,
        cache_continuity_available=True,
    )
    single_off_config = _cache_ranking_config(
        strategy="bonus",
        topologies=["multiple"],
    )
    topology_false = _single_decision(
        *models,
        analysis=analysis,
        ranking_config=single_off_config,
    )
    topology_true = _single_decision(
        *models,
        analysis=analysis,
        ranking_config=single_off_config,
        cache_continuity_available=True,
    )

    assert absent_true == absent_false
    assert topology_true == topology_false

    multiple_off_config = _cache_ranking_config(
        strategy="bonus",
        topologies=["single"],
    )
    multiple_false = _decision(
        *models,
        analysis=analysis,
        ranking_config=multiple_off_config,
    )
    multiple_true = _decision(
        *models,
        analysis=analysis,
        ranking_config=multiple_off_config,
        cache_continuity_available=True,
    )
    assert multiple_true == multiple_false


@pytest.mark.parametrize(
    "policy",
    [
        {
            "strategy": "bonus",
            "topologies": [],
            "ttl_seconds": 300,
            "age_decay": "linear",
            "bonus_by_evidence": {"read_hit": 0.01, "write_only": 0.01},
        },
        {
            "strategy": "bonus",
            "topologies": ["single", "single"],
            "ttl_seconds": 300,
            "age_decay": "linear",
            "bonus_by_evidence": {"read_hit": 0.01, "write_only": 0.01},
        },
        {
            "strategy": "bonus",
            "topologies": ["single"],
            "ttl_seconds": 0,
            "age_decay": "linear",
            "bonus_by_evidence": {"read_hit": 0.01, "write_only": 0.01},
        },
        {
            "strategy": "bonus",
            "topologies": ["single"],
            "ttl_seconds": 300,
            "age_decay": "exponential",
            "bonus_by_evidence": {"read_hit": 0.01, "write_only": 0.01},
        },
        {
            "strategy": "bonus",
            "topologies": ["single"],
            "ttl_seconds": 300,
            "age_decay": "linear",
            "bonus_by_evidence": {"read_hit": 1.0, "write_only": 0.01},
        },
        {
            "strategy": "expected_cost",
            "topologies": ["single"],
            "ttl_seconds": 300,
            "age_decay": "linear",
            "hit_probability_by_evidence": {
                "read_hit": 1.1,
                "write_only": 0.5,
            },
        },
        {
            "strategy": "bonus",
            "topologies": ["single"],
            "ttl_seconds": 300,
            "age_decay": "linear",
            "bonus_by_evidence": {"read_hit": 0.01, "write_only": 0.01},
            "hit_probability_by_evidence": {
                "read_hit": 0.8,
                "write_only": 0.5,
            },
        },
    ],
)
def test_cache_affinity_config_is_strict_and_discriminated(
    policy: dict[str, Any],
) -> None:
    with pytest.raises(DynamicRankingError):
        ranking_config_snapshot(override={"session": {"kv_cache_affinity": policy}})


def test_single_cache_continuity_only_preserves_analyzer_continue() -> None:
    context = _single_context()
    config = _cache_ranking_config(strategy="bonus", topologies=["single"])
    continued, valid, _ = normalize_task_profile(
        _task_profile(intent="continue", intent_confidence=1.0),
        routed_tier="c2",
        request_context=context,
        ranking_config=config,
        cache_continuity_available=True,
    )
    downgraded, downgraded_valid, _ = normalize_task_profile(
        _task_profile(intent="continue", intent_confidence=1.0),
        routed_tier="c2",
        request_context=context,
    )
    new_task, new_task_valid, _ = normalize_task_profile(
        _task_profile(intent="new_task", intent_confidence=1.0),
        routed_tier="c2",
        request_context=context,
        cache_continuity_available=True,
    )

    assert valid and downgraded_valid and new_task_valid
    assert continued["session_intent"]["type"] == "continue"
    assert downgraded["session_intent"]["type"] == "new_task"
    assert new_task["session_intent"]["type"] == "new_task"


@pytest.mark.asyncio
@pytest.mark.parametrize("analyzer_path", ["primary", "retry", "fallback_chain"])
@pytest.mark.parametrize(
    ("cache_continuity_available", "expected_intent"),
    [(False, "new_task"), (True, "continue")],
)
async def test_cache_continuity_flag_reaches_every_live_analyzer_path(
    analyzer_path: str,
    cache_continuity_available: bool,
    expected_intent: str,
) -> None:
    ranking_config = _cache_ranking_config(
        strategy="bonus",
        topologies=["single"],
    )
    response = json.dumps(_task_profile(intent="continue", intent_confidence=1.0))
    if analyzer_path == "retry":
        ranking_config["task_analyzer"]["max_retries"] = 1
        provider = _AnalyzerProvider(["not-json", response])
        result = await analyze_task_with_provider(
            provider=provider,
            message="continue the prior task",
            user_profile_enabled=False,
            request_context=_single_context(),
            routed_tier="c2",
            routing_confidence=0.9,
            ranking_config=ranking_config,
            cache_continuity_available=cache_continuity_available,
        )
        assert len(provider.calls) == 2
    elif analyzer_path == "fallback_chain":
        provider = _AnalyzerProvider(response)
        result = await analyze_task_with_fallback_chain(
            candidates=_task_analyzer_chain_candidates([None, provider, None]),
            message="continue the prior task",
            user_profile_enabled=False,
            request_context=_single_context(),
            routed_tier="c2",
            routing_confidence=0.9,
            ranking_config=ranking_config,
            cache_continuity_available=cache_continuity_available,
            decision_id="c" * 32,
        )
        assert len(provider.calls) == 1
    else:
        provider = _AnalyzerProvider(response)
        result = await analyze_task_with_provider(
            provider=provider,
            message="continue the prior task",
            user_profile_enabled=False,
            request_context=_single_context(),
            routed_tier="c2",
            routing_confidence=0.9,
            ranking_config=ranking_config,
            cache_continuity_available=cache_continuity_available,
        )
        assert len(provider.calls) == 1

    assert result.schema_valid is True
    assert result.profile["session_intent"]["type"] == expected_intent


@pytest.mark.parametrize("topology", ["single", "multiple"])
def test_schema_invalid_high_confidence_continue_cannot_apply_cache_live_or_replay(
    topology: str,
) -> None:
    invalid_analysis = TaskAnalysisResult(
        profile=_task_profile(
            tier=1,
            intent="continue",
            intent_confidence=1.0,
        ),
        source="llm_provider",
        schema_valid=False,
        confidence=1.0,
        fallback_reason="TimeoutError",
    )

    if topology == "single":
        alpha = _model("alpha", capability=0.9)
        beta = _model("beta", capability=0.9)
        beta_identity = "test-provider:beta"
        config = _cache_ranking_config(
            strategy="bonus",
            topologies=["single"],
        )
        affinity_inputs = {"single": {beta_identity: _cache_evidence(beta_identity, role="single")}}
        valid = _single_decision(
            alpha,
            beta,
            analysis=_analysis(
                tier=1,
                intent="continue",
                intent_confidence=1.0,
            ),
            ranking_config=config,
            cache_continuity_available=True,
            cache_affinity_inputs=affinity_inputs,
        )
        invalid = _single_decision(
            alpha,
            beta,
            analysis=invalid_analysis,
            ranking_config=config,
            cache_continuity_available=True,
            cache_affinity_inputs=affinity_inputs,
        )

        assert valid.model.identity == beta_identity
        assert invalid.model.identity == "test-provider:alpha"
        replay_reasons = single_ranking_trace_replay_reasons
        expected_tamper_reasons = ["invalid_single_ranking_replay_cache_affinity_inputs"]
        score_rows = invalid.trace["model_scores"]
    else:
        proposer_alpha = _model(
            "p-alpha",
            roles=["proposer"],
            capability=0.9,
        )
        proposer_beta = _model(
            "p-beta",
            roles=["proposer"],
            capability=0.9,
        )
        aggregator_alpha = _model(
            "a-alpha",
            roles=["aggregator"],
            capability=0.8,
            aggregator_fit=0.9,
        )
        aggregator_beta = _model(
            "a-beta",
            roles=["aggregator"],
            capability=0.8,
            aggregator_fit=0.9,
        )
        proposer_identity = "test-provider:p-beta"
        aggregator_identity = "test-provider:a-beta"
        config = _cache_ranking_config(
            strategy="bonus",
            topologies=["multiple"],
        )
        affinity_inputs = {
            "proposer": {
                proposer_identity: _cache_evidence(
                    proposer_identity,
                    role="proposer",
                )
            },
            "aggregator": {
                aggregator_identity: _cache_evidence(
                    aggregator_identity,
                    role="aggregator",
                )
            },
        }
        context = _context(
            last_route={
                "selected_P": ["unrelated:old"],
                "selected_A": "unrelated:old-aggregator",
            }
        )
        models = (
            proposer_alpha,
            proposer_beta,
            aggregator_alpha,
            aggregator_beta,
        )
        valid = _decision(
            *models,
            analysis=_analysis(
                tier=1,
                intent="continue",
                intent_confidence=1.0,
            ),
            context=context,
            ranking_config=config,
            cache_affinity_inputs=affinity_inputs,
            user_profile_enabled=False,
        )
        invalid = _decision(
            *models,
            analysis=invalid_analysis,
            context=context,
            ranking_config=config,
            cache_affinity_inputs=affinity_inputs,
            user_profile_enabled=False,
        )

        assert valid.proposers[0].identity == proposer_identity
        assert valid.aggregator.identity == aggregator_identity
        assert invalid.proposers[0].identity == "test-provider:p-alpha"
        assert invalid.aggregator.identity == "test-provider:a-alpha"
        replay_reasons = ranking_trace_replay_reasons
        expected_tamper_reasons = ["g1_frozen_ranker_replay_failed"]
        score_rows = [
            *invalid.trace["model_scores"],
            *invalid.trace["aggregator"]["scores"],
        ]

    assert invalid.trace["task_analyzer"]["schema_valid"] is False
    assert invalid.trace["cache_affinity_inputs"] == []
    assert all("cache_affinity" not in row for row in score_rows)
    assert replay_reasons(invalid.trace) == []

    tampered = deepcopy(valid.trace)
    tampered["task_analyzer"]["schema_valid"] = False
    assert replay_reasons(tampered) == expected_tamper_reasons


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_mode", "expected_reason"),
    [
        ("provider_unavailable", "provider_unavailable"),
        ("timeout", "TimeoutError"),
    ],
)
async def test_configured_continue_fallback_never_applies_cache_after_analyzer_failure(
    failure_mode: str,
    expected_reason: str,
) -> None:
    config = _cache_ranking_config(
        strategy="bonus",
        topologies=["single"],
    )
    config["fallback_task_profile"]["session_intent"] = {
        "type": "continue",
        "confidence": 1.0,
    }

    class TimeoutAnalyzerProvider:
        accounts_physical_usage = True

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config

            async def stream() -> AsyncIterator[Any]:
                await asyncio.Event().wait()
                yield TextDeltaEvent(text="unreachable")

            return stream()

    provider = None if failure_mode == "provider_unavailable" else TimeoutAnalyzerProvider()
    analysis = await analyze_task_with_provider(
        provider=provider,
        message="continue the previous task",
        user_profile_enabled=False,
        request_context=_single_context(),
        routed_tier="c2",
        routing_confidence=0.9,
        ranking_config=config,
        cache_continuity_available=True,
        timeout_seconds=0.01 if failure_mode == "timeout" else None,
    )
    beta_identity = "test-provider:beta"
    decision = _single_decision(
        _model("alpha", capability=0.9),
        _model("beta", capability=0.9),
        analysis=analysis,
        ranking_config=config,
        cache_continuity_available=True,
        cache_affinity_inputs={
            "single": {beta_identity: _cache_evidence(beta_identity, role="single")}
        },
    )

    assert analysis.source == "router_fallback"
    assert analysis.schema_valid is False
    assert analysis.fallback_reason == expected_reason
    assert analysis.profile["session_intent"] == {
        "type": "continue",
        "confidence": 1.0,
    }
    assert decision.model.identity == "test-provider:alpha"
    assert decision.trace["cache_affinity_inputs"] == []
    assert all("cache_affinity" not in row for row in decision.trace["model_scores"])
    assert single_ranking_trace_replay_reasons(decision.trace) == []


def test_single_bonus_applies_after_hard_filter_before_top1() -> None:
    alpha = _model("alpha", capability=0.9)
    beta = _model("beta", capability=0.9)
    beta_identity = "test-provider:beta"
    config = _cache_ranking_config(strategy="bonus", topologies=["single"])
    decision = _single_decision(
        alpha,
        beta,
        analysis=_analysis(intent="continue", intent_confidence=1.0),
        ranking_config=config,
        cache_continuity_available=True,
        cache_affinity_inputs={
            "single": {beta_identity: _cache_evidence(beta_identity, role="single")}
        },
    )

    assert decision.model.identity == beta_identity
    assert decision.trace["selection_policy"] == "cache_adjusted_base_score_top1"
    scores = {row["identity"]: row for row in decision.trace["model_scores"]}
    alpha_score = scores["test-provider:alpha"]
    beta_score = scores[beta_identity]
    assert alpha_score["S_base_clean"] == beta_score["S_base_clean"]
    assert beta_score["cache_affinity"]["score_adjustment"] == pytest.approx(0.05)


def test_single_cache_bonus_is_soft_and_cannot_overcome_a_larger_score_gap() -> None:
    alpha = _model("alpha", capability=0.99)
    beta = _model("beta", capability=0.50)
    beta_identity = "test-provider:beta"
    decision = _single_decision(
        alpha,
        beta,
        analysis=_analysis(intent="continue", intent_confidence=1.0),
        ranking_config=_cache_ranking_config(
            strategy="bonus",
            topologies=["single"],
        ),
        cache_continuity_available=True,
        cache_affinity_inputs={
            "single": {beta_identity: _cache_evidence(beta_identity, role="single")}
        },
    )

    assert decision.model.identity == "test-provider:alpha"


def test_cache_intent_gate_includes_threshold_and_rejects_other_intents() -> None:
    alpha = _model("alpha", capability=0.9)
    beta = _model("beta", capability=0.9)
    beta_identity = "test-provider:beta"
    config = _cache_ranking_config(strategy="bonus", topologies=["single"])
    threshold = config["session"]["intent_confidence_threshold"]
    evidence = {"single": {beta_identity: _cache_evidence(beta_identity, role="single")}}

    at_threshold = _single_decision(
        alpha,
        beta,
        analysis=_analysis(intent="continue", intent_confidence=threshold),
        ranking_config=config,
        cache_continuity_available=True,
        cache_affinity_inputs=evidence,
    )
    below_threshold = _single_decision(
        alpha,
        beta,
        analysis=_analysis(
            intent="continue",
            intent_confidence=threshold - 1e-9,
        ),
        ranking_config=config,
        cache_continuity_available=True,
        cache_affinity_inputs=evidence,
    )

    assert at_threshold.model.identity == beta_identity
    assert below_threshold.model.identity == "test-provider:alpha"
    for intent in ("new_task", "redo", "unknown"):
        rejected = _single_decision(
            alpha,
            beta,
            analysis=_analysis(intent=intent, intent_confidence=1.0),
            ranking_config=config,
            cache_continuity_available=True,
            cache_affinity_inputs=evidence,
        )
        assert rejected.model.identity == "test-provider:alpha"


def test_single_expected_cost_replaces_only_input_cost_component() -> None:
    alpha = _model("alpha", capability=0.9, price=2.0)
    beta = _model(
        "beta",
        capability=0.9,
        price=2.0,
        price_source="synthetic_exact",
    )
    beta_identity = "test-provider:beta"
    quote = CachePriceQuote(
        provider="test-provider",
        canonical_model="beta",
        endpoint_scope="https://provider.test:443/v1",
        upstream_scope="test-provider",
        price_source="synthetic_exact",
        normal_input_per_million=2.0,
        normal_output_per_million=2.0,
        cache_read_per_million=0.2,
        cache_write_per_million=2.5,
    )
    decision = _single_decision(
        alpha,
        beta,
        analysis=_analysis(intent="continue", intent_confidence=1.0),
        ranking_config=_cache_ranking_config(
            strategy="expected_cost",
            topologies=["single"],
        ),
        cache_continuity_available=True,
        cache_affinity_inputs={
            "single": {
                beta_identity: _cache_evidence(
                    beta_identity,
                    role="single",
                    price_quote=quote,
                )
            }
        },
    )

    assert decision.model.identity == beta_identity
    beta_score = next(
        row for row in decision.trace["model_scores"] if row["identity"] == beta_identity
    )
    assert beta_score["S_base"] == beta_score["S_base_clean"]
    assert beta_score["cache_affinity"]["N"] == 1_000
    assert beta_score["cache_affinity"]["K"] == 1_000
    assert beta_score["cache_affinity"]["score_adjustment"] > 0.0


def test_expected_cost_unavailable_reason_is_safe_replayable_and_tamper_bound() -> None:
    identity = "test-provider:beta"
    reason = ranking_router.CacheAffinityUnavailableReason.EXACT_CACHE_PRICE_QUOTE_UNAVAILABLE.value
    reasons = [{"role": "single", "identity": identity, "reason": reason}]
    decision = _single_decision(
        _model("alpha", capability=0.9, price=2.0),
        _model("beta", capability=0.9, price=2.0),
        analysis=_analysis(intent="continue", intent_confidence=1.0),
        ranking_config=_cache_ranking_config(
            strategy="expected_cost",
            topologies=["single"],
        ),
        cache_continuity_available=True,
        cache_affinity_unavailable_reasons=reasons,
    )

    assert decision.trace["cache_affinity_unavailable_reasons"] == reasons
    assert single_ranking_trace_replay_reasons(decision.trace) == []
    tampered = deepcopy(decision.trace)
    tampered["cache_affinity_unavailable_reasons"][0]["reason"] = "untrusted_reason"
    assert single_ranking_trace_replay_reasons(tampered) == [
        "invalid_single_ranking_replay_cache_affinity_inputs"
    ]


def test_multiple_expected_cost_unavailable_reason_is_replayable() -> None:
    identity = "test-provider:p-beta"
    reason = ranking_router.CacheAffinityUnavailableReason.EXACT_CACHE_PRICE_QUOTE_UNAVAILABLE.value
    reasons = [{"role": "proposer", "identity": identity, "reason": reason}]
    decision = _decision(
        _model("p-alpha", roles=["proposer"], capability=0.9, price=2.0),
        _model("p-beta", roles=["proposer"], capability=0.9, price=2.0),
        _model(
            "a-alpha",
            roles=["aggregator"],
            capability=0.8,
            aggregator_fit=0.9,
            price=2.0,
        ),
        analysis=_analysis(tier=1, intent="continue", intent_confidence=1.0),
        ranking_config=_cache_ranking_config(
            strategy="expected_cost",
            topologies=["multiple"],
        ),
        cache_continuity_available=True,
        cache_affinity_unavailable_reasons=reasons,
        user_profile_enabled=False,
    )

    assert decision.trace["cache_affinity_unavailable_reasons"] == reasons
    assert ranking_trace_replay_reasons(decision.trace) == []
    tampered = deepcopy(decision.trace)
    tampered["cache_affinity_unavailable_reasons"][0]["identity"] = "test-provider:unknown"
    assert ranking_trace_replay_reasons(tampered) == ["g1_frozen_ranker_replay_failed"]


def test_cache_unavailable_reason_is_absent_for_bonus_and_disabled_policy() -> None:
    models = (
        _model("alpha", capability=0.9),
        _model("beta", capability=0.8),
    )
    absent = _single_decision(*models)
    bonus = _single_decision(
        *models,
        ranking_config=_cache_ranking_config(
            strategy="bonus",
            topologies=["single"],
        ),
    )
    absent_without_private_input = rank_single_model(
        task_analysis=_analysis(),
        user_profile=None,
        request_context=_single_context(),
        registry_snapshot=_snapshot(*models),
        routed_tier="c2",
        routing_confidence=0.9,
        requires_tools=False,
        decision_id="single-test",
    )
    bonus_config = _cache_ranking_config(
        strategy="bonus",
        topologies=["single"],
    )
    bonus_without_private_input = rank_single_model(
        task_analysis=_analysis(),
        user_profile=None,
        request_context=_single_context(),
        registry_snapshot=_snapshot(*models),
        routed_tier="c2",
        routing_confidence=0.9,
        requires_tools=False,
        ranking_config=bonus_config,
        decision_id="single-test",
    )
    bonus_with_explicit_none = _single_decision(
        *models,
        ranking_config=bonus_config,
        cache_affinity_unavailable_reasons=None,
    )

    assert "cache_affinity_unavailable_reasons" not in absent.trace
    assert "cache_affinity_unavailable_reasons" not in bonus.trace
    assert absent.trace == absent_without_private_input.trace
    assert bonus_with_explicit_none.trace == bonus_without_private_input.trace
    with pytest.raises(
        DynamicRankingError,
        match="require an active expected_cost policy",
    ):
        _single_decision(
            *models,
            ranking_config=_cache_ranking_config(
                strategy="bonus",
                topologies=["single"],
            ),
            cache_affinity_unavailable_reasons=[
                {
                    "role": "single",
                    "identity": "test-provider:alpha",
                    "reason": (
                        ranking_router.CacheAffinityUnavailableReason.EXACT_CACHE_PRICE_QUOTE_UNAVAILABLE.value
                    ),
                }
            ],
        )


@pytest.mark.parametrize("strategy", ["bonus", "expected_cost"])
def test_single_cache_aware_trace_replay_and_tamper_rejection(
    strategy: str,
) -> None:
    alpha = _model("alpha", capability=0.9, price=2.0)
    beta = _model(
        "beta",
        capability=0.9,
        price=2.0,
        price_source=("synthetic_exact" if strategy == "expected_cost" else None),
    )
    beta_identity = "test-provider:beta"
    quote = (
        CachePriceQuote(
            provider="test-provider",
            canonical_model="beta",
            endpoint_scope="https://provider.test:443/v1",
            upstream_scope="test-provider",
            price_source="synthetic_exact",
            normal_input_per_million=2.0,
            normal_output_per_million=2.0,
            cache_read_per_million=0.2,
            cache_write_per_million=2.5,
        )
        if strategy == "expected_cost"
        else None
    )
    decision = _single_decision(
        alpha,
        beta,
        analysis=_analysis(intent="continue", intent_confidence=1.0),
        ranking_config=_cache_ranking_config(
            strategy=strategy,
            topologies=["single"],
        ),
        cache_continuity_available=True,
        cache_affinity_inputs={
            "single": {
                beta_identity: _cache_evidence(
                    beta_identity,
                    role="single",
                    price_quote=quote,
                )
            }
        },
    )

    assert single_ranking_trace_replay_reasons(decision.trace) == []
    tampered = deepcopy(decision.trace)
    tampered["cache_affinity_inputs"][0]["score_adjustment"] += 0.001
    assert single_ranking_trace_replay_reasons(tampered)


def test_multiple_bonus_applies_only_at_proposer_marginal_and_aggregator_score() -> None:
    proposer_alpha = _model(
        "p-alpha",
        roles=["proposer"],
        capability=0.9,
    )
    proposer_beta = _model(
        "p-beta",
        roles=["proposer"],
        capability=0.9,
    )
    aggregator_alpha = _model(
        "a-alpha",
        roles=["aggregator"],
        capability=0.8,
        aggregator_fit=0.9,
    )
    aggregator_beta = _model(
        "a-beta",
        roles=["aggregator"],
        capability=0.8,
        aggregator_fit=0.9,
    )
    proposer_identity = "test-provider:p-beta"
    aggregator_identity = "test-provider:a-beta"
    decision = _decision(
        proposer_alpha,
        proposer_beta,
        aggregator_alpha,
        aggregator_beta,
        analysis=_analysis(tier=1, intent="continue", intent_confidence=1.0),
        context=_context(
            last_route={
                "selected_P": ["unrelated:old"],
                "selected_A": "unrelated:old-aggregator",
            }
        ),
        ranking_config=_cache_ranking_config(
            strategy="bonus",
            topologies=["multiple"],
        ),
        cache_affinity_inputs={
            "proposer": {
                proposer_identity: _cache_evidence(
                    proposer_identity,
                    role="proposer",
                )
            },
            "aggregator": {
                aggregator_identity: _cache_evidence(
                    aggregator_identity,
                    role="aggregator",
                )
            },
        },
    )

    assert decision.proposers[0].identity == proposer_identity
    assert decision.aggregator.identity == aggregator_identity
    proposer_scores = {row["identity"]: row for row in decision.trace["model_scores"]}
    assert (
        proposer_scores["test-provider:p-alpha"]["S_base_clean"]
        == proposer_scores[proposer_identity]["S_base_clean"]
    )
    assert decision.trace["selection_steps"][0]["cache_affinity"][
        "score_adjustment"
    ] == pytest.approx(0.05)
    assert decision.trace["aggregator"]["selected"]["cache_affinity"][
        "score_adjustment"
    ] == pytest.approx(0.05)


def test_multiple_cache_affinity_preserves_bounds_quorum_and_backup_contracts() -> None:
    models = (
        _model("p-alpha", roles=["proposer"], capability=0.9),
        _model("p-beta", roles=["proposer"], capability=0.9),
        _model("p-gamma", roles=["proposer"], capability=0.85),
        _model(
            "a-alpha",
            roles=["aggregator"],
            capability=0.8,
            aggregator_fit=0.9,
        ),
        _model(
            "a-beta",
            roles=["aggregator"],
            capability=0.8,
            aggregator_fit=0.9,
        ),
    )
    analysis = _analysis(tier=1, intent="continue", intent_confidence=1.0)
    context = _context(
        last_route={
            "selected_P": ["unrelated:old"],
            "selected_A": "unrelated:old-aggregator",
        }
    )
    baseline = _decision(*models, analysis=analysis, context=context)
    adjusted = _decision(
        *models,
        analysis=analysis,
        context=context,
        ranking_config=_cache_ranking_config(
            strategy="bonus",
            topologies=["multiple"],
        ),
        cache_affinity_inputs={
            "proposer": {
                "test-provider:p-beta": _cache_evidence(
                    "test-provider:p-beta",
                    role="proposer",
                )
            },
            "aggregator": {
                "test-provider:a-beta": _cache_evidence(
                    "test-provider:a-beta",
                    role="aggregator",
                )
            },
        },
    )

    for field in (
        "N_min",
        "N_max",
        "configured_proposer_backup_count",
        "effective_proposer_backup_count",
        "proposer_recovery_policy",
    ):
        assert adjusted.trace[field] == baseline.trace[field]


def test_cache_aware_trace_replay_consumes_only_frozen_adjustments() -> None:
    proposer_alpha = _model(
        "p-alpha",
        roles=["proposer"],
        capability=0.9,
    )
    proposer_beta = _model(
        "p-beta",
        roles=["proposer"],
        capability=0.9,
    )
    aggregator_alpha = _model(
        "a-alpha",
        roles=["aggregator"],
        capability=0.8,
        aggregator_fit=0.9,
    )
    aggregator_beta = _model(
        "a-beta",
        roles=["aggregator"],
        capability=0.8,
        aggregator_fit=0.9,
    )
    decision = _decision(
        proposer_alpha,
        proposer_beta,
        aggregator_alpha,
        aggregator_beta,
        analysis=_analysis(tier=1, intent="continue", intent_confidence=1.0),
        context=_context(
            last_route={
                "selected_P": ["unrelated:old"],
                "selected_A": "unrelated:old-aggregator",
            }
        ),
        ranking_config=_cache_ranking_config(
            strategy="bonus",
            topologies=["multiple"],
        ),
        cache_affinity_inputs={
            "proposer": {
                "test-provider:p-beta": _cache_evidence(
                    "test-provider:p-beta",
                    role="proposer",
                )
            },
            "aggregator": {
                "test-provider:a-beta": _cache_evidence(
                    "test-provider:a-beta",
                    role="aggregator",
                )
            },
        },
        user_profile_enabled=False,
    )

    assert ranking_trace_replay_reasons(decision.trace) == []
    tampered = deepcopy(decision.trace)
    tampered["cache_affinity_inputs"][0]["score_adjustment"] += 0.001
    assert ranking_trace_replay_reasons(tampered) == ["invalid_g1_replay_cache_affinity_inputs"]


def test_expected_cost_trace_replay_uses_frozen_safe_table() -> None:
    proposer_alpha = _model(
        "p-alpha",
        roles=["proposer"],
        capability=0.9,
        price=2.0,
    )
    proposer_beta = _model(
        "p-beta",
        roles=["proposer"],
        capability=0.9,
        price=2.0,
        price_source="synthetic_exact",
    )
    aggregator_alpha = _model(
        "a-alpha",
        roles=["aggregator"],
        capability=0.8,
        aggregator_fit=0.9,
        price=2.0,
    )
    aggregator_beta = _model(
        "a-beta",
        roles=["aggregator"],
        capability=0.8,
        aggregator_fit=0.9,
        price=2.0,
        price_source="synthetic_exact",
    )

    def quote(model: str) -> CachePriceQuote:
        return CachePriceQuote(
            provider="test-provider",
            canonical_model=model,
            endpoint_scope="https://provider.test:443/v1",
            upstream_scope="test-provider",
            price_source="synthetic_exact",
            normal_input_per_million=2.0,
            normal_output_per_million=2.0,
            cache_read_per_million=0.2,
            cache_write_per_million=2.5,
        )

    decision = _decision(
        proposer_alpha,
        proposer_beta,
        aggregator_alpha,
        aggregator_beta,
        analysis=_analysis(tier=1, intent="continue", intent_confidence=1.0),
        context=_context(
            last_route={
                "selected_P": ["unrelated:old"],
                "selected_A": "unrelated:old-aggregator",
            }
        ),
        ranking_config=_cache_ranking_config(
            strategy="expected_cost",
            topologies=["multiple"],
        ),
        cache_affinity_inputs={
            "proposer": {
                "test-provider:p-beta": _cache_evidence(
                    "test-provider:p-beta",
                    role="proposer",
                    price_quote=quote("p-beta"),
                )
            },
            "aggregator": {
                "test-provider:a-beta": _cache_evidence(
                    "test-provider:a-beta",
                    role="aggregator",
                    price_quote=quote("a-beta"),
                )
            },
        },
        user_profile_enabled=False,
    )

    assert ranking_trace_replay_reasons(decision.trace) == []
    tampered = deepcopy(decision.trace)
    tampered["cache_affinity_inputs"][0]["p"] = float("nan")
    assert ranking_trace_replay_reasons(tampered) == ["invalid_g1_replay_cache_affinity_inputs"]

    coordinated = deepcopy(decision.trace)
    target_identity = coordinated["cache_affinity_inputs"][0]["identity"]
    changed = 0

    def tamper_price_source(value: object) -> None:
        nonlocal changed
        if isinstance(value, dict):
            if value.get("identity") == target_identity and "price_source" in value:
                value["price_source"] = "forged_price_source"
                changed += 1
            for child in value.values():
                tamper_price_source(child)
        elif isinstance(value, list):
            for child in value:
                tamper_price_source(child)

    for field_name, field_value in coordinated.items():
        if field_name != "registry_snapshot":
            tamper_price_source(field_value)
    assert changed >= 2  # frozen input plus its nested score/selection projection
    assert ranking_trace_replay_reasons(coordinated) == ["g1_frozen_ranker_replay_failed"]

    coordinated_k = deepcopy(decision.trace)
    changed_k = 0

    def tamper_cache_tokens(value: object) -> None:
        nonlocal changed_k
        if isinstance(value, dict):
            if value.get("identity") == target_identity and "K" in value:
                value["K"] -= 1
                changed_k += 1
            for child in value.values():
                tamper_cache_tokens(child)
        elif isinstance(value, list):
            for child in value:
                tamper_cache_tokens(child)

    for field_name, field_value in coordinated_k.items():
        if field_name != "registry_snapshot":
            tamper_cache_tokens(field_value)
    assert changed_k >= 2
    assert ranking_trace_replay_reasons(coordinated_k) == [
        "invalid_g1_replay_cache_affinity_inputs"
    ]


@pytest.mark.asyncio
async def test_single_context_compaction_preserves_only_direct_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _AnalyzerProvider(json.dumps(_task_profile(tier=2)))
    config = load_ranking_config()
    config["task_analyzer"].update(
        {
            "payload_max_chars": 2_200,
            "payload_max_bytes": 5_000,
            "payload_max_estimated_tokens": 1_500,
        }
    )
    request_context = build_single_model_request_context(
        message="direct request",
        turn_metadata={
            "router_dynamic_request_context": {
                "conversation": {"summary": "large context " * 1_000},
                "intermediate_outputs": {"previous_candidates": ["stale candidate answer"]},
                "last_route": {
                    "selected_P": ["provider:stale-proposer"],
                    "selected_A": "provider:stale-aggregator",
                },
            }
        },
        attachments=[],
        output_tokens=2_048,
        ranking_config=config,
    )

    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("fusion last-route helper must not be called")

    monkeypatch.setattr(ranking_router, "_sanitize_last_route", fail_if_called)
    result = await analyze_task_with_provider(
        provider=provider,
        message="classify this direct request",
        user_profile_enabled=False,
        request_context=request_context,
        routed_tier="c1",
        routing_confidence=0.8,
        ranking_config=config,
    )

    assert result.schema_valid is True
    payload = json.loads(str(provider.calls[0][0][0].content))
    compact_context = payload["request_context"]
    assert compact_context["payload_context_truncated"] is True
    assert compact_context["routing_budget"] == {
        "estimated_input_tokens": request_context["routing_budget"]["estimated_input_tokens"],
        "tool_log_tokens": request_context["routing_budget"]["tool_log_tokens"],
        "direct_output_tokens": 2_048,
    }
    assert compact_context["last_route"] == {}
    assert compact_context["snapshot_hash"] == request_context["snapshot_hash"]
    assert _nested_mapping_keys(compact_context).isdisjoint(_SINGLE_CONTEXT_FUSION_FIELDS)


def test_rank_single_model_has_no_fusion_or_roster_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("fusion dependency was called")

    forbidden = {
        "rank_models",
        "_selection_roster_counts",
        "_proposer_bounds",
        "_aggregator_filter_rows",
        "_aggregator_rows",
        "_assign_thinking_levels",
        "_coverage_gain",
        "_similarity",
        "_error_complementarity",
    }
    for name in forbidden:
        monkeypatch.setattr(ranking_router, name, fail_if_called)

    decision = _single_decision(_model("only", capability=0.9))
    source = inspect.getsource(rank_single_model)
    tree = ast.parse(source)
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert decision.model.model_id == "only"
    assert called_names.isdisjoint(forbidden)
    assert "selection_roster" not in source
    assert "aggregator" not in source


def test_rank_single_model_enforces_direct_execution_hard_filters() -> None:
    config = load_ranking_config()
    config["hard_filter"]["eligible_statuses"].append("active")
    unavailable_health = config["hard_filter"]["unavailable_health_states"][0]
    decision = _single_decision(
        _model(
            "wrong-status",
            status="active",
            capability=0.99,
            modalities=["text", "image"],
        ),
        _model(
            "unhealthy",
            health=unavailable_health,
            capability=0.98,
            modalities=["text", "image"],
        ),
        _model(
            "no-credential",
            credential_available=False,
            capability=0.97,
            modalities=["text", "image"],
        ),
        _model(
            "short-context",
            context_window=1,
            capability=0.96,
            modalities=["text", "image"],
        ),
        _model("wrong-modality", capability=0.95, modalities=["text"]),
        _model("eligible", capability=0.80, modalities=["text", "image"]),
        analysis=_analysis(modalities=["text", "image"]),
        ranking_config=config,
    )
    filter_rows = {row["model"]: row for row in decision.trace["hard_filter"]["proposer_results"]}

    assert decision.model.model_id == "eligible"
    assert "status_not_enabled" in filter_rows["wrong-status"]["reasons"]
    assert "health_unavailable" in filter_rows["unhealthy"]["reasons"]
    assert "credential_unavailable" in filter_rows["no-credential"]["reasons"]
    assert "context_exceeded" in filter_rows["short-context"]["reasons"]
    assert "modality_mismatch" in filter_rows["wrong-modality"]["reasons"]


def test_rank_single_model_uses_candidate_specific_direct_output_budget() -> None:
    exact_fit = _model(
        "candidate-exact-fit",
        capability=0.90,
        context_window=6_000,
    )
    exact_fit["registry_facts"]["runtime_direct_output_tokens"] = 5_000
    exact_short = _model(
        "candidate-exact-short",
        capability=0.99,
        context_window=5_999,
    )
    exact_short["registry_facts"]["runtime_direct_output_tokens"] = 5_000
    fallback = _model(
        "request-fallback",
        capability=0.80,
        context_window=2_000,
    )
    decision = _single_decision(
        exact_fit,
        exact_short,
        fallback,
        context=_single_context(input_tokens=1_000, output_tokens=1_000),
    )
    filter_rows = {row["model"]: row for row in decision.trace["hard_filter"]["proposer_results"]}

    assert decision.model.model_id == "candidate-exact-fit"
    assert filter_rows["candidate-exact-fit"]["context_need_tokens"] == 6_000
    assert filter_rows["candidate-exact-fit"]["direct_output_tokens"] == 5_000
    assert filter_rows["candidate-exact-fit"]["direct_output_tokens_source"] == (
        "runtime_registry_fact"
    )
    assert "context_exceeded" in filter_rows["candidate-exact-short"]["reasons"]
    assert filter_rows["candidate-exact-short"]["context_need_tokens"] == 6_000
    assert filter_rows["request-fallback"]["context_need_tokens"] == 2_000
    assert filter_rows["request-fallback"]["direct_output_tokens"] == 1_000
    assert filter_rows["request-fallback"]["direct_output_tokens_source"] == (
        "request_context_fallback"
    )


@pytest.mark.parametrize(
    "runtime_direct_output_tokens",
    [True, 0, -1, 1.5, "1000"],
)
def test_rank_single_model_rejects_invalid_runtime_direct_output_tokens(
    runtime_direct_output_tokens: object,
) -> None:
    model = _model("invalid-runtime-output")
    model["registry_facts"]["runtime_direct_output_tokens"] = runtime_direct_output_tokens

    with pytest.raises(
        DynamicRankingError,
        match="invalid runtime_direct_output_tokens",
    ):
        _single_decision(
            model,
            context=_single_context(output_tokens=5_000),
        )


@pytest.mark.parametrize("thinking_assignment_enabled", [False, True])
def test_rank_single_model_tools_filter_is_independent_of_thinking_switch(
    thinking_assignment_enabled: bool,
) -> None:
    model_factory = _thinking_model if thinking_assignment_enabled else _model
    unsupported = model_factory("unsupported-tools", capability=0.99)
    capable = model_factory("tool-capable", capability=0.80)
    capable["registry_facts"]["supports_tools"] = True

    without_tools = _single_decision(
        unsupported,
        capable,
        requires_tools=False,
        thinking_assignment_enabled=thinking_assignment_enabled,
    )
    with_tools = _single_decision(
        unsupported,
        capable,
        requires_tools=True,
        thinking_assignment_enabled=thinking_assignment_enabled,
    )

    assert without_tools.model.model_id == "unsupported-tools"
    assert with_tools.model.model_id == "tool-capable"
    unsupported_filter = next(
        row
        for row in with_tools.trace["hard_filter"]["proposer_results"]
        if row["model"] == "unsupported-tools"
    )
    assert unsupported_filter["reasons"] == ["required_parameter_tools_unsupported"]


@pytest.mark.parametrize("surface", ["registry_snapshot", "request_context"])
def test_rank_single_model_trace_rejects_secret_like_evidence(surface: str) -> None:
    model = _model("unsafe", capability=0.90)
    context = _single_context()
    if surface == "registry_snapshot":
        model["registry_facts"]["api_key"] = "must-not-enter-trace"
    else:
        context["authorization"] = "must-not-enter-trace"

    with pytest.raises(DynamicRankingError, match="secret-like"):
        _single_decision(model, context=context)


def test_rank_single_model_uses_base_score_top_one_with_stable_ties() -> None:
    decision = _single_decision(
        _model("lower", capability=0.70, price=1.0),
        _model("higher", capability=0.95, price=1.0),
    )
    tied = _single_decision(
        _model("beta", capability=0.90, price=1.0),
        _model("alpha", capability=0.90, price=1.0),
    )

    assert decision.model.model_id == "higher"
    assert decision.trace["model_scores"][0]["model"] == "higher"
    assert tied.model.model_id == "alpha"


def test_rank_single_model_assigns_only_proposer_thinking() -> None:
    decision = _single_decision(
        _thinking_model("thinking", capability=0.95),
        thinking_assignment_enabled=True,
    )

    assert decision.model.requested_thinking_level is not None
    assert decision.model.effective_thinking_level is not None
    assert set(decision.thinking_assignment) == {
        "proposers",
        "thinking_policy_version",
    }
    assert set(decision.thinking_assignment_details) == {
        "effective_tier",
        "proposers",
    }


def test_rank_single_model_trace_has_no_fusion_selection_fields() -> None:
    decision = _single_decision(_model("direct", capability=0.90))
    forbidden = {
        "selected_A",
        "aggregator",
        "aggregator_candidates",
        "aggregator_feasibility",
        "selection_roster",
    }

    assert decision.trace["strategy"] == "router_dynamic"
    assert decision.trace["execution_mode"] == "router_single"
    assert decision.trace["selection_policy"] == "base_score_top1"
    assert decision.trace["selected_model"] == decision.model.identity
    assert decision.trace["selected_P"] == [decision.model.identity]
    assert forbidden.isdisjoint(decision.trace)
    assert "aggregator_results" not in decision.trace["hard_filter"]


def test_rank_single_model_no_eligible_model_fails_closed() -> None:
    with pytest.raises(DynamicRankingError) as exc_info:
        _single_decision(_model("disabled", status="disabled"))

    assert exc_info.value.reason == "no_eligible_single_model"


def test_single_route_calibration_absent_or_disabled_preserves_v1_scores() -> None:
    models = (
        _model("alpha", capability=0.9, price=1.0),
        _model("beta", capability=0.8, price=0.5),
    )
    absent = _single_decision(*models)
    disabled = _single_decision(
        *models,
        ranking_config=_calibration_ranking_config(enabled=False),
    )

    assert absent.model.identity == disabled.model.identity
    assert absent.trace["model_scores"] == disabled.trace["model_scores"]
    assert absent.trace["ranking_version"] == "router-single-ranking-v1"
    assert disabled.trace["ranking_version"] == "router-single-ranking-v1"
    assert absent.trace["selection_policy"] == "base_score_top1"
    assert disabled.trace["selection_policy"] == "base_score_top1"
    assert "quality_guard" not in disabled.trace


def test_single_route_calibration_does_not_change_multi_model_scoring() -> None:
    models = (
        _model("alpha", capability=0.9, price=1.0),
        _model("beta", capability=0.8, price=1.0),
    )
    calibrated_config = _calibration_ranking_config(
        prior_weight=1.0,
        priors={
            "test-provider:alpha": 0.1,
            "test-provider:beta": 0.9,
        },
    )
    baseline = _decision(*models, user_profile=None)
    calibrated = _decision(
        *models,
        user_profile=None,
        ranking_config=calibrated_config,
    )

    assert [model.identity for model in baseline.proposers] == [
        model.identity for model in calibrated.proposers
    ]
    assert baseline.aggregator.identity == calibrated.aggregator.identity
    assert baseline.trace["model_scores"] == calibrated.trace["model_scores"]


def test_single_route_model_prior_blend_changes_top1_and_emits_v2_trace() -> None:
    config = _calibration_ranking_config(
        prior_weight=1.0,
        priors={
            "test-provider:raw-high": 0.2,
            "test-provider:prior-high": 0.9,
        },
    )
    decision = _single_decision(
        _model("raw-high", capability=0.95, price=1.0),
        _model("prior-high", capability=0.70, price=1.0),
        ranking_config=config,
    )

    assert decision.model.model_id == "prior-high"
    assert decision.trace["ranking_version"] == "router-single-ranking-v2"
    assert decision.trace["selection_policy"] == "calibrated_base_score_top1"
    prior_score = next(
        row
        for row in decision.trace["model_scores"]
        if row["model"] == "prior-high"
    )
    calibration = prior_score["single_route_calibration"]
    assert calibration["model_quality_prior"] == pytest.approx(0.9)
    assert calibration["quality_prior_blended"] == pytest.approx(0.9)
    assert calibration["quality_calibrated_clean"] == pytest.approx(0.9)


def test_single_route_centered_residual_changes_top1() -> None:
    centered_residual = _calibration_residual(0.2)
    centered_residual["centers"]["capability"] = 0.5
    centered_residual["coefficients"]["capability"] = 0.5
    config = _calibration_ranking_config(
        residual_weight=1.0,
        residuals={
            "test-provider:raw-high": _calibration_residual(-0.3),
            "test-provider:residual-high": centered_residual,
        },
    )
    decision = _single_decision(
        _model("raw-high", capability=0.9, price=1.0),
        _model("residual-high", capability=0.7, price=1.0),
        ranking_config=config,
    )

    assert decision.model.model_id == "residual-high"
    residual_score = next(
        row
        for row in decision.trace["model_scores"]
        if row["model"] == "residual-high"
    )["single_route_calibration"]
    assert residual_score["match_features"] == {
        "capability": pytest.approx(0.7),
        "domain": pytest.approx(0.7),
        "tier": pytest.approx(0.7),
    }
    assert residual_score["residual_raw"] == pytest.approx(0.3)
    assert residual_score["residual_clipped"] == pytest.approx(0.3)


def test_single_route_predicted_total_cost_uses_effective_tier_token_mix() -> None:
    input_cheap = _model("input-cheap", capability=0.8)
    input_cheap["registry_facts"]["price"].update(
        {"input_per_million": 1.0, "output_per_million": 20.0}
    )
    output_cheap = _model("output-cheap", capability=0.8)
    output_cheap["registry_facts"]["price"].update(
        {"input_per_million": 10.0, "output_per_million": 1.0}
    )
    input_tokens = {str(tier): 1_000 for tier in range(1, 5)}
    output_tokens = {str(tier): 1_000 for tier in range(1, 5)}
    input_tokens["3"] = 10_000
    output_tokens["3"] = 100
    decision = _single_decision(
        input_cheap,
        output_cheap,
        analysis=_analysis(tier=3),
        ranking_config=_calibration_ranking_config(
            predicted_total_cost_enabled=True,
            predicted_cost_reference_usd=0.2,
            input_tokens_by_tier=input_tokens,
            output_tokens_by_tier=output_tokens,
        ),
    )

    assert decision.model.model_id == "input-cheap"
    cost_trace = next(
        row
        for row in decision.trace["model_scores"]
        if row["model"] == "input-cheap"
    )["single_route_calibration"]["predicted_total_cost"]
    assert cost_trace["effective_tier"] == "3"
    assert cost_trace["input_tokens"] == 10_000
    assert cost_trace["output_tokens"] == 100
    assert cost_trace["predicted_cost_usd"] == pytest.approx(0.012)
    assert cost_trace["normalized"] == pytest.approx(0.06)


def test_single_route_quality_guard_uses_strict_drop_boundary() -> None:
    quality_best = _model("quality-best", capability=0.8, price=100.0)
    cheap = _model("cheap", capability=0.8, price=0.0)
    common = {
        "prior_weight": 1.0,
        "priors": {
            "test-provider:quality-best": 0.875,
            "test-provider:cheap": 0.8125,
        },
        "quality_guard_enabled": True,
        "predicted_total_cost_enabled": True,
        "predicted_cost_reference_usd": 0.1,
    }
    boundary = _single_decision(
        quality_best,
        cheap,
        ranking_config=_calibration_ranking_config(
            **common,
            max_quality_drop=0.0625,
        ),
    )
    guarded = _single_decision(
        quality_best,
        cheap,
        ranking_config=_calibration_ranking_config(
            **common,
            max_quality_drop=0.06,
        ),
    )

    assert boundary.model.model_id == "cheap"
    assert boundary.trace["quality_guard"]["applied"] is False
    assert guarded.model.model_id == "quality-best"
    assert guarded.trace["quality_guard"]["applied"] is True
    assert guarded.trace["quality_guard"]["provisional_model"] == (
        "test-provider:cheap"
    )
    assert guarded.trace["quality_guard"]["selected_model"] == (
        guarded.model.identity
    )
    assert guarded.trace["model_scores"][0]["model"] == "cheap"
    assert guarded.trace["selected_model"] == guarded.model.identity
    assert guarded.trace["selected_P"] == [guarded.model.identity]


def test_single_route_calibration_requires_full_eligible_model_coverage() -> None:
    config = _calibration_ranking_config(
        prior_weight=0.5,
        priors={"test-provider:covered": 0.8},
    )

    with pytest.raises(DynamicRankingError, match="lacks eligible identities"):
        _single_decision(
            _model("covered"),
            _model("missing"),
            ranking_config=config,
        )


def test_single_route_calibrated_trace_is_replayable() -> None:
    config = _calibration_ranking_config(
        prior_weight=0.5,
        priors={
            "test-provider:alpha": 0.8,
            "test-provider:beta": 0.9,
        },
    )
    decision = _single_decision(
        _model("alpha", capability=0.9),
        _model("beta", capability=0.8),
        ranking_config=config,
    )

    assert single_ranking_trace_replay_reasons(decision.trace) == []


def test_pre_scope_single_route_calibrated_trace_remains_replayable() -> None:
    config = _calibration_ranking_config(
        prior_weight=0.5,
        priors={
            "test-provider:alpha": 0.8,
            "test-provider:beta": 0.9,
        },
    )
    trace = deepcopy(
        _single_decision(
            _model("alpha", capability=0.9),
            _model("beta", capability=0.8),
            ranking_config=config,
        ).trace
    )
    trace.pop("single_route_calibration_scope")
    trace["ranking_parameters"]["single_route_calibration"].pop(
        "activation_model_identities"
    )
    trace["ranking_config_hash"] = canonical_json_sha256(
        trace["ranking_parameters"]
    )

    assert single_ranking_trace_replay_reasons(trace) == []


def test_scoped_single_route_calibrated_trace_requires_scope_evidence() -> None:
    measured_models = (
        _model("deepseek/deepseek-v4-flash", provider="openrouter"),
        _model("deepseek/deepseek-v4-pro", provider="openrouter"),
        _model("qwen/qwen3.5-122b-a10b", provider="openrouter"),
        _model("qwen/qwen3.5-9b", provider="openrouter"),
    )
    trace = deepcopy(_single_decision(*measured_models).trace)
    trace.pop("single_route_calibration_scope")

    assert single_ranking_trace_replay_reasons(trace) == [
        "single_frozen_ranker_replay_mismatch"
    ]


def test_single_route_calibration_schema_rejects_unknown_policy_key() -> None:
    with pytest.raises(DynamicRankingError, match="unknown or missing keys"):
        ranking_config_snapshot(
            override={
                "single_route_calibration": {
                    "unknown_policy_key": True,
                }
            }
        )


def test_packaged_c1_baseline_parameters_are_exact() -> None:
    config = load_ranking_config()

    assert config["normalization"] == {
        "price_reference_usd_per_million": pytest.approx(5.5),
        "latency_reference_ms": pytest.approx(30_000.0),
        "price_input_weight": pytest.approx(0.30),
        "price_output_weight": pytest.approx(0.70),
    }
    assert config["penalties"]["task_cost_weights"] == {
        "low": pytest.approx(0.45),
        "medium": pytest.approx(0.10),
        "high": pytest.approx(0.04),
        "hard_limit": pytest.approx(0.28),
    }
    assert {
        key: config["task_match"][key]
        for key in ("capability_weight", "domain_weight", "tier_weight")
    } == {
        "capability_weight": pytest.approx(0.55),
        "domain_weight": pytest.approx(0.15),
        "tier_weight": pytest.approx(0.30),
    }
    calibration = config["single_route_calibration"]
    assert calibration["enabled"] is True
    assert calibration["model_prior_weight"] == pytest.approx(0.25)
    assert calibration["residual_weight"] == pytest.approx(0.0)
    assert calibration["quality_guard_enabled"] is False
    assert calibration["predicted_total_cost_enabled"] is False
    assert set(calibration["activation_model_identities"]) == set(
        calibration["model_quality_priors"]
    )


def test_packaged_c1_calibration_is_scoped_to_the_measured_four_model_pool() -> None:
    measured_models = (
        _model("deepseek/deepseek-v4-flash", provider="openrouter"),
        _model("deepseek/deepseek-v4-pro", provider="openrouter"),
        _model("qwen/qwen3.5-122b-a10b", provider="openrouter"),
        _model("qwen/qwen3.5-9b", provider="openrouter"),
    )

    measured = _single_decision(*measured_models)
    full = _single_decision(
        *measured_models,
        _model("openai/gpt-5.6-sol", provider="openrouter"),
    )

    assert measured.trace["ranking_version"] == "router-single-ranking-v2"
    assert measured.trace["single_route_calibration_scope"]["applied"] is True
    assert all("single_route_calibration" in row for row in measured.trace["model_scores"])
    assert full.trace["ranking_version"] == "router-single-ranking-v1"
    assert full.trace["single_route_calibration_scope"]["applied"] is False
    assert all("single_route_calibration" not in row for row in full.trace["model_scores"])
    assert single_ranking_trace_replay_reasons(full.trace) == []
