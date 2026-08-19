import logging

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.steps import squilla_router as squilla_router_step
from opensquilla.engine.steps.squilla_router import (
    apply_squilla_router,
    finalize_squilla_router_capacity,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.squilla_router.v4_phase3 import V4Phase3Strategy


class FakeStrategy:
    def __init__(self, tier: str, confidence: float, extra: dict) -> None:
        self.tier = tier
        self.confidence = confidence
        self.extra = extra
        self.calls = 0
        self.messages: list[str] = []

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
    ) -> tuple[str, float, str, dict]:
        self.calls += 1
        self.messages.append(message)
        assert self.tier in valid_tiers
        return self.tier, self.confidence, "v4_phase3", dict(self.extra)


class ContextAwareFakeStrategy(FakeStrategy):
    def __init__(self, tier: str, confidence: float, extra: dict) -> None:
        super().__init__(tier, confidence, extra)
        self.contexts: list[dict] = []

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        prev_assistant_text: str | None = None,
        prev_assistant_usage: dict | None = None,
        history_user_texts: list[str] | None = None,
        flags_text_override: str | None = None,
    ) -> tuple[str, float, str, dict]:
        self.calls += 1
        self.messages.append(message)
        self.contexts.append(
            {
                "routing_history": [dict(entry) for entry in routing_history or []],
                "prev_assistant_text": prev_assistant_text,
                "prev_assistant_usage": dict(prev_assistant_usage or {}),
                "history_user_texts": list(history_user_texts or []),
                "flags_text_override": flags_text_override,
            }
        )
        assert self.tier in valid_tiers
        return self.tier, self.confidence, "v4_phase3", dict(self.extra)


class ExplodingV4Strategy:
    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError(
            "failed to initialize V4 Phase 3 router: DLL load failed while importing "
            "onnxruntime_pybind11_state"
        )


class MacLibompExplodingV4Strategy:
    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError(
            "failed to initialize V4 Phase 3 router: dlopen("
            ".../lightgbm/lib/lib_lightgbm.dylib, 0x0006): Library not loaded: "
            "@rpath/libomp.dylib Referenced from: .../lib_lightgbm.dylib"
        )


@pytest.fixture(autouse=True)
def reset_squilla_router_state(monkeypatch: pytest.MonkeyPatch) -> None:
    squilla_router_step._history_store.clear()
    squilla_router_step._strategy = None
    squilla_router_step._strategy_key = None
    squilla_router_step._router_runtime_warning_emitted = False
    yield
    squilla_router_step._history_store.clear()
    squilla_router_step._strategy = None
    squilla_router_step._strategy_key = None
    squilla_router_step._router_runtime_warning_emitted = False
    monkeypatch.undo()


def make_context(
    message: str,
    *,
    rollout_phase: str = "full",
    session_key: str = "test-session",
    raw_message: str | None = None,
    attachments: list[dict] | None = None,
) -> TurnContext:
    # Pin the packaged openrouter ladder: these tests assert tier moves by
    # their distinct per-tier models, which the tokenrhythm default's uniform
    # synthesized ladder cannot express.
    config = GatewayConfig(llm={"provider": "openrouter"})
    config.squilla_router.rollout_phase = rollout_phase
    return TurnContext(
        message=message,
        session_key=session_key,
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        raw_message=raw_message,
        attachments=attachments or [],
    )


def require_runtime_router() -> None:
    try:
        V4Phase3Strategy(require_router_runtime=True)
    except Exception as exc:
        pytest.skip(f"V4 model router runtime unavailable: {exc}")


def fake_strategy(
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
    confidence: float,
    extra: dict,
) -> FakeStrategy:
    strategy = FakeStrategy(tier, confidence, extra)
    monkeypatch.setattr(squilla_router_step, "_get_strategy", lambda _config: strategy)
    return strategy


def context_aware_fake_strategy(
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
    confidence: float,
    extra: dict,
) -> ContextAwareFakeStrategy:
    strategy = ContextAwareFakeStrategy(tier, confidence, extra)
    monkeypatch.setattr(squilla_router_step, "_get_strategy", lambda _config: strategy)
    return strategy


def test_vision_followup_gate_default_timeout_allows_real_provider_latency() -> None:
    config = GatewayConfig()

    assert config.squilla_router.vision_followup_gate_timeout_seconds >= 10.0


def test_vision_followup_gate_default_output_budget_handles_reasoning_models() -> None:
    config = GatewayConfig()

    assert config.squilla_router.vision_followup_gate_max_output_tokens >= 512


def test_large_context_admission_uses_configured_thinking_budget() -> None:
    ctx = make_context("reason about this archive")
    object.__setattr__(ctx.config.llm, "thinking", "high")

    assert (
        squilla_router_step._route_thinking_budget_tokens(
            ctx,
            ctx.config.squilla_router,
            {},
        )
        == 20_000
    )

    object.__setattr__(ctx.config.llm, "thinking", "xhigh")
    assert (
        squilla_router_step._route_thinking_budget_tokens(
            ctx,
            ctx.config.squilla_router,
            {},
        )
        == 50_000
    )


@pytest.mark.asyncio
async def test_full_rollout_applies_routed_model_thinking_and_p0_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c1",
        0.91,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context("Summarize this short note.")
    baseline_model = ctx.model

    routed = await apply_squilla_router(ctx)

    assert routed.model == "deepseek/deepseek-v4-pro"
    assert routed.metadata["routed_tier"] == "c1"
    assert routed.metadata["routed_model"] == "deepseek/deepseek-v4-pro"
    assert routed.metadata["routing_applied"] is True
    assert routed.metadata["applied_model"] == "deepseek/deepseek-v4-pro"
    assert routed.metadata["baseline_model"] == baseline_model
    assert routed.metadata["routing_confidence"] == 0.91
    assert routed.metadata["routing_source"] == "v4_phase3"
    assert "savings_pct" in routed.metadata
    assert "savings_max_price_per_m" in routed.metadata
    assert "savings_routed_price_per_m" in routed.metadata
    assert routed.metadata["thinking_mode"] == "T1"
    assert routed.metadata["thinking_requested"] is True
    assert routed.metadata["thinking_level"] == "low"
    assert routed.metadata["prompt_policy"] == "P0"
    assert "[RESPONSE_POLICY: Answer directly" in routed.message


@pytest.mark.asyncio
async def test_router_records_lower_text_tier_fallback_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c3",
        0.91,
        {
            "route_class": "R3",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )

    routed = await apply_squilla_router(make_context("Solve a difficult architecture problem."))

    assert routed.metadata["routed_tier"] == "c3"
    assert [item["tier"] for item in routed.metadata["router_fallback_chain"]] == [
        "c2",
        "c1",
        "c0",
    ]
    assert [item["model"] for item in routed.metadata["router_fallback_chain"]] == [
        "z-ai/glm-5.2",
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
    ]


