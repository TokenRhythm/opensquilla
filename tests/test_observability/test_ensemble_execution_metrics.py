from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
import structlog.testing

from opensquilla.engine.routing.health import ProviderHealthLedger
from opensquilla.gateway.config import GatewayConfig
from opensquilla.observability import ensemble_execution_metrics as metrics_module
from opensquilla.observability.ensemble_execution_metrics import (
    TRACE_SIZE_CAP_BYTES,
    TRACE_SIZE_VISIT_CAP,
    build_ensemble_execution_metrics,
    log_ensemble_execution_metrics,
    log_ensemble_execution_metrics_once,
)
from opensquilla.provider import ensemble as ensemble_provider
from opensquilla.provider.ranking_router import TaskAnalysisResult


def test_terminal_trace_projects_content_free_bounded_stage_metrics() -> None:
    private_text = "sk-private-never-emit user prompt and hidden reasoning"
    trace: dict[str, Any] = {
        "mode": "b5_fusion",
        "selection_strategy": "router_dynamic",
        "fallback_used": False,
        "llm_request_count": 4,
        "physical_request_count": 4,
        "usage_missing_count": 1,
        "selection_plan": {
            "strategy": "router_dynamic",
            "request_context": private_text,
        },
        "candidates": [
            {
                "request_started": True,
                "elapsed_ms": 120,
                "text": private_text,
                "execution": {
                    "admission": {
                        "outcome": "admitted",
                        "wait_ms": 7,
                    }
                },
            },
            {
                "request_started": False,
                "elapsed_ms": 999,
                "execution": {
                    "admission": {
                        "outcome": "timeout",
                        "wait_ms": 50,
                    }
                },
            },
            {
                "request_started": True,
                "elapsed_ms": 250,
            },
        ],
        "proposer_quorum": {
            "quorum_reached": True,
            "time_to_quorum_ms": 115,
            "grace_elapsed_ms": 9,
            "pending_at_quorum": 2,
            "cancellation": {"requested_task_count": 1},
            "cleanup": {
                "awaited_task_count": 1,
                "completed_task_count": 1,
                "lingering_task_count": 0,
                "stream_close_proven_count": 1,
                "stream_close_unproven_count": 0,
            },
        },
        "proposer_recovery": {
            "additional_physical_requests_started": 2,
        },
        "final_request": {
            "execution": {
                "admission": {
                    "outcome": "admitted",
                    "wait_ms": 3,
                }
            },
            "output": private_text,
        },
    }
    before = deepcopy(trace)

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert trace == before
    assert metrics == {
        "schema": "opensquilla.ensemble-execution-metrics/v1",
        "terminal_outcome": "completed",
        "execution_status": "success",
        "selection_family": "router_dynamic",
        "fallback_used_observed": True,
        "fallback_used": False,
        "task_analyzer_observed": False,
        "aggregator_recovery_observed": False,
        "aggregator_stage_observed": False,
        "aggregator_physical_request_count_observed": False,
        "runtime_health_filter_observed": False,
        "canary_rollout_observed": False,
        "canary_rollout_projection_complete": False,
        "canary_rollout_conservation_observed": False,
        "canary_physical_budget_observed": False,
        "canary_physical_budget_projection_complete": False,
        "canary_physical_budget_accounting_observed": False,
        "canary_physical_budget_conservation_observed": False,
        "canary_physical_budget_exhausted_observed": False,
        "aggregator_final_request_usage_container_observed": False,
        "aggregator_final_request_usage_projection_complete": False,
        "aggregator_final_request_usage_observed": False,
        "trace_size_observed": True,
        "trace_compact_json_bytes": len(
            json.dumps(
                trace,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "trace_compact_json_bytes_capped": False,
        "trace_compact_json_bytes_cap": TRACE_SIZE_CAP_BYTES,
        "trace_compact_json_visit_cap": TRACE_SIZE_VISIT_CAP,
        "proposer_candidates_observed": True,
        "proposer_candidate_count": 3,
        "proposer_candidate_scan_count": 3,
        "proposer_candidate_scan_capped": False,
        "proposer_elapsed_observation_count": 2,
        "proposer_candidate_elapsed_ms_max": 250,
        "proposer_candidate_elapsed_ms_total": 370,
        "proposer_runtime_health_observed": False,
        "proposer_runtime_health_observation_count": 0,
        "proposer_runtime_health_tracked_observation_count": 0,
        "proposer_runtime_health_tracked_count": 0,
        "proposer_runtime_health_state_observation_count": 0,
        "proposer_runtime_health_healthy_count": 0,
        "proposer_runtime_health_benched_count": 0,
        "proposer_runtime_health_half_open_count": 0,
        "proposer_runtime_health_unknown_state_count": 0,
        "proposer_runtime_health_probe_observation_count": 0,
        "proposer_runtime_health_probe_count": 0,
        "proposer_runtime_health_benched_deferred_count": 0,
        "proposer_runtime_health_half_open_busy_deferred_count": 0,
        "proposer_runtime_health_unknown_deferred_count": 0,
        "proposer_logical_terminal_http_status_observed": False,
        "proposer_logical_terminal_http_status_observation_count": 0,
        "proposer_logical_terminal_rate_limited_count": 0,
        "proposer_logical_terminal_upstream_5xx_count": 0,
        "proposer_physical_request_count_observation_count": 0,
        "proposer_physical_request_count_observed": False,
        "proposer_unknown_usage_count_observation_count": 0,
        "proposer_unknown_usage_count_observed": False,
        "proposer_usage_observed": False,
        "proposer_usage_projection_complete": False,
        "proposer_usage_container_observation_count": 0,
        "proposer_usage_row_scan_count": 0,
        "proposer_usage_row_scan_capped": False,
        "proposer_usage_receipt_count": 0,
        "proposer_usage_unknown_row_count": 0,
        "proposer_usage_malformed_row_count": 0,
        "proposer_usage_row_count_observed": False,
        "proposer_input_tokens_observation_count": 0,
        "proposer_output_tokens_observation_count": 0,
        "proposer_reasoning_tokens_observation_count": 0,
        "proposer_cache_read_tokens_observation_count": 0,
        "proposer_cache_write_tokens_observation_count": 0,
        "proposer_billed_cost_usd_observation_count": 0,
        "admission_observation_count": 3,
        "admission_wait_observation_count": 3,
        "admission_timeout_count": 1,
        "admission_rejected_count": 0,
        "admission_wait_ms_max": 50,
        "admission_wait_ms_total": 60,
        "proposer_admission_observed": True,
        "proposer_admission_projection_complete": False,
        "proposer_admission_observation_count": 2,
        "proposer_admission_wait_observation_count": 2,
        "proposer_admission_admitted_count_lower_bound": 1,
        "proposer_admission_timeout_count_lower_bound": 1,
        "proposer_admission_rejected_count_lower_bound": 0,
        "proposer_admission_wait_ms_max_lower_bound": 50,
        "proposer_admission_wait_ms_total_lower_bound": 57,
        "aggregator_admission_observed": False,
        "aggregator_admission_projection_complete": False,
        "quorum_observed": True,
        "quorum_reached_observed": True,
        "quorum_reached": True,
        "time_to_quorum_ms": 115,
        "quorum_grace_elapsed_ms": 9,
        "pending_at_quorum": 2,
        "cleanup_observed": True,
        "quorum_cancel_requested_task_count": 1,
        "cleanup_awaited_task_count": 1,
        "cleanup_completed_task_count": 1,
        "cleanup_lingering_task_count": 0,
        "cleanup_stream_close_proven_count": 1,
        "cleanup_stream_close_unproven_count": 0,
        "proposer_recovery_observed": True,
        "proposer_recovery_calls": 2,
        "physical_request_count_observed": True,
        "physical_request_count": 4,
        "unknown_usage_count_observed": True,
        "unknown_usage_count": 1,
    }
    assert private_text not in json.dumps(metrics, sort_keys=True)


def _canary_reason_counts(**overrides: int) -> dict[str, int]:
    counts = {
        "canary_policy_invalid": 0,
        "canary_rollout_disabled": 0,
        "canary_decision_id_missing": 0,
        "canary_task_ineligible": 0,
        "canary_global_cohort_excluded": 0,
        "canary_role_disabled": 0,
        "canary_role_cohort_excluded": 0,
        "canary_health_unhealthy": 0,
        "canary_role_unsupported": 0,
        "canary_reliability_coverage_insufficient": 0,
        "canary_reliability_threshold_exceeded": 0,
        "canary_candidate_cap": 0,
    }
    counts.update(overrides)
    return counts


def test_canary_trace_projects_only_fixed_low_cardinality_evidence() -> None:
    private_text = "private model identity, policy hash, bucket, and reason"
    trace = {
        "selection_plan": {
            "canary_rollout": {
                "schema": "opensquilla.ensemble-canary-rollout/v1",
                "enabled": True,
                "config_valid": True,
                "policy_version": private_text,
                "policy_sha256": private_text,
                "root_subject_sha256": private_text,
                "global_bucket": private_text,
                "role_bucket": {"proposer": private_text},
                "input_canary_count": 12,
                "admitted_by_role": {"proposer": 1, "aggregator": 0},
                "task_gate": {
                    "analyzer_source_eligible": True,
                    "schema_valid": True,
                    "confidence_eligible": True,
                    "risk": "low",
                    "eligible": True,
                    "model": private_text,
                },
                "reason_counts": {
                    **_canary_reason_counts(
                        canary_role_disabled=12,
                        canary_candidate_cap=11,
                    ),
                    private_text: 999,
                },
            }
        },
        "canary_physical_budget": {
            "schema": "opensquilla.ensemble-canary-physical-budget/v1",
            "limit": 1,
            "committed": 0,
            "reserved": 0,
            "rejected": 2,
            "refunded": 3,
            "identity": private_text,
        },
    }
    before = deepcopy(trace)

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert trace == before
    assert metrics["canary_rollout_observed"] is True
    assert metrics["canary_rollout_projection_complete"] is True
    assert metrics["canary_rollout_conservation_observed"] is True
    assert metrics["canary_rollout_conservation_valid"] is True
    assert metrics["canary_rollout_enabled"] is True
    assert metrics["canary_rollout_config_valid"] is True
    assert metrics["canary_rollout_input_canary_count"] == 12
    assert metrics["canary_rollout_proposer_admitted_count"] == 1
    assert metrics["canary_rollout_aggregator_admitted_count"] == 0
    assert metrics["canary_task_analyzer_source_eligible"] is True
    assert metrics["canary_task_schema_valid"] is True
    assert metrics["canary_task_confidence_eligible"] is True
    assert metrics["canary_task_risk"] == "low"
    assert metrics["canary_task_eligible"] is True
    assert metrics["canary_rollout_reason_counts_observed"] is True
    assert metrics["canary_rollout_reason_policy_invalid_count"] == 0
    assert metrics["canary_rollout_reason_candidate_cap_count"] == 11
    assert metrics["canary_physical_budget_projection_complete"] is True
    assert metrics["canary_physical_budget_conservation_valid"] is True
    assert metrics["canary_physical_budget_limit"] == 1
    assert metrics["canary_physical_budget_committed"] == 0
    assert metrics["canary_physical_budget_reserved"] == 0
    assert metrics["canary_physical_budget_rejected"] == 2
    assert metrics["canary_physical_budget_refunded"] == 3
    assert metrics["canary_physical_budget_exhausted"] is True
    assert private_text not in json.dumps(metrics, sort_keys=True)
    assert not any(
        forbidden in key
        for key in metrics
        for forbidden in (
            "bucket",
            "identity",
            "policy_version",
            "root_subject",
            "sha256",
        )
    )


def test_real_canary_writer_trace_projects_without_private_trace_fields() -> None:
    policy = GatewayConfig(
        llm_ensemble={
            "canary_rollout": {
                "enabled": True,
                "global_basis_points": 10_000,
                "proposer": {
                    "basis_points": 10_000,
                    "max_candidates_per_decision": 1,
                },
            }
        }
    ).llm_ensemble.canary_rollout.model_dump(mode="json")
    snapshot = {
        "models": [
            {
                "registry_facts": {
                    "provider": "fake",
                    "model_id": "private-canary-model",
                    "status": "canary",
                    "roles": ["proposer", "aggregator"],
                    "health": "healthy",
                },
                "online_profile": {
                    "role_reliability": {
                        "proposer": {"success": 20, "failure": 0},
                    }
                },
            }
        ]
    }
    ledger = ProviderHealthLedger()
    ledger.record_success("fake", "private-canary-model")
    rollout = ensemble_provider._apply_canary_candidate_filter(  # noqa: SLF001
        snapshot,
        rollout_config=policy,
        task_analysis=TaskAnalysisResult(
            profile={"constraints": {"risk": "low"}},
            source="llm_provider",
            schema_valid=True,
            confidence=0.9,
        ),
        decision_id="private-root-decision",
        health_ledger=ledger,
    )
    assert rollout is not None
    budget = ensemble_provider._CanaryPhysicalRequestBudget()  # noqa: SLF001
    reservation = budget.reserve()
    assert reservation is not None
    reservation.commit()
    assert budget.reserve() is None
    trace = {
        "selection_plan": {"canary_rollout": rollout},
        "canary_physical_budget": {
            "schema": "opensquilla.ensemble-canary-physical-budget/v1",
            **budget.snapshot(),
        },
    }
    before = deepcopy(trace)

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert trace == before
    assert metrics["canary_rollout_projection_complete"] is True
    assert metrics["canary_rollout_conservation_valid"] is True
    assert metrics["canary_rollout_input_canary_count"] == 1
    assert metrics["canary_rollout_proposer_admitted_count"] == 1
    assert metrics["canary_task_eligible"] is True
    assert metrics["canary_rollout_reason_role_disabled_count"] == 1
    assert metrics["canary_physical_budget_committed"] == 1
    assert metrics["canary_physical_budget_rejected"] == 1
    assert metrics["canary_physical_budget_exhausted"] is True
    serialized = json.dumps(metrics, sort_keys=True)
    for private_value in (
        "private-canary-model",
        "private-root-decision",
        rollout["policy_sha256"],
        rollout["root_subject_sha256"],
    ):
        assert private_value not in serialized


def test_malformed_canary_metrics_fail_open_and_omit_untrusted_values() -> None:
    private_text = "private future risk, reason, identity, and hash"
    reason_counts = _canary_reason_counts()
    reason_counts["canary_policy_invalid"] = True
    trace = {
        "selection_plan": {
            "canary_rollout": {
                "schema": "opensquilla.ensemble-canary-rollout/v1",
                "enabled": 1,
                "config_valid": "true",
                "input_canary_count": True,
                "admitted_by_role": {"proposer": -1, "aggregator": 0},
                "task_gate": {
                    "analyzer_source_eligible": 1,
                    "schema_valid": True,
                    "confidence_eligible": False,
                    "risk": private_text,
                    "eligible": "false",
                },
                "reason_counts": {**reason_counts, private_text: 1},
                "policy_sha256": private_text,
            }
        },
        "canary_physical_budget": {
            "schema": "opensquilla.ensemble-canary-physical-budget/v1",
            "limit": 1,
            "committed": 1,
            "reserved": 1,
            "rejected": 0,
            "refunded": 0,
            "identity": private_text,
        },
    }
    before = deepcopy(trace)

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="failed",
    )

    assert trace == before
    assert metrics["canary_rollout_observed"] is True
    assert metrics["canary_rollout_projection_complete"] is False
    assert metrics["canary_rollout_conservation_observed"] is False
    assert metrics["canary_rollout_enabled_observed"] is False
    assert metrics["canary_rollout_config_valid_observed"] is False
    assert metrics["canary_rollout_input_canary_count_observed"] is False
    assert metrics["canary_rollout_admitted_counts_observed"] is False
    assert metrics["canary_task_analyzer_source_eligible_observed"] is False
    assert metrics["canary_task_schema_valid"] is True
    assert metrics["canary_task_confidence_eligible"] is False
    assert metrics["canary_task_eligible_observed"] is False
    assert metrics["canary_task_risk_observed"] is False
    assert "canary_task_risk" not in metrics
    assert metrics["canary_rollout_reason_counts_observed"] is False
    assert not any(
        key.startswith("canary_rollout_reason_")
        and key.endswith("_count")
        for key in metrics
    )
    assert metrics["canary_physical_budget_observed"] is True
    assert metrics["canary_physical_budget_accounting_observed"] is True
    assert metrics["canary_physical_budget_conservation_observed"] is True
    assert metrics["canary_physical_budget_conservation_valid"] is False
    assert metrics["canary_physical_budget_projection_complete"] is False
    assert metrics["canary_physical_budget_exhausted_observed"] is False
    assert "canary_physical_budget_committed" not in metrics
    assert private_text not in json.dumps(metrics, sort_keys=True)

    unknown_schema = build_ensemble_execution_metrics(
        {
            "selection_plan": {
                "canary_rollout": {
                    "schema": "opensquilla.ensemble-canary-rollout/v2",
                    "enabled": True,
                    "identity": private_text,
                }
            },
            "canary_physical_budget": {
                "schema": "opensquilla.ensemble-canary-physical-budget/v2",
                "limit": 1,
                "committed": 0,
                "reserved": 0,
                "rejected": 0,
                "refunded": 0,
            },
        },
        terminal_outcome="completed",
    )
    assert unknown_schema["canary_rollout_observed"] is False
    assert unknown_schema["canary_rollout_projection_complete"] is False
    assert unknown_schema["canary_rollout_conservation_observed"] is False
    assert unknown_schema["canary_physical_budget_observed"] is False
    assert unknown_schema["canary_physical_budget_projection_complete"] is False
    assert unknown_schema["canary_physical_budget_accounting_observed"] is False


def test_canary_rollout_rejects_contradictory_admission_counts() -> None:
    trace = {
        "selection_plan": {
            "canary_rollout": {
                "schema": "opensquilla.ensemble-canary-rollout/v1",
                "enabled": True,
                "config_valid": True,
                "input_canary_count": 0,
                "admitted_by_role": {"proposer": 99, "aggregator": 7},
                "task_gate": {
                    "analyzer_source_eligible": True,
                    "schema_valid": True,
                    "confidence_eligible": True,
                    "risk": "low",
                    "eligible": True,
                },
                "reason_counts": _canary_reason_counts(),
            }
        }
    }

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert metrics["canary_rollout_observed"] is True
    assert metrics["canary_rollout_input_canary_count_observed"] is True
    assert metrics["canary_rollout_admitted_counts_observed"] is False
    assert metrics["canary_rollout_projection_complete"] is False
    assert metrics["canary_rollout_conservation_observed"] is True
    assert metrics["canary_rollout_conservation_valid"] is False
    assert "canary_rollout_proposer_admitted_count" not in metrics
    assert "canary_rollout_aggregator_admitted_count" not in metrics


@pytest.mark.parametrize(
    ("proposer_admitted", "enabled", "reason_counts"),
    [
        (
            0,
            False,
            _canary_reason_counts(
                canary_role_disabled=1,
                canary_rollout_disabled=1,
            ),
        ),
        (1, True, _canary_reason_counts(canary_role_disabled=1)),
    ],
)
def test_canary_rollout_conservation_accepts_valid_zero_or_one_admission(
    proposer_admitted: int,
    enabled: bool,
    reason_counts: dict[str, int],
) -> None:
    trace = {
        "selection_plan": {
            "canary_rollout": {
                "schema": "opensquilla.ensemble-canary-rollout/v1",
                "enabled": enabled,
                "config_valid": True,
                "input_canary_count": 1,
                "admitted_by_role": {
                    "proposer": proposer_admitted,
                    "aggregator": 0,
                },
                "task_gate": {
                    "analyzer_source_eligible": True,
                    "schema_valid": True,
                    "confidence_eligible": True,
                    "risk": "low",
                    "eligible": True,
                },
                "reason_counts": reason_counts,
            }
        }
    }

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert metrics["canary_rollout_conservation_observed"] is True
    assert metrics["canary_rollout_conservation_valid"] is True
    assert metrics["canary_rollout_projection_complete"] is True
    assert metrics["canary_rollout_proposer_admitted_count"] == (
        proposer_admitted
    )


@pytest.mark.parametrize(
    ("enabled", "config_valid", "task_eligible"),
    [
        (False, True, True),
        (True, False, True),
        (True, True, False),
    ],
)
def test_canary_rollout_conservation_rejects_admission_when_gates_are_off(
    enabled: bool,
    config_valid: bool,
    task_eligible: bool,
) -> None:
    trace = {
        "selection_plan": {
            "canary_rollout": {
                "schema": "opensquilla.ensemble-canary-rollout/v1",
                "enabled": enabled,
                "config_valid": config_valid,
                "input_canary_count": 1,
                "admitted_by_role": {"proposer": 1, "aggregator": 0},
                "task_gate": {
                    "analyzer_source_eligible": True,
                    "schema_valid": True,
                    "confidence_eligible": True,
                    "risk": "low",
                    "eligible": task_eligible,
                },
                "reason_counts": _canary_reason_counts(
                    canary_role_disabled=1
                ),
            }
        }
    }

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert metrics["canary_rollout_admitted_counts_observed"] is True
    assert metrics["canary_rollout_conservation_observed"] is True
    assert metrics["canary_rollout_conservation_valid"] is False
    assert metrics["canary_rollout_projection_complete"] is False


@pytest.mark.parametrize(
    "ineligible_task_field",
    ["analyzer_source_eligible", "schema_valid", "confidence_eligible"],
)
def test_canary_rollout_conservation_rejects_incoherent_task_gate(
    ineligible_task_field: str,
) -> None:
    task_gate = {
        "analyzer_source_eligible": True,
        "schema_valid": True,
        "confidence_eligible": True,
        "risk": "low",
        "eligible": True,
    }
    task_gate[ineligible_task_field] = False
    metrics = build_ensemble_execution_metrics(
        {
            "selection_plan": {
                "canary_rollout": {
                    "schema": "opensquilla.ensemble-canary-rollout/v1",
                    "enabled": False,
                    "config_valid": True,
                    "input_canary_count": 1,
                    "admitted_by_role": {
                        "proposer": 0,
                        "aggregator": 0,
                    },
                    "task_gate": task_gate,
                    "reason_counts": _canary_reason_counts(
                        canary_role_disabled=1,
                        canary_rollout_disabled=1,
                    ),
                }
            }
        },
        terminal_outcome="completed",
    )

    assert metrics["canary_rollout_conservation_observed"] is True
    assert metrics["canary_rollout_conservation_valid"] is False
    assert metrics["canary_rollout_projection_complete"] is False


def test_canary_rollout_conservation_requires_nonempty_input_receipt() -> None:
    metrics = build_ensemble_execution_metrics(
        {
            "selection_plan": {
                "canary_rollout": {
                    "schema": "opensquilla.ensemble-canary-rollout/v1",
                    "enabled": False,
                    "config_valid": True,
                    "input_canary_count": 0,
                    "admitted_by_role": {
                        "proposer": 0,
                        "aggregator": 0,
                    },
                    "task_gate": {
                        "analyzer_source_eligible": True,
                        "schema_valid": True,
                        "confidence_eligible": True,
                        "risk": "low",
                        "eligible": True,
                    },
                    "reason_counts": _canary_reason_counts(),
                }
            }
        },
        terminal_outcome="completed",
    )

    assert metrics["canary_rollout_admitted_counts_observed"] is True
    assert metrics["canary_rollout_conservation_observed"] is True
    assert metrics["canary_rollout_conservation_valid"] is False
    assert metrics["canary_rollout_projection_complete"] is False


def test_canary_rollout_conservation_rejects_enabled_invalid_config() -> None:
    metrics = build_ensemble_execution_metrics(
        {
            "selection_plan": {
                "canary_rollout": {
                    "schema": "opensquilla.ensemble-canary-rollout/v1",
                    "enabled": True,
                    "config_valid": False,
                    "input_canary_count": 1,
                    "admitted_by_role": {
                        "proposer": 0,
                        "aggregator": 0,
                    },
                    "task_gate": {
                        "analyzer_source_eligible": True,
                        "schema_valid": True,
                        "confidence_eligible": True,
                        "risk": "low",
                        "eligible": True,
                    },
                    "reason_counts": _canary_reason_counts(
                        canary_role_disabled=1,
                        canary_policy_invalid=1,
                    ),
                }
            }
        },
        terminal_outcome="completed",
    )

    assert metrics["canary_rollout_conservation_observed"] is True
    assert metrics["canary_rollout_conservation_valid"] is False
    assert metrics["canary_rollout_projection_complete"] is False


def test_canary_rollout_conservation_requires_aggregator_role_exclusion(
) -> None:
    metrics = build_ensemble_execution_metrics(
        {
            "selection_plan": {
                "canary_rollout": {
                    "schema": "opensquilla.ensemble-canary-rollout/v1",
                    "enabled": False,
                    "config_valid": True,
                    "input_canary_count": 1,
                    "admitted_by_role": {
                        "proposer": 0,
                        "aggregator": 0,
                    },
                    "task_gate": {
                        "analyzer_source_eligible": True,
                        "schema_valid": True,
                        "confidence_eligible": True,
                        "risk": "low",
                        "eligible": True,
                    },
                    # Total still matches 2*input, but the mandatory one
                    # aggregator role_disabled occurrence is absent.
                    "reason_counts": _canary_reason_counts(
                        canary_rollout_disabled=2
                    ),
                }
            }
        },
        terminal_outcome="completed",
    )

    assert metrics["canary_rollout_conservation_observed"] is True
    assert metrics["canary_rollout_conservation_valid"] is False
    assert metrics["canary_rollout_projection_complete"] is False


@pytest.mark.parametrize("reason_total", [1, 3])
def test_canary_rollout_conservation_rejects_reason_sum_drift(
    reason_total: int,
) -> None:
    trace = {
        "selection_plan": {
            "canary_rollout": {
                "schema": "opensquilla.ensemble-canary-rollout/v1",
                "enabled": False,
                "config_valid": True,
                "input_canary_count": 1,
                "admitted_by_role": {"proposer": 0, "aggregator": 0},
                "task_gate": {
                    "analyzer_source_eligible": True,
                    "schema_valid": True,
                    "confidence_eligible": True,
                    "risk": "low",
                    "eligible": True,
                },
                "reason_counts": _canary_reason_counts(
                    canary_role_disabled=reason_total
                ),
            }
        }
    }

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert metrics["canary_rollout_conservation_observed"] is True
    assert metrics["canary_rollout_conservation_valid"] is False
    assert metrics["canary_rollout_projection_complete"] is False


def test_canary_budget_rejection_requires_prior_reservation_evidence() -> None:
    metrics = build_ensemble_execution_metrics(
        {
            "canary_physical_budget": {
                "schema": (
                    "opensquilla.ensemble-canary-physical-budget/v1"
                ),
                "limit": 1,
                "committed": 0,
                "reserved": 0,
                "rejected": 1,
                "refunded": 0,
            }
        },
        terminal_outcome="failed",
    )

    assert metrics["canary_physical_budget_accounting_observed"] is True
    assert metrics["canary_physical_budget_conservation_observed"] is True
    assert metrics["canary_physical_budget_conservation_valid"] is False
    assert metrics["canary_physical_budget_projection_complete"] is False
    assert metrics["canary_physical_budget_exhausted_observed"] is False
    assert "canary_physical_budget_rejected" not in metrics


def test_terminal_trace_projects_analyzer_and_aggregator_recovery() -> None:
    private_text = "sk-private model/user/reasoning/error text"
    trace: dict[str, Any] = {
        "selection_plan": {
            "task_analyzer": {
                "source": "llm_provider",
                "schema_valid": True,
                "provider": private_text,
                "model": private_text,
                "fallback_reason": private_text,
                "chain": {
                    "attempt_outcomes": [
                        {
                            "outcome": "failed",
                            "physical_request_count": 1,
                            "reason": private_text,
                        },
                        {
                            "outcome": "success",
                            "physical_request_count": 1,
                            "model": private_text,
                        },
                    ],
                    "selected_index": 1,
                    "exhausted": False,
                    "deadline": {
                        "configured_seconds": 2.0,
                        "elapsed_seconds": 0.1254,
                        "remaining_seconds": 1.8746,
                        "expired": False,
                    },
                },
            }
        },
        "aggregator_recovery": {
            "attempts": [
                {
                    "kind": "primary",
                    "request_started": True,
                    "physical_request_count": 1,
                    "outcome": "failed",
                    "code": private_text,
                },
                {
                    "kind": "continuation",
                    "request_started": True,
                    "physical_request_count": 1,
                    "outcome": "succeeded",
                    "trigger": private_text,
                },
            ],
            "success": True,
            "selected_kind": "continuation",
            "fallback_index": 0,
            "continuation_count": 1,
            "same_model_recovery_count": 0,
            "exhausted": False,
            "degraded": False,
        },
    }
    before = deepcopy(trace)

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert trace == before
    assert metrics["task_analyzer_observed"] is True
    assert metrics["task_analyzer_source_family"] == "live_provider"
    assert metrics["task_analyzer_schema_valid"] is True
    assert metrics["task_analyzer_chain_attempt_count"] == 2
    assert metrics["task_analyzer_chain_attempt_scan_count"] == 2
    assert metrics["task_analyzer_chain_attempt_scan_capped"] is False
    assert metrics["task_analyzer_chain_success_count"] == 1
    assert metrics["task_analyzer_chain_failed_count"] == 1
    assert metrics["task_analyzer_chain_physical_request_count"] == 2
    assert metrics["task_analyzer_selected"] is True
    assert metrics["task_analyzer_selected_index"] == 1
    assert metrics["task_analyzer_exhausted"] is False
    assert metrics["task_analyzer_deadline_configured_ms"] == 2_000
    assert metrics["task_analyzer_elapsed_ms"] == 125
    assert metrics["task_analyzer_deadline_remaining_ms"] == 1_875
    assert metrics["task_analyzer_deadline_expired"] is False
    assert metrics["aggregator_recovery_observed"] is True
    assert metrics["aggregator_stage_observed"] is True
    assert metrics["aggregator_recovery_attempt_count"] == 2
    assert metrics["aggregator_recovery_attempt_scan_capped"] is False
    assert metrics["aggregator_primary_attempt_count"] == 1
    assert metrics["aggregator_continuation_attempt_count"] == 1
    assert metrics["aggregator_request_started_count"] == 2
    assert metrics["aggregator_physical_request_count_observed"] is True
    assert metrics["aggregator_physical_request_count"] == 2
    assert metrics["aggregator_succeeded_attempt_count"] == 1
    assert metrics["aggregator_failed_attempt_count"] == 1
    assert metrics["aggregator_abandoned_attempt_count"] == 0
    assert metrics["aggregator_unsuccessful_attempt_count"] == 1
    assert metrics["aggregator_unavailable_attempt_count"] == 0
    assert metrics["aggregator_unknown_outcome_attempt_count"] == 0
    assert metrics["aggregator_selected_kind"] == "continuation"
    assert metrics["aggregator_fallback_index"] == 0
    assert metrics["aggregator_recovery_success"] is True
    assert metrics["aggregator_recovery_exhausted"] is False
    assert metrics["aggregator_recovery_degraded"] is False
    assert metrics["aggregator_continuation_count"] == 1
    assert metrics["aggregator_same_model_recovery_count"] == 0
    assert private_text not in json.dumps(metrics, sort_keys=True)


def test_terminal_trace_projects_role_health_admission_usage_and_failures() -> None:
    private_text = "private provider/model/error/identity text"
    trace: dict[str, Any] = {
        "selection_plan": {
            "runtime_health_filter": {
                "enabled": True,
                "input_candidate_count": 3,
                "fresh_deployment_count": 2,
                "requires_rerank": True,
                "active_unavailable_by_role": {
                    "proposer": 1,
                    "aggregator": 2,
                },
                "filtered_by_role": {"proposer": 1, "aggregator": 1},
                "half_open_by_role": {"proposer": 1, "aggregator": 0},
                "never_strand_minimum_by_role": {
                    "proposer": 2,
                    "aggregator": 1,
                },
                "never_strand_exempt_identities_by_role": {
                    "proposer": [private_text],
                    "aggregator": [private_text, private_text],
                },
                "never_strand": True,
            }
        },
        "candidates": [
            {
                "request_started": True,
                "physical_request_count": 2,
                "usage_missing_count": 1,
                "error_code": "429",
                "execution": {
                    "admission": {"outcome": "admitted", "wait_ms": 5},
                    "runtime_health_admission": {
                        "state": "healthy",
                        "reason": "",
                        "probe": False,
                        "tracked": True,
                    },
                },
                "model_usage_breakdown": [
                    {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "reasoning_tokens": 1,
                        "cached_tokens": 4,
                        "cache_write_tokens": 3,
                        "billed_cost": 0.12,
                        "provider": private_text,
                    },
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "billed_cost": 0.0,
                        "usage_unknown": True,
                        "provider_usage": {"usage_unknown": True},
                        "requested_model": private_text,
                    },
                ],
            },
            {
                "request_started": False,
                "physical_request_count": 0,
                "usage_missing_count": 0,
                "error_code": private_text,
                "execution": {
                    "admission": {"outcome": "timeout", "wait_ms": 7},
                    "runtime_health_admission": {
                        "state": "benched",
                        "reason": "runtime_deployment_benched",
                        "probe": False,
                        "tracked": True,
                    },
                },
                "model_usage_breakdown": [],
            },
            {
                "request_started": True,
                "physical_request_count": 1,
                "usage_missing_count": 0,
                "error_code": "503",
                "execution": {
                    "runtime_health_admission": {
                        "state": "half_open",
                        "reason": "",
                        "probe": True,
                        "tracked": True,
                    }
                },
                "model_usage_breakdown": [
                    {
                        "input_tokens": 20,
                        "output_tokens": 4,
                        "reasoning_tokens": 2,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "billed_cost": 0.34,
                        "model": private_text,
                    }
                ],
            },
        ],
        "aggregator_recovery": {
            "attempts": [
                {
                    "kind": "primary",
                    "request_started": True,
                    "physical_request_count": 1,
                    "outcome": "failed",
                    "code": "503",
                },
                {
                    "kind": "model_fallback",
                    "request_started": False,
                    "physical_request_count": 0,
                    "outcome": "runtime_health_deferred",
                    "code": "runtime_deployment_half_open_busy",
                },
                {
                    "kind": "continuation",
                    "request_started": True,
                    "physical_request_count": 1,
                    "outcome": "succeeded",
                    "code": private_text,
                },
            ],
            "success": True,
            "selected_kind": "continuation",
        },
        "final_request": {
            "role": "aggregator",
            "execution": {
                "admission": {
                    "role": "aggregator_recovery",
                    "outcome": "admitted",
                    "wait_ms": 2,
                }
            },
            "usage": {
                "input_tokens": 30,
                "output_tokens": 6,
                "reasoning_tokens": 3,
                "cached_tokens": 5,
                "cache_write_tokens": 1,
                "billed_cost": 0.56,
                "provider": private_text,
                "model": private_text,
            },
        },
    }
    before = deepcopy(trace)

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert trace == before
    assert metrics["runtime_health_filter_observed"] is True
    assert metrics["runtime_health_fresh_deployment_count"] == 2
    assert metrics["runtime_health_proposer_half_open_count"] == 1
    assert metrics["runtime_health_aggregator_active_unavailable_count"] == 2
    assert metrics["runtime_health_proposer_never_strand_exempt_count"] == 1
    assert metrics["runtime_health_aggregator_never_strand_exempt_count"] == 2
    assert metrics["proposer_runtime_health_observation_count"] == 3
    assert metrics["proposer_runtime_health_tracked_count"] == 3
    assert metrics["proposer_runtime_health_healthy_count"] == 1
    assert metrics["proposer_runtime_health_benched_count"] == 1
    assert metrics["proposer_runtime_health_half_open_count"] == 1
    assert metrics["proposer_runtime_health_probe_count"] == 1
    assert metrics["proposer_runtime_health_benched_deferred_count"] == 1
    assert metrics["proposer_logical_terminal_rate_limited_count"] == 1
    assert metrics["proposer_logical_terminal_upstream_5xx_count"] == 1
    assert metrics["aggregator_runtime_health_deferred_count"] == 1
    assert metrics[
        "aggregator_runtime_health_half_open_busy_deferred_count"
    ] == 1
    assert metrics["aggregator_logical_terminal_upstream_5xx_count"] == 1
    assert metrics["proposer_admission_observation_count"] == 2
    assert metrics["proposer_admission_projection_complete"] is False
    assert metrics["proposer_admission_admitted_count_lower_bound"] == 1
    assert metrics["proposer_admission_timeout_count_lower_bound"] == 1
    assert metrics["proposer_admission_wait_ms_total_lower_bound"] == 12
    assert metrics["aggregator_admission_observation_count"] == 1
    assert metrics["aggregator_admission_projection_complete"] is False
    assert metrics["aggregator_admission_admitted_count_lower_bound"] == 1
    assert metrics["aggregator_admission_wait_ms_total_lower_bound"] == 2
    assert "aggregator_admission_admitted_count" not in metrics
    assert metrics["proposer_physical_request_count"] == 3
    assert metrics["proposer_unknown_usage_count"] == 1
    assert metrics["proposer_usage_projection_complete"] is False
    assert metrics["proposer_usage_row_count_observed"] is False
    assert metrics["proposer_usage_receipt_count"] == 2
    assert metrics["proposer_usage_unknown_row_count"] == 1
    assert "proposer_input_tokens" not in metrics
    assert "proposer_output_tokens" not in metrics
    assert "proposer_reasoning_tokens" not in metrics
    assert "proposer_cache_read_tokens" not in metrics
    assert "proposer_cache_write_tokens" not in metrics
    assert "proposer_cache_hit_request_count" not in metrics
    assert "proposer_billed_cost_usd" not in metrics
    assert metrics["aggregator_physical_request_count"] == 2
    assert metrics["aggregator_physical_request_count_observed"] is True
    assert metrics["aggregator_final_request_usage_projection_complete"] is True
    assert metrics["aggregator_final_request_usage_observed"] is True
    assert metrics["aggregator_final_request_input_tokens"] == 30
    assert metrics["aggregator_final_request_cache_read_tokens"] == 5
    assert metrics["aggregator_final_request_cache_hit"] is True
    assert metrics[
        "aggregator_final_request_billed_cost_usd"
    ] == pytest.approx(0.56)
    assert private_text not in json.dumps(metrics, sort_keys=True)


