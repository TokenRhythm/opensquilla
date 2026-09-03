"""Gateway Adapter implementations for provider-configuration Ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from opensquilla.gateway.provider_status_runtime import read_provider_status
from opensquilla.gateway.rpc import RpcContext

ModelCatalogLoader = Callable[[RpcContext], Awaitable[dict[str, Any]]]
RoutingReader = Callable[[RpcContext], Awaitable[dict[str, Any]]]
RoutingWriter = Callable[[str, RpcContext], Awaitable[dict[str, Any]]]


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


class GatewayProviderStatusPort:
    """Provider status projection backed by concrete Gateway dependencies."""

    def __init__(self, ctx: RpcContext) -> None:
        self._ctx = ctx

    async def load_provider_status(
        self,
        *,
        provider_id: str | None,
        probe_models: bool,
    ) -> Mapping[str, Any]:
        return await read_provider_status(
            config=getattr(self._ctx, "config", None),
            provider_selector=getattr(self._ctx, "provider_selector", None),
            provider_stats=getattr(self._ctx, "provider_stats", None),
            provider_id=provider_id,
            probe_models=probe_models,
        )
