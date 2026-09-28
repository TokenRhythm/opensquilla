"""Transport-neutral, read-only catalog of configured runtime agents."""

from __future__ import annotations

from collections.abc import Sequence
from typing import NotRequired, Protocol, TypedDict


class AgentProjection(TypedDict):
    id: str
    name: str
    enabled: bool
    isBuiltin: bool
    type: str
    description: NotRequired[str | None]
    model: NotRequired[str | None]
    workspace: NotRequired[str | None]
    agentDir: NotRequired[str | None]
    systemPrompt: NotRequired[str | None]
    tools: NotRequired[object]
    skills: NotRequired[object]
    subagents: NotRequired[object]


class AgentRegistryPort(Protocol):
    async def list(self, *, include_builtin: bool) -> Sequence[AgentProjection]: ...


class AgentCatalog:
    """Read the configured agents used by existing sessions and tools."""

    def __init__(self, registry: AgentRegistryPort | None) -> None:
        self._registry = registry

    async def list(self, *, include_builtin: bool = True) -> Sequence[AgentProjection]:
        if self._registry is None:
            return ()
        return await self._registry.list(include_builtin=include_builtin)


__all__ = ["AgentCatalog", "AgentProjection", "AgentRegistryPort"]
