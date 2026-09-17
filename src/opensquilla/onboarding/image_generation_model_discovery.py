"""Picker-safe image-generation model discovery for onboarding clients.

The capability editor must never reuse the general LLM model catalog: those
endpoints commonly include chat-only models that cannot produce images.  Live
discovery is therefore limited to providers with a dedicated image-model
endpoint on a fixed official origin.  Every provider retains a curated catalog
fallback so model selection remains useful offline and on older deployments.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.onboarding.image_generation_specs import (
    ImageGenerationProviderSetupSpec,
    get_image_generation_provider_setup_spec,
)
from opensquilla.provider.app_attribution import provider_app_headers
from opensquilla.provider.error_redaction import redacted_httpx_error
from opensquilla.provider.image_generation_policy import (
    IMAGE_GENERATION_OFFICIAL_BASE_URLS,
)
from opensquilla.provider.tokenrhythm_correlation import (
    redact_tokenrhythm_install_ids,
    tokenrhythm_install_id_headers,
)

log = structlog.get_logger(__name__)

_OPENROUTER_IMAGE_MODELS_URL = (
    f"{IMAGE_GENERATION_OFFICIAL_BASE_URLS['openrouter'].rstrip('/')}/images/models"
)
_TOKENRHYTHM_IMAGE_MODELS_URL = "https://tokenrhythm.studio/api/models"
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


def parse_tokenrhythm_image_models(payload: Any) -> list[dict[str, Any]]:
    """Normalize online image-capable rows from TokenRhythm's public catalog."""

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("TokenRhythm image model response has no data list")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        model_type = str(item.get("type") or "").strip().lower()
        abilities = item.get("abilities")
        normalized_abilities = {
            str(ability or "").strip().lower()
            for ability in abilities
            if isinstance(ability, str)
        } if isinstance(abilities, list) else set()
        if (
            not model_id
            or model_id in seen
            or status not in {"", "online"}
            or (model_type != "image" and "image" not in normalized_abilities)
        ):
            continue

        seen.add(model_id)
        rows.append(
            _model_row(
                model_id,
                name=str(item.get("name") or "").strip(),
                capability_source="TokenRhythm",
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


async def _fetch_tokenrhythm_image_models() -> list[dict[str, Any]]:
    safe_request_error: Exception | None = None
    cancelled_request_error: asyncio.CancelledError | None = None
    headers: dict[str, str] = {
        "Accept": "application/json",
        **provider_app_headers(_TOKENRHYTHM_IMAGE_MODELS_URL),
    }
    client: Any = None
    response: Any = None
    payload: Any = None
    parsed: list[dict[str, Any]] | None = None
    raw_message = ""
    raw_state = ""
    try:
        async with httpx.AsyncClient(
            timeout=_DISCOVERY_TIMEOUT_SECONDS,
            trust_env=_trust_env(),
            follow_redirects=False,
        ) as client:
            headers.update(
                tokenrhythm_install_id_headers(
                    "tokenrhythm",
                    _TOKENRHYTHM_IMAGE_MODELS_URL,
                )
            )
            response = await client.get(
                _TOKENRHYTHM_IMAGE_MODELS_URL,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            parsed = parse_tokenrhythm_image_models(payload)
    except asyncio.CancelledError:
        cancelled_request_error = asyncio.CancelledError()
    except httpx.HTTPError as exc:
        safe_request_error = redacted_httpx_error(exc, api_key="")
    except json.JSONDecodeError as exc:
        if redact_tokenrhythm_install_ids(exc.doc) == exc.doc:
            exc.__cause__ = None
            exc.__context__ = None
            exc.__traceback__ = None
            safe_request_error = exc
        else:
            safe_request_error = ValueError(
                "TokenRhythm image catalog returned invalid JSON"
            )
    except Exception as exc:
        raw_message = str(exc)
        safe_message = redact_tokenrhythm_install_ids(raw_message)
        raw_state = repr(getattr(exc, "__dict__", {}))
        if (
            safe_message != raw_message
            or redact_tokenrhythm_install_ids(raw_state) != raw_state
        ):
            safe_request_error = RuntimeError(
                safe_message
                if safe_message != raw_message
                else "TokenRhythm image catalog parsing failed"
            )
        else:
            exc.__cause__ = None
            exc.__context__ = None
            exc.__traceback__ = None
            safe_request_error = exc

    if cancelled_request_error is not None:
        headers.clear()
        client = None
        response = None
        payload = None
        parsed = None
        raw_message = ""
        raw_state = ""
        raise cancelled_request_error
    if safe_request_error is not None:
        headers.clear()
        client = None
        response = None
        payload = None
        parsed = None
        raw_message = ""
        raw_state = ""
        raise safe_request_error
    return parsed or []


async def discover_image_generation_models(provider_id: str) -> dict[str, Any]:
    """Return a live image catalog when available, otherwise curated rows."""
    spec = get_image_generation_provider_setup_spec(str(provider_id or "").strip())
    curated = curated_image_generation_models(spec)
    fetch_live = {
        "openrouter": _fetch_openrouter_image_models,
        "tokenrhythm": _fetch_tokenrhythm_image_models,
    }.get(spec.provider_id)
    if fetch_live is None:
        return {
            "ok": True,
            "providerId": spec.provider_id,
            "source": "catalog",
            "models": curated,
        }

    try:
        live = await fetch_live()
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
    "parse_tokenrhythm_image_models",
]
