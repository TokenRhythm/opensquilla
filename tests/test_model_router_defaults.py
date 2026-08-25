import sys
import tomllib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
import yaml

from opensquilla.gateway.config import ROUTER_TIER_PROFILE_IDS

REPO_ROOT = Path(__file__).resolve().parents[1]
DIRECT_ROUTER_PROFILE_IDS = sorted(ROUTER_TIER_PROFILE_IDS - {"openrouter"})


def _squilla_router_config_cls():
    config_path = REPO_ROOT / "src" / "opensquilla" / "gateway" / "config.py"
    spec = spec_from_file_location("opensquilla_gateway_config_under_test", config_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    # Register the module for the duration of exec (the documented importlib
    # recipe): pydantic resolves stringified annotations such as
    # Literal["route", "veto"] through sys.modules[cls.__module__] at class
    # build time, so an unregistered module leaves the models half-defined.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module.SquillaRouterConfig


def _gateway_config_cls():
    from opensquilla.gateway.config import GatewayConfig

    return GatewayConfig


def test_squilla_router_defaults_match_runtime_router_config() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()
    cfg = squilla_router_config_cls()

    assert cfg.enabled is True
    assert cfg.auto_thinking is True
    assert cfg.rollout_phase == "full"
    assert cfg.strategy == "v4_phase3"
    assert cfg.default_tier == "c1"
    assert cfg.confidence_threshold == 0.5
    assert cfg.confidence_high_tier_margin == 0.05
    assert cfg.v4_use_aux_head is True
    assert cfg.kv_cache_anti_downgrade_enabled is True
    assert cfg.kv_cache_anti_downgrade_window_seconds == 600
    assert cfg.complaint_upgrade_enabled is True
    assert cfg.complaint_upgrade_steps == 1
    assert cfg.complaint_upgrade_max_chars == 160
    assert cfg.require_router_runtime is True
    assert cfg.vision_followup_gate_tier == "c0"

    assert cfg.tiers["c0"]["model"] == "deepseek/deepseek-v4-flash"
    assert cfg.tiers["c0"]["thinking_level"] == "high"
    assert cfg.tiers["c1"]["model"] == "deepseek/deepseek-v4-pro"
    assert cfg.tiers["c1"]["thinking_level"] == "high"
    assert cfg.tiers["c2"]["model"] == "z-ai/glm-5.2"
    assert cfg.tiers["c2"]["thinking_level"] == "high"
    assert cfg.tiers["c3"]["model"] == "anthropic/claude-opus-4.8"
    assert cfg.tiers["c3"]["thinking_level"] == "high"
    assert cfg.tiers["image_model"]["model"] == "moonshotai/kimi-k2.6"
    assert cfg.tiers["image_model"]["supports_image"] is True
    assert cfg.tiers["image_model"]["image_only"] is True


def test_squilla_router_rejects_unknown_tier_ensemble_selection_mode() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()
    tiers = squilla_router_config_cls().tiers
    tiers["c3"] = {
        **tiers["c3"],
        "ensemble_selection_mode": "static_tokenrhythm_typo",
    }

    with pytest.raises(
        ValueError,
        match=r"squilla_router\.tiers\.c3\.ensemble_selection_mode must be one of",
    ):
        squilla_router_config_cls(tiers=tiers)


def test_squilla_router_rejects_non_boolean_tier_ensemble_enabled() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()
    tiers = squilla_router_config_cls().tiers
    tiers["c3"] = {**tiers["c3"], "ensemble_enabled": "true"}

    with pytest.raises(
        ValueError,
        match=r"squilla_router\.tiers\.c3\.ensemble_enabled must be a boolean",
    ):
        squilla_router_config_cls(tiers=tiers)


def test_squilla_router_explicit_openrouter_profile_matches_default_tiers() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    default_cfg = squilla_router_config_cls()
    explicit_cfg = squilla_router_config_cls(tier_profile="openrouter")

    assert explicit_cfg.tiers == default_cfg.tiers
    assert explicit_cfg.tier_profile == "openrouter"


def test_squilla_router_canonical_tier_wins_over_legacy_alias() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    cfg = squilla_router_config_cls(
        tiers={
            "c1": {"provider": "openrouter", "model": "canonical-model"},
            "t1": {"provider": "openrouter", "model": "legacy-model"},
        },
        default_tier="t1",
    )

    assert cfg.default_tier == "c1"
    assert cfg.tiers["c1"]["model"] == "canonical-model"


def test_provider_profile_requires_matching_llm_provider() -> None:
    gateway_config_cls = _gateway_config_cls()

    try:
        gateway_config_cls(
            llm={"provider": "openrouter"},
            squilla_router={"tier_profile": "dashscope"},
        )
    except ValueError as exc:
        assert "squilla_router.tier_profile requires llm.provider" in str(exc)
    else:
        raise AssertionError("expected provider/profile mismatch to fail")


def test_explicit_openrouter_profile_requires_openrouter_provider() -> None:
    gateway_config_cls = _gateway_config_cls()

    try:
        gateway_config_cls(
            llm={"provider": "deepseek"},
            squilla_router={"tier_profile": "openrouter"},
        )
    except ValueError as exc:
        assert "squilla_router.tier_profile requires llm.provider" in str(exc)
    else:
        raise AssertionError("expected explicit openrouter profile mismatch to fail")


def test_provider_profile_accepts_matching_llm_provider() -> None:
    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(
        llm={"provider": "dashscope"},
        squilla_router={"tier_profile": "dashscope"},
    )

    assert cfg.llm.provider == "dashscope"
    assert cfg.squilla_router.tier_profile == "dashscope"
    assert cfg.squilla_router.tiers["c0"]["provider"] == "dashscope"
    assert cfg.squilla_router.tiers["c0"]["model"] == "qwen3.6-flash"


@pytest.mark.parametrize("provider_id", DIRECT_ROUTER_PROFILE_IDS)
def test_unset_tier_profile_uses_matching_direct_provider_profile(provider_id: str) -> None:
    from opensquilla.gateway.config import _router_tier_profile_defaults

    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(llm={"provider": provider_id})

    expected = _router_tier_profile_defaults(provider_id)
    assert cfg.squilla_router.tier_profile == provider_id
    for tier in ("c0", "c1", "c2", "c3"):
        assert cfg.squilla_router.tiers[tier]["provider"] == provider_id
        assert cfg.squilla_router.tiers[tier]["model"] == expected[tier]["model"]


@pytest.mark.parametrize("provider_id", DIRECT_ROUTER_PROFILE_IDS)
def test_direct_legacy_openrouter_router_defaults_are_migrated(provider_id: str) -> None:
    from opensquilla.gateway.config import _router_tier_profile_defaults

    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(
        llm={"provider": provider_id},
        squilla_router={"enabled": True, "tiers": _router_tier_profile_defaults("openrouter")},
    )

    expected = _router_tier_profile_defaults(provider_id)
    assert cfg.squilla_router.tier_profile == provider_id
    for tier in ("c0", "c1", "c2", "c3"):
        assert cfg.squilla_router.tiers[tier]["provider"] == provider_id
        assert cfg.squilla_router.tiers[tier]["model"] == expected[tier]["model"]


TOKENRHYTHM_EXPECTED_TIER_MODELS = {
    "c0": "deepseek-v4-flash-0731",
    "c1": "deepseek-v4-pro-0813",
    "c2": "kimi-k2.7-code",
    "c3": "glm-5.2",
}


def test_unset_tier_profile_seeds_tokenrhythm_curated_inline_tiers() -> None:
    # tokenrhythm has a curated ladder but must never persist as a
    # tier_profile id (downgrade contract): boot seeds it as inline tiers.
    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(llm={"provider": "tokenrhythm", "model": "deepseek-v4-pro"})

    assert cfg.squilla_router.tier_profile is None
    for tier, model in TOKENRHYTHM_EXPECTED_TIER_MODELS.items():
        assert cfg.squilla_router.tiers[tier]["provider"] == "tokenrhythm"
        assert cfg.squilla_router.tiers[tier]["model"] == model
    assert cfg.squilla_router.tiers["c0"]["supports_image"] is False
    assert cfg.squilla_router.tiers["c3"]["ensemble_enabled"] is True
    assert "ensemble_selection_mode" not in cfg.squilla_router.tiers["c3"]
    assert cfg.squilla_router.tiers["image_model"]["model"] == "kimi-k2.6"


def test_tokenrhythm_follow_primary_binding_refreshes_managed_inline_tiers() -> None:
    gateway_config_cls = _gateway_config_cls()
    legacy_tiers = {
        "c0": {"provider": "tokenrhythm", "model": "deepseek-v4-flash"},
        "c1": {"provider": "tokenrhythm", "model": "deepseek-v4-pro"},
        "c2": {"provider": "tokenrhythm", "model": "kimi-k2.7-code"},
        "c3": {"provider": "tokenrhythm", "model": "glm-5.2"},
    }

    cfg = gateway_config_cls(
        llm={"provider": "tokenrhythm", "model": "deepseek-v4-pro"},
        squilla_router={
            "enabled": True,
            "preset_binding": "follow_primary",
            "tiers": legacy_tiers,
        },
    )

    for tier, model in TOKENRHYTHM_EXPECTED_TIER_MODELS.items():
        assert cfg.squilla_router.tiers[tier]["model"] == model
        assert "thinking_level" not in cfg.squilla_router.tiers[tier]
    assert cfg.squilla_router.tiers["c3"]["ensemble_enabled"] is True
    assert "ensemble_selection_mode" not in cfg.squilla_router.tiers["c3"]


def test_tokenrhythm_direct_legacy_openrouter_router_defaults_are_migrated() -> None:
    from opensquilla.gateway.config import _router_tier_profile_defaults

    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(
        llm={"provider": "tokenrhythm", "model": "deepseek-v4-pro"},
        squilla_router={"enabled": True, "tiers": _router_tier_profile_defaults("openrouter")},
    )

    assert cfg.squilla_router.tier_profile is None
    for tier, model in TOKENRHYTHM_EXPECTED_TIER_MODELS.items():
        assert cfg.squilla_router.tiers[tier]["provider"] == "tokenrhythm"
        assert cfg.squilla_router.tiers[tier]["model"] == model


def test_tokenrhythm_boot_seed_respects_custom_inline_tiers() -> None:
    gateway_config_cls = _gateway_config_cls()

    custom = {
        tier: {"provider": "tokenrhythm", "model": "glm-5", "description": "custom"}
        for tier in ("c0", "c1", "c2", "c3")
    }
    cfg = gateway_config_cls(
        llm={"provider": "tokenrhythm", "model": "deepseek-v4-pro"},
        squilla_router={"enabled": True, "tiers": custom},
    )

    assert cfg.squilla_router.tier_profile is None
    assert cfg.squilla_router.preset_binding is None
    for tier in ("c0", "c1", "c2", "c3"):
        assert cfg.squilla_router.tiers[tier]["model"] == "glm-5"


def test_deepseek_direct_legacy_openrouter_model_default_is_normalized() -> None:
    gateway_config_cls = _gateway_config_cls()

    cfg = gateway_config_cls(
        llm={
            "provider": "deepseek",
            "model": "deepseek/deepseek-v4-pro",
        }
    )

    assert cfg.llm.model == "deepseek-v4-pro"


def test_each_provider_profile_has_four_text_tiers_without_default_image_model() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    for profile in ("dashscope", "deepseek", "gemini", "volcengine", "byteplus"):
        cfg = squilla_router_config_cls(tier_profile=profile)
        assert {"c0", "c1", "c2", "c3"}.issubset(cfg.tiers)
        assert "image_model" not in cfg.tiers
        assert {cfg.tiers[tier]["provider"] for tier in ("c0", "c1", "c2", "c3")} == {
            profile
        }


def test_direct_provider_profiles_have_four_text_tiers_without_default_image_model() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    for profile in ("openai", "zhipu", "moonshot"):
        cfg = squilla_router_config_cls(tier_profile=profile)
        assert {"c0", "c1", "c2", "c3"}.issubset(cfg.tiers)
        assert "image_model" not in cfg.tiers
        assert {cfg.tiers[tier]["provider"] for tier in ("c0", "c1", "c2", "c3")} == {
            profile
        }


def test_openai_profile_uses_streaming_compatible_models() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    cfg = squilla_router_config_cls(tier_profile="openai")

    assert cfg.tiers["c0"]["model"] == "gpt-5.4-nano"
    assert cfg.tiers["c1"]["model"] == "gpt-5.4-mini"
    assert cfg.tiers["c2"]["model"] == "gpt-5.5"
    assert cfg.tiers["c3"]["model"] == "gpt-5.5"
    assert cfg.tiers["c3"]["thinking_level"] == "high"
    assert all(
        cfg.tiers[tier]["model"] not in {"gpt-5", "gpt-5-mini", "gpt-5.5-pro"}
        for tier in ("c0", "c1", "c2", "c3")
    )


def test_zhipu_profile_uses_current_glm_5_ladder() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    cfg = squilla_router_config_cls(tier_profile="zhipu")

    assert cfg.tiers["c0"]["model"] == "glm-5-turbo"
    assert cfg.tiers["c1"]["model"] == "glm-5"
    assert cfg.tiers["c2"]["model"] == "glm-5.1"
    assert cfg.tiers["c3"]["model"] == "glm-5.2"
    assert cfg.tiers["c3"]["thinking_level"] == "high"


def test_moonshot_profile_uses_kimi_for_strong_tiers() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    cfg = squilla_router_config_cls(tier_profile="moonshot")

    assert cfg.tiers["c0"]["model"] == "kimi-k2.6"
    assert cfg.tiers["c1"]["model"] == "kimi-k2.6"
    assert cfg.tiers["c2"]["model"] == "kimi-k2.6"
    assert cfg.tiers["c3"]["model"] == "kimi-k2.7-code"
    assert all(
        cfg.tiers[tier]["supports_image"] is True for tier in ("c0", "c1", "c2", "c3")
    )


def test_volcengine_profile_uses_seed_2_capability_ladder() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    cfg = squilla_router_config_cls(tier_profile="volcengine")

    assert cfg.tiers["c0"]["model"] == "doubao-seed-2-0-lite-260215"
    assert cfg.tiers["c0"]["thinking_level"] == "off"
    assert cfg.tiers["c1"]["model"] == "doubao-seed-2-0-lite-260215"
    assert cfg.tiers["c1"]["thinking_level"] == "low"
    assert cfg.tiers["c2"]["model"] == "doubao-seed-2-0-pro-260215"
    assert cfg.tiers["c2"]["thinking_level"] == "medium"
    assert cfg.tiers["c3"]["model"] == "doubao-seed-2-0-pro-260215"
    assert cfg.tiers["c3"]["thinking_level"] == "high"


def test_byteplus_profile_uses_seed_2_modelark_ladder() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    cfg = squilla_router_config_cls(tier_profile="byteplus")

    assert cfg.tiers["c0"]["model"] == "seed-2-0-lite-260228"
    assert cfg.tiers["c0"]["thinking_level"] == "off"
    assert cfg.tiers["c1"]["model"] == "seed-2-0-lite-260228"
    assert cfg.tiers["c1"]["thinking_level"] == "low"
    assert cfg.tiers["c2"]["model"] == "seed-2-0-lite-260228"
    assert cfg.tiers["c2"]["thinking_level"] == "medium"
    assert cfg.tiers["c3"]["model"] == "seed-2-0-lite-260228"
    assert cfg.tiers["c3"]["thinking_level"] == "high"


def test_profile_tier_override_merges_keys_inside_tier() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    cfg = squilla_router_config_cls(
        tier_profile="gemini",
        tiers={"c2": {"thinking_level": "high"}},
    )

    assert cfg.tiers["c2"]["provider"] == "gemini"
    assert cfg.tiers["c2"]["model"] == "gemini-3.1-pro-preview"
    assert cfg.tiers["c2"]["thinking_level"] == "high"


def test_profile_rejects_non_dict_tier_override() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    with pytest.raises((ValueError, TypeError)) as excinfo:
        squilla_router_config_cls(
            tier_profile="gemini",
            tiers=[],
        )

    assert "tiers" in str(excinfo.value)


def test_profile_preserves_explicit_provider_compatible_image_model() -> None:
    squilla_router_config_cls = _squilla_router_config_cls()

    cfg = squilla_router_config_cls(
        tier_profile="gemini",
        tiers={
            "image_model": {
                "provider": "gemini",
                "model": "gemini-2.5-flash",
                "supports_image": True,
                "image_only": True,
            }
        },
    )

    assert cfg.tiers["image_model"]["provider"] == "gemini"
    assert cfg.tiers["image_model"]["supports_image"] is True
    assert cfg.tiers["c0"]["provider"] == "gemini"


def test_example_toml_enables_runtime_router_defaults() -> None:
    example = REPO_ROOT / "opensquilla.toml.example"

    data = tomllib.loads(example.read_text(encoding="utf-8"))
    squilla_router = data["squilla_router"]

    assert data["llm"]["provider"] == "tokenrhythm"
    assert data["llm"]["model"] == "deepseek-v4-pro-0813"
    assert data["llm_ensemble"]["enabled"] is False
    assert data["llm_ensemble"]["selection_mode"] == "static_tokenrhythm_b5"
    assert squilla_router["enabled"] is True
    assert squilla_router["auto_thinking"] is True
    assert squilla_router["rollout_phase"] == "full"
    assert squilla_router["strategy"] == "v4_phase3"
    assert "cache_ttl_seconds" not in squilla_router
    assert squilla_router["default_tier"] == "c1"
    assert squilla_router["confidence_threshold"] == 0.5
    assert squilla_router["v4_use_aux_head"] is True
    assert squilla_router["kv_cache_anti_downgrade_enabled"] is True
    assert squilla_router["kv_cache_anti_downgrade_window_seconds"] == 600
    assert squilla_router["complaint_upgrade_enabled"] is True
    assert squilla_router["complaint_upgrade_steps"] == 1
    assert squilla_router["complaint_upgrade_max_chars"] == 160
    assert squilla_router["require_router_runtime"] is True

    tiers = squilla_router["tiers"]
    # The packaged mixed-family ladder has no tier-wide thinking settings;
    # Router auto-thinking or explicit turn controls may still select one.
    for name in ("c0", "c1", "c2", "c3", "image_model"):
        assert tiers[name]["provider"] == "tokenrhythm"
        assert "thinking_level" not in tiers[name]
    assert tiers["c0"]["model"] == "deepseek-v4-flash-0731"
    assert tiers["c0"]["supports_image"] is False
    assert tiers["c1"]["model"] == "deepseek-v4-pro-0813"
    assert tiers["c2"]["model"] == "kimi-k2.7-code"
    assert tiers["c2"]["supports_image"] is False
    assert tiers["c3"]["model"] == "glm-5.2"
    assert tiers["c3"]["ensemble_enabled"] is True
    assert "ensemble_selection_mode" not in tiers["c3"]
    assert tiers["image_model"]["model"] == "kimi-k2.6"
    assert tiers["image_model"]["supports_image"] is True
    assert tiers["image_model"]["image_only"] is True


def test_runtime_router_config_does_not_ship_unused_cost_fields() -> None:
    runtime_config = (
        REPO_ROOT
        / "src"
        / "opensquilla"
        / "squilla_router"
        / "models"
        / "v4.2_phase3_inference"
        / "router.runtime.yaml"
    )

    text = runtime_config.read_text(encoding="utf-8")
    data = yaml.safe_load(text)

    assert data["tier_registry"]["S"] == ["deepseek/deepseek-v4-flash"]
    assert data["tier_registry"]["M"] == ["deepseek/deepseek-v4-pro"]
    assert data["tier_registry"]["L"] == ["z-ai/glm-5.2"]
    assert data["tier_registry"]["XL"] == ["anthropic/claude-opus-4.8"]
    assert data["tier_explanations"]["L"]["model"] == "z-ai/glm-5.2"
    assert data["tier_explanations"]["XL"]["model"] == "anthropic/claude-opus-4.8"
    assert "cost_ratios:" not in text
    assert "cost_matrix:" not in text
    assert "under_routing_multiplier" not in text
    assert "over_routing_multiplier" not in text
