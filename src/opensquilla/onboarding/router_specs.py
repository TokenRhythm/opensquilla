"""Onboarding catalog for squilla-router tier profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opensquilla.gateway.config import (
    ROUTER_TIER_PROFILE_IDS,
    _router_tier_profile_defaults,
)
from opensquilla.router_tiers import DEFAULT_TEXT_TIER, TEXT_TIERS


@dataclass(frozen=True)
class RouterSetupProfile:
    profile_id: str
    provider_id: str
    label: str
    tiers: dict[str, dict[str, Any]]


_PROFILE_LABELS: dict[str, str] = {
    "openrouter": "OpenRouter mixed defaults",
    "dashscope": "Aliyun DashScope",
    "deepseek": "DeepSeek",
    "gemini": "Google Gemini",
    "volcengine": "Volcengine Ark",
    "byteplus": "BytePlus ModelArk",
    "openai": "OpenAI",
    "zhipu": "Zhipu",
    "moonshot": "Moonshot AI",
}


def _profile_to_setup(profile_id: str) -> RouterSetupProfile:
    normalized = profile_id.strip().lower()
    if normalized not in ROUTER_TIER_PROFILE_IDS:
        raise KeyError(f"unknown router profile: {profile_id!r}")
    raw_tiers = _router_tier_profile_defaults(normalized)
    exposed_tiers = {
        name: dict(value)
        for name, value in raw_tiers.items()
        if name in set(TEXT_TIERS) | {"image_model"}
    }
    return RouterSetupProfile(
        profile_id=normalized,
        provider_id=normalized,
        label=_PROFILE_LABELS.get(normalized, normalized),
        tiers=exposed_tiers,
    )


def list_router_setup_profiles() -> list[RouterSetupProfile]:
    return [_profile_to_setup(pid) for pid in sorted(ROUTER_TIER_PROFILE_IDS)]


def get_router_setup_profile(profile_id: str) -> RouterSetupProfile:
    return _profile_to_setup(profile_id)


def _tier_payload(tier: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": tier.get("provider", ""),
        "model": tier.get("model", ""),
        "description": tier.get("description", ""),
        "thinkingLevel": tier.get("thinking_level", ""),
        "supportsImage": bool(tier.get("supports_image", False)),
    }


def router_catalog_payload() -> dict[str, Any]:
    return {
        "defaultTier": DEFAULT_TEXT_TIER,
        "textTiers": list(TEXT_TIERS),
        "modes": [
            {
                "mode": "recommended",
                "label": "Recommended provider profile",
                "description": "Use the selected provider's default c0-c3 routing profile.",
            },
            {
                "mode": "openrouter-mix",
                "label": "OpenRouter mixed defaults",
                "description": "Keep the built-in OpenRouter mixed model routes.",
            },
            {
                "mode": "custom",
                "label": "Custom provider mix",
                "description": (
                    "Route c0-c3 on the active provider using its registry "
                    "preset tiers, with per-tier overrides."
                ),
            },
            {
                "mode": "disabled",
                "label": "Disable router",
                "description": "Use the configured provider/model directly.",
            },
        ],
        "profiles": [
            {
                "profileId": profile.profile_id,
                "providerId": profile.provider_id,
                "label": profile.label,
                "tiers": {
                    name: _tier_payload(tier)
                    for name, tier in profile.tiers.items()
                },
            }
            for profile in list_router_setup_profiles()
        ],
    }