def test_proposer_admission_exactness_requires_one_to_one_attempt_evidence() -> None:
    exact = build_ensemble_execution_metrics(
        {
            "candidates": [
                {
                    "request_started": True,
                    "physical_request_count": 1,
                    "usage_missing_count": 1,
                    "execution": {
                        "admission": {
                            "outcome": "admitted",
                            "wait_ms": 3,
                        }
                    },
                },
                {
                    "request_started": False,
                    "physical_request_count": 0,
                    "usage_missing_count": 0,
                    "error_code": "ensemble_provider_admission_timeout",
                    "execution": {
                        "admission": {
                            "outcome": "timeout",
                            "wait_ms": 11,
                        }
                    },
                },
            ]
        },
        terminal_outcome="failed",
    )

    assert exact["proposer_admission_projection_complete"] is True
    assert exact["proposer_admission_admitted_count"] == 1
    assert exact["proposer_admission_timeout_count"] == 1
    assert exact["proposer_admission_wait_ms_total"] == 14
    assert "proposer_admission_admitted_count_lower_bound" not in exact

    hidden_error = build_ensemble_execution_metrics(
        {
            "candidates": [
                {
                    "request_started": False,
                    "physical_request_count": 0,
                    "usage_missing_count": 0,
                    "error_code": "ensemble_provider_admission_timeout",
                }
            ]
        },
        terminal_outcome="failed",
    )
    assert hidden_error["proposer_admission_observed"] is True
    assert hidden_error["proposer_admission_projection_complete"] is False
    assert "proposer_admission_timeout_count" not in hidden_error

    multi_request = build_ensemble_execution_metrics(
        {
            "candidates": [
                {
                    "request_started": True,
                    "physical_request_count": 2,
                    "usage_missing_count": 2,
                    "execution": {
                        "admission": {
                            "outcome": "admitted",
                            "wait_ms": 5,
                        }
                    },
                }
            ]
        },
        terminal_outcome="failed",
    )
    assert multi_request["proposer_admission_projection_complete"] is False
    assert multi_request["proposer_admission_admitted_count_lower_bound"] == 1
    assert "proposer_admission_admitted_count" not in multi_request


