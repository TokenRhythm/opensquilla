"""Gateway Adapter for channel administration application Modules."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from opensquilla.application.channel_administration import (
    ApprovePairing,
    ChannelAdministration,
    ChannelAdministrationPort,
    ChannelPairingAdministration,
    ChannelPairingPort,
    PairingQuery,
    PairingTarget,
    ProbeChannel,
    SetChannelAdmin,
)


class GatewayChannelAdministrationAdapter:
    def __init__(
        self,
        administration: ChannelAdministrationPort,
        pairings: ChannelPairingPort,
    ) -> None:
        self._administration = ChannelAdministration(administration)
        self._pairings = ChannelPairingAdministration(pairings)

    async def status(self, _params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._administration.status())

    async def get(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._administration.get(self._channel(params)))

    async def probe(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        entry = raw.get("entry")
        command = ProbeChannel(
            name=cast(str | None, raw.get("name")),
            entry=cast(Mapping[str, Any] | None, entry if isinstance(entry, dict) else None),
        )
        return dict(await self._administration.probe(command))

    async def restart(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._administration.restart(self._channel(params)))

    async def logout(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._administration.logout(self._channel(params)))

    async def list_pairings(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        limit = raw.get("limit")
        offset = raw.get("offset", 0)
        rows = await self._pairings.list(
            PairingQuery(
                channel_name=cast(str, raw.get("channelName")),
                status=cast(str | None, raw.get("status")),
                limit=int(limit) if limit is not None else None,
                offset=int(offset) if offset is not None else 0,
            )
        )
        return {"pairings": [dict(row) for row in rows]}

    async def approve_pairing(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        return dict(
            await self._pairings.approve(
                ApprovePairing(self._pairing_target(raw), bool(raw.get("asAdmin", False)))
            )
        )

    async def revoke_pairing(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        return dict(await self._pairings.revoke(self._pairing_target(raw)))

    async def set_admin(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        return dict(
            await self._pairings.set_admin(
                SetChannelAdmin(
                    channel_name=cast(str, raw.get("channelName")),
                    sender_id=cast(str, raw.get("senderId")),
                    admin=cast(bool, raw.get("admin")),
                )
            )
        )

    @staticmethod
    def _channel(params: dict[str, Any] | None) -> Any:
        raw = params if isinstance(params, dict) else {}
        return raw.get("name") or raw.get("channel")

    @staticmethod
    def _pairing_target(raw: Mapping[str, Any]) -> PairingTarget:
        return PairingTarget(
            channel_name=cast(str, raw.get("channelName")),
            pairing_id=cast(str | None, raw.get("pairingId")),
            pairing_code=cast(str | None, raw.get("pairingCode")),
        )


__all__ = [
    "GatewayChannelAdministrationAdapter",
]
