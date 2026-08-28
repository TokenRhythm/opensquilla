"""Measurement-only evaluation harnesses.

This package hosts observational benchmarks that quantify runtime behavior
without changing it. The first member, :mod:`ensemble_benchmark`, times the
ensemble provider against a single-model baseline purely through their public
``LLMProvider`` surface — it never reaches into ensemble internals.
"""

from __future__ import annotations

from opensquilla.eval.ensemble_benchmark import (
    ArmReport,
    BenchmarkPrompt,
    BenchmarkReport,
    RunOutcome,
    aggregate_arm,
    build_report,
    default_synthetic_prompts,
    pricing_price_lookup,
    run_arm,
    run_benchmark,
)
from opensquilla.eval.scenarios import run_config_benchmark, run_dry_run_benchmark
from opensquilla.eval.synthetic import SyntheticProvider

__all__ = [
    "ArmReport",
    "BenchmarkPrompt",
    "BenchmarkReport",
    "RunOutcome",
    "SyntheticProvider",
    "aggregate_arm",
    "build_report",
    "default_synthetic_prompts",
    "pricing_price_lookup",
    "run_arm",
    "run_benchmark",
    "run_config_benchmark",
    "run_dry_run_benchmark",
]