def test_aggregator_admission_exactness_requires_one_joined_attempt() -> None:
    exact = build_ensemble_execution_metrics(
        {
            "aggregator_recovery": {
                "attempts": [
                    {
                        "kind": "primary",
                        "request_started": True,
                        "physical_request_count": 1,
                        "outcome": "succeeded",
                    }
                ]
            },
            "final_request": {
                "role": "aggregator",
                "execution": {
                    "admission": {
                        "role": "aggregator",
                        "outcome": "admitted",
                        "wait_ms": 4,
                    }
                },
            },
        },
        terminal_outcome="completed",
    )

    assert exact["aggregator_admission_observed"] is True
    assert exact["aggregator_admission_projection_complete"] is True
    assert exact["aggregator_admission_admitted_count"] == 1
    assert exact["aggregator_admission_wait_ms_total"] == 4
    assert "aggregator_admission_admitted_count_lower_bound" not in exact

    missing_attempt_evidence = build_ensemble_execution_metrics(
        {
            "final_request": {
                "role": "aggregator",
                "execution": {
                    "admission": {
                        "role": "aggregator",
                        "outcome": "admitted",
                        "wait_ms": 2,
                    }
                },
            }
        },
        terminal_outcome="completed",
    )
    assert missing_attempt_evidence["aggregator_admission_projection_complete"] is False
    assert missing_attempt_evidence["aggregator_admission_admitted_count_lower_bound"] == 1


