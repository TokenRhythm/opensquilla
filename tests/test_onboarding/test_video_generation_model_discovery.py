"""Provider-scoped video setup metadata and model discovery."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from opensquilla.onboarding import video_generation_model_discovery as discovery
from opensquilla.onboarding.setup_engine import setup_catalog_payload
from opensquilla.provider.video_generation_catalog import (
    get_video_generation_provider_catalog_entry,
    video_generation_provider_catalog_payload,
)
from opensquilla.provider.video_generation_policy import (
    VIDEO_GENERATION_DEFAULT_ENV_KEYS,
    VIDEO_GENERATION_OFFICIAL_BASE_URLS,
)


def test_video_provider_catalog_uses_runtime_endpoints_and_model_defaults() -> None:
    rows = video_generation_provider_catalog_payload()

    assert {row["providerId"] for row in rows} == {
        "openrouter",
        "gemini",
        "xai",
        "qwen",
        "qwen_token_plan",
        "tokenrhythm",
    }
    for row in rows:
        provider_id = str(row["providerId"])
        assert row["envKey"] == VIDEO_GENERATION_DEFAULT_ENV_KEYS[provider_id]
        assert row["defaultBaseUrl"] == VIDEO_GENERATION_OFFICIAL_BASE_URLS[provider_id]
        assert row["defaultModel"] in row["suggestedModels"]
        assert row["runtimeSupported"] is True

    tokenrhythm = next(row for row in rows if row["providerId"] == "tokenrhythm")
    assert tokenrhythm["defaultModel"] == "wan3.0-video"
    assert tokenrhythm["defaultModelVerification"] == "documented"
    assert setup_catalog_payload("video") == {"videoGenerationProviders": rows}


def test_video_catalog_rejects_unknown_provider() -> None:
    with pytest.raises(KeyError, match="unknown video generation provider"):
        get_video_generation_provider_catalog_entry("unknown")


def test_tokenrhythm_video_parser_requires_online_generation_capability() -> None:
    rows = discovery.parse_tokenrhythm_video_models(
        {
            "data": [
                {"id": "wan-video", "name": "Wan Video", "status": "online", "type": "video"},
                {
                    "id": "custom-generation",
                    "status": "online",
                    "type": "multimodal",
                    "abilities": ["text-to-video"],
                },
                {"id": "offline-video", "status": "offline", "type": "video"},
                {"id": "unknown-status", "type": "video"},
                {
                    "id": "chat-video-input",
                    "status": "online",
                    "type": "chat",
                    "abilities": ["video"],
                },
                {"id": "wan-video", "status": "online", "type": "video"},
                "invalid",
            ]
        }
    )

    assert [row["id"] for row in rows] == ["wan-video", "custom-generation"]
    assert all(row["verified"] is True for row in rows)
    assert rows[0]["name"] == "Wan Video"

    with pytest.raises(ValueError, match="data list"):
        discovery.parse_tokenrhythm_video_models({"data": {}})


@pytest.mark.asyncio
async def test_tokenrhythm_video_discovery_uses_only_fixed_public_endpoint(monkeypatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": [{"id": "online-video", "status": "online", "type": "video"}]},
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def fake_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(discovery.httpx, "AsyncClient", fake_client)
    result = await discovery.discover_video_generation_models("tokenrhythm")

    assert result["source"] == "live"
    assert [row["id"] for row in result["models"]] == ["online-video"]
    assert len(requests) == 1
    assert str(requests[0].url) == "https://tokenrhythm.studio/api/models"
    assert "Authorization" not in requests[0].headers


@pytest.mark.asyncio
async def test_tokenrhythm_documented_model_is_not_mislabeled_as_live(monkeypatch) -> None:
    async def no_live_models() -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(discovery, "_fetch_tokenrhythm_video_models", no_live_models)
    result = await discovery.discover_video_generation_models("tokenrhythm")

    assert result["source"] == "documented"
    assert [row["id"] for row in result["models"]] == ["wan3.0-video"]
    assert all(row["verified"] is False for row in result["models"])


@pytest.mark.asyncio
async def test_tokenrhythm_public_catalog_failure_keeps_documented_choice(monkeypatch) -> None:
    async def unavailable() -> list[dict[str, Any]]:
        raise httpx.ConnectError("synthetic connection failure")

    monkeypatch.setattr(discovery, "_fetch_tokenrhythm_video_models", unavailable)
    result = await discovery.discover_video_generation_models("tokenrhythm")

    assert result["source"] == "documented"
    assert result["models"][0]["verified"] is False


@pytest.mark.asyncio
async def test_other_video_providers_use_curated_models_without_network(monkeypatch) -> None:
    async def unexpected() -> list[dict[str, Any]]:
        pytest.fail("unrelated video provider should not fetch TokenRhythm catalog")

    monkeypatch.setattr(discovery, "_fetch_tokenrhythm_video_models", unexpected)
    result = await discovery.discover_video_generation_models("xai")

    assert result["source"] == "catalog"
    assert result["models"][0]["id"] == "grok-imagine-video-1.5"
    assert result["models"][0]["verified"] is False
