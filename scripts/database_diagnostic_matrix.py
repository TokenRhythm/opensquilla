"""Storage-only smoke and contention controls using fresh synthetic databases.

These controls are not the D0/D2/D4 Gateway or client acceptance matrix:
``index-pending`` is a newly created schema before existing post-ready index
preparation, not an old-version migration. No provider, real profile, or RPC is
used. No locks, timeouts, SQL, or worker functions are patched.

Run with the repository's development Python environment::

    python scripts/database_diagnostic_matrix.py --output .cache/db-smoke

The output directory must not exist. All data and logs remain there. A failed
operation or invariant produces exit code 1 and an evidence file. Cancellation
propagates after the owned database is closed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import platform
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opensquilla.observability.log_privacy import PrivateLogFormatter  # noqa: E402
from opensquilla.session.models import SessionNode  # noqa: E402
from opensquilla.session.storage import SessionStorage  # noqa: E402
from opensquilla.session.usage_ledger import UsageEventStart  # noqa: E402

IDENTITY_QUERY = (
    "SELECT agent_id, epoch FROM sessions "
    "WHERE session_id = ? ORDER BY session_key LIMIT 1"
)
SOURCE_FILES = (
    "src/opensquilla/session/storage.py",
    "src/opensquilla/compat/aiosqlite.py",
    "scripts/database_diagnostic_matrix.py",
)


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def usage_start(index: int) -> UsageEventStart:
    return UsageEventStart(
        event_id=f"event-{index}", execution_id=f"execution-{index}",
        call_index=0, session_id="live", started_at_ms=1,
    )


async def seed(storage: SessionStorage, session_count: int, transcript_rows: int) -> None:
    for session_id in ("live", "heavy"):
        await storage.upsert_session(SessionNode(
            session_key=f"agent:main:webchat:{session_id}", session_id=session_id,
            agent_id="main", created_at=1, updated_at=1,
        ))
    # Direct bulk writes only prepare synthetic fixture data, outside measurement.
    await storage.conn.execute("BEGIN IMMEDIATE")
    try:
        await storage.conn.executemany(
            "INSERT INTO sessions(session_key,session_id,created_at,updated_at) VALUES(?,?,?,?)",
            ((f"agent:main:webchat:archived-{i}", f"archived-{i}", 1, 1)
             for i in range(session_count)),
        )
        await storage.conn.executemany(
            "INSERT INTO transcript_entries "
            "(session_id,session_key,message_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
            (("heavy", "agent:main:webchat:heavy", f"message-{i}", "assistant",
              f"synthetic archived message {i} " + "history " * 32, i + 1)
             for i in range(transcript_rows)),
        )
        await storage.conn.execute(
            "INSERT INTO transcript_entries "
            "(session_id,session_key,message_id,role,content,created_at) VALUES(?,?,?,?,?,?)",
            ("live", "agent:main:webchat:live", "sibling-message", "assistant",
             "synthetic sibling must survive", 1),
        )
        await storage.conn.commit()
    except BaseException:
        await storage.conn.rollback()
        raise
    await storage.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


async def reserve(storage: SessionStorage, index: int) -> dict[str, Any]:
    started = time.perf_counter()
    holder = storage._operation_holder
    result = {"event_id": f"event-{index}", "started": started,
              "holder_at_start": holder[0] if holder else None}
    try:
        await storage.start_usage_event(usage_start(index))
        result["status"] = "ok"
    except Exception as exc:
        result["status"] = type(exc).__name__
        for field in ("stage", "resource", "waited_ms", "holder_operation", "hold_ms"):
            value = getattr(exc, field, None)
            if value is not None:
                result[field] = value
    result["ended"] = time.perf_counter()
    result["elapsed_ms"] = round((result["ended"] - started) * 1000, 3)
    return result


async def delete(storage: SessionStorage) -> dict[str, Any]:
    started = time.perf_counter()
    result = {"started": started}
    try:
        await storage.delete_session("agent:main:webchat:heavy")
        result["status"] = "ok"
    except Exception as exc:
        result["status"] = type(exc).__name__
    result["ended"] = time.perf_counter()
    result["elapsed_ms"] = round((result["ended"] - started) * 1000, 3)
    return result


def database_snapshot(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return {
            "quick_check": [row[0] for row in db.execute("PRAGMA quick_check")],
            "foreign_key_errors": list(db.execute("PRAGMA foreign_key_check")),
            "usage_events": list(db.execute(
                "SELECT event_id,execution_id,call_index,status FROM usage_events ORDER BY event_id"
            )),
            "sibling_transcript": list(db.execute(
                "SELECT message_id,content FROM transcript_entries "
                "WHERE session_id='live' ORDER BY id"
            )),
            "session_count": db.execute("SELECT count(*) FROM sessions").fetchone()[0],
            "heavy_transcript_count": db.execute(
                "SELECT count(*) FROM transcript_entries WHERE session_id='heavy'"
            ).fetchone()[0],
            "heavy_session_count": db.execute(
                "SELECT count(*) FROM sessions WHERE session_id='heavy'"
            ).fetchone()[0],
        }


async def run_case(output: Path, name: str, reservations: int,
                   session_count: int = 0, transcript_rows: int = 0,
                   prepare_indexes: bool = False) -> dict[str, Any]:
    path = output / f"{name}.sqlite"
    result: dict[str, Any] = {"name": name, "ok": False, "log_file": f"{name}.log",
                              "reservations": reservations,
                              "seed_sessions": session_count + 2,
                              "seed_transcript_rows": transcript_rows + 1}
    storage = await SessionStorage.open(str(path))
    pending: list[asyncio.Task[Any]] = []
    try:
        await seed(storage, session_count, transcript_rows)
        if prepare_indexes:
            await storage.prepare_usage_backfill_indexes()
        async with storage.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sessions_id_key'"
        ) as cursor:
            index_present = await cursor.fetchone() is not None
        async with storage.conn.execute(
            "EXPLAIN QUERY PLAN " + IDENTITY_QUERY, ("live",)
        ) as cursor:
            identity_plan = [list(row) for row in await cursor.fetchall()]
        async with storage.conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM transcript_entries WHERE session_id = ?", ("heavy",)
        ) as cursor:
            delete_plan = [list(row) for row in await cursor.fetchall()]
        result.update(identity_index_present=index_present, identity_query_plan=identity_plan,
                      delete_query_plan=delete_plan, backend=type(storage.conn).__module__)
        before = database_snapshot(path)
        deletion = None
        if transcript_rows:
            deletion = asyncio.create_task(delete(storage))
            pending.append(deletion)
            deadline = time.perf_counter() + 5
            while not deletion.done():
                holder = storage._operation_holder
                if holder and holder[0] == "delete_session":
                    break
                if time.perf_counter() >= deadline:
                    raise TimeoutError("delete did not acquire its operation lock")
                await asyncio.sleep(0)
        calls = [asyncio.create_task(reserve(storage, i)) for i in range(reservations)]
        pending.extend(calls)
        records = await asyncio.gather(*calls)
        result["records"] = records
        if deletion is not None:
            result["delete"] = await deletion
            result["delete_overlap_observed"] = any(
                row["holder_at_start"] == "delete_session"
                and row["started"] < result["delete"]["ended"] for row in records
            )
        result["status_counts"] = dict(Counter(row["status"] for row in records))
        first = database_snapshot(path)
        successes = {row["event_id"] for row in records if row["status"] == "ok"}
        # Repeat only successful reservations; a real retry must not add a row.
        for i in range(reservations):
            if f"event-{i}" in successes:
                await storage.start_usage_event(usage_start(i))
        after = database_snapshot(path)
        checks = {
            "all_reservations_succeeded": len(successes) == reservations,
            "successful_returns_persisted": {row[0] for row in first["usage_events"]} == successes,
            "reservation_identity_matches": set(first["usage_events"]) == {
                (f"event-{i}", f"execution-{i}", 0, "started")
                for i in range(reservations) if f"event-{i}" in successes
            },
            "duplicate_reservations_unchanged": first["usage_events"] == after["usage_events"],
            "sibling_preserved": before["sibling_transcript"] == after["sibling_transcript"],
            "quick_check_ok": after["quick_check"] == ["ok"],
            "foreign_keys_ok": not after["foreign_key_errors"],
            "lock_released": not storage._operation_lock.locked(),
            "transaction_closed": not storage.conn.in_transaction,
            "index_state_matches": index_present == prepare_indexes,
            "session_count_matches": (
                after["session_count"] == session_count + (1 if deletion else 2)
            ),
        }
        if deletion:
            checks.update(
                delete_succeeded=result["delete"]["status"] == "ok",
                overlap_observed=result["delete_overlap_observed"],
                heavy_removed=after["heavy_transcript_count"] == after["heavy_session_count"] == 0,
            )
        result.update(checks=checks, ok=all(checks.values()),
                      persisted_usage_rows=len(after["usage_events"]))
    except Exception as exc:
        result["failure_class"] = type(exc).__name__
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await storage.close()
        write_json(output / f"{name}.json", result)
    return result


async def run(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    summary = {
        "schema": "opensquilla.storage-diagnostic-smoke/v1", "ok": False,
        "platform": platform.platform(), "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version, "diagnostics": args.diagnostics,
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in SOURCE_FILES},
        "boundary": (
            "fresh synthetic storage controls; not old-version migration, RPC, "
            "provider, native-client acceptance, or stable performance benchmark"
        ),
        "cases": [],
    }
    logger = logging.getLogger("opensquilla.session.storage")
    previous_level = logger.level
    previous_diagnostics = os.environ.get("OPENSQUILLA_STORAGE_DIAGNOSTICS")
    logger.setLevel(logging.INFO)
    os.environ["OPENSQUILLA_STORAGE_DIAGNOSTICS"] = "1" if args.diagnostics else "0"
    try:
        for repetition in range(1, args.repeat + 1):
            definitions = [
                ("ordinary", 0, 0, False),
                ("index-pending", args.session_count, 0, False),
                ("index-ready", args.session_count, 0, True),
                ("delete-overlap", 0, args.transcript_rows, False),
            ]
            # Alternate the independent index controls to avoid a fixed order.
            if repetition % 2 == 0:
                definitions[1:3] = reversed(definitions[1:3])
            for label, session_count, transcript_rows, prepare in definitions:
                name = f"{repetition:02d}-{label}"
                handler = logging.FileHandler(output / f"{name}.log", encoding="utf-8")
                handler.setFormatter(PrivateLogFormatter())
                logger.addHandler(handler)
                try:
                    result = await run_case(output, name, args.reservations,
                                            session_count, transcript_rows, prepare)
                finally:
                    logger.removeHandler(handler)
                    handler.close()
                summary["cases"].append(result)
                write_json(output / "summary.json", summary)
                print(json.dumps({"case": name, "ok": result["ok"],
                                  "checks": result.get("checks"),
                                  "failure_class": result.get("failure_class")}), flush=True)
        summary["source_unchanged"] = summary["source_sha256"] == {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES
        }
        summary["ok"] = all(case["ok"] for case in summary["cases"]) and summary["source_unchanged"]
    finally:
        logger.setLevel(previous_level)
        if previous_diagnostics is None:
            os.environ.pop("OPENSQUILLA_STORAGE_DIAGNOSTICS", None)
        else:
            os.environ["OPENSQUILLA_STORAGE_DIAGNOSTICS"] = previous_diagnostics
        write_json(output / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reservations", type=int, default=4)
    parser.add_argument("--session-count", type=int, default=100)
    parser.add_argument("--transcript-rows", type=int, default=100)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--diagnostics", action="store_true")
    args = parser.parse_args()
    if min(args.reservations, args.session_count, args.transcript_rows, args.repeat) < 1:
        parser.error("all workload counts must be positive")
    result = asyncio.run(run(args))
    print(json.dumps({"ok": result["ok"], "summary": str(args.output / "summary.json")}))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
