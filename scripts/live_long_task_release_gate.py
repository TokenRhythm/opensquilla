#!/usr/bin/env python3
"""Bounded release-gate coordinator for long-running chat scenarios.

This coordinator owns selection, repetition, resume, budgets, credential
isolation, and the public report.  A checked-in or temporary case driver owns
one concrete browser/provider scenario and communicates through two temporary
JSON files.  Provider credentials are injected only into the driver's
environment; they never appear in argv, case files, or reports.

Driver protocol::

    python driver.py --case-file /tmp/.../case.json --output /tmp/.../result.json

The result must contain only bounded metrics (``status``, ``stage``,
``physical_requests``, ``usage``, ``cost``, ``latency_ms``, ``counts``, and
``metrics``).  A required ``skipped`` or ``inconclusive`` result is a gate
failure, never a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from opensquilla.provider.registry import get_provider_spec  # noqa: E402
from scripts.live_harness_security import (  # noqa: E402
    child_environment,
    classify_failure,
    is_temporary_report_path,
    redact_text,
    report_contains_secret,
    sanitize_report,
    scan_and_remove_temporary_tree,
    write_safe_report,
)

EXIT_PASSED: Final = 0
EXIT_FAILED: Final = 1
EXIT_CONFIGURATION: Final = 2
EXIT_BUDGET: Final = 3
EXIT_INTERRUPTED: Final = 130
MAX_CASE_WALL_SECONDS: Final = 30 * 60
DEFAULT_CASE_DRIVER: Final = REPO_ROOT / "scripts" / "live_long_task_case_driver.py"
PERFORMANCE_REPORT_ENV: Final[dict[str, str]] = {
    "baseline": "OPENSQUILLA_LONG_TASK_PERF_BASELINE_PATH",
    "candidate": "OPENSQUILLA_LONG_TASK_PERF_CANDIDATE_PATH",
    "resilience": "OPENSQUILLA_LONG_TASK_PERF_RESILIENCE_PATH",
}

CREDENTIAL_ENV_BY_PROVIDER: Final[dict[str, str]] = {
    "deepseek": "DEEPSEEK_API_KEY",
    "tokenrhythm": "TOKENRHYTHM_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

MODEL_MATRIX: Final[dict[str, tuple[str, str]]] = {
    "deepseek": ("deepseek-v4-flash", "deepseek-v4-pro"),
    "tokenrhythm": ("deepseek-v4-flash", "deepseek-v4-pro"),
    "openrouter": (
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    ),
}

FAULT_SCENARIOS: Final[tuple[str, ...]] = (
    "fault_429_retry_after",
    "fault_503",
    "fault_reset_before_first_token",
    "fault_partial_then_reset",
    "fault_reasoning_only",
    "fault_late_terminal",
)

BROWSER_SCENARIOS: Final[tuple[str, ...]] = (
    "browser_refresh",
    "browser_websocket_interrupt",
    "browser_gateway_graceful_restart",
    "browser_gateway_forced_restart",
    "browser_hidden_11_minutes",
    "browser_stop_each_phase",
    "queue_refresh",
    "queue_switch_session",
    "queue_dual_tab",
    "queue_gateway_restart",
)

ALL_SCENARIOS: Final[tuple[str, ...]] = (
    "direct",
    "router",
    "long_reasoning",
    "tool_compaction",
    "long_answer",
    *FAULT_SCENARIOS,
    "fallback",
    *BROWSER_SCENARIOS,
)

_ALLOWED_STATUSES = frozenset({"passed", "failed", "skipped", "inconclusive"})
_ALLOWED_USAGE_KEYS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cached_tokens",
        "total_tokens",
    }
)
_ALLOWED_COST_KEYS = frozenset(
    {
        "billed_cost_usd",
        "estimated_cost_usd",
    }
)
_ALLOWED_COUNT_KEYS = frozenset(
    {
        "provider_legs",
        "accounted_provider_legs",
        "usage_missing_cost_entries",
        "tool_legs",
        "retry_legs",
        "fallback_legs",
        "activity_events",
        "tokens_rendered",
        "mounted_rows",
        "dom_nodes",
        "compactions",
        "output_bytes",
        "incremental_chunks",
        "router_decisions",
        "reasoning_pulses",
        "retry_after_honored",
        "fallback_before_request",
        "subscription_recoveries",
        "interruption_notices",
        "stop_phases",
        "cancelled_turns",
        "queued_inputs",
        "dispatched_inputs",
        "transcript_occurrences",
        "attachment_inputs",
        "browser_failure_code",
        "queue_exact_once",
        "pending_inputs_remaining",
    }
)
_ALLOWED_METRIC_KEYS = frozenset(
    {
        "activity_latency_ms",
        "first_reasoning_ms",
        "first_token_ms",
        "input_next_paint_p95_ms",
        "input_next_paint_max_ms",
        "max_main_thread_task_ms",
        "peak_heap_delta_bytes",
        "post_gc_heap_delta_bytes",
        "anchor_drift_px",
        "bottom_gap_px",
        "subscription_recovery_ms",
        "retry_wait_ms",
        "max_reasoning_pulse_gap_ms",
        "hidden_duration_ms",
        "post_gc_growth_bytes",
        "markdown_parse_reduction_pct",
        "recalc_style_reduction_pct",
        "peak_heap_reduction_pct",
    }
)
_ALLOWED_FAILURE_CLASSES = frozenset(
    {
        "assertion",
        "auth",
        "balance",
        "budget",
        "configuration",
        "implementation",
        "inconclusive",
        "missing-credential",
        "model-unavailable",
        "not-entitled",
        "provider-unavailable",
        "rate-limit",
        "skipped",
        "transport",
        "unknown",
    }
)


@dataclass(frozen=True)
class BudgetLimits:
    wall_seconds: int = 4 * 60 * 60
    billed_cost_usd: float = 30.0
    physical_requests: int = 120
    billed_tokens: int = 1_000_000

    def validate(self) -> None:
        if not 1 <= self.wall_seconds <= 4 * 60 * 60:
            raise ValueError("wall budget must be between 1 second and 4 hours")
        if not 0 < self.billed_cost_usd <= 30:
            raise ValueError("cost budget must be greater than zero and at most $30")
        if not 1 <= self.physical_requests <= 120:
            raise ValueError("request budget must be between 1 and 120")
        if not 1 <= self.billed_tokens <= 1_000_000:
            raise ValueError("token budget must be between 1 and 1,000,000")


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    provider: str
    model: str
    scenario: str
    repeat_index: int
    fallback_provider: str | None = None


@dataclass(frozen=True)
class DriverResult:
    status: Literal["passed", "failed", "skipped", "inconclusive"]
    stage: str
    physical_requests: int
    latency_ms: int
    usage: dict[str, int]
    cost: dict[str, float]
    counts: dict[str, int]
    metrics: dict[str, float]
    failure_class: str | None = None
    driver_exit_code: int = 0


@dataclass(frozen=True)
class RemainingBudget:
    wall_ms: int
    billed_cost_usd: float
    physical_requests: int
    billed_tokens: int


CaseExecutor = Callable[[CaseSpec, RemainingBudget], DriverResult]
CheckpointWriter = Callable[[dict[str, Any]], None]


_MINIMUM_PHYSICAL_REQUESTS: Final[dict[str, int]] = {
    "direct": 1,
    "router": 1,
    "long_reasoning": 1,
    # One response may issue all twenty idempotent tool calls in parallel;
    # one follow-up provider leg consumes their results and the mandatory
    # manual compaction performs one additional summarization request.
    "tool_compaction": 3,
    "long_answer": 1,
    "fault_429_retry_after": 2,
    "fault_503": 2,
    "fault_reset_before_first_token": 2,
    "fault_partial_then_reset": 2,
    "fault_reasoning_only": 2,
    "fault_late_terminal": 1,
    "fallback": 2,
    "browser_refresh": 1,
    # After the 30-second outage, a recovery probe proves that new deltas are
    # subscribed rather than merely reconciled from durable history.
    "browser_websocket_interrupt": 2,
    # The interrupted turn is never replayed automatically.  A second request
    # proves that the recovered subscription renders its first token live.
    "browser_gateway_graceful_restart": 2,
    "browser_gateway_forced_restart": 2,
    "browser_hidden_11_minutes": 1,
    "browser_stop_each_phase": 4,
    # One running task plus one staged text+attachment dispatch.
    "queue_refresh": 2,
    "queue_switch_session": 2,
    "queue_dual_tab": 2,
    "queue_gateway_restart": 2,
}


def minimum_physical_requests(cases: Iterable[CaseSpec]) -> int:
    total = 0
    for case in cases:
        try:
            total += _MINIMUM_PHYSICAL_REQUESTS[case.scenario]
        except KeyError as exc:
            raise ValueError(f"no physical-request minimum for {case.scenario!r}") from exc
    return total


def _safe_component(value: str) -> str:
    rendered = "".join(ch.lower() if ch.isalnum() else "-" for ch in value)
    return "-".join(part for part in rendered.split("-") if part)


def _case(
    provider: str,
    model: str,
    scenario: str,
    repeat_index: int,
    *,
    fallback_provider: str | None = None,
) -> CaseSpec:
    parts = [provider, scenario, model or "router"]
    if fallback_provider:
        parts.append(f"to-{fallback_provider}")
    parts.append(str(repeat_index))
    case_id = "-".join(_safe_component(part) for part in parts)
    return CaseSpec(
        case_id=case_id,
        provider=provider,
        model=model,
        scenario=scenario,
        repeat_index=repeat_index,
        fallback_provider=fallback_provider,
    )


def build_mandatory_matrix(*, repeat_override: int | None = None) -> list[CaseSpec]:
    """Build the fixed release contract without prompts or user content."""

    if repeat_override is not None and not 1 <= repeat_override <= 10:
        raise ValueError("repeat override must be between 1 and 10")
    rows: list[CaseSpec] = []

    def repetitions(default: int) -> range:
        return range(1, (repeat_override or default) + 1)

    # Cheap probes run first so an unavailable provider suppresses its later,
    # more expensive rows while the other providers continue collecting data.
    for provider, models in MODEL_MATRIX.items():
        for model in models:
            rows.extend(_case(provider, model, "direct", index) for index in repetitions(3))
    for provider, models in MODEL_MATRIX.items():
        rows.extend(_case(provider, models[0], "router", index) for index in repetitions(2))

    reasoning_repeats = {"deepseek": 1, "tokenrhythm": 2, "openrouter": 1}
    for provider, default_repeat in reasoning_repeats.items():
        model = MODEL_MATRIX[provider][1]
        rows.extend(
            _case(provider, model, "long_reasoning", index) for index in repetitions(default_repeat)
        )

    for scenario in ("tool_compaction", "long_answer"):
        for provider, models in MODEL_MATRIX.items():
            rows.extend(_case(provider, models[1], scenario, index) for index in repetitions(1))

    for scenario in FAULT_SCENARIOS:
        default_repeat = (
            2
            if scenario
            in {
                "fault_429_retry_after",
                "fault_reset_before_first_token",
            }
            else 1
        )
        for provider, models in MODEL_MATRIX.items():
            rows.extend(
                _case(provider, models[0], scenario, index) for index in repetitions(default_repeat)
            )

    fallback_ring = (
        ("tokenrhythm", "deepseek"),
        ("deepseek", "openrouter"),
        ("openrouter", "tokenrhythm"),
    )
    for provider, fallback_provider in fallback_ring:
        model = MODEL_MATRIX[provider][1]
        rows.extend(
            _case(
                provider,
                model,
                "fallback",
                index,
                fallback_provider=fallback_provider,
            )
            for index in repetitions(2)
        )

    # Browser lifecycle and durable-queue cases use TokenRhythm Pro because it
    # is the production path that originally exposed the long-reasoning stall.
    for scenario in BROWSER_SCENARIOS:
        rows.extend(
            _case(
                "tokenrhythm",
                MODEL_MATRIX["tokenrhythm"][1],
                scenario,
                index,
            )
            for index in repetitions(1)
        )
    return rows


def select_cases(
    cases: Iterable[CaseSpec],
    *,
    providers: Iterable[str] = (),
    scenarios: Iterable[str] = (),
) -> list[CaseSpec]:
    provider_filter = frozenset(providers)
    scenario_filter = frozenset(scenarios)
    selected = [
        case
        for case in cases
        if (not provider_filter or case.provider in provider_filter)
        and (not scenario_filter or case.scenario in scenario_filter)
    ]
    if not selected:
        raise ValueError("provider/scenario selection produced no mandatory rows")
    return selected


def matrix_fingerprint(cases: Iterable[CaseSpec]) -> str:
    payload = [asdict(case) for case in cases]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _nonnegative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"driver field {field!r} must be a non-negative integer")
    return int(value)


def _bounded_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"driver field {field!r} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"driver field {field!r} must be finite and non-negative")
    return number


def _bounded_mapping(
    value: Any,
    *,
    allowed_keys: frozenset[str],
    field: str,
    integer: bool,
) -> dict[str, int] | dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError(f"driver field {field!r} must be an object")
    unknown = set(value) - allowed_keys
    if unknown:
        raise ValueError(f"driver field {field!r} contains unsupported metrics")
    if integer:
        return {
            str(key): _nonnegative_int(item, field=f"{field}.{key}") for key, item in value.items()
        }
    return {str(key): _bounded_number(item, field=f"{field}.{key}") for key, item in value.items()}


def parse_driver_result(value: Any, *, driver_exit_code: int = 0) -> DriverResult:
    if not isinstance(value, Mapping):
        raise ValueError("driver result must be a JSON object")
    allowed_fields = {
        "status",
        "stage",
        "physical_requests",
        "latency_ms",
        "usage",
        "cost",
        "counts",
        "metrics",
        "failure_class",
    }
    if set(value) - allowed_fields:
        raise ValueError("driver result contains unsupported fields")
    status = str(value.get("status") or "")
    if status not in _ALLOWED_STATUSES:
        raise ValueError("driver status must be passed, failed, skipped, or inconclusive")
    if driver_exit_code != EXIT_PASSED and status == "passed":
        raise ValueError("a non-zero case-driver exit cannot report passed")
    stage = str(value.get("stage") or "")
    if (
        not stage
        or len(stage) > 80
        or not all(character.isalnum() or character in "_-" for character in stage)
    ):
        raise ValueError("driver stage must be a short identifier")
    physical_requests = _nonnegative_int(
        value.get("physical_requests", 0),
        field="physical_requests",
    )
    if status == "passed" and physical_requests == 0:
        raise ValueError("a passed live case must account for at least one physical request")
    latency_ms = _nonnegative_int(value.get("latency_ms", 0), field="latency_ms")
    usage = _bounded_mapping(
        value.get("usage", {}),
        allowed_keys=_ALLOWED_USAGE_KEYS,
        field="usage",
        integer=True,
    )
    cost = _bounded_mapping(
        value.get("cost", {}),
        allowed_keys=_ALLOWED_COST_KEYS,
        field="cost",
        integer=False,
    )
    counts = _bounded_mapping(
        value.get("counts", {}),
        allowed_keys=_ALLOWED_COUNT_KEYS,
        field="counts",
        integer=True,
    )
    metrics = _bounded_mapping(
        value.get("metrics", {}),
        allowed_keys=_ALLOWED_METRIC_KEYS,
        field="metrics",
        integer=False,
    )
    failure_class_value = value.get("failure_class")
    failure_class = str(failure_class_value) if failure_class_value is not None else None
    if failure_class is not None and failure_class not in _ALLOWED_FAILURE_CLASSES:
        raise ValueError("driver failure_class is not in the public taxonomy")
    if status == "passed" and failure_class is not None:
        raise ValueError("a passed live case cannot report a failure_class")
    if physical_requests:
        if "total_tokens" not in usage:
            raise ValueError("a live case with requests must report usage.total_tokens")
        if "billed_cost_usd" not in cost:
            raise ValueError("a live case with requests must report cost.billed_cost_usd")
        if counts.get("provider_legs") != physical_requests:
            raise ValueError(
                "a live case must reconcile counts.provider_legs with physical_requests"
            )
    if status != "passed" and failure_class is None:
        failure_class = "unknown"
    return DriverResult(
        status=status,  # type: ignore[arg-type]
        stage=stage,
        physical_requests=physical_requests,
        latency_ms=latency_ms,
        usage=usage,  # type: ignore[arg-type]
        cost=cost,  # type: ignore[arg-type]
        counts=counts,  # type: ignore[arg-type]
        metrics=metrics,  # type: ignore[arg-type]
        failure_class=failure_class,
        driver_exit_code=driver_exit_code,
    )


def _validate_driver_result_object(result: DriverResult) -> DriverResult:
    """Apply the subprocess protocol invariants to every executor implementation."""

    return parse_driver_result(
        {
            "status": result.status,
            "stage": result.stage,
            "physical_requests": result.physical_requests,
            "latency_ms": result.latency_ms,
            "usage": result.usage,
            "cost": result.cost,
            "counts": result.counts,
            "metrics": result.metrics,
            "failure_class": result.failure_class,
        },
        driver_exit_code=result.driver_exit_code,
    )


def _driver_protocol_failure() -> DriverResult:
    return DriverResult(
        status="failed",
        stage="driver_protocol",
        physical_requests=0,
        latency_ms=0,
        usage={},
        cost={},
        counts={},
        metrics={},
        failure_class="implementation",
        driver_exit_code=EXIT_FAILED,
    )


def _scenario_assertion_failure(result: DriverResult) -> DriverResult:
    return replace(
        result,
        status="failed",
        stage="scenario_assertion",
        failure_class="assertion",
        driver_exit_code=EXIT_FAILED,
    )


def _require_count(result: DriverResult, key: str, minimum: int = 1) -> None:
    if int(result.counts.get(key, 0)) < minimum:
        raise ValueError(f"scenario evidence requires counts.{key} >= {minimum}")


def _require_metric_at_most(result: DriverResult, key: str, maximum: float) -> None:
    value = result.metrics.get(key)
    if value is None or float(value) > maximum:
        raise ValueError(f"scenario evidence requires metrics.{key} <= {maximum:g}")


def _require_metric_at_least(result: DriverResult, key: str, minimum: float) -> None:
    value = result.metrics.get(key)
    if value is None or float(value) < minimum:
        raise ValueError(f"scenario evidence requires metrics.{key} >= {minimum:g}")


def validate_scenario_evidence(case: CaseSpec, result: DriverResult) -> None:
    """Fail closed when a passed row omits its mandatory acceptance evidence."""

    if result.status != "passed":
        return
    if int(result.counts.get("accounted_provider_legs", -1)) != result.physical_requests:
        raise ValueError(
            "scenario evidence requires every physical provider leg in the durable usage ledger"
        )
    if int(result.counts.get("browser_failure_code", 0)) != 0:
        raise ValueError("a passed scenario cannot retain a browser failure code")
    scenario = case.scenario
    if scenario == "router":
        _require_count(result, "router_decisions")
    elif scenario == "long_reasoning":
        _require_count(result, "reasoning_pulses")
        _require_count(result, "activity_events")
        _require_metric_at_most(result, "activity_latency_ms", 1_000)
        _require_metric_at_most(result, "max_reasoning_pulse_gap_ms", 6_000)
    elif scenario == "tool_compaction":
        if int(result.counts.get("provider_legs", 0)) + int(result.counts.get("tool_legs", 0)) < 20:
            raise ValueError("scenario evidence requires at least 20 provider/tool legs")
        _require_count(result, "compactions")
    elif scenario == "long_answer":
        output_bytes = int(result.counts.get("output_bytes", 0))
        if not 16 * 1024 <= output_bytes <= 32 * 1024:
            raise ValueError("scenario evidence requires a 16-32 KiB answer")
        _require_count(result, "incremental_chunks", 2)
        _require_metric_at_most(result, "input_next_paint_p95_ms", 100)
        _require_metric_at_most(result, "input_next_paint_max_ms", 250)
        _require_metric_at_most(result, "max_main_thread_task_ms", 200)
        _require_metric_at_most(result, "peak_heap_delta_bytes", 48 * 1024 * 1024)
        _require_metric_at_most(result, "post_gc_heap_delta_bytes", 16 * 1024 * 1024)
        _require_metric_at_most(result, "post_gc_growth_bytes", 5 * 1024 * 1024)
        _require_metric_at_most(result, "anchor_drift_px", 2)
        _require_metric_at_most(result, "bottom_gap_px", 2)
        _require_metric_at_least(result, "markdown_parse_reduction_pct", 95)
        _require_metric_at_least(result, "recalc_style_reduction_pct", 70)
        _require_metric_at_least(result, "peak_heap_reduction_pct", 50)
        mounted_rows = result.counts.get("mounted_rows")
        if mounted_rows is None or int(mounted_rows) > 30:
            raise ValueError("scenario evidence requires counts.mounted_rows <= 30")
        dom_nodes = result.counts.get("dom_nodes")
        if dom_nodes is None or int(dom_nodes) > 15_000:
            raise ValueError("scenario evidence requires counts.dom_nodes <= 15000")
    elif scenario == "fault_429_retry_after":
        _require_count(result, "retry_legs")
        _require_count(result, "retry_after_honored")
        _require_metric_at_least(result, "retry_wait_ms", 8_000)
    elif scenario in {"fault_503", "fault_reset_before_first_token"}:
        _require_count(result, "retry_legs")
    elif scenario == "fault_partial_then_reset":
        _require_count(result, "incremental_chunks")
    elif scenario == "fault_reasoning_only":
        _require_count(result, "reasoning_pulses")
    elif scenario == "fault_late_terminal":
        _require_count(result, "incremental_chunks")
    elif scenario == "fallback":
        _require_count(result, "fallback_legs")
        _require_count(result, "fallback_before_request")
        if result.physical_requests < 2:
            raise ValueError("fallback evidence requires at least two physical requests")
    elif scenario in {
        "browser_refresh",
        "browser_websocket_interrupt",
        "browser_gateway_graceful_restart",
    }:
        _require_count(result, "subscription_recoveries")
        _require_count(result, "incremental_chunks")
        _require_metric_at_most(result, "subscription_recovery_ms", 2_000)
    elif scenario == "browser_gateway_forced_restart":
        _require_count(result, "subscription_recoveries")
        _require_count(result, "interruption_notices")
        _require_metric_at_most(result, "subscription_recovery_ms", 2_000)
    elif scenario == "browser_hidden_11_minutes":
        _require_metric_at_least(result, "hidden_duration_ms", 11 * 60 * 1_000)
    elif scenario == "browser_stop_each_phase":
        _require_count(result, "stop_phases", 4)
        _require_count(result, "cancelled_turns", 4)
    elif scenario.startswith("queue_"):
        _require_count(result, "queued_inputs")
        _require_count(result, "attachment_inputs")
        _require_count(result, "queue_exact_once")
        queued = int(result.counts.get("queued_inputs", 0))
        if int(result.counts.get("dispatched_inputs", 0)) != queued:
            raise ValueError("queue evidence must dispatch every staged input")
        if int(result.counts.get("transcript_occurrences", 0)) != queued:
            raise ValueError("queue evidence must commit each staged input exactly once")
        if int(result.counts.get("attachment_inputs", 0)) != queued:
            raise ValueError("queue evidence must commit each staged attachment exactly once")
        if int(result.counts.get("pending_inputs_remaining", -1)) != 0:
            raise ValueError("queue evidence must leave no dispatched pending input")


def _billed_tokens(result: DriverResult | Mapping[str, Any]) -> int:
    usage = result.usage if isinstance(result, DriverResult) else result.get("usage", {})
    if not isinstance(usage, Mapping):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool):
        return max(0, total)
    return sum(
        int(usage.get(key) or 0)
        for key in ("input_tokens", "output_tokens", "reasoning_tokens")
        if isinstance(usage.get(key, 0), int) and not isinstance(usage.get(key, 0), bool)
    )


def _billed_cost(result: DriverResult | Mapping[str, Any]) -> float:
    cost = result.cost if isinstance(result, DriverResult) else result.get("cost", {})
    if not isinstance(cost, Mapping):
        return 0.0
    billed = cost.get("billed_cost_usd")
    if isinstance(billed, int | float) and not isinstance(billed, bool):
        return max(0.0, float(billed))
    estimated = cost.get("estimated_cost_usd")
    if isinstance(estimated, int | float) and not isinstance(estimated, bool):
        return max(0.0, float(estimated))
    return 0.0


def _totals(rows: Iterable[Mapping[str, Any]], wall_time_ms: int) -> dict[str, Any]:
    materialized = list(rows)
    return {
        "wall_time_ms": max(0, wall_time_ms),
        "physical_requests": sum(int(row.get("physical_requests") or 0) for row in materialized),
        "billed_tokens": sum(_billed_tokens(row) for row in materialized),
        "billed_cost_usd": round(sum(_billed_cost(row) for row in materialized), 10),
    }


def _remaining(limits: BudgetLimits, totals: Mapping[str, Any]) -> RemainingBudget:
    return RemainingBudget(
        wall_ms=max(0, limits.wall_seconds * 1000 - int(totals.get("wall_time_ms") or 0)),
        billed_cost_usd=max(
            0.0,
            limits.billed_cost_usd - float(totals.get("billed_cost_usd") or 0),
        ),
        physical_requests=max(
            0,
            limits.physical_requests - int(totals.get("physical_requests") or 0),
        ),
        billed_tokens=max(
            0,
            limits.billed_tokens - int(totals.get("billed_tokens") or 0),
        ),
    )


def _is_exhausted(remaining: RemainingBudget) -> bool:
    return (
        remaining.wall_ms <= 0
        or remaining.billed_cost_usd <= 0
        or remaining.physical_requests <= 0
        or remaining.billed_tokens <= 0
    )


def _limits_exceeded(limits: BudgetLimits, totals: Mapping[str, Any]) -> bool:
    return (
        int(totals.get("wall_time_ms") or 0) > limits.wall_seconds * 1000
        or float(totals.get("billed_cost_usd") or 0) > limits.billed_cost_usd
        or int(totals.get("physical_requests") or 0) > limits.physical_requests
        or int(totals.get("billed_tokens") or 0) > limits.billed_tokens
    )


def _latest_rows(rows: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    latest: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "")
        previous = latest.get(case_id)
        run_attempt = int(row.get("run_attempt") or 0)
        previous_attempt = int(previous.get("run_attempt") or 0) if previous else -1
        if case_id and run_attempt > previous_attempt:
            latest[case_id] = row
    return latest


def _summary(
    cases: list[CaseSpec],
    rows: list[dict[str, Any]],
    *,
    budget_exhausted: bool,
) -> dict[str, Any]:
    latest = _latest_rows(rows)
    statuses = [str(latest.get(case.case_id, {}).get("status") or "pending") for case in cases]
    passed = sum(status == "passed" for status in statuses)
    failed = sum(status == "failed" for status in statuses)
    skipped = sum(status == "skipped" for status in statuses)
    inconclusive = sum(status == "inconclusive" for status in statuses)
    pending = sum(status == "pending" for status in statuses)
    return {
        "status": (
            "budget_exhausted"
            if budget_exhausted and passed != len(cases)
            else ("passed" if passed == len(cases) else "failed")
        ),
        "required": len(cases),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "inconclusive": inconclusive,
        "pending": pending,
    }


def _result_row(case: CaseSpec, result: DriverResult, run_attempt: int) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "provider": case.provider,
        "model": case.model,
        "scenario": case.scenario,
        "fallback_provider": case.fallback_provider,
        "repeat_index": case.repeat_index,
        "run_attempt": run_attempt,
        "status": result.status,
        "stage": result.stage,
        "failure_class": result.failure_class,
        "latency_ms": result.latency_ms,
        "physical_requests": result.physical_requests,
        "usage": dict(result.usage),
        "cost": dict(result.cost),
        "counts": dict(result.counts),
        "metrics": dict(result.metrics),
    }


def _build_report(
    cases: list[CaseSpec],
    rows: list[dict[str, Any]],
    limits: BudgetLimits,
    *,
    wall_time_ms: int,
    budget_exhausted: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "matrix_fingerprint": matrix_fingerprint(cases),
        "limits": asdict(limits),
        "totals": _totals(rows, wall_time_ms),
        "summary": _summary(cases, rows, budget_exhausted=budget_exhausted),
        "rows": [dict(row) for row in rows],
    }


def _skipped_unavailable_row(case: CaseSpec, run_attempt: int) -> dict[str, Any]:
    return _result_row(
        case,
        DriverResult(
            status="skipped",
            stage="provider_preflight",
            physical_requests=0,
            latency_ms=0,
            usage={},
            cost={},
            counts={},
            metrics={},
            failure_class="provider-unavailable",
        ),
        run_attempt,
    )


def run_gate(
    cases: list[CaseSpec],
    *,
    executor: CaseExecutor,
    limits: BudgetLimits = BudgetLimits(),
    resumed_rows: Iterable[Mapping[str, Any]] = (),
    resumed_wall_time_ms: int = 0,
    checkpoint: CheckpointWriter | None = None,
) -> tuple[dict[str, Any], int]:
    """Run selected mandatory rows with deterministic exit-code precedence."""

    limits.validate()
    rows = [dict(row) for row in resumed_rows]
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("mandatory case ids must be unique")
    selected_ids = {case.case_id for case in cases}
    if any(str(row.get("case_id") or "") not in selected_ids for row in rows):
        raise ValueError("resume report contains rows outside the selected matrix")

    started = time.monotonic()
    previous_attempts: dict[str, int] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "")
        previous_attempts[case_id] = max(
            previous_attempts.get(case_id, 0),
            int(row.get("run_attempt") or 0),
        )
    latest = _latest_rows(rows)
    unavailable_providers: set[str] = set()
    configuration_error = False
    budget_exhausted = False

    def write_checkpoint() -> None:
        if checkpoint is None:
            return
        current_wall_time_ms = resumed_wall_time_ms + int((time.monotonic() - started) * 1000)
        checkpoint(
            _build_report(
                cases,
                rows,
                limits,
                wall_time_ms=current_wall_time_ms,
                budget_exhausted=budget_exhausted,
            )
        )

    for case in cases:
        if latest.get(case.case_id, {}).get("status") == "passed":
            continue
        run_attempt = previous_attempts.get(case.case_id, 0) + 1
        if case.provider in unavailable_providers or (
            case.fallback_provider is not None and case.fallback_provider in unavailable_providers
        ):
            row = _skipped_unavailable_row(case, run_attempt)
            rows.append(row)
            latest[case.case_id] = row
            write_checkpoint()
            continue

        wall_time_ms = resumed_wall_time_ms + int((time.monotonic() - started) * 1000)
        totals = _totals(rows, wall_time_ms)
        remaining = _remaining(limits, totals)
        if _is_exhausted(remaining):
            budget_exhausted = True
            break

        try:
            result = _validate_driver_result_object(executor(case, remaining))
        except (TypeError, ValueError):
            result = _driver_protocol_failure()
        try:
            validate_scenario_evidence(case, result)
        except ValueError:
            result = _scenario_assertion_failure(result)
        row = _result_row(case, result, run_attempt)
        rows.append(row)
        latest[case.case_id] = row
        if result.driver_exit_code == EXIT_CONFIGURATION or result.failure_class in {
            "auth",
            "balance",
            "configuration",
            "missing-credential",
            "model-unavailable",
            "not-entitled",
        }:
            configuration_error = True
            unavailable_providers.add(case.provider)
        elif result.failure_class == "provider-unavailable":
            # Keep collecting independent providers, but do not classify an
            # upstream outage as an operator/configuration error (exit 2).
            unavailable_providers.add(case.provider)
        if result.driver_exit_code == EXIT_BUDGET or result.failure_class == "budget":
            budget_exhausted = True
        write_checkpoint()
        if budget_exhausted:
            break

        wall_time_ms = resumed_wall_time_ms + int((time.monotonic() - started) * 1000)
        if _is_exhausted(_remaining(limits, _totals(rows, wall_time_ms))):
            budget_exhausted = True
            break

    latest = _latest_rows(rows)
    pending_required = any(case.case_id not in latest for case in cases)
    explicit_budget_row = any(
        str(row.get("failure_class") or "") == "budget" for row in latest.values()
    )
    # Reaching an exact hard limit after the final mandatory row is legal. It
    # is exit 3 only when work remains or a case itself ended inconclusively on
    # budget; a completed assertion failure remains exit 1.
    budget_exhausted = budget_exhausted and (pending_required or explicit_budget_row)
    wall_time_ms = resumed_wall_time_ms + int((time.monotonic() - started) * 1000)
    report = _build_report(
        cases,
        rows,
        limits,
        wall_time_ms=wall_time_ms,
        budget_exhausted=budget_exhausted,
    )
    summary = report["summary"]
    if configuration_error:
        return report, EXIT_CONFIGURATION
    if _limits_exceeded(limits, report["totals"]):
        summary["status"] = "budget_exhausted"
        return report, EXIT_BUDGET
    if summary["status"] == "passed":
        return report, EXIT_PASSED
    if budget_exhausted:
        return report, EXIT_BUDGET
    return report, EXIT_FAILED


def _validate_driver_path(path: Path) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise ValueError("case driver does not exist") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ValueError("case driver must be a regular file, not a link")
    resolved = path.resolve(strict=True)
    allowed = False
    for root in (REPO_ROOT.resolve(), Path(tempfile.gettempdir()).resolve()):
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        allowed = True
        break
    if not allowed:
        raise ValueError("case driver must be inside the repository or system temporary directory")
    if resolved.suffix != ".py":
        raise ValueError("case driver must be a Python script")
    return resolved


class SubprocessCaseExecutor:
    def __init__(
        self,
        *,
        driver: Path,
        secrets: Mapping[str, str],
        performance_reports: Mapping[str, Path] | None = None,
    ) -> None:
        self._driver = _validate_driver_path(driver)
        self._secrets = {
            name: str(value)
            for name, value in secrets.items()
            if name in CREDENTIAL_ENV_BY_PROVIDER.values() and value
        }
        self._performance_reports = {
            name: Path(path)
            for name, path in (performance_reports or {}).items()
            if name in PERFORMANCE_REPORT_ENV
        }

    def __call__(self, case: CaseSpec, remaining: RemainingBudget) -> DriverResult:
        temporary = Path(tempfile.mkdtemp(prefix="opensquilla-long-task-case-"))
        case_path = temporary / "case.json"
        result_path = temporary / "result.json"
        try:
            case_payload = {
                "schema_version": 1,
                "case_id": case.case_id,
                "provider": case.provider,
                "model": case.model,
                "scenario": case.scenario,
                "repeat_index": case.repeat_index,
                "fallback_provider": case.fallback_provider,
                "remaining_budget": asdict(remaining),
            }
            case_path.write_text(
                json.dumps(case_payload, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.chmod(case_path, 0o600)

            env = child_environment(case.provider, self._secrets)
            if case.fallback_provider:
                fallback_spec = get_provider_spec(case.fallback_provider)
                fallback_secret = self._secrets.get(fallback_spec.env_key, "")
                if fallback_secret:
                    env[fallback_spec.env_key] = fallback_secret
            env.update(
                {
                    "OPENSQUILLA_LONG_TASK_LIVE": "1",
                    "OPENSQUILLA_LIVE_DISABLE_DOTENV": "1",
                    "PYTHONPATH": os.pathsep.join((str(REPO_ROOT), str(SRC_DIR))),
                }
            )
            for name, path in self._performance_reports.items():
                env[PERFORMANCE_REPORT_ENV[name]] = str(path)
            global_timeout_seconds = max(0.001, remaining.wall_ms / 1000)
            timeout_seconds = min(global_timeout_seconds, MAX_CASE_WALL_SECONDS)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(self._driver),
                    "--case-file",
                    str(case_path),
                    "--output",
                    str(result_path),
                ],
                cwd=REPO_ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            secret_values = tuple(self._secrets.values())
            if any(
                secret and secret in output
                for secret in secret_values
                for output in (completed.stdout, completed.stderr)
            ):
                raise RuntimeError("credential detected in case-driver output")
            if completed.returncode == EXIT_INTERRUPTED:
                raise KeyboardInterrupt
            if completed.returncode == EXIT_BUDGET and not result_path.exists():
                return DriverResult(
                    status="inconclusive",
                    stage="budget",
                    physical_requests=0,
                    latency_ms=0,
                    usage={},
                    cost={},
                    counts={},
                    metrics={},
                    failure_class="budget",
                    driver_exit_code=EXIT_BUDGET,
                )
            if not result_path.exists():
                failure = classify_failure(redact_text(completed.stderr, secret_values))
                if completed.returncode == EXIT_CONFIGURATION:
                    failure = "configuration"
                return DriverResult(
                    status="failed",
                    stage="driver",
                    physical_requests=0,
                    latency_ms=0,
                    usage={},
                    cost={},
                    counts={},
                    metrics={},
                    failure_class=failure,
                    driver_exit_code=completed.returncode,
                )
            try:
                result_mode = result_path.lstat().st_mode
                if stat.S_ISLNK(result_mode) or not stat.S_ISREG(result_mode):
                    raise ValueError("case-driver result must be a regular file, not a link")
                if result_path.stat().st_size > 1024 * 1024:
                    raise ValueError("case-driver result exceeds 1 MiB")
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                return parse_driver_result(payload, driver_exit_code=completed.returncode)
            except (json.JSONDecodeError, OSError, ValueError):
                # A malformed result is an implementation/protocol failure, not
                # a credential/model configuration failure.  Never include the
                # result contents or parser exception in the public report.
                return _driver_protocol_failure()
        except subprocess.TimeoutExpired:
            global_budget_expired = remaining.wall_ms <= MAX_CASE_WALL_SECONDS * 1000
            return DriverResult(
                status="inconclusive",
                stage="budget" if global_budget_expired else "case_timeout",
                physical_requests=0,
                latency_ms=min(remaining.wall_ms, MAX_CASE_WALL_SECONDS * 1000),
                usage={},
                cost={},
                counts={},
                metrics={},
                failure_class="budget" if global_budget_expired else "inconclusive",
                driver_exit_code=EXIT_BUDGET if global_budget_expired else EXIT_FAILED,
            )
        finally:
            scan_and_remove_temporary_tree(temporary, self._secrets)


def _load_resume(path: Path, cases: list[CaseSpec]) -> tuple[list[dict[str, Any]], int]:
    if not is_temporary_report_path(path):
        raise ValueError("resume report must be inside the system temporary directory")
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise ValueError("resume report must be a regular file, not a link")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("resume report exceeds 16 MiB")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ValueError("resume report has an unsupported schema")
    if payload.get("matrix_fingerprint") != matrix_fingerprint(cases):
        raise ValueError("resume report does not match the selected matrix")
    rows = payload.get("rows")
    totals = payload.get("totals")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("resume report rows are invalid")
    if not isinstance(totals, Mapping):
        raise ValueError("resume report totals are invalid")
    wall_time_ms = _nonnegative_int(totals.get("wall_time_ms", 0), field="wall_time_ms")
    # Re-project every row through the public shape used by run_gate. Unknown
    # fields are rejected so prompts or responses cannot be smuggled forward.
    expected_fields = {
        "case_id",
        "provider",
        "model",
        "scenario",
        "fallback_provider",
        "repeat_index",
        "run_attempt",
        "status",
        "stage",
        "failure_class",
        "latency_ms",
        "physical_requests",
        "usage",
        "cost",
        "counts",
        "metrics",
    }
    case_by_id = {case.case_id: case for case in cases}
    attempts: set[tuple[str, int]] = set()
    validated: list[dict[str, Any]] = []
    for row in rows:
        if set(row) != expected_fields:
            raise ValueError("resume report contains non-public row fields")
        case_id = str(row.get("case_id") or "")
        case = case_by_id.get(case_id)
        if case is None:
            raise ValueError("resume report contains an unknown case")
        identity = (
            row.get("provider"),
            row.get("model"),
            row.get("scenario"),
            row.get("fallback_provider"),
            row.get("repeat_index"),
        )
        expected_identity = (
            case.provider,
            case.model,
            case.scenario,
            case.fallback_provider,
            case.repeat_index,
        )
        if identity != expected_identity:
            raise ValueError("resume report case identity does not match the matrix")
        run_attempt = _nonnegative_int(row.get("run_attempt"), field="run_attempt")
        if run_attempt == 0 or (case_id, run_attempt) in attempts:
            raise ValueError("resume report has an invalid or duplicate run attempt")
        attempts.add((case_id, run_attempt))
        result = parse_driver_result(
            {
                "status": row.get("status"),
                "stage": row.get("stage"),
                "physical_requests": row.get("physical_requests"),
                "latency_ms": row.get("latency_ms"),
                "usage": row.get("usage"),
                "cost": row.get("cost"),
                "counts": row.get("counts"),
                "metrics": row.get("metrics"),
                "failure_class": row.get("failure_class"),
            }
        )
        validate_scenario_evidence(case, result)
        validated.append(_result_row(case, result, run_attempt))
    if wall_time_ms < sum(int(row["latency_ms"]) for row in validated):
        raise ValueError("resume report understates elapsed case time")
    return validated, wall_time_ms


def _missing_credentials(cases: Iterable[CaseSpec], secrets: Mapping[str, str]) -> set[str]:
    providers: set[str] = set()
    for case in cases:
        providers.add(case.provider)
        if case.fallback_provider:
            providers.add(case.fallback_provider)
    return {
        provider
        for provider in providers
        if not secrets.get(CREDENTIAL_ENV_BY_PROVIDER[provider], "")
    }


def _configuration_report(
    cases: list[CaseSpec],
    limits: BudgetLimits,
    *,
    failure_class: str,
) -> dict[str, Any]:
    rows = [
        _result_row(
            case,
            DriverResult(
                status="skipped",
                stage="configuration",
                physical_requests=0,
                latency_ms=0,
                usage={},
                cost={},
                counts={},
                metrics={},
                failure_class=failure_class,
                driver_exit_code=EXIT_CONFIGURATION,
            ),
            1,
        )
        for case in cases
    ]
    return {
        "schema_version": 1,
        "matrix_fingerprint": matrix_fingerprint(cases),
        "limits": asdict(limits),
        "totals": _totals(rows, 0),
        "summary": _summary(cases, rows, budget_exhausted=False),
        "rows": rows,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--driver", type=Path, default=DEFAULT_CASE_DRIVER)
    parser.add_argument(
        "--provider",
        action="append",
        choices=tuple(CREDENTIAL_ENV_BY_PROVIDER),
        default=[],
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=ALL_SCENARIOS,
        default=[],
    )
    parser.add_argument("--repeat", type=int)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--max-wall-seconds", type=int, default=4 * 60 * 60)
    parser.add_argument("--max-cost-usd", type=float, default=30.0)
    parser.add_argument("--max-requests", type=int, default=120)
    parser.add_argument("--max-billed-tokens", type=int, default=1_000_000)
    parser.add_argument(
        "--perf-baseline",
        type=Path,
        help="absolute system-temp JSON from baseline long-task characterization",
    )
    parser.add_argument(
        "--perf-candidate",
        type=Path,
        help="absolute system-temp JSON from candidate long-task characterization",
    )
    parser.add_argument(
        "--perf-resilience",
        type=Path,
        help="absolute system-temp JSON from candidate long-task resilience",
    )
    args = parser.parse_args(argv)
    if not is_temporary_report_path(args.output):
        parser.error("--output must be inside the system temporary directory")
    if args.resume is not None and not is_temporary_report_path(args.resume):
        parser.error("--resume must be inside the system temporary directory")
    for option, path in (
        ("--perf-baseline", args.perf_baseline),
        ("--perf-candidate", args.perf_candidate),
        ("--perf-resilience", args.perf_resilience),
    ):
        if path is not None and (not path.is_absolute() or not is_temporary_report_path(path)):
            parser.error(f"{option} must be an absolute path inside the system temporary directory")
    if args.repeat is not None and not 1 <= args.repeat <= 10:
        parser.error("--repeat must be between 1 and 10")
    try:
        BudgetLimits(
            wall_seconds=args.max_wall_seconds,
            billed_cost_usd=args.max_cost_usd,
            physical_requests=args.max_requests,
            billed_tokens=args.max_billed_tokens,
        ).validate()
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    limits = BudgetLimits(
        wall_seconds=args.max_wall_seconds,
        billed_cost_usd=args.max_cost_usd,
        physical_requests=args.max_requests,
        billed_tokens=args.max_billed_tokens,
    )
    cases = select_cases(
        build_mandatory_matrix(repeat_override=args.repeat),
        providers=args.provider,
        scenarios=args.scenario,
    )
    secrets = {
        env_name: os.environ.get(env_name, "")
        for env_name in CREDENTIAL_ENV_BY_PROVIDER.values()
        if os.environ.get(env_name)
    }
    output = args.output
    report: dict[str, Any]
    exit_code: int
    minimum_requests = minimum_physical_requests(cases)

    def persist(candidate: dict[str, Any]) -> None:
        safe_report = sanitize_report(candidate, secrets)
        if report_contains_secret(safe_report, secrets):
            raise RuntimeError("refusing to write a report containing credentials")
        write_safe_report(output, safe_report, secrets)

    try:
        if limits.physical_requests < minimum_requests:
            report = _configuration_report(cases, limits, failure_class="configuration")
            report["preflight"] = {
                "minimum_physical_requests": minimum_requests,
                "configured_physical_requests": limits.physical_requests,
                "budget_reachable": False,
            }
            exit_code = EXIT_CONFIGURATION
        elif _missing_credentials(cases, secrets):
            report = _configuration_report(cases, limits, failure_class="missing-credential")
            report["preflight"] = {
                "minimum_physical_requests": minimum_requests,
                "configured_physical_requests": limits.physical_requests,
                "budget_reachable": True,
            }
            exit_code = EXIT_CONFIGURATION
        else:
            resumed_rows: list[dict[str, Any]] = []
            resumed_wall_time_ms = 0
            if args.resume is not None:
                resumed_rows, resumed_wall_time_ms = _load_resume(args.resume, cases)
            performance_reports = {
                name: path
                for name, path in {
                    "baseline": args.perf_baseline,
                    "candidate": args.perf_candidate,
                    "resilience": args.perf_resilience,
                }.items()
                if path is not None
            }
            executor = SubprocessCaseExecutor(
                driver=args.driver,
                secrets=secrets,
                performance_reports=performance_reports,
            )
            report, exit_code = run_gate(
                cases,
                executor=executor,
                limits=limits,
                resumed_rows=resumed_rows,
                resumed_wall_time_ms=resumed_wall_time_ms,
                checkpoint=persist,
            )
            report["preflight"] = {
                "minimum_physical_requests": minimum_requests,
                "configured_physical_requests": limits.physical_requests,
                "budget_reachable": True,
            }
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except (json.JSONDecodeError, OSError, RuntimeError, ValueError) as exc:
        print(
            redact_text(f"long-task live gate configuration failed: {exc}", secrets),
            file=sys.stderr,
        )
        return EXIT_CONFIGURATION

    try:
        persist(report)
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            redact_text(f"unable to write long-task live report: {exc}", secrets),
            file=sys.stderr,
        )
        return EXIT_CONFIGURATION

    diagnostic = {
        "status": report["summary"]["status"],
        "required": report["summary"]["required"],
        "passed": report["summary"]["passed"],
        "failed": report["summary"]["failed"],
        "skipped": report["summary"]["skipped"],
        "inconclusive": report["summary"]["inconclusive"],
        "pending": report["summary"]["pending"],
        "physical_requests": report["totals"]["physical_requests"],
        "billed_tokens": report["totals"]["billed_tokens"],
        "billed_cost_usd": report["totals"]["billed_cost_usd"],
    }
    print("long-task live gate: " + json.dumps(diagnostic, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
