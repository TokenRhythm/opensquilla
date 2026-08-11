"""Shared DRACO paid-generation recovery and evidence-preservation helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from opensquilla.eval.draco_run_result import RunResult
from opensquilla.usage_evidence import USAGE_EVIDENCE_SCHEMA


def generation_postprocessing_failure_reason(
    stage: str,
    exc: Exception,
) -> str:
    """Return a non-sensitive terminal reason for paid-call postprocessing."""

    return (
        "generation_postprocessing_failed:"
        + str(stage or "unknown")
        + ":"
        + type(exc).__name__
    )


def primitive_unknown_usage_payload(
    *,
    physical_count: int,
    identity_seed: str,
    requested_provider: str,
    requested_model: str,
    role: str = "usage_missing",
) -> dict[str, Any]:
    """Build strict unknown units without depending on canonicalizers."""

    count = (
        physical_count
        if isinstance(physical_count, int)
        and not isinstance(physical_count, bool)
        and physical_count > 0
        else 0
    )
    units: list[dict[str, Any]] = []
    for ordinal in range(1, count + 1):
        canonical = json.dumps(
            {
                "identity_seed": str(identity_seed),
                "ordinal": ordinal,
                "role": role,
                "schema": USAGE_EVIDENCE_SCHEMA,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        evidence_id = (
            "sha256:"
            + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )
        units.append(
            {
                "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
                "usage_evidence_id": evidence_id,
                "usage_evidence_source": (
                    "emergency_physical_request_counter"
                ),
                "role": role,
                "physical_request_ordinal": ordinal,
                "provider": "",
                "model": "",
                "requested_provider": str(
                    requested_provider or ""
                ).strip(),
                "requested_model": str(
                    requested_model or ""
                ).strip(),
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
                "cost_source": "none",
                "usage_unknown": True,
                "provider_usage": {
                    "usage_unknown": True,
                    "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
                    "usage_evidence_id": evidence_id,
                },
            }
        )
    return {
        "usage_evidence_schema": USAGE_EVIDENCE_SCHEMA,
        "model_usage_breakdown": units,
        "usage_missing_count": count,
    }


def emergency_generation_run_summary_core(
    result: RunResult,
    *,
    reason: str,
    stage: str,
    exception_type: str,
    identity_seed: str,
    expected_provider: str = "",
    expected_model: str = "",
    dependencies: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist conservative physical evidence when normal summarization fails."""

    canonicalize_run_usage = dependencies["canonicalize_run_usage"]
    coerce_metric_int = dependencies["coerce_metric_int"]
    derive_physical_request_count = dependencies["derive_physical_request_count"]
    done_payload = dependencies["done_payload"]
    json_safe = dependencies["json_safe"]
    primitive_unknown_usage_payload = dependencies["primitive_unknown_usage_payload"]
    run_result_error_physical_request_count = dependencies[
        "run_result_error_physical_request_count"
    ]
    run_result_summary = dependencies["run_result_summary"]
    run_result_usage_payload = dependencies["run_result_usage_payload"]
    run_result_was_blocked_before_request = dependencies[
        "run_result_was_blocked_before_request"
    ]
    safe_provider_build_routing_trace = dependencies[
        "safe_provider_build_routing_trace"
    ]
    server_tool_counts_from_usage_payload = dependencies[
        "server_tool_counts_from_usage_payload"
    ]
    text_sha256 = dependencies["text_sha256"]
    usage_rows_request_count = dependencies["usage_rows_request_count"]
    usage_unknown_count_from_usage_payload = dependencies[
        "usage_unknown_count_from_usage_payload"
    ]

    source_error_present = bool(result.error)
    primitive_usage_fallback = False
    try:
        summary = run_result_summary(
            result,
            requested_provider=expected_provider or None,
            requested_model=expected_model or None,
        )
        summary_error_type = ""
        unvalidated_usage: Any = None
    except Exception as summary_exc:  # noqa: BLE001 - this is the last evidence boundary
        summary_error_type = type(summary_exc).__name__
        try:
            raw_usage = run_result_usage_payload(result)
        except Exception:  # noqa: BLE001 - retain a conservative unknown unit
            raw_usage = {}
        try:
            unvalidated_usage = json_safe(raw_usage)
        except Exception:  # noqa: BLE001 - never persist an unsafe object
            unvalidated_usage = {
                "capture_failed": True,
                "exception_type": summary_error_type,
            }
        if not isinstance(unvalidated_usage, Mapping):
            unvalidated_usage = {}
        breakdown = unvalidated_usage.get("model_usage_breakdown")
        represented_units = (
            len([unit for unit in breakdown if isinstance(unit, Mapping)])
            if isinstance(breakdown, list)
            else 0
        )
        explicit_count = run_result_error_physical_request_count(result)
        if explicit_count is not None:
            primary_count = max(0, explicit_count)
        elif result.done is not None:
            try:
                done_usage = done_payload(result.done)
                done_evidence: dict[str, Any] = {
                    "usage": done_usage,
                    "request_started": True,
                }
                if isinstance(result.done.ensemble_trace, Mapping):
                    done_evidence["ensemble_trace"] = (
                        result.done.ensemble_trace
                    )
                primary_count = derive_physical_request_count(
                    done_evidence,
                    default_request_count=1,
                )
            except Exception:  # noqa: BLE001 - one completed call is conservative
                primary_count = 1
        elif (
            bool(result.error or result.final_text)
            and not run_result_was_blocked_before_request(result)
        ):
            primary_count = 1
        else:
            primary_count = 0
        try:
            setup_count = usage_rows_request_count(result.setup_usage)
        except Exception:  # noqa: BLE001 - one row is at least one request
            setup_count = len(result.setup_usage)
        physical_count = max(
            represented_units,
            primary_count + setup_count,
        )
        try:
            canonical_usage = canonicalize_run_usage(
                {
                    "usage": dict(unvalidated_usage),
                    "physical_request_count": physical_count,
                    "request_started": physical_count > 0,
                },
                identity_seed=identity_seed,
                requested_provider=expected_provider,
                requested_model=expected_model,
            )
        except Exception:  # noqa: BLE001 - retry only with safe primitives
            try:
                canonical_usage = canonicalize_run_usage(
                    {
                        "usage": {},
                        "physical_request_count": physical_count,
                        "request_started": physical_count > 0,
                    },
                    identity_seed=identity_seed + ":unknown",
                    requested_provider=expected_provider,
                    requested_model=expected_model,
                )
            except Exception:  # noqa: BLE001 - canonicalizer itself is unavailable
                primitive_usage_fallback = True
                canonical_usage = primitive_unknown_usage_payload(
                    physical_count=physical_count,
                    identity_seed=identity_seed + ":primitive-unknown",
                    requested_provider=expected_provider,
                    requested_model=expected_model,
                )
        unknown_count = (
            physical_count
            if primitive_usage_fallback
            else usage_unknown_count_from_usage_payload(
                canonical_usage
            )
        )
        try:
            server_tool_use = server_tool_counts_from_usage_payload(
                canonical_usage
            )
        except Exception:  # noqa: BLE001 - tools are secondary evidence
            server_tool_use = {}
        server_tool_call_count = sum(server_tool_use.values())
        total_tool_call_count = (
            coerce_metric_int(result.tool_call_count)
            + server_tool_call_count
        )
        try:
            safe_trace_events = json_safe(result.trace_events)
        except Exception as trace_exc:  # noqa: BLE001 - primitive evidence only
            safe_trace_events = [
                {
                    "kind": "error",
                    "code": "trace_evidence_capture_failed",
                    "exception_type": type(trace_exc).__name__,
                }
            ]
        try:
            safe_routing_trace = safe_provider_build_routing_trace(
                result.routing_trace
            )
        except Exception as routing_exc:  # noqa: BLE001 - primitive evidence only
            safe_routing_trace = {
                "capture_failed": True,
                "exception_type": type(routing_exc).__name__,
            }
        summary = {
            "latency_ms": coerce_metric_int(result.latency_ms),
            "ttft_ms": result.ttft_ms,
            "tool_call_count": coerce_metric_int(result.tool_call_count),
            "stream_tool_call_count": coerce_metric_int(
                result.tool_call_count
            ),
            "server_tool_call_count": server_tool_call_count,
            "server_tool_use": server_tool_use,
            "total_tool_call_count": total_tool_call_count,
            "trajectory_steps": total_tool_call_count + physical_count,
            "llm_request_count": physical_count,
            "usage_unknown_count": unknown_count,
            "error": reason,
            "final_text_chars": len(result.final_text),
            "final_text_sha256": text_sha256(result.final_text),
            "usage": canonical_usage,
            "trace_events": safe_trace_events,
            "setup_latency_ms": coerce_metric_int(
                result.setup_latency_ms
            ),
            "routing_trace": safe_routing_trace,
        }
    summary["error"] = reason
    summary["generation_postprocessing_failure"] = {
        "stage": stage,
        "exception_type": exception_type,
        "summary_exception_type": summary_error_type,
        "source_error_present": source_error_present,
        "evidence_precision": (
            "unvalidated_raw_plus_primitive_unknown"
            if summary_error_type and primitive_usage_fallback
            else "unvalidated_raw_plus_conservative_unknown"
            if summary_error_type
            else "canonical"
        ),
    }
    if unvalidated_usage is not None:
        summary["unvalidated_usage_evidence"] = unvalidated_usage
    return summary


