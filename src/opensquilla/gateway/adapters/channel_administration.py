"""Gateway Adapter for channel administration application Modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from opensquilla.application.channel_administration import (
    ApprovePairing,
    ChannelAdministration,
    ChannelAdministrationPort,
    ChannelPairingAdministration,
    ChannelPairingPort,
    ChannelTarget,
    PairingQuery,
    PairingTarget,
    ProbeChannel,
    SetChannelAdmin,
)
from opensquilla.gateway.rpc import RpcContext

type ChannelExecutor = Callable[
    [dict[str, Any] | None, RpcContext], Awaitable[dict[str, Any]]
]


@dataclass(frozen=True, slots=True)
class GatewayChannelAdministrationCallbacks:
    status: ChannelExecutor
    get: ChannelExecutor
    probe: ChannelExecutor
    restart: ChannelExecutor
    logout: ChannelExecutor
    pairings: ChannelExecutor
    approve_pairing: ChannelExecutor
    revoke_pairing: ChannelExecutor
    set_admin: ChannelExecutor


class GatewayChannelAdministrationRuntime(ChannelAdministrationPort):
    def __init__(
        self, context: RpcContext, callbacks: GatewayChannelAdministrationCallbacks
    ) -> None:
        self._context = context
        self._callbacks = callbacks

    async def status(self) -> Mapping[str, Any]:
        return await self._callbacks.status(None, self._context)

    async def get(self, target: ChannelTarget) -> Mapping[str, Any]:
        return await self._callbacks.get({"name": target.name}, self._context)

    async def probe(self, command: ProbeChannel) -> Mapping[str, Any]:
        params = (
            {"entry": dict(command.entry)}
            if command.entry is not None
            else {"name": command.name}
        )
        return await self._callbacks.probe(params, self._context)

    async def restart(self, target: ChannelTarget) -> Mapping[str, Any]:
        return await self._callbacks.restart({"name": target.name}, self._context)

    async def logout(self, target: ChannelTarget) -> Mapping[str, Any]:
        return await self._callbacks.logout({"name": target.name}, self._context)


class GatewayChannelPairingRuntime(ChannelPairingPort):
    def __init__(
        self, context: RpcContext, callbacks: GatewayChannelAdministrationCallbacks
    ) -> None:
        self._context = context
        self._callbacks = callbacks

    @staticmethod
    def _target(target: PairingTarget) -> dict[str, Any]:
        return {
            "channelName": target.channel_name,
            **({"pairingId": target.pairing_id} if target.pairing_id else {}),
            **({"pairingCode": target.pairing_code} if target.pairing_code else {}),
        }

    async def list(self, query: PairingQuery) -> Sequence[Mapping[str, Any]]:
        result = await self._callbacks.pairings(
            {
                "channelName": query.channel_name,
                **({"status": query.status} if query.status else {}),
                **({"limit": query.limit} if query.limit is not None else {}),
                "offset": query.offset,
            },
            self._context,
        )
        pairings = result.get("pairings")
        return pairings if isinstance(pairings, list) else ()

    async def approve(self, command: ApprovePairing) -> Mapping[str, Any]:
        return await self._callbacks.approve_pairing(
            {**self._target(command.target), **({"asAdmin": True} if command.as_admin else {})},
            self._context,
        )

    async def revoke(self, target: PairingTarget) -> Mapping[str, Any]:
        return await self._callbacks.revoke_pairing(self._target(target), self._context)

    async def set_admin(self, command: SetChannelAdmin) -> Mapping[str, Any]:
        return await self._callbacks.set_admin(
            {
                "channelName": command.channel_name,
                "senderId": command.sender_id,
                "admin": command.admin,
            },
            self._context,
        )


class GatewayChannelAdministrationAdapter:
    def __init__(
        self, context: RpcContext, callbacks: GatewayChannelAdministrationCallbacks
    ) -> None:
        self._administration = ChannelAdministration(
            GatewayChannelAdministrationRuntime(context, callbacks)
        )
        self._pairings = ChannelPairingAdministration(
            GatewayChannelPairingRuntime(context, callbacks)
        )

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
    "GatewayChannelAdministrationCallbacks",
    "GatewayChannelAdministrationRuntime",
    "GatewayChannelPairingRuntime",
]