@pytest.mark.parametrize("recovery_kind", ["continuation", "model_fallback"])
def test_aggregator_admission_is_lower_bound_after_multiple_attempts(
    recovery_kind: str,
) -> None:
    metrics = build_ensemble_execution_metrics(
        {
            "aggregator_recovery": {
                "attempts": [
                    {
                        "kind": "primary",
                        "request_started": True,
                        "physical_request_count": 1,
                        "outcome": "failed",
                    },
                    {
                        "kind": recovery_kind,
                        "request_started": True,
                        "physical_request_count": 1,
                        "outcome": "succeeded",
                    },
                ]
            },
            "final_request": {
                "role": "aggregator",
                "execution": {
                    "admission": {
                        "role": "aggregator_recovery",
                        "outcome": "admitted",
                        "wait_ms": 3,
                    }
                },
            },
        },
        terminal_outcome="completed",
    )

    assert metrics["aggregator_admission_observed"] is True
    assert metrics["aggregator_admission_projection_complete"] is False
    assert metrics["aggregator_admission_admitted_count_lower_bound"] == 1
    assert metrics["aggregator_admission_wait_ms_total_lower_bound"] == 3
    assert "aggregator_admission_admitted_count" not in metrics


def test_proposer_usage_totals_require_every_physical_usage_receipt() -> None:
    complete = build_ensemble_execution_metrics(
        {
            "candidates": [
                {
                    "request_started": True,
                    "physical_request_count": 1,
                    "usage_missing_count": 0,
                    "model_usage_breakdown": [
                        {
                            "input_tokens": 12,
                            "output_tokens": 3,
                            "reasoning_tokens": 2,
                            "cached_tokens": 4,
                            "cache_write_tokens": 1,
                            "billed_cost": 0.25,
                        }
                    ],
                }
            ]
        },
        terminal_outcome="completed",
    )

    assert complete["proposer_usage_projection_complete"] is True
    assert complete["proposer_input_tokens"] == 12
    assert complete["proposer_cache_hit_request_count"] == 1
    assert complete["proposer_billed_cost_usd"] == pytest.approx(0.25)

    missing = build_ensemble_execution_metrics(
        {
            "candidates": [
                {
                    "request_started": True,
                    "physical_request_count": 1,
                    "usage_missing_count": 1,
                    "model_usage_breakdown": [
                        {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "reasoning_tokens": 0,
                            "cached_tokens": 0,
                            "cache_write_tokens": 0,
                            "billed_cost": 0.0,
                            "usage_unknown": True,
                        }
                    ],
                }
            ]
        },
        terminal_outcome="failed",
    )
    assert missing["proposer_unknown_usage_count"] == 1
    assert missing["proposer_usage_projection_complete"] is False
    assert "proposer_input_tokens" not in missing
    assert "proposer_billed_cost_usd" not in missing


