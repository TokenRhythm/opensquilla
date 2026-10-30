from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.routing.model_policy import ModelPolicyState
from opensquilla.engine.steps import squilla_router
from opensquilla.engine.steps.model_router import ModelRouteError, validate_model_route_capacity
from opensquilla.gateway.config import GatewayConfig, SquillaRouterConfig
from opensquilla.router_control import RouterControlHoldStore


class Classifier:
    def __init__(self, intent="new_task", tier="c0"):
        self.intent, self.tier = intent, tier
        self.inputs = []

    def predict(self, value):
        self.inputs.append(deepcopy(value))
        return SimpleNamespace(
            intent_probabilities={
                k: float(k == self.intent) for k in ("new_task", "continue", "redo")
            },
            tier_probabilities={k: float(k == self.tier) for k in ("c0", "c1", "c2", "c3")},
            intent_decision=self.intent,
            tier_decision=self.tier,
        )


def context(*, message="Explain this.", state=None, provider="openrouter", **router):
    cfg = GatewayConfig(llm={"provider": provider}, squilla_router=router)
    ctx = TurnContext(
        message=message,
        session_key="model-test",
        config=cfg,
        provider=None,
        model=cfg.llm.model,
        tool_defs=[],
        system_prompt="",
    )
    ctx.metadata["router_model_input_message_id"] = "input-2"
    if state:
        ctx.metadata["router_model_state"] = state.to_dict()
    return ctx


def install(monkeypatch, classifier):
    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: classifier)


def test_current_and_saved_v4_defaults_use_model_policy():
    assert SquillaRouterConfig().strategy == "lightgbm_model"
    assert SquillaRouterConfig(strategy="v4_phase3").strategy == "lightgbm_model"
    assert SquillaRouterConfig().tier_policy == "model"
    assert squilla_router._strategy_name(SquillaRouterConfig()) == "lightgbm_model"
    assert (
        squilla_router._strategy_name(SquillaRouterConfig(strategy="legacy_v4_phase3"))
        == "v4_phase3"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent,expected", [("continue", "c1"), ("redo", "c2"), ("new_task", "c0")]
)
async def test_model_policy_bypasses_legacy_complaint_confidence_and_context_rules(
    monkeypatch, intent, expected
):
    model = Classifier(intent, "c0")
    install(monkeypatch, model)
    ctx = context(
        message="Your calculation is wrong. Debug production security failure. " + "data " * 3000,
        state=ModelPolicyState("c1"),
        confidence_threshold=1.0,
        budget={"action": "cap", "cap_tier": "c0", "limit_usd": 0.01},
    )
    ctx.metadata.update(session_billed_cost_usd=1.0, session_cost_source="billed")
    result = await squilla_router.apply_squilla_router(ctx)
    assert result.metadata["routed_tier"] == expected
    assert result.model == result.config.squilla_router.tiers[expected]["model"]
    assert result.metadata["router_fallback_chain"] == []
    assert result.metadata["router_model_decision"]["state"]["tier"] == expected
    assert result.metadata["routing_extra"]["tier_policy"] == "model"
    assert len(model.inputs) == 1
    assert (
        model.inputs[0]["current_request"] == ctx.semantic_message
        or result.metadata["prompt_policy"] == "P0"
    )


@pytest.mark.asyncio
async def test_replay_keeps_same_decision_without_predicting_or_redo_twice(monkeypatch):
    model = Classifier("redo")
    install(monkeypatch, model)
    first = await squilla_router.apply_squilla_router(context(state=ModelPolicyState("c1")))
    replay = context(
        state=ModelPolicyState.from_dict(first.metadata["router_model_decision"]["state"])
    )
    replay.metadata["router_model_decision"] = deepcopy(first.metadata["router_model_decision"])
    monkeypatch.setattr(
        squilla_router, "_get_strategy", lambda _: pytest.fail("replay must not classify")
    )
    second = await squilla_router.apply_squilla_router(replay)
    assert second.metadata["routed_tier"] == "c2"
    assert (
        second.metadata["router_model_decision"]["state"]
        == first.metadata["router_model_decision"]["state"]
    )


