from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.routing import (
    ArtifactRoutingFacts,
    ArtifactRoutingUnavailableError,
    BudgetGateInput,
    PolicyInputs,
    RoutingDecision,
    RoutingPolicyEngine,
)
from opensquilla.engine.steps.squilla_router import (
    _router_text_fallback_chain,
    apply_squilla_router,
)
from opensquilla.router_control import RouterControlHoldStore, RouterControlTarget

TIERS = {
    "c0": {"model": "model-c0"},
    "c1": {"model": "model-c1"},
    "c2": {"model": "model-c2"},
    "c3": {"model": "model-c3"},
}


def _inputs(
    *,
    tier: str = "c0",
    operation: str = "open",
    budget: BudgetGateInput | None = None,
) -> PolicyInputs:
    return PolicyInputs(
        decision=RoutingDecision(
            tier=tier,
            model=TIERS[tier]["model"],
            confidence=0.9,
            source="test",
        ),
        message="synthetic",
        router_cfg=SimpleNamespace(),
        tiers=TIERS,
        valid_tiers=list(TIERS),
        routing_history=None,
        extra={},
        thinking_mode=None,
        prompt_policy=None,
        history_strategy=False,
        material_estimated_tokens=0,
        context_window_tokens=128_000,
        budget=budget,
        artifact=ArtifactRoutingFacts.from_values("html", operation),
    )


def test_open_artifact_has_c1_floor() -> None:
    result = RoutingPolicyEngine().run(_inputs(operation="open"))

    assert result.decision.tier == "c1"
    assert result.decision.source == "artifact_floor"
    assert result.metadata_updates == {
        "artifact_format": "html",
        "artifact_operation_class": "open",
        "artifact_minimum_tier": "c1",
        "artifact_floor_applied": True,
        "artifact_floor_from_tier": "c0",
    }


def test_selection_edit_has_c2_floor_without_content_metadata() -> None:
    result = RoutingPolicyEngine().run(_inputs(operation="selection_edit"))

    assert result.decision.tier == "c2"
    assert set(result.metadata_updates) == {
        "artifact_format",
        "artifact_operation_class",
        "artifact_minimum_tier",
        "artifact_floor_applied",
        "artifact_floor_from_tier",
    }


def test_browser_use_has_c3_floor() -> None:
    result = RoutingPolicyEngine().run(_inputs(operation="browser_use"))

    assert result.decision.tier == "c3"


def test_budget_cap_cannot_drop_below_artifact_floor() -> None:
    result = RoutingPolicyEngine().run(
        _inputs(
            tier="c3",
            operation="selection_edit",
            budget=BudgetGateInput(
                action="cap",
                limit_usd=1.0,
                spend_usd=2.0,
                cap_tier="c0",
            ),
        )
    )

    assert result.decision.tier == "c2"
    assert result.metadata_updates["router_budget_to_tier"] == "c2"


def test_missing_c2_uses_next_available_tier() -> None:
    inputs = _inputs(operation="selection_edit")
    inputs.valid_tiers = ["c0", "c1", "c3"]
    inputs.tiers = {name: TIERS[name] for name in inputs.valid_tiers}

    assert RoutingPolicyEngine().run(inputs).decision.tier == "c3"


def test_missing_every_capable_tier_fails_closed() -> None:
    inputs = _inputs(operation="selection_edit")
    inputs.valid_tiers = ["c0", "c1"]
    inputs.tiers = {name: TIERS[name] for name in inputs.valid_tiers}

    with pytest.raises(ArtifactRoutingUnavailableError) as exc_info:
        RoutingPolicyEngine().run(inputs)

    assert exc_info.value.code == "artifact_router_tier_unavailable"
    assert exc_info.value.minimum_tier == "c2"


def _router_context(
    message: str,
    *,
    tiers: dict[str, dict[str, str]],
    hold: bool = False,
    rollout_phase: str = "full",
) -> TurnContext:
    router_cfg = SimpleNamespace(
        enabled=True,
        rollout_phase=rollout_phase,
        tiers=tiers,
        default_tier="c0",
        auto_thinking=False,
        require_router_runtime=False,
    )
    metadata: dict[str, object] = {
        "artifact_format": "html",
        "artifact_operation_class": "selection_edit",
    }
    if hold:
        store = RouterControlHoldStore()
        store.set_hold(
            "agent:main:artifact-floor",
            RouterControlTarget(
                target_id="tier:c1",
                target_type="tier",
                tier="c1",
                model=tiers["c1"]["model"],
            ),
            evidence="synthetic hold",
        )
        metadata["router_control_hold_store"] = store
    return TurnContext(
        message=message,
        session_key="agent:main:artifact-floor",
        config=SimpleNamespace(squilla_router=router_cfg),
        provider=None,
        model="baseline-model",
        tool_defs=[],
        system_prompt="system",
        metadata=metadata,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "hold"),
    [("classify this", False), ("", False), ("honor the hold", True)],
)
async def test_router_shortcuts_fail_closed_without_capable_artifact_tier(
    message: str,
    hold: bool,
) -> None:
    ctx = _router_context(
        message,
        tiers={"c0": TIERS["c0"], "c1": TIERS["c1"]},
        hold=hold,
    )

    with pytest.raises(ArtifactRoutingUnavailableError):
        await apply_squilla_router(ctx)


@pytest.mark.asyncio
async def test_empty_artifact_prompt_routes_to_floor_even_in_observe_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _classification_must_not_run(_config: object) -> object:
        raise AssertionError("empty artifact prompts must use the deterministic floor")

    monkeypatch.setattr(
        "opensquilla.engine.steps.squilla_router._get_strategy",
        _classification_must_not_run,
    )
    ctx = _router_context("   ", tiers=TIERS, rollout_phase="observe")

    routed = await apply_squilla_router(ctx)

    assert routed.model == "model-c2"
    assert routed.metadata["routed_tier"] == "c2"
    assert routed.metadata["routing_source"] == "artifact_floor"
    assert routed.metadata["artifact_floor_applied"] is True
    assert routed.metadata["routing_applied"] is True
    assert [item["tier"] for item in routed.metadata["router_fallback_chain"]] == ["c3"]


def test_artifact_fallback_chain_never_crosses_c2_floor() -> None:
    chain = _router_text_fallback_chain(
        "c2", TIERS, minimum_tier="c2", allow_stronger_fallbacks=True
    )

    assert [item["tier"] for item in chain] == ["c3"]


def test_artifact_c3_fallback_can_use_c2_but_not_lower_tiers() -> None:
    chain = _router_text_fallback_chain(
        "c3", TIERS, minimum_tier="c2", allow_stronger_fallbacks=True
    )

    assert [item["tier"] for item in chain] == ["c2"]


def test_structural_edit_c3_floor_has_no_lower_fallback() -> None:
    chain = _router_text_fallback_chain(
        "c3", TIERS, minimum_tier="c3", allow_stronger_fallbacks=True
    )

    assert chain == []