@pytest.mark.parametrize(
    "usage",
    [
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.0,
            "usage_missing_count": 1,
        },
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.0,
        },
        {
            "input_tokens": 10,
            "output_tokens": 2,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "billed_cost": 0.1,
        },
    ],
)
def test_aggregator_final_usage_omits_missing_or_incomplete_evidence(
    usage: dict[str, Any],
) -> None:
    metrics = build_ensemble_execution_metrics(
        {"final_request": {"role": "aggregator", "usage": usage}},
        terminal_outcome="completed",
    )

    assert metrics["aggregator_final_request_usage_container_observed"] is True
    assert metrics["aggregator_final_request_usage_projection_complete"] is False
    assert metrics["aggregator_final_request_usage_observed"] is False
    assert "aggregator_final_request_input_tokens" not in metrics
    assert "aggregator_final_request_billed_cost_usd" not in metrics


def test_contradictory_predispatch_evidence_is_not_health_unavailable() -> None:
    metrics = build_ensemble_execution_metrics(
        {
            "candidates": [
                {
                    "request_started": False,
                    "physical_request_count": 1,
                    "usage_missing_count": 1,
                    "execution": {
                        "runtime_health_admission": {
                            "tracked": True,
                            "state": "benched",
                            "reason": "runtime_deployment_benched",
                            "probe": False,
                        }
                    },
                }
            ],
            "aggregator_recovery": {
                "attempts": [
                    {
                        "kind": "model_fallback",
                        "request_started": False,
                        "physical_request_count": 1,
                        "outcome": "runtime_health_deferred",
                        "code": "runtime_deployment_benched",
                    }
                ]
            },
        },
        terminal_outcome="failed",
    )

    assert metrics["proposer_runtime_health_benched_deferred_count"] == 0
    assert metrics["aggregator_unavailable_attempt_count"] == 0
    assert metrics["aggregator_runtime_health_deferred_count"] == 0
    assert metrics["aggregator_unknown_outcome_attempt_count"] == 1


