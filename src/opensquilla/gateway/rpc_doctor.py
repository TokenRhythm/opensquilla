"""Unified readiness doctor RPC."""

from __future__ import annotations

import re
from typing import Any, cast

from opensquilla.application.observability import (
    _COLLECTION_INSPECT_COMMANDS as _APPLICATION_COLLECTION_INSPECT_COMMANDS,
)
from opensquilla.application.observability import (
    ReadinessDataPort,
    ReadinessDiagnostics,
    ReadinessFinding,
    ReadinessQuery,
)
from opensquilla.application.provider_configuration import ProviderStatus
from opensquilla.gateway.adapters.observability import (
    GatewayReadinessReportPort,
    evaluate_readiness_surface,
)
from opensquilla.gateway.adapters.observability_contract import (
    register_observability_contract,
)
from opensquilla.gateway.adapters.provider_configuration import GatewayProviderStatusPort
from opensquilla.gateway.channel_status_runtime import read_channel_status
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.log_status_runtime import read_log_status
from opensquilla.gateway.memory_status_runtime import read_memory_status
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.search_status_runtime import read_search_status
from opensquilla.sandbox.status import status_payload as _sandbox_status_payload

_d = get_dispatcher()

# Compatibility export for recovery-command consumers; the Application Module
# remains the single owner of the command mapping.
_COLLECTION_INSPECT_COMMANDS = _APPLICATION_COLLECTION_INSPECT_COMMANDS

