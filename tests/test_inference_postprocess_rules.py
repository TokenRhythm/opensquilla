from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from opensquilla.squilla_router.v4_phase3 import (
    default_bundle_dir,
    runtime_src_import_path,
)

np = pytest.importorskip("numpy", reason="router inference postprocess tests need numpy")

BUNDLE_DIR = default_bundle_dir()


def _runtime_config() -> dict[str, Any]:
    return yaml.safe_load((BUNDLE_DIR / "router.runtime.yaml").read_text(encoding="utf-8"))


def _decide(
    probs: list[float],
    *,
    text: str = "Please answer this routine question.",
    aux_probs: dict[str, float] | None = None,
    config: dict[str, Any] | None = None,
    history: list[Any] | None = None,
    flags_text: str | None = None,
    turn_index: int = 0,
):
    cfg = config or _runtime_config()
    with runtime_src_import_path(BUNDLE_DIR):
        from src.router.inference.postprocess import apply_postprocess
        from src.router.inference.types import InferenceRequest

        request = InferenceRequest(
            current_user_text=text,
            history_user_texts=[],
            prev_assistant_text=None,
            prev_assistant_usage=None,
            prev_route_decisions=history or [],
            flags_text_override=flags_text,
            context_metadata={"turn_index": turn_index},
        )
        return apply_postprocess(np.array(probs, dtype=np.float64), aux_probs, request, cfg)


def test_under_routing_safety_promotes_heavy_tail_to_r2():
    decision = _decide([0.40, 0.14, 0.31, 0.15])

    assert decision.route_class == "R2"
    assert decision.selected_model == "deepseek/deepseek-v4-pro-0813"
    assert decision.thinking_mode == "T2"


def test_high_risk_flags_override_low_probability_route_to_r2_p2_t3():
    decision = _decide(
        [0.96, 0.02, 0.01, 0.01],
        flags_text="production deploy rollback migration",
    )

    assert decision.route_class == "R2"
    assert decision.flags["high_risk"] is True
    assert decision.prompt_policy == "P2"
    assert decision.thinking_mode == "T3"
    assert decision.selected_model == "deepseek/deepseek-v4-pro-0813"


def test_trivial_ack_forces_t0_p0_on_r0():
    decision = _decide([0.93, 0.03, 0.02, 0.02], text="好的")

    assert decision.route_class == "R0"
    assert decision.prompt_policy == "P0"
    assert decision.thinking_mode == "T0"
    assert decision.selected_model == "qwen/qwen3.7-flash"


def test_optional_sticky_tier_blocks_downgrade_without_margin_gate():
    config = deepcopy(_runtime_config())
    config["v4"]["sticky_tier"]["enabled"] = True
    config["v4"]["sticky_tier"]["max_user_len"] = 200

    decision = _decide(
        [0.99, 0.006, 0.003, 0.001],
        text="short follow-up",
        config=config,
        history=[SimpleNamespace(route_class="R3")],
    )

    assert decision.route_class == "R3"
    assert decision.sticky_applied is True
    assert decision.selected_model == "z-ai/glm-5.3"


def test_optional_sticky_tier_still_blocks_downgrade_after_margin_upgrade():
    config = deepcopy(_runtime_config())
    config["v4"]["sticky_tier"]["enabled"] = True
    config["v4"]["sticky_tier"]["max_user_len"] = 200

    decision = _decide(
        [0.40, 0.35, 0.15, 0.10],
        text="short follow-up",
        config=config,
        history=[SimpleNamespace(route_class="R3")],
    )

    assert decision.route_class == "R3"
    assert decision.sticky_applied is True
    assert decision.selected_model == "z-ai/glm-5.3"


def test_aux_downgrade_can_lower_high_route_when_enabled_and_not_margin_upgraded():
    config = deepcopy(_runtime_config())
    config["v4"]["aux_downgrade"]["enabled"] = True
    config["v4"]["aux_downgrade"]["threshold"] = 0.55

    decision = _decide(
        [0.01, 0.02, 0.03, 0.94],
        aux_probs={"initial": 0.02, "maintain": 0.10, "upgrade": 0.05, "downgrade": 0.85},
        config=config,
    )

    assert decision.route_class == "R2"
    assert decision.aux_downgrade_applied is True
    assert decision.selected_model == "deepseek/deepseek-v4-pro-0813"


def test_deep_conversation_context_floors_low_route_to_r1():
    decision = _decide(
        [0.94, 0.03, 0.02, 0.01],
        turn_index=4,
    )

    assert decision.route_class == "R1"
    assert decision.selected_model == "deepseek/deepseek-v4-flash-0731"
