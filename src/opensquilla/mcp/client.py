"""MCPClient abstract base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from opensquilla.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult


class MCPClient(ABC):
    """Abstract base class for MCP transport clients."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the MCP server."""

    @abstractmethod
    async def close(self) -> None:
        """Close the connection."""

    @abstractmethod
    async def list_tools(self) -> list[MCPToolDef]:
        """List available tools from the MCP server."""

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Call a tool on the MCP server."""
