from __future__ import annotations

from typing import Any

import pytest

from opensquilla.cli.gateway_client import GatewayClient


@pytest.mark.asyncio
async def test_list_sessions_uses_shared_contract_adapter_without_changing_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GatewayClient()
    calls: list[tuple[str, dict[str, Any] | None]] = []
    expected: dict[str, Any] = {
        "sessions": [],
        "count": 0,
        "ts": 1,
        "future": {"kept": True},
    }

    async def fake_call(method: str, params: dict[str, Any] | None = None) -> Any:
        calls.append((method, params))
        return expected

    monkeypatch.setattr(client, "_call", fake_call)

    result = await client.list_sessions(limit=29)

    assert calls == [("sessions.list", {"limit": 29})]
    assert result is expected
