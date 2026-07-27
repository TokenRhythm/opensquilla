from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from opensquilla.provider.ollama import _OLLAMA_DEFAULT_NUM_CTX, OllamaProvider
from opensquilla.provider.selector import ProviderConfig, _build_provider
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
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


def _ndjson(*chunks: dict[str, Any]) -> bytes:
    return b"".join((json.dumps(chunk) + "\n").encode() for chunk in chunks)


_DEFAULT_BODY = _ndjson(
    {"model": "llama3", "message": {"role": "assistant", "content": "ok"}},
    {
        "model": "llama3",
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "prompt_eval_count": 3,
        "eval_count": 2,
    },
)


def _patch_stream(monkeypatch: Any, captured: dict[str, Any], body: bytes = _DEFAULT_BODY) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.ollama.httpx.AsyncClient", patched_async_client)


def _collect(
    provider: OllamaProvider,
    messages: list[Message],
    cfg: ChatConfig | None = None,
    tools: list[ToolDefinition] | None = None,
) -> list[Any]:
    async def _run() -> list[Any]:
        return [
            event
            async for event in provider.chat(messages, tools=tools, config=cfg or ChatConfig())
        ]

    return asyncio.run(_run())


def test_tool_use_replayed_as_tool_calls_and_correlated_tool_message(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_stream(monkeypatch, captured)
    provider = OllamaProvider(model="llama3")
    messages = [
        Message(role="user", content="look it up"),
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="call_1", name="lookup", input={"q": "cache"})],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="call_1", content="cache is warm")],
        ),
        Message(role="user", content="continue"),
    ]

    _collect(provider, messages)

    msgs = captured["payload"]["messages"]
    # Assistant turn keeps its tool_calls instead of being dropped.
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["tool_calls"] == [{"function": {"name": "lookup", "arguments": {"q": "cache"}}}]
    # Tool result becomes its own tool-role message, correlated by tool_name.
    assert msgs[2] == {"role": "tool", "content": "cache is warm", "tool_name": "lookup"}
    assert msgs[3] == {"role": "user", "content": "continue"}


def test_parallel_tool_results_expand_to_separate_tool_messages(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_stream(monkeypatch, captured)
    provider = OllamaProvider(model="llama3")
    messages = [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(id="call_a", name="foo", input={"x": 1}),
                ContentBlockToolUse(id="call_b", name="bar", input={"y": 2}),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(tool_use_id="call_a", content="ra"),
                ContentBlockToolResult(tool_use_id="call_b", content="rb"),
            ],
        ),
    ]

    _collect(provider, messages)

    msgs = captured["payload"]["messages"]
    assert msgs[0]["tool_calls"] == [
        {"function": {"name": "foo", "arguments": {"x": 1}}},
        {"function": {"name": "bar", "arguments": {"y": 2}}},
    ]
    # Both results survive as distinct tool messages (the old code dropped all but one).
    assert msgs[1] == {"role": "tool", "content": "ra", "tool_name": "foo"}
    assert msgs[2] == {"role": "tool", "content": "rb", "tool_name": "bar"}


def test_tool_result_list_content_is_json_encoded(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_stream(monkeypatch, captured)
    provider = OllamaProvider(model="llama3")
    messages = [
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="call_x", content=[{"k": "v"}])],
        ),
    ]

    _collect(provider, messages)

    assert captured["payload"]["messages"][0]["content"] == json.dumps([{"k": "v"}])


