from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from opensquilla.provider.compat_policy import compat_policy_for_kind
from opensquilla.provider.openai import OpenAIProvider
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

_TOOL = ToolDefinition(
    name="lookup",
    description="Look up a value.",
    input_schema=ToolInputSchema(
        properties={"q": {"type": "string"}},
        required=["q"],
    ),
)


def _sse(*chunks: dict[str, Any], done: bool = True) -> bytes:
    body = b"".join(
        f"data: {json.dumps(chunk)}\n\n".encode()
        for chunk in chunks
    )
    return body + (b"data: [DONE]\n\n" if done else b"")


def _patch_body(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
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

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)


def _collect(
    provider: OpenAIProvider,
    *,
    config: ChatConfig | None = None,
    tools: list[ToolDefinition] | None = None,
) -> list[Any]:
    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hi")],
                tools=[_TOOL] if tools is None else tools,
                config=config or ChatConfig(),
            )
        ]

    return asyncio.run(run())


def _assert_not_committed(events: list[Any], code: str | None = None) -> None:
    assert not any(isinstance(event, ToolUseEndEvent) for event in events)
    assert not any(isinstance(event, DoneEvent) for event in events)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert errors
    if code is not None:
        assert errors[-1].code == code


def _tokenrhythm_long_tool_name_sse(
    long_name: str,
    *,
    tool_call_id: str = "private-upstream-id",
) -> bytes:
    return _sse(
        {
            "id": "synthetic-tokenrhythm-response",
            "model": "glm-5.2",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": tool_call_id,
                                "function": {
                                    "name": long_name,
                                    "arguments": '{"city":"Shanghai"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "synthetic-tokenrhythm-response",
            "model": "glm-5.2",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 11},
        },
    )


def test_tokenrhythm_inert_candidate_demotes_overlong_native_tool_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_name = "candidate answer misplaced in function.name: " + ("x" * 17_000)
    long_tool_call_id = "private-upstream-id-" + ("i" * 300_000)
    monkeypatch.setattr(
        "opensquilla.provider.openai._candidate_wire_digest",
        lambda _value: pytest.fail("a valid stream index must take priority over wire IDs"),
    )
    _patch_body(
        monkeypatch,
        _tokenrhythm_long_tool_name_sse(
            long_name,
            tool_call_id=long_tool_call_id,
        ),
    )
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.2",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )

    events = _collect(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
        tools=[],
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    text_events = [event for event in events if isinstance(event, TextDeltaEvent)]
    assert len(text_events) == 1
    artifact = json.loads(text_events[0].text)
    assert artifact["kind"] == "inert_proposer_tool_output"
    assert artifact["executable"] is False
    assert artifact["actions"] == [
        {
            "arguments_text": '{"city":"Shanghai"}',
            "issues": ["name_over_execution_limit"],
            "name_text": long_name,
        }
    ]
    assert long_tool_call_id not in text_events[0].text
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert (done.input_tokens, done.output_tokens) == (7, 11)
    assert events[-2:] == [text_events[0], done]


def test_tokenrhythm_normal_mode_keeps_overlong_tool_name_safety_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _tokenrhythm_long_tool_name_sse("x" * 17_000),
    )
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.2",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
    )

    events = _collect(provider, tools=[])

    _assert_not_committed(events, "provider_protocol_error")


def test_openai_inert_candidate_reuses_single_call_for_identityless_continuations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "private-call-id",
                                    "function": {
                                        "name": "draft_action",
                                        "arguments": '{"city":',
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
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"function": {"arguments": '"Shanghai"'}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"function": {"arguments": "}"}}]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ),
    )
    provider = OpenAIProvider(api_key="test", model="compat-model")

    events = _collect(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
        tools=[],
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    artifact_text = next(
        event.text for event in events if isinstance(event, TextDeltaEvent)
    )
    assert json.loads(artifact_text)["actions"] == [
        {
            "arguments_text": '{"city":"Shanghai"}',
            "issues": [],
            "name_text": "draft_action",
        }
    ]
    assert "private-call-id" not in artifact_text
    assert isinstance(events[-1], DoneEvent)


def test_openai_inert_candidate_does_not_publish_without_terminal_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "name": "draft_action",
                                        "arguments": '{"partial":true}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            done=False,
        ),
    )
    provider = OpenAIProvider(api_key="test", model="glm-5.2")

    events = _collect(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
        tools=[],
    )

    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_stream"
        for event in events
    )


