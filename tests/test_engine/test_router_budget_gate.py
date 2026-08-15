"""Unit tests for the additive router budget gate.

The gate reads a session's accumulated spend (assembled by the router step
and passed in as plain data) and, when spend crosses a configured limit,
either warns (annotate + log, tier UNCHANGED) or caps (lower the tier). The
default state — no configured limit — is a complete no-op, and an unknown
spend/price SUSPENDS rather than acting on missing data.

Everything here runs on plain dataclasses: no classifier bundle, no
TurnContext, no network, no session storage. Spend figures are synthetic.
Old-vs-new parity of the full pipeline with the gate off is pinned separately
by ``test_routing_policy_parity.py``; the paired-run below re-proves it at the
engine level (default/None budget == a suspended budget == no gate).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.engine.routing import (
    BudgetGateInput,
    PolicyInputs,
    RoutingDecision,
    RoutingPolicyEngine,
    apply_budget_gate,
    budget_gate,
    route_class_for_tier,
)
from opensquilla.engine.steps.squilla_router import _session_accumulated_spend

VALID_TIERS = ["c0", "c1", "c2", "c3"]

TIERS = {
    "c0": {"model": "dummy-nano-1"},
    "c1": {"model": "dummy-mini-1"},
    "c2": {"model": "dummy-pro-1"},
    "c3": {"model": "dummy-max-1"},
}


def budget_input(**overrides: object) -> BudgetGateInput:
    knobs: dict[str, object] = {
        "action": "warn",
        "limit_usd": 1.0,
        "spend_usd": 5.0,
        "spend_source": "billed",
        "session_key": "agent:budget:main",
    }
    knobs.update(overrides)
    return BudgetGateInput(**knobs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# budget_gate — pure decision logic
# ---------------------------------------------------------------------------


def test_off_when_input_none() -> None:
    result = budget_gate("c2", valid_tiers=VALID_TIERS, budget=None)
    assert (result.tier, result.outcome) == ("c2", "off")


def test_warn_over_limit_keeps_tier() -> None:
    result = budget_gate("c2", valid_tiers=VALID_TIERS, budget=budget_input(spend_usd=5.0))
    assert (result.tier, result.outcome, result.action) == ("c2", "warn", "warn")
    assert result.spend_usd == 5.0
    assert result.limit_usd == 1.0


def test_under_limit_is_noop() -> None:
    result = budget_gate("c2", valid_tiers=VALID_TIERS, budget=budget_input(spend_usd=0.5))
    assert (result.tier, result.outcome) == ("c2", "under_limit")


def test_at_limit_boundary_is_noop() -> None:
    # spend == limit is not "exceeds": stay under.
    result = budget_gate("c2", valid_tiers=VALID_TIERS, budget=budget_input(spend_usd=1.0))
    assert result.outcome == "under_limit"


def test_cap_lowers_to_cap_tier() -> None:
    result = budget_gate(
        "c3",
        valid_tiers=VALID_TIERS,
        budget=budget_input(action="cap", cap_tier="c0", spend_usd=9.0),
    )
    assert (result.tier, result.outcome, result.from_tier) == ("c0", "cap", "c3")


def test_cap_never_raises_when_target_at_or_above_current() -> None:
    # Working tier c0, cap target c2: capping would RAISE — degrade to warn.
    result = budget_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        budget=budget_input(action="cap", cap_tier="c2", spend_usd=9.0),
    )
    assert (result.tier, result.outcome) == ("c0", "warn")


def test_cap_with_unknown_target_degrades_to_warn() -> None:
    result = budget_gate(
        "c3",
        valid_tiers=VALID_TIERS,
        budget=budget_input(action="cap", cap_tier="zz_unknown", spend_usd=9.0),
    )
    assert (result.tier, result.outcome) == ("c3", "warn")


def test_suspends_on_unknown_spend() -> None:
    # No billed/estimated total yet (or a required price could not be priced):
    # never act on missing data.
    result = budget_gate(
        "c2",
        valid_tiers=VALID_TIERS,
        budget=budget_input(spend_usd=None, spend_source="unknown"),
    )
    assert (result.tier, result.outcome) == ("c2", "suspended")


def test_cap_suspends_on_unknown_spend() -> None:
    # A cap action must also suspend on unknown spend — no downgrade on a guess.
    result = budget_gate(
        "c3",
        valid_tiers=VALID_TIERS,
        budget=budget_input(action="cap", cap_tier="c0", spend_usd=None),
    )
    assert (result.tier, result.outcome) == ("c3", "suspended")


def test_forward_estimate_pushes_over_limit() -> None:
    # spend alone (0.9) is under; spend + estimate (1.1) crosses.
    result = budget_gate(
        "c2",
        valid_tiers=VALID_TIERS,
        budget=budget_input(spend_usd=0.9, estimate_usd=0.2),
    )
    assert result.outcome == "warn"
    assert result.projected_usd == pytest.approx(1.1)


def test_absent_estimate_compares_spend_alone() -> None:
    result = budget_gate(
        "c2",
        valid_tiers=VALID_TIERS,
        budget=budget_input(spend_usd=0.9, estimate_usd=None),
    )
    assert result.outcome == "under_limit"


# ---------------------------------------------------------------------------
# apply_budget_gate — decision + metadata effects
# ---------------------------------------------------------------------------


def _decision(tier: str = "c2") -> RoutingDecision:
    return RoutingDecision(
        tier=tier, model=TIERS[tier]["model"], confidence=0.9, source="v4_phase3"
    )


def test_apply_off_writes_nothing() -> None:
    extra: dict = {"route_class": "R2"}
    meta: dict = {}
    decision = _decision("c2")
    result = budget_gate("c2", valid_tiers=VALID_TIERS, budget=None)
    out = apply_budget_gate(decision, result, tiers=TIERS, extra=extra, metadata_updates=meta)
    assert out is decision
    assert meta == {}
    assert extra == {"route_class": "R2"}


def test_apply_suspended_writes_nothing() -> None:
    extra: dict = {"route_class": "R2"}
    meta: dict = {}
    decision = _decision("c2")
    result = budget_gate(
        "c2", valid_tiers=VALID_TIERS, budget=budget_input(spend_usd=None)
    )
    out = apply_budget_gate(decision, result, tiers=TIERS, extra=extra, metadata_updates=meta)
    assert out.tier == "c2"
    assert meta == {}
    assert extra == {"route_class": "R2"}


def test_apply_under_limit_writes_nothing() -> None:
    extra: dict = {"route_class": "R2"}
    meta: dict = {}
    decision = _decision("c2")
    result = budget_gate(
        "c2", valid_tiers=VALID_TIERS, budget=budget_input(spend_usd=0.1)
    )
    out = apply_budget_gate(decision, result, tiers=TIERS, extra=extra, metadata_updates=meta)
    assert out.tier == "c2"
    assert meta == {}
    assert extra == {"route_class": "R2"}


def test_apply_warn_annotates_but_keeps_tier() -> None:
    extra: dict = {"route_class": "R2"}
    meta: dict = {}
    decision = _decision("c2")
    result = budget_gate("c2", valid_tiers=VALID_TIERS, budget=budget_input(spend_usd=5.0))
    out = apply_budget_gate(decision, result, tiers=TIERS, extra=extra, metadata_updates=meta)
    # Tier + model UNCHANGED.
    assert (out.tier, out.model, out.source) == ("c2", "dummy-pro-1", "v4_phase3")
    assert meta["router_budget_applied"] is True
    assert meta["router_budget_outcome"] == "warn"
    assert meta["router_budget_action"] == "warn"
    assert meta["router_budget_spend_usd"] == 5.0
    assert meta["router_budget_limit_usd"] == 1.0
    trail = extra["routing_trail"]
    assert trail[-1]["stage"] == "budget_gate"
    assert trail[-1]["rule"] == "warn"
    assert extra["budget_gate_applied"] is True


def test_apply_cap_rebinds_tier_and_model() -> None:
    extra: dict = {"route_class": "R3", "final_tier": "c3"}
    meta: dict = {}
    decision = _decision("c3")
    result = budget_gate(
        "c3",
        valid_tiers=VALID_TIERS,
        budget=budget_input(action="cap", cap_tier="c1", spend_usd=9.0),
    )
    out = apply_budget_gate(decision, result, tiers=TIERS, extra=extra, metadata_updates=meta)
    assert (out.tier, out.model, out.source) == ("c1", "dummy-mini-1", "budget_cap")
    assert meta["router_budget_outcome"] == "cap"
    assert meta["router_budget_from_tier"] == "c3"
    assert meta["router_budget_to_tier"] == "c1"
    assert extra["final_tier"] == "c1"
    assert extra["final_route_class"] == route_class_for_tier("c1")
    trail = extra["routing_trail"]
    assert trail[-1] == {
        "stage": "budget_gate",
        "rule": "cap",
        "spend_usd": 9.0,
        "limit_usd": 1.0,
        "spend_source": "billed",
        "from_tier": "c3",
        "to_tier": "c1",
    }


def test_apply_warn_records_spend_source_in_trail_and_metadata() -> None:
    # A warn outcome threads the spend basis into both the metadata trace and
    # the routing-trail entry so the decision is auditable.
    extra: dict = {"route_class": "R2"}
    meta: dict = {}
    decision = _decision("c2")
    result = budget_gate(
        "c2",
        valid_tiers=VALID_TIERS,
        budget=budget_input(spend_usd=5.0, spend_source="estimate_mixed"),
    )
    apply_budget_gate(decision, result, tiers=TIERS, extra=extra, metadata_updates=meta)
    assert meta["router_budget_spend_source"] == "estimate_mixed"
    assert extra["routing_trail"][-1]["spend_source"] == "estimate_mixed"


# ---------------------------------------------------------------------------
# _session_accumulated_spend — seeded provenance -> spend source
# ---------------------------------------------------------------------------


def _spend_ctx(**metadata: object) -> SimpleNamespace:
    return SimpleNamespace(metadata=dict(metadata))


def test_accumulated_spend_billed_ignores_rollup_label() -> None:
    # A positive billed total is authoritative regardless of the rollup label.
    spend, source = _session_accumulated_spend(
        _spend_ctx(
            session_billed_cost_usd=3.0,
            session_total_cost_usd=3.0,
            session_cost_source="mixed",
        )
    )
    assert (spend, source) == (3.0, "billed")


def test_accumulated_spend_estimate_label_maps_to_estimate() -> None:
    spend, source = _session_accumulated_spend(
        _spend_ctx(
            session_billed_cost_usd=0.0,
            session_total_cost_usd=2.5,
            session_estimated_cost_usd=2.5,
            session_cost_source="opensquilla_estimate",
        )
    )
    assert (spend, source) == (2.5, "estimate")


def test_accumulated_spend_mixed_label_maps_to_estimate_mixed() -> None:
    spend, source = _session_accumulated_spend(
        _spend_ctx(
            session_billed_cost_usd=0.0,
            session_total_cost_usd=4.0,
            session_cost_source="mixed",
        )
    )
    assert (spend, source) == (4.0, "estimate_mixed")


def test_accumulated_spend_absent_label_stays_estimate() -> None:
    spend, source = _session_accumulated_spend(_spend_ctx(session_total_cost_usd=1.5))
    assert (spend, source) == (1.5, "estimate")


def test_accumulated_spend_unknown_and_none_preserved() -> None:
    # No spend keys at all -> unknown (the gate suspends on missing data).
    assert _session_accumulated_spend(_spend_ctx()) == (None, "unknown")
    # Keys present but all zero -> a known-zero spend, not an unknown one, even
    # with a rollup label present.
    assert _session_accumulated_spend(
        _spend_ctx(
            session_billed_cost_usd=0.0,
            session_total_cost_usd=0.0,
            session_cost_source="mixed",
        )
    ) == (0.0, "none")


# ---------------------------------------------------------------------------
# Full-engine integration + default-path parity
# ---------------------------------------------------------------------------


def router_cfg(**overrides: object) -> SimpleNamespace:
    knobs: dict[str, object] = {
        "default_tier": "c1",
        "confidence_threshold": 0.5,
        "confidence_high_tier_margin": 0.05,
        "complaint_upgrade_enabled": True,
        "complaint_upgrade_steps": 1,
        "complaint_upgrade_max_chars": 160,
        "kv_cache_anti_downgrade_enabled": True,
        "kv_cache_anti_downgrade_window_seconds": 600,
    }
    knobs.update(overrides)
    return SimpleNamespace(**knobs)


def make_inputs(
    *,
    tier: str = "c2",
    confidence: float = 0.9,
    tokens: int = 0,
    budget: BudgetGateInput | None = None,
    extra: dict | None = None,
) -> PolicyInputs:
    return PolicyInputs(
        decision=RoutingDecision(
            tier=tier, model=TIERS[tier]["model"], confidence=confidence, source="v4_phase3"
        ),
        message="please summarize this short note",
        router_cfg=router_cfg(),
        tiers=TIERS,
        valid_tiers=VALID_TIERS,
        routing_history=None,
        extra=extra if extra is not None else {"route_class": route_class_for_tier(tier)},
        thinking_mode="T2",
        prompt_policy="P1",
        history_strategy=True,
        material_estimated_tokens=tokens,
        context_window_tokens=200_000,
        now=1000.0,
        budget=budget,
    )


def test_engine_warn_over_limit_keeps_routed_tier() -> None:
    engine = RoutingPolicyEngine()
    result = engine.run(
        make_inputs(tier="c2", budget=budget_input(action="warn", limit_usd=1.0, spend_usd=9.0))
    )
    assert result.decision.tier == "c2"  # warn never changes the tier
    assert result.metadata_updates["router_budget_outcome"] == "warn"
    assert result.metadata_updates["router_budget_applied"] is True


def test_engine_cap_lowers_routed_tier() -> None:
    engine = RoutingPolicyEngine()
    result = engine.run(
        make_inputs(
            tier="c3",
            confidence=0.99,
            budget=budget_input(action="cap", limit_usd=1.0, spend_usd=9.0, cap_tier="c0"),
        )
    )
    assert result.decision.tier == "c0"
    assert result.decision.model == "dummy-nano-1"
    assert result.decision.source == "budget_cap"
    assert result.metadata_updates["router_budget_to_tier"] == "c0"


def test_engine_budget_cap_cannot_lower_large_context_floor() -> None:
    engine = RoutingPolicyEngine()
    result = engine.run(
        make_inputs(
            tier="c0",
            confidence=0.99,
            tokens=90_000,
            budget=budget_input(
                action="cap",
                limit_usd=1.0,
                spend_usd=9.0,
                cap_tier="c0",
            ),
        )
    )

    assert result.decision.tier == "c3"
    assert result.decision.source == "large_context_floor"
    assert result.metadata_updates["large_context_floor_min_tier"] == "c3"
    assert result.metadata_updates["router_budget_outcome"] == "warn"


def test_engine_default_budget_writes_no_budget_metadata() -> None:
    engine = RoutingPolicyEngine()
    result = engine.run(make_inputs(tier="c2", budget=None))
    budget_keys = [k for k in result.metadata_updates if k.startswith("router_budget")]
    assert budget_keys == []


# The parity matrix: default (None) budget must equal an active-but-suspended
# budget must equal an active-but-under-limit budget — all three are complete
# no-ops. This re-proves, at the engine level, that the additive stage does not
# perturb routing output when it does not act.
PARITY_SCENARIOS = [
    {"tier": "c2", "confidence": 0.9, "tokens": 0},
    {"tier": "c0", "confidence": 0.40, "tokens": 0},
    {"tier": "c3", "confidence": 0.20, "tokens": 0},
    {"tier": "c1", "confidence": 0.9, "tokens": 30_000},
    {"tier": "c0", "confidence": 0.9, "tokens": 90_000},
]


def _engine_snapshot(scenario: dict, budget: BudgetGateInput | None) -> tuple:
    engine = RoutingPolicyEngine()
    extra: dict = {"route_class": route_class_for_tier(scenario["tier"])}
    result = engine.run(make_inputs(budget=budget, extra=extra, **scenario))
    return (
        result.decision.tier,
        result.decision.model,
        result.decision.source,
        result.decision.confidence,
        result.thinking_mode,
        result.prompt_policy,
        extra,
        result.metadata_updates,
    )


@pytest.mark.parametrize("scenario", PARITY_SCENARIOS)
def test_default_budget_matches_suspended_and_under_limit(scenario: dict) -> None:
    baseline = _engine_snapshot(scenario, None)
    suspended = _engine_snapshot(
        scenario, budget_input(limit_usd=1.0, spend_usd=None, spend_source="unknown")
    )
    under_limit = _engine_snapshot(scenario, budget_input(limit_usd=1_000_000.0, spend_usd=0.01))
    assert baseline == suspended
    assert baseline == under_limit
