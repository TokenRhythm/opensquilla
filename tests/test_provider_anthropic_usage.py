import asyncio
import json

import httpx
import pytest

from opensquilla.provider.anthropic import (
    AnthropicProvider,
    _anthropic_input_token_counts,
    _anthropic_iteration_token_counts,
    _build_message_payload,
)
from opensquilla.provider.selector import build_provider
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockCompaction,
    DoneEvent,
    ErrorEvent,
    Message,
    ModelInfo,
)


def test_anthropic_input_tokens_include_cache_read_and_creation_tokens() -> None:
    total, cache_read, cache_creation = _anthropic_input_token_counts(
        {
            "input_tokens": 21,
            "cache_read_input_tokens": 188_086,
            "cache_creation_input_tokens": 456,
            "output_tokens": 393,
        }
    )

    assert total == 188_563
    assert cache_read == 188_086
    assert cache_creation == 456


def test_anthropic_input_tokens_include_structured_cache_creation_tokens() -> None:
    total, cache_read, cache_creation = _anthropic_input_token_counts(
        {
            "input_tokens": 21,
            "cache_read_input_tokens": 100,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 456,
                "ephemeral_1h_input_tokens": 100,
            },
            "output_tokens": 393,
        }
    )

    assert total == 677
    assert cache_read == 100
    assert cache_creation == 556


def test_anthropic_iteration_tokens_sum_compaction_and_message_usage() -> None:
    input_tokens, output_tokens = _anthropic_iteration_token_counts(
        {
            "input_tokens": 23000,
            "output_tokens": 1000,
            "iterations": [
                {"type": "compaction", "input_tokens": 180000, "output_tokens": 3500},
                {"type": "message", "input_tokens": 23000, "output_tokens": 1000},
            ],
        }
    )

    assert input_tokens == 203000
    assert output_tokens == 4500


