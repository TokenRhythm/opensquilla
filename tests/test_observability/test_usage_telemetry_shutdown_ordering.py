from __future__ import annotations

import asyncio
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from opensquilla.observability import daily_usage_store, install_telemetry, usage_telemetry
from opensquilla.observability.daily_usage_store import DailyUsageStore


@pytest.mark.parametrize(
    ("close_first", "fail_first_write"),
    [(False, False), (True, False), (False, True)],
    ids=["writer-cancelled-first", "close-cancelled-first", "failed-write-retried"],
)
@pytest.mark.parametrize(
    "wait_for_cleanup", [False, True], ids=["immediate-runner-exit", "cleanup-already-draining"],
)
def test_runner_shutdown_does_not_replay_or_lose_counter_snapshot(
    tmp_path, monkeypatch, close_first, fail_first_write, wait_for_cleanup,
):
    """Exercise asyncio.run's cancel-all shutdown with a real SQLite transaction."""
    config = SimpleNamespace(state_dir=str(tmp_path / "state"))
    now = datetime(2026, 7, 20, 12, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(_timezone):
            return now

    monkeypatch.setattr(usage_telemetry, "datetime", Clock)
    monkeypatch.setattr(daily_usage_store, "_utc_timestamp", lambda: now.timestamp())
    monkeypatch.setattr(install_telemetry, "_telemetry_skip_reason", lambda **_kwargs: None)
    monkeypatch.setattr(usage_telemetry, "STANDALONE_CLOSE_TIMEOUT_SECONDS", 0.01)
    path = usage_telemetry.standalone_daily_usage_path(config)
    transaction_started = threading.Event()
    release_transaction = threading.Event()
    cleanup_entered = asyncio.Event()
    attempts = []
    commits = []
    rollbacks = []
    loop_errors = []
    shutdown_tasks = []
    inflight_write = None
    original_connect = sqlite3.connect
    original_all_tasks = asyncio.tasks.all_tasks
    original_shield = asyncio.shield
    runtime = usage_telemetry.StandaloneUsageTelemetry(
        config=config, legacy_storage=None, upload_on_close=False,
        flush_interval_seconds=3600,
    )

    class BlockedCommitConnection(sqlite3.Connection):
        def __exit__(self, exc_type, exc_value, traceback):
            if exc_type is None and self.in_transaction:
                attempts.append(1)
                if len(attempts) == 1:
                    # The INSERT already ran; keep the real SQLite transaction
                    # uncommitted until the runner has cancelled all tasks.
                    transaction_started.set()
                    if not release_transaction.wait(5):
                        raise AssertionError("Runner did not release the SQLite transaction")
                    if fail_first_write:
                        error = sqlite3.OperationalError("Synthetic commit failure")
                        super().__exit__(type(error), error, error.__traceback__)
                        rollbacks.append(1)
                        raise error
                result = super().__exit__(exc_type, exc_value, traceback)
                commits.append(1)
                return result
            return super().__exit__(exc_type, exc_value, traceback)

    def blocked_connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=BlockedCommitConnection)

    def observe_cleanup_shield(target):
        if (
            asyncio.current_task() is runtime._close_task
            and target is runtime._flush_task
        ):
            cleanup_entered.set()
        return original_shield(target)

    def cancel_again_and_release():
        # A second cancellation arrives after the first cancellation has been
        # delivered, while the SQLite operation is still in its worker thread.
        task = inflight_write
        if task is not None and not task.done():
            task.cancel()
        release_transaction.set()

    def ordered_shutdown_tasks(loop=None):
        tasks = original_all_tasks(loop)
        # asyncio.run normally iterates a set in an unspecified order. Select
        # two legal orders so this regression cannot depend on object hashes.
        ordered = sorted(tasks, key=lambda task: (
            (task.get_name() != "daily-usage-telemetry-close")
            if close_first else (task.get_name() == "daily-usage-telemetry-close"),
            task.get_name(),
        ))
        shutdown_tasks.extend(ordered)
        loop.call_soon(lambda: loop.call_soon(cancel_again_and_release))
        return ordered

    async def run_until_close_deadline():
        nonlocal inflight_write
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
        runtime._storage = await DailyUsageStore.open(path)
        monkeypatch.setattr(sqlite3, "connect", blocked_connect)
        await runtime.record_turn(run_kind="default", done_event=SimpleNamespace(
            input_tokens=10, output_tokens=3, cached_tokens=4, cache_write_tokens=2,
        ))
        asyncio.create_task(runtime.flush(), name="shutdown-regression-flush")
        assert await asyncio.to_thread(transaction_started.wait, 2)
        inflight_write = runtime._flush_task
        await runtime.record_turn(run_kind="default", done_event=SimpleNamespace(
            input_tokens=7, output_tokens=11, cached_tokens=13, cache_write_tokens=17,
        ))
        before_close = loop.time()
        await runtime.close()
        assert loop.time() - before_close < 1
        assert not release_transaction.is_set()
        assert not commits
        if wait_for_cleanup:
            # Let the deadline cancellation reach finish_close's final drain
            # before the runner delivers a second cancellation to that cleanup.
            await asyncio.wait_for(cleanup_entered.wait(), timeout=1)
        # Returning triggers the real runner's global task cancellation.

    monkeypatch.setattr(asyncio, "shield", observe_cleanup_shield)
    monkeypatch.setattr(asyncio.tasks, "all_tasks", ordered_shutdown_tasks)
    try:
        asyncio.run(run_until_close_deadline())
    finally:
        release_transaction.set()

    with closing(original_connect(path)) as connection:
        rows = connection.execute(
            """
            SELECT conversation_turns, input_tokens, output_tokens,
                   cached_tokens, cache_write_tokens
            FROM telemetry_daily_usage
            """
        ).fetchall()
    assert rows == [(2, 17, 14, 17, 19)]
    assert len(attempts) == 2
    assert len(commits) == (1 if fail_first_write else 2)
    assert len(rollbacks) == int(fail_first_write)
    assert runtime._pending == {}
    assert runtime._storage is None
    assert shutdown_tasks and all(task.done() for task in shutdown_tasks)
    # Cancellation stays cancellation even when its SQLite transaction fails;
    # the final close flush safely retries the snapshot after rollback.
    assert loop_errors == []
