"""Pure DRACO helpers for merging and deduplicating usage receipts.

The experiment and resume runners deliberately re-export these names for
backward compatibility.  Keep this module independent from either script so
both execution paths use exactly the same receipt semantics.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from opensquilla.usage_evidence import (
    MISSING_USAGE_PLACEHOLDER_ROLES,
    is_missing_usage_placeholder,
)

STABLE_RECEIPT_EVIDENCE_KEY = "stable_receipt_evidence"


def coerce_metric_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(0, int(value))
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


_coerce_metric_int = coerce_metric_int


def build_task_analyzer_usage_row(
    usage: Mapping[str, Any],
    *,
    provider_id: str,
    model_id: str,
    source: str,
    fallback_reason: str,
    coerce_metric_int: Callable[[Any], int],
    trusted_provider_billed_cost: Callable[[Mapping[str, Any]], float],
    exact_provider_usage_cost: Callable[[Mapping[str, Any]], float | None],
    new_physical_attempt_id: Callable[[], str],
) -> dict[str, Any]:
    """Build one analyzer receipt while preserving runner-owned billing policy."""

    usage_unknown = bool(usage.get("usage_unknown")) or not bool(usage)
    provider_usage = (
        dict(usage.get("provider_usage"))
        if isinstance(usage.get("provider_usage"), Mapping)
        else {}
    )
    provider_usage.update(
        {
            "task_analysis_source": source,
            "fallback_reason": fallback_reason,
            "usage_unknown": usage_unknown,
        }
    )
    physical_attempt_id = str(usage.get("physical_attempt_id") or new_physical_attempt_id())
    provider_usage["physical_attempt_id"] = physical_attempt_id
    row = {
        "role": "unknown_request" if usage_unknown else "task_analyzer",
        "label": "task_analyzer",
        "request_count": 1,
        "attempt": max(1, coerce_metric_int(usage.get("attempt"))),
        "physical_attempt_id": physical_attempt_id,
        "provider": str(usage.get("provider") or ""),
        "model": str(usage.get("model") or ""),
        "requested_provider": str(usage.get("requested_provider") or provider_id or ""),
        "requested_model": str(usage.get("requested_model") or model_id or ""),
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "reasoning_tokens": int(usage.get("reasoning_tokens") or 0),
        "cached_tokens": int(usage.get("cached_tokens") or 0),
        "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
        "billed_cost": float(usage.get("billed_cost") or 0.0),
        "cost_source": str(usage.get("cost_source") or "none"),
        "provider_usage": provider_usage,
    }
    billing_receipt = usage.get("billing_receipt", usage.get("billingReceipt"))
    if billing_receipt is not None:
        row["billing_receipt"] = billing_receipt
    row["billed_cost"] = trusted_provider_billed_cost(row)
    exact_cost = exact_provider_usage_cost(row)
    if exact_cost is not None:
        row["cost_source"] = "provider_billed"
    elif billing_receipt is not None:
        row["cost_source"] = "unavailable"
    return row


def expand_task_analyzer_usage_rows(
    usage: Mapping[str, Any],
    *,
    provider_id: str,
    model_id: str,
    source: str,
    fallback_reason: str,
    coerce_metric_int: Callable[[Any], int],
    usage_row_builder: Callable[..., dict[str, Any]],
    new_physical_attempt_id: Callable[[], str],
) -> list[dict[str, Any]]:
    """Expand analyzer retry accounting into one row per physical request."""

    raw_attempts = usage.get("physical_attempts")
    attempts = (
        [dict(item) for item in raw_attempts if isinstance(item, Mapping)]
        if isinstance(raw_attempts, list)
        else []
    )
    raw_declared_count = usage.get("attempt_count")
    if (
        not attempts
        and isinstance(raw_declared_count, int)
        and not isinstance(raw_declared_count, bool)
        and raw_declared_count == 0
    ):
        return []
    declared_count = max(
        1,
        coerce_metric_int(usage.get("attempt_count")),
        len(attempts),
    )
    if not attempts and declared_count == 1:
        single = dict(usage)
        single.pop("physical_attempts", None)
        single.pop("attempt_count", None)
        single.setdefault("attempt", 1)
        return [
            usage_row_builder(
                single,
                provider_id=provider_id,
                model_id=model_id,
                source=source,
                fallback_reason=fallback_reason,
            )
        ]

    attempts_by_ordinal: dict[int, dict[str, Any]] = {}
    for position, attempt_usage in enumerate(attempts, start=1):
        ordinal = max(
            1,
            coerce_metric_int(attempt_usage.get("attempt")) or position,
        )
        if ordinal in attempts_by_ordinal:
            continue
        attempts_by_ordinal[ordinal] = attempt_usage

    aggregate_provider_usage = (
        usage.get("provider_usage") if isinstance(usage.get("provider_usage"), Mapping) else {}
    )
    aggregate_evidence = {
        "attempt_count": declared_count,
        "provider": str(usage.get("provider") or ""),
        "model": str(usage.get("model") or ""),
        "requested_provider": str(usage.get("requested_provider") or provider_id),
        "requested_model": str(usage.get("requested_model") or model_id),
        "input_tokens": coerce_metric_int(usage.get("input_tokens")),
        "output_tokens": coerce_metric_int(usage.get("output_tokens")),
        "reasoning_tokens": coerce_metric_int(usage.get("reasoning_tokens")),
        "cached_tokens": coerce_metric_int(usage.get("cached_tokens")),
        "cache_write_tokens": coerce_metric_int(usage.get("cache_write_tokens")),
        "billed_cost": float(usage.get("billed_cost") or 0.0),
        "cost_source": str(usage.get("cost_source") or "none"),
        "response_ids": [
            str(value)
            for value in aggregate_provider_usage.get("response_ids", [])
            if str(value).strip()
        ],
    }
    rows: list[dict[str, Any]] = []
    for ordinal in range(1, declared_count + 1):
        attempt_usage = dict(attempts_by_ordinal.get(ordinal) or {})
        if not attempt_usage:
            attempt_usage = {
                "attempt": ordinal,
                "physical_attempt_id": new_physical_attempt_id(),
                "requested_provider": provider_id,
                "requested_model": model_id,
                "usage_unknown": True,
                "unknown_reason": "per_attempt_receipt_unavailable",
                "provider_usage": {
                    "usage_unknown": True,
                    "unknown_reason": "per_attempt_receipt_unavailable",
                },
            }
            if ordinal == 1:
                attempt_usage["provider_usage"]["unallocated_aggregate_usage"] = (
                    aggregate_evidence
                )
        rows.append(
            usage_row_builder(
                attempt_usage,
                provider_id=provider_id,
                model_id=model_id,
                source=source,
                fallback_reason=fallback_reason,
            )
        )
    return rows


def recover_task_analyzer_usage_rows(
    usage: Any,
    *,
    provider_id: str,
    model_id: str,
    source: str,
    fallback_reason: str,
    usage_row_builder: Callable[..., dict[str, Any]],
    new_physical_attempt_id: Callable[[], str],
) -> list[dict[str, Any]]:
    """Recover analyzer retry cardinality and IDs without trusting parsing."""

    def safe_get(value: Any, key: str, default: Any = None) -> Any:
        try:
            return value.get(key, default) if isinstance(value, Mapping) else default
        except Exception:  # noqa: BLE001 - evidence objects may be malformed
            return default

    def safe_count(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return max(0, value)
        try:
            return max(0, int(str(value).strip()))
        except Exception:  # noqa: BLE001 - use observed attempts instead
            return 0

    raw_attempts = safe_get(usage, "physical_attempts", [])
    attempts = raw_attempts if isinstance(raw_attempts, list) else []
    declared_raw = safe_get(usage, "attempt_count")
    declared_count = safe_count(declared_raw)
    if declared_raw == 0 and not isinstance(declared_raw, bool) and not attempts:
        return []
    request_count = max(1, declared_count, len(attempts))
    attempts_by_ordinal: dict[int, Any] = {}
    for position, attempt in enumerate(attempts, start=1):
        ordinal = safe_count(safe_get(attempt, "attempt")) or position
        attempts_by_ordinal.setdefault(ordinal, attempt)

    rows: list[dict[str, Any]] = []
    for ordinal in range(1, request_count + 1):
        raw_attempt = attempts_by_ordinal.get(ordinal)
        if raw_attempt is None and request_count == 1:
            raw_attempt = usage
        try:
            attempt_payload = dict(raw_attempt) if isinstance(raw_attempt, Mapping) else {}
            attempt_payload.pop("physical_attempts", None)
            attempt_payload.pop("attempt_count", None)
            attempt_payload.setdefault("attempt", ordinal)
            row = usage_row_builder(
                attempt_payload,
                provider_id=provider_id,
                model_id=model_id,
                source=source,
                fallback_reason=fallback_reason,
            )
        except Exception:  # noqa: BLE001 - build a primitive unknown row
            try:
                physical_attempt_id = str(
                    safe_get(raw_attempt, "physical_attempt_id") or ""
                ).strip()
            except Exception:  # noqa: BLE001 - generate a stable-shape ID
                physical_attempt_id = ""
            physical_attempt_id = physical_attempt_id or new_physical_attempt_id()
            row = {
                "role": "unknown_request",
                "label": "task_analyzer",
                "request_count": 1,
                "attempt": ordinal,
                "physical_attempt_id": physical_attempt_id,
                "provider": "",
                "model": "",
                "requested_provider": str(provider_id or ""),
                "requested_model": str(model_id or ""),
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "billed_cost": 0.0,
                "cost_source": "none",
                "usage_unknown": True,
                "provider_usage": {
                    "physical_attempt_id": physical_attempt_id,
                    "usage_unknown": True,
                    "task_analysis_source": source,
                    "fallback_reason": fallback_reason,
                    "recovery_source": "analyzer_postprocess_primitive_fallback",
                },
            }
        rows.append(row)
    return rows


def llm_response_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if record.get("kind") in {"llm_response", "llm_error", "llm_abandoned"}
        and isinstance(record.get("payload"), dict)
    ]


def payload_physical_request_count(payload: Mapping[str, Any]) -> int | None:
    value = payload.get("physical_request_count")
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    if payload.get("request_started") is False:
        return 0
    return None


def aggregate_agent_model_usage(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for call_index, record in enumerate(llm_response_records(records), start=1):
        payload = record["payload"]
        missing_usage_count = max(
            0,
            coerce_metric_int(payload.get("usage_missing_count")),
        )

        def append_unknown_usage(count: int) -> None:
            for missing_index in range(1, count + 1):
                rows.append(
                    {
                        "role": "agent_llm_request_unknown",
                        "agent_call_index": call_index,
                        "agent_iteration": coerce_metric_int(payload.get("iteration")),
                        "agent_call_attempt": coerce_metric_int(
                            payload.get("attempt") or payload.get("call_attempt")
                        ),
                        "agent_missing_usage_index": missing_index,
                        "model": "",
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "cached_tokens": 0,
                        "cache_write_tokens": 0,
                        "billed_cost": 0.0,
                        "cost_source": "none",
                        "request_outcome": str(record.get("kind") or "unknown"),
                    }
                )

        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            explicit_request_count = payload_physical_request_count(payload)
            append_unknown_usage(
                max(
                    (explicit_request_count if explicit_request_count is not None else 1),
                    missing_usage_count,
                )
            )
            continue
        breakdown = usage.get("model_usage_breakdown")
        if isinstance(breakdown, list) and breakdown:
            represented_missing_count = sum(
                1
                for row in breakdown
                if isinstance(row, dict)
                and str(row.get("role") or "").strip().casefold()
                in MISSING_USAGE_PLACEHOLDER_ROLES
            )
            for row in breakdown:
                if not isinstance(row, dict):
                    continue
                enriched = dict(row)
                enriched["agent_call_index"] = call_index
                enriched["agent_iteration"] = coerce_metric_int(payload.get("iteration"))
                enriched["agent_call_attempt"] = coerce_metric_int(
                    payload.get("attempt") or payload.get("call_attempt")
                )
                rows.append(enriched)
            append_unknown_usage(max(0, missing_usage_count - represented_missing_count))
            continue
        synthesized = {
            "role": "agent_llm_call",
            "agent_call_index": call_index,
            "agent_iteration": coerce_metric_int(payload.get("iteration")),
            "agent_call_attempt": coerce_metric_int(
                payload.get("attempt") or payload.get("call_attempt")
            ),
            "provider": str(usage.get("provider") or ""),
            "model": str(usage.get("model") or ""),
            "requested_provider": str(usage.get("requested_provider") or ""),
            "requested_model": str(usage.get("requested_model") or ""),
            "input_tokens": coerce_metric_int(usage.get("input_tokens")),
            "output_tokens": coerce_metric_int(usage.get("output_tokens")),
            "reasoning_tokens": coerce_metric_int(usage.get("reasoning_tokens")),
            "cached_tokens": coerce_metric_int(usage.get("cached_tokens")),
            "cache_write_tokens": coerce_metric_int(usage.get("cache_write_tokens")),
            "billed_cost": float(usage.get("billed_cost") or 0.0),
            "cost_source": str(usage.get("cost_source") or "none"),
            "provider_usage": (
                dict(usage.get("provider_usage"))
                if isinstance(usage.get("provider_usage"), Mapping)
                else {}
            ),
        }
        billing_receipt = usage.get("billing_receipt", usage.get("billingReceipt"))
        if billing_receipt is not None:
            synthesized["billing_receipt"] = billing_receipt
        rows.append(synthesized)
        append_unknown_usage(missing_usage_count)
    return rows


def aggregate_agent_ensemble_trace(records: list[dict[str, Any]]) -> dict[str, Any]:
    call_records = llm_response_records(records)
    traces: list[dict[str, Any]] = []
    first_real_trace: dict[str, Any] | None = None
    last_real_trace: dict[str, Any] | None = None
    total_llm_requests = 0
    untraced_llm_requests = 0
    for call_index, record in enumerate(call_records, start=1):
        payload = record["payload"]
        trace = payload.get("ensemble_trace") if isinstance(payload, dict) else None
        if record.get("kind") == "llm_abandoned":
            trace_requests = (
                coerce_metric_int(trace.get("llm_request_count"))
                if isinstance(trace, dict)
                else 0
            )
            request_count = max(
                1,
                trace_requests,
                coerce_metric_int(payload.get("usage_missing_count")),
            )
            untraced_llm_requests += request_count
            traces.append(
                {
                    "request_outcome": "llm_abandoned",
                    "agent_call_index": call_index,
                    "agent_call_id": str(payload.get("call_id") or ""),
                    "agent_iteration": coerce_metric_int(payload.get("iteration")),
                    "agent_call_attempt": coerce_metric_int(payload.get("attempt")),
                    "agent_call_duration_ms": coerce_metric_int(payload.get("duration_ms")),
                    "trace_missing": True,
                    "llm_request_count": request_count,
                    "physical_request_count": request_count,
                    "usage_missing_count": max(
                        request_count,
                        coerce_metric_int(payload.get("usage_missing_count")),
                    ),
                }
            )
            continue
        if not isinstance(trace, dict) or not trace:
            usage = payload.get("usage") if isinstance(payload, dict) else None
            breakdown = usage.get("model_usage_breakdown") if isinstance(usage, dict) else None
            represented_missing = (
                sum(
                    1
                    for item in breakdown
                    if isinstance(item, dict)
                    and str(item.get("role") or "").strip().casefold()
                    in MISSING_USAGE_PLACEHOLDER_ROLES
                )
                if isinstance(breakdown, list)
                else 0
            )
            observed = (
                sum(1 for item in breakdown if isinstance(item, dict))
                if isinstance(breakdown, list) and breakdown
                else (1 if isinstance(usage, dict) and usage else 0)
            )
            missing = coerce_metric_int(
                payload.get("usage_missing_count") if isinstance(payload, dict) else 0
            )
            inferred_count = observed + max(0, missing - represented_missing)
            explicit_request_count = payload_physical_request_count(payload)
            if explicit_request_count is not None:
                request_count = max(explicit_request_count, inferred_count)
            else:
                # Older logs have no explicit start evidence. A response/error
                # record conservatively represents at least one request.
                request_count = max(1, inferred_count)
            receipt_count = max(0, observed - represented_missing)
            effective_missing = max(missing, request_count - receipt_count)
            untraced_llm_requests += request_count
            traces.append(
                {
                    "request_outcome": str(record.get("kind") or "unknown"),
                    "agent_call_index": call_index,
                    "agent_call_id": str(payload.get("call_id") or ""),
                    "agent_iteration": coerce_metric_int(payload.get("iteration")),
                    "agent_call_attempt": coerce_metric_int(payload.get("attempt")),
                    "agent_call_duration_ms": coerce_metric_int(payload.get("duration_ms")),
                    "trace_missing": True,
                    "llm_request_count": request_count,
                    "physical_request_count": request_count,
                    "usage_missing_count": effective_missing,
                }
            )
            continue
        enriched = dict(trace)
        if first_real_trace is None:
            first_real_trace = enriched
        last_real_trace = enriched
        enriched["request_outcome"] = str(record.get("kind") or "unknown")
        enriched["agent_call_index"] = call_index
        if payload.get("call_id") is not None:
            enriched["agent_call_id"] = str(payload.get("call_id"))
        enriched["agent_iteration"] = coerce_metric_int(payload.get("iteration"))
        enriched["agent_call_attempt"] = coerce_metric_int(payload.get("attempt"))
        enriched["agent_call_duration_ms"] = coerce_metric_int(payload.get("duration_ms"))
        traces.append(enriched)
        total_llm_requests += max(
            0,
            coerce_metric_int(
                trace.get("physical_request_count")
                if trace.get("physical_request_count") is not None
                else trace.get("llm_request_count")
            ),
        )
    if not call_records:
        return {}
    first_trace = first_real_trace or {}
    terminal_trace = last_real_trace or {}
    payload: dict[str, Any] = {
        "mode": "agent_loop",
        "agent_llm_call_count": len(call_records),
        "untraced_agent_llm_call_count": sum(
            1
            for record in call_records
            if record.get("kind") == "llm_abandoned"
            or not isinstance(record.get("payload", {}).get("ensemble_trace"), dict)
            or not record.get("payload", {}).get("ensemble_trace")
        ),
        "llm_request_count": total_llm_requests + untraced_llm_requests,
        "physical_request_count": total_llm_requests + untraced_llm_requests,
        "usage_missing_count": sum(
            max(0, coerce_metric_int(call.get("usage_missing_count"))) for call in traces
        ),
        "calls": traces,
    }
    for key in (
        "profile",
        "shuffle_candidates",
        "moa_layers",
        "moa_refine_count",
        "moa_intermediate_layers",
        "output_strategy",
    ):
        if key in first_trace:
            payload[key] = first_trace[key]
    for key in (
        "configured_candidate_order_seed",
        "candidate_order_seed",
        "candidate_order_seed_source",
    ):
        if key in terminal_trace:
            payload[key] = terminal_trace[key]
    for key in (
        "successful_proposers",
        "total_candidates",
        "fallback_used",
        "final_request_role",
    ):
        if key in terminal_trace:
            payload[key] = terminal_trace[key]
    intermediate_fallback_indexes = [
        coerce_metric_int(call.get("agent_call_index"))
        for call in traces[:-1]
        if call.get("fallback_used") is True
    ]
    payload["terminal_call_index"] = coerce_metric_int(terminal_trace.get("agent_call_index"))
    payload["any_intermediate_fallback"] = bool(intermediate_fallback_indexes)
    payload["intermediate_fallback_call_indexes"] = intermediate_fallback_indexes
    if terminal_trace.get("output_strategy") == "select_best_candidate":
        for key in ("selected_candidate_count", "selected_candidate_indexes"):
            if key in terminal_trace:
                payload[key] = terminal_trace[key]
    return payload


def usage_row_is_missing_placeholder(row: Mapping[str, Any]) -> bool:
    return is_missing_usage_placeholder(row)


def usage_row_response_ids(row: Mapping[str, Any]) -> frozenset[str]:
    values: list[Any] = []
    direct = row.get("response_id")
    if direct is not None:
        values.append(direct)
    provider_usage = row.get("provider_usage")
    if isinstance(provider_usage, Mapping):
        response_ids = provider_usage.get("response_ids")
        if isinstance(response_ids, (list, tuple, set, frozenset)):
            values.extend(response_ids)
        elif response_ids is not None:
            values.append(response_ids)
        response_id = provider_usage.get("response_id")
        if response_id is not None:
            values.append(response_id)
    return frozenset(str(value).strip() for value in values if str(value).strip())


def usage_receipt_fingerprint(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("provider") or "").strip(),
        str(row.get("model") or "").strip(),
        _coerce_metric_int(row.get("input_tokens")),
        _coerce_metric_int(row.get("output_tokens")),
        _coerce_metric_int(row.get("reasoning_tokens")),
        _coerce_metric_int(row.get("cached_tokens")),
        _coerce_metric_int(row.get("cache_write_tokens")),
        float(row.get("billed_cost") or 0.0),
        str(row.get("cost_source") or "none").strip().casefold(),
    )


def usage_row_match_priority(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> int | None:
    if usage_row_is_missing_placeholder(left) != usage_row_is_missing_placeholder(right):
        return None
    left_ids = usage_row_response_ids(left)
    right_ids = usage_row_response_ids(right)
    if left_ids and right_ids:
        return 0 if left_ids & right_ids else None
    if usage_receipt_fingerprint(left) != usage_receipt_fingerprint(right):
        return None
    return 1 if not left_ids and not right_ids else 2


def build_stable_receipt_evidence(
    *rows: Mapping[str, Any],
) -> dict[str, Any]:
    providers: set[str] = set()
    models: set[str] = set()
    cost_usd_nanos: set[int] = set()
    usage_is_byok_values: set[bool] = set()
    router_is_byok_values: set[bool] = set()
    token_values: dict[str, set[int]] = {
        key: set()
        for key in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cached_tokens",
            "cache_write_tokens",
        )
    }
    inherited_conflicts: set[str] = set()

    def _add_bool(value: Any, target: set[bool]) -> None:
        if value is True or value is False:
            target.add(value)

    def _add_cost(value: Any) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            return
        cost_usd_nanos.add(int(round(float(value) * 1_000_000_000)))

    for row in rows:
        provider = str(row.get("provider") or "").strip().casefold()
        if provider:
            providers.add(provider)
        model = str(row.get("model") or "").strip()
        if model:
            models.add(model)
        _add_cost(row.get("billed_cost"))
        for key, values in token_values.items():
            raw_value = row.get(key)
            if isinstance(raw_value, int) and not isinstance(raw_value, bool) and raw_value >= 0:
                values.add(raw_value)
        billing_receipt = row.get("billing_receipt")
        if isinstance(billing_receipt, Mapping):
            receipt_nanos = billing_receipt.get("usd_equivalent_nanos")
            if (
                isinstance(receipt_nanos, int)
                and not isinstance(receipt_nanos, bool)
                and receipt_nanos >= 0
            ):
                cost_usd_nanos.add(receipt_nanos)

        provider_usage = row.get("provider_usage")
        if not isinstance(provider_usage, Mapping):
            continue
        _add_bool(provider_usage.get("is_byok"), usage_is_byok_values)
        _add_cost(provider_usage.get("provider_reported_cost"))
        router_metadata = provider_usage.get("router_metadata")
        if isinstance(router_metadata, Mapping):
            _add_bool(router_metadata.get("is_byok"), router_is_byok_values)
        inherited = provider_usage.get(STABLE_RECEIPT_EVIDENCE_KEY)
        if not isinstance(inherited, Mapping):
            continue
        providers.update(
            str(value).strip().casefold()
            for value in inherited.get("providers") or []
            if str(value).strip()
        )
        models.update(
            str(value).strip() for value in inherited.get("models") or [] if str(value).strip()
        )
        for value in inherited.get("cost_usd_nanos") or []:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                cost_usd_nanos.add(value)
        for value in inherited.get("usage_is_byok_values") or []:
            _add_bool(value, usage_is_byok_values)
        for value in inherited.get("router_is_byok_values") or []:
            _add_bool(value, router_is_byok_values)
        inherited_token_values = inherited.get("token_values")
        if isinstance(inherited_token_values, Mapping):
            for key, values in token_values.items():
                for value in inherited_token_values.get(key) or []:
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        values.add(value)
        inherited_conflicts.update(
            str(value).strip()
            for value in inherited.get("conflict_fields") or []
            if str(value).strip()
        )

    conflict_fields = set(inherited_conflicts)
    if len(providers) > 1:
        conflict_fields.add("provider")
    if len(models) > 1:
        conflict_fields.add("model")
    if len(cost_usd_nanos) > 1:
        conflict_fields.add("cost_usd_nanos")
    if len(usage_is_byok_values | router_is_byok_values) > 1:
        conflict_fields.add("is_byok")
    for key, values in token_values.items():
        if len(values) > 1:
            conflict_fields.add(key)

    return {
        "providers": sorted(providers),
        "models": sorted(models),
        "cost_usd_nanos": sorted(cost_usd_nanos),
        "usage_is_byok_values": sorted(usage_is_byok_values),
        "router_is_byok_values": sorted(router_is_byok_values),
        "token_values": {key: sorted(values) for key, values in token_values.items() if values},
        "conflict_fields": sorted(conflict_fields),
        "receipt_conflict": bool(conflict_fields),
    }


def merge_usage_receipt_provenance(
    target: dict[str, Any],
    source: Mapping[str, Any],
) -> None:
    target_ids = usage_row_response_ids(target)
    source_ids = usage_row_response_ids(source)
    stable_id_match = bool(target_ids and source_ids and target_ids & source_ids)
    stable_receipt_evidence = (
        build_stable_receipt_evidence(target, source) if stable_id_match else None
    )
    if stable_id_match:
        for key in (
            "provider",
            "model",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "billed_cost",
            "cost_source",
            "billing_receipt",
        ):
            if key in source:
                target[key] = source[key]
    for key in (
        "provider",
        "model",
        "requested_provider",
        "requested_model",
        "response_id",
        "billing_receipt",
    ):
        if not target.get(key) and source.get(key):
            target[key] = source[key]
    source_usage = source.get("provider_usage")
    source_usage = source_usage if isinstance(source_usage, Mapping) else {}
    target_usage = (
        dict(target.get("provider_usage"))
        if isinstance(target.get("provider_usage"), Mapping)
        else {}
    )
    for key, value in source_usage.items():
        if key == STABLE_RECEIPT_EVIDENCE_KEY:
            continue
        if key == "response_ids":
            existing = target_usage.get(key)
            existing_values = (
                list(existing)
                if isinstance(existing, (list, tuple, set, frozenset))
                else [existing]
                if existing is not None
                else []
            )
            source_values = (
                list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]
            )
            target_usage[key] = sorted(
                {
                    str(item).strip()
                    for item in [*existing_values, *source_values]
                    if str(item).strip()
                }
            )
        elif stable_id_match or not target_usage.get(key):
            target_usage[key] = value
    if stable_receipt_evidence is not None:
        target_usage[STABLE_RECEIPT_EVIDENCE_KEY] = stable_receipt_evidence
    if target_usage:
        target["provider_usage"] = target_usage


def deduplicate_stable_usage_receipts(
    units: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse repeated physical receipts only when a stable response ID proves it."""

    deduplicated: list[dict[str, Any]] = []
    for unit in units:
        row = dict(unit)
        response_ids = usage_row_response_ids(row)
        if not response_ids:
            # Similar token/cost values are not proof of the same physical call.
            deduplicated.append(row)
            continue
        matching_indexes = [
            index
            for index, existing in enumerate(deduplicated)
            if response_ids & usage_row_response_ids(existing)
        ]
        if not matching_indexes:
            deduplicated.append(row)
            continue
        target = deduplicated[matching_indexes[0]]
        merge_usage_receipt_provenance(target, row)
        # A later receipt can bridge two previously separate response-id sets.
        for index in reversed(matching_indexes[1:]):
            merge_usage_receipt_provenance(target, deduplicated.pop(index))
    return deduplicated


__all__ = [
    "STABLE_RECEIPT_EVIDENCE_KEY",
    "build_stable_receipt_evidence",
    "deduplicate_stable_usage_receipts",
    "merge_usage_receipt_provenance",
    "usage_receipt_fingerprint",
    "usage_row_is_missing_placeholder",
    "usage_row_match_priority",
    "usage_row_response_ids",
]
