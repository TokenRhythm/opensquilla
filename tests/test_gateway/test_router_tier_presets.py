"""Router tier presets: config adapter parity, upgrade fixtures, downgrade guard.

The golden fixture (``tests/test_provider/golden/router_tier_profiles.json``)
originated from
``git show staging/provider-overhaul:src/opensquilla/gateway/config.py``
(the ``_router_tier_profile_defaults`` dict literals at f884d4c9) and tracks
intentional updates to packaged defaults. The fixture battery shapes come
from the upgrade audit and protect how existing explicit configs load.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import tomli_w

from opensquilla.gateway.config import (
    ROUTER_TIER_PROFILE_IDS,
    GatewayConfig,
    _default_tiers,
    _router_tier_profile_defaults,
)
from opensquilla.onboarding.config_store import load_config, persist_config
from opensquilla.onboarding.router_specs import router_catalog_payload
from opensquilla.provider.preset_registry import (
    get_preset,
    router_ladder_provider,
)

GOLDEN_PATH = (
    Path(__file__).resolve().parents[1]
    / "test_provider"
    / "golden"
    / "router_tier_profiles.json"
)
LEGACY_NINE = frozenset(
    {
        "openrouter",
        "dashscope",
        "deepseek",
        "gemini",
        "volcengine",
        "byteplus",
        "openai",
        "zhipu",
        "moonshot",
    }
)
UNKNOWN_PROFILE_ERROR = (
    "unknown squilla_router.tier_profile 'groq'; expected one of "
    "byteplus, dashscope, deepseek, gemini, moonshot, openai, openrouter, "
    "volcengine, zhipu"
)


def _golden() -> dict[str, dict]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


# --- adapter parity ---------------------------------------------------------


def test_router_tier_profile_ids_are_exactly_the_legacy_nine() -> None:
    assert ROUTER_TIER_PROFILE_IDS == LEGACY_NINE


@pytest.mark.parametrize("profile_id", sorted(LEGACY_NINE))
def test_profile_defaults_match_packaged_golden(profile_id: str) -> None:
    assert _router_tier_profile_defaults(profile_id) == _golden()[profile_id]


def test_default_tiers_match_pre_registry_openrouter_literal() -> None:
    assert _default_tiers() == _golden()["openrouter"]


def test_profile_defaults_returns_mutable_copies() -> None:
    first = _router_tier_profile_defaults("openai")
    first["c0"]["model"] = "mutated"
    assert _router_tier_profile_defaults("openai")["c0"]["model"] != "mutated"


def test_synthesized_preset_id_is_rejected_as_tier_profile() -> None:
    # groq has a synthesized preset in the registry, but tier_profile
    # acceptance stays pinned to the legacy nine (rc1 bricks on unknown ids).
    with pytest.raises(ValueError) as excinfo:
        _router_tier_profile_defaults("groq")
    assert str(excinfo.value) == UNKNOWN_PROFILE_ERROR


# --- fixture battery (upgrade audit) ----------------------------------------


@pytest.mark.parametrize("profile_id", sorted(LEGACY_NINE))
def test_minimal_toml_per_profile_loads_identical_tiers(
    tmp_path: Path, profile_id: str
) -> None:
    """(a) each legacy tier_profile in a minimal TOML -> today's effective tiers."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'[llm]\nprovider = "{profile_id}"\n\n'
        f'[squilla_router]\ntier_profile = "{profile_id}"\n',
        encoding="utf-8",
    )
    cfg = GatewayConfig.load_from_toml(path)
    assert cfg.squilla_router.tier_profile == profile_id
    assert cfg.squilla_router.tiers == _golden()[profile_id]


