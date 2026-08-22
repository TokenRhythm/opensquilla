from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from opensquilla.provider import anthropic as anthropic_module
from opensquilla.provider import openai as openai_module
from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import (
    ChatConfig,
    ContentBlockImage,
    ContentBlockText,
    ContentBlockToolResult,
    DoneEvent,
    ErrorEvent,
    Message,
    ToolDefinition,
    ToolInputSchema,
)


def _openai_sse_body(model: str = "test-model") -> bytes:
    chunks = [
        {
            "model": model,
            "choices": [{"delta": {"content": "ok"}, "finish_reason": None}],
        },
        {
            "model": model,
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    ]
    return b"".join(
        f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks
    ) + b"data: [DONE]\n\n"


def _anthropic_sse_body(events: list[dict[str, Any]]) -> bytes:
    parts: list[bytes] = []
    for event in events:
        parts.append(f"event: {event['type']}\n".encode())
        parts.append(f"data: {json.dumps(event)}\n\n".encode())
    return b"".join(parts)


def test_openai_final_request_proof_blocks_oversized_send(monkeypatch: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(api_key="test", model="gpt-test")

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="x" * 5000)],
                config=ChatConfig(provider_request_max_chars=1000),
            )
        ]

    events = asyncio.run(run())

    assert requests == []
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "provider_request_budget_exhausted"
    proof = json.loads(events[0].message)
    assert proof["fits"] is False
    assert proof["retry_count"] == 4
    assert proof["top_contributors"][0]["chars"] == 5000


def test_openai_env_request_proof_blocks_as_controlled_error(monkeypatch: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setenv("OPENSQUILLA_PROVIDER_REQUEST_PROOF_MAX_CHARS", "1")
    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(api_key="test", model="gpt-test")

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hello")],
                config=ChatConfig(provider_request_max_chars=0),
            )
        ]

    events = asyncio.run(run())

    assert requests == []
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "provider_request_budget_exhausted"


def test_openai_final_request_proof_allows_native_image_payload(
    monkeypatch: Any,
) -> None:
    requests: list[httpx.Request] = []
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_openai_sse_body(),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(
        api_key="test",
        model="vision-test",
        base_url="https://openrouter.ai/api/v1",
        provider_kind="openrouter",
    )
    messages = [
        Message(
            role="user",
            content=[
                ContentBlockText(text="describe this image"),
                ContentBlockImage(
                    source_type="base64",
                    media_type="image/png",
                    data="a" * 5000,
                ),
            ],
        )
    ]

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                messages,
                config=ChatConfig(provider_request_max_chars=10_000),
            )
        ]

    events = asyncio.run(run())

    assert len(requests) == 1
    assert any(isinstance(event, DoneEvent) for event in events)
    media_url = payloads[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert media_url.startswith("data:image/png;base64,")
    assert len(media_url) > 5000


def test_anthropic_final_request_proof_blocks_oversized_send(monkeypatch: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)
    provider = AnthropicProvider(api_key="test", model="claude-test")

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="x" * 5000)],
                config=ChatConfig(provider_request_max_chars=1000),
            )
        ]

    events = asyncio.run(run())

    assert requests == []
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "provider_request_budget_exhausted"
    proof = json.loads(events[0].message)
    assert proof["fits"] is False
    assert proof["retry_count"] == 4


def test_anthropic_env_request_proof_blocks_as_controlled_error(monkeypatch: Any) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setenv("OPENSQUILLA_PROVIDER_REQUEST_PROOF_MAX_CHARS", "1")
    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)
    provider = AnthropicProvider(api_key="test", model="claude-test")

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hello")],
                config=ChatConfig(provider_request_max_chars=0),
            )
        ]

    events = asyncio.run(run())

    assert requests == []
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "provider_request_budget_exhausted"


def test_anthropic_final_request_proof_allows_native_image_payload(
    monkeypatch: Any,
) -> None:
    requests: list[httpx.Request] = []
    payloads: list[dict[str, Any]] = []

    body = _anthropic_sse_body(
        [
            {
                "type": "message_start",
                "message": {"id": "msg_1", "model": "claude-test", "usage": {}},
            },
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "ok"},
            },
            {"type": "message_delta", "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)
    provider = AnthropicProvider(api_key="test", model="claude-test")
    messages = [
        Message(
            role="user",
            content=[
                ContentBlockText(text="describe this image"),
                ContentBlockImage(
                    source_type="base64",
                    media_type="image/png",
                    data="a" * 5000,
                ),
            ],
        )
    ]

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                messages,
                config=ChatConfig(provider_request_max_chars=10_000),
            )
        ]

    events = asyncio.run(run())

    assert len(requests) == 1
    assert any(isinstance(event, DoneEvent) for event in events)
    media_source = payloads[0]["messages"][0]["content"][1]["source"]
    assert media_source == {
        "type": "base64",
        "media_type": "image/png",
        "data": "a" * 5000,
    }


