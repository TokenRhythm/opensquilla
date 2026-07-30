"""RPC tests for provider probe fallback and credential status/reveal."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from opensquilla.gateway import rpc_onboarding
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.provider.failures import ProviderFailureKind


def _sse_ok_body() -> bytes:
    chunks = [
        {"choices": [{"delta": {"content": "pong"}, "finish_reason": None}]},
        {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def _probe_success_response() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_sse_ok_body(),
    )


def _patch_openai_response(
    monkeypatch: Any,
) -> tuple[list[httpx.Request], list[dict[str, Any]]]:
    seen_requests: list[httpx.Request] = []
    seen_client_kwargs: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return _probe_success_response()

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        seen_client_kwargs.append(dict(kwargs))
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    return seen_requests, seen_client_kwargs


class _ProbePayload:
    def to_payload(self) -> dict[str, bool]:
        return {"ok": True}


def _stored_openai_ctx(
    tmp_path,
    *,
    api_key: str = "sk-stored",
    api_key_env: str = "",
    base_url: str | None = None,
    proxy: str = "",
) -> RpcContext:
    llm_kwargs = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key": api_key,
        "api_key_env": api_key_env,
        "proxy": proxy,
    }
    if base_url is not None:
        llm_kwargs["base_url"] = base_url
    cfg = GatewayConfig(
        config_path=str(tmp_path / "opensquilla.toml"),
        llm=LlmProviderConfig(**llm_kwargs),
    )
    return RpcContext(conn_id="t", config=cfg)


def _ctx(
    tmp_path,
    *,
    is_owner: bool,
    llm: LlmProviderConfig,
) -> RpcContext:
    scopes = frozenset({"operator.admin"}) if is_owner else frozenset({"operator.read"})
    return RpcContext(
        conn_id="t",
        principal=Principal(
            role="operator",
            scopes=scopes,
            is_owner=is_owner,
            authenticated=True,
        ),
        config=GatewayConfig(
            config_path=str(tmp_path / "opensquilla.toml"),
            llm=llm,
        ),
    )


async def test_provider_probe_rpc_reuses_stored_credentials_when_blank(
    tmp_path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen, _ = _patch_openai_response(monkeypatch)
    ctx = _stored_openai_ctx(tmp_path)

    payload = await rpc_onboarding._provider_probe({"providerId": "openai", "model": "gpt-4o"}, ctx)

    assert payload["ok"] is True
    assert seen[0].headers["authorization"] == "Bearer sk-stored"


async def test_provider_probe_rpc_binds_synthetic_usage_scope(
    tmp_path, monkeypatch: Any
) -> None:
    from opensquilla.engine.usage_accounting import current_usage_accounting_scope

    observed = []

    async def fake_probe_llm_provider(**kwargs: Any) -> _ProbePayload:
        scope = current_usage_accounting_scope()
        assert scope is not None
        assert callable(kwargs.get("chat_stream_factory"))
        observed.append(scope.context)
        return _ProbePayload()

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.probe_llm_provider",
        fake_probe_llm_provider,
    )
    ctx = _stored_openai_ctx(tmp_path)
    ctx.usage_event_sink = object()

    payload = await rpc_onboarding._provider_probe(
        {"providerId": "openai", "model": "gpt-4o"},
        ctx,
    )

    assert payload["ok"] is True
    assert observed[0].run_kind == "onboarding_probe"
    assert observed[0].session_id


async def test_provider_probe_rpc_reuses_stored_base_url_and_proxy_when_blank(
    tmp_path, monkeypatch: Any
) -> None:
    captured: dict[str, Any] = {}

    async def fake_probe_llm_provider(**kwargs: Any) -> _ProbePayload:
        captured.update(kwargs)
        return _ProbePayload()

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.probe_llm_provider",
        fake_probe_llm_provider,
    )
    ctx = _stored_openai_ctx(
        tmp_path,
        base_url="https://stored.example/api/v1",
        proxy="http://127.0.0.1:9876",
    )

    payload = await rpc_onboarding._provider_probe({"providerId": "openai", "model": "gpt-4o"}, ctx)

    assert payload["ok"] is True
    assert captured["base_url"] == "https://stored.example/api/v1"
    assert captured["proxy"] == "http://127.0.0.1:9876"


async def test_provider_probe_rpc_reuses_stored_api_key_env_when_blank(
    tmp_path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_STORED_TEST_KEY", "sk-from-stored-env")
    seen, _ = _patch_openai_response(monkeypatch)
    ctx = _stored_openai_ctx(
        tmp_path,
        api_key="",
        api_key_env="OPENAI_STORED_TEST_KEY",
    )

    payload = await rpc_onboarding._provider_probe({"providerId": "openai", "model": "gpt-4o"}, ctx)

    assert payload["ok"] is True
    assert seen[0].headers["authorization"] == "Bearer sk-from-stored-env"


async def test_provider_probe_rpc_reuses_stored_key_for_same_origin_path_change(
    tmp_path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)
    seen, _ = _patch_openai_response(monkeypatch)
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="custom",
            model="test-model",
            api_key="sk-origin-a",
            base_url="https://a.example.test/v1",
        ),
    )

    payload = await rpc_onboarding._provider_probe(
        {
            "providerId": "custom",
            "model": "test-model",
            "baseUrl": "https://A.example.test:443/alternate/v2",
        },
        ctx,
    )

    assert payload["ok"] is True
    assert seen[0].url.host == "a.example.test"
    assert seen[0].headers["authorization"] == "Bearer sk-origin-a"


async def test_provider_probe_rpc_never_reuses_key_for_cross_origin_endpoint(
    tmp_path, monkeypatch: Any
) -> None:
    # Even the provider's default env fallback holds A's key; changing to B
    # must suppress both the stored explicit key and that implicit fallback.
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "sk-default-origin-a")
    seen, _ = _patch_openai_response(monkeypatch)
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="custom",
            model="test-model",
            api_key="sk-explicit-origin-a",
            base_url="https://a.example.test/v1",
        ),
    )

    payload = await rpc_onboarding._provider_probe(
        {
            "providerId": "custom",
            "model": "test-model",
            "baseUrl": "https://b.example.test/v1",
        },
        ctx,
    )

    assert payload["ok"] is True
    assert seen[0].url.host == "b.example.test"
    authorization = seen[0].headers.get("authorization", "")
    assert "sk-explicit-origin-a" not in authorization
    assert "sk-default-origin-a" not in authorization


async def test_provider_probe_rpc_never_reuses_stored_env_for_cross_origin_endpoint(
    tmp_path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("CUSTOM_ORIGIN_A_KEY", "sk-env-origin-a")
    monkeypatch.delenv("CUSTOM_LLM_API_KEY", raising=False)
    seen, _ = _patch_openai_response(monkeypatch)
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="custom",
            model="test-model",
            api_key="",
            api_key_env="CUSTOM_ORIGIN_A_KEY",
            base_url="https://a.example.test/v1",
        ),
    )

    payload = await rpc_onboarding._provider_probe(
        {
            "providerId": "custom",
            "model": "test-model",
            "baseUrl": "https://b.example.test/v1",
        },
        ctx,
    )

    assert payload["ok"] is True
    assert seen[0].url.host == "b.example.test"
    assert "sk-env-origin-a" not in seen[0].headers.get("authorization", "")


async def test_provider_probe_rpc_does_not_leak_stored_credentials_across_providers(
    tmp_path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")
    seen, _ = _patch_openai_response(monkeypatch)
    ctx = _stored_openai_ctx(tmp_path)

    payload = await rpc_onboarding._provider_probe(
        {"providerId": "deepseek", "model": "deepseek-v4-flash"}, ctx
    )

    authorization_headers = [request.headers.get("authorization", "") for request in seen]
    assert payload["ok"] is True
    assert seen
    assert authorization_headers == ["Bearer sk-deepseek"]
    assert "Bearer sk-stored" not in authorization_headers


async def test_provider_probe_rpc_reports_missing_key_for_other_provider_without_leak(
    tmp_path, monkeypatch: Any
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    seen, _ = _patch_openai_response(monkeypatch)
    ctx = _stored_openai_ctx(tmp_path)

    payload = await rpc_onboarding._provider_probe(
        {"providerId": "deepseek", "model": "deepseek-v4-flash"}, ctx
    )

    assert payload["ok"] is False
    assert payload["failureKind"] == ProviderFailureKind.AUTH_INVALID.value
    assert seen == []


async def test_provider_probe_rpc_explicit_credentials_override_stored(
    tmp_path, monkeypatch: Any
) -> None:
    seen, _ = _patch_openai_response(monkeypatch)
    ctx = _stored_openai_ctx(tmp_path)

    payload = await rpc_onboarding._provider_probe(
        {"providerId": "openai", "model": "gpt-4o", "apiKey": "sk-candidate"}, ctx
    )

    assert payload["ok"] is True
    assert seen[0].headers["authorization"] == "Bearer sk-candidate"


def test_status_payload_owner_can_reveal_explicit_key(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="sk-deepseek-secret-123456",
            base_url="https://api.deepseek.com",
        ),
    )

    payload = rpc_onboarding._status_payload(ctx)

    assert payload["llmCredentialStatus"]["revealAllowed"] is True


def test_status_payload_non_owner_cannot_reveal_explicit_key(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        is_owner=False,
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="sk-deepseek-secret-123456",
            base_url="https://api.deepseek.com",
        ),
    )

    payload = rpc_onboarding._status_payload(ctx)

    assert payload["llmCredentialStatus"]["revealAllowed"] is False


def test_status_payload_does_not_contain_raw_secret_string(tmp_path) -> None:
    raw_secret = "sk-deepseek-secret-123456"
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key=raw_secret,
            base_url="https://api.deepseek.com",
        ),
    )

    payload = rpc_onboarding._status_payload(ctx)

    assert raw_secret not in json.dumps(payload, sort_keys=True)


@pytest.mark.asyncio
async def test_provider_credential_reveal_returns_explicit_saved_key(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="sk-deepseek-secret-123456",
            base_url="https://api.deepseek.com",
        ),
    )

    payload = await rpc_onboarding._provider_credential_reveal(
        {"providerId": "deepseek"},
        ctx,
    )

    assert payload == {
        "ok": True,
        "provider": "deepseek",
        "source": "explicit",
        "envKey": "DEEPSEEK_API_KEY",
        "apiKey": "sk-deepseek-secret-123456",
    }


@pytest.mark.asyncio
async def test_optional_custom_credential_status_and_reveal_use_saved_key(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="custom",
            model="test-model",
            api_key="sk-custom-secret-123456",
            base_url="https://custom.example.test/v1",
        ),
    )

    status = rpc_onboarding._status_payload(ctx)["llmCredentialStatus"]
    payload = await rpc_onboarding._provider_credential_reveal(
        {"providerId": "custom"},
        ctx,
    )

    assert status["available"] is True
    assert status["source"] == "explicit"
    assert status["masked"].endswith("3456")
    assert status["revealAllowed"] is True
    assert payload == {
        "ok": True,
        "provider": "custom",
        "source": "explicit",
        "envKey": "CUSTOM_LLM_API_KEY",
        "apiKey": "sk-custom-secret-123456",
    }


@pytest.mark.asyncio
async def test_provider_credential_reveal_returns_env_key_value(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-env-654321")
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="",
            api_key_env="DEEPSEEK_API_KEY",
            base_url="https://api.deepseek.com",
        ),
    )

    payload = await rpc_onboarding._provider_credential_reveal(
        {"providerId": "deepseek"},
        ctx,
    )

    assert payload == {
        "ok": True,
        "provider": "deepseek",
        "source": "env",
        "envKey": "DEEPSEEK_API_KEY",
        "apiKey": "sk-deepseek-env-654321",
    }


@pytest.mark.asyncio
async def test_provider_credential_reveal_ignores_runtime_secret_cache_when_env_missing(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="sk-runtime-cache",
            api_key_env="DEEPSEEK_API_KEY",
            base_url="https://api.deepseek.com",
        ),
    )
    ctx.config.mark_runtime_secret("llm.api_key")

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_onboarding._provider_credential_reveal({"providerId": "deepseek"}, ctx)

    assert excinfo.value.code == "onboarding.provider.credential.unavailable"


@pytest.mark.asyncio
async def test_provider_credential_reveal_prefers_env_over_runtime_secret_cache(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-current")
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="sk-runtime-cache",
            api_key_env="DEEPSEEK_API_KEY",
            base_url="https://api.deepseek.com",
        ),
    )
    ctx.config.mark_runtime_secret("llm.api_key")

    payload = await rpc_onboarding._provider_credential_reveal(
        {"providerId": "deepseek"},
        ctx,
    )

    assert payload == {
        "ok": True,
        "provider": "deepseek",
        "source": "env",
        "envKey": "DEEPSEEK_API_KEY",
        "apiKey": "sk-env-current",
    }


@pytest.mark.asyncio
async def test_provider_credential_reveal_denies_non_owner(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        is_owner=False,
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="sk-deepseek-secret-123456",
            base_url="https://api.deepseek.com",
        ),
    )

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_onboarding._provider_credential_reveal({"providerId": "deepseek"}, ctx)

    assert excinfo.value.code == "onboarding.provider.credential.not_owner"


@pytest.mark.asyncio
async def test_provider_credential_reveal_denies_inactive_provider(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="deepseek",
            model="deepseek-v4-flash",
            api_key="sk-deepseek-secret-123456",
            base_url="https://api.deepseek.com",
        ),
    )

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_onboarding._provider_credential_reveal({"providerId": "openai"}, ctx)

    assert excinfo.value.code == "onboarding.provider.credential.inactive_provider"


@pytest.mark.asyncio
async def test_provider_credential_reveal_rejects_unsupported_active_provider(tmp_path) -> None:
    ctx = _ctx(
        tmp_path,
        is_owner=True,
        llm=LlmProviderConfig(
            provider="no-such-provider",
            model="m",
            api_key="sk-unsupported",
            base_url="https://example.invalid",
        ),
    )

    with pytest.raises(RpcHandlerError) as excinfo:
        await rpc_onboarding._provider_credential_reveal(
            {"providerId": "no-such-provider"},
            ctx,
        )

    assert excinfo.value.code == "onboarding.provider.credential.unsupported_provider"
