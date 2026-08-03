"""Picker-safe image-generation model discovery for onboarding clients.

The capability editor must never reuse the general LLM model catalog: those
endpoints commonly include chat-only models that cannot produce images.  Live
discovery is therefore limited to providers with a dedicated image-model
endpoint on a fixed official origin.  Every provider retains a curated catalog
fallback so model selection remains useful offline and on older deployments.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from opensquilla.onboarding.image_generation_specs import (
    ImageGenerationProviderSetupSpec,
    get_image_generation_provider_setup_spec,
)
from opensquilla.provider.image_generation_policy import (
    IMAGE_GENERATION_OFFICIAL_BASE_URLS,
)

log = structlog.get_logger(__name__)

_OPENROUTER_IMAGE_MODELS_URL = (
    f"{IMAGE_GENERATION_OFFICIAL_BASE_URLS['openrouter'].rstrip('/')}/images/models"
)
_DISCOVERY_TIMEOUT_SECONDS = 8.0


def _local_model_id(provider_id: str, model_id: str) -> str:
    """Return the provider-local id used by the capability editor."""
    value = str(model_id or "").strip()
    prefix = f"{provider_id}/"
    return value[len(prefix) :] if value.startswith(prefix) else value


def _model_row(
    model_id: str,
    *,
    name: str = "",
    capability_source: str = "",
) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": name or model_id,
        "contextWindow": None,
        "maxOutputTokens": None,
        "capabilities": [],
        "pricing": None,
        "capabilitySource": capability_source,
    }


def curated_image_generation_models(
    spec: ImageGenerationProviderSetupSpec,
) -> list[dict[str, Any]]:
    """Build the offline-safe picker rows from the provider setup catalog."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_model_id in spec.suggested_models:
        model_id = _local_model_id(spec.provider_id, raw_model_id)
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        rows.append(_model_row(model_id))
    return rows


def parse_openrouter_image_models(payload: Any) -> list[dict[str, Any]]:
    """Normalize OpenRouter's dedicated image-model response for the WebUI."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("OpenRouter image model response has no data list")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue

        # The endpoint is already image-specific.  If a row nevertheless
        # declares output modalities, fail closed on rows that omit image.
        architecture = item.get("architecture")
        output_modalities = (
            architecture.get("output_modalities")
            if isinstance(architecture, dict)
            else None
        )
        if (
            isinstance(output_modalities, list)
            and output_modalities
            and "image" not in output_modalities
        ):
            continue

        seen.add(model_id)
        rows.append(
            _model_row(
                model_id,
                name=str(item.get("name") or "").strip(),
                capability_source="OpenRouter",
            )
        )
    return rows


async def _fetch_openrouter_image_models() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=_DISCOVERY_TIMEOUT_SECONDS) as client:
        response = await client.get(
            _OPENROUTER_IMAGE_MODELS_URL,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return parse_openrouter_image_models(response.json())


async def discover_image_generation_models(provider_id: str) -> dict[str, Any]:
    """Return a live image catalog when available, otherwise curated rows."""
    spec = get_image_generation_provider_setup_spec(str(provider_id or "").strip())
    curated = curated_image_generation_models(spec)
    if spec.provider_id != "openrouter":
        return {
            "ok": True,
            "providerId": spec.provider_id,
            "source": "catalog",
            "models": curated,
        }

    try:
        live = await _fetch_openrouter_image_models()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.info(
            "image_model_discovery_fallback",
            provider=spec.provider_id,
            error_type=type(exc).__name__,
        )
        live = []

    return {
        "ok": True,
        "providerId": spec.provider_id,
        "source": "live" if live else "catalog",
        "models": live or curated,
    }


__all__ = [
    "curated_image_generation_models",
    "discover_image_generation_models",
    "parse_openrouter_image_models",
]