def test_openai_inert_candidate_strips_ids_from_malformed_tool_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": {
                                "id": "private-id",
                                "call_id": "private-call-id",
                                "payload": {
                                    "tool_use_id": "private-tool-use-id",
                                    "value": "keep",
                                },
                            }
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ]
            },
        ),
    )
    provider = OpenAIProvider(api_key="test", model="glm-5.2")

    events = _collect(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
        tools=[],
    )

    artifact_event = next(event for event in events if isinstance(event, TextDeltaEvent))
    assert "private-id" not in artifact_event.text
    assert "private-call-id" not in artifact_event.text
    assert "private-tool-use-id" not in artifact_event.text
    action = json.loads(artifact_event.text)["actions"][0]
    assert json.loads(action["arguments_text"]) == {"payload": {"value": "keep"}}
    assert isinstance(events[-1], DoneEvent)


def test_openai_inert_candidate_keeps_textual_tool_syntax_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    textual_call = (
        '<tool_call>{"name":"lookup","arguments":{"q":"Shanghai"}}</tool_call>'
    )
    _patch_body(
        monkeypatch,
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": textual_call},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ]
            },
        ),
    )
    provider = OpenAIProvider(
        api_key="test",
        model="qwen3.6-flash",
        provider_kind="dashscope",
    )

    events = _collect(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
        tools=[_TOOL],
    )

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        textual_call
    ]
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    assert isinstance(events[-1], DoneEvent)


def test_openai_stream_inert_candidate_preserves_malformed_mapping_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "private-wrapper-id",
                                    "type": "function",
                                    "function": {
                                        "name": "",
                                        "arguments": {},
                                    },
                                    "name": "draft_action",
                                    "arguments": {"city": "Shanghai"},
                                    "payload": {
                                        "tool_use_id": "private-nested-id",
                                        "keep": "advisory",
                                    },
                                },
                                {
                                    "index": 1,
                                    "id": "private-no-arg-id",
                                    "type": "function",
                                    "function": {
                                        "name": "get_status",
                                        "arguments": {},
                                    },
                                },
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ),
    )
    provider = OpenAIProvider(api_key="test", model="compat-model")

    events = _collect(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
        tools=[],
    )

    artifact_text = next(
        event.text for event in events if isinstance(event, TextDeltaEvent)
    )
    assert "private-wrapper-id" not in artifact_text
    assert "private-nested-id" not in artifact_text
    assert "private-no-arg-id" not in artifact_text
    actions = json.loads(artifact_text)["actions"]
    action = actions[0]
    assert action["name_text"] == ""
    assert action["issues"] == ["missing_name"]
    assert json.loads(action["arguments_text"]) == {
        "malformed_tool_call": {
            "arguments": {"city": "Shanghai"},
            "function": {"arguments": {}, "name": ""},
            "index": 0,
            "name": "draft_action",
            "payload": {"keep": "advisory"},
            "type": "function",
        }
    }
    assert actions[1] == {
        "arguments_text": "{}",
        "issues": [],
        "name_text": "get_status",
    }
    assert isinstance(events[-1], DoneEvent)


