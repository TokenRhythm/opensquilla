"""RPC mutation boundary for scoped telemetry consent."""

from __future__ import annotations

import inspect
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from opensquilla.gateway.adapters.telemetry_contract import register_telemetry_contract
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.config_secrets import inherit_then_clear_explicit
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.rpc_config import (
    persist_gateway_config,
    update_gateway_config_in_place,
)
from opensquilla.observability.network_policy import telemetry_scope_forced_off_reasons
from opensquilla.telemetry.consent import (
    CURRENT_PRODUCT_ANALYTICS_NOTICE_VERSION,
    CURRENT_RELIABILITY_NOTICE_VERSION,
    TelemetryScope,
)
from opensquilla.telemetry.consent_transition import (
    publish_desktop_consent_mirror,
    telemetry_state_dir,
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
from opensquilla.telemetry.desktop_state import (
    DesktopTelemetryStateError,
    clear_desktop_early_spool_scope,
)
from opensquilla.telemetry.growth.state import delete_growth_cohort_state
from opensquilla.telemetry.identity import (
    TelemetryIdentityKind,
    delete_identity,
    identity_state_path,
)
from opensquilla.telemetry.outbox import TelemetryOutbox

log = structlog.get_logger(__name__)

_d = get_dispatcher()
_EXPECTED_PARAMS = frozenset({"scope", "enabled"})
_TUI_CONNECTION_LIMIT = 1024
_tui_connection_order: deque[str] = deque()
_tui_connection_ids: set[str] = set()


def is_registered_tui_connection(conn_id: str) -> bool:
    return conn_id in _tui_connection_ids


def _register_tui_connection(conn_id: str) -> None:
    if conn_id in _tui_connection_ids:
        return
    _tui_connection_ids.add(conn_id)
    _tui_connection_order.append(conn_id)
    while len(_tui_connection_order) > _TUI_CONNECTION_LIMIT:
        _tui_connection_ids.discard(_tui_connection_order.popleft())


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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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


async def _await_hook_result(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


async def _default_scope_cleanup(scope: TelemetryScope, config: Any) -> None:
    """Clear only telemetry state whose exact location the Gateway owns."""

    async with await TelemetryOutbox.open(telemetry_state_dir(config), scope) as outbox:
        await outbox.clear_scope()
    if scope is TelemetryScope.GROWTH:
        delete_identity(identity_state_path(TelemetryIdentityKind.ANALYTICS_USER, config=config))
        delete_growth_cohort_state(config=config)
    desktop_cleanup = clear_desktop_early_spool_scope(telemetry_state_dir(config), scope)
    if not desktop_cleanup.complete:
        raise DesktopTelemetryStateError("desktop early spool could not be cleared completely")


async def _cleanup_scope(scope: TelemetryScope, ctx: RpcContext) -> None:
    cleanup = getattr(ctx, "telemetry_consent_cleanup", None)
    if cleanup is None:
        await _default_scope_cleanup(scope, ctx.config)
    elif callable(cleanup):
        await _await_hook_result(cleanup(scope=scope, config=ctx.config))
    else:
        raise TypeError("telemetry consent cleanup hook is not callable")

    if scope is not TelemetryScope.GROWTH:
        return
    eligibility_cleanup = getattr(ctx, "telemetry_growth_eligibility_cleanup", None)
    if eligibility_cleanup is None:
        return
    if not callable(eligibility_cleanup):
        raise TypeError("growth eligibility cleanup hook is not callable")
    await _await_hook_result(eligibility_cleanup(config=ctx.config))


def _commit_record(
    *,
    ctx: RpcContext,
    fields: _ScopeFields,
    enabled: bool,
    consented_at_utc: str | None,
) -> bool:
    privacy = ctx.config.privacy
    notice_version = fields.notice_version if enabled else None
    target = (enabled, notice_version, consented_at_utc)
    current = tuple(
        getattr(privacy, field) for field in (fields.enabled, fields.notice, fields.timestamp)
    )
    if current == target:
        return False

    payload = ctx.config.model_dump(mode="python")
    privacy_payload = payload.setdefault("privacy", {})
    if not isinstance(privacy_payload, dict):
        raise TypeError("privacy config is unavailable")
    privacy_payload[fields.enabled] = enabled
    privacy_payload[fields.notice] = notice_version
    privacy_payload[fields.timestamp] = consented_at_utc

    candidate = GatewayConfig.model_validate(payload)
    explicit_paths = set(fields.paths)
    inherit_then_clear_explicit(ctx.config, candidate, explicit_paths)
    candidate._mark_env_absorbed_secrets(payload)
    candidate.inherit_persist_provenance(ctx.config)
    for path in fields.paths:
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


def _cleanup_failure(
    scope: TelemetryScope,
    *,
    accepted: bool,
    phase: str,
    exc: Exception,
) -> RpcHandlerError:
    log.warning(
        "gateway.telemetry_consent_cleanup_failed",
        scope=scope.value,
        phase=phase,
        error=type(exc).__name__,
    )
    return RpcHandlerError(
        "TELEMETRY_CONSENT_CLEANUP_FAILED",
        "Local telemetry data could not be cleared completely. Try again.",
        retryable=True,
        accepted=accepted,
        details={
            "scope": scope.value,
            "enabled": False,
            "cleanupComplete": False,
            "phase": phase,
        },
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
            fail_closed_scopes=(scope,) if fail_closed else (),
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
    async with coordinator.transition(scope):
        privacy = ctx.config.privacy
        previous_enabled = getattr(privacy, fields.enabled)

        # A runtime/global veto may pause an existing grant, but it may never
        # manufacture a new grant from an unset or declined record.
        if enabled and previous_enabled is not True:
            forced_off = telemetry_scope_forced_off_reasons(scope.value, config=ctx.config)
            if forced_off:
                raise RpcHandlerError(
                    "TELEMETRY_CONSENT_FORCED_OFF",
                    "This telemetry preference is disabled by current policy.",
                    accepted=False,
                    details={"scope": scope.value, "forcedOff": True},
                )

        # Close the Desktop producer before any cleanup or config mutation.
        # The other scope is rebuilt from the still-authoritative live config.
        _write_mirror(
            scope,
            ctx=ctx,
            enabled=enabled,
            fail_closed=True,
            accepted=False,
        )

        cleanup_performed = False
        if enabled:
            # An explicit prior decline may have left state behind after a
            # partial cleanup. Clear it before re-opening the collection gate.
            if previous_enabled is False:
                try:
                    await _cleanup_scope(scope, ctx)
                except Exception as exc:
                    raise _cleanup_failure(
                        scope,
                        accepted=False,
                        phase="before_enable",
                        exc=exc,
                    ) from exc
                cleanup_performed = True

            current_notice = getattr(privacy, fields.notice)
            current_timestamp = getattr(privacy, fields.timestamp)
            if (
                previous_enabled is True
                and current_notice == fields.notice_version
                and isinstance(current_timestamp, str)
                and current_timestamp
            ):
                # Also acts as the retry path after a prior final-mirror write
                # failed: no config rewrite is needed, but Desktop can reopen.
                _write_mirror(
                    scope,
                    ctx=ctx,
                    enabled=True,
                    fail_closed=False,
                    accepted=True,
                )
                return _response(
                    scope,
                    enabled=True,
                    notice_version=current_notice,
                    consented_at_utc=current_timestamp,
                    changed=False,
                    cleanup_performed=cleanup_performed,
                )

            consented_at_utc = _utc_now()
            try:
                changed = _commit_record(
                    ctx=ctx,
                    fields=fields,
                    enabled=True,
                    consented_at_utc=consented_at_utc,
                )
            except Exception as exc:
                raise _persist_failure(scope, True, exc) from exc
            _write_mirror(
                scope,
                ctx=ctx,
                enabled=True,
                fail_closed=False,
                accepted=True,
            )
            return _response(
                scope,
                enabled=True,
                notice_version=fields.notice_version,
                consented_at_utc=consented_at_utc,
                changed=changed,
                cleanup_performed=cleanup_performed,
            )

        # Withdrawal is deliberately two-phase. Persist and hot-apply the
        # closed gate first; only then erase queue/rejection/identity state.
        # Repeated withdrawals still retry cleanup after an earlier partial
        # failure, while avoiding an unnecessary config rewrite.
        try:
            changed = _commit_record(
                ctx=ctx,
                fields=fields,
                enabled=False,
                consented_at_utc=None,
            )
        except Exception as exc:
            raise _persist_failure(scope, False, exc) from exc
        try:
            await _cleanup_scope(scope, ctx)
        except Exception as exc:
            raise _cleanup_failure(
                scope,
                accepted=True,
                phase="after_disable",
                exc=exc,
            ) from exc
        _write_mirror(
            scope,
            ctx=ctx,
            enabled=False,
            fail_closed=False,
            accepted=True,
        )
        return _response(
            scope,
            enabled=False,
            notice_version=None,
            consented_at_utc=None,
            changed=changed,
            cleanup_performed=True,
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
    _register_tui_connection(ctx.conn_id)
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


__all__ = [
    "_handle_client_launch_record",
    "_handle_telemetry_consent_set",
    "is_registered_tui_connection",
]
