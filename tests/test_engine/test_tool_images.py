from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
from typing import Any

import httpx
import pytest
from PIL import Image

from opensquilla.engine import Agent, AgentConfig, tool_images
from opensquilla.engine.tool_images import ToolImageBudget, project_tool_images
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.types import ContentBlockImage, ContentBlockToolResult, ModelCapabilities
from opensquilla.tool_boundary import ToolCall, ToolOutput, ToolResult
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import ToolSpec


def _image(*, format: str = "PNG", width: int = 2) -> tuple[dict[str, Any], bytes]:
    stream = io.BytesIO()
    Image.new("RGB", (width, 2), "green").save(stream, format=format)
    raw = stream.getvalue()
    return {
        "type": "image",
        "mimeType": "image/png" if format == "PNG" else "image/jpeg",
        "data": base64.b64encode(raw).decode(),
    }, raw


@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_inline_images_keep_exact_bytes_and_source_hash(format: str) -> None:
    block, raw = _image(format=format)
    result = ToolResult("call-1", "mcp_read", "source text", content_blocks=[block])
    projection = project_tool_images(result, ToolImageBudget(), supports_vision=True)
    assert projection.records[0]["status"] == "prepared"
    assert projection.records[0]["sourceSha256"] == hashlib.sha256(raw).hexdigest()
    assert "sent" not in projection.records[0]
    images = [
        part for part in projection.messages[0].content if isinstance(part, ContentBlockImage)
    ]
    assert len(images) == 1
    assert images[0].data == block["data"]
    assert result.content_blocks == [block]


@pytest.mark.parametrize(
    "block,reason",
    [
        ({"type": "image", "mimeType": "image/svg+xml", "data": "AA=="}, "unsupported_image_mime"),
        ({"type": "image", "mimeType": [], "data": "AA=="}, "unsupported_image_mime"),
        ({"type": "image", "mimeType": "image/png", "data": "%%%"}, "invalid_image_base64"),
        ({"type": "image", "mimeType": "image/png", "data": "AA=="}, "invalid_image"),
        (
            {"type": "image", "mimeType": "image/png", "data": "data:image/png;base64,AA=="},
            "inline_base64_required",
        ),
        (
            {
                "type": "image",
                "mimeType": "image/png",
                "data": "https://example.invalid/picture.png",
            },
            "inline_base64_required",
        ),
        (
            {"type": "resource_link", "uri": "https://example.invalid/private"},
            "unsupported_content_block; no resource was fetched",
        ),
        (
            {"type": "resource", "resource": {"uri": "file:///private"}},
            "unsupported_content_block; no resource was fetched",
        ),
    ],
)
def test_invalid_or_remote_content_is_not_delivered_or_fetched(
    block: dict[str, Any], reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("tool media preparation must never fetch external content")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx, "get", forbidden)
    result = ToolResult("call-1", "mcp_read", "", content_blocks=[block])
    projection = project_tool_images(result, ToolImageBudget(), supports_vision=True)
    assert projection.records == [{"blockIndex": 0, "status": "omitted", "reason": reason}]
    assert not any(isinstance(part, ContentBlockImage) for part in projection.messages[0].content)
    assert reason in str(projection.messages[0].content)


