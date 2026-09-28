"""Exercise real adapter serialization with offline HTTP, never a model service."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import httpx
import pytest
from PIL import Image

from opensquilla.engine.tool_images import ToolImageBudget, project_tool_images
from opensquilla.provider.anthropic import AnthropicProvider
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.openai_responses import OpenAIResponsesProvider
from opensquilla.provider.types import ContentBlockToolResult, ContentBlockToolUse, Message
from opensquilla.tool_boundary import ToolResult


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic", "responses"])
@pytest.mark.parametrize("supports_vision", [True, False, None])
async def test_real_adapter_wire_keeps_tool_result_and_distinct_image_message(
    kind: str, monkeypatch: pytest.MonkeyPatch, supports_vision: bool | None
) -> None:
    requests: list[dict[str, Any]] = []
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "green").save(image, format="PNG")
    encoded = base64.b64encode(image.getvalue()).decode()
    result = ToolResult(
        "call-one",
        "mcp_read",
        "source text",
        content_blocks=[{"type": "image", "mimeType": "image/png", "data": encoded}],
    )
    projection = project_tool_images(result, ToolImageBudget(), supports_vision=supports_vision)
    messages = [
        Message(
            role="assistant",
            content=[ContentBlockToolUse(id="call-one", name="mcp_read", input={})],
        ),
        Message(
            role="user",
            content=[ContentBlockToolResult(tool_use_id="call-one", content="source text")],
        ),
        *projection.messages,
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "offline wire capture"}})

    client_class = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(respond)
        kwargs.pop("proxy", None)
        return client_class(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    providers = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "responses": OpenAIResponsesProvider,
    }
    provider = providers[kind]("offline-test", base_url="https://offline.invalid")
    events = [event async for event in provider.chat(messages)]
    assert any(event.kind == "error" for event in events)
    assert len(requests) == 1
    payload = requests[0]
    if kind == "openai":
        tool = next(message for message in payload["messages"] if message["role"] == "tool")
        assert tool == {"role": "tool", "tool_call_id": "call-one", "content": "source text"}
        image_parts = [
            part
            for message in payload["messages"]
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image_url"
        ]
        assert image_parts == (
            [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}}]
            if supports_vision is True
            else []
        )
    elif kind == "anthropic":
        parts = [part for message in payload["messages"] for part in message["content"]]
        tool = next(part for part in parts if part["type"] == "tool_result")
        assert tool["tool_use_id"] == "call-one"
        assert tool["content"] == "source text"
        images = [part for part in parts if part["type"] == "image"]
        assert images == (
            [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": encoded},
                }
            ]
            if supports_vision is True
            else []
        )
    else:
        tool = next(item for item in payload["input"] if item.get("type") == "function_call_output")
        assert tool["call_id"] == "call-one"
        assert tool["output"] == "source text"
        parts = [
            part
            for item in payload["input"]
            if isinstance(item.get("content"), list)
            for part in item["content"]
        ]
        images = [part for part in parts if part["type"] == "input_image"]
        assert images == (
            [{"type": "input_image", "image_url": "data:image/png;base64," + encoded}]
            if supports_vision is True
            else []
        )
    if supports_vision is not True:
        reason = "model_vision_unsupported" if supports_vision is False else "model_vision_unknown"
        assert reason in json.dumps(payload)
        assert encoded not in json.dumps(payload)
