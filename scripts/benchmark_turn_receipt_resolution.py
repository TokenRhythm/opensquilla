"""Isolated, reproducible receipt-marker size and SessionStorage microbenchmark.

Run from the repository with PYTHONPATH=src;. and PYTHONDONTWRITEBYTECODE=1.
Only the explicitly supplied, initially absent output directory is written.
No Gateway, provider, user database, or user profile is opened.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from opensquilla.contracts.turn_request_scope import resolvable_webchat_scope
from opensquilla.persistence.turn_request_resolution import (
    EPOCH_VALID_HOURS,
    RESOLUTION_CLEANUP_BATCH,
    TurnReceiptEpochPolicy,
    parse_turn_receipt_epoch,
)
from opensquilla.session.models import AgentTaskRecord, SessionNode, TranscriptEntry
from opensquilla.session.storage import (
    SessionStorage,
    StorageBusyError,
    TurnRequestResolvedError,
)

KEY = "agent:main:webchat:00000000-0000-4000-8000-000000000001"
SESSION_ID = "00000000-0000-4000-8000-000000000002"
V2_SCOPE = resolvable_webchat_scope("operator")
LEGACY_SCOPE = "web:webchat:operator"
NOW = 1_792_440_000_000
MESSAGE = "Synthetic benchmark input, with no external execution."
BOOT = "d" * 32
EPOCH = f"v2.{BOOT}.0.{EPOCH_VALID_HOURS}"


def identity(index: int, epoch: str | None = EPOCH) -> dict[str, str]:
    logical_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"osq-benchmark/{index}"))
    return {
        "request_session_key": KEY,
        # Deterministic UUIDs with nonsequential index order, closer to browser UUIDs.
        "client_request_id": f"tr2:{epoch}:{logical_id}" if epoch else logical_id,
        "request_fingerprint": "sha256:" + hashlib.sha256(str(index).encode()).hexdigest(),
    }


async def accept(
    store: SessionStorage, index: int, scope: str, epoch: str = EPOCH,
) -> str:
    await store.accept_turn(
        TranscriptEntry(
            session_id=SESSION_ID, session_key=KEY,
            message_id=str(uuid.UUID(int=index + 1_000_000)), role="user", content=MESSAGE,
        ),
        expected_epoch=1, updated_at=NOW,
        task_record=AgentTaskRecord(
            session_key=KEY, task_id=str(uuid.UUID(int=index + 2_000_000)),
        ),
        source_scope=scope, **identity(index, epoch if scope == V2_SCOPE else None),
    )
    return "accepted"


async def resolve(store: SessionStorage, index: int, epoch: str = EPOCH) -> str:
    return await store.resolve_turn_request_attempt(source_scope=V2_SCOPE, **identity(index, epoch))


def distribution(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0}

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 4)

    return {
        "count": len(values), "mean_ms": round(statistics.mean(values), 4),
        "p50_ms": percentile(0.50), "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99), "max_ms": round(max(values), 4),
    }


async def timed(operation: Any, *, index: int, kind: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        outcome = await operation
    except StorageBusyError as error:
        outcome = "storage_busy"
        details = {"stage": error.stage, "waited_ms": error.waited_ms}
    except TurnRequestResolvedError:
        outcome = "retired_fence"
        details = {}
    else:
        details = {}
    return {
        "index": index, "kind": kind, "outcome": outcome,
        "elapsed_ms": (time.perf_counter() - started) * 1000, **details,
    }


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(sample["outcome"] for sample in samples)
    return {
        **distribution([sample["elapsed_ms"] for sample in samples]),
        "outcomes": dict(counts),
        "busy_error_rate": counts["storage_busy"] / len(samples) if samples else 0,
        "by_kind": {
            kind: distribution([item["elapsed_ms"] for item in samples if item["kind"] == kind])
            for kind in sorted({sample["kind"] for sample in samples})
        },
    }


async def open_stores(
    path: Path, count: int = 1, *, policy: TurnReceiptEpochPolicy | None = None,
) -> list[SessionStorage]:
    policy = policy or TurnReceiptEpochPolicy(BOOT, clock=lambda: 0)
    stores = [await SessionStorage.open(str(path))]
    await stores[0].upsert_session(
        SessionNode(session_key=KEY, session_id=SESSION_ID, epoch=1),
    )
    for _ in range(count - 1):
        stores.append(await SessionStorage.open(str(path)))
    for store in stores:
        store.configure_turn_receipt_epochs(policy, exclusive_gateway=True)
    return stores


async def close_stores(stores: list[SessionStorage]) -> None:
    for store in reversed(stores):
        await store.close()


def file_sizes(path: Path) -> dict[str, int]:
    return {
        name: candidate.stat().st_size if candidate.exists() else 0
        for name, candidate in {
            "main_bytes": path, "wal_bytes": Path(str(path) + "-wal"),
            "shm_bytes": Path(str(path) + "-shm"),
        }.items()
    }


async def checkpoint(store: SessionStorage) -> list[int]:
    async with store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cursor:
        row = await cursor.fetchone()
    return list(row)


async def pragmas(store: SessionStorage) -> dict[str, Any]:
    result = {}
    for name in ["journal_mode", "synchronous", "page_size", "wal_autocheckpoint", "busy_timeout"]:
        async with store.conn.execute(f"PRAGMA {name}") as cursor:
            row = await cursor.fetchone()
        result[name] = row[0]
    result["application_busy_budget_seconds"] = store._busy_budget_seconds
    return result


async def populate_markers(
    store: SessionStorage, start: int, stop: int, *, epoch: str = EPOCH,
) -> None:
    validity = parse_turn_receipt_epoch(epoch)
    assert validity is not None
    rows = [
        (V2_SCOPE, KEY, identity(index, epoch)["client_request_id"],
         identity(index, epoch)["request_fingerprint"],
         "accepted" if index % 2 else "retired", NOW,
         validity.boot_id, validity.expires_hour)
        for index in range(start, stop)
    ]
    async with store._write_transaction("benchmark_marker_population") as conn:
        await conn.executemany(
            "INSERT INTO turn_request_resolution "
            "(source_scope, request_session_key, client_request_id, "
            "request_fingerprint, state, updated_at, validity_boot_id, validity_expires_hour) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


async def sequential(output: Path, count: int, repeats: int) -> list[dict[str, Any]]:
    results = []
    # Alternate order to avoid consistently favoring the first or second run.
    for repeat in range(repeats):
        protocols = ["legacy", "v2"] if repeat % 2 == 0 else ["v2", "legacy"]
        for protocol in protocols:
            path = output / f"accept-{protocol}-{repeat}.sqlite"
            stores = await open_stores(path)
            store = stores[0]
            scope = V2_SCOPE if protocol == "v2" else LEGACY_SCOPE
            try:
                settings = await pragmas(store)
                for index in range(20):
                    await accept(store, index, scope)
                samples = [
                    await timed(accept(store, index + 20, scope), index=index, kind="accept")
                    for index in range(count)
                ]
                statements: list[str] = []
                await store.conn.set_trace_callback(statements.append)
                await accept(store, count + 20, scope)
                await store.conn.set_trace_callback(None)
                before = file_sizes(path)
                checkpoint_result = await checkpoint(store)
                results.append({
                    "protocol": protocol, "repeat": repeat, "settings": settings,
                    "summary": summarize(samples), "samples": samples,
                    "single_accept_sql": statements,
                    "single_accept_statement_count": len(statements),
                    "before_checkpoint": before, "checkpoint_result": checkpoint_result,
                    "after_checkpoint": file_sizes(path),
                })
            finally:
                await close_stores(stores)
            print(f"accept {protocol} repeat={repeat}: {results[-1]['summary']}", flush=True)
    return results


async def marker_growth(output: Path, targets: list[int]) -> dict[str, Any]:
    path = output / "marker-growth.sqlite"
    stores = await open_stores(path)
    store = stores[0]
    try:
        settings = await pragmas(store)
        # Isolate marker bytes; no fabricated receipt/task/transcript rows.
        await checkpoint(store)
        baseline = file_sizes(path)
        await store.conn.execute("PRAGMA wal_autocheckpoint=0")
        measurements = []
        previous = 0
        for target in targets:
            started = time.perf_counter()
            for start in range(previous, target, 1000):
                stop = min(start + 1000, target)
                await populate_markers(store, start, stop)
            before = file_sizes(path)
            checkpoint_result = await checkpoint(store)
            after = file_sizes(path)
            try:
                async with store.conn.execute(
                    "SELECT name, SUM(pgsize) AS bytes FROM dbstat "
                    "WHERE name LIKE '%turn_request_resolution%' GROUP BY name"
                ) as cursor:
                    page_usage = [dict(row) for row in await cursor.fetchall()]
            except sqlite3.OperationalError:
                page_usage = None
            async with store.conn.execute("SELECT COUNT(*) FROM turn_request_resolution") as cursor:
                actual = (await cursor.fetchone())[0]
            assert actual == target
            measurements.append({
                "rows": target, "population_seconds": time.perf_counter() - started,
                "before_checkpoint": before, "checkpoint_result": checkpoint_result,
                "after_checkpoint": after, "table_and_index_page_usage": page_usage,
                "incremental_main_bytes": after["main_bytes"] - baseline["main_bytes"],
                "main_bytes_per_marker": (
                    after["main_bytes"] - baseline["main_bytes"]
                ) / target,
            })
            previous = target
            print(f"marker rows={target}: {measurements[-1]}", flush=True)
        return {
            "settings": settings, "baseline": baseline,
            "population": "1000-row real SQLite transactions; size-only, not per-call timing",
            "wal_note": "autocheckpoint disabled here; WAL is since preceding checkpoint",
            "key_lengths": {
                "scope": len(V2_SCOPE), "session_key": len(KEY),
                "request_id": len(identity(0)["client_request_id"]),
                "fingerprint": 71,
            },
            "measurements": measurements,
        }
    finally:
        await close_stores(stores)


async def contention(
    output: Path, count: int, connections: int, mode: str, repeat: int,
) -> dict[str, Any]:
    path = output / f"contention-{mode}-{connections}-{repeat}.sqlite"
    stores = await open_stores(path, connections)
    samples: list[dict[str, Any]] = []
    begin_counts = [0] * connections

    def trace_for(worker: int) -> Any:
        def trace(sql: str) -> None:
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                begin_counts[worker] += 1
        return trace

    async def measured(operation: Any, index: int, kind: str, worker: int) -> dict[str, Any]:
        before = begin_counts[worker]
        sample = await timed(operation, index=index, kind=kind)
        sample["begin_immediate_attempts"] = begin_counts[worker] - before
        return sample

    try:
        settings = await pragmas(stores[0])
        for worker, store in enumerate(stores):
            await store.conn.set_trace_callback(trace_for(worker))
        started = time.perf_counter()
        if mode == "accept-resolve-race":
            assert connections % 2 == 0
            pairs = connections // 2

            async def racer(pair: int) -> None:
                for index in range(pair, count, pairs):
                    left = measured(
                        accept(stores[pair * 2], index, V2_SCOPE), index, "accept", pair * 2,
                    )
                    right = measured(
                        resolve(stores[pair * 2 + 1], index), index, "resolve", pair * 2 + 1,
                    )
                    results = await asyncio.gather(*([left, right] if index % 2 else [right, left]))
                    samples.extend(results)
            await asyncio.gather(*(racer(pair) for pair in range(pairs)))
        else:
            async def worker_run(worker: int) -> None:
                for index in range(worker, count, connections):
                    if mode == "resolve":
                        operation = resolve(stores[worker], index)
                    else:
                        scope = LEGACY_SCOPE if mode == "legacy-accept" else V2_SCOPE
                        operation = accept(stores[worker], index, scope)
                    samples.append(await measured(operation, index, mode, worker))
            await asyncio.gather(*(worker_run(worker) for worker in range(connections)))
        elapsed = time.perf_counter() - started
        for store in stores:
            await store.conn.set_trace_callback(None)
        async with stores[0].conn.execute(
            "SELECT state, COUNT(*) AS n FROM turn_request_resolution GROUP BY state"
        ) as cursor:
            markers = [dict(row) for row in await cursor.fetchall()]
        return {
            "mode": mode, "connections": connections, "repeat": repeat, "settings": settings,
            "summary": summarize(samples), "samples": samples, "elapsed_seconds": elapsed,
            "completed_operations_per_second": len(samples) / elapsed,
            "begin_immediate_trace_count": sum(begin_counts),
            "extra_begin_attempts": max(0, sum(begin_counts) - len(samples)),
            "operations_with_begin_retry_rate": sum(
                sample["begin_immediate_attempts"] > 1 for sample in samples
            ) / len(samples),
            "markers": markers,
        }
    finally:
        await close_stores(stores)


async def held_writer(output: Path, hold_ms: int) -> dict[str, Any]:
    path = output / f"held-writer-{hold_ms}.sqlite"
    stores = await open_stores(path, 2)
    blocker = sqlite3.connect(path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")

        async def unlock() -> None:
            await asyncio.sleep(hold_ms / 1000)
            blocker.rollback()

        tasks = [
            timed(accept(stores[0], 0, V2_SCOPE), index=0, kind="accept"),
            timed(resolve(stores[1], 1), index=1, kind="resolve"),
        ]
        results = await asyncio.gather(*tasks, unlock())
        samples = results[:2]
        return {"hold_ms": hold_ms, "summary": summarize(samples), "samples": samples}
    finally:
        blocker.close()
        await close_stores(stores)


async def mature_table(output: Path, samples: int) -> list[dict[str, Any]]:
    """Measure actual calls on the size DB, separately from bulk population."""
    results = []
    for label, filename in [
        ("fresh", "marker-performance-fresh.sqlite"), ("mature", "marker-growth.sqlite"),
    ]:
        stores = await open_stores(output / filename)
        store = stores[0]
        try:
            if label == "fresh":
                # Use real retirement APIs, not bulk inserts, for the small baseline.
                for index in range(samples):
                    await resolve(store, index * 2)
            async with store.conn.execute("SELECT COUNT(*) FROM turn_request_resolution") as cur:
                initial_markers = (await cur.fetchone())[0]
            async with store.conn.execute(
                "EXPLAIN QUERY PLAN SELECT rowid FROM turn_request_resolution WHERE "
                "(validity_boot_id = ? AND validity_expires_hour <= ?) "
                "OR validity_boot_id < ? OR validity_boot_id > ? LIMIT ?",
                (BOOT, 0, BOOT, BOOT, RESOLUTION_CLEANUP_BATCH),
            ) as cursor:
                cleanup_plan = [dict(row) for row in await cursor.fetchall()]
            assert not any(
                "SCAN turn_request_resolution" in row["detail"] for row in cleanup_plan
            )
            settings = await pragmas(store)
            operations = []
            for kind in ["marker-get", "existing-resolve", "new-accept"]:
                timed_samples = []
                for index in range(samples):
                    if kind == "marker-get":
                        operation = store.get_turn_request_resolution(
                            source_scope=V2_SCOPE, **identity(index * 2),
                        )
                    elif kind == "existing-resolve":
                        operation = resolve(store, index * 2)
                    else:
                        operation = accept(store, 1_000_000 + index, V2_SCOPE)
                    timed_samples.append(await timed(operation, index=index, kind=kind))
                operations.append({
                    "kind": kind, "summary": summarize(timed_samples), "samples": timed_samples,
                })
            async with store.conn.execute("SELECT COUNT(*) FROM turn_request_resolution") as cur:
                final_markers = (await cur.fetchone())[0]
            assert final_markers == initial_markers + samples
            results.append({
                "table": label, "initial_marker_count": initial_markers,
                "final_marker_count": final_markers,
                "cleanup_query_plan": cleanup_plan,
                "settings": settings, "operations": operations,
                "note": "Existing keys are the same retired identities in both tables. "
                "New accepts run last and add markers plus real task/message/receipt rows. "
                "Size results were captured before this stage.",
            })
        finally:
            await close_stores(stores)
        print(f"marker performance {label}: "
              f"{[(item['kind'], item['summary']) for item in operations]}", flush=True)
    return results


async def expired_backlog(output: Path, count: int) -> dict[str, Any]:
    """Every measured acceptance reclaims the maximum batch, not just an empty DELETE."""
    now = [0.0]
    policy = TurnReceiptEpochPolicy(BOOT, clock=lambda: now[0])
    stores = await open_stores(output / "expired-backlog.sqlite", policy=policy)
    store = stores[0]
    backlog = count * RESOLUTION_CLEANUP_BATCH
    try:
        for start in range(0, backlog, 1000):
            await populate_markers(store, start, min(start + 1000, backlog))
        now[0] = EPOCH_VALID_HOURS * 3600
        epoch = policy.current_epoch()
        samples = [
            await timed(
                accept(store, backlog + index, V2_SCOPE, epoch),
                index=index, kind="accept-delete-64",
            )
            for index in range(count)
        ]
        async with store.conn.execute("SELECT COUNT(*) FROM turn_request_resolution") as cursor:
            remaining = (await cursor.fetchone())[0]
        assert remaining == count
        return {
            "initial_expired_rows": backlog, "remaining_current_rows": remaining,
            "max_deletes_per_accept": RESOLUTION_CLEANUP_BATCH,
            "summary": summarize(samples), "samples": samples,
        }
    finally:
        await close_stores(stores)


async def marker_churn(output: Path, hours: int, per_hour: int) -> dict[str, Any]:
    """Advance only the policy clock; use real transactions for continuous retirement."""
    now = [0.0]
    policy = TurnReceiptEpochPolicy(BOOT, clock=lambda: now[0])
    path = output / "marker-churn.sqlite"
    stores = await open_stores(path, policy=policy)
    store = stores[0]
    observations = []
    try:
        for hour in range(hours):
            now[0] = hour * 3600
            epoch = policy.current_epoch()
            for index in range(hour * per_hour, (hour + 1) * per_hour):
                assert await resolve(store, index, epoch) == "retired"
            if hour in {0, 24, 25, 49, hours - 1}:
                checkpoint_result = await checkpoint(store)
                async with store.conn.execute(
                    "SELECT COUNT(*) FROM turn_request_resolution",
                ) as cursor:
                    rows = (await cursor.fetchone())[0]
                sizes = {}
                for name in ("page_count", "freelist_count"):
                    async with store.conn.execute(f"PRAGMA {name}") as cursor:
                        sizes[name] = (await cursor.fetchone())[0]
                assert rows == min(hour + 1, EPOCH_VALID_HOURS) * per_hour
                observations.append({
                    "hour": hour, "rows": rows, **sizes,
                    "checkpoint": checkpoint_result, **file_sizes(path),
                })
        return {
            "simulated_hours": hours, "requests_per_hour": per_hour,
            "observations": observations,
            "scope": "Continuous writes reclaim expired evidence; idle databases retain rows. "
            "Freed SQLite pages are reusable, not automatically removed from the file. "
            "This demonstrates lifetime/row churn at this request rate, not an absolute byte cap.",
        }
    finally:
        await close_stores(stores)


async def run(args: argparse.Namespace) -> None:
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = Path(__file__).resolve().parents[1]
    tracked = [
        "src/opensquilla/session/storage.py",
        "src/opensquilla/persistence/turn_request_resolution.py",
        "scripts/benchmark_turn_receipt_resolution.py",
    ]
    result: dict[str, Any] = {
        "environment": {
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "platform": platform.platform(), "python": sys.version, "executable": sys.executable,
            "sqlite": sqlite3.sqlite_version, "cpu_count": os.cpu_count(),
            "head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            ).strip(),
            "source_sha256": {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in tracked
            },
            "command": subprocess.list2cmdline(sys.argv), "arguments": vars(args),
        },
        "scope": "Local synthetic SQLite microbenchmark, not production/provider/WS performance",
    }
    result_file = output / "results.json"

    def save() -> None:
        result_file.write_text(json.dumps(result, indent=2), encoding="utf-8")

    save()
    result["acceptance"] = await sequential(output, args.samples, args.repeats)
    save()
    result["marker_growth"] = await marker_growth(output, args.marker_sizes)
    save()
    if args.marker_sizes[-1] >= 100000:
        result["mature_table"] = await mature_table(output, args.mature_samples)
        save()
    result["expired_backlog"] = await expired_backlog(output, args.samples)
    save()
    result["marker_churn"] = await marker_churn(output, args.churn_hours, args.churn_per_hour)
    save()
    result["contention"] = []
    for repeat in range(args.contention_repeats):
        accept_modes = ["legacy-accept", "v2-accept"]
        if repeat % 2:
            accept_modes.reverse()
        cases = [(1, "resolve"), *((args.connections, mode) for mode in accept_modes),
                 (args.connections, "accept-resolve-race")]
        for connections, mode in cases:
            case = await contention(output, args.contention_samples, connections, mode, repeat)
            result["contention"].append(case)
            save()
            print(f"contention {mode}/{connections}/{repeat}: {case['summary']}", flush=True)
    result["held_writer"] = []
    for duration in [250, 2250]:
        result["held_writer"].append(await held_writer(output, duration))
        save()
    result["environment"]["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result["environment"]["source_unchanged"] = all(
        hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
        for name, digest in result["environment"]["source_sha256"].items()
    )
    save()
    print(f"Results: {result_file}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--samples", type=int, default=400)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--contention-samples", type=int, default=400)
    parser.add_argument("--contention-repeats", type=int, default=2)
    parser.add_argument("--mature-samples", type=int, default=200)
    parser.add_argument("--connections", type=int, default=8)
    parser.add_argument("--churn-hours", type=int, default=60)
    parser.add_argument("--churn-per-hour", type=int, default=64)
    parser.add_argument("--marker-sizes", type=int, nargs="+", default=[1000, 10000, 100000])
    options = parser.parse_args()
    if options.connections < 2 or options.connections % 2:
        parser.error("--connections must be an even number >= 2")
    if min(options.samples, options.contention_samples, options.repeats,
           options.contention_repeats, options.mature_samples,
           options.churn_hours, options.churn_per_hour) < 1:
        parser.error("sample counts and repeats must be positive")
    if options.marker_sizes != sorted(set(options.marker_sizes)) or options.marker_sizes[0] < 1:
        parser.error("--marker-sizes must be positive and strictly increasing")
    asyncio.run(run(options))
