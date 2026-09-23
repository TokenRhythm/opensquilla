#!/usr/bin/env python3
"""Compare channel storage isolation with a real 500 ms SQLite writer lock.

Run this same script with PYTHONPATH pointing at each checkout. --mode sync
works against the pre-worker baseline. Measurements are diagnostic, not CI
wall-clock gates; the payload contains only synthetic identifiers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import statistics
import tempfile
import threading
import time
from pathlib import Path

from opensquilla.channels.delivery_store import ChannelDeliveryStore
from opensquilla.channels.types import IncomingMessage


def distribution(values):
    ordered = sorted(values)

    def percentile(fraction):
        return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]

    return {
        "p50": round(statistics.median(values), 3),
        "p95": round(percentile(0.95), 3),
        "p99": round(percentile(0.99), 3),
        "max": round(max(values), 3),
    }


async def sample(mode, active, lock_ms):
    with tempfile.TemporaryDirectory(prefix="channel-worker-bench-") as directory:
        path = Path(directory) / "channel.sqlite"
        if mode == "worker":
            from opensquilla.channels.storage_worker import AsyncChannelDeliveryStore

            store = AsyncChannelDeliveryStore(path)
            await store.open()
        else:
            store = ChannelDeliveryStore(path)
        acquired = threading.Event()
        release = threading.Event()

        def block_writer():
            with sqlite3.connect(path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                acquired.set()
                release.wait()
                time.sleep(lock_ms / 1000)
                connection.rollback()

        blocker = threading.Thread(target=block_writer)
        blocker.start()
        while not acquired.is_set():
            await asyncio.sleep(0.001)
        lag, reads, elapsed_calls = [], [], []
        done = asyncio.Event()

        async def heartbeat():
            while not done.is_set():
                target = time.perf_counter() + 0.005
                await asyncio.sleep(0.005)
                lag.append(max(0.0, time.perf_counter() - target) * 1000)

        async def unrelated_request():
            while not done.is_set():
                start = time.perf_counter()
                await asyncio.sleep(0.005)
                reads.append((time.perf_counter() - start) * 1000)

        async def client(index):
            for iteration in range(8):
                message = IncomingMessage(
                    sender_id=f"user-{index}",
                    channel_id=f"session-{index}",
                    content="synthetic",
                    metadata={"event_id": f"{index}-{iteration}"},
                )
                start = time.perf_counter()
                if mode == "worker":
                    await store.accept_inbound("benchmark", message)
                else:
                    store.accept_inbound("benchmark", message)
                elapsed_calls.append((time.perf_counter() - start) * 1000)
                await asyncio.sleep(0)

        monitor = asyncio.create_task(heartbeat())
        other = asyncio.create_task(unrelated_request())
        await asyncio.sleep(0.02)
        release.set()
        started = time.perf_counter()
        await asyncio.gather(*(client(index) for index in range(active)))
        elapsed = (time.perf_counter() - started) * 1000
        await asyncio.sleep(0.02)
        done.set()
        await asyncio.gather(monitor, other)
        metrics = store.metrics() if mode == "worker" else None
        close_start = time.perf_counter()
        if mode == "worker":
            await store.close()
        else:
            store.close()
        close_ms = (time.perf_counter() - close_start) * 1000
        blocker.join()
        return {
            "mode": mode,
            "active_sessions": active,
            "lock_ms": lock_ms,
            "operations": active * 8,
            "elapsed_ms": round(elapsed, 3),
            "heartbeat_lag_ms": distribution(lag),
            "unrelated_request_ms": distribution(reads),
            "storage_response_ms": distribution(elapsed_calls),
            "close_ms": round(close_ms, 3),
            "worker": metrics,
        }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("sync", "worker"), required=True)
    parser.add_argument("--active", nargs="+", type=int, default=[1, 8, 32])
    parser.add_argument("--lock-ms", type=float, default=500)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    results = []
    for active in args.active:
        for repetition in range(args.repeats):
            result = await sample(args.mode, active, args.lock_ms)
            result["repetition"] = repetition + 1
            results.append(result)
    print(
        json.dumps({"benchmark": "channel-storage-real-writer-lock", "samples": results}, indent=2)
    )


if __name__ == "__main__":
    asyncio.run(main())
