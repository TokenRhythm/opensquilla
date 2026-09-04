"""Application Module for public settings reads and mutations.

The Module owns path semantics and mutation validation.  Its Ports expose
configuration capabilities rather than Gateway request objects, so neither
the public Interface nor the application logic depends on ``RpcContext`` or
the v4 wire shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NotRequired, Protocol, TypedDict, cast

type SettingsValue = (
    None
    | bool
    | int
    | float
    | str
    | list["SettingsValue"]
    | dict[str, "SettingsValue"]
)
type SettingsObject = dict[str, SettingsValue]


class EffectiveSetting(TypedDict):
    value: SettingsValue
    source: str


class EffectiveSettings(TypedDict):
    fields: dict[str, EffectiveSetting]


class SettingsMutation(TypedDict):
    patched: list[str]
    restartRequired: bool
    restartSections: NotRequired[list[str]]
    liveApplied: NotRequired[list[str]]
    linked: NotRequired[list[str]]
    model_routing: NotRequired[SettingsObject]


class SettingsReadPort(Protocol):
    """Read-only configuration Port implemented at the Gateway boundary."""

    async def read_public_settings(self) -> SettingsObject: ...

    async def read_effective_settings(self) -> EffectiveSettings: ...


class SettingsMutationPort(Protocol):
    """Durable mutation Port implemented by the running Gateway."""

    async def patch_settings(
        self,
        changes: Mapping[str, SettingsValue],
        *,
        safe: bool,
    ) -> SettingsMutation: ...

    async def merge_settings(self, patch: Mapping[str, SettingsValue]) -> SettingsMutation: ...


@dataclass(frozen=True, slots=True)
class SettingChange:
    path: str
    value: SettingsValue


class AppSettings:
    """Expose business-oriented settings operations over narrow Ports."""

    def __init__(
        self,
        reader: SettingsReadPort,
        mutator: SettingsMutationPort | None = None,
    ) -> None:
        self._reader = reader
        self._mutator = mutator

    async def read_all(self) -> SettingsObject:
        return dict(await self._reader.read_public_settings())

    async def read(self, path: str) -> SettingsValue | None:
        normalized = self._normalize_path(path)
        value: SettingsValue = await self._reader.read_public_settings()
        for part in normalized.split("."):
            if not isinstance(value, Mapping):
                return None
            value = value.get(part)
        return value

    async def read_effective(self) -> EffectiveSettings:
        return cast(EffectiveSettings, dict(await self._reader.read_effective_settings()))

    async def patch(self, changes: Sequence[SettingChange]) -> SettingsMutation:
        return await self._patch(changes, safe=False)

    async def patch_safe(self, changes: Sequence[SettingChange]) -> SettingsMutation:
        return await self._patch(changes, safe=True)

    async def merge(self, patch: Mapping[str, SettingsValue]) -> SettingsMutation:
        if not patch:
            raise ValueError("settings patch must not be empty")
        return cast(
            SettingsMutation,
            dict(await self._mutation_port().merge_settings(dict(patch))),
        )

    async def _patch(
        self,
        changes: Sequence[SettingChange],
        *,
        safe: bool,
    ) -> SettingsMutation:
        if not changes:
            raise ValueError("settings changes must not be empty")
        normalized: dict[str, SettingsValue] = {}
        for change in changes:
            path = self._normalize_path(change.path)
            if path in normalized:
                raise ValueError(f"duplicate settings path: {path}")
            normalized[path] = change.value
        return cast(
            SettingsMutation,
            dict(await self._mutation_port().patch_settings(normalized, safe=safe)),
        )

    def _mutation_port(self) -> SettingsMutationPort:
        if self._mutator is None:
            raise RuntimeError("settings mutation Port is not configured")
        return self._mutator

    @staticmethod
    def _normalize_path(path: str) -> str:
        normalized = str(path or "").strip()
        if not normalized or any(not part for part in normalized.split(".")):
            raise ValueError("settings path must be a non-empty dotted path")
        return normalized
