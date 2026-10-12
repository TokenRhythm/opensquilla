"""MCP tool discovery and registration into OpenSquilla ToolRegistry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from opensquilla.mcp.client import MCPClient
from opensquilla.mcp.types import MCPServerConfig, MCPToolDef
from opensquilla.tools.registry import ToolRegistry
from opensquilla.tools.types import SafeToolError, ToolSpec


@dataclass(frozen=True)
class ActiveMCPClient:
    """Tracked MCP client with the owner that controls its lifecycle."""

    owner: str
    server_name: str
    transport: str
    client: MCPClient

    async def close(self) -> None:
        await self.client.close()


# Module-level registry to keep clients alive for tool handlers.
_active_clients: list[ActiveMCPClient] = []


def active_clients_snapshot() -> tuple[ActiveMCPClient, ...]:
    """Return active MCP clients without exposing mutable runtime state."""
    return tuple(_active_clients)


async def close_active_clients(owner: str | None = None) -> int:
    """Close active MCP clients, optionally scoped to one owner/server name."""
    remaining: list[ActiveMCPClient] = []
    closing: list[ActiveMCPClient] = []
    for entry in _active_clients:
        if owner is None or entry.owner == owner or entry.server_name == owner:
            closing.append(entry)
        else:
            remaining.append(entry)
    _active_clients[:] = remaining

    closed = 0
    for entry in closing:
        try:
            await entry.close()
            closed += 1
        except Exception:
            pass
    return closed


def create_client(config: MCPServerConfig) -> MCPClient:
    """Factory: create the appropriate MCPClient for the given transport."""
    if config.transport == "stdio":
        from opensquilla.mcp.stdio import MCPStdioClient

        return MCPStdioClient(config)
    elif config.transport == "sse":
        from opensquilla.mcp.sse import MCPSSEClient

        return MCPSSEClient(config)
    else:
        raise ValueError(f"Unknown MCP transport: {config.transport!r}")


def _make_tool_handler(
    client: MCPClient,
    tool_name: str,
    tool_def: MCPToolDef,
    registry: ToolRegistry,
    timeout_seconds: float,
) -> None:
    """Register a single MCP tool into the registry with an mcp_ prefix."""
    # Extract properties and required from input_schema
    schema = tool_def.input_schema
    properties: dict[str, Any] = schema.get("properties", {})
    required: list[str] = schema.get("required", [])

    spec = ToolSpec(
        name=f"mcp_{tool_name}",
        description=tool_def.description,
        parameters=properties,
        required=required,
    )

    async def handler(**kwargs: Any) -> str:
        try:
            result = await asyncio.wait_for(
                client.call_tool(tool_name, kwargs),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            raise SafeToolError(
                f"MCP tool '{tool_name}' timed out after {timeout_seconds}s"
            ) from None
        # An MCP error (result-level isError, or a JSON-RPC error the client
        # flags) must reach the tool boundary AS an error, not be laundered into
        # a successful result. Raising SafeToolError makes dispatch record
        # is_error=True with an error execution status while preserving the
        # server's message for the model.
        if result.is_error:
            raise SafeToolError(result.content or f"MCP tool '{tool_name}' failed")
        return result.content

    registry.register(spec, handler)


async def discover_and_register(
    config: MCPServerConfig,
    registry: ToolRegistry,
    *,
    owner: str | None = None,
) -> list[str]:
    """Connect to MCP server, list tools, register each as a OpenSquilla tool.

    Returns list of registered tool names.
    The client is kept alive in module-level _active_clients so tool handlers can use it.
    """
    client = create_client(config)
    entry: ActiveMCPClient | None = None

    registered: list[str] = []
    try:
        await client.connect()
        entry = ActiveMCPClient(
            owner=owner or config.name,
            server_name=config.name,
            transport=config.transport,
            client=client,
        )
        _active_clients.append(entry)
        tools = await client.list_tools()
        for t in tools:
            _make_tool_handler(
                client,
                t.name,
                t,
                registry,
                timeout_seconds=config.tool_timeout_seconds,
            )
            registered.append(f"mcp_{t.name}")
    except Exception:
        if entry is not None:
            try:
                _active_clients.remove(entry)
            except ValueError:
                pass
        await client.close()
        raise
    return registered
