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

log = structlog.get_logger(__name__)

ENSEMBLE_EXECUTION_METRICS_SCHEMA = "opensquilla.ensemble-execution-metrics/v1"
TRACE_SIZE_CAP_BYTES = 262_144
TRACE_SIZE_VISIT_CAP = 16_384
_MAX_CANDIDATE_ROWS = 64
_MAX_JSON_DEPTH = 64
_JSON_STRING_CHUNK_CHARS = 4_096
_MAX_METRIC_INT = (1 << 63) - 1
_TERMINAL_OUTCOMES = frozenset({"completed", "failed"})
_MAX_ANALYZER_ATTEMPT_ROWS = 8
_MAX_AGGREGATOR_RECOVERY_ATTEMPT_ROWS = 16
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
            outcome = _enum_token(attempt.get("outcome"))
            if outcome == "succeeded":
                succeeded_count += 1
            elif outcome == "failed":
                failed_count += 1
            elif outcome == "abandoned":
                abandoned_count += 1
            elif outcome in _AGGREGATOR_UNAVAILABLE_OUTCOMES:
                if raw_request_started is False:
                    unavailable_count += 1
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
            }
        )
        physical_total = _bounded_metric_sum(physical_counts)
        if physical_total is not None:
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


def _admission_rows(trace: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    top_level = trace.get("admission")
    if type(top_level) is dict:
        rows.append(top_level)

    raw_candidates = trace.get("candidates")
    if type(raw_candidates) is list:
        for candidate in raw_candidates[:_MAX_CANDIDATE_ROWS]:
            execution = _mapping(_mapping(candidate).get("execution"))
            admission = execution.get("admission")
            if type(admission) is dict:
                rows.append(admission)

    final_execution = _mapping(
        _mapping(trace.get("final_request")).get("execution")
    )
    final_admission = final_execution.get("admission")
    if type(final_admission) is dict:
        rows.append(final_admission)
    return rows


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

    _project_task_analyzer_metrics(trace, metrics)
    _project_aggregator_recovery_metrics(trace, metrics)

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

    admissions = _admission_rows(trace)
    admission_waits = [
        wait
        for row in admissions
        if (wait := _non_negative_int(row.get("wait_ms"))) is not None
    ]
    metrics.update(
        {
            "admission_observation_count": len(admissions),
            "admission_wait_observation_count": len(admission_waits),
            "admission_timeout_count": sum(
                1
                for row in admissions
                if _enum_token(row.get("outcome")) == "timeout"
            ),
            "admission_rejected_count": sum(
                1
                for row in admissions
                if _enum_token(row.get("outcome")) == "rejected"
            ),
        }
    )
    if admission_waits:
        metrics["admission_wait_ms_max"] = max(admission_waits)
        admission_wait_total = _bounded_metric_sum(admission_waits)
        if admission_wait_total is not None:
            metrics["admission_wait_ms_total"] = admission_wait_total

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
        for source, target in (
            ("requested_task_count", "quorum_cancel_requested_task_count"),
        ):
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
        log.info("llm_ensemble.execution.metrics", **metrics)
    except Exception:  # noqa: BLE001 - observability must fail open
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
