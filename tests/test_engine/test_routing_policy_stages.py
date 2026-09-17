"""Unit tests for the post-classifier routing policy stages.

Each stage of :mod:`opensquilla.engine.routing.policy` is exercised directly
on plain data — no classifier bundle, no TurnContext, no network. Old-vs-new
equivalence of the composed pipeline is covered separately by
``test_routing_policy_parity.py``; these tests pin each stage's trigger and
no-trigger behavior in isolation.
"""

from __future__ import annotations

from types import SimpleNamespace

from opensquilla.engine.routing import (
    PolicyInputs,
    RoutingDecision,
    RoutingPolicyEngine,
    anti_downgrade,
    bind,
    complaint_upgrade,
    confidence_gate,
    detect_complaint,
    large_context_floor,
    large_context_min_tier,
    previous_final_entry,
    previous_final_tier,
    provider_mismatch,
    reconcile_controller_with_final_tier,
    resolve_large_context_floor_tier,
)
from opensquilla.engine.routing.policy_data import COMPLAINT_TERMS

VALID_TIERS = ["c0", "c1", "c2", "c3"]

TIERS = {
    "c0": {"model": "dummy-nano-1"},
    "c1": {"model": "dummy-mini-1"},
    "c2": {"model": "dummy-pro-1"},
    "c3": {"model": "dummy-max-1"},
    "image_model": {"model": "dummy-vision-1", "supports_image": True, "image_only": True},
}


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


# ---------------------------------------------------------------------------
# confidence_gate
# ---------------------------------------------------------------------------


def test_confidence_gate_falls_back_below_margin_cutoff() -> None:
    result = confidence_gate(
        "c2", confidence=0.30, router_cfg=router_cfg(), valid_tiers=VALID_TIERS, tiers=TIERS
    )
    assert (result.tier, result.applied) == ("c1", True)
    assert result.threshold == 0.5
    assert result.default_tier == "c1"


def test_confidence_gate_margin_discount_keeps_high_tier() -> None:
    # Above-default tiers get threshold - margin (0.45); 0.46 clears it.
    result = confidence_gate(
        "c2", confidence=0.46, router_cfg=router_cfg(), valid_tiers=VALID_TIERS, tiers=TIERS
    )
    assert (result.tier, result.applied) == ("c2", False)


def test_confidence_gate_low_tier_uses_full_threshold() -> None:
    result = confidence_gate(
        "c0", confidence=0.46, router_cfg=router_cfg(), valid_tiers=VALID_TIERS, tiers=TIERS
    )
    assert (result.tier, result.applied) == ("c1", True)


def test_confidence_gate_no_default_tier_disables_gate() -> None:
    result = confidence_gate(
        "c2",
        confidence=0.0,
        router_cfg=router_cfg(default_tier=None),
        valid_tiers=VALID_TIERS,
        tiers=TIERS,
    )
    assert (result.tier, result.applied, result.default_tier) == ("c2", False, None)


def test_confidence_gate_skips_image_only_tier() -> None:
    result = confidence_gate(
        "image_model",
        confidence=0.0,
        router_cfg=router_cfg(),
        valid_tiers=VALID_TIERS,
        tiers=TIERS,
    )
    assert (result.tier, result.applied) == ("image_model", False)


def test_confidence_gate_no_change_when_already_default() -> None:
    result = confidence_gate(
        "c1", confidence=0.0, router_cfg=router_cfg(), valid_tiers=VALID_TIERS, tiers=TIERS
    )
    assert (result.tier, result.applied) == ("c1", False)


# ---------------------------------------------------------------------------
# complaint_upgrade
# ---------------------------------------------------------------------------


def test_complaint_upgrade_steps_up_one_tier() -> None:
    result = complaint_upgrade(
        "c0",
        message="wrong, try again",
        router_cfg=router_cfg(),
        valid_tiers=VALID_TIERS,
        pre_confidence_tier="c0",
        previous_tier=None,
    )
    assert (result.tier, result.applied) == ("c1", True)
    assert "wrong" in result.terms
    assert (result.steps, result.max_chars) == (1, 160)


def test_complaint_upgrade_disabled_reports_config_knobs() -> None:
    result = complaint_upgrade(
        "c0",
        message="wrong, try again",
        router_cfg=router_cfg(complaint_upgrade_enabled=False, complaint_upgrade_steps=2),
        valid_tiers=VALID_TIERS,
        pre_confidence_tier="c0",
        previous_tier=None,
    )
    assert (result.tier, result.applied, result.terms) == ("c0", False, [])
    assert result.steps == 2  # bind still records configured knobs


