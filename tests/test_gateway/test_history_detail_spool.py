from __future__ import annotations

import asyncio
import errno
import hashlib
from collections.abc import Awaitable, Callable

import pytest

from opensquilla.gateway import history_detail as history_detail_module
from opensquilla.gateway.history_detail import (
    HistoryDetailCapacityError,
    HistoryDetailEntryTooLargeError,
    HistoryDetailSpool,
    HistoryDetailSpoolClearingError,
    HistoryDetailSpoolClosedError,
    HistoryDetailStorageError,
    HistoryDetailWriter,
)


def _builder(
    payload: bytes,
    calls: list[str],
    *,
    label: str = "build",
    write_size: int = 3,
) -> Callable[[HistoryDetailWriter], Awaitable[None]]:
    async def build(writer: HistoryDetailWriter) -> None:
        calls.append(label)
        for start in range(0, len(payload), write_size):
            await writer.write(payload[start : start + write_size])

    return build


@pytest.mark.asyncio
async def test_small_detail_stays_in_memory_and_returns_stable_chunks(tmp_path) -> None:
    spool = HistoryDetailSpool(
        memory_threshold_bytes=16,
        max_memory_bytes=32,
        disk_budget_bytes=32,
        max_chunk_bytes=8,
        temporary_parent=tmp_path,
    )
    payload = "你好-history".encode()
    calls: list[str] = []
    try:
        first = await spool.read_chunk(
            ("session", "cursor", "message"),
            offset=0,
            max_bytes=8,
            builder=_builder(payload, calls),
        )
        second = await spool.read_chunk(
            ("session", "cursor", "message"),
            offset=8,
            max_bytes=8,
            builder=_builder(b"must-not-run", calls, label="unexpected"),
        )

        assert first.data + second.data == payload
        assert first.offset == 0
        assert first.next_offset == 8
        assert second.next_offset is None
        assert first.total == second.total == len(payload)
        assert first.sha256 == second.sha256 == hashlib.sha256(payload).hexdigest()
        assert first.storage == second.storage == "memory"
        assert calls == ["build"]

        stats = await spool.stats()
        assert stats.entries == 1
        assert stats.memory_bytes == len(payload)
        assert stats.disk_bytes == 0
        assert stats.building_memory_bytes == 0
        assert stats.building_disk_bytes == 0
    finally:
        await spool.aclose()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_large_detail_may_exceed_soft_disk_budget_and_remains_readable(tmp_path) -> None:
    spool = HistoryDetailSpool(
        memory_threshold_bytes=8,
        max_memory_bytes=16,
        disk_budget_bytes=16,
        max_chunk_bytes=13,
        temporary_parent=tmp_path,
    )
    payload = bytes(range(79))
    calls: list[str] = []
    try:
        chunks: list[bytes] = []
        offset = 0
        while True:
            chunk = await spool.read_chunk(
                "oversized-soft-budget-entry",
                offset=offset,
                max_bytes=13,
                builder=_builder(payload, calls, write_size=5),
            )
            chunks.append(chunk.data)
            assert chunk.storage == "disk"
            assert chunk.total == len(payload)
            assert chunk.sha256 == hashlib.sha256(payload).hexdigest()
            if chunk.next_offset is None:
                break
            offset = chunk.next_offset

        assert b"".join(chunks) == payload
        assert calls == ["build"]
        stats = await spool.stats()
        assert stats.entries == 1
        assert stats.memory_bytes == 0
        assert stats.disk_bytes == len(payload)
        assert stats.disk_over_budget is True
    finally:
        await spool.aclose()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_detail_entry_has_a_hard_size_cap(tmp_path) -> None:
    spool = HistoryDetailSpool(
        memory_threshold_bytes=4,
        max_memory_bytes=8,
        disk_budget_bytes=64,
        max_entry_bytes=12,
        max_chunk_bytes=8,
        temporary_parent=tmp_path,
    )
    try:
        with pytest.raises(HistoryDetailEntryTooLargeError):
            await spool.read_chunk(
                "too-large",
                offset=0,
                max_bytes=8,
                builder=_builder(b"x" * 13, [], write_size=5),
            )
        stats = await spool.stats()
        assert stats.entries == stats.inflight == 0
        assert stats.memory_bytes == stats.building_memory_bytes == 0
        assert stats.disk_bytes == stats.building_disk_bytes == 0
    finally:
        await spool.aclose()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_expired_disk_entry_is_released_without_another_cache_call(tmp_path) -> None:
    spool = HistoryDetailSpool(
        memory_threshold_bytes=4,
        max_memory_bytes=8,
        disk_budget_bytes=64,
        ttl_seconds=0.01,
        max_chunk_bytes=8,
        temporary_parent=tmp_path,
    )
    try:
        await spool.read_chunk(
            "expires",
            offset=0,
            max_bytes=8,
            builder=_builder(b"disk-backed", []),
        )
        await asyncio.sleep(0.05)
        # Inspect counters directly so the assertion proves the scheduled TTL
        # cleanup ran; stats() itself also performs a defensive lazy prune.
        assert spool._entries == {}  # noqa: SLF001
        assert spool._disk_bytes == 0  # noqa: SLF001
    finally:
        await spool.aclose()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_same_key_build_is_single_flight_and_waiter_cancel_does_not_cancel_it(
    tmp_path,
) -> None:
    spool = HistoryDetailSpool(
        memory_threshold_bytes=8,
        max_memory_bytes=16,
        disk_budget_bytes=64,
        max_chunk_bytes=16,
        temporary_parent=tmp_path,
    )
    payload = b"single-flight-payload"
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def build(writer: HistoryDetailWriter) -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        await writer.write(payload[:7])
        await writer.write(payload[7:])

    try:
        first = asyncio.create_task(
            spool.read_chunk("same", offset=0, max_bytes=16, builder=build)
        )
        await started.wait()
        followers = [
            asyncio.create_task(
                spool.read_chunk("same", offset=0, max_bytes=16, builder=build)
            )
            for _ in range(5)
        ]
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        release.set()
        chunks = await asyncio.gather(*followers)
        assert calls == 1
        assert all(chunk.data == payload[:16] for chunk in chunks)
        assert all(chunk.total == len(payload) for chunk in chunks)
    finally:
        await spool.aclose()


