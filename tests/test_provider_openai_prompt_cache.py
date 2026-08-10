from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from opensquilla.provider import openai as openai_module
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ChatConfig, DoneEvent, Message


def _openai_sse_body() -> bytes:
    chunks = [
        {
            "model": "test-model",
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": "test-model",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 5},
            },
        },
    ]
    body = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def _patch_openai_transport(monkeypatch, captured: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_openai_sse_body(),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _collect_done(
    provider: OpenAIProvider,
    cfg: ChatConfig,
    messages: list[Message] | None = None,
) -> DoneEvent:
    request_messages = messages if messages is not None else [Message(role="user", content="hi")]

    async def _collect() -> DoneEvent:
        done: DoneEvent | None = None
        async for ev in provider.chat(request_messages, config=cfg):
            if isinstance(ev, DoneEvent):
                done = ev
        assert done is not None
        return done

    return asyncio.run(_collect())


def test_provider_strips_trailing_paste_punctuation_from_api_key(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test-key、",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
    )

    _collect_done(provider, ChatConfig())

    assert captured["headers"]["Authorization"] == "Bearer test-key"


def test_openrouter_anthropic_auto_cache_adds_top_level_cache_control(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="anthropic/claude-opus-4.8",
        base_url="https://openrouter.ai/api/v1",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="auto",
    )

    done = _collect_done(provider, cfg)

    assert done.cached_tokens == 5
    headers = captured["headers"]
    assert headers["HTTP-Referer"] == "https://opensquilla.ai"
    assert headers["X-Title"] == "OpenSquilla"
    payload = captured["payload"]
    assert payload["cache_control"] == {"type": "ephemeral"}
    system_message = payload["messages"][0]
    assert system_message["role"] == "system"
    assert len(system_message["content"]) == 1
    assert system_message["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert system_message["content"][0]["text"] == "stable base"
    assert payload["messages"][1] == {"role": "user", "content": "hi"}


def test_openrouter_deepseek_auto_cache_does_not_add_top_level_cache_control(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-pro",
        base_url="https://openrouter.ai/api/v1",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="auto",
    )

    _collect_done(provider, cfg)

    payload = captured["payload"]
    assert "cache_control" not in payload
    assert len(payload["messages"][0]["content"]) == 1
    assert payload["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_dashscope_qwen36_flash_auto_cache_adds_message_cache_control(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="auto",
    )

    done = _collect_done(provider, cfg)

    assert done.cached_tokens == 5
    payload = captured["payload"]
    assert "cache_control" not in payload
    system_message = payload["messages"][0]
    assert system_message["role"] == "system"
    assert len(system_message["content"]) == 1
    assert system_message["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert system_message["content"][0]["text"] == "stable base"
    assert payload["messages"][1] == {"role": "user", "content": "hi"}


def test_dashscope_cache_on_without_system_still_marks_user(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        provider_kind="dashscope",
    )

    _collect_done(provider, ChatConfig(cache_mode="on"))

    assert captured["payload"]["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "hi",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]


def test_tokenrhythm_qwen37_max_on_adds_message_cache_control(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.7-max",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="on",
    )

    done = _collect_done(provider, cfg)

    assert done.cached_tokens == 5
    payload = captured["payload"]
    assert "cache_control" not in payload
    assert payload["messages"] == [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "stable base",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "hi",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
    ]


def test_tokenrhythm_qwen37_max_on_without_system_marks_initial_user(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.7-max",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )

    _collect_done(provider, ChatConfig(cache_mode="on"))

    assert captured["payload"]["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "hi",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
    ]


@pytest.mark.parametrize(
    ("model", "cache_mode"),
    [
        ("glm-5.2", "on"),
        ("qwen3.7-max", "auto"),
        ("qwen3.7-max", "off"),
    ],
)
def test_tokenrhythm_without_system_keeps_unsupported_or_non_on_requests_unmarked(
    monkeypatch,
    model: str,
    cache_mode: str,
) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model=model,
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )

    _collect_done(provider, ChatConfig(cache_mode=cache_mode))

    assert captured["payload"]["messages"] == [{"role": "user", "content": "hi"}]


def test_tokenrhythm_qwen37_max_auto_enables_system_cache_control(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.7-max",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="auto",
    )

    _collect_done(provider, cfg)

    assert captured["payload"]["messages"] == [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": "stable base",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        {"role": "user", "content": "hi"},
    ]


def test_tokenrhythm_non_qwen_cache_on_does_not_add_cache_control(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.2",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="on",
    )

    _collect_done(provider, cfg)

    assert captured["payload"]["messages"] == [
        {"role": "system", "content": "stable base"},
        {"role": "user", "content": "hi"},
    ]


def test_tokenrhythm_qwen37_max_cache_off_does_not_add_cache_control(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.7-max",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="off",
    )

    _collect_done(provider, cfg)

    assert captured["payload"]["messages"] == [
        {"role": "system", "content": "stable base"},
        {"role": "user", "content": "hi"},
    ]


def test_tokenrhythm_qwen37_max_on_never_exceeds_four_markers(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.7-max",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[
            {"text": f"stable block {index}", "cache": "true"}
            for index in range(5)
        ],
        cache_mode="on",
    )

    _collect_done(provider, cfg)

    messages = captured["payload"]["messages"]
    markers = [
        block["cache_control"]
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("cache_control")
    ]
    assert markers == [{"type": "ephemeral"}] * 4
    assert messages[0]["content"][4] == {"type": "text", "text": "stable block 4"}
    assert messages[1] == {"role": "user", "content": "hi"}


def test_tokenrhythm_qwen37_max_on_without_system_never_exceeds_four_markers(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.7-max",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    request_messages = [
        Message(role="user", content="initial issue"),
        Message(role="assistant", content="analysis 1"),
        Message(role="user", content="tool result 1"),
        Message(role="assistant", content="analysis 2"),
        Message(role="user", content="tool result 2"),
        Message(role="assistant", content="analysis 3"),
    ]

    _collect_done(
        provider,
        ChatConfig(cache_mode="on"),
        messages=request_messages,
    )

    messages = captured["payload"]["messages"]
    marker_positions = [
        (message_index, message["role"], block_index)
        for message_index, message in enumerate(messages)
        if isinstance(message.get("content"), list)
        for block_index, block in enumerate(message["content"])
        if block.get("cache_control") == {"type": "ephemeral"}
    ]
    assert marker_positions == [
        (0, "user", 0),
        (3, "assistant", 0),
        (4, "user", 0),
        (5, "assistant", 0),
    ]
    assert len(marker_positions) == 4


def test_tokenrhythm_cache_usage_log_contains_hashes_but_not_prompt_text(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.7-max",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )
    private_prompt = "private prompt must not appear in logs"
    cfg = ChatConfig(
        system=private_prompt,
        cache_breakpoints=[{"text": private_prompt, "cache": "true"}],
        cache_mode="on",
    )
    log_events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        openai_module.log,
        "info",
        lambda event, **payload: log_events.append((event, payload)),
    )

    _collect_done(provider, cfg)

    event = next(
        payload
        for name, payload in log_events
        if name == "tokenrhythm.prompt_cache_usage"
    )
    assert event["system_hash"]
    assert event["messages_prefix_hash"]
    assert event["explicit_cache_marker_count"] == 2
    assert private_prompt not in json.dumps(event)


def test_openrouter_qwen36_flash_auto_cache_adds_alibaba_message_cache_control(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen/qwen3.6-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="auto",
    )

    _collect_done(provider, cfg)

    payload = captured["payload"]
    assert "cache_control" not in payload
    assert payload["messages"][0] == {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": "stable base",
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    assert payload["messages"][1] == {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "hi",
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


def test_openrouter_unprefixed_qwen36_flash_auto_cache_adds_message_cache_control(
    monkeypatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="on",
    )

    _collect_done(provider, cfg)

    messages = captured["payload"]["messages"]
    marker_positions = [
        (message_index, message["role"], block_index)
        for message_index, message in enumerate(messages)
        if isinstance(message.get("content"), list)
        for block_index, block in enumerate(message["content"])
        if block.get("cache_control") == {"type": "ephemeral"}
    ]
    assert marker_positions == [
        (0, "system", 0),
        (1, "user", 0),
    ]


@pytest.mark.parametrize("cache_mode", ["auto", "on"])
def test_openrouter_qwen36_flash_without_system_keeps_existing_unmarked_shape(
    monkeypatch,
    cache_mode: str,
) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="qwen/qwen3.6-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )

    _collect_done(provider, ChatConfig(cache_mode=cache_mode))

    assert captured["payload"]["messages"] == [{"role": "user", "content": "hi"}]


def test_openrouter_zai_auto_cache_requires_live_capability_proof(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="z-ai/glm-5.1",
        base_url="https://openrouter.ai/api/v1",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="auto",
    )

    _collect_done(provider, cfg)

    payload = captured["payload"]
    assert "cache_control" not in payload
    assert payload["messages"][0] == {"role": "system", "content": "stable base"}


def test_openrouter_payload_cache_shape_logs_fixed_prefix_item_hashes(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-pro",
        base_url="https://openrouter.ai/api/v1",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="auto",
    )

    log_events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        openai_module.log,
        "debug",
        lambda event, **payload: log_events.append((event, payload)),
    )

    _collect_done(provider, cfg)

    event = next(
        payload
        for name, payload in log_events
        if name == "openrouter.payload_cache_shape"
    )
    assert event["first_non_system_hash"]
    assert event["non_system_prefix_item_hashes"] == [event["first_non_system_hash"]]


def test_openrouter_anthropic_cache_off_does_not_add_cache_control(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    _patch_openai_transport(monkeypatch, captured)
    provider = OpenAIProvider(
        api_key="test",
        model="anthropic/claude-opus-4.8",
        base_url="https://openrouter.ai/api/v1",
    )
    cfg = ChatConfig(
        system="stable base",
        cache_breakpoints=[{"text": "stable base", "cache": "true"}],
        cache_mode="off",
    )

    _collect_done(provider, cfg)

    payload = captured["payload"]
    assert "cache_control" not in payload
    assert payload["messages"][0] == {"role": "system", "content": "stable base"}
