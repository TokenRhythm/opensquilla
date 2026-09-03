"""Channels domain RPC handlers."""

from __future__ import annotations

import contextlib
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import structlog

from opensquilla.gateway.adapters.channel_administration import (
    GatewayChannelAdministrationAdapter,
    GatewayChannelAdministrationCallbacks,
)
from opensquilla.gateway.adapters.channel_administration_contract import (
    register_channel_administration_contract,
)
from opensquilla.gateway.channel_status_runtime import (
    ADMISSION_ADMIT_REASONS,
    configured_channel_entries,
    status_for,
)
from opensquilla.gateway.channel_status_runtime import (
    read_channel_status as _read_channel_status,
)
from opensquilla.gateway.config_persistence import persist_gateway_config
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.redaction import redact_error_text

if TYPE_CHECKING:
    from opensquilla.gateway.config import GatewayConfig

log = structlog.get_logger(__name__)

_d = get_dispatcher()

# Compatibility export for admission-vocabulary parity tests.
_ADMISSION_ADMIT_REASONS = ADMISSION_ADMIT_REASONS


def _configured_channel_entries(ctx: RpcContext) -> list[dict[str, Any]]:
    return configured_channel_entries(getattr(ctx, "config", None))


def _status_for(
    *,
    connected: bool,
    enabled: bool,
    dispatch_state: str | None,
    connection_phase: str | None,
) -> str:
    """Compatibility export for status projection tests and in-process users."""

    return status_for(
        connected=connected,
        enabled=enabled,
        dispatch_state=dispatch_state,
        connection_phase=connection_phase,
    )


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


async def read_channel_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    from opensquilla.gateway.boot import _boot_id

    return await _read_channel_status(
        config=getattr(ctx, "config", None),
        channel_manager=getattr(ctx, "channel_manager", None),
        boot_id=_boot_id,
    )


async def _handle_channels_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Compatibility callable for tests and older in-process consumers."""
    return await read_channel_status(params, ctx)


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
    # Persist-before-apply: write the candidate to disk first so a failed
    # write leaves memory and TOML agreeing on the old state.
    candidate = ctx.config.model_copy(update={"channel_admin_senders": admin_senders})
    persist_gateway_config(candidate)
    ctx.config.channel_admin_senders = admin_senders
    log.info(
        "channel.admin_set" if admin else "channel.admin_removed",
        channel=channel_name,
        sender_id=sender_id,
    )
    return admin_senders.get(channel_name, [])


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


def _channel_administration_adapter(ctx: RpcContext) -> GatewayChannelAdministrationAdapter:
    return GatewayChannelAdministrationAdapter(
        ctx,
        GatewayChannelAdministrationCallbacks(
            status=read_channel_status,
            get=_handle_channels_get,
            probe=_handle_channels_probe,
            restart=_handle_channels_restart,
            logout=_handle_channels_logout,
            pairings=_handle_channels_pairings,
            approve_pairing=_handle_channels_pairing_approve,
            revoke_pairing=_handle_channels_pairing_revoke,
            set_admin=_handle_channels_admin_set,
        ),
    )


async def _channel_status_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _channel_administration_adapter(ctx).status(params)


async def _channel_get_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _channel_administration_adapter(ctx).get(params)


async def _channel_probe_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _channel_administration_adapter(ctx).probe(params)


async def _channel_logout_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _channel_administration_adapter(ctx).logout(params)


async def _channel_restart_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _channel_administration_adapter(ctx).restart(params)


async def _channel_pairings_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _channel_administration_adapter(ctx).list_pairings(params)


async def _channel_pairing_approve_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _channel_administration_adapter(ctx).approve_pairing(params)


async def _channel_admin_set_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _channel_administration_adapter(ctx).set_admin(params)


async def _channel_pairing_revoke_contract(
    params: dict[str, Any] | None, ctx: RpcContext
) -> dict[str, Any]:
    return await _channel_administration_adapter(ctx).revoke_pairing(params)


for _channel_method, _channel_implementation in (
    ("channels.status", _channel_status_contract),
    ("channels.get", _channel_get_contract),
    ("channels.probe", _channel_probe_contract),
    ("channels.logout", _channel_logout_contract),
    ("channels.restart", _channel_restart_contract),
    ("channels.pairings", _channel_pairings_contract),
    ("channels.pairing.approve", _channel_pairing_approve_contract),
    ("channels.admin.set", _channel_admin_set_contract),
    ("channels.pairing.revoke", _channel_pairing_revoke_contract),
):
    register_channel_administration_contract(
        _d,
        _channel_method,
        _channel_implementation,
        internal_error=RpcHandlerError,
        guest_allowed_checker=is_guest_rpc_method_allowed,
    )
