"""Pairing RPC handlers for the web remote-control feature.

The desktop UI calls gateway.pairing.create to mint a one-shot QR token.
All three methods are owner-only: creating a pairing token grants operator
scopes to whoever scans the QR code, so a non-owner must never be able to
mint one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import structlog

from opensquilla.gateway.pairing import PairingService, render_qr_svg
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.tunnel import TunnelInfo, TunnelManager, TunnelUnavailableError
from opensquilla.sandbox.setup_runtime import current_sandbox_capability_report

_log = structlog.get_logger(__name__)

_d = get_dispatcher()

_PAIRING_SERVICE: PairingService | None = None
_TUNNEL_MANAGER: TunnelManager | None = None


def _pairing_service_for(ctx: RpcContext) -> PairingService:
    global _PAIRING_SERVICE
    if _PAIRING_SERVICE is None:
        state_dir = getattr(ctx.config, "state_dir", None)
        if not state_dir:
            raise RpcHandlerError(
                "UNAVAILABLE", "Gateway state directory is not configured"
            )
        _PAIRING_SERVICE = PairingService.for_state_dir(state_dir)
    return _PAIRING_SERVICE


def _require_owner(ctx: RpcContext) -> None:
    if not getattr(ctx.principal, "is_owner", False):
        raise RpcHandlerError(
            "UNAUTHORIZED", "gateway.pairing.* requires owner principal."
        )


# Injectable for tests and for the tunnel manager (C stage) to override the
# base URL with a public tunnel domain.
_BASE_URL_PROVIDER: Callable[[RpcContext], str] | None = None


def set_pairing_base_url_provider(
    provider: Callable[[RpcContext], str] | None
) -> None:
    global _BASE_URL_PROVIDER
    _BASE_URL_PROVIDER = provider


async def _base_url(ctx: RpcContext) -> str:
    if _BASE_URL_PROVIDER is not None:
        return _BASE_URL_PROVIDER(ctx)
    tunnel = await _ensure_tunnel(ctx)
    return tunnel.base_url


async def _ensure_tunnel(ctx: RpcContext) -> TunnelInfo:
    global _TUNNEL_MANAGER
    if _TUNNEL_MANAGER is None:
        config = ctx.config
        state_dir = getattr(config, "state_dir", None)
        _TUNNEL_MANAGER = TunnelManager(
            port=getattr(config, "port", 18791),
            bind_host=getattr(config, "host", "127.0.0.1"),
            control_base_path=getattr(config.control_ui, "base_path", "/control"),
            # Keep the downloaded binary across restarts (~30 MB).
            download_dir=Path(state_dir) / "bin" if state_dir else None,
        )
    try:
        return await asyncio.to_thread(_TUNNEL_MANAGER.ensure_tunnel)
    except TunnelUnavailableError as exc:
        # Remote control is tunnel-only: without a registered edge connection
        # there is no reachable URL to hand the phone, and issuing one anyway
        # would only produce a QR that resolves to Cloudflare error 1033.
        raise RpcHandlerError("UNAVAILABLE", str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise RpcHandlerError(
            "UNAVAILABLE",
            f"Tunnel setup failed: {exc}",
        ) from exc


def _pairing_url(base_url: str, token: str, session_key: str | None) -> str:
    """Build the scan URL with the secret in the fragment, never the query.

    A query string is part of the request target: the browser sends it to the
    server on the very first navigation, so it can land in access logs,
    proxy logs, and referrer-adjacent telemetry *before* any client-side
    scrubbing runs. A fragment is never transmitted, which keeps the one-shot
    pairing secret out of that initial request entirely. The session hint is
    not a credential and stays in the query so ordinary link handling and
    server-side routing keep working.
    """

    url = base_url.rstrip(chr(47))
    if session_key:
        url += f"/?session={quote(session_key, safe='')}"
    else:
        url += "/"
    return f"{url}#token={quote(token, safe='')}"



async def _safe_mode_warning(ctx: RpcContext, *, allow_host_execute: bool) -> str | None:
    """Fail before minting a token the phone could never send a turn with.

    A pairing without ``host.execute`` is restricted to Safe mode. When the
    host Safe sandbox is unavailable, ``resolve_mode`` fail-closes every
    request from that phone (``sandbox_unavailable_for_guest`` for Safe and
    ``host_capability_required`` for Full), so the phone connects, renders the
    chat, and then silently rejects every send. Surfacing the dead end here
    keeps that boundary intact while telling the owner what to do about it.
    """

    if allow_host_execute:
        return None
    try:
        capability = await current_sandbox_capability_report(ctx.config)
    except Exception:  # noqa: BLE001 - capability probing must not block pairing
        _log.warning("gateway.pairing.capability_probe_unavailable", exc_info=True)
        return None
    if capability.available:
        return None
    _log.info(
        "gateway.pairing.safe_mode_unavailable",
        backend=capability.backend,
        code=capability.code,
    )
    return capability.code or "sandbox_unavailable"


@_d.method("gateway.pairing.create", scope="operator.write")
async def _handle_pairing_create(
    params: dict | None, ctx: RpcContext
) -> dict[str, Any]:
    _require_owner(ctx)
    params = params if isinstance(params, dict) else {}

    session_key = params.get("sessionKey") or params.get("session_key")
    if session_key is not None and not isinstance(session_key, str):
        raise ValueError("sessionKey must be a string")
    expires_in = params.get("expiresInSeconds", 600)
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        raise ValueError("expiresInSeconds must be an integer")
    allow_host_execute = bool(params.get("allowHostExecute", False))
    safe_mode_warning = await _safe_mode_warning(
        ctx, allow_host_execute=allow_host_execute
    )

    service = _pairing_service_for(ctx)
    token, info = service.create(
        session_key=session_key,
        expires_in_seconds=expires_in,
        allow_host_execute=allow_host_execute,
    )
    try:
        base_url = await _base_url(ctx)
    except Exception:
        # Any failure to build the reachable URL must not leave a live token
        # behind: the desktop UI would then list a phantom "pending device"
        # that no phone can ever claim.
        service.revoke(info.public_id)
        raise
    pairing_url = _pairing_url(base_url, token, info.session_key)
    _log.info(
        "gateway.pairing.created",
        public_id=info.public_id,
        session_bound=info.session_key is not None,
        allow_host_execute=info.allow_host_execute,
    )
    return {
        "pairingUrl": pairing_url,
        "qrCodeData": render_qr_svg(pairing_url),
        # Epoch seconds for API consistency with gateway.pairing.list, plus an
        # explicit millisecond field for JS clients comparing against Date.now().
        "expiresAt": info.expires_at,
        "expiresAtMs": info.expires_at * 1000,
        "publicId": info.public_id,
        "sessionKey": info.session_key,
        "allowHostExecute": info.allow_host_execute,
        "safeModeUnavailableReason": safe_mode_warning,
    }


@_d.method("gateway.pairing.revoke", scope="operator.write")
async def _handle_pairing_revoke(
    params: dict | None, ctx: RpcContext
) -> dict[str, Any]:
    _require_owner(ctx)
    params = params if isinstance(params, dict) else {}
    public_id = params.get("publicId") or params.get("public_id")
    if not isinstance(public_id, str) or not public_id.strip():
        raise ValueError("publicId is required")
    revoked = _pairing_service_for(ctx).revoke(public_id.strip())
    return {"publicId": public_id.strip(), "revoked": revoked}


@_d.method("gateway.pairing.list", scope="operator.read")
async def _handle_pairing_list(
    params: dict | None, ctx: RpcContext
) -> dict[str, Any]:
    _require_owner(ctx)
    pairings = [
        {
            "publicId": info.public_id,
            "expiresAt": info.expires_at,
            "createdAt": info.created_at,
            "claimedAt": info.claimed_at,
            "lastUsedAt": info.last_used_at,
            "lastPeer": info.last_peer,
            "sessionKey": info.session_key,
            "allowHostExecute": info.allow_host_execute,
        }
        for info in _pairing_service_for(ctx).list_active()
    ]
    return {"pairings": pairings}


__all__ = [
    "set_pairing_base_url_provider",
]
