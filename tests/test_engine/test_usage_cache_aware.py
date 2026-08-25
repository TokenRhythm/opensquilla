"""Cache-aware, provenance-labeled cost estimates in the usage tracker and turn loop.

Covers the issue-490 fix: unbilled token buckets are priced with the
four-bucket ``estimate_cost`` at the layered-resolver price, every
cost-fields dict discloses ``estimateBasis``/``priceSource``, and a model
mixing billed and unbilled calls reports billed + estimate instead of
collapsing to billed-only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.turn_runner.turn_finalizer_stage import _turn_usage_payload
from opensquilla.engine.types import DoneEvent
from opensquilla.engine.usage import SessionUsage, UsageTracker, usage_scope
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText


def test_issue_490_row_estimate_is_cache_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    su = SessionUsage()
    su.add(
        11_559_964,
        262_086,
        model_id="deepseek/deepseek-v4-pro",
        cache_read_tokens=10_313_958,
        provider="deepseek",
    )
    row = su.model_breakdown[0]
    # fresh 1,246,006 * 0.435 + read 10,313,958 * 0.003625 + out 262,086 * 0.87 (per M)
    assert row["costUsd"] == pytest.approx(0.807416, abs=1e-4)
    assert row["estimateBasis"] == "cache_aware"
    # The vendored models.dev snapshot carries deepseek-v4-pro's own cost
    # keys, so the provider-qualified lookup resolves through the catalog
    # layer (same rates as the static table).
    assert row["priceSource"] == "catalog"
    assert row["costSource"] == "opensquilla_estimate"
    # Session cost property delegates to the same cache-aware math.
    assert su.cost == pytest.approx(0.807416, abs=1e-4)
    assert su.total_cost == pytest.approx(0.807416, abs=1e-4)


def test_unknown_cache_rate_labels_cache_blind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    su = SessionUsage()
    su.add(1_000_000, 0, model_id="kimi-latest", cache_read_tokens=900_000, provider="moonshot")
    row = su.model_breakdown[0]
    assert row["estimateBasis"] == "cache_blind"
    assert row["priceSource"] == "default"
    # Cache-blind fallback prices the full input at the default rate.
    assert row["costUsd"] == pytest.approx(3.0)


def test_billed_plus_unbilled_same_model_is_mixed_not_collapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    su = SessionUsage()
    su.add(1000, 100, model_id="deepseek/deepseek-v4-pro", billed_cost=0.05, provider="openrouter")
    su.add(2_000_000, 0, model_id="deepseek/deepseek-v4-pro", provider="deepseek")
    row = su.model_breakdown[0]
    assert row["costSource"] == "mixed"
    assert row["costUsd"] == pytest.approx(0.05 + 2 * 0.435, abs=1e-4)
    assert row["billedCostUsd"] == pytest.approx(0.05)
    # total_cost keeps the row invariant: billed + estimate-of-unbilled.
    assert su.total_cost == pytest.approx(0.05 + 2 * 0.435, abs=1e-4)
    breakdown_sum = sum(item["costUsd"] for item in su.model_breakdown)
    assert breakdown_sum == pytest.approx(su.total_cost, abs=1e-6)


def test_billed_only_row_has_null_basis_but_price_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    su = SessionUsage()
    su.add(1000, 50, model_id="claude-opus-4-7", billed_cost=0.05)
    row = su.model_breakdown[0]
    assert row["costSource"] == "provider_billed"
    assert row["costUsd"] == pytest.approx(0.05)
    assert row["estimatedCostUsd"] == 0.0
    assert row["estimateBasis"] is None
    assert row["estimate_basis"] is None
    assert row["priceSource"] == "static_table"
    assert row["price_source"] == "static_table"


def test_local_free_row_labels_free_basis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    su = SessionUsage()
    su.add(10_000, 500, model_id="qwen3:4b", provider="ollama")
    row = su.model_breakdown[0]
    assert row["costUsd"] == 0.0
    assert row["estimateBasis"] == "free"
    assert row["priceSource"] == "local_free"


@pytest.mark.parametrize("provider", ["custom", "custom_anthropic"])
def test_generic_custom_free_row_preserves_zero_estimate_provenance(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    su = SessionUsage()
    su.add(10_000, 500, model_id="gpt-4.1", provider=provider)

    row = su.model_breakdown[0]
    assert row["costUsd"] == 0.0
    assert row["costSource"] == "unavailable"
    assert row["estimateBasis"] == "free"
    assert row["priceSource"] == "custom_free"


def test_unbilled_counters_accumulate_only_when_call_is_unbilled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    su = SessionUsage()
    su.add(1000, 100, model_id="m", billed_cost=0.05, cache_read_tokens=300, cache_write_tokens=10)
    su.add(2000, 200, model_id="m", cache_read_tokens=500, cache_write_tokens=20)

    assert su._per_model is not None
    mu = su._per_model["m"]
    # Totals accumulate everything.
    assert mu.input_tokens == 3000
    assert mu.output_tokens == 300
    assert mu.cache_read_tokens == 800
    assert mu.cache_write_tokens == 30
    # Unbilled buckets carry only the unbilled call.
    assert mu.unbilled_input_tokens == 2000
    assert mu.unbilled_output_tokens == 200
    assert mu.unbilled_cache_read_tokens == 500
    assert mu.unbilled_cache_write_tokens == 20


def test_scoped_usage_accumulates_unbilled_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    tracker = UsageTracker()
    with usage_scope("subagent:researcher"):
        tracker.add("s1", 1000, 50, model_id="m", cache_read_tokens=400)
    scoped = tracker.get_scope("s1", "subagent:researcher")
    assert scoped is not None
    assert scoped._per_model is not None
    mu = scoped._per_model["m"]
    assert mu.unbilled_input_tokens == 1000
    assert mu.unbilled_output_tokens == 50
    assert mu.unbilled_cache_read_tokens == 400


def test_checkpoint_clone_preserves_unbilled_counters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    tracker = UsageTracker()
    tracker.add("s1", 1000, 50, model_id="m", cache_read_tokens=400, cache_write_tokens=30)
    checkpoint = tracker.session_checkpoint("s1")
    assert checkpoint is not None
    assert checkpoint._per_model is not None
    mu = checkpoint._per_model["m"]
    assert mu.unbilled_input_tokens == 1000
    assert mu.unbilled_output_tokens == 50
    assert mu.unbilled_cache_read_tokens == 400
    assert mu.unbilled_cache_write_tokens == 30

    # Mutating the live session must not leak into the checkpoint.
    tracker.add("s1", 500, 5, model_id="m")
    assert mu.unbilled_input_tokens == 1000


def test_delta_snapshot_prices_unbilled_delta_cache_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    tracker = UsageTracker()
    sk = "agent:main:delta"
    tracker.add(
        sk,
        1000,
        100,
        model_id="deepseek/deepseek-v4-pro",
        billed_cost=0.05,
        provider="deepseek",
    )
    checkpoint = tracker.session_checkpoint(sk)
    tracker.add(
        sk,
        1_000_000,
        0,
        model_id="deepseek/deepseek-v4-pro",
        cache_read_tokens=900_000,
        provider="deepseek",
    )

    delta = tracker.session_delta_snapshot(sk, checkpoint)
    assert delta is not None
    assert delta.input_tokens == 1_000_000
    assert delta.cache_read_tokens == 900_000
    assert delta.billed_cost == 0.0
    # fresh 100,000 * 0.435 + read 900,000 * 0.003625 (per M) = 0.0467625
    assert delta.cost_usd == pytest.approx(0.0467625, abs=1e-6)


def test_delta_snapshot_mixed_turn_adds_billed_and_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    tracker = UsageTracker()
    sk = "agent:main:mixed-delta"
    checkpoint = tracker.session_checkpoint(sk)
    tracker.add(
        sk,
        1000,
        100,
        model_id="deepseek/deepseek-v4-pro",
        billed_cost=0.05,
        provider="deepseek",
    )
    tracker.add(sk, 2_000_000, 0, model_id="deepseek/deepseek-v4-pro", provider="deepseek")

    delta = tracker.session_delta_snapshot(sk, checkpoint)
    assert delta is not None
    assert delta.billed_cost == pytest.approx(0.05)
    # Billed delta + estimate of the unbilled delta — no billed-only collapse.
    assert delta.cost_usd == pytest.approx(0.05 + 2 * 0.435, abs=1e-6)


class _CachedUsageProvider:
    provider_name = "fake"

    def __init__(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        model: str = "",
    ) -> None:
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self._cached_tokens = cached_tokens
        self._model = model

    def chat(self, messages: Any, tools: Any = None, config: Any = None) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        yield ProviderText(text="done")
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cached_tokens=self._cached_tokens,
            model=self._model,
        )

    async def list_models(self) -> list[Any]:
        return []


async def test_agent_turn_end_estimate_is_cache_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    provider = _CachedUsageProvider(
        input_tokens=1_000_000,
        output_tokens=0,
        cached_tokens=900_000,
        model="deepseek/deepseek-v4-pro",
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(model_id="deepseek/deepseek-v4-pro", provider_id="deepseek"),
    )
    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")
    # fresh 100,000 * 0.435 + read 900,000 * 0.003625 (per M) = 0.0467625
    assert done.cost_usd == pytest.approx(0.0467625, abs=1e-6)
    assert done.cost_source == "opensquilla_static_estimate"
    assert done.estimate_basis == "cache_aware"


async def test_agent_turn_end_billed_cost_has_null_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")

    class _BilledProvider(_CachedUsageProvider):
        async def _stream(self) -> AsyncIterator[Any]:
            yield ProviderText(text="done")
            yield ProviderDone(
                stop_reason="stop",
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                billed_cost=0.02,
                model=self._model,
            )

    agent = Agent(
        provider=_BilledProvider(input_tokens=1000, output_tokens=10, model="claude-opus-4-7"),
        config=AgentConfig(model_id="claude-opus-4-7"),
    )
    events = [event async for event in agent.run_turn("hello")]
    done = next(event for event in events if event.kind == "done")
    assert done.cost_source == "provider_billed"
    assert done.cost_usd == pytest.approx(0.02)
    assert done.estimate_basis is None


def test_turn_usage_payload_carries_estimate_basis() -> None:
    done = DoneEvent(input_tokens=5, output_tokens=3, estimate_basis="cache_aware")
    payload = _turn_usage_payload(done, resolved_model="m")
    assert payload is not None
    assert payload["estimate_basis"] == "cache_aware"

    legacy = _turn_usage_payload(DoneEvent(input_tokens=5, output_tokens=3), resolved_model="m")
    assert legacy is not None
    assert legacy["estimate_basis"] is None


def test_turn_usage_payload_carries_decision_id() -> None:
    """The feedback loop's client entry point: decisionId on the wire."""
    done = DoneEvent(input_tokens=5, output_tokens=3, decision_id="a" * 32)
    payload = _turn_usage_payload(done, resolved_model="m")
    assert payload is not None
    assert payload["decision_id"] == "a" * 32

    legacy = _turn_usage_payload(DoneEvent(input_tokens=5, output_tokens=3), resolved_model="m")
    assert legacy is not None
    assert legacy["decision_id"] is None
