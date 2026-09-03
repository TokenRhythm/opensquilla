from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.adapters.channel_administration import (
    GatewayChannelAdministrationAdapter,
    GatewayChannelAdministrationCallbacks,
)
from opensquilla.gateway.rpc import RpcContext


def _callbacks() -> GatewayChannelAdministrationCallbacks:
    return GatewayChannelAdministrationCallbacks(
        status=AsyncMock(return_value={"channels": [{"name": "ops"}]}),
        get=AsyncMock(return_value={"entry": {"name": "ops"}, "secretFields": []}),
        probe=AsyncMock(return_value={"status": "verified", "connected": True}),
        restart=AsyncMock(return_value={"status": "restarted", "channel": "ops"}),
        logout=AsyncMock(return_value={"status": "disconnected", "channel": "ops"}),
        pairings=AsyncMock(
            return_value={
                "pairings": [
                    {
                        "pairingId": "pair-1",
                        "channelName": "ops",
                        "senderId": "user-1",
                        "status": "pending",
                    }
                ]
            }
        ),
        approve_pairing=AsyncMock(
            return_value={
                "pairing": {
                    "pairingId": "pair-1",
                    "channelName": "ops",
                    "senderId": "user-1",
                    "status": "approved",
                },
                "adminGranted": True,
            }
        ),
        revoke_pairing=AsyncMock(
            return_value={
                "pairing": {
                    "pairingId": "pair-1",
                    "channelName": "ops",
                    "senderId": "user-1",
                    "status": "revoked",
                }
            }
        ),
        set_admin=AsyncMock(
            return_value={
                "channelName": "ops",
                "senderId": "user-1",
                "admin": True,
                "admins": ["user-1"],
            }
        ),
    )


@pytest.mark.asyncio
async def test_channel_adapter_projects_typed_channel_and_pairing_intents() -> None:
    callbacks = _callbacks()
    context = cast(RpcContext, SimpleNamespace())
    adapter = GatewayChannelAdministrationAdapter(context, callbacks)

    assert await adapter.get({"name": " ops "}) == {
        "entry": {"name": "ops"},
        "secretFields": [],
    }
    assert await adapter.list_pairings(
        {"channelName": " ops ", "status": " pending ", "limit": 10, "offset": 1}
    ) == {
        "pairings": [
            {
                "pairingId": "pair-1",
                "channelName": "ops",
                "senderId": "user-1",
                "status": "pending",
            }
        ]
    }
    await adapter.approve_pairing(
        {"channelName": "ops", "pairingCode": "abcdef12", "asAdmin": True}
    )

    cast(AsyncMock, callbacks.get).assert_awaited_once_with({"name": "ops"}, context)
    cast(AsyncMock, callbacks.pairings).assert_awaited_once_with(
        {"channelName": "ops", "status": "pending", "limit": 10, "offset": 1},
        context,
    )
    cast(AsyncMock, callbacks.approve_pairing).assert_awaited_once_with(
        {"channelName": "ops", "pairingCode": "abcdef12", "asAdmin": True},
        context,
    )


@pytest.mark.asyncio
async def test_channel_adapter_rejects_invalid_admin_before_runtime() -> None:
    callbacks = _callbacks()
    adapter = GatewayChannelAdministrationAdapter(
        cast(RpcContext, SimpleNamespace()), callbacks
    )

    with pytest.raises(ValueError, match="admin required"):
        await adapter.set_admin({"channelName": "ops", "senderId": "user-1"})

    cast(AsyncMock, callbacks.set_admin).assert_not_awaited()
