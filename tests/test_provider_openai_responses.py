from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from opensquilla.provider import (
    ChatConfig,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    DoneEvent,
    Message,
    TextDeltaEvent,
)
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.registry import get_provider_spec
from opensquilla.provider.selector import build_provider
from opensquilla.provider.types import (
    ContentBlockImage,
    ErrorEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)


def _patch_transport(
    monkeypatch: Any,
    captured: dict[str, Any],
    response: httpx.Response,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["payload"] = (
            json.loads(request.content.decode("utf-8")) if request.content else None
        )
        return response

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.openai_responses.httpx.AsyncClient",
        patched_async_client,
    )


def _collect_events(
    provider: OpenAIResponsesProvider,
    *,
    config: ChatConfig | None = None,
) -> list[Any]:
    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                config=config or ChatConfig(),
            )
        ]

    return asyncio.run(_run())


def test_openai_responses_provider_is_separate_from_chat_completions_provider() -> None:
    provider = build_provider("openai_responses", "gpt-5.4", api_key="test")

    assert isinstance(provider, OpenAIResponsesProvider)
    assert get_provider_spec("openai_responses").backend == "openai_responses"
    assert get_provider_spec("openai").backend == "openai_compat"
    assert isinstance(
        build_provider("openai", "gpt-5.4", api_key="test"),
        OpenAIProvider,
    )


def test_openai_responses_api_url_absorbs_versioned_base_url() -> None:
    provider = OpenAIResponsesProvider(
        api_key="test",
        model="m",
        base_url="https://ark.cn-beijing.volces.com/api/coding/v3",
    )

    assert (
        provider._api_url("/v1/responses")
        == "https://ark.cn-beijing.volces.com/api/coding/v3/responses"
    )


def test_openai_responses_provider_posts_responses_payload_and_usage(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_test",
                "model": "gpt-5.4",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "ok",
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 5,
                    "input_tokens_details": {"cached_tokens": 1},
                    "output_tokens": 2,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 7,
                },
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")

    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                config=ChatConfig(system="stable system", max_tokens=12),
            )
        ]

    events = asyncio.run(_run())

    assert captured["url"] == "https://api.openai.com/v1/responses"
    payload = captured["payload"]
    assert payload["model"] == "gpt-5.4"
    assert payload["instructions"] == "stable system"
    assert payload["input"] == [{"role": "user", "content": "hi"}]
    assert payload["max_output_tokens"] == 12
    assert payload["store"] is False
    assert "messages" not in payload

    assert any(isinstance(event, TextDeltaEvent) and event.text == "ok" for event in events)
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.input_tokens == 5
    assert done.cached_tokens == 1
    assert done.output_tokens == 2
    assert done.reasoning_tokens == 0
    assert done.model == "gpt-5.4"


