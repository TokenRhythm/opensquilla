"""Attempt ownership, bounded build memory, and installation reclamation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from opensquilla.gateway import snapshot_transfer as snapshots
from opensquilla.gateway.snapshot_transfer import SnapshotRegistry, SnapshotTransferError
from opensquilla.gateway.transport_flow import (
    CONTROL_BUFFER_BYTES,
    GLOBAL_BUFFER_BYTES,
    RECOVERY_WINDOW_BYTES,
    FlowWindow,
    TransportBudget,
)


def payload(key: str, size: int = 100) -> dict:
    return {
        "key": key, "task_id": None, "stream_generation": "stream",
        "current_stream_seq": 3,
        "events": [{"event": "session.event.text_delta", "payload": {"text": "x" * size}}],
    }


def receipt(part: dict) -> dict:
    return {**{k: part[k] for k in ("key", "sync_revision", "snapshot_id", "stream_generation")},
            "stream_seq": part["current_stream_seq"]}


async def test_two_sessions_build_without_reserving_two_maximum_snapshots():
    budget = TransportBudget(3 * 1024 * 1024)
    registry = SnapshotRegistry(budget.reserve, budget.release)
    first = registry.admit("a", "r1", object())
    second = registry.admit("b", "r1", object())
    try:
        a, b = await asyncio.gather(
            first.create("a", "r1", lambda: payload("a", 600_000)),
            second.create("b", "r1", lambda: payload("b", 600_000)),
        )
        assert first.matches_install(receipt(a))
        assert second.matches_install(receipt(b))
        assert 1_200_000 < budget.used <= budget.limit
    finally:
        registry.close()
    assert budget.used == 0


async def test_old_release_and_captured_worker_cannot_touch_new_owner():
    budget = TransportBudget()
    registry = SnapshotRegistry(budget.reserve, budget.release)
    old_lease, new_lease = object(), object()
    old = registry.admit("a", "old", old_lease)
    registry.release("a", "old")
    current = registry.admit("a", "new", new_lease)
    try:
        with pytest.raises(SnapshotTransferError, match="STALE"):
            await old.create("a", "old", lambda: payload("a"))
        part = await current.create("a", "new", lambda: payload("a"), identity=("same", 0))
        assert not registry.release("a", "old")
        assert not registry.release("a", "new", "wrong-id")
        registry.retire_lease("a", old_lease)
        assert current.matches_install(receipt(part))
        with pytest.raises(SnapshotTransferError, match="STALE"):
            registry.admit("a", "new", old_lease)
    finally:
        registry.close()
    assert budget.used == 0


async def test_retry_reuses_bytes_and_never_renews_admission_deadline(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(snapshots, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    budget = TransportBudget()
    registry = SnapshotRegistry(budget.reserve, budget.release, hard_seconds=120)
    lease = object()
    transfer = registry.admit("a", "revision", lease)
    calls = 0

    def capture():
        nonlocal calls
        calls += 1
        return payload("a")

    first = await transfer.create("a", "revision", capture)
    clock[0] = 129
    assert registry.admit("a", "revision", lease) is transfer
    again = await transfer.create("a", "revision", capture)
    assert first == again and calls == 1 and transfer.deadline == 130
    clock[0] = 130
    with pytest.raises(SnapshotTransferError, match="EXPIRED"):
        transfer.read("a", "revision", first["snapshot_id"], 0)
    assert budget.used == 0
    registry.close()


async def test_installing_reclaims_body_but_preserves_exact_receipt():
    budget = TransportBudget()
    registry = SnapshotRegistry(budget.reserve, budget.release)
    transfer = registry.admit("a", "r", object())
    try:
        part = await transfer.create("a", "r", lambda: payload("a", 500_000), identity=("id", 2))
        proof = receipt(part)
        with pytest.raises(SnapshotTransferError, match="BUSY"):
            transfer.begin_install(proof)
        for index in range(part["segment_count"]):
            transfer.stage_piece(index)
        assert budget.used > 500_000
        assert transfer.begin_install(proof) == ("id", 2)
        assert budget.used == 0
        assert transfer.matches_install(proof)
        assert transfer.begin_install(proof) == ("id", 2)
        with pytest.raises(SnapshotTransferError, match="EXPIRED"):
            await transfer.create("a", "r", lambda: pytest.fail("must not rebuild"),
                                  identity=("id", 2))
    finally:
        registry.close()


async def test_budget_failure_unwinds_partial_build_without_waiting():
    budget = TransportBudget(1024 * 1024)
    registry = SnapshotRegistry(budget.reserve, budget.release)
    transfer = registry.admit("a", "r", object())
    try:
        with pytest.raises(SnapshotTransferError, match="BUSY"):
            await asyncio.wait_for(
                transfer.create("a", "r", lambda: payload("a", 2 * 1024 * 1024)), 1
            )
        assert budget.used == 0 and not transfer.building
        part = await transfer.create("a", "r", lambda: payload("a"))
        assert transfer.matches_install(receipt(part))
    finally:
        registry.close()


def test_global_headroom_allows_control_and_recovery_when_bulk_is_full():
    budget = TransportBudget()
    bulk = GLOBAL_BUFFER_BYTES - CONTROL_BUFFER_BYTES - RECOVERY_WINDOW_BYTES
    assert budget.reserve(bulk, kind="bulk")
    assert not budget.reserve(1, kind="bulk")
    assert budget.reserve(CONTROL_BUFFER_BYTES, kind="control")
    assert budget.reserve(RECOVERY_WINDOW_BYTES, kind="recovery")
    assert budget.used == GLOBAL_BUFFER_BYTES
    budget.release(RECOVERY_WINDOW_BYTES, kind="recovery")
    assert not budget.reserve(1, kind="bulk")
    assert budget.reserve(RECOVERY_WINDOW_BYTES, kind="recovery")
    budget.release(bulk, kind="bulk")
    budget.release(CONTROL_BUFFER_BYTES, kind="control")
    budget.release(RECOVERY_WINDOW_BYTES, kind="recovery")
    assert budget.used == 0


def test_two_pieces_stage_out_of_order_without_crossing_ordinary_hole():
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release, recovery_limit=2)
    ordinary = flow.admit(100)
    flow.mark_sent(ordinary)
    a = flow.admit(1000, recovery=True, key="a")
    b = flow.admit(1000, recovery=True, key="b")
    assert a == 2 and b == 3
    assert flow.admit(1000, recovery=True, key="a", owner=object()) is None
    flow.mark_sent(b)
    flow.stage(flow.epoch, b)
    with pytest.raises(ValueError, match="unsent"):
        flow.stage(flow.epoch, a)
    flow.mark_sent(a)
    flow.stage(flow.epoch, a)
    flow.stage(flow.epoch, b)
    assert flow.ack_id == 0 and list(flow.deliveries) == [ordinary] and budget.used == 100
    flow.close()
    assert budget.used == 0


def test_exact_publication_and_tombstone_never_counts_as_staged_snapshot():
    budget = TransportBudget()
    staged = []
    flow = FlowWindow(budget.reserve, budget.release, recovery_limit=2, on_stage=staged.append)
    ordinary = flow.admit(100)
    flow.mark_sent(ordinary)
    cancelled = flow.admit(1000, recovery=True, key="a", owner=object(), segment_index=0)
    assert flow.claim(cancelled, "tombstone")
    assert not flow.claim(cancelled, "original")
    assert not flow.claim(cancelled, "tombstone")
    flow.mark_sending(cancelled)
    flow.stage(flow.epoch, cancelled)
    assert budget.used == 1100 and not staged
    assert flow.admit(1000, recovery=True, key="a") is None
    flow.mark_sent(cancelled)
    current = flow.admit(1000, recovery=True, key="a", segment_index=0)
    assert flow.claim(current, "original")
    flow.mark_sent(current)
    flow.stage(flow.epoch, current)
    flow.stage(flow.epoch, current)
    assert len(staged) == 1 and staged[0].segment_index == 0
    assert flow.ack_id == 0
    flow.close()
    assert budget.used == 0


def test_dirty_barrier_cannot_aba_or_change_for_an_unrelated_session():
    budget = TransportBudget()
    flow = FlowWindow(budget.reserve, budget.release)
    clean_b = flow.dirty_revision("b")
    flow.mark_dirty("a", "generation", 1)
    old_a = flow.dirty_revision("a")
    flow.dirty.pop("a")
    flow.mark_dirty("a", "generation", 1)
    assert old_a != flow.dirty_revision("a")
    assert clean_b == flow.dirty_revision("b")
    flow.close()


def test_close_keeps_native_write_charged_until_it_unwinds():
    budget = TransportBudget()
    staged = []
    flow = FlowWindow(budget.reserve, budget.release, on_stage=staged.append)
    piece = flow.admit(1000, recovery=True, key="a")
    assert flow.claim(piece, "original")
    flow.mark_sending(piece)
    flow.close()
    flow.close()
    assert budget.used == 1000 and not staged
    assert flow.admit(1) is None
    flow.mark_send_finished(piece)
    flow.mark_send_finished(piece)
    assert budget.used == 0 and not staged


async def test_encoder_capacity_includes_retiring_builds_across_connections(monkeypatch):
    budget = TransportBudget()
    registries = [SnapshotRegistry(budget.reserve, budget.release) for _ in range(3)]
    gates = [asyncio.Event() for _ in range(4)]
    entered = asyncio.Event()
    original = snapshots.encode_segments
    calls = 0

    async def gated_encode(*args, **kwargs):
        nonlocal calls
        index = calls
        calls += 1
        if calls == 4:
            entered.set()
        if index < 4:
            await gates[index].wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "encode_segments", gated_encode)
    transfers = [registries[i // 2].admit(str(i), "r", object()) for i in range(4)]
    tasks = [asyncio.create_task(item.create(str(i), "r", lambda: payload("a")))
             for i, item in enumerate(transfers)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        transfers[0].close()
        charged = budget.used
        assert charged >= 4 * 2 * snapshots.SNAPSHOT_SEGMENT_BYTES
        fifth = registries[2].admit("fifth", "r", object())
        with pytest.raises(SnapshotTransferError, match="BUSY"):
            await fifth.create("fifth", "r", lambda: payload("fifth"))
        assert budget.used == charged
        gates[0].set()
        with pytest.raises(SnapshotTransferError, match="STALE"):
            await tasks[0]
        assert budget.used < charged
        part = await fifth.create("fifth", "r", lambda: payload("fifth"))
        assert fifth.matches_install(receipt(part))
    finally:
        for gate in gates:
            gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for registry in registries:
            registry.close()
    assert budget.used == 0 and snapshots._active_encoders == 0


async def test_published_first_piece_retry_bypasses_saturated_encoder_capacity(monkeypatch):
    budget = TransportBudget()
    registries = [SnapshotRegistry(budget.reserve, budget.release) for _ in range(2)]
    published = registries[0].admit("published", "r", object())
    first = await published.create("published", "r", lambda: payload("published"))
    original_deadline = published.deadline
    original = snapshots.encode_segments
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def gated_encode(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(snapshots, "encode_segments", gated_encode)
    transfers = [registries[index // 2].admit(str(index), "r", object()) for index in range(4)]
    tasks = [asyncio.create_task(transfer.create(str(index), "r", lambda: payload("building")))
             for index, transfer in enumerate(transfers)]
    try:
        await asyncio.wait_for(entered.wait(), 1)
        transfers[0].close()
        charged = budget.used
        again = await published.create(
            "published", "r", lambda: pytest.fail("published first piece must not recapture"),
        )
        assert again == first
        assert published.deadline == original_deadline
        assert budget.used == charged
        assert snapshots._active_encoders == 4 and not release.is_set()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for registry in registries:
            registry.close()
    assert budget.used == 0 and snapshots._active_encoders == 0
