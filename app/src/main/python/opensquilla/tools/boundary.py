"""Tool boundary re-export for callers that import through opensquilla.tools."""

from __future__ import annotations

from opensquilla.tool_boundary import AgentToolHandler, ToolCall, ToolResult

__all__ = ["AgentToolHandler", "ToolCall", "ToolResult"]
