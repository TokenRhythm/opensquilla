"""Progressive disclosure entry point for the authorized tool catalog."""

from __future__ import annotations

import json

from opensquilla.tools.registry import tool
from opensquilla.tools.search import BUILTIN_TOOL_NAMESPACE, MCP_NAMESPACE_PREFIX
from opensquilla.tools.types import PlanAccess, SafeToolError, current_tool_context


@tool(
    name="tool_search",
    description=(
        "Search tools authorized for this turn. Searches built-in tools by default. "
        "To search one connected MCP server, first select its explicit mcp__<server> "
        "namespace; MCP tools are never searched globally. Matching tools become "
        "callable on the next model step. Use each returned 'name' exactly when calling it."
    ),
    params={
        "query": {
            "type": "string",
            "description": "Capability or task to find, in natural language or by tool name.",
        },
        "namespace": {
            "type": "string",
            "description": (
                "Search scope. Use 'builtin' for ordinary tools or an advertised "
                "mcp__<server> namespace for MCP tools."
            ),
            "default": BUILTIN_TOOL_NAMESPACE,
        },
        "limit": {
            "type": "integer",
            "description": "Maximum matches to disclose (1-20).",
            "minimum": 1,
            "maximum": 20,
            "default": 5,
        },
    },
    required=["query"],
    plan_access=PlanAccess.READ_ONLY,
)
async def tool_search(
    query: str,
    namespace: str = BUILTIN_TOOL_NAMESPACE,
    limit: int = 5,
) -> str:
    ctx = current_tool_context.get()
    index = getattr(ctx, "tool_search_index", None) if ctx is not None else None
    if index is None:
        raise SafeToolError("The authorized tool index is unavailable for this turn")

    scope = str(namespace or BUILTIN_TOOL_NAMESPACE).strip()
    if scope != BUILTIN_TOOL_NAMESPACE and not scope.startswith(MCP_NAMESPACE_PREFIX):
        raise SafeToolError("namespace must be 'builtin' or an advertised mcp__<server> namespace")
    mcp_namespaces = getattr(ctx, "tool_search_namespaces", {}) or {}
    if scope.startswith(MCP_NAMESPACE_PREFIX) and scope not in mcp_namespaces:
        raise SafeToolError(f"Unknown or disconnected MCP namespace: {scope}")

    hits = index.search(str(query), namespace=scope, limit=limit)
    disclosed = getattr(ctx, "disclosed_tool_names", None)
    if disclosed is not None:
        disclosed.update(hit.name for hit in hits)
    return json.dumps(
        {
            "query": query,
            "namespace": scope,
            "matches": [hit.to_payload() for hit in hits],
            "connected_mcp_namespaces": mcp_namespaces,
        },
        ensure_ascii=False,
    )