def test_role_metric_projection_omits_unproven_or_capped_aggregates() -> None:
    private_text = "private model/code/text"
    malformed_trace = {
        "candidates": [
            {
                "request_started": True,
                "physical_request_count": 1,
                "usage_missing_count": 1,
                "error_code": private_text,
                "execution": {
                    "runtime_health_admission": {
                        "state": private_text,
                        "reason": private_text,
                        "probe": 1,
                        "tracked": "yes",
                    }
                },
                "model_usage_breakdown": [
                    {
                        "usage_unknown": True,
                        "provider_usage": {"usage_unknown": True},
                        "provider": private_text,
                    },
                    private_text,
                    {
                        "input_tokens": True,
                        "output_tokens": -1,
                        "reasoning_tokens": float("nan"),
                        "cached_tokens": "3",
                        "cache_write_tokens": None,
                        "billed_cost": 10**1000,
                    },
                ],
            }
        ],
        "final_request": {
            "role": "aggregator",
            "usage": {
                "input_tokens": True,
                "cached_tokens": -1,
                "billed_cost": 10**1000,
                "model": private_text,
            },
        },
    }

    metrics = build_ensemble_execution_metrics(
        malformed_trace,
        terminal_outcome="failed",
    )

    assert metrics["proposer_runtime_health_unknown_state_count"] == 1
    assert metrics["proposer_logical_terminal_http_status_observed"] is False
    assert metrics["proposer_usage_unknown_row_count"] == 1
    assert metrics["proposer_usage_malformed_row_count"] == 1
    assert metrics["proposer_usage_projection_complete"] is False
    assert metrics["proposer_usage_receipt_count"] == 1
    assert metrics["proposer_input_tokens_observation_count"] == 0
    assert metrics["proposer_billed_cost_usd_observation_count"] == 0
    assert "proposer_input_tokens" not in metrics
    assert "proposer_billed_cost_usd" not in metrics
    assert metrics["aggregator_final_request_usage_container_observed"] is True
    assert metrics["aggregator_final_request_usage_projection_complete"] is False
    assert metrics["aggregator_final_request_usage_observed"] is False
    assert "aggregator_final_request_input_tokens_observed" not in metrics
    assert "aggregator_final_request_billed_cost_usd_observed" not in metrics
    assert "aggregator_final_request_billed_cost_usd" not in metrics
    assert private_text not in json.dumps(metrics, sort_keys=True)

    capped_trace = {
        "candidates": [
            {
                "physical_request_count": 1,
                "usage_missing_count": 0,
                "model_usage_breakdown": [
                    {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "billed_cost": 0.01,
                    }
                ],
            }
            for _ in range(65)
        ]
    }
    capped = build_ensemble_execution_metrics(
        capped_trace,
        terminal_outcome="completed",
    )

    assert capped["proposer_candidate_scan_capped"] is True
    assert capped["proposer_physical_request_count_observed"] is False
    assert "proposer_physical_request_count" not in capped
    assert capped["proposer_usage_row_scan_capped"] is True
    assert capped["proposer_usage_projection_complete"] is False
    assert capped["proposer_usage_row_count_observed"] is False
    assert "proposer_usage_row_count" not in capped
    assert "proposer_input_tokens" not in capped


