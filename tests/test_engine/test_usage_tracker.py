"""Unit tests for SessionUsage / UsageTracker cache token accumulation."""

import pytest

from opensquilla.engine.usage import ModelUsage, SessionUsage, UsageTracker


def test_session_usage_accumulates_cache_tokens() -> None:
    usage = SessionUsage(model_id="claude-opus-4-7")
    usage.add(1000, 50, "claude-opus-4-7", cache_read_tokens=500, cache_write_tokens=100)
    usage.add(2000, 80, "claude-opus-4-7", cache_read_tokens=300, cache_write_tokens=40)

    assert usage.input_tokens == 3000
    assert usage.output_tokens == 130
    assert usage.cache_read_tokens == 800
    assert usage.cache_write_tokens == 140


def test_session_usage_per_model_breakdown_isolates_cache_tokens() -> None:
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", cache_read_tokens=500, cache_write_tokens=100)
    usage.add(2000, 80, "deepseek-v4-pro", cache_read_tokens=300, cache_write_tokens=40)

    assert usage._per_model is not None
    opus = usage._per_model["claude-opus-4-7"]
    deepseek = usage._per_model["deepseek-v4-pro"]
    assert opus.cache_read_tokens == 500
    assert opus.cache_write_tokens == 100
    assert deepseek.cache_read_tokens == 300
    assert deepseek.cache_write_tokens == 40


def test_usage_tracker_session_total_equals_sum_across_providers() -> None:
    # R9c guard: add() is synchronous, so a session's totals are the exact
    # arithmetic sum of every accumulation, including turns billed to
    # different providers/models (no interleaving / lost-update race).
    tracker = UsageTracker()
    sk = "agent:main:s1"
    tracker.add(sk, 1000, 100, model_id="anthropic/m1", provider="anthropic")
    tracker.add(sk, 2000, 200, model_id="deepseek/m2", provider="deepseek")
    tracker.add(sk, 500, 50, model_id="anthropic/m1", provider="anthropic")

    usage = tracker.get(sk)
    assert usage is not None
    assert usage.input_tokens == 3500
    assert usage.output_tokens == 350
    assert usage._per_model is not None
    assert usage._per_model["anthropic/m1"].input_tokens == 1500
    assert usage._per_model["deepseek/m2"].input_tokens == 2000


def test_deployment_breakdown_keeps_same_model_separate_across_providers() -> None:
    usage = SessionUsage()
    usage.add(100, 10, "shared-model", provider="provider-a", billed_cost=0.01)
    usage.add(200, 20, "shared-model", provider="provider-b", billed_cost=0.02)

    # Legacy field remains model-keyed for compatibility.
    assert usage._per_model is not None
    assert list(usage._per_model) == ["shared-model"]
    assert usage._per_model["shared-model"].input_tokens == 300

    by_deployment = {
        (row["provider"], row["model"]): row for row in usage.deployment_breakdown
    }
    assert by_deployment[("provider-a", "shared-model")]["inputTokens"] == 100
    assert by_deployment[("provider-b", "shared-model")]["inputTokens"] == 200
    assert sum(row["billedCostUsd"] for row in by_deployment.values()) == pytest.approx(
        usage.billed_cost
    )


def test_session_usage_add_default_cache_zero() -> None:
    """Existing positional callers (no cache kwargs) still work; cache fields stay at 0."""
    usage = SessionUsage(model_id="claude-opus-4-7")
    usage.add(1000, 50, "claude-opus-4-7")

    assert usage.input_tokens == 1000
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 0


def test_usage_tracker_add_passes_cache_to_session() -> None:
    tracker = UsageTracker()
    tracker.add(
        "session-a",
        input_tokens=1000,
        output_tokens=50,
        model_id="claude-opus-4-7",
        cache_read_tokens=200,
        cache_write_tokens=80,
    )
    tracker.add(
        "session-a",
        input_tokens=500,
        output_tokens=20,
        model_id="claude-opus-4-7",
        cache_read_tokens=100,
        cache_write_tokens=40,
    )

    usage = tracker.get("session-a")
    assert usage is not None
    assert usage.cache_read_tokens == 300
    assert usage.cache_write_tokens == 120


