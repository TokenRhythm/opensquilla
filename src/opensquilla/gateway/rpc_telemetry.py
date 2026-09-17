"""Compatibility RPC boundary for the unified telemetry upload preference."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from opensquilla.gateway.adapters.app_settings import update_gateway_config_in_place
from opensquilla.gateway.adapters.telemetry_contract import register_telemetry_contract
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.config_persistence import persist_gateway_config
from opensquilla.gateway.config_secrets import inherit_then_clear_explicit
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.telemetry_connections import (
    is_registered_tui_connection,
    register_tui_connection,
)
from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
    TelemetryScope,
)
from opensquilla.telemetry.consent_transition import (
    publish_desktop_consent_mirror,
)
from opensquilla.telemetry.contracts.common import (
    ClientEntrypoint,
    ClientSurface,
    ExecutionMode,
)
from opensquilla.telemetry.coordination import (
    ScopeConsentCoordinator,
    scope_consent_coordinator_for,
)

log = structlog.get_logger(__name__)

_d = get_dispatcher()
_EXPECTED_PARAMS = frozenset({"scope", "enabled"})

@dataclass(frozen=True)
class _ScopeFields:
    enabled: str
    notice: str
    timestamp: str
    notice_version: str

    @property
    def paths(self) -> tuple[str, str, str]:
        return (
            f"privacy.{self.enabled}",
            f"privacy.{self.notice}",
            f"privacy.{self.timestamp}",
        )


_SCOPE_FIELDS = {
    TelemetryScope.RELIABILITY: _ScopeFields(
        enabled="reliability_diagnostics_enabled",
        notice="reliability_notice_version",
        timestamp="reliability_consented_at_utc",
        notice_version=CURRENT_RELIABILITY_NOTICE_VERSION,
    ),
    TelemetryScope.GROWTH: _ScopeFields(
        enabled="product_analytics_enabled",
        notice="product_analytics_notice_version",
        timestamp="product_analytics_consented_at_utc",
        notice_version=CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    ),
}


def _strict_params(params: Any) -> tuple[TelemetryScope, bool]:
    if not isinstance(params, dict) or set(params) != _EXPECTED_PARAMS:
        raise RpcHandlerError(
            "INVALID_REQUEST",
            "params must contain exactly scope and enabled",
            accepted=False,
        )
    raw_scope = params.get("scope")
    enabled = params.get("enabled")
    if type(raw_scope) is not str or raw_scope not in {scope.value for scope in TelemetryScope}:
        raise RpcHandlerError(
            "INVALID_REQUEST",
            "scope must be reliability or growth",
            accepted=False,
        )
    if type(enabled) is not bool:
        raise RpcHandlerError(
            "INVALID_REQUEST",
            "enabled must be a boolean",
            accepted=False,
        )
    return TelemetryScope(raw_scope), enabled


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _commit_record(
    *,
    ctx: RpcContext,
    enabled: bool,
) -> bool:
    privacy = ctx.config.privacy
    legacy_paths = {path for fields in _SCOPE_FIELDS.values() for path in fields.paths}
    if privacy.disable_network_observability is (not enabled) and not any(
        getattr(privacy, path.removeprefix("privacy.")) is not None
        for path in legacy_paths
    ):
        return False

    payload = ctx.config.model_dump(mode="python")
    privacy_payload = payload.setdefault("privacy", {})
    if not isinstance(privacy_payload, dict):
        raise TypeError("privacy config is unavailable")
    privacy_payload["disable_network_observability"] = not enabled
    for path in legacy_paths:
        privacy_payload[path.removeprefix("privacy.")] = None

    candidate = GatewayConfig.model_validate(payload)
    explicit_paths = legacy_paths | {"privacy.disable_network_observability"}
    inherit_then_clear_explicit(ctx.config, candidate, explicit_paths)
    candidate._mark_env_absorbed_secrets(payload)
    candidate.inherit_persist_provenance(ctx.config)
    for path in explicit_paths:
        candidate.clear_runtime_override(path)
        candidate.mark_force_persist(path)

    # Durable state wins before the shared live object changes. On failure the
    # old gate remains authoritative in both memory and the config file.
    persist_gateway_config(candidate)
    update_gateway_config_in_place(ctx.config, candidate)
    return True


def _persist_failure(scope: TelemetryScope, enabled: bool, exc: Exception) -> RpcHandlerError:
    log.warning(
        "gateway.telemetry_consent_persist_failed",
        scope=scope.value,
        enabled=enabled,
        error=type(exc).__name__,
    )
    return RpcHandlerError(
        "TELEMETRY_CONSENT_PERSIST_FAILED",
        "The telemetry preference could not be saved. Try again.",
        retryable=True,
        accepted=False,
        details={"scope": scope.value, "enabled": enabled},
    )


def _mirror_failure(
    scope: TelemetryScope,
    *,
    enabled: bool,
    accepted: bool,
    phase: str,
    exc: Exception,
) -> RpcHandlerError:
    log.warning(
        "gateway.telemetry_consent_mirror_failed",
        scope=scope.value,
        phase=phase,
        error=type(exc).__name__,
    )
    return RpcHandlerError(
        "TELEMETRY_CONSENT_MIRROR_FAILED",
        "The local telemetry consent gate could not be synchronized. Try again.",
        retryable=True,
        accepted=accepted,
        details={
            "scope": scope.value,
            "enabled": enabled,
            "mirrorSynchronized": False,
            "phase": phase,
        },
    )


def _write_mirror(
    scope: TelemetryScope,
    *,
    ctx: RpcContext,
    enabled: bool,
    fail_closed: bool,
    accepted: bool,
) -> None:
    phase = "before_change" if fail_closed else "after_change"
    try:
        publish_desktop_consent_mirror(
            ctx.config,
            fail_closed_scopes=tuple(TelemetryScope) if fail_closed else (),
        )
    except Exception as exc:
        raise _mirror_failure(
            scope,
            enabled=enabled,
            accepted=accepted,
            phase=phase,
            exc=exc,
        ) from exc


def _response(
    scope: TelemetryScope,
    *,
    enabled: bool,
    notice_version: str | None,
    consented_at_utc: str | None,
    changed: bool,
    cleanup_performed: bool,
) -> dict[str, Any]:
    return {
        "scope": scope.value,
        "enabled": enabled,
        "noticeVersion": notice_version,
        "consentedAtUtc": consented_at_utc,
        "changed": changed,
        "cleanupPerformed": cleanup_performed,
        "cleanupComplete": True,
    }


async def _handle_telemetry_consent_set(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    scope, enabled = _strict_params(params)
    if ctx.config is None or not isinstance(ctx.config, GatewayConfig):
        raise RpcHandlerError(
            "UNAVAILABLE",
            "Gateway configuration is unavailable.",
            retryable=True,
            accepted=False,
        )

    fields = _SCOPE_FIELDS[scope]
    coordinator = getattr(ctx, "telemetry_consent_coordinator", None)
    if coordinator is None:
        coordinator = scope_consent_coordinator_for(ctx.config)
    if not isinstance(coordinator, ScopeConsentCoordinator):
        raise RpcHandlerError(
            "UNAVAILABLE",
            "Telemetry consent coordination is unavailable.",
            retryable=True,
            accepted=False,
        )
    async with coordinator.transition(TelemetryScope.RELIABILITY):
        async with coordinator.transition(TelemetryScope.GROWTH):
            # The saved global preference may change while runtime vetoes
            # (CI, DNT, or managed policy) continue to prevent actual uploads.
            _write_mirror(
                scope, ctx=ctx, enabled=enabled, fail_closed=True, accepted=False,
            )
            try:
                changed = _commit_record(ctx=ctx, enabled=enabled)
            except Exception as exc:
                raise _persist_failure(scope, enabled, exc) from exc
            _write_mirror(
                scope, ctx=ctx, enabled=enabled, fail_closed=False, accepted=True,
            )
            return _response(
                scope,
                enabled=enabled,
                notice_version=fields.notice_version if enabled else None,
                # Older clients require a timestamp to acknowledge this explicit
                # preference action. It is not persisted as scoped consent and
                # does not enter the upload-policy mirror or event envelope.
                consented_at_utc=_utc_now() if enabled else None,
                changed=changed,
                cleanup_performed=False,
            )


async def _handle_client_launch_record(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Record the hard-coded Gateway TUI launch; clients supply no dimensions."""

    if params not in (None, {}):
        raise RpcHandlerError(
            "INVALID_REQUEST",
            "client launch params must be empty",
            accepted=False,
        )
    if not ctx.principal.is_owner or not ctx.principal.authenticated:
        raise RpcHandlerError(
            "UNAUTHORIZED",
            "An authenticated owner connection is required.",
            accepted=False,
        )
    register_tui_connection(ctx.conn_id)
    sink = getattr(ctx.turn_runner, "growth_event_sink", None)
    record = getattr(sink, "record_client_launch", None)
    if not callable(record):
        return {"recorded": False}
    recorded = await record(
        surface=ClientSurface.TUI,
        entrypoint=ClientEntrypoint.CHAT,
        execution_mode=ExecutionMode.GATEWAY,
    )
    return {"recorded": bool(recorded)}


