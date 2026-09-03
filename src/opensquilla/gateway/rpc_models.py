"""RPC handlers for the models domain."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.provider.model_catalog import ModelCatalog

if TYPE_CHECKING:
    from opensquilla.application.provider_configuration import (
        ModelRouting as ApplicationModelRouting,
    )

_d = get_dispatcher()

# Keep the legacy, offline enrichment boundary for providers that do not own
# an authority-scoped metadata projection.  A gateway-warmed shared catalog
# must not silently alter source/reasoning fields on their ``models.list`` rows.
_catalog = ModelCatalog()


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
    """Resolve one numeric compatibility field without rewriting metadata."""

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
    """Keep TokenRhythm capability tri-state and explicit ``False`` priority."""

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


def _model_info_to_wire(m: dict[str, Any]) -> dict[str, Any]:
    """Convert a ModelInfo.model_dump() dict to the RPC wire format."""
    provider_id = str(m.get("provider", "") or "")
    model_id = str(m.get("model_id", "") or "")
    catalog = _catalog
    entry = catalog.resolve_entry(model_id, provider=provider_id)
    capabilities: list[str] = ["chat"]
    context_window = m.get("context_window", 0)
    max_output_tokens = m.get("max_output_tokens", 0)
    metadata = dict(m["metadata"]) if isinstance(m.get("metadata"), dict) else None

    if (
        provider_id.strip().lower() == "tokenrhythm"
        and metadata is not None
        and metadata.get("schemaVersion") == 1
    ):
        # The selector snapshot already resolved this ModelInfo against the
        # exact credential authority.  Missing normalized fields may fall back
        # to that row or the key-independent packaged catalog, never the shared
        # active authority's projection.
        entry = catalog.resolve_entry(model_id, provider=provider_id)
        # The authenticated declaration wins, then the public catalog. A
        # missing value falls through to the shared catalog/corrections layer;
        # the projected ModelInfo default must not mask that known fallback.
        context_window = _tokenrhythm_metadata_value(metadata, "contextWindow")
        max_output_tokens = _tokenrhythm_metadata_value(metadata, "maxOutputTokens")
        if context_window is None:
            context_window = _positive_int(m.get("context_window")) or entry.context_window
        if max_output_tokens is None:
            max_output_tokens = (
                _positive_int(m.get("max_output_tokens")) or entry.max_output_tokens
            )
        declared_tools = _tokenrhythm_metadata_capability(metadata, "tools")
        declared_vision = _tokenrhythm_metadata_capability(metadata, "vision")
        declared_reasoning = _tokenrhythm_metadata_capability(metadata, "reasoning")
        supports_tools = (
            declared_tools
            if declared_tools is not None
            else bool(m.get("supports_tools"))
        )
        supports_vision = (
            declared_vision
            if declared_vision is not None
            else bool(m.get("supports_vision"))
        )
        # A provider declaration cannot by itself choose a safe request
        # dialect. Explicit false still disables reasoning; true is exposed
        # only when the shared catalog also knows an executable dialect.
        supports_reasoning = (
            False
            if declared_reasoning is False
            else bool(m.get("supports_reasoning"))
        )
        if supports_tools:
            capabilities.append("tools")
        if supports_reasoning:
            capabilities.append("reasoning")
        if supports_vision:
            capabilities.append("vision")
    elif m.get("supports_tools"):
        capabilities.append("tools")

    # Providers can signal vision support via extra fields; keep extensible
    return {
        "id": model_id,
        "name": m.get("display_name") or model_id,
        "provider": provider_id,
        "contextWindow": context_window,
        "maxOutputTokens": max_output_tokens,
        "capabilities": capabilities,
        "pricing": {
            "inputPer1k": m.get("input_cost_per_1k", 0.0),
            "outputPer1k": m.get("output_cost_per_1k", 0.0),
        },
        # Catalog provenance; a model unknown to every layer still resolves
        # (source="synthesized") so the key is always present.
        "source": entry.source,
        "reasoningFormat": entry.reasoning_format,
        "metadata": metadata,
    }


def _snapshot_config_for_selector_leg(config: Any) -> Any:
    """Adapt one resolved selector leg to the coordinator's config reader."""

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
    """Preserve compatibility with selector-like test/plugin implementations."""

    try:
        return "snapshot_resolver" in inspect.signature(list_models_detailed).parameters
    except (TypeError, ValueError):
        return False


def _list_error_to_wire(err: Any) -> dict[str, Any]:
    """Convert a selector ProviderListError to the RPC wire format."""
    return {
        "provider": str(getattr(err, "provider", "")),
        "kind": str(getattr(err, "kind", "")),
        "detail": str(getattr(err, "detail", "")),
    }