@pytest.mark.asyncio
async def test_router_reports_provider_state_loss_without_changing_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c1",
        0.91,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context("Continue the long task.")
    ctx.metadata["session_context_states"] = [
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "state_kind": "anthropic_compaction_block",
            "valid": True,
            "portable": False,
        },
        {
            "provider": "portable",
            "model": "",
            "state_kind": "structured_summary_v1",
            "valid": True,
            "portable": True,
        },
    ]

    routed = await apply_squilla_router(ctx)

    assert routed.model == "deepseek/deepseek-v4-pro"
    diagnostic = routed.metadata["provider_state_continuity"]
    assert diagnostic["decision"] == "use_portable_fallback"
    assert diagnostic["provider_state_loss_risk"] is True
    assert diagnostic["candidate_provider"] == "openrouter"


@pytest.mark.asyncio
async def test_router_continuity_diagnostic_ignores_expired_provider_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c1",
        0.91,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context("Continue the long task.")
    ctx.metadata["session_context_states"] = [
        {
            "provider": "anthropic",
            "model": "claude-sonnet-4-6",
            "state_kind": "anthropic_compaction_block",
            "created_at": 100,
            "expires_at": 150,
            "valid": True,
            "portable": False,
        },
        {
            "provider": "portable",
            "model": "",
            "state_kind": "structured_summary_v1",
            "created_at": 90,
            "valid": True,
            "portable": True,
        },
    ]

    routed = await apply_squilla_router(ctx)

    diagnostic = routed.metadata["provider_state_continuity"]
    assert diagnostic["decision"] == "use_portable_fallback"
    assert diagnostic["provider_state_loss_risk"] is False
    assert diagnostic["active_state_provider"] is None
    assert diagnostic["portable_fallback_available"] is True


@pytest.mark.asyncio
async def test_p2_prompt_hint_is_recorded_but_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c3",
        0.97,
        {
            "route_class": "R3",
            "thinking_mode": "T3",
            "prompt_policy": "P2",
            "prompt_hint": "Use a careful plan before answering.",
        },
    )
    ctx = make_context("Plan a risky multi-step migration.")

    routed = await apply_squilla_router(ctx)

    assert routed.model == "anthropic/claude-opus-4.8"
    assert routed.metadata["routed_tier"] == "c3"
    assert routed.metadata["thinking_level"] == "high"
    assert routed.metadata["prompt_policy"] == "P2"
    assert routed.metadata["routing_extra"]["prompt_hint"] == "Use a careful plan before answering."
    assert "[RESPONSE_POLICY:" not in routed.message


@pytest.mark.asyncio
async def test_v4_thinking_mode_overrides_explicit_tier_thinking_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c2",
        0.92,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    ctx = make_context("Analyze this implementation path.")

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c2"
    assert routed.metadata["thinking_mode"] == "T2"
    assert routed.metadata["thinking_requested"] is True
    assert routed.metadata["thinking_level"] == "medium"


@pytest.mark.asyncio
async def test_confidence_gate_promotes_low_confidence_t0_to_default_t1_and_reconciles_thinking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c0",
        0.1,
        {
            "route_class": "R0",
            "thinking_mode": "T0",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context("Maybe simple, but classifier is uncertain.")

    routed = await apply_squilla_router(ctx)
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routed_tier"] == "c1"
    assert routed.model == "deepseek/deepseek-v4-pro"
    assert extra["confidence_gate_applied"] is True
    assert extra["base_tier"] == "c0"
    assert extra["final_tier"] == "c1"
    assert routed.metadata["thinking_mode"] == "T1"
    assert routed.metadata["thinking_level"] == "low"
    assert "[RESPONSE_POLICY: Answer directly" in routed.message


@pytest.mark.asyncio
async def test_confidence_gate_falls_back_low_confidence_non_default_text_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c2",
        0.1,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    ctx = make_context("Classifier is uncertain but picked an expensive tier.")

    routed = await apply_squilla_router(ctx)
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routed_tier"] == "c1"
    assert routed.model == "deepseek/deepseek-v4-pro"
    assert extra["confidence_gate_applied"] is True
    assert extra["pre_confidence_tier"] == "c2"
    assert extra["final_tier"] == "c1"


@pytest.mark.asyncio
async def test_confidence_gate_keeps_near_threshold_high_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c3",
        0.49,
        {
            "route_class": "R3",
            "thinking_mode": "T3",
            "prompt_policy": "P2",
        },
    )
    ctx = make_context("Classifier is near threshold but chose a high-risk tier.")

    routed = await apply_squilla_router(ctx)
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routed_tier"] == "c3"
    assert extra["confidence_gate_applied"] is False
    assert extra["pre_confidence_tier"] == "c3"
    assert extra["final_tier"] == "c3"


@pytest.mark.asyncio
async def test_large_material_estimate_floors_low_router_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c1", 0.91, {"route_class": "R1"})
    ctx = make_context("Please process the attached pasted text.")
    ctx.metadata["input_normalization"] = {
        "guard_action": "generated_text_attachment",
        "material_estimated_tokens": 45_000,
    }
    ctx.metadata["material_estimated_tokens"] = 45_000

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c2"
    assert routed.metadata["routing_source"] == "large_context_floor"
    assert routed.metadata["large_context_floor_from_tier"] == "c1"
    assert routed.metadata["large_context_material_tokens"] == 45_000
    assert routed.metadata["routing_extra"]["final_tier"] == "c2"


@pytest.mark.parametrize(
    ("character_count", "expected_tier", "expected_floor"),
    [
        (99_996, "c0", None),
        (100_000, "c2", "c2"),
    ],
)
@pytest.mark.asyncio
async def test_plain_text_large_context_floor_boundary_keeps_legacy_parity(
    monkeypatch: pytest.MonkeyPatch,
    character_count: int,
    expected_tier: str,
    expected_floor: str | None,
) -> None:
    fake_strategy(monkeypatch, "c0", 0.91, {"route_class": "R0"})
    ctx = make_context("a" * character_count)

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == expected_tier
    assert routed.metadata.get("large_context_floor_min_tier") == expected_floor
    assert "large_context_capacity_required" not in routed.metadata


@pytest.mark.asyncio
async def test_large_material_ratio_floors_low_router_tier_to_t3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c1", 0.91, {"route_class": "R1"})
    ctx = make_context("Please process the attached pasted text.")
    object.__setattr__(ctx.config.squilla_router, "context_window_tokens", 100_000)
    ctx.metadata["input_normalization"] = {
        "guard_action": "generated_text_attachment",
        "material_estimated_tokens": 40_000,
    }

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c3"
    assert routed.metadata["routing_source"] == "large_context_floor"
    assert routed.metadata["large_context_floor_from_tier"] == "c1"
    assert routed.metadata["large_context_material_tokens"] == 40_000