def test_openai_nonstream_inert_candidate_preserves_invalid_action_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_payload = {
        "id": "synthetic-nonstream-response",
        "model": "glm-5.2",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "private-nonstream-id",
                            "function": {
                                "name": "draft_action",
                                "arguments": "{not-json",
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        if request_payload.get("stream"):
            raise httpx.ReadTimeout("force fallback", request=request)
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    compat = replace(
        compat_policy_for_kind("tokenrhythm"),
        stream_timeout_fallback=True,
    )
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.2",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
        compat=compat,
    )

    events = _collect(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
        tools=[],
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    artifact_event = next(event for event in events if isinstance(event, TextDeltaEvent))
    artifact = json.loads(artifact_event.text)
    assert artifact["actions"][0] == {
        "arguments_text": "{not-json",
        "issues": ["invalid_arguments_json"],
        "name_text": "draft_action",
    }
    assert "private-nonstream-id" not in artifact_event.text
    done = next(event for event in events if isinstance(event, DoneEvent))
    assert (done.input_tokens, done.output_tokens) == (3, 5)
    assert events[-2:] == [artifact_event, done]


def test_openai_nonstream_inert_candidate_preserves_malformed_mapping_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_payload = {
        "id": "synthetic-nonstream-wrapper",
        "model": "glm-5.2",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "private-nonstream-wrapper-id",
                            "type": "function",
                            "function": {
                                "name": "",
                                "arguments": {},
                            },
                            "name": "draft_action",
                            "arguments": {"city": "Shanghai"},
                            "payload": {
                                "tool_use_id": "private-nested-id",
                                "keep": "advisory",
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        if request_payload.get("stream"):
            raise httpx.ReadTimeout("force fallback", request=request)
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    compat = replace(
        compat_policy_for_kind("tokenrhythm"),
        stream_timeout_fallback=True,
    )
    provider = OpenAIProvider(
        api_key="test",
        model="glm-5.2",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
        compat=compat,
    )

    events = _collect(
        provider,
        config=ChatConfig(candidate_output_mode="inert_artifact"),
        tools=[],
    )

    artifact_text = next(
        event.text for event in events if isinstance(event, TextDeltaEvent)
    )
    assert "private-nonstream-wrapper-id" not in artifact_text
    assert "private-nested-id" not in artifact_text
    action = json.loads(artifact_text)["actions"][0]
    assert action["issues"] == ["missing_name"]
    assert json.loads(action["arguments_text"]) == {
        "malformed_tool_call": {
            "arguments": {"city": "Shanghai"},
            "function": {"arguments": {}, "name": ""},
            "name": "draft_action",
            "payload": {"keep": "advisory"},
            "type": "function",
        }
    }
    assert isinstance(events[-1], DoneEvent)


def test_tokenrhythm_nonstream_fallback_preserves_confirmed_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_payload = {
        "id": "synthetic-response",
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "OK"},
            }
        ],
        "usage": {
            "prompt_tokens": 6,
            "completion_tokens": 9,
            "completion_tokens_details": {"reasoning_tokens": 6},
            "prompt_tokens_details": {"cached_tokens": 4},
        },
        "billing_pending": False,
        "cost_cny": 0.000021,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        if request_payload.get("stream"):
            raise httpx.ReadTimeout("force fallback", request=request)
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        patched_async_client,
    )
    compat = replace(
        compat_policy_for_kind("tokenrhythm"),
        stream_timeout_fallback=True,
    )
    provider = OpenAIProvider(
        api_key="test",
        model="deepseek-v4-flash",
        base_url="https://tokenrhythm.studio/v1",
        provider_kind="tokenrhythm",
        compat=compat,
    )

    events = _collect(provider)

    done = next(event for event in events if isinstance(event, DoneEvent))
    assert done.input_tokens == 6
    assert done.output_tokens == 9
    assert done.reasoning_tokens == 6
    assert done.cached_tokens == 4
    assert done.billed_cost == 0.000003011
    assert done.cost_source == "provider_billed"
    assert done.billing_receipt is not None
    assert done.billing_receipt.amount_nanos == 21_000
    assert done.billing_receipt.usd_equivalent_nanos == 3_011


def test_openai_stream_rejects_tool_mutation_after_finish_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "late",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":"late"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
        ),
    )

    events = _collect(OpenAIProvider(api_key="test", model="gpt-test"))

    _assert_not_committed(events, "invalid_stream_order")


