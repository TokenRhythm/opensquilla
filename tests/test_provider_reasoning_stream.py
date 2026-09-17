"""Provider-level contract for real-time reasoning streaming.

These lock the root-cause fix: reasoning/thinking content must be emitted as
first-class streaming events (ReasoningDeltaEvent) from the provider source —
not silently buffered and only revealed at turn end. The concatenation of the
streamed reasoning deltas must still equal DoneEvent.reasoning_content so that
all the non-TUI consumers (signature replay, persistence, compaction, cost)
keep working unchanged.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.types import (
    ChatConfig,
    DoneEvent,
    Message,
    ReasoningDeltaEvent,
    TextDeltaEvent,
)


def _sse_body(events: list[dict]) -> bytes:
    parts = []
    for ev in events:
        parts.append(f"event: {ev['type']}\n".encode())
        parts.append(f"data: {json.dumps(ev)}\n\n".encode())
    return b"".join(parts)


def _patch_transport(monkeypatch, body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client
    )


def _anthropic_thinking_sse() -> bytes:
    return _sse_body(
        [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "model": "claude-opus-4-7",
                    "usage": {"input_tokens": 10},
                },
            },
            # thinking block streams first
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Let me "},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "consider this."},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig-abc"},
            },
            {"type": "content_block_stop", "index": 0},
            # then the real answer text
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "Hello."},
            },
            {"type": "content_block_stop", "index": 1},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            },
            {"type": "message_stop"},
        ]
    )


def _collect(provider) -> list[object]:
    async def _run() -> list[object]:
        return [
            ev
            async for ev in provider.chat(
                [Message(role="user", content="hi")],
                config=ChatConfig(thinking=True),
            )
        ]

    return asyncio.run(_run())


def test_anthropic_streams_reasoning_as_delta_events(monkeypatch) -> None:
    _patch_transport(monkeypatch, _anthropic_thinking_sse())
    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    events = _collect(provider)

    reasoning = [ev for ev in events if isinstance(ev, ReasoningDeltaEvent)]
    assert reasoning, "expected ReasoningDeltaEvent to be streamed in real time"
    assert "".join(ev.text for ev in reasoning) == "Let me consider this."


def test_anthropic_reasoning_deltas_concat_equals_done_reasoning_content(
    monkeypatch,
) -> None:
    _patch_transport(monkeypatch, _anthropic_thinking_sse())
    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    events = _collect(provider)

    streamed = "".join(
        ev.text for ev in events if isinstance(ev, ReasoningDeltaEvent)
    )
    done = next(ev for ev in events if isinstance(ev, DoneEvent))
    assert done.reasoning_content == streamed
    # signature still arrives on DoneEvent for multi-turn replay
    assert done.thinking_signature == "sig-abc"


def test_anthropic_reasoning_precedes_answer_text(monkeypatch) -> None:
    """Ordering contract: reasoning deltas arrive before the answer text delta,
    so the renderer can open a thinking block then a separate answer block —
    never retyping one into the other."""
    _patch_transport(monkeypatch, _anthropic_thinking_sse())
    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    events = _collect(provider)
    kinds = [
        type(ev).__name__
        for ev in events
        if isinstance(ev, ReasoningDeltaEvent)
        or type(ev).__name__ == "TextDeltaEvent"
    ]
    assert kinds.index("ReasoningDeltaEvent") < kinds.index("TextDeltaEvent")


# --- OpenAI-compatible (openrouter/deepseek) ---------------------------------


def _openai_chunks_body(chunks: list[dict]) -> bytes:
    body = b"".join(f"data: {json.dumps(c)}\n\n".encode() for c in chunks)
    return body + b"data: [DONE]\n\n"


def _patch_openai_transport(monkeypatch, body: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient", patched_async_client
    )


def _collect_openai(provider, cfg) -> list[object]:
    async def _run() -> list[object]:
        return [
            ev
            async for ev in provider.chat(
                [Message(role="user", content="hi")], config=cfg
            )
        ]

    return asyncio.run(_run())


def _openai_reasoning_cfg():
    from opensquilla.engine.types import ThinkingLevel
    from opensquilla.provider.types import ModelCapabilities

    return ChatConfig(
        thinking=True,
        thinking_level=ThinkingLevel.HIGH,
        model_capabilities=ModelCapabilities(
            supports_reasoning=True,
            supports_tools=True,
            reasoning_format="openrouter",
        ),
    )


def test_openai_streams_reasoning_details_as_delta_events(monkeypatch) -> None:
    from opensquilla.provider.openai import OpenAIProvider

    chunks = [
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "I considered "}
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [
                {
                    "delta": {
                        "reasoning_details": [
                            {"type": "reasoning.text", "text": "the request."}
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": "deepseek/deepseek-v4-flash",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    _patch_openai_transport(monkeypatch, _openai_chunks_body(chunks))
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
        provider_routing={"deepseek/deepseek-v4-flash": "deepseek"},
    )

    events = _collect_openai(provider, _openai_reasoning_cfg())

    streamed = "".join(
        ev.text for ev in events if isinstance(ev, ReasoningDeltaEvent)
    )
    done = next(ev for ev in events if isinstance(ev, DoneEvent))
    assert streamed == "I considered the request."
    assert done.reasoning_content == "I considered the request."


def test_openai_streams_reasoning_content_field_as_delta_events(monkeypatch) -> None:
    from opensquilla.provider.openai import OpenAIProvider

    chunks = [
        {
            "model": "deepseek-reasoner",
            "choices": [
                {"delta": {"reasoning_content": "Step one. "}, "finish_reason": None}
            ],
        },
        {
            "model": "deepseek-reasoner",
            "choices": [
                {"delta": {"reasoning_content": "Step two."}, "finish_reason": None}
            ],
        },
        {
            "model": "deepseek-reasoner",
            "choices": [{"delta": {"content": "answer"}, "finish_reason": None}],
        },
        {
            "model": "deepseek-reasoner",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    _patch_openai_transport(monkeypatch, _openai_chunks_body(chunks))
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-reasoner",
        base_url="https://api.deepseek.com/v1",
        provider_kind="deepseek",
    )

    events = _collect_openai(provider, _openai_reasoning_cfg())

    streamed = "".join(
        ev.text for ev in events if isinstance(ev, ReasoningDeltaEvent)
    )
    done = next(ev for ev in events if isinstance(ev, DoneEvent))
    assert streamed == "Step one. Step two."
    assert done.reasoning_content == "Step one. Step two."


def test_openai_emits_reasoning_before_text_from_the_same_delta(monkeypatch) -> None:
    """JSON field order must not let answer text precede its thinking block."""
    from opensquilla.provider.openai import OpenAIProvider

    chunks = [
        {
            "model": "deepseek-reasoner",
            "choices": [
                {
                    "delta": {
                        "content": "answer",
                        "reasoning_content": "thinking",
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "model": "deepseek-reasoner",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    _patch_openai_transport(monkeypatch, _openai_chunks_body(chunks))
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-reasoner",
        base_url="https://api.deepseek.com/v1",
        provider_kind="deepseek",
    )

    visible_events = [
        event
        for event in _collect_openai(provider, _openai_reasoning_cfg())
        if isinstance(event, (ReasoningDeltaEvent, TextDeltaEvent))
    ]

    assert [type(event) for event in visible_events] == [
        ReasoningDeltaEvent,
        TextDeltaEvent,
    ]
    assert [event.text for event in visible_events] == ["thinking", "answer"]
