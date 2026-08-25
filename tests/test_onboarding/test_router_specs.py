"""Tests for router onboarding catalog."""

from opensquilla.onboarding.router_specs import (
    get_router_setup_profile,
    router_catalog_payload,
)


def test_router_catalog_exposes_supported_profiles_and_tiers():
    payload = router_catalog_payload()

    profiles = {p["profileId"]: p for p in payload["profiles"]}
    assert {"openrouter", "deepseek", "openai", "byteplus"} <= set(profiles)
    deepseek = profiles["deepseek"]
    assert deepseek["providerId"] == "deepseek"
    assert set(deepseek["tiers"]) == {"c0", "c1", "c2", "c3"}
    assert deepseek["tiers"]["c0"]["model"]
    assert deepseek["tiers"]["c0"]["provider"] == "deepseek"
    assert "description" in deepseek["tiers"]["c0"]
    assert "thinkingLevel" in deepseek["tiers"]["c0"]
    openrouter = profiles["openrouter"]
    assert {
        tier: openrouter["tiers"][tier]["model"]
        for tier in ("c0", "c1", "c2", "c3")
    } == {
        "c0": "qwen/qwen3.7-flash",
        "c1": "deepseek/deepseek-v4-flash",
        "c2": "deepseek/deepseek-v4-pro",
        "c3": "z-ai/glm-5.3",
    }
    assert "image_model" in openrouter["tiers"]
    assert openrouter["tiers"]["image_model"]["supportsImage"] is True
    byteplus = profiles["byteplus"]
    assert byteplus["providerId"] == "byteplus"
    assert byteplus["tiers"]["c1"]["model"] == "seed-2-0-lite-260228"
    assert byteplus["tiers"]["c1"]["provider"] == "byteplus"
    assert payload["defaultTier"] == "c1"
    assert set(payload["textTiers"]) == {"c0", "c1", "c2", "c3"}


def test_get_router_setup_profile_rejects_unknown_profile():
    try:
        get_router_setup_profile("does-not-exist")
    except KeyError as exc:
        assert "unknown router profile" in str(exc)
    else:
        raise AssertionError("expected unknown router profile to fail")
