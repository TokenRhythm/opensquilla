"""Tests for picker-safe image-generation model discovery."""

from __future__ import annotations

import pytest

from opensquilla.onboarding import image_generation_model_discovery as discovery
from opensquilla.onboarding.image_generation_specs import (
    get_image_generation_provider_setup_spec,
)


def test_curated_models_are_provider_local_and_deduplicated():
    spec = get_image_generation_provider_setup_spec("qwen_token_plan")

    rows = discovery.curated_image_generation_models(spec)

    assert [row["id"] for row in rows] == ["wan2.7-image", "wan2.7-image-pro"]
    assert all(row["contextWindow"] is None for row in rows)


def test_openrouter_parser_keeps_only_image_output_rows():
    rows = discovery.parse_openrouter_image_models(
        {
            "data": [
                {
                    "id": "vendor/image-a",
                    "name": "Image A",
                    "architecture": {"output_modalities": ["text", "image"]},
                },
                {
                    "id": "vendor/text-only",
                    "architecture": {"output_modalities": ["text"]},
                },
                {"id": "vendor/image-a", "name": "duplicate"},
                {"id": ""},
                "invalid",
            ]
        }
    )

    assert rows == [
        {
            "id": "vendor/image-a",
            "name": "Image A",
            "contextWindow": None,
            "maxOutputTokens": None,
            "capabilities": [],
            "pricing": None,
            "capabilitySource": "OpenRouter",
        }
    ]


@pytest.mark.asyncio
async def test_openrouter_discovery_uses_live_image_catalog(monkeypatch):
    async def _live():
        return [
            {
                "id": "vendor/image-live",
                "name": "Image Live",
                "contextWindow": None,
                "maxOutputTokens": None,
                "capabilities": [],
                "pricing": None,
                "capabilitySource": "OpenRouter",
            }
        ]

    monkeypatch.setattr(discovery, "_fetch_openrouter_image_models", _live)

    result = await discovery.discover_image_generation_models("openrouter")

    assert result["source"] == "live"
    assert [row["id"] for row in result["models"]] == ["vendor/image-live"]


@pytest.mark.asyncio
async def test_openrouter_discovery_falls_back_to_curated_catalog(monkeypatch):
    async def _unavailable():
        raise ValueError("synthetic malformed response")

    monkeypatch.setattr(discovery, "_fetch_openrouter_image_models", _unavailable)

    result = await discovery.discover_image_generation_models("openrouter")

    assert result["source"] == "catalog"
    assert [row["id"] for row in result["models"]] == [
        "google/gemini-3.1-flash-image-preview"
    ]


def test_tokenrhythm_parser_keeps_online_image_models():
    rows = discovery.parse_tokenrhythm_image_models(
        {
            "data": [
                {
                    "id": "qwen-image-2.0",
                    "name": "Qwen Image 2.0",
                    "status": "online",
                    "type": "image",
                },
                {
                    "id": "wan2.7-image",
                    "status": "online",
                    "type": "multimodal",
                    "abilities": ["text", "image"],
                },
                {
                    "id": "offline-image",
                    "status": "offline",
                    "type": "image",
                },
                {"id": "chat-only", "status": "online", "type": "text"},
                {"id": "qwen-image-2.0", "status": "online", "type": "image"},
                "invalid",
            ]
        }
    )

    assert [row["id"] for row in rows] == ["qwen-image-2.0", "wan2.7-image"]
    assert rows[0]["capabilitySource"] == "TokenRhythm"


@pytest.mark.asyncio
async def test_tokenrhythm_discovery_uses_live_image_catalog(monkeypatch):
    async def _live():
        return [discovery._model_row("qwen-image-2.0", capability_source="TokenRhythm")]

    monkeypatch.setattr(discovery, "_fetch_tokenrhythm_image_models", _live)

    result = await discovery.discover_image_generation_models("tokenrhythm")

    assert result["source"] == "live"
    assert [row["id"] for row in result["models"]] == ["qwen-image-2.0"]


@pytest.mark.asyncio
async def test_tokenrhythm_discovery_falls_back_to_curated_catalog(monkeypatch):
    async def _unavailable():
        raise ValueError("synthetic malformed response")

    monkeypatch.setattr(discovery, "_fetch_tokenrhythm_image_models", _unavailable)

    result = await discovery.discover_image_generation_models("tokenrhythm")

    assert result["source"] == "catalog"
    assert [row["id"] for row in result["models"]] == [
        "qwen-image-2.0",
        "wan2.7-image",
    ]