def test_preinitialized_recovery_block_does_not_enter_aggregator_denominator() -> None:
    private_text = "private/provider:model"
    trace = {
        "aggregator_recovery": {
            "schema": "opensquilla.ensemble-aggregator-recovery/v1",
            "mode": "serving",
            "candidate_count": 3,
            "candidate_ids": [private_text],
            "max_tokens_cap": 8_192,
            "visible_answer_reserve_tokens": 1_024,
            "attempts": [],
            "proposer_reused": True,
            "success": False,
        },
        "run_outcome": "proposer_quorum_failed",
    }

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="failed",
    )

    assert metrics["aggregator_recovery_observed"] is True
    assert metrics["aggregator_recovery_attempts_observed"] is True
    assert metrics["aggregator_recovery_attempt_count"] == 0
    assert metrics["aggregator_stage_observed"] is False
    assert metrics["aggregator_physical_request_count_observed"] is False
    assert "aggregator_physical_request_count" not in metrics
    assert metrics["aggregator_recovery_success_observed"] is False
    assert "aggregator_recovery_success" not in metrics
    assert metrics["aggregator_recovery_exhausted_observed"] is False
    assert metrics["aggregator_recovery_degraded_observed"] is False
    assert metrics["aggregator_continuation_count_observed"] is False
    assert metrics["aggregator_same_model_recovery_count_observed"] is False
    assert private_text not in json.dumps(metrics, sort_keys=True)


def test_abandoned_and_unknown_aggregator_outcomes_are_not_hidden() -> None:
    private_text = "private failure code and model"
    metrics = build_ensemble_execution_metrics(
        {
            "aggregator_recovery": {
                "attempts": [
                    {
                        "kind": "primary",
                        "request_started": True,
                        "physical_request_count": 1,
                        "outcome": "abandoned",
                        "code": private_text,
                    },
                    {
                        "kind": "future_recovery_kind",
                        "request_started": True,
                        "physical_request_count": 1,
                        "outcome": "future_terminal_outcome",
                        "requested_model": private_text,
                    },
                    {
                        "kind": "model_fallback",
                        "request_started": True,
                        "physical_request_count": 1,
                        "outcome": "member_unavailable",
                    },
                    {
                        "kind": "model_fallback",
                        "physical_request_count": 0,
                        "outcome": "provider_build_failed",
                    },
                    {
                        "kind": "model_fallback",
                        "request_started": False,
                        "physical_request_count": 0,
                        "outcome": "runtime_health_deferred",
                    },
                ],
                "success": False,
                "exhausted": True,
            }
        },
        terminal_outcome="failed",
    )

    assert metrics["aggregator_stage_observed"] is True
    assert metrics["aggregator_failed_attempt_count"] == 0
    assert metrics["aggregator_abandoned_attempt_count"] == 1
    assert metrics["aggregator_unsuccessful_attempt_count"] == 1
    assert metrics["aggregator_unavailable_attempt_count"] == 1
    assert metrics["aggregator_unknown_outcome_attempt_count"] == 3
    assert metrics["aggregator_unknown_kind_attempt_count"] == 1
    assert (
        metrics["aggregator_succeeded_attempt_count"]
        + metrics["aggregator_failed_attempt_count"]
        + metrics["aggregator_abandoned_attempt_count"]
        + metrics["aggregator_unavailable_attempt_count"]
        + metrics["aggregator_unknown_outcome_attempt_count"]
        == metrics["aggregator_recovery_attempt_scan_count"]
    )
    assert metrics["aggregator_recovery_success"] is False
    assert metrics["aggregator_recovery_exhausted"] is True
    assert private_text not in json.dumps(metrics, sort_keys=True)


def test_stage_projection_is_bounded_and_malformed_values_are_omitted() -> None:
    private_text = "private model, provider, code, prompt, and reasoning"
    analyzer_attempts = [
        {
            "outcome": "failed",
            "physical_request_count": 1,
            "reason": private_text,
        }
        for _ in range(10)
    ]
    aggregator_attempts = [
        {
            "kind": "model_fallback",
            "request_started": False,
            "physical_request_count": 0,
            "outcome": "member_unavailable",
            "requested_model": private_text,
        }
        for _ in range(18)
    ]
    trace: dict[str, Any] = {
        "selection_plan": {
            "task_analyzer": {
                "source": private_text,
                "schema_valid": 1,
                "chain": {
                    "attempt_outcomes": analyzer_attempts,
                    "selected_index": None,
                    "exhausted": True,
                    "deadline": {
                        "configured_seconds": 10**1000,
                        "elapsed_seconds": -1.0,
                        "remaining_seconds": True,
                        "expired": True,
                    },
                },
            }
        },
        "aggregator_recovery": {
            "attempts": aggregator_attempts,
            "success": False,
            "selected_kind": private_text,
            "fallback_index": True,
            "continuation_count": -1,
            "same_model_recovery_count": float("nan"),
            "exhausted": True,
            "degraded": True,
        },
    }
    before = deepcopy(trace)

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert trace == before
    assert metrics["execution_status"] == "degraded"
    assert metrics["task_analyzer_source_family"] == "unknown"
    assert metrics["task_analyzer_schema_valid_observed"] is False
    assert metrics["task_analyzer_chain_attempt_count"] == 10
    assert metrics["task_analyzer_chain_attempt_scan_count"] == 8
    assert metrics["task_analyzer_chain_attempt_scan_capped"] is True
    assert metrics["task_analyzer_chain_failed_count"] == 8
    assert metrics["task_analyzer_chain_physical_request_count"] == 8
    assert metrics["task_analyzer_selected"] is False
    assert "task_analyzer_selected_index" not in metrics
    assert metrics["task_analyzer_exhausted"] is True
    assert "task_analyzer_deadline_configured_ms" not in metrics
    assert "task_analyzer_elapsed_ms" not in metrics
    assert "task_analyzer_deadline_remaining_ms" not in metrics
    assert metrics["task_analyzer_deadline_expired"] is True
    assert metrics["aggregator_recovery_attempt_count"] == 18
    assert metrics["aggregator_recovery_attempt_scan_count"] == 16
    assert metrics["aggregator_recovery_attempt_scan_capped"] is True
    assert metrics["aggregator_model_fallback_attempt_count"] == 16
    assert metrics["aggregator_stage_observed"] is True
    assert metrics["aggregator_abandoned_attempt_count"] == 0
    assert metrics["aggregator_unsuccessful_attempt_count"] == 0
    assert metrics["aggregator_unavailable_attempt_count"] == 16
    assert metrics["aggregator_unknown_outcome_attempt_count"] == 0
    assert metrics["aggregator_request_started_count"] == 0
    assert metrics["aggregator_physical_request_count_observed"] is False
    assert "aggregator_physical_request_count" not in metrics
    assert metrics["aggregator_selected_kind"] == "unknown"
    assert metrics["aggregator_fallback_index_observed"] is False
    assert "aggregator_fallback_index" not in metrics
    assert metrics["aggregator_continuation_count_observed"] is False
    assert "aggregator_continuation_count" not in metrics
    assert metrics["aggregator_same_model_recovery_count_observed"] is False
    assert "aggregator_same_model_recovery_count" not in metrics
    assert private_text not in json.dumps(metrics, sort_keys=True)


