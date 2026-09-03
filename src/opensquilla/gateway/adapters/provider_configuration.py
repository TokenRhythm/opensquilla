"""Gateway Adapter implementations for provider-configuration Ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from opensquilla.application.provider_configuration import PreparedModelRouting
from opensquilla.gateway.model_routing import (
    apply_model_routing_mode,
    broadcast_model_routing_changed_if_needed,
    model_routing_patches,
    model_routing_public_snapshot,
)
from opensquilla.gateway.provider_runtime import (
    resolve_provider_selector_config,
    sync_resolved_provider_selector,
)
from opensquilla.gateway.provider_status_runtime import read_provider_status
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.setup_config_runtime import sync_media_runtime

ModelCatalogLoader = Callable[[RpcContext], Awaitable[dict[str, Any]]]


class RpcContextModelCatalogPort:
    def __init__(self, ctx: RpcContext, loader: ModelCatalogLoader) -> None:
        self._ctx = ctx
        self._loader = loader

    async def load_model_catalog(self) -> Mapping[str, Any]:
        return await self._loader(self._ctx)


class GatewayModelRoutingPolicyPort:
    """Translate domain routing intent into a detached config candidate."""

    def snapshot(self, config: Any) -> Mapping[str, Any]:
        return model_routing_public_snapshot(config)

    def prepare(self, config: Any, mode: str) -> PreparedModelRouting:
        patches = model_routing_patches(config, mode)
        candidate = config.model_copy(deep=True)
        apply_model_routing_mode(candidate, mode, activation_config=config)
        return PreparedModelRouting(candidate, tuple(patches))


class RpcContextModelRoutingRuntimePort:
    """Reconcile a committed routing candidate with live Gateway state."""

    def __init__(self, ctx: RpcContext) -> None:
        self._ctx = ctx

    def prepare_reconciliation(self, config: Any) -> Any:
        # Resolve environment-backed provider values before persistence.  The
        # candidate tracks their provenance so sparse config writes never bake
        # runtime credentials into the durable file.
        return resolve_provider_selector_config(config)

    async def reconcile(self, config: Any, prepared: Any) -> None:
        sync_resolved_provider_selector(self._ctx, prepared)
        sync_media_runtime(config)

    async def publish_changed(
        self,
        previous: Mapping[str, Any],
        config: Any,
        *,
        source: str,
    ) -> None:
        await broadcast_model_routing_changed_if_needed(
            self._ctx,
            previous=dict(previous),
            source=source,
            config=config,
        )


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
