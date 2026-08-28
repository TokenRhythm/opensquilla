"""Inbound MCP server bridge for OpenSquilla.

This package exposes OpenSquilla sessions to external MCP clients. It is
intentionally separate from :mod:`opensquilla.mcp`, which is the outbound MCP
client integration used to import tools from external MCP servers.
"""

from opensquilla.mcp_server.bridge import OpenSquillaMCPBridge
from opensquilla.mcp_server.server import create_mcp_server

__all__ = ["OpenSquillaMCPBridge", "create_mcp_server"]
