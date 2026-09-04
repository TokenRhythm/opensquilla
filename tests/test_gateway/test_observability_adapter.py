from __future__ import annotations

import pytest

from opensquilla.application.observability import ReadinessFinding, ReadinessQuery
from opensquilla.gateway import rpc_doctor
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_doctor import _GatewayReadinessRuntime


@pytest.mark.asyncio
async def test_readiness_runtime_projects_queries_to_domain_collectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    def findings(surface: str, payload: dict[str, object]) -> tuple[ReadinessFinding, ...]:
        calls.append((surface, payload))
        return (
            ReadinessFinding(
                id=f"{surface}.ready",
                severity="ok",
                surface=surface,
                title="Ready",
                detail="Ready",
            ),
        )

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
    monkeypatch.setattr(rpc_doctor, "evaluate_readiness_surface", findings)

    port = _GatewayReadinessRuntime(RpcContext(conn_id="test"))
    query = ReadinessQuery(agent_id="operator", deep=False, probe_providers=True)

    assert [item.id for item in await port.provider(query)] == ["provider.ready"]
    assert [item.id for item in await port.router(query)] == ["router.ready"]

    assert calls == [
        ("provider", {"probeModels": True}),
        ("provider", {"surface": "provider"}),
        ("router", False),
        ("router", {"surface": "router"}),
    ]