def test_openai_responses_candidate_mode_demotes_oversized_function_call(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    oversized_name = "proposed_action_" + ("x" * 17_000)
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_candidate_action",
                "model": "gpt-5.4",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "analysis"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_should_not_escape",
                        "name": oversized_name,
                        "arguments": '{"city":"Shanghai"}',
                    }
                ],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 5,
                    "output_tokens_details": {"reasoning_tokens": 2},
                },
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")

    events = _collect_events(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in events
    )
    artifact_event = next(
        event
        for event in events
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
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == "tool_use"
    assert done.input_tokens == 11
    assert done.output_tokens == 5
    assert done.reasoning_tokens == 2
    assert "".join(
        event.text for event in events if isinstance(event, TextDeltaEvent)
    ).startswith("analysis\n{")
    assert events.index(artifact_event) < events.index(done)


def test_openai_responses_normal_mode_keeps_tool_name_limit(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_invalid_action",
                "model": "gpt-5.4",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "x" * 257,
                        "arguments": "{}",
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")

    events = _collect_events(provider)

    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in events
    )
    assert not any(isinstance(event, DoneEvent) for event in events)


def test_openai_responses_candidate_mode_retains_semantically_invalid_call(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_degraded_action",
                "model": "gpt-5.4",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": None,
                        "arguments": '{"city":',
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")

    events = _collect_events(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in events
    )
    artifact_text = next(
        event.text for event in events if isinstance(event, TextDeltaEvent)
    )
    artifact = json.loads(artifact_text)
    assert artifact["actions"] == [
        {
            "arguments_text": '{"city":',
            "issues": ["invalid_arguments_json", "missing_name"],
            "name_text": "",
        }
    ]
    assert any(isinstance(event, DoneEvent) for event in events)


def test_openai_responses_candidate_mode_retains_length_truncated_function_call(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_truncated_candidate_action",
                "model": "gpt-5.4",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "type": "function_call",
                        "id": "private-item-id",
                        "call_id": "private-call-id",
                        "name": "draft_action",
                        "arguments": '{"city":"Shang',
                    }
                ],
                "usage": {"input_tokens": 8, "output_tokens": 13},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")

    events = _collect_events(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in events
    )
    artifact_text = next(
        event.text for event in events if isinstance(event, TextDeltaEvent)
    )
    assert "private-item-id" not in artifact_text
    assert "private-call-id" not in artifact_text
    assert json.loads(artifact_text)["actions"] == [
        {
            "arguments_text": '{"city":"Shang',
            "issues": ["incomplete_call", "invalid_arguments_json"],
            "name_text": "draft_action",
        }
    ]
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == "length"
    assert (done.input_tokens, done.output_tokens) == (8, 13)
    assert events[-2:] == [
        next(event for event in events if isinstance(event, TextDeltaEvent)),
        done,
    ]


@pytest.mark.parametrize(
    ("status", "incomplete_details", "item_status", "expected_stop_reason"),
    [
        ("completed", None, "completed", "tool_use"),
        (
            "incomplete",
            {"reason": "max_output_tokens"},
            "in_progress",
            "length",
        ),
    ],
)
def test_openai_responses_candidate_mode_does_not_render_structural_only_call(
    monkeypatch: Any,
    status: str,
    incomplete_details: dict[str, str] | None,
    item_status: str,
    expected_stop_reason: str,
) -> None:
    captured: dict[str, Any] = {}
    payload: dict[str, Any] = {
        "id": "resp_structural_only",
        "model": "gpt-5.4",
        "status": status,
        "output": [
            {
                "type": "function_call",
                "id": "private-item-id",
                "call_id": "private-call-id",
                "status": item_status,
                "name": "",
                "arguments": "",
            }
        ],
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }
    if incomplete_details is not None:
        payload["incomplete_details"] = incomplete_details
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(200, json=payload),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")

    events = _collect_events(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in events
    )
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == expected_stop_reason
    assert (done.input_tokens, done.output_tokens) == (2, 1)


def test_openai_responses_candidate_mode_strips_malformed_wrapper_ids(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_malformed_action",
                "model": "gpt-5.4",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "private-item-id",
                        "call_id": "private-call-id",
                        "status": "completed",
                        "name": "",
                        "arguments": {},
                        "payload": {
                            "tool_use_id": "private-nested-id",
                            "keep": True,
                        },
                    },
                    {
                        "type": "function_call",
                        "id": "private-no-arg-item-id",
                        "call_id": "private-no-arg-call-id",
                        "status": "completed",
                        "name": "get_status",
                        "arguments": {},
                    },
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")

    events = _collect_events(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
    )

    artifact_text = next(
        event.text for event in events if isinstance(event, TextDeltaEvent)
    )
    assert "private-item-id" not in artifact_text
    assert "private-call-id" not in artifact_text
    assert "private-nested-id" not in artifact_text
    assert "private-no-arg-item-id" not in artifact_text
    assert "private-no-arg-call-id" not in artifact_text
    actions = json.loads(artifact_text)["actions"]
    action = actions[0]
    assert json.loads(action["arguments_text"]) == {
        "malformed_function_call": {
            "arguments": {},
            "name": "",
            "payload": {"keep": True},
            "status": "completed",
            "type": "function_call",
        }
    }
    assert action["issues"] == ["missing_name"]
    assert actions[1] == {
        "arguments_text": "{}",
        "issues": [],
        "name_text": "get_status",
    }
    assert any(isinstance(event, DoneEvent) for event in events)


def test_openai_responses_sends_configured_json_output_schema(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_schema",
                "model": "gpt-5.4",
                "output": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"outcome": {"type": "string", "enum": ["allow", "deny"]}},
        "required": ["outcome"],
    }

    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                config=ChatConfig(
                    output_json_schema=schema,
                    output_json_schema_strict=False,
                ),
            )
        ]

    asyncio.run(_run())

    assert captured["payload"]["text"] == {
        "format": {
            "type": "json_schema",
            "name": "structured_output",
            "strict": False,
            "schema": schema,
        }
    }


def test_openai_responses_provider_writes_llm_trace(monkeypatch: Any, tmp_path: Any) -> None:
    captured: dict[str, Any] = {}
    trace_path = tmp_path / "responses-llm-calls.jsonl"
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_RECORDER", "full")
    monkeypatch.setenv("OPENSQUILLA_LLM_TRACE_PATH", str(trace_path))
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_trace",
                "model": "gpt-5.4",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"input_tokens": 5, "output_tokens": 2},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.4")

    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                config=ChatConfig(max_tokens=12),
            )
        ]

    events = asyncio.run(_run())

    assert any(isinstance(event, DoneEvent) for event in events)
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert [row["event"] for row in rows] == ["llm.request", "llm.response"]
    assert rows[0]["provider"] == "openai_responses"
    assert rows[0]["headers"]["Authorization"] == "[REDACTED]"
    assert rows[-1]["assistant_text"] == "ok"
    assert rows[-1]["response_ids"] == ["resp_trace"]


