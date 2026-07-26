from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.provider.ollama import OllamaProvider
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.stream_assembly import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_MAX_TOTAL_TOOL_ARGUMENT_CHARS,
)

_TOOL_EVENTS = (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent)


def _patch_http_response(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    body: bytes,
    *,
    content_type: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(f"{module}.httpx.AsyncClient", patched_async_client)


def _ndjson(*chunks: dict[str, Any]) -> bytes:
    return b"".join((json.dumps(chunk) + "\n").encode() for chunk in chunks)


def _lookup_tool() -> ToolDefinition:
    return ToolDefinition(
        name="lookup",
        description="Look up a value.",
        input_schema=ToolInputSchema(
            properties={"q": {"type": "string"}},
            required=["q"],
        ),
    )


def _collect(provider: Any) -> list[Any]:
    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="look it up")],
                tools=[_lookup_tool()],
                config=ChatConfig(max_tokens=32),
            )
        ]

    return asyncio.run(run())


def test_ollama_eof_without_done_preserves_text_but_never_commits_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _ndjson(
        {
            "model": "test-model",
            "message": {
                "role": "assistant",
                "content": "partial text",
                "tool_calls": [
                    {"function": {"name": "lookup", "arguments": {"q": "partial"}}}
                ],
            },
            "done": False,
        }
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )

    events = _collect(OllamaProvider(model="test-model"))

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "partial text"
    ]
    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert [(event.code, event.message) for event in errors] == [
        ("incomplete_stream", "Ollama stream ended before done=true")
    ]


def test_ollama_malformed_frame_cannot_be_laundered_by_later_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (
        _ndjson(
            {
                "model": "test-model",
                "message": {
                    "role": "assistant",
                    "content": "partial text",
                    "tool_calls": [
                        {"function": {"name": "lookup", "arguments": {"q": "partial"}}}
                    ],
                },
                "done": False,
            }
        )
        + b'{"malformed"\n'
        + _ndjson(
            {
                "model": "test-model",
                "message": {"role": "assistant", "content": ""},
                "done": True,
            }
        )
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )

    events = _collect(OllamaProvider(model="test-model"))

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "partial text"
    ]
    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "invalid_stream_frame"
    ]


def test_ollama_done_true_commits_buffered_tool_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _ndjson(
        {
            "model": "test-model",
            "message": {
                "role": "assistant",
                "content": "checking",
                "tool_calls": [
                    {"function": {"name": "lookup", "arguments": {"q": "complete"}}}
                ],
            },
            "done": False,
        },
        {
            "model": "test-model",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 8,
            "eval_count": 3,
        },
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )

    events = _collect(OllamaProvider(model="test-model"))

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.tool_name == "lookup"
    assert tool_end.arguments == {"q": "complete"}
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert (done.input_tokens, done.output_tokens) == (8, 3)
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        pytest.param("lookup", '{"q":', id="malformed-json-string"),
        pytest.param("lookup", ["not", "an", "object"], id="json-non-object"),
        pytest.param("", {"q": "complete"}, id="empty-tool-name"),
        pytest.param("x" * 257, {"q": "complete"}, id="oversized-tool-name"),
    ],
)
def test_ollama_done_true_rejects_invalid_native_tool_call_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: Any,
) -> None:
    body = _ndjson(
        {
            "model": "test-model",
            "message": {
                "role": "assistant",
                "content": "partial text",
                "tool_calls": [
                    {"function": {"name": "lookup", "arguments": {"q": "valid"}}},
                    {"function": {"name": tool_name, "arguments": arguments}}
                ],
            },
            "done": False,
        },
        {
            "model": "test-model",
            "message": {"role": "assistant", "content": ""},
            "done": True,
        },
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )

    events = _collect(OllamaProvider(model="test-model"))

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "partial text"
    ]
    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "incomplete_tool_call"
    ]


def test_ollama_duplicate_tool_ids_fail_before_any_tool_lifecycle_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _ndjson(
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "duplicate",
                        "function": {"name": "lookup", "arguments": {"q": "a"}},
                    },
                    {
                        "id": "duplicate",
                        "function": {"name": "lookup", "arguments": {"q": "b"}},
                    },
                ],
            },
            "done": True,
        }
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )

    events = _collect(OllamaProvider(model="test-model"))

    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "incomplete_tool_call"
    ]