def test_num_ctx_defaults_above_ollama_2048(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_stream(monkeypatch, captured)
    provider = OllamaProvider(model="llama3")

    _collect(provider, [Message(role="user", content="hi")], ChatConfig(max_tokens=512))

    options = captured["payload"]["options"]
    assert options["num_ctx"] == _OLLAMA_DEFAULT_NUM_CTX
    assert _OLLAMA_DEFAULT_NUM_CTX > 2048
    assert options["num_predict"] == 512


def test_num_ctx_is_configurable(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_stream(monkeypatch, captured)
    provider = OllamaProvider(model="llama3", num_ctx=32768)

    _collect(provider, [Message(role="user", content="hi")])

    assert captured["payload"]["options"]["num_ctx"] == 32768


def test_bearer_header_sent_when_api_key_set(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_stream(monkeypatch, captured)
    provider = OllamaProvider(
        model="gpt-oss:120b-cloud", base_url="https://ollama.com", api_key="secret"
    )

    _collect(provider, [Message(role="user", content="hi")])

    assert captured["url"] == "https://ollama.com/api/chat"
    assert captured["headers"]["authorization"] == "Bearer secret"


def test_no_authorization_header_without_api_key(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_stream(monkeypatch, captured)
    provider = OllamaProvider(model="llama3")

    _collect(provider, [Message(role="user", content="hi")])

    assert "authorization" not in captured["headers"]


def test_image_block_attached_to_message_images(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_stream(monkeypatch, captured)
    provider = OllamaProvider(model="llava")
    messages = [
        Message(
            role="user",
            content=[
                ContentBlockText(text="what is this"),
                ContentBlockImage(source_type="base64", media_type="image/png", data="QkFTRTY0"),
            ],
        ),
    ]

    _collect(provider, messages)

    msg = captured["payload"]["messages"][0]
    assert msg["content"] == "what is this"
    assert msg["images"] == ["QkFTRTY0"]


def test_system_prompt_is_first_message(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_stream(monkeypatch, captured)
    provider = OllamaProvider(model="llama3")

    _collect(provider, [Message(role="user", content="hi")], ChatConfig(system="be brief"))

    assert captured["payload"]["messages"][0] == {"role": "system", "content": "be brief"}


def test_stream_emits_text_and_done_with_usage(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    _patch_stream(monkeypatch, captured)
    provider = OllamaProvider(model="llama3")

    events = _collect(provider, [Message(role="user", content="hi")])

    text = next(e for e in events if isinstance(e, TextDeltaEvent))
    done = next(e for e in events if isinstance(e, DoneEvent))
    assert text.text == "ok"
    assert done.input_tokens == 3
    assert done.output_tokens == 2


def test_stream_tool_call_emits_tool_events(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    body = _ndjson(
        {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "lookup", "arguments": {"q": "hi"}}}],
            },
        },
        {
            "model": "llama3",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 4,
            "eval_count": 1,
        },
    )
    _patch_stream(monkeypatch, captured, body)
    provider = OllamaProvider(model="llama3")
    tool = ToolDefinition(
        name="lookup",
        description="Lookup a value.",
        input_schema=ToolInputSchema(properties={"q": {"type": "string"}}, required=["q"]),
    )

    events = _collect(provider, [Message(role="user", content="hi")], tools=[tool])

    tool_end = next(e for e in events if isinstance(e, ToolUseEndEvent))
    assert tool_end.tool_name == "lookup"
    assert tool_end.arguments == {"q": "hi"}
    assert captured["payload"]["tools"][0]["function"]["name"] == "lookup"


def test_stream_candidate_mode_demotes_oversized_tool_name(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    oversized_name = "proposed_action_" + ("x" * 17_000)
    body = _ndjson(
        {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "analysis",
                "tool_calls": [
                    {
                        "id": "call_should_not_escape",
                        "function": {
                            "name": oversized_name,
                            "arguments": {"city": "Shanghai"},
                        },
                    }
                ],
            },
        },
        {
            "model": "llama3",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 8,
            "eval_count": 3,
        },
    )
    _patch_stream(monkeypatch, captured, body)
    provider = OllamaProvider(model="llama3")

    events = _collect(
        provider,
        [Message(role="user", content="hi")],
        ChatConfig(candidate_output_mode="inert_artifact"),
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
    assert done.input_tokens == 8
    assert done.output_tokens == 3
    assert "".join(
        event.text for event in events if isinstance(event, TextDeltaEvent)
    ).startswith("analysis\n{")
    assert events.index(artifact_event) < events.index(done)


def test_stream_candidate_mode_rejects_rendered_artifact_over_total_limit(
    monkeypatch: Any,
) -> None:
    from opensquilla.provider.candidate_artifact import CandidateArtifactBuilder

    captured: dict[str, Any] = {}
    body = _ndjson(
        {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "\0" * 20,
                            "arguments": {},
                        },
                    }
                ],
            },
        },
        {
            "model": "llama3",
            "message": {"role": "assistant", "content": ""},
            "done": True,
        },
    )
    _patch_stream(monkeypatch, captured, body)
    monkeypatch.setattr(
        "opensquilla.provider.ollama.CandidateArtifactBuilder",
        lambda: CandidateArtifactBuilder(max_total_chars=180),
    )
    provider = OllamaProvider(model="llama3")

    events = _collect(
        provider,
        [Message(role="user", content="hi")],
        ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in events
    )
    assert not any(isinstance(event, DoneEvent) for event in events)
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "candidate_artifact_limit_exceeded"
        for event in events
    )


