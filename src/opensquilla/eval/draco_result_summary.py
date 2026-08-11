"""Memory-bounded DRACO result summary projections.

Complete sealed result rows remain the durable audit record. Terminal reports
need only derived scalars plus compact receipt evidence, so run and resume can
release large response, trace, and tool payloads after each committed append.
Each projected receipt and stable ID has a fixed payload bound; aggregate
memory is O(physical receipts + stable IDs), independent of response content,
trace bodies, provider metadata, or final-text size.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

MAX_STABLE_RESPONSE_ID_BYTES = 4096
MAX_SUMMARY_TEXT_BYTES = 4096
MAX_SUMMARY_METRIC = (1 << 63) - 1


class DracoResultSummaryProjectionError(ValueError):
    """A sealed row cannot be represented by the bounded summary contract."""


_MERGED_ACCOUNT_NUMERIC_KEYS = (
    "request_count",
    "usage_observed_request_count",
    "duplicate_stable_receipt_count",
    "exact_request_count",
    "estimated_request_count",
    "mixed_request_count",
    "unknown_request_count",
    "total_tokens",
    "exact_tokens",
    "estimated_tokens",
    "mixed_tokens",
    "unknown_tokens",
    "recorded_cost_usd",
)


def _metric_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = (
            max(0, int(value))
            if isinstance(value, int | float)
            else max(0, int(str(value).strip()))
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise DracoResultSummaryProjectionError("invalid summary metric") from exc
    if result > MAX_SUMMARY_METRIC:
        raise DracoResultSummaryProjectionError("summary metric exceeds fixed bound")
    return result


def _bounded_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise DracoResultSummaryProjectionError(f"{field} must be a string")
    if len(value.encode("utf-8")) > MAX_SUMMARY_TEXT_BYTES:
        raise DracoResultSummaryProjectionError(f"{field} exceeds fixed byte bound")
    return value


def _bounded_derived_metric(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DracoResultSummaryProjectionError(f"{field} must be an integer")
    if value < 0 or value > MAX_SUMMARY_METRIC:
        raise DracoResultSummaryProjectionError(f"{field} exceeds fixed bound")
    return value


def _bounded_row_int(value: Any, *, field: str) -> int:
    try:
        result = int(value or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DracoResultSummaryProjectionError(f"{field} must be an integer") from exc
    if result < 0 or result > MAX_SUMMARY_METRIC:
        raise DracoResultSummaryProjectionError(f"{field} exceeds fixed bound")
    return result


def _bounded_summary_float(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DracoResultSummaryProjectionError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DracoResultSummaryProjectionError(f"{field} must be a bounded finite float") from exc
    if not math.isfinite(result) or abs(result) > MAX_SUMMARY_METRIC:
        raise DracoResultSummaryProjectionError(f"{field} must be a bounded finite float")
    return result


def _finite_nonnegative(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DracoResultSummaryProjectionError("summary cost must be numeric or null")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DracoResultSummaryProjectionError("summary cost is not a finite float") from exc
    if not math.isfinite(number) or number > MAX_SUMMARY_METRIC:
        raise DracoResultSummaryProjectionError("summary cost is not a bounded finite float")
    if number < 0.0:
        raise DracoResultSummaryProjectionError("summary cost must be nonnegative")
    return number


def _first_finite(unit: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in unit:
            continue
        value = _finite_nonnegative(unit.get(key))
        if value is not None:
            return value
    return None


class _StableResponseIdProjector:
    def digest(self, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            raise DracoResultSummaryProjectionError("stable response id must be a string or null")
        normalized = value.strip()
        if not normalized:
            return ""
        if len(normalized.encode("utf-8")) > MAX_STABLE_RESPONSE_ID_BYTES:
            raise DracoResultSummaryProjectionError("stable response id exceeds fixed byte bound")
        return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _receipt_fingerprint_text(value: Any, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DracoResultSummaryProjectionError(f"{field} must be a string or null")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > MAX_SUMMARY_TEXT_BYTES:
        raise DracoResultSummaryProjectionError(f"{field} exceeds fixed byte bound")
    return normalized


def _receipt_model_fingerprint(
    *,
    provider: str,
    model: str,
    cost_source: str,
) -> str:
    digest = hashlib.sha256()
    for value in (provider, model, cost_source.casefold()):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return "sha256:" + digest.hexdigest()


def _project_response_ids(
    value: Any,
    *,
    projector: _StableResponseIdProjector,
) -> list[str]:
    values = (
        value
        if isinstance(value, (list, tuple, set, frozenset))
        else ()
        if value is None
        else (value,)
    )
    projected = [digest for item in values if (digest := projector.digest(item))]
    exact_list = (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
    )
    if not exact_list:
        # Stable-id matching ignores this marker, while the provider-billed
        # structural check still observes an invalid response-id list.
        projected.append("")
    return projected


def _project_billing_receipt(value: Any) -> dict[str, Any]:
    invalid = {
        "currency": "",
        "status": "",
        "amount_nanos": None,
        "usd_equivalent_nanos": None,
        "fx_native_per_usd_nanos": 0,
        "schema_version": 0,
    }
    if not isinstance(value, Mapping):
        return invalid

    def receipt_int(raw: Any, *, nullable: bool = False) -> int | None:
        if raw is None and nullable:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > (1 << 63) - 1:
            raise ValueError
        return int(raw)

    try:
        amount_nanos = receipt_int(value.get("amount_nanos"), nullable=True)
        usd_nanos = receipt_int(value.get("usd_equivalent_nanos"), nullable=True)
        raw_fx = value.get("fx_native_per_usd_nanos")
        raw_schema = value.get("schema_version", 1)
        if (
            isinstance(raw_fx, bool)
            or not isinstance(raw_fx, int)
            or raw_fx <= 0
            or raw_fx > (1 << 63) - 1
            or isinstance(raw_schema, bool)
            or not isinstance(raw_schema, int)
            or raw_schema != 1
        ):
            return invalid
        currency = value.get("currency")
        status = value.get("status")
        if (
            not isinstance(currency, str)
            or len(currency) != 3
            or any(character < "A" or character > "Z" for character in currency)
            or status not in {"confirmed", "pending"}
        ):
            return invalid
        if status == "confirmed" and (amount_nanos is None or usd_nanos is None):
            return invalid
        if status == "pending" and usd_nanos is not None:
            return invalid
        if status == "confirmed":
            expected_usd_nanos = (amount_nanos * 1_000_000_000 + raw_fx // 2) // raw_fx
            if expected_usd_nanos != usd_nanos:
                return invalid
    except (TypeError, ValueError, OverflowError):
        return invalid
    return {
        "currency": currency,
        "status": status,
        "amount_nanos": amount_nanos,
        "usd_equivalent_nanos": usd_nanos,
        "fx_native_per_usd_nanos": raw_fx,
        "schema_version": 1,
    }


def _compact_receipt_unit(
    unit: Mapping[str, Any],
    *,
    stable_id_projector: _StableResponseIdProjector,
) -> dict[str, Any]:
    """Retain bounded accounting fields and hashed stable identities only."""

    projected: dict[str, Any] = {}
    raw_provider = _receipt_fingerprint_text(
        unit.get("provider"),
        field="receipt provider",
    )
    raw_model = _receipt_fingerprint_text(
        unit.get("model"),
        field="receipt model",
    )
    raw_cost_source = _receipt_fingerprint_text(
        unit.get("cost_source") or "none",
        field="receipt cost_source",
    ).casefold()
    projected["provider"] = (
        "openrouter" if raw_provider.casefold() == "openrouter" else "other" if raw_provider else ""
    )
    # The receipt matcher fingerprints provider/model/source separately.  The
    # fixed provider and source enums therefore need one bounded digest that
    # preserves their original equality relation without retaining identity.
    projected["model"] = _receipt_model_fingerprint(
        provider=raw_provider,
        model=raw_model,
        cost_source=raw_cost_source,
    )
    for key in (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
    ):
        if key in unit:
            projected[key] = _metric_int(unit.get(key))
    if "response_id" in unit:
        projected["response_id"] = stable_id_projector.digest(unit.get("response_id"))
    if "billed_cost" in unit:
        projected["billed_cost"] = _finite_nonnegative(unit.get("billed_cost"))

    cost_source = raw_cost_source
    if "cost_source" in unit:
        projected["cost_source"] = (
            cost_source
            if cost_source in {"openrouter_usage", "provider_billed", "mixed"}
            else "opensquilla_estimate"
            if cost_source.startswith("opensquilla_")
            else "none"
        )
    total_cost = _first_finite(unit, "cost_usd", "costUsd")
    if total_cost is not None:
        projected["cost_usd"] = total_cost
    billed_cost = _first_finite(unit, "billed_cost_usd", "billedCostUsd")
    if billed_cost is not None:
        projected["billed_cost_usd"] = billed_cost
    estimated_cost = _first_finite(unit, "estimated_cost_usd", "estimatedCostUsd")
    if estimated_cost is not None:
        projected["estimated_cost_usd"] = estimated_cost

    raw_receipt = unit.get("billing_receipt", unit.get("billingReceipt"))
    if raw_receipt is not None:
        projected["billing_receipt"] = _project_billing_receipt(raw_receipt)

    provider_usage = unit.get("provider_usage")
    if isinstance(provider_usage, Mapping):
        compact_provider_usage: dict[str, Any] = {}
        if "is_byok" in provider_usage:
            is_byok = provider_usage.get("is_byok")
            compact_provider_usage["is_byok"] = (
                is_byok if is_byok is True or is_byok is False else None
            )
        if "provider_reported_cost" in provider_usage:
            compact_provider_usage["provider_reported_cost"] = _finite_nonnegative(
                provider_usage.get("provider_reported_cost")
            )
        if "router_metadata" in provider_usage:
            router_metadata = provider_usage.get("router_metadata")
            if isinstance(router_metadata, Mapping):
                router_is_byok = router_metadata.get("is_byok")
                compact_provider_usage["router_metadata"] = {
                    "is_byok": (
                        router_is_byok
                        if router_is_byok is True or router_is_byok is False
                        else None
                    )
                }
            else:
                compact_provider_usage["router_metadata"] = None
        if "response_ids" in provider_usage:
            compact_provider_usage["response_ids"] = _project_response_ids(
                provider_usage.get("response_ids"),
                projector=stable_id_projector,
            )
        if "response_id" in provider_usage:
            compact_provider_usage["response_id"] = stable_id_projector.digest(
                provider_usage.get("response_id")
            )
        if compact_provider_usage:
            projected["provider_usage"] = compact_provider_usage
    return projected


def compact_cost_merge_account(account: Mapping[str, Any]) -> dict[str, Any]:
    """Project an account to fields used by group-level cost coverage."""

    receipts = account.get("_stable_usage_receipts")
    if account.get("_receipt_provenance_complete") is True and isinstance(receipts, list):
        request_count = _metric_int(account.get("request_count"))
        if len(receipts) > request_count:
            raise DracoResultSummaryProjectionError(
                "stable receipt list exceeds physical request count"
            )
        if any(not isinstance(unit, Mapping) for unit in receipts):
            raise DracoResultSummaryProjectionError("stable receipt must be a mapping")
        stable_id_projector = _StableResponseIdProjector()
        return {
            "request_count": request_count,
            "duplicate_stable_receipt_count": _metric_int(
                account.get("duplicate_stable_receipt_count")
            ),
            "_stable_usage_receipts": [
                _compact_receipt_unit(
                    unit,
                    stable_id_projector=stable_id_projector,
                )
                for unit in receipts
                if isinstance(unit, Mapping)
            ],
            "_receipt_provenance_complete": True,
        }
    compact: dict[str, Any] = {}
    for key in _MERGED_ACCOUNT_NUMERIC_KEYS:
        value = account.get(key)
        if key == "recorded_cost_usd":
            normalized = _finite_nonnegative(value)
            compact[key] = normalized if normalized is not None else 0.0
        else:
            compact[key] = _metric_int(value)
    return compact


@dataclass(frozen=True, slots=True)
class DracoResultSummaryFact:
    group: str
    task_id: str
    completion_failed: bool
    completed: bool
    judging_present: bool
    latency_ms: int
    quality_scored: bool
    quality_value: float
    pass_rate: float | None
    judge_error_count: int
    generation_cost_usd: float
    actual_generation_cost_usd: float
    judge_cost_usd: float
    candidate_judge_cost_usd: float
    total_cost_usd: float
    actual_spend_cost_usd: float
    llm_cost_complete: bool
    result_cost_complete: bool
    actual_spend_cost_complete: bool
    usage_unknown_count: int
    llm_merge_account: dict[str, Any]
    actual_llm_merge_account: dict[str, Any]
    visible_tokens: int
    reasoning_tokens: int
    stream_tool_calls: int
    server_tool_calls: int
    total_tool_calls: int
    trajectory_steps: int
    llm_requests: int


def build_result_summary_fact(
    row: dict[str, Any],
    *,
    row_cost_accounting: Callable[[dict[str, Any]], dict[str, Any]],
    row_usage_number: Callable[[dict[str, Any], str], float],
    row_metric_int: Callable[[dict[str, Any], str, str | None], int],
    row_server_tool_call_count: Callable[[dict[str, Any]], int],
    row_total_tool_call_count: Callable[[dict[str, Any]], int],
    row_trajectory_steps: Callable[[dict[str, Any]], int],
    row_llm_request_count: Callable[[dict[str, Any]], int],
) -> DracoResultSummaryFact:
    error = row.get("error")
    completed = not bool(error)
    group = _bounded_text(row.get("group"), field="group")
    task_id = _bounded_text(row.get("task_id"), field="task_id")
    completion = row.get("completion_status")
    completion_complete = bool(
        isinstance(completion, Mapping) and completion.get("status") == "complete"
    )
    judge = row.get("judge")
    judge_mapping = judge if isinstance(judge, Mapping) else {}
    pass_rate_value = judge_mapping.get("pass_rate")
    pass_rate = (
        _bounded_summary_float(pass_rate_value, field="judge pass_rate")
        if completed and isinstance(pass_rate_value, int | float)
        else None
    )
    judge_error_count = (
        _bounded_row_int(
            judge_mapping.get("judge_error_count"),
            field="judge_error_count",
        )
        if completed
        else 0
    )
    quality_total = row["quality_total"] if completed else None
    quality_value = (
        0.0
        if not completed
        else _bounded_summary_float(quality_total, field="quality_total")
        if isinstance(quality_total, int | float)
        else 0.0
    )
    try:
        account = row_cost_accounting(row)
        visible_tokens = int(row_usage_number(row, "input_tokens")) + int(
            row_usage_number(row, "output_tokens")
        )
        reasoning_tokens = int(row_usage_number(row, "reasoning_tokens"))
        stream_tool_calls = row_metric_int(
            row,
            "stream_tool_call_count",
            "tool_call_count",
        )
        server_tool_calls = row_server_tool_call_count(row)
        total_tool_calls = row_total_tool_call_count(row)
        trajectory_steps = row_trajectory_steps(row)
        llm_requests = row_llm_request_count(row)
    except DracoResultSummaryProjectionError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise DracoResultSummaryProjectionError(
            "row summary derivation produced invalid numeric evidence"
        ) from exc
    return DracoResultSummaryFact(
        group=group,
        task_id=task_id,
        completion_failed=bool(error) or not completion_complete,
        completed=completed,
        judging_present=isinstance(judge, dict),
        latency_ms=_bounded_row_int(row.get("latency_ms"), field="latency_ms"),
        quality_scored=completed and quality_total is not None,
        quality_value=quality_value,
        pass_rate=pass_rate,
        judge_error_count=_bounded_derived_metric(
            judge_error_count,
            field="judge_error_count",
        ),
        generation_cost_usd=_bounded_summary_float(
            account["generation"]["recorded_cost_usd"],
            field="generation_cost_usd",
        ),
        actual_generation_cost_usd=_bounded_summary_float(
            account["actual_generation_spend"]["recorded_cost_usd"],
            field="actual_generation_cost_usd",
        ),
        judge_cost_usd=_bounded_summary_float(
            account["judge"]["recorded_cost_usd"],
            field="judge_cost_usd",
        ),
        candidate_judge_cost_usd=_bounded_summary_float(
            account["candidate_judge"]["recorded_cost_usd"],
            field="candidate_judge_cost_usd",
        ),
        total_cost_usd=_bounded_summary_float(
            account["recorded_total_cost_usd"],
            field="total_cost_usd",
        ),
        actual_spend_cost_usd=_bounded_summary_float(
            account["actual_spend_recorded_total_cost_usd"],
            field="actual_spend_cost_usd",
        ),
        llm_cost_complete=bool(account["llm_total"]["cost_complete"]),
        result_cost_complete=bool(account["result_cost_complete"]),
        actual_spend_cost_complete=bool(account["actual_spend_cost_complete"]),
        usage_unknown_count=_metric_int(account["llm_total"]["unknown_request_count"]),
        llm_merge_account=compact_cost_merge_account(account["llm_total"]),
        actual_llm_merge_account=compact_cost_merge_account(account["actual_llm_total"]),
        visible_tokens=_bounded_derived_metric(
            visible_tokens,
            field="visible_tokens",
        ),
        reasoning_tokens=_bounded_derived_metric(
            reasoning_tokens,
            field="reasoning_tokens",
        ),
        stream_tool_calls=_bounded_derived_metric(
            stream_tool_calls,
            field="stream_tool_calls",
        ),
        server_tool_calls=_bounded_derived_metric(
            server_tool_calls,
            field="server_tool_calls",
        ),
        total_tool_calls=_bounded_derived_metric(
            total_tool_calls,
            field="total_tool_calls",
        ),
        trajectory_steps=_bounded_derived_metric(
            trajectory_steps,
            field="trajectory_steps",
        ),
        llm_requests=_bounded_derived_metric(
            llm_requests,
            field="llm_requests",
        ),
    )


def result_completion_failures(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Recreate legacy manifest failures from a one-row-at-a-time source."""

    failures: list[dict[str, Any]] = []
    for row in rows:
        completion = row.get("completion_status")
        completion_complete = bool(
            isinstance(completion, Mapping) and completion.get("status") == "complete"
        )
        if not row.get("error") and completion_complete:
            continue
        reasons = (
            list(completion.get("incomplete_reasons") or [])
            if isinstance(completion, Mapping)
            else ["missing_completion_status"]
        )
        if row.get("error"):
            reasons.append(str(row["error"]))
        failures.append(
            {
                "stage": "result_completion",
                "group": row.get("group"),
                "task_id": row.get("task_id"),
                "reasons": list(dict.fromkeys(reason for reason in reasons if reason)),
                "model_or_judge_started": True,
            }
        )
    return failures


