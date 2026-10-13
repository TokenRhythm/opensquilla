from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.adapters.agent_catalog import GatewayAgentCatalogAdapter


@pytest.mark.asyncio
@pytest.mark.parametrize("include_builtin", [True, False])
async def test_agent_catalog_adapter_reads_registry(include_builtin: bool) -> None:
    registry = AsyncMock()
    registry.list_agents.return_value = [{"id": "ops", "name": "Operations"}]
    adapter = GatewayAgentCatalogAdapter(registry)

    assert await adapter.list({"includeBuiltin": include_builtin}) == {
        "agents": [{"id": "ops", "name": "Operations"}]
    }
    registry.list_agents.assert_awaited_once_with(include_builtin=include_builtin)
