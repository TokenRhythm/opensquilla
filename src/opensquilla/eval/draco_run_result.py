"""Shared in-memory DRACO run result and deterministic summary helpers."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any

from opensquilla.eval.draco_usage_evidence import (
    coerce_metric_int,
    ensemble_usage_unknown_count,
    exact_provider_usage_cost,
    trusted_provider_billed_cost,
    usage_unknown_count_from_usage_payload,
)
from opensquilla.provider.types import DoneEvent
from opensquilla.usage_evidence import (
    canonicalize_run_usage,
    derive_physical_request_count,
)


@dataclass
class RunResult:
    final_text: str
    done: DoneEvent | None
    error: str = ""
    latency_ms: int = 0
    ttft_ms: int | None = None
    tool_call_count: int = 0
    trace_events: list[dict[str, Any]] = field(default_factory=list)
    setup_latency_ms: int = 0
    setup_usage: list[dict[str, Any]] = field(default_factory=list)
    routing_trace: dict[str, Any] = field(default_factory=dict)
    audit_warnings: list[str] = field(default_factory=list)


@dataclass
class ProviderBuildResult:
    provider: Any
    prompt: str
    setup_latency_ms: int = 0
    setup_usage: list[dict[str, Any]] = field(default_factory=list)
    routing_trace: dict[str, Any] = field(default_factory=dict)


class ProviderBuildError(RuntimeError):
    """Preserve already-billed setup receipts when provider construction fails."""

    def __init__(
        self,
        cause: Exception,
        *,
        setup_latency_ms: int,
        setup_usage: list[dict[str, Any]],
        routing_trace: dict[str, Any],
    ) -> None:
        super().__init__(
            "provider_build_failed_after_setup:"
            + type(cause).__name__
        )
        self.setup_latency_ms = setup_latency_ms
        self.setup_usage = list(setup_usage)
        self.routing_trace = dict(routing_trace)


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, Path):
        return str(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def server_tool_counts_from_provider_usage(provider_usage: Any) -> dict[str, int]:
    if not isinstance(provider_usage, dict):
        return {}
    raw_counts = provider_usage.get("server_tool_use")
    if not isinstance(raw_counts, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw_counts.items():
        count = coerce_metric_int(value)
        if count:
            counts[str(key)] = counts.get(str(key), 0) + count
    return counts


def add_metric_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value)


def server_tool_counts_from_usage_payload(usage: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    breakdown = usage.get("model_usage_breakdown")
    if isinstance(breakdown, list):
        for row in breakdown:
            if isinstance(row, dict):
                add_metric_counts(
                    counts,
                    server_tool_counts_from_provider_usage(row.get("provider_usage")),
                )
        if counts:
            return counts
    return server_tool_counts_from_provider_usage(usage.get("provider_usage"))


def llm_request_count_for_run(
    *,
    spec: dict[str, str],
    done: DoneEvent | None,
    provider_attempted: bool,
) -> int:
    if not provider_attempted:
        return 0
    if done is not None:
        usage = done_payload(done)
        trace = done.ensemble_trace if isinstance(done.ensemble_trace, Mapping) else {}
        evidence: dict[str, Any] = {
            "usage": usage,
            "request_started": True,
        }
        if trace:
            evidence["ensemble_trace"] = trace
        return derive_physical_request_count(evidence, default_request_count=1)
    return 1


def done_payload(done: DoneEvent | None) -> dict[str, Any]:
    if done is None:
        return {}
    payload = {
        "provider": str(getattr(done, "provider", "") or ""),
        "model": done.model,
        "requested_provider": str(getattr(done, "requested_provider", "") or ""),
        "requested_model": str(getattr(done, "requested_model", "") or ""),
        "stop_reason": done.stop_reason,
        "input_tokens": done.input_tokens,
        "output_tokens": done.output_tokens,
        "reasoning_tokens": done.reasoning_tokens,
        "cached_tokens": done.cached_tokens,
        "cache_write_tokens": done.cache_write_tokens,
        "billed_cost": done.billed_cost,
        "cost_source": done.cost_source,
        "usage_missing_count": max(
            0,
            coerce_metric_int(getattr(done, "usage_missing_count", 0)),
        ),
        "provider_usage": getattr(done, "provider_usage", {}),
        "model_usage_breakdown": done.model_usage_breakdown,
        "reasoning_content_chars": len(done.reasoning_content or ""),
        "thinking_signature_present": bool(done.thinking_signature),
    }
    billing_receipt = getattr(done, "billing_receipt", None)
    physical_attempt_id = str(
        getattr(done, "physical_attempt_id", "") or ""
    )
    if physical_attempt_id:
        payload["physical_attempt_id"] = physical_attempt_id
    if billing_receipt is not None:
        payload["billing_receipt"] = billing_receipt
    payload["billed_cost"] = trusted_provider_billed_cost(payload)
    exact_cost = exact_provider_usage_cost(payload)
    if exact_cost is not None:
        payload["cost_source"] = "provider_billed"
    elif billing_receipt is not None:
        payload["cost_source"] = "unavailable"
    trace = done.ensemble_trace if isinstance(done.ensemble_trace, Mapping) else {}
    evidence_run: dict[str, Any] = {
        "usage": payload,
        "request_started": True,
    }
    if trace:
        evidence_run["ensemble_trace"] = trace
    identity_material = {
        "provider": payload["provider"],
        "model": payload["model"],
        "requested_provider": payload["requested_provider"],
        "requested_model": payload["requested_model"],
        "stop_reason": payload["stop_reason"],
        "provider_usage": payload["provider_usage"],
        "usage_missing_count": payload["usage_missing_count"],
    }
    identity_seed = (
        "done:"
        + hashlib.sha256(
            json.dumps(
                identity_material,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    payload = canonicalize_run_usage(
        evidence_run,
        identity_seed=identity_seed,
        requested_provider=payload["requested_provider"],
        requested_model=payload["requested_model"],
        default_request_count=1,
    )
    server_tool_use = server_tool_counts_from_usage_payload(payload)
    payload["server_tool_use"] = server_tool_use
    payload["server_tool_call_count"] = sum(server_tool_use.values())
    return payload


def run_result_usage_payload(result: RunResult) -> dict[str, Any]:
    payload = done_payload(result.done)
    if not result.setup_usage:
        return payload
    merged = dict(payload)
    merged.setdefault("model", getattr(result.done, "model", "") if result.done else "")
    merged.setdefault(
        "requested_provider",
        getattr(result.done, "requested_provider", "") if result.done else "",
    )
    merged.setdefault(
        "requested_model",
        getattr(result.done, "requested_model", "") if result.done else "",
    )
    merged.setdefault("stop_reason", getattr(result.done, "stop_reason", "") if result.done else "")
    merged.setdefault("provider_usage", {})
    merged.setdefault("reasoning_content_chars", 0)
    merged.setdefault("thinking_signature_present", False)
    for key in (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "cache_write_tokens",
    ):
        merged[key] = coerce_metric_int(merged.get(key)) + sum(
            coerce_metric_int(row.get(key)) for row in result.setup_usage
        )
    merged["billed_cost"] = float(merged.get("billed_cost") or 0.0) + sum(
        trusted_provider_billed_cost(row) for row in result.setup_usage
    )
    existing_breakdown = merged.get("model_usage_breakdown")
    merged["model_usage_breakdown"] = [
        *result.setup_usage,
        *(existing_breakdown if isinstance(existing_breakdown, list) else []),
    ]
    cost_sources = {str(row.get("cost_source") or "none") for row in result.setup_usage}
    if merged.get("cost_source"):
        cost_sources.add(str(merged["cost_source"]))
    cost_sources.discard("none")
    merged["cost_source"] = (
        next(iter(cost_sources)) if len(cost_sources) == 1 else "mixed" if cost_sources else "none"
    )
    server_tool_use = server_tool_counts_from_usage_payload(merged)
    merged["server_tool_use"] = server_tool_use
    merged["server_tool_call_count"] = sum(server_tool_use.values())
    return merged


def candidate_texts(
    done: DoneEvent | None,
    *,
    final_agent_call_only: bool = False,
) -> list[str]:
    if done is None:
        return []
    trace = done.ensemble_trace or {}
    candidates: list[Any] = []
    if isinstance(trace, dict):
        direct_candidates = trace.get("candidates")
        if isinstance(direct_candidates, list):
            candidates.extend(direct_candidates)
        calls = trace.get("calls")
        if isinstance(calls, list):
            selected_calls = calls[-1:] if final_agent_call_only else calls
            for call in selected_calls:
                if not isinstance(call, dict):
                    continue
                call_candidates = call.get("candidates")
                if isinstance(call_candidates, list):
                    candidates.extend(call_candidates)
    if not isinstance(candidates, list):
        return []
    return [
        str(candidate.get("text") or "")
        for candidate in candidates
        if isinstance(candidate, dict) and str(candidate.get("text") or "").strip()
    ]


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_NO_PHYSICAL_REQUEST_GATE_CODES = frozenset(
    {
        "agent_cleanup_in_progress",
        "agent_turn_in_progress",
        "benchmark_owner_cleanup_in_progress",
        "ensemble_cleanup_in_progress",
        "ensemble_call_in_progress",
    }
)


def run_result_was_blocked_before_request(result: RunResult) -> bool:
    """Return true only for ownership gates that start no LLM request."""

    return any(
        str(event.get("code") or "").strip() in _NO_PHYSICAL_REQUEST_GATE_CODES
        for event in result.trace_events
        if isinstance(event, dict)
    )


def run_result_error_physical_request_count(result: RunResult) -> int | None:
    """Return explicit adapter/Agent failure evidence, including zero."""

    for event in reversed(result.trace_events):
        if not isinstance(event, dict) or str(event.get("kind") or "") != "error":
            continue
        value = event.get("physical_request_count")
        if isinstance(value, int) and not isinstance(value, bool):
            return max(0, value)
        if event.get("request_started") is False:
            return 0
    return None


def run_result_summary(
    result: RunResult,
    *,
    requested_provider: str | None = None,
    requested_model: str | None = None,
    missing_usage_role: str | None = None,
    include_ensemble_trace: bool = False,
) -> dict[str, Any]:
    usage = run_result_usage_payload(result)
    server_tool_call_count = coerce_metric_int(usage.get("server_tool_call_count"))
    total_tool_call_count = result.tool_call_count + server_tool_call_count
    llm_request_count = 0
    usage_unknown_count = 0
    if result.done is not None:
        done_usage = done_payload(result.done)
        trace = (
            result.done.ensemble_trace if isinstance(result.done.ensemble_trace, Mapping) else {}
        )
        done_evidence: dict[str, Any] = {
            "usage": done_usage,
            "request_started": True,
        }
        if trace:
            done_evidence["ensemble_trace"] = trace
        llm_request_count = derive_physical_request_count(
            done_evidence,
            default_request_count=1,
        )
        usage_unknown_count = ensemble_usage_unknown_count(trace)
        usage_unknown_count = max(
            usage_unknown_count,
            coerce_metric_int(result.done.usage_missing_count),
            usage_unknown_count_from_usage_payload(done_usage),
        )
    elif result.error or result.final_text:
        explicit_request_count = run_result_error_physical_request_count(result)
        if explicit_request_count is not None:
            llm_request_count = explicit_request_count
            usage_unknown_count = max(
                usage_unknown_count,
                explicit_request_count,
            )
        elif not run_result_was_blocked_before_request(result):
            llm_request_count = 1
            usage_unknown_count = max(usage_unknown_count, 1)
    llm_request_count += usage_rows_request_count(result.setup_usage)
    if result.done is None and result.setup_usage:
        usage_unknown_count += usage_unknown_count_from_usage_payload(
            {"model_usage_breakdown": result.setup_usage}
        )
    if llm_request_count:
        identity_seed = (
            "run-result:"
            + hashlib.sha256(
                json.dumps(
                    {
                        "trace_events": result.trace_events,
                        "latency_ms": result.latency_ms,
                        "final_text_sha256": text_sha256(result.final_text),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
        )
        usage = canonicalize_run_usage(
            {
                "usage": usage,
                "physical_request_count": llm_request_count,
                "request_started": True,
            },
            identity_seed=identity_seed,
            requested_provider=(
                str(usage.get("requested_provider") or "")
                if requested_provider is None
                else requested_provider
            ),
            requested_model=(
                str(usage.get("requested_model") or "")
                if requested_model is None
                else requested_model
            ),
            role=missing_usage_role,
        )
        usage_unknown_count = max(
            usage_unknown_count,
            usage_unknown_count_from_usage_payload(usage),
        )
    summary = {
        "latency_ms": result.latency_ms,
        "ttft_ms": result.ttft_ms,
        "tool_call_count": result.tool_call_count,
        "stream_tool_call_count": result.tool_call_count,
        "server_tool_call_count": server_tool_call_count,
        "server_tool_use": usage.get("server_tool_use") or {},
        "total_tool_call_count": total_tool_call_count,
        "trajectory_steps": total_tool_call_count + llm_request_count,
        "llm_request_count": llm_request_count,
        "usage_unknown_count": usage_unknown_count,
        "error": result.error,
        "audit_warnings": list(result.audit_warnings),
        "final_text_chars": len(result.final_text),
        "final_text_sha256": text_sha256(result.final_text),
        "usage": usage,
        # Preserve the paid setup rows as an immutable mirror of their
        # aggregate usage entries for resume/finalizer reconciliation.
        "setup_usage": copy.deepcopy(result.setup_usage),
        "trace_events": result.trace_events,
        "setup_latency_ms": result.setup_latency_ms,
        "routing_trace": result.routing_trace,
    }
    selection_plan = (
        result.routing_trace.get("selection_plan")
        if isinstance(result.routing_trace, Mapping)
        else None
    )
    if (
        include_ensemble_trace
        or isinstance(selection_plan, Mapping)
        and selection_plan.get("ranking_thinking_assignment_enabled") is True
    ):
        summary["ensemble_trace"] = json_safe(
            result.done.ensemble_trace
            if result.done is not None
            and isinstance(result.done.ensemble_trace, Mapping)
            else {}
        )
    return summary


def judge_run_result_summary(
    result: RunResult,
    *,
    judge_provider: Any,
) -> dict[str, Any]:
    """Bind missing Judge usage to the frozen requested route at creation."""

    return run_result_summary(
        result,
        requested_provider=str(
            getattr(judge_provider, "provider_id", "") or ""
        ),
        requested_model=str(getattr(judge_provider, "model", "") or ""),
        missing_usage_role="unknown_request",
    )


def usage_rows_request_count(rows: list[dict[str, Any]]) -> int:
    """Count physical requests represented by aggregate setup-usage rows."""

    total = 0
    for row in rows:
        provider_usage = row.get("provider_usage")
        response_ids = (
            provider_usage.get("response_ids") if isinstance(provider_usage, Mapping) else None
        )
        total += max(
            1,
            coerce_metric_int(row.get("request_count")),
            len(response_ids) if isinstance(response_ids, list) else 0,
        )
    return total
