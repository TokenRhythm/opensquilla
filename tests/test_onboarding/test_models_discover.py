"""Behavior tests for live model discovery (offline, stubbed transport)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from opensquilla.gateway import rpc_onboarding
from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.onboarding import probe as probe_module
from opensquilla.onboarding.probe import (
    ProviderModelsDiscoverResult,
    discover_provider_models,
    discover_selectable_provider_models,
)
from opensquilla.provider.failures import ProviderFailureKind


def _patch_response(monkeypatch: Any, response_factory) -> list[httpx.Request]:
    """Route provider HTTP through a MockTransport, capturing requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response_factory()

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    return seen


def _patch_transport_error(monkeypatch: Any, exc: Exception) -> None:
    """Route provider HTTP through a transport that always fails to connect."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _models_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps(
            {"data": [{"id": "test-model-a", "name": "Test Model A", "context_length": 64_000}]}
        ).encode(),
    )


def _discover(**kwargs: Any):
    return asyncio.run(discover_provider_models(**kwargs))


def _discover_selectable(**kwargs: Any):
    return asyncio.run(discover_selectable_provider_models(**kwargs))


def test_selectable_discovery_fails_closed_before_credentials_or_provider_build(
    monkeypatch: Any,
) -> None:
    """Unverified adapters stay free text and must not touch secrets/network."""

    def _unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("untrusted discovery must not resolve credentials or build")

    monkeypatch.setattr(probe_module, "_resolve_probe_api_key", _unexpected)
    monkeypatch.setattr(probe_module, "build_provider", _unexpected)
    monkeypatch.setattr(probe_module, "discover_provider_models", _unexpected)

    result = _discover_selectable(
        provider_id="openai",
        api_key="synthetic-secret",
        base_url="https://api.openai.com/v1",
    )

    assert result == ProviderModelsDiscoverResult(ok=True, provider_id="openai")


@pytest.mark.parametrize("provider_id", ["openrouter", "tokenrhythm"])
def test_selectable_discovery_delegates_verified_official_hosts(
    monkeypatch: Any, provider_id: str
) -> None:
    calls: list[dict[str, Any]] = []

    async def _fake_raw(**kwargs: Any) -> ProviderModelsDiscoverResult:
        calls.append(kwargs)
        return ProviderModelsDiscoverResult(
            ok=True,
            provider_id=kwargs["provider_id"],
            source="live",
            models=[{"id": "verified-model"}],
        )

    monkeypatch.setattr(probe_module, "discover_provider_models", _fake_raw)

    result = _discover_selectable(provider_id=provider_id, api_key="synthetic-key")

    assert result.source == "live"
    assert result.models == [{"id": "verified-model"}]
    assert calls == [
        {
            "provider_id": provider_id,
            "api_key": "synthetic-key",
            "api_key_env": "",
            "base_url": "",
            "proxy": "",
        }
    ]


@pytest.mark.parametrize(
    ("provider_id", "base_url"),
    [
        ("openrouter", "https://openrouter.ai.attacker.example/v1"),
        ("tokenrhythm", "https://tokenrhythm.studio.attacker.example/v1"),
    ],
)
def test_selectable_discovery_rejects_non_official_base_url_before_raw_discovery(
    monkeypatch: Any, provider_id: str, base_url: str
) -> None:
    async def _unexpected_raw(**_kwargs: Any) -> ProviderModelsDiscoverResult:
        raise AssertionError("a custom host must not be treated as a verified catalog")

    monkeypatch.setattr(probe_module, "discover_provider_models", _unexpected_raw)

    result = _discover_selectable(
        provider_id=provider_id,
        api_key="synthetic-key",
        base_url=base_url,
    )

    assert result == ProviderModelsDiscoverResult(ok=True, provider_id=provider_id)


@pytest.mark.parametrize(
    ("provider_id", "base_url"),
    [
        ("openrouter", "http://openrouter.ai/api/v1"),
        ("tokenrhythm", "http://tokenrhythm.studio/v1"),
    ],
)
def test_selectable_discovery_rejects_plain_http_before_credentials_or_raw_discovery(
    monkeypatch: Any, provider_id: str, base_url: str
) -> None:
    def _unexpected(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("plain HTTP must not resolve credentials or discover models")

    monkeypatch.setattr(probe_module, "_resolve_probe_api_key", _unexpected)
    monkeypatch.setattr(probe_module, "discover_provider_models", _unexpected)

    result = _discover_selectable(
        provider_id=provider_id,
        api_key="synthetic-key",
        base_url=base_url,
    )

    assert result == ProviderModelsDiscoverResult(ok=True, provider_id=provider_id)


def test_selectable_discovery_still_validates_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="no runtime support"):
        _discover_selectable(provider_id="github_copilot")


def test_selectable_discovery_still_validates_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        _discover_selectable(provider_id="no-such-provider")


@pytest.mark.parametrize(
    ("provider_id", "base_url"),
    [
        ("openrouter", "https://api.openrouter.ai/v1"),
        ("tokenrhythm", "https://api.tokenrhythm.studio/v1"),
    ],
)
def test_selectable_discovery_accepts_official_subdomains(
    monkeypatch: Any, provider_id: str, base_url: str
) -> None:
    calls: list[str] = []

    async def _fake_raw(**kwargs: Any) -> ProviderModelsDiscoverResult:
        calls.append(kwargs["base_url"])
        return ProviderModelsDiscoverResult(ok=True, provider_id=provider_id)

    monkeypatch.setattr(probe_module, "discover_provider_models", _fake_raw)

    result = _discover_selectable(provider_id=provider_id, base_url=base_url)

    assert result.ok is True
    assert calls == [base_url]


def test_discover_reports_missing_key_without_network(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = _discover(provider_id="openai")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.AUTH_INVALID.value
    assert "OPENAI_API_KEY" in result.detail
    assert result.models == []


def test_discover_rejects_unknown_provider_as_validation_error() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        _discover(provider_id="no-such-provider")


def test_discover_reports_build_failure_as_bad_request() -> None:
    # vllm requires an explicit base_url; building without one is a
    # configuration-shaped failure, not transport noise.
    result = _discover(provider_id="vllm", base_url="")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.BAD_REQUEST.value
    assert "base_url" in result.detail


def test_discover_lists_models_with_explicit_key(monkeypatch: Any) -> None:
    seen = _patch_response(monkeypatch, _models_response)
    result = _discover(provider_id="openai", api_key="sk-explicit")
    assert result.ok is True
    assert result.source == "live"
    assert [m["id"] for m in result.models] == ["test-model-a"]
    assert seen[0].headers["authorization"] == "Bearer sk-explicit"


def test_discover_resolves_key_from_provider_env(monkeypatch: Any) -> None:
    # Mirrors the probe: an unset explicit key falls back to the provider's
    # registry env key.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    seen = _patch_response(monkeypatch, _models_response)
    result = _discover(provider_id="openai")
    assert result.ok is True
    assert seen[0].headers["authorization"] == "Bearer sk-from-env"


def test_discover_classifies_rejected_key_as_auth_failure(monkeypatch: Any) -> None:
    """A 401 during listing is a wrong key, never ok=True/source='none'."""
    _patch_response(
        monkeypatch,
        lambda: httpx.Response(
            401,
            headers={"content-type": "application/json"},
            content=b'{"error": {"message": "Incorrect API key provided"}}',
        ),
    )
    result = _discover(provider_id="openai", api_key="sk-bad")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.AUTH_INVALID.value
    assert result.source == "none"
    assert result.models == []


def test_discover_classifies_connection_failure_as_transport_transient(
    monkeypatch: Any,
) -> None:
    _patch_transport_error(monkeypatch, httpx.ConnectError("connection refused"))
    result = _discover(provider_id="openai", api_key="sk-test")
    assert result.ok is False
    assert result.failure_kind == ProviderFailureKind.TRANSPORT_TRANSIENT.value
    assert result.models == []


def test_discover_empty_catalog_stays_ok_with_no_live_source(monkeypatch: Any) -> None:
    # Distinguishable from the auth failure above: the provider answered
    # successfully but lists nothing.
    _patch_response(
        monkeypatch,
        lambda: httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"data": []}',
        ),
    )
    result = _discover(provider_id="openai", api_key="sk-test")
    assert result.ok is True
    assert result.source == "none"
    assert result.models == []


def test_discover_row_context_window_prefers_user_override(monkeypatch: Any) -> None:
    """A per-model ``[models.*]`` context_window override beats the live listing.

    Discovery rows must show the window budgeting will actually use, so the
    operator-declared value wins even when the provider reports its own.
    """
    from opensquilla.provider.model_catalog import ModelCatalog, set_shared_catalog

    catalog = ModelCatalog()
    catalog.set_user_overrides({"openai/test-model-a": {"context_window": 32_000}})
    set_shared_catalog(catalog)
    try:
        _patch_response(monkeypatch, _models_response)
        result = _discover(provider_id="openai", api_key="sk-test")
    finally:
        set_shared_catalog(None)

    assert result.ok is True
    (row,) = result.models
    # The live listing said 64_000; the user override is authoritative.
    assert row["contextWindow"] == 32_000


async def test_discover_rpc_reuses_stored_credentials_when_blank(
    tmp_path, monkeypatch: Any
) -> None:
    """Blank credentials on the RPC reuse the stored config's for the same
    provider — upsert_llm_provider's "leave blank to keep current" semantics."""
    seen = _patch_response(monkeypatch, _models_response)
    cfg = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm=LlmProviderConfig(provider="openrouter", model="m", api_key="sk-stored"),
    )
    ctx = RpcContext(conn_id="t", config=cfg)

    payload = await rpc_onboarding._models_discover({"providerId": "openrouter"}, ctx)

    assert payload["ok"] is True
    assert seen[0].headers["authorization"] == "Bearer sk-stored"