def test_usage_tracker_isolates_sessions() -> None:
    tracker = UsageTracker()
    tracker.add(
        "session-a",
        input_tokens=100,
        output_tokens=10,
        model_id="m",
        cache_read_tokens=50,
        cache_write_tokens=5,
    )
    tracker.add(
        "session-b",
        input_tokens=200,
        output_tokens=20,
        model_id="m",
        cache_read_tokens=70,
        cache_write_tokens=15,
    )

    a = tracker.get("session-a")
    b = tracker.get("session-b")
    assert a is not None and b is not None
    assert a.cache_read_tokens == 50
    assert a.cache_write_tokens == 5
    assert b.cache_read_tokens == 70
    assert b.cache_write_tokens == 15


# ---------------------------------------------------------------------------
# Positional dataclass construction safety.
# ---------------------------------------------------------------------------


def test_session_usage_positional_construction_does_not_shift_fields() -> None:
    """Regression: SessionUsage(1, 2, "claude-opus-4-7") must keep the third
    positional arg in model_id, not in a cache field. Asserts the new cache
    counters were appended at the *end* of the dataclass."""
    usage = SessionUsage(1000, 50, "claude-opus-4-7")

    assert usage.input_tokens == 1000
    assert usage.output_tokens == 50
    assert usage.model_id == "claude-opus-4-7"
    assert usage.cache_read_tokens == 0
    assert usage.cache_write_tokens == 0


def test_model_usage_positional_construction_keeps_model_id_first() -> None:
    """ModelUsage(model_id, in, out) — sanity check for positional callers."""
    mu = ModelUsage("claude-opus-4-7", 1000, 50)

    assert mu.model_id == "claude-opus-4-7"
    assert mu.input_tokens == 1000
    assert mu.output_tokens == 50
    assert mu.cache_read_tokens == 0
    assert mu.cache_write_tokens == 0


# ---------------------------------------------------------------------------
# model_breakdown must surface cache fields.
# ---------------------------------------------------------------------------


def test_model_breakdown_serializes_cache_fields_for_per_model_path() -> None:
    """Multi-model session: each breakdown entry must carry cache R/W counters
    so the UI's modelBreakdown column can show per-model cache split."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", cache_read_tokens=500, cache_write_tokens=100)
    usage.add(2000, 80, "deepseek-v4-pro", cache_read_tokens=300, cache_write_tokens=40)

    breakdown = usage.model_breakdown
    assert len(breakdown) == 2

    by_model = {row["model"]: row for row in breakdown}
    assert by_model["claude-opus-4-7"]["cacheReadTokens"] == 500
    assert by_model["claude-opus-4-7"]["cacheWriteTokens"] == 100
    assert by_model["deepseek-v4-pro"]["cacheReadTokens"] == 300
    assert by_model["deepseek-v4-pro"]["cacheWriteTokens"] == 40


def test_model_breakdown_serializes_cache_fields_for_single_model_path() -> None:
    """Single-model session (no per-model dict yet): the synthesized one-row
    breakdown must also carry cache R/W counters."""
    usage = SessionUsage(model_id="claude-opus-4-7")
    # Direct field mutation — exercises the no-_per_model branch in model_breakdown.
    usage.input_tokens = 1000
    usage.output_tokens = 50
    usage.cache_read_tokens = 200
    usage.cache_write_tokens = 80

    [row] = usage.model_breakdown
    assert row["model"] == "claude-opus-4-7"
    assert row["cacheReadTokens"] == 200
    assert row["cacheWriteTokens"] == 80


# ---------------------------------------------------------------------------
# Per-model billed_cost: real provider-billed amounts flow through ModelUsage
# so the breakdown can show actual numbers instead of relying on the rpc_usage
# pro-rate workaround.
# ---------------------------------------------------------------------------


def test_model_usage_accumulates_billed_cost() -> None:
    """ModelUsage.billed_cost accumulates across SessionUsage.add(...) calls
    for the same model. Mirrors the agent.py raw_ev loop where one model can
    receive multiple provider calls within a turn."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", billed_cost=0.0500)
    usage.add(2000, 80, "claude-opus-4-7", billed_cost=0.0125)

    assert usage._per_model is not None
    opus = usage._per_model["claude-opus-4-7"]
    assert opus.billed_cost == pytest.approx(0.0625)


