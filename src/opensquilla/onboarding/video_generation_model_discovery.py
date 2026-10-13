"""Video-output model discovery for the capability editor."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from opensquilla.env import trust_env as _trust_env
from opensquilla.provider.tokenrhythm_catalog import TOKENRHYTHM_PUBLIC_CATALOG_URL
from opensquilla.provider.video_generation_catalog import (
    VideoGenerationProviderCatalogEntry,
    get_video_generation_provider_catalog_entry,
)

log = structlog.get_logger(__name__)

_DISCOVERY_TIMEOUT_SECONDS = 8.0
_VIDEO_GENERATION_ABILITIES = frozenset(
    {"video-generation", "video_generation", "text-to-video", "text_to_video"}
)


def _model_row(
    model_id: str,
    *,
    name: str = "",
    capability_source: str = "",
    verified: bool = False,
) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": name or model_id,
        "contextWindow": None,
        "maxOutputTokens": None,
        "capabilities": [],
        "pricing": None,
        "capabilitySource": capability_source,
        "verified": verified,
    }


def curated_video_generation_models(
    entry: VideoGenerationProviderCatalogEntry,
) -> list[dict[str, Any]]:
    """Return documented choices without claiming current account access."""

    source = "TokenRhythm documentation" if entry.provider_id == "tokenrhythm" else "Catalog"
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for raw_model_id in entry.suggested_models:
        model_id = str(raw_model_id or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        rows.append(_model_row(model_id, capability_source=source))
    return rows


def parse_tokenrhythm_video_models(payload: Any) -> list[dict[str, Any]]:
    """Only online generation rows qualify as live video picker choices."""

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise ValueError("TokenRhythm video model response has no data list")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        model_type = str(item.get("type") or "").strip().lower()
        abilities = item.get("abilities")
        ability_names = (
            {str(value).strip().lower() for value in abilities if isinstance(value, str)}
            if isinstance(abilities, list)
            else set()
        )
        generation_capable = (
            model_type in {"video", "video-generation", "video_generation", "text-to-video"}
            or bool(ability_names & _VIDEO_GENERATION_ABILITIES)
        )
        if not model_id or model_id in seen or status != "online" or not generation_capable:
            continue
        seen.add(model_id)
        rows.append(
            _model_row(
                model_id,
                name=str(item.get("name") or "").strip(),
                capability_source="TokenRhythm",
                verified=True,
            )
        )
    return rows


async def _fetch_tokenrhythm_video_models() -> list[dict[str, Any]]:
    # This public endpoint is fixed. No operator URL or credential is sent.
    async with httpx.AsyncClient(
        timeout=_DISCOVERY_TIMEOUT_SECONDS,
        trust_env=_trust_env(),
        follow_redirects=False,
    ) as client:
        response = await client.get(
            TOKENRHYTHM_PUBLIC_CATALOG_URL,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return parse_tokenrhythm_video_models(response.json())


async def discover_video_generation_models(provider_id: str) -> dict[str, Any]:
    entry = get_video_generation_provider_catalog_entry(provider_id)
    curated = curated_video_generation_models(entry)
    if entry.provider_id != "tokenrhythm":
        return {
            "ok": True,
            "providerId": entry.provider_id,
            "models": curated,
            "source": "catalog",
        }

    try:
        live = await _fetch_tokenrhythm_video_models()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        log.info(
            "video_model_discovery_fallback",
            provider=entry.provider_id,
            error_type=type(exc).__name__,
        )
        live = []

    return {
        "ok": True,
        "providerId": entry.provider_id,
        "models": live or curated,
        "source": "live" if live else "documented",
    }


__all__ = [
    "curated_video_generation_models",
    "discover_video_generation_models",
    "parse_tokenrhythm_video_models",
]