def test_mixed_tiers_without_profile_round_trip_untouched(tmp_path: Path) -> None:
    """(b) openrouter-mix shape: no tier_profile, explicit tiers stay verbatim."""
    tiers = {
        "c0": {
            "provider": "openrouter",
            "model": "vendor-a/fast-model",
            "description": "custom fast route",
            "supports_image": False,
            "thinking_level": "low",
        },
        "c1": {
            "provider": "openrouter",
            "model": "vendor-b/balanced-model",
            "description": "custom balanced route",
            "supports_image": False,
            "thinking_level": "medium",
        },
        "c2": {
            "provider": "openrouter",
            "model": "vendor-c/strong-model",
            "description": "custom strong route",
            "supports_image": False,
            "thinking_level": "high",
        },
        "c3": {
            "provider": "openrouter",
            "model": "vendor-c/strong-model",
            "description": "custom highest route",
            "supports_image": False,
            "thinking_level": "high",
        },
        "image_model": {
            "provider": "openrouter",
            "model": "vendor-d/vision-model",
            "description": "custom image route",
            "supports_image": True,
            "image_only": True,
            "thinking_level": "medium",
        },
    }
    path = tmp_path / "config.toml"
    path.write_text(
        tomli_w.dumps(
            {
                "llm": {"provider": "openrouter"},
                "squilla_router": {"enabled": True, "tiers": tiers},
            }
        ),
        encoding="utf-8",
    )
    cfg = GatewayConfig.load_from_toml(path)
    assert cfg.squilla_router.tier_profile is None
    assert cfg.squilla_router.tiers == tiers

    dump = cfg.to_toml_dict()
    router = dump["squilla_router"]
    assert "tier_profile" not in router
    assert router["tiers"] == tiers

    # full round-trip: dump -> TOML -> load -> identical tiers
    path2 = tmp_path / "roundtrip.toml"
    path2.write_text(tomli_w.dumps(dump), encoding="utf-8")
    cfg2 = GatewayConfig.load_from_toml(path2)
    assert cfg2.squilla_router.tier_profile is None
    assert cfg2.squilla_router.tiers == tiers


def _previous_openrouter_tiers() -> dict[str, dict[str, str]]:
    return {
        tier: {"provider": "openrouter", "model": model, "thinking_level": "high"}
        for tier, model in zip(
            ("c0", "c1", "c2", "c3"),
            (
                "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro",
                "z-ai/glm-5.2", "anthropic/claude-opus-4.8",
            ),
            strict=True,
        )
    }


@pytest.mark.parametrize("binding", ["follow_primary", "custom", None])
@pytest.mark.parametrize("provider", ["openrouter", "tokenrhythm"])
def test_v054_default_inline_upgrade_preserves_controls_and_sparse_saves(
    tmp_path: Path, binding: str | None, provider: str,
) -> None:
    if provider == "openrouter":
        old_tiers = _previous_openrouter_tiers()
    else:
        old_tiers = {
            name: {"provider": provider, "model": model}
            for name, model in zip(
                ("c0", "c1", "c2", "c3"), _PREVIOUS_MODELS[provider][-1], strict=True,
            )
        }
        old_tiers["c3"]["ensemble_enabled"] = True
    direct_model = old_tiers["c1"]["model"]
    router = {
        "enabled": True,
        "default_tier": "c2",
        "rollout_phase": "observe",
        "tiers": old_tiers,
        **({"preset_binding": binding} if binding else {}),
    }
    path = tmp_path / "config.toml"
    raw = tomli_w.dumps({
        "llm": {"provider": provider, "model": direct_model},
        "squilla_router": router,
    })
    path.write_text(raw, encoding="utf-8")
    cfg = load_config(path)
    preset = get_preset(provider)
    assert preset is not None
    expected_tiers = preset.tier_defaults()
    assert cfg.squilla_router.tiers == expected_tiers
    assert cfg.squilla_router.tier_profile is None
    assert cfg.squilla_router.model_fields_set == set(router)
    assert cfg.squilla_router.default_tier == "c2"
    assert cfg.squilla_router.rollout_phase == "observe"
    assert cfg.squilla_router.preset_binding == binding
    assert cfg.llm.provider == provider
    assert cfg.llm.model == direct_model
    assert path.read_text(encoding="utf-8") == raw
    if binding == "follow_primary" and provider == "openrouter":
        profile = next(
            item for item in router_catalog_payload()["profiles"]
            if item["providerId"] == "openrouter"
        )
        assert {k: v["model"] for k, v in cfg.squilla_router.tiers.items()} == {
            k: v["model"] for k, v in profile["tiers"].items()
        }

    # An unrelated settings write must not turn an in-memory upgrade into
    # an implicit config migration, or reintroduce stale tiers on reload.
    cfg.log_level = "DEBUG"
    persist_config(cfg, path=path, backup=False)
    saved = tomllib.loads(path.read_text(encoding="utf-8"))
    assert saved["squilla_router"] == router
    assert load_config(path).squilla_router.tiers == expected_tiers