def test_anthropic_message_payload_replays_compaction_block_with_cache_control() -> None:
    payload = _build_message_payload(
        Message(
            role="assistant",
            content=[
                ContentBlockCompaction(
                    content="summary text",
                    cache_control={"type": "ephemeral"},
                )
            ],
        )
    )

    assert payload == {
        "role": "assistant",
        "content": [
            {
                "type": "compaction",
                "content": "summary text",
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


def _sse_body(events: list[dict]) -> bytes:
    parts = []
    for ev in events:
        parts.append(f"event: {ev['type']}\n".encode())
        parts.append(f"data: {json.dumps(ev)}\n\n".encode())
    return b"".join(parts)


@pytest.mark.parametrize(
    ("provider_id", "expected_base_url"),
    [
        ("minimax", "https://api.minimaxi.com/anthropic"),
        ("minimax_global", "https://api.minimax.io/anthropic"),
        ("volcengine_coding_plan_anthropic", "https://ark.cn-beijing.volces.com/api/coding"),
        (
            "byteplus_coding_plan_anthropic",
            "https://ark.ap-southeast.bytepluses.com/api/coding",
        ),
        (
            "tencent_token_plan_anthropic",
            "https://api.lkeap.cloud.tencent.com/plan/anthropic",
        ),
    ],
)
def test_anthropic_compatible_endpoints_use_authorization_bearer(
    monkeypatch,
    provider_id: str,
    expected_base_url: str,
) -> None:
    """Registry-built Anthropic-compatible providers sign with Authorization: Bearer.

    The auth style comes from the ProviderSpec (auth_header_style) rather
    than a base-url sniff, so the proof drives the production build path.
    """
    captured: dict[str, object] = {}
    body = _sse_body(
        [
            {
                "type": "message_start",
                "message": {"id": "msg_1", "model": "MiniMax-M2.7", "usage": {}},
            },
            {"type": "message_stop"},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
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

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)

    provider = build_provider(
        provider=provider_id,
        model="MiniMax-M2.7",
        api_key="test-key",
    )
    assert isinstance(provider, AnthropicProvider)

    async def _collect() -> DoneEvent:
        done: DoneEvent | None = None
        async for event in provider.chat(
            [Message(role="user", content="hi")], config=ChatConfig()
        ):
            if isinstance(event, DoneEvent):
                done = event
        assert done is not None
        return done

    done = asyncio.run(_collect())

    headers = captured["headers"]
    assert captured["url"] == f"{expected_base_url}/v1/messages"
    assert headers["Authorization"] == "Bearer test-key"
    assert "x-api-key" not in headers
    assert provider.provider_name == "anthropic"  # adapter family stays stable
    assert provider.provider_id == provider_id
    assert done.provider == provider_id
    assert done.model == "MiniMax-M2.7"


def test_direct_construction_defaults_to_x_api_key(monkeypatch) -> None:
    """Bare AnthropicProvider construction keeps Anthropic-proper auth."""
    captured: dict[str, object] = {}
    body = _sse_body(
        [
            {
                "type": "message_start",
                "message": {"id": "msg_1", "model": "claude-test", "usage": {}},
            },
            {"type": "message_stop"},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
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

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)

    provider = AnthropicProvider(api_key="test-key", model="claude-test")

    async def _collect() -> DoneEvent:
        done: DoneEvent | None = None
        async for event in provider.chat(
            [Message(role="user", content="hi")], config=ChatConfig()
        ):
            if isinstance(event, DoneEvent):
                done = event
        assert done is not None
        return done

    done = asyncio.run(_collect())

    headers = captured["headers"]
    assert headers["x-api-key"] == "test-key"
    assert "Authorization" not in headers
    assert provider.provider_name == "anthropic"
    assert provider.provider_id == "anthropic"
    assert done.provider == "anthropic"


def test_anthropic_done_event_carries_cache_write_tokens(monkeypatch) -> None:
    """End-to-end: SSE usage populates DoneEvent.cache_write_tokens."""

    sse_events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "claude-opus-4-7",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 500,
                },
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "ok"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {
                "output_tokens": 5,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 500,
            },
        },
        {"type": "message_stop"},
    ]

    body = _sse_body(sse_events)

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

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)

    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    async def _collect() -> DoneEvent:
        done: DoneEvent | None = None
        async for ev in provider.chat([Message(role="user", content="hi")], config=ChatConfig()):
            if isinstance(ev, DoneEvent):
                done = ev
        assert done is not None
        return done

    done = asyncio.run(_collect())
    assert done.cached_tokens == 1000
    assert done.cache_write_tokens == 500


def test_anthropic_provider_writes_llm_trace(monkeypatch, tmp_path) -> None:
    trace_path = tmp_path / "anthropic-llm-calls.jsonl"
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_RECORDER", "full")
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_PATH", str(trace_path))
    sse_events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 10},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "ok"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
    body = _sse_body(sse_events)

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

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)
    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    async def _collect() -> list[object]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                config=ChatConfig(),
            )
        ]

    events = asyncio.run(_collect())

    assert any(isinstance(event, DoneEvent) for event in events)
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["event"] == "llm.request"
    assert rows[0]["headers"]["x-api-key"] == "[REDACTED]"
    assert any(row["event"] == "llm.response_chunk" for row in rows)
    assert rows[-1]["event"] == "llm.response"
    assert rows[-1]["assistant_text"] == "ok"
    assert rows[-1]["response_ids"] == ["msg_1"]


def test_anthropic_done_event_includes_compaction_iteration_usage(monkeypatch) -> None:
    sse_events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 10},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {
                "input_tokens": 23,
                "output_tokens": 5,
                "iterations": [
                    {"type": "compaction", "input_tokens": 100, "output_tokens": 7},
                    {"type": "message", "input_tokens": 23, "output_tokens": 5},
                ],
            },
        },
        {"type": "message_stop"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(sse_events),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)

    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    async def _collect() -> DoneEvent:
        done: DoneEvent | None = None
        async for ev in provider.chat([Message(role="user", content="hi")], config=ChatConfig()):
            if isinstance(ev, DoneEvent):
                done = ev
        assert done is not None
        return done

    done = asyncio.run(_collect())
    assert done.input_tokens == 123
    assert done.output_tokens == 12


