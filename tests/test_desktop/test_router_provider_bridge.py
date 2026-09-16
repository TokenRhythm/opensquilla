"""Compiled Desktop production presets/serializer cross the real Gateway boundary."""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.onboarding.mutations import LlmProfileActivationError, upsert_llm_provider
from opensquilla.onboarding.router_policy import (
    RouterProviderConflictError,
    validate_router_candidate,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def desktop_router_toml():
    module = ROOT / "desktop/electron/dist/desktop-router-config.js"
    node = shutil.which("node")
    if not node or not module.is_file():
        if os.environ.get("OPENSQUILLA_REQUIRE_DESKTOP_ROUTER_BRIDGE") == "1":
            pytest.fail("Build Desktop TypeScript and provide Node before the Router bridge check")
        pytest.skip("Desktop compiled bridge runs explicitly after TypeScript build in Desktop CI")

    def serialize(binding: str | None = "follow_primary", enabled: bool = True) -> str:
        script = """
import {
  routerConfigTomlLines, resolveDesktopRouterUpdate,
} from './desktop/electron/dist/desktop-router-config.js';
import { defaultRouterTiers } from './desktop/electron/dist/desktop-router-profiles.js';
const router = resolveDesktopRouterUpdate({
  payload: {}, existing: null, routerMode: 'recommended', routerDefaultTier: 'c1',
  defaultTiers: defaultRouterTiers('openrouter', 'recommended'), freshConfig: true,
});
const [binding, enabled] = process.argv.slice(1);
if (binding === 'absent') delete router.routerPresetBinding;
else router.routerPresetBinding = binding;
if (enabled === 'false') router.routerMode = 'disabled';
process.stdout.write(routerConfigTomlLines(router).join('\\n'));
"""
        result = subprocess.run(
            [node, "--input-type=module", "-e", script, binding or "absent", str(enabled).lower()],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        return result.stdout

    return serialize


def config_from_desktop(raw: str) -> GatewayConfig:
    # A pristine Desktop runtime may carry the OpenRouter default without a key.
    return GatewayConfig.model_validate({"llm": {"provider": "openrouter"}, **tomllib.loads(raw)})


def test_new_desktop_recommendations_follow_first_usable_primary(desktop_router_toml) -> None:
    source = config_from_desktop(desktop_router_toml())
    assert source.squilla_router.enabled
    assert source.squilla_router.preset_binding == "follow_primary"
    assert source.squilla_router.tiers["c1"]["provider"] == "openrouter"
    result = upsert_llm_provider(source, provider_id="tokenrhythm", api_key="synthetic-bridge-key")
    assert result.config.llm.provider == "tokenrhythm"
    assert result.config.squilla_router.preset_binding == "follow_primary"
    assert result.config.squilla_router.tiers["c1"]["provider"] == "tokenrhythm"
    assert result.config.squilla_router.tier_profile is None
    assert source.llm.provider == "openrouter"


@pytest.mark.parametrize("binding", [None, "custom"])
def test_identical_historical_or_custom_presets_need_explicit_resolution(
    desktop_router_toml, binding: str | None,
) -> None:
    raw = desktop_router_toml(binding)
    if binding is None:
        assert "preset_binding" not in raw
    source = config_from_desktop(raw)
    before = source.model_dump()
    with pytest.raises(LlmProfileActivationError) as caught:
        upsert_llm_provider(source, provider_id="tokenrhythm", api_key="synthetic-bridge-key")
    assert caught.value.reason == "router_provider_conflict"
    assert caught.value.details["conflictProviders"] == ["openrouter"]
    assert source.model_dump() == before
    disabled = upsert_llm_provider(
        source, provider_id="tokenrhythm", api_key="synthetic-bridge-key", router_action="disable",
    ).config
    assert not disabled.squilla_router.enabled
    assert disabled.squilla_router.tiers == source.squilla_router.tiers
    recommended = upsert_llm_provider(
        source, provider_id="tokenrhythm", api_key="synthetic-bridge-key",
        router_action="use_recommended",
    ).config
    assert recommended.squilla_router.tiers["c1"]["provider"] == "tokenrhythm"
    assert recommended.squilla_router.preset_binding == "follow_primary"


def test_disabled_legacy_desktop_router_does_not_block_provider_save(desktop_router_toml) -> None:
    source = config_from_desktop(desktop_router_toml(None, enabled=False))
    result = upsert_llm_provider(source, provider_id="tokenrhythm", api_key="synthetic-bridge-key")
    assert result.config.llm.provider == "tokenrhythm"
    assert not result.config.squilla_router.enabled


def test_desktop_primary_change_matches_gateway_execution_dependencies(desktop_router_toml):
    # Exercise compiled production TypeScript against the Gateway policy, with
    # synthetic saved tables and no credentials, provider calls, or disk writes.
    cases = []
    expected = []
    modes = (None, "custom_b5", "static_tokenrhythm_b5", "static_openrouter_b5", "router_dynamic")
    for mode, enabled, c3_enabled, legacy_tier, foreign_tier in itertools.product(
        modes, (False, True), (None, False, True), (None, "c0", "c3"), ("c0", "c3")
    ):
        tiers = {
            tier: {"provider": "tokenrhythm", "model": f"synthetic-{tier}"}
            for tier in ("c0", "c1", "c2", "c3")
        }
        tiers[foreign_tier]["provider"] = "openrouter"
        if c3_enabled is not None:
            tiers["c3"]["ensemble_enabled"] = c3_enabled
        if legacy_tier is not None:
            tiers[legacy_tier]["ensemble_selection_mode"] = "router_dynamic"
        ensemble = {
            "enabled": enabled,
            "candidates": [
                {"provider": "tokenrhythm", "model": "synthetic-a"},
                {"provider": "tokenrhythm", "model": "synthetic-b"},
            ],
        }
        if mode is not None:
            ensemble["selection_mode"] = mode
        case = {
            "llm": {"provider": "openrouter", "model": "synthetic-primary"},
            "squilla_router": {
                "enabled": True, "preset_binding": "custom",
                "cross_provider_tiers": False, "tiers": tiers,
            },
            "llm_ensemble": ensemble,
        }
        candidate = GatewayConfig.model_validate(case)
        candidate.llm.provider = "tokenrhythm"
        try:
            validate_router_candidate(candidate)
        except RouterProviderConflictError:
            expected.append(False)
        else:
            expected.append(True)
        cases.append(case)

    script = """
import { readFileSync } from 'node:fs';
import { stringify } from './desktop/electron/node_modules/smol-toml/dist/index.js';
import { prepareDesktopPrimaryProviderChange }
  from './desktop/electron/dist/desktop-primary-provider-change.js';
const cases = JSON.parse(readFileSync(0, 'utf8'));
const results = cases.map(config => {
  try {
    prepareDesktopPrimaryProviderChange({
      existingRaw: stringify(config), provider: 'tokenrhythm', defaultTiers: {},
      requestedRouter: {
        routerMode: 'custom', routerDefaultTier: 'c1', routerTiers: {},
        routerPresetBinding: 'custom', writeIntent: 'preserve',
      },
    });
    return true;
  } catch (error) {
    if (!error.message.startsWith('Saved Router tiers use another provider.')) throw error;
    return false;
  }
});
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", script],
        input=json.dumps(cases), cwd=ROOT, check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=30,
    )
    actual = json.loads(result.stdout)
    assert len(actual) == len(expected)
    for case, actual_result, expected_result in zip(cases, actual, expected, strict=True):
        assert actual_result == expected_result, case