def test_complaint_upgrade_ignores_long_messages() -> None:
    result = complaint_upgrade(
        "c0",
        message="wrong " + "waffle " * 40,
        router_cfg=router_cfg(),
        valid_tiers=VALID_TIERS,
        pre_confidence_tier="c0",
        previous_tier=None,
    )
    assert (result.tier, result.applied, result.terms) == ("c0", False, [])


def test_complaint_upgrade_restarts_from_pre_confidence_tier() -> None:
    result = complaint_upgrade(
        "c1",  # post-gate tier
        message="try again",
        router_cfg=router_cfg(),
        valid_tiers=VALID_TIERS,
        pre_confidence_tier="c2",
        previous_tier=None,
    )
    assert (result.tier, result.applied) == ("c3", True)


def test_complaint_upgrade_restarts_from_previous_tier() -> None:
    result = complaint_upgrade(
        "c0",
        message="redo",
        router_cfg=router_cfg(),
        valid_tiers=VALID_TIERS,
        pre_confidence_tier="c0",
        previous_tier="c2",
    )
    assert (result.tier, result.applied) == ("c3", True)


def test_complaint_upgrade_caps_at_highest_tier() -> None:
    result = complaint_upgrade(
        "c3",
        message="redo",
        router_cfg=router_cfg(),
        valid_tiers=VALID_TIERS,
        pre_confidence_tier="c3",
        previous_tier=None,
    )
    assert (result.tier, result.applied) == ("c3", False)


def test_complaint_upgrade_ranks_canonically_when_tiers_declared_out_of_order() -> None:
    result = complaint_upgrade(
        "c2",
        message="wrong",
        router_cfg=router_cfg(),
        valid_tiers=["c3", "c2", "c1", "c0"],
        pre_confidence_tier="c2",
        previous_tier=None,
    )
    assert (result.tier, result.applied) == ("c3", True)


def test_detect_complaint_matches_zh_and_en_terms() -> None:
    assert detect_complaint("这个不对，请重写") == ["不对", "重写"]
    assert "try again" in detect_complaint("please try again")
    assert detect_complaint("all good, thanks") == []


def test_complaint_terms_list_shape() -> None:
    # The list is data moved verbatim; guard against accidental edits.
    assert len(COMPLAINT_TERMS) == len(set(COMPLAINT_TERMS)) == 101


# ---------------------------------------------------------------------------
# anti_downgrade
# ---------------------------------------------------------------------------


def test_anti_downgrade_holds_previous_higher_tier() -> None:
    result = anti_downgrade(
        "c1", router_cfg=router_cfg(), valid_tiers=VALID_TIERS, previous_tier="c3"
    )
    assert (result.tier, result.applied) == ("c3", True)


def test_anti_downgrade_no_previous_tier() -> None:
    result = anti_downgrade(
        "c1", router_cfg=router_cfg(), valid_tiers=VALID_TIERS, previous_tier=None
    )
    assert (result.tier, result.applied) == ("c1", False)


def test_anti_downgrade_previous_lower_is_ignored() -> None:
    result = anti_downgrade(
        "c2", router_cfg=router_cfg(), valid_tiers=VALID_TIERS, previous_tier="c0"
    )
    assert (result.tier, result.applied) == ("c2", False)


def test_anti_downgrade_ranks_canonically_when_tiers_declared_out_of_order() -> None:
    result = anti_downgrade(
        "c3",
        router_cfg=router_cfg(),
        valid_tiers=["c3", "c2", "c1", "c0"],
        previous_tier="c0",
    )
    assert (result.tier, result.applied) == ("c3", False)


def test_anti_downgrade_disabled() -> None:
    result = anti_downgrade(
        "c1",
        router_cfg=router_cfg(kv_cache_anti_downgrade_enabled=False),
        valid_tiers=VALID_TIERS,
        previous_tier="c3",
    )
    assert (result.tier, result.applied) == ("c1", False)


def test_anti_downgrade_unknown_working_tier_is_ignored() -> None:
    result = anti_downgrade(
        "zz", router_cfg=router_cfg(), valid_tiers=VALID_TIERS, previous_tier="c3"
    )
    assert (result.tier, result.applied) == ("zz", False)


# ---------------------------------------------------------------------------
# previous-turn lookup
# ---------------------------------------------------------------------------


def test_previous_final_entry_respects_window() -> None:
    history = [{"final_tier": "c3", "_ts": 1000.0}]
    assert previous_final_entry(history, now=1500.0, window=600.0) == history[0]
    assert previous_final_entry(history, now=1700.0, window=600.0) is None
    assert previous_final_entry(None, now=1500.0, window=600.0) is None


