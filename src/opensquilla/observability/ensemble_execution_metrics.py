"""Low-cardinality metrics projected from terminal ensemble traces.

The ensemble trace is already the authoritative execution receipt carried by a
terminal provider event.  This module reads that receipt without mutating it and
emits only fixed enums, booleans, and numeric aggregates.  It deliberately does
not include model identities, prompts, outputs, reasoning, or error text.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import structlog

from opensquilla.observability.ensemble_execution_metrics_contract import (
    ENSEMBLE_EXECUTION_METRICS_SCHEMA,
)
from opensquilla.observability.ensemble_execution_metrics_jsonl import (
    write_ensemble_execution_metrics_jsonl,
)

log = structlog.get_logger(__name__)

TRACE_SIZE_CAP_BYTES = 262_144
TRACE_SIZE_VISIT_CAP = 16_384
_MAX_CANDIDATE_ROWS = 64
_MAX_JSON_DEPTH = 64
_JSON_STRING_CHUNK_CHARS = 4_096
_MAX_METRIC_INT = (1 << 63) - 1
_TERMINAL_OUTCOMES = frozenset({"completed", "failed"})
_MAX_ANALYZER_ATTEMPT_ROWS = 8
_MAX_AGGREGATOR_RECOVERY_ATTEMPT_ROWS = 16
_MAX_USAGE_ROWS_PER_CANDIDATE = 8
_RUNTIME_HEALTH_STATES = frozenset({"healthy", "benched", "half_open"})
_RUNTIME_HEALTH_BENCHED_REASON = "runtime_deployment_benched"
_RUNTIME_HEALTH_HALF_OPEN_BUSY_REASON = (
    "runtime_deployment_half_open_busy"
)
_CANARY_ROLLOUT_SCHEMA = "opensquilla.ensemble-canary-rollout/v1"
_CANARY_PHYSICAL_BUDGET_SCHEMA = (
    "opensquilla.ensemble-canary-physical-budget/v1"
)
_CANARY_PERSISTENT_ROLLOUT_SCHEMA = (
    "opensquilla.ensemble-canary-persistent-rollout/v1"
)
_RANKING_STAGE_OBSERVABILITY_SCHEMA = (
    "opensquilla.router-dynamic-ranking-stage-observability/v1"
)
_RANKING_STAGE_OBSERVABILITY_FIELDS = frozenset(
    {
        "schema",
        "snapshot_build_ms",
        "hard_filter_ms",
        "score_ms",
        "packaged_template_cache_hit",
    }
)
_MAX_CANARY_PERSISTENT_RECEIPTS = 8
_CANARY_PERSISTENT_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "role",
        "available",
        "allowed",
        "probe",
        "admission_reason",
        "state_before",
        "mutation_available",
        "mutation_applied",
        "mutation_reason",
        "state_after",
        "latch_reason",
        "provider_outcome",
        "usage_outcome",
        "cancelled_before_request",
        "rollback_transition",
        "recovery_transition",
        "recovery_successes",
    }
)
_CANARY_PERSISTENT_STATES = frozenset({"active", "rolled_back", "half_open"})
_CANARY_PERSISTENT_ADMISSION_REASONS = frozenset(
    {
        "active",
        "half_open_probe",
        "latched",
        "half_open_busy",
        "recovery_wait",
        "pending_capacity",
        "scope_capacity",
        "ledger_unavailable",
    }
)
_CANARY_PERSISTENT_MUTATION_REASONS = frozenset(
    {
        "applied",
        "duplicate",
        "cancelled",
        "unknown_token",
        "token_conflict",
        "unknown_scope",
        "ledger_unavailable",
        "not_attempted",
    }
)
_CANARY_PERSISTENT_LATCH_REASONS = frozenset(
    {
        "none",
        "configuration_failure",
        "rate_limited",
        "usage_missing",
        "consecutive_provider_failures",
        "provider_failure_rate",
        "quality_coverage_insufficient",
        "quality_failure_rate",
        "attempt_abandoned",
        "probe_failed",
        "probe_abandoned",
        "policy_contract_mismatch",
    }
)
_CANARY_PERSISTENT_PROVIDER_OUTCOME_SUFFIXES = (
    ("success", "success"),
    ("rate_limited", "rate_limited"),
    ("upstream_5xx", "upstream_5xx"),
    ("transport_failure", "transport_failure"),
    ("invalid_response", "invalid_response"),
    ("configuration_failure", "configuration_failure"),
    ("unknown_failure", "unknown_failure"),
)
_CANARY_PERSISTENT_PROVIDER_OUTCOMES = frozenset(
    {source for source, _ in _CANARY_PERSISTENT_PROVIDER_OUTCOME_SUFFIXES}
)
_CANARY_PERSISTENT_USAGE_OUTCOMES = frozenset({"observed", "missing"})
_CANARY_TASK_RISKS = frozenset({"low", "medium", "high", "unknown"})
_CANARY_REASON_METRIC_SUFFIXES = (
    ("canary_policy_invalid", "policy_invalid"),
    ("canary_rollout_disabled", "rollout_disabled"),
    ("canary_decision_id_missing", "decision_id_missing"),
    ("canary_task_ineligible", "task_ineligible"),
    ("canary_global_cohort_excluded", "global_cohort_excluded"),
    ("canary_role_disabled", "role_disabled"),
    ("canary_role_cohort_excluded", "role_cohort_excluded"),
    ("canary_health_unhealthy", "health_unhealthy"),
    ("canary_role_unsupported", "role_unsupported"),
    (
        "canary_reliability_coverage_insufficient",
        "reliability_coverage_insufficient",
    ),
    (
        "canary_reliability_threshold_exceeded",
        "reliability_threshold_exceeded",
    ),
    ("canary_candidate_cap", "candidate_cap"),
)
_PROPOSER_ADMISSION_ERROR_CODES = frozenset(
    {
        "ensemble_provider_admission_error",
        "ensemble_provider_admission_timeout",
        "ensemble_provider_admission_capacity",
    }
)
_ANALYZER_SOURCE_FAMILIES = {
    "llm_provider": "live_provider",
    "frozen_replay": "frozen_replay",
    "router_fallback": "fallback",
    "analyzer_postprocess_failed": "fallback",
    "router_anchor": "local",
    "legacy_model_options": "local",
}
_AGGREGATOR_SELECTED_KINDS = frozenset(
    {
        "primary",
        "continuation",
        "same_model_recovery",
        "model_fallback",
        "continuation_fallback",
        "partial_salvage",
        "degraded_delivery",
    }
)
_AGGREGATOR_ATTEMPT_KINDS = frozenset(
    {
        "primary",
        "continuation",
        "same_model_recovery",
        "model_fallback",
        "continuation_fallback",
    }
)
_AGGREGATOR_UNAVAILABLE_OUTCOMES = frozenset(
    {
        "member_unavailable",
        "provider_build_failed",
        "runtime_health_deferred",
        "tool_capability_unavailable",
    }
)
_AGGREGATOR_KNOWN_OUTCOMES = frozenset(
    {
        "succeeded",
        "failed",
        "abandoned",
        *_AGGREGATOR_UNAVAILABLE_OUTCOMES,
    }
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if type(value) is dict else {}


def _enum_token(value: Any) -> str:
    if type(value) is not str or len(value) > 64:
        return ""
    return value.strip()


def _non_negative_int(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= _MAX_METRIC_INT else None


def _bounded_metric_sum(values: list[int]) -> int | None:
    total = 0
    for value in values:
        if value > _MAX_METRIC_INT - total:
            return None
        total += value
    return total


def _non_negative_float(value: Any) -> float | None:
    if type(value) is int:
        if value < 0 or value > _MAX_METRIC_INT:
            return None
        return float(value)
    if type(value) is not float:
        return None
    normalized = value
    if (
        not math.isfinite(normalized)
        or normalized < 0
        or normalized > _MAX_METRIC_INT
    ):
        return None
    return normalized


def _bounded_metric_float_sum(values: list[float]) -> float | None:
    try:
        total = math.fsum(values)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(total) or total > _MAX_METRIC_INT:
        return None
    return total


def _http_status(value: Any) -> int | None:
    token = _enum_token(value)
    if len(token) != 3 or not token.isascii() or not token.isdigit():
        return None
    status = int(token)
    return status if 100 <= status <= 599 else None


def _usage_row_is_unknown(row: Mapping[str, Any]) -> bool:
    provider_usage = _mapping(row.get("provider_usage"))
    return bool(
        row.get("usage_unknown") is True
        or provider_usage.get("usage_unknown") is True
    )


def _usage_row_has_missing_marker(row: Mapping[str, Any]) -> bool:
    for container in (row, _mapping(row.get("provider_usage"))):
        if container.get("usage_unknown") is True:
            return True
        if "usage_missing_count" in container:
            missing_count = _non_negative_int(
                container.get("usage_missing_count")
            )
            if missing_count is None or missing_count > 0:
                return True
    return False


def _non_negative_seconds_to_ms(value: Any) -> int | None:
    if type(value) is int:
        if value < 0 or value > _MAX_METRIC_INT // 1_000:
            return None
        return value * 1_000
    if type(value) is not float or not math.isfinite(value) or value < 0:
        return None
    scaled = value * 1_000
    if not math.isfinite(scaled) or scaled > _MAX_METRIC_INT:
        return None
    rounded = int(round(scaled))
    return rounded if rounded <= _MAX_METRIC_INT else None


def _aggregator_attempt_has_evidence(value: Any) -> bool:
    if type(value) is not dict:
        return False
    kind = _enum_token(value.get("kind"))
    outcome = _enum_token(value.get("outcome"))
    return bool(
        kind in _AGGREGATOR_ATTEMPT_KINDS
        or outcome in _AGGREGATOR_KNOWN_OUTCOMES
        or type(value.get("request_started")) is bool
        or _non_negative_int(value.get("physical_request_count")) is not None
    )


def _aggregator_stage_observed(recovery: Mapping[str, Any]) -> bool:
    raw_attempts = recovery.get("attempts")
    if type(raw_attempts) is list and any(
        _aggregator_attempt_has_evidence(attempt)
        for attempt in raw_attempts[:_MAX_AGGREGATOR_RECOVERY_ATTEMPT_ROWS]
    ):
        return True
    return _enum_token(recovery.get("selected_kind")) in (
        _AGGREGATOR_SELECTED_KINDS
    )


def _selection_family(trace: Mapping[str, Any]) -> str:
    strategy = _enum_token(trace.get("selection_strategy"))
    if not strategy:
        strategy = _enum_token(
            _mapping(trace.get("selection_plan")).get("strategy")
        )
    if strategy == "router_dynamic":
        return "router_dynamic"
    if strategy == "router_tree_baseline":
        return "router_tree_baseline"
    if strategy:
        return "fixed"
    return "unknown"


def _project_ranking_stage_metrics(
    trace: Mapping[str, Any],
    metrics: dict[str, Any],
) -> None:
    raw_stage = trace.get("ranking_stage_observability")
    stage_observed = bool(
        type(raw_stage) is dict
        and raw_stage.get("schema")
        == _RANKING_STAGE_OBSERVABILITY_SCHEMA
    )
    metrics["ranking_stage_observed"] = stage_observed

    stage = raw_stage if stage_observed else {}
    all_timings_observed = True
    for source_field, metric_field in (
        ("snapshot_build_ms", "ranking_snapshot_build_ms"),
        ("hard_filter_ms", "ranking_hard_filter_ms"),
        ("score_ms", "ranking_score_ms"),
    ):
        value = _non_negative_int(stage.get(source_field))
        observed = bool(stage_observed and value is not None)
        metrics[f"{metric_field}_observed"] = observed
        all_timings_observed = all_timings_observed and observed
        if observed:
            metrics[metric_field] = value

    raw_cache_hit = stage.get("packaged_template_cache_hit")
    cache_hit_observed = bool(
        stage_observed and type(raw_cache_hit) is bool
    )
    metrics["ranking_packaged_template_cache_hit_observed"] = (
        cache_hit_observed
    )
    if cache_hit_observed:
        metrics["ranking_packaged_template_cache_hit"] = raw_cache_hit

    shape_valid = bool(
        stage_observed
        and set(stage).issubset(_RANKING_STAGE_OBSERVABILITY_FIELDS)
        and (
            "packaged_template_cache_hit" not in stage
            or cache_hit_observed
        )
    )
    metrics["ranking_stage_projection_complete"] = bool(
        shape_valid and all_timings_observed
    )


def _project_task_analyzer_metrics(
    trace: Mapping[str, Any],
    metrics: dict[str, Any],
) -> None:
    selection_plan = _mapping(trace.get("selection_plan"))
    raw_analyzer = selection_plan.get("task_analyzer")
    analyzer_observed = type(raw_analyzer) is dict
    metrics["task_analyzer_observed"] = analyzer_observed
    if not analyzer_observed:
        return

    analyzer = raw_analyzer
    source = _enum_token(analyzer.get("source"))
    metrics["task_analyzer_source_observed"] = bool(source)
    if source:
        metrics["task_analyzer_source_family"] = (
            _ANALYZER_SOURCE_FAMILIES.get(source, "unknown")
        )

    raw_schema_valid = analyzer.get("schema_valid")
    schema_valid_observed = type(raw_schema_valid) is bool
    metrics["task_analyzer_schema_valid_observed"] = schema_valid_observed
    if schema_valid_observed:
        metrics["task_analyzer_schema_valid"] = raw_schema_valid

    raw_chain = analyzer.get("chain")
    chain_observed = type(raw_chain) is dict
    metrics["task_analyzer_chain_observed"] = chain_observed
    if not chain_observed:
        return

    chain = raw_chain
    raw_attempts = chain.get("attempt_outcomes")
    attempts_observed = type(raw_attempts) is list
    metrics["task_analyzer_chain_attempts_observed"] = attempts_observed
    if attempts_observed:
        scanned_attempts = raw_attempts[:_MAX_ANALYZER_ATTEMPT_ROWS]
        success_count = 0
        failed_count = 0
        physical_counts: list[int] = []
        for raw_attempt in scanned_attempts:
            attempt = _mapping(raw_attempt)
            outcome = _enum_token(attempt.get("outcome"))
            if outcome == "success":
                success_count += 1
            elif outcome == "failed":
                failed_count += 1
            physical_count = _non_negative_int(
                attempt.get("physical_request_count")
            )
            if physical_count is not None:
                physical_counts.append(physical_count)
        metrics.update(
            {
                "task_analyzer_chain_attempt_count": len(raw_attempts),
                "task_analyzer_chain_attempt_scan_count": len(
                    scanned_attempts
                ),
                "task_analyzer_chain_attempt_scan_capped": (
                    len(raw_attempts) > _MAX_ANALYZER_ATTEMPT_ROWS
                ),
                "task_analyzer_chain_success_count": success_count,
                "task_analyzer_chain_failed_count": failed_count,
                "task_analyzer_chain_physical_request_observation_count": (
                    len(physical_counts)
                ),
            }
        )
        physical_total = _bounded_metric_sum(physical_counts)
        if physical_total is not None:
            metrics["task_analyzer_chain_physical_request_count"] = (
                physical_total
            )

    raw_selected_index = chain.get("selected_index")
    selected_index = _non_negative_int(raw_selected_index)
    selected_field_observed = (
        "selected_index" in chain
        and (raw_selected_index is None or selected_index is not None)
    )
    metrics["task_analyzer_selected_field_observed"] = (
        selected_field_observed
    )
    if selected_field_observed:
        metrics["task_analyzer_selected"] = selected_index is not None
    if selected_index is not None:
        metrics["task_analyzer_selected_index"] = selected_index

    raw_exhausted = chain.get("exhausted")
    exhausted_observed = type(raw_exhausted) is bool
    metrics["task_analyzer_exhausted_observed"] = exhausted_observed
    if exhausted_observed:
        metrics["task_analyzer_exhausted"] = raw_exhausted

    raw_deadline = chain.get("deadline")
    deadline_observed = type(raw_deadline) is dict
    metrics["task_analyzer_deadline_observed"] = deadline_observed
    if not deadline_observed:
        return
    deadline = raw_deadline
    for source_key, target_key in (
        ("configured_seconds", "task_analyzer_deadline_configured_ms"),
        ("elapsed_seconds", "task_analyzer_elapsed_ms"),
        ("remaining_seconds", "task_analyzer_deadline_remaining_ms"),
    ):
        value = _non_negative_seconds_to_ms(deadline.get(source_key))
        if value is not None:
            metrics[target_key] = value
    raw_expired = deadline.get("expired")
    expired_observed = type(raw_expired) is bool
    metrics["task_analyzer_deadline_expired_observed"] = expired_observed
    if expired_observed:
        metrics["task_analyzer_deadline_expired"] = raw_expired


def _project_aggregator_recovery_metrics(
    trace: Mapping[str, Any],
    metrics: dict[str, Any],
) -> None:
    raw_recovery = trace.get("aggregator_recovery")
    recovery_observed = type(raw_recovery) is dict
    metrics["aggregator_recovery_observed"] = recovery_observed
    stage_observed = bool(
        recovery_observed and _aggregator_stage_observed(raw_recovery)
    )
    metrics["aggregator_stage_observed"] = stage_observed
    metrics["aggregator_physical_request_count_observed"] = False
    if not recovery_observed:
        return

    recovery = raw_recovery
    raw_attempts = recovery.get("attempts")
    attempts_observed = type(raw_attempts) is list
    metrics["aggregator_recovery_attempts_observed"] = attempts_observed
    if attempts_observed:
        scanned_attempts = raw_attempts[
            :_MAX_AGGREGATOR_RECOVERY_ATTEMPT_ROWS
        ]
        kind_counts = {
            kind: 0 for kind in _AGGREGATOR_ATTEMPT_KINDS
        }
        unknown_kind_count = 0
        request_started_observation_count = 0
        request_started_count = 0
        physical_counts: list[int] = []
        succeeded_count = 0
        failed_count = 0
        abandoned_count = 0
        unavailable_count = 0
        unknown_outcome_count = 0
        runtime_health_deferred_count = 0
        runtime_health_benched_count = 0
        runtime_health_half_open_busy_count = 0
        runtime_health_unknown_reason_count = 0
        http_status_observation_count = 0
        rate_limited_terminal_count = 0
        upstream_5xx_terminal_count = 0
        for raw_attempt in scanned_attempts:
            attempt = _mapping(raw_attempt)
            kind = _enum_token(attempt.get("kind"))
            if kind in kind_counts:
                kind_counts[kind] += 1
            else:
                unknown_kind_count += 1
            raw_request_started = attempt.get("request_started")
            if type(raw_request_started) is bool:
                request_started_observation_count += 1
                request_started_count += int(raw_request_started)
            physical_count = _non_negative_int(
                attempt.get("physical_request_count")
            )
            if physical_count is not None:
                physical_counts.append(physical_count)
            status = (
                _http_status(attempt.get("code"))
                if raw_request_started is True
                and physical_count is not None
                and physical_count > 0
                else None
            )
            if status is not None:
                http_status_observation_count += 1
                rate_limited_terminal_count += int(status == 429)
                upstream_5xx_terminal_count += int(500 <= status <= 599)
            outcome = _enum_token(attempt.get("outcome"))
            if outcome == "succeeded":
                succeeded_count += 1
            elif outcome == "failed":
                failed_count += 1
            elif outcome == "abandoned":
                abandoned_count += 1
            elif outcome in _AGGREGATOR_UNAVAILABLE_OUTCOMES:
                if raw_request_started is False and physical_count == 0:
                    unavailable_count += 1
                    if outcome == "runtime_health_deferred":
                        runtime_health_deferred_count += 1
                        reason = _enum_token(attempt.get("code"))
                        if reason == _RUNTIME_HEALTH_BENCHED_REASON:
                            runtime_health_benched_count += 1
                        elif reason == _RUNTIME_HEALTH_HALF_OPEN_BUSY_REASON:
                            runtime_health_half_open_busy_count += 1
                        else:
                            runtime_health_unknown_reason_count += 1
                else:
                    # An unavailable attempt is, by contract, pre-dispatch.
                    # Missing or contradictory dispatch evidence cannot enter
                    # the genuine-unavailable bucket.
                    unknown_outcome_count += 1
            else:
                unknown_outcome_count += 1
        metrics.update(
            {
                "aggregator_recovery_attempt_count": len(raw_attempts),
                "aggregator_recovery_attempt_scan_count": len(
                    scanned_attempts
                ),
                "aggregator_recovery_attempt_scan_capped": (
                    len(raw_attempts)
                    > _MAX_AGGREGATOR_RECOVERY_ATTEMPT_ROWS
                ),
                "aggregator_primary_attempt_count": kind_counts["primary"],
                "aggregator_continuation_attempt_count": kind_counts[
                    "continuation"
                ],
                "aggregator_same_model_recovery_attempt_count": kind_counts[
                    "same_model_recovery"
                ],
                "aggregator_model_fallback_attempt_count": kind_counts[
                    "model_fallback"
                ],
                "aggregator_continuation_fallback_attempt_count": (
                    kind_counts["continuation_fallback"]
                ),
                "aggregator_unknown_kind_attempt_count": unknown_kind_count,
                "aggregator_request_started_observation_count": (
                    request_started_observation_count
                ),
                "aggregator_request_started_count": request_started_count,
                "aggregator_physical_request_observation_count": len(
                    physical_counts
                ),
                "aggregator_succeeded_attempt_count": succeeded_count,
                "aggregator_failed_attempt_count": failed_count,
                "aggregator_abandoned_attempt_count": abandoned_count,
                "aggregator_unsuccessful_attempt_count": (
                    failed_count + abandoned_count
                ),
                "aggregator_unavailable_attempt_count": unavailable_count,
                "aggregator_unknown_outcome_attempt_count": (
                    unknown_outcome_count
                ),
                "aggregator_runtime_health_deferred_count": (
                    runtime_health_deferred_count
                ),
                "aggregator_runtime_health_benched_deferred_count": (
                    runtime_health_benched_count
                ),
                "aggregator_runtime_health_half_open_busy_deferred_count": (
                    runtime_health_half_open_busy_count
                ),
                "aggregator_runtime_health_unknown_deferred_count": (
                    runtime_health_unknown_reason_count
                ),
                "aggregator_runtime_health_observed": bool(
                    runtime_health_deferred_count
                ),
                "aggregator_logical_terminal_http_status_observation_count": (
                    http_status_observation_count
                ),
                "aggregator_logical_terminal_http_status_observed": bool(
                    http_status_observation_count
                ),
                "aggregator_logical_terminal_rate_limited_count": (
                    rate_limited_terminal_count
                ),
                "aggregator_logical_terminal_upstream_5xx_count": (
                    upstream_5xx_terminal_count
                ),
            }
        )
        physical_projection_complete = bool(
            stage_observed
            and len(raw_attempts)
            <= _MAX_AGGREGATOR_RECOVERY_ATTEMPT_ROWS
            and len(physical_counts) == len(raw_attempts)
        )
        metrics["aggregator_physical_request_count_observed"] = (
            physical_projection_complete
        )
        physical_total = _bounded_metric_sum(physical_counts)
        if physical_projection_complete and physical_total is not None:
            metrics["aggregator_physical_request_count"] = physical_total

    selected_kind = _enum_token(recovery.get("selected_kind"))
    metrics["aggregator_selected_kind_observed"] = bool(selected_kind)
    if selected_kind:
        metrics["aggregator_selected_kind"] = (
            selected_kind
            if selected_kind in _AGGREGATOR_SELECTED_KINDS
            else "unknown"
        )
    fallback_index = _non_negative_int(recovery.get("fallback_index"))
    metrics["aggregator_fallback_index_observed"] = fallback_index is not None
    if fallback_index is not None:
        metrics["aggregator_fallback_index"] = fallback_index

    for source_key, observed_key, target_key in (
        (
            "success",
            "aggregator_recovery_success_observed",
            "aggregator_recovery_success",
        ),
        (
            "exhausted",
            "aggregator_recovery_exhausted_observed",
            "aggregator_recovery_exhausted",
        ),
        (
            "degraded",
            "aggregator_recovery_degraded_observed",
            "aggregator_recovery_degraded",
        ),
    ):
        raw_value = recovery.get(source_key)
        observed = stage_observed and type(raw_value) is bool
        metrics[observed_key] = observed
        if observed:
            metrics[target_key] = raw_value

    for source_key, observed_key, target_key in (
        (
            "continuation_count",
            "aggregator_continuation_count_observed",
            "aggregator_continuation_count",
        ),
        (
            "same_model_recovery_count",
            "aggregator_same_model_recovery_count_observed",
            "aggregator_same_model_recovery_count",
        ),
    ):
        value = _non_negative_int(recovery.get(source_key))
        observed = stage_observed and value is not None
        metrics[observed_key] = observed
        if observed:
            metrics[target_key] = value


def _project_runtime_health_filter_metrics(
    trace: Mapping[str, Any],
    metrics: dict[str, Any],
) -> None:
    selection_plan = _mapping(trace.get("selection_plan"))
    raw_filter = selection_plan.get("runtime_health_filter")
    observed = type(raw_filter) is dict
    metrics["runtime_health_filter_observed"] = observed
    if not observed:
        return

    runtime_filter = raw_filter
    raw_enabled = runtime_filter.get("enabled")
    metrics["runtime_health_filter_enabled_observed"] = (
        type(raw_enabled) is bool
    )
    if type(raw_enabled) is bool:
        metrics["runtime_health_filter_enabled"] = raw_enabled

    raw_requires_rerank = runtime_filter.get("requires_rerank")
    metrics["runtime_health_requires_rerank_observed"] = (
        type(raw_requires_rerank) is bool
    )
    if type(raw_requires_rerank) is bool:
        metrics["runtime_health_requires_rerank"] = raw_requires_rerank

    for source_key, target_key in (
        ("input_candidate_count", "runtime_health_input_candidate_count"),
        (
            "fresh_deployment_count",
            "runtime_health_fresh_deployment_count",
        ),
    ):
        value = _non_negative_int(runtime_filter.get(source_key))
        if value is not None:
            metrics[target_key] = value

    active_unavailable = _mapping(
        runtime_filter.get("active_unavailable_by_role")
    )
    filtered = _mapping(runtime_filter.get("filtered_by_role"))
    half_open = _mapping(runtime_filter.get("half_open_by_role"))
    minimum = _mapping(
        runtime_filter.get("never_strand_minimum_by_role")
    )
    exemptions = _mapping(
        runtime_filter.get("never_strand_exempt_identities_by_role")
    )
    for role in ("proposer", "aggregator"):
        for source, target in (
            (
                active_unavailable,
                f"runtime_health_{role}_active_unavailable_count",
            ),
            (filtered, f"runtime_health_{role}_filtered_count"),
            (half_open, f"runtime_health_{role}_half_open_count"),
            (
                minimum,
                f"runtime_health_{role}_never_strand_minimum",
            ),
        ):
            value = _non_negative_int(source.get(role))
            if value is not None:
                metrics[target] = value
        role_exemptions = exemptions.get(role)
        if type(role_exemptions) is list and len(role_exemptions) <= _MAX_METRIC_INT:
            # The identities themselves are deliberately never copied.
            metrics[
                f"runtime_health_{role}_never_strand_exempt_count"
            ] = len(role_exemptions)

    raw_never_strand = runtime_filter.get("never_strand")
    metrics["runtime_health_never_strand_observed"] = (
        type(raw_never_strand) is bool
    )
    if type(raw_never_strand) is bool:
        metrics["runtime_health_never_strand"] = raw_never_strand


def _project_canary_rollout_metrics(
    trace: Mapping[str, Any],
    metrics: dict[str, Any],
) -> None:
    selection_plan = _mapping(trace.get("selection_plan"))
    raw_rollout = selection_plan.get("canary_rollout")
    rollout_observed = bool(
        type(raw_rollout) is dict
        and raw_rollout.get("schema") == _CANARY_ROLLOUT_SCHEMA
    )
    metrics["canary_rollout_observed"] = rollout_observed
    metrics["canary_rollout_projection_complete"] = False
    metrics["canary_rollout_conservation_observed"] = False
    if not rollout_observed:
        return

    rollout = raw_rollout
    rollout_boolean_observations: list[bool] = []
    for source_key, target_key in (
        ("enabled", "canary_rollout_enabled"),
        ("config_valid", "canary_rollout_config_valid"),
    ):
        raw_value = rollout.get(source_key)
        observed_key = f"{target_key}_observed"
        value_observed = type(raw_value) is bool
        rollout_boolean_observations.append(value_observed)
        metrics[observed_key] = value_observed
        if value_observed:
            metrics[target_key] = raw_value

    input_count = _non_negative_int(rollout.get("input_canary_count"))
    metrics["canary_rollout_input_canary_count_observed"] = (
        input_count is not None
    )
    if input_count is not None:
        metrics["canary_rollout_input_canary_count"] = input_count

    admitted_by_role = rollout.get("admitted_by_role")
    proposer_admitted = (
        _non_negative_int(admitted_by_role.get("proposer"))
        if type(admitted_by_role) is dict
        else None
    )
    aggregator_admitted = (
        _non_negative_int(admitted_by_role.get("aggregator"))
        if type(admitted_by_role) is dict
        else None
    )
    admitted_count_types_observed = bool(
        proposer_admitted is not None
        and aggregator_admitted is not None
    )
    admitted_counts_observed = bool(
        admitted_count_types_observed
        and input_count is not None
        and proposer_admitted <= 1
        and aggregator_admitted == 0
        and proposer_admitted + aggregator_admitted <= input_count
    )
    metrics["canary_rollout_admitted_counts_observed"] = (
        admitted_counts_observed
    )
    if admitted_counts_observed:
        metrics["canary_rollout_proposer_admitted_count"] = (
            proposer_admitted
        )
        metrics["canary_rollout_aggregator_admitted_count"] = (
            aggregator_admitted
        )

    raw_task_gate = rollout.get("task_gate")
    task_gate_observed = type(raw_task_gate) is dict
    metrics["canary_task_gate_observed"] = task_gate_observed
    task_boolean_observations: list[bool] = []
    risk_observed = False
    if task_gate_observed:
        task_gate = raw_task_gate
        for source_key, target_key in (
            (
                "analyzer_source_eligible",
                "canary_task_analyzer_source_eligible",
            ),
            ("schema_valid", "canary_task_schema_valid"),
            ("confidence_eligible", "canary_task_confidence_eligible"),
            ("eligible", "canary_task_eligible"),
        ):
            raw_value = task_gate.get(source_key)
            value_observed = type(raw_value) is bool
            task_boolean_observations.append(value_observed)
            metrics[f"{target_key}_observed"] = value_observed
            if value_observed:
                metrics[target_key] = raw_value
        risk = _enum_token(task_gate.get("risk"))
        risk_observed = risk in _CANARY_TASK_RISKS
        metrics["canary_task_risk_observed"] = risk_observed
        if risk_observed:
            metrics["canary_task_risk"] = risk

    raw_reason_counts = rollout.get("reason_counts")
    reason_counts: list[tuple[str, int]] = []
    if type(raw_reason_counts) is dict:
        for source_key, suffix in _CANARY_REASON_METRIC_SUFFIXES:
            count = _non_negative_int(raw_reason_counts.get(source_key))
            if count is None:
                reason_counts = []
                break
            reason_counts.append((suffix, count))
    reason_counts_observed = (
        len(reason_counts) == len(_CANARY_REASON_METRIC_SUFFIXES)
    )
    metrics["canary_rollout_reason_counts_observed"] = (
        reason_counts_observed
    )
    if reason_counts_observed:
        for suffix, count in reason_counts:
            metrics[f"canary_rollout_reason_{suffix}_count"] = count
    reason_count_total = _bounded_metric_sum(
        [count for _, count in reason_counts]
    )
    doubled_input_count = (
        _bounded_metric_sum([input_count, input_count])
        if input_count is not None
        else None
    )
    enabled = rollout.get("enabled")
    config_valid = rollout.get("config_valid")
    task_eligible = (
        raw_task_gate.get("eligible")
        if task_gate_observed
        else None
    )
    conservation_observed = bool(
        all(rollout_boolean_observations)
        and input_count is not None
        and admitted_count_types_observed
        and len(task_boolean_observations) == 4
        and all(task_boolean_observations)
        and reason_counts_observed
        and reason_count_total is not None
        and doubled_input_count is not None
    )
    metrics["canary_rollout_conservation_observed"] = conservation_observed
    conservation_valid = False
    if conservation_observed:
        assert input_count is not None
        assert proposer_admitted is not None
        assert doubled_input_count is not None
        assert reason_count_total is not None
        reason_count_by_suffix = dict(reason_counts)
        analyzer_source_eligible = raw_task_gate.get(
            "analyzer_source_eligible"
        )
        task_schema_valid = raw_task_gate.get("schema_valid")
        confidence_eligible = raw_task_gate.get("confidence_eligible")
        role_disabled_count = reason_count_by_suffix["role_disabled"]
        conservation_valid = bool(
            input_count >= 1
            and admitted_counts_observed
            and proposer_admitted <= doubled_input_count
            and reason_count_total
            == doubled_input_count - proposer_admitted
            and role_disabled_count >= input_count
            and (enabled is not True or config_valid is True)
            and (
                task_eligible is not True
                or (
                    analyzer_source_eligible is True
                    and task_schema_valid is True
                    and confidence_eligible is True
                )
            )
            and (
                proposer_admitted == 0
                or (
                    enabled is True
                    and config_valid is True
                    and task_eligible is True
                )
            )
        )
        metrics["canary_rollout_conservation_valid"] = conservation_valid
    metrics["canary_rollout_projection_complete"] = bool(
        all(rollout_boolean_observations)
        and input_count is not None
        and admitted_counts_observed
        and task_gate_observed
        and len(task_boolean_observations) == 4
        and all(task_boolean_observations)
        and risk_observed
        and reason_counts_observed
        and conservation_valid
    )


def _project_canary_physical_budget_metrics(
    trace: Mapping[str, Any],
    metrics: dict[str, Any],
) -> None:
    raw_budget = trace.get("canary_physical_budget")
    budget_observed = bool(
        type(raw_budget) is dict
        and raw_budget.get("schema") == _CANARY_PHYSICAL_BUDGET_SCHEMA
    )
    metrics["canary_physical_budget_observed"] = budget_observed
    metrics["canary_physical_budget_projection_complete"] = False
    metrics["canary_physical_budget_accounting_observed"] = False
    metrics["canary_physical_budget_conservation_observed"] = False
    metrics["canary_physical_budget_exhausted_observed"] = False
    if not budget_observed:
        return

    budget = raw_budget
    values = {
        key: _non_negative_int(budget.get(key))
        for key in ("limit", "committed", "reserved", "rejected", "refunded")
    }
    accounting_observed = all(value is not None for value in values.values())
    metrics["canary_physical_budget_accounting_observed"] = (
        accounting_observed
    )
    if not accounting_observed:
        return

    limit = values["limit"]
    committed = values["committed"]
    reserved = values["reserved"]
    rejected = values["rejected"]
    refunded = values["refunded"]
    assert limit is not None
    assert committed is not None
    assert reserved is not None
    assert rejected is not None
    assert refunded is not None
    active_and_committed = _bounded_metric_sum([committed, reserved])
    conservation_valid = bool(
        limit == 1
        and active_and_committed is not None
        and active_and_committed <= limit
        and (
            rejected == 0
            or committed > 0
            or reserved > 0
            or refunded > 0
        )
    )
    metrics["canary_physical_budget_conservation_observed"] = True
    metrics["canary_physical_budget_conservation_valid"] = (
        conservation_valid
    )
    if not conservation_valid:
        return

    metrics.update(
        {
            "canary_physical_budget_projection_complete": True,
            "canary_physical_budget_limit": limit,
            "canary_physical_budget_committed": committed,
            "canary_physical_budget_reserved": reserved,
            "canary_physical_budget_rejected": rejected,
            "canary_physical_budget_refunded": refunded,
            "canary_physical_budget_exhausted_observed": True,
            # A rejection is direct evidence that the physical ceiling
            # prevented at least one attempted canary reservation.
            "canary_physical_budget_exhausted": rejected > 0,
        }
    )


def _project_persistent_canary_rollout_metrics(
    trace: Mapping[str, Any],
    metrics: dict[str, Any],
) -> None:
    raw_evidence = trace.get("canary_persistent_rollout")
    observed = bool(
        type(raw_evidence) is dict
        and raw_evidence.get("schema") == _CANARY_PERSISTENT_ROLLOUT_SCHEMA
    )
    metrics["canary_persistent_rollout_observed"] = observed
    metrics["canary_persistent_rollout_projection_complete"] = False
    metrics["canary_persistent_rollout_enabled_observed"] = False
    metrics["canary_persistent_rollout_receipt_count_observed"] = False
    if not observed:
        return

    evidence = raw_evidence
    raw_enabled = evidence.get("enabled")
    enabled_observed = type(raw_enabled) is bool
    metrics["canary_persistent_rollout_enabled_observed"] = enabled_observed
    if enabled_observed:
        metrics["canary_persistent_rollout_enabled"] = raw_enabled

    receipt_count = _non_negative_int(evidence.get("receipt_count"))
    receipt_count_observed = bool(
        receipt_count is not None
        and receipt_count <= _MAX_CANARY_PERSISTENT_RECEIPTS
    )
    metrics["canary_persistent_rollout_receipt_count_observed"] = (
        receipt_count_observed
    )
    if receipt_count_observed:
        metrics["canary_persistent_rollout_receipt_count"] = receipt_count

    raw_receipts = evidence.get("receipts")
    if not (
        set(evidence) == {"schema", "enabled", "receipt_count", "receipts"}
        and raw_enabled is True
        and receipt_count_observed
        and type(raw_receipts) is list
        and len(raw_receipts) == receipt_count
        and len(raw_receipts) <= _MAX_CANARY_PERSISTENT_RECEIPTS
    ):
        return

    counters = {
        "admission_allowed": 0,
        "admission_denied": 0,
        "admission_unavailable": 0,
        "probe": 0,
        "settled": 0,
        "cancelled_before_request": 0,
        "mutation_unavailable": 0,
        "rollback_transition": 0,
        "recovery_transition": 0,
        "usage_observed": 0,
        "usage_missing": 0,
        **{
            f"provider_{suffix}": 0
            for _, suffix in _CANARY_PERSISTENT_PROVIDER_OUTCOME_SUFFIXES
        },
    }
    for raw_receipt in raw_receipts:
        if (
            type(raw_receipt) is not dict
            or set(raw_receipt) != _CANARY_PERSISTENT_RECEIPT_FIELDS
            or raw_receipt.get("schema") != _CANARY_PERSISTENT_ROLLOUT_SCHEMA
        ):
            return
        receipt = raw_receipt
        role = _enum_token(receipt.get("role"))
        available = receipt.get("available")
        allowed = receipt.get("allowed")
        probe = receipt.get("probe")
        admission_reason = _enum_token(receipt.get("admission_reason"))
        state_before = _enum_token(receipt.get("state_before"))
        mutation_available = receipt.get("mutation_available")
        mutation_applied = receipt.get("mutation_applied")
        mutation_reason = _enum_token(receipt.get("mutation_reason"))
        state_after = _enum_token(receipt.get("state_after"))
        latch_reason = _enum_token(receipt.get("latch_reason"))
        provider_outcome = _enum_token(receipt.get("provider_outcome"))
        usage_outcome = _enum_token(receipt.get("usage_outcome"))
        cancelled_before_request = receipt.get("cancelled_before_request")
        rollback_transition = receipt.get("rollback_transition")
        recovery_transition = receipt.get("recovery_transition")
        recovery_successes = _non_negative_int(
            receipt.get("recovery_successes")
        )
        if not (
            role in {"proposer", "aggregator"}
            and type(available) is bool
            and type(allowed) is bool
            and type(probe) is bool
            and admission_reason in _CANARY_PERSISTENT_ADMISSION_REASONS
            and state_before in _CANARY_PERSISTENT_STATES
            and type(mutation_available) is bool
            and type(mutation_applied) is bool
            and mutation_reason in _CANARY_PERSISTENT_MUTATION_REASONS
            and state_after in _CANARY_PERSISTENT_STATES
            and latch_reason in _CANARY_PERSISTENT_LATCH_REASONS
            and provider_outcome
            in _CANARY_PERSISTENT_PROVIDER_OUTCOMES | {"not_applicable"}
            and usage_outcome
            in _CANARY_PERSISTENT_USAGE_OUTCOMES | {"not_applicable"}
            and type(cancelled_before_request) is bool
            and type(rollback_transition) is bool
            and type(recovery_transition) is bool
            and recovery_successes is not None
        ):
            return
        if not (
            (available or admission_reason == "ledger_unavailable")
            and (not available or admission_reason != "ledger_unavailable")
            and (not allowed or available)
            and (probe == (admission_reason == "half_open_probe"))
            and (
                not allowed
                or admission_reason in {"active", "half_open_probe"}
            )
            and (
                allowed
                or admission_reason not in {"active", "half_open_probe"}
            )
            and (not probe or state_before == "half_open")
            and (probe or admission_reason != "half_open_probe")
            and (not mutation_applied or mutation_available)
            and (
                mutation_applied
                == (mutation_reason in {"applied", "cancelled"})
            )
            and (
                mutation_available
                or mutation_reason in {"ledger_unavailable", "not_attempted"}
            )
            and (
                not mutation_available
                or mutation_reason not in {"ledger_unavailable", "not_attempted"}
            )
            and rollback_transition
            == (state_before == "active" and state_after == "rolled_back")
            and recovery_transition == (probe and state_after == "active")
        ):
            return

        if not allowed:
            if not (
                mutation_available is False
                and mutation_applied is False
                and mutation_reason == "not_attempted"
                and state_after == state_before
                and provider_outcome == "not_applicable"
                and usage_outcome == "not_applicable"
                and cancelled_before_request is True
                and rollback_transition is False
                and recovery_transition is False
            ):
                return
        elif cancelled_before_request:
            if not (
                provider_outcome == "not_applicable"
                and usage_outcome == "not_applicable"
            ):
                return
        elif not (
            provider_outcome in _CANARY_PERSISTENT_PROVIDER_OUTCOMES
            and usage_outcome in _CANARY_PERSISTENT_USAGE_OUTCOMES
        ):
            return

        if allowed:
            counters["admission_allowed"] += 1
            if cancelled_before_request:
                counters["cancelled_before_request"] += 1
            else:
                counters["settled"] += 1
                counters[f"provider_{provider_outcome}"] += 1
                counters[f"usage_{usage_outcome}"] += 1
            if mutation_available is False:
                counters["mutation_unavailable"] += 1
        else:
            counters["admission_denied"] += 1
        if available is False:
            counters["admission_unavailable"] += 1
        if probe:
            counters["probe"] += 1
        if rollback_transition:
            counters["rollback_transition"] += 1
        if recovery_transition:
            counters["recovery_transition"] += 1

    assert receipt_count is not None
    if not (
        counters["admission_allowed"] + counters["admission_denied"]
        == receipt_count
        and counters["settled"]
        + counters["cancelled_before_request"]
        == counters["admission_allowed"]
        and counters["probe"] <= counters["admission_allowed"]
        and counters["rollback_transition"]
        <= counters["admission_allowed"]
        and counters["recovery_transition"] <= counters["settled"]
    ):
        return
    for suffix, value in counters.items():
        metrics[f"canary_persistent_rollout_{suffix}_count"] = value
    metrics["canary_persistent_rollout_projection_complete"] = True


def _project_proposer_runtime_health_and_failures(
    candidates: list[Any],
    *,
    candidates_observed: bool,
    metrics: dict[str, Any],
) -> None:
    metrics["proposer_runtime_health_observed"] = False
    metrics["proposer_logical_terminal_http_status_observed"] = False
    if not candidates_observed:
        return

    health_rows: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    http_statuses: list[int] = []
    for raw_candidate in candidates:
        candidate = _mapping(raw_candidate)
        execution = _mapping(candidate.get("execution"))
        raw_health = execution.get("runtime_health_admission")
        if type(raw_health) is dict:
            health_rows.append((candidate, raw_health))
        if (
            candidate.get("request_started") is True
            and (_non_negative_int(candidate.get("physical_request_count")) or 0)
            > 0
        ):
            status = _http_status(candidate.get("error_code"))
            if status is not None:
                http_statuses.append(status)

    metrics["proposer_runtime_health_observed"] = bool(health_rows)
    metrics["proposer_runtime_health_observation_count"] = len(health_rows)
    tracked_observation_count = 0
    tracked_count = 0
    state_observation_count = 0
    state_counts = {state: 0 for state in _RUNTIME_HEALTH_STATES}
    unknown_state_count = 0
    probe_observation_count = 0
    probe_count = 0
    benched_deferred_count = 0
    half_open_busy_deferred_count = 0
    unknown_deferred_count = 0
    for candidate, health in health_rows:
        raw_tracked = health.get("tracked")
        if type(raw_tracked) is bool:
            tracked_observation_count += 1
            tracked_count += int(raw_tracked)
        state = _enum_token(health.get("state"))
        if state:
            state_observation_count += 1
            if state in state_counts:
                state_counts[state] += 1
            else:
                unknown_state_count += 1
        raw_probe = health.get("probe")
        if type(raw_probe) is bool:
            probe_observation_count += 1
            probe_count += int(raw_probe)
        physical_count = _non_negative_int(
            candidate.get("physical_request_count")
        )
        if (
            candidate.get("request_started") is not False
            or physical_count != 0
        ):
            continue
        reason = _enum_token(health.get("reason"))
        if reason == _RUNTIME_HEALTH_BENCHED_REASON:
            benched_deferred_count += 1
        elif reason == _RUNTIME_HEALTH_HALF_OPEN_BUSY_REASON:
            half_open_busy_deferred_count += 1
        elif reason:
            unknown_deferred_count += 1
    metrics.update(
        {
            "proposer_runtime_health_tracked_observation_count": (
                tracked_observation_count
            ),
            "proposer_runtime_health_tracked_count": tracked_count,
            "proposer_runtime_health_state_observation_count": (
                state_observation_count
            ),
            "proposer_runtime_health_healthy_count": state_counts["healthy"],
            "proposer_runtime_health_benched_count": state_counts["benched"],
            "proposer_runtime_health_half_open_count": state_counts[
                "half_open"
            ],
            "proposer_runtime_health_unknown_state_count": (
                unknown_state_count
            ),
            "proposer_runtime_health_probe_observation_count": (
                probe_observation_count
            ),
            "proposer_runtime_health_probe_count": probe_count,
            "proposer_runtime_health_benched_deferred_count": (
                benched_deferred_count
            ),
            "proposer_runtime_health_half_open_busy_deferred_count": (
                half_open_busy_deferred_count
            ),
            "proposer_runtime_health_unknown_deferred_count": (
                unknown_deferred_count
            ),
            "proposer_logical_terminal_http_status_observed": bool(
                http_statuses
            ),
            "proposer_logical_terminal_http_status_observation_count": len(
                http_statuses
            ),
            "proposer_logical_terminal_rate_limited_count": sum(
                status == 429 for status in http_statuses
            ),
            "proposer_logical_terminal_upstream_5xx_count": sum(
                500 <= status <= 599 for status in http_statuses
            ),
        }
    )


def _project_proposer_role_usage(
    candidates: list[Any],
    *,
    candidates_observed: bool,
    candidate_scan_capped: bool,
    metrics: dict[str, Any],
) -> None:
    metrics["proposer_physical_request_count_observed"] = False
    metrics["proposer_unknown_usage_count_observed"] = False
    metrics["proposer_usage_observed"] = False
    if not candidates_observed:
        return

    physical_counts: list[int] = []
    missing_counts: list[int] = []
    usage_container_count = 0
    usage_row_lengths: list[int] = []
    usage_scan_count = 0
    usage_scan_capped = candidate_scan_capped
    usage_projection_complete = bool(
        candidates_observed and not candidate_scan_capped
    )
    known_rows: list[Mapping[str, Any]] = []
    unknown_row_count = 0
    malformed_row_count = 0

    for raw_candidate in candidates:
        candidate = _mapping(raw_candidate)
        request_started = candidate.get("request_started")
        physical_count = _non_negative_int(
            candidate.get("physical_request_count")
        )
        physical_consistent = bool(
            physical_count is not None
            and (
                (request_started is True and physical_count > 0)
                or (request_started is False and physical_count == 0)
            )
        )
        if physical_consistent and physical_count is not None:
            physical_counts.append(physical_count)
        else:
            usage_projection_complete = False
        missing_count = _non_negative_int(candidate.get("usage_missing_count"))
        if (
            missing_count is not None
            and physical_count is not None
            and missing_count <= physical_count
        ):
            missing_counts.append(missing_count)
        else:
            usage_projection_complete = False

        raw_rows = candidate.get("model_usage_breakdown")
        if type(raw_rows) is not list:
            if physical_count:
                usage_projection_complete = False
            continue
        usage_container_count += 1
        usage_row_lengths.append(len(raw_rows))
        if physical_count is None or len(raw_rows) != physical_count:
            usage_projection_complete = False
        scanned_rows = raw_rows[:_MAX_USAGE_ROWS_PER_CANDIDATE]
        usage_scan_count += len(scanned_rows)
        usage_scan_capped = bool(
            usage_scan_capped
            or len(raw_rows) > _MAX_USAGE_ROWS_PER_CANDIDATE
        )
        if usage_scan_capped:
            usage_projection_complete = False
        for raw_row in scanned_rows:
            if type(raw_row) is not dict:
                malformed_row_count += 1
                usage_projection_complete = False
                continue
            if _usage_row_is_unknown(raw_row):
                unknown_row_count += 1
                usage_projection_complete = False
                continue
            known_rows.append(raw_row)

    full_candidate_projection = bool(
        candidates_observed
        and not candidate_scan_capped
        and len(physical_counts) == len(candidates)
    )
    missing_total = _bounded_metric_sum(missing_counts)
    if (
        missing_total is None
        or len(missing_counts) != len(candidates)
        or missing_total != unknown_row_count
    ):
        usage_projection_complete = False
    metrics.update(
        {
            "proposer_physical_request_count_observation_count": len(
                physical_counts
            ),
            "proposer_physical_request_count_observed": (
                full_candidate_projection
            ),
            "proposer_unknown_usage_count_observation_count": len(
                missing_counts
            ),
            "proposer_unknown_usage_count_observed": bool(
                full_candidate_projection
                and len(missing_counts) == len(candidates)
            ),
            "proposer_usage_observed": usage_container_count > 0,
            "proposer_usage_projection_complete": (
                usage_projection_complete
            ),
            "proposer_usage_container_observation_count": usage_container_count,
            "proposer_usage_row_scan_count": usage_scan_count,
            "proposer_usage_row_scan_capped": usage_scan_capped,
            "proposer_usage_receipt_count": len(known_rows),
            "proposer_usage_unknown_row_count": unknown_row_count,
            "proposer_usage_malformed_row_count": malformed_row_count,
        }
    )
    if full_candidate_projection:
        physical_total = _bounded_metric_sum(physical_counts)
        if physical_total is not None:
            metrics["proposer_physical_request_count"] = physical_total
    if metrics["proposer_unknown_usage_count_observed"]:
        if missing_total is not None:
            metrics["proposer_unknown_usage_count"] = missing_total
    usage_row_count_observed = bool(
        usage_projection_complete
        and usage_container_count > 0
    )
    metrics["proposer_usage_row_count_observed"] = usage_row_count_observed
    usage_row_count = _bounded_metric_sum(usage_row_lengths)
    if usage_row_count_observed and usage_row_count is not None:
        metrics["proposer_usage_row_count"] = usage_row_count

    int_fields = {
        "input_tokens": "proposer_input_tokens",
        "output_tokens": "proposer_output_tokens",
        "reasoning_tokens": "proposer_reasoning_tokens",
        "cached_tokens": "proposer_cache_read_tokens",
        "cache_write_tokens": "proposer_cache_write_tokens",
    }
    projected_int_values: dict[str, list[int]] = {}
    for source_key, target_key in int_fields.items():
        values = [
            value
            for row in known_rows
            if (value := _non_negative_int(row.get(source_key))) is not None
        ]
        projected_int_values[source_key] = values
        metrics[f"{target_key}_observation_count"] = len(values)
        if (
            known_rows
            and usage_projection_complete
            and len(values) == len(known_rows)
        ):
            total = _bounded_metric_sum(values)
            if total is not None:
                metrics[target_key] = total

    cached_values = projected_int_values["cached_tokens"]
    if (
        known_rows
        and usage_projection_complete
        and len(cached_values) == len(known_rows)
    ):
        metrics["proposer_cache_hit_request_count"] = sum(
            value > 0 for value in cached_values
        )

    billed_costs = [
        value
        for row in known_rows
        if (value := _non_negative_float(row.get("billed_cost"))) is not None
    ]
    metrics["proposer_billed_cost_usd_observation_count"] = len(billed_costs)
    if (
        known_rows
        and usage_projection_complete
        and len(billed_costs) == len(known_rows)
    ):
        billed_total = _bounded_metric_float_sum(billed_costs)
        if billed_total is not None:
            metrics["proposer_billed_cost_usd"] = billed_total


def _project_aggregator_final_request_usage(
    trace: Mapping[str, Any],
    metrics: dict[str, Any],
) -> None:
    final_request = _mapping(trace.get("final_request"))
    raw_usage = final_request.get("usage")
    container_observed = bool(
        _enum_token(final_request.get("role")) == "aggregator"
        and type(raw_usage) is dict
    )
    metrics["aggregator_final_request_usage_container_observed"] = (
        container_observed
    )
    if not container_observed:
        metrics["aggregator_final_request_usage_projection_complete"] = False
        metrics["aggregator_final_request_usage_observed"] = False
        return

    usage = raw_usage
    int_fields = (
        ("input_tokens", "aggregator_final_request_input_tokens"),
        ("output_tokens", "aggregator_final_request_output_tokens"),
        (
            "reasoning_tokens",
            "aggregator_final_request_reasoning_tokens",
        ),
        ("cached_tokens", "aggregator_final_request_cache_read_tokens"),
        (
            "cache_write_tokens",
            "aggregator_final_request_cache_write_tokens",
        ),
    )
    int_values = {
        source_key: _non_negative_int(usage.get(source_key))
        for source_key, _ in int_fields
    }
    billed_cost = _non_negative_float(usage.get("billed_cost"))
    missing_marker = bool(
        _usage_row_has_missing_marker(usage)
        or _usage_row_has_missing_marker(final_request)
    )
    complete_fields = bool(
        all(value is not None for value in int_values.values())
        and billed_cost is not None
    )
    nonempty_evidence = bool(
        any((value or 0) > 0 for value in int_values.values())
        or (billed_cost or 0.0) > 0
    )
    projection_complete = bool(
        complete_fields and nonempty_evidence and not missing_marker
    )
    metrics["aggregator_final_request_usage_projection_complete"] = (
        projection_complete
    )
    metrics["aggregator_final_request_usage_observed"] = projection_complete
    if not projection_complete:
        return

    for source_key, target_key in int_fields:
        value = int_values[source_key]
        assert value is not None
        metrics[f"{target_key}_observed"] = True
        metrics[target_key] = value
    cached_tokens = int_values["cached_tokens"]
    assert cached_tokens is not None
    metrics["aggregator_final_request_cache_hit_observed"] = True
    metrics["aggregator_final_request_cache_hit"] = cached_tokens > 0
    assert billed_cost is not None
    metrics["aggregator_final_request_billed_cost_usd_observed"] = True
    metrics["aggregator_final_request_billed_cost_usd"] = billed_cost


def _compact_json_size(
    value: Mapping[str, Any],
    *,
    cap_bytes: int = TRACE_SIZE_CAP_BYTES,
    visit_cap: int = TRACE_SIZE_VISIT_CAP,
) -> tuple[int | None, bool, str]:
    """Count compact UTF-8 JSON bytes without materializing serialized text.

    Only exact built-in JSON containers/scalars are accepted. Traversal stops
    at the byte, visit, or depth cap, so a very large diagnostic string/list
    has a fixed measurement-work ceiling.
    """

    total = 0
    capped = False
    cap_reason = ""
    invalid = False
    visits = 0
    active_containers: set[int] = set()

    def add(size: int) -> bool:
        nonlocal total, capped, cap_reason
        remaining = cap_bytes - total
        if size > remaining:
            total = cap_bytes
            capped = True
            cap_reason = "byte_limit"
            return False
        total += size
        return True

    def add_string(text: str) -> None:
        nonlocal invalid
        if not add(1):
            return
        for offset in range(0, len(text), _JSON_STRING_CHUNK_CHARS):
            chunk = text[offset : offset + _JSON_STRING_CHUNK_CHARS]
            try:
                # JSON escaping happens in C over a fixed-size chunk. The two
                # surrounding ASCII quotes are counted once outside the loop.
                encoded_width = len(
                    json.dumps(
                        chunk,
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                ) - 2
            except (TypeError, ValueError, UnicodeEncodeError):
                invalid = True
                return
            if not add(encoded_width):
                return
        add(1)

    def visit(item: Any, *, depth: int) -> None:
        nonlocal capped, cap_reason, invalid, visits
        if capped or invalid:
            return
        if depth > _MAX_JSON_DEPTH:
            capped = True
            cap_reason = "depth_limit"
            return
        visits += 1
        if visits > visit_cap:
            capped = True
            cap_reason = "visit_limit"
            return
        if item is None:
            add(4)
            return
        if item is True:
            add(4)
            return
        if item is False:
            add(5)
            return
        if type(item) is str:
            add_string(item)
            return
        if type(item) is int:
            if not -_MAX_METRIC_INT <= item <= _MAX_METRIC_INT:
                invalid = True
                return
            add(len(str(item)))
            return
        if type(item) is float:
            if not math.isfinite(item):
                invalid = True
                return
            # A finite binary64 JSON representation has a small fixed bound.
            add(len(json.dumps(item, allow_nan=False)))
            return
        if type(item) not in {dict, list}:
            invalid = True
            return

        identity = id(item)
        if identity in active_containers:
            invalid = True
            return
        active_containers.add(identity)
        try:
            if type(item) is list:
                if not add(1):
                    return
                for index, child in enumerate(item):
                    if index and not add(1):
                        return
                    visit(child, depth=depth + 1)
                    if capped or invalid:
                        return
                add(1)
                return

            if not add(1):
                return
            for index, (key, child) in enumerate(item.items()):
                if type(key) is not str:
                    invalid = True
                    return
                if index and not add(1):
                    return
                add_string(key)
                if capped or invalid or not add(1):
                    return
                visit(child, depth=depth + 1)
                if capped or invalid:
                    return
            add(1)
        finally:
            active_containers.remove(identity)

    visit(value, depth=0)
    if invalid:
        return None, False, ""
    return total, capped, cap_reason


def _admission_role_family(value: Any) -> str:
    role = _enum_token(value)
    if role in {"proposer", "proposer_recovery"}:
        return "proposer"
    if role in {"aggregator", "aggregator_recovery"}:
        return "aggregator"
    if role == "fallback_single":
        return "fallback"
    return "unknown"


def _admission_observations(
    trace: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    rows: list[tuple[str, Mapping[str, Any]]] = []
    top_level = trace.get("admission")
    if type(top_level) is dict:
        rows.append((_admission_role_family(top_level.get("role")), top_level))

    raw_candidates = trace.get("candidates")
    if type(raw_candidates) is list:
        for candidate in raw_candidates[:_MAX_CANDIDATE_ROWS]:
            execution = _mapping(_mapping(candidate).get("execution"))
            admission = execution.get("admission")
            if type(admission) is dict:
                rows.append(("proposer", admission))

    final_request = _mapping(trace.get("final_request"))
    final_execution = _mapping(final_request.get("execution"))
    final_admission = final_execution.get("admission")
    if type(final_admission) is dict:
        raw_role = final_request.get("role") or final_admission.get("role")
        rows.append((_admission_role_family(raw_role), final_admission))
    return rows


def _proposer_admission_projection_state(
    candidates: list[Any],
    *,
    candidates_observed: bool,
    candidate_scan_capped: bool,
    observations: list[tuple[str, Mapping[str, Any]]],
) -> tuple[bool, bool]:
    rows = [row for family, row in observations if family == "proposer"]
    admission_error_observed = any(
        _enum_token(_mapping(candidate).get("error_code"))
        in _PROPOSER_ADMISSION_ERROR_CODES
        for candidate in candidates
    )
    observed = bool(rows or admission_error_observed)
    if not observed:
        return False, False
    if not candidates_observed or candidate_scan_capped:
        return True, False

    candidate_row_count = 0
    for raw_candidate in candidates:
        candidate = _mapping(raw_candidate)
        request_started = candidate.get("request_started")
        physical_count = _non_negative_int(
            candidate.get("physical_request_count")
        )
        execution = _mapping(candidate.get("execution"))
        raw_admission = execution.get("admission")
        admission = _mapping(raw_admission)
        error_code = _enum_token(candidate.get("error_code"))
        if type(raw_admission) is dict:
            candidate_row_count += 1
            outcome = _enum_token(admission.get("outcome"))
            if outcome == "admitted":
                if request_started is not True or physical_count != 1:
                    return True, False
            elif outcome in {"timeout", "rejected"}:
                if request_started is not False or physical_count != 0:
                    return True, False
            else:
                return True, False
            continue
        if error_code in _PROPOSER_ADMISSION_ERROR_CODES:
            # The current producer exposes the typed error but drops its
            # admission row while folding the child event into a candidate.
            return True, False
        if physical_count is None or type(request_started) is not bool:
            return True, False
        if physical_count > 0 and rows:
            # Admission is process-wide for one ensemble call. Once any row is
            # present, a started request with no row proves the projection is
            # not a complete count of admission attempts.
            return True, False

    # Top-level admission evidence cannot be joined to one candidate without a
    # stable attempt id. Treat it as a lower bound rather than double-counting.
    if candidate_row_count != len(rows):
        return True, False
    return True, True


def _aggregator_admission_projection_state(
    trace: Mapping[str, Any],
    observations: list[tuple[str, Mapping[str, Any]]],
) -> tuple[bool, bool]:
    rows = [row for family, row in observations if family == "aggregator"]
    observed = bool(rows)
    if not observed:
        return False, False

    final_request = _mapping(trace.get("final_request"))
    final_admission = _mapping(_mapping(final_request.get("execution")).get("admission"))
    raw_recovery = trace.get("aggregator_recovery")
    if (
        len(rows) != 1
        or not final_admission
        or rows[0] is not final_admission
        or type(raw_recovery) is not dict
    ):
        return True, False

    raw_attempts = raw_recovery.get("attempts")
    if type(raw_attempts) is not list or len(raw_attempts) != 1:
        # final_request retains only the last admission row. Without exactly
        # one recovery attempt, prior continuation/fallback admissions cannot
        # be joined and the visible row is only a lower bound.
        return True, False
    attempt = _mapping(raw_attempts[0])
    if not attempt or not _aggregator_attempt_has_evidence(attempt):
        return True, False
    request_started = attempt.get("request_started")
    physical_count = _non_negative_int(attempt.get("physical_request_count"))
    if (
        type(request_started) is not bool
        or physical_count is None
        or physical_count not in {0, 1}
        or request_started is not (physical_count == 1)
    ):
        return True, False

    admission_outcome = _enum_token(final_admission.get("outcome"))
    attempt_outcome = _enum_token(attempt.get("outcome"))
    if admission_outcome in {"timeout", "rejected"}:
        return True, request_started is False and physical_count == 0
    if admission_outcome != "admitted" or not attempt_outcome:
        return True, False
    if attempt_outcome in {
        "member_unavailable",
        "provider_build_failed",
        "tool_capability_unavailable",
    }:
        # These outcomes happen before queue admission; an admitted row cannot
        # be joined to them.
        return True, False
    return True, True


def _project_role_admission_metrics(
    observations: list[tuple[str, Mapping[str, Any]]],
    metrics: dict[str, Any],
    *,
    proposer_projection_observed: bool,
    proposer_projection_complete: bool,
    aggregator_projection_observed: bool,
    aggregator_projection_complete: bool,
) -> None:
    for role in ("proposer", "aggregator"):
        rows = [row for family, row in observations if family == role]
        role_observed = (
            proposer_projection_observed if role == "proposer" else aggregator_projection_observed
        )
        metrics[f"{role}_admission_observed"] = role_observed
        if role == "proposer":
            metrics["proposer_admission_projection_complete"] = proposer_projection_complete
        else:
            metrics["aggregator_admission_projection_complete"] = aggregator_projection_complete
        if not rows:
            continue
        waits = [
            wait for row in rows if (wait := _non_negative_int(row.get("wait_ms"))) is not None
        ]
        admitted_count = sum(_enum_token(row.get("outcome")) == "admitted" for row in rows)
        timeout_count = sum(_enum_token(row.get("outcome")) == "timeout" for row in rows)
        rejected_count = sum(_enum_token(row.get("outcome")) == "rejected" for row in rows)
        metrics.update(
            {
                f"{role}_admission_observation_count": len(rows),
                f"{role}_admission_wait_observation_count": len(waits),
            }
        )
        exact_projection = bool(
            proposer_projection_complete if role == "proposer" else aggregator_projection_complete
        )
        suffix = "" if exact_projection else "_lower_bound"
        metrics.update(
            {
                f"{role}_admission_admitted_count{suffix}": admitted_count,
                f"{role}_admission_timeout_count{suffix}": timeout_count,
                f"{role}_admission_rejected_count{suffix}": rejected_count,
            }
        )
        if waits:
            wait_suffix = "" if exact_projection and len(waits) == len(rows) else "_lower_bound"
            metrics[f"{role}_admission_wait_ms_max{wait_suffix}"] = max(waits)
            wait_total = _bounded_metric_sum(waits)
            if wait_total is not None:
                metrics[f"{role}_admission_wait_ms_total{wait_suffix}"] = wait_total


def build_ensemble_execution_metrics(
    trace: Mapping[str, Any],
    *,
    terminal_outcome: str,
) -> dict[str, Any]:
    """Build one content-free, bounded metric row from a terminal trace."""

    if terminal_outcome not in _TERMINAL_OUTCOMES:
        raise ValueError("terminal_outcome must be 'completed' or 'failed'")
    if type(trace) is not dict:
        raise TypeError("trace must be a built-in dict")

    raw_fallback_used = trace.get("fallback_used")
    fallback_used_observed = type(raw_fallback_used) is bool
    fallback_used = raw_fallback_used is True
    degradation_reasons = trace.get("degradation_reasons")
    aggregator_recovery = _mapping(trace.get("aggregator_recovery"))
    aggregator_stage_observed = _aggregator_stage_observed(
        aggregator_recovery
    )
    degraded = bool(
        fallback_used
        or (
            aggregator_stage_observed
            and aggregator_recovery.get("degraded") is True
        )
        or (
            type(degradation_reasons) is list
            and bool(degradation_reasons)
        )
        or _enum_token(trace.get("run_outcome"))
        in {"partial_proposer_quorum", "length_capped_usable"}
    )
    execution_status = (
        "failed"
        if terminal_outcome == "failed"
        else "degraded"
        if degraded
        else "success"
    )
    metrics: dict[str, Any] = {
        "schema": ENSEMBLE_EXECUTION_METRICS_SCHEMA,
        "terminal_outcome": terminal_outcome,
        "execution_status": execution_status,
        "selection_family": _selection_family(trace),
        "fallback_used_observed": fallback_used_observed,
    }
    if fallback_used_observed:
        metrics["fallback_used"] = fallback_used

    _project_ranking_stage_metrics(trace, metrics)
    _project_task_analyzer_metrics(trace, metrics)
    _project_aggregator_recovery_metrics(trace, metrics)
    _project_runtime_health_filter_metrics(trace, metrics)
    _project_canary_rollout_metrics(trace, metrics)
    _project_canary_physical_budget_metrics(trace, metrics)
    _project_persistent_canary_rollout_metrics(trace, metrics)
    _project_aggregator_final_request_usage(trace, metrics)

    trace_size, trace_size_capped, trace_size_cap_reason = (
        _compact_json_size(trace)
    )
    metrics["trace_size_observed"] = trace_size is not None
    if trace_size is not None:
        metrics["trace_compact_json_bytes_capped"] = trace_size_capped
        metrics["trace_compact_json_bytes_cap"] = TRACE_SIZE_CAP_BYTES
        metrics["trace_compact_json_visit_cap"] = TRACE_SIZE_VISIT_CAP
        if trace_size_capped:
            metrics["trace_compact_json_bytes_lower_bound"] = trace_size
            metrics["trace_compact_json_bytes_cap_reason"] = (
                trace_size_cap_reason
            )
        else:
            metrics["trace_compact_json_bytes"] = trace_size

    raw_candidates = trace.get("candidates")
    candidates_observed = type(raw_candidates) is list
    candidates = raw_candidates if candidates_observed else []
    scanned_candidates = candidates[:_MAX_CANDIDATE_ROWS]
    candidate_elapsed: list[int] = []
    for candidate in scanned_candidates:
        row = _mapping(candidate)
        if row.get("request_started") is not True:
            continue
        elapsed = _non_negative_int(row.get("elapsed_ms"))
        if elapsed is not None:
            candidate_elapsed.append(elapsed)
    metrics["proposer_candidates_observed"] = candidates_observed
    if candidates_observed:
        metrics.update(
            {
                "proposer_candidate_count": len(candidates),
                "proposer_candidate_scan_count": len(scanned_candidates),
                "proposer_candidate_scan_capped": (
                    len(candidates) > _MAX_CANDIDATE_ROWS
                ),
                "proposer_elapsed_observation_count": len(candidate_elapsed),
            }
        )
        if candidate_elapsed:
            metrics["proposer_candidate_elapsed_ms_max"] = max(
                candidate_elapsed
            )
            candidate_elapsed_total = _bounded_metric_sum(candidate_elapsed)
            if candidate_elapsed_total is not None:
                metrics["proposer_candidate_elapsed_ms_total"] = (
                    candidate_elapsed_total
                )

    _project_proposer_runtime_health_and_failures(
        scanned_candidates,
        candidates_observed=candidates_observed,
        metrics=metrics,
    )
    _project_proposer_role_usage(
        scanned_candidates,
        candidates_observed=candidates_observed,
        candidate_scan_capped=(len(candidates) > _MAX_CANDIDATE_ROWS),
        metrics=metrics,
    )

    admission_observations = _admission_observations(trace)
    (
        proposer_admission_observed,
        proposer_admission_projection_complete,
    ) = _proposer_admission_projection_state(
        scanned_candidates,
        candidates_observed=candidates_observed,
        candidate_scan_capped=(len(candidates) > _MAX_CANDIDATE_ROWS),
        observations=admission_observations,
    )
    (
        aggregator_admission_observed,
        aggregator_admission_projection_complete,
    ) = _aggregator_admission_projection_state(
        trace,
        admission_observations,
    )
    admissions = [row for _, row in admission_observations]
    admission_waits = [
        wait for row in admissions if (wait := _non_negative_int(row.get("wait_ms"))) is not None
    ]
    metrics.update(
        {
            "admission_observation_count": len(admissions),
            "admission_wait_observation_count": len(admission_waits),
            "admission_timeout_count": sum(
                1 for row in admissions if _enum_token(row.get("outcome")) == "timeout"
            ),
            "admission_rejected_count": sum(
                1 for row in admissions if _enum_token(row.get("outcome")) == "rejected"
            ),
        }
    )
    if admission_waits:
        metrics["admission_wait_ms_max"] = max(admission_waits)
        admission_wait_total = _bounded_metric_sum(admission_waits)
        if admission_wait_total is not None:
            metrics["admission_wait_ms_total"] = admission_wait_total
    _project_role_admission_metrics(
        admission_observations,
        metrics,
        proposer_projection_observed=proposer_admission_observed,
        proposer_projection_complete=(proposer_admission_projection_complete),
        aggregator_projection_observed=aggregator_admission_observed,
        aggregator_projection_complete=(aggregator_admission_projection_complete),
    )

    quorum = _mapping(trace.get("proposer_quorum"))
    metrics["quorum_observed"] = bool(quorum)
    if quorum:
        raw_quorum_reached = quorum.get("quorum_reached")
        quorum_reached_observed = type(raw_quorum_reached) is bool
        metrics["quorum_reached_observed"] = quorum_reached_observed
        if quorum_reached_observed:
            metrics["quorum_reached"] = raw_quorum_reached
        for source, target in (
            ("time_to_quorum_ms", "time_to_quorum_ms"),
            ("grace_elapsed_ms", "quorum_grace_elapsed_ms"),
            ("pending_at_quorum", "pending_at_quorum"),
        ):
            value = _non_negative_int(quorum.get(source))
            if value is not None:
                metrics[target] = value

        cancellation = _mapping(quorum.get("cancellation"))
        cleanup = _mapping(quorum.get("cleanup"))
        metrics["cleanup_observed"] = bool(cleanup)
        for source, target in (("requested_task_count", "quorum_cancel_requested_task_count"),):
            value = _non_negative_int(cancellation.get(source))
            if value is not None:
                metrics[target] = value
        for source, target in (
            ("awaited_task_count", "cleanup_awaited_task_count"),
            ("completed_task_count", "cleanup_completed_task_count"),
            ("lingering_task_count", "cleanup_lingering_task_count"),
            ("stream_close_proven_count", "cleanup_stream_close_proven_count"),
            (
                "stream_close_unproven_count",
                "cleanup_stream_close_unproven_count",
            ),
        ):
            value = _non_negative_int(cleanup.get(source))
            if value is not None:
                metrics[target] = value
    else:
        metrics["cleanup_observed"] = False

    recovery = _mapping(trace.get("proposer_recovery"))
    recovery_calls = _non_negative_int(
        recovery.get("additional_physical_requests_started")
    )
    metrics["proposer_recovery_observed"] = recovery_calls is not None
    if recovery_calls is not None:
        metrics["proposer_recovery_calls"] = recovery_calls

    physical_request_count = _non_negative_int(
        trace.get("physical_request_count")
    )
    metrics["physical_request_count_observed"] = (
        physical_request_count is not None
    )
    if physical_request_count is not None:
        metrics["physical_request_count"] = physical_request_count

    unknown_usage_count = _non_negative_int(trace.get("usage_missing_count"))
    metrics["unknown_usage_count_observed"] = unknown_usage_count is not None
    if unknown_usage_count is not None:
        metrics["unknown_usage_count"] = unknown_usage_count
    return metrics


def log_ensemble_execution_metrics(
    trace: Mapping[str, Any],
    *,
    terminal_outcome: str,
) -> None:
    """Emit metrics without allowing projection or logging to affect a turn."""

    try:
        metrics = build_ensemble_execution_metrics(
            trace,
            terminal_outcome=terminal_outcome,
        )
    except Exception:  # noqa: BLE001 - projection must fail open
        _warn_ensemble_execution_metrics_failed(terminal_outcome)
        return

    # The ordinary diagnostic log and the opt-in structured transport are
    # independent fail-open destinations.  A broken structlog processor must
    # not suppress a valid JSONL row, and a filesystem failure must not erase
    # the existing diagnostic event or affect the model turn.
    try:
        log.info("llm_ensemble.execution.metrics", **metrics)
    except Exception:  # noqa: BLE001 - diagnostic logging must fail open
        _warn_ensemble_execution_metrics_failed(terminal_outcome)
    try:
        write_ensemble_execution_metrics_jsonl(metrics)
    except Exception:  # noqa: BLE001 - retain a final integration guard
        pass


def _warn_ensemble_execution_metrics_failed(terminal_outcome: str) -> None:
    try:
        log.warning(
            "llm_ensemble.execution.metrics_failed",
            schema=ENSEMBLE_EXECUTION_METRICS_SCHEMA,
            terminal_outcome=(
                terminal_outcome
                if terminal_outcome in _TERMINAL_OUTCOMES
                else "invalid"
            ),
            exc_info=True,
        )
    except Exception:  # noqa: BLE001 - broken processors also fail open
        pass


def log_ensemble_execution_metrics_once(
    trace: Mapping[str, Any],
    *,
    terminal_outcome: str,
    already_emitted: bool,
) -> bool:
    """Emit at most once for one physical provider-call event stream."""

    if already_emitted or type(trace) is not dict:
        return already_emitted
    log_ensemble_execution_metrics(
        trace,
        terminal_outcome=terminal_outcome,
    )
    # A broken log processor is fail-open, but must not cause every malformed
    # duplicate terminal event to retry observability work on the hot path.
    return True
