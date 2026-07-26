"""Offline tests for the measurement-only ensemble benchmark harness.

Everything here runs with no network and no credentials: providers are the
offline :class:`SyntheticProvider`, failures are scripted through a
``FailureInjector``, latency is pinned by an injected clock, and cost math uses
a stub price lookup. The conftest strips provider keys, so this suite is safe
regardless of the developer's shell.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.pricing import ModelPrice, PriceEntry
from opensquilla.eval.ensemble_benchmark import (
    ArmReport,
    BenchmarkPrompt,
    RunOutcome,
    aggregate_arm,
    build_report,
    default_synthetic_prompts,
    pricing_price_lookup,
    run_arm,
    run_benchmark,
)
from opensquilla.eval.synthetic import SyntheticProvider
from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.types import ChatConfig, DoneEvent, FailureInjector


class ScriptedClock:
    """Deterministic clock returning queued values (one per ``clock()`` call)."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)

    def __call__(self) -> float:
        return self._values.pop(0)


def _latency_clock(per_run: list[float]) -> Callable[[], float]:
    """A clock scripting start=0, end=latency for each sequential run."""
    queue: list[float] = []
    for latency in per_run:
        queue.extend((0.0, latency))
    return ScriptedClock(queue)


_ENSEMBLE_TRACE = {
    "mode": "b5_fusion",
    "profile": "static_openrouter_b5",
    "successful_proposers": 3,
    "total_candidates": 5,
    "fallback_used": False,
}

_STUB_PRICES = {
    "ens-model": ModelPrice(input_per_token=1e-6, output_per_token=2e-6),
    "base-model": ModelPrice(input_per_token=1e-6, output_per_token=2e-6),
}


def _stub_price_lookup(model_id: str) -> ModelPrice | None:
    return _STUB_PRICES.get(model_id)


def _prompts(n: int) -> list[BenchmarkPrompt]:
    return [BenchmarkPrompt(id=f"p{i}", text=f"dummy prompt {i}") for i in range(n)]


# ---------------------------------------------------------------------------
# End-to-end offline benchmark: ensemble vs baseline
# ---------------------------------------------------------------------------


def test_offline_benchmark_ensemble_vs_baseline_deterministic() -> None:
    prompts = _prompts(3)
    ensemble = SyntheticProvider(
        model="ens-model",
        input_tokens=100,
        output_tokens=50,
        ensemble_trace=_ENSEMBLE_TRACE,
    )
    baseline = SyntheticProvider(model="base-model", input_tokens=80, output_tokens=40)
    # ensemble: 3rd run rate-limited; baseline: all succeed.
    ensemble_injector = FailureInjector(
        script=["succeed", "succeed", ProviderFailureKind.RATE_LIMITED]
    )
    baseline_injector = FailureInjector(script=["succeed", "succeed", "succeed"])
    # ensemble arm consumes its 3 runs first, then baseline arm.
    clock = _latency_clock([0.1, 0.2, 0.3, 0.05, 0.05, 0.05])

    report = asyncio.run(
        run_benchmark(
            prompts=prompts,
            ensemble_provider=ensemble,
            baseline_provider=baseline,
            clock=clock,
            price_lookup=_stub_price_lookup,
            ensemble_injector=ensemble_injector,
            baseline_injector=baseline_injector,
        )
    )

    ens = report.ensemble
    assert ens.runs == 3
    assert ens.successes == 2
    assert ens.failures == 1
    assert ens.success_rate == pytest.approx(2 / 3)
    assert ens.failure_kinds == {"rate_limited": 1}
    assert ens.mean_latency_s == pytest.approx(0.2)
    assert ens.p95_latency_s == pytest.approx(0.3)
    # Only successful runs emit a DoneEvent with tokens.
    assert ens.total_input_tokens == 200
    assert ens.total_output_tokens == 100
    # 2 successful runs, each 100*1e-6 + 50*2e-6 = 2e-4.
    assert ens.total_estimated_cost_usd == pytest.approx(4e-4)
    # Ensemble aggregates read from the public trace on the 2 successful runs.
    assert ens.mean_successful_proposers == pytest.approx(3.0)
    assert ens.mean_total_candidates == pytest.approx(5.0)
    assert ens.fallback_runs == 0

    base = report.baseline
    assert base.runs == 3
    assert base.success_rate == pytest.approx(1.0)
    assert base.mean_latency_s == pytest.approx(0.05)
    assert base.total_input_tokens == 240
    assert base.mean_successful_proposers is None  # no ensemble trace
    assert base.fallback_runs is None

    assert report.latency_delta_s == pytest.approx(0.15)
    assert report.latency_ratio == pytest.approx(4.0)
    assert report.success_rate_delta == pytest.approx(2 / 3 - 1.0)
    assert report.estimated_cost_delta_usd == pytest.approx(4e-4 - 4.8e-4)


