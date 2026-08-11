"""Strict transport contract for content-free ensemble execution metrics.

The projector deliberately emits only a closed set of scalar fields.  The
JSONL transport validates that boundary again before a row reaches disk so a
future projector change cannot silently turn the metrics file into a raw trace
or identity log.  This module is intentionally dependency-free: it is used on
the terminal provider path and must not import a dashboard or telemetry SDK.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

ENSEMBLE_EXECUTION_METRICS_SCHEMA = "opensquilla.ensemble-execution-metrics/v1"
ENSEMBLE_EXECUTION_METRICS_EVENT = "llm_ensemble.execution.metrics"
ENSEMBLE_EXECUTION_METRICS_JSONL_SCHEMA = "opensquilla.ensemble-execution-metrics-jsonl/v1"

_MAX_METRIC_INT = (1 << 63) - 1

# These are the literal output fields in ensemble_execution_metrics.py.  Keep
# this list explicit: an unknown projector field must make the optional sink
# fail closed until its privacy, cardinality, type, and evidence semantics are
# reviewed here.
_FIXED_FIELDS = frozenset(
    """
    admission_observation_count
    admission_rejected_count
    admission_timeout_count
    admission_wait_ms_max
    admission_wait_ms_total
    admission_wait_observation_count
    aggregator_abandoned_attempt_count
    aggregator_admission_projection_complete
    aggregator_continuation_attempt_count
    aggregator_continuation_fallback_attempt_count
    aggregator_failed_attempt_count
    aggregator_fallback_index
    aggregator_fallback_index_observed
    aggregator_final_request_billed_cost_usd
    aggregator_final_request_billed_cost_usd_observed
    aggregator_final_request_cache_hit
    aggregator_final_request_cache_hit_observed
    aggregator_final_request_usage_container_observed
    aggregator_final_request_usage_observed
    aggregator_final_request_usage_projection_complete
    aggregator_logical_terminal_http_status_observation_count
    aggregator_logical_terminal_http_status_observed
    aggregator_logical_terminal_rate_limited_count
    aggregator_logical_terminal_upstream_5xx_count
    aggregator_model_fallback_attempt_count
    aggregator_physical_request_count
    aggregator_physical_request_count_observed
    aggregator_physical_request_observation_count
    aggregator_primary_attempt_count
    aggregator_recovery_attempt_count
    aggregator_recovery_attempt_scan_capped
    aggregator_recovery_attempt_scan_count
    aggregator_recovery_attempts_observed
    aggregator_recovery_observed
    aggregator_request_started_count
    aggregator_request_started_observation_count
    aggregator_runtime_health_benched_deferred_count
    aggregator_runtime_health_deferred_count
    aggregator_runtime_health_half_open_busy_deferred_count
    aggregator_runtime_health_observed
    aggregator_runtime_health_unknown_deferred_count
    aggregator_same_model_recovery_attempt_count
    aggregator_selected_kind
    aggregator_selected_kind_observed
    aggregator_stage_observed
    aggregator_succeeded_attempt_count
    aggregator_unavailable_attempt_count
    aggregator_unknown_kind_attempt_count
    aggregator_unknown_outcome_attempt_count
    aggregator_unsuccessful_attempt_count
    canary_physical_budget_accounting_observed
    canary_physical_budget_committed
    canary_physical_budget_conservation_observed
    canary_physical_budget_conservation_valid
    canary_physical_budget_exhausted
    canary_physical_budget_exhausted_observed
    canary_physical_budget_limit
    canary_physical_budget_observed
    canary_physical_budget_projection_complete
    canary_physical_budget_refunded
    canary_physical_budget_rejected
    canary_physical_budget_reserved
    canary_persistent_rollout_admission_allowed_count
    canary_persistent_rollout_admission_denied_count
    canary_persistent_rollout_admission_unavailable_count
    canary_persistent_rollout_cancelled_before_request_count
    canary_persistent_rollout_enabled
    canary_persistent_rollout_enabled_observed
    canary_persistent_rollout_mutation_unavailable_count
    canary_persistent_rollout_observed
    canary_persistent_rollout_probe_count
    canary_persistent_rollout_projection_complete
    canary_persistent_rollout_provider_configuration_failure_count
    canary_persistent_rollout_provider_invalid_response_count
    canary_persistent_rollout_provider_rate_limited_count
    canary_persistent_rollout_provider_success_count
    canary_persistent_rollout_provider_transport_failure_count
    canary_persistent_rollout_provider_unknown_failure_count
    canary_persistent_rollout_provider_upstream_5xx_count
    canary_persistent_rollout_receipt_count
    canary_persistent_rollout_receipt_count_observed
    canary_persistent_rollout_recovery_transition_count
    canary_persistent_rollout_rollback_transition_count
    canary_persistent_rollout_settled_count
    canary_persistent_rollout_usage_missing_count
    canary_persistent_rollout_usage_observed_count
    canary_rollout_admitted_counts_observed
    canary_rollout_aggregator_admitted_count
    canary_rollout_conservation_observed
    canary_rollout_conservation_valid
    canary_rollout_input_canary_count
    canary_rollout_input_canary_count_observed
    canary_rollout_observed
    canary_rollout_projection_complete
    canary_rollout_proposer_admitted_count
    canary_rollout_reason_counts_observed
    canary_task_gate_observed
    canary_task_risk
    canary_task_risk_observed
    cleanup_observed
    execution_status
    fallback_used
    fallback_used_observed
    physical_request_count
    physical_request_count_observed
    proposer_admission_projection_complete
    proposer_billed_cost_usd
    proposer_billed_cost_usd_observation_count
    proposer_cache_hit_request_count
    proposer_candidate_count
    proposer_candidate_elapsed_ms_max
    proposer_candidate_elapsed_ms_total
    proposer_candidate_scan_capped
    proposer_candidate_scan_count
    proposer_candidates_observed
    proposer_elapsed_observation_count
    proposer_logical_terminal_http_status_observation_count
    proposer_logical_terminal_http_status_observed
    proposer_logical_terminal_rate_limited_count
    proposer_logical_terminal_upstream_5xx_count
    proposer_physical_request_count
    proposer_physical_request_count_observation_count
    proposer_physical_request_count_observed
    proposer_recovery_calls
    proposer_recovery_observed
    proposer_runtime_health_benched_count
    proposer_runtime_health_benched_deferred_count
    proposer_runtime_health_half_open_busy_deferred_count
    proposer_runtime_health_half_open_count
    proposer_runtime_health_healthy_count
    proposer_runtime_health_observation_count
    proposer_runtime_health_observed
    proposer_runtime_health_probe_count
    proposer_runtime_health_probe_observation_count
    proposer_runtime_health_state_observation_count
    proposer_runtime_health_tracked_count
    proposer_runtime_health_tracked_observation_count
    proposer_runtime_health_unknown_deferred_count
    proposer_runtime_health_unknown_state_count
    proposer_unknown_usage_count
    proposer_unknown_usage_count_observation_count
    proposer_unknown_usage_count_observed
    proposer_usage_container_observation_count
    proposer_usage_malformed_row_count
    proposer_usage_observed
    proposer_usage_projection_complete
    proposer_usage_receipt_count
    proposer_usage_row_count
    proposer_usage_row_count_observed
    proposer_usage_row_scan_capped
    proposer_usage_row_scan_count
    proposer_usage_unknown_row_count
    quorum_observed
    quorum_reached
    quorum_reached_observed
    runtime_health_filter_enabled
    runtime_health_filter_enabled_observed
    runtime_health_filter_observed
    runtime_health_never_strand
    runtime_health_never_strand_observed
    runtime_health_requires_rerank
    runtime_health_requires_rerank_observed
    schema
    selection_family
    task_analyzer_chain_attempt_count
    task_analyzer_chain_attempt_scan_capped
    task_analyzer_chain_attempt_scan_count
    task_analyzer_chain_attempts_observed
    task_analyzer_chain_failed_count
    task_analyzer_chain_observed
    task_analyzer_chain_physical_request_count
    task_analyzer_chain_physical_request_observation_count
    task_analyzer_chain_success_count
    task_analyzer_deadline_expired
    task_analyzer_deadline_expired_observed
    task_analyzer_deadline_observed
    task_analyzer_exhausted
    task_analyzer_exhausted_observed
    task_analyzer_observed
    task_analyzer_schema_valid
    task_analyzer_schema_valid_observed
    task_analyzer_selected
    task_analyzer_selected_field_observed
    task_analyzer_selected_index
    task_analyzer_source_family
    task_analyzer_source_observed
    terminal_outcome
    trace_compact_json_bytes
    trace_compact_json_bytes_cap
    trace_compact_json_bytes_cap_reason
    trace_compact_json_bytes_capped
    trace_compact_json_bytes_lower_bound
    trace_compact_json_visit_cap
    trace_size_observed
    unknown_usage_count
    unknown_usage_count_observed
    """.split()
)

_DYNAMIC_FIELDS: set[str] = {
    "task_analyzer_deadline_configured_ms",
    "task_analyzer_elapsed_ms",
    "task_analyzer_deadline_remaining_ms",
    "runtime_health_input_candidate_count",
    "runtime_health_fresh_deployment_count",
    "time_to_quorum_ms",
    "quorum_grace_elapsed_ms",
    "pending_at_quorum",
    "quorum_cancel_requested_task_count",
    "cleanup_awaited_task_count",
    "cleanup_completed_task_count",
    "cleanup_lingering_task_count",
    "cleanup_stream_close_proven_count",
    "cleanup_stream_close_unproven_count",
}

for _name in ("success", "exhausted", "degraded"):
    _DYNAMIC_FIELDS.update(
        {
            f"aggregator_recovery_{_name}_observed",
            f"aggregator_recovery_{_name}",
        }
    )
for _name in ("continuation", "same_model_recovery"):
    _DYNAMIC_FIELDS.update(
        {
            f"aggregator_{_name}_count_observed",
            f"aggregator_{_name}_count",
        }
    )

for _role in ("proposer", "aggregator"):
    for _suffix in (
        "active_unavailable_count",
        "filtered_count",
        "half_open_count",
        "never_strand_minimum",
        "never_strand_exempt_count",
    ):
        _DYNAMIC_FIELDS.add(f"runtime_health_{_role}_{_suffix}")
    _DYNAMIC_FIELDS.update(
        {
            f"{_role}_admission_observed",
            f"{_role}_admission_observation_count",
            f"{_role}_admission_wait_observation_count",
        }
    )
    for _outcome in ("admitted", "timeout", "rejected"):
        _DYNAMIC_FIELDS.update(
            {
                f"{_role}_admission_{_outcome}_count",
                f"{_role}_admission_{_outcome}_count_lower_bound",
            }
        )
    for _aggregate in ("max", "total"):
        _DYNAMIC_FIELDS.update(
            {
                f"{_role}_admission_wait_ms_{_aggregate}",
                f"{_role}_admission_wait_ms_{_aggregate}_lower_bound",
            }
        )

for _name in (
    "input",
    "output",
    "reasoning",
    "cache_read",
    "cache_write",
):
    _DYNAMIC_FIELDS.update(
        {
            f"proposer_{_name}_tokens_observation_count",
            f"proposer_{_name}_tokens",
            f"aggregator_final_request_{_name}_tokens_observed",
            f"aggregator_final_request_{_name}_tokens",
        }
    )

for _name in ("enabled", "config_valid"):
    _DYNAMIC_FIELDS.update(
        {
            f"canary_rollout_{_name}_observed",
            f"canary_rollout_{_name}",
        }
    )
for _name in (
    "analyzer_source_eligible",
    "schema_valid",
    "confidence_eligible",
    "eligible",
):
    _DYNAMIC_FIELDS.update(
        {
            f"canary_task_{_name}_observed",
            f"canary_task_{_name}",
        }
    )
for _suffix in (
    "policy_invalid",
    "rollout_disabled",
    "decision_id_missing",
    "task_ineligible",
    "global_cohort_excluded",
    "role_disabled",
    "role_cohort_excluded",
    "health_unhealthy",
    "role_unsupported",
    "reliability_coverage_insufficient",
    "reliability_threshold_exceeded",
    "candidate_cap",
):
    _DYNAMIC_FIELDS.add(f"canary_rollout_reason_{_suffix}_count")

ALLOWED_ENSEMBLE_EXECUTION_METRIC_FIELDS = frozenset(_FIXED_FIELDS | _DYNAMIC_FIELDS)

_STRING_DOMAINS: dict[str, frozenset[str]] = {
    "schema": frozenset({ENSEMBLE_EXECUTION_METRICS_SCHEMA}),
    "terminal_outcome": frozenset({"completed", "failed"}),
    "execution_status": frozenset({"success", "degraded", "failed"}),
    "selection_family": frozenset({"router_dynamic", "router_tree_baseline", "fixed", "unknown"}),
    "task_analyzer_source_family": frozenset(
        {"live_provider", "frozen_replay", "fallback", "local", "unknown"}
    ),
    "aggregator_selected_kind": frozenset(
        {
            "primary",
            "continuation",
            "same_model_recovery",
            "model_fallback",
            "continuation_fallback",
            "partial_salvage",
            "degraded_delivery",
            "unknown",
        }
    ),
    "trace_compact_json_bytes_cap_reason": frozenset({"byte_limit", "visit_limit", "depth_limit"}),
    "canary_task_risk": frozenset({"low", "medium", "high", "unknown"}),
}

_BOOLEAN_FIELDS = frozenset(
    field
    for field in ALLOWED_ENSEMBLE_EXECUTION_METRIC_FIELDS
    if field.endswith(("_observed", "_projection_complete", "_scan_capped"))
) | frozenset(
    {
        "fallback_used",
        "trace_compact_json_bytes_capped",
        "task_analyzer_schema_valid",
        "task_analyzer_selected",
        "task_analyzer_exhausted",
        "task_analyzer_deadline_expired",
        "aggregator_recovery_success",
        "aggregator_recovery_exhausted",
        "aggregator_recovery_degraded",
        "runtime_health_filter_enabled",
        "runtime_health_requires_rerank",
        "runtime_health_never_strand",
        "canary_rollout_enabled",
        "canary_rollout_config_valid",
        "canary_task_analyzer_source_eligible",
        "canary_task_schema_valid",
        "canary_task_confidence_eligible",
        "canary_task_eligible",
        "canary_rollout_conservation_valid",
        "canary_physical_budget_conservation_valid",
        "canary_physical_budget_exhausted",
        "canary_persistent_rollout_enabled",
        "aggregator_final_request_cache_hit",
        "quorum_reached",
    }
)

_FLOAT_FIELDS = frozenset(
    {
        "proposer_billed_cost_usd",
        "aggregator_final_request_billed_cost_usd",
    }
)

_REQUIRED_FIELDS = frozenset(
    {
        "schema",
        "terminal_outcome",
        "execution_status",
        "selection_family",
        "fallback_used_observed",
    }
)


class EnsembleExecutionMetricsContractError(ValueError):
    """The optional transport row violated its reviewed scalar contract."""


def validate_ensemble_execution_metrics(metrics: Mapping[str, Any]) -> None:
    """Validate one projector result against the fixed transport allowlist."""

    if type(metrics) is not dict:
        raise EnsembleExecutionMetricsContractError("metrics must be a built-in dict")
    raw_keys = set(metrics)
    if any(type(key) is not str for key in raw_keys):
        raise EnsembleExecutionMetricsContractError("metrics keys must be built-in strings")
    unknown = raw_keys - ALLOWED_ENSEMBLE_EXECUTION_METRIC_FIELDS
    if unknown:
        raise EnsembleExecutionMetricsContractError(f"unknown metrics fields: {sorted(unknown)!r}")
    missing = _REQUIRED_FIELDS - raw_keys
    if missing:
        raise EnsembleExecutionMetricsContractError(
            f"missing required metrics fields: {sorted(missing)!r}"
        )

    for field, value in metrics.items():
        domain = _STRING_DOMAINS.get(field)
        if domain is not None:
            if type(value) is not str or value not in domain:
                raise EnsembleExecutionMetricsContractError(f"invalid enum value for {field}")
            continue
        if field in _BOOLEAN_FIELDS:
            if type(value) is not bool:
                raise EnsembleExecutionMetricsContractError(f"{field} must be a built-in bool")
            continue
        if field in _FLOAT_FIELDS:
            if (
                type(value) is not float
                or not math.isfinite(value)
                or value < 0
                or value > _MAX_METRIC_INT
            ):
                raise EnsembleExecutionMetricsContractError(
                    f"{field} must be a finite non-negative bounded float"
                )
            continue
        if type(value) is not int or not 0 <= value <= _MAX_METRIC_INT:
            raise EnsembleExecutionMetricsContractError(
                f"{field} must be a non-negative bounded integer"
            )

    terminal_outcome = metrics["terminal_outcome"]
    execution_status = metrics["execution_status"]
    if (terminal_outcome == "failed") != (execution_status == "failed"):
        raise EnsembleExecutionMetricsContractError(
            "terminal_outcome and execution_status disagree"
        )
    if "fallback_used" in metrics and metrics["fallback_used_observed"] is not True:
        raise EnsembleExecutionMetricsContractError(
            "fallback_used requires fallback_used_observed=true"
        )