def test_openai_stream_post_terminal_empty_choices_must_be_usage_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {}, "delta": {"content": "late"}},
        ),
    )

    events = _collect(OpenAIProvider(api_key="test", model="gpt-test"))

    _assert_not_committed(events, "invalid_stream_order")


def test_tokenrhythm_accepts_only_inert_choice_usage_epilogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "OK"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                "billing_pending": False,
                "cost_cny": 0.000001,
                "reasoning_available": True,
                "trace_id": "trace-synthetic",
            },
        ),
    )

    events = _collect(
        OpenAIProvider(
            api_key="test",
            model="deepseek-v4-flash",
            base_url="https://tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
        )
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == "stop"
    assert done[0].input_tokens == 3
    assert done[0].output_tokens == 2
    assert done[0].cost_source == "provider_billed"
    assert done[0].billing_receipt is not None
    assert done[0].billing_receipt.amount_nanos == 1_000


def test_tokenrhythm_billing_status_and_amount_can_arrive_on_separate_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "model": "deepseek-v4-flash",
                "choices": [
                    {"index": 0, "delta": {"content": "OK"}, "finish_reason": None}
                ],
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "billing_pending": False,
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                "cost_cny": 0.000001,
            },
        ),
    )

    events = _collect(
        OpenAIProvider(
            api_key="test",
            model="deepseek-v4-flash",
            base_url="https://tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
        )
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert len(done) == 1
    assert done[0].cost_source == "provider_billed"
    assert done[0].billing_receipt is not None
    assert done[0].billing_receipt.amount_nanos == 1_000


def test_tokenrhythm_ignores_post_terminal_null_usage_noop_before_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "model": "deepseek-v4-flash",
                "choices": [
                    {"index": 0, "delta": {"content": "OK"}, "finish_reason": None}
                ],
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": None,
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {}}],
                "usage": None,
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "completion_tokens_details": {"reasoning_tokens": 1},
                },
                "billing_pending": False,
                "cost_cny": 0.000001,
                "reasoning_available": True,
                "trace_id": "trace-synthetic-null-usage-noop",
            },
        ),
    )

    events = _collect(
        OpenAIProvider(
            api_key="test",
            model="deepseek-v4-flash",
            base_url="https://tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
        )
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == "stop"
    assert done[0].input_tokens == 3
    assert done[0].output_tokens == 2
    assert done[0].reasoning_tokens == 1
    assert done[0].cost_source == "provider_billed"
    assert done[0].billing_receipt is not None
    assert done[0].billing_receipt.amount_nanos == 1_000


def test_tokenrhythm_one_token_length_epilogue_accepts_billing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "reasoning_content": "synthetic reasoning",
                        },
                    }
                ],
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [{"index": 0, "delta": {}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            {
                "model": "deepseek-v4-flash",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "billing_pending": False,
                "cost_cny": 0.0,
                "reasoning_available": True,
                "trace_id": "trace-synthetic",
            },
        ),
    )

    events = _collect(
        OpenAIProvider(
            api_key="test",
            model="deepseek-v4-flash",
            base_url="https://tokenrhythm.studio/v1",
            provider_kind="tokenrhythm",
        )
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == "length"
    assert done[0].input_tokens == 1
    assert done[0].output_tokens == 1
    assert done[0].billed_cost == 0.0
    assert done[0].cost_source == "provider_billed"
    assert done[0].billing_receipt is not None
    assert done[0].billing_receipt.amount_nanos == 0


def test_openrouter_accepts_structurally_empty_choice_usage_epilogue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_choice = {
        "index": 0,
        "delta": {"content": "", "role": "assistant"},
        "finish_reason": "stop",
        "native_finish_reason": "stop",
    }
    _patch_body(
        monkeypatch,
        _sse(
            {
                "provider": "synthetic-provider",
                "choices": [terminal_choice],
            },
            {
                "provider": "synthetic-provider",
                "choices": [terminal_choice],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "cost": 0.012,
                },
            },
        ),
    )

    events = _collect(
        OpenAIProvider(
            api_key="test",
            model="openai/gpt-test",
            provider_kind="openrouter",
        )
    )

    assert not any(isinstance(event, ErrorEvent) for event in events)
    done = [event for event in events if isinstance(event, DoneEvent)]
    assert len(done) == 1
    assert done[0].stop_reason == "stop"
    assert done[0].input_tokens == 3
    assert done[0].output_tokens == 2
    assert done[0].billed_cost == 0.012
    assert done[0].billing_receipt is not None
    assert done[0].billing_receipt.currency == "USD"
    assert done[0].billing_receipt.amount_nanos == 12_000_000