def test_ollama_applies_tool_call_limit_before_terminal_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _ndjson(
        *(
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call_{index}",
                            "function": {
                                "name": "lookup",
                                "arguments": {"q": str(index)},
                            },
                        }
                    ],
                },
                "done": False,
            }
            for index in range(DEFAULT_MAX_TOOL_CALLS + 1)
        )
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )

    events = _collect(OllamaProvider(model="test-model"))

    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "incomplete_tool_call"
    ]


def test_ollama_applies_aggregate_argument_limit_while_ingesting_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argument_chars = 180_000
    assert argument_chars * 6 > DEFAULT_MAX_TOTAL_TOOL_ARGUMENT_CHARS
    body = _ndjson(
        *(
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call_{index}",
                            "function": {
                                "name": "lookup",
                                "arguments": {"q": "x" * argument_chars},
                            },
                        }
                    ],
                },
                "done": False,
            }
            for index in range(6)
        )
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )

    events = _collect(OllamaProvider(model="test-model"))

    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "incomplete_tool_call"
    ]


@pytest.mark.parametrize(
    "error_payload",
    [
        pytest.param("model runner failed", id="message"),
        pytest.param({}, id="empty-object"),
        pytest.param("", id="empty-string"),
        pytest.param(False, id="false"),
        pytest.param(0, id="zero"),
        pytest.param([], id="empty-array"),
    ],
)
def test_ollama_error_frame_poison_wins_over_done_and_buffered_tools(
    monkeypatch: pytest.MonkeyPatch,
    error_payload: Any,
) -> None:
    body = _ndjson(
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "lookup", "arguments": {"q": "x"}}}
                ],
            },
            "done": False,
        },
        {"error": error_payload, "done": True},
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )

    events = _collect(OllamaProvider(model="test-model"))

    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "stream_error"
    ]


def test_ollama_null_error_field_does_not_poison_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _ndjson(
        {
            "error": None,
            "message": {"content": "ok"},
            "done": True,
        }
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.ollama",
        body,
        content_type="application/x-ndjson",
    )

    events = _collect(OllamaProvider(model="test-model"))

    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)


def _responses_body(
    *,
    status: str,
    extra: dict[str, Any] | None = None,
    arguments: str = '{"q":"complete"}',
) -> bytes:
    payload: dict[str, Any] = {
        "id": f"resp_{status}",
        "model": "gpt-test",
        "status": status,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "partial text"}],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": arguments,
            },
        ],
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }
    payload.update(extra or {})
    return json.dumps(payload).encode()


def test_responses_max_output_tokens_keeps_length_continuation_but_drops_partial_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _responses_body(
        status="incomplete",
        extra={"incomplete_details": {"reason": "max_output_tokens"}},
        arguments='{"q":"unfinished',
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        body,
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "partial text"
    ]
    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == "length"
    assert (done.input_tokens, done.output_tokens) == (11, 7)


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        pytest.param("lookup", '{"q":', id="malformed-json"),
        pytest.param("lookup", '["not", "an", "object"]', id="json-non-object"),
        pytest.param("lookup", '{"q":1e999}', id="non-finite-number"),
        pytest.param("", '{"q":"complete"}', id="empty-tool-name"),
        pytest.param("x" * 257, '{"q":"complete"}', id="oversized-tool-name"),
    ],
)
def test_responses_completed_rejects_invalid_native_tool_call_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: str,
) -> None:
    payload = json.loads(_responses_body(status="completed", arguments=arguments))
    payload["output"][-1]["name"] = tool_name
    payload["output"].insert(
        1,
        {
            "type": "function_call",
            "id": "fc_valid",
            "call_id": "call_valid",
            "name": "lookup",
            "arguments": '{"q":"valid"}',
        },
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        json.dumps(payload).encode(),
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "partial text"
    ]
    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "incomplete_tool_call"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("call_id", 123, id="numeric-call-id"),
        pytest.param("call_id", "", id="empty-call-id"),
        pytest.param("id", ["unhashable"], id="list-item-id"),
        pytest.param("id", {}, id="mapping-item-id"),
    ],
)
def test_responses_completed_rejects_invalid_tool_identity_atomically(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    payload = json.loads(_responses_body(status="completed"))
    payload["output"].insert(
        1,
        {
            "type": "function_call",
            "id": "fc_valid",
            "call_id": "call_valid",
            "name": "lookup",
            "arguments": '{"q":"valid"}',
        },
    )
    payload["output"][-1][field] = value
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        json.dumps(payload).encode(),
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "partial text"
    ]
    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "incomplete_tool_call"
    ]


