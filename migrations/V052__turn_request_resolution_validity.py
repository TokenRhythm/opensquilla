"""Bound terminal markers by immutable, process-owned request validity."""

from __future__ import annotations

from yoyo import step

from opensquilla.persistence.turn_request_resolution import (
    TURN_REQUEST_RESOLUTION_EXPIRY_INDEX,
)

__depends__: set[str] = {"V051__turn_request_resolution"}


def apply_step(conn) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(turn_request_resolution)")}
    if "validity_boot_id" not in columns:
        conn.execute(
            "ALTER TABLE turn_request_resolution "
            "ADD COLUMN validity_boot_id TEXT NOT NULL DEFAULT ''"
        )
    if "validity_expires_hour" not in columns:
        conn.execute(
            "ALTER TABLE turn_request_resolution "
            "ADD COLUMN validity_expires_hour INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(TURN_REQUEST_RESOLUTION_EXPIRY_INDEX)


def rollback_step(conn) -> None:
    # Keep additive metadata/index so a later upgrade can recover pending
    # evidence; older versions ignore these columns.
    pass


steps = [step(apply_step, rollback_step)]
