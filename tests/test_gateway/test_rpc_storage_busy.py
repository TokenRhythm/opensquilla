from __future__ import annotations

import pytest

from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc.registry import RpcRegistry
from opensquilla.session.storage import StorageBusyError


@pytest.mark.asyncio
async def test_dispatch_maps_storage_busy_for_non_send_handlers() -> None:
    registry = RpcRegistry()

    async def _busy(params, ctx):
        raise StorageBusyError(
            "claim_memory_repair_receipt",
            waited_ms=2075,
            retry_after_ms=250,
        )

    registry.register("test.storage_busy", _busy, "operator.read")
    response = await registry.dispatch(
        "req-storage-busy",
        "test.storage_busy",
        {},
        RpcContext(conn_id="test", config=GatewayConfig()),
    )

    assert response.ok is False
    assert response.error is not None
    assert response.error.code == "STORAGE_BUSY"
    assert response.error.retryable is True
    assert response.error.retry_after_ms == 250
    assert response.error.accepted is None
    assert response.error.details == {
        "operation": "claim_memory_repair_receipt",
        "waited_ms": 2075,
    }