@pytest.mark.asyncio
async def test_observation_does_not_commit_candidate_state(monkeypatch):
    install(monkeypatch, Classifier("redo"))
    ctx = context(state=ModelPolicyState("c1"), rollout_phase="observe")
    original = ctx.model
    await squilla_router.apply_squilla_router(ctx)
    assert ctx.model == original
    assert "router_model_decision" not in ctx.metadata
    assert ctx.metadata["routing_applied"] is False
    assert ctx.metadata["routed_tier"] == "c2"
    assert "router_fallback_strict" not in ctx.metadata
    assert "router_fallback_chain" not in ctx.metadata
    assert "thinking_requested" not in ctx.metadata


@pytest.mark.asyncio
async def test_model_manual_hold_overrides_after_intent_prediction(monkeypatch):
    model = Classifier("redo")
    install(monkeypatch, model)
    ctx = context(state=ModelPolicyState("c1"))
    store = RouterControlHoldStore()
    target = next(
        t for t in store.build_targets(ctx.config.squilla_router) if t.target_id == "tier:c0"
    )
    store.set_hold(ctx.session_key, target, evidence="use c0")
    ctx.metadata["router_control_hold_store"] = store
    await squilla_router.apply_squilla_router(ctx)
    assert ctx.metadata["routed_tier"] == "c0"
    assert ctx.metadata["router_model_decision"]["state"]["tier"] == "c0"
    assert ctx.metadata["router_control_hold_applied"] is True
    assert len(model.inputs) == 1


@pytest.mark.asyncio
async def test_image_turn_uses_model_then_validates_selected_deployment(monkeypatch):
    model = Classifier(tier="c2")
    install(monkeypatch, model)
    ctx = context()
    ctx.attachments = [{"type": "image/png"}]
    await squilla_router.apply_squilla_router(ctx)
    assert ctx.metadata["routed_tier"] == "c2"
    assert len(model.inputs) == 1
    monkeypatch.setattr(squilla_router, "_tier_deployment_vision_support", lambda *_: "unsupported")
    with pytest.raises(ModelRouteError, match="image support"):
        validate_model_route_capacity(ctx)
    assert ctx.metadata["routed_tier"] == "c2"


@pytest.mark.asyncio
async def test_unconfigured_prediction_is_not_replaced_by_another_tier(monkeypatch):
    install(monkeypatch, Classifier(tier="c3"))
    ctx = context()
    ctx.config.squilla_router.tiers.pop("c3")
    with pytest.raises(ModelRouteError, match="unconfigured tier c3"):
        await squilla_router.apply_squilla_router(ctx)


@pytest.mark.asyncio
async def test_t0_explicitly_disables_provider_thinking(monkeypatch):
    install(monkeypatch, Classifier(tier="c0"))
    ctx = context(message="Hello")
    await squilla_router.apply_squilla_router(ctx)
    assert ctx.metadata["thinking_mode"] == "T0"
    assert ctx.metadata["thinking_requested"] is True
    assert ctx.metadata["thinking_level"] == "off"


@pytest.mark.asyncio
async def test_generation_context_controls_effort_without_changing_tier(monkeypatch):
    install(monkeypatch, Classifier("continue"))
    ctx = context(state=ModelPolicyState("c2"))
    ctx.metadata["router_model_control_context"] = {"context_tokens_est": 3000}
    await squilla_router.apply_squilla_router(ctx)
    assert ctx.metadata["routed_tier"] == "c2"
    assert ctx.metadata["thinking_mode"] == "T3"
    assert ctx.metadata["thinking_level"] == "high"


@pytest.mark.asyncio
async def test_explicit_new_disables_provider_native_context_replay(monkeypatch):
    install(monkeypatch, Classifier())
    ctx = context(state=ModelPolicyState("c2"))
    ctx.metadata["router_model_control_event"] = "new_task"
    await squilla_router.apply_squilla_router(ctx)
    assert ctx.metadata["router_model_decision"]["decision"]["context_reset"] is True
    assert ctx.metadata["provider_state_replay_disabled"] == "router_explicit_new_task"


