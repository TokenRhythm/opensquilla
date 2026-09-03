from __future__ import annotations

import pytest

from opensquilla.application.observability import ReadinessQuery
from opensquilla.gateway import rpc_doctor
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_doctor import _GatewayReadinessRuntime


@pytest.mark.asyncio
async def test_readiness_runtime_projects_queries_to_domain_collectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    async def provider(params: object, context: RpcContext) -> dict[str, str]:
        assert context.conn_id == "test"
        calls.append(("provider", params))
        return {"surface": "provider"}

    def router(context: RpcContext, *, deep: bool) -> dict[str, str]:
        assert context.conn_id == "test"
        calls.append(("router", deep))
        return {"surface": "router"}

    monkeypatch.setattr(rpc_doctor, "_provider_payload", provider)
    monkeypatch.setattr(rpc_doctor, "_router_payload", router)

    port = _GatewayReadinessRuntime(RpcContext(conn_id="test"))
    query = ReadinessQuery(agent_id="operator", deep=False, probe_providers=True)

    assert await port.provider(query) == {"surface": "provider"}
    assert await port.router(query) == {"surface": "router"}

    assert calls == [
        ("provider", {"probeModels": True}),
        ("router", False),
    ]
