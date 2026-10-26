"""Upgrade a real pre-metering database without changing historical charges."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.gateway.usage_query import query_usage_ledger
from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.storage import SessionStorage
from opensquilla.usage_metering import UsageSettlementRevision

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


async def test_v048_upgrade_preserves_bill_and_reconciles_revision_once(tmp_path):
    path = tmp_path / "sessions.db"
    backend = get_backend("sqlite:///" + str(path))
    try:
        previous = read_migrations(str(MIGRATIONS)).filter(lambda item: item.id < "V049")
        with backend.lock():
            backend.apply_migrations(backend.to_apply(previous))
    finally:
        backend.connection.close()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO usage_events (event_id,execution_id,call_index,session_id,"
            "started_at_ms,completed_at_ms,status,cost_nanos,billed_cost_nanos,"
            "cost_source,coverage_status,origin) VALUES "
            "('historical','run',0,'deleted-owner',100,200,'finalized',250000000,"
            "250000000,'provider_billed','complete','live_provider')"
        )
        conn.execute(
            "INSERT INTO usage_event_items(event_id,ordinal,cost_nanos,billed_cost_nanos,"
            "cost_source) VALUES ('historical',0,250000000,250000000,'provider_billed')"
        )
        before = conn.execute("SELECT * FROM usage_events").fetchall()
    assert apply_pending(str(path), MIGRATIONS) == [
        "V049__usage_metering_settlements",
        "V050__usage_task_attribution",
    ]
    assert apply_pending(str(path), MIGRATIONS) == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM usage_events").fetchall() == before
        assert conn.execute("SELECT COUNT(*) FROM usage_settlement_revisions").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    storage = await SessionStorage.open(str(path))
    try:
        initial = UsageSettlementRevision(
            "initial",
            "provider_confirmed",
            "final",
            currency="USD",
            native_amount="0.25",
            usd_amount="0.25",
        )
        await storage.append_usage_settlement("historical", 0, initial)
        revision = UsageSettlementRevision(
            "adjusted",
            "provider_confirmed",
            "final",
            currency="USD",
            native_amount="0.30",
            usd_amount="0.30",
            previous_revision_id="initial",
        )
        await storage.append_usage_settlement("historical", 0, revision)
        accounting_revision = await storage.get_usage_accounting_revision()
        await storage.append_usage_settlement("historical", 0, revision)
        assert await storage.get_usage_accounting_revision() == accounting_revision
        report = await query_usage_ledger(
            storage,
            {"range": {"fromMs": 1, "toMs": 1000}, "timezone": "UTC"},
            now_ms=1000,
        )
        assert report["totals"]["costUsdExact"] == "0.30"
        assert report["totals"]["eventCount"] == 1
        assert report["taskGroups"][0]["attributionStatus"] == "partial"
        assert report["taskGroups"][0]["totals"]["costUsdExact"] == "0.30"
    finally:
        await storage.close()

    # A later V049 rollback is also protected. Reject V050 first so a failed
    # downgrade cannot erase deleted owners before reaching that protection.
    backend = get_backend("sqlite:///" + str(path))
    try:
        migration = read_migrations(str(MIGRATIONS)).filter(
            lambda item: item.id == "V050__usage_task_attribution"
        )
        with backend.lock(), pytest.raises(RuntimeError, match="durable usage attribution"):
            backend.rollback_migrations(migration)
    finally:
        backend.connection.close()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_attribution_runs").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM usage_settlement_revisions").fetchone() == (2,)
