"""RPC handlers for the tools domain."""

from __future__ import annotations

from typing import Any, cast

from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.search_status_runtime import read_search_status as _read_search_status
from opensquilla.sandbox.integration import (
    run_in_process_network_action,
)
from opensquilla.sandbox.types import DenialResult
from opensquilla.tools.builtin.web import (
    _search_plan_argv_token,
    get_active_provider,
    search_runtime_status,
)
from opensquilla.tools.builtin.web import (
    run_web_discover_payload as _run_web_discover_payload,
)
from opensquilla.tools.registry import get_default_registry
from opensquilla.tools.rpc_payload import (
    tools_catalog_payload,
    tools_effective_payload,
)

_d = get_dispatcher()


async def run_web_search_payload(
    query: str,
    max_results: int | None = None,
    *,
    provider: str | None = None,
) -> dict[str, Any]:
    """RPC hook for tests/managed-network wrapping; search.query stays discover-backed."""

    return await _run_web_discover_payload(
        query,
        max_results,
        provider_name=provider,
    )


@_d.method("tools.catalog", scope="operator.read")
async def _handle_tools_catalog(params: dict | None, ctx: RpcContext) -> dict:
    tool_registry = getattr(ctx, "tool_registry", None) or get_default_registry()
    return await tools_catalog_payload(
        params,
        tool_registry=tool_registry,
        session_manager=getattr(ctx, "session_manager", None),
        task_runtime=getattr(ctx, "task_runtime", None),
        scheduler=getattr(ctx, "cron_scheduler", None),
        gateway_config=getattr(ctx, "config", None),
        channel_manager=getattr(ctx, "channel_manager", None),
        originating_envelope=getattr(ctx, "originating_envelope", None),
        is_owner=ctx.principal.is_owner,
    )


@_d.method("tools.effective", scope="operator.read")
async def _handle_tools_effective(params: dict | None, ctx: RpcContext) -> dict:
    tool_registry = getattr(ctx, "tool_registry", None) or get_default_registry()
    return await tools_effective_payload(
        params,
        tool_registry=tool_registry,
        session_manager=getattr(ctx, "session_manager", None),
        task_runtime=getattr(ctx, "task_runtime", None),
        scheduler=getattr(ctx, "cron_scheduler", None),
        gateway_config=getattr(ctx, "config", None),
        channel_manager=getattr(ctx, "channel_manager", None),
        originating_envelope=getattr(ctx, "originating_envelope", None),
        is_owner=ctx.principal.is_owner,
    )


@_d.method("tools.search_provider", scope="operator.read")
async def _handle_tools_search_provider(params: dict | None, ctx: RpcContext) -> dict:
    return {"provider": get_active_provider()}


async def read_provider_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.application.provider_configuration import ProviderStatus
    from opensquilla.gateway.adapters.provider_configuration import (
        GatewayProviderStatusPort,
    )

    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    query = params or {}
    status = ProviderStatus(
        GatewayProviderStatusPort(
            config=ctx.config,
            provider_selector=ctx.provider_selector,
            provider_stats=ctx.provider_stats,
        )
    )
    return cast(
        dict[str, Any],
        await status.read(
            provider_id=query.get("provider"),
            probe_models=bool(query.get("probeModels", False)),
        ),
    )


async def _handle_providers_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return await read_provider_status(params, ctx)


async def read_search_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    provider = (params or {}).get("provider")
    return _read_search_status(str(provider) if provider else None)


@_d.method("search.status", scope="operator.read")
async def _handle_search_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    return await read_search_status(params, ctx)


def _query_limit(params: dict[str, Any]) -> int | None:
    if "limit" not in params or params.get("limit") is None:
        return None
    try:
        limit = int(params["limit"])
    except (TypeError, ValueError) as exc:
        raise ValueError("params.limit must be an integer") from exc
    if limit < 1 or limit > 20:
        raise ValueError("params.limit must be between 1 and 20")
    return limit


@_d.method("search.query", scope="operator.write")
async def _handle_search_query(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    query = str(params.get("query") or "").strip()
    if not query:
        raise ValueError("params.query is required")
    provider = params.get("provider")
    provider_name = str(provider) if provider else None
    if provider_name:
        search_runtime_status(provider_name)
    limit = _query_limit(params)

    async def _run_search() -> dict[str, Any]:
        return await run_web_search_payload(
            query,
            limit,
            provider=provider_name,
        )

    payload_or_denial = await run_in_process_network_action(
        action_kind="web.fetch",
        argv=(
            "web_search",
            query,
            str(limit or ""),
            _search_plan_argv_token(
                {"query": query, "provider": provider_name},
                tool_name="web_discover",
            ),
        ),
        callback=_run_search,
    )
    if isinstance(payload_or_denial, DenialResult):
        denial = payload_or_denial
        return {
            "ok": False,
            "query": query,
            "provider": provider_name or get_active_provider(),
            "results": [],
            "retry_allowed": False,
            "error": {
                "kind": denial.reason.value,
                "class": "SandboxDenied",
                "message": denial.message,
                "retryable": denial.retryable,
            },
        }

    payload = payload_or_denial
    error = payload.get("error")
    if payload.get("ok", False):
        result = {
            "ok": True,
            "query": payload.get("query", query),
            "provider": payload.get("provider", provider_name or get_active_provider()),
            "results": payload.get("results", []),
        }
        if payload.get("fallbackFrom"):
            result["fallbackFrom"] = payload.get("fallbackFrom")
        if payload.get("attempts") is not None:
            result["attempts"] = payload.get("attempts")
        return result
    if not isinstance(error, dict):
        error = {
            "kind": payload.get("error_kind", "unknown"),
            "class": payload.get("error_class", ""),
            "message": str(payload.get("error") or ""),
            "retryable": False,
        }
    result = {
        "ok": False,
        "query": payload.get("query", query),
        "provider": payload.get("provider", provider_name or get_active_provider()),
        "results": payload.get("results", []),
        "retry_allowed": False,
        "error": error,
    }
    if payload.get("attempts") is not None:
        result["attempts"] = payload.get("attempts")
    return result


# Generated descriptors own identity/scope/validation for contracted
# Platform configuration reads; search methods retain their existing handlers.
from opensquilla.gateway.adapters.platform_configuration_contract import (  # noqa: E402
    register_platform_configuration_contract,
)
from opensquilla.gateway.guest_rpc_policy import (  # noqa: E402
    is_guest_rpc_method_allowed,
)
from opensquilla.gateway.rpc import RpcHandlerError  # noqa: E402

_PLATFORM_CONFIGURATION_IMPLEMENTATIONS = {
    "providers.status": _handle_providers_status,
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
