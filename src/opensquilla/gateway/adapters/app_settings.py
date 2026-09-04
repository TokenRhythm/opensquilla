"""Gateway Adapter implementing the AppSettings Ports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast

from opensquilla.application.app_settings import (
    EffectiveSettings,
    SettingsMutation,
    SettingsObject,
    SettingsValue,
)


class AppSettingsRuntime(Protocol):
    """Gateway-owned settings persistence and reconciliation primitives."""

    async def read_effective_settings(self) -> EffectiveSettings: ...

    async def patch_settings(
        self,
        changes: Mapping[str, SettingsValue],
        *,
        safe: bool,
    ) -> SettingsMutation: ...

    async def merge_settings(
        self, patch: Mapping[str, SettingsValue]
    ) -> SettingsMutation: ...


class GatewayAppSettingsPort:
    """Adapt the Gateway composition context to application capabilities."""

    def __init__(
        self,
        config: Any,
        *,
        runtime: AppSettingsRuntime | None = None,
    ) -> None:
        self._config = config
        self._runtime = runtime

    async def read_public_settings(self) -> SettingsObject:
        config = self._config
        if config is None:
            return {}
        value = (
            config.to_public_dict()
            if hasattr(config, "to_public_dict")
            else config.model_dump()
            if hasattr(config, "model_dump")
            else {}
        )
        return cast(SettingsObject, dict(value)) if isinstance(value, Mapping) else {}

    async def read_effective_settings(self) -> EffectiveSettings:
        if self._runtime is None:
            raise RuntimeError("effective settings reader is not configured")
        return cast(EffectiveSettings, await self._runtime.read_effective_settings())

    async def patch_settings(
        self,
        changes: Mapping[str, SettingsValue],
        *,
        safe: bool,
    ) -> SettingsMutation:
        if self._runtime is None:
            raise RuntimeError("settings mutation runner is not configured")
        return cast(
            SettingsMutation,
            await self._runtime.patch_settings(changes, safe=safe),
        )

    async def merge_settings(self, patch: Mapping[str, SettingsValue]) -> SettingsMutation:
        if self._runtime is None:
            raise RuntimeError("settings mutation runner is not configured")
        return cast(
            SettingsMutation,
            await self._runtime.merge_settings(patch),
        )


__all__ = ["AppSettingsRuntime", "GatewayAppSettingsPort"]
