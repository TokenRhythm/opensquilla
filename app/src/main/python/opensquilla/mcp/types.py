"""MCP client type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MCPServerConfig:
    name: str
    transport: str  # "stdio" | "sse"
    command: str | None = None  # for stdio
    args: list[str] = field(default_factory=list)  # for stdio
    url: str | None = None  # for sse
    message_endpoint: str | None = None  # for sse, default "/message"
    env: dict[str, str] = field(default_factory=dict)
    tool_timeout_seconds: float = 30.0


@dataclass
class MCPToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class MCPToolResult:
    content: str
    is_error: bool = False
