from __future__ import annotations

from typing import Any

import pytest

from opensquilla.mcp.sse import MCPSSEClient
from opensquilla.mcp.stdio import MCPStdioClient
from opensquilla.mcp.types import MCPServerConfig, MCPToolResult


@pytest.mark.parametrize("transport", ["stdio", "sse"])
@pytest.mark.parametrize("is_error", [False, True])
async def test_public_call_preserves_blocks_and_structured_content(
    monkeypatch: pytest.MonkeyPatch, transport: str, is_error: bool
) -> None:
    blocks = [
        {"type": "text", "text": "first", "annotations": {"audience": ["user"]}},
        {"type": "image", "data": "AA==", "mimeType": "image/png"},
        {"type": "resource", "resource": {"uri": "sample:///one", "blob": "AA=="}},
        {"type": "resource_link", "uri": "sample:///two", "name": "second"},
        {"type": "text", "text": "last"},
    ]
    response = {
        "result": {
            "content": blocks,
            "structuredContent": {"source": {"revision": "v1"}, "items": [1, 2]},
            "isError": is_error,
        }
    }
    client = _client(transport)

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "tools/call"
        assert params == {"name": "read", "arguments": {"id": "one"}}
        return response

    monkeypatch.setattr(client, _request_method(transport), request)
    result = await client.call_tool("read", {"id": "one"})

    assert result.content == "first\nlast"
    assert result.is_error is is_error
    assert result.content_blocks == blocks
    assert result.structured_content == response["result"]["structuredContent"]


@pytest.mark.parametrize("transport", ["stdio", "sse"])
@pytest.mark.parametrize(
    "payload",
    [
        {"content": [{"type": "image", "data": "AA==", "mimeType": "image/png"}]},
        {"structuredContent": {"count": 1}},
        {"content": []},
    ],
)
async def test_non_text_results_keep_empty_legacy_text(
    monkeypatch: pytest.MonkeyPatch, transport: str, payload: dict[str, Any]
) -> None:
    client = _client(transport)

    async def request(*_args: Any) -> dict[str, Any]:
        return {"result": payload}

    monkeypatch.setattr(client, _request_method(transport), request)
    result = await client.call_tool("read", {})
    assert result.content == ""
    assert not result.is_error
    assert result.content_blocks == payload.get("content", [])
    assert result.structured_content == payload.get("structuredContent")


@pytest.mark.parametrize("transport", ["stdio", "sse"])
@pytest.mark.parametrize(
    "response",
    [
        {},
        {"result": None},
        {"result": []},
        {"result": {"content": None}},
        {"result": {"content": "not blocks"}},
        {"result": {"content": [None]}},
        {"result": {"content": [{}]}},
        {"result": {"content": [{"type": ""}]}},
        {"result": {"content": [{"type": "text", "text": 42}]}},
        {"result": {"structuredContent": []}},
        {"result": {"isError": "false"}},
        {"error": None},
        {"error": {"message": []}},
    ],
)
async def test_malformed_results_fail_without_partial_success(
    monkeypatch: pytest.MonkeyPatch, transport: str, response: dict[str, Any]
) -> None:
    client = _client(transport)

    async def request(*_args: Any) -> dict[str, Any]:
        return response

    monkeypatch.setattr(client, _request_method(transport), request)
    result = await client.call_tool("read", {})
    assert result.is_error
    assert result.content.startswith("Malformed MCP")
    assert result.content_blocks == []
    assert result.structured_content is None


@pytest.mark.parametrize("transport", ["stdio", "sse"])
async def test_rpc_error_keeps_legacy_message(
    monkeypatch: pytest.MonkeyPatch, transport: str
) -> None:
    client = _client(transport)

    async def request(*_args: Any) -> dict[str, Any]:
        return {"error": {"code": -32602, "message": "Invalid arguments"}}

    monkeypatch.setattr(client, _request_method(transport), request)
    result = await client.call_tool("read", {})
    assert result == MCPToolResult("Invalid arguments", True)


def test_existing_construction_and_independent_default_blocks() -> None:
    first, second = MCPToolResult("first"), MCPToolResult("second", True)
    first.content_blocks.append({"type": "text", "text": "extra"})
    assert second.content_blocks == []
    assert second.structured_content is None
    assert second.is_error


def _client(transport: str) -> MCPStdioClient | MCPSSEClient:
    config = MCPServerConfig(name="test", transport=transport, command="unused")
    return MCPStdioClient(config) if transport == "stdio" else MCPSSEClient(config)


def _request_method(transport: str) -> str:
    return "_send_request" if transport == "stdio" else "_send_and_receive"
