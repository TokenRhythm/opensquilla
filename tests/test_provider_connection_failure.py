from __future__ import annotations

import ssl

import httpx
import pytest

from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.codex_auth import CodexCredentials
from opensquilla.provider.failures import (
    CONNECTION_FAILED_CODE,
    ProviderFailureKind,
    classify_provider_error,
    is_connection_failure,
)
from opensquilla.provider.ollama import OllamaProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.openai_codex import OpenAICodexProvider
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.types import ChatConfig, ErrorEvent, Message


@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        (httpx.ConnectError, True),
        (httpx.ConnectTimeout, True),
        (httpx.ReadTimeout, False),
        (httpx.WriteTimeout, False),
        (httpx.PoolTimeout, False),
        (httpx.RemoteProtocolError, False),
        (httpx.ProxyError, False),
        (RuntimeError, False),
    ],
)
def test_connection_failure_uses_exception_type(error_type, expected) -> None:
    assert is_connection_failure(error_type("connection timeout")) is expected


def test_tls_connect_error_requires_configuration_repair() -> None:
    error = httpx.ConnectError("connection failed")
    error.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    assert not is_connection_failure(error)


def test_connection_failure_code_retains_finite_legacy_retry_classification() -> None:
    assert classify_provider_error(
        "openai", None, raw_code=CONNECTION_FAILED_CODE
    ) is ProviderFailureKind.TRANSPORT_TRANSIENT


@pytest.mark.parametrize(
    "message",
    [
        "insufficient_quota: quota exhausted",
        "You exceeded your current quota, please check your plan and billing details.",
    ],
)
def test_billing_quota_takes_precedence_over_http_rate_limit(message) -> None:
    assert classify_provider_error(
        "openai", 429, raw_code="429", message=message
    ) is ProviderFailureKind.INSUFFICIENT_CREDITS




@pytest.mark.parametrize(
    "provider_type",
    [
        OpenAIProvider,
        AnthropicProvider,
        OllamaProvider,
        OpenAIResponsesProvider,
        OpenAICodexProvider,
    ],
)
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_adapters_preserve_connection_type_with_one_physical_attempt(
    monkeypatch, provider_type, error_type
) -> None:
    calls = []
    original_client = httpx.AsyncClient

    def request(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise error_type("temporary connection failure", request=request)

    def client(*args, **kwargs):
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(request)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(
        "opensquilla.provider.openai_codex.load_codex_credentials",
        lambda *_args, **_kwargs: CodexCredentials(access_token="dummy-access"),
    )
    provider_kwargs = {"api_key": "dummy-key", "base_url": "https://provider.test"}
    if provider_type is OpenAIProvider:
        provider_kwargs["provider_kind"] = "openrouter"
    provider = provider_type(**provider_kwargs)
    events = [
        event
        async for event in provider.chat(
            [Message(role="user", content="hi")],
            config=ChatConfig(physical_attempt_limit=1),
        )
    ]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(calls) == 1
    assert [event.code for event in errors] == [CONNECTION_FAILED_CODE]
