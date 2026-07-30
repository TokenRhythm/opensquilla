"""V024 - per-item provider-native billing receipts.

The existing usage ledger remains the canonical USD accounting surface. This
additive migration records each physical provider receipt in its native
currency, its settlement state, and the exact normalization basis used for the
canonical USD equivalent. Historical ledger rows are intentionally not
backfilled; the singleton state row marks the exact-coverage cutover.
"""

from __future__ import annotations

import time

from yoyo import step

__depends__: set[str] = {"V023__router_deployment_telemetry"}


CREATE_USAGE_ITEM_BILLING_RECEIPTS = """
CREATE TABLE IF NOT EXISTS usage_item_billing_receipts (
    event_id                    TEXT NOT NULL,
    ordinal                     INTEGER NOT NULL CHECK (ordinal >= 0),
    currency                    TEXT NOT NULL
                                CHECK (length(currency) = 3 AND currency = upper(currency)),
    status                      TEXT NOT NULL
                                CHECK (status IN ('confirmed', 'pending')),
    amount_nanos                INTEGER CHECK (amount_nanos >= 0),
    usd_equivalent_nanos        INTEGER CHECK (usd_equivalent_nanos >= 0),
    fx_native_per_usd_nanos     INTEGER NOT NULL
                                CHECK (fx_native_per_usd_nanos > 0),
    schema_version              INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1),
    PRIMARY KEY (event_id, ordinal),
    FOREIGN KEY (event_id, ordinal)
        REFERENCES usage_event_items(event_id, ordinal) ON DELETE CASCADE,
    CHECK (
        (status = 'confirmed' AND amount_nanos IS NOT NULL
         AND usd_equivalent_nanos IS NOT NULL)
        OR
        (status = 'pending' AND usd_equivalent_nanos IS NULL)
    )
)
"""

CREATE_USAGE_BILLING_RECEIPT_STATE = """
CREATE TABLE IF NOT EXISTS usage_billing_receipt_state (
    singleton_id                INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    tracking_started_at_ms      INTEGER NOT NULL CHECK (tracking_started_at_ms >= 0),
    schema_version              INTEGER NOT NULL DEFAULT 1 CHECK (schema_version >= 1)
)
"""


def apply_step(conn) -> None:
    cur = conn.cursor()
    cur.execute(CREATE_USAGE_ITEM_BILLING_RECEIPTS)
    cur.execute(CREATE_USAGE_BILLING_RECEIPT_STATE)
    cur.execute(
        """
        INSERT OR IGNORE INTO usage_billing_receipt_state (
            singleton_id, tracking_started_at_ms, schema_version
        ) VALUES (1, ?, 1)
        """,
        (time.time_ns() // 1_000_000,),
    )


def rollback_step(conn) -> None:
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS usage_item_billing_receipts")
    cur.execute("DROP TABLE IF EXISTS usage_billing_receipt_state")


steps = [step(apply_step, rollback_step)]
