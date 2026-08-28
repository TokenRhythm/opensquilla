"""Server-side auth resolution: Principal + ScopeResolver.

Two resolver strategies live here:

* :class:`TokenScopeResolver` validates a shared token and issues a
  Principal whose scopes come from ``config.auth.token_scopes`` — normalized
  via :func:`opensquilla.gateway.scopes.normalize_operator_scopes` so that a
  token declared with ``["operator.write"]`` behaves identically to one
  declared with ``["operator.write", "operator.read"]``.
* :class:`OpenScopeResolver` serves no-auth mode. An operator who connects
  from a loopback peer to a loopback-bound gateway is treated as the local
  owner and gets :data:`CLI_DEFAULT_OPERATOR_SCOPES`. Any other no-auth
  operator — including a loopback peer on a gateway bound to ``0.0.0.0``
  — gets the narrower :data:`REMOTE_OPERATOR_SCOPES` set (no ``admin``,
  no ``pairing``).

The loopback-upgrade path is the mechanism by which the Control UI gets
admin privileges in the default single-machine deployment. Remote browser
access remains ungranted until a deliberate token is configured.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import secrets
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

import structlog

from opensquilla.gateway.scopes import (
    CLI_DEFAULT_OPERATOR_SCOPES,
    GUEST_SAFE_CAPABILITIES,
    HUMAN_TOKEN_CAPABILITIES,
    LOCAL_OWNER_CAPABILITIES,
    NODE_DEFAULT_SCOPES,
    REMOTE_OPERATOR_SCOPES,
    is_loopback_address,
    is_loopback_bind,
    normalize_operator_scopes,
)
from opensquilla.gateway.token_store import TokenRecord, TokenStore, token_public_id

if TYPE_CHECKING:
    from opensquilla.gateway.config import GatewayConfig

log = structlog.get_logger(__name__)

_PRIVATE_CLIENT_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
    )
)

_GUEST_SESSION_KEY_RE = re.compile(r"^osqg_[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True)
class Principal:
    """Server-computed identity credential. Immutable. Lifetime = connection.

    ``is_owner`` flags the caller as a locally-proven gateway owner. It is
    **advisory only** — authorization decisions must go through
    :mod:`opensquilla.gateway.scopes` so that the scope set (not the flag)
    governs what a caller may do. Non-gateway consumers (tool dispatch,
    scheduler handlers) still read the flag for owner-only tool gating;
    the field remains for their benefit.
    """

    role: str  # "operator" | "node"
    scopes: frozenset[str]  # server-computed, not client-declared
    is_owner: bool  # operator on a loopback-proven channel → True
    authenticated: bool
    capabilities: frozenset[str] = frozenset()
    auth_state: Literal["authenticated", "guest", "invalid"] | None = None
    token_public_id: str | None = None
    guest_owner_id: str | None = None
    guest_session_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.auth_state is None:
            state: Literal["authenticated", "guest", "invalid"] = (
                "authenticated" if self.authenticated or self.is_owner else "guest"
            )
            object.__setattr__(self, "auth_state", state)
        if not self.capabilities:
            if self.role == "operator" and (self.authenticated or self.is_owner):
                object.__setattr__(self, "capabilities", LOCAL_OWNER_CAPABILITIES)
            elif not self.authenticated:
                object.__setattr__(self, "capabilities", GUEST_SAFE_CAPABILITIES)

    def has(self, capability: str) -> bool:
        return str(capability) in self.capabilities


@runtime_checkable
class ScopeResolver(Protocol):
    """Strategy interface for auth-mode-specific scope computation."""

    def resolve(
        self,
        auth_params: dict,
        role_claim: str,
        config: GatewayConfig,
        *,
        peer_ip: str | None = None,
    ) -> Principal: ...


class TokenScopeResolver:
    """Token mode: validate token, compute scopes from config, ignore client claims."""

    def resolve(
        self,
        auth_params: dict,
        role_claim: str,
        config: GatewayConfig,
        *,
        peer_ip: str | None = None,
    ) -> Principal:
        allowed_roles = config.auth.allowed_roles
        if role_claim not in allowed_roles:
            raise ValueError(f"Invalid role: {role_claim!r}")
        if not _private_or_unknown_peer(
            peer_ip,
            allowed_cidrs=config.auth.allowed_client_cidrs,
        ):
            raise ValueError("Public peers are not accepted")

        provided = str((auth_params or {}).get("token") or "")
        if provided and role_claim == "operator":
            from opensquilla.gateway.desktop_ownership import (
                active_desktop_gateway_auth_token_matches,
            )

            if (
                is_loopback_bind(config.host)
                and is_loopback_address(peer_ip)
                and active_desktop_gateway_auth_token_matches(provided)
            ):
                return Principal(
                    role="operator",
                    scopes=normalize_operator_scopes(config.auth.token_scopes),
                    is_owner=True,
                    authenticated=True,
                    capabilities=HUMAN_TOKEN_CAPABILITIES,
                    auth_state="authenticated",
                    token_public_id="desktop",
                )
        if not provided:
            return _guest_principal(
                auth_state="guest",
                guest_session_key=_resolve_guest_session_key(auth_params),
            )

        named_record = _verify_named_token(config, provided, peer_ip=peer_ip)
        if named_record is not None:
            if role_claim not in named_record.roles:
                return _guest_principal(
                    auth_state="invalid",
                    public_id=named_record.public_id,
                    guest_session_key=_resolve_guest_session_key(auth_params),
                )
            scopes = (
                NODE_DEFAULT_SCOPES
                if role_claim == "node"
                else normalize_operator_scopes(named_record.scopes)
            )
            return Principal(
                role=role_claim,
                scopes=scopes,
                is_owner=False,
                authenticated=True,
                capabilities=named_record.capabilities,
                auth_state="authenticated",
                token_public_id=named_record.public_id,
            )

        configured = str(config.auth.token or "")
        valid_legacy = bool(configured) and secrets.compare_digest(provided, configured)
        if not valid_legacy:
            return _guest_principal(
                auth_state="invalid",
                public_id=token_public_id(provided),
                guest_session_key=_resolve_guest_session_key(auth_params),
            )

        if role_claim == "node":
            scopes = NODE_DEFAULT_SCOPES
            capabilities: frozenset[str] = frozenset()
            is_owner = False
        else:
            scopes = normalize_operator_scopes(config.auth.token_scopes)
            capabilities = HUMAN_TOKEN_CAPABILITIES
            # Owner flag follows proximity, not the token — a shared token
            # used from a LAN peer should not claim ownership.
            is_owner = is_loopback_bind(config.host) and is_loopback_address(peer_ip)

        return Principal(
            role=role_claim,
            scopes=scopes,
            is_owner=is_owner,
            authenticated=True,
            capabilities=capabilities,
            auth_state="authenticated",
            token_public_id="legacy",
        )


class OpenScopeResolver:
    """No-auth mode with loopback-scoped admin upgrade.

    Scope issuance depends on transport provenance:

    * Operator on a loopback-bound gateway connecting from a loopback peer
      → :data:`CLI_DEFAULT_OPERATOR_SCOPES` (includes ``admin`` and
      ``pairing``). This is the Control UI / local CLI case.
    * Operator elsewhere → :data:`REMOTE_OPERATOR_SCOPES` (``read`` /
      ``write`` / ``approvals``; no ``admin``). A gateway bound to
      ``0.0.0.0`` accepts remote peers and must not auto-upgrade even a
      loopback client, because that client could be a reverse-tunnel
      relay.
    * Node → :data:`NODE_DEFAULT_SCOPES` regardless of peer address.

    Debug mode never changes transport-derived authority. A remote no-auth
    caller remains a guest even during development.
    """

    def resolve(
        self,
        auth_params: dict,
        role_claim: str,
        config: GatewayConfig,
        *,
        peer_ip: str | None = None,
    ) -> Principal:
        allowed_roles = config.auth.allowed_roles
        if role_claim not in allowed_roles:
            raise ValueError(f"Invalid role: {role_claim!r}")
        if not _private_or_unknown_peer(
            peer_ip,
            allowed_cidrs=config.auth.allowed_client_cidrs,
        ):
            raise ValueError("Public peers are not accepted")

        if role_claim == "node":
            return Principal(
                role="node",
                scopes=NODE_DEFAULT_SCOPES,
                is_owner=False,
                authenticated=False,
                capabilities=frozenset(),
                auth_state="guest",
            )

        local_owner = is_loopback_bind(config.host) and is_loopback_address(peer_ip)

        if local_owner:
            scopes = CLI_DEFAULT_OPERATOR_SCOPES
        else:
            scopes = REMOTE_OPERATOR_SCOPES
        guest_session_key = None
        guest_owner_id = None
        if not local_owner:
            guest_session_key = _resolve_guest_session_key(auth_params)
            guest_owner_id = _guest_owner_id(guest_session_key)

        return Principal(
            role=role_claim,
            scopes=scopes,
            is_owner=local_owner,
            authenticated=False,
            capabilities=(
                LOCAL_OWNER_CAPABILITIES if local_owner else GUEST_SAFE_CAPABILITIES
            ),
            auth_state="authenticated" if local_owner else "guest",
            guest_owner_id=guest_owner_id,
            guest_session_key=guest_session_key,
        )


def _private_or_unknown_peer(
    peer_ip: str | None,
    *,
    allowed_cidrs: list[str] | tuple[str, ...] = (),
) -> bool:
    if peer_ip is None:
        return True
    try:
        address = ipaddress.ip_address(str(peer_ip).split("%", 1)[0])
    except ValueError:
        # Starlette's in-process test transport uses a symbolic peer name.
        # Real socket peers are always parsed IP literals at this boundary.
        return True
    # ``allowed_client_cidrs`` is a remote-client allowlist.  It must not lock
    # the desktop app out of its own loopback gateway.  Authority is still
    # decided separately from the configured bind address below, so a
    # loopback peer on a wildcard-bound gateway does not become an owner.
    if address.is_loopback:
        return True
    networks = (
        tuple(ipaddress.ip_network(value) for value in allowed_cidrs)
        if allowed_cidrs
        else _PRIVATE_CLIENT_NETWORKS
    )
    return any(
        address.version == network.version and address in network
        for network in networks
    )


def _guest_principal(
    *,
    auth_state: Literal["guest", "invalid"],
    public_id: str | None = None,
    guest_session_key: str | None = None,
) -> Principal:
    resolved_guest_key = guest_session_key or _new_guest_session_key()
    return Principal(
        role="operator",
        scopes=REMOTE_OPERATOR_SCOPES,
        is_owner=False,
        authenticated=False,
        capabilities=GUEST_SAFE_CAPABILITIES,
        auth_state=auth_state,
        token_public_id=public_id,
        guest_owner_id=_guest_owner_id(resolved_guest_key),
        guest_session_key=resolved_guest_key,
    )


def _new_guest_session_key() -> str:
    return f"osqg_{secrets.token_urlsafe(32)}"


def _resolve_guest_session_key(auth_params: dict | None) -> str:
    candidate = str((auth_params or {}).get("guestSessionKey") or "")
    if _GUEST_SESSION_KEY_RE.fullmatch(candidate):
        return candidate
    return _new_guest_session_key()


def _guest_owner_id(guest_session_key: str) -> str:
    return hashlib.sha256(guest_session_key.encode("utf-8")).hexdigest()


def _verify_named_token(
    config: GatewayConfig,
    token: str,
    *,
    peer_ip: str | None,
) -> TokenRecord | None:
    if not token.startswith("osq_"):
        return None
    state_dir = getattr(config, "state_dir", None)
    if not state_dir:
        return None
    try:
        return TokenStore(Path(str(state_dir)) / "sessions.db").verify(
            token,
            peer_ip=peer_ip,
        )
    except (OSError, sqlite3.Error):
        log.exception("auth.named_token_store_unavailable")
        return None


_RESOLVERS: dict[str, ScopeResolver] = {
    "token": TokenScopeResolver(),
    "none": OpenScopeResolver(),
}


def resolve_auth(
    config: GatewayConfig,
    auth_params: dict,
    role_claim: str,
    *,
    peer_ip: str | None = None,
) -> Principal | None:
    """Pick resolver by auth mode, return Principal or None on failure.

    ``peer_ip`` is the caller's IP as observed at the transport layer
    (WebSocket upgrade, HTTP request). It is consulted for loopback
    proximity checks in :class:`OpenScopeResolver` and to set the
    ``is_owner`` flag in :class:`TokenScopeResolver`. ``None`` is treated
    as "unknown" — non-loopback for the purposes of any upgrade.
    """
    resolver = _RESOLVERS.get(config.auth.mode)
    if resolver is None:
        log.warning("auth.unsupported_mode", mode=config.auth.mode)
        return None
    try:
        return resolver.resolve(auth_params, role_claim, config, peer_ip=peer_ip)
    except ValueError as exc:
        log.warning("auth.failed", mode=config.auth.mode, error=str(exc))
        return None
