"""Measurement-only ensemble benchmark harness.

Runs a set of prompts through two ``LLMProvider`` arms — typically the static
B5 ensemble versus a single-model baseline — and reports per-run latency,
success/failure (classified through the shared provider failure taxonomy),
ensemble member/proposer counts, and an optional cost estimate. It is purely
observational: it constructs providers from the caller, invokes their public
``chat`` surface as a black box, times the outer call, and records outcomes. It
never touches ensemble internals — ensemble member/proposer success counts are
read only from the public ``DoneEvent.ensemble_trace`` the ensemble already
emits.

The harness is offline- and test-friendly: the clock is injectable (deterministic
latencies), the price lookup is injectable (deterministic cost math), and a
:class:`~opensquilla.provider.types.FailureInjector` can script provider
outcomes so the default path needs no network or credentials.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.engine.pricing import (
    ModelPrice,
    estimate_cost,
    resolve_model_price,
)
from opensquilla.provider.failures import ProviderFailureKind, classify_provider_error
from opensquilla.provider.protocol import provider_metadata
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    FailureInjector,
    Message,
)

if TYPE_CHECKING:
    from opensquilla.provider.protocol import LLMProvider

log = structlog.get_logger(__name__)

# A monotonic-ish clock: returns seconds. Injectable so tests pin latency.
Clock = Callable[[], float]
# Maps a model id to per-token pricing, or ``None`` when unknown. Injectable so
# tests exercise the cost column against a stub instead of live pricing.
PriceLookup = Callable[[str], "ModelPrice | None"]


@dataclass(frozen=True)
class BenchmarkPrompt:
    """One synthetic prompt to run through both arms.

    ``text`` is generic public-dummy content; never a real user prompt.
    """

    id: str
    text: str
    system: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    """Result of one prompt run through one provider arm (black-box)."""

    arm: str
    prompt_id: str
    run_index: int
    ok: bool
    latency_s: float
    failure_kind: str | None
    error_code: str
    error_message: str
    input_tokens: int
    output_tokens: int
    billed_cost: float
    cost_source: str
    estimated_cost_usd: float | None
    # Public-surface ensemble reads (``None`` when the run carried no trace).
    successful_proposers: int | None
    total_candidates: int | None
    fallback_used: bool | None
    ensemble_mode: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArmReport:
    """Aggregate metrics for one arm over all its runs (pure)."""

    label: str
    runs: int
    successes: int
    failures: int
    success_rate: float
    mean_latency_s: float
    p95_latency_s: float
    total_input_tokens: int
    total_output_tokens: int
    total_billed_cost: float
    total_estimated_cost_usd: float | None
    failure_kinds: dict[str, int]
    # Ensemble aggregates — ``None`` when no run carried an ensemble trace.
    mean_successful_proposers: float | None
    mean_total_candidates: float | None
    fallback_runs: int | None
    outcomes: list[RunOutcome] = field(default_factory=list)

    def to_dict(self, *, include_outcomes: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "label": self.label,
            "runs": self.runs,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": self.success_rate,
            "mean_latency_s": self.mean_latency_s,
            "p95_latency_s": self.p95_latency_s,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_billed_cost": self.total_billed_cost,
            "total_estimated_cost_usd": self.total_estimated_cost_usd,
            "failure_kinds": dict(self.failure_kinds),
            "mean_successful_proposers": self.mean_successful_proposers,
            "mean_total_candidates": self.mean_total_candidates,
            "fallback_runs": self.fallback_runs,
        }
        if include_outcomes:
            payload["outcomes"] = [outcome.to_dict() for outcome in self.outcomes]
        return payload


@dataclass(frozen=True)
class BenchmarkReport:
    """Ensemble-vs-baseline comparison with ensemble-minus-baseline deltas."""

    ensemble: ArmReport
    baseline: ArmReport
    latency_delta_s: float
    latency_ratio: float | None
    success_rate_delta: float
    billed_cost_delta_usd: float
    billed_cost_ratio: float | None
    estimated_cost_delta_usd: float | None
    estimated_cost_ratio: float | None

    def to_dict(self, *, include_outcomes: bool = True) -> dict[str, Any]:
        return {
            "ensemble": self.ensemble.to_dict(include_outcomes=include_outcomes),
            "baseline": self.baseline.to_dict(include_outcomes=include_outcomes),
            "deltas": {
                "latency_delta_s": self.latency_delta_s,
                "latency_ratio": self.latency_ratio,
                "success_rate_delta": self.success_rate_delta,
                "billed_cost_delta_usd": self.billed_cost_delta_usd,
                "billed_cost_ratio": self.billed_cost_ratio,
                "estimated_cost_delta_usd": self.estimated_cost_delta_usd,
                "estimated_cost_ratio": self.estimated_cost_ratio,
            },
        }


# ---------------------------------------------------------------------------
# Public price lookup (live path) — reads pricing.py, never mutates it.
# ---------------------------------------------------------------------------


def pricing_price_lookup(model_id: str, provider: str = "") -> ModelPrice | None:
    """Per-token price for ``model_id`` from :mod:`opensquilla.engine.pricing`.

    A read-only adapter over the layered, provider-aware price resolver into
    the legacy per-token :class:`ModelPrice` shape accepted by injected test
    lookups. The built-in benchmark path recognizes this adapter and uses the
    full four-bucket estimator directly. This is a separate measurement
    estimate; it deliberately does not touch the router's savings calculation.
    """
    model = (model_id or "").strip()
    if not model:
        return None
    entry = resolve_model_price(model, provider).entry
    return ModelPrice(
        input_per_token=entry.input_per_m / 1_000_000,
        output_per_token=entry.output_per_m / 1_000_000,
    )


# ---------------------------------------------------------------------------
# Black-box run + outcome classification
# ---------------------------------------------------------------------------


def _provider_name(provider: Any) -> str:
    name = getattr(provider, "provider_name", "") or ""
    if name:
        return str(name)
    return provider_metadata(provider).provider_name


def _classify_error_event(event: ErrorEvent, provider_name: str) -> ProviderFailureKind:
    """Map a terminal ``ErrorEvent`` back to a failure kind (round-trips the
    synthetic injector shapes for a registered provider family)."""
    code = (event.code or "").strip()
    status_code = int(code) if code.isdigit() else None
    raw_code = "" if status_code is not None else code
    return classify_provider_error(provider_name, status_code, raw_code, event.message or "")


def _read_ensemble_trace(
    done: DoneEvent | None,
) -> tuple[int | None, int | None, bool | None, str | None]:
    """Read member/proposer counts from the public ``ensemble_trace`` only."""
    trace = getattr(done, "ensemble_trace", None) if done is not None else None
    if not isinstance(trace, dict):
        return None, None, None, None
    successful = trace.get("successful_proposers")
    total = trace.get("total_candidates")
    fallback = trace.get("fallback_used")
    mode = trace.get("mode")
    return (
        int(successful) if isinstance(successful, int) else None,
        int(total) if isinstance(total, int) else None,
        bool(fallback) if isinstance(fallback, bool) else None,
        str(mode) if isinstance(mode, str) else None,
    )


def _config_for_prompt(base: ChatConfig | None, prompt: BenchmarkPrompt) -> ChatConfig:
    config = base.model_copy(deep=True) if base is not None else ChatConfig()
    if prompt.system is not None:
        config = config.model_copy(update={"system": prompt.system})
    return config


def _estimate_cost(
    price_lookup: PriceLookup | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    provider: str = "",
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    if price_lookup is None or not model:
        return None
    if price_lookup is pricing_price_lookup:
        return estimate_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            price=resolve_model_price(model, provider).entry,
        ).cost_usd
    price = price_lookup(model)
    if price is None:
        return None
    return input_tokens * price.input_per_token + output_tokens * price.output_per_token


def _estimate_done_cost(
    price_lookup: PriceLookup | None,
    done: DoneEvent,
    *,
    provider_hint: str,
    model_hint: str,
) -> float | None:
    rows = done.model_usage_breakdown
    if isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows):
        estimates: list[float] = []
        for row in rows:
            estimated = _estimate_cost(
                price_lookup,
                str(row.get("model") or model_hint),
                int(row.get("input_tokens") or 0),
                int(row.get("output_tokens") or 0),
                provider=str(row.get("provider") or provider_hint),
                cache_read_tokens=int(
                    row.get("cache_read_tokens") or row.get("cached_tokens") or 0
                ),
                cache_write_tokens=int(row.get("cache_write_tokens") or 0),
            )
            if estimated is None:
                return None
            estimates.append(estimated)
        return sum(estimates)
    return _estimate_cost(
        price_lookup,
        done.model or model_hint,
        done.input_tokens,
        done.output_tokens,
        provider=done.provider or provider_hint,
        cache_read_tokens=done.cached_tokens,
        cache_write_tokens=done.cache_write_tokens,
    )


async def run_single(
    provider: LLMProvider,
    prompt: BenchmarkPrompt,
    *,
    arm: str,
    run_index: int = 0,
    base_config: ChatConfig | None = None,
    clock: Clock = time.monotonic,
    price_lookup: PriceLookup | None = None,
    injector: FailureInjector | None = None,
    model_hint: str = "",
    provider_hint: str = "",
) -> RunOutcome:
    """Run one prompt through one provider as a black box and classify it.

    Times the whole outer ``chat`` call, consumes the stream, and derives the
    outcome from the terminal event: a ``DoneEvent`` is success; an
    ``ErrorEvent`` (or a raised exception) is a classified failure. Token and
    cost figures come only from the ``DoneEvent`` the provider already emits.
    """
    config = _config_for_prompt(base_config, prompt)
    messages = [Message(role="user", content=prompt.text)]
    name = _provider_name(provider)

    done: DoneEvent | None = None
    error_event: ErrorEvent | None = None
    raised: Exception | None = None

    start = clock()
    try:
        stream: Any
        if injector is not None:
            stream = injector.chat(provider, messages, config=config)
        else:
            stream = provider.chat(messages, config=config)
        async for event in stream:
            if isinstance(event, DoneEvent):
                done = event
            elif isinstance(event, ErrorEvent):
                error_event = event
                break
    except Exception as exc:  # noqa: BLE001 - provider boundary is recorded, not raised
        raised = exc
    finally:
        end = clock()
    latency_s = max(0.0, end - start)

    if error_event is not None:
        ok = False
        failure_kind: str | None = _classify_error_event(error_event, name).value
        error_code = error_event.code or ""
        error_message = error_event.message or ""
    elif raised is not None:
        ok = False
        failure_kind = classify_provider_error(name, None, "", str(raised)).value
        error_code = ""
        error_message = str(raised)
    elif done is not None:
        ok = True
        failure_kind = None
        error_code = ""
        error_message = ""
    else:
        ok = False
        failure_kind = classify_provider_error(
            name, None, "", "stream ended before a terminal event"
        ).value
        error_code = ""
        error_message = "stream ended before a terminal event"

    input_tokens = done.input_tokens if done is not None else 0
    output_tokens = done.output_tokens if done is not None else 0
    billed_cost = done.billed_cost if done is not None else 0.0
    cost_source = done.cost_source if done is not None else "none"
    successful, total, fallback, mode = _read_ensemble_trace(done)
    priced_model = (done.model if done is not None and done.model else "") or model_hint
    estimated = (
        _estimate_done_cost(
            price_lookup,
            done,
            provider_hint=provider_hint or name,
            model_hint=priced_model,
        )
        if done is not None
        else None
    )

    return RunOutcome(
        arm=arm,
        prompt_id=prompt.id,
        run_index=run_index,
        ok=ok,
        latency_s=latency_s,
        failure_kind=failure_kind,
        error_code=error_code,
        error_message=error_message,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        billed_cost=billed_cost,
        cost_source=cost_source,
        estimated_cost_usd=estimated,
        successful_proposers=successful,
        total_candidates=total,
        fallback_used=fallback,
        ensemble_mode=mode,
    )


async def run_arm(
    provider: LLMProvider,
    prompts: Sequence[BenchmarkPrompt],
    *,
    arm: str,
    repeat: int = 1,
    base_config: ChatConfig | None = None,
    clock: Clock = time.monotonic,
    price_lookup: PriceLookup | None = None,
    injector: FailureInjector | None = None,
    model_hint: str = "",
    provider_hint: str = "",
) -> list[RunOutcome]:
    """Run every prompt (``repeat`` times each) through one provider arm."""
    outcomes: list[RunOutcome] = []
    passes = max(1, int(repeat))
    for prompt in prompts:
        for run_index in range(passes):
            outcomes.append(
                await run_single(
                    provider,
                    prompt,
                    arm=arm,
                    run_index=run_index,
                    base_config=base_config,
                    clock=clock,
                    price_lookup=price_lookup,
                    injector=injector,
                    model_hint=model_hint,
                    provider_hint=provider_hint,
                )
            )
    return outcomes


# ---------------------------------------------------------------------------
# Aggregation (pure)
# ---------------------------------------------------------------------------


def _percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile; ``0.0`` for an empty sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = math.ceil((pct / 100.0) * len(ordered))
    index = min(max(rank - 1, 0), len(ordered) - 1)
    return ordered[index]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def aggregate_arm(label: str, runs: Sequence[RunOutcome]) -> ArmReport:
    """Aggregate a list of run outcomes into one arm report (pure)."""
    total = len(runs)
    successes = sum(1 for run in runs if run.ok)
    failures = total - successes
    latencies = [run.latency_s for run in runs]
    failure_kinds: Counter[str] = Counter()
    for run in runs:
        if not run.ok and run.failure_kind:
            failure_kinds[run.failure_kind] += 1
    estimates = [run.estimated_cost_usd for run in runs if run.estimated_cost_usd is not None]
    total_estimated = sum(estimates) if estimates else None

    trace_runs = [run for run in runs if run.total_candidates is not None]
    if trace_runs:
        mean_successful: float | None = _mean(
            [float(run.successful_proposers or 0) for run in trace_runs]
        )
        mean_total: float | None = _mean(
            [float(run.total_candidates or 0) for run in trace_runs]
        )
        fallback_runs: int | None = sum(1 for run in trace_runs if run.fallback_used)
    else:
        mean_successful = None
        mean_total = None
        fallback_runs = None

    return ArmReport(
        label=label,
        runs=total,
        successes=successes,
        failures=failures,
        success_rate=(successes / total) if total else 0.0,
        mean_latency_s=_mean(latencies),
        p95_latency_s=_percentile(latencies, 95.0),
        total_input_tokens=sum(run.input_tokens for run in runs),
        total_output_tokens=sum(run.output_tokens for run in runs),
        total_billed_cost=sum(run.billed_cost for run in runs),
        total_estimated_cost_usd=total_estimated,
        failure_kinds={str(k): int(v) for k, v in sorted(failure_kinds.items())},
        mean_successful_proposers=mean_successful,
        mean_total_candidates=mean_total,
        fallback_runs=fallback_runs,
        outcomes=list(runs),
    )


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def build_report(ensemble: ArmReport, baseline: ArmReport) -> BenchmarkReport:
    """Compose an ensemble-vs-baseline report with ensemble-minus-baseline deltas."""
    if (
        ensemble.total_estimated_cost_usd is not None
        and baseline.total_estimated_cost_usd is not None
    ):
        est_delta: float | None = (
            ensemble.total_estimated_cost_usd - baseline.total_estimated_cost_usd
        )
        est_ratio = _ratio(
            ensemble.total_estimated_cost_usd, baseline.total_estimated_cost_usd
        )
    else:
        est_delta = None
        est_ratio = None

    return BenchmarkReport(
        ensemble=ensemble,
        baseline=baseline,
        latency_delta_s=ensemble.mean_latency_s - baseline.mean_latency_s,
        latency_ratio=_ratio(ensemble.mean_latency_s, baseline.mean_latency_s),
        success_rate_delta=ensemble.success_rate - baseline.success_rate,
        billed_cost_delta_usd=ensemble.total_billed_cost - baseline.total_billed_cost,
        billed_cost_ratio=_ratio(ensemble.total_billed_cost, baseline.total_billed_cost),
        estimated_cost_delta_usd=est_delta,
        estimated_cost_ratio=est_ratio,
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run_benchmark(
    *,
    prompts: Sequence[BenchmarkPrompt],
    ensemble_provider: LLMProvider,
    baseline_provider: LLMProvider,
    ensemble_label: str = "ensemble",
    baseline_label: str = "baseline",
    ensemble_model_hint: str = "",
    baseline_model_hint: str = "",
    ensemble_provider_hint: str = "",
    baseline_provider_hint: str = "",
    repeat: int = 1,
    base_config: ChatConfig | None = None,
    clock: Clock = time.monotonic,
    price_lookup: PriceLookup | None = None,
    ensemble_injector: FailureInjector | None = None,
    baseline_injector: FailureInjector | None = None,
) -> BenchmarkReport:
    """Run both arms over the same prompts and build the comparison report."""
    ensemble_runs = await run_arm(
        ensemble_provider,
        prompts,
        arm=ensemble_label,
        repeat=repeat,
        base_config=base_config,
        clock=clock,
        price_lookup=price_lookup,
        injector=ensemble_injector,
        model_hint=ensemble_model_hint,
        provider_hint=ensemble_provider_hint,
    )
    baseline_runs = await run_arm(
        baseline_provider,
        prompts,
        arm=baseline_label,
        repeat=repeat,
        base_config=base_config,
        clock=clock,
        price_lookup=price_lookup,
        injector=baseline_injector,
        model_hint=baseline_model_hint,
        provider_hint=baseline_provider_hint,
    )
    return build_report(
        aggregate_arm(ensemble_label, ensemble_runs),
        aggregate_arm(baseline_label, baseline_runs),
    )


# ---------------------------------------------------------------------------
# Built-in synthetic prompt set (generic public-dummy only)
# ---------------------------------------------------------------------------


def default_synthetic_prompts() -> list[BenchmarkPrompt]:
    """A small, generic, public-dummy prompt set for the offline benchmark.

    No real user prompt or transcript content — these are throwaway task
    stubs suitable for shaping and demonstrating the report.
    """
    return [
        BenchmarkPrompt(
            id="summarize",
            text="Summarize the water cycle in two sentences.",
        ),
        BenchmarkPrompt(
            id="translate",
            text="Translate 'good morning' into French, Spanish, and German.",
        ),
        BenchmarkPrompt(
            id="reason",
            text="If a train travels 60 km in 45 minutes, what is its speed in km/h?",
        ),
        BenchmarkPrompt(
            id="code",
            text="Write a Python function that returns the nth Fibonacci number.",
        ),
        BenchmarkPrompt(
            id="classify",
            text="Is the sentence 'The service was terrible' positive or negative?",
        ),
    ]