def result_failures_and_coverage(
    facts: list[DracoResultSummaryFact],
    *,
    expected_keys: set[tuple[str, str]],
    completion_rows: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected_failure_count = sum(1 for fact in facts if fact.completion_failed)
    if expected_failure_count:
        if completion_rows is None:
            raise DracoResultSummaryProjectionError(
                "failed result facts require verified completion rows"
            )
        failures = result_completion_failures(completion_rows)
        if len(failures) != expected_failure_count:
            raise DracoResultSummaryProjectionError(
                "verified completion failures do not match summary facts"
            )
    else:
        failures = []
    counts: dict[tuple[str, str], int] = {}
    for fact in facts:
        key = (
            str(fact.group or "").strip().upper(),
            str(fact.task_id or "").strip(),
        )
        counts[key] = counts.get(key, 0) + 1
    actual_keys = set(counts)
    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    duplicates = sorted(key for key, count in counts.items() if count > 1)
    coverage = {
        "pass": not missing and not unexpected and not duplicates,
        "expected_row_count": len(expected_keys),
        "actual_row_count": len(facts),
        "actual_unique_key_count": len(actual_keys),
        "missing_keys": [list(key) for key in missing],
        "unexpected_keys": [list(key) for key in unexpected],
        "duplicate_keys": [{"key": list(key), "count": counts[key]} for key in duplicates],
    }
    if not coverage["pass"]:
        coverage_reasons: list[str] = []
        if coverage["missing_keys"]:
            coverage_reasons.append("missing_result_rows")
        if coverage["unexpected_keys"]:
            coverage_reasons.append("unexpected_result_rows")
        if coverage["duplicate_keys"]:
            coverage_reasons.append("duplicate_result_rows")
        failures.append(
            {
                "stage": "result_coverage",
                **coverage,
                "reasons": coverage_reasons,
                "model_or_judge_started": bool(facts),
            }
        )
    return failures, coverage


def _percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, round((pct / 100.0) * (len(ordered) - 1))),
    )
    return float(ordered[index])


