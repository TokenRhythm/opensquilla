"""Shared parsing of MCP tool responses without discarding non-text content."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opensquilla.mcp.types import MCPToolResult


def parse_tool_response(response: Mapping[str, Any]) -> MCPToolResult:
    if "error" in response:
        error = response["error"]
        message = error.get("message") if isinstance(error, dict) else None
        return MCPToolResult(
            content=message if isinstance(message, str) else "Malformed MCP RPC error",
            is_error=True,
        )

    result = response.get("result")
    if not isinstance(result, dict):
        return _malformed("result must be an object")
    blocks = result.get("content", [])
    if not isinstance(blocks, list):
        return _malformed("content must be a list")
    text: list[str] = []
    for block in blocks:
        if (
            not isinstance(block, dict)
            or not isinstance(block.get("type"), str)
            or not block["type"]
        ):
            return _malformed("content blocks must be objects with a type")
        if block["type"] == "text":
            if not isinstance(block.get("text"), str):
                return _malformed("text content must be a string")
            text.append(block["text"])
    structured = result.get("structuredContent")
    if structured is not None and not isinstance(structured, dict):
        return _malformed("structuredContent must be an object")
    is_error = result.get("isError", False)
    if not isinstance(is_error, bool):
        return _malformed("isError must be a boolean")
    return MCPToolResult(
        content="\n".join(text),
        is_error=is_error,
        content_blocks=blocks,
        structured_content=structured,
    )


def _malformed(reason: str) -> MCPToolResult:
    return MCPToolResult(content=f"Malformed MCP tool result: {reason}", is_error=True)
