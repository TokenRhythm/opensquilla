"""MCP client package — connect to external MCP servers and register their tools."""

from __future__ import annotations

from opensquilla.mcp.client import MCPClient
from opensquilla.mcp.discovery import (
    ActiveMCPClient,
    active_clients_snapshot,
    close_active_clients,
    discover_and_register,
)
from opensquilla.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult

__all__ = [
    "ActiveMCPClient",
    "MCPClient",
    "MCPServerConfig",
    "MCPToolDef",
    "MCPToolResult",
    "active_clients_snapshot",
    "close_active_clients",
    "discover_and_register",
]
