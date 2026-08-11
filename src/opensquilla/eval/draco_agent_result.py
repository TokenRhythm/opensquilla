"""Shared DRACO adapters from agent terminal events to provider receipts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from opensquilla.engine.types import DoneEvent as AgentDoneEvent
from opensquilla.eval.draco_runtime_contract import canonical_json_sha256
from opensquilla.eval.draco_usage_evidence import (
    aggregate_agent_ensemble_trace,
    aggregate_agent_model_usage,
    build_stable_receipt_evidence,
    coerce_metric_int,
    llm_response_records,
    merge_usage_receipt_provenance,
    trusted_provider_billed_cost,
    usage_row_is_missing_placeholder,
    usage_row_match_priority,
    usage_row_response_ids,
)
from opensquilla.provider.types import DoneEvent, ErrorEvent
from opensquilla.usage_evidence import MISSING_USAGE_PLACEHOLDER_ROLES

IGNORED_AGENT_DONE_POLICY_EVIDENCE_KEY = "ignored_agent_done_summary_policy_evidence"


def provider_done_from_agent_done_core(
    done: AgentDoneEvent | None,
    *,
    recorder: Any,
    fallback_model: str,
    ignored_agent_done_summary_policy_evidence_fn: Callable[..., Any],
) -> DoneEvent | None:
    ignored_agent_done_summary_policy_evidence = ignored_agent_done_summary_policy_evidence_fn

    call_records = llm_response_records(recorder.records)
    breakdown = aggregate_agent_model_usage(recorder.records)
    trace = aggregate_agent_ensemble_trace(recorder.records)
    ignored_agent_done_summary_rows = 0
    ignored_agent_done_policy_evidence: list[dict[str, Any]] = []
    if done is not None:
        done_rows = [
            dict(row)
            for row in getattr(done, "model_usage_breakdown", [])
            if isinstance(row, Mapping)
        ]
        if (
            not done_rows
            and not call_records
            and not breakdown
            and (
                getattr(done, "billing_receipt", None) is not None
                or getattr(done, "provider_usage", None)
                or done.input_tokens
                or done.output_tokens
                or done.reasoning_tokens
                or done.cached_tokens
                or done.cache_write_tokens
                or done.billed_cost
            )
        ):
            done_rows = [
                {
                    "role": "agent_done",
                    "provider": str(getattr(done, "provider", "") or ""),
                    "model": str(getattr(done, "model", "") or ""),
                    "requested_provider": str(getattr(done, "requested_provider", "") or ""),
                    "requested_model": str(getattr(done, "requested_model", "") or fallback_model),
                    "input_tokens": done.input_tokens,
                    "output_tokens": done.output_tokens,
                    "reasoning_tokens": done.reasoning_tokens,
                    "cached_tokens": done.cached_tokens,
                    "cache_write_tokens": done.cache_write_tokens,
                    "billed_cost": done.billed_cost,
                    "cost_source": done.cost_source,
                    "provider_usage": dict(
                        getattr(done, "provider_usage", {})
                        if isinstance(getattr(done, "provider_usage", {}), Mapping)
                        else {}
                    ),
                    **(
                        {"billing_receipt": getattr(done, "billing_receipt")}
                        if getattr(done, "billing_receipt", None) is not None
                        else {}
                    ),
                }
            ]
        for done_row in done_rows:
            if not call_records:
                breakdown.append(done_row)
                continue
            request_count = max(
                0,
                coerce_metric_int(done_row.get("request_count")),
            )
            response_ids = usage_row_response_ids(done_row)
            if request_count > 1 or len(response_ids) > 1:
                ignored_agent_done_summary_rows += 1
                policy_evidence = ignored_agent_done_summary_policy_evidence(
                    done_row,
                    physical_rows=breakdown,
                )
                if policy_evidence is not None:
                    ignored_agent_done_policy_evidence.append(policy_evidence)
                continue
            if response_ids:
                candidates = [
                    (0, index)
                    for index, row in enumerate(breakdown)
                    if response_ids & usage_row_response_ids(row)
                ]
            else:
                candidates = [
                    (priority, index)
                    for index, row in enumerate(breakdown)
                    if not usage_row_response_ids(row)
                    if (priority := usage_row_match_priority(row, done_row)) is not None
                ]
            if candidates:
                merge_usage_receipt_provenance(
                    breakdown[min(candidates)[1]],
                    done_row,
                )
            elif len(response_ids) == 1:
                breakdown.append(done_row)
            else:
                ignored_agent_done_summary_rows += 1
        done_trace = dict(done.ensemble_trace) if isinstance(done.ensemble_trace, Mapping) else {}
        if done_trace:
            if not trace:
                trace = done_trace
            else:
                for key, value in done_trace.items():
                    trace.setdefault(key, value)
                for key in (
                    "llm_request_count",
                    "physical_request_count",
                    "usage_missing_count",
                ):
                    trace[key] = max(
                        coerce_metric_int(trace.get(key)),
                        coerce_metric_int(done_trace.get(key)),
                    )
    observed_rows = [
        row
        for row in breakdown
        if str(row.get("role") or "").strip().casefold() not in MISSING_USAGE_PLACEHOLDER_ROLES
    ]
    observed_providers = {
        str(row.get("provider") or "").strip()
        for row in observed_rows
        if str(row.get("provider") or "").strip()
    }
    observed_models = {
        str(row.get("model") or "").strip()
        for row in observed_rows
        if str(row.get("model") or "").strip()
    }
    observed_requested_providers = {
        str(row.get("requested_provider") or "").strip()
        for row in observed_rows
        if str(row.get("requested_provider") or "").strip()
    }
    usage_missing_count = max(
        sum(
            1
            for row in breakdown
            if str(row.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
        ),
        coerce_metric_int(trace.get("usage_missing_count")) if trace else 0,
        coerce_metric_int(getattr(done, "usage_missing_count", 0) if done is not None else 0),
    )
    if done is not None and (breakdown or usage_missing_count):
        represented_missing = sum(1 for row in breakdown if usage_row_is_missing_placeholder(row))
        physical_request_count = len(breakdown) + max(
            0,
            usage_missing_count - represented_missing,
        )
        trace = dict(trace or {})
        trace["llm_request_count"] = max(
            coerce_metric_int(trace.get("llm_request_count")),
            physical_request_count,
        )
        trace["physical_request_count"] = max(
            coerce_metric_int(trace.get("physical_request_count")),
            physical_request_count,
        )
        trace["usage_missing_count"] = usage_missing_count
    if done is None:
        if not breakdown:
            return None
        sources = {str(row.get("cost_source") or "none").strip().casefold() for row in breakdown}
        if sources == {"provider_billed"}:
            envelope_source = "provider_billed"
        elif len(sources) == 1:
            envelope_source = next(iter(sources))
        else:
            envelope_source = "mixed"
        return DoneEvent(
            stop_reason="error",
            input_tokens=sum(coerce_metric_int(row.get("input_tokens")) for row in breakdown),
            output_tokens=sum(coerce_metric_int(row.get("output_tokens")) for row in breakdown),
            reasoning_tokens=sum(
                coerce_metric_int(row.get("reasoning_tokens")) for row in breakdown
            ),
            cached_tokens=sum(coerce_metric_int(row.get("cached_tokens")) for row in breakdown),
            cache_write_tokens=sum(
                coerce_metric_int(row.get("cache_write_tokens")) for row in breakdown
            ),
            billed_cost=sum(trusted_provider_billed_cost(row) for row in breakdown),
            model=(next(iter(observed_models)) if len(observed_models) == 1 else ""),
            provider=(next(iter(observed_providers)) if len(observed_providers) == 1 else ""),
            cost_source=envelope_source,
            requested_model=fallback_model,
            requested_provider=(
                next(iter(observed_requested_providers))
                if len(observed_requested_providers) == 1
                else ""
            ),
            model_usage_breakdown=breakdown,
            ensemble_trace=trace,
            usage_missing_count=usage_missing_count,
            provider_usage={
                "diagnostic_usage_only": True,
                "agent_llm_call_count": len(call_records),
                "requested_model": fallback_model,
                "requested_provider": (
                    next(iter(observed_requested_providers))
                    if len(observed_requested_providers) == 1
                    else ""
                ),
            },
        )
    if trace:
        trace["agent_iterations"] = done.iterations
    provider_usage: dict[str, Any] = dict(
        getattr(done, "provider_usage", {})
        if isinstance(getattr(done, "provider_usage", {}), Mapping)
        else {}
    )
    provider_usage.update(
        {
            "agent_iterations": done.iterations,
            "agent_llm_call_count": len(call_records),
            "agent_done_summary_rows_ignored": ignored_agent_done_summary_rows,
            "provider_identity_source": (
                "unique_model_usage_breakdown" if len(observed_providers) == 1 else "unresolved"
            ),
            "requested_model": str(getattr(done, "requested_model", "") or fallback_model),
            "requested_provider": str(getattr(done, "requested_provider", "") or ""),
        }
    )
    if ignored_agent_done_policy_evidence:
        provider_usage[IGNORED_AGENT_DONE_POLICY_EVIDENCE_KEY] = ignored_agent_done_policy_evidence
    done_provider = str(getattr(done, "provider", "") or "").strip()
    done_model = str(getattr(done, "model", "") or "").strip()
    provider = done_provider or (
        next(iter(observed_providers)) if len(observed_providers) == 1 else ""
    )
    model = done_model or (next(iter(observed_models)) if len(observed_models) == 1 else "")
    requested_provider = str(
        getattr(done, "requested_provider", "")
        or (
            next(iter(observed_requested_providers))
            if len(observed_requested_providers) == 1
            else ""
        )
        or ""
    )
    provider_usage["requested_provider"] = requested_provider
    provider_done = DoneEvent(
        stop_reason="stop",
        input_tokens=done.input_tokens,
        output_tokens=done.output_tokens,
        reasoning_content=done.reasoning_content,
        reasoning_tokens=done.reasoning_tokens,
        cached_tokens=done.cached_tokens,
        billed_cost=done.billed_cost,
        model=model,
        provider=provider,
        requested_model=str(getattr(done, "requested_model", "") or fallback_model),
        requested_provider=requested_provider,
        cache_write_tokens=done.cache_write_tokens,
        cost_source=done.cost_source,
        model_usage_breakdown=breakdown,
        ensemble_trace=trace,
        usage_missing_count=usage_missing_count,
        billing_receipt=getattr(done, "billing_receipt", None),
        provider_usage=provider_usage,
    )
    return provider_done


def ignored_agent_done_summary_policy_evidence_core(
    summary: Mapping[str, Any],
    *,
    physical_rows: list[Mapping[str, Any]],
    models_equivalent: Callable[[Any, Any], bool],
) -> dict[str, Any] | None:
    """Retain policy contradictions from a non-physical AgentDone roll-up.

    A roll-up with ``request_count > 1`` is never a physical request and must
    not contribute tokens, cost, or request cardinality.  It can still carry
    security-relevant receipt evidence, however.  Preserve only a compact
    contradiction record so a later non-BYOK audit cannot lose an explicit
    BYOK assertion or a stable-receipt identity conflict.
    """

    _formal_openrouter_models_equivalent = models_equivalent

    stable = build_stable_receipt_evidence(summary)
    conflict_fields = {
        str(value).strip() for value in stable.get("conflict_fields") or [] if str(value).strip()
    }
    summary_provider = str(summary.get("provider") or "").strip().casefold()
    summary_model = str(summary.get("model") or "").strip()
    physical_providers = {
        str(row.get("provider") or "").strip().casefold()
        for row in physical_rows
        if str(row.get("provider") or "").strip()
    }
    physical_models = {
        str(row.get("model") or "").strip()
        for row in physical_rows
        if str(row.get("model") or "").strip()
    }
    if summary_provider and physical_providers and summary_provider not in physical_providers:
        conflict_fields.add("provider")
    if (
        summary_model
        and physical_models
        and not any(
            _formal_openrouter_models_equivalent(summary_model, physical_model)
            for physical_model in physical_models
        )
    ):
        conflict_fields.add("model")

    summary_ids = usage_row_response_ids(summary)
    matching_rows = [row for row in physical_rows if summary_ids & usage_row_response_ids(row)]
    if matching_rows:
        overlap_evidence = build_stable_receipt_evidence(summary, *matching_rows)
        overlap_conflicts = {
            str(value).strip()
            for value in overlap_evidence.get("conflict_fields") or []
            if str(value).strip()
        }
        # Cost/token totals on a multi-response summary are aggregates rather
        # than per-request receipt fields.  They must not poison accounting.
        if len(summary_ids) > 1:
            overlap_conflicts &= {"provider", "model", "is_byok"}
        conflict_fields.update(overlap_conflicts)

    byok_values = {
        value
        for key in ("usage_is_byok_values", "router_is_byok_values")
        for value in stable.get(key) or []
        if value is True or value is False
    }
    explicit_byok = True in byok_values
    if not explicit_byok and not conflict_fields:
        return None
    classification = "conflict" if conflict_fields else "explicit_byok"
    response_id_fingerprint = canonical_json_sha256(sorted(summary_ids)) if summary_ids else ""
    return {
        "source": "ignored_agent_done_summary",
        "classification": classification,
        "request_count": max(
            0,
            coerce_metric_int(summary.get("request_count")),
        ),
        "response_id_set_sha256": response_id_fingerprint,
        "explicit_byok": explicit_byok,
        "conflict_fields": sorted(conflict_fields),
    }


def diagnostic_done_from_error_event(event: ErrorEvent) -> DoneEvent | None:
    """Preserve receipts from a failed composite request for spend accounting."""

    nested_done = event.diagnostic_done if isinstance(event.diagnostic_done, DoneEvent) else None
    rows = [dict(row) for row in event.model_usage_breakdown if isinstance(row, Mapping)]
    trace = dict(event.ensemble_trace) if isinstance(event.ensemble_trace, dict) else None
    missing_count = max(
        coerce_metric_int(event.usage_missing_count),
        coerce_metric_int(trace.get("usage_missing_count")) if trace else 0,
    )
    explicit_count = (
        max(0, event.physical_request_count)
        if isinstance(event.physical_request_count, int)
        and not isinstance(event.physical_request_count, bool)
        else None
    )
    explicit_zero = explicit_count == 0 or event.request_started is False
    if (
        nested_done is not None
        and not rows
        and trace is None
        and explicit_count in {None, 1}
        and missing_count <= 0
    ):
        return nested_done
    if nested_done is not None:
        nested_rows = [
            dict(row) for row in nested_done.model_usage_breakdown if isinstance(row, Mapping)
        ]
        if not nested_rows:
            nested_rows = [
                {
                    "role": "diagnostic_request",
                    "provider": str(nested_done.provider or ""),
                    "model": str(nested_done.model or ""),
                    "requested_provider": str(nested_done.requested_provider or ""),
                    "requested_model": str(nested_done.requested_model or ""),
                    "input_tokens": nested_done.input_tokens,
                    "output_tokens": nested_done.output_tokens,
                    "reasoning_tokens": nested_done.reasoning_tokens,
                    "cached_tokens": nested_done.cached_tokens,
                    "cache_write_tokens": nested_done.cache_write_tokens,
                    "billed_cost": nested_done.billed_cost,
                    "cost_source": nested_done.cost_source,
                    "provider_usage": dict(nested_done.provider_usage),
                    **(
                        {"billing_receipt": nested_done.billing_receipt}
                        if nested_done.billing_receipt is not None
                        else {}
                    ),
                }
            ]
        nested_trace = (
            nested_done.ensemble_trace if isinstance(nested_done.ensemble_trace, dict) else {}
        )
        nested_placeholder_count = sum(
            1 for row in nested_rows if usage_row_is_missing_placeholder(row)
        )
        nested_real_receipt_count = len(nested_rows) - nested_placeholder_count
        nested_physical_count = max(
            coerce_metric_int(nested_trace.get("physical_request_count")),
            coerce_metric_int(nested_trace.get("llm_request_count")),
            nested_real_receipt_count
            + max(
                nested_placeholder_count,
                coerce_metric_int(nested_done.usage_missing_count),
                coerce_metric_int(nested_trace.get("usage_missing_count")),
            ),
        )
        missing_count = max(
            missing_count,
            nested_placeholder_count,
            coerce_metric_int(nested_done.usage_missing_count),
            coerce_metric_int(nested_trace.get("usage_missing_count")),
            nested_physical_count - nested_real_receipt_count,
        )

        outer_row_count = len(rows)
        consumed_rows: set[int] = set()
        matched_outer_by_nested: dict[int, int] = {}
        nested_match_order = sorted(
            range(len(nested_rows)),
            key=lambda index: (
                0 if usage_row_response_ids(nested_rows[index]) else 1,
                index,
            ),
        )
        for nested_index in nested_match_order:
            nested_row = nested_rows[nested_index]
            candidates = [
                (priority, index)
                for index, row in enumerate(rows[:outer_row_count])
                if index not in consumed_rows
                if (priority := usage_row_match_priority(row, nested_row)) is not None
            ]
            matched_index = min(candidates)[1] if candidates else None
            if matched_index is not None:
                consumed_rows.add(matched_index)
                matched_outer_by_nested[nested_index] = matched_index
        for nested_index, nested_row in enumerate(nested_rows):
            matched_index = matched_outer_by_nested.get(nested_index)
            if matched_index is None:
                rows.append(nested_row)
            else:
                merge_usage_receipt_provenance(rows[matched_index], nested_row)

    placeholder_count = sum(
        1
        for row in rows
        if str(row.get("role") or "").strip().casefold() in MISSING_USAGE_PLACEHOLDER_ROLES
    )
    real_receipt_count = max(0, len(rows) - placeholder_count)
    trace_count = max(
        coerce_metric_int(trace.get("physical_request_count")) if trace else 0,
        coerce_metric_int(trace.get("llm_request_count")) if trace else 0,
    )
    if explicit_zero and real_receipt_count == 0:
        physical_count = 0
        missing_count = 0
        rows = []
    else:
        physical_count = max(
            trace_count,
            explicit_count or 0,
            real_receipt_count + max(missing_count, placeholder_count),
        )
        missing_count = max(missing_count, physical_count - real_receipt_count)
    if not rows and physical_count <= 0 and missing_count <= 0:
        return None
    if physical_count > 0:
        trace = dict(trace or {})
        trace.setdefault("mode", "diagnostic_error")
        trace["llm_request_count"] = physical_count
        trace["physical_request_count"] = physical_count
        trace["usage_missing_count"] = missing_count
    models = {
        str(row.get("model") or "").strip()
        for row in rows
        if not usage_row_is_missing_placeholder(row)
        if str(row.get("model") or "").strip()
    }
    providers = {
        str(row.get("provider") or "").strip()
        for row in rows
        if not usage_row_is_missing_placeholder(row)
        if str(row.get("provider") or "").strip()
    }
    requested_models = {
        str(row.get("requested_model") or "").strip()
        for row in rows
        if str(row.get("requested_model") or "").strip()
    }
    requested_providers = {
        str(row.get("requested_provider") or "").strip()
        for row in rows
        if str(row.get("requested_provider") or "").strip()
    }
    sources = {str(row.get("cost_source") or "none").strip().casefold() for row in rows}
    cost_source = next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "none"
    return DoneEvent(
        stop_reason="error",
        input_tokens=sum(coerce_metric_int(row.get("input_tokens")) for row in rows),
        output_tokens=sum(coerce_metric_int(row.get("output_tokens")) for row in rows),
        reasoning_tokens=sum(coerce_metric_int(row.get("reasoning_tokens")) for row in rows),
        cached_tokens=sum(coerce_metric_int(row.get("cached_tokens")) for row in rows),
        cache_write_tokens=sum(coerce_metric_int(row.get("cache_write_tokens")) for row in rows),
        billed_cost=sum(trusted_provider_billed_cost(row) for row in rows),
        model=next(iter(models)) if len(models) == 1 else "",
        provider=next(iter(providers)) if len(providers) == 1 else "",
        requested_model=(next(iter(requested_models)) if len(requested_models) == 1 else ""),
        requested_provider=(
            next(iter(requested_providers)) if len(requested_providers) == 1 else ""
        ),
        cost_source=cost_source,
        model_usage_breakdown=rows,
        ensemble_trace=trace,
        usage_missing_count=missing_count,
        billing_receipt=(rows[0].get("billing_receipt") if len(rows) == 1 else None),
        provider_usage={
            "diagnostic_usage_only": True,
            "terminal_error_code": str(event.code or ""),
        },
    )
