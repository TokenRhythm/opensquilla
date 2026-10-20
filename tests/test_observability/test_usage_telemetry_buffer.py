from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from opensquilla.observability import daily_usage_store, install_telemetry, usage_telemetry
from opensquilla.observability.daily_usage_store import DailyUsageDelta, DailyUsageStore


@pytest.fixture
def buffered_usage(tmp_path, monkeypatch):
    config = SimpleNamespace(state_dir=str(tmp_path / "state"), disabled=False)
    clock = [datetime(2026, 7, 20, 23, 59, 59, tzinfo=UTC)]

    class Clock:
        @staticmethod
        def now(_timezone):
            return clock[0]

    async def skip_upload(*_args, **_kwargs):
        return None

    monkeypatch.setattr(usage_telemetry, "datetime", Clock)
    monkeypatch.setattr(daily_usage_store, "_utc_timestamp", lambda: clock[0].timestamp())
    monkeypatch.setattr(usage_telemetry, "_upload_pending_sources", skip_upload)
    monkeypatch.setattr(
        install_telemetry, "_telemetry_skip_reason",
        lambda *, config: "disabled" if config.disabled else None,
    )
    return config, clock


def _turn(**values):
    return SimpleNamespace(**({
        "input_tokens": 10, "output_tokens": 3,
        "cached_tokens": 4, "cache_write_tokens": 2,
    } | values))


class _Store:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.batches: list[tuple[DailyUsageDelta, ...]] = []
        self.fail = False
        self.opened = False
        self.closed = False

    async def open(self, _path):
        self.opened = True
        return self

    async def record_daily_usage_batch(self, rows, **_kwargs):
        self.started.set()
        await self.release.wait()
        if self.fail:
            raise OSError("Synthetic counter storage failure")
        self.batches.append(rows)

    async def close(self):
        self.closed = True


async def test_recording_never_waits_for_busy_counter_storage(buffered_usage, monkeypatch):
    config, _clock = buffered_usage
    store = _Store()
    store.release.clear()
    monkeypatch.setattr(usage_telemetry, "DailyUsageStore", store)
    runtime = usage_telemetry.StandaloneUsageTelemetry(
        config=config, legacy_storage=None, flush_interval_seconds=3600,
    )
    await runtime.record_turn(run_kind="default", done_event=_turn())
    assert not store.opened
    flush = asyncio.create_task(runtime.flush())
    await asyncio.wait_for(store.started.wait(), timeout=1)
    try:
        for _ in range(99):
            await asyncio.wait_for(
                runtime.record_turn(run_kind="default", done_event=_turn()), timeout=0.2,
            )
        assert not flush.done()
    finally:
        store.release.set()
        await flush
        await runtime.close()

    assert [batch[0].conversation_turns for batch in store.batches] == [1, 99]
    assert sum(batch[0].input_tokens for batch in store.batches) == 1000
    assert store.closed


async def test_one_hundred_turns_use_one_background_counter_transaction(
    buffered_usage, monkeypatch,
):
    config, _clock = buffered_usage
    statements = []
    connections = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connections.append(connection)
        connection.set_trace_callback(lambda sql: statements.append((id(connection), sql)))
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    runtime = usage_telemetry.StandaloneUsageTelemetry(
        config=config, legacy_storage=None, flush_interval_seconds=3600,
    )
    try:
        for _ in range(100):
            await runtime.record_turn(run_kind="default", done_event=_turn())
        assert statements == []
        assert not usage_telemetry.standalone_daily_usage_path(config).exists()
        await runtime.flush()
        writes = [
            (connection, sql) for connection, sql in statements
            if sql.lstrip().startswith("INSERT INTO telemetry_daily_usage")
        ]
        assert len(writes) == 1
        transaction = [sql for connection, sql in statements if connection == writes[0][0]]
        assert transaction.count("BEGIN IMMEDIATE") == 1
        assert transaction.count("COMMIT") == 1
        rows = await runtime._storage.list_pending_daily_usage(before_day="9999-12-31")
        assert len(rows) == 1
        assert rows[0]["conversation_turns"] == 100
        assert rows[0]["input_tokens"] == 1000
        assert rows[0]["output_tokens"] == 300
        assert rows[0]["cached_tokens"] == 400
        assert rows[0]["cache_write_tokens"] == 200
    finally:
        await runtime.close()


