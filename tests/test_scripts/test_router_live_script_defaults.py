from __future__ import annotations

import ast
import importlib.util
import sys
import tomllib
from pathlib import Path

from opensquilla.tools.registry import ToolProfile

EXPECTED_ROUTER_MODELS = {
    "c0": "qwen/qwen3.7-flash",
    "c1": "deepseek/deepseek-v4-flash",
    "c2": "deepseek/deepseek-v4-pro",
    "c3": "z-ai/glm-5.3",
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


def test_smoke_script_tier_defaults_match_router_defaults(tmp_path: Path) -> None:
    smoke = _load_smoke_module()
    assert {tier: cfg["model"] for tier, cfg in smoke.TIERS.items()} == EXPECTED_ROUTER_MODELS

    config_path = tmp_path / "gateway.toml"
    smoke._write_live_gateway_config(config_path, "")

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert data["llm"]["model"] == EXPECTED_ROUTER_MODELS["c1"]
    assert {
        tier: cfg["model"]
        for tier, cfg in data["squilla_router"]["tiers"].items()
    } == EXPECTED_ROUTER_MODELS


def test_live_scripts_use_valid_registry_tool_profile_values() -> None:
    valid_profiles = {profile.value for profile in ToolProfile}
    script_paths = [
        Path("scripts/live_provider_profile_gateway_e2e.py"),
        Path("scripts/live_v4_router_evidence.py"),
    ]

    for script_path in script_paths:
        tree = ast.parse(script_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "OPENSQUILLA_TOOL_PROFILE"
                ):
                    continue
                assert isinstance(node.value, ast.Constant), script_path
                assert node.value.value in valid_profiles, script_path