async def test_discover_rpc_reuses_stored_key_for_same_origin_path_change(
    tmp_path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    seen = _patch_response(monkeypatch, _models_response)
    cfg = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm=LlmProviderConfig(
            provider="openrouter",
            model="m",
            api_key="sk-origin-a",
            base_url="https://openrouter.ai/api/v1",
        ),
    )
    ctx = RpcContext(conn_id="t", config=cfg)

    payload = await rpc_onboarding._models_discover(
        {
            "providerId": "openrouter",
            "baseUrl": "https://OPENROUTER.ai:443/alternate/v2",
        },
        ctx,
    )

    assert payload["ok"] is True
    assert seen[0].url.host == "openrouter.ai"
    assert seen[0].headers["authorization"] == "Bearer sk-origin-a"


async def test_discover_rpc_never_reuses_key_for_cross_origin_endpoint(
    tmp_path, monkeypatch: Any
) -> None:
    # The official subdomain remains selectable, so this exercises the
    # credential boundary rather than the catalog host allowlist.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-default-origin-a")
    seen = _patch_response(monkeypatch, _models_response)
    cfg = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm=LlmProviderConfig(
            provider="openrouter",
            model="m",
            api_key="sk-explicit-origin-a",
            base_url="https://openrouter.ai/api/v1",
        ),
    )
    ctx = RpcContext(conn_id="t", config=cfg)

    payload = await rpc_onboarding._models_discover(
        {
            "providerId": "openrouter",
            "baseUrl": "https://api.openrouter.ai/v1",
        },
        ctx,
    )

    assert payload["ok"] is False
    assert payload["failureKind"] == ProviderFailureKind.AUTH_INVALID.value
    assert seen == []


