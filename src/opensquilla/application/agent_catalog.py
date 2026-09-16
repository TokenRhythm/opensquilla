"""Transport-neutral AgentCatalog use cases."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields, replace
from typing import Any, NotRequired, Protocol, TypedDict

from opensquilla.agent_ids import normalize_agent_id


class AgentCatalogError(Exception):
    """Base class for stable AgentCatalog domain failures."""


class AgentRegistryUnavailableError(AgentCatalogError):
    """The durable agent registry is not available."""


class AgentExistsError(AgentCatalogError):
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent '{agent_id}' already exists")


class AgentBuiltinImmutableError(AgentCatalogError):
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Cannot modify builtin agent: {agent_id}")


class AgentNotFoundError(AgentCatalogError):
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent '{agent_id}' does not exist")


class _Unset:
    __slots__ = ()


UNSET = _Unset()
type PatchValue = Any | _Unset
type AgentTools = Mapping[str, Any] | Sequence[str] | str | None


@dataclass(frozen=True, slots=True)
class CreateAgent:
    agent_id: str | None = None
    name: str | None = None
    description: str | None = None
    model: str | None = None
    workspace: str | None = None
    agent_dir: str | None = None
    enabled: bool = True
    system_prompt: str | None = None
    tools: AgentTools = None


@dataclass(frozen=True, slots=True)
class UpdateAgent:
    agent_id: str
    name: PatchValue = UNSET
    description: PatchValue = UNSET
    model: PatchValue = UNSET
    workspace: PatchValue = UNSET
    agent_dir: PatchValue = UNSET
    enabled: PatchValue = UNSET
    system_prompt: PatchValue = UNSET
    tools: PatchValue = UNSET

    def changed_fields(self) -> dict[str, Any]:
        return {
            field.name: value
            for field in fields(self)
            if field.name != "agent_id" and (value := getattr(self, field.name)) is not UNSET
        }


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

    async def create(self, command: CreateAgent) -> AgentProjection: ...

    async def update(self, command: UpdateAgent) -> AgentProjection: ...

    async def remove(self, agent_id: str) -> None: ...


class AgentCatalog:
    """Own agent identity normalization and mutation invariants."""

    def __init__(self, registry: AgentRegistryPort | None) -> None:
        self._registry = registry

    async def list(self, *, include_builtin: bool = True) -> Sequence[AgentProjection]:
        if self._registry is None:
            return ()
        return await self._registry.list(include_builtin=include_builtin)

    async def create(self, command: CreateAgent) -> AgentProjection:
        registry = self._required_registry()
        raw_id = command.agent_id
        if raw_id is None and command.name:
            raw_id = self._slugify(command.name)
        agent_id = self._agent_id(raw_id)
        if agent_id == "main":
            raise AgentBuiltinImmutableError(agent_id)
        name = command.name.strip() if isinstance(command.name, str) else command.name
        return await registry.create(
            replace(command, agent_id=agent_id, name=name or agent_id)
        )

    async def update(self, command: UpdateAgent) -> AgentProjection:
        registry = self._required_registry()
        agent_id = self._agent_id(command.agent_id)
        if agent_id == "main":
            raise AgentBuiltinImmutableError(agent_id)
        if not command.changed_fields():
            raise ValueError("No fields to update")
        return await registry.update(replace(command, agent_id=agent_id))

    async def remove(self, agent_id: str) -> None:
        registry = self._required_registry()
        normalized = self._agent_id(agent_id)
        if normalized == "main":
            raise AgentBuiltinImmutableError(normalized)
        await registry.remove(normalized)

    def _required_registry(self) -> AgentRegistryPort:
        if self._registry is None:
            raise AgentRegistryUnavailableError("Agent registry not available")
        return self._registry

    @staticmethod
    def _agent_id(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("agent id is required")
        return normalize_agent_id(value)

    @staticmethod
    def _slugify(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        return slug or "agent"


__all__ = [
    "AgentBuiltinImmutableError",
    "AgentCatalog",
    "AgentExistsError",
    "AgentNotFoundError",
    "AgentProjection",
    "AgentRegistryPort",
    "AgentRegistryUnavailableError",
    "AgentTools",
    "CreateAgent",
    "UNSET",
    "UpdateAgent",
]
