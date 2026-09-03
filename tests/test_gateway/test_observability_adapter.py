from __future__ import annotations

from typing import Any

import pytest

from opensquilla.application.observability import ReadinessQuery
from opensquilla.gateway.adapters.observability import GatewayReadinessDataPort
from opensquilla.gateway.rpc import RpcContext


@pytest.mark.asyncio
async def test_readiness_adapter_projects_domain_queries_to_existing_collectors() -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    def reader(name: str):
        async def collect(
            params: dict[str, Any] | None,
            context: RpcContext,
        ) -> dict[str, Any]:
            assert context.conn_id == "test"
            calls.append((name, params))
            return {"surface": name}

        return collect

    port = GatewayReadinessDataPort(
        RpcContext(conn_id="test"),
        provider=reader("provider"),
        logs=reader("logs"),
        memory=reader("memory"),
        channels=reader("channels"),
        sandbox=reader("sandbox"),
        router=reader("router"),
        squilla_router=reader("squilla_router"),
        memory_embedding=reader("memory_embedding"),
        search=reader("search"),
        image_generation=reader("image_generation"),
        llm_ensemble=reader("llm_ensemble"),
    )
    query = ReadinessQuery(agent_id="operator", deep=False, probe_providers=True)

    assert await port.provider(query) == {"surface": "provider"}
    assert await port.memory(query) == {"surface": "memory"}
    assert await port.router(query) == {"surface": "router"}

    assert calls == [
        ("provider", {"probeModels": True}),
        ("memory", {"agentId": "operator", "deep": False}),
        ("router", {"deep": False}),
    ]
