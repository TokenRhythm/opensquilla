from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.channel_administration import (
    ApprovePairing,
    ChannelAdministrationPort,
    ChannelPairingPort,
    ChannelTarget,
    PairingQuery,
    PairingTarget,
)
from opensquilla.gateway.adapters.channel_administration import (
    GatewayChannelAdministrationAdapter,
)


def _ports() -> tuple[AsyncMock, AsyncMock]:
    administration = AsyncMock(spec=ChannelAdministrationPort)
    administration.status.return_value = {"channels": [{"name": "ops"}]}
    administration.get.return_value = {
        "entry": {"name": "ops"},
        "secretFields": [],
    }
    administration.probe.return_value = {"status": "verified", "connected": True}
    administration.restart.return_value = {"status": "restarted", "channel": "ops"}
    administration.logout.return_value = {"status": "disconnected", "channel": "ops"}

    pairings = AsyncMock(spec=ChannelPairingPort)
    pairings.list.return_value = [
        {
            "pairingId": "pair-1",
            "channelName": "ops",
            "senderId": "user-1",
            "status": "pending",
        }
    ]
    pairings.approve.return_value = {
        "pairing": {
            "pairingId": "pair-1",
            "channelName": "ops",
            "senderId": "user-1",
            "status": "approved",
        },
        "adminGranted": True,
    }
    pairings.revoke.return_value = {
        "pairing": {
            "pairingId": "pair-1",
            "channelName": "ops",
            "senderId": "user-1",
            "status": "revoked",
        }
    }
    pairings.set_admin.return_value = {
        "channelName": "ops",
        "senderId": "user-1",
        "admin": True,
        "admins": ["user-1"],
    }
    return administration, pairings


@pytest.mark.asyncio
async def test_channel_adapter_projects_typed_channel_and_pairing_intents() -> None:
    administration, pairings = _ports()
    adapter = GatewayChannelAdministrationAdapter(administration, pairings)

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

    administration.get.assert_awaited_once_with(ChannelTarget("ops"))
    pairings.list.assert_awaited_once_with(
        PairingQuery(channel_name="ops", status="pending", limit=10, offset=1)
    )
    pairings.approve.assert_awaited_once_with(
        ApprovePairing(
            PairingTarget(channel_name="ops", pairing_code="abcdef12"),
            as_admin=True,
        )
    )


@pytest.mark.asyncio
async def test_channel_adapter_rejects_invalid_admin_before_runtime() -> None:
    administration, pairings = _ports()
    adapter = GatewayChannelAdministrationAdapter(administration, pairings)

    with pytest.raises(ValueError, match="admin required"):
        await adapter.set_admin({"channelName": "ops", "senderId": "user-1"})

    pairings.set_admin.assert_not_awaited()
