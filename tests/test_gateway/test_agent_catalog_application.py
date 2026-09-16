from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.agent_catalog import (
    AgentBuiltinImmutableError,
    AgentCatalog,
    CreateAgent,
    UpdateAgent,
)


@pytest.mark.asyncio
async def test_agent_catalog_normalizes_create_and_explicit_patch() -> None:
    registry = AsyncMock()
    registry.create.return_value = {"id": "research-agent", "name": "Research Agent"}
    registry.update.return_value = {"id": "research-agent", "name": "Research"}
    catalog = AgentCatalog(registry)

    await catalog.create(CreateAgent(name=" Research Agent ", tools=("web",)))
    await catalog.update(UpdateAgent(agent_id="Research Agent", name="Research"))

    created = registry.create.await_args.args[0]
    assert created.agent_id == "research-agent"
    assert created.name == "Research Agent"
    assert created.tools == ("web",)
    updated = registry.update.await_args.args[0]
    assert updated.agent_id == "research-agent"
    assert updated.changed_fields() == {"name": "Research"}


@pytest.mark.asyncio
async def test_agent_catalog_rejects_builtin_and_empty_patch_before_port() -> None:
    registry = AsyncMock()
    catalog = AgentCatalog(registry)

    with pytest.raises(AgentBuiltinImmutableError):
        await catalog.remove("main")
    with pytest.raises(ValueError, match="No fields to update"):
        await catalog.update(UpdateAgent(agent_id="ops"))

    registry.remove.assert_not_awaited()
    registry.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_catalog_lists_empty_when_registry_is_unavailable() -> None:
    assert await AgentCatalog(None).list() == ()