async def _load_models(ctx: RpcContext) -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if ctx.provider_selector is not None and getattr(
        ctx.provider_selector, "is_configured", True
    ):
        try:
            from opensquilla.provider.registry import UnknownProviderError, get_provider_spec

            list_models_detailed = ctx.provider_selector.list_models_detailed
            if _supports_snapshot_resolver(list_models_detailed):
                from opensquilla.gateway.model_catalog_refresh import (
                    cached_tokenrhythm_models,
                )

                def snapshot_resolver(config: Any):
                    try:
                        spec = get_provider_spec(str(getattr(config, "provider", "") or ""))
                    except UnknownProviderError:
                        return None
                    if spec.live_catalog_shape != "tokenrhythm":
                        return None
                    # Returning even an empty list is authoritative: an
                    # ordinary read must never turn a TokenRhythm primary or
                    # fallback leg into hidden credentialed network I/O.
                    return cached_tokenrhythm_models(
                        _snapshot_config_for_selector_leg(config)
                    )

                detailed = await list_models_detailed(
                    snapshot_resolver=snapshot_resolver
                )
                models = [_model_info_to_wire(m) for m in detailed.models]
                errors = [_list_error_to_wire(e) for e in detailed.errors]
            else:
                # Compatibility path for selector-shaped integrations that
                # predate the additive callback. Keep the prior current-leg
                # TokenRhythm snapshot rule rather than passing its key to a
                # live list operation.
                current = getattr(ctx.provider_selector, "current_config", None)
                provider_id = str(getattr(current, "provider", "") or "")
                try:
                    spec = get_provider_spec(provider_id)
                except UnknownProviderError:
                    spec = None
                if spec is not None and spec.live_catalog_shape == "tokenrhythm":
                    from opensquilla.gateway.model_catalog_refresh import (
                        cached_tokenrhythm_models,
                    )

                    cached = cached_tokenrhythm_models(ctx.config)
                    models = [_model_info_to_wire(item.model_dump()) for item in cached]
                else:
                    detailed = await list_models_detailed()
                    models = [_model_info_to_wire(m) for m in detailed.models]
                    errors = [_list_error_to_wire(e) for e in detailed.errors]
        except Exception:
            pass

    return {"models": models, "errors": errors}


async def _handle_models_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.provider_configuration import ModelCatalog
    from opensquilla.gateway.adapters.provider_configuration import (
        RpcContextModelCatalogPort,
    )

    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    query = params or {}
    capabilities = query.get("capabilities")
    if capabilities is not None and not isinstance(capabilities, list):
        raise ValueError("params.capabilities must be an array")
    catalog = ModelCatalog(RpcContextModelCatalogPort(ctx, _load_models))
    return cast(
        dict[str, Any],
        await catalog.query(
            provider_id=query.get("provider"),
            capabilities=capabilities,
        ),
    )


def _model_routing(ctx: RpcContext) -> ApplicationModelRouting:
    from opensquilla.application.provider_configuration import ModelRouting
    from opensquilla.gateway.adapters.provider_configuration import (
        GatewayModelRoutingPolicyPort,
        RpcContextModelRoutingRuntimePort,
    )
    from opensquilla.gateway.adapters.setup_config import GatewaySetupConfigPort

    if ctx.config is None:
        raise ValueError("No config available")
    return ModelRouting(
        GatewaySetupConfigPort(ctx),
        GatewayModelRoutingPolicyPort(),
        RpcContextModelRoutingRuntimePort(ctx),
    )


async def _handle_models_routing_get(
    _params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    return cast(dict[str, Any], await _model_routing(ctx).read())


async def _handle_models_routing_set(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    if not isinstance(params, dict) or not isinstance(params.get("mode"), str):
        raise ValueError("params.mode is required")
    return cast(dict[str, Any], await _model_routing(ctx).set_mode(params["mode"]))


# Generated descriptors own identity/scope/validation for the contracted
# Platform configuration methods.
from opensquilla.gateway.adapters.platform_configuration_contract import (  # noqa: E402
    register_platform_configuration_contract,
)
from opensquilla.gateway.guest_rpc_policy import (  # noqa: E402
    is_guest_rpc_method_allowed,
)
from opensquilla.gateway.rpc import RpcHandlerError  # noqa: E402

_PLATFORM_CONFIGURATION_IMPLEMENTATIONS = {
    "models.list": _handle_models_list,
    "models.routing.get": _handle_models_routing_get,
    "models.routing.set": _handle_models_routing_set,
}

_PLATFORM_CONFIGURATION_CONTRACT_HANDLERS = {
    method: register_platform_configuration_contract(
        _d,
        method,
        implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
    for method, implementation in _PLATFORM_CONFIGURATION_IMPLEMENTATIONS.items()
}