def test_stream_normal_mode_keeps_tool_name_limit(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    body = _ndjson(
        {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "x" * 257, "arguments": {"q": "hi"}}}
                ],
            },
        },
        {
            "model": "llama3",
            "message": {"role": "assistant", "content": ""},
            "done": True,
        },
    )
    _patch_stream(monkeypatch, captured, body)
    provider = OllamaProvider(model="llama3")

    events = _collect(provider, [Message(role="user", content="hi")])

    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_tool_call"
        for event in events
    )
    assert not any(isinstance(event, DoneEvent) for event in events)


def test_stream_candidate_mode_retains_malformed_tool_shape(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    body = _ndjson(
        {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": {
                    "id": "private-wrapper-id",
                    "nested": {
                        "tool_use_id": "private-nested-id",
                        "confidence": 0,
                        "unexpected": True,
                    },
                },
            },
        },
        {
            "model": "llama3",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 2,
            "eval_count": 1,
        },
    )
    _patch_stream(monkeypatch, captured, body)
    provider = OllamaProvider(model="llama3")

    events = _collect(
        provider,
        [Message(role="user", content="hi")],
        ChatConfig(candidate_output_mode="inert_artifact"),
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
            "arguments_text": (
                '{"malformed_tool_calls":{"nested":{"confidence":0,"unexpected":true}}}'
            ),
            "issues": ["missing_name"],
            "name_text": "",
        }
    ]
    assert "private-wrapper-id" not in artifact_text
    assert "private-nested-id" not in artifact_text
    assert any(isinstance(event, DoneEvent) for event in events)


def test_stream_candidate_mode_drops_null_and_empty_tool_wrappers(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    body = _ndjson(
        {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": None,
            },
        },
        {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": {},
            },
        },
        {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    None,
                    [],
                    {"id": "private-empty-id", "function": {}},
                    {"index": 0, "function": {}},
                    {"function": {"index": 0}},
                    {
                        "type": "function",
                        "function": {"name": "", "arguments": {}},
                    },
                ],
            },
        },
        {
            "model": "llama3",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "prompt_eval_count": 2,
            "eval_count": 1,
        },
    )
    _patch_stream(monkeypatch, captured, body)
    provider = OllamaProvider(model="llama3")

    events = _collect(
        provider,
        [Message(role="user", content="hi")],
        ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in events
    )
    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert any(isinstance(event, DoneEvent) for event in events)


def test_stream_candidate_mode_does_not_publish_artifact_before_done(
    monkeypatch: Any,
) -> None:
    captured: dict[str, Any] = {}
    body = _ndjson(
        {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "lookup", "arguments": {"q": "hi"}}}
                ],
            },
        },
    )
    _patch_stream(monkeypatch, captured, body)
    provider = OllamaProvider(model="llama3")

    events = _collect(
        provider,
        [Message(role="user", content="hi")],
        ChatConfig(candidate_output_mode="inert_artifact"),
    )

    assert any(
        isinstance(event, ErrorEvent) and event.code == "incomplete_stream"
        for event in events
    )
    assert not any(isinstance(event, TextDeltaEvent) for event in events)
    assert not any(
        isinstance(event, ToolUseStartEvent | ToolUseDeltaEvent | ToolUseEndEvent)
        for event in events
    )
    assert not any(isinstance(event, DoneEvent) for event in events)


def test_selector_passes_api_key_to_ollama_provider() -> None:
    cfg = ProviderConfig(
        provider="ollama",
        model="gpt-oss:120b-cloud",
        api_key="sk-test",
        base_url="https://ollama.com",
    )

    provider = _build_provider(cfg)

    assert isinstance(provider, OllamaProvider)
    assert provider._headers() == {"Authorization": "Bearer sk-test"}


def test_selector_omits_api_key_for_local_ollama() -> None:
    cfg = ProviderConfig(provider="ollama", model="llama3")

    provider = _build_provider(cfg)

    assert isinstance(provider, OllamaProvider)
    assert provider._headers() == {}
