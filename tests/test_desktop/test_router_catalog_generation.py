"""Desktop's offline catalog must offer the same Router routes as onboarding."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.mutations import _normalize_tier_payload, upsert_router
from opensquilla.onboarding.provider_specs import provider_catalog_payload
from opensquilla.provider.preset_registry import get_preset
from opensquilla.router_tiers import TEXT_TIERS, TierConfig

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "desktop/electron/src/generated/desktop-router-catalog.ts"
SUPPORTED_PROVIDERS = tuple(
    provider["providerId"]
    for provider in provider_catalog_payload()
    if provider["routerSupported"]
)


@pytest.fixture(scope="module")
def desktop_profiles() -> dict:
    source = CATALOG.read_text(encoding="utf-8")
    return json.loads(source.split(" = ", 1)[1])


def test_generated_desktop_catalog_is_current() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate_desktop_router_catalog.py"), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_desktop_offers_every_backend_supported_router(desktop_profiles: dict) -> None:
    assert set(desktop_profiles) == set(SUPPORTED_PROVIDERS)
    assert {
        "kimi_coding_openai", "kimi_coding_anthropic", "minimax", "minimax_cn",
        "minimax_global", "minimax_coding_openai", "minimax_coding_anthropic",
        "mimo_openai", "mimo_anthropic", "volcengine_coding_plan", "qianfan",
    } <= desktop_profiles.keys()
    assert "minimax_openai" not in desktop_profiles


@pytest.mark.parametrize("provider_id", SUPPORTED_PROVIDERS)
def test_desktop_routes_preserve_backend_execution_defaults(
    desktop_profiles: dict, provider_id: str,
) -> None:
    preset = get_preset(provider_id)
    assert preset is not None
    desktop_tiers = desktop_profiles[provider_id]
    assert set(TEXT_TIERS) <= desktop_tiers.keys()
    assert desktop_tiers.keys() == preset.tiers.keys()
    for name, tier in desktop_tiers.items():
        assert tier["provider"] and tier["model"]
        expected = dict(preset.tiers[name])
        expected.pop("supports_image", None)
        normalized = _normalize_tier_payload(name, tier)
        assert TierConfig.from_value(normalized) == TierConfig.from_value(expected)
        assert tier.get("imageOnly", False) == expected.get("image_only", False)
        assert tier.get("description", "") == expected.get("description", "")
        assert "supportsImage" not in tier

    config = GatewayConfig(
        llm={"provider": provider_id, "model": preset.default_model},
        squilla_router={"enabled": False},
    )
    desktop = upsert_router(config, mode="recommended", tiers=desktop_tiers).config
    backend = upsert_router(config, mode="recommended").config
    assert desktop.squilla_router.enabled
    assert desktop.squilla_router.preset_binding == "follow_primary"
    assert desktop.squilla_router.tiers == backend.squilla_router.tiers


def test_c3_single_model_default_and_image_routes_are_preserved(desktop_profiles: dict) -> None:
    assert desktop_profiles["tokenrhythm"]["c3"]["ensembleEnabled"] is False
    assert "ensembleEnabled" not in desktop_profiles["openrouter"]["c3"]
    assert "ensembleSelectionMode" not in desktop_profiles["tokenrhythm"]["c3"]
    assert desktop_profiles["qianfan"]["image_model"]["imageOnly"] is True
