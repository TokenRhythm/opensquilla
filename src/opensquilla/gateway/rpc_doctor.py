"""Unified readiness doctor RPC."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, cast

from opensquilla.gateway.config import (
    STATIC_B5_SELECTION_MODE_PROVIDERS,
    GatewayConfig,
    static_b5_ensemble_enabled,
)
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.rpc_channels import _handle_channels_status
from opensquilla.gateway.rpc_logs import _build_logs_status
from opensquilla.gateway.rpc_system import _handle_doctor_memory_status
from opensquilla.gateway.rpc_tools import _handle_providers_status, _handle_search_status
from opensquilla.health.evaluator import (
    evaluate_channels,
    evaluate_image_generation,
    evaluate_llm_ensemble,
    evaluate_logs,
    evaluate_memory,
    evaluate_memory_embedding,
    evaluate_provider,
    evaluate_router,
    evaluate_sandbox,
    evaluate_search,
    evaluate_squilla_router_runtime,
)
from opensquilla.health.model import FixStep, HealthFinding, HealthSeverity, build_report
from opensquilla.health.recovery_commands import command_with_config as _command_with_config
from opensquilla.sandbox.status import status_payload as _sandbox_status_payload
from opensquilla.session.keys import normalize_agent_id

_d = get_dispatcher()

Collector = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]
Evaluator = Callable[[dict[str, Any]], list[HealthFinding]]

_COLLECTION_INSPECT_COMMANDS = {
    "provider": "opensquilla providers status --json",
    "logs": "opensquilla diagnostics status",
    "memory": "opensquilla memory status --deep --json",
    "channels": "opensquilla channels status --json",
    "sandbox": "opensquilla sandbox status --json",
    "router": "opensquilla diagnostics status",
    "squilla_router": "opensquilla diagnostics status",
    "memory_embedding": "opensquilla memory status --deep --json",
    "search": "opensquilla search status --json",
    "image_generation": "opensquilla onboard status --json",
    "llm_ensemble": "opensquilla diagnostics status",
}
_READINESS_CRITICAL_COLLECTIONS = {"provider"}
_UNKNOWN_SEARCH_PROVIDER_RE = re.compile(
    r"Unknown search provider ['\"]([^'\"]+)['\"]"
    r"|unknown search provider: ['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


def _collection_error(surface: str, exc: Exception) -> HealthFinding:
    inspect_command = _COLLECTION_INSPECT_COMMANDS.get(surface)
    fix_steps = []
    if inspect_command:
        fix_steps.append(FixStep(label=f"Inspect {surface}", command=inspect_command))
    if inspect_command != "opensquilla diagnostics status":
        fix_steps.append(
            FixStep(label="Inspect diagnostics", command="opensquilla diagnostics status")
        )
    fix_steps.append(FixStep(label="Restart gateway", command="opensquilla gateway restart"))
    severity: HealthSeverity = (
        "error" if surface in _READINESS_CRITICAL_COLLECTIONS else "warn"
    )
    return HealthFinding(
        id=f"{surface}.diagnostic.unavailable",
        severity=severity,
        surface=surface,
        title=f"{surface.title()} diagnostics unavailable",
        detail=f"{type(exc).__name__}: {exc}",
        evidence={"errorType": type(exc).__name__},
        fix_steps=fix_steps,
        restart_required=True,
    )


def _config_path(ctx: RpcContext) -> str | None:
    config = getattr(ctx, "config", None)
    value = getattr(config, "config_path", None)
    return str(value) if value else None


def _with_config_recovery_steps(
    findings: list[HealthFinding],
    config_path: str | None,
) -> list[HealthFinding]:
    if not config_path:
        return findings
    adjusted: list[HealthFinding] = []
    for finding in findings:
        fix_steps = [
            replace(step, command=_command_with_config(step.command, config_path))
            if step.command
            else step
            for step in finding.fix_steps
        ]
        adjusted.append(replace(finding, fix_steps=fix_steps))
    return adjusted


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


async def _search_payload(ctx: RpcContext) -> dict[str, Any]:
    try:
        payload = cast(dict[str, Any], await _handle_search_status({}, ctx))
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
    if isinstance(tiers, dict) and active_provider.strip():
        from opensquilla.router_tiers import TierConfig

        active_l = active_provider.strip().lower()
        for tier_name, tier_value in tiers.items():
            tier = TierConfig.from_value(tier_value)
            if tier.provider and tier.provider.lower() != active_l:
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
        "tierProviderMismatch": str(
            getattr(router, "tier_provider_mismatch", "route") or "route"
        ),
        "mismatchedTierProviders": mismatched_tier_providers,
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
    ensemble_cfg = getattr(config, "llm_ensemble", None) if config is not None else None
    payload: dict[str, Any] = {
        "enabled": bool(getattr(ensemble_cfg, "enabled", False)),
        "selectionMode": str(getattr(ensemble_cfg, "selection_mode", "") or ""),
        "activeProvider": str(
            getattr(getattr(config, "llm", None), "provider", "") or ""
        ),
    }
    if config is not None and static_b5_ensemble_enabled(config):
        from opensquilla.provider.ensemble import static_b5_credential_available
        from opensquilla.provider.registry import get_provider_spec

        selection_mode = str(getattr(ensemble_cfg, "selection_mode", "") or "")
        member_provider = STATIC_B5_SELECTION_MODE_PROVIDERS.get(selection_mode, "openrouter")
        payload["memberProvider"] = member_provider
        payload["apiKeyEnv"] = str(get_provider_spec(member_provider).env_key or "")
        payload["credentialAvailable"] = static_b5_credential_available(
            config,
            getattr(config, "llm", None),
            selection_mode,
        )
    elif payload["enabled"] and payload["selectionMode"] == "custom_b5":
        from opensquilla.provider.ensemble import custom_b5_lineup_ready

        ready, reason = custom_b5_lineup_ready(config)
        payload["lineupReady"] = ready
        payload["lineupBlockedReason"] = reason
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


async def _evaluate_collection(
    surface: str,
    collect: Collector,
    evaluate: Evaluator,
) -> list[HealthFinding]:
    try:
        value = collect()
        payload = await value if inspect.isawaitable(value) else value
        return evaluate(payload)
    except Exception as exc:  # noqa: BLE001 - doctor reports partial diagnostic failures.
        return [_collection_error(surface, exc)]


@_d.method("doctor.status", scope="operator.read")
async def _handle_doctor_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    if params is not None and not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = params or {}
    agent_id = normalize_agent_id(str(params.get("agentId") or "main"))
    deep = bool(params.get("deep", True))

    findings: list[HealthFinding] = [
        HealthFinding(
            id="gateway.rpc.ready",
            severity="ok",
            surface="gateway",
            title="Gateway RPC ready",
            detail="The gateway accepted and handled doctor.status.",
            evidence={"connId": ctx.conn_id},
        )
    ]

    collectors: list[tuple[str, Collector, Evaluator]] = [
        (
            "provider",
            lambda: _handle_providers_status({"probeModels": False}, ctx),
            evaluate_provider,
        ),
        ("logs", lambda: _build_logs_status(ctx), evaluate_logs),
        (
            "memory",
            lambda: _handle_doctor_memory_status({"agentId": agent_id, "deep": deep}, ctx),
            evaluate_memory,
        ),
        ("channels", lambda: _handle_channels_status({}, ctx), evaluate_channels),
        ("sandbox", lambda: _sandbox_payload(ctx), evaluate_sandbox),
        ("router", lambda: _router_payload(ctx, deep=deep), evaluate_router),
        (
            "squilla_router",
            lambda: _squilla_router_runtime_payload(ctx),
            evaluate_squilla_router_runtime,
        ),
        (
            "memory_embedding",
            lambda: _memory_embedding_payload(ctx),
            evaluate_memory_embedding,
        ),
        ("search", lambda: _search_payload(ctx), evaluate_search),
        (
            "image_generation",
            lambda: _image_generation_payload(ctx),
            evaluate_image_generation,
        ),
        (
            "llm_ensemble",
            lambda: _llm_ensemble_payload(ctx),
            evaluate_llm_ensemble,
        ),
    ]

    for surface, collect, evaluate in collectors:
        findings.extend(await _evaluate_collection(surface, collect, evaluate))

    config_path = _config_path(ctx)
    findings = _with_config_recovery_steps(findings, config_path)
    report = build_report(findings)
    report["agentId"] = agent_id
    if config_path:
        report["configPath"] = config_path
    return report