def test_session_usage_per_model_billed_isolates_by_model() -> None:
    """Multi-model session: each model's billed_cost stays independent."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", billed_cost=0.1254)
    usage.add(9000, 100, "z-ai/glm-5.1", billed_cost=0.0111)

    assert usage._per_model is not None
    assert usage._per_model["claude-opus-4-7"].billed_cost == pytest.approx(0.1254)
    assert usage._per_model["z-ai/glm-5.1"].billed_cost == pytest.approx(0.0111)


def test_model_breakdown_uses_billed_when_present() -> None:
    """When provider returned real billed costs, model_breakdown must surface
    those (not the cache-blind pricing-table estimate). Source must flip to
    'provider_billed' so the UI renders the solid 'Actual' chip without
    triggering the pro-rate disclosure notice."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", billed_cost=0.1254)
    usage.add(9000, 100, "z-ai/glm-5.1", billed_cost=0.0111)

    breakdown = usage.model_breakdown
    by_model = {row["model"]: row for row in breakdown}

    assert by_model["claude-opus-4-7"]["costUsd"] == pytest.approx(0.1254)
    assert by_model["claude-opus-4-7"]["billedCostUsd"] == pytest.approx(0.1254)
    assert by_model["claude-opus-4-7"]["costSource"] == "provider_billed"
    assert by_model["z-ai/glm-5.1"]["costUsd"] == pytest.approx(0.0111)
    assert by_model["z-ai/glm-5.1"]["costSource"] == "provider_billed"

    # Sum of per-model billed equals total session billed (the property the
    # WebUI relies on to render the "Actual" badge without pro-rating).
    total = sum(row["costUsd"] for row in breakdown)
    assert total == pytest.approx(0.1365)


def test_model_breakdown_falls_back_to_estimate_when_no_billed() -> None:
    """When billed_cost is 0 (provider unavailable / estimate-only path),
    model_breakdown reverts to the local pricing-table estimate and tags the
    source as 'opensquilla_estimate'."""
    usage = SessionUsage()
    # Use a model that has a stable price entry.
    usage.add(1000, 50, "claude-opus-4-7")  # no billed_cost kwarg → defaults to 0.0

    [row] = usage.model_breakdown
    assert row["billedCostUsd"] == 0.0
    assert row["costSource"] == "opensquilla_estimate"
    # costUsd here is the pricing-table estimate (non-zero for known models).
    assert row["costUsd"] > 0