@pytest.mark.asyncio
async def test_large_material_head_honors_global_context_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c3", 0.91, {"route_class": "R3"})
    ctx = make_context("Please process the attached archive.")
    object.__setattr__(ctx.config.llm, "context_window_tokens", 80_000)
    object.__setattr__(ctx.config.llm, "max_tokens", 10_000)
    ctx.metadata["attachment_material_estimated_tokens"] = 50_000

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["large_context_capacity_blocked"] is True
    assert routed.metadata["large_context_context_window_override_tokens"] == 80_000
    assert routed.metadata["large_context_max_output_override_tokens"] == 10_000
    assert "routed_tier" not in routed.metadata


@pytest.mark.asyncio
async def test_attachment_below_large_floor_uses_effective_request_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attachment admission must use usable input, not the raw context window."""

    fake_strategy(monkeypatch, "c0", 0.91, {"route_class": "R0"})
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openrouter/text-tight": {
                "context_window": 32_000,
                "max_output_tokens": 4_000,
            },
            "openrouter/text-safe": {
                "context_window": 64_000,
                "max_output_tokens": 4_000,
            },
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    ctx = make_context(
        "Summarize the attachment.",
        attachments=[{"type": "application/pdf"}],
    )
    ctx.config.squilla_router.tiers = {
        "c0": {
            "provider": "openrouter",
            "model": "text-tight",
            "thinking_level": "off",
        },
        "c1": {
            "provider": "openrouter",
            "model": "text-safe",
            "thinking_level": "off",
        },
    }
    ctx.metadata["attachment_material_estimated_tokens"] = 20_000
    ctx.metadata["routing_history_capacity_estimated_tokens"] = 5_000
    ctx.metadata["routing_history_capacity_message_count"] = 4

    routed = await apply_squilla_router(ctx)
    routed = await finalize_squilla_router_capacity(routed)

    assert "large_context_floor_min_tier" not in routed.metadata
    assert routed.metadata["routed_tier"] == "c1"
    assert routed.model == "text-safe"


@pytest.mark.asyncio
async def test_complete_estimate_replaces_legacy_fixed_headroom_at_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c2", 0.91, {"route_class": "R2"})
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openrouter/boundary-safe": {
                "context_window": 80_000,
                "max_output_tokens": 4_000,
            }
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    ctx = make_context(
        "Use the attachment.",
        attachments=[{"type": "text/plain"}],
    )
    ctx.config.squilla_router.auto_thinking = False
    ctx.config.squilla_router.tiers = {
        "c2": {
            "provider": "openrouter",
            "model": "boundary-safe",
            "thinking_level": "off",
        }
    }
    ctx.metadata["attachment_material_estimated_tokens"] = 40_000

    routed = await finalize_squilla_router_capacity(
        await apply_squilla_router(ctx)
    )

    assert routed.metadata["routed_tier"] == "c2"
    assert routed.metadata["large_context_request_input_tokens"] < 47_600


@pytest.mark.asyncio
async def test_same_attachment_fits_short_history_but_long_history_is_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c0", 0.91, {"route_class": "R0"})
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openrouter/text-tight": {
                "context_window": 32_000,
                "max_output_tokens": 4_000,
            },
            "openrouter/text-safe": {
                "context_window": 64_000,
                "max_output_tokens": 4_000,
            },
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)

    async def route(history_tokens: int) -> TurnContext:
        ctx = make_context(
            "Use the supplied material.",
            attachments=[{"type": "text/plain"}],
        )
        ctx.config.squilla_router.tiers = {
            "c0": {
                "provider": "openrouter",
                "model": "text-tight",
                "thinking_level": "off",
            },
            "c1": {
                "provider": "openrouter",
                "model": "text-safe",
                "thinking_level": "off",
            },
        }
        ctx.metadata["attachment_material_estimated_tokens"] = 4_000
        ctx.metadata["routing_history_capacity_estimated_tokens"] = history_tokens
        ctx.metadata["routing_history_capacity_message_count"] = 8
        return await finalize_squilla_router_capacity(
            await apply_squilla_router(ctx)
        )

    short_history = await route(1_000)
    long_history = await route(21_000)

    assert short_history.metadata["routed_tier"] == "c0"
    assert long_history.metadata["routed_tier"] == "c1"
    assert (
        long_history.metadata["large_context_request_input_tokens"]
        > short_history.metadata["large_context_request_input_tokens"]
    )


@pytest.mark.asyncio
async def test_catalog_unknown_custom_attachment_config_has_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c0", 0.91, {"route_class": "R0"})
    monkeypatch.setattr(
        "opensquilla.provider.model_catalog._shared_catalog",
        ModelCatalog(),
    )
    ctx = make_context(
        "Read the synthetic attachment.",
        attachments=[{"type": "text/plain"}],
    )
    ctx.config.llm.provider = "custom"
    ctx.config.llm.model = "private-model"
    ctx.config.llm.context_window_tokens = 0
    ctx.config.squilla_router.tiers = {
        "c0": {
            "provider": "custom",
            "model": "private-model",
            "thinking_level": "off",
        }
    }
    ctx.metadata["attachment_material_estimated_tokens"] = 1_000

    routed = await finalize_squilla_router_capacity(
        await apply_squilla_router(ctx)
    )

    assert routed.metadata["large_context_capacity_blocked"] is True
    assert "llm.context_window_tokens" in routed.metadata[
        "large_context_capacity_block_reason"
    ]


@pytest.mark.asyncio
async def test_catalog_unknown_custom_plain_text_keeps_existing_routing_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c0", 0.91, {"route_class": "R0"})
    monkeypatch.setattr(
        "opensquilla.provider.model_catalog._shared_catalog",
        ModelCatalog(),
    )
    ctx = make_context("No attachment on this turn.")
    ctx.config.llm.provider = "custom"
    ctx.config.llm.model = "private-model"
    ctx.config.llm.context_window_tokens = 0
    ctx.config.squilla_router.tiers = {
        "c0": {
            "provider": "custom",
            "model": "private-model",
            "thinking_level": "off",
        }
    }

    routed = await finalize_squilla_router_capacity(
        await apply_squilla_router(ctx)
    )

    assert routed.model == "private-model"
    assert routed.metadata["routing_applied"] is True
    assert "large_context_capacity_blocked" not in routed.metadata


@pytest.mark.asyncio
async def test_long_user_history_system_tools_and_attachment_share_one_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c0", 0.91, {"route_class": "R0"})
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openrouter/text-tight": {
                "context_window": 32_000,
                "max_output_tokens": 4_000,
            },
            "openrouter/text-safe": {
                "context_window": 64_000,
                "max_output_tokens": 4_000,
            },
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    ctx = make_context(
        "u" * 12_000,
        attachments=[{"type": "application/pdf"}],
    )
    ctx.config.squilla_router.tiers = {
        "c0": {
            "provider": "openrouter",
            "model": "text-tight",
            "thinking_level": "off",
        },
        "c1": {
            "provider": "openrouter",
            "model": "text-safe",
            "thinking_level": "off",
        },
    }
    ctx.system_prompt = "s" * 8_000
    ctx.tool_defs = [
        {
            "name": "synthetic_tool",
            "description": "d" * 8_000,
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    ctx.metadata["attachment_material_estimated_tokens"] = 4_000
    ctx.metadata["routing_history_capacity_estimated_tokens"] = 12_000
    ctx.metadata["routing_history_capacity_message_count"] = 12

    routed = await finalize_squilla_router_capacity(
        await apply_squilla_router(ctx)
    )

    assert routed.metadata["routed_tier"] == "c1"
    assert routed.metadata["large_context_material_tokens"] >= 7_000
    assert routed.metadata["large_context_history_tokens"] == 12_000
    assert routed.metadata["large_context_system_tools_tokens"] > 3_000
    assert routed.metadata["large_context_structural_tokens"] > 0


@pytest.mark.asyncio
async def test_capacity_revalidation_never_downgrades_semantic_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(monkeypatch, "c2", 0.91, {"route_class": "R2"})
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openrouter/lower-large-window": {
                "context_window": 128_000,
                "max_output_tokens": 4_000,
            },
            "openrouter/selected-tight": {
                "context_window": 32_000,
                "max_output_tokens": 4_000,
            },
            "openrouter/higher-safe": {
                "context_window": 64_000,
                "max_output_tokens": 4_000,
            },
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    ctx = make_context(
        "Analyze the attachment.",
        attachments=[{"type": "text/plain"}],
    )
    ctx.config.squilla_router.tiers = {
        "c0": {
            "provider": "openrouter",
            "model": "lower-large-window",
            "thinking_level": "off",
        },
        "c2": {
            "provider": "openrouter",
            "model": "selected-tight",
            "thinking_level": "off",
        },
        "c3": {
            "provider": "openrouter",
            "model": "higher-safe",
            "thinking_level": "off",
        },
    }
    ctx.metadata["attachment_material_estimated_tokens"] = 4_000
    ctx.metadata["routing_history_capacity_estimated_tokens"] = 21_000
    ctx.metadata["routing_history_capacity_message_count"] = 8

    routed = await finalize_squilla_router_capacity(
        await apply_squilla_router(ctx)
    )

    assert routed.metadata["routed_tier"] == "c3"
    assert routed.metadata["large_context_final_route_from_tier"] == "c2"


@pytest.mark.asyncio
async def test_anti_downgrade_keeps_recent_higher_tier_despite_confidence_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx1 = make_context("Hard first turn.", session_key="test-confidence-history")
    fake_strategy(
        monkeypatch,
        "c2",
        0.9,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    routed1 = await apply_squilla_router(ctx1)
    assert routed1.metadata["routed_tier"] == "c2"

    fake_strategy(
        monkeypatch,
        "c0",
        0.1,
        {
            "route_class": "R0",
            "thinking_mode": "T0",
            "prompt_policy": "P0",
        },
    )
    ctx2 = make_context("Uncertain follow-up.", session_key="test-confidence-history")

    routed2 = await apply_squilla_router(ctx2)
    extra = routed2.metadata["routing_extra"]

    assert routed2.metadata["routed_tier"] == "c2"
    assert routed2.model == "z-ai/glm-5.2"
    assert extra["confidence_gate_applied"] is True
    assert extra["pre_confidence_tier"] == "c0"
    assert extra["final_tier"] == "c2"
    assert extra["anti_downgrade_applied"] is True
    assert extra["previous_tier"] == "c2"

    fake_strategy(
        monkeypatch,
        "c1",
        0.9,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P1",
        },
    )
    ctx3 = make_context("Normal follow-up.", session_key="test-confidence-history")

    routed3 = await apply_squilla_router(ctx3)
    extra3 = routed3.metadata["routing_extra"]

    assert routed3.metadata["routed_tier"] == "c2"
    assert routed3.model == "z-ai/glm-5.2"
    assert extra3["confidence_gate_applied"] is False
    assert extra3["anti_downgrade_applied"] is True
    assert extra3["previous_tier"] == "c2"


@pytest.mark.asyncio
async def test_anti_downgrade_uses_previous_turn_not_window_highest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "test-previous-not-highest"
    fake_strategy(
        monkeypatch,
        "c3",
        0.9,
        {
            "route_class": "R3",
            "thinking_mode": "T3",
            "prompt_policy": "P2",
        },
    )
    routed1 = await apply_squilla_router(make_context("Very hard turn.", session_key=session_key))
    assert routed1.metadata["routed_tier"] == "c3"

    ctx2 = make_context("Less hard turn.", session_key=session_key)
    ctx2.config.squilla_router.kv_cache_anti_downgrade_enabled = False
    fake_strategy(
        monkeypatch,
        "c2",
        0.9,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    routed2 = await apply_squilla_router(ctx2)
    assert routed2.metadata["routed_tier"] == "c2"

    ctx3 = make_context("Easy follow-up.", session_key=session_key)
    fake_strategy(
        monkeypatch,
        "c1",
        0.9,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P1",
        },
    )
    routed3 = await apply_squilla_router(ctx3)
    extra3 = routed3.metadata["routing_extra"]

    assert routed3.metadata["routed_tier"] == "c2"
    assert routed3.model == "z-ai/glm-5.2"
    assert extra3["anti_downgrade_applied"] is True
    assert extra3["previous_tier"] == "c2"


@pytest.mark.asyncio
async def test_anti_downgrade_keeps_previous_high_tier_without_margin_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "test-anti-downgrade-ignore-margin"
    fake_strategy(
        monkeypatch,
        "c3",
        0.95,
        {
            "route_class": "R3",
            "thinking_mode": "T3",
            "prompt_policy": "P2",
            "margin": 0.99,
        },
    )
    routed1 = await apply_squilla_router(
        make_context("Architecture review.", session_key=session_key)
    )
    assert routed1.metadata["routed_tier"] == "c3"

    fake_strategy(
        monkeypatch,
        "c1",
        0.99,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P1",
            "margin": 0.99,
        },
    )
    routed2 = await apply_squilla_router(make_context("Follow-up.", session_key=session_key))
    extra = routed2.metadata["routing_extra"]

    assert routed2.metadata["routed_tier"] == "c3"
    assert routed2.model == "anthropic/claude-opus-4.8"
    assert extra["anti_downgrade_applied"] is True
    assert extra["previous_tier"] == "c3"
    assert extra["kv_cache_window_seconds"] == 600


@pytest.mark.asyncio
async def test_complaint_upgrade_promotes_tier_thinking_and_blocks_compressed_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c1",
        0.9,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context("不对，重新回答")

    routed = await apply_squilla_router(ctx)
    extra = routed.metadata["routing_extra"]

    assert routed.metadata["routed_tier"] == "c2"
    assert routed.model == "z-ai/glm-5.2"
    assert extra["complaint_detected"] is True
    assert extra["complaint_upgrade_applied"] is True
    assert routed.metadata["thinking_mode"] == "T2"
    assert routed.metadata["thinking_level"] == "medium"
    assert routed.metadata["prompt_policy"] == "P1"
    assert "[RESPONSE_POLICY:" not in routed.message


@pytest.mark.asyncio
async def test_complaint_upgrade_starts_from_previous_experienced_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "test-complaint-upgrade-previous-tier"
    fake_strategy(
        monkeypatch,
        "c2",
        0.9,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    routed1 = await apply_squilla_router(
        make_context("Analyze this tricky failure.", session_key=session_key)
    )
    assert routed1.metadata["routed_tier"] == "c2"

    fake_strategy(
        monkeypatch,
        "c1",
        0.9,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    routed2 = await apply_squilla_router(make_context("答非所问", session_key=session_key))
    extra = routed2.metadata["routing_extra"]

    assert routed2.metadata["routed_tier"] == "c3"
    assert routed2.model == "anthropic/claude-opus-4.8"
    assert extra["previous_tier"] == "c2"
    assert extra["complaint_detected"] is True
    assert extra["complaint_upgrade_applied"] is True
    assert extra["anti_downgrade_applied"] is False


@pytest.mark.asyncio
async def test_complaint_upgrade_uses_pre_confidence_high_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_key = "test-complaint-upgrade-pre-confidence-tier"
    fake_strategy(
        monkeypatch,
        "c1",
        0.9,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P1",
        },
    )
    await apply_squilla_router(
        make_context("Compare PostgreSQL and MySQL.", session_key=session_key)
    )

    fake_strategy(
        monkeypatch,
        "c2",
        0.01,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    routed = await apply_squilla_router(
        make_context("不对，太泛了，重新写。", session_key=session_key)
    )
    extra = routed.metadata["routing_extra"]

    assert extra["confidence_gate_applied"] is True
    assert extra["pre_confidence_tier"] == "c2"
    assert routed.metadata["routed_tier"] == "c3"
    assert extra["complaint_upgrade_applied"] is True


@pytest.mark.asyncio
async def test_router_classifies_raw_semantic_input_but_injects_prompt_into_display_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = fake_strategy(
        monkeypatch,
        "c0",
        0.92,
        {
            "route_class": "R0",
            "thinking_mode": "T0",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context(
        "Displayed prompt wrapper",
        raw_message="Summarize the underlying user input.",
    )

    routed = await apply_squilla_router(ctx)

    assert strategy.messages == ["Summarize the underlying user input."]
    assert routed.metadata["routed_tier"] == "c0"
    assert routed.metadata["prompt_policy"] == "P0"
    assert routed.message.startswith("Displayed prompt wrapper")
    assert "Summarize the underlying user input." not in routed.message
    assert "[RESPONSE_POLICY: Answer directly" in routed.message


@pytest.mark.asyncio
async def test_router_passes_transcript_context_into_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = context_aware_fake_strategy(
        monkeypatch,
        "c2",
        0.88,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P1",
        },
    )
    ctx = make_context("Continue from the previous answer.")
    ctx.metadata.update(
        {
            "router_prev_assistant_text": "Previous assistant answer.",
            "router_prev_assistant_usage": {"output_tokens": 321},
            "router_history_user_texts": ["First user question.", "Second user question."],
            "router_flags_text_override": "Continue from the previous answer.",
            "routing_history": [
                {
                    "text": "First user question.",
                    "route_class": "R1",
                    "final_route_class": "R1",
                    "difficulty": 1.0,
                    "margin": 0.5,
                }
            ],
        }
    )

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c2"
    assert strategy.messages == ["Continue from the previous answer."]
    assert strategy.contexts == [
        {
            "routing_history": [
                {
                    "text": "First user question.",
                    "route_class": "R1",
                    "final_route_class": "R1",
                    "difficulty": 1.0,
                    "margin": 0.5,
                    "_ts": pytest.approx(strategy.contexts[0]["routing_history"][0]["_ts"]),
                }
            ],
            "prev_assistant_text": "Previous assistant answer.",
            "prev_assistant_usage": {"output_tokens": 321},
            "history_user_texts": ["First user question.", "Second user question."],
            "flags_text_override": "Continue from the previous answer.",
        }
    ]


def test_v4_request_contains_current_history_assistant_and_route_context() -> None:
    class FakeInferenceRequest:
        def __init__(self, **kwargs) -> None:
            self.__dict__.update(kwargs)

    strategy = V4Phase3Strategy(require_router_runtime=False)
    strategy._request_type = FakeInferenceRequest

    request = strategy._build_request(
        "Current user question.",
        [
            {
                "text": "Previous user question.",
                "final_route_class": "R2",
                "difficulty_score": 2.0,
                "margin": 0.7,
            }
        ],
        prev_assistant_text="Previous assistant answer.",
        prev_assistant_usage={"output_tokens": 456},
        history_user_texts=["Earlier user question.", "Previous user question."],
        flags_text_override="Current user question.",
    )

    assert request.current_user_text == "Current user question."
    assert request.history_user_texts == ["Earlier user question.", "Previous user question."]
    assert request.prev_assistant_text == "Previous assistant answer."
    assert request.prev_assistant_usage == {"output_tokens": 456}
    assert request.prev_route_decisions[0].route_class == "R2"
    assert request.prev_route_decisions[0].difficulty == 2.0
    assert request.prev_route_decisions[0].margin == 0.7
    assert request.flags_text_override == "Current user question."
    assert request.context_metadata["history_user_turn_count"] == 2
    assert request.context_metadata["has_prev_assistant"] is True
    assert request.context_metadata["context_tokens_est"] > 0


@pytest.mark.asyncio
async def test_image_input_routes_directly_to_vision_model_without_prompt_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "What is in this screenshot?",
        attachments=[{"type": "image", "mime_type": "image/png"}],
    )

    routed = await apply_squilla_router(ctx)

    assert routed.model == "moonshotai/kimi-k2.6"
    assert routed.metadata["routed_tier"] == "image_model"
    assert routed.metadata["routed_model"] == "moonshotai/kimi-k2.6"
    assert routed.metadata["routing_applied"] is True
    assert routed.metadata["routing_confidence"] == 1.0
    assert routed.metadata["routing_source"] == "image_route"
    assert routed.metadata["route_max_history_turns"] == 1
    assert routed.metadata["thinking_requested"] is True
    assert routed.metadata["thinking_level"] == "medium"
    assert "[RESPONSE_POLICY:" not in routed.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fusion_config",
    [
        {"ensemble_enabled": True},
        {"ensemble_selection_mode": "router_dynamic"},
    ],
    ids=["shared", "legacy"],
)
async def test_c3_fusion_prefers_dedicated_image_model(
    monkeypatch: pytest.MonkeyPatch,
    fusion_config: dict[str, object],
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image/png", "data": "abc"}],
    )
    ctx.config.squilla_router.tiers = {
        "c0": {
            "model": "vision/fast",
            "supports_image": True,
        },
        "c3": {
            "model": "vision/high-quality",
            "supports_image": True,
            **fusion_config,
        },
        "image_model": {
            "model": "vision/dedicated",
            "supports_image": True,
            "image_only": True,
        },
    }

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "image_model"
    assert routed.model == "vision/dedicated"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fusion_config",
    [
        {"ensemble_enabled": True},
        {"ensemble_selection_mode": "router_dynamic"},
    ],
    ids=["shared", "legacy"],
)
async def test_c3_fusion_uses_another_non_c3_image_tier_without_dedicated_model(
    monkeypatch: pytest.MonkeyPatch,
    fusion_config: dict[str, object],
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image/png", "data": "abc"}],
    )
    ctx.config.squilla_router.tiers = {
        "c3": {
            "model": "vision/high-quality",
            "supports_image": True,
            **fusion_config,
        },
        "c0": {
            "model": "vision/fast",
            "supports_image": True,
        },
    }

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c0"
    assert routed.model == "vision/fast"


@pytest.mark.asyncio
async def test_c3_fusion_rejects_image_input_when_no_independent_image_tier_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image/png", "data": "abc"}],
    )
    ctx.config.squilla_router.tiers = {
        "c3": {
            "model": "vision/high-quality",
            "supports_image": True,
            "ensemble_enabled": True,
        },
    }

    result = await apply_squilla_router(ctx)

    assert result.metadata["image_input_forced_rejection_reason"] == (
        "router_image_route_unavailable"
    )


@pytest.mark.asyncio
async def test_global_fusion_rejects_c3_as_the_only_image_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image/png", "data": "abc"}],
    )
    ctx.config.llm_ensemble.enabled = True
    ctx.config.llm_ensemble.selection_mode = "static_openrouter_b5"
    ctx.config.squilla_router.tiers = {
        "c3": {
            "model": "vision/high-quality",
            "supports_image": True,
            "ensemble_enabled": False,
        },
    }

    result = await apply_squilla_router(ctx)

    assert result.metadata["image_input_forced_rejection_reason"] == (
        "router_image_route_unavailable"
    )


@pytest.mark.asyncio
async def test_single_c3_remains_available_for_image_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image/png", "data": "abc"}],
    )
    ctx.config.squilla_router.tiers = {
        "c3": {
            "model": "vision/high-quality",
            "supports_image": True,
            "ensemble_enabled": False,
        },
    }

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c3"
    assert routed.model == "vision/high-quality"


@pytest.mark.asyncio
async def test_image_route_prefers_dedicated_tier_over_declaration_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image/png", "data": "abc"}],
    )
    ctx.config.squilla_router.tiers = {
        "vision_primary": {
            "model": "vision/primary",
            "supports_image": True,
        },
        "image_model": {
            "model": "vision/dedicated",
            "supports_image": True,
            "image_only": True,
        },
    }

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "image_model"
    assert routed.model == "vision/dedicated"


@pytest.mark.asyncio
async def test_image_route_falls_back_when_dedicated_tier_has_blank_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image/png", "data": "abc"}],
    )
    ctx.config.squilla_router.tiers = {
        "vision_primary": {
            "model": "vision/primary",
            "supports_image": True,
        },
        "image_model": {
            "model": "   ",
            "supports_image": True,
            "image_only": True,
        },
    }

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "vision_primary"
    assert routed.model == "vision/primary"


@pytest.mark.asyncio
async def test_image_route_rejects_when_every_image_tier_has_blank_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image/png", "data": "abc"}],
    )
    ctx.config.squilla_router.tiers = {
        "vision_primary": {"model": "", "supports_image": True},
        "image_model": {
            "model": "   ",
            "supports_image": True,
            "image_only": True,
        },
    }

    result = await apply_squilla_router(ctx)

    assert result.metadata["image_input_forced_rejection_reason"] == (
        "router_image_route_unavailable"
    )


@pytest.mark.asyncio
async def test_large_image_attachment_bypass_honors_capacity_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "foreign/vision-small": {
                "context_window": 300_000,
                "max_output_tokens": 10_000,
            },
            "openrouter/vision-large": {
                "context_window": 300_000,
                "max_output_tokens": 10_000,
            },
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    ctx = make_context(
        "Describe the attached materials.",
        attachments=[{"type": "image", "mime_type": "image/png"}],
    )
    ctx.config.squilla_router.tiers = {
        "vision_small": {
            "provider": "foreign",
            "model": "vision-small",
            "supports_image": True,
            "image_only": True,
            "thinking_level": "off",
        },
        "vision_large": {
            "provider": "openrouter",
            "model": "vision-large",
            "supports_image": True,
            "image_only": True,
            "thinking_level": "off",
        },
        "c3": {
            "provider": "openrouter",
            "model": "vision-large",
            "thinking_level": "off",
        },
    }
    ctx.config.squilla_router.tier_provider_mismatch = "veto"
    ctx.metadata["attachment_material_estimated_tokens"] = 50_000

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["large_context_floor_min_tier"] == "c3"
    assert routed.metadata["routed_tier"] == "vision_large"
    assert routed.model == "vision-large"
    assert routed.metadata["router_fallback_chain"] == []
    assert "router_tier_provider_mismatch" not in routed.metadata


@pytest.mark.asyncio
async def test_empty_caption_large_attachment_routes_by_proven_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("empty attachment route should not classify"),
    )
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "openrouter/text-small": {
                "context_window": 128_000,
                "max_output_tokens": 10_000,
            },
            "openrouter/text-large": {
                "context_window": 300_000,
                "max_output_tokens": 10_000,
            },
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    ctx = make_context("", attachments=[{"mime_type": "application/pdf"}])
    ctx.config.squilla_router.tiers = {
        "c0": {
            "provider": "openrouter",
            "model": "text-small",
            "thinking_level": "off",
        },
        "c3": {
            "provider": "openrouter",
            "model": "text-large",
            "thinking_level": "off",
        },
    }
    ctx.metadata["attachment_material_estimated_tokens"] = 50_000

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routing_source"] == "large_context_attachment_route"
    assert routed.metadata["large_context_floor_min_tier"] == "c3"
    assert routed.metadata["routed_tier"] == "c3"
    assert routed.model == "text-large"
    assert routed.metadata["router_fallback_chain"] == []


@pytest.mark.asyncio
async def test_empty_large_attachment_veto_fails_closed_without_active_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = ModelCatalog()
    catalog.set_user_overrides(
        {
            "foreign/text-large": {
                "context_window": 300_000,
                "max_output_tokens": 10_000,
            }
        }
    )
    monkeypatch.setattr("opensquilla.provider.model_catalog._shared_catalog", catalog)
    ctx = make_context("", attachments=[{"mime_type": "application/pdf"}])
    ctx.config.squilla_router.tier_provider_mismatch = "veto"
    ctx.config.squilla_router.tiers = {
        "c3": {
            "provider": "foreign",
            "model": "text-large",
            "thinking_level": "off",
        }
    }
    ctx.metadata["attachment_material_estimated_tokens"] = 90_000

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["large_context_capacity_blocked"] is True
    assert "routed_tier" not in routed.metadata


@pytest.mark.asyncio
async def test_image_route_uses_first_configured_image_tier_without_random_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    monkeypatch.setattr(
        "random.choice",
        lambda _items: pytest.fail("image route should be deterministic"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image/png", "data": "abc"}],
    )
    ctx.config.squilla_router.tiers = {
        "vision_primary": {
            "model": "vision/primary",
            "supports_image": True,
            "thinking_level": "low",
        },
        "vision_backup": {
            "model": "vision/backup",
            "supports_image": True,
            "thinking_level": "high",
        },
        "c1": {"model": "text/model"},
    }

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "vision_primary"
    assert routed.metadata["routed_model"] == "vision/primary"
    assert routed.model == "vision/primary"


@pytest.mark.asyncio
async def test_image_route_records_tier_provider_for_cross_provider_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "What is in this screenshot?",
        attachments=[{"type": "image", "mime_type": "image/png"}],
    )
    ctx.config.llm.provider = "anthropic"
    ctx.config.squilla_router.cross_provider_tiers = True
    ctx.config.squilla_router.tiers = {
        "image_model": {
            "model": "vision/model-1",
            "provider": "openai",
            "supports_image": True,
            "image_only": True,
        },
        "c1": {"model": "text/model-1"},
    }

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routing_source"] == "image_route"
    assert routed.metadata["routing_applied"] is True
    assert routed.metadata.get("routed_provider") == "openai"


@pytest.mark.asyncio
async def test_image_route_flags_tier_provider_mismatch_when_cross_provider_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image", "mime_type": "image/png"}],
    )
    ctx.config.llm.provider = "anthropic"
    ctx.config.squilla_router.cross_provider_tiers = False
    ctx.config.squilla_router.tiers = {
        "image_model": {
            "model": "vision/model-1",
            "provider": "openai",
            "supports_image": True,
            "image_only": True,
        },
        "c1": {"model": "text/model-1"},
    }

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routing_source"] == "image_route"
    assert routed.metadata.get("router_tier_provider_mismatch") == "openai"


@pytest.mark.asyncio
async def test_global_fixed_lineup_keeps_image_provider_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    ctx = make_context(
        "Describe this screenshot.",
        attachments=[{"type": "image", "mime_type": "image/png"}],
    )
    ctx.config.llm.provider = "deepseek"
    ctx.config.llm_ensemble.enabled = True
    ctx.config.llm_ensemble.selection_mode = "static_openrouter_b5"
    ctx.config.squilla_router.cross_provider_tiers = False
    ctx.config.squilla_router.tiers = {
        "image_model": {
            "model": "vision/model-1",
            "provider": "openai",
            "supports_image": True,
            "image_only": True,
        },
        "c1": {"model": "text/model-1", "provider": "openai"},
    }

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routing_source"] == "image_route"
    assert routed.metadata["router_tier_provider_role"] == "direct"
    assert routed.metadata["router_tier_provider_mismatch"] == "openai"
    assert routed.metadata["routed_provider"] == "openai"


@pytest.mark.asyncio
async def test_gate_needs_image_routes_followup_to_vision_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("gate image routing should not invoke text strategy"),
    )
    ctx = make_context("Continue from that image.")
    ctx.metadata["router_history_has_recent_image"] = True
    ctx.metadata["router_history_image_turn_count"] = 2
    ctx.metadata["router_turns_since_last_image"] = 1
    ctx.metadata["router_vision_followup_gate_decision"] = "needs_image"
    ctx.metadata["router_vision_followup_needs_image"] = True

    routed = await apply_squilla_router(ctx)

    assert routed.model == "moonshotai/kimi-k2.6"
    assert routed.metadata["routed_tier"] == "image_model"
    assert routed.metadata["routing_source"] == "image_route"
    assert routed.metadata["image_route_reason"] == "gate_history"
    assert routed.metadata["route_max_history_turns"] == 8


@pytest.mark.asyncio
async def test_sticky_without_gate_no_longer_routes_to_vision_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = fake_strategy(
        monkeypatch,
        "c2",
        0.77,
        {"route_class": "R2", "thinking_mode": "T1", "prompt_policy": "P1"},
    )
    ctx = make_context("Write a Python script.")
    ctx.metadata["router_history_has_recent_image"] = True
    ctx.metadata["router_history_image_turn_count"] = 1
    ctx.metadata["router_vision_sticky_remaining"] = 3
    ctx.metadata["router_vision_followup_gate_decision"] = "text_only"
    ctx.metadata["router_vision_followup_needs_image"] = False

    routed = await apply_squilla_router(ctx)

    assert strategy.calls == 1
    assert routed.metadata["routing_source"] != "image_route"
    assert routed.metadata.get("image_route_reason") is None


@pytest.mark.asyncio
async def test_recent_historical_image_without_sticky_uses_text_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = fake_strategy(
        monkeypatch,
        "c0",
        0.88,
        {"route_class": "R0", "thinking_mode": "T1", "prompt_policy": "P0"},
    )
    ctx = make_context("Help me write a Python script.")
    ctx.metadata["router_history_has_recent_image"] = True
    ctx.metadata["router_history_image_turn_count"] = 1

    routed = await apply_squilla_router(ctx)

    assert strategy.calls == 1
    assert routed.metadata["routing_source"] == "v4_phase3"
    assert routed.metadata["routed_tier"] == "c0"
    assert "image_route_reason" not in routed.metadata


@pytest.mark.asyncio
async def test_image_attachment_without_image_tier_fails_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail(
            "image routing without image tier should not invoke text strategy"
        ),
    )
    ctx = make_context(
        "What is in this screenshot?",
        attachments=[{"type": "image", "mime_type": "image/png"}],
    )
    ctx.config.squilla_router.tiers["image_model"]["supports_image"] = False

    result = await apply_squilla_router(ctx)

    assert result.metadata["image_input_forced_rejection_reason"] == (
        "router_image_route_unavailable"
    )


@pytest.mark.asyncio
async def test_caption_less_image_attachment_still_routes_to_vision_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail("image routing should not invoke text strategy"),
    )
    for caption in ("", "   \n\t "):
        ctx = make_context(
            caption,
            attachments=[{"type": "image", "mime_type": "image/png"}],
        )

        routed = await apply_squilla_router(ctx)

        assert routed.metadata["routing_source"] == "image_route"
        assert routed.metadata["routed_tier"] == "image_model"


@pytest.mark.asyncio
async def test_caption_less_image_attachment_without_image_tier_fails_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        squilla_router_step,
        "_get_strategy",
        lambda _config: pytest.fail(
            "image routing without image tier should not invoke text strategy"
        ),
    )
    ctx = make_context(
        "",
        attachments=[{"type": "image", "mime_type": "image/png"}],
    )
    ctx.config.squilla_router.tiers["image_model"]["supports_image"] = False

    result = await apply_squilla_router(ctx)

    assert result.metadata["image_input_forced_rejection_reason"] == (
        "router_image_route_unavailable"
    )


@pytest.mark.asyncio
async def test_non_image_attachment_does_not_force_vision_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = fake_strategy(
        monkeypatch,
        "c1",
        0.91,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )
    ctx = make_context(
        "Summarize the attached PDF text.",
        attachments=[{"type": "application/pdf", "mime_type": "application/pdf"}],
    )

    routed = await apply_squilla_router(ctx)

    assert strategy.calls == 1
    assert routed.metadata["routing_source"] == "v4_phase3"
    assert routed.metadata["routed_tier"] == "c1"


@pytest.mark.asyncio
async def test_observe_rollout_records_decisions_without_applying_model_or_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_strategy(
        monkeypatch,
        "c2",
        0.93,
        {
            "route_class": "R2",
            "thinking_mode": "T2",
            "prompt_policy": "P2",
            "prompt_hint": "Use extra care.",
        },
    )
    ctx = make_context("Analyze this code path.", rollout_phase="observe")
    baseline_model = ctx.model

    routed = await apply_squilla_router(ctx)

    assert routed.model == baseline_model
    assert routed.metadata["routed_tier"] == "c2"
    assert routed.metadata["routed_model"] == "z-ai/glm-5.2"
    assert routed.metadata["routing_applied"] is False
    assert routed.metadata["thinking_mode"] == "T2"
    assert routed.metadata["thinking_level"] == "medium"
    assert "[RESPONSE_POLICY:" not in routed.message


@pytest.mark.asyncio
async def test_repeated_message_across_sessions_is_classified_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = fake_strategy(
        monkeypatch,
        "c1",
        0.91,
        {
            "route_class": "R1",
            "thinking_mode": "T1",
            "prompt_policy": "P0",
        },
    )

    first = await apply_squilla_router(make_context("Repeat this.", session_key="session-a"))
    second = await apply_squilla_router(make_context("Repeat this.", session_key="session-b"))

    assert first.metadata["routing_source"] == "v4_phase3"
    assert second.metadata["routing_source"] == "v4_phase3"
    assert strategy.calls == 2


@pytest.mark.asyncio
async def test_required_router_runtime_failure_falls_back_to_heuristic_tiering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    ctx = make_context("Explain the setup steps.")
    ctx.config.squilla_router.require_router_runtime = True

    routed = await apply_squilla_router(ctx)

    # Short plain text lands in the heuristic c0 band; the decision is
    # honestly attributed to the fallback classifier, not the ML runtime.
    assert routed.metadata["routing_source"] == "heuristic"
    assert routed.metadata["routed_tier"] == "c0"
    assert routed.metadata["routing_confidence"] == pytest.approx(0.55)
    assert routed.metadata["routing_extra"]["heuristic_band"] == "short_plain"


@pytest.mark.asyncio
async def test_router_runtime_failure_emits_one_operator_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    caplog.set_level(logging.WARNING)

    first = make_context("Explain the setup steps.")
    first.config.squilla_router.require_router_runtime = True
    await apply_squilla_router(first)
    squilla_router_step._strategy = None
    squilla_router_step._strategy_key = None
    second = make_context("Explain the setup steps again.")
    second.config.squilla_router.require_router_runtime = True
    await apply_squilla_router(second)

    messages = [
        record.getMessage()
        for record in caplog.records
        if "Microsoft Visual C++ Redistributable 2015-2022 x64" in record.getMessage()
    ]
    assert len(messages) == 1
    assert "safe router fallback" in messages[0]
    assert "https://aka.ms/vs/17/release/vc_redist.x64.exe" in messages[0]
    assert "After installing, reopen PowerShell and restart OpenSquilla" in messages[0]


@pytest.mark.asyncio
async def test_router_runtime_failure_emits_macos_libomp_guidance(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", MacLibompExplodingV4Strategy)
    caplog.set_level(logging.WARNING)

    ctx = make_context("Explain the setup steps.")
    ctx.config.squilla_router.require_router_runtime = True
    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routing_source"] == "heuristic"
    messages = [
        record.getMessage()
        for record in caplog.records
        if "brew install libomp" in record.getMessage()
    ]
    assert len(messages) == 1
    assert "default-tier routing" in messages[0]
    assert "opensquilla gateway restart" in messages[0]


@pytest.mark.asyncio
async def test_runtime_router_short_chinese_prompt_injects_localized_p0_hint() -> None:
    require_runtime_router()
    ctx = make_context("直接总结这句话。")

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routing_source"] == "v4_phase3"
    assert routed.metadata["routed_tier"] == "c0"
    assert routed.model == "deepseek/deepseek-v4-flash"
    assert routed.metadata["thinking_mode"] == "T0"
    assert routed.metadata.get("thinking_requested") is None
    assert routed.metadata["prompt_policy"] == "P0"
    assert "[RESPONSE_POLICY: 直接作答，缩短思考长度，避免无关展开。]" in routed.message


@pytest.mark.asyncio
async def test_runtime_router_complex_request_applies_deep_thinking_without_p2_prompt() -> None:
    require_runtime_router()
    ctx = make_context("Plan a risky multi-step database migration with rollback and verification.")

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routing_source"] == "v4_phase3"
    assert routed.metadata["routed_tier"] == "c3"
    assert routed.model == "anthropic/claude-opus-4.8"
    assert routed.metadata["thinking_mode"] == "T3"
    assert routed.metadata["thinking_requested"] is True
    assert routed.metadata["thinking_level"] == "high"
    assert routed.metadata["prompt_policy"] == "P2"
    assert routed.metadata["routing_extra"]["prompt_hint"] == (
        "Analyze thoroughly, cover key constraints, avoid omissions."
    )
    assert "[RESPONSE_POLICY:" not in routed.message
