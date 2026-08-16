"""Tests for squilla_router.tier_provider_mismatch = "veto" rebinding.

The default ("route") must preserve the historical flag-and-misroute
behavior byte-for-byte (also pinned by the parity golden); "veto" rebinds a
mismatched classify-path decision to the nearest tier that executes on the
active provider (or the default tier) and records the veto in the routing
trail. All tier/model/provider names are synthetic dummy data.
"""

from __future__ import annotations

import asyncio
import copy

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.routing import ProviderMismatchVeto, provider_mismatch_veto
from opensquilla.engine.steps import squilla_router as squilla_router_step
from opensquilla.gateway.config import GatewayConfig

VALID_TIERS = ["c0", "c1", "c2", "c3"]


def tiers_with(providers: dict[str, str]) -> dict:
    tiers = {
        "c0": {"model": "dummy-nano-1"},
        "c1": {"model": "dummy-mini-1"},
        "c2": {"model": "dummy-pro-1"},
        "c3": {"model": "dummy-max-1"},
    }
    for name, provider in providers.items():
        tiers[name] = {**tiers[name], "provider": provider}
    return tiers


# ---------------------------------------------------------------------------
# provider_mismatch_veto (pure stage)
# ---------------------------------------------------------------------------


def test_veto_rebinds_to_nearest_executing_tier_preferring_lower() -> None:
    veto = provider_mismatch_veto(
        tiers=tiers_with({"c2": "otherprov"}),
        tier_name="c2",
        valid_tiers=VALID_TIERS,
        routing_applied=True,
        active_provider="mainprov",
        cross_provider_tiers=False,
    )
    # c1 and c3 are both one step away; the cheaper tier wins the tie.
    assert veto == ProviderMismatchVeto(True, "c2", "c1")


def test_veto_skips_mismatched_neighbors() -> None:
    veto = provider_mismatch_veto(
        tiers=tiers_with({"c1": "otherprov", "c2": "otherprov"}),
        tier_name="c2",
        valid_tiers=VALID_TIERS,
        routing_applied=True,
        active_provider="mainprov",
        cross_provider_tiers=False,
    )
    assert veto == ProviderMismatchVeto(True, "c2", "c3")


def test_veto_tier_naming_active_provider_counts_as_executing() -> None:
    veto = provider_mismatch_veto(
        tiers=tiers_with({"c2": "otherprov", "c1": "MainProv"}),
        tier_name="c2",
        valid_tiers=VALID_TIERS,
        routing_applied=True,
        active_provider="mainprov",
        cross_provider_tiers=False,
    )
    assert veto == ProviderMismatchVeto(True, "c2", "c1")


def test_veto_falls_back_to_default_tier() -> None:
    all_foreign = tiers_with(
        {"c0": "otherprov", "c1": "otherprov", "c2": "otherprov", "c3": "otherprov"}
    )
    veto = provider_mismatch_veto(
        tiers=all_foreign,
        tier_name="c2",
        valid_tiers=VALID_TIERS,
        routing_applied=True,
        active_provider="mainprov",
        cross_provider_tiers=False,
        default_tier="c1",
    )
    assert veto == ProviderMismatchVeto(True, "c2", "c1")


def test_veto_abstains_without_usable_target() -> None:
    veto = provider_mismatch_veto(
        tiers={"c2": {"model": "dummy-pro-1", "provider": "otherprov"}},
        tier_name="c2",
        valid_tiers=["c2"],
        routing_applied=True,
        active_provider="mainprov",
        cross_provider_tiers=False,
        default_tier="c2",
    )
    assert veto.applied is False


def test_veto_abstains_on_non_mismatch_outcomes() -> None:
    matched = provider_mismatch_veto(
        tiers=tiers_with({"c2": "mainprov"}),
        tier_name="c2",
        valid_tiers=VALID_TIERS,
        routing_applied=True,
        active_provider="mainprov",
        cross_provider_tiers=False,
    )
    assert matched.applied is False

    cross = provider_mismatch_veto(
        tiers=tiers_with({"c2": "otherprov"}),
        tier_name="c2",
        valid_tiers=VALID_TIERS,
        routing_applied=True,
        active_provider="mainprov",
        cross_provider_tiers=True,
    )
    assert cross.applied is False

    skipped = provider_mismatch_veto(
        tiers=tiers_with({"c2": "otherprov"}),
        tier_name="c2",
        valid_tiers=VALID_TIERS,
        routing_applied=False,
        active_provider="mainprov",
        cross_provider_tiers=False,
    )
    assert skipped.applied is False


# ---------------------------------------------------------------------------
# step-level: apply_squilla_router in route vs veto mode
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


