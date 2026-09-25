"""Synthetic session-query comparison against fully migrated SessionStorage.

Run this same script against each checkout's source and dependencies, for example:
PYTHONPATH=/checkout/src /checkout/.venv/bin/python /path/to/bench_session_queries.py

Defaults to five rounds of 20 uninstrumented warm operations. Structural counters
are collected separately. Temporary databases contain only generated fixtures;
no Gateway, provider, user profile, or existing database is opened. These are
query timings, not client startup timings. Run competing workloads separately.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


def git_metadata(source: Path) -> dict:
    root = source.parents[3]
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    return {"root": str(root), "head": git("rev-parse", "HEAD"),
            "tracked_dirty": bool(git("diff", "HEAD", "--name-only"))}


async def seed_sessions(storage, count: int):
    keys = [f"agent:main:webchat:synthetic-{index:05d}" for index in range(count)]
    await storage.conn.executemany(
        """INSERT INTO sessions (session_key, session_id, created_at, updated_at, status)
        VALUES (?, ?, ?, ?, 'done')""",
        [(key, f"synthetic-session-{index:05d}", index + 1, index + 1)
         for index, key in enumerate(keys)],
    )
    await storage.conn.commit()
    return keys


async def seed_tasks(storage, keys, count: int, *, active: bool = False):
    await storage.conn.executemany(
        """INSERT INTO agent_tasks
        (task_id, session_key, source_kind, queue_mode, status, created_at, updated_at, details)
        VALUES (?, ?, 'webui', 'followup', ?, ?, ?, ?)""",
        [(f"{key}-task-{index:04d}", key, "running" if active else "succeeded",
          index // 3, 100_000 if active else index // 3, '{"synthetic":"' + "x" * 1024 + '"}')
         for key in keys for index in range(count)],
    )
    await storage.conn.commit()


async def observe(storage, operation):
    from opensquilla.session import storage as storage_module

    counters = {"fetchall_calls": 0, "fetched_rows": 0, "task_models": 0,
                "title_batch_calls": 0, "title_single_calls": 0}
    async with storage.conn.execute("SELECT 1") as cursor:
        cursor_type = type(cursor)
    fetchall = cursor_type.fetchall
    deserialize = storage_module._deserialize_row
    batch = storage.list_user_transcript_content_batch
    single = storage.get_transcript

    async def counted_fetchall(cursor):
        rows = await fetchall(cursor)
        counters["fetchall_calls"] += 1
        counters["fetched_rows"] += len(rows)
        return rows

    def counted_deserialize(row):
        if "task_id" in row:
            counters["task_models"] += 1
        return deserialize(row)

    async def counted_batch(*args, **kwargs):
        counters["title_batch_calls"] += 1
        return await batch(*args, **kwargs)

    async def counted_single(*args, **kwargs):
        counters["title_single_calls"] += 1
        return await single(*args, **kwargs)

    with (
        patch.object(cursor_type, "fetchall", counted_fetchall),
        patch.object(storage_module, "_deserialize_row", counted_deserialize),
        patch.object(storage, "list_user_transcript_content_batch", counted_batch),
        patch.object(storage, "get_transcript", counted_single),
    ):
        result = await operation()
    return {**counters, "result": result}


async def measure(storage, operation, *, rounds: int, runs: int):
    structural = await observe(storage, operation)
    for _ in range(2):
        assert await operation() == structural["result"]
    samples = []
    for _ in range(rounds):
        round_samples = []
        for _ in range(runs):
            start = time.perf_counter()
            result = await operation()
            round_samples.append((time.perf_counter() - start) * 1000)
            assert result == structural["result"]
        samples.append(round_samples)
    flat = sorted(value for values in samples for value in values)
    return {
        "structure": structural,
        "timing_ms": {"median": statistics.median(flat), "p95": flat[min(len(flat) - 1,
                       int(len(flat) * 0.95))], "round_medians": [statistics.median(values)
                       for values in samples], "round_samples": samples},
    }


async def benchmark(root: Path, rounds: int, runs: int):
    from opensquilla.gateway.rpc_sessions import _list_transcript_titles
    from opensquilla.session import storage as storage_module
    from opensquilla.session.models import SessionNode
    from opensquilla.session.storage import SessionStorage

    cases = []
    for count in (100, 1000, 10_000):
        async with SessionStorage(str(root / f"history-{count}.db")) as storage:
            keys = await seed_sessions(storage, count)
            await seed_tasks(storage, keys[:20], 1, active=True)

            async def page():
                result = await storage.list_sessions_page(limit=200)
                tasks = await storage.list_agent_tasks_for_sessions(
                    [session.session_key for session in result.sessions],
                )
                titles = await _list_transcript_titles(storage, result.sessions)
                counts = await storage.count_transcript_entries_batch(
                    [session.session_id for session in result.sessions],
                )
                assert len(result.sessions) == min(count, 200)
                assert sum(map(len, tasks.values())) == 20
                assert not titles and sum(counts.values()) == 0
                return {"sessions": len(result.sessions), "task_summaries": 20,
                        "titles": len(titles), "has_more": result.has_more}

            cases.append({"case": "history_first_page", "history_sessions": count,
                          "active_tasks": 20, "page_limit": 200,
                          **await measure(storage, page, rounds=rounds, runs=runs)})

    async with SessionStorage(str(root / "task-history.db")) as storage:
        keys = await seed_sessions(storage, 10)
        await seed_tasks(storage, keys, 1000)

        async def summaries():
            result = await storage.list_agent_tasks_for_sessions(keys, limit_per_session=100)
            for key, tasks in result.items():
                assert [task.task_id for task in tasks] == [
                    f"{key}-task-{index:04d}" for index in reversed(range(900, 1000))
                ]
                assert all(task.details is None for task in tasks)
            return {"sessions": len(result), "task_summaries": sum(map(len, result.values()))}

        cases.append({"case": "task_history", "sessions": 10, "tasks_per_session": 1000,
                      "limit_per_session": 100,
                      **await measure(storage, summaries, rounds=rounds, runs=runs)})

    async with SessionStorage(str(root / "empty-titles.db")) as storage:
        keys = await seed_sessions(storage, 200)
        sessions = [SessionNode(session_key=key, session_id=f"synthetic-session-{index:05d}")
                    for index, key in enumerate(keys)]

        async def titles():
            result = await _list_transcript_titles(storage, sessions)
            assert not result
            return {"sessions": len(sessions), "titles": len(result)}

        cases.append({"case": "empty_titles", "sessions": 200,
                      **await measure(storage, titles, rounds=rounds, runs=runs)})

    return {"schema_version": 1, "benchmark": "session_queries", "synthetic_only": True,
            "rounds": rounds, "runs_per_round": runs, "warmups": 2,
            "python": sys.version, "interpreter": sys.executable, "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(), "source": git_metadata(Path(storage_module.__file__)),
            "measurement": "warm queries; structure sampled separately; not client startup",
            "cases": cases}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()
    if args.rounds < 1 or args.runs < 1:
        parser.error("rounds and runs must be positive")
    with tempfile.TemporaryDirectory(prefix="opensquilla-query-benchmark-") as directory:
        root = Path(directory)
        for key, name in (("OPENSQUILLA_STATE_DIR", "state"), ("OPENSQUILLA_LOG_DIR", "logs"),
                          ("OPENSQUILLA_USER_STATE_DIR", "profile")):
            os.environ[key] = str(root / name)
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(benchmark(root, args.rounds, args.runs))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