def test_model_breakdown_mixed_billed_and_estimate() -> None:
    """If only one of two models has a billed cost, each row's source reflects
    its own situation — one provider_billed, one opensquilla_estimate. Total
    is mixed but the rpc_usage layer handles that aggregation separately."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", billed_cost=0.05)
    usage.add(2000, 80, "deepseek-v4-pro")  # no billed

    by_model = {row["model"]: row for row in usage.model_breakdown}
    assert by_model["claude-opus-4-7"]["costSource"] == "provider_billed"
    assert by_model["deepseek-v4-pro"]["costSource"] == "opensquilla_estimate"


def test_usage_tracker_add_forwards_billed_cost() -> None:
    """UsageTracker.add must thread billed_cost through to SessionUsage —
    this is the surface agent.py uses, so a regression here breaks the whole
    provider-billed cost path."""
    tracker = UsageTracker()
    tracker.add(
        "session-a",
        input_tokens=1000,
        output_tokens=50,
        model_id="claude-opus-4-7",
        billed_cost=0.05,
    )
    tracker.add(
        "session-a",
        input_tokens=500,
        output_tokens=20,
        model_id="claude-opus-4-7",
        billed_cost=0.025,
    )

    usage = tracker.get("session-a")
    assert usage is not None
    assert usage._per_model is not None
    assert usage._per_model["claude-opus-4-7"].billed_cost == pytest.approx(0.075)


def test_confirmed_zero_receipt_is_billed_and_never_reestimated() -> None:
    tracker = UsageTracker()
    tracker.add(
        "session-a",
        input_tokens=1_000,
        output_tokens=50,
        model_id="claude-opus-4-7",
        billed_cost=0.0,
        cost_source="provider_billed",
    )

    usage = tracker.get("session-a")
    assert usage is not None
    assert usage.provider_billed_entries == 1
    assert usage.unbilled_entries == 0
    assert usage.cost_source == "provider_billed"
    assert usage.total_cost == 0.0
    [row] = usage.model_breakdown
    assert row["costUsd"] == 0.0
    assert row["billedCostUsd"] == 0.0
    assert row["estimatedCostUsd"] == 0.0
    assert row["costSource"] == "provider_billed"

    snapshot = tracker.session_delta_snapshot("session-a", None)
    assert snapshot is not None
    assert snapshot.billed_cost == 0.0
    assert snapshot.cost_usd == 0.0
    assert snapshot.cost_source == "provider_billed"
    assert snapshot.provider_billed_entries == 1
    assert snapshot.unbilled_entries == 0


def test_zero_receipt_and_unbilled_call_roll_up_as_mixed() -> None:
    usage = SessionUsage()
    usage.add(
        1_000,
        50,
        "claude-opus-4-7",
        billed_cost=0.0,
        cost_source="provider_billed",
    )
    usage.add(2_000, 80, "claude-opus-4-7", cost_source="opensquilla_estimate")

    assert usage.cost_source == "mixed"
    assert usage.provider_billed_entries == 1
    assert usage.unbilled_entries == 1
    [row] = usage.model_breakdown
    assert row["costSource"] == "mixed"
    assert row["billedCostUsd"] == 0.0
    assert row["estimatedCostUsd"] > 0.0
    assert row["costUsd"] == row["estimatedCostUsd"]


def test_session_billed_cost_aggregates_across_models() -> None:
    """SessionUsage.billed_cost sums per-model real billed totals.
    Used by rpc_usage._tracker_rows to decide the row-level cost_source."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", billed_cost=0.1254)
    usage.add(9000, 100, "z-ai/glm-5.1", billed_cost=0.0111)

    assert usage.billed_cost == pytest.approx(0.1365)


def test_session_cost_source_provider_billed_when_all_models_billed() -> None:
    """Every model has real billed → row source = provider_billed."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", billed_cost=0.05)
    usage.add(2000, 80, "deepseek-v4-pro", billed_cost=0.01)

    assert usage.cost_source == "provider_billed"


def test_session_cost_source_mixed_when_only_some_models_billed() -> None:
    """Some models billed, some estimate-only → source = mixed.
    Prevents the row from claiming 'Actual' when half its breakdown is
    estimated."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", billed_cost=0.05)
    usage.add(2000, 80, "deepseek-v4-pro")  # no billed

    assert usage.cost_source == "mixed"


def test_session_cost_source_estimate_when_no_models_billed() -> None:
    """No billed data anywhere → source = opensquilla_estimate (the
    estimate-only path; preserved for sessions where provider returned no cost
    on any call)."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7")
    usage.add(2000, 80, "deepseek-v4-pro")

    assert usage.cost_source == "opensquilla_estimate"
    assert usage.billed_cost == 0.0


def test_session_total_cost_uses_billed_when_available() -> None:
    """All-billed session: total_cost equals billed_cost (no double-count)."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", billed_cost=0.05)
    usage.add(2000, 80, "deepseek-v4-pro", billed_cost=0.01)

    assert usage.total_cost == pytest.approx(0.06)
    assert usage.billed_cost == pytest.approx(0.06)