def test_openrouter_managed_inline_refresh_survives_full_model_revalidation() -> None:
    cfg = GatewayConfig(
        llm={"provider": "openrouter"},
        squilla_router={"enabled": False, "preset_binding": "follow_primary"},
    )
    cfg.squilla_router.tiers = _previous_openrouter_tiers()
    cfg.squilla_router.enabled = True
    # Full snapshots explicitly include tier_profile=None. That must not
    # suppress the managed refresh during hot reload/config validation.
    restored = GatewayConfig.model_validate(cfg.model_dump(mode="python"))
    assert restored.squilla_router.tier_profile is None
    assert restored.squilla_router.tiers == _default_tiers()


def test_disabled_openrouter_managed_inline_refreshes_without_enabling() -> None:
    cfg = GatewayConfig(
        llm={"provider": "openrouter"},
        squilla_router={
            "enabled": False, "preset_binding": "follow_primary",
            "tiers": _previous_openrouter_tiers(),
        },
    )
    assert cfg.squilla_router.enabled is False
    assert cfg.squilla_router.tiers == _default_tiers()
    cfg.squilla_router.enabled = True
    cfg.initialize_router_profile_defaults()
    assert cfg.squilla_router.tiers == _default_tiers()


def test_openrouter_inline_upgrade_does_not_rebind_foreign_provider_tiers() -> None:
    tiers = _previous_openrouter_tiers()
    tiers["c1"] = {"provider": "tokenrhythm", "model": "deepseek-flash"}
    cfg = GatewayConfig(
        llm={"provider": "openrouter"},
        squilla_router={"preset_binding": "follow_primary", "tiers": tiers},
    )
    assert cfg.squilla_router.tiers == tiers


@pytest.mark.parametrize("cross_provider", [False, True])
def test_managed_dormant_foreign_draft_does_not_gain_unapproved_routes(
    cross_provider: bool,
) -> None:
    from opensquilla.onboarding.router_policy import (
        executable_router_dependencies,
        validate_router_candidate,
    )

    tiers = {"c3": {
        "provider": "tokenrhythm", "model": "custom-c3", "ensemble_enabled": True,
    }}
    cfg = GatewayConfig(
        llm={"provider": "openrouter"},
        squilla_router={
            "enabled": True, "preset_binding": "follow_primary",
            "cross_provider_tiers": cross_provider, "tiers": tiers,
        },
        llm_ensemble={"enabled": False, "selection_mode": "static_openrouter_b5"},
    )
    validate_router_candidate(cfg)
    if cross_provider:
        assert {name for name, _, _ in executable_router_dependencies(cfg)} == {
            "c0", "c1", "c2", "c3",
        }
    else:
        assert cfg.squilla_router.tiers == tiers
        assert executable_router_dependencies(cfg) == set()
    assert GatewayConfig.model_validate(cfg.model_dump()).squilla_router.tiers == (
        cfg.squilla_router.tiers
    )


# Independent snapshots of the shipped model sequences, including the two
# older TokenRhythm ladders and the immediate pre-refresh defaults.
_PREVIOUS_MODELS = {
    "openrouter": (
        (
            "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro",
            "z-ai/glm-5.2", "z-ai/glm-5.2",
        ),
        (
            "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro",
            "z-ai/glm-5.2", "anthropic/claude-opus-4.8",
        ),
    ),
    "tokenrhythm": (
        ("deepseek-v4-flash", "deepseek-v4-pro", "kimi-k2.7-code", "glm-5.1"),
        ("deepseek-v4-flash", "deepseek-v4-pro", "kimi-k2.7-code", "glm-5.2"),
        ("qwen3.7-flash", "deepseek-v4-flash-0731", "glm-5.2", "glm-5.2"),
        ("deepseek-v4-flash-0731", "deepseek-v4-pro-0813", "kimi-k2.7-code", "glm-5.2"),
    ),
}
_PREVIOUS_LADDERS = [
    pytest.param(provider, models, id=f"{provider}-{index}")
    for provider, ladders in _PREVIOUS_MODELS.items()
    for index, models in enumerate(ladders)
]