_UNKNOWN_SEARCH_PROVIDER_RE = re.compile(
    r"Unknown search provider ['\"]([^'\"]+)['\"]"
    r"|unknown search provider: ['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


def _build_logs_status(ctx: RpcContext) -> dict[str, Any]:
    return read_log_status(
        config=getattr(ctx, "config", None),
        diagnostics_state=getattr(ctx, "diagnostics_state", None),
    )


def _config_path(ctx: RpcContext) -> str | None:
    config = getattr(ctx, "config", None)
    value = getattr(config, "config_path", None)
    return str(value) if value else None


def _unknown_search_provider(exc: Exception) -> str:
    message = str(exc)
    match = _UNKNOWN_SEARCH_PROVIDER_RE.search(message)
    if not match:
        return "unknown"
    return next(group for group in match.groups() if group) or "unknown"


def _search_api_key_env(ctx: RpcContext, payload: dict[str, Any]) -> str:
    config = getattr(ctx, "config", None)
    configured_env = str(getattr(config, "search_api_key_env", "") or "")
    if configured_env:
        return configured_env
    provider = str(payload.get("provider") or payload.get("activeProvider") or "")
    if not provider:
        return ""
    try:
        from opensquilla.search.registry import get_provider_spec

        return str(get_provider_spec(provider).env_key or "")
    except Exception:  # noqa: BLE001 - unknown search providers are reported separately.
        return ""


async def _search_payload(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    del params
    try:
        payload = cast(dict[str, Any], await _search_runtime_payload({}, ctx))
        payload.setdefault("apiKeyEnv", _search_api_key_env(ctx, payload))
        return payload
    except (KeyError, ValueError) as exc:
        provider = _unknown_search_provider(exc)
        return {
            "activeProvider": provider,
            "provider": provider,
            "apiKeyEnv": "",
            "unknownProvider": True,
            "configured": False,
            "runtimeSupported": False,
            "requiresApiKey": False,
            "apiKeyConfigured": False,
            "buildable": False,
            "error": str(exc),
        }


async def _search_runtime_payload(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    del ctx
    provider = (params or {}).get("provider")
    return read_search_status(str(provider) if provider else None)


async def _provider_payload(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    query = params or {}
    return cast(
        dict[str, Any],
        await ProviderStatus(
            GatewayProviderStatusPort(
                config=ctx.config,
                provider_selector=ctx.provider_selector,
                provider_stats=ctx.provider_stats,
            )
        ).read(
            provider_id=query.get("provider"),
            probe_models=bool(query.get("probeModels", False)),
        ),
    )


async def _readiness_provider(
    query: ReadinessQuery,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Collect the provider projection from one typed readiness query."""

    return await _provider_payload({"probeModels": query.probe_providers}, ctx)


async def _readiness_memory(
    query: ReadinessQuery,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Collect memory health from explicit runtime dependencies."""

    return await read_memory_status(
        {"agentId": query.agent_id, "deep": query.deep},
        memory_backend=getattr(ctx, "memory_backend", None),
        memory_managers=getattr(ctx, "memory_managers", None),
        session_manager=getattr(ctx, "session_manager", None),
    )


async def _channel_payload(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    del params
    from opensquilla.gateway.boot import _boot_id

    return await read_channel_status(
        config=getattr(ctx, "config", None),
        channel_manager=getattr(ctx, "channel_manager", None),
        boot_id=_boot_id,
    )


def _sandbox_payload(ctx: RpcContext) -> dict[str, Any]:
    config = getattr(ctx, "config", None)
    if config is None:
        return {
            "posture": "unknown",
            "sandbox": {"sandbox": False, "security_grading": False},
            "permissions": {"default_mode": "unknown"},
            "restart_required": False,
        }
    return _sandbox_status_payload(config, restart_required=False)


def _image_generation_payload(ctx: RpcContext) -> dict[str, Any]:
    config = getattr(ctx, "config", None)
    if config is None:
        return {
            "enabled": False,
            "configured": False,
            "status": "optional",
            "provider": "",
            "primary": "",
            "source": "none",
            "apiKeyEnv": "",
            "configPath": None,
        }

    from opensquilla.onboarding.status import get_onboarding_status

    status = get_onboarding_status(config)
    section_status = status.sections.get("image_generation")
    status_value = getattr(section_status, "value", str(section_status or "unknown"))
    provider = status.image_generation_provider
    primary = status.image_generation_primary
    if not provider and "/" in primary:
        provider = primary.split("/", 1)[0]
    return {
        "enabled": status.image_generation_enabled,
        "configured": status.image_generation_configured,
        "status": status_value,
        "provider": status.image_generation_provider,
        "primary": primary,
        "source": status.image_generation_source,
        "apiKeyEnv": _image_generation_api_key_env(config, provider),
        "configPath": status.config_path,
    }


def _image_generation_api_key_env(config: Any, provider: str) -> str:
    if not provider:
        return ""
    provider_id = provider.strip().lower()
    try:
        from opensquilla.onboarding.image_generation_specs import (
            get_image_generation_provider_setup_spec,
        )

        spec = get_image_generation_provider_setup_spec(provider_id)
    except KeyError:
        return ""
    providers = getattr(getattr(config, "image_generation", None), "providers", None)
    provider_cfg = getattr(providers, provider_id, None) if providers is not None else None
    configured_env = str(getattr(provider_cfg, "api_key_env", "") or "")
    return configured_env or str(spec.env_key or "")


def _router_payload(ctx: RpcContext, *, deep: bool = False) -> dict[str, Any]:
    config = cast(GatewayConfig | None, getattr(ctx, "config", None))
    if config is None:
        return {
            "enabled": False,
            "rolloutPhase": "unknown",
            "strategy": "unknown",
            "tierProfile": "custom",
            "defaultTier": None,
            "runtimeValid": True,
            "requireRouterRuntime": False,
            "runtimeErrorKind": None,
            "routerProviderRoles": {},
        }

    router = config.squilla_router
    if router is None:
        return {
            "enabled": False,
            "rolloutPhase": "unknown",
            "strategy": "unknown",
            "tierProfile": "custom",
            "defaultTier": None,
            "runtimeValid": True,
            "requireRouterRuntime": False,
            "runtimeErrorKind": None,
            "routerProviderRoles": {},
        }

    runtime_valid = True
    error: str | None = None
    runtime_error_kind: str | None = None
    try:
        from opensquilla.gateway.boot import (
            validate_squilla_router_runtime,
            validate_squilla_router_runtime_deep,
        )

        if deep:
            validate_squilla_router_runtime_deep(config)
        else:
            validate_squilla_router_runtime(config)
    except Exception as exc:  # noqa: BLE001 - doctor turns runtime validation into guidance.
        from opensquilla.router_runtime_diagnostics import classify_router_runtime_error

        runtime_valid = False
        error = str(exc)
        runtime_error_kind = classify_router_runtime_error(exc)

    active_provider = str(getattr(getattr(config, "llm", None), "provider", "") or "")
    mismatched_tier_providers: dict[str, str] = {}
    tiers = getattr(router, "tiers", {}) or {}
    from opensquilla.router_tiers import (
        TierConfig,
        effective_ensemble_selection_mode,
        router_dynamic_tier_members_active,
        router_tier_provider_roles,
        tier_provider_role,
    )

    shared_selection_mode = effective_ensemble_selection_mode(config)
    ensemble_globally_enabled = bool(
        getattr(getattr(config, "llm_ensemble", None), "enabled", False)
    )
    provider_roles = router_tier_provider_roles(
        tiers if isinstance(tiers, dict) else {},
        shared_selection_mode=shared_selection_mode,
        ensemble_globally_enabled=ensemble_globally_enabled,
    )
    dynamic_members_active = router_dynamic_tier_members_active(
        tiers if isinstance(tiers, dict) else {},
        shared_selection_mode=shared_selection_mode,
        ensemble_globally_enabled=ensemble_globally_enabled,
    )
    if isinstance(tiers, dict) and active_provider.strip():
        active_l = active_provider.strip().lower()
        for tier_name, tier_value in tiers.items():
            tier = TierConfig.from_value(tier_value)
            provider_role = tier_provider_role(
                tier_name,
                tier_value,
                shared_selection_mode=shared_selection_mode,
                router_dynamic_members_active=dynamic_members_active,
                ensemble_globally_enabled=ensemble_globally_enabled,
            )
            if (
                provider_role in {"direct", "dynamic_member"}
                and tier.provider
                and tier.provider.lower() != active_l
            ):
                mismatched_tier_providers[str(tier_name)] = tier.provider

    return {
        "enabled": bool(getattr(router, "enabled", False)),
        "rolloutPhase": getattr(router, "rollout_phase", None),
        "strategy": getattr(router, "strategy", None),
        "tierProfile": getattr(router, "tier_profile", None),
        "defaultTier": getattr(router, "default_tier", None),
        "runtimeValid": runtime_valid,
        "requireRouterRuntime": bool(getattr(router, "require_router_runtime", False)),
        "runtimeErrorKind": runtime_error_kind,
        "error": error,
        "activeProvider": active_provider,
        "crossProviderTiers": bool(getattr(router, "cross_provider_tiers", False)),
        "tierProviderMismatch": str(getattr(router, "tier_provider_mismatch", "route") or "route"),
        "mismatchedTierProviders": mismatched_tier_providers,
        "routerProviderRoles": provider_roles,
    }


def _squilla_router_runtime_payload(ctx: RpcContext) -> dict[str, Any]:
    """Live router runtime load outcome from the turn loop's strategy cache.

    Complements ``_router_payload`` (config/asset re-validation) with what is
    actually serving turns. This collector only reads: the strategy cache is
    populated by the gateway's boot-time background preload, the first routed
    turn, or the router surface's deep validation (which runs just before
    this collector). Until one of those lands the payload reports
    ``initialized=False`` and the evaluator stays silent.
    """
    config = getattr(ctx, "config", None)
    router = getattr(config, "squilla_router", None) if config is not None else None
    payload: dict[str, Any] = {
        "enabled": bool(getattr(router, "enabled", False)),
        "requireRouterRuntime": bool(getattr(router, "require_router_runtime", False)),
    }
    if not payload["enabled"]:
        return payload
    from opensquilla.engine.steps.squilla_router import router_runtime_status

    payload.update(router_runtime_status())
    return payload


def _llm_ensemble_payload(ctx: RpcContext) -> dict[str, Any]:
    config = getattr(ctx, "config", None)
    if config is None:
        return {
            "enabled": False,
            "selectionMode": "",
            "activeProvider": "",
            "runtimeStatus": "disabled",
            "configurationReady": None,
            "configuredAllFailedPolicy": "fallback_single",
            "effectiveAllFailedPolicy": "fallback_single",
            "policyDeprecated": False,
            "tierEnsembleStatuses": {},
        }

    from opensquilla.provider.ensemble import (
        ensemble_runtime_status,
        tier_ensemble_runtime_statuses,
    )
    from opensquilla.router_tiers import (
        CUSTOM_B5_SELECTION_MODE,
        static_b5_profile,
    )

    def decorate(runtime: dict[str, Any]) -> dict[str, Any]:
        decorated = {
            **runtime,
            "activeProvider": str(getattr(config.llm, "provider", "") or ""),
        }
        static_profile = static_b5_profile(str(decorated["selectionMode"]))
        if static_profile is not None and decorated["enabled"]:
            from opensquilla.provider.registry import get_provider_spec

            decorated["memberProvider"] = static_profile.provider_id
            decorated["apiKeyEnv"] = str(
                get_provider_spec(static_profile.provider_id).env_key or ""
            )
            decorated["credentialAvailable"] = bool(decorated["configurationReady"])
        elif decorated["enabled"] and decorated["selectionMode"] == CUSTOM_B5_SELECTION_MODE:
            decorated["lineupReady"] = bool(decorated["configurationReady"])
            decorated["lineupBlockedReason"] = str(decorated["blockedReason"] or "")
        return decorated

    payload = decorate(ensemble_runtime_status(config))
    configured_policy = str(
        getattr(config.llm_ensemble, "all_failed_policy", "fallback_single") or "fallback_single"
    ).strip()
    payload.setdefault("configuredAllFailedPolicy", configured_policy)
    payload.setdefault("effectiveAllFailedPolicy", configured_policy)
    payload.setdefault("policyDeprecated", False)
    payload["tierEnsembleStatuses"] = {
        tier: decorate(runtime) for tier, runtime in tier_ensemble_runtime_statuses(config).items()
    }
    return payload


def _memory_embedding_payload(ctx: RpcContext) -> dict[str, Any]:
    config = getattr(ctx, "config", None)
    memory_config = getattr(config, "memory", None) if config is not None else None
    if memory_config is None:
        return {
            "status": "fts_only",
            "requestedProvider": "none",
            "effectiveProvider": "none",
            "model": "fts-only",
            "retrievalMode": "fts_only",
            "reason": "memory_unavailable",
        }

    embed_cfg = getattr(memory_config, "embedding", None)
    requested = str(getattr(embed_cfg, "requested_provider", "auto") or "auto")
    retrieval_mode = str(getattr(memory_config, "retrieval_mode", "hybrid") or "hybrid")
    try:
        from opensquilla.memory.embedding_resolver import resolve_memory_embedding

        decision = resolve_memory_embedding(memory_config)
    except Exception as exc:  # noqa: BLE001 - doctor reports config interpretation failures.
        return {
            "status": "error",
            "requestedProvider": requested,
            "effectiveProvider": "none",
            "model": "",
            "retrievalMode": retrieval_mode,
            "error": str(exc),
        }

    effective = str(decision.effective_provider)
    return {
        "status": "fts_only" if effective == "none" else "ready",
        "requestedProvider": decision.requested_provider,
        "effectiveProvider": effective,
        "model": decision.model,
        "retrievalMode": retrieval_mode,
        "reason": decision.reason,
    }


async def _doctor_status_contract(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = params or {}
    port = _GatewayReadinessRuntime(ctx)
    return cast(
        dict[str, Any],
        await ReadinessDiagnostics(port, GatewayReadinessReportPort()).assess(
            ReadinessQuery(
                agent_id=str(params.get("agentId") or "main"),
                deep=bool(params.get("deep", True)),
                probe_providers=bool(params.get("probeProviders", False)),
            ),
            connection_id=ctx.conn_id,
            config_path=_config_path(ctx),
        ),
    )


class _GatewayReadinessRuntime(ReadinessDataPort):
    """Compose doctor data from domain projections, never other RPC handlers."""

    def __init__(self, ctx: RpcContext) -> None:
        self._ctx = ctx

    @staticmethod
    def _findings(
        surface: str,
        payload: dict[str, Any],
    ) -> tuple[ReadinessFinding, ...]:
        return evaluate_readiness_surface(surface, payload)

    async def provider(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return self._findings("provider", await _readiness_provider(query, self._ctx))

    async def logs(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        del query
        return self._findings("logs", _build_logs_status(self._ctx))

    async def memory(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return self._findings("memory", await _readiness_memory(query, self._ctx))

    async def channels(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        del query
        return self._findings("channels", await _channel_payload(None, self._ctx))

    async def sandbox(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        del query
        return self._findings("sandbox", _sandbox_payload(self._ctx))

    async def router(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        return self._findings("router", _router_payload(self._ctx, deep=bool(query.deep)))

    async def squilla_router(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        del query
        return self._findings("squilla_router", _squilla_router_runtime_payload(self._ctx))

    async def memory_embedding(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        del query
        return self._findings("memory_embedding", _memory_embedding_payload(self._ctx))

    async def search(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        del query
        return self._findings("search", await _search_payload(None, self._ctx))

    async def image_generation(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        del query
        return self._findings("image_generation", _image_generation_payload(self._ctx))

    async def llm_ensemble(self, query: ReadinessQuery) -> tuple[ReadinessFinding, ...]:
        del query
        return self._findings("llm_ensemble", _llm_ensemble_payload(self._ctx))


_handle_doctor_status = register_observability_contract(
    _d,
    "doctor.status",
    _doctor_status_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
