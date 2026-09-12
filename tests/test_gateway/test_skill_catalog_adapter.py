from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.skill_catalog import (
    SkillCatalogReadPort,
    SkillIdentity,
    SkillSearchPage,
    SkillSearchQuery,
)
from opensquilla.gateway.adapters.skill_catalog import (
    GatewaySkillCatalogAdapter,
)


@pytest.mark.asyncio
async def test_skill_catalog_adapter_projects_aliases_to_typed_read_queries() -> None:
    reader = AsyncMock(spec=SkillCatalogReadPort)
    reader.list.return_value = [{"name": "demo"}]
    reader.detail.return_value = {"name": "demo", "content": "# Demo"}
    reader.search.return_value = SkillSearchPage(
        results=[{"name": "remote"}],
        diagnostics=[{"code": "source.timeout"}],
        partial=True,
        all_sources_unavailable=False,
    )
    adapter = GatewaySkillCatalogAdapter(reader)

    assert await adapter.list({"includeLifecycle": True}) == {
        "skills": [{"name": "demo"}]
    }
    assert await adapter.detail(
        {
            "name": "demo",
            "instance_id": "managed:1",
            "installId": "install-1",
            "includeLifecycle": True,
        }
    ) == {"name": "demo", "content": "# Demo"}
    assert await adapter.search({"query": "demo", "limit": "500"}) == {
        "results": [{"name": "remote"}],
        "diagnostics": [{"code": "source.timeout"}],
        "partial": True,
        "allSourcesUnavailable": False,
    }

    reader.list.assert_awaited_once_with(include_lifecycle=True)
    reader.detail.assert_awaited_once_with(
        SkillIdentity(name="demo", instance_id="managed:1", install_id="install-1"),
        include_lifecycle=True,
    )
    reader.search.assert_awaited_once_with(
        SkillSearchQuery(query="demo", limit=100)
    )


@pytest.mark.asyncio
async def test_skill_catalog_adapter_rejects_conflicting_identity_aliases() -> None:
    reader = AsyncMock()
    adapter = GatewaySkillCatalogAdapter(reader)

    with pytest.raises(ValueError, match="must match"):
        await adapter.detail(
            {"name": "demo", "installId": "one", "install_id": "two"}
        )

    reader.detail.assert_not_awaited()
