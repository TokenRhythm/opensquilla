from __future__ import annotations

import math
from typing import Any

import pytest

from opensquilla.observability.ensemble_execution_metrics import (
    build_ensemble_execution_metrics,
)
from opensquilla.observability.ensemble_execution_metrics_contract import (
    ALLOWED_ENSEMBLE_EXECUTION_METRIC_FIELDS,
    ENSEMBLE_EXECUTION_METRICS_SCHEMA,
    EnsembleExecutionMetricsContractError,
    validate_ensemble_execution_metrics,
)


def _minimal_metrics() -> dict[str, Any]:
    return {
        "schema": ENSEMBLE_EXECUTION_METRICS_SCHEMA,
        "terminal_outcome": "completed",
        "execution_status": "success",
        "selection_family": "unknown",
        "fallback_used_observed": False,
    }


def test_projector_rows_satisfy_the_transport_contract() -> None:
    rows = [
        build_ensemble_execution_metrics({}, terminal_outcome="completed"),
        build_ensemble_execution_metrics({}, terminal_outcome="failed"),
        build_ensemble_execution_metrics(
            {
                "fallback_used": True,
                "selection_strategy": "router_dynamic",
                "ranking_stage_observability": {
                    "schema": (
                        "opensquilla.router-dynamic-ranking-stage-observability/v1"
                    ),
                    "snapshot_build_ms": 1,
                    "hard_filter_ms": 2,
                    "score_ms": 3,
                    "packaged_template_cache_hit": True,
                },
                "selection_plan": {
                    "task_analyzer": {
                        "source": "llm_provider",
                        "schema_valid": True,
                        "chain": {
                            "attempt_outcomes": [
                                {
                                    "outcome": "success",
                                    "physical_request_count": 1,
                                }
                            ],
                            "selected_index": 0,
                            "exhausted": False,
                            "deadline": {
                                "configured_seconds": 2.0,
                                "elapsed_seconds": 0.25,
                                "remaining_seconds": 1.75,
                                "expired": False,
                            },
                        },
                    },
                    "runtime_health_filter": {
                        "enabled": True,
                        "requires_rerank": False,
                        "input_candidate_count": 2,
                        "fresh_deployment_count": 2,
                        "active_unavailable_by_role": {
                            "proposer": 0,
                            "aggregator": 0,
                        },
                        "filtered_by_role": {"proposer": 0, "aggregator": 0},
                        "half_open_by_role": {"proposer": 0, "aggregator": 0},
                        "never_strand_minimum_by_role": {
                            "proposer": 1,
                            "aggregator": 1,
                        },
                        "never_strand_exempt_identities_by_role": {
                            "proposer": [],
                            "aggregator": [],
                        },
                        "never_strand": False,
                    },
                },
                "aggregator_recovery": {
                    "attempts": [
                        {
                            "kind": "primary",
                            "outcome": "succeeded",
                            "request_started": True,
                            "physical_request_count": 1,
                            "usage": {
                                "schema": (
                                    "opensquilla.ensemble-aggregator-"
                                    "attempt-usage/v1"
                                ),
                                "physical_request_count": 1,
                                "usage_row_count": 1,
                                "usage_missing_count": 0,
                                "input_tokens": 3,
                                "output_tokens": 2,
                                "reasoning_tokens": 1,
                                "cached_tokens": 0,
                                "cache_write_tokens": 0,
                                "cache_hit_request_count": 0,
                                "billed_cost_usd": 0.1,
                                "cost_source_kind": "provider_billed",
                            },
                        }
                    ],
                    "selected_kind": "primary",
                    "fallback_index": 0,
                    "success": True,
                    "exhausted": False,
                    "degraded": False,
                    "continuation_count": 0,
                    "same_model_recovery_count": 0,
                },
                "proposer_quorum": {
                    "quorum_reached": True,
                    "time_to_quorum_ms": 12,
                    "grace_elapsed_ms": 2,
                    "pending_at_quorum": 0,
                    "cancellation": {"requested_task_count": 0},
                    "cleanup": {
                        "awaited_task_count": 1,
                        "completed_task_count": 1,
                        "lingering_task_count": 0,
                        "stream_close_proven_count": 1,
                        "stream_close_unproven_count": 0,
                    },
                },
            },
            terminal_outcome="completed",
        ),
    ]

    for row in rows:
        validate_ensemble_execution_metrics(row)


