"""Terminal evidence for the explicitly versioned WebChat ingress."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass

EPOCH_VALID_HOURS = 25
RESOLUTION_CLEANUP_BATCH = 64
_EPOCH = re.compile(r"v2\.([0-9a-f]{32})\.(0|[1-9][0-9]{0,9})\.(0|[1-9][0-9]{0,9})\Z")


@dataclass(frozen=True, slots=True)
class TurnReceiptEpoch:
    boot_id: str
    issued_hour: int
    expires_hour: int


def parse_turn_receipt_epoch(value: object) -> TurnReceiptEpoch | None:
    match = _EPOCH.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    issued, expires = int(match[2]), int(match[3])
    if expires != issued + EPOCH_VALID_HOURS:
        return None
    return TurnReceiptEpoch(match[1], issued, expires)


def request_epoch(client_request_id: str) -> TurnReceiptEpoch | None:
    if not client_request_id.startswith("tr2:") or len(client_request_id) > 256:
        return None
    epoch, separator, logical_id = client_request_id[4:].partition(":")
    return parse_turn_receipt_epoch(epoch) if separator and logical_id.strip() else None


def request_binds_epoch(client_request_id: object, epoch: object) -> bool:
    return (
        isinstance(client_request_id, str)
        and request_epoch(client_request_id) is not None
        and client_request_id.startswith(f"tr2:{epoch}:")
    )


class TurnReceiptEpochPolicy:
    """Process-owned validity; wall-clock changes cannot reopen an expired request."""

    def __init__(self, boot_id: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", boot_id) is None:
            raise ValueError("Receipt epoch requires a Gateway boot nonce")
        self.boot_id = boot_id
        self._clock = clock
        self._started_at = clock()
        self._hour = 0

    @property
    def current_hour(self) -> int:
        self._hour = max(self._hour, int((self._clock() - self._started_at) // 3600))
        return self._hour

    def current_epoch(self) -> str:
        hour = self.current_hour
        return f"v2.{self.boot_id}.{hour}.{hour + EPOCH_VALID_HOURS}"

    def accepts(self, epoch: TurnReceiptEpoch | None) -> bool:
        return (
            epoch is not None and epoch.boot_id == self.boot_id
            and epoch.issued_hour <= self.current_hour < epoch.expires_hour
        )


TURN_REQUEST_RESOLUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_request_resolution (
    source_scope TEXT NOT NULL,
    request_session_key TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('retired', 'accepted')),
    updated_at INTEGER NOT NULL,
    validity_boot_id TEXT NOT NULL DEFAULT '',
    validity_expires_hour INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_scope, request_session_key, client_request_id)
)
"""

TURN_REQUEST_RESOLUTION_EXPIRY_INDEX = """
CREATE INDEX IF NOT EXISTS idx_turn_request_resolution_expiry
ON turn_request_resolution(validity_boot_id, validity_expires_hour)
"""
