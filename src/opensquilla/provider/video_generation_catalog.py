"""Provider-owned metadata for video generation setup."""

from __future__ import annotations

from dataclasses import dataclass

from opensquilla.provider.video_generation_policy import (
    VIDEO_GENERATION_DEFAULT_ENV_KEYS,
    VIDEO_GENERATION_OFFICIAL_BASE_URLS,
)


@dataclass(frozen=True)
class VideoGenerationProviderCatalogEntry:
    provider_id: str
    label: str
    env_key: str
    default_base_url: str
    default_model: str
    suggested_models: tuple[str, ...]
    default_model_verification: str = "catalog"


_VIDEO_GENERATION_PROVIDER_CATALOG = (
    VideoGenerationProviderCatalogEntry(
        provider_id="openrouter",
        label="OpenRouter",
        env_key=VIDEO_GENERATION_DEFAULT_ENV_KEYS["openrouter"],
        default_base_url=VIDEO_GENERATION_OFFICIAL_BASE_URLS["openrouter"],
        default_model="google/veo-3.1-fast",
        suggested_models=("google/veo-3.1-fast", "google/veo-3.1"),
    ),
    VideoGenerationProviderCatalogEntry(
        provider_id="gemini",
        label="Google Gemini",
        env_key=VIDEO_GENERATION_DEFAULT_ENV_KEYS["gemini"],
        default_base_url=VIDEO_GENERATION_OFFICIAL_BASE_URLS["gemini"],
        default_model="veo-3.1-fast-generate-preview",
        suggested_models=(
            "veo-3.1-fast-generate-preview",
            "veo-3.1-generate-preview",
            "veo-3.1-lite-generate-preview",
        ),
    ),
    VideoGenerationProviderCatalogEntry(
        provider_id="xai",
        label="xAI",
        env_key=VIDEO_GENERATION_DEFAULT_ENV_KEYS["xai"],
        default_base_url=VIDEO_GENERATION_OFFICIAL_BASE_URLS["xai"],
        default_model="grok-imagine-video-1.5",
        suggested_models=(
            "grok-imagine-video-1.5",
            "grok-imagine-video-1.5-lite",
            "grok-imagine-video",
        ),
    ),
    VideoGenerationProviderCatalogEntry(
        provider_id="qwen",
        label="Qwen (Standard DashScope)",
        env_key=VIDEO_GENERATION_DEFAULT_ENV_KEYS["qwen"],
        default_base_url=VIDEO_GENERATION_OFFICIAL_BASE_URLS["qwen"],
        default_model="wan2.7-t2v",
        suggested_models=("wan2.7-t2v", "wan2.7-t2v-2026-06-12", "wan2.6-t2v"),
    ),
    VideoGenerationProviderCatalogEntry(
        provider_id="qwen_token_plan",
        label="Qwen Token Plan",
        env_key=VIDEO_GENERATION_DEFAULT_ENV_KEYS["qwen_token_plan"],
        default_base_url=VIDEO_GENERATION_OFFICIAL_BASE_URLS["qwen_token_plan"],
        default_model="happyhorse-1.1-t2v",
        suggested_models=("happyhorse-1.1-t2v",),
    ),
    VideoGenerationProviderCatalogEntry(
        provider_id="tokenrhythm",
        label="TokenRhythm",
        env_key=VIDEO_GENERATION_DEFAULT_ENV_KEYS["tokenrhythm"],
        default_base_url=VIDEO_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"],
        default_model="wan3.0-video",
        suggested_models=("wan3.0-video",),
        default_model_verification="documented",
    ),
)


def list_video_generation_provider_catalog_entries() -> tuple[
    VideoGenerationProviderCatalogEntry, ...
]:
    return _VIDEO_GENERATION_PROVIDER_CATALOG


def get_video_generation_provider_catalog_entry(
    provider_id: str,
) -> VideoGenerationProviderCatalogEntry:
    provider = str(provider_id or "").strip().lower()
    for entry in _VIDEO_GENERATION_PROVIDER_CATALOG:
        if entry.provider_id == provider:
            return entry
    raise KeyError(f"unknown video generation provider: {provider_id!r}")


def video_generation_provider_catalog_payload() -> list[dict[str, object]]:
    return [
        {
            "providerId": entry.provider_id,
            "label": entry.label,
            "runtimeSupported": True,
            "requiresApiKey": True,
            "envKey": entry.env_key,
            "defaultBaseUrl": entry.default_base_url,
            "defaultModel": entry.default_model,
            "suggestedModels": list(entry.suggested_models),
            "defaultModelVerification": entry.default_model_verification,
        }
        for entry in _VIDEO_GENERATION_PROVIDER_CATALOG
    ]


__all__ = [
    "VideoGenerationProviderCatalogEntry",
    "get_video_generation_provider_catalog_entry",
    "list_video_generation_provider_catalog_entries",
    "video_generation_provider_catalog_payload",
]
