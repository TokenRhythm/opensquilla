"""Gateway Adapter for SkillCatalog read use cases."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, cast

from opensquilla.application.skill_catalog import (
    SkillCatalog,
    SkillCatalogReadPort,
    SkillIdentity,
    SkillSearchPage,
    SkillSearchQuery,
)
from opensquilla.gateway.rpc import RpcContext

type ReadHandler = Callable[[dict[str, Any] | None, RpcContext], Awaitable[dict[str, Any]]]
type ReadGuard = Callable[[], AbstractAsyncContextManager[None]]


class GatewaySkillCatalogReadPort(SkillCatalogReadPort):
    """Terminate catalog reads at the existing pinned Skill runtime."""

    def __init__(
        self,
        context: RpcContext,
        *,
        list_reader: ReadHandler,
        detail_reader: ReadHandler,
        search_reader: ReadHandler,
        committed_read: ReadGuard,
    ) -> None:
        self._context = context
        self._list_reader = list_reader
        self._detail_reader = detail_reader
        self._search_reader = search_reader
        self._committed_read = committed_read

    async def list(self, *, include_lifecycle: bool) -> Sequence[Mapping[str, Any]]:
        params = {"includeLifecycle": True} if include_lifecycle else None
        if include_lifecycle:
            async with self._committed_read():
                payload = await self._list_reader(params, self._context)
        else:
            payload = await self._list_reader(params, self._context)
        return cast(Sequence[Mapping[str, Any]], payload.get("skills", ()))

    async def detail(
        self,
        identity: SkillIdentity,
        *,
        include_lifecycle: bool,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            **({"name": identity.name} if identity.name is not None else {}),
            **({"instanceId": identity.instance_id} if identity.instance_id else {}),
            **({"installId": identity.install_id} if identity.install_id else {}),
            **({"includeLifecycle": True} if include_lifecycle else {}),
        }
        lifecycle_read = include_lifecycle or bool(identity.install_id)
        if lifecycle_read:
            async with self._committed_read():
                return await self._detail_reader(params, self._context)
        return await self._detail_reader(params, self._context)

    async def search(self, query: SkillSearchQuery) -> SkillSearchPage:
        payload = await self._search_reader(
            {
                "query": query.query,
                "limit": query.limit,
                **({"source": query.source} if query.source is not None else {}),
            },
            self._context,
        )
        return SkillSearchPage(
            results=cast(Sequence[Mapping[str, Any]], payload.get("results", ())),
            diagnostics=cast(
                Sequence[Mapping[str, Any]], payload.get("diagnostics", ())
            ),
            message=str(payload.get("message", "")),
            partial=cast(bool | None, payload.get("partial")),
            all_sources_unavailable=cast(
                bool | None, payload.get("allSourcesUnavailable")
            ),
        )


class GatewaySkillCatalogAdapter:
    """Translate v4 wire aliases to the narrow SkillCatalog Interface."""

    def __init__(self, reader: SkillCatalogReadPort) -> None:
        self._application = SkillCatalog(reader)

    async def list(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        skills = await self._application.list(
            include_lifecycle=raw.get("includeLifecycle") is True
        )
        return {"skills": [dict(skill) for skill in skills]}

    async def detail(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise ValueError("params.name is required")
        instance_id = self._identity(params, "instanceId", "instance_id")
        install_id = self._identity(params, "installId", "install_id")
        name = params.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("params.name must be a string")
        try:
            identity = SkillIdentity(
                name=name,
                instance_id=instance_id,
                install_id=install_id,
            )
        except ValueError as exc:
            raise ValueError("params.name is required") from exc
        result = await self._application.detail(
            identity,
            include_lifecycle=params.get("includeLifecycle") is True,
        )
        return dict(result)

    async def search(self, params: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(params, dict) or "query" not in params:
            raise ValueError("params.query is required")
        query = params["query"]
        if not isinstance(query, str):
            raise ValueError("params.query must be a string")
        try:
            limit = int(params.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        source = params.get("source")
        source_id = source if isinstance(source, str) else None
        page = await self._application.search(
            SkillSearchQuery(query=query, limit=limit, source=source_id)
        )
        payload: dict[str, Any] = {
            "results": [dict(item) for item in page.results],
        }
        if page.message:
            payload["message"] = page.message
        if page.diagnostics:
            payload["diagnostics"] = [dict(item) for item in page.diagnostics]
        if page.partial is not None:
            payload["partial"] = page.partial
        if page.all_sources_unavailable is not None:
            payload["allSourcesUnavailable"] = page.all_sources_unavailable
        return payload

    @staticmethod
    def _identity(params: Mapping[str, Any], camel: str, snake: str) -> str:
        values = [params[key] for key in (camel, snake) if key in params]
        if not values:
            return ""
        if any(not isinstance(value, str) for value in values):
            raise ValueError(f"params.{camel} must be a string")
        normalized = [cast(str, value).strip() for value in values]
        if len(set(normalized)) > 1:
            raise ValueError(f"params.{camel} and params.{snake} must match")
        return normalized[0]


__all__ = ["GatewaySkillCatalogAdapter", "GatewaySkillCatalogReadPort"]