@pytest.mark.parametrize(
    "second_choice",
    [
        {
            "index": 0,
            "delta": {"content": "late", "role": "assistant"},
            "finish_reason": "stop",
            "native_finish_reason": "stop",
        },
        {
            "index": 0,
            "delta": {"content": "", "role": "user"},
            "finish_reason": "stop",
            "native_finish_reason": "stop",
        },
        {
            "index": 0,
            "delta": {"content": "", "role": "assistant"},
            "finish_reason": "stop",
            "native_finish_reason": "length",
        },
    ],
    ids=["content", "role", "native-finish"],
)
def test_openrouter_choice_usage_epilogue_rejects_semantic_changes(
    monkeypatch: pytest.MonkeyPatch,
    second_choice: dict[str, Any],
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "provider": "synthetic-provider",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "", "role": "assistant"},
                        "finish_reason": "stop",
                        "native_finish_reason": "stop",
                    }
                ],
            },
            {
                "provider": "synthetic-provider",
                "choices": [second_choice],
                "usage": {},
            },
        ),
    )

    events = _collect(
        OpenAIProvider(
            api_key="test",
            model="openai/gpt-test",
            provider_kind="openrouter",
        )
    )

    _assert_not_committed(events, "invalid_stream_order")


def test_post_terminal_noop_choice_requires_explicit_provider_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        ),
    )

    events = _collect(OpenAIProvider(api_key="test", model="gpt-test"))

    _assert_not_committed(events, "invalid_stream_order")


@pytest.mark.parametrize(
    "epilogue",
    [
        {"choices": [{"index": 0, "delta": {"content": "late"}}], "usage": {}},
        {
            "choices": [{"index": 0, "delta": {"reasoning_content": "late"}}],
            "usage": {},
        },
        {
            "choices": [
                {"index": 0, "delta": {"tool_calls": []}, "finish_reason": "stop"}
            ],
            "usage": {},
        },
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}],
            "usage": {},
        },
        {"choices": [{"index": 1, "delta": {}}], "usage": {}},
        {"choices": [{"index": 0, "delta": []}], "usage": {}},
        {"choices": [{"index": 0, "finish_reason": "stop"}], "usage": {}},
        {"choices": [{"index": 0, "delta": {}, "message": {}}], "usage": {}},
        {"choices": [{"index": 0, "delta": {}}], "usage": []},
        {"choices": [], "usage": {}, "message": {"content": "late"}},
    ],
    ids=[
        "content",
        "reasoning",
        "tool-calls",
        "different-finish",
        "different-index",
        "non-object-delta",
        "missing-delta",
        "choice-extra",
        "non-object-usage",
        "top-level-message",
    ],
)
def test_tokenrhythm_post_terminal_epilogue_rejects_state_changes(
    monkeypatch: pytest.MonkeyPatch,
    epilogue: dict[str, Any],
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            epilogue,
        ),
    )

    events = _collect(
        OpenAIProvider(
            api_key="test",
            model="deepseek-v4-flash",
            provider_kind="tokenrhythm",
        )
    )

    _assert_not_committed(events, "invalid_stream_order")