def test_canary_projector_row_satisfies_fixed_reason_and_task_fields() -> None:
    reasons = {
        "canary_policy_invalid": 0,
        "canary_rollout_disabled": 0,
        "canary_decision_id_missing": 0,
        "canary_task_ineligible": 0,
        "canary_global_cohort_excluded": 0,
        "canary_role_disabled": 1,
        "canary_role_cohort_excluded": 0,
        "canary_health_unhealthy": 0,
        "canary_role_unsupported": 0,
        "canary_reliability_coverage_insufficient": 0,
        "canary_reliability_threshold_exceeded": 0,
        "canary_candidate_cap": 0,
    }
    row = build_ensemble_execution_metrics(
        {
            "selection_plan": {
                "canary_rollout": {
                    "schema": "opensquilla.ensemble-canary-rollout/v1",
                    "enabled": True,
                    "config_valid": True,
                    "input_canary_count": 1,
                    "admitted_by_role": {"proposer": 1, "aggregator": 0},
                    "task_gate": {
                        "analyzer_source_eligible": True,
                        "schema_valid": True,
                        "confidence_eligible": True,
                        "risk": "low",
                        "eligible": True,
                    },
                    "reason_counts": reasons,
                }
            }
        },
        terminal_outcome="completed",
    )

    validate_ensemble_execution_metrics(row)


def test_persistent_canary_projector_row_satisfies_closed_transport_contract() -> None:
    receipt = {
        "schema": "opensquilla.ensemble-canary-persistent-rollout/v1",
        "role": "proposer",
        "available": True,
        "allowed": True,
        "probe": False,
        "admission_reason": "active",
        "state_before": "active",
        "mutation_available": True,
        "mutation_applied": True,
        "mutation_reason": "applied",
        "state_after": "active",
        "latch_reason": "none",
        "provider_outcome": "success",
        "usage_outcome": "observed",
        "cancelled_before_request": False,
        "rollback_transition": False,
        "recovery_transition": False,
        "recovery_successes": 0,
    }
    row = build_ensemble_execution_metrics(
        {
            "canary_persistent_rollout": {
                "schema": "opensquilla.ensemble-canary-persistent-rollout/v1",
                "enabled": True,
                "receipt_count": 1,
                "receipts": [receipt],
            }
        },
        terminal_outcome="completed",
    )

    validate_ensemble_execution_metrics(row)
    assert row["canary_persistent_rollout_projection_complete"] is True
    assert row["canary_persistent_rollout_provider_success_count"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        {"model": "private-model"},
        {"admission_observation_count": {"raw": "trace"}},
        {"admission_observation_count": True},
        {"admission_observation_count": -1},
        {"admission_observation_count": 1 << 63},
        {"proposer_billed_cost_usd": math.nan},
        {"proposer_billed_cost_usd": math.inf},
        {"selection_family": "private-deployment"},
    ],
)
def test_contract_rejects_unknown_nested_unbounded_and_identity_values(
    mutation: dict[str, Any],
) -> None:
    with pytest.raises(EnsembleExecutionMetricsContractError):
        validate_ensemble_execution_metrics(_minimal_metrics() | mutation)


def test_contract_requires_core_fields_and_consistent_terminal_status() -> None:
    missing = _minimal_metrics()
    missing.pop("schema")
    with pytest.raises(EnsembleExecutionMetricsContractError, match="missing"):
        validate_ensemble_execution_metrics(missing)

    inconsistent = _minimal_metrics() | {"execution_status": "failed"}
    with pytest.raises(EnsembleExecutionMetricsContractError, match="disagree"):
        validate_ensemble_execution_metrics(inconsistent)


@pytest.mark.parametrize(
    "mutation",
    [
        {"ranking_snapshot_build_ms": -1},
        {"ranking_hard_filter_ms": True},
        {"ranking_score_ms": 1 << 63},
        {"ranking_packaged_template_cache_hit": 1},
    ],
)
def test_ranking_stage_transport_contract_rejects_bad_scalar_values(
    mutation: dict[str, Any],
) -> None:
    with pytest.raises(EnsembleExecutionMetricsContractError):
        validate_ensemble_execution_metrics(_minimal_metrics() | mutation)


@pytest.mark.parametrize(
    "mutation",
    [
        {"aggregator_usage_physical_request_count": -1},
        {"aggregator_usage_missing_count": True},
        {"aggregator_input_tokens": 1 << 63},
        {"aggregator_billed_cost_usd": math.nan},
        {"aggregator_billed_cost_usd": math.inf},
        {"aggregator_cost_source_kind": "private-provider"},
    ],
)
def test_aggregator_usage_transport_contract_rejects_bad_scalar_values(
    mutation: dict[str, Any],
) -> None:
    with pytest.raises(EnsembleExecutionMetricsContractError):
        validate_ensemble_execution_metrics(_minimal_metrics() | mutation)


def test_allowlist_excludes_raw_trace_identity_and_hash_keys() -> None:
    forbidden = {
        "model",
        "provider",
        "deployment",
        "session_id",
        "task_id",
        "user_id",
        "tenant_id",
        "policy_sha256",
        "root_subject_sha256",
        "selection_plan",
        "candidates",
        "final_request",
        "prompt",
        "output",
        "reasoning",
        "error_text",
        "response_id",
    }

    assert forbidden.isdisjoint(ALLOWED_ENSEMBLE_EXECUTION_METRIC_FIELDS)
