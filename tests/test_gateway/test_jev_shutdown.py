from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.boot import GatewayServer
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.task_runtime import TaskRuntimeShutdownResult


@pytest.mark.parametrize("clean", [True, False])
async def test_local_ledger_closes_after_runtime_only_when_writers_are_drained(
    monkeypatch, clean,
) -> None:
    calls = []

    async def shutdown(**_kwargs):
        calls.append("runtime")
        return TaskRuntimeShutdownResult(
            clean=clean, elapsed_ms=0, abandoned_task_count=0,
            remaining_driver_count=0 if clean else 1,
            remaining_reservation_count=0, remaining_auxiliary_count=0,
        )

    ledger = SimpleNamespace(close=AsyncMock(side_effect=lambda: calls.append("jev")))
    services = SimpleNamespace(
        task_runtime=SimpleNamespace(shutdown=shutdown),
        close=AsyncMock(side_effect=lambda: calls.append("services")),
    )
    server = GatewayServer(
        app=SimpleNamespace(state=SimpleNamespace(turn_runner=SimpleNamespace(jev_service=ledger))),
        config=GatewayConfig(), _services=services,
    )
    monkeypatch.setattr(
        "opensquilla.gateway.boot.get_registry",
        lambda: SimpleNamespace(broadcast=AsyncMock(), all=lambda: []),
    )
    monkeypatch.setattr("opensquilla.mcp.discovery.close_active_clients", AsyncMock())
    monkeypatch.setattr(
        "opensquilla.gateway.terminal_sessions.get_terminal_manager",
        lambda: SimpleNamespace(close_all=AsyncMock()),
    )
    result = await server.close()
    assert result.clean is clean
    assert calls == (["runtime", "jev", "services"] if clean else ["runtime"])
