"""RPC handlers for the agents domain."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from opensquilla.agents import files as agent_files
from opensquilla.agents.scope import resolve_agent_workspace_dir
from opensquilla.gateway.adapters.agent_catalog import GatewayAgentCatalogAdapter
from opensquilla.gateway.adapters.agent_catalog_contract import (
    register_agent_catalog_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.session.keys import normalize_agent_id

_d = get_dispatcher()

_WORKSPACE_AGENT_FILE_NAMES = agent_files.AGENT_FILE_NAMES


def _get_agent_registry(ctx: RpcContext):
    agent_registry = getattr(ctx, "agent_registry", None)
    if agent_registry is None:
        raise RpcUnavailableError("Agent registry not available")
    return agent_registry


def _workspace_file_root(ctx: RpcContext, agent_id: str) -> Path | None:
    config = getattr(ctx, "config", None)
    if not getattr(config, "workspace_dir", None):
        return None
    return resolve_agent_workspace_dir(agent_id, config).expanduser()


_validate_workspace_file_name = agent_files.validate_name


def _list_workspace_agent_files(root: Path) -> list[dict[str, Any]]:
    return [agent_files.file_entry(root, name) for name in _WORKSPACE_AGENT_FILE_NAMES]


def _agent_catalog(ctx: RpcContext) -> GatewayAgentCatalogAdapter:
    return GatewayAgentCatalogAdapter(getattr(ctx, "agent_registry", None))


async def _handle_agents_list(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _agent_catalog(ctx).list(params)


register_agent_catalog_contract(
    _d,
    "agents.list",
    _handle_agents_list,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


@_d.method("agents.files.list", scope="operator.read")
async def _handle_agents_files_list(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict) or "agentId" not in params:
        raise ValueError("params.agentId is required")

    agent_id = normalize_agent_id(params["agentId"])

    agent_registry = getattr(ctx, "agent_registry", None)
    if agent_registry is None:
        root = _workspace_file_root(ctx, agent_id)
        if root is None:
            raise RpcUnavailableError("Agent registry not available")
        return {"files": _list_workspace_agent_files(root)}
    files = await agent_registry.list_agent_files(agent_id)
    return {"files": files}


@_d.method("agents.files.get", scope="operator.read")
async def _handle_agents_files_get(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params required: agentId, name")
    if "agentId" not in params:
        raise ValueError("params.agentId is required")
    if "name" not in params:
        raise ValueError("params.name is required")

    agent_id = normalize_agent_id(params["agentId"])
    name = _validate_workspace_file_name(params["name"])

    agent_registry = getattr(ctx, "agent_registry", None)
    if agent_registry is None:
        root = _workspace_file_root(ctx, agent_id)
        if root is None:
            raise RpcUnavailableError("Agent registry not available")
        return await agent_files.read_file(root, name)
    content = await agent_registry.get_agent_file(agent_id, name)
    return cast(dict, content)


@_d.method("agents.files.set", scope="operator.admin")
async def _handle_agents_files_set(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict):
        raise ValueError("params required: agentId, name, content")
    if "agentId" not in params:
        raise ValueError("params.agentId is required")
    if "name" not in params:
        raise ValueError("params.name is required")
    if "content" not in params:
        raise ValueError("params.content is required")

    name = _validate_workspace_file_name(params["name"])
    expected_revision = params.get("expectedRevision")
    if "expectedRevision" in params and (
        not isinstance(expected_revision, str) or not expected_revision
    ):
        raise ValueError("params.expectedRevision must be a non-empty revision")
    agent_id = normalize_agent_id(params["agentId"])

    def invalidate() -> None:
        runner = getattr(ctx, "turn_runner", None)
        callback = getattr(runner, "invalidate_profile_snapshot", None)
        if callable(callback):
            callback(agent_id)
        if name in {"MEMORY.md", "memory.md"}:
            refresh = getattr(runner, "refresh_memory_snapshot", None)
            if callable(refresh):
                refresh(agent_id)

    try:
        agent_registry = getattr(ctx, "agent_registry", None)
        if agent_registry is None:
            root = _workspace_file_root(ctx, agent_id)
            if root is None:
                raise RpcUnavailableError("Agent registry not available")
            return await agent_files.replace_file(
                root, name, params["content"], expected_revision=expected_revision,
                on_write=invalidate,
            )
        result = await agent_registry.set_agent_file(
            agent_id, name, params["content"], expected_revision=expected_revision,
            on_write=invalidate,
        )
        return cast(dict, result)
    except agent_files.AgentFileConflictError as exc:
        raise RpcHandlerError("AGENT_FILE_CONFLICT", str(exc)) from exc



@_d.method("agent.identity.get", scope="operator.read")
async def _handle_agent_identity_get(params: dict | None, ctx: RpcContext) -> dict:
    agent_id = normalize_agent_id((params or {}).get("agentId", "main"))

    agent_registry = _get_agent_registry(ctx)
    identity = await agent_registry.get_identity(agent_id)
    return cast(dict, identity)