def test_baseline_arm_is_untouched_by_ensemble_injector() -> None:
    # A single run through a baseline provider with no injector always succeeds
    # and carries no ensemble trace (black-box: nothing ensemble-specific leaks).
    baseline = SyntheticProvider(model="base-model", input_tokens=10, output_tokens=5)
    runs = asyncio.run(
        run_arm(baseline, _prompts(2), arm="baseline", clock=_latency_clock([0.01, 0.02]))
    )
    assert [run.ok for run in runs] == [True, True]
    assert all(run.successful_proposers is None for run in runs)
    assert all(run.ensemble_mode is None for run in runs)


# ---------------------------------------------------------------------------
# Failure classification round-trip via the injector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.PROVIDER_OVERLOADED,
        ProviderFailureKind.AUTH_INVALID,
        ProviderFailureKind.INSUFFICIENT_CREDITS,
        ProviderFailureKind.MODEL_NOT_FOUND,
        ProviderFailureKind.BAD_REQUEST,
    ],
)
def test_injected_failure_kind_round_trips(kind: ProviderFailureKind) -> None:
    # provider_name defaults to the registered "openai" so family-scoped kinds
    # classify back to themselves.
    provider = SyntheticProvider(model="m", provider_name="openai")
    injector = FailureInjector(script=[kind])
    runs = asyncio.run(
        run_arm(provider, _prompts(1), arm="x", injector=injector, clock=_latency_clock([0.0]))
    )
    assert runs[0].ok is False
    assert runs[0].failure_kind == kind.value


def test_raised_exception_is_recorded_not_propagated() -> None:
    provider = SyntheticProvider(model="m", provider_name="openai")
    injector = FailureInjector(script=[RuntimeError("connection timeout")])
    runs = asyncio.run(
        run_arm(provider, _prompts(1), arm="x", injector=injector, clock=_latency_clock([0.0]))
    )
    assert runs[0].ok is False
    assert "connection timeout" in runs[0].error_message


# ---------------------------------------------------------------------------
# Pure aggregation math
# ---------------------------------------------------------------------------


def _outcome(**kwargs: object) -> RunOutcome:
    defaults: dict[str, object] = {
        "arm": "a",
        "prompt_id": "p",
        "run_index": 0,
        "ok": True,
        "latency_s": 0.0,
        "failure_kind": None,
        "error_code": "",
        "error_message": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "billed_cost": 0.0,
        "cost_source": "none",
        "estimated_cost_usd": None,
        "successful_proposers": None,
        "total_candidates": None,
        "fallback_used": None,
        "ensemble_mode": None,
    }
    defaults.update(kwargs)
    return RunOutcome(**defaults)  # type: ignore[arg-type]


def test_aggregate_arm_math_and_percentile() -> None:
    runs = [
        _outcome(ok=True, latency_s=0.1, estimated_cost_usd=0.001),
        _outcome(ok=True, latency_s=0.3, estimated_cost_usd=0.002),
        _outcome(ok=False, latency_s=0.5, failure_kind="rate_limited"),
    ]
    arm = aggregate_arm("a", runs)
    assert arm.runs == 3
    assert arm.successes == 2
    assert arm.failures == 1
    assert arm.success_rate == pytest.approx(2 / 3)
    assert arm.mean_latency_s == pytest.approx(0.3)
    assert arm.p95_latency_s == pytest.approx(0.5)  # nearest-rank
    assert arm.failure_kinds == {"rate_limited": 1}
    assert arm.total_estimated_cost_usd == pytest.approx(0.003)


def test_aggregate_arm_no_estimates_yields_none_total() -> None:
    runs = [_outcome(ok=True, latency_s=0.1), _outcome(ok=True, latency_s=0.2)]
    arm = aggregate_arm("a", runs)
    assert arm.total_estimated_cost_usd is None


def test_aggregate_arm_reads_fallback_from_trace() -> None:
    runs = [
        _outcome(ok=True, successful_proposers=2, total_candidates=5, fallback_used=True),
        _outcome(ok=True, successful_proposers=4, total_candidates=5, fallback_used=False),
    ]
    arm = aggregate_arm("ens", runs)
    assert arm.mean_successful_proposers == pytest.approx(3.0)
    assert arm.mean_total_candidates == pytest.approx(5.0)
    assert arm.fallback_runs == 1