async def test_periodic_flush_batches_turns_and_preserves_utc_days(buffered_usage):
    config, clock = buffered_usage
    runtime = usage_telemetry.StandaloneUsageTelemetry(
        config=config, legacy_storage=None, flush_interval_seconds=0.01,
    )
    for _ in range(20):
        await runtime.record_turn(run_kind="default", done_event=_turn())
    clock[0] += timedelta(seconds=2)
    await runtime.record_turn(run_kind="channel_turn", done_event=_turn(input_tokens=-8))
    try:
        async with asyncio.timeout(2):
            while runtime._pending:
                await asyncio.sleep(0.01)
    finally:
        await runtime.close()

    restored = await DailyUsageStore.open(usage_telemetry.standalone_daily_usage_path(config))
    try:
        rows = await restored.list_pending_daily_usage(before_day="9999-12-31")
        assert [row["day"] for row in rows] == ["2026-07-20", "2026-07-21"]
        assert [row["conversation_turns"] for row in rows] == [20, 1]
        assert [row["input_tokens"] for row in rows] == [200, 0]
        assert [row["output_tokens"] for row in rows] == [60, 3]
        assert [row["cached_tokens"] for row in rows] == [80, 4]
        assert [row["cache_write_tokens"] for row in rows] == [40, 2]
    finally:
        await restored.close()


async def test_failed_flush_retries_complete_aggregate_once(buffered_usage, monkeypatch):
    config, _clock = buffered_usage
    store = _Store()
    store.fail = True
    monkeypatch.setattr(usage_telemetry, "DailyUsageStore", store)
    runtime = usage_telemetry.StandaloneUsageTelemetry(
        config=config, legacy_storage=None, flush_interval_seconds=3600,
    )
    await runtime.record_turn(run_kind="default", done_event=_turn())
    with pytest.raises(OSError, match="Synthetic"):
        await runtime.flush()
    await runtime.record_turn(run_kind="web_turn", done_event=_turn())
    store.fail = False
    await runtime.flush()
    await runtime.close()
    assert len(store.batches) == 1
    assert store.batches[0][0].conversation_turns == 2
    assert store.batches[0][0].input_tokens == 20


