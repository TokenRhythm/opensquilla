"""RPC handlers for the tools domain."""

from __future__ import annotations

import os
import re
from typing import Any

from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.sandbox.integration import run_in_process_network_action
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


def _active_llm_provider(ctx: RpcContext) -> str | None:
    selector = getattr(ctx, "provider_selector", None)
    current_config = getattr(selector, "current_config", None)
    provider = getattr(current_config, "provider", None)
    if provider:
        return str(provider)
    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    provider = getattr(llm_cfg, "provider", None)
    return str(provider) if provider else None


def _provider_api_key_env(provider_id: str, default_env_key: str, ctx: RpcContext) -> str:
    active = provider_id == _active_llm_provider(ctx)
    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    if active:
        configured_env = str(getattr(llm_cfg, "api_key_env", "") or "")
        if configured_env:
            return configured_env
    return default_env_key


def _provider_key_material(provider_id: str, env_key: str, ctx: RpcContext) -> str:
    """Resolve a row's configured key material: active config key, then env."""
    active = provider_id == _active_llm_provider(ctx)
    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    key_value = ""
    if active:
        key_value = str(getattr(llm_cfg, "api_key", "") or "")
    if not key_value and env_key:
        key_value = os.environ.get(env_key, "") or ""
    return key_value


def _provider_key_configured(provider_id: str, env_key: str, ctx: RpcContext) -> bool:
    return bool(_provider_key_material(provider_id, env_key, ctx))


_URL_SHAPED_KEY_RE = re.compile(r"^https?://")
_ENV_NAME_SHAPED_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_OPENSQUILLA_ENV_NAME_RE = re.compile(r"^OPENSQUILLA_[A-Z0-9_]+$")
_ENV_NAME_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY_ENV")


def _api_key_shape(key_value: str, *, expected_env_name: str = "") -> str:
    """Classify obviously-misconfigured key material without emitting it.

    Only the classification label ever leaves this function; the key value
    itself is never logged or included in any payload. ``looks_like_env_name``
    is deliberately narrow — legitimate all-uppercase keys exist — so it fires
    only on shell-paste ``$NAME`` values and on env-name-shaped values that
    equal the row's expected env var name, are ``OPENSQUILLA_*`` names, or
    carry a well-known env-key suffix.
    """
    value = key_value.strip()
    if not value:
        return "ok"
    if _URL_SHAPED_KEY_RE.match(value):
        return "looks_like_url"
    if value.startswith("$"):
        return "looks_like_env_name"
    if _ENV_NAME_SHAPED_KEY_RE.match(value) and (
        (expected_env_name and value == expected_env_name)
        or _OPENSQUILLA_ENV_NAME_RE.match(value)
        or value.endswith(_ENV_NAME_SUFFIXES)
    ):
        return "looks_like_env_name"
    return "ok"


def _provider_api_key_shape(provider_id: str, env_key: str, ctx: RpcContext) -> str:
    """Shape of the key material resolved by ``_provider_key_material``."""
    return _api_key_shape(
        _provider_key_material(provider_id, env_key, ctx),
        expected_env_name=env_key,
    )


def _provider_base_url(provider_id: str, default_base_url: str, ctx: RpcContext) -> str:
    active = provider_id == _active_llm_provider(ctx)
    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    configured_base_url = getattr(llm_cfg, "base_url", None)
    if active and configured_base_url:
        return str(configured_base_url)
    return default_base_url


async def _model_probe(provider_id: str, ctx: RpcContext) -> dict[str, Any]:
    selector = getattr(ctx, "provider_selector", None)
    if selector is None or not getattr(selector, "is_configured", True):
        return {
            "attempted": True,
            "status": "unavailable",
            "count": 0,
            "error": "No provider selector configured",
        }
    try:
        rows = await selector.list_models()
        matching = [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("provider") or "") == provider_id
        ]
        return {"attempted": True, "status": "ok", "count": len(matching), "error": None}
    except Exception as exc:  # noqa: BLE001 - diagnostic surface
        return {"attempted": True, "status": "error", "count": 0, "error": str(exc)}


@_d.method("providers.status", scope="operator.read")
async def _handle_providers_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.onboarding.provider_specs import list_provider_setup_specs
    from opensquilla.provider.selector import ProviderBuildError, build_provider

    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    provider_filter = (params or {}).get("provider")
    probe_models = bool((params or {}).get("probeModels", False))

    specs = list_provider_setup_specs()
    by_id = {spec.provider_id: spec for spec in specs}
    if provider_filter:
        provider_filter = str(provider_filter)
        if provider_filter not in by_id:
            raise ValueError(f"Unknown provider: {provider_filter}")
        specs = [by_id[provider_filter]]

    active = _active_llm_provider(ctx)
    llm_cfg = getattr(getattr(ctx, "config", None), "llm", None)
    rows: list[dict[str, Any]] = []
    for spec in specs:
        is_active = spec.provider_id == active
        api_key_env = _provider_api_key_env(spec.provider_id, spec.env_key, ctx)
        api_key_configured = _provider_key_configured(spec.provider_id, api_key_env, ctx)
        api_key_shape = _provider_api_key_shape(spec.provider_id, api_key_env, ctx)
        base_url = _provider_base_url(spec.provider_id, spec.default_base_url, ctx)
        base_url_configured = bool(base_url)
        configured = (
            spec.runtime_supported
            and (not spec.requires_api_key or api_key_configured)
            and (not spec.requires_base_url or base_url_configured)
        )
        model = str(getattr(llm_cfg, "model", "") or "") if is_active else ""
        api_key = str(getattr(llm_cfg, "api_key", "") or "") if is_active else ""
        if is_active and not api_key and api_key_env:
            api_key = os.environ.get(api_key_env, "")
        error: str | None = None
        buildable = False
        try:
            build_provider(
                spec.provider_id,
                model or "diagnostic-model",
                api_key=api_key,
                base_url=base_url,
            )
            buildable = True
        except ProviderBuildError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - diagnostic surface
            error = str(exc)
        probe = (
            await _model_probe(spec.provider_id, ctx)
            if probe_models and is_active
            else {"attempted": False, "status": "skipped", "count": 0, "error": None}
        )
        rows.append(
            {
                "providerId": spec.provider_id,
                "active": is_active,
                "configured": configured,
                "buildable": buildable,
                "model": model,
                "requiresApiKey": spec.requires_api_key,
                "apiKeyEnv": api_key_env,
                "apiKeyConfigured": api_key_configured,
                "apiKeyShape": api_key_shape,
                "baseUrlConfigured": base_url_configured,
                "error": error,
                "modelProbe": probe,
                "latency": (
                    ctx.provider_stats.snapshot(spec.provider_id)
                    if getattr(ctx, "provider_stats", None)
                    else None
                ),
            }
        )
    return {"activeProvider": active, "providers": rows, "count": len(rows)}


@_d.method("search.status", scope="operator.read")
async def _handle_search_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    provider = (params or {}).get("provider")
    return search_runtime_status(str(provider) if provider else None)


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