async def _handle_product_active_record(
    params: dict[str, Any] | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Accept a content-free foreground activity observation from an owner UI."""

    if (
        not isinstance(params, dict)
        or set(params) != {"surface"}
        or type(params["surface"]) is not str
        or params["surface"] not in {surface.value for surface in ClientSurface}
    ):
        raise RpcHandlerError(
            "INVALID_REQUEST", "params must contain only a valid surface", accepted=False,
        )
    if not ctx.principal.is_owner or not ctx.principal.authenticated:
        raise RpcHandlerError(
            "UNAUTHORIZED", "An authenticated owner connection is required.", accepted=False,
        )
    sink = getattr(ctx.turn_runner, "growth_event_sink", None)
    record = getattr(sink, "record_product_active", None)
    if not callable(record):
        return {"recorded": False}
    recorded = await record(surface=ClientSurface(params["surface"]))
    return {"recorded": bool(recorded)}


_handle_telemetry_consent_set_contract = register_telemetry_contract(
    _d,
    "telemetry.consent.set",
    _handle_telemetry_consent_set,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_client_launch_record_contract = register_telemetry_contract(
    _d,
    "telemetry.client_launch.record",
    _handle_client_launch_record,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_product_active_record_contract = register_telemetry_contract(
    _d,
    "telemetry.product_active.record",
    _handle_product_active_record,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)


__all__ = [
    "_handle_client_launch_record",
    "_handle_product_active_record",
    "_handle_telemetry_consent_set",
    "is_registered_tui_connection",
]
