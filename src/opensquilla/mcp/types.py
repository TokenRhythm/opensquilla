"""MCP client type definitions."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MCPServerConfig:
    name: str
    transport: str  # "stdio" | "sse"
    command: str | None = None  # for stdio
    args: list[str] = field(default_factory=list)  # for stdio
    url: str | None = None  # for sse
    message_endpoint: str | None = None  # legacy SSE override; non-None is rejected
    env: dict[str, str] = field(default_factory=dict)
    tool_timeout_seconds: float = 30.0
    description: str = ""


@dataclass
class MCPToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class MCPToolResult:
    content: str
    is_error: bool = False
    # Keep the historical text fields first for integrations constructing this
    # record positionally. Binary blocks never become the text fallback.
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    structured_content: dict[str, Any] | None = None

    @classmethod
    def from_response(cls, response: dict[str, Any]) -> MCPToolResult:
        """Retain rich MCP results while preserving the text-only client API."""
        if "error" in response:
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else None
            return cls(
                content=message if isinstance(message, str) else "Unknown MCP error",
                is_error=True,
            )
        result = response.get("result")
        if not isinstance(result, dict):
            return cls(content="Invalid MCP tool response", is_error=True)
        raw_blocks = result.get("content", [])
        blocks = (
            [dict(block) for block in raw_blocks if isinstance(block, dict)]
            if isinstance(raw_blocks, list)
            else []
        )
        text = "\n".join(
            block["text"]
            for block in blocks
            if block.get("type") == "text" and isinstance(block.get("text"), str)
        )
        structured = result.get("structuredContent")
        return cls(
            content=text,
            is_error=bool(result.get("isError", False)),
            content_blocks=blocks,
            structured_content=dict(structured) if isinstance(structured, dict) else None,
        )


@dataclass(frozen=True)
class MCPCallContext:
    """Per-call identity supplied by trusted tool dispatch, never model arguments."""

    tool_use_id: str


current_mcp_call_context: ContextVar[MCPCallContext | None] = ContextVar(
    "current_mcp_call_context", default=None
)
