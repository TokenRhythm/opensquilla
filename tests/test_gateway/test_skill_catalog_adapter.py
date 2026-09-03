from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.adapters.skill_catalog import (
    GatewaySkillCatalogAdapter,
    GatewaySkillCatalogReadPort,
)
from opensquilla.gateway.rpc import RpcContext


@pytest.mark.asyncio
async def test_skill_catalog_adapter_pins_lifecycle_reads_and_projects_aliases() -> None:
    list_reader = AsyncMock(return_value={"skills": [{"name": "demo"}]})
    detail_reader = AsyncMock(return_value={"name": "demo", "content": "# Demo"})
    search_reader = AsyncMock(
        return_value={
            "results": [{"name": "remote"}],
            "diagnostics": [{"code": "source.timeout"}],
            "partial": True,
            "allSourcesUnavailable": False,
        }
    )
    guarded = 0

    @asynccontextmanager
    async def committed_read():
        nonlocal guarded
        guarded += 1
        yield

    context = RpcContext(conn_id="test")
    adapter = GatewaySkillCatalogAdapter(
        GatewaySkillCatalogReadPort(
            context,
            list_reader=list_reader,
            detail_reader=detail_reader,
            search_reader=search_reader,
            committed_read=committed_read,
        )
    )

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

    assert guarded == 2
    assert detail_reader.await_args.args[0] == {
        "name": "demo",
        "instanceId": "managed:1",
        "installId": "install-1",
        "includeLifecycle": True,
    }
    assert search_reader.await_args.args[0]["limit"] == 100


@pytest.mark.asyncio
async def test_skill_catalog_adapter_rejects_conflicting_identity_aliases() -> None:
    reader = AsyncMock()
    adapter = GatewaySkillCatalogAdapter(reader)

    with pytest.raises(ValueError, match="must match"):
        await adapter.detail(
            {"name": "demo", "installId": "one", "install_id": "two"}
        )

    reader.detail.assert_not_awaited()
