"""Gateway Adapter implementing the AppSettings Ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from opensquilla.application.app_settings import (
    EffectiveSettings,
    SettingsMutation,
    SettingsObject,
    SettingsValue,
)
from opensquilla.gateway.rpc import RpcContext

PatchRunner = Callable[[dict[str, Any], RpcContext, str], Awaitable[dict[str, Any]]]
EffectiveReader = Callable[[RpcContext], Awaitable[dict[str, Any]]]


class RpcContextAppSettingsPort:
    """Adapt the Gateway composition context to application capabilities."""

    def __init__(
        self,
        ctx: RpcContext,
        *,
        patch_runner: PatchRunner | None = None,
        effective_reader: EffectiveReader | None = None,
    ) -> None:
        self._ctx = ctx
        self._patch_runner = patch_runner
        self._effective_reader = effective_reader

    async def read_public_settings(self) -> SettingsObject:
        config = self._ctx.config
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
        if self._effective_reader is None:
            raise RuntimeError("effective settings reader is not configured")
        return cast(EffectiveSettings, await self._effective_reader(self._ctx))

    async def patch_settings(
        self,
        changes: Mapping[str, SettingsValue],
        *,
        safe: bool,
    ) -> SettingsMutation:
        if self._patch_runner is None:
            raise RuntimeError("settings mutation runner is not configured")
        source = "config.patch.safe" if safe else "config.patch"
        return cast(
            SettingsMutation,
            await self._patch_runner({"patches": dict(changes)}, self._ctx, source),
        )

    async def merge_settings(self, patch: Mapping[str, SettingsValue]) -> SettingsMutation:
        if self._patch_runner is None:
            raise RuntimeError("settings mutation runner is not configured")
        return cast(
            SettingsMutation,
            await self._patch_runner({"patch": dict(patch)}, self._ctx, "config.patch"),
        )
