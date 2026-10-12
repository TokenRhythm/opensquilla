"""Onboarding-friendly memory embedding provider catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Deployment = Literal["auto", "local", "cloud", "custom", "disabled"]


@dataclass(frozen=True)
class MemoryEmbeddingProviderSetupSpec:
    provider_id: str
    label: str
    deployment: Deployment
    runtime_supported: bool
    requires_api_key: bool
    env_key: str
    requires_base_url: bool
    blocking: bool
    can_probe: bool
    readme_scenarios: tuple[str, ...]
    what_you_need: tuple[str, ...]


_MEMORY_EMBEDDING_PROVIDER_DATA: dict[str, dict[str, Any]] = {
    "auto": {
        "label": "Auto (local BGE first)",
        "deployment": "auto",
        "requires_api_key": False,
        "requires_base_url": False,
        "what_you_need": (
            "Bundled local embeddings for the default path.",
            "Optional remote fallback credentials if configured.",
        ),
    },
    "local": {
        "label": "Bundled BGE-small",
        "deployment": "local",
        "requires_api_key": False,
        "requires_base_url": False,
        "what_you_need": (
            "Local ONNX embedding assets from the recommended install.",
            "Optional ONNX directory override for custom local assets.",
        ),
    },
    "openai": {
        "label": "OpenAI",
        "deployment": "cloud",
        "requires_api_key": True,
        "env_key": "OPENAI_API_KEY",
        "requires_base_url": False,
        "what_you_need": (
            "Remote embedding API key or OPENAI_API_KEY reference.",
            "Optional embedding model override.",
        ),
    },
    "openai-compatible": {
        "label": "OpenAI-compatible remote",
        "deployment": "custom",
        "requires_api_key": True,
        "env_key": "",
        "requires_base_url": False,
        "what_you_need": (
            "Remote embedding API key or env reference.",
            "Optional base URL and model for the compatible endpoint.",
        ),
    },
    "ollama": {
        "label": "Ollama",
        "deployment": "local",
        "requires_api_key": False,
        "requires_base_url": False,
        "what_you_need": (
            "A reachable Ollama server.",
            "Optional embedding model override.",
        ),
    },
    "none": {
        "label": "FTS-only",
        "deployment": "disabled",
        "requires_api_key": False,
        "requires_base_url": False,
        "what_you_need": ("No embedding service; keyword search remains available.",),
    },
}


def list_memory_embedding_provider_setup_specs() -> list[MemoryEmbeddingProviderSetupSpec]:
    return [
        MemoryEmbeddingProviderSetupSpec(
            provider_id=provider_id,
            label=str(data["label"]),
            deployment=data["deployment"],
            runtime_supported=True,
            requires_api_key=bool(data["requires_api_key"]),
            env_key=str(data.get("env_key", "")),
            requires_base_url=bool(data["requires_base_url"]),
            blocking=False,
            can_probe=False,
            readme_scenarios=("memory", "first-run setup"),
            what_you_need=tuple(data["what_you_need"]),
        )
        for provider_id, data in _MEMORY_EMBEDDING_PROVIDER_DATA.items()
    ]


def get_memory_embedding_provider_setup_spec(
    provider_id: str,
) -> MemoryEmbeddingProviderSetupSpec:
    for spec in list_memory_embedding_provider_setup_specs():
        if spec.provider_id == provider_id:
            return spec
    raise KeyError(f"unknown memory embedding provider: {provider_id!r}")


def memory_embedding_provider_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "providerId": s.provider_id,
            "label": s.label,
            "deployment": s.deployment,
            "runtimeSupported": s.runtime_supported,
            "requiresApiKey": s.requires_api_key,
            "envKey": s.env_key,
            "requiresBaseUrl": s.requires_base_url,
            "blocking": s.blocking,
            "canProbe": s.can_probe,
            "readmeScenarios": list(s.readme_scenarios),
            "whatYouNeed": list(s.what_you_need),
        }
        for s in list_memory_embedding_provider_setup_specs()
    ]