@pytest.mark.asyncio
async def test_quality_retry_replay_uses_effective_tier_preserves_original_audit(monkeypatch):
    from dataclasses import replace

    install(monkeypatch, Classifier(tier="c1"))
    first = await squilla_router.apply_squilla_router(context())
    record = deepcopy(first.metadata["router_model_decision"])
    original = deepcopy(record["decision"])
    state = ModelPolicyState.from_dict(record["state"])
    record["state"] = replace(state, tier="c2", version=state.version + 1).to_dict()
    record["quality_retry"] = {"used": True, "from_tier": "c1", "to_tier": "c2"}
    target = first.config.squilla_router.tiers["c2"]
    record["execution_update"] = {
        "tier": "c2",
        "model": target["model"],
        "provider": target["provider"],
        "thinking_mode": "T3",
        "prompt_policy": "P2",
        "thinking_level": "high",
    }
    ctx = context()
    ctx.metadata["router_model_decision"] = record
    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: pytest.fail("reclassified"))
    await squilla_router.apply_squilla_router(ctx)
    assert ctx.metadata["routed_tier"] == "c2"
    assert ctx.metadata["router_model_decision"]["state"] == record["state"]
    assert ctx.metadata["router_model_decision"]["decision"] == original
    assert ctx.metadata["thinking_level"] == "high"


@pytest.mark.asyncio
async def test_nonattachment_oversize_is_rejected_without_tier_replacement(monkeypatch):
    install(monkeypatch, Classifier(tier="c0"))
    ctx = context()
    await squilla_router.apply_squilla_router(ctx)
    monkeypatch.setattr(
        squilla_router,
        "_capacity_tier_assessments",
        lambda *a, **k: [
            SimpleNamespace(assessment=SimpleNamespace(fits=False)),
        ],
    )
    with pytest.raises(ModelRouteError, match="complete request"):
        validate_model_route_capacity(ctx)
    assert ctx.metadata["routed_tier"] == "c0"


@pytest.mark.parametrize("provider", ["openrouter", "tokenrhythm"])
@pytest.mark.parametrize("tier", ["c0", "c1", "c2", "c3"])
def test_reference_pool_is_admissible_offline(tier, provider):
    ctx = context(provider=provider)
    ctx.config.llm.provider = provider
    ctx.metadata.update(routing_applied=True, routed_tier=tier, thinking_mode="T2")
    assert validate_model_route_capacity(ctx) is ctx


@pytest.mark.parametrize("tier", ["c0", "c3"])
def test_capable_selected_deployment_accepts_images_without_image_tier_shortcut(tier):
    ctx = context()
    ctx.attachments = [{"mime_type": "image/png"}]
    ctx.metadata.update(routing_applied=True, routed_tier=tier, thinking_mode="T2")
    assert validate_model_route_capacity(ctx) is ctx
    assert ctx.metadata["routed_tier"] == tier


@pytest.mark.asyncio
async def test_default_step_runs_pinned_pair_and_reuses_durable_redo(monkeypatch):
    import json

    from opensquilla.squilla_router.lightgbm_model import default_model_root

    fixture = json.loads(
        (default_model_root() / "source_parity_cases.json").read_text(encoding="utf-8")
    )["cases"][0]
    ctx = context(message=fixture["input"]["current_request"])
    ctx.metadata["router_model_input"] = fixture["input"]
    model = squilla_router.preload_strategy(ctx.config.squilla_router)
    predict = model.predict
    evidence = {}

    def capture_prediction(value):
        prediction = predict(value)
        evidence.update(
            input=value,
            feature_hash=prediction.feature_vector_hash,
            identity=prediction.identity,
            features=model.build_features(value).tolist(),
        )
        return prediction

    monkeypatch.setattr(model, "predict", capture_prediction)
    await squilla_router.apply_squilla_router(ctx)
    original = ctx.metadata["router_model_decision"]["decision"]
    assert original["tier_audit"]["probabilities"] == pytest.approx(
        {k.lower(): v for k, v in fixture["tier_probabilities"].items()},
        abs=1e-10,
    ), evidence
    assert ctx.metadata["routed_tier"] == "c0"
    status = squilla_router.router_runtime_status()
    assert status["loaded"] is True
    assert status["strategy"] == "lightgbm_model"
    validate_model_route_capacity(ctx)

    retry = context(
        message=ctx.semantic_message,
        state=ModelPolicyState.from_dict(
            ctx.metadata["router_model_decision"]["state"],
        ),
    )
    retry.metadata["router_model_control_event"] = "redo"
    # Explicit redo must reuse state instead of the classifier output.
    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: Classifier("new_task", "c3"))
    await squilla_router.apply_squilla_router(retry)
    assert retry.metadata["routed_tier"] == "c1"
    assert (
        retry.metadata["router_model_decision"]["decision"]["tier_audit"]["run_status"] == "not_run"
    )