async def test_cancelled_flush_waiter_cannot_duplicate_committed_totals(
    buffered_usage, monkeypatch,
):
    config, _clock = buffered_usage
    store = _Store()
    store.release.clear()
    monkeypatch.setattr(usage_telemetry, "DailyUsageStore", store)
    runtime = usage_telemetry.StandaloneUsageTelemetry(
        config=config, legacy_storage=None, flush_interval_seconds=3600,
    )
    await runtime.record_turn(run_kind="default", done_event=_turn())
    waiter = asyncio.create_task(runtime.flush())
    await asyncio.wait_for(store.started.wait(), timeout=1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    store.release.set()
    await runtime.flush()
    await runtime.close()
    assert len(store.batches) == 1
    assert store.batches[0][0].conversation_turns == 1


async def test_buffer_is_bounded_by_utc_days_and_keeps_accepted_opted_out_usage(
    buffered_usage, monkeypatch,
):
    config, clock = buffered_usage
    store = _Store()
    monkeypatch.setattr(usage_telemetry, "DailyUsageStore", store)
    runtime = usage_telemetry.StandaloneUsageTelemetry(
        config=config, legacy_storage=None, flush_interval_seconds=3600, max_buffer_days=2,
    )
    for _ in range(3):
        await runtime.record_turn(run_kind="default", done_event=_turn())
        clock[0] += timedelta(days=1)
    assert len(runtime._pending) == 2
    config.disabled = True
    await runtime.record_turn(run_kind="default", done_event=_turn())
    await runtime.close()
    assert len(store.batches) == 1
    assert [row.day for row in store.batches[0]] == ["2026-07-20", "2026-07-21"]
    assert sum(row.conversation_turns for row in store.batches[0]) == 2


async def test_shutdown_deadline_does_not_wait_for_stalled_flush(buffered_usage, monkeypatch):
    config, _clock = buffered_usage
    store = _Store()
    store.release.clear()
    monkeypatch.setattr(usage_telemetry, "DailyUsageStore", store)
    monkeypatch.setattr(usage_telemetry, "STANDALONE_CLOSE_TIMEOUT_SECONDS", 0.01)
    runtime = usage_telemetry.StandaloneUsageTelemetry(
        config=config, legacy_storage=None, flush_interval_seconds=3600,
    )
    await runtime.record_turn(run_kind="default", done_event=_turn())
    flush = asyncio.create_task(runtime.flush())
    await asyncio.wait_for(store.started.wait(), timeout=1)
    await runtime.record_turn(run_kind="default", done_event=_turn())
    try:
        await asyncio.wait_for(runtime.close(), timeout=0.2)
        assert store.started.is_set()
        assert not store.closed
        assert not store.batches
    finally:
        store.release.set()
        await flush
        await asyncio.gather(runtime._close_task, return_exceptions=True)
    assert store.closed
    assert [batch[0].conversation_turns for batch in store.batches] == [1, 1]


async def test_batch_adds_to_old_rows_in_one_transaction(tmp_path, monkeypatch):
    path = tmp_path / "usage.sqlite3"
    storage = await DailyUsageStore.open(path)
    await storage.record_daily_usage(
        day="2026-07-20", input_tokens=10, output_tokens=3,
        cached_tokens=4, cache_write_tokens=2, updated_at=1,
    )
    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    try:
        await storage.record_daily_usage_batch((
            DailyUsageDelta("2026-07-20", 20, 200, 60, 80, 40, 2),
            DailyUsageDelta("2026-07-21", 2, 20, 6, 8, 4, 3),
        ))
        assert sum(statement == "BEGIN IMMEDIATE" for statement in statements) == 1
        assert sum(statement == "COMMIT" for statement in statements) == 1
        rows = await storage.list_pending_daily_usage(before_day="9999-12-31")
        assert [row["conversation_turns"] for row in rows] == [21, 2]
        assert [row["input_tokens"] for row in rows] == [210, 20]
        assert [row["output_tokens"] for row in rows] == [63, 6]
        assert [row["cached_tokens"] for row in rows] == [84, 8]
        assert [row["cache_write_tokens"] for row in rows] == [42, 4]
    finally:
        await storage.close()


@pytest.mark.parametrize("late_flush", [False, True])
async def test_two_sinks_finalize_shared_day_without_changed_idempotent_payload(
    buffered_usage, monkeypatch, late_flush,
):
    config, clock = buffered_usage
    first = usage_telemetry.StandaloneUsageTelemetry(config=config, legacy_storage=None)
    second = usage_telemetry.StandaloneUsageTelemetry(config=config, legacy_storage=None)
    sent = []
    accepted = {}

    async def post(_endpoint, payload):
        sent.append(payload)
        accepted.setdefault(payload["event_id"], payload["conversation_turns"])
        return True, None

    monkeypatch.setattr(usage_telemetry, "_post_payload", post)
    monkeypatch.setattr(
        install_telemetry, "ensure_install_telemetry_id", lambda **_kw: "synthetic-device",
    )
    try:
        await first.record_turn(run_kind="default", done_event=_turn())
        await second.record_turn(run_kind="default", done_event=_turn())
        await first.flush()
        clock[0] = datetime(2026, 7, 21, 0, 0, 5, tzinfo=UTC)
        assert await usage_telemetry.upload_pending_standalone_daily_usage(config=config) == 0
        assert sent == []
        if not late_flush:
            await second.flush()
        clock[0] = datetime(2026, 7, 21, 0, 1, tzinfo=UTC)
        assert await usage_telemetry.upload_pending_standalone_daily_usage(config=config) == 1
        # A suspended buffer is discarded once this day could have reached
        # the server. It cannot reopen the day with an incompatible retry.
        if late_flush:
            await second.flush()
        assert second._pending == {}
        assert await usage_telemetry.upload_pending_standalone_daily_usage(config=config) == 0
        assert [payload["conversation_turns"] for payload in sent] == [1 if late_flush else 2]
        assert list(accepted.values()) == [1 if late_flush else 2]
    finally:
        await first.close()
        await second.close()


async def test_buffer_expiry_is_decided_after_acquiring_sqlite_writer_lock(
    buffered_usage, monkeypatch,
):
    config, clock = buffered_usage
    path = usage_telemetry.standalone_daily_usage_path(config)
    storage = await DailyUsageStore.open(path)
    runtime = usage_telemetry.StandaloneUsageTelemetry(config=config, legacy_storage=None)
    runtime._storage = storage
    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    attempted = threading.Event()
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(
            lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None
        )
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    await runtime.record_turn(run_kind="default", done_event=_turn())
    flush = asyncio.create_task(runtime.flush())
    try:
        assert await asyncio.to_thread(attempted.wait, 2)
        clock[0] = datetime(2026, 7, 21, 0, 1, 1, tzinfo=UTC)
        holder.rollback()
        await asyncio.wait_for(flush, timeout=2)
        assert runtime._pending == {}
        assert await storage.list_pending_daily_usage(before_day="9999-12-31") == []
    finally:
        holder.close()
        await asyncio.gather(flush, return_exceptions=True)
        await runtime.close()


async def test_upload_snapshot_waits_for_admitted_counter_transaction(
    buffered_usage, monkeypatch,
):
    config, clock = buffered_usage
    path = usage_telemetry.standalone_daily_usage_path(config)
    writer = await DailyUsageStore.open(path)
    reader = await DailyUsageStore.open(path)
    admitted = threading.Event()
    release = threading.Event()
    snapshot_requested = threading.Event()
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)

        def trace(sql):
            if sql.lstrip().startswith("INSERT INTO telemetry_daily_usage"):
                admitted.set()
                release.wait(5)
            elif sql == "BEGIN IMMEDIATE" and admitted.is_set():
                snapshot_requested.set()

        connection.set_trace_callback(trace)
        return connection

    sent = []

    async def post(_endpoint, payload):
        sent.append(payload)
        return True, None

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    monkeypatch.setattr(usage_telemetry, "_post_payload", post)
    monkeypatch.setattr(
        install_telemetry, "ensure_install_telemetry_id", lambda **_kw: "synthetic-device",
    )
    clock[0] = datetime(2026, 7, 21, 0, 0, 59, tzinfo=UTC)
    write = asyncio.create_task(writer.record_daily_usage_batch(
        (DailyUsageDelta("2026-07-20", 1, 10, 3, 4, 2, 1),), discard_expired=True,
    ))
    upload = None
    try:
        assert await asyncio.to_thread(admitted.wait, 2)
        clock[0] = datetime(2026, 7, 21, 0, 1, 1, tzinfo=UTC)
        upload = asyncio.create_task(
            usage_telemetry.upload_pending_daily_usage(reader, config=config),
        )
        assert await asyncio.to_thread(snapshot_requested.wait, 2)
        assert not upload.done()
        release.set()
        await write
        assert await upload == 1
        assert [payload["conversation_turns"] for payload in sent] == [1]
    finally:
        release.set()
        await asyncio.gather(
            write, *([upload] if upload is not None else []), return_exceptions=True,
        )
        await writer.close()
        await reader.close()


