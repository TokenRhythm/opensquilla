"""Adapter-level contract for the model listing used by onboarding discovery.

Every runtime caller of ``list_models()`` relies on the historical
swallow-errors default (an unreachable or unauthorized provider degrades to
an empty list). Discovery instead needs failures surfaced so a wrong key and
an empty catalog stay distinguishable, which is what the keyword-only
``raise_on_error=True`` opt-in provides. Both sides are pinned here for the
three adapters that support it (offline, stubbed transport).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from opensquilla.provider.ollama import OllamaProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.protocol import ProviderModelListingResponseError

_ADAPTER_MODULES = {
    "openai": "opensquilla.provider.openai",
    "ollama": "opensquilla.provider.ollama",
    "openai_responses": "opensquilla.provider.openai_responses",
}


def _patch_response(monkeypatch: Any, module: str, response: httpx.Response) -> None:
    transport = httpx.MockTransport(lambda request: response)
    _patch_client(monkeypatch, module, transport)


def _patch_transport_error(monkeypatch: Any, module: str, exc: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    _patch_client(monkeypatch, module, httpx.MockTransport(handler))


def _patch_client(monkeypatch: Any, module: str, transport: httpx.MockTransport) -> None:
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(f"{_ADAPTER_MODULES[module]}.httpx.AsyncClient", patched_async_client)


def _build(module: str) -> Any:
    if module == "openai":
        return OpenAIProvider(api_key="sk-test", model="gpt-4o")
    if module == "ollama":
        return OllamaProvider(model="llama3")
    return OpenAIResponsesProvider(api_key="sk-test", model="gpt-5.5")


def _unauthorized() -> httpx.Response:
    return httpx.Response(
        401,
        headers={"content-type": "application/json"},
        content=b'{"error": {"message": "Incorrect API key provided"}}',
    )


def _garbled() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=b'{"data": [truncated',
    )


def _wrong_shape(module: str) -> httpx.Response:
    collection = "models" if module == "ollama" else "data"
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={collection: {"unexpected": "shape"}},
    )


@pytest.mark.parametrize("module", sorted(_ADAPTER_MODULES))
def test_list_models_default_swallows_http_errors(monkeypatch: Any, module: str) -> None:
    _patch_response(monkeypatch, module, _unauthorized())
    assert asyncio.run(_build(module).list_models()) == []


@pytest.mark.parametrize("module", sorted(_ADAPTER_MODULES))
def test_list_models_default_swallows_transport_errors(monkeypatch: Any, module: str) -> None:
    _patch_transport_error(monkeypatch, module, httpx.ConnectError("connection refused"))
    assert asyncio.run(_build(module).list_models()) == []


@pytest.mark.parametrize("module", sorted(_ADAPTER_MODULES))
def test_list_models_default_swallows_garbled_body(monkeypatch: Any, module: str) -> None:
    _patch_response(monkeypatch, module, _garbled())
    assert asyncio.run(_build(module).list_models()) == []


@pytest.mark.parametrize("module", sorted(_ADAPTER_MODULES))
def test_list_models_opt_in_raises_http_status_error(monkeypatch: Any, module: str) -> None:
    _patch_response(monkeypatch, module, _unauthorized())
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        asyncio.run(_build(module).list_models(raise_on_error=True))
    assert excinfo.value.response.status_code == 401


@pytest.mark.parametrize("module", sorted(_ADAPTER_MODULES))
def test_list_models_opt_in_raises_transport_error(monkeypatch: Any, module: str) -> None:
    _patch_transport_error(monkeypatch, module, httpx.ConnectError("connection refused"))
    with pytest.raises(httpx.ConnectError):
        asyncio.run(_build(module).list_models(raise_on_error=True))


@pytest.mark.parametrize("module", sorted(_ADAPTER_MODULES))
def test_list_models_opt_in_raises_on_garbled_body(monkeypatch: Any, module: str) -> None:
    _patch_response(monkeypatch, module, _garbled())
    with pytest.raises(ProviderModelListingResponseError) as excinfo:
        asyncio.run(_build(module).list_models(raise_on_error=True))
    assert excinfo.value.status_code == 200


@pytest.mark.parametrize("module", sorted(_ADAPTER_MODULES))
def test_list_models_opt_in_preserves_status_for_wrong_shape(
    monkeypatch: Any,
    module: str,
) -> None:
    _patch_response(monkeypatch, module, _wrong_shape(module))
    with pytest.raises(ProviderModelListingResponseError) as excinfo:
        asyncio.run(_build(module).list_models(raise_on_error=True))
    assert excinfo.value.status_code == 200