def _numeric_pct_delta(value: Any, baseline: Any) -> float | None:
    if isinstance(value, int | float) and isinstance(baseline, int | float):
        baseline_float = float(baseline)
        if baseline_float == 0.0:
            return None
        return (float(value) - baseline_float) / baseline_float * 100.0
    return None


def summarize_result_facts(
    facts: list[DracoResultSummaryFact],
    *,
    merge_cost_accounting: Callable[[str, list[dict[str, Any]]], dict[str, Any]],
) -> dict[str, Any]:
    summary: dict[str, Any] = {"groups": {}}
    judging_enabled = any(fact.judging_present for fact in facts)
    for group in sorted({fact.group for fact in facts}):
        group_facts = [fact for fact in facts if fact.group == group]
        completed_facts = [fact for fact in group_facts if fact.completed]
        latencies = [fact.latency_ms for fact in group_facts]
        scored_totals = [fact.quality_value for fact in completed_facts if fact.quality_scored]
        quality_values = [fact.quality_value for fact in group_facts] if judging_enabled else []
        pass_rates = [fact.pass_rate for fact in completed_facts if fact.pass_rate is not None]
        generation_costs = [fact.generation_cost_usd for fact in group_facts]
        actual_generation_costs = [fact.actual_generation_cost_usd for fact in group_facts]
        judge_costs = [fact.judge_cost_usd for fact in group_facts]
        candidate_judge_costs = [fact.candidate_judge_cost_usd for fact in group_facts]
        costs = [fact.total_cost_usd for fact in group_facts]
        actual_spend_costs = [fact.actual_spend_cost_usd for fact in group_facts]
        completed_costs = [fact.total_cost_usd for fact in completed_facts]
        group_llm_cost = merge_cost_accounting(
            "group_llm_total",
            [fact.llm_merge_account for fact in group_facts],
        )
        actual_group_llm_cost = merge_cost_accounting(
            "actual_group_llm_total",
            [fact.actual_llm_merge_account for fact in group_facts],
        )
        visible_tokens = [fact.visible_tokens for fact in group_facts]
        reasoning_tokens = [fact.reasoning_tokens for fact in group_facts]
        all_tokens = list(visible_tokens)
        stream_tool_calls = [fact.stream_tool_calls for fact in group_facts]
        server_tool_calls = [fact.server_tool_calls for fact in group_facts]
        total_tool_calls = [fact.total_tool_calls for fact in group_facts]
        trajectory_steps = [fact.trajectory_steps for fact in group_facts]
        llm_requests = [fact.llm_requests for fact in group_facts]
        usage_unknown = [fact.usage_unknown_count for fact in group_facts]
        summary["groups"][group] = {
            "rows": len(group_facts),
            "task_ids": sorted(str(fact.task_id or "") for fact in group_facts),
            "completed": len(completed_facts),
            "scored_rows": len(scored_totals),
            "score_coverage_pct": (
                len(scored_totals) / len(group_facts) * 100.0 if group_facts else 0.0
            ),
            "avg_quality": statistics.mean(quality_values) if quality_values else None,
            "avg_quality_scored": (statistics.mean(scored_totals) if scored_totals else None),
            "avg_pass_rate": statistics.mean(pass_rates) if pass_rates else None,
            "judge_errors": sum(fact.judge_error_count for fact in completed_facts),
            "avg_cost_usd": statistics.mean(costs) if costs else 0.0,
            "avg_cost_completed_usd": (
                statistics.mean(completed_costs) if completed_costs else None
            ),
            "recorded_total_cost_usd": sum(costs),
            "avg_actual_spend_cost_usd": (
                statistics.mean(actual_spend_costs) if actual_spend_costs else 0.0
            ),
            "actual_spend_recorded_total_cost_usd": sum(actual_spend_costs),
            "recorded_generation_cost_usd": sum(generation_costs),
            "actual_spend_generation_cost_usd": sum(actual_generation_costs),
            "recorded_judge_cost_usd": sum(judge_costs),
            "recorded_candidate_judge_cost_usd": sum(candidate_judge_costs),
            "avg_recorded_generation_cost_usd": (
                statistics.mean(generation_costs) if generation_costs else 0.0
            ),
            "avg_actual_spend_generation_cost_usd": (
                statistics.mean(actual_generation_costs) if actual_generation_costs else 0.0
            ),
            "avg_recorded_judge_cost_usd": (statistics.mean(judge_costs) if judge_costs else 0.0),
            "avg_recorded_candidate_judge_cost_usd": (
                statistics.mean(candidate_judge_costs) if candidate_judge_costs else 0.0
            ),
            "known_cost_request_coverage_pct": group_llm_cost["known_request_coverage_pct"],
            "exact_cost_request_coverage_pct": group_llm_cost["exact_request_coverage_pct"],
            "unknown_cost_request_count": group_llm_cost["unknown_request_count"],
            "unknown_cost_tokens": group_llm_cost["unknown_tokens"],
            "llm_cost_complete_rows": sum(1 for fact in group_facts if fact.llm_cost_complete),
            "result_cost_complete_rows": sum(
                1 for fact in group_facts if fact.result_cost_complete
            ),
            "actual_spend_known_cost_request_coverage_pct": actual_group_llm_cost[
                "known_request_coverage_pct"
            ],
            "actual_spend_unknown_cost_request_count": actual_group_llm_cost[
                "unknown_request_count"
            ],
            "actual_spend_cost_complete_rows": sum(
                1 for fact in group_facts if fact.actual_spend_cost_complete
            ),
            "avg_visible_tokens": (statistics.mean(visible_tokens) if visible_tokens else 0.0),
            "avg_reasoning_tokens": (
                statistics.mean(reasoning_tokens) if reasoning_tokens else 0.0
            ),
            "avg_total_tokens": statistics.mean(all_tokens) if all_tokens else 0.0,
            "avg_stream_tool_calls": (
                statistics.mean(stream_tool_calls) if stream_tool_calls else 0.0
            ),
            "avg_server_tool_calls": (
                statistics.mean(server_tool_calls) if server_tool_calls else 0.0
            ),
            "avg_tool_calls": (statistics.mean(total_tool_calls) if total_tool_calls else 0.0),
            "total_tool_calls": sum(total_tool_calls),
            "tool_call_rate_pct": (
                sum(1 for count in total_tool_calls if count > 0) / len(total_tool_calls) * 100.0
                if total_tool_calls
                else 0.0
            ),
            "avg_trajectory_steps": (
                statistics.mean(trajectory_steps) if trajectory_steps else 0.0
            ),
            "avg_llm_requests": (statistics.mean(llm_requests) if llm_requests else 0.0),
            "total_llm_requests": sum(llm_requests),
            "avg_usage_unknown": (statistics.mean(usage_unknown) if usage_unknown else 0.0),
            "total_usage_unknown": sum(usage_unknown),
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p95_ms": _percentile(latencies, 95),
        }
    for item in summary["groups"].values():
        for baseline in ("B0", "B1"):
            baseline_item = summary["groups"].get(baseline) or {}
            suffix = baseline.lower()
            item[f"avg_quality_pct_delta_vs_{suffix}"] = _numeric_pct_delta(
                item.get("avg_quality"),
                baseline_item.get("avg_quality"),
            )
            comparable_costs = (
                item.get("result_cost_complete_rows") == item.get("rows")
                and baseline_item.get("result_cost_complete_rows") == baseline_item.get("rows")
                and item.get("completed") == item.get("rows")
                and baseline_item.get("completed") == baseline_item.get("rows")
                and item.get("task_ids") == baseline_item.get("task_ids")
            )
            item[f"avg_cost_pct_delta_vs_{suffix}"] = (
                _numeric_pct_delta(
                    item.get("avg_cost_usd"),
                    baseline_item.get("avg_cost_usd"),
                )
                if comparable_costs
                else None
            )
    return summary


__all__ = [
    "DracoResultSummaryFact",
    "DracoResultSummaryProjectionError",
    "MAX_STABLE_RESPONSE_ID_BYTES",
    "MAX_SUMMARY_TEXT_BYTES",
    "build_result_summary_fact",
    "compact_cost_merge_account",
    "result_completion_failures",
    "result_failures_and_coverage",
    "summarize_result_facts",
]
