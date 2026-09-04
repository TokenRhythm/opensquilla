"""Transport-neutral channel administration use cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ChannelTarget:
    name: str


@dataclass(frozen=True, slots=True)
class ProbeChannel:
    name: str | None = None
    entry: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PairingQuery:
    channel_name: str
    status: str | None = None
    limit: int | None = None
    offset: int = 0


@dataclass(frozen=True, slots=True)
class PairingTarget:
    channel_name: str
    pairing_id: str | None = None
    pairing_code: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovePairing:
    target: PairingTarget
    as_admin: bool = False


@dataclass(frozen=True, slots=True)
class SetChannelAdmin:
    channel_name: str
    sender_id: str
    admin: bool


class ChannelAdministrationPort(Protocol):
    async def status(self) -> Mapping[str, Any]: ...

    async def get(self, target: ChannelTarget) -> Mapping[str, Any]: ...

    async def probe(self, command: ProbeChannel) -> Mapping[str, Any]: ...

    async def restart(self, target: ChannelTarget) -> Mapping[str, Any]: ...

    async def logout(self, target: ChannelTarget) -> Mapping[str, Any]: ...


class ChannelPairingPort(Protocol):
    async def list(self, query: PairingQuery) -> Sequence[Mapping[str, Any]]: ...

    async def approve(self, command: ApprovePairing) -> Mapping[str, Any]: ...

    async def revoke(self, target: PairingTarget) -> Mapping[str, Any]: ...

    async def set_admin(self, command: SetChannelAdmin) -> Mapping[str, Any]: ...


class ChannelAdministration:
    def __init__(self, port: ChannelAdministrationPort) -> None:
        self._port = port

    async def status(self) -> Mapping[str, Any]:
        return await self._port.status()

    async def get(self, name: str) -> Mapping[str, Any]:
        return await self._port.get(ChannelTarget(self._name(name)))

    async def probe(self, command: ProbeChannel) -> Mapping[str, Any]:
        if command.entry is not None:
            if not isinstance(command.entry, Mapping):
                raise ValueError("channel entry must be an object")
            return await self._port.probe(command)
        if command.name is None:
            raise ValueError("channel entry or name required")
        return await self._port.probe(replace(command, name=self._name(command.name)))

    async def restart(self, name: str) -> Mapping[str, Any]:
        return await self._port.restart(ChannelTarget(self._name(name)))

    async def logout(self, name: str) -> Mapping[str, Any]:
        return await self._port.logout(ChannelTarget(self._name(name)))

    @staticmethod
    def _name(value: str) -> str:
        name = value.strip() if isinstance(value, str) else ""
        if not name:
            raise ValueError("channel name required")
        return name


class ChannelPairingAdministration:
    def __init__(self, port: ChannelPairingPort) -> None:
        self._port = port

    async def list(self, query: PairingQuery) -> Sequence[Mapping[str, Any]]:
        channel_name = self._name(query.channel_name)
        status = query.status.strip() if isinstance(query.status, str) else None
        if query.limit is not None and query.limit < 0:
            raise ValueError("limit must be non-negative")
        if query.offset < 0:
            raise ValueError("offset must be non-negative")
        return await self._port.list(
            replace(query, channel_name=channel_name, status=status or None)
        )

    async def approve(self, command: ApprovePairing) -> Mapping[str, Any]:
        target = self._target(command.target)
        return await self._port.approve(replace(command, target=target))

    async def revoke(self, target: PairingTarget) -> Mapping[str, Any]:
        return await self._port.revoke(self._target(target))

    async def set_admin(self, command: SetChannelAdmin) -> Mapping[str, Any]:
        channel_name = self._name(command.channel_name)
        sender_id = command.sender_id.strip() if isinstance(command.sender_id, str) else ""
        if not sender_id:
            raise ValueError("senderId required")
        if not isinstance(command.admin, bool):
            raise ValueError("admin required (boolean)")
        return await self._port.set_admin(
            replace(command, channel_name=channel_name, sender_id=sender_id)
        )

    @classmethod
    def _target(cls, target: PairingTarget) -> PairingTarget:
        channel_name = cls._name(target.channel_name)
        pairing_id = target.pairing_id.strip() if isinstance(target.pairing_id, str) else ""
        pairing_code = (
            target.pairing_code.strip() if isinstance(target.pairing_code, str) else ""
        )
        if not pairing_id and not pairing_code:
            raise ValueError("pairingId or pairingCode required")
        return replace(
            target,
            channel_name=channel_name,
            pairing_id=pairing_id or None,
            pairing_code=pairing_code or None,
        )

    @staticmethod
    def _name(value: str) -> str:
        name = value.strip() if isinstance(value, str) else ""
        if not name:
            raise ValueError("channelName required")
        return name


__all__ = [
    "ApprovePairing",
    "ChannelAdministration",
    "ChannelAdministrationPort",
    "ChannelPairingAdministration",
    "ChannelPairingPort",
    "ChannelTarget",
    "PairingQuery",
    "PairingTarget",
    "ProbeChannel",
    "SetChannelAdmin",
]
