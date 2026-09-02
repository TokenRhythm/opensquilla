"""Tests for the ``balance.query`` RPC and its provider balance backend."""

from __future__ import annotations

import httpx
import pytest

from opensquilla.gateway.balance_query import (
    BalanceQueryError,
    query_provider_balance,
)
from opensquilla.gateway.config import GatewayConfig, LlmProviderConfig
from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher


def _openrouter_transport(
    *, status: int = 200, payload: dict | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/credits")
        assert request.headers.get("Authorization") == "Bearer sk-or-v1-testkey"
        if payload is None:
            return httpx.Response(status)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_openrouter_balance_ok() -> None:
    transport = _openrouter_transport(
        payload={"data": {"total_credits": 10.0, "total_usage": 3.5}}
    )
    result = await query_provider_balance(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-v1-testkey",
        transport=transport,
    )
    assert result["status"] == "ok"
    assert result["provider"] == "openrouter"
    assert result["balance"] == pytest.approx(6.5)
    assert result["totalCredits"] == pytest.approx(10.0)
    assert result["totalUsage"] == pytest.approx(3.5)
    assert result["currency"] == "USD"


@pytest.mark.asyncio
async def test_openrouter_resolved_from_base_url_when_provider_generic() -> None:
    # provider="custom" but the endpoint is clearly OpenRouter → hint resolves it.
    transport = _openrouter_transport(
        payload={"data": {"total_credits": 5, "total_usage": 5}}
    )
    result = await query_provider_balance(
        provider="custom",
        base_url="https://openrouter.ai/api/v1",
        api_key="sk-or-v1-testkey",
        transport=transport,
    )
    assert result["status"] == "ok"
    assert result["balance"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_openrouter_http_error_raises() -> None:
    transport = _openrouter_transport(status=401, payload={"error": "unauthorized"})
    with pytest.raises(BalanceQueryError):
        await query_provider_balance(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-v1-testkey",
            transport=transport,
        )


@pytest.mark.asyncio
async def test_openrouter_malformed_payload_raises() -> None:
    transport = _openrouter_transport(payload={"data": {"total_credits": 10.0}})
    with pytest.raises(BalanceQueryError):
        await query_provider_balance(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-v1-testkey",
            transport=transport,
        )


@pytest.mark.asyncio
async def test_missing_api_key_raises() -> None:
    with pytest.raises(BalanceQueryError):
        await query_provider_balance(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="",
            api_key_env="",
        )


@pytest.mark.asyncio
async def test_api_key_resolved_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-testkey")
    transport = _openrouter_transport(
        payload={"data": {"total_credits": 2.0, "total_usage": 0.0}}
    )
    result = await query_provider_balance(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key="",
        api_key_env="OPENROUTER_API_KEY",
        transport=transport,
    )
    assert result["status"] == "ok"
    assert result["balance"] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_tokenrhythm_is_unsupported() -> None:
    result = await query_provider_balance(
        provider="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
        api_key="sk_tr_whatever_value_here",
    )
    assert result["status"] == "unsupported"
    assert result["provider"] == "tokenrhythm"
    assert result["balance"] is None


@pytest.mark.asyncio
async def test_local_custom_provider_is_unsupported() -> None:
    result = await query_provider_balance(
        provider="ollama",
        base_url="http://127.0.0.1:11434/v1",
        api_key="",
    )
    assert result["status"] == "unsupported"
    assert result["provider"] == "ollama"


@pytest.mark.asyncio
async def test_rpc_handler_dispatches_and_wraps_error() -> None:
    """The ``balance.query`` RPC handler reads ctx.config.llm and reports it.

    With no real network available, a supported provider surfaces the backend
    failure as an ``RpcUnavailableError`` rather than crashing.
    """
    dispatcher = get_dispatcher()
    entry = dispatcher.get_entry("balance.query")
    assert entry is not None
    handler = entry.handler

    # Unsupported provider → normal result, not an error.
    config = GatewayConfig()
    config.llm = LlmProviderConfig(
        provider="tokenrhythm",
        base_url="https://tokenrhythm.studio/v1",
        api_key="sk_tr_whatever_value_here",
    )
    ctx = RpcContext(conn_id="test", config=config)
    result = await handler(None, ctx)
    assert result["status"] == "unsupported"


@pytest.mark.asyncio
async def test_rpc_handler_no_provider_configured() -> None:
    dispatcher = get_dispatcher()
    entry = dispatcher.get_entry("balance.query")
    assert entry is not None
    handler = entry.handler
    ctx = RpcContext(conn_id="test", config=None)
    with pytest.raises(RpcUnavailableError):
        await handler(None, ctx)
