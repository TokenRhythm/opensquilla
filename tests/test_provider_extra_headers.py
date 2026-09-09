"""Operator-defined extra request headers for OpenAI-family providers.

Some routing gateways in front of a model API require extra headers (e.g.
opencode Zen/Go answers ``400 MissingSessionID`` on ``/responses`` without
``x-opencode-session``). ``extra_headers`` carries them from provider config
to the wire; adapter-managed framing (Authorization/Content-Type/Accept)
always wins. Every test below runs offline against a mocked transport.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from opensquilla.gateway.config import LlmProviderConfig
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider.openai import OpenAIProvider, merge_extra_headers
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import (
    ProviderConfig,
    _build_context,
    _build_openai_compat,
    _build_openai_responses,
)

EXTRA = {"x-opencode-session": "session-under-test"}


def _capture_transport(
    monkeypatch: Any,
    module: str,
    response: httpx.Response,
    calls: list[httpx.Request],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return response

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        f"opensquilla.provider.{module}.httpx.AsyncClient",
        patched_async_client,
    )


def _responses_completed() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "resp_test",
            "model": "m",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {
                "input_tokens": 5,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 2,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 7,
            },
        },
    )


def _chat_sse_text(text: str) -> bytes:
    chunks = [
        {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def test_merge_extra_headers_passes_custom_headers() -> None:
    headers = {"Authorization": "Bearer k"}
    out = merge_extra_headers(headers, EXTRA)
    assert out["x-opencode-session"] == "session-under-test"
    assert out["Authorization"] == "Bearer k"


def test_merge_extra_headers_protects_reserved_names() -> None:
    headers = {
        "Authorization": "Bearer k",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    out = merge_extra_headers(
        headers,
        {
            "Authorization": "Bearer evil",
            "CONTENT-TYPE": "text/plain",
            "accept": "text/html",
            "x-opencode-session": "s",
        },
    )
    assert out["Authorization"] == "Bearer k"
    assert out["Content-Type"] == "application/json"
    assert out["Accept"] == "application/json"
    assert out["x-opencode-session"] == "s"


def test_merge_extra_headers_none_safe() -> None:
    headers = {"Authorization": "Bearer k"}
    assert merge_extra_headers(headers, None) == {"Authorization": "Bearer k"}


def test_responses_chat_sends_extra_headers(monkeypatch: Any) -> None:
    calls: list[httpx.Request] = []
    _capture_transport(
        monkeypatch, "openai_responses", _responses_completed(), calls
    )
    provider = OpenAIResponsesProvider(
        api_key="test", model="m", extra_headers=dict(EXTRA)
    )

    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")], config=ChatConfig()
            )
        ]

    asyncio.run(_run())
    assert calls, "expected at least one upstream request"
    sent = calls[-1].headers
    assert sent["x-opencode-session"] == "session-under-test"
    assert sent["authorization"] == "Bearer test"


def test_responses_list_models_sends_extra_headers(monkeypatch: Any) -> None:
    calls: list[httpx.Request] = []
    _capture_transport(
        monkeypatch,
        "openai_responses",
        httpx.Response(200, json={"data": []}),
        calls,
    )
    provider = OpenAIResponsesProvider(
        api_key="test", model="m", extra_headers=dict(EXTRA)
    )
    asyncio.run(provider.list_models())
    assert calls, "expected a catalog request"
    assert calls[-1].headers["x-opencode-session"] == "session-under-test"


def test_chat_completions_sends_extra_headers(monkeypatch: Any) -> None:
    calls: list[httpx.Request] = []
    _capture_transport(
        monkeypatch,
        "openai",
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_chat_sse_text("ok"),
        ),
        calls,
    )
    provider = OpenAIProvider(
        api_key="k", model="m", provider_kind="openai", extra_headers=dict(EXTRA)
    )

    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")], config=ChatConfig()
            )
        ]

    asyncio.run(_run())
    assert calls, "expected at least one upstream request"
    sent = calls[-1].headers
    assert sent["x-opencode-session"] == "session-under-test"
    assert sent["authorization"] == "Bearer k"


def test_chat_list_models_sends_extra_headers(monkeypatch: Any) -> None:
    calls: list[httpx.Request] = []
    _capture_transport(
        monkeypatch,
        "openai",
        httpx.Response(200, json={"data": []}),
        calls,
    )
    provider = OpenAIProvider(
        api_key="k", model="m", provider_kind="openai", extra_headers=dict(EXTRA)
    )
    asyncio.run(provider.list_models())
    assert calls, "expected a catalog request"
    assert calls[-1].headers["x-opencode-session"] == "session-under-test"


def test_selector_plumbing_carries_extra_headers() -> None:
    spec = get_provider_spec("openai_responses")
    cfg = ProviderConfig(
        provider="openai_responses",
        model="m",
        api_key="k",
        extra_headers=dict(EXTRA),
    )
    ctx = _build_context(cfg, spec)
    assert ctx.extra_headers == EXTRA
    provider = _build_openai_responses(ctx)
    assert isinstance(provider, OpenAIResponsesProvider)
    assert provider._extra_headers == EXTRA


def test_compat_factory_carries_extra_headers() -> None:
    spec = get_provider_spec("openai")
    cfg = ProviderConfig(
        provider="openai", model="m", api_key="k", extra_headers=dict(EXTRA)
    )
    ctx = _build_context(cfg, spec)
    provider = _build_openai_compat(ctx)
    assert isinstance(provider, OpenAIProvider)
    assert provider._extra_headers == EXTRA


def test_llm_provider_config_accepts_extra_headers() -> None:
    assert LlmProviderConfig().extra_headers == {}
    cfg = LlmProviderConfig(
        provider="openai_responses",
        model="m",
        extra_headers={"x-opencode-session": "s"},
    )
    assert cfg.extra_headers == {"x-opencode-session": "s"}
