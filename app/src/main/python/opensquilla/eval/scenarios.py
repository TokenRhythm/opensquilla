"""Scenario builders for the ensemble benchmark.

These functions own all provider/engine coupling for the two ways the benchmark
is driven — an offline scripted dry run and a live run built from a loaded
config — so the CLI stays a thin driver over the ``eval`` package and never
reaches into the provider layer directly. Neither path changes runtime behavior
or ensemble internals; both only observe providers through their public
``chat`` surface.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from opensquilla.eval.ensemble_benchmark import (
    BenchmarkPrompt,
    BenchmarkReport,
    Clock,
    pricing_price_lookup,
    run_benchmark,
)
from opensquilla.eval.synthetic import SyntheticProvider
from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.types import ChatConfig, FailureInjector

if TYPE_CHECKING:
    from collections.abc import Sequence

# Synthetic dry-run model ids are intentionally unqualified (no "/") so the
# pricing lookup resolves them from the offline static table without a network
# call, while still differing so the cost comparison is meaningful.
DRY_RUN_ENSEMBLE_MODEL = "gpt-5.5"
DRY_RUN_BASELINE_MODEL = "gpt-5.4-mini"


def _dry_run_script(
    total: int, fail_indices: set[int], kind: ProviderFailureKind
) -> list[Any]:
    """A FailureInjector script: ``kind`` at each failing index, else succeed."""
    if not fail_indices:
        return []
    length = min(total, max(fail_indices) + 1)
    return [kind if i in fail_indices else "succeed" for i in range(length)]


async def run_dry_run_benchmark(
    *,
    prompts: Sequence[BenchmarkPrompt],
    repeat: int = 1,
    cost: bool = True,
    clock: Clock = time.monotonic,
) -> BenchmarkReport:
    """Run both arms fully offline against scripted synthetic providers.

    Both arms always succeed at the provider level; a ``FailureInjector`` script
    overlays a deterministic mix of failures so the report is representative. No
    network, no credentials.
    """
    total = len(prompts) * max(1, repeat)
    ensemble_provider = SyntheticProvider(
        model=DRY_RUN_ENSEMBLE_MODEL,
        input_tokens=2400,
        output_tokens=600,
        ensemble_trace={
            "mode": "b5_fusion",
            "profile": "static_openrouter_b5",
            "successful_proposers": 3,
            "total_candidates": 5,
            "fallback_used": False,
        },
    )
    baseline_provider = SyntheticProvider(
        model=DRY_RUN_BASELINE_MODEL,
        input_tokens=1200,
        output_tokens=400,
    )
    ensemble_injector = FailureInjector(
        script=_dry_run_script(total, {2}, ProviderFailureKind.PROVIDER_OVERLOADED)
    )
    baseline_injector = FailureInjector(
        script=_dry_run_script(total, {1, 3}, ProviderFailureKind.RATE_LIMITED)
    )
    return await run_benchmark(
        prompts=prompts,
        ensemble_provider=ensemble_provider,
        baseline_provider=baseline_provider,
        repeat=repeat,
        base_config=ChatConfig(max_tokens=512),
        clock=clock,
        price_lookup=pricing_price_lookup if cost else None,
        ensemble_injector=ensemble_injector,
        baseline_injector=baseline_injector,
        ensemble_model_hint=DRY_RUN_ENSEMBLE_MODEL,
        baseline_model_hint=DRY_RUN_BASELINE_MODEL,
        ensemble_provider_hint="",
        baseline_provider_hint="",
    )


async def run_config_benchmark(
    *,
    prompts: Sequence[BenchmarkPrompt],
    config: Any,
    baseline_model: str | None = None,
    repeat: int = 1,
    cost: bool = True,
    max_tokens: int = 512,
    timeout: float = 120.0,
    clock: Clock = time.monotonic,
) -> BenchmarkReport:
    """Build real ensemble + baseline providers from ``config`` and benchmark them.

    Live by nature (contacts providers); never reached by the default test
    suite. The ensemble is built through its public config builder and the
    single-model baseline through the public provider factory. Raises
    ``ValueError`` on incomplete provider config before any provider is built.
    """
    from opensquilla.provider.ensemble import build_ensemble_provider_from_config
    from opensquilla.provider.selector import ProviderConfig, build_provider

    llm = getattr(config, "llm", None)
    if llm is None:
        raise ValueError("no [llm] provider configured")
    provider_id = str(getattr(llm, "provider", "") or "").strip()
    model = (baseline_model or str(getattr(llm, "model", "") or "")).strip()
    if not provider_id or not model:
        raise ValueError("[llm] must define both provider and model")
    api_key = str(getattr(llm, "api_key", "") or "")
    if not api_key:
        env_name = str(getattr(llm, "api_key_env", "") or "")
        api_key = os.environ.get(env_name, "") if env_name else ""
    base_url = str(getattr(llm, "base_url", "") or "")

    inherited = ProviderConfig(
        provider=provider_id, model=model, api_key=api_key, base_url=base_url
    )
    baseline_provider = build_provider(
        provider=provider_id, model=model, api_key=api_key, base_url=base_url
    )
    ensemble_provider = build_ensemble_provider_from_config(
        config=config,
        inherited_provider_config=inherited,
        fallback_provider=baseline_provider,
    )
    return await run_benchmark(
        prompts=prompts,
        ensemble_provider=ensemble_provider,
        baseline_provider=baseline_provider,
        repeat=repeat,
        base_config=ChatConfig(max_tokens=max_tokens, timeout=timeout),
        clock=clock,
        price_lookup=pricing_price_lookup if cost else None,
        ensemble_model_hint=model,
        baseline_model_hint=model,
        ensemble_provider_hint=provider_id,
        baseline_provider_hint=provider_id,
    )
