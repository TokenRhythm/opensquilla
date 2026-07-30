"""RPC handlers for the models domain."""

from __future__ import annotations

from typing import Any

from opensquilla.gateway.model_routing import (
    model_routing_patches,
    model_routing_snapshot,
)
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.provider.model_catalog import ModelCatalog

_d = get_dispatcher()

# Offline layered catalog (corrections + snapshot + synthesized fallback) used
# only to enrich rows with provenance; ``resolve_entry`` never fails and never
# touches the network.
_catalog = ModelCatalog()


def _model_info_to_wire(m: dict[str, Any]) -> dict[str, Any]:
    """Convert a ModelInfo.model_dump() dict to the RPC wire format."""
    capabilities: list[str] = ["chat"]
    if m.get("supports_tools"):
        capabilities.append("tools")
    entry = _catalog.resolve_entry(m.get("model_id", ""), provider=m.get("provider", ""))
    # Providers can signal vision support via extra fields; keep extensible
    return {
        "id": m.get("model_id", ""),
        "name": m.get("display_name") or m.get("model_id", ""),
        "provider": m.get("provider", ""),
        "contextWindow": m.get("context_window", 0),
        "capabilities": capabilities,
        "pricing": {
            "inputPer1k": m.get("input_cost_per_1k", 0.0),
            "outputPer1k": m.get("output_cost_per_1k", 0.0),
        },
        # Catalog provenance; a model unknown to every layer still resolves
        # (source="synthesized") so the key is always present.
        "source": entry.source,
        "reasoningFormat": entry.reasoning_format,
    }


def _list_error_to_wire(err: Any) -> dict[str, Any]:
    """Convert a selector ProviderListError to the RPC wire format."""
    return {
        "provider": str(getattr(err, "provider", "")),
        "kind": str(getattr(err, "kind", "")),
        "detail": str(getattr(err, "detail", "")),
    }


@_d.method("models.list", scope="operator.read")
async def _handle_models_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    provider_filter = (params or {}).get("provider")
    capabilities_filter: list[str] | None = (params or {}).get("capabilities")

    models: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if ctx.provider_selector is not None and getattr(
        ctx.provider_selector, "is_configured", True
    ):
        try:
            detailed = await ctx.provider_selector.list_models_detailed()
            models = [_model_info_to_wire(m) for m in detailed.models]
            errors = [_list_error_to_wire(e) for e in detailed.errors]
        except Exception:
            pass

    if provider_filter:
        models = [m for m in models if m["provider"] == provider_filter]

    if capabilities_filter:
        required = set(capabilities_filter)
        models = [m for m in models if required.issubset(set(m["capabilities"]))]

    return {"models": models, "errors": errors}


@_d.method("models.routing.get", scope="operator.read")
async def _handle_models_routing_get(
    _params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    if ctx.config is None:
        raise ValueError("No config available")
    return model_routing_snapshot(ctx.config)


@_d.method("models.routing.set", scope="operator.write")
async def _handle_models_routing_set(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    if not isinstance(params, dict) or not isinstance(params.get("mode"), str):
        raise ValueError("params.mode is required")
    if ctx.config is None:
        raise ValueError("No config available")

    # Reuse the safe write transaction so persistence, validation, runtime
    # synchronization, and old config.patch.safe clients keep one contract.
    from opensquilla.gateway.rpc_config import _handle_config_patch_safe

    patch_result = await _handle_config_patch_safe(
        {"patches": model_routing_patches(ctx.config, params["mode"])},
        ctx,
    )
    return {
        **model_routing_snapshot(ctx.config),
        "patched": list(patch_result.get("patched") or []),
        "restart_required": bool(
            patch_result.get("restartRequired", patch_result.get("restart_required", False))
        ),
    }
