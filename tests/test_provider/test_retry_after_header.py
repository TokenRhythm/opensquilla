"""Retry-After header threading: adapter 429/5xx errors carry parsed seconds.

Fully offline via ``httpx.MockTransport`` — same pattern as the golden
harness. Synthetic credentials and models only.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from opensquilla.provider.codex_auth import CodexCredentials
from opensquilla.provider.selector import build_provider
from opensquilla.provider.types import ChatConfig, ErrorEvent, Message

FAKE_API_KEY = "sk-test-000"


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, response_factory: Any
) -> None:
    transport = httpx.MockTransport(lambda request: response_factory())
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched_async_client)


async def _chat_errors(provider: Any) -> list[ErrorEvent]:
    messages = [Message(role="user", content="ping")]
    events = [
        event
        async for event in provider.chat(messages, tools=None, config=ChatConfig())
    ]
    return [event for event in events if isinstance(event, ErrorEvent)]


async def test_openai_compat_429_carries_retry_after_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transport(
        monkeypatch,
        lambda: httpx.Response(
            429,
            headers={"Retry-After": "7"},
            json={"error": {"message": "synthetic rate limit"}},
        ),
    )
    provider = build_provider("openai", "test-chat-model", api_key=FAKE_API_KEY)
    errors = await _chat_errors(provider)
    assert errors
    assert errors[0].code == "429"
    assert errors[0].retry_after_s == 7.0


async def test_openai_compat_503_carries_retry_after_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transport(
        monkeypatch,
        lambda: httpx.Response(
            503,
            headers={"Retry-After": "12"},
            json={"error": {"message": "synthetic overload"}},
        ),
    )
    provider = build_provider("openai", "test-chat-model", api_key=FAKE_API_KEY)
    errors = await _chat_errors(provider)
    assert errors
    assert errors[0].code == "503"
    assert errors[0].retry_after_s == 12.0


async def test_openai_compat_400_never_carries_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transport(
        monkeypatch,
        lambda: httpx.Response(
            400,
            headers={"Retry-After": "7"},
            json={"error": {"message": "synthetic bad request"}},
        ),
    )
    provider = build_provider("openai", "test-chat-model", api_key=FAKE_API_KEY)
    errors = await _chat_errors(provider)
    assert errors
    assert errors[0].code == "400"
    assert errors[0].retry_after_s is None


async def test_anthropic_429_carries_retry_after_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transport(
        monkeypatch,
        lambda: httpx.Response(
            429,
            headers={"Retry-After": "9"},
            json={"error": {"type": "rate_limit_error", "message": "synthetic"}},
        ),
    )
    provider = build_provider("anthropic", "claude-sonnet-4-6", api_key=FAKE_API_KEY)
    errors = await _chat_errors(provider)
    assert errors
    assert errors[0].code == "429"
    assert errors[0].retry_after_s == 9.0


async def test_missing_header_leaves_retry_after_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transport(
        monkeypatch,
        lambda: httpx.Response(
            429,
            json={"error": {"message": "synthetic rate limit"}},
        ),
    )
    provider = build_provider("openai", "test-chat-model", api_key=FAKE_API_KEY)
    errors = await _chat_errors(provider)
    assert errors
    assert errors[0].code == "429"
    assert errors[0].retry_after_s is None


@pytest.mark.parametrize("provider_id", ["openai_codex", "ollama", "openai_responses"])
@pytest.mark.parametrize(
    "status,header,expected",
    [(429, "60", 60.0), (503, "12", 12.0), (400, "60", None), (429, None, None)],
)
async def test_other_adapters_preserve_retry_after_without_changing_status(
    monkeypatch, provider_id, status, header, expected
):
    _patch_transport(
        monkeypatch,
        lambda: httpx.Response(
            status,
            headers={"Retry-After": header} if header is not None else {},
            json={"error": {"message": "synthetic upstream failure"}},
        ),
    )
    monkeypatch.setattr(
        "opensquilla.provider.openai_codex.load_codex_credentials",
        lambda *_args: CodexCredentials(access_token="dummy-oauth"),
    )
    provider = build_provider(provider_id, "test-chat-model", api_key=FAKE_API_KEY)
    errors = await _chat_errors(provider)
    assert len(errors) == 1
    assert errors[0].code == str(status)
    assert errors[0].retry_after_s == expected