def test_build_report_deltas_and_ratios() -> None:
    ens = ArmReport(
        label="ensemble",
        runs=2,
        successes=2,
        failures=0,
        success_rate=1.0,
        mean_latency_s=0.4,
        p95_latency_s=0.5,
        total_input_tokens=0,
        total_output_tokens=0,
        total_billed_cost=0.0,
        total_estimated_cost_usd=0.004,
        failure_kinds={},
        mean_successful_proposers=3.0,
        mean_total_candidates=5.0,
        fallback_runs=0,
    )
    base = ArmReport(
        label="baseline",
        runs=2,
        successes=2,
        failures=0,
        success_rate=1.0,
        mean_latency_s=0.1,
        p95_latency_s=0.1,
        total_input_tokens=0,
        total_output_tokens=0,
        total_billed_cost=0.0,
        total_estimated_cost_usd=0.001,
        failure_kinds={},
        mean_successful_proposers=None,
        mean_total_candidates=None,
        fallback_runs=None,
    )
    report = build_report(ens, base)
    assert report.latency_delta_s == pytest.approx(0.3)
    assert report.latency_ratio == pytest.approx(4.0)
    assert report.estimated_cost_delta_usd == pytest.approx(0.003)
    assert report.estimated_cost_ratio == pytest.approx(4.0)


def test_pricing_price_lookup_is_offline_for_unqualified_model() -> None:
    # "gpt-5.5" has no "/", so pricing.py resolves it from the static table with
    # no network call. This proves the cost column uses pricing.py offline.
    price = pricing_price_lookup("gpt-5.5")
    assert price is not None
    assert price.input_per_token == pytest.approx(5.0 / 1_000_000)
    assert price.output_per_token == pytest.approx(30.0 / 1_000_000)


def test_live_pricing_estimate_sums_provider_aware_four_bucket_breakdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.eval.ensemble_benchmark as benchmark

    resolved: list[tuple[str, str]] = []

    def fake_resolve(model: str, provider: str) -> SimpleNamespace:
        resolved.append((model, provider))
        return SimpleNamespace(
            entry=PriceEntry(
                input_per_m=10.0,
                output_per_m=20.0,
                cache_read_per_m=1.0,
                cache_write_per_m=2.0,
            )
        )

    class BreakdownProvider:
        provider_name = "ensemble"

        async def chat(self, *_args: Any, **_kwargs: Any):
            yield DoneEvent(
                input_tokens=300,
                output_tokens=30,
                model="ensemble/static-tokenrhythm-b5",
                model_usage_breakdown=[
                    {
                        "model": "model-a",
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "cached_tokens": 40,
                    },
                    {
                        "model": "model-b",
                        "input_tokens": 200,
                        "output_tokens": 20,
                        "cache_write_tokens": 50,
                    },
                ],
            )

    monkeypatch.setattr(benchmark, "resolve_model_price", fake_resolve)
    report = asyncio.run(
        run_benchmark(
            prompts=_prompts(1),
            ensemble_provider=BreakdownProvider(),
            baseline_provider=BreakdownProvider(),
            price_lookup=pricing_price_lookup,
            ensemble_provider_hint="tokenrhythm",
            baseline_provider_hint="tokenrhythm",
            clock=_latency_clock([0.0, 0.0]),
        )
    )

    # model-a: 60*10 + 40*1 + 10*20 = 840; model-b:
    # 150*10 + 50*2 + 20*20 = 2000, all prices per million.
    assert report.ensemble.total_estimated_cost_usd == pytest.approx(2840 / 1_000_000)
    assert report.baseline.total_estimated_cost_usd == pytest.approx(2840 / 1_000_000)
    assert resolved == [
        ("model-a", "tokenrhythm"),
        ("model-b", "tokenrhythm"),
        ("model-a", "tokenrhythm"),
        ("model-b", "tokenrhythm"),
    ]


def test_report_to_dict_shape() -> None:
    prompts = _prompts(2)
    provider = SyntheticProvider(model="base-model", input_tokens=10, output_tokens=5)
    report = asyncio.run(
        run_benchmark(
            prompts=prompts,
            ensemble_provider=SyntheticProvider(
                model="ens-model", ensemble_trace=_ENSEMBLE_TRACE
            ),
            baseline_provider=provider,
            clock=_latency_clock([0.1, 0.1, 0.1, 0.1]),
            price_lookup=_stub_price_lookup,
        )
    )
    payload = report.to_dict()
    assert set(payload) == {"ensemble", "baseline", "deltas"}
    assert payload["ensemble"]["mean_successful_proposers"] == pytest.approx(3.0)
    assert "outcomes" in payload["ensemble"]
    assert len(payload["ensemble"]["outcomes"]) == 2
    # to_dict must be JSON-serializable.
    json.dumps(payload)


def test_default_synthetic_prompts_are_generic() -> None:
    prompts = default_synthetic_prompts()
    assert prompts
    assert all(p.text for p in prompts)
    assert len({p.id for p in prompts}) == len(prompts)


def test_config_system_override_per_prompt() -> None:
    # A prompt.system must not raise and must be applied without touching the base.
    base = ChatConfig(max_tokens=42)
    provider = SyntheticProvider(model="m")
    prompt = BenchmarkPrompt(id="s", text="hi", system="be terse")
    runs = asyncio.run(
        run_arm(
            provider,
            [prompt],
            arm="x",
            base_config=base,
            clock=_latency_clock([0.0]),
        )
    )
    assert runs[0].ok is True
    assert base.system is None  # base config not mutated
