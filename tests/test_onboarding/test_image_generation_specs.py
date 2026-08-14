"""Tests for onboarding image generation provider catalog."""

from __future__ import annotations

from opensquilla.onboarding.image_generation_specs import (
    image_generation_provider_catalog_payload,
)


def test_image_generation_payload_exposes_optional_capability_metadata():
    payload = image_generation_provider_catalog_payload()

    assert {row["providerId"] for row in payload} == {
        "openai",
        "openrouter",
        "tokenrhythm",
        "qwen_token_plan",
    }
    for row in payload:
        assert row["blocking"] is False
        assert row["canProbe"] is False
        assert row["deployment"] == "cloud"
        assert row["whatYouNeed"]
        assert row["readmeScenarios"]

    qwen = next(row for row in payload if row["providerId"] == "qwen_token_plan")
    assert qwen["envKey"] == "QWEN_TOKEN_PLAN_API_KEY"
    assert qwen["defaultModel"] == "qwen_token_plan/wan2.7-image"
    assert qwen["suggestedModels"] == [
        "qwen_token_plan/wan2.7-image",
        "qwen_token_plan/wan2.7-image-pro",
    ]

    tokenrhythm = next(row for row in payload if row["providerId"] == "tokenrhythm")
    assert tokenrhythm["envKey"] == "TOKENRHYTHM_API_KEY"
    assert tokenrhythm["defaultBaseUrl"] == "https://tokenrhythm.studio/v1"
    assert tokenrhythm["defaultModel"] == "tokenrhythm/qwen-image-2.0"
    assert tokenrhythm["suggestedModels"] == [
        "tokenrhythm/qwen-image-2.0",
        "tokenrhythm/wan2.7-image",
    ]