def _config(mode: str, *, rollout_phase: str = "full") -> GatewayConfig:
    config = GatewayConfig()
    config.llm.provider = "mainprov"
    config.llm.model = "dummy-base-model"
    config.squilla_router.enabled = True
    config.squilla_router.rollout_phase = rollout_phase
    config.squilla_router.tier_provider_mismatch = mode  # type: ignore[assignment]
    config.squilla_router.tiers = tiers_with({"c2": "otherprov"})
    return config


def _run_step(config: GatewayConfig) -> TurnContext:
    sr = squilla_router_step
    sr._history_store.clear()
    sr._strategy = None
    sr._strategy_key = None
    ctx = TurnContext(
        message="please summarize this short note",
        session_key="agent:veto:main",
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={},
    )
    strategy = _FixedStrategy(
        "c2", 0.9, {"route_class": "R2", "thinking_mode": "T2", "prompt_policy": "P1"}
    )
    original = sr._get_strategy
    sr._get_strategy = lambda _config: strategy  # type: ignore[assignment]
    try:
        asyncio.run(sr.apply_squilla_router(ctx))
    finally:
        sr._get_strategy = original  # type: ignore[assignment]
        sr._history_store.clear()
    return ctx


def test_step_veto_mode_rebinds_and_records_trail() -> None:
    ctx = _run_step(_config("veto"))
    assert ctx.metadata["routed_tier"] == "c1"
    assert ctx.metadata["routed_model"] == "dummy-mini-1"
    assert ctx.model == "dummy-mini-1"
    assert ctx.metadata["provider_mismatch_veto_applied"] is True
    assert ctx.metadata["provider_mismatch_veto_from_tier"] == "c2"
    assert ctx.metadata["provider_mismatch_veto_to_tier"] == "c1"
    extra = ctx.metadata["routing_extra"]
    assert extra["provider_mismatch_veto_applied"] is True
    assert extra["final_tier"] == "c1"
    assert extra["final_route_class"] == "R1"
    assert extra["routing_trail"] == [
        {
            "stage": "provider_mismatch",
            "rule": "veto_rebind",
            "from_tier": "c2",
            "to_tier": "c1",
        }
    ]
    # The rebound tier executes on the active provider: no mismatch flag.
    assert "router_tier_provider_mismatch" not in ctx.metadata


def test_step_default_route_mode_preserves_flag_and_misroute() -> None:
    ctx = _run_step(_config("route"))
    assert ctx.metadata["routed_tier"] == "c2"
    assert ctx.metadata["routed_model"] == "dummy-pro-1"
    assert ctx.model == "dummy-pro-1"  # the documented-intentional misroute
    assert ctx.metadata["router_tier_provider_mismatch"] == "otherprov"
    assert "provider_mismatch_veto_applied" not in ctx.metadata
    extra = ctx.metadata["routing_extra"]
    assert "provider_mismatch_veto_applied" not in extra
    assert "routing_trail" not in extra
    assert extra["final_tier"] == "c2"


def test_step_default_config_value_is_route() -> None:
    assert GatewayConfig().squilla_router.tier_provider_mismatch == "route"
    ctx = _run_step(_config("route"))
    baseline = _run_step(_config("veto", rollout_phase="observe"))
    # Observe phase never applies routing, so even veto mode cannot rebind.
    assert baseline.metadata["routed_tier"] == "c2"
    assert "provider_mismatch_veto_applied" not in baseline.metadata
    assert ctx.metadata["routed_tier"] == "c2"


def test_step_veto_mode_noop_when_cross_provider_enabled() -> None:
    config = _config("veto")
    config.squilla_router.cross_provider_tiers = True
    ctx = _run_step(config)
    assert ctx.metadata["routed_tier"] == "c2"
    assert ctx.metadata["routed_provider"] == "otherprov"
    assert "provider_mismatch_veto_applied" not in ctx.metadata


def test_step_veto_cannot_rebind_large_context_below_floor(monkeypatch) -> None:
    config = _config("veto")
    config.squilla_router.tiers = tiers_with({"c3": "otherprov"})
    ctx = TurnContext(
        message="analyze the attached archive",
        session_key="agent:veto:large-context",
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        attachments=[],
        metadata={"attachment_material_estimated_tokens": 90_000},
    )
    strategy = _FixedStrategy(
        "c3",
        0.9,
        {"route_class": "R3", "thinking_mode": "T3", "prompt_policy": "P1"},
    )
    monkeypatch.setattr(squilla_router_step, "_get_strategy", lambda _config: strategy)

    out = asyncio.run(squilla_router_step.apply_squilla_router(ctx))

    assert out.metadata["provider_mismatch_veto_to_tier"] == "c2"
    assert out.metadata["large_context_floor_min_tier"] == "c3"
    assert out.metadata["large_context_capacity_blocked"] is True
    assert "routed_tier" not in out.metadata
