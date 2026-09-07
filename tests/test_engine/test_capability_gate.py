"""Unit tests for the capability_gate routing stage and its catalog facts.

The gate acts only on DEFINITE catalog signals: an unknown model, a
synthesized entry, or the anthropic/ollama flag-gated empty capabilities
must produce no action ("never act on ignorance"). The composed default —
no capability data at all — is byte-identical to the pre-gate pipeline and
is pinned separately by ``test_routing_policy_parity.py``.

All model/tier/provider names are synthetic dummy data; the catalog is
populated in-process (no network, no credentials).
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.routing import (
    CapabilityGateAction,
    PolicyInputs,
    RoutingDecision,
    RoutingPolicyEngine,
    TierCapability,
    capability_gate,
    record_capability_gate_trail,
)
from opensquilla.engine.steps import squilla_router as squilla_router_step
from opensquilla.engine.steps.squilla_router import _tier_capability_facts
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog

VALID_TIERS = ["c0", "c1", "c2", "c3"]

TIERS = {
    "c0": {"model": "dummy-nano-1"},
    "c1": {"model": "dummy-mini-1"},
    "c2": {"model": "dummy-pro-1"},
    "c3": {"model": "dummy-max-1"},
}


def caps(**overrides: TierCapability) -> dict[str, TierCapability]:
    facts = {name: TierCapability() for name in VALID_TIERS}
    facts.update(overrides)
    return facts


# ---------------------------------------------------------------------------
# vision walk-up
# ---------------------------------------------------------------------------


def test_vision_walk_up_to_nearest_vision_tier() -> None:
    result = capability_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(
            c0=TierCapability(supports_vision=False),
            c2=TierCapability(supports_vision=True),
            c3=TierCapability(supports_vision=True),
        ),
        turn_has_image=True,
        material_tokens=100,
    )
    assert result.tier == "c2"  # nearest definite vision tier, not the top
    assert result.actions == (CapabilityGateAction("vision_walk_up", "c0", "c2"),)


def test_vision_unknown_caps_no_op() -> None:
    # supports_vision=None covers unknown models, synthesized entries, and
    # the anthropic/ollama flag-gated empty capabilities: no action.
    result = capability_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(c2=TierCapability(supports_vision=True)),
        turn_has_image=True,
        material_tokens=100,
    )
    assert (result.tier, result.actions) == ("c0", ())


def test_vision_no_definite_target_no_op() -> None:
    # The working tier definitely lacks vision but no tier above is
    # definitely vision-capable: moving anyway would be acting on ignorance.
    result = capability_gate(
        "c1",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(
            c1=TierCapability(supports_vision=False),
            c2=TierCapability(supports_vision=False),
        ),
        turn_has_image=True,
        material_tokens=100,
    )
    assert (result.tier, result.actions) == ("c1", ())


def test_vision_rule_requires_image_turn() -> None:
    result = capability_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(
            c0=TierCapability(supports_vision=False),
            c2=TierCapability(supports_vision=True),
        ),
        turn_has_image=False,
        material_tokens=100,
    )
    assert (result.tier, result.actions) == ("c0", ())


# ---------------------------------------------------------------------------
# context walk-up
# ---------------------------------------------------------------------------


def test_context_walk_up_to_first_fitting_tier() -> None:
    result = capability_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(
            c0=TierCapability(context_window=8_000),
            c2=TierCapability(context_window=200_000),
        ),
        turn_has_image=False,
        material_tokens=30_000,
    )
    assert result.tier == "c2"  # c1's window is unknown, so it cannot qualify
    assert result.actions == (CapabilityGateAction("context_walk_up", "c0", "c2"),)


def test_context_unknown_window_no_op() -> None:
    result = capability_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(c2=TierCapability(context_window=200_000)),
        turn_has_image=False,
        material_tokens=500_000,
    )
    assert (result.tier, result.actions) == ("c0", ())


def test_context_fitting_window_no_op() -> None:
    result = capability_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(c0=TierCapability(context_window=64_000)),
        turn_has_image=False,
        material_tokens=30_000,
    )
    assert (result.tier, result.actions) == ("c0", ())


def test_context_top_tier_saturation_when_nothing_fits() -> None:
    result = capability_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(
            c0=TierCapability(context_window=8_000),
            c3=TierCapability(context_window=100_000),
        ),
        turn_has_image=False,
        material_tokens=500_000,
    )
    assert result.tier == "c3"
    assert result.actions == (CapabilityGateAction("context_walk_up", "c0", "c3"),)


def test_context_already_top_tier_no_op() -> None:
    result = capability_gate(
        "c3",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(c3=TierCapability(context_window=8_000)),
        turn_has_image=False,
        material_tokens=30_000,
    )
    assert (result.tier, result.actions) == ("c3", ())


def test_vision_then_context_walk_up_compose() -> None:
    result = capability_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(
            c0=TierCapability(supports_vision=False),
            c1=TierCapability(supports_vision=True, context_window=8_000),
            c3=TierCapability(supports_vision=True, context_window=200_000),
        ),
        turn_has_image=True,
        material_tokens=30_000,
    )
    assert result.tier == "c3"
    assert result.actions == (
        CapabilityGateAction("vision_walk_up", "c0", "c1"),
        CapabilityGateAction("context_walk_up", "c1", "c3"),
    )


# ---------------------------------------------------------------------------
# no-signal defaults + trail recording
# ---------------------------------------------------------------------------


def test_no_capability_data_is_strict_no_op() -> None:
    for tier_capabilities in (None, {}):
        result = capability_gate(
            "c0",
            valid_tiers=VALID_TIERS,
            tier_capabilities=tier_capabilities,
            turn_has_image=True,
            material_tokens=500_000,
        )
        assert (result.tier, result.actions) == ("c0", ())


def test_unknown_tier_is_no_op() -> None:
    result = capability_gate(
        "zz",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(c2=TierCapability(supports_vision=True)),
        turn_has_image=True,
        material_tokens=100,
    )
    assert (result.tier, result.actions) == ("zz", ())


def test_trail_recorded_only_when_gate_acted() -> None:
    extra: dict = {}
    acted = capability_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        tier_capabilities=caps(
            c0=TierCapability(context_window=8_000),
            c2=TierCapability(context_window=200_000),
        ),
        turn_has_image=False,
        material_tokens=30_000,
    )
    record_capability_gate_trail(extra, acted)
    assert extra["capability_gate_applied"] is True
    assert extra["routing_trail"] == [
        {
            "stage": "capability_gate",
            "rule": "context_walk_up",
            "from_tier": "c0",
            "to_tier": "c2",
        }
    ]

    untouched: dict = {}
    idle = capability_gate(
        "c0",
        valid_tiers=VALID_TIERS,
        tier_capabilities=None,
        turn_has_image=True,
        material_tokens=500_000,
    )
    record_capability_gate_trail(untouched, idle)
    assert untouched == {}  # no keys at all on a no-op turn


# ---------------------------------------------------------------------------
# engine composition
# ---------------------------------------------------------------------------


def _engine_inputs(**overrides: object) -> PolicyInputs:
    values: dict = {
        "decision": RoutingDecision(
            tier="c0", model="dummy-nano-1", confidence=0.9, source="v4_phase3"
        ),
        "message": "please summarize this short note",
        "router_cfg": SimpleNamespace(
            default_tier="c1",
            confidence_threshold=0.5,
            confidence_high_tier_margin=0.05,
            complaint_upgrade_enabled=True,
            complaint_upgrade_steps=1,
            complaint_upgrade_max_chars=160,
            kv_cache_anti_downgrade_enabled=True,
            kv_cache_anti_downgrade_window_seconds=600,
        ),
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


def test_engine_gate_runs_before_bind_and_reconciles() -> None:
    extra: dict = {}
    result = RoutingPolicyEngine().run(
        _engine_inputs(
            decision=RoutingDecision(
                tier="c1", model="dummy-mini-1", confidence=0.9, source="v4_phase3"
            ),
            extra=extra,
            turn_has_image=True,
            tier_capabilities=caps(
                c1=TierCapability(supports_vision=False),
                c2=TierCapability(supports_vision=True),
            ),
        )
    )
    assert result.decision.tier == "c2"
    assert result.decision.model == "dummy-pro-1"  # bind rebound to the gated tier
    assert extra["final_tier"] == "c2"
    assert extra["final_route_class"] == "R2"
    assert extra["capability_gate_applied"] is True
    assert extra["routing_trail"] == [
        {
            "stage": "capability_gate",
            "rule": "vision_walk_up",
            "from_tier": "c1",
            "to_tier": "c2",
        }
    ]
    assert result.thinking_mode == "T2"  # reconciled against the gated final tier


def test_engine_default_inputs_match_pre_gate_pipeline() -> None:
    baseline_extra: dict = {}
    baseline = RoutingPolicyEngine().run(_engine_inputs(extra=baseline_extra))
    gated_extra: dict = {}
    gated = RoutingPolicyEngine().run(
        _engine_inputs(
            extra=gated_extra,
            turn_has_image=False,
            tier_capabilities=None,
        )
    )
    assert gated.decision == baseline.decision
    assert gated_extra == baseline_extra
    assert "capability_gate_applied" not in gated_extra
    assert "routing_trail" not in gated_extra


# ---------------------------------------------------------------------------
# catalog facts gathering (never act on ignorance, at the source)
# ---------------------------------------------------------------------------


@pytest.fixture()
def facts_catalog() -> Iterator[ModelCatalog]:
    catalog = ModelCatalog()
    catalog._populate_from_data(
        [
            {
                "id": "dummy-vis-live-1",
                "context_length": 200_000,
                "supported_parameters": ["tools"],
                "architecture": {"input_modalities": ["text", "image"]},
            },
            {
                "id": "dummy-small-live-1",
                "context_length": 8_000,
                "supported_parameters": ["reasoning"],
                "architecture": {"input_modalities": ["text"]},
            },
        ]
    )
    catalog.set_user_overrides(
        {"anthroprov/dummy-anthropic-x": {"supports_vision": True}}
    )
    set_shared_catalog(catalog)
    try:
        yield catalog
    finally:
        set_shared_catalog(None)


def _facts_context(provider: str = "mainprov") -> TurnContext:
    config = GatewayConfig(llm={"provider": provider})
    config.squilla_router.cross_provider_tiers = True
    return TurnContext(
        message="",
        session_key="capability-facts",
        config=config,
        provider=None,
        model="",
        tool_defs=[],
        system_prompt="",
    )


def test_facts_definite_signals_from_catalog(facts_catalog: ModelCatalog) -> None:
    tiers = {
        "c0": {"model": "dummy-small-live-1"},
        "c1": {"model": "dummy-mini-unknown-1"},
        "c2": {"model": "dummy-vis-live-1"},
    }
    facts = _tier_capability_facts(
        _facts_context("openrouter"), tiers, ["c0", "c1", "c2"], "openrouter"
    )
    assert facts["c0"] == TierCapability(supports_vision=False, context_window=8_000)
    assert facts["c2"] == TierCapability(supports_vision=True, context_window=200_000)


def test_facts_synthesized_entry_gives_no_signal(facts_catalog: ModelCatalog) -> None:
    facts = _tier_capability_facts(
        _facts_context(), {"c1": {"model": "dummy-mini-unknown-1"}}, ["c1"], "mainprov"
    )
    assert facts["c1"] == TierCapability(supports_vision=None, context_window=None)


def test_facts_user_override_window_counts_as_definite(facts_catalog: ModelCatalog) -> None:
    # An operator-declared [models.*] context_window is knowledge, not an
    # estimate: the gate may act on it exactly like a catalog-sourced window.
    facts_catalog.set_user_overrides(
        {"mainprov/dummy-pinned-window-1": {"context_window": 64_000}}
    )
    facts = _tier_capability_facts(
        _facts_context(), {"c1": {"model": "dummy-pinned-window-1"}}, ["c1"], "mainprov"
    )
    assert facts["c1"].context_window == 64_000


def test_facts_keep_capability_evidence_provider_scoped(facts_catalog: ModelCatalog) -> None:
    # Explicit evidence for one provider cannot authorize a different
    # provider's deployment with the same model identifier.
    overridden = _tier_capability_facts(
        _facts_context(),
        {"c2": {"model": "dummy-anthropic-x", "provider": "anthroprov"}},
        ["c2"],
        "mainprov",
    )
    assert overridden["c2"].supports_vision is True

    anthropic_facts = _tier_capability_facts(
        _facts_context(),
        {"c2": {"model": "dummy-anthropic-x", "provider": "anthropic"}},
        ["c2"],
        "mainprov",
    )
    assert anthropic_facts["c2"] == TierCapability(
        supports_vision=None, context_window=None
    )


def test_facts_blank_model_gives_no_signal(facts_catalog: ModelCatalog) -> None:
    facts = _tier_capability_facts(_facts_context(), {"c0": {"model": ""}}, ["c0"], "mainprov")
    assert facts["c0"] == TierCapability()


# ---------------------------------------------------------------------------
# step-level: context walk-up through apply_squilla_router
# ---------------------------------------------------------------------------


class _FixedStrategy:
    def __init__(self, tier: str, confidence: float, extra: dict) -> None:
        self.tier = tier
        self.confidence = confidence
        self.extra = extra

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
    ) -> tuple[str, float, str, dict]:
        return self.tier, self.confidence, "v4_phase3", copy.deepcopy(self.extra)


def _run_step(
    config: GatewayConfig,
    strategy: object,
    *,
    message: str = "please summarize this short note",
    metadata: dict | None = None,
) -> TurnContext:
    sr = squilla_router_step
    sr._history_store.clear()
    sr._strategy = None
    sr._strategy_key = None
    ctx = TurnContext(
        message=message,
        session_key="agent:capgate:main",
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata=metadata or {},
    )
    original = sr._get_strategy
    sr._get_strategy = lambda _config: strategy  # type: ignore[assignment]
    try:
        asyncio.run(sr.apply_squilla_router(ctx))
    finally:
        sr._get_strategy = original  # type: ignore[assignment]
        sr._history_store.clear()
    return ctx


def _gate_config() -> GatewayConfig:
    config = GatewayConfig()
    config.llm.provider = "mainprov"
    config.llm.model = "dummy-base-model"
    config.squilla_router.enabled = True
    config.squilla_router.rollout_phase = "full"
    config.squilla_router.tiers = {
        "c0": {"model": "dummy-small-live-1"},
        "c1": {"model": "dummy-mini-unknown-1"},
        "c2": {"model": "dummy-vis-live-1"},
        "c3": {"model": "dummy-max-unknown-1"},
    }
    return config


def test_step_context_walk_up_end_to_end(facts_catalog: ModelCatalog) -> None:
    # 10k material tokens: above c0's definite 8k window, below the 25k
    # large-context floor — only the capability gate can move this turn.
    ctx = _run_step(
        _gate_config(),
        _FixedStrategy("c0", 0.9, {"route_class": "R0", "thinking_mode": "T0"}),
        metadata={"material_estimated_tokens": 10_000},
    )
    assert ctx.metadata["routed_tier"] == "c2"
    assert ctx.metadata["routed_model"] == "dummy-vis-live-1"
    assert ctx.metadata["routing_extra"]["capability_gate_applied"] is True
    assert ctx.metadata["routing_extra"]["routing_trail"] == [
        {
            "stage": "capability_gate",
            "rule": "context_walk_up",
            "from_tier": "c0",
            "to_tier": "c2",
        }
    ]
    assert ctx.metadata["routing_extra"]["final_tier"] == "c2"


def test_step_no_gate_keys_when_catalog_unknown(facts_catalog: ModelCatalog) -> None:
    config = _gate_config()
    config.squilla_router.tiers = {
        "c0": {"model": "dummy-unknown-a"},
        "c1": {"model": "dummy-unknown-b"},
        "c2": {"model": "dummy-unknown-c"},
        "c3": {"model": "dummy-unknown-d"},
    }
    ctx = _run_step(
        config,
        _FixedStrategy("c0", 0.9, {"route_class": "R0", "thinking_mode": "T0"}),
        metadata={"material_estimated_tokens": 10_000},
    )
    assert ctx.metadata["routed_tier"] == "c0"
    assert "capability_gate_applied" not in ctx.metadata["routing_extra"]
    assert "routing_trail" not in ctx.metadata["routing_extra"]