def test_openai_responses_compact_window_returns_opaque_output(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    compact_output = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "kept"}],
        },
        {
            "type": "reasoning",
            "encrypted_content": "opaque-encrypted-compaction-item",
        },
    ]
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_compact",
                "model": "gpt-5.5",
                "output": compact_output,
                "usage": {"input_tokens": 120, "output_tokens": 30},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.5")
    input_items = [
        {"type": "message", "role": "user", "content": "first"},
        {"type": "message", "role": "assistant", "content": "second"},
    ]

    compacted = asyncio.run(provider.compact_window(input_items))

    assert captured["url"] == "https://api.openai.com/v1/responses/compact"
    assert captured["payload"] == {"model": "gpt-5.5", "input": input_items}
    assert compacted["output"] == compact_output


def test_openai_responses_chat_items_sends_canonical_window_as_input(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_next",
                "model": "gpt-5.5",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "continued"}],
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 3},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.5")
    input_items = [
        {"type": "message", "role": "assistant", "content": "retained"},
        {"type": "reasoning", "encrypted_content": "opaque-latest"},
        {"type": "message", "role": "user", "content": "continue"},
    ]

    async def _run() -> list[Any]:
        return [event async for event in provider.chat_items(input_items)]

    events = asyncio.run(_run())

    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["payload"]["input"] == input_items
    assert "messages" not in captured["payload"]
    assert any(isinstance(event, TextDeltaEvent) and event.text == "continued" for event in events)


def test_openai_responses_list_models_uses_model_info_schema(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-5.5", "name": "GPT 5.5"},
                    {"id": "gpt-5.5-mini"},
                ]
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.5")

    models = asyncio.run(provider.list_models())

    assert captured["url"] == "https://api.openai.com/v1/models"
    assert [(model.provider, model.model_id, model.display_name) for model in models] == [
        ("openai_responses", "gpt-5.5", "GPT 5.5"),
        ("openai_responses", "gpt-5.5-mini", "gpt-5.5-mini"),
    ]


def test_openai_responses_chat_replays_tool_items(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_tool_followup",
                "model": "gpt-5.5",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 2},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.5")

    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [
                    Message(role="user", content="inspect"),
                    Message(
                        role="assistant",
                        content=[
                            ContentBlockText(text="I will inspect."),
                            ContentBlockToolUse(
                                id="call_read_1",
                                name="read_file",
                                input={"path": "README.md"},
                            ),
                        ],
                    ),
                    Message(
                        role="user",
                        content=[
                            ContentBlockToolResult(
                                tool_use_id="call_read_1",
                                content="README contents",
                            )
                        ],
                    ),
                    Message(role="user", content="continue"),
                ],
                config=ChatConfig(max_tokens=16),
            )
        ]

    asyncio.run(_run())

    assert captured["payload"]["input"] == [
        {"role": "user", "content": "inspect"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "I will inspect."}],
        },
        {
            "type": "function_call",
            "call_id": "call_read_1",
            "name": "read_file",
            "arguments": '{"path": "README.md"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_read_1",
            "output": "README contents",
        },
        {"role": "user", "content": "continue"},
    ]


def test_openai_responses_chat_sends_image_blocks_as_input_image(monkeypatch: Any) -> None:
    image_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_image",
                "model": "gpt-5.5",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 1},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.5")

    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [
                    Message(
                        role="user",
                        content=[
                            ContentBlockText(text="what does this dialog say?"),
                            ContentBlockImage(media_type="image/png", data=image_b64),
                        ],
                    )
                ],
                config=ChatConfig(max_tokens=16),
            )
        ]

    asyncio.run(_run())

    assert captured["payload"]["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what does this dialog say?"},
                {
                    "type": "input_image",
                    "image_url": f"data:image/png;base64,{image_b64}",
                },
            ],
        }
    ]


def test_openai_responses_incomplete_max_output_tokens_reports_length(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    _patch_transport(
        monkeypatch,
        captured,
        httpx.Response(
            200,
            json={
                "id": "resp_trunc",
                "model": "gpt-5.5",
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "def solve(n):"}],
                    }
                ],
                "usage": {"input_tokens": 40, "output_tokens": 16},
            },
        ),
    )
    provider = OpenAIResponsesProvider(api_key="test", model="gpt-5.5")

    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="write solve")],
                config=ChatConfig(max_tokens=16),
            )
        ]

    events = asyncio.run(_run())

    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.stop_reason == "length"
