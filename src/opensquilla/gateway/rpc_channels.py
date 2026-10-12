"""Channels domain RPC handlers."""

from __future__ import annotations

import contextlib
import importlib
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import structlog

from opensquilla.channels._util import ChannelAccessPolicy
from opensquilla.channels.contract import (
    channel_capability_evidence,
    channel_capability_profile,
    channel_platform_manifest,
)
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.redaction import redact_error_text

if TYPE_CHECKING:
    from opensquilla.gateway.config import GatewayConfig

log = structlog.get_logger(__name__)

_d = get_dispatcher()


def _channel_status(connected: bool) -> str:
    return "connected" if connected else "stopped"


def _configured_channel_entries(ctx: RpcContext) -> list[dict[str, Any]]:
    config = getattr(ctx, "config", None)
    channels_cfg = getattr(config, "channels", None)
    entries = getattr(channels_cfg, "channels", None) or []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if hasattr(entry, "model_dump"):
            out.append(entry.model_dump(mode="python"))
        elif isinstance(entry, dict):
            out.append(dict(entry))
    return out


def _health_extra(health: Any) -> dict[str, Any]:
    extra = getattr(health, "extra", None)
    return extra if isinstance(extra, dict) else {}


def _status_for(
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
    return _channel_status(connected)


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
        out = dict(diagnostic)
        out.setdefault("source", "start_error")
        return out
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
    out = dict(diagnostic)
    out.setdefault("source", "adapter")
    return out


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


# The admission vocabulary's admit outcomes; every other reason is a denial.
_ADMISSION_ADMIT_REASONS = frozenset({"dm_admitted", "group_admitted"})


def _admission_diagnostics(manager: Any | None, name: str, adapter: Any) -> dict[str, Any] | None:
    """Explain-why facts for a channel: policy mode + per-reason tallies.

    Answers the operator question "why did that message not create a session?"
    with the effective access mode and denial reason codes/counts — never a
    sender identity. Absent entirely when there is nothing to explain (no
    running adapter and no recorded decisions).
    """
    payload: dict[str, Any] = {}
    if adapter is not None:
        policy = getattr(adapter, "policy", None)
        if not isinstance(policy, ChannelAccessPolicy):
            # Admission treats a missing/foreign policy as the default policy,
            # so the effective mode shown here must do the same.
            policy = ChannelAccessPolicy()
        payload["dmAccess"] = str(policy.dm_access)
        payload["allowlist"] = {
            "configured": bool(policy.allowlist),
            "entryCount": len(policy.allowlist),
            "blankEntryCount": sum(1 for e in policy.allowlist if not str(e).strip()),
        }
    store = getattr(manager, "_delivery_store", None)
    counts_fn = getattr(store, "admission_reason_counts", None)
    if callable(counts_fn):
        try:
            tallies = counts_fn(name)
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
            # Tallies are lifetime, not current-policy: label the horizon so a
            # months-old denial under a since-changed policy reads as history.
            first_ats = [
                float(entry["first_at"])
                for entry in tallies.values()
                if isinstance(entry, dict) and isinstance(entry.get("first_at"), int | float)
            ]
            if first_ats:
                payload["since"] = _iso_timestamp(min(first_ats))
            denials: list[tuple[str, float]] = []
            for reason, entry in tallies.items():
                if not isinstance(entry, dict) or reason in _ADMISSION_ADMIT_REASONS:
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


def _pairing_store(ctx: RpcContext) -> Any:
    manager = getattr(ctx, "channel_manager", None)
    store = getattr(manager, "_delivery_store", None)
    if store is None or not callable(getattr(store, "list_pairings", None)):
        raise RuntimeError("channel pairing store is unavailable")
    return store


def _iso_timestamp(value: Any) -> str | None:
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(float(value), tz=UTC).isoformat()


def _pairing_payload(pairing: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pairingId": str(pairing.pairing_id),
        "pairingCode": str(pairing.pairing_id)[:8],
        "channelName": str(pairing.channel_name),
        "senderId": str(pairing.sender_id),
        "status": str(pairing.status),
        "createdAt": _iso_timestamp(pairing.created_at),
        "approvedAt": _iso_timestamp(pairing.approved_at),
    }
    if pairing.sender_name:
        payload["senderName"] = str(pairing.sender_name)
    return payload


def _probe_secret_values(payload: dict[str, Any]) -> tuple[str, ...]:
    """Extract configured credential values for exact-match error redaction."""

    secret_names = (
        "authorization",
        "credential",
        "password",
        "private_key",
        "secret",
        "ticket",
        "token",
    )
    return tuple(
        str(value)
        for key, value in payload.items()
        if value
        and isinstance(value, str)
        and any(marker in key.lower() for marker in secret_names)
    )


def _redact_probe_result(value: Any, secrets: tuple[str, ...]) -> Any:
    """Remove credential-shaped probe evidence without altering public IDs."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _probe_secret_values({key_text: item}):
                redacted[key_text] = "***"
            else:
                redacted[key_text] = _redact_probe_result(item, secrets)
        return redacted
    if isinstance(value, list | tuple):
        return [_redact_probe_result(item, secrets) for item in value]
    if isinstance(value, str):
        redacted_text = value
        for secret in sorted(set(secrets), key=len, reverse=True):
            if len(secret) >= 4:
                redacted_text = redacted_text.replace(secret, "***")
        return redacted_text
    return value


def _pending_pairings_by_channel(ctx: RpcContext) -> dict[str, int]:
    """One query for every channel's pending-approval count.

    A pending pairing is an inbound sender waiting on the operator — the one
    state that otherwise produces a confusing "nothing happened" in the UI —
    so the status roll-up carries it to every surface for free. Best-effort:
    a missing store just means zero badges.
    """
    counts: dict[str, int] = {}
    try:
        for record in _pairing_store(ctx).list_pairings(status="pending"):
            name = str(getattr(record, "channel_name", "") or "")
            if name:
                counts[name] = counts.get(name, 0) + 1
    except Exception:  # noqa: BLE001 - status must not fail on pairing storage
        return {}
    return counts


@_d.method("channels.status", scope="operator.read")
async def _handle_channels_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    health_map = await ctx.channel_manager.health() if ctx.channel_manager else {}
    start_errors = _manager_start_errors(ctx.channel_manager)
    manager_types = (
        getattr(ctx.channel_manager, "_channel_types", {}) if ctx.channel_manager else {}
    )
    pending_pairings = _pending_pairings_by_channel(ctx)
    channels: list[dict[str, Any]] = []
    seen: set[str] = set()

    for entry in _configured_channel_entries(ctx):
        name = str(entry.get("name") or "")
        if not name:
            continue
        enabled = bool(entry.get("enabled", True))
        health = health_map.get(name)
        extra = _health_extra(health)
        adapter = ctx.channel_manager.get(name) if ctx.channel_manager else None
        capabilities, capability_profile = _capability_payload(adapter)
        platform_manifest = _platform_manifest_payload(adapter)
        connected = bool(getattr(health, "connected", False)) if health else False
        channels.append(
            {
                "name": name,
                "connected": connected,
                "status": _status_for(
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
                "platform_manifest": platform_manifest,
                "diagnostics": _diagnostics_payload(
                    extra=extra,
                    start_error=start_errors.get(name),
                    delivery=_delivery_diagnostics(ctx.channel_manager, name),
                    admission=_admission_diagnostics(ctx.channel_manager, name, adapter),
                ),
            }
        )
        seen.add(name)

    for name, health in health_map.items():
        if name in seen:
            continue
        extra = _health_extra(health)
        adapter = ctx.channel_manager.get(name) if ctx.channel_manager else None
        capabilities, capability_profile = _capability_payload(adapter)
        platform_manifest = _platform_manifest_payload(adapter)
        connected = bool(getattr(health, "connected", False))
        channels.append(
            {
                "name": name,
                "connected": connected,
                "status": _status_for(
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
                "platform_manifest": platform_manifest,
                "diagnostics": _diagnostics_payload(
                    extra=extra,
                    start_error=start_errors.get(name),
                    delivery=_delivery_diagnostics(ctx.channel_manager, name),
                    admission=_admission_diagnostics(ctx.channel_manager, name, adapter),
                ),
            }
        )

    from opensquilla.gateway.boot import _boot_id

    return {"channels": channels, "bootId": _boot_id}


@_d.method("channels.get", scope="operator.admin")
async def _handle_channels_get(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    name = str((params or {}).get("name") or (params or {}).get("channel") or "")
    if not name:
        raise ValueError("channel name required")
    from opensquilla.onboarding.redaction import redact_channel_entry

    for entry in _configured_channel_entries(ctx):
        if str(entry.get("name") or "") != name:
            continue
        channel_type = str(entry.get("type") or "")
        redacted = redact_channel_entry(channel_type, entry)
        return {
            "entry": redacted,
            "secretFields": [key for key, value in redacted.items() if value == "***"],
        }
    raise KeyError(f"Channel not found: {name}")


@_d.method("channels.probe", scope="operator.admin")
async def _handle_channels_probe(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Run a non-mutating provider credential/network probe when implemented."""
    from opensquilla.channels.registry import build_managed_channel, parse_channel_entry
    from opensquilla.onboarding.mutations import (
        merge_channel_entry_secrets,
        validate_channel_entry,
    )

    raw_entry = (params or {}).get("entry")
    if raw_entry is None:
        name = str((params or {}).get("name") or "")
        raw_entry = next(
            (
                entry
                for entry in _configured_channel_entries(ctx)
                if str(entry.get("name") or "") == name
            ),
            None,
        )
    if not isinstance(raw_entry, dict):
        raise ValueError("channel entry or name required")

    config = cast("GatewayConfig", getattr(ctx, "config", None))
    normalized = validate_channel_entry(merge_channel_entry_secrets(config, raw_entry))
    secret_values = _probe_secret_values(normalized)
    entry = parse_channel_entry(normalized)
    adapter = build_managed_channel(entry)
    if adapter is None:
        raise ValueError(f"unsupported channel type: {normalized.get('type')}")
    probe = getattr(adapter, "probe_connection", None)
    started = time.perf_counter()
    try:
        if not callable(probe):
            return {
                "status": "unsupported",
                "connected": False,
                "latencyMs": None,
                "detail": "This adapter does not yet expose a safe non-mutating live probe.",
            }
        try:
            result = await probe()
        except Exception as exc:  # noqa: BLE001 - provider boundary is rendered as evidence
            return {
                "status": "failed",
                "connected": False,
                "latencyMs": round((time.perf_counter() - started) * 1000),
                "detail": redact_error_text(
                    str(exc),
                    max_len=500,
                    known_secrets=secret_values,
                ),
            }
    finally:
        stop = getattr(adapter, "stop", None)
        close = getattr(adapter, "close", None)
        if callable(stop):
            with contextlib.suppress(Exception):
                await stop()
        elif callable(close):
            with contextlib.suppress(Exception):
                await close()
    latency_ms = round((time.perf_counter() - started) * 1000)
    payload = _redact_probe_result(result, secret_values) if isinstance(result, dict) else {}
    supported = bool(payload.get("supported", True))
    authenticated = bool(payload.get("authenticated", False))
    return {
        "status": ("verified" if supported and authenticated else "unsupported"),
        "connected": authenticated,
        "latencyMs": latency_ms,
        "detail": str(payload.get("reason") or ""),
        "result": payload,
    }


@_d.method("channels.logout", scope="operator.admin")
async def _handle_channels_logout(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    channel_name = None
    if isinstance(params, dict):
        channel_name = params.get("channel") or params.get("name")
    if not channel_name:
        raise ValueError("channel name required")
    if ctx.channel_manager is None:
        raise KeyError(f"Channel not found: {channel_name}")
    if ctx.channel_manager.get(channel_name) is None:
        raise KeyError(f"Channel not found: {channel_name}")
    await ctx.channel_manager.stop_channel(channel_name)
    return {"status": "disconnected", "channel": channel_name}


@_d.method("channels.restart", scope="operator.admin")
async def _handle_channels_restart(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    channel_name = None
    if isinstance(params, dict):
        channel_name = params.get("channel") or params.get("name")
    if not channel_name:
        raise ValueError("channel name required")
    # A configured-but-not-loaded channel (e.g. added since the last gateway
    # start) cannot be restarted in place; a stable code lets the UI say
    # "restart the gateway" instead of surfacing a coarse NOT_FOUND.
    if ctx.channel_manager is None or ctx.channel_manager.get(channel_name) is None:
        raise RpcHandlerError(
            "channels.adapter_not_loaded",
            f"Channel {channel_name!r} is not loaded in this gateway process; "
            "restart the gateway to start it.",
        )
    await ctx.channel_manager.restart_channel(channel_name)
    return {"status": "restarted", "channel": channel_name}


@_d.method("channels.pairings", scope="operator.pairing")
async def _handle_channels_pairings(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    data = params or {}
    channel_name = str(data.get("channelName") or "").strip()
    if not channel_name:
        raise ValueError("channelName required")
    status = str(data.get("status") or "").strip() or None
    limit_raw = data.get("limit")
    offset_raw = data.get("offset")
    limit = int(limit_raw) if limit_raw is not None else None
    offset = int(offset_raw) if offset_raw is not None else 0
    records = _pairing_store(ctx).list_pairings(
        channel_name=channel_name,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"pairings": [_pairing_payload(record) for record in records]}


def _pairing_mutation_params(params: dict | None, ctx: RpcContext) -> tuple[str, str]:
    """Resolve the target pairing from ``pairingId`` or the 8-char ``pairingCode``.

    The code is what a sender's pairing notice shows and what the operator
    list renders, so mutations accept it directly instead of making operators
    hunt for the full id.
    """
    channel_name = str((params or {}).get("channelName") or "").strip()
    pairing_id = str((params or {}).get("pairingId") or "").strip()
    pairing_code = str((params or {}).get("pairingCode") or "").strip()
    if not channel_name:
        raise ValueError("channelName required")
    if pairing_id:
        return channel_name, pairing_id
    if not pairing_code:
        raise ValueError("pairingId or pairingCode required")
    matches = [
        record
        for record in _pairing_store(ctx).list_pairings(channel_name=channel_name)
        if str(getattr(record, "pairing_id", "")).startswith(pairing_code)
    ]
    if not matches:
        raise KeyError(f"no pairing matches code {pairing_code!r}")
    if len(matches) > 1:
        raise ValueError(f"pairing code {pairing_code!r} is ambiguous; use the full pairingId")
    return channel_name, str(matches[0].pairing_id)


def _channel_entry(ctx: RpcContext, channel_name: str) -> dict[str, Any] | None:
    for entry in _configured_channel_entries(ctx):
        if str(entry.get("name") or "") == channel_name:
            return entry
    return None


def _pairing_status_of(store: Any, channel_name: str, pairing_id: str) -> str:
    for record in store.list_pairings(channel_name=channel_name):
        if str(getattr(record, "pairing_id", "")) == pairing_id:
            return str(getattr(record, "status", ""))
    return ""


async def _send_pairing_approved_notice(ctx: RpcContext, record: Any) -> None:
    """Tell an approved sender they can start — best effort, never fatal.

    Approval is otherwise silent: the request that triggered it is not
    retained, so without this the sender is never told to send another
    message and the conversation never begins.
    """
    channel_name = str(getattr(record, "channel_name", "") or "")
    reply_to = str(getattr(record, "reply_to", "") or "")
    if not channel_name or not reply_to:
        return
    entry = _channel_entry(ctx, channel_name)
    if entry is not None and not bool(entry.get("pairing_approved_notice", True)):
        return
    manager = getattr(ctx, "channel_manager", None)
    adapter = manager.get(channel_name) if manager is not None else None
    send = getattr(adapter, "send", None)
    if not callable(send):
        return
    from opensquilla.channels.system_messages import render_channel_message
    from opensquilla.channels.types import OutgoingMessage

    try:
        await send(
            OutgoingMessage(
                content=render_channel_message("pairing_approved", config=ctx.config),
                reply_to=reply_to,
                metadata={"pairing_approved": True},
            )
        )
    except Exception as exc:  # noqa: BLE001 - the approval already succeeded
        log.warning(
            "channel.pairing_approved_notice_failed",
            channel=channel_name,
            error_type=type(exc).__name__,
        )


@_d.method("channels.pairing.approve", scope="operator.pairing")
async def _handle_channels_pairing_approve(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    channel_name, pairing_id = _pairing_mutation_params(params, ctx)
    as_admin = bool((params or {}).get("asAdmin", False))
    store = _pairing_store(ctx)
    # Re-approving an already-approved pairing must not re-notify the sender.
    was_approved = _pairing_status_of(store, channel_name, pairing_id) == "approved"
    record = store.set_pairing_status(
        channel_name=channel_name,
        pairing_id=pairing_id,
        status="approved",
    )
    payload: dict[str, Any] = {"pairing": _pairing_payload(record)}
    if as_admin:
        # Deliberate, narrow scope expansion: an operator.pairing caller may
        # mark the sender they are approving RIGHT NOW as an admin of the
        # channel they are approving them on — never an arbitrary config
        # write. This is the "this is me" shortcut in the pairing flow.
        #
        # The approval above already committed, so a failed admin grant is
        # NON-fatal: the caller learns adminGranted=false (plus a warning)
        # and the approved-sender notice below still goes out — raising here
        # would suppress the notice forever, because a retry would see the
        # pairing as already approved.
        sender_id = str(getattr(record, "sender_id", "") or "")
        if _channel_entry(ctx, channel_name) is None:
            # Same guard channels.admin.set applies to grants: a pairing can
            # outlive its channel (removal never prunes the pairing store), and
            # granting here would persist a dormant admin entry that silently
            # re-arms for any future channel created under the same name.
            log.warning(
                "channel.pairing_admin_grant_skipped_unknown_channel",
                channel=channel_name,
            )
            payload["adminGranted"] = False
            payload["warnings"] = [
                "The pairing was approved, but the channel is no longer "
                "configured, so the admin grant was skipped."
            ]
        else:
            try:
                admins = _set_channel_admin_sender(
                    ctx,
                    channel_name=channel_name,
                    sender_id=sender_id,
                    admin=True,
                )
                payload["adminGranted"] = sender_id.strip() in admins
            except Exception as exc:  # noqa: BLE001 - the approval already committed
                log.warning(
                    "channel.pairing_admin_grant_failed",
                    channel=channel_name,
                    error_type=type(exc).__name__,
                )
                payload["adminGranted"] = False
                payload["warnings"] = [
                    "The pairing was approved, but granting channel-admin standing "
                    f"failed ({type(exc).__name__}). Retry from the members view."
                ]
    if not was_approved:
        await _send_pairing_approved_notice(ctx, record)
    return payload


def _set_channel_admin_sender(
    ctx: RpcContext,
    *,
    channel_name: str,
    sender_id: str,
    admin: bool,
) -> list[str]:
    """Grant or revoke ``sender_id`` in ``channel_admin_senders[channel_name]``.

    ``admin=True`` appends the sender if absent; ``admin=False`` removes it.
    An empty admin list drops the channel key entirely so the persisted TOML
    never accumulates empty stanzas.

    Persist-before-apply: the updated mapping is written to the TOML first,
    then swapped into the live config object (which channel dispatch reads
    per message, so the change is live from the next inbound message). Both
    directions are idempotent. Returns the resulting admin list for the
    channel.
    """
    sender_id = sender_id.strip()
    if not sender_id or ctx.config is None:
        return []
    current = getattr(ctx.config, "channel_admin_senders", None)
    admin_senders: dict[str, list[str]] = {
        str(name): [
            str(item)
            for item in (values if isinstance(values, list | tuple) else [values])
        ]
        for name, values in (current or {}).items()
    }
    existing = admin_senders.get(channel_name, [])
    if admin:
        if sender_id not in existing:
            admin_senders[channel_name] = [*existing, sender_id]
    else:
        remaining = [item for item in existing if item != sender_id]
        if remaining:
            admin_senders[channel_name] = remaining
        else:
            admin_senders.pop(channel_name, None)
    from opensquilla.gateway.rpc_config import _persist_config

    # Persist-before-apply: write the candidate to disk first so a failed
    # write leaves memory and TOML agreeing on the old state.
    candidate = ctx.config.model_copy(update={"channel_admin_senders": admin_senders})
    _persist_config(candidate)
    ctx.config.channel_admin_senders = admin_senders
    log.info(
        "channel.admin_set" if admin else "channel.admin_removed",
        channel=channel_name,
        sender_id=sender_id,
    )
    return admin_senders.get(channel_name, [])


@_d.method("channels.admin.set", scope="operator.pairing")
async def _handle_channels_admin_set(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    """Grant or revoke a sender's channel-admin standing.

    The recoverable counterpart to the pairing-time admin grant: a mistaken
    grant can be lifted, and an admin added directly to the TOML can be
    promoted or demoted from the same members view. Narrow by design — it
    only edits ``channel_admin_senders`` for the named channel.
    """
    data = params or {}
    channel_name = str(data.get("channelName") or "").strip()
    sender_id = str(data.get("senderId") or "").strip()
    admin_param = data.get("admin")
    if not channel_name:
        raise ValueError("channelName required")
    if not sender_id:
        raise ValueError("senderId required")
    # An omitted admin flag must never default to a silent revoke.
    if not isinstance(admin_param, bool):
        raise ValueError("admin required (boolean)")
    admin = admin_param
    # Grants are channel-bound: a typo'd channel name would persist a dormant
    # admin entry that silently activates if a channel with that name is ever
    # created. Revokes stay unvalidated on purpose — TOML-added admins on
    # removed channels must remain demotable.
    if admin and _channel_entry(ctx, channel_name) is None:
        raise ValueError(f"unknown channel: {channel_name}")
    admins = _set_channel_admin_sender(
        ctx,
        channel_name=channel_name,
        sender_id=sender_id,
        admin=admin,
    )
    return {
        "channelName": channel_name,
        "senderId": sender_id,
        "admin": sender_id in admins,
        "admins": admins,
    }


@_d.method("channels.pairing.revoke", scope="operator.pairing")
async def _handle_channels_pairing_revoke(
    params: dict | None,
    ctx: RpcContext,
) -> dict[str, Any]:
    channel_name, pairing_id = _pairing_mutation_params(params, ctx)
    record = _pairing_store(ctx).set_pairing_status(
        channel_name=channel_name,
        pairing_id=pairing_id,
        status="revoked",
    )
    payload: dict[str, Any] = {"pairing": _pairing_payload(record)}
    # Revoking access also withdraws channel-admin standing: leaving the
    # grant dormant would let a plain Reapprove silently restore the full
    # admin tool surface the operator believed was withdrawn.
    sender_id = str(getattr(record, "sender_id", "") or "").strip()
    current = (
        getattr(ctx.config, "channel_admin_senders", None) if ctx.config is not None else None
    )
    existing = [str(item) for item in ((current or {}).get(channel_name) or [])]
    if sender_id and sender_id in existing:
        try:
            _set_channel_admin_sender(
                ctx,
                channel_name=channel_name,
                sender_id=sender_id,
                admin=False,
            )
            payload["adminRemoved"] = True
        except Exception as exc:  # noqa: BLE001 - the revoke already committed
            log.warning(
                "channel.pairing_revoke_admin_removal_failed",
                channel=channel_name,
                error_type=type(exc).__name__,
            )
            payload["adminRemoved"] = False
            payload["warnings"] = [
                "The pairing was revoked, but the sender's channel-admin "
                f"standing could not be removed ({type(exc).__name__}). "
                "Remove it from the members view."
            ]
    return payload
