from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.ollama import OllamaProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.openai_codex import OpenAICodexProvider
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.recovery_origin import transport_recovery_origin
from opensquilla.provider.types import ChatConfig, ErrorEvent, Message


@pytest.mark.parametrize(
    ("error", "origin"),
    [
        (httpx.ReadTimeout("synthetic"), "read_timeout"),
        (httpx.ReadError("synthetic"), "transport_disconnect"),
        (httpx.RemoteProtocolError("synthetic"), "transport_disconnect"),
        (ConnectionResetError(), "transport_disconnect"),
        (ConnectionAbortedError(), "transport_disconnect"),
        (BrokenPipeError(), "unknown"),
        (httpx.ConnectTimeout("synthetic"), "unknown"),
        (httpx.ConnectError("synthetic"), "unknown"),
        (httpx.WriteTimeout("synthetic"), "unknown"),
        (httpx.PoolTimeout("synthetic"), "unknown"),
        (httpx.LocalProtocolError("synthetic"), "unknown"),
        (httpx.CloseError("synthetic"), "unknown"),
        (TimeoutError("read timeout"), "unknown"),
        (TypeError("connection reset"), "unknown"),
        (ValueError("timeout"), "unknown"),
    ],
)
def test_recovery_requires_concrete_transport_evidence(error, origin) -> None:
    assert transport_recovery_origin(error) == origin


def test_recovery_origin_never_reads_exception_prose_or_causes() -> None:
    class UnrenderableError(TypeError):
        def __str__(self) -> str:
            raise AssertionError("must not render provider prose")

    error = UnrenderableError()
    error.__cause__ = httpx.ReadTimeout("synthetic")
    assert transport_recovery_origin(error) == "unknown"
    assert ErrorEvent(message="timeout", code="response_incomplete").recovery_origin == "unknown"


@pytest.mark.parametrize(
    "provider_kind", ["openai", "openai_responses", "anthropic", "ollama", "openai_codex"],
)
@pytest.mark.parametrize(
    ("error_type", "expected_origin"),
    [
        (httpx.ReadTimeout, "read_timeout"),
        (httpx.ReadError, "transport_disconnect"),
        (httpx.RemoteProtocolError, "transport_disconnect"),
        (httpx.ConnectTimeout, "unknown"),
        (httpx.WriteTimeout, "unknown"),
        (httpx.LocalProtocolError, "unknown"),
    ],
)
async def test_adapters_preserve_transport_origin(
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
    error_type: type[httpx.RequestError],
    expected_origin: str,
) -> None:
    monkeypatch.setattr(
        "opensquilla.provider.openai_codex.load_codex_credentials",
        lambda _path: SimpleNamespace(access_token="synthetic-token", account_id=""),
    )
    factories: dict[str, Any] = {
        "openai": OpenAIProvider,
        "openai_responses": OpenAIResponsesProvider,
        "anthropic": AnthropicProvider,
        "ollama": OllamaProvider,
        "openai_codex": OpenAICodexProvider,
    }
    provider = factories[provider_kind](api_key="synthetic-key", model="synthetic-model")
    real_client = httpx.AsyncClient

    def fail(request: httpx.Request) -> httpx.Response:
        raise error_type("synthetic transport diagnostic", request=request)

    transport = httpx.MockTransport(fail)

    def build_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real_client(*args, **{**kwargs, "transport": transport})

    monkeypatch.setattr(httpx, "AsyncClient", build_client)
    events = [
        event async for event in provider.chat(
            [Message(role="user", content="synthetic request")],
            config=ChatConfig(max_tokens=1, physical_attempt_limit=1),
        )
    ]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].recovery_origin == expected_origin