def test_previous_final_entry_scans_back_to_recent_entry() -> None:
    history = [
        {"final_tier": "c3", "_ts": 1400.0},
        {"final_tier": "c0", "_ts": 100.0},
    ]
    entry = previous_final_entry(history, now=1500.0, window=600.0)
    assert entry is not None and entry["final_tier"] == "c3"


def test_previous_final_tier_prefers_final_tier_then_route_class() -> None:
    assert previous_final_tier({"final_tier": "t2"}) == "c2"  # legacy alias normalized
    assert previous_final_tier({"route_class": "R3"}) == "c3"
    assert previous_final_tier({"final_route_class": "R0"}) == "c0"
    assert previous_final_tier(None) is None


# ---------------------------------------------------------------------------
# bind
# ---------------------------------------------------------------------------


def test_bind_records_trail_and_rebinds_model() -> None:
    cfg = router_cfg()
    decision = RoutingDecision(tier="c2", model="dummy-pro-1", confidence=0.3, source="v4_phase3")
    extra: dict = {}
    gate = confidence_gate(
        "c2", confidence=0.3, router_cfg=cfg, valid_tiers=VALID_TIERS, tiers=TIERS
    )
    complaint = complaint_upgrade(
        gate.tier,
        message="try again",
        router_cfg=cfg,
        valid_tiers=VALID_TIERS,
        pre_confidence_tier="c2",
        previous_tier=None,
    )
    downgrade = anti_downgrade(
        complaint.tier, router_cfg=cfg, valid_tiers=VALID_TIERS, previous_tier=None
    )
    bound = bind(
        decision,
        final_tier=downgrade.tier,
        tiers=TIERS,
        extra=extra,
        base_tier="c2",
        pre_confidence_tier="c2",
        gate=gate,
        complaint=complaint,
        downgrade=downgrade,
        previous_tier=None,
        previous_route_class=None,
        window=600.0,
    )
    assert bound == RoutingDecision(
        tier="c3", model="dummy-max-1", confidence=0.3, source="v4_phase3"
    )
    assert extra["base_tier"] == "c2"
    assert extra["pre_confidence_tier"] == "c2"
    assert extra["confidence_gate_applied"] is True
    assert extra["complaint_upgrade_applied"] is True
    assert extra["anti_downgrade_applied"] is False
    assert extra["final_tier"] == "c3"
    assert extra["final_route_class"] == "R3"
    assert extra["kv_cache_window_seconds"] == 600.0


# ---------------------------------------------------------------------------
# controller reconcile
# ---------------------------------------------------------------------------


def test_reconcile_noop_when_final_matches_base() -> None:
    extra = {"base_tier": "c1", "final_tier": "c1"}
    assert reconcile_controller_with_final_tier("T1", "P1", extra) == ("T1", "P1")
    assert "controller_reconciled" not in extra


def test_reconcile_promotes_thinking_and_prompt() -> None:
    extra = {"base_tier": "c0", "final_tier": "c3", "complaint_detected": False}
    thinking, prompt = reconcile_controller_with_final_tier("T0", "P0", extra)
    assert (thinking, prompt) == ("T3", "P1")
    assert extra["controller_reconciled"] is True
    assert extra["base_thinking_mode"] == "T0"
    assert extra["base_prompt_policy"] == "P0"


def test_reconcile_complaint_forces_full_prompt_on_low_tier() -> None:
    extra = {"base_tier": "c0", "final_tier": "c1", "complaint_detected": True}
    thinking, prompt = reconcile_controller_with_final_tier("T1", "P0", extra)
    assert (thinking, prompt) == ("T1", "P1")


def test_reconcile_records_no_change() -> None:
    extra = {"base_tier": "c0", "final_tier": "c1"}
    thinking, prompt = reconcile_controller_with_final_tier("T2", "P1", extra)
    assert (thinking, prompt) == ("T2", "P1")
    assert extra["controller_reconciled"] is False


# ---------------------------------------------------------------------------
# large_context_floor
# ---------------------------------------------------------------------------


def test_large_context_min_tier_boundaries() -> None:
    assert large_context_min_tier(24_999, 200_000) is None
    assert large_context_min_tier(25_000, 200_000) == "c2"
    assert large_context_min_tier(79_999, 200_000) == "c2"
    assert large_context_min_tier(80_000, 200_000) == "c3"
    # 40% of the context window kicks in below the absolute c3 floor.
    assert large_context_min_tier(20_000, 50_000) == "c3"
    assert large_context_min_tier(19_999, 50_000) is None


