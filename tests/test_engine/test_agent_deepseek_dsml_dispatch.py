from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.types import ToolCall
from opensquilla.provider import ToolDefinition, ToolInputSchema
from opensquilla.provider.openai import OpenAIProvider

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_DSML_CALL = (
    '<｜DSML｜tool_calls><｜DSML｜invoke name="echo">'
    '<｜DSML｜parameter name="value" string="true">hello from dsml'
    '</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>'
)


def _sse(*chunks: dict[str, Any]) -> bytes:
    return b"".join(
        f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        for chunk in chunks
    ) + b"data: [DONE]\n\n"


def _streaming_text_response(text: str, *, finish_reason: str = "stop") -> bytes:
    return _sse(
        {
            "model": "deepseek-v4-flash",
            "choices": [{"delta": {"content": text}, "finish_reason": None}],
        },
        {
            "model": "deepseek-v4-flash",
            "choices": [{"delta": {}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    )


def _patch_deepseek_transport(
    monkeypatch: pytest.MonkeyPatch,
    response_bodies: list[bytes],
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        response_index = len(requests) - 1
        assert response_index < len(response_bodies), "unexpected extra provider request"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=response_bodies[response_index],
        )

    transport = httpx.MockTransport(handler)

    def patched_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.provider.openai.httpx.AsyncClient",
        patched_async_client,
    )
    return requests


def _deepseek_provider() -> OpenAIProvider:
    return OpenAIProvider(
        api_key="synthetic-test-key",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        provider_kind="deepseek",
        provider_id="deepseek",
    )


def _echo_definition() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="Return the supplied synthetic value.",
        input_schema=ToolInputSchema(
            properties={"value": {"type": "string"}},
            required=["value"],
            additionalProperties=False,
        ),
    )


def test_direct_deepseek_dsml_dispatches_fake_tool_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _patch_deepseek_transport(
        monkeypatch,
        [
            _streaming_text_response(_DSML_CALL),
            _streaming_text_response("tool result accepted"),
        ],
    )
    handled: list[ToolCall] = []

    async def tool_handler(call: ToolCall) -> ToolResult:
        handled.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="synthetic echo complete",
        )

    agent = Agent(
        provider=_deepseek_provider(),
        config=AgentConfig(
            max_iterations=2,
            max_provider_retries=0,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_definitions=[_echo_definition()],
        tool_handler=tool_handler,
    )

    async def run() -> list[Any]:
        return [event async for event in agent.run_turn("echo the synthetic value")]

    events = asyncio.run(run())

    assert len(requests) == 2
    assert all(request["model"] == "deepseek-v4-flash" for request in requests)
    assert [(call.tool_name, call.arguments) for call in handled] == [
        ("echo", {"value": "hello from dsml"})
    ]
    assert any(event.kind == "tool_result" for event in events)
    assert any(
        event.kind == "done" and event.text == "tool result accepted"
        for event in events
    )
    assert not any(_DSML_CALL in getattr(event, "text", "") for event in events)


def test_direct_deepseek_malformed_dsml_fails_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = (
        '<｜DSML｜tool_calls><｜DSML｜invoke name="echo">'
        '<｜DSML｜parameter name="value" string="true">never execute'
        '</｜DSML｜invoke></｜DSML｜tool_calls>'
    )
    requests = _patch_deepseek_transport(
        monkeypatch,
        [_streaming_text_response(malformed)],
    )
    handled: list[ToolCall] = []

    async def tool_handler(call: ToolCall) -> ToolResult:
        handled.append(call)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="unexpected",
        )

    agent = Agent(
        provider=_deepseek_provider(),
        config=AgentConfig(
            max_provider_retries=0,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
        tool_definitions=[_echo_definition()],
        tool_handler=tool_handler,
    )

    async def run() -> list[Any]:
        return [event async for event in agent.run_turn("echo the synthetic value")]

    events = asyncio.run(run())

    assert len(requests) == 1
    assert handled == []
    assert any(
        event.kind == "error" and event.code == "incomplete_tool_call"
        for event in events
    )
    assert not any(event.kind == "tool_result" for event in events)
    assert not any(malformed in getattr(event, "text", "") for event in events)
