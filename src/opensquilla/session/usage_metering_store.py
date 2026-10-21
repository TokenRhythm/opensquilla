"""Additive metering facts and append-only settlement revisions on usage items."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal, localcontext
from typing import Any

from opensquilla.session.usage_ledger import UsageLedgerConflictError
from opensquilla.usage_metering import (
    UsageItemMetering,
    UsageMeter,
    UsagePriceSnapshot,
    UsageSettlementRevision,
    canonical_json,
)

USAGE_METERING_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS usage_accounting_state (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL)""",
    "INSERT OR IGNORE INTO usage_accounting_state VALUES(1,0)",
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
) + tuple(
    f"""CREATE TRIGGER IF NOT EXISTS usage_revision_{table}_{operation.lower()}
        AFTER {operation} ON {table} BEGIN
        UPDATE usage_accounting_state SET revision=revision+1 WHERE singleton=1;
        END"""
    for table in (
        "usage_events",
        "usage_event_items",
        "usage_item_billing_receipts",
        "usage_item_metering",
        "usage_settlement_revisions",
        "usage_meter_observations",
        "usage_legacy_baselines",
        "usage_ledger_state",
        "usage_billing_receipt_state",
    )
    for operation in ("INSERT", "UPDATE", "DELETE")
)


def _merge_meters(meters: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for meter in meters:
        key = canonical_json({k: meter[k] for k in ("metric", "unit", "dimensions", "source")})
        previous = merged.get(key)
        if (
            previous is not None
            and previous["known"]
            and meter["known"]
            and Decimal(previous["quantity"]) != Decimal(meter["quantity"])
        ):
            raise UsageLedgerConflictError("known usage measurement cannot be changed")
        if previous is None or meter["known"]:
            merged[key] = meter
    return list(merged.values())


async def _write_snapshot(conn: Any, snapshot: UsagePriceSnapshot) -> str:
    snapshot_id = snapshot.snapshot_id
    payload = canonical_json(asdict(snapshot))
    await conn.execute(
        "INSERT OR IGNORE INTO usage_price_snapshots VALUES(?,?)", (snapshot_id, payload)
    )
    async with conn.execute(
        "SELECT payload_json FROM usage_price_snapshots WHERE snapshot_id=?", (snapshot_id,)
    ) as cur:
        stored = await cur.fetchone()
    if stored[0] != payload:
        raise UsageLedgerConflictError("price snapshot content differs from its identity")
    return snapshot_id


async def append_settlement_on_conn(
    conn: Any,
    event_id: str,
    ordinal: int,
    revision: UsageSettlementRevision,
    *,
    meters: Sequence[UsageMeter] = (),
    price_snapshot: UsagePriceSnapshot | None = None,
) -> bool:
    """CAS append. Exact duplicate returns False, changed identity fails closed."""
    revision.cost_fields()  # validate numeric projection before committing
    if price_snapshot is not None:
        if revision.price_snapshot_id != price_snapshot.snapshot_id:
            raise UsageLedgerConflictError(
                "settlement snapshot does not match supplied price snapshot"
            )
        await _write_snapshot(conn, price_snapshot)
    payload = canonical_json(asdict(revision))
    observations = canonical_json([asdict(m) for m in meters])
    async with conn.execute(
        "SELECT payload_json FROM usage_settlement_revisions "
        "WHERE event_id=? AND ordinal=? AND revision_id=?",
        (event_id, ordinal, revision.revision_id),
    ) as cur:
        existing = await cur.fetchone()
    if existing is not None:
        original = UsageSettlementRevision(**json.loads(existing[0]))
        if canonical_json(asdict(original)) != payload:
            raise UsageLedgerConflictError(
                "settlement revision identity was reused with different data"
            )
        async with conn.execute(
            "SELECT meters_json FROM usage_meter_observations "
            "WHERE event_id=? AND ordinal=? AND revision_id=?",
            (event_id, ordinal, revision.revision_id),
        ) as cur:
            recorded = await cur.fetchone()
        if (recorded[0] if recorded else "[]") != observations:
            raise UsageLedgerConflictError("settlement revision measurements differ")
        return False
    async with conn.execute(
        "SELECT revision_id,revision_no,payload_json FROM usage_settlement_revisions "
        "WHERE event_id=? AND ordinal=? ORDER BY revision_no DESC LIMIT 1",
        (event_id, ordinal),
    ) as cur:
        current = await cur.fetchone()
    expected = current[0] if current is not None else None
    if revision.previous_revision_id != expected:
        raise UsageLedgerConflictError("settlement predecessor is not the current revision")
    if current is not None:
        previous = json.loads(current[2])
        if (
            previous["authority"] == "provider_confirmed"
            and revision.authority != "provider_confirmed"
        ):
            raise UsageLedgerConflictError("confirmed settlement cannot be replaced by an estimate")
    if revision.price_snapshot_id is not None:
        async with conn.execute(
            "SELECT 1 FROM usage_price_snapshots WHERE snapshot_id=?", (revision.price_snapshot_id,)
        ) as cur:
            if await cur.fetchone() is None:
                raise UsageLedgerConflictError("settlement references an unknown price snapshot")
    if meters:
        rows = await read_metering_on_conn(conn, [event_id])
        original_meters: list[dict[str, Any]] = next(
            (r["meters"] for r in rows if r["ordinal"] == ordinal), []
        )
        _merge_meters([*original_meters, *[asdict(m) for m in meters]])
    await conn.execute(
        "INSERT INTO usage_settlement_revisions"
        "(event_id,ordinal,revision_id,revision_no,payload_json) VALUES(?,?,?,?,?)",
        (event_id, ordinal, revision.revision_id, (current[1] + 1) if current else 1, payload),
    )
    if meters:
        await conn.execute(
            "INSERT INTO usage_meter_observations VALUES(?,?,?,?)",
            (event_id, ordinal, revision.revision_id, observations),
        )
    return True


async def write_metering_on_conn(
    conn: Any, event_id: str, metering: Mapping[int, UsageItemMetering]
) -> None:
    for ordinal, value in metering.items():
        snapshot_id = None
        if value.price_snapshot is not None:
            snapshot_id = await _write_snapshot(conn, value.price_snapshot)
        _merge_meters([asdict(m) for m in value.meters])
        payload = canonical_json([asdict(m) for m in value.meters])
        expected = (payload, snapshot_id, int(value.token_usage_known))
        async with conn.execute(
            "SELECT meters_json,price_snapshot_id,token_usage_known FROM usage_item_metering "
            "WHERE event_id=? AND ordinal=?",
            (event_id, ordinal),
        ) as cur:
            stored = await cur.fetchone()
        if stored is not None:
            if tuple(stored) != expected:
                raise UsageLedgerConflictError("usage measurements are immutable")
        else:
            await conn.execute(
                "INSERT INTO usage_item_metering VALUES(?,?,?,?,?)", (event_id, ordinal, *expected)
            )
        if value.settlement is not None:
            fields = value.settlement.cost_fields()
            async with conn.execute(
                "SELECT cost_nanos,billed_cost_nanos,estimated_cost_nanos "
                "FROM usage_event_items WHERE event_id=? AND ordinal=?",
                (event_id, ordinal),
            ) as cur:
                item_amounts = await cur.fetchone()
            if item_amounts is None or tuple(item_amounts) != tuple(
                fields[k] for k in ("cost_nanos", "billed_cost_nanos", "estimated_cost_nanos")
            ):
                raise UsageLedgerConflictError("initial settlement does not reconcile with item")
            if (
                value.settlement.price_snapshot_id is not None
                and value.settlement.price_snapshot_id != snapshot_id
            ):
                raise UsageLedgerConflictError(
                    "settlement snapshot does not match metering snapshot"
                )
            await append_settlement_on_conn(conn, event_id, ordinal, value.settlement)


async def read_settlements_on_conn(
    conn: Any, event_ids: Sequence[str], *, history: bool = False
) -> list[dict[str, Any]]:
    result = []
    for start in range(0, len(event_ids), 400):
        batch = event_ids[start : start + 400]
        marks = ",".join("?" for _ in batch)
        current = (
            ""
            if history
            else "AND r.revision_no=(SELECT MAX(s.revision_no) FROM usage_settlement_revisions s "
            "WHERE s.event_id=r.event_id AND s.ordinal=r.ordinal)"
        )
        async with conn.execute(
            "SELECT r.event_id,r.ordinal,r.revision_no,r.payload_json "
            f"FROM usage_settlement_revisions r WHERE r.event_id IN ({marks}) {current} "
            "ORDER BY r.event_id,r.ordinal,r.revision_no",
            tuple(batch),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            value = json.loads(row[3])
            revision = UsageSettlementRevision(**value)
            result.append(
                {
                    "event_id": row[0],
                    "ordinal": row[1],
                    "revision_no": row[2],
                    **value,
                    **revision.cost_fields(),
                }
            )
    return result


async def overlay_session_cost_totals_on_conn(
    conn: Any,
    totals: Mapping[str, Any],
    *,
    session_id: str,
    session_epoch: int,
) -> dict[str, Any]:
    """Replace only current valuations in a legacy aggregate, never token facts.

    Used by session reconciliation and individual turn read models. Exact USD
    deltas are summed before producing compatibility nanos and floats.
    """
    rows = await _read_settlement_items_on_conn(conn, session_id, session_epoch)
    return _overlay_cost_totals(totals, rows)


async def overlay_turn_cost_totals_on_conn(
    conn: Any,
    totals_by_turn: Mapping[str, Mapping[str, Any]],
    *,
    session_id: str,
    session_epoch: int,
) -> dict[str, dict[str, Any]]:
    """Apply current valuations to a bounded transcript page with one query."""
    if not totals_by_turn:
        return {}
    rows = await _read_settlement_items_on_conn(
        conn, session_id, session_epoch, turn_ids=tuple(totals_by_turn)
    )
    by_turn: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_turn.setdefault(row["usage_turn_id"], []).append(row)
    return {
        turn_id: _overlay_cost_totals(totals, by_turn.get(turn_id, ()))
        for turn_id, totals in totals_by_turn.items()
    }


async def _read_settlement_items_on_conn(
    conn: Any,
    session_id: str,
    session_epoch: int,
    *,
    turn_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    turn_filter = (
        f" AND e.turn_id IN ({','.join('?' for _ in turn_ids)})" if turn_ids else ""
    )
    async with conn.execute(
        """SELECT i.*, e.turn_id AS usage_turn_id, e.cost_source AS event_cost_source,
               e.estimated_cost_nanos AS event_estimated_cost_nanos,
               e.missing_cost_entries AS event_missing_cost_entries,
               r.payload_json AS revision_payload
           FROM usage_event_items i JOIN usage_events e ON e.event_id=i.event_id
           LEFT JOIN usage_settlement_revisions r ON r.event_id=i.event_id
               AND r.ordinal=i.ordinal AND r.revision_no=(
                   SELECT MAX(n.revision_no) FROM usage_settlement_revisions n
                   WHERE n.event_id=i.event_id AND n.ordinal=i.ordinal)
           WHERE e.session_id=? AND e.session_epoch=? AND e.origin='live_provider'
               AND e.status='finalized'
        """
        + turn_filter
        + " ORDER BY i.event_id,i.ordinal",
        (session_id, session_epoch, *(turn_ids or ())),
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


def _overlay_cost_totals(
    totals: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    result = dict(totals)
    by_event: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        by_event.setdefault(item["event_id"], []).append(item)
    with localcontext() as context:
        context.prec = 64
        exact_billed = Decimal(result["billed_cost_nanos"]) / 10**9
        exact_estimated = Decimal(result["estimated_cost_nanos"]) / 10**9
        for items in by_event.values():
            if not any(item["revision_payload"] for item in items):
                continue
            sources = []
            event_estimated = Decimal(0)
            old_unknown = new_unknown = 0
            for item in items:
                old_billed = Decimal(item["billed_cost_nanos"]) / 10**9
                old_estimated = Decimal(item["estimated_cost_nanos"]) / 10**9
                old_unknown += int(item["cost_source"] in {"none", "unavailable"})
                if item["revision_payload"]:
                    revision = UsageSettlementRevision(**json.loads(item["revision_payload"]))
                    amount = Decimal(revision.usd_amount or "0")
                    billed = revision.authority == "provider_confirmed"
                    new_billed = amount if billed else Decimal(0)
                    new_estimated = Decimal(0) if billed else amount
                    fields = revision.cost_fields()
                    source = fields["cost_source"]
                    new_unknown += fields["missing_cost_entries"]
                else:
                    new_billed, new_estimated = old_billed, old_estimated
                    source = item["cost_source"]
                    new_unknown += int(source in {"none", "unavailable"})
                exact_billed += new_billed - old_billed
                exact_estimated += new_estimated - old_estimated
                event_estimated += new_estimated
                sources.append(source)
            original = items[0]
            missing = max(0, original["event_missing_cost_entries"] - old_unknown) + new_unknown
            result["missing_cost_entries"] += missing - original["event_missing_cost_entries"]
            # A mixed rollup may contain only an estimate plus missing usage.
            # Count actual billed items so that subtotal is not shown as a $0 bill.
            result["provider_billed_entries"] += int(
                any(source in {"provider_billed", "mixed"} for source in sources)
            ) - int(original["event_cost_source"] in {"provider_billed", "mixed"})
            result["estimated_cost_entries"] += int(event_estimated > 0) - int(
                original["event_estimated_cost_nanos"] > 0
            )
        from opensquilla.session.usage_ledger import usd_to_nanos

        result["billed_cost_nanos"] = usd_to_nanos(exact_billed)
        result["estimated_cost_nanos"] = usd_to_nanos(exact_estimated)
        result["cost_nanos"] = result["billed_cost_nanos"] + result["estimated_cost_nanos"]
        result["billed_cost_usd_exact"] = exact_billed
        result["estimated_cost_usd_exact"] = exact_estimated
        result["cost_usd_exact"] = exact_billed + exact_estimated
    return result


async def read_metering_on_conn(conn: Any, event_ids: Sequence[str]) -> list[dict[str, Any]]:
    result = []
    for start in range(0, len(event_ids), 400):
        batch = event_ids[start : start + 400]
        marks = ",".join("?" for _ in batch)
        async with conn.execute(
            "SELECT event_id,ordinal,meters_json,price_snapshot_id,token_usage_known "
            f"FROM usage_item_metering WHERE event_id IN ({marks}) ORDER BY event_id,ordinal",
            tuple(batch),
        ) as cur:
            rows = await cur.fetchall()
        async with conn.execute(
            "SELECT o.event_id,o.ordinal,o.meters_json FROM usage_meter_observations o "
            "JOIN usage_settlement_revisions r USING(event_id,ordinal,revision_id) "
            f"WHERE o.event_id IN ({marks}) ORDER BY r.revision_no",
            tuple(batch),
        ) as cur:
            additions = await cur.fetchall()
        observations_by_item: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for event_id, ordinal, meters_json in additions:
            observations_by_item.setdefault((event_id, ordinal), []).extend(json.loads(meters_json))
        for row in rows:
            meters = json.loads(row[2])
            meters.extend(observations_by_item.get((row[0], row[1]), ()))
            result.append(
                {
                    "event_id": row[0],
                    "ordinal": row[1],
                    "meters": _merge_meters(meters),
                    "price_snapshot_id": row[3],
                    "token_usage_known": bool(row[4]),
                }
            )
    return result