def test_large_context_floor_raises_tier_and_reports_metadata() -> None:
    decision = RoutingDecision(tier="c0", model="dummy-nano-1", confidence=0.9, source="v4_phase3")
    extra: dict = {"base_tier": "c0", "final_tier": "c0"}
    updates: dict = {}
    floored = large_context_floor(
        decision,
        tiers=TIERS,
        valid_tiers=VALID_TIERS,
        material_tokens=30_000,
        context_window_tokens=200_000,
        extra=extra,
        metadata_updates=updates,
    )
    assert floored.tier == "c2"
    assert floored.model == "dummy-pro-1"
    assert floored.source == "large_context_floor"
    assert updates == {
        "large_context_floor_from_tier": "c0",
        "large_context_material_tokens": 30_000,
    }
    assert extra["large_context_floor_applied"] is True
    assert extra["large_context_pre_floor_source"] == "v4_phase3"
    assert extra["final_tier"] == "c2"
    assert extra["final_route_class"] == "R2"


def test_large_context_floor_without_extra_dict() -> None:
    decision = RoutingDecision(tier="c0", model="dummy-nano-1", confidence=0.9, source="v4_phase3")
    updates: dict = {}
    floored = large_context_floor(
        decision,
        tiers=TIERS,
        valid_tiers=VALID_TIERS,
        material_tokens=90_000,
        context_window_tokens=200_000,
        extra=None,
        metadata_updates=updates,
    )
    assert floored.tier == "c3"
    assert updates["large_context_floor_from_tier"] == "c0"


def test_large_context_floor_uses_next_configured_tier_when_exact_tier_missing() -> None:
    tiers = {name: cfg for name, cfg in TIERS.items() if name != "c2"}
    valid_tiers = ["c0", "c1", "c3"]
    decision = RoutingDecision(
        tier="c0",
        model="dummy-nano-1",
        confidence=0.9,
        source="v4_phase3",
    )
    updates: dict = {}

    floored = large_context_floor(
        decision,
        tiers=tiers,
        valid_tiers=valid_tiers,
        material_tokens=30_000,
        context_window_tokens=200_000,
        extra=None,
        metadata_updates=updates,
    )

    assert resolve_large_context_floor_tier("c2", valid_tiers) == "c3"
    assert floored.tier == "c3"
    assert floored.model == "dummy-max-1"


def test_large_context_floor_no_trigger_paths() -> None:
    high = RoutingDecision(tier="c3", model="dummy-max-1", confidence=0.9, source="v4_phase3")
    unknown = RoutingDecision(tier="zz", model="dummy-x", confidence=0.9, source="v4_phase3")
    updates: dict = {}
    assert (
        large_context_floor(
            high,
            tiers=TIERS,
            valid_tiers=VALID_TIERS,
            material_tokens=30_000,
            context_window_tokens=200_000,
            extra=None,
            metadata_updates=updates,
        )
        is high
    )
    assert (
        large_context_floor(
            unknown,
            tiers=TIERS,
            valid_tiers=VALID_TIERS,
            material_tokens=90_000,
            context_window_tokens=200_000,
            extra=None,
            metadata_updates=updates,
        )
        is unknown
    )
    assert updates == {}


# ---------------------------------------------------------------------------
# provider_mismatch (flag-only)
# ---------------------------------------------------------------------------


def test_provider_mismatch_skipped_when_not_applied() -> None:
    outcome = provider_mismatch(
        tiers={"c2": {"model": "dummy-pro-1", "provider": "otherprov"}},
        tier_name="c2",
        routing_applied=False,
        active_provider="mainprov",
        cross_provider_tiers=False,
    )
    assert (outcome.outcome, outcome.routed_provider) == ("skipped", None)


def test_provider_mismatch_flags_other_provider() -> None:
    outcome = provider_mismatch(
        tiers={"c2": {"model": "dummy-pro-1", "provider": "OtherProv"}},
        tier_name="c2",
        routing_applied=True,
        active_provider="mainprov",
        cross_provider_tiers=False,
    )
    assert outcome.outcome == "mismatch"
    assert outcome.routed_provider == "otherprov"
    assert outcome.tier_provider == "OtherProv"  # original casing preserved for the flag
    assert outcome.tier_model == "dummy-pro-1"


