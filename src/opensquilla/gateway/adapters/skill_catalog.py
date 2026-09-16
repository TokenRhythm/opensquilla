"""Gateway Adapter for SkillCatalog read use cases."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from opensquilla.application.skill_catalog import (
    SkillCatalog,
    SkillCatalogReadPort,
    SkillIdentity,
    SkillSearchQuery,
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


__all__ = ["GatewaySkillCatalogAdapter"]
