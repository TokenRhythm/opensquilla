"""Heuristic router fallback: deterministic bands, wiring, and status accessor.

The heuristic strategy replaces the silent default-tier degradation when the
V4 ML runtime cannot load. These tests never touch the real ONNX bundle: the
V4 constructor is monkeypatched at the step's import seam, exactly like the
existing degraded-path tests.
"""

from __future__ import annotations

import pytest

from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.routing.heuristic import (
    BORDERLINE_CONFIDENCE,
    CODE_OR_MATERIAL_MIN_CHARS,
    HEAVY_MIN_CHARS,
    MEDIUM_PLAIN_MAX_CHARS,
    SHORT_PLAIN_MAX_CHARS,
    HeuristicRouterStrategy,
    classify_features,
    extract_features,
)
from opensquilla.engine.steps import squilla_router as squilla_router_step
from opensquilla.engine.steps.squilla_router import (
    apply_squilla_router,
    router_runtime_status,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.router_runtime_diagnostics import (
    ROUTER_RUNTIME_UNAVAILABLE,
    WINDOWS_VC_RUNTIME_MISSING,
)

ALL_TIERS = ["c0", "c1", "c2", "c3"]


class ExplodingV4Strategy:
    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError(
            "failed to initialize V4 Phase 3 router: DLL load failed while importing "
            "onnxruntime_pybind11_state"
        )


class UnavailableConstructedV4Strategy:
    """V4 adapter shape when require_router_runtime=false swallowed the failure."""

    source = "v4_phase3"
    _available = False

    def __init__(self, *args, **kwargs) -> None:
        pass


class LoadedFakeV4Strategy:
    source = "v4_phase3"
    _available = True

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def classify(
        self,
        message: str,
        valid_tiers: list[str],
        routing_history: list[dict] | None = None,
        **kwargs: object,
    ) -> tuple[str, float, str, dict]:
        return "c1", 0.9, "v4_phase3", {"route_class": "R1"}


class ExplodingHeuristic:
    def __init__(self, *args, **kwargs) -> None:
        raise RuntimeError("synthetic heuristic construction failure")


@pytest.fixture(autouse=True)
def reset_squilla_router_state() -> None:
    squilla_router_step._history_store.clear()
    squilla_router_step._strategy = None
    squilla_router_step._strategy_key = None
    squilla_router_step._router_runtime_warning_emitted = False
    yield
    squilla_router_step._history_store.clear()
    squilla_router_step._strategy = None
    squilla_router_step._strategy_key = None
    squilla_router_step._router_runtime_warning_emitted = False


def make_context(
    message: str,
    *,
    session_key: str = "agent:heuristic:test",
    attachments: list[dict] | None = None,
) -> TurnContext:
    config = GatewayConfig()
    config.squilla_router.rollout_phase = "full"
    return TurnContext(
        message=message,
        session_key=session_key,
        config=config,
        provider=None,
        model=config.llm.model,
        tool_defs=[],
        system_prompt="system",
        attachments=attachments or [],
    )


async def classify(
    message: str,
    *,
    valid_tiers: list[str] | None = None,
    routing_history: list[dict] | None = None,
    attachment_count: int | None = None,
) -> tuple[str, float, str, dict]:
    strategy = HeuristicRouterStrategy()
    return await strategy.classify(
        message,
        valid_tiers if valid_tiers is not None else list(ALL_TIERS),
        routing_history=routing_history,
        attachment_count=attachment_count,
    )


# --- determinism ------------------------------------------------------------


async def test_same_input_always_produces_same_decision() -> None:
    message = "please compare these two deployment options"
    first = await classify(message)
    second = await classify(message)
    assert first == second


def test_classify_features_is_deterministic_for_same_features() -> None:
    features = extract_features("x" * 3000, routing_history=[{}, {}], attachment_count=1)
    assert classify_features(features) == classify_features(dict(features))


# --- band boundaries ---------------------------------------------------------


async def test_short_plain_band_routes_c0() -> None:
    tier, confidence, source, extra = await classify("a" * SHORT_PLAIN_MAX_CHARS)
    assert (tier, source) == ("c0", "heuristic")
    assert confidence == pytest.approx(0.55)
    assert extra["heuristic_band"] == "short_plain"


async def test_just_over_short_boundary_routes_c1() -> None:
    tier, confidence, _, extra = await classify("a" * (SHORT_PLAIN_MAX_CHARS + 1))
    assert tier == "c1"
    assert confidence == pytest.approx(0.55)
    assert extra["heuristic_band"] == "medium_plain"


async def test_medium_boundary_stays_confident_c1() -> None:
    _, confidence, _, extra = await classify("a" * MEDIUM_PLAIN_MAX_CHARS)
    assert extra["heuristic_band"] == "medium_plain"
    assert confidence == pytest.approx(0.55)


async def test_borderline_band_is_below_gate_threshold() -> None:
    tier, confidence, _, extra = await classify("a" * (MEDIUM_PLAIN_MAX_CHARS + 1))
    assert tier == "c1"
    assert extra["heuristic_band"] == "borderline_plain"
    assert confidence == pytest.approx(BORDERLINE_CONFIDENCE)
    # Deliberately below both gate cutoffs (0.5, and 0.45 for above-default
    # tiers) so the confidence gate defers to the configured default_tier.
    assert confidence < 0.45


async def test_long_plain_input_routes_c2() -> None:
    tier, confidence, _, extra = await classify("a" * CODE_OR_MATERIAL_MIN_CHARS)
    assert tier == "c2"
    assert confidence == pytest.approx(0.60)
    assert extra["heuristic_band"] == "code_or_material"


async def test_just_below_long_boundary_is_borderline() -> None:
    _, _, _, extra = await classify("a" * (CODE_OR_MATERIAL_MIN_CHARS - 1))
    assert extra["heuristic_band"] == "borderline_plain"


async def test_code_fence_routes_c2_even_when_short() -> None:
    tier, _, _, extra = await classify("```python\nprint('hi')\n```")
    assert tier == "c2"
    assert extra["heuristic_band"] == "code_or_material"
    assert extra["heuristic_features"]["has_code_fence"] is True


async def test_attachments_route_c2_even_when_short() -> None:
    tier, _, _, extra = await classify("summarize this file", attachment_count=1)
    assert tier == "c2"
    assert extra["heuristic_band"] == "code_or_material"
    assert extra["heuristic_features"]["attachment_count"] == 1


async def test_very_long_input_routes_c3() -> None:
    tier, confidence, _, extra = await classify("a" * HEAVY_MIN_CHARS)
    assert tier == "c3"
    assert confidence == pytest.approx(0.60)
    assert extra["heuristic_band"] == "heavy"


async def test_just_below_heavy_boundary_routes_c2() -> None:
    tier, _, _, extra = await classify("a" * (HEAVY_MIN_CHARS - 1))
    assert tier == "c2"
    assert extra["heuristic_band"] == "code_or_material"


async def test_three_fenced_blocks_route_c3() -> None:
    block = "```\ncode\n```\n"
    tier, _, _, extra = await classify(block * 3)
    assert tier == "c3"
    assert extra["heuristic_band"] == "heavy"
    assert extra["heuristic_features"]["code_fence_blocks"] == 3


async def test_two_fenced_blocks_stay_c2() -> None:
    block = "```\ncode\n```\n"
    tier, _, _, extra = await classify(block * 2)
    assert tier == "c2"
    assert extra["heuristic_band"] == "code_or_material"


# --- confidence invariants vs the policy confidence gate ----------------------


async def test_confident_band_confidences_survive_default_gate() -> None:
    """Confident bands must sit at/above the 0.5 default threshold.

    If they dropped below it, the confidence gate would flatten every
    heuristic decision back to the default tier, recreating the silent
    v4_unavailable degradation this strategy exists to replace.
    """
    for message, expected_tier in [
        ("short ask", "c0"),
        ("a" * 500, "c1"),
        ("a" * 3000, "c2"),
        ("a" * 13000, "c3"),
    ]:
        _, confidence, _, _ = await classify(message)
        assert confidence >= 0.5, (message[:20], expected_tier)


# --- output shape --------------------------------------------------------------


async def test_extra_mirrors_v4_adapter_shape_with_features() -> None:
    tier, _, source, extra = await classify("a" * 3000)
    assert source == "heuristic"
    assert extra["route_class"] == "R2"
    assert extra["top1_label"] == "R2"
    assert extra["thinking_mode"] == "T2"
    assert extra["prompt_policy"] == "P1"
    assert extra["model_version"] == "heuristic-v1"
    features = extra["heuristic_features"]
    assert features["char_len"] == 3000
    assert features["history_depth"] == 0
    assert tier == "c2"


async def test_history_depth_recorded_for_observability() -> None:
    _, _, _, extra = await classify(
        "quick follow up",
        routing_history=[{"final_tier": "c2"}, {"final_tier": "c2"}],
    )
    assert extra["heuristic_features"]["history_depth"] == 2


async def test_unconfigured_tier_falls_to_nearest_valid() -> None:
    tier_high, _, _, _ = await classify("a" * 13000, valid_tiers=["c0", "c1"])
    assert tier_high == "c1"
    tier_low, _, _, _ = await classify("short ask", valid_tiers=["c2", "c3"])
    assert tier_low == "c2"


# --- step wiring: failed V4 load falls back to the heuristic -------------------


@pytest.mark.asyncio
async def test_failed_v4_load_installs_heuristic_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    ctx = make_context("Summarize the meeting notes in two sentences.")

    routed = await apply_squilla_router(ctx)

    assert isinstance(squilla_router_step._strategy, HeuristicRouterStrategy)
    assert routed.metadata["routing_source"] == "heuristic"
    assert routed.metadata["routed_tier"] == "c0"
    expected_model = ctx.config.squilla_router.tiers["c0"]["model"]
    assert routed.metadata["routed_model"] == expected_model
    assert routed.model == expected_model


@pytest.mark.asyncio
async def test_heuristic_metadata_flows_through_policy_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    ctx = make_context("Summarize the meeting notes in two sentences.")

    routed = await apply_squilla_router(ctx)

    extra = routed.metadata["routing_extra"]
    # Policy bind stage ran over the heuristic decision unchanged.
    assert extra["base_tier"] == "c0"
    assert extra["final_tier"] == "c0"
    assert extra["confidence_threshold"] == 0.5
    assert extra["confidence_gate_applied"] is False
    assert extra["anti_downgrade_applied"] is False
    # Heuristic observability payload survives the policy stages.
    assert extra["heuristic_band"] == "short_plain"
    assert extra["heuristic_features"]["char_len"] == len(ctx.message.split("\n\n---\n")[0])
    # History accumulated exactly like an ML-classified turn.
    history = routed.metadata["routing_history"]
    assert history[-1]["final_tier"] == "c0"


@pytest.mark.asyncio
async def test_goal_routing_hint_drives_router_without_persisting_objective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    generic_event = "Continue working on the active Goal."
    objective = "Repair the migration and implement the typed API. " + "x" * 3000
    ctx = make_context(generic_event, session_key="agent:heuristic:goal-routing")
    ctx.routing_hint = objective

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routing_extra"]["heuristic_features"]["char_len"] == len(
        objective
    )
    assert routed.metadata["routing_history"][-1]["text"] == generic_event
    assert objective not in repr(routed.metadata)


