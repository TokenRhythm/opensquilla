"""Transport-neutral read use cases for the Skill catalog."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class SkillIdentity:
    """A winner name plus optional exact managed-install identity."""

    name: str | None = None
    instance_id: str = ""
    install_id: str = ""

    def __post_init__(self) -> None:
        if not self.name and not self.instance_id and not self.install_id:
            raise ValueError("skill identity is required")


@dataclass(frozen=True, slots=True)
class SkillSearchQuery:
    query: str
    limit: int = 20
    source: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query, str):
            raise ValueError("skill search query is required")
        if self.limit > 100:
            object.__setattr__(self, "limit", 100)


@dataclass(frozen=True, slots=True)
class SkillSearchPage:
    results: Sequence[Mapping[str, Any]]
    diagnostics: Sequence[Mapping[str, Any]] = ()
    message: str = ""
    partial: bool | None = None
    all_sources_unavailable: bool | None = None


class SkillCatalogReadPort(Protocol):
    """One-shot catalog views supplied by the production Skill runtime."""

    async def list(self, *, include_lifecycle: bool) -> Sequence[Mapping[str, Any]]: ...

    async def detail(
        self,
        identity: SkillIdentity,
        *,
        include_lifecycle: bool,
    ) -> Mapping[str, Any]: ...

    async def search(self, query: SkillSearchQuery) -> SkillSearchPage: ...


class SkillCatalog:
    """Own read intent while the Port pins each catalog read to one generation."""

    def __init__(self, reader: SkillCatalogReadPort) -> None:
        self._reader = reader

    async def list(self, *, include_lifecycle: bool = False) -> tuple[Mapping[str, Any], ...]:
        return tuple(await self._reader.list(include_lifecycle=include_lifecycle))

    async def detail(
        self,
        identity: SkillIdentity,
        *,
        include_lifecycle: bool = False,
    ) -> Mapping[str, Any]:
        result = await self._reader.detail(
            identity,
            include_lifecycle=include_lifecycle,
        )
        resolved_name = result.get("name")
        if identity.name is not None and resolved_name != identity.name:
            raise KeyError(f"Skill identity does not match name: {identity.name}")
        return result

    async def search(self, query: SkillSearchQuery) -> SkillSearchPage:
        return await self._reader.search(query)


__all__ = [
    "SkillCatalog",
    "SkillCatalogReadPort",
    "SkillIdentity",
    "SkillSearchPage",
    "SkillSearchQuery",
]