@pytest.mark.parametrize(("provider", "models"), _PREVIOUS_LADDERS)
@pytest.mark.parametrize("binding", [None, "custom", "follow_primary"])
@pytest.mark.parametrize("enabled", [False, True])
def test_shipped_old_ladders_upgrade_without_changing_provider_or_controls(
    provider: str, models: tuple[str, ...], binding: str | None, enabled: bool,
) -> None:
    primary = "tokenrhythm" if provider == "openrouter" else "openrouter"
    tiers = {
        name: {"provider": provider, "model": model}
        for name, model in zip(("c0", "c1", "c2", "c3"), models, strict=True)
    }
    tiers["c3"]["ensemble_enabled"] = True
    image = {"provider": "openai", "model": "custom-image-model", "supports_image": True}
    tiers["image_model"] = image
    cfg = GatewayConfig(
        llm={"provider": primary},
        squilla_router={
            "enabled": enabled,
            "preset_binding": binding,
            "tier_profile": None,
            "default_tier": "c2",
            "rollout_phase": "observe",
            "cross_provider_tiers": True,
            "tiers": tiers,
        },
    )
    preset = get_preset(provider)
    assert preset is not None
    for name in ("c0", "c1", "c2", "c3"):
        assert cfg.squilla_router.tiers[name]["provider"] == provider
        assert cfg.squilla_router.tiers[name]["model"] == preset.tiers[name]["model"]
    assert not cfg.squilla_router.tiers["c3"].get("ensemble_enabled")
    assert cfg.squilla_router.tiers["image_model"] == image
    assert cfg.squilla_router.enabled is enabled
    assert cfg.squilla_router.preset_binding == binding
    assert cfg.squilla_router.default_tier == "c2"
    assert cfg.squilla_router.rollout_phase == "observe"
    assert cfg.squilla_router.cross_provider_tiers is True
    assert cfg.llm.provider == primary
    restored = GatewayConfig.model_validate(cfg.model_dump(mode="python"))
    assert restored.squilla_router == cfg.squilla_router


@pytest.mark.parametrize("provider", ["openrouter", "tokenrhythm"])
@pytest.mark.parametrize("binding", [None, "custom"])
def test_old_ladder_with_changed_model_is_preserved(provider: str, binding: str | None) -> None:
    models = _PREVIOUS_MODELS[provider][0]
    tiers = {
        name: {"provider": provider, "model": model}
        for name, model in zip(("c0", "c1", "c2", "c3"), models, strict=True)
    }
    tiers["c1"]["model"] = "operator-custom-model"
    cfg = GatewayConfig(
        llm={"provider": provider},
        squilla_router={"preset_binding": binding, "tiers": tiers},
    )
    assert cfg.squilla_router.tiers == tiers


def test_old_ladder_upgrade_preserves_explicit_reasoning_and_other_tier_options() -> None:
    tiers = _previous_openrouter_tiers()
    tiers["c1"].update({"thinking_level": "low", "temperature": 0.42})
    cfg = GatewayConfig(
        llm={"provider": "openrouter"},
        squilla_router={"preset_binding": "custom", "tiers": tiers},
    )
    assert cfg.squilla_router.tiers["c1"]["model"] == "deepseek/deepseek-v4-flash-0731"
    assert cfg.squilla_router.tiers["c1"]["thinking_level"] == "low"
    assert cfg.squilla_router.tiers["c1"]["temperature"] == 0.42


@pytest.mark.parametrize("provider", ["openrouter", "tokenrhythm"])
def test_current_custom_ladder_keeps_reasoning_and_fusion_changes(provider: str) -> None:
    preset = get_preset(provider)
    assert preset is not None
    tiers = preset.tier_defaults()
    tiers["c1"]["thinking_level"] = "low"
    tiers["c3"]["ensemble_enabled"] = True
    cfg = GatewayConfig(
        llm={"provider": provider},
        squilla_router={"preset_binding": "custom", "tiers": tiers},
    )
    assert cfg.squilla_router.tiers == tiers
    assert cfg.squilla_router.preset_binding == "custom"


