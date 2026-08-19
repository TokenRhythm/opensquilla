"""Tests for the turn-local lazy transcript snapshot."""

from __future__ import annotations

import asyncio

import pytest

from opensquilla.engine.turn_runner import TurnTranscriptSnapshot


@pytest.mark.asyncio
async def test_get_entries_caches_one_successful_load() -> None:
    calls = 0

    async def loader() -> list[str]:
        nonlocal calls
        calls += 1
        return ["user", "assistant"]

    snapshot = TurnTranscriptSnapshot(loader)

    first = await snapshot.get_entries()
    second = await snapshot.get_entries()

    assert first == ("user", "assistant")
    assert second is first
    assert calls == 1
    assert snapshot.load_count == 1
    assert snapshot.generation == 0


@pytest.mark.asyncio
async def test_get_entries_caches_empty_tuple() -> None:
    calls = 0

    async def loader() -> list[str]:
        nonlocal calls
        calls += 1
        return []

    snapshot = TurnTranscriptSnapshot(loader)

    assert await snapshot.get_entries() == ()
    assert await snapshot.get_entries() == ()
    assert calls == 1
    assert snapshot.load_count == 1


@pytest.mark.asyncio
async def test_get_entries_does_not_cache_loader_exception() -> None:
    calls = 0

    async def loader() -> list[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient read failure")
        return ["recovered"]

    snapshot = TurnTranscriptSnapshot(loader)

    with pytest.raises(RuntimeError, match="transient read failure"):
        await snapshot.get_entries()

    assert await snapshot.get_entries() == ("recovered",)
    assert calls == 2
    assert snapshot.load_count == 2
    assert snapshot.generation == 0


@pytest.mark.asyncio
async def test_invalidate_reloads_in_next_generation() -> None:
    calls = 0

    async def loader() -> list[int]:
        nonlocal calls
        calls += 1
        return [calls]

    snapshot = TurnTranscriptSnapshot(loader)

    assert await snapshot.get_entries() == (1,)
    snapshot.invalidate()

    assert snapshot.generation == 1
    assert snapshot.load_count == 1
    assert await snapshot.get_entries() == (2,)
    assert snapshot.load_count == 2


@pytest.mark.asyncio
async def test_concurrent_get_entries_uses_one_loader_call() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def loader() -> list[str]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return ["shared"]

    snapshot = TurnTranscriptSnapshot(loader)
    tasks = [asyncio.create_task(snapshot.get_entries()) for _ in range(8)]

    await entered.wait()
    release.set()
    results = await asyncio.gather(*tasks)

    assert results == [("shared",)] * 8
    assert calls == 1
    assert snapshot.load_count == 1


@pytest.mark.asyncio
async def test_invalidation_during_load_discards_stale_result() -> None:
    first_load_entered = asyncio.Event()
    release_first_load = asyncio.Event()
    calls = 0

    async def loader() -> list[int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_load_entered.set()
            await release_first_load.wait()
        return [calls]

    snapshot = TurnTranscriptSnapshot(loader)
    task = asyncio.create_task(snapshot.get_entries())

    await first_load_entered.wait()
    snapshot.invalidate()
    release_first_load.set()

    assert await task == (2,)
    assert snapshot.load_count == 2
    assert snapshot.generation == 1


def test_counters_are_read_only() -> None:
    async def loader() -> list[str]:
        return []

    snapshot = TurnTranscriptSnapshot(loader)

    with pytest.raises(AttributeError):
        setattr(snapshot, "load_count", 3)
    with pytest.raises(AttributeError):
        setattr(snapshot, "generation", 4)
