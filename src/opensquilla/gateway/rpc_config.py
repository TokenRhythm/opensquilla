"""RPC parsing and projection for the configuration application owner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from opensquilla.application.app_settings import AppSettings, SettingChange
from opensquilla.gateway.adapters.app_settings import GatewayAppSettingsPort
from opensquilla.gateway.rpc import RpcContext, get_dispatcher

if TYPE_CHECKING:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.provider.selector import ProviderConfig

_d = get_dispatcher()


def _app_settings(
    ctx: RpcContext, *, source: str = "config.patch"
) -> AppSettings[GatewayConfig, ProviderConfig | None]:
    return AppSettings(
        GatewayAppSettingsPort(
            ctx.config,
            task_runtime=getattr(ctx, "task_runtime", None),
            provider_selector=getattr(ctx, "provider_selector", None),
            subscription_manager=getattr(ctx, "subscription_manager", None),
            source=source,
        )
    )


@_d.method("config.set", scope="operator.admin")
async def _handle_config_set(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict) or "path" not in params or "value" not in params:
        raise ValueError("params.path and params.value are required")
    return cast(
        dict[str, Any],
        await _app_settings(ctx, source="config.set").set(params["path"], params["value"]),
    )


async def _handle_config_patch(
    params: dict | None, ctx: RpcContext, *, _model_routing_source: str = "config.patch"
) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params.patch or params.patches is required")
    patch_data = params.get("patch") or {}
    dot_patches = params.get("patches") or {}
    if not isinstance(patch_data, dict) or not isinstance(dot_patches, dict):
        raise ValueError("params.patch and params.patches must be objects")
    if not patch_data and not dot_patches:
        raise ValueError("params.patch or params.patches is required")
    settings = _app_settings(ctx, source=_model_routing_source)
    if patch_data and dot_patches:
        result = await settings.patch_combined(patch_data, dot_patches)
    elif patch_data:
        result = await settings.merge(patch_data)
    else:
        result = await settings.patch(
            [SettingChange(path, value) for path, value in dot_patches.items()]
        )
    return cast(dict[str, Any], result)


async def _handle_config_patch_safe(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params.patches is required")
    if params.get("patch") or {}:
        raise ValueError("params.patch is not supported for safe config patch")
    dot_patches = params.get("patches") or {}
    if not dot_patches:
        raise ValueError("params.patches is required")
    if not isinstance(dot_patches, dict):
        raise ValueError("params.patches must be an object")
    return cast(
        dict[str, Any],
        await _app_settings(ctx, source="config.patch.safe").patch_safe(
            [SettingChange(path, value) for path, value in dot_patches.items()]
        ),
    )


@_d.method("config.apply", scope="operator.admin")
async def _handle_config_apply(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params.config is required")
    payload = params.get("config")
    if payload is None and "config_yaml" in params:
        import yaml  # type: ignore[import-untyped]

        payload = yaml.safe_load(params["config_yaml"]) or {}
    if not isinstance(payload, dict):
        raise ValueError("params.config is required")
    return cast(dict[str, Any], await _app_settings(ctx, source="config.apply").apply(payload))


@_d.method("config.reload", scope="operator.admin")
async def _handle_config_reload(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return cast(dict[str, Any], await _app_settings(ctx, source="config.reload").reload())


async def _handle_config_effective(_params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return cast(dict[str, Any], await _app_settings(ctx).read_effective())


@_d.method("config.schema", scope="operator.admin")
async def _handle_config_schema(params: dict | None, ctx: RpcContext) -> dict:
    from opensquilla.gateway.config import GatewayConfig

    schema = GatewayConfig.model_json_schema()

    if isinstance(params, dict) and params.get("section"):
        section = params["section"]
        # Navigate into $defs or properties
        props = schema.get("properties", {})
        if section in props:
            return {"schema": props[section]}
        defs = schema.get("$defs", {})
        if section in defs:
            return {"schema": defs[section]}
        raise KeyError(f"Schema section not found: {section}")

    return {"schema": schema}


@_d.method("config.schema.lookup", scope="operator.read")
async def _handle_config_schema_lookup(params: dict | None, ctx: RpcContext) -> dict:
    if not isinstance(params, dict) or "path" not in params:
        raise ValueError("params.path is required")

    from opensquilla.gateway.config import GatewayConfig

    schema = GatewayConfig.model_json_schema()
    path = params["path"]
    parts = path.split(".")

    # Walk through the schema tree resolving $ref along the way
    node: dict = schema
    for part in parts:
        props = node.get("properties", {})
        if part in props:
            node = props[part]
            # Resolve $ref if present
            ref = node.get("$ref")
            if ref and ref.startswith("#/$defs/"):
                def_name = ref.split("/")[-1]
                node = schema.get("$defs", {}).get(def_name, node)
        else:
            raise KeyError(f"Schema path not found: {path}")

    return {
        "path": path,
        "type": node.get("type", "object"),
        "description": node.get("description"),
        "default": node.get("default"),
        "enum": node.get("enum"),
    }


# Generated descriptors own identity/scope/validation for the contracted
# Platform configuration methods. Keep the implementations importable for
# compatibility tests and route all runtime registration through one seam.
from opensquilla.gateway.adapters.platform_configuration_contract import (  # noqa: E402
    register_platform_configuration_contract,
)
from opensquilla.gateway.guest_rpc_policy import (  # noqa: E402
    is_guest_rpc_method_allowed,
)
from opensquilla.gateway.rpc import RpcHandlerError  # noqa: E402

_PLATFORM_CONFIGURATION_IMPLEMENTATIONS = {
    "config.patch": _handle_config_patch,
    "config.patch.safe": _handle_config_patch_safe,
    "config.effective": _handle_config_effective,
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