@pytest.mark.asyncio
async def test_borderline_band_defers_to_configured_default_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    ctx = make_context("please summarize the following notes " + "n" * 1500)
    ctx.config.squilla_router.default_tier = "c2"

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routing_extra"]["heuristic_band"] == "borderline_plain"
    assert routed.metadata["routing_extra"]["confidence_gate_applied"] is True
    assert routed.metadata["routed_tier"] == "c2"


@pytest.mark.asyncio
async def test_heuristic_history_participates_in_anti_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    session = "agent:heuristic:anti-downgrade"

    first = await apply_squilla_router(
        make_context("```python\nprint('hi')\n```\nexplain what this prints", session_key=session)
    )
    assert first.metadata["routed_tier"] == "c2"

    second = await apply_squilla_router(
        make_context("thanks for the help", session_key=session)
    )
    assert second.metadata["routing_extra"]["anti_downgrade_applied"] is True
    assert second.metadata["routed_tier"] == "c2"


@pytest.mark.asyncio
async def test_non_image_attachments_reach_heuristic_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    ctx = make_context(
        "summarize this document",
        attachments=[{"type": "text/plain", "name": "synthetic.txt"}],
    )

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routed_tier"] == "c2"
    assert routed.metadata["routing_extra"]["heuristic_features"]["attachment_count"] == 1


