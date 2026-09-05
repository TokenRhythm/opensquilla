"""Onboarding-friendly search provider catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from opensquilla.search.registry import list_provider_specs
from opensquilla.search.types import DEFAULT_SEARCH_MAX_RESULTS, SearchProviderSpec

FieldType = Literal["text", "password", "select", "bool", "int"]


@dataclass(frozen=True)
class SearchProviderSetupField:
    name: str
    label: str
    field_type: FieldType
    required: bool
    default: str | int | bool | None = None
    choices: tuple[str, ...] = ()
    description: str = ""
    secret: bool = False


@dataclass(frozen=True)
class SearchProviderSetupSpec:
    provider_id: str
    label: str
    runtime_supported: bool
    metadata_supported: bool
    requires_api_key: bool
    env_key: str
    deployment: Literal["cloud", "local"]
    blocking: bool
    can_probe: bool
    readme_scenarios: tuple[str, ...]
    what_you_need: tuple[str, ...]
    capabilities: tuple[str, ...]
    fields: tuple[SearchProviderSetupField, ...]


_SEARCH_PROVIDER_LABELS: dict[str, str] = {
    "bocha": "Bocha",
    "brave": "Brave Search",
    "duckduckgo": "DuckDuckGo",
    "iqs": "Alibaba Cloud IQS",
    "serpdive": "SERPdive",
    "tavily": "Tavily",
    "exa": "Exa",
    "perplexity": "Perplexity",
}


def _fields_for(spec: SearchProviderSpec) -> tuple[SearchProviderSetupField, ...]:
    return (
        SearchProviderSetupField(
            name="api_key",
            label="API key",
            field_type="password",
            required=spec.requires_api_key,
            description=(
                (
                    "Saved as plaintext search_api_key in the config file and "
                    f"used ahead of {spec.env_key}. Leave blank to read "
                    f"{spec.env_key} from the environment instead."
                )
                if spec.env_key
                else ""
            ),
            secret=True,
        ),
        SearchProviderSetupField(
            name="max_results",
            label="Max results",
            field_type="int",
            required=False,
            default=DEFAULT_SEARCH_MAX_RESULTS,
        ),
        SearchProviderSetupField(
            name="proxy",
            label="HTTP proxy",
            field_type="text",
            required=False,
            default="",
        ),
        SearchProviderSetupField(
            name="use_env_proxy",
            label="Use environment proxy",
            field_type="bool",
            required=False,
            default=False,
        ),
        SearchProviderSetupField(
            name="fallback_policy",
            label="Fallback policy",
            field_type="select",
            required=False,
            default="off",
            choices=("off", "network"),
            description=(
                "network tries at most one additional configured, compatible provider "
                "after a transient failure, using DuckDuckGo when no keyed fallback is "
                "available; off sends at most one provider request and never switches "
                "after a network failure."
            ),
        ),
        SearchProviderSetupField(
            name="diagnostics",
            label="Diagnostics",
            field_type="bool",
            required=False,
            default=False,
            description=(
                "Include provider attempt/error details in search results for "
                "troubleshooting; does not enable raw capture."
            ),
        ),
    )


def _to_setup_spec(spec: SearchProviderSpec) -> SearchProviderSetupSpec:
    what_you_need = (
        (f"API key via {spec.env_key} or a one-time paste.",)
        if spec.requires_api_key
        else ("No API key required.",)
    )
    return SearchProviderSetupSpec(
        provider_id=spec.provider_id,
        label=_SEARCH_PROVIDER_LABELS.get(spec.provider_id, spec.provider_id),
        runtime_supported=spec.runtime_supported,
        metadata_supported=spec.metadata_supported,
        requires_api_key=spec.requires_api_key,
        env_key=spec.env_key,
        deployment="cloud" if spec.requires_api_key else "local",
        blocking=False,
        can_probe=False,
        readme_scenarios=("built-in web search", "first-run setup"),
        what_you_need=what_you_need,
        capabilities=tuple(sorted(spec.capabilities)),
        fields=_fields_for(spec),
    )


def list_search_provider_setup_specs() -> list[SearchProviderSetupSpec]:
    return [_to_setup_spec(s) for s in list_provider_specs()]


def get_search_provider_setup_spec(provider_id: str) -> SearchProviderSetupSpec:
    for spec in list_search_provider_setup_specs():
        if spec.provider_id == provider_id:
            return spec
    raise KeyError(f"unknown search provider: {provider_id!r}")


def search_provider_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "providerId": s.provider_id,
            "label": s.label,
            "runtimeSupported": s.runtime_supported,
            "metadataSupported": s.metadata_supported,
            "requiresApiKey": s.requires_api_key,
            "envKey": s.env_key,
            "deployment": s.deployment,
            "blocking": s.blocking,
            "canProbe": s.can_probe,
            "readmeScenarios": list(s.readme_scenarios),
            "whatYouNeed": list(s.what_you_need),
            "capabilities": list(s.capabilities),
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
        for s in list_search_provider_setup_specs()
    ]
