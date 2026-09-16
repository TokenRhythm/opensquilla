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
from opensquilla.onboarding.probe import ProviderProbeResult
from opensquilla.provider.failures import ProviderFailureKind
from opensquilla.provider.types import DoneEvent


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
        assert kwargs["mode"] == "model"
        assert kwargs["timeout"] == 60.0
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


async def test_provider_probe_rpc_forwards_reachability_mode(
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
    ctx = _stored_openai_ctx(tmp_path)

    payload = await rpc_onboarding._provider_probe(
        {"providerId": "openai", "model": "gpt-4o", "mode": "reachability"},
        ctx,
    )

    assert payload["ok"] is True
    assert captured["mode"] == "reachability"
    assert captured["timeout"] == 60.0


async def test_primary_reachability_fallback_uses_saved_model_when_request_omits_it(
    tmp_path,
    monkeypatch: Any,
) -> None:
    request = httpx.Request("GET", "https://api.openai.com/v1/models")
    response = httpx.Response(404, request=request)
    selected_models: list[str] = []
    calls: list[str] = []

    class _Provider:
        provider_name = "openai"

        async def list_models(self, *, raise_on_error: bool = False) -> list[Any]:
            calls.append("list")
            raise httpx.HTTPStatusError(
                "404 Not Found",
                request=request,
                response=response,
            )

        def chat(self, messages: Any, tools: Any = None, config: Any = None) -> Any:
            calls.append("chat")

            async def stream() -> Any:
                yield DoneEvent()

            return stream()

    def build_provider(provider_id: str, model: str, **kwargs: Any) -> _Provider:
        selected_models.append(model)
        return _Provider()

    monkeypatch.setattr("opensquilla.onboarding.probe.build_provider", build_provider)

    payload = await rpc_onboarding._provider_probe(
        {"providerId": "openai", "mode": "reachability"},
        _stored_openai_ctx(tmp_path),
    )

    assert payload["ok"] is True
    assert payload["verificationLevel"] == "model_verified"
    assert selected_models == ["gpt-4o"]
    assert calls == ["list", "chat"]


@pytest.mark.parametrize("provider_id", ["openai", "custom"])
@pytest.mark.parametrize("model_fields", [{}, {"model": ""}], ids=["omitted", "empty"])
@pytest.mark.parametrize(
    ("status_code", "body", "expected_kind"),
    [
        (200, {"data": [{"id": "synthetic-model"}]}, ""),
        (200, {"data": []}, ""),
        (401, {"error": {"message": "Invalid API key"}}, "auth_invalid"),
        (404, {"error": {"message": "Not Found"}}, "unsupported_feature"),
    ],
    ids=["models", "empty-catalog", "invalid-key", "unsupported-listing"],
)
async def test_primary_reachability_without_model_uses_live_listing(
    tmp_path,
    monkeypatch: Any,
    provider_id: str,
    model_fields: dict[str, str],
    status_code: int,
    body: dict[str, Any],
    expected_kind: str,
) -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://model-list.example.test/v1/models"
        assert request.headers["authorization"] == "Bearer synthetic-probe-key"
        return httpx.Response(status_code, json=body)

    real_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(respond)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", client)
    ctx = _ctx(tmp_path, is_owner=True, llm=LlmProviderConfig())
    before = ctx.config.model_dump(mode="python")
    payload = await rpc_onboarding._provider_probe(
        {
            "providerId": provider_id,
            "apiKey": "synthetic-probe-key",
            "baseUrl": "https://model-list.example.test/v1",
            "mode": "reachability",
            **model_fields,
        },
        ctx,
    )

    assert len(seen) == 1
    assert payload["ok"] is (status_code == 200)
    assert payload["model"] == ""
    assert payload["failureKind"] == expected_kind
    assert payload["verificationLevel"] == "reachable"
    assert payload["failureStage"] == "reachability"
    assert payload["firstResponseMs"] is None
    assert ctx.config.model_dump(mode="python") == before
    assert not (tmp_path / "opensquilla.toml").exists()


@pytest.mark.parametrize("mode_fields", [{}, {"mode": None}])
async def test_legacy_primary_probe_still_requires_an_explicit_model(
    tmp_path,
    mode_fields: dict[str, Any],
) -> None:
    with pytest.raises(RpcHandlerError, match="Model is required"):
        await rpc_onboarding._provider_probe(
            {"providerId": "openai", **mode_fields},
            _stored_openai_ctx(tmp_path),
        )


async def test_reachability_only_result_does_not_update_model_probe_history(
    tmp_path, monkeypatch: Any
) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_probe_llm_provider(**kwargs: Any) -> ProviderProbeResult:
        return ProviderProbeResult(
            ok=True,
            provider_id="openai",
            model="gpt-4o",
            verification_level="reachable",
            failure_stage="reachability",
        )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.probe_llm_provider",
        fake_probe_llm_provider,
    )
    monkeypatch.setattr(
        "opensquilla.onboarding.probe_history.record_probe",
        lambda *args, **kwargs: recorded.append(dict(kwargs)),
    )

    payload = await rpc_onboarding._provider_probe(
        {"providerId": "openai", "model": "gpt-4o", "mode": "reachability"},
        _stored_openai_ctx(tmp_path),
    )

    assert payload["verificationLevel"] == "reachable"
    assert recorded == []


async def test_explicit_model_probe_is_diagnostic_but_legacy_probe_updates_history(
    tmp_path, monkeypatch: Any
) -> None:
    recorded: list[dict[str, Any]] = []

    async def fake_probe_llm_provider(**kwargs: Any) -> ProviderProbeResult:
        return ProviderProbeResult(
            ok=True,
            provider_id="openai",
            model="gpt-4o",
        )

    monkeypatch.setattr(
        "opensquilla.onboarding.probe.probe_llm_provider",
        fake_probe_llm_provider,
    )
    monkeypatch.setattr(
        "opensquilla.onboarding.probe_history.record_probe",
        lambda *args, **kwargs: recorded.append(dict(kwargs)),
    )
    ctx = _stored_openai_ctx(tmp_path)

    await rpc_onboarding._provider_probe(
        {"providerId": "openai", "model": "gpt-4o", "mode": "model"},
        ctx,
    )
    assert recorded == []

    await rpc_onboarding._provider_probe(
        {"providerId": "openai", "model": "gpt-4o"},
        ctx,
    )
    await rpc_onboarding._provider_probe(
        {"providerId": "openai", "model": "gpt-4o", "mode": None},
        ctx,
    )
    assert recorded == [
        {"ok": True, "failure_kind": ""},
        {"ok": True, "failure_kind": ""},
    ]


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