@pytest.mark.asyncio
async def test_constructed_but_unavailable_v4_also_falls_back_to_heuristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", UnavailableConstructedV4Strategy)
    ctx = make_context("Summarize the meeting notes in two sentences.")
    ctx.config.squilla_router.require_router_runtime = False

    routed = await apply_squilla_router(ctx)

    assert isinstance(squilla_router_step._strategy, HeuristicRouterStrategy)
    assert routed.metadata["routing_source"] == "heuristic"


@pytest.mark.asyncio
async def test_unavailable_safety_net_when_heuristic_import_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.engine.routing.heuristic as heuristic_module
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    monkeypatch.setattr(heuristic_module, "HeuristicRouterStrategy", ExplodingHeuristic)
    ctx = make_context("Explain the setup steps.")

    routed = await apply_squilla_router(ctx)

    assert routed.metadata["routing_source"] == "v4_unavailable"
    assert routed.metadata["routed_tier"] == "c1"
    assert routed.metadata["routing_confidence"] == 0.0


# --- router_runtime_status accessor --------------------------------------------


def test_status_reports_uninitialized_before_first_strategy_load() -> None:
    status = router_runtime_status()
    assert status == {
        "initialized": False,
        "loaded": False,
        "code": None,
        "strategy": "unavailable",
        "error": None,
    }


