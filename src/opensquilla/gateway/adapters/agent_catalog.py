"""Gateway Adapter for the read-only AgentCatalog application Module."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from opensquilla.application.agent_catalog import AgentCatalog, AgentProjection, AgentRegistryPort


class GatewayAgentRegistryPort(AgentRegistryPort):
    """Read existing runtime profiles from the durable registry."""

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def list(self, *, include_builtin: bool) -> Sequence[AgentProjection]:
        return cast(
            Sequence[AgentProjection],
            await self._registry.list_agents(include_builtin=include_builtin),
        )


class GatewayAgentCatalogAdapter:
    """Project the configured agent list to the v4 wire format."""

    def __init__(self, registry: Any | None) -> None:
        port = GatewayAgentRegistryPort(registry) if registry is not None else None
        self._application = AgentCatalog(port)

    async def list(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        agents = await self._application.list(
            include_builtin=bool(raw.get("includeBuiltin", True))
        )
        return {"agents": [dict(agent) for agent in agents]}


__all__ = ["GatewayAgentCatalogAdapter", "GatewayAgentRegistryPort"]