def test_declared_provider_cannot_silently_execute_on_primary_credentials():
    ctx = context()
    ctx.config.llm.provider = "tokenrhythm"
    ctx.metadata.update(routing_applied=True, routed_tier="c0", thinking_mode="T2")
    with pytest.raises(ModelRouteError, match="cross-provider"):
        validate_model_route_capacity(ctx)


def test_only_proven_history_pressure_can_defer_to_same_tier_compaction():
    ctx = context()
    ctx.config.llm.context_window_tokens = 10000
    ctx.metadata.update(
        routing_applied=True,
        routed_tier="c0",
        thinking_mode="T0",
        routing_history_capacity_estimated_tokens=20000,
        routing_history_capacity_message_count=20,
        routing_history_capacity_estimate_complete=True,
    )
    with pytest.raises(ModelRouteError, match="complete request"):
        validate_model_route_capacity(ctx)
    validate_model_route_capacity(ctx, allow_compaction_retry=True)
    assert ctx.metadata["router_model_capacity_retry_pending"] is True
    assert ctx.metadata["routed_tier"] == "c0"
    # A pending marker is not authorization to execute an oversized request.
    with pytest.raises(ModelRouteError, match="complete request"):
        validate_model_route_capacity(ctx)
    ctx.metadata["routing_history_capacity_estimated_tokens"] = 2000
    validate_model_route_capacity(ctx)
    assert "router_model_capacity_retry_pending" not in ctx.metadata


@pytest.mark.asyncio
async def test_staged_unadmitted_decision_cannot_reclassify_or_execute(monkeypatch):
    install(monkeypatch, Classifier(tier="c2"))
    first = await squilla_router.apply_squilla_router(context())
    envelope = deepcopy(first.metadata["router_model_decision"])
    envelope.update(status="staged", state_committed=False)
    ctx = context()
    ctx.metadata["router_model_decision"] = envelope
    monkeypatch.setattr(squilla_router, "_get_strategy", lambda _: pytest.fail("reclassified"))
    with pytest.raises(ModelRouteError, match="not admitted"):
        await squilla_router.apply_squilla_router(ctx)
    assert "routing_applied" not in ctx.metadata


def test_model_strategy_cache_ignores_legacy_settings_but_tracks_assets(monkeypatch):
    from opensquilla.squilla_router import lightgbm_model

    loaded = []

    def load(model_root):
        loaded.append(model_root)
        return SimpleNamespace(source="lightgbm_model", _available=True)

    monkeypatch.setattr(lightgbm_model, "LightGBMRouterModel", load)
    monkeypatch.setattr(squilla_router, "_strategy", None)
    monkeypatch.setattr(squilla_router, "_strategy_key", None)
    config = SquillaRouterConfig()
    first = squilla_router.preload_strategy(config)
    config.confidence_threshold = 0.99
    config.v4_bundle_dir = "/unused-legacy-bundle"
    config.v4_use_aux_head = True
    config.require_router_runtime = True
    assert squilla_router.preload_strategy(config) is first
    assert loaded == [None]
    status = squilla_router.router_runtime_status()
    assert status["loaded"] is True
    assert status["strategy"] == "lightgbm_model"
    config.lightgbm_model_dir = "/another-model-bundle"
    assert squilla_router.preload_strategy(config) is not first
    assert loaded == [None, "/another-model-bundle"]


def test_explicit_thinking_requires_reasoning_even_when_policy_suggests_t0():
    ctx = context(auto_thinking=False)
    ctx.config.llm.thinking = "high"
    ctx.config.squilla_router.tiers["c0"]["model"] = "qwen/qwen-vl-max"
    ctx.metadata.update(routing_applied=True, routed_tier="c0", thinking_mode="T0")
    with pytest.raises(ModelRouteError, match="does not support reasoning"):
        validate_model_route_capacity(ctx)