@pytest.mark.asyncio
async def test_status_reports_heuristic_fallback_with_diagnostics_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    await apply_squilla_router(make_context("Explain the setup steps."))

    status = router_runtime_status()
    assert status["initialized"] is True
    assert status["loaded"] is False
    assert status["strategy"] == "heuristic"
    assert status["code"] == WINDOWS_VC_RUNTIME_MISSING
    assert "onnxruntime_pybind11_state" in status["error"]


def test_status_reports_loaded_v4_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", LoadedFakeV4Strategy)
    config = GatewayConfig()
    squilla_router_step.preload_strategy(config.squilla_router)

    status = router_runtime_status()
    assert status["initialized"] is True
    assert status["loaded"] is True
    assert status["strategy"] == "v4_phase3"
    assert status["code"] is None


@pytest.mark.asyncio
async def test_status_reports_unavailable_when_even_heuristic_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.engine.routing.heuristic as heuristic_module
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", ExplodingV4Strategy)
    monkeypatch.setattr(heuristic_module, "HeuristicRouterStrategy", ExplodingHeuristic)
    await apply_squilla_router(make_context("Explain the setup steps."))

    status = router_runtime_status()
    assert status["strategy"] == "unavailable"
    assert status["loaded"] is False
    assert status["code"] == WINDOWS_VC_RUNTIME_MISSING


@pytest.mark.asyncio
async def test_status_code_defaults_generic_for_unclassified_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.squilla_router.v4_phase3 as v4_phase3

    monkeypatch.setattr(v4_phase3, "V4Phase3Strategy", UnavailableConstructedV4Strategy)
    ctx = make_context("Explain the setup steps.")
    ctx.config.squilla_router.require_router_runtime = False
    await apply_squilla_router(ctx)

    status = router_runtime_status()
    assert status["strategy"] == "heuristic"
    assert status["code"] == ROUTER_RUNTIME_UNAVAILABLE
