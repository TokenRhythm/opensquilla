"""Onboarding-friendly image generation provider catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from opensquilla.provider.image_generation_catalog import (
    ImageGenerationProviderCatalogEntry,
    list_image_generation_provider_catalog_entries,
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


def _fields_for(
    entry: ImageGenerationProviderCatalogEntry,
) -> tuple[ImageGenerationProviderSetupField, ...]:
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
            default=entry.default_model,
            description="Provider/model identifier.",
        ),
        ImageGenerationProviderSetupField(
            name="api_key",
            label="API key",
            field_type="password",
            required=False,
            default="",
            description=f"May be provided by {entry.env_key}.",
            secret=True,
        ),
        ImageGenerationProviderSetupField(
            name="base_url",
            label="Base URL",
            field_type="text",
            required=False,
            default=entry.default_base_url,
            description="Override the upstream HTTP base URL.",
        ),
    )


def list_image_generation_provider_setup_specs() -> list[ImageGenerationProviderSetupSpec]:
    return [
        ImageGenerationProviderSetupSpec(
            provider_id=entry.provider_id,
            label=entry.label,
            runtime_supported=True,
            requires_api_key=True,
            env_key=entry.env_key,
            default_base_url=entry.default_base_url,
            default_model=entry.default_model,
            suggested_models=entry.suggested_models,
            deployment="cloud",
            blocking=False,
            can_probe=False,
            readme_scenarios=("image generation", "first-run setup"),
            what_you_need=(
                f"API key via {entry.env_key} or a one-time paste.",
                "A provider/model id that supports image generation.",
            ),
            fields=_fields_for(entry),
        )
        for entry in list_image_generation_provider_catalog_entries()
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