def test_session_total_cost_mixes_billed_and_estimate() -> None:
    """Mixed session: total_cost = billed (for billed models) +
    estimate (for unbilled). This is the key property that prevents
    the row from under-reporting the unbilled portion of cost.

    Without this, _tracker_rows would set row.cost_usd to billed_cost
    only ($0.05) while the breakdown's unbilled item still contributes
    its estimate ($0.03) — the row would show $0.05 but the breakdown
    sum would be $0.08, a visible inconsistency."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", billed_cost=0.05)
    # deepseek-v4-pro: no billed; pricing-table estimate kicks in
    usage.add(2000, 80, "deepseek-v4-pro")

    # billed_cost only counts billed models
    billed = usage.billed_cost
    assert billed == pytest.approx(0.05)
    # cost (estimate) sums both models' pricing-table estimates
    estimate = usage.cost
    assert estimate > 0  # both models have pricing entries
    # total_cost: billed for first + estimate for second
    deepseek_estimate = usage._per_model["deepseek-v4-pro"].cost
    expected_total = 0.05 + deepseek_estimate
    assert usage.total_cost == pytest.approx(expected_total)
    # And total_cost equals the breakdown sum (key invariant). The breakdown
    # serializer rounds to 6 decimals so we allow a 1e-6 tolerance against
    # the un-rounded total_cost; user-facing display rounds to <=4 decimals.
    breakdown_sum = sum(item["costUsd"] for item in usage.model_breakdown)
    assert breakdown_sum == pytest.approx(usage.total_cost, abs=1e-6)


def test_session_total_cost_same_model_mixed_calls_adds_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One model, one billed call plus one unbilled call: total_cost must be
    billed + estimate-of-unbilled, not the old billed-only collapse. The
    breakdown row flips to 'mixed' and still sums to total_cost."""
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7", billed_cost=0.05)
    usage.add(2000, 80, "claude-opus-4-7")  # provider returned no cost

    # $15/M in + $75/M out (static table): (2000 * 15 + 80 * 75) / 1e6 = 0.036
    assert usage.total_cost == pytest.approx(0.05 + 0.036)
    [row] = usage.model_breakdown
    assert row["costSource"] == "mixed"
    assert row["billedCostUsd"] == pytest.approx(0.05)
    assert row["costUsd"] == pytest.approx(usage.total_cost, abs=1e-6)


def test_session_total_cost_falls_back_to_estimate_when_no_billed() -> None:
    """No billed anywhere → total_cost == cost (pure estimate)."""
    usage = SessionUsage()
    usage.add(1000, 50, "claude-opus-4-7")
    usage.add(2000, 80, "deepseek-v4-pro")

    assert usage.total_cost == pytest.approx(usage.cost)


def test_session_total_cost_empty_session_is_zero() -> None:
    """Defensive: empty session returns 0 (falls through to .cost which is 0)."""
    usage = SessionUsage()
    assert usage.total_cost == 0.0


def test_session_billed_cost_zero_when_no_per_model() -> None:
    """Defensive: empty session (no add() calls) returns 0 billed and
    estimate cost source. Guards against AttributeError when _per_model
    is None."""
    usage = SessionUsage()
    assert usage.billed_cost == 0.0
    assert usage.cost_source == "opensquilla_estimate"


def test_model_usage_positional_construction_keeps_billed_cost_default() -> None:
    """Regression: the new billed_cost field MUST be appended at the end of
    ModelUsage so positional callers (ModelUsage(model_id, in, out, ...))
    keep aligning. Existing test (around line 112) already covers
    cache_read/write at the tail; this extends to billed_cost."""
    mu = ModelUsage("claude-opus-4-7", 1000, 50, 200, 80)
    assert mu.model_id == "claude-opus-4-7"
    assert mu.input_tokens == 1000
    assert mu.output_tokens == 50
    assert mu.cache_read_tokens == 200
    assert mu.cache_write_tokens == 80
    assert mu.billed_cost == 0.0  # default at the tail


def test_local_provider_estimates_as_free() -> None:
    usage = SessionUsage()
    usage.add(10_000, 500, "qwen3:4b", provider="ollama")
    assert usage.cost == 0.0
    assert usage._per_model is not None
    assert usage._per_model["qwen3:4b"].cost == 0.0
    breakdown = usage.model_breakdown[0]
    assert breakdown["costUsd"] == 0.0
    assert breakdown["billedCostUsd"] == 0.0


def test_cloud_provider_cost_unchanged() -> None:
    usage = SessionUsage()
    usage.add(1_000_000, 0, "claude-sonnet-4", provider="anthropic")
    assert usage.cost == pytest.approx(3.0)


def test_usage_tracker_threads_provider_to_per_model() -> None:
    tracker = UsageTracker()
    tracker.add("s", 8000, 300, "gemma3:4b", provider="ollama")
    sess = tracker._sessions["s"]
    assert sess.cost == 0.0
    assert sess._per_model is not None
    assert sess._per_model["gemma3:4b"].provider == "ollama"