@pytest.mark.parametrize("kind", ["bytes", "pixels", "count", "total", "blocks"])
def test_image_limits_are_bounded(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    block, raw = _image()
    blocks = [block]
    budget = ToolImageBudget()
    if kind == "bytes":
        monkeypatch.setattr(tool_images, "MAX_IMAGE_BYTES", len(raw) - 1)
    elif kind == "pixels":
        monkeypatch.setattr(tool_images, "MAX_IMAGE_PIXELS", 3)
    elif kind == "count":
        budget.remaining_images = 0
    elif kind == "total":
        budget.remaining_bytes = len(raw) - 1
    else:
        monkeypatch.setattr(tool_images, "MAX_CONTENT_BLOCKS", 0)
    projection = project_tool_images(
        ToolResult("id", "read", "", content_blocks=blocks), budget, supports_vision=True
    )
    assert projection.records
    assert all(item["status"] == "omitted" for item in projection.records)
    assert not any(isinstance(part, ContentBlockImage) for part in projection.messages[0].content)


def test_error_images_are_preserved_but_not_prepared() -> None:
    block, _ = _image()
    result = ToolResult("id", "read", "failed", True, content_blocks=[block])
    projection = project_tool_images(result, ToolImageBudget(), supports_vision=True)
    assert projection.records[0]["reason"] == "tool_error"
    assert result.content_blocks == [block]


def test_turn_budget_is_shared_across_tool_calls() -> None:
    block, raw = _image()
    result = ToolResult("id", "read", "", content_blocks=[block])
    budget = ToolImageBudget(remaining_images=1, remaining_bytes=len(raw))
    assert (
        project_tool_images(result, budget, supports_vision=True).records[0]["status"] == "prepared"
    )
    assert (
        project_tool_images(result, budget, supports_vision=True).records[0]["reason"]
        == "turn_image_budget"
    )


@pytest.mark.parametrize("supports_vision", [False, None])
def test_text_only_or_unknown_model_keeps_hash_without_consuming_budget(
    supports_vision: bool | None,
) -> None:
    block, raw = _image()
    result = ToolResult("id", "read", "source", content_blocks=[block])
    budget = ToolImageBudget(remaining_images=1, remaining_bytes=len(raw))
    projection = project_tool_images(result, budget, supports_vision=supports_vision)
    reason = "model_vision_unsupported" if supports_vision is False else "model_vision_unknown"
    assert projection.records == [
        {
            "blockIndex": 0,
            "status": "omitted",
            "sourceSha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "reason": reason,
        }
    ]
    assert reason in str(projection.messages[0].content)
    assert not any(isinstance(part, ContentBlockImage) for part in projection.messages[0].content)
    assert budget == ToolImageBudget(remaining_images=1, remaining_bytes=len(raw))
    assert result.content_blocks == [block]


@pytest.mark.asyncio
async def test_text_projection_preserves_raw_blocks_and_structured_identity() -> None:
    block, _ = _image()
    agent = Agent(provider=OpenAIProvider("offline-test"), config=AgentConfig(max_iterations=2))
    result = ToolResult(
        "id",
        "read",
        "source " * 1000,
        content_blocks=[block],
        structured_content={"sourceRevision": "a" * 64},
    )
    projected = await agent._project_tool_result_for_delivery(
        result, tool_call=ToolCall("id", "read", {})
    )
    assert projected.content_blocks == [block]
    assert projected.structured_content == result.structured_content


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_vision", [True, False, None])
async def test_real_agent_and_openai_adapter_respect_vision_capability(
    monkeypatch: pytest.MonkeyPatch,
    supports_vision: bool | None,
) -> None:
    requests: list[dict[str, Any]] = []
    block, raw = _image()

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            delta: dict[str, Any] = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "image-call",
                        "type": "function",
                        "function": {"name": "mcp_read", "arguments": "{}"},
                    }
                ]
            }
            stop = "tool_calls"
        else:
            delta, stop = {"content": "The table is visible."}, "stop"
        chunks = [
            {
                "id": "offline",
                "model": "gpt-4o",
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            },
            {
                "id": "offline",
                "model": "gpt-4o",
                "choices": [{"index": 0, "delta": {}, "finish_reason": stop}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        ]
        wire = (
            "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    client_class = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(respond)
        kwargs.pop("proxy", None)
        return client_class(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    registry = ToolRegistry()

    async def read() -> ToolOutput:
        return ToolOutput(
            "source text", content_blocks=[block], structured_content={"sourceRevision": "a" * 64}
        )

    registry.register(ToolSpec(name="mcp_read", description="Read source", parameters={}), read)
    agent = Agent(
        provider=OpenAIProvider("offline-test", base_url="https://offline.invalid"),
        config=AgentConfig(
            max_iterations=2,
            model_capabilities=(
                ModelCapabilities(supports_vision=supports_vision)
                if supports_vision is not None
                else None
            ),
        ),
        tool_definitions=registry.to_tool_definitions(),
        tool_handler=build_tool_handler(registry),
        session_key="agent:test:tool-images",
    )
    events = [event async for event in agent.run_turn("Read the table.")]
    assert not [event for event in events if event.kind == "error"]
    assert len(requests) == 2
    messages = requests[1]["messages"]
    tool_index = next(index for index, message in enumerate(messages) if message["role"] == "tool")
    assert messages[tool_index]["tool_call_id"] == "image-call"
    assert "source text" in messages[tool_index]["content"]
    image_messages = [
        message
        for message in messages[tool_index + 1 :]
        if isinstance(message.get("content"), list)
        and any(part.get("type") == "image_url" for part in message["content"])
    ]
    if supports_vision is True:
        assert len(image_messages) == 1
        image_url = next(
            part["image_url"]["url"]
            for part in image_messages[0]["content"]
            if part["type"] == "image_url"
        )
        assert base64.b64decode(image_url.split(",", 1)[1]) == raw
        assert "image-call" in json.dumps(image_messages[0])
    else:
        assert not image_messages
        wire = json.dumps(messages)
        reason = "model_vision_unsupported" if supports_vision is False else "model_vision_unknown"
        assert reason in wire
        assert "image_url" not in wire
        assert block["data"] not in wire
    assert hashlib.sha256(raw).hexdigest() in json.dumps(messages)
    assert any(
        isinstance(part, ContentBlockToolResult)
        for message in agent.history_snapshot()
        if not isinstance(message.content, str)
        for part in message.content
    )
