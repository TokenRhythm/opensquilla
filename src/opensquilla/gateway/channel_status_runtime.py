"""Channel runtime status projection shared by administration and diagnostics."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from typing import Any

from opensquilla.channels._util import ChannelAccessPolicy
from opensquilla.channels.contract import (
    channel_capability_evidence,
    channel_capability_profile,
    channel_platform_manifest,
)


def configured_channel_entries(config: Any) -> list[dict[str, Any]]:
    channels_config = getattr(config, "channels", None)
    entries = getattr(channels_config, "channels", None) or []
    result: list[dict[str, Any]] = []
    for entry in entries:
        if hasattr(entry, "model_dump"):
            result.append(entry.model_dump(mode="python"))
        elif isinstance(entry, dict):
            result.append(dict(entry))
    return result


def status_for(
    *,
    connected: bool,
    enabled: bool,
    dispatch_state: str | None,
    connection_phase: str | None,
) -> str:
    if not enabled:
        return "disabled"
    if dispatch_state in {"dead", "exhausted", "restarting"}:
        return dispatch_state
    if connection_phase in {"connecting", "reconnecting"}:
        return "restarting"
    return "connected" if connected else "stopped"


def _health_extra(health: Any) -> dict[str, Any]:
    extra = getattr(health, "extra", None)
    return extra if isinstance(extra, dict) else {}


def _capability_payload(adapter: Any | None) -> tuple[list[str], dict[str, Any] | None]:
    profile = channel_capability_profile(adapter)
    if profile is None:
        return [], None
    maturity = "unrated"
    module_name = getattr(type(adapter), "__module__", "")
    if module_name:
        try:
            maturity = str(
                getattr(importlib.import_module(module_name), "CAPABILITY_TIER", maturity)
            )
        except ImportError:
            pass
    return sorted(profile.capability_tags()), {
        "channel_type": profile.channel_type,
        "transports": list(profile.transports),
        "maturity": maturity,
        "evidence": channel_capability_evidence(adapter),
    }


def _platform_manifest_payload(adapter: Any | None) -> dict[str, Any] | None:
    manifest = channel_platform_manifest(adapter)
    return manifest.to_dict() if manifest is not None else None


def _manager_start_errors(manager: Any | None) -> dict[str, Any]:
    if manager is None:
        return {}
    start_errors = getattr(manager, "start_errors", None)
    if not callable(start_errors):
        return {}
    try:
        errors = start_errors()
    except Exception:
        return {}
    return errors if isinstance(errors, dict) else {}


def _diagnostic_from_start_error(start_error: Any) -> dict[str, Any] | None:
    if not isinstance(start_error, dict):
        return None
    diagnostic = start_error.get("diagnostic")
    if isinstance(diagnostic, dict):
        result = dict(diagnostic)
        result.setdefault("source", "start_error")
        return result
    error_type = str(start_error.get("error_type") or "StartupError")
    return {
        "error_class": "startup_failed",
        "message": f"Channel failed during startup: {error_type}",
        "retryable": False,
        "source": "start_error",
    }


def _diagnostic_from_health_extra(extra: dict[str, Any]) -> dict[str, Any] | None:
    diagnostic = extra.get("last_error")
    if not isinstance(diagnostic, dict):
        return None
    result = dict(diagnostic)
    result.setdefault("source", "adapter")
    return result


def _diagnostics_payload(
    *,
    extra: dict[str, Any] | None = None,
    start_error: Any = None,
    delivery: dict[str, Any] | None = None,
    admission: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"network_probe": "not_run"}
    last_error = _diagnostic_from_start_error(start_error)
    if last_error is None and extra is not None:
        last_error = _diagnostic_from_health_extra(extra)
    if last_error is not None:
        payload["last_error"] = last_error
    if extra is not None and isinstance(extra.get("connection_phase"), str):
        payload["connection_phase"] = extra["connection_phase"]
    if extra is not None and isinstance(extra.get("transport_lease"), dict):
        payload["transport_lease"] = dict(extra["transport_lease"])
    if delivery is not None:
        payload["delivery"] = delivery
    if admission is not None:
        payload["admission"] = admission
    return payload


def _delivery_diagnostics(manager: Any | None, name: str) -> dict[str, Any] | None:
    store = getattr(manager, "_delivery_store", None)
    diagnostics = getattr(store, "diagnostics", None)
    if not callable(diagnostics):
        return None
    try:
        result = diagnostics(name)
    except Exception:
        return None
    return result if isinstance(result, dict) else None


ADMISSION_ADMIT_REASONS = frozenset({"dm_admitted", "group_admitted"})


def _iso_timestamp(value: Any) -> str | None:
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(float(value), tz=UTC).isoformat()


def _admission_diagnostics(manager: Any | None, name: str, adapter: Any) -> dict[str, Any] | None:
    payload: dict[str, Any] = {}
    if adapter is not None:
        policy = getattr(adapter, "policy", None)
        if not isinstance(policy, ChannelAccessPolicy):
            policy = ChannelAccessPolicy()
        payload["dmAccess"] = str(policy.dm_access)
        payload["allowlist"] = {
            "configured": bool(policy.allowlist),
            "entryCount": len(policy.allowlist),
            "blankEntryCount": sum(1 for entry in policy.allowlist if not str(entry).strip()),
        }
    store = getattr(manager, "_delivery_store", None)
    counts = getattr(store, "admission_reason_counts", None)
    if callable(counts):
        try:
            tallies = counts(name)
        except Exception:
            tallies = None
        if isinstance(tallies, dict) and tallies:
            payload["reasons"] = {
                reason: {
                    "count": int(entry.get("count", 0)),
                    "lastAt": _iso_timestamp(entry.get("last_at")),
                }
                for reason, entry in tallies.items()
                if isinstance(entry, dict)
            }
            first_times = [
                float(entry["first_at"])
                for entry in tallies.values()
                if isinstance(entry, dict)
                and isinstance(entry.get("first_at"), int | float)
            ]
            if first_times:
                payload["since"] = _iso_timestamp(min(first_times))
            denials: list[tuple[str, float]] = []
            for reason, entry in tallies.items():
                if not isinstance(entry, dict) or reason in ADMISSION_ADMIT_REASONS:
                    continue
                last_at = entry.get("last_at")
                if isinstance(last_at, int | float):
                    denials.append((reason, float(last_at)))
            if denials:
                last_reason, last_denied_at = max(denials, key=lambda item: item[1])
                payload["lastDenial"] = {
                    "reason": last_reason,
                    "at": _iso_timestamp(last_denied_at),
                }
    return payload or None


def _pending_pairings_by_channel(manager: Any | None) -> dict[str, int]:
    store = getattr(manager, "_delivery_store", None)
    list_pairings = getattr(store, "list_pairings", None)
    if not callable(list_pairings):
        return {}
    counts: dict[str, int] = {}
    try:
        for record in list_pairings(status="pending"):
            name = str(getattr(record, "channel_name", "") or "")
            if name:
                counts[name] = counts.get(name, 0) + 1
    except Exception:
        return {}
    return counts


async def read_channel_status(
    *,
    config: Any,
    channel_manager: Any | None,
    boot_id: str,
) -> dict[str, Any]:
    """Project ChannelManager state without importing an RPC handler."""

    health_map = await channel_manager.health() if channel_manager else {}
    start_errors = _manager_start_errors(channel_manager)
    manager_types = getattr(channel_manager, "_channel_types", {}) if channel_manager else {}
    pending_pairings = _pending_pairings_by_channel(channel_manager)
    channels: list[dict[str, Any]] = []
    seen: set[str] = set()

    for entry in configured_channel_entries(config):
        name = str(entry.get("name") or "")
        if not name:
            continue
        enabled = bool(entry.get("enabled", True))
        health = health_map.get(name)
        extra = _health_extra(health)
        adapter = channel_manager.get(name) if channel_manager else None
        capabilities, capability_profile = _capability_payload(adapter)
        connected = bool(getattr(health, "connected", False)) if health else False
        channels.append(
            {
                "name": name,
                "connected": connected,
                "status": status_for(
                    connected=connected,
                    enabled=enabled,
                    dispatch_state=extra.get("dispatch_state"),
                    connection_phase=extra.get("connection_phase"),
                ),
                "bot_user_id": getattr(health, "bot_user_id", None) if health else None,
                "connected_since": extra.get("connected_since"),
                "restart_attempts": extra.get("restart_attempts", 0),
                "pendingPairings": pending_pairings.get(name, 0),
                "type": entry.get("type"),
                "enabled": enabled,
                "configured": True,
                "capabilities": capabilities,
                "capability_profile": capability_profile,
                "platform_manifest": _platform_manifest_payload(adapter),
                "diagnostics": _diagnostics_payload(
                    extra=extra,
                    start_error=start_errors.get(name),
                    delivery=_delivery_diagnostics(channel_manager, name),
                    admission=_admission_diagnostics(channel_manager, name, adapter),
                ),
            }
        )
        seen.add(name)

    for name, health in health_map.items():
        if name in seen:
            continue
        extra = _health_extra(health)
        adapter = channel_manager.get(name) if channel_manager else None
        capabilities, capability_profile = _capability_payload(adapter)
        connected = bool(getattr(health, "connected", False))
        channels.append(
            {
                "name": name,
                "connected": connected,
                "status": status_for(
                    connected=connected,
                    enabled=True,
                    dispatch_state=extra.get("dispatch_state"),
                    connection_phase=extra.get("connection_phase"),
                ),
                "bot_user_id": getattr(health, "bot_user_id", None),
                "connected_since": extra.get("connected_since"),
                "restart_attempts": extra.get("restart_attempts", 0),
                "pendingPairings": pending_pairings.get(name, 0),
                "type": manager_types.get(name) or type(adapter).__name__,
                "enabled": True,
                "configured": False,
                "capabilities": capabilities,
                "capability_profile": capability_profile,
                "platform_manifest": _platform_manifest_payload(adapter),
                "diagnostics": _diagnostics_payload(
                    extra=extra,
                    start_error=start_errors.get(name),
                    delivery=_delivery_diagnostics(channel_manager, name),
                    admission=_admission_diagnostics(channel_manager, name, adapter),
                ),
            }
        )

    return {"channels": channels, "bootId": boot_id}