def test_openai_stream_rejects_multiple_choice_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "choices": [
                    {"index": 0, "delta": {"content": "a"}, "finish_reason": "stop"},
                    {"index": 1, "delta": {"content": "b"}, "finish_reason": "stop"},
                ]
            }
        ),
    )

    events = _collect(OpenAIProvider(api_key="test", model="gpt-test"))

    _assert_not_committed(events, "invalid_stream_frame")


def test_openai_stream_duplicate_ids_across_indices_never_reach_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def call(index: int, q: str) -> dict[str, Any]:
        return {
            "index": index,
            "id": "duplicate",
            "function": {"name": "lookup", "arguments": json.dumps({"q": q})},
        }
    _patch_body(
        monkeypatch,
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [call(0, "a")]},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [call(1, "b")]},
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ),
    )

    events = _collect(OpenAIProvider(api_key="test", model="gpt-test"))

    _assert_not_committed(events, "provider_protocol_error")


def test_openai_stream_error_frame_discards_pending_tool_before_later_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":"x"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"error": {"code": "upstream", "message": "failed"}},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ),
    )

    events = _collect(OpenAIProvider(api_key="test", model="gpt-test"))

    _assert_not_committed(events, "upstream")


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
def test_openai_stream_explicit_empty_error_field_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    error_payload: Any,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"q":"x"}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"error": error_payload},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ),
    )

    events = _collect(OpenAIProvider(api_key="test", model="gpt-test"))

    _assert_not_committed(events, "stream_error")


def test_openai_stream_null_error_field_does_not_poison_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_body(
        monkeypatch,
        _sse(
            {
                "error": None,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
        ),
    )

    events = _collect(OpenAIProvider(api_key="test", model="gpt-test"))

    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"code": "bad", "message": "failed"}},
        {"error": {}},
        {"error": ""},
        {"error": False},
        {"error": 0},
        {"error": []},
        {},
        {"choices": []},
        {
            "choices": [
                {"index": 0, "message": {"content": "a"}, "finish_reason": "stop"},
                {"index": 1, "message": {"content": "b"}, "finish_reason": "stop"},
            ]
        },
        {
            "choices": [
                {"index": 1, "message": {"content": "bad"}, "finish_reason": "stop"}
            ]
        },
        {
            "choices": [
                {"index": 0, "message": {"content": "partial"}, "finish_reason": None}
            ]
        },
    ],
)
def test_openai_nonstream_requires_one_index_zero_finished_choice(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        if request_payload.get("stream"):
            raise httpx.ReadTimeout("force fallback", request=request)
        return httpx.Response(200, json=payload)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="test-model",
        provider_kind="openrouter",
    )

    events = _collect(provider)

    _assert_not_committed(events)


def test_openai_nonstream_null_error_field_does_not_poison_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_payload = {
        "error": None,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"content": "ok"},
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        if request_payload.get("stream"):
            raise httpx.ReadTimeout("force fallback", request=request)
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    events = _collect(
        OpenAIProvider(
            api_key="test",
            model="test-model",
            provider_kind="openrouter",
        )
    )

    assert any(isinstance(event, DoneEvent) for event in events)
    assert not any(isinstance(event, ErrorEvent) for event in events)


def test_openai_nonstream_duplicate_tool_ids_fail_before_first_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_payload = {
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "duplicate",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"q":"a"}',
                            },
                        },
                        {
                            "id": "duplicate",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"q":"b"}',
                            },
                        },
                    ],
                },
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        request_payload = json.loads(request.content)
        if request_payload.get("stream"):
            raise httpx.ReadTimeout("force fallback", request=request)
        return httpx.Response(200, json=response_payload)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    events = _collect(
        OpenAIProvider(
            api_key="test",
            model="test-model",
            provider_kind="openrouter",
        )
    )

    _assert_not_committed(events, "provider_protocol_error")
