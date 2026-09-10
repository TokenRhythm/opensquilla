"""Native continuation state survives decoding, serialization, and projection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.engine.thinking import drop_reasoning
from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.openai import OpenAIProvider, _openai_replay_source
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolUse,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelCapabilities,
    ProviderReplayState,
    ReasoningDeltaEvent,
    TextDeltaEvent,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

_MODEL = "anthropic/synthetic-reasoning-model"
_BASE_URL = "https://openrouter.ai/api/v1"
_DETAILS = [
    {
        "type": "reasoning.text",
        "text": "Consider the synthetic input.",
        "signature": "synthetic-signature",
        "id": "reasoning-1",
        "format": "anthropic-claude-v1",
        "index": 0,
    },
    {
        "type": "reasoning.encrypted",
        "data": "synthetic-opaque-data",
        "id": "reasoning-2",
        "format": "anthropic-claude-v1",
        "index": 1,
    },
    {"type": "reasoning.summary", "summary": "Synthetic summary.", "index": 2},
]


def _provider(**kwargs: Any) -> OpenAIProvider:
    return OpenAIProvider(
        api_key="synthetic-key",
        model=kwargs.pop("model", _MODEL),
        base_url=kwargs.pop("base_url", _BASE_URL),
        provider_kind=kwargs.pop("provider_kind", "openrouter"),
        **kwargs,
    )


def _config() -> ChatConfig:
    return ChatConfig(
        thinking=True,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True, supports_tools=True, reasoning_format="openrouter"
        ),
    )


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", factory)


def _sse(deltas: list[dict[str, Any]], *, final: dict[str, Any] | None = None) -> bytes:
    frames = [
        {"model": _MODEL + "-resolved", "choices": [{"delta": delta, "finish_reason": None}]}
        for delta in deltas
    ]
    frames.append(final or {"choices": [{"delta": {}, "finish_reason": "stop"}]})
    return b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames) + (
        b"data: [DONE]\n\n"
    )


@pytest.mark.parametrize("stream", [True, False])
async def test_native_reasoning_roundtrip_preserves_order_and_source(
    monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    response_message = {
        "content": "Synthetic answer.",
        "reasoning": _DETAILS[0]["text"],
        "reasoning_details": _DETAILS,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if stream:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse([
                    {"reasoning": _DETAILS[0]["text"], "reasoning_details": _DETAILS[:1]},
                    {"reasoning_details": _DETAILS[1:]},
                    {"content": "Synthetic answer."},
                ]),
            )
        return httpx.Response(200, json={
            "model": _MODEL + "-resolved",
            "choices": [{"message": response_message, "finish_reason": "stop"}],
        })

    _patch_transport(monkeypatch, handler)
    provider = _provider()
    if stream:
        events = [event async for event in provider.chat(
            [Message(role="user", content="Synthetic input.")], config=_config()
        )]
    else:
        events = [event async for event in provider._complete_non_stream(
            payload={"model": _MODEL, "messages": []}, headers={}, cfg=_config(),
            tools=None, timeout_exc=httpx.ReadTimeout("synthetic timeout"),
        )]
    assert not [event for event in events if isinstance(event, ErrorEvent)]
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.reasoning_content == _DETAILS[0]["text"]
    assert "".join(event.text for event in events if isinstance(event, ReasoningDeltaEvent)) == (
        done.reasoning_content
    )
    assert done.provider_replay is not None
    assert done.provider_replay.reasoning_details == _DETAILS
    assert done.provider_replay.model == _MODEL
    assert done.model == _MODEL + "-resolved"

    stored = Message(role="assistant", content="Synthetic answer.",
                     reasoning_content=done.reasoning_content, provider_replay=done.provider_replay)
    restored = Message.model_validate_json(stored.model_dump_json())
    assert provider.can_replay_reasoning(restored)
    payload, *_ = provider._build_payload([restored], None, _config())
    assert payload["messages"][0]["reasoning_details"] == _DETAILS
    assert "reasoning_content" not in payload["messages"][0]
    payload["messages"][0]["reasoning_details"][0]["text"] = "mutated projection"
    assert restored.provider_replay is not None
    assert restored.provider_replay.reasoning_details == _DETAILS


@pytest.mark.parametrize("target", [
    {"model": "another/model"},
    {"base_url": "https://different.example/v1"},
    {"provider_kind": "openai"},
    {"replay_provider_state": False},
])
def test_foreign_target_withholds_native_state_without_mutating_history(target: dict) -> None:
    source = _provider()
    message = Message(
        role="assistant",
        content=[ContentBlockThinking(thinking="private", signature="synthetic-signature"),
                 ContentBlockToolUse(id="call-synthetic", name="lookup", input={})],
        reasoning_content="private",
        provider_replay=ProviderReplayState(
            protocol="openai_chat_completions", source=source._replay_source,
            model=_MODEL, reasoning_details=_DETAILS,
        ),
    )
    before = message.model_dump_json()
    target_provider = _provider(**target)
    assert source.can_replay_reasoning(message)
    assert not target_provider.can_replay_reasoning(message)
    payload, *_ = target_provider._build_payload([message], None, _config())
    assistant = payload["messages"][0]
    assert "reasoning_details" not in assistant
    assert "reasoning_content" not in assistant
    assert "extra_content" not in assistant["tool_calls"][0]
    assert message.model_dump_json() == before


@pytest.mark.parametrize("protocol, enabled, expected_thinking", [
    ("openai_chat_completions", True, False),
    ("unknown_future_protocol", True, False),
    ("anthropic_messages", True, True),
    (None, True, True),
    ("anthropic_messages", False, False),
    (None, False, False),
])
def test_anthropic_filters_explicit_foreign_native_state_per_message(
    protocol: str | None, enabled: bool, expected_thinking: bool,
) -> None:
    message = Message(
        role="assistant",
        content=[
            ContentBlockThinking(thinking="Synthetic reasoning", signature="synthetic-signature"),
            ContentBlockText(text="Synthetic answer"),
            ContentBlockToolUse(id="call-synthetic", name="lookup", input={"value": 3}),
        ],
        reasoning_content="Synthetic reasoning",
        provider_replay=ProviderReplayState(
            protocol=protocol, source="synthetic-source", model="synthetic-source-model",
        ) if protocol else None,
    )
    original = message.model_dump_json()
    # Reproduce the request-history path whose canonical retention exposed
    # foreign thought signatures to the native Anthropic adapter.
    history = drop_reasoning(
        [message], preserve_reasoning_content=True, preserve_tool_call_reasoning=True,
    )
    provider = AnthropicProvider(
        api_key="synthetic-key", model="synthetic-target", replay_provider_state=enabled,
    )
    payload, _ = provider._build_payload(history, None, ChatConfig(), record_diagnostics=False)
    parts = payload["messages"][0]["content"]
    thinking = [part for part in parts if part["type"] == "thinking"]
    assert bool(thinking) is expected_thinking
    if thinking:
        assert thinking == [{
            "type": "thinking", "thinking": "Synthetic reasoning",
            "signature": "synthetic-signature",
        }]
    assert [part for part in parts if part["type"] != "thinking"] == [
        {"type": "text", "text": "Synthetic answer"},
        {"type": "tool_use", "id": "call-synthetic", "name": "lookup", "input": {"value": 3}},
    ]
    assert message.model_dump_json() == original


@pytest.mark.parametrize("protocol, source_url, model, expected_signature", [
    ("openai_chat_completions", "https://gemini.example/v1", "gemini-synthetic", True),
    ("openai_chat_completions", "https://other.example/v1", "gemini-synthetic", False),
    ("openai_chat_completions", "https://gemini.example/v1", "gemini-other", False),
    ("anthropic_messages", "https://gemini.example/v1", "gemini-synthetic", False),
])
def test_gemini_thought_signature_requires_same_captured_route(
    protocol: str, source_url: str, model: str, expected_signature: bool,
) -> None:
    provider = _provider(
        provider_kind="gemini", base_url="https://gemini.example/v1", model="gemini-synthetic",
    )
    message = Message(
        role="assistant", content=[
            ContentBlockThinking(thinking="Synthetic reasoning", signature="synthetic-signature"),
            ContentBlockToolUse(id="call-synthetic", name="lookup", input={}),
        ],
        provider_replay=ProviderReplayState(
            protocol=protocol, source=_openai_replay_source("gemini", source_url), model=model,
        ),
    )
    original = message.model_dump_json()
    payload, *_ = provider._build_payload([message], None, ChatConfig(thinking=True))
    call = payload["messages"][0]["tool_calls"][0]
    assert ("extra_content" in call) is expected_signature
    assert call["id"] == "call-synthetic"
    assert call["function"] == {"name": "lookup", "arguments": "{}"}
    assert message.model_dump_json() == original


@pytest.mark.parametrize("reasoning_field, expected", [(None, None), ("", "")])
async def test_capture_distinguishes_absent_and_empty_reasoning(
    monkeypatch: pytest.MonkeyPatch, reasoning_field: str | None, expected: str | None
) -> None:
    delta: dict[str, Any] = {"content": "answer"}
    if reasoning_field is not None:
        delta["reasoning_content"] = reasoning_field
    _patch_transport(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=_sse([delta])
    ))
    events = [event async for event in _provider().chat(
        [Message(role="user", content="Synthetic input.")], config=_config()
    )]
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.reasoning_content == expected
    assert done.provider_replay is not None
    assert done.provider_replay.reasoning_details is None
    assert done.provider_replay.native_reasoning_content == expected


@pytest.mark.parametrize("details", [{"type": "reasoning.text"}, ["invalid"]])
async def test_malformed_native_state_cannot_be_accepted(
    monkeypatch: pytest.MonkeyPatch, details: Any
) -> None:
    _patch_transport(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_sse([{"reasoning_details": details}, {"content": "answer"}]),
    ))
    events = [event async for event in _provider().chat(
        [Message(role="user", content="Synthetic input.")], config=_config()
    )]
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(isinstance(event, ErrorEvent) and event.code == "invalid_stream_frame"
               for event in events)


def test_replay_source_normalizes_equivalent_routes_without_exposing_secrets() -> None:
    assert _openai_replay_source("openai", "https://API.EXAMPLE:443") == (
        _openai_replay_source("openai", "https://api.example/v1/")
    )
    source = _openai_replay_source(
        "openai", "https://synthetic-user:synthetic-pass@api.example/private-route?key=dummy"
    )
    assert source.startswith("openai_compat:")
    assert len(source) == len("openai_compat:") + 64
    for private_part in ("synthetic-user", "synthetic-pass", "private-route", "dummy"):
        assert private_part not in source
    assert source != _openai_replay_source("openai", "https://api.example/v1")


@pytest.mark.parametrize("details", [[_DETAILS[1]], []])
def test_captured_native_details_replay_without_a_capability_catalog_entry(details: list) -> None:
    provider = _provider()
    message = Message(
        role="assistant", content="answer",
        provider_replay=ProviderReplayState(
            protocol="openai_chat_completions", source=provider._replay_source,
            model=_MODEL, reasoning_details=details,
        ),
    )
    payload, *_ = provider._build_payload([message], None, ChatConfig())
    assert payload["messages"][0]["reasoning_details"] == details
    message.provider_replay.protocol = "unknown_future_protocol"
    assert not provider.can_replay_reasoning(message)
    payload, *_ = provider._build_payload([message], None, ChatConfig())
    assert "reasoning_details" not in payload["messages"][0]


@pytest.mark.parametrize("tool_call, reasoning, expected", [
    (False, "private reasoning", ""),
    (True, "private reasoning", "private reasoning"),
    (True, "x" * 50_001, ""),
])
def test_tokenrhythm_projects_captured_state_without_changing_canonical_reasoning(
    tool_call: bool, reasoning: str, expected: str
) -> None:
    provider = _provider(
        provider_kind="tokenrhythm", base_url="https://tokenrhythm.studio/v1",
        model="deepseek-v4-flash",
    )
    content: Any = "answer"
    if tool_call:
        content = [ContentBlockToolUse(id="call-synthetic", name="lookup", input={})]
    message = Message(
        role="assistant", content=content, reasoning_content=reasoning,
        provider_replay=ProviderReplayState(
            protocol="openai_chat_completions", source=provider._replay_source,
            model="deepseek-v4-flash", native_reasoning_content=reasoning,
        ),
    )
    payload, *_ = provider._build_payload([message], None, ChatConfig(thinking=True))
    assert payload["messages"][0]["reasoning_content"] == expected
    assert message.reasoning_content == reasoning


async def test_rejected_response_never_releases_captured_native_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_transport(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_sse([{"reasoning_details": _DETAILS}], final={
            "error": {"message": "synthetic rejection", "code": "invalid_response"}
        }),
    ))
    events = [event async for event in _provider().chat(
        [Message(role="user", content="Synthetic input.")], config=_config()
    )]
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)


async def test_stream_reassembles_logical_details_without_merging_encrypted_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Repeated index zero is a real OpenRouter streaming grammar. It cannot
    # globally identify a logical block across a text/summary/encrypted boundary.
    fragments = [
        {"type": "reasoning.text", "text": "First ", "index": 0},
        {"type": "reasoning.text", "text": "thought.", "index": 0},
        {"type": "reasoning.text", "signature": "synthetic-signature", "id": "text-1",
         "format": "anthropic-claude-v1", "index": 0},
        {"type": "reasoning.summary", "summary": "First ", "index": 0},
        {"type": "reasoning.summary", "summary": "summary.",
         "format": "openai-responses-v1", "index": 0},
        {"type": "reasoning.encrypted", "data": "opaque-one", "id": "opaque-1", "index": 0},
        {"type": "reasoning.encrypted", "data": "opaque-two", "id": "opaque-2", "index": 0},
        {"type": "reasoning.summary", "summary": "After opaque.", "index": 0},
        {"type": "reasoning.text", "text": "New block.", "id": "text-2", "index": 0},
        {"type": "reasoning.text", "text": "Distinct ID.", "id": "text-3", "index": 0},
        {"type": "reasoning.text", "text": "Distinct index.", "id": "text-3", "index": 1},
    ]
    expected = [
        {"type": "reasoning.text", "text": "First thought.", "index": 0,
         "signature": "synthetic-signature", "id": "text-1", "format": "anthropic-claude-v1"},
        {"type": "reasoning.summary", "summary": "First summary.", "index": 0,
         "format": "openai-responses-v1"},
        *fragments[5:],
    ]
    _patch_transport(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_sse([{"reasoning_details": [fragment]} for fragment in fragments]
                     + [{"content": "Synthetic answer."}]),
    ))
    provider = _provider()
    events = [event async for event in provider.chat(
        [Message(role="user", content="Synthetic input.")], config=_config()
    )]
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.provider_replay is not None
    assert done.provider_replay.reasoning_details == expected
    restored = Message.model_validate_json(Message(
        role="assistant", content="Synthetic answer.", reasoning_content=done.reasoning_content,
        provider_replay=done.provider_replay,
    ).model_dump_json())
    payload, *_ = provider._build_payload([restored], None, _config())
    assert payload["messages"][0]["reasoning_details"] == expected


@pytest.mark.parametrize("signature_first", [False, True])
@pytest.mark.parametrize("text_delta", [{"text": None}, {}], ids=["null", "omitted"])
async def test_stream_preserves_nullable_signature_frames_in_either_order(
    monkeypatch: pytest.MonkeyPatch, signature_first: bool, text_delta: dict[str, Any],
) -> None:
    text = {"type": "reasoning.text", "text": "Synthetic reasoning.", "index": 0}
    signature = {
        "type": "reasoning.text", "signature": "synthetic-signature", "index": 0,
        **text_delta,
    }
    fragments = [signature, text] if signature_first else [text, signature]
    _patch_transport(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_sse([{"reasoning_details": [fragment]} for fragment in fragments]
                     + [{"content": "Synthetic answer."}]),
    ))
    provider = _provider()
    events = [event async for event in provider.chat(
        [Message(role="user", content="Synthetic input.")], config=_config()
    )]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert len(done) == 1
    assert "".join(event.text for event in events if isinstance(event, TextDeltaEvent)) == (
        "Synthetic answer."
    )
    assert done[0].reasoning_content == "Synthetic reasoning."
    assert done[0].provider_replay is not None
    expected = [{**text, "signature": "synthetic-signature"}]
    assert done[0].provider_replay.reasoning_details == expected
    restored = Message.model_validate_json(Message(
        role="assistant", content="Synthetic answer.",
        reasoning_content=done[0].reasoning_content, provider_replay=done[0].provider_replay,
    ).model_dump_json())
    payload, *_ = provider._build_payload([restored], None, _config())
    assert payload["messages"][0]["reasoning_details"] == expected


@pytest.mark.parametrize("detail_type, text_field, value", [
    ("reasoning.text", "text", 1),
    ("reasoning.text", "text", {}),
    ("reasoning.text", "text", []),
    ("reasoning.summary", "summary", None),
    ("reasoning.summary", "summary", 1),
])
@pytest.mark.parametrize("position", ["first", "continuation", "distinct-block"])
async def test_stream_rejects_invalid_reasoning_text_values(
    monkeypatch: pytest.MonkeyPatch, detail_type: str, text_field: str,
    value: Any, position: str,
) -> None:
    fragments = [] if position == "first" else [
        {"type": detail_type, text_field: "Synthetic reasoning.", "index": 0},
    ]
    fragments.append({
        "type": detail_type, text_field: value,
        "index": 1 if position == "distinct-block" else 0,
    })
    _patch_transport(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=_sse([{"reasoning_details": [fragment]} for fragment in fragments]
                     + [{"content": "Synthetic answer."}]),
    ))
    events = [event async for event in _provider().chat(
        [Message(role="user", content="Synthetic input.")], config=_config()
    )]
    assert not any(isinstance(event, DoneEvent) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].code == "invalid_stream_frame"


@pytest.mark.parametrize("provider_kind, base_url, model, expected", [
    ("deepseek", "https://api.deepseek.com", "deepseek-flash", True),
    ("deepseek", "https://api.deepseek.com/v1", "deepseek-flash", True),
    ("deepseek", "https://api.deepseek.com", "deepseek-v4-flash", True),
    ("deepseek", "https://api.deepseek.com/v1", "deepseek-v4-pro", True),
    ("openrouter", _BASE_URL, "deepseek/deepseek-v4-flash", True),
    ("openrouter", _BASE_URL, "deepseek/deepseek-flash", False),
    ("openrouter", _BASE_URL, _MODEL, False),
    ("deepseek", "https://custom.example/v1", "deepseek-v4-flash", False),
    ("deepseek", "https://api.deepseek.com/custom", "deepseek-v4-flash", False),
    ("deepseek", "https://api.deepseek.com", "deepseek-reasoner", False),
    ("tokenrhythm", "https://tokenrhythm.studio/v1", "deepseek-v4-flash", False),
    ("tokenrhythm", "https://tokenrhythm.studio/v1", "deepseek-flash", False),
])
def test_strict_reasoning_history_requirement_is_route_scoped(
    provider_kind: str, base_url: str, model: str, expected: bool
) -> None:
    provider = _provider(provider_kind=provider_kind, base_url=base_url, model=model)
    assert provider.requires_complete_reasoning_history(tools=True, thinking=True) is expected
    assert not provider.requires_complete_reasoning_history(tools=False, thinking=True)
    assert not provider.requires_complete_reasoning_history(tools=True, thinking=False)


@pytest.mark.parametrize("base_url", ["https://api.deepseek.com", "https://api.deepseek.com/v1"])
def test_official_deepseek_flash_alias_replays_captured_reasoning(base_url: str) -> None:
    provider = _provider(provider_kind="deepseek", base_url=base_url, model="deepseek-flash")
    message = Message(
        role="assistant",
        content="Synthetic answer.",
        reasoning_content="Complete synthetic reasoning.",
        provider_replay=ProviderReplayState(
            protocol="openai_chat_completions",
            source=_openai_replay_source("deepseek", base_url),
            model="deepseek-flash",
        ),
    )
    payload, *_ = provider._build_payload([message], None, ChatConfig(thinking=True))
    assert payload["messages"][0]["reasoning_content"] == message.reasoning_content
    assert payload["thinking"] == {"type": "enabled"}
    assert provider.can_replay_reasoning(message)


def test_source_identity_distinguishes_explicit_zero_port_from_default() -> None:
    assert _openai_replay_source("deepseek", "https://api.deepseek.com:0") != (
        _openai_replay_source("deepseek", "https://api.deepseek.com")
    )


@pytest.mark.parametrize("port", ["bad", "-1", "65536"])
def test_source_identity_does_not_eagerly_reject_invalid_ports(port: str) -> None:
    invalid_url = f"https://api.deepseek.com:{port}/v1"
    source = _openai_replay_source("deepseek", invalid_url)
    assert source != _openai_replay_source("deepseek", "https://api.deepseek.com/v1")
    assert source != _openai_replay_source("deepseek", "https://api.deepseek.com:0/v1")
    assert source == _openai_replay_source(
        "deepseek", f"https://synthetic-user:synthetic-pass@api.deepseek.com:{port}/v1"
    )
    assert "synthetic-user" not in source and "synthetic-pass" not in source
    provider = _provider(provider_kind="deepseek", base_url=invalid_url, model="deepseek-flash")
    assert not provider.requires_complete_reasoning_history(tools=True, thinking=True)


@pytest.mark.parametrize("provider_kind", ["deepseek", "openrouter", "tokenrhythm"])
async def test_native_replay_exceeding_budget_never_reaches_transport(
    monkeypatch: pytest.MonkeyPatch, provider_kind: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    _patch_transport(monkeypatch, handler)
    # Even rolling back optional recent-message protection cannot authorize
    # truncation of native continuation state to make a request fit.
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_ASSISTANT", "0")
    if provider_kind in {"deepseek", "tokenrhythm"}:
        provider = _provider(
            provider_kind=provider_kind,
            base_url=(
                "https://api.deepseek.com" if provider_kind == "deepseek"
                else "https://tokenrhythm.studio/v1"
            ),
            model="deepseek-flash" if provider_kind == "deepseek" else "kimi-k2.7-code",
        )
        details = None
    else:
        provider = _provider()
        details = [{"type": "reasoning.encrypted", "data": "synthetic-opaque" * 1000}]
    message = Message(
        role="assistant", content="Synthetic answer.",
        reasoning_content="synthetic-reasoning" * 1000 if details is None else None,
        provider_replay=ProviderReplayState(
            protocol="openai_chat_completions", source=provider._replay_source,
            model=provider._model, reasoning_details=details,
            native_reasoning_content=(
                "synthetic-reasoning" * 1000 if provider_kind == "tokenrhythm" else None
            ),
        ),
    )
    original = message.model_dump_json()
    events = [event async for event in provider.chat(
        [message, Message(role="user", content="Continue.")],
        config=ChatConfig(thinking=True, provider_request_max_chars=1000),
    )]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert requests == []
    assert len(errors) == 1
    assert errors[0].code == "provider_request_budget_exhausted"
    assert json.loads(errors[0].message)["fits"] is False
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert message.model_dump_json() == original


def test_legacy_reasoning_text_is_not_captured_native_state() -> None:
    provider = _provider()
    assert not provider.can_replay_reasoning(Message(
        role="assistant", content="answer", reasoning_content="legacy aggregate"
    ))


def _tokenrhythm_provider(**kwargs: Any) -> OpenAIProvider:
    return _provider(
        provider_kind=kwargs.pop("provider_kind", "tokenrhythm"),
        base_url=kwargs.pop("base_url", "https://tokenrhythm.studio/v1"),
        model=kwargs.pop("model", "kimi-k2.7-code"),
        **kwargs,
    )


def _captured_tokenrhythm_message(
    provider: OpenAIProvider, native_reasoning: str | None,
) -> Message:
    return Message(
        role="assistant", content="Synthetic answer.",
        reasoning_content="Display reasoning may differ from the native field.",
        provider_replay=ProviderReplayState(
            protocol="openai_chat_completions", source=provider._replay_source,
            model=provider.model, native_reasoning_content=native_reasoning,
        ),
    )


@pytest.mark.parametrize("model", [
    "glm-5", "glm-5.1", "glm-5.2", "glm-5.3", "glm-5.3-flash",
    "minimax-m2.5", "minimax-m2.7", "kimi-k2.5", "kimi-k2.6", "kimi-k2.7-code",
    "mimo-v2.5-pro", "qwen3.7-max", "qwen3.7-flash", "qwen3.8-max", "qwen3.8-27b",
    "qwen3.8-flash", "seed-2.1-pro", "seed-2.1-turbo", "longcat-2.0", "deepseek-flash",
])
@pytest.mark.parametrize("thinking", [False, True])
@pytest.mark.parametrize("tool_call", [False, True])
def test_tokenrhythm_captured_reasoning_does_not_require_a_thinking_dialect(
    model: str, thinking: bool, tool_call: bool,
) -> None:
    provider = _tokenrhythm_provider(model=model)
    native = "  Synthetic\nreasoning\twith Unicode: 猫 🦐  "
    message = _captured_tokenrhythm_message(provider, native)
    if tool_call:
        message.content = [ContentBlockToolUse(id="call-synthetic", name="lookup", input={})]
    original = message.model_dump_json()
    config = ChatConfig(thinking=thinking, model_capabilities=ModelCapabilities(
        supports_tools=True, supports_reasoning=False, reasoning_format="none",
    ))
    payload, *_ = provider._build_payload([message], None, config)
    assert payload["messages"][0]["reasoning_content"] == native
    assert not set(payload) & {
        "thinking", "enable_thinking", "reasoning_effort", "preserve_thinking",
    }
    assert provider.can_replay_reasoning(message)
    assert message.model_dump_json() == original


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("native", [None, "", " \n原生🦐\t "])
async def test_tokenrhythm_capture_preserves_only_original_reasoning_content(
    monkeypatch: pytest.MonkeyPatch, stream: bool, native: str | None,
) -> None:
    deltas: list[dict[str, Any]] = [{
        "reasoning": "Synthetic alias.",
        "reasoning_details": [{"type": "reasoning.text", "text": "Synthetic display."}],
    }]
    if native is not None:
        deltas.extend({"reasoning_content": part} for part in [native[:2], native[2:]])
    deltas.append({"content": "Synthetic answer."})
    response_message = {**deltas[0], "content": "Synthetic answer."}
    if native is not None:
        response_message["reasoning_content"] = native

    _patch_transport(monkeypatch, lambda request: httpx.Response(
        200,
        headers={"content-type": "text/event-stream"} if stream else {},
        content=_sse(deltas) if stream else None,
        json=None if stream else {
            "model": "synthetic-resolved-model",
            "choices": [{"message": response_message, "finish_reason": "stop"}],
        },
    ))
    provider = _tokenrhythm_provider()
    if stream:
        events = [event async for event in provider.chat(
            [Message(role="user", content="Synthetic input.")], config=ChatConfig()
        )]
    else:
        events = [event async for event in provider._complete_non_stream(
            payload={"model": provider.model, "messages": []}, headers={}, cfg=ChatConfig(),
            tools=None, timeout_exc=httpx.ReadTimeout("synthetic timeout"),
        )]
    assert not [event for event in events if isinstance(event, ErrorEvent)]
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.provider_replay is not None
    assert done.provider_replay.native_reasoning_content == native
    assert done.provider_replay.model == provider.model
    message = Message(
        role="assistant", content="Synthetic answer.", reasoning_content=done.reasoning_content,
        provider_replay=done.provider_replay,
    )
    payload, *_ = provider._build_payload([message], None, ChatConfig())
    assistant = payload["messages"][0]
    assert ("reasoning_content" in assistant) is (native is not None)
    if native is not None:
        assert assistant["reasoning_content"] == native


@pytest.mark.parametrize("stream", [False, True])
async def test_tokenrhythm_think_tags_do_not_manufacture_native_reasoning(
    monkeypatch: pytest.MonkeyPatch, stream: bool,
) -> None:
    content = "<think>Synthetic thought.</think>Synthetic answer."
    _patch_transport(monkeypatch, lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"} if stream else {},
        content=_sse([{"content": content}]) if stream else None,
        json=None if stream else {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        },
    ))
    provider = _tokenrhythm_provider(model="minimax-m2.7")
    config = ChatConfig(model_capabilities=ModelCapabilities(
        supports_reasoning=True, reasoning_format="think_tags",
    ))
    if stream:
        events = [event async for event in provider.chat(
            [Message(role="user", content="Synthetic input.")], config=config,
        )]
    else:
        events = [event async for event in provider._complete_non_stream(
            payload={"model": provider.model, "messages": []}, headers={}, cfg=config,
            tools=None, timeout_exc=httpx.ReadTimeout("synthetic timeout"),
        )]
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.reasoning_content == "Synthetic thought."
    assert done.provider_replay is not None
    assert done.provider_replay.native_reasoning_content is None
    message = Message(
        role="assistant", content="Synthetic answer.", reasoning_content=done.reasoning_content,
        provider_replay=done.provider_replay,
    )
    payload, *_ = provider._build_payload([message], None, config)
    assert "reasoning_content" not in payload["messages"][0]


@pytest.mark.parametrize("target", [
    {"model": "kimi-k2.6"},
    {"base_url": "https://other.example/v1"},
    {"provider_kind": "openai"},
    {"replay_provider_state": False},
])
def test_tokenrhythm_captured_reasoning_cannot_cross_routes(target: dict) -> None:
    source = _tokenrhythm_provider()
    message = _captured_tokenrhythm_message(source, "Synthetic native reasoning.")
    original = message.model_dump_json()
    provider = _tokenrhythm_provider(**target)
    assert not provider.can_replay_reasoning(message)
    payload, *_ = provider._build_payload([message], None, ChatConfig())
    assert "reasoning_content" not in payload["messages"][0]
    assert message.model_dump_json() == original


def test_tokenrhythm_non_v4_does_not_inherit_the_v4_field_limit() -> None:
    provider = _tokenrhythm_provider()
    native = "🦐" * 25_001
    message = _captured_tokenrhythm_message(provider, native)
    payload, *_ = provider._build_payload([message], None, ChatConfig())
    assert payload["messages"][0]["reasoning_content"] == native
    assert message.provider_replay is not None
    assert message.provider_replay.native_reasoning_content == native


@pytest.mark.parametrize("base_url", [
    "https://tokenrhythm.studio/v1/custom", "https://tokenrhythm.studio/v1?tenant=synthetic",
    "https://api.tokenrhythm.studio/v1", "http://tokenrhythm.studio/v1",
    "https://tokenrhythm.studio.evil.example/v1", "https://tokenrhythm.studio:8443/v1",
])
def test_tokenrhythm_nonofficial_endpoint_does_not_inherit_captured_echo(base_url: str) -> None:
    provider = _tokenrhythm_provider(base_url=base_url)
    message = _captured_tokenrhythm_message(provider, "Synthetic native reasoning.")
    payload, *_ = provider._build_payload([message], None, ChatConfig())
    assert "reasoning_content" not in payload["messages"][0]


@pytest.mark.parametrize("case", ["legacy", "foreign_protocol", "user", "echo_disabled"])
def test_tokenrhythm_captured_echo_respects_message_and_request_boundaries(
    monkeypatch: pytest.MonkeyPatch, case: str,
) -> None:
    if case == "echo_disabled":
        monkeypatch.setenv("OPENSQUILLA_REASONING_ECHO_TURNS", "0")
    provider = _tokenrhythm_provider()
    message = _captured_tokenrhythm_message(provider, "Synthetic native reasoning.")
    if case == "legacy":
        message.provider_replay = None
    elif case == "foreign_protocol":
        assert message.provider_replay is not None
        message.provider_replay.protocol = "anthropic_messages"
    elif case == "user":
        message.role = "user"
    payload, *_ = provider._build_payload([message], None, ChatConfig())
    assert "reasoning_content" not in payload["messages"][0]


@pytest.mark.parametrize("native", [None, "", " \nSynthetic preserved reasoning 猫🦐\t "])
async def test_tokenrhythm_native_reasoning_survives_sqlite_reopen(
    tmp_path: Path, native: str | None,
) -> None:
    provider = _tokenrhythm_provider()
    message = _captured_tokenrhythm_message(provider, native)
    envelope = {"version": 1, "messages": [message.model_dump(mode="json")]}
    path = str(tmp_path / "synthetic-sessions.db")
    storage = SessionStorage(path)
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        node = await manager.create("agent:main:synthetic-replay")
        await manager.append_message(
            node.session_key, "assistant", "Synthetic answer.", assistant_replay=envelope,
        )
    finally:
        await storage.close()
    storage = SessionStorage(path)
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        transcript = await manager.get_canonical_transcript(node.session_key)
        saved = transcript[0].assistant_replay
        assert saved == envelope
        assert saved is not None
        restored = Message.model_validate(saved["messages"][0])
        assert restored.provider_replay is not None
        assert restored.provider_replay.native_reasoning_content == native
        payload, *_ = provider._build_payload([restored], None, ChatConfig())
        assert ("reasoning_content" in payload["messages"][0]) is (native is not None)
        if native is not None:
            assert payload["messages"][0]["reasoning_content"] == native
    finally:
        await storage.close()