def test_provider_mismatch_cross_provider_is_informational() -> None:
    outcome = provider_mismatch(
        tiers={"c2": {"model": "dummy-pro-1", "provider": "otherprov"}},
        tier_name="c2",
        routing_applied=True,
        active_provider="mainprov",
        cross_provider_tiers=True,
    )
    assert (outcome.outcome, outcome.routed_provider) == ("cross_provider", "otherprov")


def test_provider_mismatch_match_and_blank_cases() -> None:
    match = provider_mismatch(
        tiers={"c2": {"model": "dummy-pro-1", "provider": "MainProv"}},
        tier_name="c2",
        routing_applied=True,
        active_provider=" mainprov ",
        cross_provider_tiers=False,
    )
    assert (match.outcome, match.routed_provider) == ("match", "mainprov")
    no_provider = provider_mismatch(
        tiers={"c2": {"model": "dummy-pro-1"}},
        tier_name="c2",
        routing_applied=True,
        active_provider="mainprov",
        cross_provider_tiers=False,
    )
    assert (no_provider.outcome, no_provider.routed_provider) == ("match", None)
    blank_active = provider_mismatch(
        tiers={"c2": {"model": "dummy-pro-1", "provider": "otherprov"}},
        tier_name="c2",
        routing_applied=True,
        active_provider="",
        cross_provider_tiers=False,
    )
    assert (blank_active.outcome, blank_active.routed_provider) == ("match", "otherprov")


# ---------------------------------------------------------------------------
# RoutingPolicyEngine composition
# ---------------------------------------------------------------------------


def _inputs(**overrides: object) -> PolicyInputs:
    values: dict = {
        "decision": RoutingDecision(
            tier="c1", model="dummy-mini-1", confidence=0.9, source="v4_phase3"
        ),
        "message": "please summarize this short note",
        "router_cfg": router_cfg(),
        "tiers": TIERS,
        "valid_tiers": VALID_TIERS,
        "routing_history": None,
        "extra": {},
        "thinking_mode": "T1",
        "prompt_policy": "P1",
        "history_strategy": True,
        "material_estimated_tokens": 100,
        "context_window_tokens": 200_000,
        "now": 10_000.0,
    }
    values.update(overrides)
    return PolicyInputs(**values)


def test_engine_runs_stages_in_legacy_order() -> None:
    # Gate pulls c2@0.30 down to c1, the complaint restarts from the pre-gate
    # tier (c2) and lands on c3 — proving the gate -> complaint interaction.
    extra: dict = {}
    result = RoutingPolicyEngine().run(
        _inputs(
            decision=RoutingDecision(
                tier="c2", model="dummy-pro-1", confidence=0.30, source="v4_phase3"
            ),
            message="try again",
            extra=extra,
        )
    )
    assert result.decision.tier == "c3"
    assert extra["confidence_gate_applied"] is True
    assert extra["complaint_upgrade_applied"] is True
    assert result.thinking_mode == "T3"  # reconciled against the final tier


def test_engine_uses_injected_clock_for_previous_entry() -> None:
    extra: dict = {}
    history = [{"final_tier": "c3", "final_route_class": "R3", "_ts": 9_700.0}]
    result = RoutingPolicyEngine().run(
        _inputs(routing_history=history, extra=extra, now=10_000.0)
    )
    assert result.decision.tier == "c3"
    assert extra["anti_downgrade_applied"] is True
    stale = RoutingPolicyEngine().run(
        _inputs(
            routing_history=[dict(history[0])],
            extra={},
            now=10_400.0,  # entry now falls outside the 600s window
        )
    )
    assert stale.decision.tier == "c1"


def test_engine_skips_finalize_for_non_history_strategy() -> None:
    extra: dict = {}
    result = RoutingPolicyEngine().run(
        _inputs(history_strategy=False, extra=extra, material_estimated_tokens=30_000)
    )
    # No finalize trail, but the large-context floor still applies.
    assert "confidence_gate_applied" not in extra
    assert result.decision.tier == "c2"
    assert result.metadata_updates["large_context_material_tokens"] == 30_000


def test_engine_floor_reconciles_thinking_after_finalize() -> None:
    extra: dict = {}
    result = RoutingPolicyEngine().run(
        _inputs(
            decision=RoutingDecision(
                tier="c0", model="dummy-nano-1", confidence=0.9, source="v4_phase3"
            ),
            thinking_mode="T0",
            prompt_policy="P0",
            extra=extra,
            material_estimated_tokens=85_000,
        )
    )
    assert result.decision.source == "large_context_floor"
    assert result.decision.tier == "c3"
    assert (result.thinking_mode, result.prompt_policy) == ("T3", "P1")
    assert extra["controller_reconciled"] is True
