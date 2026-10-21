from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.observability import daily_usage_store, install_telemetry, usage_telemetry
from opensquilla.observability.daily_usage_store import DailyUsageStore

_NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


@pytest.fixture
def shutdown_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    class Clock:
        @staticmethod
        def now(_timezone: object) -> datetime:
            return _NOW

    monkeypatch.setattr(usage_telemetry, "datetime", Clock)
    monkeypatch.setattr(daily_usage_store, "_utc_timestamp", lambda: _NOW.timestamp())
    monkeypatch.setattr(install_telemetry, "_telemetry_skip_reason", lambda **_kwargs: None)
    return SimpleNamespace(state_dir=str(tmp_path / "state"), disabled=False)


def _turn(input_tokens: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=input_tokens, output_tokens=3, cached_tokens=4, cache_write_tokens=2,
    )


class _SQLiteWriteBarrier:
    """Pause a real transaction while the event loop cancels all its tasks."""

    def __init__(
        self, *, fail_commit: bool = False, block_initialization: bool = False,
    ) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.waiter_cancelled = threading.Event()
        self.timed_out = threading.Event()
        self.commit_rejected = threading.Event()
        self._fail_commit = fail_commit
        self._block_initialization = block_initialization
        self._connect = sqlite3.connect

    def connect(self, *args: object, **kwargs: object) -> sqlite3.Connection:
        connection = self._connect(*args, **kwargs)
        writing_usage = False

        def trace(statement: str) -> None:
            nonlocal writing_usage
            statement = statement.lstrip()
            if statement.startswith("INSERT INTO telemetry_daily_usage"):
                writing_usage = True
            should_block = (
                statement.startswith("CREATE TABLE IF NOT EXISTS telemetry_daily_usage")
                if self._block_initialization
                else writing_usage
            )
            if should_block and not self.entered.is_set():
                self.entered.set()
                if not self.release.wait(10):
                    self.timed_out.set()

        def authorize(action: int, first: str | None, *_args: object) -> int:
            if (
                writing_usage
                and action == sqlite3.SQLITE_TRANSACTION
                and first == "COMMIT"
                and self._fail_commit
            ):
                self._fail_commit = False
                self.commit_rejected.set()
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_trace_callback(trace)
        connection.set_authorizer(authorize)
        return connection

    def release_after_cancellation(self) -> None:
        if not self.waiter_cancelled.wait(10):
            self.timed_out.set()
        self.release.set()

    def counters(self, path: Path) -> tuple[int, int, int, int, int]:
        with self._connect(path) as connection:
            row = connection.execute(
                "SELECT conversation_turns, input_tokens, output_tokens, "
                "cached_tokens, cache_write_tokens FROM telemetry_daily_usage"
            ).fetchone()
        return tuple(row) if row is not None else (0, 0, 0, 0, 0)


def _cancel_loop_during_write(
    config: SimpleNamespace,
    barrier: _SQLiteWriteBarrier,
    *,
    add_new_turn: bool = False,
    close_before_return: bool = False,
    preopen_storage: bool = True,
) -> usage_telemetry.StandaloneUsageTelemetry:
    async def main() -> usage_telemetry.StandaloneUsageTelemetry:
        runtime = usage_telemetry.StandaloneUsageTelemetry(
            config=config, legacy_storage=None, upload_on_close=False,
            flush_interval_seconds=3600,
        )
        if preopen_storage:
            runtime._storage = await DailyUsageStore.open(
                usage_telemetry.standalone_daily_usage_path(config),
            )
        await runtime.record_turn(run_kind="default", done_event=_turn())

        async def flush() -> None:
            try:
                await runtime.flush()
            except asyncio.CancelledError:
                barrier.waiter_cancelled.set()
                raise

        asyncio.create_task(flush(), name="synthetic-shutdown-flush")
        assert await asyncio.to_thread(barrier.entered.wait, 5)
        if add_new_turn:
            await runtime.record_turn(run_kind="default", done_event=_turn(input_tokens=20))
        if close_before_return:
            await runtime.close()
        # Returning deliberately invokes asyncio.run's global cancellation,
        # including the persist task and every Task wrapping an executor job.
        return runtime

    release = threading.Thread(target=barrier.release_after_cancellation, daemon=True)
    release.start()
    try:
        runtime = asyncio.run(main())
    finally:
        barrier.release.set()
        release.join(10)
    assert not release.is_alive()
    assert not barrier.timed_out.is_set()
    assert barrier.waiter_cancelled.is_set()
    return runtime