async def test_snapshot_cannot_become_final_after_its_read_lock_is_released(
    buffered_usage, monkeypatch,
):
    config, clock = buffered_usage
    first = usage_telemetry.StandaloneUsageTelemetry(config=config, legacy_storage=None)
    second = usage_telemetry.StandaloneUsageTelemetry(config=config, legacy_storage=None)
    reader = await DailyUsageStore.open(usage_telemetry.standalone_daily_usage_path(config))
    read = reader.list_pending_daily_usage
    advanced = False
    sent = []

    async def read_across_boundary(**kwargs):
        nonlocal advanced
        snapshot = await read(**kwargs)
        if not advanced:
            await second.flush()
            clock[0] = datetime(2026, 7, 21, 0, 1, 1, tzinfo=UTC)
            advanced = True
        return snapshot

    async def post(_endpoint, payload):
        sent.append(payload)
        return True, None

    monkeypatch.setattr(reader, "list_pending_daily_usage", read_across_boundary)
    monkeypatch.setattr(usage_telemetry, "_post_payload", post)
    monkeypatch.setattr(
        install_telemetry, "ensure_install_telemetry_id", lambda **_kw: "synthetic-device",
    )
    try:
        await first.record_turn(run_kind="default", done_event=_turn())
        await second.record_turn(run_kind="default", done_event=_turn())
        await first.flush()
        clock[0] = datetime(2026, 7, 21, 0, 0, 59, tzinfo=UTC)
        assert await usage_telemetry.upload_pending_daily_usage(reader, config=config) == 0
        assert sent == []
        assert await usage_telemetry.upload_pending_daily_usage(reader, config=config) == 1
        assert [payload["conversation_turns"] for payload in sent] == [2]
    finally:
        await first.close()
        await second.close()
        await reader.close()


async def test_passive_lifecycle_flushes_locally_without_uploading(buffered_usage, monkeypatch):
    config, _clock = buffered_usage
    calls = []

    async def upload(*_args, **_kwargs):
        calls.append("upload")

    monkeypatch.setattr(usage_telemetry, "_upload_pending_sources", upload)
    runtime = usage_telemetry.StandaloneUsageTelemetry(
        config=config, legacy_storage=None, upload_on_close=False,
    )
    await runtime.record_turn(run_kind="default", done_event=_turn())
    await runtime.close()
    assert calls == []
    restored = await DailyUsageStore.open(usage_telemetry.standalone_daily_usage_path(config))
    try:
        rows = await restored.list_pending_daily_usage(before_day="9999-12-31")
        assert [row["conversation_turns"] for row in rows] == [1]
    finally:
        await restored.close()