def _large_schema_tool() -> ToolDefinition:
    return ToolDefinition(
        name="large_schema_tool",
        description="Tool with enough schema text to prove final adapter payload accounting.",
        input_schema=ToolInputSchema(
            properties={
                "payload": {
                    "type": "string",
                    "description": "schema details " * 120,
                }
            },
            required=["payload"],
        ),
    )


def test_openai_request_proof_serialization_failure_is_controlled(
    monkeypatch: Any,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    provider = OpenAIProvider(api_key="test", model="gpt-test")

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                [Message(role="user", content="hello")],
                tools=[_large_schema_tool()],
                config=ChatConfig(
                    provider_request_max_chars=10_000,
                    tool_choice={"not-json-serializable"},
                ),
            )
        ]

    events = asyncio.run(run())

    assert requests == []
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "provider_internal"
    assert events[0].message == "Provider request could not be serialized."


def test_openai_final_request_proof_compacts_adapter_payload_with_tools(
    monkeypatch: Any,
) -> None:
    # Exercise the documented one-version rollback path for installations
    # that still permit request-only rewriting of recent/unresolved results.
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_RESULTS", "0")
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_ERROR_RESULTS", "0")
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_UNRESOLVED_RESULTS", "0")
    requests: list[httpx.Request] = []
    payloads: list[dict[str, Any]] = []
    proofs: list[dict[str, Any]] = []
    budget = 3600

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_openai_sse_body(),
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.openai.httpx.AsyncClient", patched_async_client)
    real_log = openai_module.log

    class _ProofLogShim:
        # Swap the module's ``log`` attribute wholesale instead of patching
        # ``log.info``: getattr on the BoundLoggerLazyProxy (which monkeypatch's
        # save step performs) binds an ephemeral logger to the processor list of
        # that moment, and teardown would install that frozen bound method on
        # the proxy permanently, breaking capture_logs for later tests.
        def info(self, event: str, **kwargs: Any) -> None:
            if event == "provider.request_proof":
                proofs.append(kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(real_log, name)

    monkeypatch.setattr(openai_module, "log", _ProofLogShim())
    provider = OpenAIProvider(api_key="test", model="gpt-test")
    messages = [
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_1",
                    content="tool output " * 500,
                )
            ],
        )
    ]

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                messages,
                tools=[_large_schema_tool()],
                config=ChatConfig(
                    system="system prompt " * 20,
                    provider_request_max_chars=budget,
                ),
            )
        ]

    events = asyncio.run(run())

    assert len(requests) == 1
    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(requests[0].content) <= budget
    assert any(
        "provider_request" in message.get("content", "")
        for message in payloads[0]["messages"]
    )
    proof = proofs[-1]
    assert proof["fits"] is True
    assert proof["retry_count"] >= 1
    assert proof["messages_chars"] > 0
    assert proof["tools_chars"] > 0
    assert proof["system_chars"] > 0
    assert proof["top_level_chars"] > 0


def test_anthropic_final_request_proof_compacts_adapter_payload_with_tools(
    monkeypatch: Any,
) -> None:
    # Exercise the documented one-version rollback path for installations
    # that still permit request-only rewriting of recent/unresolved results.
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_RECENT_RESULTS", "0")
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_ERROR_RESULTS", "0")
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_COMPACTION_PROTECT_UNRESOLVED_RESULTS", "0")
    requests: list[httpx.Request] = []
    payloads: list[dict[str, Any]] = []
    proofs: list[dict[str, Any]] = []
    budget = 3600
    body = _anthropic_sse_body(
        [
            {
                "type": "message_start",
                "message": {"id": "msg_1", "model": "claude-test", "usage": {}},
            },
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "ok"},
            },
            {"type": "message_delta", "usage": {"output_tokens": 1}},
            {"type": "message_stop"},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("opensquilla.provider.anthropic.httpx.AsyncClient", patched_async_client)
    real_log = anthropic_module.log

    class _ProofLogShim:
        def info(self, event: str, **kwargs: Any) -> None:
            if event == "provider.request_proof":
                proofs.append(kwargs)

        def __getattr__(self, name: str) -> Any:
            return getattr(real_log, name)

    monkeypatch.setattr(anthropic_module, "log", _ProofLogShim())
    provider = AnthropicProvider(api_key="test", model="claude-test")
    messages = [
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="call_1",
                    content="tool output " * 500,
                )
            ],
        )
    ]

    async def run() -> list[Any]:
        return [
            event
            async for event in provider.chat(
                messages,
                tools=[_large_schema_tool()],
                config=ChatConfig(
                    system="system prompt " * 20,
                    provider_request_max_chars=budget,
                ),
            )
        ]

    events = asyncio.run(run())

    assert len(requests) == 1
    assert any(isinstance(event, DoneEvent) for event in events)
    assert len(requests[0].content) <= budget
    assert "provider_request" in payloads[0]["messages"][0]["content"][0]["content"]
    proof = proofs[-1]
    assert proof["fits"] is True
    assert proof["retry_count"] >= 1
    assert proof["messages_chars"] > 0
    assert proof["tools_chars"] > 0
    assert proof["system_chars"] > 0
    assert proof["top_level_chars"] > 0