@pytest.mark.parametrize("provider", [
    "openrouter", "tokenrhythm", "qwen_token_plan", "dashscope", "openai", "anthropic", "groq",
])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("explicit_null_profile", [False, True])
def test_sparse_managed_ladder_uses_primary_provider(
    provider: str, enabled: bool, explicit_null_profile: bool,
) -> None:
    cfg = GatewayConfig(
        llm={"provider": provider, "model": "configured-direct-model"},
        squilla_router={
            "enabled": enabled,
            "preset_binding": "follow_primary",
            **({"tier_profile": None} if explicit_null_profile else {}),
        },
    )
    preset = get_preset(provider)
    assert preset is not None
    expected = preset.tier_defaults()
    for tier in expected.values():
        if not tier.get("model"):
            tier["model"] = "configured-direct-model"
    assert cfg.squilla_router.tiers == expected
    assert cfg.squilla_router.enabled is enabled
    assert cfg.squilla_router.preset_binding == "follow_primary"
    if preset.synthesized:
        assert cfg.squilla_router.tier_profile is None
    assert GatewayConfig.model_validate(cfg.model_dump()).squilla_router.tiers == (
        cfg.squilla_router.tiers
    )


@pytest.mark.parametrize("provider", ["anthropic", "groq"])
@pytest.mark.parametrize("binding", [None, "custom"])
def test_unmanaged_synthesized_provider_keeps_legacy_boot_behavior(
    provider: str, binding: str | None,
) -> None:
    cfg = GatewayConfig(
        llm={"provider": provider},
        squilla_router={"preset_binding": binding},
    )
    assert cfg.squilla_router.tiers == _default_tiers()
    assert cfg.squilla_router.preset_binding == binding


def test_router_ladder_provider_normalizes_aliases_and_ignores_image() -> None:
    assert router_ladder_provider(
        {
            "t0": {"provider": "TokenRhythm"},
            "c0": {"provider": "tokenrhythm"},
            "t1": {"provider": "tokenrhythm"},
            "image_model": {"provider": "openai"},
        },
        "openrouter",
    ) == "tokenrhythm"
    assert router_ladder_provider({}, "OpenRouter") == "openrouter"
    assert router_ladder_provider({"c0": {"model": "synthetic-model"}}, "openrouter") == (
        "openrouter"
    )
    assert router_ladder_provider(
        {"c0": {"provider": "tokenrhythm"}, "c1": {"provider": "openrouter"}},
        "openrouter",
    ) is None


