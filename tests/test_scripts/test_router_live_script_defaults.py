from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

EXPECTED_ROUTER_MODELS = {
    "c0": "deepseek/deepseek-v4-flash",
    "c1": "deepseek/deepseek-v4-pro",
    "c2": "z-ai/glm-5.2",
    "c3": "anthropic/claude-opus-4.8",
}


def _load_smoke_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "smoke_v4_phase3_router.py"
    spec = importlib.util.spec_from_file_location("smoke_v4_phase3_router", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_smoke_script_tier_defaults_match_router_defaults() -> None:
    smoke = _load_smoke_module()
    assert {tier: cfg["model"] for tier, cfg in smoke.TIERS.items()} == EXPECTED_ROUTER_MODELS
