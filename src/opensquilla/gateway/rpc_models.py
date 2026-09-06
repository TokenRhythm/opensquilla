"""RPC handlers for the models domain."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from opensquilla.gateway.rpc import RpcContext, get_dispatcher

if TYPE_CHECKING:
    from opensquilla.application.provider_configuration import (
        ModelRouting as ApplicationModelRouting,
    )

_d = get_dispatcher()


async def _handle_models_list(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.provider_configuration import ModelCatalog
    from opensquilla.gateway.adapters.provider_configuration import (
        GatewayModelCatalogPort,
    )

    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    query = params or {}
    capabilities = query.get("capabilities")
    if capabilities is not None and not isinstance(capabilities, list):
        raise ValueError("params.capabilities must be an array")
    catalog = ModelCatalog(
        GatewayModelCatalogPort(ctx.provider_selector, ctx.config)
    )
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
        GatewayModelRoutingRuntimePort,
    )
    from opensquilla.gateway.adapters.setup_config import GatewaySetupConfigPort

    if ctx.config is None:
        raise ValueError("No config available")
    return ModelRouting(
        GatewaySetupConfigPort(ctx),
        GatewayModelRoutingPolicyPort(),
        GatewayModelRoutingRuntimePort(
            ctx.provider_selector,
            ctx.subscription_manager,
        ),
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
