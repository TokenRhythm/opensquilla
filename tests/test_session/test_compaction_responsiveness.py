"""Long-history planning preserves complete rounds without starving gateway work."""

import asyncio
import threading
from copy import deepcopy

import pytest

from opensquilla.session import compaction
from opensquilla.session.storage import SessionStorage
from tests.helpers.compaction import synthetic_compaction_config


def _rounds(count):
    return [
        entry
        for index in range(count)
        for entry in (
            {"role": "user", "content": f"Source round {index}"},
            {"role": "assistant", "content": f"Response {index}"},
        )
    ]


def _request():
    return compaction.CompactionRequest(
        session_id="synthetic-responsive-compaction",
        entries=_rounds(3),
        context_window_tokens=4000,
        forced_prefix_cut=4,
        trigger="message_count",
        config=synthetic_compaction_config(),
    )


@pytest.mark.parametrize("preparation", [
    "_compaction_source_size", "_bound_summary_history", "_fit_compaction_input_to_target",
])
async def test_slow_compaction_preparation_allows_storage_reads(monkeypatch, preparation):
    loop = asyncio.get_running_loop()
    event_loop_thread = threading.get_ident()
    entered = asyncio.Event()
    released = threading.Event()
    original = getattr(compaction, preparation)

    def slow_preparation(*args, **kwargs):
        assert threading.get_ident() != event_loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert released.wait(5), "event loop could not service a storage read"
        return original(*args, **kwargs)

    monkeypatch.setattr(compaction, preparation, slow_preparation)
    storage = await SessionStorage.open(":memory:")
    request = _request()
    source = deepcopy(request.entries)
    task = asyncio.create_task(compaction.compact_context(request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert await asyncio.wait_for(storage.get_session("synthetic-missing"), 2) is None
        assert not task.done()
    finally:
        released.set()
        try:
            result = await task
        finally:
            await storage.close()

    assert result.removed_count == 4
    assert request.entries == source


async def test_cancellation_during_preparation_does_not_start_provider(monkeypatch):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    finished = asyncio.Event()
    released = threading.Event()
    original = compaction._compaction_source_size
    request = _request()
    source = deepcopy(request.entries)

    def slow_measure(entries, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            assert released.wait(5)
            return original(entries, **kwargs)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(compaction, "_compaction_source_size", slow_measure)
    task = asyncio.create_task(compaction.compact_context(request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        released.set()
        await asyncio.wait_for(finished.wait(), 2)

    assert request.config.llm_calls_started == 0
    assert request.entries == source
