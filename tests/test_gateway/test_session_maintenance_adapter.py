from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.adapters.session_maintenance import (
    GatewaySessionMaintenanceAdapter,
    GatewaySessionMaintenanceCallbacks,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError


def _adapter() -> tuple[GatewaySessionMaintenanceAdapter, AsyncMock, AsyncMock, RpcContext]:
    reset = AsyncMock(return_value={"key": "canonical", "reset": True})
    compact = AsyncMock(return_value={"key": "canonical", "status": "started"})
    context = cast(RpcContext, SimpleNamespace())
    adapter = GatewaySessionMaintenanceAdapter(
        context,
        GatewaySessionMaintenanceCallbacks(
            require_key=lambda params: str((params or {})["key"]),
            execute_reset=reset,
            execute_compact=compact,
        ),
    )
    return adapter, reset, compact, context


async def test_adapter_maps_wire_fields_to_typed_commands() -> None:
    adapter, reset, compact, context = _adapter()

    await adapter.reset({"key": "canonical", "force": True})
    await adapter.compact(
        {
            "key": "canonical",
            "wait": False,
            "contextWindowTokens": 8_192,
            "instructions": "Keep obligations.",
        }
    )

    reset.assert_awaited_once_with(
        {"key": "canonical", "force": True},
        context,
    )
    compact.assert_awaited_once_with(
        {
            "key": "canonical",
            "wait": False,
            "contextWindowTokens": 8_192,
            "instructions": "Keep obligations.",
        },
        context,
    )


async def test_adapter_rejects_invalid_instructions_before_runtime() -> None:
    adapter, _reset, compact, _context = _adapter()

    with pytest.raises(RpcHandlerError) as raised:
        await adapter.compact({"key": "canonical", "instructions": 3})

    assert raised.value.code == "INVALID_PARAMS"
    compact.assert_not_awaited()
