from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.agent_catalog import AgentCatalog


@pytest.mark.asyncio
async def test_agent_catalog_reads_configured_profiles() -> None:
    registry = AsyncMock()
    registry.list.return_value = [{"id": "ops", "name": "Operations"}]
    catalog = AgentCatalog(registry)

    assert await catalog.list(include_builtin=False) == [{"id": "ops", "name": "Operations"}]
    registry.list.assert_awaited_once_with(include_builtin=False)


@pytest.mark.asyncio
async def test_agent_catalog_lists_empty_when_registry_is_unavailable() -> None:
    assert await AgentCatalog(None).list() == ()