def test_rc1_desktop_legacy_tier_keys_merge_to_single_canonical(tmp_path: Path) -> None:
    """(c) rc1-desktop shape: t0-t3 keys + default_tier="t1" + tier_profile.

    Legacy keys must normalize BEFORE the profile merge, producing a single
    canonical c0-c3 key set with the overrides applied inside the matching
    canonical tier (normalize-before-merge invariant).
    """
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            [
                "[llm]",
                'provider = "deepseek"',
                "",
                "[squilla_router]",
                'tier_profile = "deepseek"',
                'default_tier = "t1"',
                "",
                "[squilla_router.tiers.t0]",
                'model = "custom-fast-model"',
                "",
                "[squilla_router.tiers.t1]",
                'model = "custom-balanced-model"',
                "",
                "[squilla_router.tiers.t2]",
                'model = "custom-strong-model"',
                'thinking_level = "high"',
                "",
                "[squilla_router.tiers.t3]",
                'model = "custom-highest-model"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    cfg = GatewayConfig.load_from_toml(path)
    router = cfg.squilla_router
    golden = _golden()["deepseek"]

    assert router.default_tier == "c1"
    assert sorted(router.tiers) == ["c0", "c1", "c2", "c3"]  # single-key merge
    assert router.tiers["c0"] == {**golden["c0"], "model": "custom-fast-model"}
    assert router.tiers["c1"] == {**golden["c1"], "model": "custom-balanced-model"}
    assert router.tiers["c2"] == {
        **golden["c2"],
        "model": "custom-strong-model",
        "thinking_level": "high",
    }
    assert router.tiers["c3"] == {**golden["c3"], "model": "custom-highest-model"}


def test_unknown_tier_profile_rejected_with_same_error_shape(tmp_path: Path) -> None:
    """(d) unknown tier_profile "groq" -> same rejection message as today."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nprovider = "groq"\n\n[squilla_router]\ntier_profile = "groq"\n',
        encoding="utf-8",
    )
    with pytest.raises(Exception) as excinfo:
        GatewayConfig.load_from_toml(path)
    assert UNKNOWN_PROFILE_ERROR in str(excinfo.value)


def test_full_default_tree_round_trips_via_to_toml_dict(tmp_path: Path) -> None:
    """(e) full-default-tree (rc1 RPC-persisted shape) loads and round-trips."""
    dump1 = GatewayConfig().to_toml_dict()
    path = tmp_path / "config.toml"
    path.write_text(tomli_w.dumps(dump1), encoding="utf-8")

    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg = GatewayConfig(**payload)
    dump2 = cfg.to_toml_dict()

    assert dump2 == dump1
    # The default tree carries the curated tokenrhythm ladder (the built-in
    # default provider ships packaged tier data but no persistable
    # tier_profile, so its ladder is applied inline).
    tiers = cfg.squilla_router.tiers
    assert set(tiers) == {"c0", "c1", "c2", "c3", "image_model"}
    expected_models = {
        "c0": "qwen3.7-flash",
        "c1": "deepseek-flash",
        "c2": "deepseek-v4-pro-0813",
        "c3": "glm-5.3",
        "image_model": "kimi-k2.6",
    }
    for name, tier in tiers.items():
        assert tier["provider"] == "tokenrhythm"
        assert tier["model"] == expected_models[name]
    assert tiers["c3"]["ensemble_enabled"] is False
    assert "ensemble_selection_mode" not in tiers["c3"]


# --- H4: downgrade chokepoint at to_toml_dict --------------------------------


def test_to_toml_dict_expands_tiers_and_omits_non_legacy_tier_profile() -> None:
    """Non-legacy tier_profile never reaches disk: tiers expand, id is dropped.

    Unreachable through validation today (non-legacy ids are rejected), so the
    guard is exercised by corrupting the profile post-validation — exactly the
    shape a future registry-wide consumer bug would produce.
    """
    cfg = GatewayConfig(
        llm={"provider": "deepseek"},
        squilla_router={"tier_profile": "deepseek"},
    )
    effective_tiers = {name: dict(t) for name, t in cfg.squilla_router.tiers.items()}
    object.__setattr__(cfg.squilla_router, "tier_profile", "groq")

    router = cfg.to_toml_dict()["squilla_router"]
    assert "tier_profile" not in router
    assert router["tiers"] == effective_tiers

    # the guarded dump must load on a legacy-nine-only validator (rc1 shape)
    reloaded = GatewayConfig(llm={"provider": "deepseek"}, squilla_router=router)
    assert reloaded.squilla_router.tier_profile is None
    assert reloaded.squilla_router.tiers == effective_tiers


def test_to_toml_dict_still_collapses_legacy_profile_default_tiers() -> None:
    """Control: a legacy profile with default tiers keeps today's compact dump."""
    cfg = GatewayConfig(
        llm={"provider": "deepseek"},
        squilla_router={"tier_profile": "deepseek"},
    )
    router = cfg.to_toml_dict()["squilla_router"]
    assert router["tier_profile"] == "deepseek"
    assert "tiers" not in router


def test_to_toml_dict_keeps_overridden_tiers_for_legacy_profile() -> None:
    """Control: profile + non-default tier override keeps both keys in the dump."""
    cfg = GatewayConfig(
        llm={"provider": "deepseek"},
        squilla_router={
            "tier_profile": "deepseek",
            "tiers": {"c2": {"model": "custom-strong-model"}},
        },
    )
    router = cfg.to_toml_dict()["squilla_router"]
    assert router["tier_profile"] == "deepseek"
    assert router["tiers"]["c2"]["model"] == "custom-strong-model"
