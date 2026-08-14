"""Provider-owned image generation setup metadata.

Runtime tools and onboarding both need the provider's credential environment,
official endpoint, and model defaults.  Keeping that neutral metadata in the
provider layer avoids making runtime tools depend on the onboarding package.
"""

from __future__ import annotations

from dataclasses import dataclass

from opensquilla.provider.image_generation_policy import (
    IMAGE_GENERATION_OFFICIAL_BASE_URLS,
)


@dataclass(frozen=True)
class ImageGenerationProviderCatalogEntry:
    provider_id: str
    label: str
    env_key: str
    default_base_url: str
    default_model: str
    suggested_models: tuple[str, ...]


_IMAGE_GENERATION_PROVIDER_CATALOG = (
    ImageGenerationProviderCatalogEntry(
        provider_id="openai",
        label="OpenAI Images",
        env_key="OPENAI_API_KEY",
        default_base_url=IMAGE_GENERATION_OFFICIAL_BASE_URLS["openai"],
        default_model="openai/gpt-image-1",
        suggested_models=("openai/gpt-image-1",),
    ),
    ImageGenerationProviderCatalogEntry(
        provider_id="openrouter",
        label="OpenRouter Images",
        env_key="OPENROUTER_API_KEY",
        default_base_url=IMAGE_GENERATION_OFFICIAL_BASE_URLS["openrouter"],
        default_model="openrouter/google/gemini-3.1-flash-image-preview",
        suggested_models=("openrouter/google/gemini-3.1-flash-image-preview",),
    ),
    ImageGenerationProviderCatalogEntry(
        provider_id="tokenrhythm",
        label="TokenRhythm Images",
        env_key="TOKENRHYTHM_API_KEY",
        default_base_url=IMAGE_GENERATION_OFFICIAL_BASE_URLS["tokenrhythm"],
        default_model="tokenrhythm/qwen-image-2.0",
        suggested_models=(
            "tokenrhythm/qwen-image-2.0",
            "tokenrhythm/wan2.7-image",
        ),
    ),
    ImageGenerationProviderCatalogEntry(
        provider_id="qwen_token_plan",
        label="Qwen Token Plan Images",
        env_key="QWEN_TOKEN_PLAN_API_KEY",
        default_base_url=IMAGE_GENERATION_OFFICIAL_BASE_URLS["qwen_token_plan"],
        default_model="qwen_token_plan/wan2.7-image",
        suggested_models=(
            "qwen_token_plan/wan2.7-image",
            "qwen_token_plan/wan2.7-image-pro",
        ),
    ),
)


def list_image_generation_provider_catalog_entries() -> tuple[
    ImageGenerationProviderCatalogEntry, ...
]:
    """Return image provider metadata in the stable setup display order."""

    return _IMAGE_GENERATION_PROVIDER_CATALOG


def get_image_generation_provider_catalog_entry(
    provider_id: str,
) -> ImageGenerationProviderCatalogEntry:
    provider = str(provider_id or "").strip().lower()
    for entry in _IMAGE_GENERATION_PROVIDER_CATALOG:
        if entry.provider_id == provider:
            return entry
    raise KeyError(f"unknown image generation provider: {provider_id!r}")


__all__ = [
    "ImageGenerationProviderCatalogEntry",
    "get_image_generation_provider_catalog_entry",
    "list_image_generation_provider_catalog_entries",
]
