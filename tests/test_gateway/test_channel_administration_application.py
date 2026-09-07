from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.channel_administration import (
    ApprovePairing,
    ChannelAdministration,
    ChannelPairingAdministration,
    PairingQuery,
    PairingTarget,
    SetChannelAdmin,
)


@pytest.mark.asyncio
async def test_channel_administration_normalizes_channel_names() -> None:
    port = AsyncMock()
    port.get.return_value = {"entry": {"name": "ops"}, "secretFields": []}
    administration = ChannelAdministration(port)

    await administration.get(" ops ")

    assert port.get.await_args.args[0].name == "ops"


@pytest.mark.asyncio
async def test_pairing_administration_normalizes_queries_and_targets() -> None:
    port = AsyncMock()
    port.list.return_value = ()
    port.approve.return_value = {"pairing": {"pairingId": "pair-1"}}
    pairings = ChannelPairingAdministration(port)

    await pairings.list(PairingQuery(" ops ", status=" pending ", limit=20, offset=2))
    await pairings.approve(
        ApprovePairing(PairingTarget(" ops ", pairing_code=" abcdef12 "), as_admin=True)
    )

    query = port.list.await_args.args[0]
    assert query == PairingQuery("ops", status="pending", limit=20, offset=2)
    command = port.approve.await_args.args[0]
    assert command.target == PairingTarget("ops", pairing_code="abcdef12")
    assert command.as_admin is True


@pytest.mark.asyncio
async def test_pairing_administration_rejects_ambiguous_empty_mutations() -> None:
    port = AsyncMock()
    pairings = ChannelPairingAdministration(port)

    with pytest.raises(ValueError, match="pairingId or pairingCode"):
        await pairings.revoke(PairingTarget("ops"))
    with pytest.raises(ValueError, match="admin required"):
        await pairings.set_admin(SetChannelAdmin("ops", "sender", None))  # type: ignore[arg-type]

    port.revoke.assert_not_awaited()
    port.set_admin.assert_not_awaited()