def test_responses_completed_rejects_non_array_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(_responses_body(status="completed"))
    payload["output"] = {"type": "message"}
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        json.dumps(payload).encode(),
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    assert not any(isinstance(event, (TextDeltaEvent, *_TOOL_EVENTS)) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "invalid_response"
    ]


def test_responses_duplicate_public_tool_id_is_rejected_before_first_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(_responses_body(status="completed"))
    payload["output"].append(
        {
            "type": "function_call",
            "id": "fc_2",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"q":"second"}',
        }
    )
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        json.dumps(payload).encode(),
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "partial text"
    ]
    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "incomplete_tool_call"
    ]


@pytest.mark.parametrize(
    "malformed",
    ["item", "content", "output_text", "unknown_content_part"],
)
def test_responses_malformed_message_batch_cannot_be_silently_dropped(
    monkeypatch: pytest.MonkeyPatch,
    malformed: str,
) -> None:
    payload = json.loads(_responses_body(status="completed"))
    payload["output"] = payload["output"][:1]
    if malformed == "item":
        payload["output"][0] = "not-an-item"
    elif malformed == "content":
        payload["output"][0]["content"] = {"type": "output_text"}
    elif malformed == "output_text":
        payload["output"][0]["content"][0]["text"] = 123
    else:
        payload["output"][0]["content"][0] = {
            "type": "unknown_assistant_content",
            "text": "must not be dropped",
        }
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        json.dumps(payload).encode(),
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    assert not any(isinstance(event, (TextDeltaEvent, *_TOOL_EVENTS)) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "invalid_response"
    ]


@pytest.mark.parametrize(
    "error_payload",
    [
        pytest.param({}, id="empty-object"),
        pytest.param("", id="empty-string"),
        pytest.param(False, id="false"),
        pytest.param(0, id="zero"),
        pytest.param([], id="empty-array"),
    ],
)
def test_responses_completed_explicit_empty_error_field_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    error_payload: Any,
) -> None:
    payload = json.loads(_responses_body(status="completed"))
    payload["error"] = error_payload
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        json.dumps(payload).encode(),
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    assert not any(isinstance(event, (TextDeltaEvent, *_TOOL_EVENTS)) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert [event.code for event in events if isinstance(event, ErrorEvent)] == [
        "invalid_response"
    ]


def test_responses_completed_null_error_field_does_not_poison_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(_responses_body(status="completed"))
    payload["error"] = None
    payload["output"] = payload["output"][:1]
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        json.dumps(payload).encode(),
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)


def test_responses_refusal_is_visible_terminal_text_not_a_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(_responses_body(status="completed"))
    payload["output"] = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "refusal", "refusal": "I cannot do that."}],
        }
    ]
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        json.dumps(payload).encode(),
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "I cannot do that."
    ]
    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert next(event for event in events if isinstance(event, DoneEvent)).stop_reason == (
        "end_turn"
    )


@pytest.mark.parametrize(
    ("status", "extra", "expected_code"),
    [
        (
            "failed",
            {"error": {"code": "server_error", "message": "upstream failed"}},
            "server_error",
        ),
        ("cancelled", {}, "response_cancelled"),
        (
            "incomplete",
            {"incomplete_details": {"reason": "content_filter"}},
            "response_incomplete",
        ),
    ],
)
def test_responses_non_success_status_never_commits_tool_or_done(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    extra: dict[str, Any],
    expected_code: str,
) -> None:
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        _responses_body(status=status, extra=extra),
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "partial text"
    ]
    assert not any(isinstance(event, _TOOL_EVENTS) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert [event.code for event in errors] == [expected_code]


def test_responses_completed_status_commits_tool_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_http_response(
        monkeypatch,
        "opensquilla.provider.openai_responses",
        _responses_body(status="completed"),
        content_type="application/json",
    )

    events = _collect(OpenAIResponsesProvider(api_key="test", model="gpt-test"))

    tool_end = next(event for event in events if isinstance(event, ToolUseEndEvent))
    assert tool_end.tool_name == "lookup"
    assert tool_end.arguments == {"q": "complete"}
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == "tool_use"
    assert not any(isinstance(event, ErrorEvent) for event in events)