async def test_discover_rpc_never_reuses_stored_env_for_cross_origin_endpoint(
    tmp_path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OPENROUTER_ORIGIN_A_KEY", "sk-env-origin-a")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    seen = _patch_response(monkeypatch, _models_response)
    cfg = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm=LlmProviderConfig(
            provider="openrouter",
            model="m",
            api_key="",
            api_key_env="OPENROUTER_ORIGIN_A_KEY",
            base_url="https://openrouter.ai/api/v1",
        ),
    )
    ctx = RpcContext(conn_id="t", config=cfg)

    payload = await rpc_onboarding._models_discover(
        {
            "providerId": "openrouter",
            "baseUrl": "https://api.openrouter.ai/v1",
        },
        ctx,
    )

    assert payload["ok"] is False
    assert payload["failureKind"] == ProviderFailureKind.AUTH_INVALID.value
    assert seen == []


async def test_discover_rpc_custom_cross_origin_never_passes_stored_key(
    tmp_path, monkeypatch: Any
) -> None:
    captured: dict[str, Any] = {}

    async def fake_discover(**kwargs: Any) -> ProviderModelsDiscoverResult:
        captured.update(kwargs)
        return ProviderModelsDiscoverResult(ok=True, provider_id="custom")

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.discover_selectable_provider_models",
        fake_discover,
    )
    cfg = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm=LlmProviderConfig(
            provider="custom",
            model="m",
            api_key="sk-origin-a",
            base_url="https://a.example.test/v1",
        ),
    )
    ctx = RpcContext(conn_id="t", config=cfg)

    payload = await rpc_onboarding._models_discover(
        {
            "providerId": "custom",
            "baseUrl": "https://b.example.test/v1",
        },
        ctx,
    )

    assert payload["ok"] is True
    assert captured["api_key"] == ""
    assert captured["api_key_env"] == ""
    assert captured["base_url"] == "https://b.example.test/v1"
    assert captured["allow_default_api_key_env"] is False


async def test_discover_rpc_does_not_leak_stored_credentials_across_providers(
    tmp_path, monkeypatch: Any
) -> None:
    # The keep-current fallback is provider-bound: discovering a DIFFERENT
    # provider with blank credentials must not send the stored key.
    monkeypatch.delenv("TOKENRHYTHM_API_KEY", raising=False)
    cfg = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm=LlmProviderConfig(provider="openrouter", model="m", api_key="sk-stored"),
    )
    ctx = RpcContext(conn_id="t", config=cfg)

    payload = await rpc_onboarding._models_discover({"providerId": "tokenrhythm"}, ctx)

    assert payload["ok"] is False
    assert payload["failureKind"] == ProviderFailureKind.AUTH_INVALID.value


async def test_discover_rpc_explicit_credentials_override_stored(
    tmp_path, monkeypatch: Any
) -> None:
    seen = _patch_response(monkeypatch, _models_response)
    cfg = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm=LlmProviderConfig(provider="openrouter", model="m", api_key="sk-stored"),
    )
    ctx = RpcContext(conn_id="t", config=cfg)

    payload = await rpc_onboarding._models_discover(
        {"providerId": "openrouter", "apiKey": "sk-candidate"}, ctx
    )

    assert payload["ok"] is True
    assert seen[0].headers["authorization"] == "Bearer sk-candidate"
