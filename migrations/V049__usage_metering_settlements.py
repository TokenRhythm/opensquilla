"""V049 - immutable usage measurements, price snapshots and settlement revisions."""

from yoyo import step

__depends__ = {"V048__office_materializations"}

# Frozen DDL; later runtime schema changes must use another migration.
USAGE_METERING_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS usage_accounting_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL)""",
    """INSERT OR IGNORE INTO usage_accounting_state VALUES(1,0)""",
    """CREATE TABLE IF NOT EXISTS usage_price_snapshots (
        snapshot_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS usage_item_metering (
        event_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
        meters_json TEXT NOT NULL, price_snapshot_id TEXT,
        token_usage_known INTEGER NOT NULL CHECK(token_usage_known IN (0,1)),
        PRIMARY KEY(event_id, ordinal),
        FOREIGN KEY(event_id,ordinal) REFERENCES usage_event_items(event_id,ordinal)
            ON DELETE CASCADE,
        FOREIGN KEY(price_snapshot_id) REFERENCES usage_price_snapshots(snapshot_id))""",
    """CREATE TABLE IF NOT EXISTS usage_settlement_revisions (
        event_id TEXT NOT NULL, ordinal INTEGER NOT NULL, revision_id TEXT NOT NULL,
        revision_no INTEGER NOT NULL CHECK(revision_no > 0), payload_json TEXT NOT NULL,
        PRIMARY KEY(event_id,ordinal,revision_id), UNIQUE(event_id,ordinal,revision_no),
        FOREIGN KEY(event_id,ordinal) REFERENCES usage_event_items(event_id,ordinal)
            ON DELETE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS usage_meter_observations (
        event_id TEXT NOT NULL, ordinal INTEGER NOT NULL, revision_id TEXT NOT NULL,
        meters_json TEXT NOT NULL, PRIMARY KEY(event_id,ordinal,revision_id),
        FOREIGN KEY(event_id,ordinal,revision_id)
            REFERENCES usage_settlement_revisions(event_id,ordinal,revision_id)
            ON DELETE CASCADE)""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_events_insert
        AFTER INSERT ON usage_events BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_events_update
        AFTER UPDATE ON usage_events BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_events_delete
        AFTER DELETE ON usage_events BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_event_items_insert
        AFTER INSERT ON usage_event_items BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_event_items_update
        AFTER UPDATE ON usage_event_items BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_event_items_delete
        AFTER DELETE ON usage_event_items BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_item_billing_receipts_insert
        AFTER INSERT ON usage_item_billing_receipts BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_item_billing_receipts_update
        AFTER UPDATE ON usage_item_billing_receipts BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_item_billing_receipts_delete
        AFTER DELETE ON usage_item_billing_receipts BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_item_metering_insert
        AFTER INSERT ON usage_item_metering BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_item_metering_update
        AFTER UPDATE ON usage_item_metering BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_item_metering_delete
        AFTER DELETE ON usage_item_metering BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_settlement_revisions_insert
        AFTER INSERT ON usage_settlement_revisions BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_settlement_revisions_update
        AFTER UPDATE ON usage_settlement_revisions BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_settlement_revisions_delete
        AFTER DELETE ON usage_settlement_revisions BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_meter_observations_insert
        AFTER INSERT ON usage_meter_observations BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_meter_observations_update
        AFTER UPDATE ON usage_meter_observations BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_meter_observations_delete
        AFTER DELETE ON usage_meter_observations BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_legacy_baselines_insert
        AFTER INSERT ON usage_legacy_baselines BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_legacy_baselines_update
        AFTER UPDATE ON usage_legacy_baselines BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_legacy_baselines_delete
        AFTER DELETE ON usage_legacy_baselines BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_ledger_state_insert
        AFTER INSERT ON usage_ledger_state BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_ledger_state_update
        AFTER UPDATE ON usage_ledger_state BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_ledger_state_delete
        AFTER DELETE ON usage_ledger_state BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_billing_receipt_state_insert
        AFTER INSERT ON usage_billing_receipt_state BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_billing_receipt_state_update
        AFTER UPDATE ON usage_billing_receipt_state BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
    """CREATE TRIGGER IF NOT EXISTS usage_revision_usage_billing_receipt_state_delete
        AFTER DELETE ON usage_billing_receipt_state BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END""",
)


def apply_step(conn):
    for statement in USAGE_METERING_SCHEMA:
        conn.execute(statement)


def rollback_step(conn):
    if (
        conn.execute("SELECT 1 FROM usage_item_metering LIMIT 1").fetchone()
        or conn.execute("SELECT 1 FROM usage_settlement_revisions LIMIT 1").fetchone()
    ):
        raise RuntimeError("Cannot discard recorded usage settlement revisions")
    for statement in USAGE_METERING_SCHEMA:
        if statement.startswith("CREATE TRIGGER"):
            name = statement.split()[5]
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    for name in (
        "usage_meter_observations",
        "usage_settlement_revisions",
        "usage_item_metering",
        "usage_price_snapshots",
        "usage_accounting_state",
    ):
        conn.execute(f"DROP TABLE IF EXISTS {name}")


steps = [step(apply_step, rollback_step)]
