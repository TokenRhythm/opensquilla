"""OpenAI-compatible text/tool boundary contracts.

These tests exercise the provider directly.  They intentionally do not use
the engine's protocol-text filter: the adapter must decide whether bytes are
literal text, a provider-specific text tool call, or a native-call scaffold
before any candidate bytes become ``TextDeltaEvent`` objects.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Iterable
from typing import Any

import httpx
import pytest
import structlog.testing

from opensquilla.provider.compat_policy import (
    TEXT_TOOL_DIALECT_MINIMAX_XML,
    TEXT_TOOL_DIALECT_PLAIN_JSON,
    TEXT_TOOL_DIALECT_QWEN_TAG,
    OpenAICompatPolicy,
    TextToolCompatProfile,
    TextToolDialect,
    compat_policy_for_kind,
)
from opensquilla.provider.openai import OpenAIProvider, _DeferredStreamEventBuffer
from opensquilla.provider.text_tool_normalizer import (
    PLAIN_TOOL_INTERSTITIAL_WHITESPACE_MAX,
    LiteralTextSegment,
    SyntheticToolSegment,
    TextToolStreamNormalizer,
    _raw_html_code_ranges,
    classify_text_tool_segments,
)
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

_REAL_ASYNC_CLIENT = httpx.AsyncClient


_SEARCH_TOOL = ToolDefinition(
    name="search",
    description="Search things.",
    input_schema=ToolInputSchema(
        properties={"query": {"type": "string"}},
        required=["query"],
        additionalProperties=False,
    ),
)

_EDIT_TOOL = ToolDefinition(
    name="edit_file",
    description="Edit a file.",
    input_schema=ToolInputSchema(
        properties={
            "path": {"type": "string"},
            "new_text": {"type": "string"},
        },
        required=["path", "new_text"],
        additionalProperties=False,
    ),
)

_TYPED_TOOL = ToolDefinition(
    name="typed",
    description="Exercise XML value decoding.",
    input_schema=ToolInputSchema(
        properties={
            "count": {"type": "integer"},
            "enabled": {"type": "boolean"},
            "items": {"type": "array"},
            "options": {"type": "object"},
            "nothing": {"type": "null"},
            "string_or_integer": {"type": ["string", "integer"]},
        },
        required=[
            "count",
            "enabled",
            "items",
            "options",
            "nothing",
            "string_or_integer",
        ],
        additionalProperties=False,
    ),
)

_AMBIGUOUS_TOOL = ToolDefinition(
    name="ambiguous",
    description="Reject ambiguous XML scalar coercion.",
    input_schema=ToolInputSchema(
        properties={
            "value": {
                "anyOf": [{"type": "integer"}, {"type": "number"}],
            }
        },
        required=["value"],
        additionalProperties=False,
    ),
)

_NUMBER_TOOL = ToolDefinition(
    name="number_tool",
    description="Accept a finite number.",
    input_schema=ToolInputSchema(
        properties={"value": {"type": "number"}},
        required=["value"],
        additionalProperties=False,
    ),
)

_QWEN_CALL = (
    '<tool_call>{"name":"search","arguments":{"query":"x"}}</tool_call>'
)
_MINIMAX_CALL = (
    "<minimax:tool_call>"
    '<invoke name="search"><parameter name="query">x</parameter></invoke>'
    "</minimax:tool_call>"
)
_PLAIN_CALL = 'search{"query":"x"}'
_DETAILS_SCAFFOLD = (
    "<details><summary>View areas around line 4</summary>"
    "provider-only navigation scaffold</details>"
)
_CODE_VALUE = (
    "`inline`\n"
    "```python\n"
    "    print('x')\n"
    "```\n"
)
_QWEN_EDIT_WITH_CODE = (
    "<tool_call><function=edit_file>"
    "<parameter=path>demo.py</parameter>"
    f"<parameter=new_text>{_CODE_VALUE}literal </function> marker</parameter>"
    "</function></tool_call>"
)
_MINIMAX_EDIT_WITH_CODE = (
    "<minimax:tool_call>"
    '<invoke name="edit_file">'
    '<parameter name="path">demo.py</parameter>'
    f'<parameter name="new_text">{_CODE_VALUE}literal </invoke> marker</parameter>'
    "</invoke></minimax:tool_call>"
)
_TYPED_EXPECTED = {
    "count": 10,
    "enabled": True,
    "items": ["x", 2],
    "options": {"x": 1},
    "nothing": None,
    "string_or_integer": "10",
}
_QWEN_TYPED_CALL = (
    "<tool_call><function=typed>"
    "<parameter=count>10</parameter>"
    "<parameter=enabled>true</parameter>"
    '<parameter=items>["x",2]</parameter>'
    '<parameter=options>{"x":1}</parameter>'
    "<parameter=nothing>null</parameter>"
    "<parameter=string_or_integer>10</parameter>"
    "</function></tool_call>"
)
_MINIMAX_TYPED_CALL = (
    "<minimax:tool_call>"
    '<invoke name="typed">'
    '<parameter name="count">10</parameter>'
    '<parameter name="enabled">true</parameter>'
    '<parameter name="items">["x",2]</parameter>'
    '<parameter name="options">{"x":1}</parameter>'
    '<parameter name="nothing">null</parameter>'
    '<parameter name="string_or_integer">10</parameter>'
    "</invoke></minimax:tool_call>"
)


def _sse(
    text_chunks: Iterable[str] = (),
    *,
    finish_reason: str | None = "stop",
    include_done: bool = True,
    native_tool_call: bool = False,
    native_query: str = "native",
) -> bytes:
    chunks: list[dict[str, Any]] = [
        {"choices": [{"delta": {"content": text}, "finish_reason": None}]}
        for text in text_chunks
    ]
    if native_tool_call:
        chunks.append(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {
                                        "name": "search",
                                        "arguments": json.dumps({"query": native_query}),
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
    if finish_reason is not None:
        chunks.append(
            {"choices": [{"delta": {}, "finish_reason": finish_reason}]}
        )
    body = b"".join(
        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        for chunk in chunks
    )
    if include_done:
        body += b"data: [DONE]\n\n"
    return body


def _raw_sse(chunks: Iterable[dict[str, Any]], *, include_done: bool = True) -> bytes:
    body = b"".join(
        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        for chunk in chunks
    )
    if include_done:
        body += b"data: [DONE]\n\n"
    return body


def _direct_classify(
    text: str,
    *,
    dialect: TextToolDialect,
    tool: ToolDefinition,
) -> list[Any]:
    return classify_text_tool_segments(
        text,
        [tool],
        dialects=frozenset({dialect}),
        provider_kind="profile_test",
        model="profile-test",
    )


def _direct_stream(
    text: str,
    *,
    dialect: TextToolDialect,
    tool: ToolDefinition,
    split: int,
) -> tuple[str, list[Any]]:
    normalizer = TextToolStreamNormalizer(
        tools=[tool],
        dialects=frozenset({dialect}),
        provider_kind="profile_test",
        model="profile-test",
    )
    visible = "".join(normalizer.push(text[:split]))
    visible += "".join(normalizer.push(text[split:]))
    return visible, normalizer.finish(successful_text_tool_terminal=True)


def _direct_stream_chunks(
    chunks: Iterable[str],
    *,
    dialect: TextToolDialect,
    tool: ToolDefinition,
) -> tuple[str, list[Any]]:
    normalizer = TextToolStreamNormalizer(
        tools=[tool],
        dialects=frozenset({dialect}),
        provider_kind="profile_test",
        model="profile-test",
    )
    visible = "".join(
        emitted
        for chunk in chunks
        for emitted in normalizer.push(chunk)
    )
    return visible, normalizer.finish(successful_text_tool_terminal=True)


def _direct_calls(segments: list[Any]) -> list[Any]:
    return [
        call
        for segment in segments
        if isinstance(segment, SyntheticToolSegment)
        for call in segment.calls
    ]


def _collect_stream(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_kind: str,
    model: str,
    text_chunks: Iterable[str],
    tools: list[ToolDefinition] | None = None,
    finish_reason: str | None = "stop",
    include_done: bool = True,
    native_tool_call: bool = False,
    native_query: str = "native",
    compat: OpenAICompatPolicy | None = None,
    raw_body: bytes | None = None,
) -> list[Any]:
    body = raw_body
    if body is None:
        body = _sse(
            text_chunks,
            finish_reason=finish_reason,
            include_done=include_done,
            native_tool_call=native_tool_call,
            native_query=native_query,
        )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        patched_async_client,
    )
    provider = OpenAIProvider(
        api_key="test",
        model=model,
        provider_kind=provider_kind,
        compat=compat,
    )

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                tools=tools,
                config=ChatConfig(),
            )
        ]

    return asyncio.run(run())


def _collect_non_stream(
    monkeypatch: pytest.MonkeyPatch,
    *,
    text: str,
    finish_reason: str | None,
    compat: OpenAICompatPolicy,
    native_arguments: str | None = None,
    native_tool_name: str | None = "search",
    native_calls: list[tuple[str | None, str]] | None = None,
    tools: list[ToolDefinition] | None = None,
) -> list[Any]:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        if payload.get("stream"):
            raise httpx.ReadTimeout("force non-stream fallback")
        message: dict[str, Any] = {"content": text}
        if native_calls is not None:
            tool_calls: list[dict[str, Any]] = []
            for index, (tool_name, arguments) in enumerate(native_calls):
                function: dict[str, Any] = {"arguments": arguments}
                if tool_name is not None:
                    function["name"] = tool_name
                tool_calls.append(
                    {
                        "id": f"call_native_{index}",
                        "type": "function",
                        "function": function,
                    }
                )
            message["tool_calls"] = tool_calls
        elif native_arguments is not None:
            function: dict[str, Any] = {"arguments": native_arguments}
            if native_tool_name is not None:
                function["name"] = native_tool_name
            message["tool_calls"] = [
                {
                    "id": "call_native",
                    "type": "function",
                    "function": function,
                }
            ]
        return httpx.Response(
            200,
            json={
                "model": "profile-test",
                "choices": [
                    {
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {},
            },
        )

    transport = httpx.MockTransport(handler)
    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        patched_async_client,
    )
    provider = OpenAIProvider(
        api_key="test",
        model="profile-test",
        provider_kind="profile_test",
        compat=compat,
    )
    tool_definitions = tools if tools is not None else [_SEARCH_TOOL]

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                tools=tool_definitions,
                config=ChatConfig(),
            )
        ]

    events = asyncio.run(run())
    assert calls == 2
    return events


def _text(events: list[Any]) -> str:
    return "".join(event.text for event in events if isinstance(event, TextDeltaEvent))


def _tool_ends(events: list[Any]) -> list[ToolUseEndEvent]:
    return [event for event in events if isinstance(event, ToolUseEndEvent)]


def _plain_profile() -> OpenAICompatPolicy:
    return OpenAICompatPolicy(
        display_name="Explicit plain-text test profile",
        stream_timeout_fallback=True,
        text_tool_profile=TextToolCompatProfile(
            dialects=frozenset({TEXT_TOOL_DIALECT_PLAIN_JSON})
        ),
    )


@pytest.mark.parametrize(
    ("provider_kind", "model", "expected"),
    [
        ("dashscope", "qwen3.6-flash", {TEXT_TOOL_DIALECT_QWEN_TAG}),
        ("dashscope", "deepseek-v3.2", set()),
        ("minimax", "MiniMax-M2.7", {TEXT_TOOL_DIALECT_MINIMAX_XML}),
        ("openrouter", "minimax/minimax-m2.7", {TEXT_TOOL_DIALECT_MINIMAX_XML}),
        ("openrouter", "deepseek/deepseek-v4", set()),
        ("openrouter", "qwen/qwen3.7", set()),
        ("tokenrhythm", "minimax-m2.7", {TEXT_TOOL_DIALECT_MINIMAX_XML}),
        ("tokenrhythm", "qwen3.7-max", {TEXT_TOOL_DIALECT_QWEN_TAG}),
        ("tokenrhythm", "deepseek-v4-flash", set()),
        ("tokenrhythm", "glm-5.2", set()),
    ],
)
def test_text_tool_profile_is_provider_and_model_scoped(
    provider_kind: str,
    model: str,
    expected: set[str],
) -> None:
    policy = compat_policy_for_kind(provider_kind)
    assert policy.text_tool_profile.dialects_for_model(model) == frozenset(expected)
    assert policy.text_tool_synthesis is bool(expected or policy.text_tool_profile.model_rules)


def test_literal_tool_calls_tag_survives_every_chunk_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    literal = "explain <tool_calls> literally"
    for split in range(1, len(literal)):
        events = _collect_stream(
            monkeypatch,
            provider_kind="dashscope",
            model="qwen3.6-flash",
            text_chunks=[literal[:split], literal[split:]],
            tools=[_SEARCH_TOOL],
        )
        assert _text(events) == literal, split
        assert _tool_ends(events) == [], split


@pytest.mark.parametrize(
    "literal",
    [
        f"inline `{_PLAIN_CALL}` example",
        f"fenced\n```json\n{_PLAIN_CALL}\n```\nexample",
        f"inline `{_QWEN_CALL}` example",
        f"fenced\n```xml\n{_MINIMAX_CALL}\n```\nexample",
    ],
)
def test_inline_and_fenced_protocol_examples_are_literal(
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="profile_test",
        model="profile-test",
        text_chunks=list(literal),
        tools=[_SEARCH_TOOL],
        compat=OpenAICompatPolicy(
            display_name="all dialect test",
            text_tool_profile=TextToolCompatProfile(
                dialects=frozenset(
                    {
                        TEXT_TOOL_DIALECT_QWEN_TAG,
                        TEXT_TOOL_DIALECT_MINIMAX_XML,
                        TEXT_TOOL_DIALECT_PLAIN_JSON,
                    }
                )
            ),
        ),
    )
    assert _text(events) == literal
    assert _tool_ends(events) == []


def test_no_tools_means_protocol_text_is_immediately_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=[_QWEN_CALL[:20], _QWEN_CALL[20:]],
        tools=None,
    )
    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        _QWEN_CALL[:20],
        _QWEN_CALL[20:],
    ]
    assert _tool_ends(events) == []


@pytest.mark.parametrize(
    "literal",
    [
        '<tool_call>{"name":"unknown","arguments":{}}</tool_call>',
        '<tool_call>{"name":"search","arguments":{"query":7}}</tool_call>',
        '<tool_call>{"name":"search","arguments":{"query":"x"}}',
    ],
)
def test_unknown_schema_invalid_and_incomplete_qwen_calls_replay_exactly(
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=list(literal),
        tools=[_SEARCH_TOOL],
    )
    assert _text(events) == literal
    assert _tool_ends(events) == []


@pytest.mark.parametrize(
    ("provider_kind", "model", "call"),
    [
        ("dashscope", "qwen3.6-flash", _QWEN_CALL),
        ("minimax", "MiniMax-M2.7", _MINIMAX_CALL),
        ("openrouter", "minimax/minimax-m2.7", _MINIMAX_CALL),
        ("tokenrhythm", "qwen3.7-max", _QWEN_CALL),
        ("tokenrhythm", "minimax-m2.7", _MINIMAX_CALL),
    ],
)
def test_profile_call_embedded_in_prose_replays_exactly(
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
    model: str,
    call: str,
) -> None:
    text = f"before:{call}:after"
    events = _collect_stream(
        monkeypatch,
        provider_kind=provider_kind,
        model=model,
        text_chunks=list(text),
        tools=[_SEARCH_TOOL],
    )
    assert _text(events) == text
    assert _tool_ends(events) == []


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        (TEXT_TOOL_DIALECT_QWEN_TAG, _QWEN_CALL),
        (TEXT_TOOL_DIALECT_MINIMAX_XML, _MINIMAX_CALL),
        (TEXT_TOOL_DIALECT_PLAIN_JSON, _PLAIN_CALL),
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        "before:{call}",
        "{call}:after",
        "before:{call}:after",
        "ordinary explanation {call}",
        "before\n{call}\nafter",
    ],
)
def test_non_standalone_protocol_is_literal_with_batch_stream_parity_at_every_split(
    dialect: TextToolDialect,
    call: str,
    template: str,
) -> None:
    literal = template.format(call=call)
    assert _direct_classify(literal, dialect=dialect, tool=_SEARCH_TOOL) == [
        LiteralTextSegment(literal)
    ]

    for split in range(1, len(literal)):
        visible, segments = _direct_stream(
            literal,
            dialect=dialect,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert _direct_calls(segments) == [], split
        literal_tail = "".join(
            segment.text
            if isinstance(segment, LiteralTextSegment)
            else segment.source_text
            for segment in segments
        )
        assert visible + literal_tail == literal, split


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        (TEXT_TOOL_DIALECT_QWEN_TAG, _QWEN_CALL),
        (TEXT_TOOL_DIALECT_MINIMAX_XML, _MINIMAX_CALL),
        (TEXT_TOOL_DIALECT_PLAIN_JSON, _PLAIN_CALL),
    ],
)
@pytest.mark.parametrize("line_break", ["\n", "\r\n", "\r"])
def test_terminal_standalone_block_accepts_prose_prefix_and_whitespace_at_every_split(
    dialect: TextToolDialect,
    call: str,
    line_break: str,
) -> None:
    text = f"Normal explanation.{line_break}  {call}{line_break} "
    expected_literal = f"Normal explanation.{line_break}  {line_break} "
    batch = _direct_classify(text, dialect=dialect, tool=_SEARCH_TOOL)
    assert [item.arguments for item in _direct_calls(batch)] == [{"query": "x"}]

    for split in range(1, len(text)):
        visible, segments = _direct_stream(
            text,
            dialect=dialect,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert [item.arguments for item in _direct_calls(segments)] == [
            {"query": "x"}
        ], split
        literal_tail = "".join(
            segment.text
            for segment in segments
            if isinstance(segment, LiteralTextSegment)
        )
        assert visible + literal_tail == expected_literal, split


@pytest.mark.parametrize("separator", ["", "\n"])
def test_multiple_qwen_standalone_blocks_execute_in_order_at_every_split(
    separator: str,
) -> None:
    second = _QWEN_CALL.replace('"x"', '"y"')
    text = f"Explanation\n{_QWEN_CALL}{separator}{second}\n"
    expected = [{"query": "x"}, {"query": "y"}]
    assert [
        item.arguments
        for item in _direct_calls(
            _direct_classify(
                text,
                dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
                tool=_SEARCH_TOOL,
            )
        )
    ] == expected

    for split in range(1, len(text)):
        _visible, segments = _direct_stream(
            text,
            dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert [item.arguments for item in _direct_calls(segments)] == expected, split


def test_plain_json_requires_explicit_profile_and_preserves_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = f"before\n{_PLAIN_CALL}"
    events = _collect_stream(
        monkeypatch,
        provider_kind="profile_test",
        model="profile-test",
        text_chunks=list(text),
        tools=[_SEARCH_TOOL],
        compat=_plain_profile(),
    )
    assert _text(events) == "before\n"
    assert [event.arguments for event in _tool_ends(events)] == [{"query": "x"}]


def test_plain_json_is_literal_for_openrouter_without_explicit_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="openrouter",
        model="deepseek/deepseek-v4",
        text_chunks=list(_PLAIN_CALL),
        tools=[_SEARCH_TOOL],
    )
    assert _text(events) == _PLAIN_CALL
    assert _tool_ends(events) == []


@pytest.mark.parametrize("call", [_QWEN_CALL, _MINIMAX_CALL, _PLAIN_CALL])
def test_candidate_is_never_emitted_before_synthetic_tool_events(
    monkeypatch: pytest.MonkeyPatch,
    call: str,
) -> None:
    if call == _QWEN_CALL:
        kind, model, compat = "dashscope", "qwen3.6-flash", None
    elif call == _MINIMAX_CALL:
        kind, model, compat = "minimax", "MiniMax-M2.7", None
    else:
        kind, model, compat = "profile_test", "profile-test", _plain_profile()
    events = _collect_stream(
        monkeypatch,
        provider_kind=kind,
        model=model,
        text_chunks=list(call),
        tools=[_SEARCH_TOOL],
        compat=compat,
    )
    start_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolUseStartEvent)
    )
    assert not any(
        isinstance(event, TextDeltaEvent) and event.text in call
        for event in events[:start_index]
    )
    assert _text(events) == ""


def test_native_tool_call_wins_and_replays_text_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=list(_QWEN_CALL),
        tools=[_SEARCH_TOOL],
        native_tool_call=True,
        finish_reason="tool_calls",
    )
    assert _text(events) == _QWEN_CALL
    ends = _tool_ends(events)
    assert len(ends) == 1
    assert ends[0].tool_use_id == "call_native"
    assert ends[0].arguments == {"query": "native"}
    assert ends[0].synthetic_from_text is False
    relevant = [
        event.kind
        for event in events
        if event.kind in {"text_delta", "tool_use_start", "tool_use_delta", "tool_use_end"}
    ]
    assert relevant == [
        "text_delta",
        "tool_use_start",
        "tool_use_delta",
        "tool_use_end",
    ]


@pytest.mark.parametrize("native_query", ["x", "native"])
def test_identity_first_native_chunk_matches_name_first_order(
    monkeypatch: pytest.MonkeyPatch,
    native_query: str,
) -> None:
    body = _raw_sse(
        [
            {"choices": [{"delta": {"content": _QWEN_CALL}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_native", "function": {}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": "search",
                                        "arguments": json.dumps(
                                            {"query": native_query}
                                        ),
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )
    expected_text = "" if native_query == "x" else _QWEN_CALL
    assert _text(events) == expected_text
    starts = [event for event in events if event.kind == "tool_use_start"]
    assert [(event.tool_use_id, event.tool_name) for event in starts] == [
        ("call_native", "search")
    ]
    assert [event.arguments for event in _tool_ends(events)] == [
        {"query": native_query}
    ]


@pytest.mark.parametrize(
    ("native_query", "expected_order", "expected_text"),
    [
        (
            "x",
            ["tool_use_start", "tool_use_delta", "tool_use_end", "text_delta"],
            "suffix",
        ),
        (
            "native",
            [
                "text_delta",
                "tool_use_start",
                "tool_use_delta",
                "tool_use_end",
                "text_delta",
            ],
            f"{_QWEN_CALL}suffix",
        ),
    ],
)
def test_post_native_text_waits_for_duplicate_resolution(
    monkeypatch: pytest.MonkeyPatch,
    native_query: str,
    expected_order: list[str],
    expected_text: str,
) -> None:
    body = _raw_sse(
        [
            {"choices": [{"delta": {"content": _QWEN_CALL}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {
                                        "name": "search",
                                        "arguments": json.dumps(
                                            {"query": native_query}
                                        ),
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {"content": "suffix"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )
    assert _text(events) == expected_text
    assert [
        event.kind
        for event in events
        if event.kind in {"text_delta", "tool_use_start", "tool_use_delta", "tool_use_end"}
    ] == expected_order


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "error"])
@pytest.mark.parametrize("arguments", [json.dumps({"query": "x"}), "{not-json"])
def test_unsuccessful_native_finish_never_emits_end_or_done(
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: str,
    arguments: str,
) -> None:
    body = _raw_sse(
        [
            {"choices": [{"delta": {"content": _QWEN_CALL}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {
                                        "name": "search",
                                        "arguments": arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": finish_reason}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )
    assert _text(events) == _QWEN_CALL
    assert _tool_ends(events) == []
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in events
    )


def test_structured_scaffold_only_consumed_when_native_call_confirms_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    without_native = _collect_stream(
        monkeypatch,
        provider_kind="deepseek",
        model="deepseek-chat",
        text_chunks=list(_DETAILS_SCAFFOLD),
        tools=[_SEARCH_TOOL],
    )
    assert _text(without_native) == _DETAILS_SCAFFOLD

    with_native = _collect_stream(
        monkeypatch,
        provider_kind="deepseek",
        model="deepseek-chat",
        text_chunks=list(_DETAILS_SCAFFOLD),
        tools=[_SEARCH_TOOL],
        native_tool_call=True,
        finish_reason="tool_calls",
    )
    assert _text(with_native) == ""
    assert len(_tool_ends(with_native)) == 1


@pytest.mark.parametrize(
    "literal",
    [
        f"explanation:{_DETAILS_SCAFFOLD}",
        f"{_DETAILS_SCAFFOLD}:after",
        f"> {_DETAILS_SCAFFOLD}",
        f"- {_DETAILS_SCAFFOLD}",
        f"`{_DETAILS_SCAFFOLD}`",
        f"```html\n{_DETAILS_SCAFFOLD}\n```",
    ],
)
def test_documented_or_embedded_scaffold_is_not_owned_by_native_call(
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="deepseek",
        model="deepseek-chat",
        text_chunks=list(literal),
        tools=[_SEARCH_TOOL],
        native_tool_call=True,
        finish_reason="tool_calls",
    )
    assert _text(events) == literal
    assert len(_tool_ends(events)) == 1


def test_standalone_scaffold_after_prose_is_owned_only_when_native_call_confirms_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = f"Explanation before navigation.\r\n{_DETAILS_SCAFFOLD}\r\n"
    events = _collect_stream(
        monkeypatch,
        provider_kind="deepseek",
        model="deepseek-chat",
        text_chunks=list(text),
        tools=[_SEARCH_TOOL],
        native_tool_call=True,
        finish_reason="tool_calls",
    )
    assert _text(events) == "Explanation before navigation.\r\n"
    assert len(_tool_ends(events)) == 1


def test_embedded_scaffold_does_not_disable_later_standalone_tool_block() -> None:
    text = f"Example:{_DETAILS_SCAFFOLD}\n{_QWEN_CALL}"
    expected_literal = f"Example:{_DETAILS_SCAFFOLD}\n"
    batch = _direct_classify(
        text,
        dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
        tool=_SEARCH_TOOL,
    )
    assert [item.arguments for item in _direct_calls(batch)] == [{"query": "x"}]
    for split in range(1, len(text)):
        visible, segments = _direct_stream(
            text,
            dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert [item.arguments for item in _direct_calls(segments)] == [
            {"query": "x"}
        ], split
        literal_tail = "".join(
            segment.text
            for segment in segments
            if isinstance(segment, LiteralTextSegment)
        )
        assert visible + literal_tail == expected_literal, split


def test_native_first_scaffold_remains_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _raw_sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {
                                        "name": "search",
                                        "arguments": json.dumps({"query": "x"}),
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"content": _DETAILS_SCAFFOLD},
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="deepseek",
        model="deepseek-chat",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )
    assert _text(events) == _DETAILS_SCAFFOLD
    assert len(_tool_ends(events)) == 1


def test_two_identical_text_candidates_consume_only_one_native_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=[_QWEN_CALL + _QWEN_CALL],
        tools=[_SEARCH_TOOL],
        native_tool_call=True,
        native_query="x",
        finish_reason="tool_calls",
    )
    assert _text(events) == _QWEN_CALL
    assert len(_tool_ends(events)) == 1


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "error"])
def test_unsuccessful_finish_reason_replays_candidate_without_execution(
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: str,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=list(_QWEN_CALL),
        tools=[_SEARCH_TOOL],
        finish_reason=finish_reason,
    )
    assert _text(events) == _QWEN_CALL
    assert _tool_ends(events) == []
    assert any(
        isinstance(event, DoneEvent) and event.stop_reason == finish_reason
        for event in events
    )


def test_done_sentinel_does_not_authorize_tools_without_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=list(_QWEN_CALL),
        tools=[_SEARCH_TOOL],
        finish_reason=None,
        include_done=True,
    )
    assert _text(events) == _QWEN_CALL
    assert _tool_ends(events) == []
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_stream"
        for event in events
    )


def test_abnormal_eof_replays_candidate_and_returns_error_not_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=list(_QWEN_CALL),
        tools=[_SEARCH_TOOL],
        finish_reason=None,
        include_done=False,
    )
    assert _text(events) == _QWEN_CALL
    assert _tool_ends(events) == []
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_stream"
        for event in events
    )
    assert not any(isinstance(event, DoneEvent) for event in events)


def test_abnormal_eof_does_not_complete_pending_native_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        native_tool_call=True,
        finish_reason=None,
        include_done=False,
    )
    assert any(isinstance(event, ToolUseStartEvent) for event in events)
    assert _tool_ends(events) == []
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_stream"
        for event in events
    )


def test_malformed_data_frame_prevents_terminal_from_authorizing_text_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _raw_sse(
        [{"choices": [{"delta": {"content": _QWEN_CALL}, "finish_reason": None}]}],
        include_done=False,
    )
    body += b"data:{malformed-json\n\n"
    body += _raw_sse(
        [{"choices": [{"delta": {}, "finish_reason": "stop"}]}],
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )

    assert _text(events) == _QWEN_CALL
    assert _tool_ends(events) == []
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "invalid_stream_frame"
        for event in events
    )


def test_valid_sse_data_field_without_optional_space_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        {"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    body = b"".join(
        f"data:{json.dumps(chunk)}\n\n".encode()
        for chunk in chunks
    )
    body += b"data:[DONE]\n\n"

    events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        raw_body=body,
    )

    assert _text(events) == "ok"
    assert any(isinstance(event, DoneEvent) for event in events)


def test_malformed_data_frame_never_closes_native_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _raw_sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"query":"x"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ],
        include_done=False,
    )
    body += b"data: {malformed-json\n\n"
    body += _raw_sse(
        [{"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}],
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )

    assert any(isinstance(event, ToolUseStartEvent) for event in events)
    assert _tool_ends(events) == []
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "invalid_stream_frame"
        for event in events
    )


@pytest.mark.parametrize(
    "raw_arguments",
    [
        "{not-json",
        '["not", "an", "object"]',
        '{"query":NaN}',
        '{"query":Infinity}',
        '{"query":-Infinity}',
        '{"query":1e999}',
    ],
)
def test_invalid_native_arguments_fail_closed_in_stream_and_non_stream(
    monkeypatch: pytest.MonkeyPatch,
    raw_arguments: str,
) -> None:
    stream_body = _raw_sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {
                                        "name": "search",
                                        "arguments": raw_arguments,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    stream_events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=stream_body,
    )
    non_stream_events = _collect_non_stream(
        monkeypatch,
        text="",
        finish_reason="tool_calls",
        compat=_plain_profile(),
        native_arguments=raw_arguments,
    )

    assert any(isinstance(event, ToolUseStartEvent) for event in stream_events)
    assert not any(
        event.kind.startswith("tool_use_") for event in non_stream_events
    )
    for events in (stream_events, non_stream_events):
        assert _tool_ends(events) == []
        assert not any(isinstance(event, DoneEvent) for event in events)
        error = next(
            event
            for event in events
            if isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        )
        assert raw_arguments not in error.message
        assert "_raw" not in error.message


def test_non_stream_native_batch_is_atomic_when_later_call_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _collect_non_stream(
        monkeypatch,
        text="",
        finish_reason="tool_calls",
        compat=_plain_profile(),
        native_calls=[
            ("search", '{"query":"valid"}'),
            ("search", "{malformed"),
        ],
    )

    assert not any(event.kind.startswith("tool_use_") for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in events
    )


def test_stream_native_batch_keeps_diagnostics_but_no_end_when_later_call_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _raw_sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_valid",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"query":"valid"}',
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "call_invalid",
                                    "function": {
                                        "name": "search",
                                        "arguments": "{malformed",
                                    },
                                },
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )

    assert len([event for event in events if isinstance(event, ToolUseStartEvent)]) == 2
    assert _tool_ends(events) == []
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in events
    )


def test_native_start_waits_for_late_nonempty_name_without_text_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _raw_sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {"arguments": '{"query":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": "search",
                                        "arguments": '"x"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )

    lifecycle = [
        event
        for event in events
        if event.kind
        in {"tool_use_start", "tool_use_delta", "tool_use_end"}
    ]
    assert [event.kind for event in lifecycle] == [
        "tool_use_start",
        "tool_use_delta",
        "tool_use_end",
    ]
    assert isinstance(lifecycle[0], ToolUseStartEvent)
    assert lifecycle[0].tool_name == "search"
    assert isinstance(lifecycle[1], ToolUseDeltaEvent)
    assert lifecycle[1].json_fragment == '{"query":"x"}'
    assert _tool_ends(events)[0].arguments == {"query": "x"}


def test_late_earlier_identity_holds_later_call_in_provider_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _raw_sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_0",
                                    "function": {"arguments": '{"query":"zero"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 1,
                                    "id": "call_1",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"query":"one"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"name": "search"},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )

    lifecycle = [
        event
        for event in events
        if event.kind in {"tool_use_start", "tool_use_delta", "tool_use_end"}
    ]
    assert [(event.kind, event.tool_use_id) for event in lifecycle] == [
        ("tool_use_start", "call_0"),
        ("tool_use_delta", "call_0"),
        ("tool_use_start", "call_1"),
        ("tool_use_delta", "call_1"),
        ("tool_use_end", "call_0"),
        ("tool_use_end", "call_1"),
    ]
    assert all(
        event.tool_name == "search"
        for event in lifecycle
        if isinstance(event, (ToolUseStartEvent, ToolUseEndEvent))
    )


def test_conflicting_reannounced_native_name_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _raw_sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {
                                        "name": "sea",
                                        "arguments": '{"query":"x"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"name": "search"}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )

    starts = [event for event in events if isinstance(event, ToolUseStartEvent)]
    assert [event.tool_name for event in starts] == ["sea"]
    assert _tool_ends(events) == []
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in events
    )


@pytest.mark.parametrize(
    "tool_calls",
    [
        [
            {
                "index": "bad-a",
                "id": "call_a",
                "function": {"name": "search", "arguments": '{"query":'},
            },
            {
                "index": "bad-b",
                "id": "call_b",
                "function": {"name": "search", "arguments": '"merged"}'},
            },
        ],
        [
            {
                "index": 0,
                "id": "call_a",
                "function": {"name": "search", "arguments": '{"query":'},
            },
            {
                "index": 0,
                "id": "call_b",
                "function": {"name": "search", "arguments": '"merged"}'},
            },
        ],
    ],
)
def test_invalid_indices_or_conflicting_ids_never_merge_into_executable_call(
    monkeypatch: pytest.MonkeyPatch,
    tool_calls: list[dict[str, Any]],
) -> None:
    body = _raw_sse(
        [
            {
                "choices": [
                    {
                        "delta": {"tool_calls": tool_calls},
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )

    assert _tool_ends(events) == []
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in events
    )


def test_permanently_missing_native_name_fails_closed_without_empty_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_arguments = '{"query":"x"}'
    stream_body = _raw_sse(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {"arguments": raw_arguments},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    stream_events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=stream_body,
    )
    non_stream_events = _collect_non_stream(
        monkeypatch,
        text="",
        finish_reason="tool_calls",
        compat=_plain_profile(),
        native_arguments=raw_arguments,
        native_tool_name=None,
    )

    for events in (stream_events, non_stream_events):
        assert not any(isinstance(event, ToolUseStartEvent) for event in events)
        assert _tool_ends(events) == []
        assert not any(isinstance(event, DoneEvent) for event in events)
        assert any(
            isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
            for event in events
        )


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_text_tool_json_dialects_replay_nonfinite_numbers_atomically(
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
) -> None:
    qwen_call = (
        '<tool_call>{"name":"number_tool","arguments":{"value":'
        f"{constant}"  # closed below to keep the non-standard token visible
        "}}</tool_call>"
    )
    plain_call = f'number_tool{{"value":{constant}}}'

    qwen_events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=[qwen_call],
        tools=[_NUMBER_TOOL],
    )
    plain_events = _collect_non_stream(
        monkeypatch,
        text=plain_call,
        finish_reason="stop",
        compat=_plain_profile(),
        tools=[_NUMBER_TOOL],
    )

    assert _text(qwen_events) == qwen_call
    assert _text(plain_events) == plain_call
    assert _tool_ends(qwen_events) == []
    assert _tool_ends(plain_events) == []


@pytest.mark.parametrize("finish_reason", ["stop", "length", None])
def test_stream_and_non_stream_share_text_tool_classification(
    monkeypatch: pytest.MonkeyPatch,
    finish_reason: str | None,
) -> None:
    events = _collect_non_stream(
        monkeypatch,
        text=f"before\n{_PLAIN_CALL}",
        finish_reason=finish_reason,
        compat=_plain_profile(),
    )
    if finish_reason == "stop":
        assert _text(events) == "before\n"
        assert len(_tool_ends(events)) == 1
    else:
        assert _text(events) == f"before\n{_PLAIN_CALL}"
        assert _tool_ends(events) == []


@pytest.mark.parametrize(
    "literal",
    [
        f"before:{_PLAIN_CALL}",
        f"{_PLAIN_CALL}:after",
        f"before\n{_PLAIN_CALL}\nafter",
    ],
)
def test_non_stream_rejects_non_standalone_plain_call_like_stream(
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
) -> None:
    non_stream = _collect_non_stream(
        monkeypatch,
        text=literal,
        finish_reason="stop",
        compat=_plain_profile(),
    )
    stream = _collect_stream(
        monkeypatch,
        provider_kind="profile_test",
        model="profile-test",
        text_chunks=list(literal),
        tools=[_SEARCH_TOOL],
        compat=_plain_profile(),
    )
    assert _text(non_stream) == literal
    assert _text(stream) == literal
    assert _tool_ends(non_stream) == []
    assert _tool_ends(stream) == []


@pytest.mark.parametrize(
    ("provider_kind", "model", "call", "compat"),
    [
        ("dashscope", "qwen3.6-flash", _QWEN_CALL, None),
        ("minimax", "MiniMax-M2.7", _MINIMAX_CALL, None),
        ("profile_test", "profile-test", _PLAIN_CALL, _plain_profile()),
    ],
)
def test_every_accepted_dialect_has_all_two_chunk_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
    model: str,
    call: str,
    compat: OpenAICompatPolicy | None,
) -> None:
    for split in range(1, len(call)):
        events = _collect_stream(
            monkeypatch,
            provider_kind=provider_kind,
            model=model,
            text_chunks=[call[:split], call[split:]],
            tools=[_SEARCH_TOOL],
            compat=compat,
        )
        assert _text(events) == "", split
        assert [event.arguments for event in _tool_ends(events)] == [
            {"query": "x"}
        ], split


@pytest.mark.parametrize(
    ("interstitial", "authorized"),
    [
        (" " * PLAIN_TOOL_INTERSTITIAL_WHITESPACE_MAX, True),
        (" " * (PLAIN_TOOL_INTERSTITIAL_WHITESPACE_MAX + 1), False),
        ("\n", False),
        ("\r\n", False),
    ],
)
def test_plain_interstitial_whitespace_batch_stream_parity_at_every_split(
    interstitial: str,
    authorized: bool,
) -> None:
    call = f'search{interstitial}{{"query":"x"}}'
    batch = _direct_classify(
        call,
        dialect=TEXT_TOOL_DIALECT_PLAIN_JSON,
        tool=_SEARCH_TOOL,
    )
    assert bool(_direct_calls(batch)) is authorized

    for split in range(1, len(call)):
        visible, segments = _direct_stream(
            call,
            dialect=TEXT_TOOL_DIALECT_PLAIN_JSON,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert bool(_direct_calls(segments)) is authorized, split
        if authorized:
            assert visible == "", split
        else:
            literal_tail = "".join(
                segment.text
                if isinstance(segment, LiteralTextSegment)
                else segment.source_text
                for segment in segments
            )
            assert visible + literal_tail == call, split


@pytest.mark.parametrize(
    ("dialect", "call", "expected_new_text"),
    [
        (
            TEXT_TOOL_DIALECT_QWEN_TAG,
            _QWEN_EDIT_WITH_CODE,
            f"{_CODE_VALUE}literal </function> marker",
        ),
        (
            TEXT_TOOL_DIALECT_MINIMAX_XML,
            _MINIMAX_EDIT_WITH_CODE,
            f"{_CODE_VALUE}literal </invoke> marker",
        ),
    ],
)
def test_markdown_and_close_tags_inside_arguments_execute_at_every_split(
    dialect: TextToolDialect,
    call: str,
    expected_new_text: str,
) -> None:
    batch_calls = _direct_calls(_direct_classify(call, dialect=dialect, tool=_EDIT_TOOL))
    assert [item.arguments for item in batch_calls] == [
        {"path": "demo.py", "new_text": expected_new_text}
    ]

    for split in range(1, len(call)):
        visible, segments = _direct_stream(
            call,
            dialect=dialect,
            tool=_EDIT_TOOL,
            split=split,
        )
        assert visible == "", split
        assert [item.arguments for item in _direct_calls(segments)] == [
            {"path": "demo.py", "new_text": expected_new_text}
        ], split


@pytest.mark.parametrize(
    "dialect",
    [TEXT_TOOL_DIALECT_QWEN_TAG, TEXT_TOOL_DIALECT_MINIMAX_XML],
)
@pytest.mark.parametrize(
    ("framed_value", "expected_value"),
    [
        ("\nx\n", "x"),
        ("\r\nx\r\n", "x"),
        ("\rx\r", "x"),
        ("  keep both  ", "  keep both  "),
        ("\n  indented\n    next\n", "  indented\n    next"),
        ("\n\n\nx\n\n\n", "\n\nx\n\n"),
    ],
)
def test_xml_arguments_remove_only_one_framing_eol_at_every_split(
    dialect: TextToolDialect,
    framed_value: str,
    expected_value: str,
) -> None:
    if dialect == TEXT_TOOL_DIALECT_QWEN_TAG:
        call = (
            "<tool_call><function=edit_file>"
            "<parameter=path>demo.py</parameter>"
            f"<parameter=new_text>{framed_value}</parameter>"
            "</function></tool_call>"
        )
    else:
        call = (
            "<minimax:tool_call><invoke name=\"edit_file\">"
            "<parameter name=\"path\">demo.py</parameter>"
            f"<parameter name=\"new_text\">{framed_value}</parameter>"
            "</invoke></minimax:tool_call>"
        )
    expected = {"path": "demo.py", "new_text": expected_value}

    assert [
        item.arguments
        for item in _direct_calls(
            _direct_classify(call, dialect=dialect, tool=_EDIT_TOOL)
        )
    ] == [expected]
    for split in range(1, len(call)):
        visible, segments = _direct_stream(
            call,
            dialect=dialect,
            tool=_EDIT_TOOL,
            split=split,
        )
        assert visible == "", split
        assert [item.arguments for item in _direct_calls(segments)] == [expected], split


def test_qwen_json_string_can_contain_literal_wrapper_close_at_every_split() -> None:
    query = "literal </tool_call> marker"
    call = f"<tool_call>{json.dumps({'name': 'search', 'arguments': {'query': query}})}</tool_call>"
    assert [item.arguments for item in _direct_calls(
        _direct_classify(call, dialect=TEXT_TOOL_DIALECT_QWEN_TAG, tool=_SEARCH_TOOL)
    )] == [{"query": query}]
    for split in range(1, len(call)):
        visible, segments = _direct_stream(
            call,
            dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert visible == "", split
        assert [item.arguments for item in _direct_calls(segments)] == [
            {"query": query}
        ], split


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        (TEXT_TOOL_DIALECT_QWEN_TAG, _QWEN_TYPED_CALL),
        (TEXT_TOOL_DIALECT_MINIMAX_XML, _MINIMAX_TYPED_CALL),
    ],
)
def test_xml_parameters_decode_schema_types_in_batch_and_stream(
    dialect: TextToolDialect,
    call: str,
) -> None:
    assert [item.arguments for item in _direct_calls(
        _direct_classify(call, dialect=dialect, tool=_TYPED_TOOL)
    )] == [_TYPED_EXPECTED]
    visible, segments = _direct_stream(
        call,
        dialect=dialect,
        tool=_TYPED_TOOL,
        split=len(call) // 2,
    )
    assert visible == ""
    assert [item.arguments for item in _direct_calls(segments)] == [_TYPED_EXPECTED]


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        (
            TEXT_TOOL_DIALECT_QWEN_TAG,
            "<tool_call><function=ambiguous><parameter=value>10</parameter>"
            "</function></tool_call>",
        ),
        (
            TEXT_TOOL_DIALECT_MINIMAX_XML,
            "<minimax:tool_call><invoke name=\"ambiguous\">"
            "<parameter name=\"value\">10</parameter></invoke>"
            "</minimax:tool_call>",
        ),
    ],
)
def test_ambiguous_xml_union_is_literal_in_batch_and_stream(
    dialect: TextToolDialect,
    call: str,
) -> None:
    batch = _direct_classify(call, dialect=dialect, tool=_AMBIGUOUS_TOOL)
    assert batch == [LiteralTextSegment(call)]
    visible, segments = _direct_stream(
        call,
        dialect=dialect,
        tool=_AMBIGUOUS_TOOL,
        split=len(call) // 2,
    )
    assert visible == ""
    assert segments == [LiteralTextSegment(call)]


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        (
            TEXT_TOOL_DIALECT_QWEN_TAG,
            "<tool_call><function=typed><parameter=count>10x</parameter>"
            "</function></tool_call>",
        ),
        (
            TEXT_TOOL_DIALECT_MINIMAX_XML,
            "<minimax:tool_call><invoke name=\"typed\">"
            "<parameter name=\"count\">10x</parameter></invoke>"
            "</minimax:tool_call>",
        ),
    ],
)
def test_invalid_typed_xml_value_is_literal(
    dialect: TextToolDialect,
    call: str,
) -> None:
    assert _direct_classify(call, dialect=dialect, tool=_TYPED_TOOL) == [
        LiteralTextSegment(call)
    ]


def test_plain_json_word_boundary_is_identical_for_every_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    literal = f"x{_PLAIN_CALL}"
    for split in range(1, len(literal)):
        events = _collect_stream(
            monkeypatch,
            provider_kind="profile_test",
            model="profile-test",
            text_chunks=[literal[:split], literal[split:]],
            tools=[_SEARCH_TOOL],
            compat=_plain_profile(),
        )
        assert _text(events) == literal, split
        assert _tool_ends(events) == [], split


def test_noncanonical_minimax_wrapper_is_literal_at_every_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    literal = _MINIMAX_CALL.replace(
        "<minimax:tool_call>", "< minimax:tool_call >"
    ).replace("</minimax:tool_call>", "< / minimax:tool_call >")
    for split in range(1, len(literal)):
        events = _collect_stream(
            monkeypatch,
            provider_kind="minimax",
            model="MiniMax-M2.7",
            text_chunks=[literal[:split], literal[split:]],
            tools=[_SEARCH_TOOL],
        )
        assert _text(events) == literal, split
        assert _tool_ends(events) == [], split


@pytest.mark.parametrize(
    "literal",
    [
        f"{_QWEN_CALL}<tool_cal",
        f"{_MINIMAX_CALL}<minimax:tool_",
        (
            "<minimax:tool_call>"
            '<invoke name="search"><parameter name="query">x</parameter></invoke>'
            '<invoke name="search"><parameter name="query">unfinished'
            "</minimax:tool_call>"
        ),
    ],
)
def test_complete_call_plus_partial_protocol_is_atomic_literal(
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
) -> None:
    is_qwen = literal.startswith("<tool_call>")
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope" if is_qwen else "minimax",
        model="qwen3.6-flash" if is_qwen else "MiniMax-M2.7",
        text_chunks=list(literal),
        tools=[_SEARCH_TOOL],
    )
    assert _text(events) == literal
    assert _tool_ends(events) == []


@pytest.mark.parametrize(
    "literal",
    [
        (
            "<tool_call><function=search>"
            "<parameter=query>x</parameter></function>VISIBLE</tool_call>"
        ),
        (
            "<tool_call><function=search><parameter=query>x</parameter></function>"
            "<function=search><parameter=query>y</parameter></function></tool_call>"
        ),
        (
            "<minimax:tool_call>JUNK<invoke name=\"search\">"
            "<parameter name=\"query\">x</parameter></invoke>"
            "</minimax:tool_call>"
        ),
        (
            "<minimax:tool_call><invoke name=\"search\">"
            "<parameter name=\"query\">x</parameter></invoke>TAIL"
            "</minimax:tool_call>"
        ),
    ],
)
def test_strict_wrapper_rejects_structural_junk_without_byte_loss(
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
) -> None:
    is_qwen = literal.startswith("<tool_call>")
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope" if is_qwen else "minimax",
        model="qwen3.6-flash" if is_qwen else "MiniMax-M2.7",
        text_chunks=list(literal),
        tools=[_SEARCH_TOOL],
    )
    assert _text(events) == literal
    assert _tool_ends(events) == []


@pytest.mark.parametrize(
    "literal",
    [
        f"`example {_QWEN_CALL}`\n{_QWEN_CALL}",
        f"~~~xml\nsome ``` mid-line\n{_QWEN_CALL}\n~~~\n{_QWEN_CALL}",
        f"    {_QWEN_CALL}\n{_QWEN_CALL}",
    ],
)
def test_documented_protocol_prefix_does_not_block_terminal_standalone_call(
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=list(literal),
        tools=[_SEARCH_TOOL],
    )
    assert _text(events) == literal[: -len(_QWEN_CALL)]
    assert [event.arguments for event in _tool_ends(events)] == [{"query": "x"}]


@pytest.mark.parametrize(
    ("dialect", "malformed", "call"),
    [
        (
            TEXT_TOOL_DIALECT_QWEN_TAG,
            "<tool_call>{not-json}</tool_call>\n",
            _QWEN_CALL,
        ),
        (
            TEXT_TOOL_DIALECT_MINIMAX_XML,
            (
                '<minimax:tool_call>\n<invoke name="web_search">\n'
                '<parameter name="query">x</parameter>\nBROKEN\n'
                "</minimax:tool_call>\n"
            ),
            _MINIMAX_CALL,
        ),
    ],
)
def test_malformed_protocol_prefix_does_not_poison_valid_terminal_call_at_any_split(
    dialect: TextToolDialect,
    malformed: str,
    call: str,
) -> None:
    text = malformed + call
    batch = _direct_classify(text, dialect=dialect, tool=_SEARCH_TOOL)
    assert [item.arguments for item in _direct_calls(batch)] == [{"query": "x"}]
    assert "".join(
        segment.text
        for segment in batch
        if isinstance(segment, LiteralTextSegment)
    ) == malformed

    for split in range(1, len(text)):
        visible, segments = _direct_stream(
            text,
            dialect=dialect,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert [item.arguments for item in _direct_calls(segments)] == [
            {"query": "x"}
        ], split
        literal_tail = "".join(
            segment.text
            for segment in segments
            if isinstance(segment, LiteralTextSegment)
        )
        assert visible + literal_tail == malformed, split


@pytest.mark.parametrize(
    "prefix",
    [
        "Use `read_file` to inspect the input.\n",
        f"Example `{_QWEN_CALL}`\n",
        f"```xml\n{_QWEN_CALL}\n```\n",
        "- Inspect the input first.\n\n",
        "> The next block is the actual action.\n\n",
        "    indented documentation\n\n",
        f"Example in prose: {_QWEN_CALL}\n",
    ],
)
def test_prior_markdown_or_documentation_does_not_poison_terminal_candidate_at_any_split(
    prefix: str,
) -> None:
    text = prefix + _QWEN_CALL
    batch = _direct_classify(
        text,
        dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
        tool=_SEARCH_TOOL,
    )
    assert [item.arguments for item in _direct_calls(batch)] == [{"query": "x"}]
    assert "".join(
        segment.text
        for segment in batch
        if isinstance(segment, LiteralTextSegment)
    ) == prefix

    for split in range(1, len(text)):
        visible, segments = _direct_stream(
            text,
            dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert [item.arguments for item in _direct_calls(segments)] == [
            {"query": "x"}
        ], split
        literal_tail = "".join(
            segment.text
            for segment in segments
            if isinstance(segment, LiteralTextSegment)
        )
        assert visible + literal_tail == prefix, split


@pytest.mark.parametrize("line_break", ["\n", "\r\n", "\r"])
def test_indented_example_does_not_block_terminal_call_for_all_line_endings(
    monkeypatch: pytest.MonkeyPatch,
    line_break: str,
) -> None:
    literal = f"example{line_break}    {_QWEN_CALL}{line_break}{_QWEN_CALL}"
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=list(literal),
        tools=[_SEARCH_TOOL],
    )
    assert _text(events) == literal[: -len(_QWEN_CALL)]
    assert [event.arguments for event in _tool_ends(events)] == [{"query": "x"}]


@pytest.mark.parametrize(
    "literal",
    [
        f"> {_QWEN_CALL}",
        f"- {_QWEN_CALL}",
        f"1. {_QWEN_CALL}",
        f"<code>{_QWEN_CALL}</code>",
        f"<pre>{_QWEN_CALL}</pre>",
    ],
)
def test_documentation_context_never_promotes_protocol_text(
    monkeypatch: pytest.MonkeyPatch,
    literal: str,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=list(literal),
        tools=[_SEARCH_TOOL],
    )
    assert _text(events) == literal
    assert _tool_ends(events) == []


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        (TEXT_TOOL_DIALECT_QWEN_TAG, _QWEN_CALL),
        (TEXT_TOOL_DIALECT_MINIMAX_XML, _MINIMAX_CALL),
        (TEXT_TOOL_DIALECT_PLAIN_JSON, _PLAIN_CALL),
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        "```text\nsome ``` mid-line\n{call}",
        "```text\r\n``` still code\r\n{call}",
        "````text\r```\r{call}",
        "~~~text\nsome ~~~ mid-line\n{call}",
    ],
)
def test_unclosed_fence_with_false_close_never_executes_at_any_split(
    dialect: TextToolDialect,
    call: str,
    template: str,
) -> None:
    literal = template.format(call=call)
    assert _direct_classify(literal, dialect=dialect, tool=_SEARCH_TOOL) == [
        LiteralTextSegment(literal)
    ]
    for split in range(1, len(literal)):
        visible, segments = _direct_stream(
            literal,
            dialect=dialect,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert _direct_calls(segments) == [], split
        literal_tail = "".join(
            segment.text
            if isinstance(segment, LiteralTextSegment)
            else segment.source_text
            for segment in segments
        )
        assert visible + literal_tail == literal, split


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        (TEXT_TOOL_DIALECT_QWEN_TAG, _QWEN_CALL),
        (TEXT_TOOL_DIALECT_MINIMAX_XML, _MINIMAX_CALL),
        (TEXT_TOOL_DIALECT_PLAIN_JSON, _PLAIN_CALL),
    ],
)
@pytest.mark.parametrize(
    "opener",
    [
        "<pre>\n",
        '<CODE class="example">\r\n',
        "<script>\r",
        "<style media='screen'>\n",
        "<textarea>\n",
        "<!-- example\n",
    ],
)
def test_unclosed_raw_html_documentation_never_executes_at_any_split(
    dialect: TextToolDialect,
    call: str,
    opener: str,
) -> None:
    literal = opener + call
    assert _direct_classify(literal, dialect=dialect, tool=_SEARCH_TOOL) == [
        LiteralTextSegment(literal)
    ]
    for split in range(1, len(literal)):
        visible, segments = _direct_stream(
            literal,
            dialect=dialect,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert _direct_calls(segments) == [], split
        literal_tail = "".join(
            segment.text
            if isinstance(segment, LiteralTextSegment)
            else segment.source_text
            for segment in segments
        )
        assert visible + literal_tail == literal, split


@pytest.mark.parametrize(
    "prefix",
    [
        f'<pre class="example">\n{_QWEN_CALL}\n</pre>\n',
        f"<script>\n{_QWEN_CALL}\n</SCRIPT   >\n",
        f"<!--\n{_QWEN_CALL}\n-->\n",
        '<code class="example" />\n',
    ],
)
def test_closed_raw_html_documentation_allows_only_the_outside_terminal_call(
    prefix: str,
) -> None:
    text = prefix + _QWEN_CALL
    batch = _direct_classify(
        text,
        dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
        tool=_SEARCH_TOOL,
    )
    assert [item.arguments for item in _direct_calls(batch)] == [{"query": "x"}]
    assert "".join(
        segment.text
        for segment in batch
        if isinstance(segment, LiteralTextSegment)
    ) == prefix

    for split in range(1, len(text)):
        visible, segments = _direct_stream(
            text,
            dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert [item.arguments for item in _direct_calls(segments)] == [
            {"query": "x"}
        ], split
        literal_tail = "".join(
            segment.text
            for segment in segments
            if isinstance(segment, LiteralTextSegment)
        )
        assert visible + literal_tail == prefix, split


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        (TEXT_TOOL_DIALECT_QWEN_TAG, _QWEN_CALL),
        (TEXT_TOOL_DIALECT_MINIMAX_XML, _MINIMAX_CALL),
        (TEXT_TOOL_DIALECT_PLAIN_JSON, _PLAIN_CALL),
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        '<pre data-example="/>">\n{call}',
        '<pre><span title="</pre>">\n{call}',
        "<pre><!-- </pre> still comment -->\n{call}",
        "<pre>\n<!--\n</pre>\nstill comment -->\n{call}",
        "<pre\n{call}",
        "<code><code>\ninside\n</code>\n{call}",
    ],
)
def test_raw_html_false_closes_and_incomplete_openers_never_execute_at_any_split(
    dialect: TextToolDialect,
    call: str,
    template: str,
) -> None:
    literal = template.format(call=call)
    assert _direct_classify(literal, dialect=dialect, tool=_SEARCH_TOOL) == [
        LiteralTextSegment(literal)
    ]
    for split in range(1, len(literal)):
        visible, segments = _direct_stream(
            literal,
            dialect=dialect,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert _direct_calls(segments) == [], split
        assert visible + "".join(
            segment.text
            if isinstance(segment, LiteralTextSegment)
            else segment.source_text
            for segment in segments
        ) == literal, split


def test_nested_comment_and_raw_closes_release_only_the_outside_terminal_call() -> None:
    prefix = (
        '<pre data-example="/>">\n'
        "<!--\n</pre> is documentation, not a close\n-->\n"
        f"{_QWEN_CALL}\n"
        "</pre>\n"
    )
    text = prefix + _QWEN_CALL
    batch = _direct_classify(
        text,
        dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
        tool=_SEARCH_TOOL,
    )
    assert [item.arguments for item in _direct_calls(batch)] == [{"query": "x"}]
    assert "".join(
        segment.text
        for segment in batch
        if isinstance(segment, LiteralTextSegment)
    ) == prefix

    for split in range(1, len(text)):
        visible, segments = _direct_stream(
            text,
            dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert [item.arguments for item in _direct_calls(segments)] == [
            {"query": "x"}
        ], split
        assert visible + "".join(
            segment.text
            for segment in segments
            if isinstance(segment, LiteralTextSegment)
        ) == prefix, split


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        (TEXT_TOOL_DIALECT_QWEN_TAG, _QWEN_CALL),
        (TEXT_TOOL_DIALECT_MINIMAX_XML, _MINIMAX_CALL),
        (TEXT_TOOL_DIALECT_PLAIN_JSON, _PLAIN_CALL),
    ],
)
def test_oversized_unfinished_html_token_permanently_fails_closed(
    dialect: TextToolDialect,
    call: str,
) -> None:
    literal = '<pre data-example="' + ("x" * 4_200) + "\n</pre>\n" + call
    assert _direct_classify(literal, dialect=dialect, tool=_SEARCH_TOOL) == [
        LiteralTextSegment(literal)
    ]
    for split in (1, 4, 32, 4_095, 4_096, 4_097, len(literal) - 1):
        visible, segments = _direct_stream(
            literal,
            dialect=dialect,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert _direct_calls(segments) == [], split
        assert visible + "".join(
            segment.text
            if isinstance(segment, LiteralTextSegment)
            else segment.source_text
            for segment in segments
        ) == literal, split


def test_oversized_unfinished_html_token_is_bounded_for_one_character_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.provider.text_tool_normalizer._RAW_HTML_TOKEN_MAX_CHARS",
        64,
    )
    literal = '<pre data-example="' + ("x" * 80) + "\n</pre>\n" + _QWEN_CALL
    visible, segments = _direct_stream_chunks(
        literal,
        dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
        tool=_SEARCH_TOOL,
    )
    assert _direct_calls(segments) == []
    assert visible + "".join(
        segment.text
        if isinstance(segment, LiteralTextSegment)
        else segment.source_text
        for segment in segments
    ) == literal


def test_raw_html_scan_scales_near_linearly_for_repeated_comments() -> None:
    def elapsed(repetitions: int) -> float:
        text = "<!--x-->" * repetitions
        started = time.perf_counter()
        ranges = _raw_html_code_ranges(text)
        duration = time.perf_counter() - started
        assert len(ranges) == repetitions
        return duration

    small = min(elapsed(8_000) for _ in range(2))
    large = min(elapsed(32_000) for _ in range(2))
    assert large <= (small * 6) + 0.05


def test_markdown_fences_and_raw_html_do_not_pollute_each_other() -> None:
    fenced_prefix = "```html\n<pre>\n```\n"
    raw_prefix = "<pre>\n```html\n</pre>\n"
    for prefix in (fenced_prefix, raw_prefix):
        text = prefix + _QWEN_CALL
        batch = _direct_classify(
            text,
            dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
            tool=_SEARCH_TOOL,
        )
        assert [item.arguments for item in _direct_calls(batch)] == [
            {"query": "x"}
        ]
        for split in range(1, len(text)):
            visible, segments = _direct_stream(
                text,
                dialect=TEXT_TOOL_DIALECT_QWEN_TAG,
                tool=_SEARCH_TOOL,
                split=split,
            )
            assert [item.arguments for item in _direct_calls(segments)] == [
                {"query": "x"}
            ], (prefix, split)
            assert visible + "".join(
                segment.text
                for segment in segments
                if isinstance(segment, LiteralTextSegment)
            ) == prefix, (prefix, split)


@pytest.mark.parametrize(
    ("dialect", "call"),
    [
        (TEXT_TOOL_DIALECT_QWEN_TAG, _QWEN_CALL),
        (TEXT_TOOL_DIALECT_MINIMAX_XML, _MINIMAX_CALL),
        (TEXT_TOOL_DIALECT_PLAIN_JSON, _PLAIN_CALL),
    ],
)
@pytest.mark.parametrize(
    "template",
    [
        "`{call}`",
        "```xml\n{call}\n```",
        "```xml\n{call}",
        "~~~xml\n{call}",
        "> {call}",
        "- {call}",
        "1. {call}",
        "    {call}",
        "<code>{call}</code>",
        "<pre>{call}</pre>",
    ],
)
def test_markdown_protocol_examples_are_literal_for_every_dialect_and_split(
    dialect: TextToolDialect,
    call: str,
    template: str,
) -> None:
    literal = template.format(call=call)
    assert _direct_classify(literal, dialect=dialect, tool=_SEARCH_TOOL) == [
        LiteralTextSegment(literal)
    ]
    for split in range(1, len(literal)):
        visible, segments = _direct_stream(
            literal,
            dialect=dialect,
            tool=_SEARCH_TOOL,
            split=split,
        )
        assert _direct_calls(segments) == [], split
        literal_tail = "".join(
            segment.text
            if isinstance(segment, LiteralTextSegment)
            else segment.source_text
            for segment in segments
        )
        assert visible + literal_tail == literal, split


@pytest.mark.parametrize(
    "literal",
    [
        f"`{_QWEN_CALL}`",
        f"~~~xml\n{_QWEN_CALL}\n~~~",
        f"    {_QWEN_CALL}",
    ],
)
def test_code_text_is_passthrough_not_held_until_finish(literal: str) -> None:
    normalizer = TextToolStreamNormalizer(
        tools=[_SEARCH_TOOL],
        dialects=frozenset({TEXT_TOOL_DIALECT_QWEN_TAG}),
        provider_kind="dashscope",
        model="qwen3.6-flash",
    )
    visible = ""
    for char in literal:
        emitted = normalizer.push(char)
        visible += "".join(emitted)
        # Once a code signal is complete, all subsequent input is live.  The
        # final assertion before finish proves no protocol body was retained.
    assert visible == literal
    assert normalizer.finish(successful_text_tool_terminal=True) == []


def test_matching_native_call_consumes_duplicate_text_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=list(_QWEN_CALL),
        tools=[_SEARCH_TOOL],
        native_tool_call=True,
        native_query="x",
        finish_reason="tool_calls",
    )
    assert _text(events) == ""
    ends = _tool_ends(events)
    assert len(ends) == 1
    assert ends[0].tool_use_id == "call_native"
    assert ends[0].arguments == {"query": "x"}
    assert ends[0].synthetic_from_text is False


@pytest.mark.parametrize("matching_position", [0, 1, 2])
def test_text_candidate_deduplicates_against_full_native_batch(
    monkeypatch: pytest.MonkeyPatch,
    matching_position: int,
) -> None:
    queries = ["other-0", "other-1", "other-2"]
    queries[matching_position] = "x"
    native_tool_calls = [
        {
            "index": index,
            "id": f"call_{index}",
            "function": {
                "name": "search",
                "arguments": json.dumps({"query": query}),
            },
        }
        for index, query in enumerate(queries)
    ]
    body = _raw_sse(
        [
            {"choices": [{"delta": {"content": _QWEN_CALL}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {"tool_calls": native_tool_calls},
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="dashscope",
        model="qwen3.6-flash",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )

    assert _text(events) == ""
    assert [event.arguments["query"] for event in _tool_ends(events)] == queries


def test_multicall_text_segment_is_deduplicated_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text_call = (
        "<minimax:tool_call>"
        '<invoke name="search"><parameter name="query">x</parameter></invoke>'
        '<invoke name="search"><parameter name="query">y</parameter></invoke>'
        "</minimax:tool_call>"
    )
    body = _raw_sse(
        [
            {"choices": [{"delta": {"content": text_call}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_x",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"query":"x"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = _collect_stream(
        monkeypatch,
        provider_kind="minimax",
        model="MiniMax-M2.7",
        text_chunks=[],
        tools=[_SEARCH_TOOL],
        raw_body=body,
    )

    assert _text(events) == text_call
    assert [event.arguments for event in _tool_ends(events)] == [{"query": "x"}]


@pytest.mark.parametrize(
    ("provider_kind", "model"),
    [
        ("dashscope", "qwen3.6-flash"),
        ("minimax", "MiniMax-M2.7"),
        ("openrouter", "deepseek/deepseek-v4"),
        ("tokenrhythm", "glm-5.2"),
    ],
)
def test_narrowed_plain_candidate_is_literal_and_diagnosable_once(
    monkeypatch: pytest.MonkeyPatch,
    provider_kind: str,
    model: str,
) -> None:
    with structlog.testing.capture_logs() as captured:
        events = _collect_stream(
            monkeypatch,
            provider_kind=provider_kind,
            model=model,
            text_chunks=list(_PLAIN_CALL),
            tools=[_SEARCH_TOOL],
        )
    assert _text(events) == _PLAIN_CALL
    assert _tool_ends(events) == []
    warnings = [
        item
        for item in captured
        if item["event"] == "provider.text_tool_candidate_not_authorized"
    ]
    assert len(warnings) == 1
    assert warnings[0]["provider"] == provider_kind
    assert warnings[0]["model"] == model
    assert "raw" not in warnings[0]
    assert "arguments" not in warnings[0]


def test_embedded_plain_example_is_literal_without_candidate_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    literal = f"Example: {_PLAIN_CALL}"
    with structlog.testing.capture_logs() as captured:
        events = _collect_stream(
            monkeypatch,
            provider_kind="dashscope",
            model="qwen3.6-flash",
            text_chunks=list(literal),
            tools=[_SEARCH_TOOL],
        )
    assert _text(events) == literal
    assert _tool_ends(events) == []
    assert not any(
        item["event"] == "provider.text_tool_candidate_not_authorized"
        for item in captured
    )


def test_oversized_candidate_replays_and_disables_synthesis_without_raw_log() -> None:
    normalizer = TextToolStreamNormalizer(
        tools=[_SEARCH_TOOL],
        dialects=frozenset({TEXT_TOOL_DIALECT_QWEN_TAG}),
        provider_kind="dashscope",
        model="qwen3.6-flash",
        max_candidate_chars=32,
    )
    literal = "<tool_call>" + "sensitive-value-" * 4
    with structlog.testing.capture_logs() as captured:
        emitted = normalizer.push(literal)
        emitted.extend(normalizer.push(_QWEN_CALL))
        tail = normalizer.finish(successful_text_tool_terminal=True)
    assert "".join(emitted) == literal + _QWEN_CALL
    assert tail == []
    warning = next(
        item
        for item in captured
        if item["event"] == "provider.text_tool_candidate_oversized"
    )
    assert warning["max_candidate_chars"] == 32
    assert "raw" not in warning


def test_deferred_native_queue_cap_releases_literal_before_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.provider.openai._MAX_DEFERRED_NATIVE_ARGUMENT_CHARS",
        len(_QWEN_CALL) + 8,
    )
    body = _raw_sse(
        [
            {"choices": [{"delta": {"content": _QWEN_CALL}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"query":',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"x"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    with structlog.testing.capture_logs() as captured:
        events = _collect_stream(
            monkeypatch,
            provider_kind="dashscope",
            model="qwen3.6-flash",
            text_chunks=[],
            tools=[_SEARCH_TOOL],
            raw_body=body,
        )
    assert _text(events) == _QWEN_CALL
    assert [event.arguments for event in _tool_ends(events)] == [{"query": "x"}]
    relevant = [
        event.kind
        for event in events
        if event.kind in {"text_delta", "tool_use_start", "tool_use_delta", "tool_use_end"}
    ]
    assert relevant[0:2] == ["text_delta", "tool_use_start"]
    warning = next(
        item
        for item in captured
        if item["event"] == "provider.deferred_native_queue_oversized"
    )
    assert "raw" not in warning
    assert "arguments" not in warning


def test_post_native_queue_cap_releases_literal_native_then_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.provider.openai._MAX_DEFERRED_NATIVE_ARGUMENT_CHARS",
        len(_QWEN_CALL) + len('{"query":"x"}') + 3,
    )
    suffix = "long suffix"
    body = _raw_sse(
        [
            {"choices": [{"delta": {"content": _QWEN_CALL}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"query":"x"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {"content": suffix}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_stream(
            monkeypatch,
            provider_kind="dashscope",
            model="qwen3.6-flash",
            text_chunks=[],
            tools=[_SEARCH_TOOL],
            raw_body=body,
        )

    assert _text(events) == _QWEN_CALL + suffix
    relevant = [
        event.kind
        for event in events
        if event.kind in {"text_delta", "tool_use_start", "tool_use_delta", "tool_use_end"}
    ]
    assert relevant == [
        "text_delta",
        "tool_use_start",
        "tool_use_delta",
        "text_delta",
        "tool_use_end",
    ]
    assert any(
        item["event"] == "provider.deferred_native_queue_oversized"
        for item in captured
    )


def test_combined_candidate_and_unresolved_identity_share_one_holdback_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "opensquilla.provider.openai._MAX_DEFERRED_NATIVE_ARGUMENT_CHARS",
        len(_QWEN_CALL) + 5,
    )
    body = _raw_sse(
        [
            {"choices": [{"delta": {"content": _QWEN_CALL}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_native",
                                    "function": {"arguments": '{"query":"x"}'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
        ]
    )

    with structlog.testing.capture_logs() as captured:
        events = _collect_stream(
            monkeypatch,
            provider_kind="dashscope",
            model="qwen3.6-flash",
            text_chunks=[],
            tools=[_SEARCH_TOOL],
            raw_body=body,
        )

    assert _text(events) == _QWEN_CALL
    assert not any(isinstance(event, ToolUseStartEvent) for event in events)
    assert _tool_ends(events) == []
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in events
    )
    assert any(
        item["event"] == "provider.pending_native_identity_oversized"
        for item in captured
    )


def test_deferred_event_buffer_uses_fragment_rope_and_materializes_once() -> None:
    buffer = _DeferredStreamEventBuffer()
    first = ToolUseDeltaEvent(tool_use_id="call", json_fragment="x")
    buffer.append(first)
    for _ in range(9_999):
        buffer.append(ToolUseDeltaEvent(tool_use_id="call", json_fragment="x"))

    assert first.json_fragment == "x"
    assert buffer.event_count == 1
    assert buffer.char_count == 10_000
    events = buffer.drain()
    assert len(events) == 1
    assert isinstance(events[0], ToolUseDeltaEvent)
    assert events[0].json_fragment == "x" * 10_000
    assert buffer.event_count == 0
    assert buffer.char_count == 0


def test_empty_stream_without_terminal_evidence_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _collect_stream(
        monkeypatch,
        provider_kind="openai",
        model="gpt-test",
        text_chunks=[],
        raw_body=b"",
    )
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_stream"
        for event in events
    )
    assert not any(isinstance(event, DoneEvent) for event in events)


def test_provider_import_before_tools_does_not_freeze_builtin_registry() -> None:
    code = """
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.tools import get_default_registry
names = set(get_default_registry().list_names())
required = {"cron", "gateway", "agents_list", "sessions_list"}
missing = sorted(required - names)
assert not missing, missing
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
