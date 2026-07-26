"""Terminal-evidence contracts for native tool streams.

Tool starts and argument deltas are diagnostic stream events.  A tool becomes
executable only after both its own completion event and the response-level
success terminal arrive.  Abnormal EOF and provider error terminals must not
manufacture ``ToolUseEndEvent`` or ``DoneEvent`` objects from partial state.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest

from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.openai_codex import OpenAICodexProvider
from opensquilla.provider.types import (
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

_SEARCH_TOOL = ToolDefinition(
    name="search",
    description="Search things.",
    input_schema=ToolInputSchema(
        properties={"query": {"type": "string"}},
        required=["query"],
        additionalProperties=False,
    ),
)


def _sse(events: Iterable[dict[str, Any]], *, anthropic: bool = False) -> bytes:
    parts: list[bytes] = []
    for event in events:
        if anthropic:
            parts.append(f"event: {event['type']}\n".encode())
        parts.append(f"data: {json.dumps(event)}\n\n".encode())
    return b"".join(parts)


def _patch_stream(monkeypatch: Any, module: str, body: bytes) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )
    )
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        f"opensquilla.provider.{module}.httpx.AsyncClient",
        patched_async_client,
    )


def _collect(provider: Any, *, config: ChatConfig | None = None) -> list[Any]:
    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                tools=[_SEARCH_TOOL],
                config=config or ChatConfig(),
            )
        ]

    return asyncio.run(run())


def _assert_incomplete(
    events: list[Any],
    *,
    code: str,
    expect_start: bool = True,
) -> None:
    assert any(isinstance(event, ToolUseStartEvent) for event in events) is expect_start
    assert not any(isinstance(event, ToolUseEndEvent) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == code
        for event in events
    )


def _anthropic_tool_prefix(
    *,
    arguments: str = '{"query":"x"}',
    tool_name: Any = "search",
) -> list[dict[str, Any]]:
    return [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "usage": {"input_tokens": 2}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": tool_name,
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": arguments},
        },
    ]


def test_anthropic_eof_with_partial_tool_has_no_end_or_done(monkeypatch: Any) -> None:
    events = _anthropic_tool_prefix(arguments='{"que')
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(observed, code="incomplete_stream")


def test_anthropic_block_stop_without_message_stop_is_not_authoritative(
    monkeypatch: Any,
) -> None:
    events = [*_anthropic_tool_prefix(), {"type": "content_block_stop", "index": 0}]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(observed, code="incomplete_stream")


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param('{"que', id="malformed-json"),
        pytest.param('["not", "an", "object"]', id="json-non-object"),
        pytest.param('{"query":1e999}', id="non-finite-overflow"),
    ],
)
def test_anthropic_invalid_tool_args_do_not_close_on_message_stop(
    monkeypatch: Any,
    arguments: str,
) -> None:
    events = [
        *_anthropic_tool_prefix(arguments=arguments),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(observed, code="incomplete_tool_call")


@pytest.mark.parametrize(
    ("second_id", "second_name"),
    [
        ("toolu_2", "search"),
        ("toolu_1", "replace"),
    ],
)
def test_anthropic_repeated_block_start_with_conflicting_identity_fails_closed(
    monkeypatch: Any,
    second_id: str,
    second_name: str,
) -> None:
    events = [
        *_anthropic_tool_prefix(),
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": second_id,
                "name": second_name,
            },
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(observed, code="incomplete_tool_call")
    (tool_start,) = [
        event for event in observed if isinstance(event, ToolUseStartEvent)
    ]
    assert (tool_start.tool_use_id, tool_start.tool_name) == ("toolu_1", "search")


@pytest.mark.parametrize(
    "tool_name",
    [
        pytest.param("", id="empty-name"),
        pytest.param(123, id="non-string-name"),
        pytest.param("x" * 257, id="oversized-name"),
    ],
)
def test_anthropic_invalid_tool_name_does_not_close_on_message_stop(
    monkeypatch: Any,
    tool_name: Any,
) -> None:
    events = [
        *_anthropic_tool_prefix(tool_name=tool_name),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(
        observed,
        code="incomplete_tool_call",
        expect_start=False,
    )


def test_anthropic_candidate_mode_demotes_oversized_tool_name_after_terminal(
    monkeypatch: Any,
) -> None:
    oversized_name = "proposed_action_" + ("x" * 17_000)
    events = [
        *_anthropic_tool_prefix(
            tool_name=oversized_name,
            arguments='{"city":"Shanghai"}',
        ),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "analysis"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 7},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(
        AnthropicProvider(api_key="test", model="claude-test"),
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, ErrorEvent) for event in observed)
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in observed
    )
    artifact_event = next(
        event
        for event in observed
        if isinstance(event, TextDeltaEvent)
        and "inert_proposer_tool_output" in event.text
    )
    artifact = json.loads(artifact_event.text)
    assert artifact["kind"] == "inert_proposer_tool_output"
    assert artifact["executable"] is False
    assert artifact["actions"] == [
        {
            "arguments_text": '{"city":"Shanghai"}',
            "issues": ["name_over_execution_limit"],
            "name_text": oversized_name,
        }
    ]
    done = next(event for event in observed if isinstance(event, DoneEvent))
    assert done.stop_reason == "tool_use"
    assert done.input_tokens == 2
    assert done.output_tokens == 7
    assert "".join(
        event.text for event in observed if isinstance(event, TextDeltaEvent)
    ).startswith("analysis\n{")
    assert observed.index(artifact_event) < observed.index(done)


def test_anthropic_candidate_mode_does_not_publish_artifact_before_message_stop(
    monkeypatch: Any,
) -> None:
    events = [
        *_anthropic_tool_prefix(),
        {"type": "content_block_stop", "index": 0},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(
        AnthropicProvider(api_key="test", model="claude-test"),
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_stream"
        for event in observed
    )
    assert not any(isinstance(event, TextDeltaEvent) for event in observed)
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in observed
    )
    assert not any(isinstance(event, DoneEvent) for event in observed)


def test_anthropic_candidate_mode_keeps_empty_initial_tool_input(
    monkeypatch: Any,
) -> None:
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "usage": {"input_tokens": 2}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "get_status",
                "input": {},
            },
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(
        AnthropicProvider(api_key="test", model="claude-test"),
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    artifact_text = next(
        event.text for event in observed if isinstance(event, TextDeltaEvent)
    )
    assert json.loads(artifact_text)["actions"] == [
        {
            "arguments_text": "{}",
            "issues": [],
            "name_text": "get_status",
        }
    ]
    assert not any(isinstance(event, ErrorEvent) for event in observed)
    assert any(isinstance(event, DoneEvent) for event in observed)


def test_anthropic_candidate_mode_drops_nameless_empty_tool_block(
    monkeypatch: Any,
) -> None:
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "usage": {"input_tokens": 2}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_private",
                "input": {},
            },
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(
        AnthropicProvider(api_key="test", model="claude-test"),
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, TextDeltaEvent) for event in observed)
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in observed
    )
    assert not any(isinstance(event, ErrorEvent) for event in observed)
    assert any(isinstance(event, DoneEvent) for event in observed)


def test_anthropic_candidate_mode_rejects_duplicate_tool_block_stop(
    monkeypatch: Any,
) -> None:
    events = [
        *_anthropic_tool_prefix(),
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(
        AnthropicProvider(api_key="test", model="claude-test"),
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, TextDeltaEvent) for event in observed)
    assert not any(isinstance(event, DoneEvent) for event in observed)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in observed
    )


def test_anthropic_candidate_mode_rejects_unmatched_content_block_stop(
    monkeypatch: Any,
) -> None:
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "usage": {"input_tokens": 2}},
        },
        {"type": "content_block_stop", "index": 9},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(
        AnthropicProvider(api_key="test", model="claude-test"),
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, TextDeltaEvent) for event in observed)
    assert not any(isinstance(event, DoneEvent) for event in observed)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in observed
    )


def test_anthropic_error_after_block_stop_discards_deferred_end(
    monkeypatch: Any,
) -> None:
    events = [
        *_anthropic_tool_prefix(),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "try later"},
        },
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(observed, code="overloaded_error")


def test_anthropic_delta_after_block_stop_discards_deferred_end(
    monkeypatch: Any,
) -> None:
    events = [
        *_anthropic_tool_prefix(),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"late"'},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(observed, code="incomplete_tool_call")
    assert not any(
        isinstance(event, ToolUseDeltaEvent) and event.json_fragment == '"late"'
        for event in observed
    )


def test_anthropic_malformed_data_frame_cannot_be_laundered_by_message_stop(
    monkeypatch: Any,
) -> None:
    before_invalid_frame = [
        *_anthropic_tool_prefix(),
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "visible before bad frame"},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    after_invalid_frame = [
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    body = (
        _sse(before_invalid_frame, anthropic=True)
        + b"data: {not-json\n\n"
        + _sse(after_invalid_frame, anthropic=True)
    )
    _patch_stream(monkeypatch, "anthropic", body)

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(observed, code="invalid_stream_frame")
    assert [event.text for event in observed if isinstance(event, TextDeltaEvent)] == [
        "visible before bad frame"
    ]
    assert any(isinstance(event, ToolUseDeltaEvent) for event in observed)


def test_anthropic_duplicate_tool_id_across_block_indices_fails_closed(
    monkeypatch: Any,
) -> None:
    events = [
        *_anthropic_tool_prefix(),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "search",
            },
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"query":"y"}'},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(observed, code="incomplete_tool_call")


def test_anthropic_tool_block_after_message_delta_poison_response(
    monkeypatch: Any,
) -> None:
    events = [
        *_anthropic_tool_prefix(),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 1},
        },
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_late",
                "name": "search",
            },
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(observed, code="invalid_stream_order")


def test_anthropic_server_tool_block_deltas_do_not_fail_the_response(
    monkeypatch: Any,
) -> None:
    """input_json_delta for a non-client-tool block (e.g. server_tool_use) is
    tolerated diagnostics: the fully streamed response must still commit."""
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "usage": {"input_tokens": 2}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "web_search",
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"query":"x"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "hello"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 3},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    assert not any(isinstance(event, ErrorEvent) for event in observed)
    assert any(isinstance(event, DoneEvent) for event in observed)
    assert [event.text for event in observed if isinstance(event, TextDeltaEvent)] == ["hello"]
    # Server-side tool activity never surfaces as client tool events.
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in observed
    )


def test_anthropic_server_tool_block_coexists_with_client_tool_call(
    monkeypatch: Any,
) -> None:
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "usage": {"input_tokens": 2}},
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "server_tool_use",
                "id": "srvtoolu_1",
                "name": "web_search",
            },
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"query":"srv"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "search"},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"query":"x"}'},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    assert not any(isinstance(event, ErrorEvent) for event in observed)
    (tool_end,) = [event for event in observed if isinstance(event, ToolUseEndEvent)]
    assert (tool_end.tool_use_id, tool_end.arguments) == ("toolu_1", {"query": "x"})
    assert any(isinstance(event, DoneEvent) for event in observed)


def test_anthropic_delta_for_never_opened_index_still_fails_closed(
    monkeypatch: Any,
) -> None:
    events = [
        {
            "type": "message_start",
            "message": {"id": "msg_1", "usage": {"input_tokens": 2}},
        },
        {
            "type": "content_block_delta",
            "index": 7,
            "delta": {"type": "input_json_delta", "partial_json": "{}"},
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    _patch_stream(monkeypatch, "anthropic", _sse(events, anthropic=True))

    observed = _collect(AnthropicProvider(api_key="test", model="claude-test"))

    _assert_incomplete(observed, code="incomplete_tool_call", expect_start=False)


def _codex_auth(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "access-test",
                    "refresh_token": "refresh-test",
                    "account_id": "account-test",
                    "id_token": "",
                },
            }
        )
    )
    return path


def _codex_tool_prefix(
    *,
    arguments: str = '{"query":"x"}',
    tool_name: Any = "search",
) -> list[dict[str, Any]]:
    return [
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": tool_name,
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": arguments,
        },
    ]


def _codex_provider(tmp_path: Path) -> OpenAICodexProvider:
    return OpenAICodexProvider(auth_path=str(_codex_auth(tmp_path / "auth.json")))


def test_codex_eof_with_partial_tool_has_no_end_or_done(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _patch_stream(monkeypatch, "openai_codex", _sse(_codex_tool_prefix(arguments='{"que')))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="incomplete_stream")


def test_codex_item_done_without_response_completed_is_not_authoritative(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    events = [
        *_codex_tool_prefix(),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"query":"x"}',
            },
        },
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="incomplete_stream")


def test_codex_completed_response_with_open_tool_is_incomplete(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    events = [
        *_codex_tool_prefix(arguments='{"que'),
        {"type": "response.completed", "response": {"id": "resp_1", "usage": {}}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="incomplete_tool_call")


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param('{"que', id="malformed-json"),
        pytest.param('["not", "an", "object"]', id="json-non-object"),
        pytest.param('{"query":1e999}', id="non-finite-overflow"),
    ],
)
def test_codex_invalid_terminal_arguments_do_not_close_on_response_completed(
    tmp_path: Path,
    monkeypatch: Any,
    arguments: str,
) -> None:
    events = [
        *_codex_tool_prefix(arguments=arguments),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": arguments,
            },
        },
        {"type": "response.completed", "response": {"id": "resp_1", "usage": {}}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="incomplete_tool_call")


@pytest.mark.parametrize(
    "tool_name",
    [
        pytest.param("", id="empty-name"),
        pytest.param(123, id="non-string-name"),
        pytest.param("x" * 257, id="oversized-name"),
    ],
)
def test_codex_invalid_final_tool_name_does_not_close_on_response_completed(
    tmp_path: Path,
    monkeypatch: Any,
    tool_name: Any,
) -> None:
    events = [
        *_codex_tool_prefix(),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": tool_name,
                "arguments": '{"query":"x"}',
            },
        },
        {"type": "response.completed", "response": {"id": "resp_1", "usage": {}}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="incomplete_tool_call")


def test_codex_final_item_can_supply_a_late_tool_name(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    events = [
        *_codex_tool_prefix(tool_name=""),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"query":"x"}',
            },
        },
        {"type": "response.completed", "response": {"id": "resp_1", "usage": {}}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    lifecycle = [
        event
        for event in observed
        if isinstance(
            event,
            (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent),
        )
    ]
    assert [type(event) for event in lifecycle] == [
        ToolUseStartEvent,
        ToolUseDeltaEvent,
        ToolUseEndEvent,
    ]
    tool_start = lifecycle[0]
    tool_end = lifecycle[-1]
    assert tool_start.tool_name == "search"
    assert tool_start.tool_use_id == tool_end.tool_use_id == "call_1"
    assert tool_end.tool_name == "search"
    assert tool_end.arguments == {"query": "x"}
    assert any(isinstance(event, DoneEvent) for event in observed)
    assert not any(isinstance(event, ErrorEvent) for event in observed)


@pytest.mark.parametrize(
    ("done_call_id", "done_name"),
    [
        ("call_2", "search"),
        ("call_1", "replace"),
    ],
)
def test_codex_item_done_with_conflicting_identity_fails_closed(
    tmp_path: Path,
    monkeypatch: Any,
    done_call_id: str,
    done_name: str,
) -> None:
    events = [
        *_codex_tool_prefix(),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": done_call_id,
                "name": done_name,
                "arguments": '{"query":"x"}',
            },
        },
        {"type": "response.completed", "response": {"id": "resp_1", "usage": {}}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="incomplete_tool_call")
    (tool_start,) = [
        event for event in observed if isinstance(event, ToolUseStartEvent)
    ]
    assert (tool_start.tool_use_id, tool_start.tool_name) == ("call_1", "search")


def test_codex_failed_response_discards_completed_item_end(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    events = [
        *_codex_tool_prefix(),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"query":"x"}',
            },
        },
        {
            "type": "response.failed",
            "response": {"error": {"code": "failed", "message": "failed"}},
        },
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="failed")


def test_codex_delta_after_item_done_discards_deferred_end(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    events = [
        *_codex_tool_prefix(),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"query":"x"}',
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": '"late"',
        },
        {"type": "response.completed", "response": {"id": "resp_1", "usage": {}}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="incomplete_tool_call")
    assert not any(
        isinstance(event, ToolUseDeltaEvent) and event.json_fragment == '"late"'
        for event in observed
    )


def test_codex_malformed_data_frame_cannot_be_laundered_by_response_completed(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    before_invalid_frame = [
        *_codex_tool_prefix(),
        {"type": "response.output_text.delta", "delta": "visible before bad frame"},
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"query":"x"}',
            },
        },
    ]
    after_invalid_frame = [
        {"type": "response.completed", "response": {"id": "resp_1", "usage": {}}},
    ]
    body = _sse(before_invalid_frame) + b"data: {not-json\n\n" + _sse(after_invalid_frame)
    _patch_stream(monkeypatch, "openai_codex", body)

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="invalid_stream_frame")
    assert [event.text for event in observed if isinstance(event, TextDeltaEvent)] == [
        "visible before bad frame"
    ]
    assert any(isinstance(event, ToolUseDeltaEvent) for event in observed)


def test_codex_duplicate_call_id_across_items_rejects_all_deferred_ends(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    events = [
        *_codex_tool_prefix(),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"query":"x"}',
            },
        },
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_2",
                "call_id": "call_1",
                "name": "search",
            },
        },
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="incomplete_tool_call")


def test_codex_top_level_error_poison_discards_deferred_end(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    events = [
        *_codex_tool_prefix(),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"query":"x"}',
            },
        },
        {"type": "error", "error": {"code": "upstream", "message": "failed"}},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="upstream")


def test_codex_completed_event_with_failed_status_cannot_commit_tool(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    events = [
        *_codex_tool_prefix(),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": '{"query":"x"}',
            },
        },
        {"type": "response.completed", "response": {"status": "failed"}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="invalid_response_status")


def test_codex_numeric_call_id_is_not_coerced_into_public_identity(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    events = [
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": 123,
                "name": "search",
            },
        },
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(
        observed,
        code="incomplete_tool_call",
        expect_start=False,
    )


@pytest.mark.parametrize(
    "arguments",
    [None, {}, [], 0, False],
    ids=["null", "object", "array", "zero", "false"],
)
def test_codex_non_string_authoritative_arguments_cannot_become_empty_object(
    tmp_path: Path,
    monkeypatch: Any,
    arguments: Any,
) -> None:
    events = [
        *_codex_tool_prefix(),
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "search",
                "arguments": arguments,
            },
        },
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    _assert_incomplete(observed, code="incomplete_tool_call")


@pytest.mark.parametrize(
    "completed_body",
    [None, [], "", 0, False],
    ids=["null", "array", "empty-string", "zero", "false"],
)
def test_codex_completed_requires_an_explicit_response_object(
    tmp_path: Path,
    monkeypatch: Any,
    completed_body: Any,
) -> None:
    events = [{"type": "response.completed", "response": completed_body}]
    _patch_stream(monkeypatch, "openai_codex", _sse(events))

    observed = _collect(_codex_provider(tmp_path))

    assert not any(isinstance(event, (ToolUseEndEvent, DoneEvent)) for event in observed)
    assert [event.code for event in observed if isinstance(event, ErrorEvent)] == [
        "invalid_response"
    ]
