"""MCP tool discovery and registration into OpenSquilla ToolRegistry."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from anyascii import anyascii

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
    # Appended defaults preserve construction compatibility for integrations
    # that used the previously exported four-field lifecycle record.
    registry: ToolRegistry | None = field(default=None, repr=False)
    namespace: str = ""
    registered_tools: tuple[str, ...] = ()

    async def close(self) -> None:
        try:
            await self.client.close()
        finally:
            if self.registry is not None:
                for tool_name in self.registered_tools:
                    self.registry.unregister(tool_name)
                if self.namespace:
                    self.registry.unregister_mcp_namespace(self.namespace)


# Module-level registry to keep clients alive for tool handlers.
_active_clients: list[ActiveMCPClient] = []

_PROVIDER_TOOL_NAME_MAX_LENGTH = 64
_MCP_NAMESPACE_MAX_LENGTH = 32


def _bounded_name(value: str, *, max_length: int, separator: str) -> str:
    """Bound an identifier while preserving deterministic collision resistance."""

    if len(value) <= max_length:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{value[: max_length - len(digest) - 1]}{separator}{digest}"


def mcp_namespace(server_name: str) -> str:
    """Return a stable provider-safe namespace for one MCP server."""

    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", anyascii(server_name.strip()).lower()).strip("-_")
    normalized = re.sub(r"-+", "-", normalized) or "server"
    return _bounded_name(
        f"mcp__{normalized}",
        max_length=_MCP_NAMESPACE_MAX_LENGTH,
        separator="-",
    )


def mcp_tool_name(namespace: str, tool_name: str) -> str:
    """Return the exact provider-safe callable name for one MCP tool.

    The advertised namespace remains ``mcp__<server>``. A second ``__``
    separates it from the tool component because dots are rejected by common
    function-calling APIs. Tool components that need normalization receive a
    short source-name hash so two distinct MCP names cannot silently alias.
    """

    source_name = tool_name.strip()
    ascii_name = anyascii(source_name)
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "_", ascii_name)
    normalized = re.sub(r"_+", "_", normalized).strip("-_") or "tool"
    changed = normalized != source_name
    available = _PROVIDER_TOOL_NAME_MAX_LENGTH - len(namespace) - 2
    if available < 10:  # Defensive: mcp_namespace currently guarantees >= 30.
        raise ValueError(f"MCP namespace is too long for a callable tool name: {namespace}")
    if changed:
        digest = hashlib.sha256(tool_name.encode("utf-8")).hexdigest()[:8]
        normalized = f"{normalized}_{digest}"
    normalized = _bounded_name(normalized, max_length=available, separator="_")
    return f"{namespace}__{normalized}"


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
    namespace: str,
    tool_name: str,
    tool_def: MCPToolDef,
    registry: ToolRegistry,
    timeout_seconds: float,
) -> None:
    """Register a single MCP tool in its server-scoped namespace."""
    # Extract properties and required from input_schema
    schema = tool_def.input_schema
    raw_properties = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    properties: dict[str, Any] = dict(raw_properties) if isinstance(raw_properties, Mapping) else {}
    raw_required = schema.get("required", []) if isinstance(schema, Mapping) else []
    required = (
        [item for item in raw_required if isinstance(item, str)]
        if isinstance(raw_required, list | tuple)
        else []
    )

    spec = ToolSpec(
        name=mcp_tool_name(namespace, tool_name),
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
    registered: list[str] = []
    namespace = mcp_namespace(config.name)
    namespace_registered = False
    try:
        await client.connect()
        tools = await client.list_tools()
        registry.register_mcp_namespace(
            namespace,
            config.description or f"Tools provided by the {config.name} MCP server.",
        )
        namespace_registered = True
        for t in tools:
            registered_name = mcp_tool_name(namespace, t.name)
            if registry.get(registered_name) is not None:
                raise ValueError(f"MCP tool is already registered: {registered_name}")
            # Track before registration so a partially failing custom registry
            # implementation is still given an idempotent cleanup attempt.
            registered.append(registered_name)
            _make_tool_handler(
                client,
                namespace,
                t.name,
                t,
                registry,
                timeout_seconds=config.tool_timeout_seconds,
            )
        _active_clients.append(
            ActiveMCPClient(
                owner=owner or config.name,
                server_name=config.name,
                transport=config.transport,
                client=client,
                registry=registry,
                namespace=namespace,
                registered_tools=tuple(registered),
            )
        )
    except BaseException:
        for registered_name in registered:
            registry.unregister(registered_name)
        if namespace_registered:
            registry.unregister_mcp_namespace(namespace)
        try:
            await asyncio.shield(client.close())
        except BaseException:
            # Preserve the discovery/cancellation failure. Lifecycle cleanup is
            # best effort, and no registry entry remains callable at this point.
            pass
        raise
    return registered
