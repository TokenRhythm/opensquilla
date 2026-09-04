"""Gateway Adapter implementations for provider-configuration Ports."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, cast

from opensquilla.application.provider_configuration import (
    ModelCatalogResult,
    ModelRoutingSnapshot,
    PreparedModelRouting,
    ProviderStatusResult,
)
from opensquilla.gateway.model_routing import (
    apply_model_routing_mode,
    model_routing_patches,
    model_routing_public_snapshot,
)
from opensquilla.gateway.provider_runtime import resolve_provider_selector_config
from opensquilla.gateway.provider_status_runtime import read_provider_status
from opensquilla.gateway.setup_config_runtime import sync_media_runtime
from opensquilla.provider.model_catalog import ModelCatalog as ProviderModelCatalog

_catalog = ProviderModelCatalog()


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _metadata_record(metadata: dict[str, Any], name: str) -> dict[str, Any] | None:
    value = metadata.get(name)
    return value if isinstance(value, dict) else None


def _tokenrhythm_metadata_value(
    metadata: dict[str, Any],
    field: str,
) -> int | None:
    for source_name in ("declared", "published"):
        source = _metadata_record(metadata, source_name)
        if source is not None:
            value = _positive_int(source.get(field))
            if value is not None:
                return value
    return None


def _tokenrhythm_metadata_capability(
    metadata: dict[str, Any],
    capability: str,
) -> bool | None:
    for source_name in ("declared", "published"):
        source = _metadata_record(metadata, source_name)
        if source is None:
            continue
        capabilities = source.get("capabilities")
        if not isinstance(capabilities, dict):
            continue
        value = capabilities.get(capability)
        if isinstance(value, bool):
            return value
    return None


def model_info_to_projection(model: dict[str, Any]) -> dict[str, Any]:
    """Project a provider ``ModelInfo`` into the stable public catalog row."""

    provider_id = str(model.get("provider", "") or "")
    model_id = str(model.get("model_id", "") or "")
    entry = _catalog.resolve_entry(model_id, provider=provider_id)
    capabilities: list[str] = ["chat"]
    context_window = model.get("context_window", 0)
    max_output_tokens = model.get("max_output_tokens", 0)
    metadata = (
        dict(model["metadata"]) if isinstance(model.get("metadata"), dict) else None
    )

    if (
        provider_id.strip().lower() == "tokenrhythm"
        and metadata is not None
        and metadata.get("schemaVersion") == 1
    ):
        context_window = _tokenrhythm_metadata_value(metadata, "contextWindow")
        max_output_tokens = _tokenrhythm_metadata_value(metadata, "maxOutputTokens")
        if context_window is None:
            context_window = (
                _positive_int(model.get("context_window")) or entry.context_window
            )
        if max_output_tokens is None:
            max_output_tokens = (
                _positive_int(model.get("max_output_tokens"))
                or entry.max_output_tokens
            )
        declared_tools = _tokenrhythm_metadata_capability(metadata, "tools")
        declared_vision = _tokenrhythm_metadata_capability(metadata, "vision")
        declared_reasoning = _tokenrhythm_metadata_capability(metadata, "reasoning")
        supports_tools = (
            declared_tools
            if declared_tools is not None
            else bool(model.get("supports_tools"))
        )
        supports_vision = (
            declared_vision
            if declared_vision is not None
            else bool(model.get("supports_vision"))
        )
        supports_reasoning = (
            False
            if declared_reasoning is False
            else bool(model.get("supports_reasoning"))
        )
        if supports_tools:
            capabilities.append("tools")
        if supports_reasoning:
            capabilities.append("reasoning")
        if supports_vision:
            capabilities.append("vision")
    elif model.get("supports_tools"):
        capabilities.append("tools")
    # Catalog/override knowledge of video input rides the resolved entry for
    # every provider (mirrors the tools/vision layering above; TokenRhythm's
    # declared/published metadata does not carry a video flag yet, so the
    # shared catalog row decides).
    if entry.supports_video:
        capabilities.append("video")

    return {
        "id": model_id,
        "name": model.get("display_name") or model_id,
        "provider": provider_id,
        "contextWindow": context_window,
        "maxOutputTokens": max_output_tokens,
        "capabilities": capabilities,
        "pricing": {
            "inputPer1k": model.get("input_cost_per_1k", 0.0),
            "outputPer1k": model.get("output_cost_per_1k", 0.0),
        },
        "source": entry.source,
        "reasoningFormat": entry.reasoning_format,
        "metadata": metadata,
    }


def _snapshot_config_for_selector_leg(config: Any) -> Any:
    return SimpleNamespace(
        llm=SimpleNamespace(
            provider=str(getattr(config, "provider", "") or ""),
            model=str(getattr(config, "model", "") or ""),
            api_key=str(getattr(config, "api_key", "") or ""),
            api_key_env="",
            base_url=str(getattr(config, "base_url", "") or ""),
            proxy=str(getattr(config, "proxy", "") or ""),
            provider_routing=dict(getattr(config, "provider_routing", {}) or {}),
        )
    )


def _supports_snapshot_resolver(list_models_detailed: Any) -> bool:
    try:
        return "snapshot_resolver" in inspect.signature(list_models_detailed).parameters
    except (TypeError, ValueError):
        return False


def model_list_error_to_projection(error: Any) -> dict[str, Any]:
    return {
        "provider": str(getattr(error, "provider", "")),
        "kind": str(getattr(error, "kind", "")),
        "detail": str(getattr(error, "detail", "")),
    }


class GatewayModelCatalogPort:
    def __init__(self, provider_selector: Any, config: Any) -> None:
        self._provider_selector = provider_selector
        self._config = config

    async def load_model_catalog(self) -> ModelCatalogResult:
        models: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        selector = self._provider_selector
        if selector is None or not getattr(selector, "is_configured", True):
            return ModelCatalogResult(models=[], errors=[])
        try:
            from opensquilla.provider.registry import (
                UnknownProviderError,
                get_provider_spec,
            )

            list_models_detailed = selector.list_models_detailed
            if _supports_snapshot_resolver(list_models_detailed):
                from opensquilla.gateway.model_catalog_refresh import (
                    cached_tokenrhythm_models,
                )

                def snapshot_resolver(config: Any) -> Any:
                    try:
                        spec = get_provider_spec(
                            str(getattr(config, "provider", "") or "")
                        )
                    except UnknownProviderError:
                        return None
                    if spec.live_catalog_shape != "tokenrhythm":
                        return None
                    return cached_tokenrhythm_models(
                        _snapshot_config_for_selector_leg(config)
                    )

                detailed = await list_models_detailed(
                    snapshot_resolver=snapshot_resolver
                )
                models = [model_info_to_projection(item) for item in detailed.models]
                errors = [
                    model_list_error_to_projection(item) for item in detailed.errors
                ]
            else:
                current = getattr(selector, "current_config", None)
                provider_id = str(getattr(current, "provider", "") or "")
                try:
                    spec = get_provider_spec(provider_id)
                except UnknownProviderError:
                    spec = None
                if spec is not None and spec.live_catalog_shape == "tokenrhythm":
                    from opensquilla.gateway.model_catalog_refresh import (
                        cached_tokenrhythm_models,
                    )

                    cached = cached_tokenrhythm_models(self._config)
                    models = [
                        model_info_to_projection(item.model_dump()) for item in cached
                    ]
                else:
                    detailed = await list_models_detailed()
                    models = [
                        model_info_to_projection(item) for item in detailed.models
                    ]
                    errors = [
                        model_list_error_to_projection(item) for item in detailed.errors
                    ]
        except Exception:
            pass
        return cast(ModelCatalogResult, {"models": models, "errors": errors})


class GatewayModelRoutingPolicyPort:
    """Translate domain routing intent into a detached config candidate."""

    def snapshot(self, config: Any) -> ModelRoutingSnapshot:
        return cast(ModelRoutingSnapshot, model_routing_public_snapshot(config))

    def prepare(self, config: Any, mode: str) -> PreparedModelRouting:
        patches = model_routing_patches(config, mode)
        candidate = config.model_copy(deep=True)
        apply_model_routing_mode(candidate, mode, activation_config=config)
        return PreparedModelRouting(candidate, tuple(patches))


class GatewayModelRoutingRuntimePort:
    """Reconcile a committed routing candidate with live Gateway state."""

    def __init__(self, provider_selector: Any, subscription_manager: Any) -> None:
        self._provider_selector = provider_selector
        self._subscription_manager = subscription_manager

    def prepare_reconciliation(self, config: Any) -> Any:
        # Resolve environment-backed provider values before persistence.  The
        # candidate tracks their provenance so sparse config writes never bake
        # runtime credentials into the durable file.
        return resolve_provider_selector_config(config)

    async def reconcile(self, config: Any, prepared: Any) -> None:
        if prepared is not None and hasattr(self._provider_selector, "sync_primary"):
            self._provider_selector.sync_primary(prepared)
        sync_media_runtime(config)

    async def publish_changed(
        self,
        previous: ModelRoutingSnapshot,
        config: Any,
        *,
        source: str,
    ) -> None:
        current = model_routing_public_snapshot(config)
        if current == previous or self._subscription_manager is None:
            return
        from opensquilla.gateway.event_bridge import EventBridge
        from opensquilla.gateway.scopes import READ_SCOPE
        from opensquilla.gateway.websocket import get_registry

        await EventBridge(
            self._subscription_manager,
            get_registry(),
        ).broadcast_scoped(
            "models.routing.changed",
            {**current, "source": source},
            required_scope=READ_SCOPE,
        )


class GatewayProviderStatusPort:
    """Provider status projection backed by concrete Gateway dependencies."""

    def __init__(
        self,
        *,
        config: Any,
        provider_selector: Any,
        provider_stats: Any,
    ) -> None:
        self._config = config
        self._provider_selector = provider_selector
        self._provider_stats = provider_stats

    async def load_provider_status(
        self,
        *,
        provider_id: str | None,
        probe_models: bool,
    ) -> ProviderStatusResult:
        return cast(
            ProviderStatusResult,
            await read_provider_status(
                config=self._config,
                provider_selector=self._provider_selector,
                provider_stats=self._provider_stats,
                provider_id=provider_id,
                probe_models=probe_models,
            ),
        )


__all__ = [
    "GatewayModelCatalogPort",
    "GatewayModelRoutingPolicyPort",
    "GatewayModelRoutingRuntimePort",
    "GatewayProviderStatusPort",
    "model_list_error_to_projection",
    "model_info_to_projection",
]
