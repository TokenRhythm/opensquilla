"""Onboarding-friendly image generation provider catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from opensquilla.provider.image_generation_policy import (
    IMAGE_GENERATION_OFFICIAL_BASE_URLS,
)

FieldType = Literal["text", "password", "select", "bool"]


@dataclass(frozen=True)
class ImageGenerationProviderSetupField:
    name: str
    label: str
    field_type: FieldType
    required: bool
    default: str | bool | None = None
    choices: tuple[str, ...] = ()
    description: str = ""
    secret: bool = False


@dataclass(frozen=True)
class ImageGenerationProviderSetupSpec:
    provider_id: str
    label: str
    runtime_supported: bool
    requires_api_key: bool
    env_key: str
    default_base_url: str
    default_model: str
    suggested_models: tuple[str, ...]
    deployment: str
    blocking: bool
    can_probe: bool
    readme_scenarios: tuple[str, ...]
    what_you_need: tuple[str, ...]
    fields: tuple[ImageGenerationProviderSetupField, ...]


_IMAGE_PROVIDER_DATA: dict[str, dict[str, Any]] = {
    "openai": {
        "label": "OpenAI Images",
        "env_key": "OPENAI_API_KEY",
        "default_base_url": IMAGE_GENERATION_OFFICIAL_BASE_URLS["openai"],
        "default_model": "openai/gpt-image-1",
        "suggested_models": ("openai/gpt-image-1",),
    },
    "openrouter": {
        "label": "OpenRouter Images",
        "env_key": "OPENROUTER_API_KEY",
        "default_base_url": IMAGE_GENERATION_OFFICIAL_BASE_URLS["openrouter"],
        "default_model": "openrouter/google/gemini-3.1-flash-image-preview",
        "suggested_models": ("openrouter/google/gemini-3.1-flash-image-preview",),
    },
    "qwen_token_plan": {
        "label": "Qwen Token Plan Images",
        "env_key": "QWEN_TOKEN_PLAN_API_KEY",
        "default_base_url": IMAGE_GENERATION_OFFICIAL_BASE_URLS["qwen_token_plan"],
        "default_model": "qwen_token_plan/wan2.7-image",
        "suggested_models": (
            "qwen_token_plan/wan2.7-image",
            "qwen_token_plan/wan2.7-image-pro",
        ),
    },
}


def _fields_for(data: dict[str, Any]) -> tuple[ImageGenerationProviderSetupField, ...]:
    return (
        ImageGenerationProviderSetupField(
            name="enabled",
            label="Enabled",
            field_type="bool",
            required=False,
            default=True,
        ),
        ImageGenerationProviderSetupField(
            name="primary",
            label="Primary model",
            field_type="text",
            required=True,
            default=str(data["default_model"]),
            description="Provider/model identifier.",
        ),
        ImageGenerationProviderSetupField(
            name="api_key",
            label="API key",
            field_type="password",
            required=False,
            default="",
            description=f"May be provided by {data['env_key']}.",
            secret=True,
        ),
        ImageGenerationProviderSetupField(
            name="base_url",
            label="Base URL",
            field_type="text",
            required=False,
            default=str(data["default_base_url"]),
            description="Override the upstream HTTP base URL.",
        ),
    )


def list_image_generation_provider_setup_specs() -> list[ImageGenerationProviderSetupSpec]:
    return [
        ImageGenerationProviderSetupSpec(
            provider_id=provider_id,
            label=str(data["label"]),
            runtime_supported=True,
            requires_api_key=True,
            env_key=str(data["env_key"]),
            default_base_url=str(data["default_base_url"]),
            default_model=str(data["default_model"]),
            suggested_models=tuple(data["suggested_models"]),
            deployment="cloud",
            blocking=False,
            can_probe=False,
            readme_scenarios=("image generation", "first-run setup"),
            what_you_need=(
                f"API key via {data['env_key']} or a one-time paste.",
                "A provider/model id that supports image generation.",
            ),
            fields=_fields_for(data),
        )
        for provider_id, data in _IMAGE_PROVIDER_DATA.items()
    ]


def get_image_generation_provider_setup_spec(
    provider_id: str,
) -> ImageGenerationProviderSetupSpec:
    for spec in list_image_generation_provider_setup_specs():
        if spec.provider_id == provider_id:
            return spec
    raise KeyError(f"unknown image generation provider: {provider_id!r}")


def image_generation_provider_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "providerId": s.provider_id,
            "label": s.label,
            "runtimeSupported": s.runtime_supported,
            "requiresApiKey": s.requires_api_key,
            "envKey": s.env_key,
            "defaultBaseUrl": s.default_base_url,
            "defaultModel": s.default_model,
            "suggestedModels": list(s.suggested_models),
            "deployment": s.deployment,
            "blocking": s.blocking,
            "canProbe": s.can_probe,
            "readmeScenarios": list(s.readme_scenarios),
            "whatYouNeed": list(s.what_you_need),
            "fields": [
                {
                    "name": f.name,
                    "label": f.label,
                    "type": f.field_type,
                    "required": f.required,
                    "default": f.default,
                    "choices": list(f.choices),
                    "description": f.description,
                    "secret": f.secret,
                }
                for f in s.fields
            ],
        }
        for s in list_image_generation_provider_setup_specs()
    ]