@pytest.mark.parametrize("add_new_turn", [False, True])
def test_loop_shutdown_does_not_replay_committed_counter_snapshot(
    shutdown_usage: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    add_new_turn: bool,
) -> None:
    barrier = _SQLiteWriteBarrier()
    monkeypatch.setattr(sqlite3, "connect", barrier.connect)
    path = usage_telemetry.standalone_daily_usage_path(shutdown_usage)

    runtime = _cancel_loop_during_write(
        shutdown_usage, barrier, add_new_turn=add_new_turn,
    )
    assert barrier.counters(path) == (1, 10, 3, 4, 2)
    try:
        # An in-memory retry must retain only additions after the committed
        # snapshot, even when the original coroutine ended by cancellation.
        asyncio.run(runtime.flush())
        expected = (2, 30, 6, 8, 4) if add_new_turn else (1, 10, 3, 4, 2)
        assert barrier.counters(path) == expected
    finally:
        if runtime._storage is not None:
            asyncio.run(runtime._storage.close())


def test_loop_shutdown_keeps_rejected_sqlite_commit_retryable(
    shutdown_usage: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _SQLiteWriteBarrier(fail_commit=True)
    monkeypatch.setattr(sqlite3, "connect", barrier.connect)
    path = usage_telemetry.standalone_daily_usage_path(shutdown_usage)

    runtime = _cancel_loop_during_write(shutdown_usage, barrier)
    assert barrier.commit_rejected.is_set()
    assert barrier.counters(path) == (0, 0, 0, 0, 0)
    try:
        asyncio.run(runtime.flush())
        assert barrier.counters(path) == (1, 10, 3, 4, 2)
    finally:
        if runtime._storage is not None:
            asyncio.run(runtime._storage.close())


def test_close_deadline_then_loop_shutdown_drains_newer_turn_once(
    shutdown_usage: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _SQLiteWriteBarrier()
    monkeypatch.setattr(sqlite3, "connect", barrier.connect)
    monkeypatch.setattr(usage_telemetry, "STANDALONE_CLOSE_TIMEOUT_SECONDS", 0.01)
    path = usage_telemetry.standalone_daily_usage_path(shutdown_usage)

    _cancel_loop_during_write(
        shutdown_usage, barrier, add_new_turn=True, close_before_return=True,
    )

    # No later loop or manual flush rescues counters here: the orderly close
    # must drain the newer accepted turn after its cancelled snapshot settles.
    assert barrier.counters(path) == (2, 30, 6, 8, 4)


def test_loop_shutdown_during_initialization_does_not_acknowledge_unwritten_turn(
    shutdown_usage: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = _SQLiteWriteBarrier(block_initialization=True)
    monkeypatch.setattr(sqlite3, "connect", barrier.connect)
    path = usage_telemetry.standalone_daily_usage_path(shutdown_usage)

    runtime = _cancel_loop_during_write(
        shutdown_usage, barrier, preopen_storage=False,
    )
    try:
        assert barrier.counters(path) == (0, 0, 0, 0, 0)
        asyncio.run(runtime.flush())
        assert barrier.counters(path) == (1, 10, 3, 4, 2)
    finally:
        if runtime._storage is not None:
            asyncio.run(runtime._storage.close())