@pytest.mark.asyncio
async def test_builder_failure_cleans_spool_and_wakes_every_waiter(tmp_path) -> None:
    spool = HistoryDetailSpool(
        memory_threshold_bytes=4,
        max_memory_bytes=8,
        disk_budget_bytes=32,
        temporary_parent=tmp_path,
    )
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def fail(writer: HistoryDetailWriter) -> None:
        nonlocal calls
        calls += 1
        await writer.write(b"already-on-disk")
        started.set()
        await release.wait()
        raise RuntimeError("synthetic projection failure")

    try:
        first = asyncio.create_task(
            spool.read_chunk("failure", offset=0, max_bytes=8, builder=fail)
        )
        await started.wait()
        second = asyncio.create_task(
            spool.read_chunk("failure", offset=0, max_bytes=8, builder=fail)
        )
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        assert calls == 1
        assert all(isinstance(result, RuntimeError) for result in results)
        assert all(str(result) == "synthetic projection failure" for result in results)
        stats = await spool.stats()
        assert stats.entries == 0
        assert stats.inflight == 0
        assert stats.memory_bytes == stats.building_memory_bytes == 0
        assert stats.disk_bytes == stats.building_disk_bytes == 0
    finally:
        await spool.aclose()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_clear_cancels_shared_build_cleans_resources_and_allows_reuse(tmp_path) -> None:
    spool = HistoryDetailSpool(
        memory_threshold_bytes=4,
        max_memory_bytes=8,
        disk_budget_bytes=32,
        temporary_parent=tmp_path,
    )
    started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked(writer: HistoryDetailWriter) -> None:
        await writer.write(b"disk-backed-build")
        started.set()
        await never_release.wait()

    first = asyncio.create_task(
        spool.read_chunk("blocked", offset=0, max_bytes=8, builder=blocked)
    )
    await started.wait()
    second = asyncio.create_task(
        spool.read_chunk("blocked", offset=0, max_bytes=8, builder=blocked)
    )
    await asyncio.sleep(0)

    await spool.clear()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, HistoryDetailSpoolClearingError) for result in results)
    stats = await spool.stats()
    assert stats.entries == stats.inflight == 0
    assert stats.memory_bytes == stats.building_memory_bytes == 0
    assert stats.disk_bytes == stats.building_disk_bytes == 0

    calls: list[str] = []
    reused = await spool.read_chunk(
        "after-clear",
        offset=0,
        max_bytes=8,
        builder=_builder(b"healthy", calls),
    )
    assert reused.data == b"healthy"
    assert calls == ["build"]

    await spool.aclose()
    await spool.aclose()
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(HistoryDetailSpoolClosedError):
        await spool.read_chunk(
            "closed",
            offset=0,
            max_bytes=8,
            builder=_builder(b"no", []),
        )


