"""RPC payload builders for tool catalog and effective tool surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opensquilla.tools.policy_runtime import (
    ToolSurfaceCapabilities,
    tool_surface_capabilities_from_runtime,
)
from opensquilla.tools.registry import ToolRegistry, get_default_registry


def tool_rpc_params(params: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Normalize optional RPC params for tools methods."""

    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise ValueError("params must be an object")
    return params


def tool_surface_capabilities_for_runtime(
    *,
    tool_surface_capabilities: ToolSurfaceCapabilities | None = None,
    session_manager: object | None = None,
    task_runtime: object | None = None,
    scheduler: object | None = None,
    gateway_config: object | None = None,
    channel_manager: object | None = None,
    originating_envelope: object | None = None,
) -> ToolSurfaceCapabilities:
    """Resolve tool surface capabilities from explicit input or runtime objects."""

    if tool_surface_capabilities is not None:
        return tool_surface_capabilities
    return tool_surface_capabilities_from_runtime(
        session_manager=session_manager,
        task_runtime=task_runtime,
        scheduler=scheduler,
        gateway_config=gateway_config,
        channel_manager=channel_manager,
        originating_envelope=originating_envelope,
    )


async def tools_catalog_payload(
    params: Mapping[str, Any] | None,
    *,
    tool_registry: ToolRegistry | None = None,
    is_owner: bool = True,
    tool_surface_capabilities: ToolSurfaceCapabilities | None = None,
    session_manager: object | None = None,
    task_runtime: object | None = None,
    scheduler: object | None = None,
    gateway_config: object | None = None,
    channel_manager: object | None = None,
    originating_envelope: object | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the tools.catalog RPC payload."""

    raw = tool_rpc_params(params)
    registry = tool_registry or get_default_registry()
    tools = await registry.list_tools(
        profile=raw.get("profile"),
        session_key=raw.get("sessionKey"),
        agent_id=raw.get("agentId"),
        caller_kind=raw.get("callerKind"),
        interaction_mode=raw.get("interactionMode"),
        tool_surface_capabilities=tool_surface_capabilities_for_runtime(
            tool_surface_capabilities=tool_surface_capabilities,
            session_manager=session_manager,
            task_runtime=task_runtime,
            scheduler=scheduler,
            gateway_config=gateway_config,
            channel_manager=channel_manager,
            originating_envelope=originating_envelope,
        ),
        is_owner=is_owner,
    )
    return {"tools": tools}


async def tools_effective_payload(
    params: Mapping[str, Any] | None,
    *,
    tool_registry: ToolRegistry | None = None,
    is_owner: bool = True,
    tool_surface_capabilities: ToolSurfaceCapabilities | None = None,
    session_manager: object | None = None,
    task_runtime: object | None = None,
    scheduler: object | None = None,
    gateway_config: object | None = None,
    channel_manager: object | None = None,
    originating_envelope: object | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the tools.effective RPC payload."""

    raw = tool_rpc_params(params)
    registry = tool_registry or get_default_registry()
    tools = await registry.effective_tools(
        session_key=raw.get("sessionKey"),
        agent_id=raw.get("agentId"),
        caller_kind=raw.get("callerKind"),
        interaction_mode=raw.get("interactionMode"),
        tool_surface_capabilities=tool_surface_capabilities_for_runtime(
            tool_surface_capabilities=tool_surface_capabilities,
            session_manager=session_manager,
            task_runtime=task_runtime,
            scheduler=scheduler,
            gateway_config=gateway_config,
            channel_manager=channel_manager,
            originating_envelope=originating_envelope,
        ),
        is_owner=is_owner,
    )
    return {"tools": tools}
