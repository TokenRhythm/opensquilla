"""Canvas and node helper functions for deployments with a node runtime."""

from __future__ import annotations

from opensquilla.tools.registry import tool
from opensquilla.tools.types import ToolError

_CANVAS_ACTIONS = ("present", "hide", "eval", "snapshot")
_NODES_ACTIONS = ("list", "describe", "invoke")


@tool(
    name="canvas",
    description=(
        "Unavailable node runtime canvas stub. Hidden by default; calls require a "
        "configured node runtime."
    ),
    params={
        "action": {
            "type": "string",
            "description": "Action: present, hide, eval, snapshot",
        },
        "node_id": {
            "type": "string",
            "description": "Target node identifier",
        },
        "content": {
            "type": "string",
            "description": "Optional canvas content",
        },
    },
    required=["action", "node_id"],
    exposed_by_default=False,
)
async def canvas(
    action: str,
    node_id: str,
    content: str | None = None,
) -> str:
    if action not in _CANVAS_ACTIONS:
        raise ToolError(f"Invalid action: {action}. Must be present|hide|eval|snapshot")
    raise ToolError("Canvas requires a configured node runtime.")


@tool(
    name="nodes",
    description=(
        "Unavailable node runtime management stub. Hidden by default; calls require a "
        "configured node runtime."
    ),
    params={
        "action": {
            "type": "string",
            "description": "Action: list, describe, invoke",
        },
        "node_id": {
            "type": "string",
            "description": "Target node identifier for describe or invoke",
        },
        "tool_name": {
            "type": "string",
            "description": "Target node tool name for invoke",
        },
        "arguments": {
            "type": "object",
            "description": "Optional node tool arguments",
        },
    },
    required=["action"],
    exposed_by_default=False,
)
async def nodes(
    action: str,
    node_id: str | None = None,
    tool_name: str | None = None,
    arguments: dict | None = None,
) -> str:
    if action not in _NODES_ACTIONS:
        raise ToolError(f"Invalid action: {action}. Must be list|describe|invoke")

    if action in ("describe", "invoke") and not node_id:
        raise ToolError(f"'node_id' required for {action}")

    if action == "invoke" and not tool_name:
        raise ToolError("'tool_name' required for invoke")

    raise ToolError("Nodes require a configured node runtime.")