@pytest.mark.asyncio
async def test_memory_disk_ttl_and_entry_lru_bounds(tmp_path) -> None:
    now = 100.0

    def clock() -> float:
        return now

    spool = HistoryDetailSpool(
        memory_threshold_bytes=8,
        max_memory_bytes=8,
        disk_budget_bytes=10,
        max_entries=2,
        ttl_seconds=5,
        max_chunk_bytes=8,
        temporary_parent=tmp_path,
        clock=clock,
    )
    calls: list[str] = []
    try:
        await spool.read_chunk(
            "memory-a", offset=0, max_bytes=8, builder=_builder(b"aaaaaaaa", calls, label="a")
        )
        await spool.read_chunk(
            "memory-b", offset=0, max_bytes=8, builder=_builder(b"bbbbbbbb", calls, label="b")
        )
        stats = await spool.stats()
        assert stats.memory_bytes == 8
        assert stats.entries == 1

        await spool.read_chunk(
            "disk-a", offset=0, max_bytes=8, builder=_builder(b"A" * 9, calls, label="da")
        )
        await spool.read_chunk(
            "disk-b", offset=0, max_bytes=8, builder=_builder(b"B" * 9, calls, label="db")
        )
        stats = await spool.stats()
        assert stats.entries <= 2
        assert stats.memory_bytes <= 8
        assert stats.disk_bytes <= 10

        await spool.read_chunk(
            "disk-a", offset=0, max_bytes=8, builder=_builder(b"A" * 9, calls, label="da")
        )
        assert calls.count("da") == 2

        now += 6
        stats = await spool.stats()
        assert stats.entries == 0
        assert stats.memory_bytes == 0
        assert stats.disk_bytes == 0
    finally:
        await spool.aclose()


@pytest.mark.asyncio
async def test_entry_limit_bounds_distinct_inflight_builds(tmp_path) -> None:
    spool = HistoryDetailSpool(
        memory_threshold_bytes=8,
        max_memory_bytes=8,
        disk_budget_bytes=16,
        max_entries=1,
        temporary_parent=tmp_path,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked(writer: HistoryDetailWriter) -> None:
        started.set()
        await release.wait()
        await writer.write(b"first")

    first = asyncio.create_task(
        spool.read_chunk("first", offset=0, max_bytes=8, builder=blocked)
    )
    await started.wait()
    try:
        with pytest.raises(HistoryDetailCapacityError):
            await spool.read_chunk(
                "second",
                offset=0,
                max_bytes=8,
                builder=_builder(b"second", []),
            )
        release.set()
        assert (await first).data == b"first"
    finally:
        release.set()
        await asyncio.gather(first, return_exceptions=True)
        await spool.aclose()


@pytest.mark.asyncio
async def test_temporary_file_failure_is_explicit_and_leaves_no_accounted_bytes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool = HistoryDetailSpool(
        memory_threshold_bytes=4,
        max_memory_bytes=8,
        disk_budget_bytes=16,
        temporary_parent=tmp_path,
    )

    def fail_temporary_file(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError(errno.ENOSPC, "synthetic disk full")

    monkeypatch.setattr(history_detail_module.tempfile, "TemporaryFile", fail_temporary_file)
    try:
        with pytest.raises(HistoryDetailStorageError, match="Unable to create"):
            await spool.read_chunk(
                "disk-full",
                offset=0,
                max_bytes=8,
                builder=_builder(b"must-spill", []),
            )
        stats = await spool.stats()
        assert stats.entries == stats.inflight == 0
        assert stats.memory_bytes == stats.building_memory_bytes == 0
        assert stats.disk_bytes == stats.building_disk_bytes == 0
    finally:
        await spool.aclose()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_chunk_arguments_are_bounded_before_builder_runs(tmp_path) -> None:
    spool = HistoryDetailSpool(
        memory_threshold_bytes=8,
        max_memory_bytes=8,
        disk_budget_bytes=16,
        max_chunk_bytes=4,
        temporary_parent=tmp_path,
    )
    calls: list[str] = []
    try:
        for offset, size in [(-1, 1), (True, 1), (0, 0), (0, 5), (0, True)]:
            with pytest.raises(ValueError):
                await spool.read_chunk(
                    "invalid",
                    offset=offset,
                    max_bytes=size,
                    builder=_builder(b"no", calls),
                )
        assert calls == []

        with pytest.raises(ValueError, match="between 0 and 3"):
            await spool.read_chunk(
                "valid-build",
                offset=4,
                max_bytes=1,
                builder=_builder(b"abc", calls),
            )
        assert calls == ["build"]
    finally:
        await spool.aclose()