@pytest.mark.parametrize(
    "creation_payload,expected",
    [
        ({"cache_creation_input_tokens": 250}, 250),
        (
            {"cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 50}},
            150,
        ),
    ],
)
def test_anthropic_done_event_cache_write_handles_both_shapes(
    monkeypatch, creation_payload, expected
) -> None:
    sse_events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 1, **creation_payload},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1, **creation_payload},
        },
        {"type": "message_stop"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(sse_events),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)

    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    async def _collect() -> DoneEvent:
        done: DoneEvent | None = None
        async for ev in provider.chat([Message(role="user", content="hi")], config=ChatConfig()):
            if isinstance(ev, DoneEvent):
                done = ev
        assert done is not None
        return done

    done = asyncio.run(_collect())
    assert done.cache_write_tokens == expected


def test_anthropic_split_message_delta_frames_merge_into_one_terminal(monkeypatch) -> None:
    """Anthropic-shaped endpoints may send usage and stop_reason across two
    message_delta frames; the second frame must merge, not fail the stream,
    and a sparse epilogue must not regress earlier usage or stop_reason."""

    sse_events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 10},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "ok"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 5}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(sse_events),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)

    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    async def _collect() -> list[object]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                config=ChatConfig(),
            )
        ]

    events = asyncio.run(_collect())

    assert not any(isinstance(event, ErrorEvent) for event in events)
    (done,) = [event for event in events if isinstance(event, DoneEvent)]
    assert done.stop_reason == "tool_use"
    assert done.input_tokens == 10
    assert done.output_tokens == 5


def test_anthropic_stop_reason_survives_a_usage_only_epilogue_delta(monkeypatch) -> None:
    sse_events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_1",
                "model": "claude-opus-4-7",
                "usage": {"input_tokens": 10},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "ok"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_delta", "delta": {}, "usage": {"cache_read_input_tokens": 3}},
        {"type": "message_stop"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_body(sse_events),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)

    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    async def _collect() -> DoneEvent:
        done: DoneEvent | None = None
        async for ev in provider.chat([Message(role="user", content="hi")], config=ChatConfig()):
            assert not isinstance(ev, ErrorEvent)
            if isinstance(ev, DoneEvent):
                done = ev
        assert done is not None
        return done

    done = asyncio.run(_collect())
    assert done.stop_reason == "tool_use"
    assert done.output_tokens == 5
    assert done.cached_tokens == 3


def test_anthropic_http_error_with_non_utf8_body_yields_error_event(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"content-type": "application/json"},
            content=b"\xffrate limited",
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)
    provider = AnthropicProvider(api_key="test", model="claude-opus-4-7")

    async def _collect() -> list[object]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                config=ChatConfig(),
            )
        ]

    events = asyncio.run(_collect())

    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ErrorEvent)
    assert error.code == "429"
    assert error.message.startswith("HTTP 429:")


def test_list_models_rows_match_retired_known_models_table() -> None:
    # list_models now builds its rows from the shared catalog's corrections
    # data instead of the retired _KNOWN_MODELS adapter table. These literals
    # are the exact rows that table produced (per-1k costs on the wire).
    expected = [
        ModelInfo(
            provider="anthropic",
            model_id="claude-opus-4-6",
            display_name="Claude Opus 4.6",
            context_window=200000,
            max_output_tokens=32000,
            input_cost_per_1k=0.005,
            output_cost_per_1k=0.025,
        ),
        ModelInfo(
            provider="anthropic",
            model_id="claude-sonnet-4-6",
            display_name="Claude Sonnet 4.6",
            context_window=200000,
            max_output_tokens=16000,
            input_cost_per_1k=0.003,
            output_cost_per_1k=0.015,
        ),
        ModelInfo(
            provider="anthropic",
            model_id="claude-haiku-4-5-20251001",
            display_name="Claude Haiku 4.5",
            context_window=200000,
            max_output_tokens=8192,
            input_cost_per_1k=0.001,
            output_cost_per_1k=0.005,
        ),
    ]

    rows = asyncio.run(AnthropicProvider(api_key="test").list_models())

    assert rows == expected