def test_trace_size_matches_compact_json_for_escapes_and_unicode() -> None:
    trace = {
        "controls": "quote=\" slash=\\ newline=\n tab=\t nul=\x00",
        "unicode": "界🙂\u2028",
        "values": [None, True, False, -7, 1.25],
    }

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    expected = len(
        json.dumps(
            trace,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert metrics["trace_compact_json_bytes"] == expected
    assert metrics["trace_compact_json_bytes_capped"] is False
    assert metrics["trace_compact_json_visit_cap"] == TRACE_SIZE_VISIT_CAP


def test_trace_size_measurement_is_clamped_before_full_string_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_dumps = metrics_module.json.dumps
    measured_string_chunks: list[int] = []

    def counting_dumps(value: Any, *args: Any, **kwargs: Any) -> str:
        if isinstance(value, str):
            measured_string_chunks.append(len(value))
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(metrics_module.json, "dumps", counting_dumps)
    trace = {
        "selection_plan": {
            "request_context": "界" * (TRACE_SIZE_CAP_BYTES * 2),
        }
    }

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert "trace_compact_json_bytes" not in metrics
    assert (
        metrics["trace_compact_json_bytes_lower_bound"]
        == TRACE_SIZE_CAP_BYTES
    )
    assert metrics["trace_compact_json_bytes_capped"] is True
    assert metrics["trace_compact_json_bytes_cap"] == TRACE_SIZE_CAP_BYTES
    assert metrics["trace_compact_json_bytes_cap_reason"] == "byte_limit"
    assert measured_string_chunks
    assert max(measured_string_chunks) <= 4_096
    assert sum(measured_string_chunks) < TRACE_SIZE_CAP_BYTES * 2


def test_malformed_fields_are_omitted_without_inventing_observations() -> None:
    metrics = build_ensemble_execution_metrics(
        {
            "selection_strategy": "arbitrary-custom-mode",
            "candidates": [
                {
                    "request_started": True,
                    "elapsed_ms": True,
                    "execution": {
                        "admission": {
                            "outcome": "timeout",
                            "wait_ms": -1,
                        }
                    },
                },
                {
                    "request_started": True,
                    "elapsed_ms": (1 << 63) - 1,
                    "execution": {
                        "admission": {
                            "outcome": "admitted",
                            "wait_ms": (1 << 63) - 1,
                        }
                    },
                },
                {
                    "request_started": True,
                    "elapsed_ms": (1 << 63) - 1,
                    "execution": {
                        "admission": {
                            "outcome": "admitted",
                            "wait_ms": (1 << 63) - 1,
                        }
                    },
                },
            ],
            "proposer_quorum": {"time_to_quorum_ms": float("nan")},
            "proposer_recovery": {
                "additional_physical_requests_started": True,
            },
            "usage_missing_count": "7",
            "physical_request_count": 10**1000,
            "not_json": float("nan"),
        },
        terminal_outcome="failed",
    )

    assert metrics["selection_family"] == "fixed"
    assert metrics["execution_status"] == "failed"
    assert metrics["fallback_used_observed"] is False
    assert "fallback_used" not in metrics
    assert metrics["trace_size_observed"] is False
    assert metrics["proposer_candidates_observed"] is True
    assert metrics["proposer_elapsed_observation_count"] == 2
    assert metrics["proposer_candidate_elapsed_ms_max"] == (1 << 63) - 1
    assert "proposer_candidate_elapsed_ms_total" not in metrics
    assert metrics["admission_observation_count"] == 3
    assert metrics["admission_wait_observation_count"] == 2
    assert metrics["admission_timeout_count"] == 1
    assert metrics["admission_wait_ms_max"] == (1 << 63) - 1
    assert "admission_wait_ms_total" not in metrics
    assert metrics["quorum_observed"] is True
    assert metrics["quorum_reached_observed"] is False
    assert "quorum_reached" not in metrics
    assert "time_to_quorum_ms" not in metrics
    assert metrics["proposer_recovery_observed"] is False
    assert metrics["physical_request_count_observed"] is False
    assert "physical_request_count" not in metrics
    assert metrics["unknown_usage_count_observed"] is False
    assert "unknown_usage_count" not in metrics


@pytest.mark.parametrize("float_value", [0.0, 1.0, 1.9])
def test_integer_trace_metrics_reject_float_values(float_value: float) -> None:
    metrics = build_ensemble_execution_metrics(
        {
            "candidates": [
                {
                    "request_started": True,
                    "elapsed_ms": float_value,
                    "execution": {
                        "admission": {
                            "outcome": "admitted",
                            "wait_ms": float_value,
                        }
                    },
                }
            ],
            "proposer_quorum": {
                "time_to_quorum_ms": float_value,
                "grace_elapsed_ms": float_value,
                "pending_at_quorum": float_value,
                "cancellation": {
                    "requested_task_count": float_value,
                },
                "cleanup": {
                    "awaited_task_count": float_value,
                    "completed_task_count": float_value,
                    "lingering_task_count": float_value,
                    "stream_close_proven_count": float_value,
                    "stream_close_unproven_count": float_value,
                },
            },
            "proposer_recovery": {
                "additional_physical_requests_started": float_value,
            },
            "physical_request_count": float_value,
            "usage_missing_count": float_value,
        },
        terminal_outcome="completed",
    )

    assert metrics["proposer_elapsed_observation_count"] == 0
    assert "proposer_candidate_elapsed_ms_max" not in metrics
    assert "proposer_candidate_elapsed_ms_total" not in metrics
    assert metrics["admission_wait_observation_count"] == 0
    assert "admission_wait_ms_max" not in metrics
    assert "admission_wait_ms_total" not in metrics
    for key in (
        "time_to_quorum_ms",
        "quorum_grace_elapsed_ms",
        "pending_at_quorum",
        "quorum_cancel_requested_task_count",
        "cleanup_awaited_task_count",
        "cleanup_completed_task_count",
        "cleanup_lingering_task_count",
        "cleanup_stream_close_proven_count",
        "cleanup_stream_close_unproven_count",
    ):
        assert key not in metrics
    assert metrics["proposer_recovery_observed"] is False
    assert "proposer_recovery_calls" not in metrics
    assert metrics["physical_request_count_observed"] is False
    assert "physical_request_count" not in metrics
    assert metrics["unknown_usage_count_observed"] is False
    assert "unknown_usage_count" not in metrics


def test_trace_size_wide_structure_stops_at_visit_cap() -> None:
    trace = {"wide": [None] * (TRACE_SIZE_VISIT_CAP + 1)}

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert metrics["trace_compact_json_bytes_capped"] is True
    assert metrics["trace_compact_json_bytes_cap_reason"] == "visit_limit"
    assert "trace_compact_json_bytes" not in metrics
    assert metrics["trace_compact_json_bytes_lower_bound"] > 0


@pytest.mark.parametrize(
    ("terminal_outcomes", "expected_outcome"),
    [
        (["failed"], "failed"),
        (["failed", "failed"], "failed"),
        (["completed", "failed"], "completed"),
    ],
)
def test_per_call_metrics_gate_emits_only_the_first_terminal_event(
    terminal_outcomes: list[str],
    expected_outcome: str,
) -> None:
    emitted = False
    with structlog.testing.capture_logs() as captured:
        for terminal_outcome in terminal_outcomes:
            emitted = log_ensemble_execution_metrics_once(
                {"fallback_used": False},
                terminal_outcome=terminal_outcome,
                already_emitted=emitted,
            )

    rows = [
        row
        for row in captured
        if row.get("event") == "llm_ensemble.execution.metrics"
    ]
    assert emitted is True
    assert len(rows) == 1
    assert rows[0]["terminal_outcome"] == expected_outcome


def test_invalid_outcome_is_rejected_by_builder_but_logging_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="terminal_outcome"):
        build_ensemble_execution_metrics({}, terminal_outcome="cancelled")

    class BrokenLogger:
        def info(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("broken info processor")

        def warning(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("broken warning processor")

    monkeypatch.setattr(metrics_module, "log", BrokenLogger())
    log_ensemble_execution_metrics({}, terminal_outcome="cancelled")
