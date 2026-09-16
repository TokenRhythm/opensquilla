"""Snapshot bounds, identity, consistency and scheduling regression tests."""

import asyncio
import base64
import json

import pytest

from opensquilla.gateway.snapshot_transfer import (
    SNAPSHOT_SEGMENT_BYTES,
    SnapshotTransfer,
    SnapshotTransferError,
    encode_segments,
    freeze_json,
)


class Budget:
    def __init__(self, limit=100 * 1024 * 1024):
        self.limit = limit
        self.used = 0

    def reserve(self, size):
        if self.used + size > self.limit:
            return False
        self.used += size
        return True

    def release(self, size):
        assert 0 <= size <= self.used
        self.used -= size


def snapshot(text="hello"):
    return {
        "key": "test",
        "task_id": "task",
        "stream_generation": "generation",
        "current_stream_seq": 50,
        "events": [{"event": "session.event.text_delta", "payload": {"text": text}}],
    }


async def test_segments_are_frozen_and_repeatable_and_identity_bound():
    budget = Budget()
    transfer = SnapshotTransfer(budget.reserve, budget.release)
    payload = snapshot('中文\\\"\n' * 200_000)
    first = await transfer.create("test", "revision", lambda: payload, identity=("session", 2))
    payload["events"][0]["payload"]["text"] = "mutated"
    parts = []
    for index in range(first["segment_count"]):
        part = transfer.read("test", "revision", first["snapshot_id"], index)
        assert part["session_id"] == "session" and part["session_epoch"] == 2
        assert len(part["data"]) <= 262_144
        assert transfer.read("test", "revision", first["snapshot_id"], index) == part
        parts.append(base64.b64decode(part["data"]))
    decoded = json.loads(b"".join(parts))
    assert decoded["events"][0]["payload"]["text"] == '中文\\\"\n' * 200_000
    assert sum(map(len, parts)) == first["byte_length"]
    assert all(len(part) == SNAPSHOT_SEGMENT_BYTES for part in parts[:-1])
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_STALE"):
        transfer.read("other", "revision", first["snapshot_id"], 0)
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_STALE"):
        await transfer.create("test", "revision", lambda: payload, identity=("session", 3))
    assert budget.used == 0


async def test_large_single_string_yields_and_does_not_read_mutating_live_state():
    payload = snapshot("a" * (8 * 1024 * 1024))
    frozen = freeze_json(payload)
    ticks = 0

    async def other_work():
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0)
            ticks += 1
            payload["events"][0]["payload"]["text"] = "new"

    task = asyncio.create_task(other_work())
    encoded = await encode_segments(frozen)
    assert ticks == 10
    assert json.loads(b"".join(encoded))["events"][0]["payload"]["text"] == "a" * (
        8 * 1024 * 1024
    )
    await task


async def test_busy_oversized_and_expired_release_budget():
    budget = Budget(limit=1)
    transfer = SnapshotTransfer(budget.reserve, budget.release)
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_BUSY"):
        await transfer.create("test", "1", snapshot)
    assert budget.used == 0
    budget.limit = 1_000_000
    transfer = SnapshotTransfer(budget.reserve, budget.release, max_bytes=1024, idle_seconds=0.01)
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_TOO_LARGE"):
        await transfer.create("test", "1", lambda: snapshot("a" * 1025))
    assert budget.used == 0
    first = await transfer.create("test", "2", snapshot)
    assert budget.used > 0
    await asyncio.sleep(0.03)
    assert budget.used == 0
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_EXPIRED"):
        transfer.read("test", "2", first["snapshot_id"], 0)


async def test_disconnect_during_encoding_cannot_install_or_release_budget_early():
    budget = Budget()
    transfer = SnapshotTransfer(budget.reserve, budget.release)
    task = asyncio.create_task(
        transfer.create("test", "1", lambda: snapshot("x" * 4_000_000))
    )
    await asyncio.sleep(0)
    assert budget.used > 0
    transfer.close()
    # The suspended encoder still owns buffers until it unwinds.
    assert budget.used > 0
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_STALE"):
        await task
    assert budget.used == 0


async def test_exact_utf8_limit_and_json_scalar_roundtrip():
    values = {"unicode": "😀中\\\"\n", "scalars": [None, False, True, 1, -3.2]}
    encoded = b"".join(await encode_segments(freeze_json(values)))
    assert json.loads(encoded) == values
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_TOO_LARGE"):
        await encode_segments(freeze_json(values), max_bytes=len(encoded) - 1)
    assert b"".join(await encode_segments(freeze_json(values), max_bytes=len(encoded))) == encoded


async def test_concurrent_create_is_busy_and_cancelled_build_releases():
    budget = Budget()
    transfer = SnapshotTransfer(budget.reserve, budget.release)
    task = asyncio.create_task(
        transfer.create("test", "1", lambda: snapshot("x" * 4_000_000))
    )
    await asyncio.sleep(0)
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_BUSY"):
        await transfer.create("other", "2", snapshot)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.used == 0


async def test_non_finite_invalid_keys_and_depth_are_local_errors():
    for value in ({"x": float("nan")}, {1: "not string"}):
        with pytest.raises(SnapshotTransferError, match="SNAPSHOT_STALE"):
            freeze_json(value)
    nested = {}
    for _ in range(130):
        nested = {"nested": nested}
    with pytest.raises(SnapshotTransferError, match="SNAPSHOT_TOO_LARGE"):
        freeze_json(nested)
