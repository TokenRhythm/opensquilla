"""Gateway Adapter for the AgentCatalog application Module."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

from opensquilla.application.agent_catalog import (
    UNSET,
    AgentBuiltinImmutableError,
    AgentCatalog,
    AgentExistsError,
    AgentNotFoundError,
    AgentRegistryPort,
    AgentRegistryUnavailableError,
    CreateAgent,
    UpdateAgent,
)
from opensquilla.gateway.rpc import RpcHandlerError, RpcUnavailableError


class GatewayAgentRegistryPort(AgentRegistryPort):
    """Terminate the application Port at the existing durable registry."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def list(self, *, include_builtin: bool) -> Sequence[Mapping[str, Any]]:
        return cast(
            Sequence[Mapping[str, Any]],
            await self._registry.list_agents(include_builtin=include_builtin),
        )

    async def create(self, command: CreateAgent) -> Mapping[str, Any]:
        assert command.agent_id is not None
        try:
            return cast(
                Mapping[str, Any],
                await self._registry.create_agent(
                    agent_id=command.agent_id,
                    name=command.name,
                    description=command.description,
                    model=command.model,
                    workspace=command.workspace,
                    agent_dir=command.agent_dir,
                    enabled=command.enabled,
                    system_prompt=command.system_prompt,
                    tools=command.tools,
                ),
            )
        except ValueError as exc:
            message = str(exc)
            if "already exists" in message:
                raise AgentExistsError(command.agent_id) from exc
            if "builtin" in message.lower() or command.agent_id == "main":
                raise AgentBuiltinImmutableError(command.agent_id) from exc
            raise

    async def update(self, command: UpdateAgent) -> Mapping[str, Any]:
        try:
            return cast(
                Mapping[str, Any],
                await self._registry.update_agent(
                    command.agent_id,
                    **command.changed_fields(),
                ),
            )
        except KeyError as exc:
            raise AgentNotFoundError(command.agent_id) from exc
        except ValueError as exc:
            if "builtin" in str(exc).lower() or command.agent_id == "main":
                raise AgentBuiltinImmutableError(command.agent_id) from exc
            raise

    async def remove(self, agent_id: str) -> None:
        try:
            await self._registry.delete_agent(agent_id)
        except KeyError as exc:
            raise AgentNotFoundError(agent_id) from exc
        except ValueError as exc:
            if "builtin" in str(exc).lower() or agent_id == "main":
                raise AgentBuiltinImmutableError(agent_id) from exc
            raise


class GatewayAgentCatalogAdapter:
    """Project v4 aliases and failures to AgentCatalog semantics."""

    _UPDATE_FIELDS = (
        ("name", ("name",)),
        ("description", ("description",)),
        ("model", ("model",)),
        ("workspace", ("workspace",)),
        ("agent_dir", ("agentDir", "agent_dir")),
        ("enabled", ("enabled",)),
        ("system_prompt", ("systemPrompt", "system_prompt")),
        ("tools", ("tools",)),
    )

    def __init__(self, registry: Any | None) -> None:
        port = GatewayAgentRegistryPort(registry) if registry is not None else None
        self._application = AgentCatalog(port)

    async def list(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        agents = await self._application.list(
            include_builtin=bool(raw.get("includeBuiltin", True))
        )
        return {"agents": [dict(agent) for agent in agents]}

    async def create(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("params.id or params.name is required")
        raw = params
        raw_id = raw.get("id") or raw.get("agentId")
        if not raw_id and not raw.get("name"):
            raise ValueError("params.id or params.name is required")
        command = CreateAgent(
            agent_id=raw_id,
            name=self._optional(raw, "name"),
            description=self._optional(raw, "description"),
            model=self._optional(raw, "model"),
            workspace=self._optional(raw, "workspace"),
            agent_dir=raw.get("agentDir") or raw.get("agent_dir"),
            enabled=raw.get("enabled", True),
            system_prompt=self._optional(raw, "systemPrompt", "system_prompt"),
            tools=raw.get("tools"),
        )
        result = await self._map(lambda: self._application.create(command))
        return dict(result)

    async def update(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(params, dict) or "id" not in params:
            raise ValueError("params.id is required")
        raw = params
        command_fields: dict[str, Any] = {"agent_id": raw.get("id")}
        for domain_name, aliases in self._UPDATE_FIELDS:
            command_fields[domain_name] = self._present(raw, *aliases)
        command = UpdateAgent(**command_fields)
        result = await self._map(lambda: self._application.update(command))
        return dict(result)

    async def remove(self, params: dict[str, Any] | None) -> None:
        if not isinstance(params, dict) or "id" not in params:
            raise ValueError("params.id is required")
        raw = params
        await self._map(lambda: self._application.remove(cast(str, raw.get("id"))))
        return None

    @staticmethod
    def _optional(raw: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in raw:
                return raw[name]
        return None

    @staticmethod
    def _present(raw: Mapping[str, Any], *names: str) -> Any:
        for name in names:
            if name in raw:
                return raw[name]
        return UNSET

    @staticmethod
    async def _map[ResultT](operation: Callable[[], Awaitable[ResultT]]) -> ResultT:
        try:
            return await operation()
        except AgentRegistryUnavailableError as exc:
            raise RpcUnavailableError(str(exc)) from exc
        except AgentExistsError as exc:
            raise RpcHandlerError(
                "agent.exists", str(exc), details={"agentId": exc.agent_id}
            ) from exc
        except AgentBuiltinImmutableError as exc:
            raise RpcHandlerError(
                "agent.builtin_immutable",
                str(exc),
                details={"agentId": exc.agent_id},
            ) from exc
        except AgentNotFoundError as exc:
            raise RpcHandlerError(
                "agent.not_found", str(exc), details={"agentId": exc.agent_id}
            ) from exc


__all__ = ["GatewayAgentCatalogAdapter", "GatewayAgentRegistryPort"]
