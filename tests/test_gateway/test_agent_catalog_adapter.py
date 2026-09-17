from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.adapters.agent_catalog import GatewayAgentCatalogAdapter
from opensquilla.gateway.rpc import RpcHandlerError, RpcUnavailableError


@pytest.mark.asyncio
async def test_agent_catalog_adapter_projects_aliases_to_registry() -> None:
    registry = AsyncMock()
    registry.list_agents.return_value = [{"id": "main", "name": "Main Agent"}]
    registry.create_agent.return_value = {"id": "ops", "name": "Operations"}
    registry.update_agent.return_value = {"id": "ops", "name": "Ops"}
    adapter = GatewayAgentCatalogAdapter(registry)

    assert await adapter.list({"includeBuiltin": False}) == {
        "agents": [{"id": "main", "name": "Main Agent"}]
    }
    await adapter.create(
        {"agentId": "ops", "name": "Operations", "agent_dir": ".agents/ops"}
    )
    await adapter.update(
        {"id": "ops", "name": "Ops", "systemPrompt": "Be concise"}
    )
    assert await adapter.remove({"id": "ops"}) is None

    registry.list_agents.assert_awaited_once_with(include_builtin=False)
    registry.create_agent.assert_awaited_once_with(
        agent_id="ops",
        name="Operations",
        description=None,
        model=None,
        workspace=None,
        agent_dir=".agents/ops",
        enabled=True,
        system_prompt=None,
        tools=None,
    )
    registry.update_agent.assert_awaited_once_with(
        "ops", name="Ops", system_prompt="Be concise"
    )
    registry.delete_agent.assert_awaited_once_with("ops")


@pytest.mark.asyncio
async def test_agent_catalog_adapter_maps_stable_domain_failures() -> None:
    registry = AsyncMock()
    registry.create_agent.side_effect = ValueError('Agent "ops" already exists')
    registry.update_agent.side_effect = KeyError("ops")
    adapter = GatewayAgentCatalogAdapter(registry)

    with pytest.raises(RpcHandlerError) as duplicate:
        await adapter.create({"id": "ops"})
    assert duplicate.value.code == "agent.exists"
    assert duplicate.value.details == {"agentId": "ops"}

    with pytest.raises(RpcHandlerError) as missing:
        await adapter.update({"id": "ops", "enabled": False})
    assert missing.value.code == "agent.not_found"

    with pytest.raises(RpcUnavailableError):
        await GatewayAgentCatalogAdapter(None).remove({"id": "ops"})