def recover_paid_generation_postprocessing_failure_core(
    pending: Mapping[str, Any],
    exc: Exception,
    *,
    dependencies: Mapping[str, Any],
) -> tuple[RunResult, list[dict[str, Any]], int, str]:
    """Commit one terminal attempt after a paid-call postprocessing exception."""

    run_result_type = dependencies["RunResult"]
    coerce_metric_int = dependencies["coerce_metric_int"]
    copy = dependencies["copy"]
    emergency_generation_run_summary = dependencies[
        "emergency_generation_run_summary"
    ]
    generation_postprocessing_failure_reason = dependencies[
        "generation_postprocessing_failure_reason"
    ]
    json_safe = dependencies["json_safe"]
    time = dependencies["time"]

    result = pending.get("result")
    if not isinstance(result, run_result_type):
        raise exc
    stage = str(pending.get("stage") or "unknown")
    reason = generation_postprocessing_failure_reason(stage, exc)
    attempt_id = str(pending.get("attempt_id") or "")
    attempt_index = coerce_metric_int(pending.get("attempt_index"))
    attempts_value = pending.get("attempts")
    attempts = (
        attempts_value
        if isinstance(attempts_value, list)
        else []
    )
    result.trace_events = [
        *(
            list(result.trace_events)
            if isinstance(result.trace_events, list)
            else []
        ),
        {
            "kind": "error",
            "code": "generation_postprocessing_failed",
            "stage": stage,
            "exception_type": type(exc).__name__,
        },
    ]
    run_summary = emergency_generation_run_summary(
        result,
        reason=reason,
        stage=stage,
        exception_type=type(exc).__name__,
        identity_seed=f"generation-attempt:{attempt_id or 'unknown'}",
        expected_provider=str(pending.get("expected_provider") or ""),
        expected_model=str(pending.get("expected_model") or ""),
    )
    result.error = reason
    existing = next(
        (
            attempt
            for attempt in reversed(attempts)
            if isinstance(attempt, dict)
            and str(attempt.get("attempt_id") or "") == attempt_id
        ),
        None,
    )
    if existing is None:
        existing = {
            "attempt_id": attempt_id,
            "attempt_kind": "generation",
            "attempt": attempt_index,
            "started_at": pending.get("attempt_started_at"),
            "completed_at": time.time(),
        }
        provider_native_g1_recovery = (
            pending.get("provider_native_g1_recovery") is True
        )
        if pending.get("adaptive_g1") is True or provider_native_g1_recovery:
            try:
                safe_plan = json_safe(
                    copy.deepcopy(
                        dict(pending.get("selection_plan") or {})
                    )
                )
            except Exception as plan_exc:  # noqa: BLE001 - evidence stays terminal
                safe_plan = {
                    "capture_failed": True,
                    "exception_type": type(plan_exc).__name__,
                }
            existing.update(
                {
                    "selection_plan": safe_plan,
                    "deterministic_proposer_failures": [],
                    "excluded_proposer_identities": sorted(
                        str(value)
                        for value in (
                            pending.get(
                                "excluded_proposer_identities"
                            )
                            or []
                        )
                    ),
                }
            )
            if provider_native_g1_recovery:
                existing["proposer_recovery_owner"] = "provider"
        attempts.append(existing)
    existing.update(
        {
            "completed_at": time.time(),
            "retryable": False,
            "retry_reason": reason,
            "retry_suppressed_reason": reason,
            "will_retry": False,
            "retry_backoff_s": 0.0,
            "run": run_summary,
            "generation_postprocessing_failure": {
                "stage": stage,
                "exception_type": type(exc).__name__,
            },
        }
    )
    return result, attempts, 0, reason
