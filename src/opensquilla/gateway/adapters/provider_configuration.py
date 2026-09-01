"""Gateway Adapter implementations for provider-configuration Ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from opensquilla.gateway.rpc import RpcContext

ModelCatalogLoader = Callable[[RpcContext], Awaitable[dict[str, Any]]]
RoutingReader = Callable[[RpcContext], Awaitable[dict[str, Any]]]
RoutingWriter = Callable[[str, RpcContext], Awaitable[dict[str, Any]]]
ProviderStatusLoader = Callable[
    [str | None, bool, RpcContext], Awaitable[dict[str, Any]]
]


class RpcContextModelCatalogPort:
    def __init__(self, ctx: RpcContext, loader: ModelCatalogLoader) -> None:
        self._ctx = ctx
        self._loader = loader

    async def load_model_catalog(self) -> Mapping[str, Any]:
        return await self._loader(self._ctx)


class RpcContextModelRoutingPort:
    def __init__(
        self,
        ctx: RpcContext,
        *,
        reader: RoutingReader,
        writer: RoutingWriter,
    ) -> None:
        self._ctx = ctx
        self._reader = reader
        self._writer = writer

    async def read_model_routing(self) -> Mapping[str, Any]:
        return await self._reader(self._ctx)

    async def write_model_routing(self, mode: str) -> Mapping[str, Any]:
        return await self._writer(mode, self._ctx)


class RpcContextProviderStatusPort:
    def __init__(self, ctx: RpcContext, loader: ProviderStatusLoader) -> None:
        self._ctx = ctx
        self._loader = loader

    async def load_provider_status(
        self,
        *,
        provider_id: str | None,
        probe_models: bool,
    ) -> Mapping[str, Any]:
        return await self._loader(provider_id, probe_models, self._ctx)
