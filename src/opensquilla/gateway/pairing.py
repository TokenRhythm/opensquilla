"""Pairing-token service for the web remote-control feature.

The desktop UI creates a short-lived, one-shot operator token that a phone
consumes by scanning a QR code. The token is minted through the shared
TokenStore with source_kind="pairing" so it is indistinguishable from
ordinary tokens at rest, but:

* it expires after a short TTL (default 10 minutes);
* it can be claimed exactly once (the first WebSocket handshake that
  presents it wins);
* it carries only the scopes/capabilities the operator explicitly chose
  (default: read/write/approvals, host-execute OFF).

The pairing token itself never appears in logs or RPC responses; callers see
only the public id and expiry metadata.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from opensquilla.gateway.scopes import APPROVALS_SCOPE, READ_SCOPE, WRITE_SCOPE
from opensquilla.gateway.token_store import TokenRecord, TokenStore

PAIRING_SOURCE_KIND = "pairing"
PAIRING_DEVICE_SOURCE_KIND = "pairing_device"
DEFAULT_PAIRING_TTL_SECONDS = 600
MAX_PAIRING_TTL_SECONDS = 24 * 60 * 60
# Reconnect credential handed to the phone when a pairing is claimed: long
# enough to survive reconnects for weeks, short enough to age out.
PAIRING_DEVICE_TTL_SECONDS = 30 * 24 * 60 * 60


def pairing_device_name(pairing_public_id: str) -> str:
    return f"pairing-device:{pairing_public_id}"

# Host execution is OFF by default: a scanned phone can steer the agent,
# review output, and approve actions, but it cannot make the gateway run
# arbitrary host commands until the operator explicitly opts in.
PAIRING_DEFAULT_SCOPES = frozenset({READ_SCOPE, WRITE_SCOPE, APPROVALS_SCOPE})
PAIRING_SAFE_CAPABILITIES = frozenset({"host.read", "task.read", "task.submit"})
PAIRING_HOST_EXECUTE_CAPABILITY = "host.execute"


def render_qr_svg(data: str) -> str:
    """Render ``data`` as an inline SVG QR code for direct ``v-html`` use."""

    import re

    import segno

    qr = segno.make(data, error="m", micro=False)
    svg = qr.svg_inline(scale=6, border=2, dark="#111827", light="#ffffff")
    # segno emits fixed width/height with no viewBox, so CSS-resized SVGs
    # crop instead of scaling; inject a matching viewBox.
    return re.sub(
        r'^<svg width="(\d+)" height="(\d+)"',
        r'<svg viewBox="0 0 \1 \2" width="\1" height="\2"',
        svg,
        count=1,
    )


@dataclass(frozen=True)
class PairingInfo:
    """Public metadata for one active pairing token (never the secret)."""

    public_id: str
    expires_at: int
    created_at: int
    claimed_at: int | None = None
    last_used_at: int | None = None
    last_peer: str | None = None
    session_key: str | None = None
    allow_host_execute: bool = False

    @property
    def expired(self) -> bool:
        return int(time.time()) >= self.expires_at


class PairingService:
    """Create, claim, list, and revoke one-shot pairing tokens."""

    def __init__(self, token_store: TokenStore) -> None:
        self._store = token_store

    @staticmethod
    def for_state_dir(state_dir: str | Path) -> PairingService:
        return PairingService(TokenStore(Path(str(state_dir)) / "sessions.db"))

    def create(
        self,
        *,
        session_key: str | None = None,
        expires_in_seconds: int = DEFAULT_PAIRING_TTL_SECONDS,
        allow_host_execute: bool = False,
    ) -> tuple[str, PairingInfo]:
        """Mint a one-shot pairing token.

        Returns (token, info). The token is returned exactly once to the
        caller (the desktop UI) which embeds it into the QR URL; it is never
        stored in logs and cannot be recovered later.
        """

        ttl = int(expires_in_seconds)
        if ttl <= 0:
            raise ValueError("expiresInSeconds must be positive")
        if ttl > MAX_PAIRING_TTL_SECONDS:
            raise ValueError(f"expiresInSeconds must be <= {MAX_PAIRING_TTL_SECONDS}")

        capabilities = set(PAIRING_SAFE_CAPABILITIES)
        if allow_host_execute:
            capabilities.add(PAIRING_HOST_EXECUTE_CAPABILITY)

        name = "pairing"
        if session_key and str(session_key).strip():
            name = f"pairing:{str(session_key).strip()[:160]}"

        expires_at = int(time.time()) + ttl
        issued = self._store.create(
            name=name,
            roles={"operator"},
            scopes=PAIRING_DEFAULT_SCOPES,
            capabilities=capabilities,
            source_kind=PAIRING_SOURCE_KIND,
            expires_at=expires_at,
        )
        info = PairingInfo(
            public_id=issued.record.public_id,
            expires_at=expires_at,
            created_at=issued.record.created_at,
            session_key=(str(session_key).strip() if session_key else None),
            allow_host_execute=allow_host_execute,
        )
        return issued.token, info

    def try_claim(self, public_id: str) -> bool:
        """Atomically claim a pairing token; only the first caller wins."""

        return self._store.claim(public_id)

    def revoke(self, public_id: str) -> bool:
        revoked = self._store.revoke(public_id)
        # A claimed phone holds a long-lived device credential minted from
        # this pairing; revoking the pairing must cut that credential off.
        self._store.revoke_by_name(pairing_device_name(public_id))
        return revoked

    def list_active(self) -> list[PairingInfo]:
        """Return active pairing tokens (claimed or not) with metadata."""

        result: list[PairingInfo] = []
        for record in self._store.list_active():
            if record.source_kind != PAIRING_SOURCE_KIND:
                continue
            if record.expires_at is not None and int(time.time()) >= int(record.expires_at):
                self._store.revoke(record.public_id)
                continue
            result.append(self._to_info(record))
        return result

    def get(self, public_id: str) -> PairingInfo | None:
        record = self._store.get(public_id)
        if record is None or record.source_kind != PAIRING_SOURCE_KIND:
            return None
        return self._to_info(record)

    @staticmethod
    def _to_info(record: TokenRecord) -> PairingInfo:
        session_key = None
        if record.name.startswith("pairing:"):
            session_key = record.name[len("pairing:") :] or None
        return PairingInfo(
            public_id=record.public_id,
            expires_at=record.expires_at or 0,
            created_at=record.created_at,
            claimed_at=record.claimed_at,
            last_used_at=record.last_used_at,
            last_peer=record.last_peer,
            session_key=session_key,
            allow_host_execute=PAIRING_HOST_EXECUTE_CAPABILITY in record.capabilities,
        )


__all__ = [
    "DEFAULT_PAIRING_TTL_SECONDS",
    "MAX_PAIRING_TTL_SECONDS",
    "PAIRING_DEFAULT_SCOPES",
    "PAIRING_DEVICE_SOURCE_KIND",
    "PAIRING_DEVICE_TTL_SECONDS",
    "PAIRING_SAFE_CAPABILITIES",
    "PAIRING_SOURCE_KIND",
    "PairingInfo",
    "PairingService",
    "pairing_device_name",
]
