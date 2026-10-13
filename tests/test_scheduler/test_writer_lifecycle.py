"""Shared scheduler writes keep transaction ownership through cancellation/failure."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from opensquilla.compat import aiosqlite
from opensquilla.scheduler.ops import SchedulerOps
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import CronJob
from tests.test_scheduler._async_helpers import drain_tasks, wait_task


def _job(name: str = "original") -> CronJob:
    return CronJob(name=name, cron_expr="*/5 * * * *", handler_key="test")


async def _reject_edits(store: JobStore) -> None:
    await store._db().execute(
        """CREATE TRIGGER reject_edit BEFORE UPDATE ON scheduler_jobs
           WHEN NEW.name = 'rejected'
           BEGIN SELECT RAISE(ABORT, 'edit rejected'); END""",
    )
    await store._db().commit()


async def test_cancelling_a_queued_editor_never_starts_its_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with JobStore(":memory:") as store:
        job = _job()
        await store.save(job)
        read = asyncio.Event()
        original_get = store.get

        async def observed_get(job_id: str) -> CronJob | None:
            value = await original_get(job_id)
            read.set()
            return value

        monkeypatch.setattr(store, "get", observed_get)
        async with store.transaction():
            queued = asyncio.create_task(SchedulerOps(store).update(job.id, name="cancelled"))
            await asyncio.wait_for(read.wait(), timeout=2)
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await wait_task(queued)
        # Passing another write through the same owner drains any queued work.
        await store.save(_job("next writer"))
        current = await original_get(job.id)
        assert current is not None and current.name == "original"
        assert not store._db().in_transaction


@pytest.mark.parametrize("phase", ["statement", "commit"])
async def test_repeated_cancellation_drains_an_admitted_write_before_the_next_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as writer:
        job = _job()
        await store.save(job)
        started, release = asyncio.Event(), asyncio.Event()
        original_commit = store._db().commit
        if phase == "statement":
            await writer._db().execute("BEGIN IMMEDIATE")
            original_execute = store._db().execute

            def observed_execute(sql, params=()):
                if sql.lstrip().startswith("UPDATE scheduler_jobs"):
                    started.set()
                return original_execute(sql, params)

            monkeypatch.setattr(store._db(), "execute", observed_execute)
        else:
            async def gated_commit() -> None:
                started.set()
                await release.wait()
                await original_commit()

            monkeypatch.setattr(store._db(), "commit", gated_commit)

        edit = asyncio.create_task(SchedulerOps(store).update(job.id, name="admitted"))
        next_write: asyncio.Task | None = None
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            edit.cancel()
            await asyncio.sleep(0)
            edit.cancel()
            await asyncio.sleep(0)
            assert not edit.done(), "cancellation must not abandon admitted SQLite work"
            other = _job("independent writer")
            next_write = asyncio.create_task(store.save(other))
            await asyncio.sleep(0)
            assert not next_write.done()
            await writer._db().rollback()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await wait_task(edit)
            await wait_task(next_write)
            assert not store._db().in_transaction
            current = await writer.get(job.id)
            assert current is not None and current.name == "admitted"
            assert await writer.get(other.id) is not None
        finally:
            release.set()
            await writer._db().rollback()
            await drain_tasks(edit, *(t for t in [next_write] if t))


async def test_failed_edit_rolls_back_before_allowing_another_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as observer:
        job = _job()
        await store.save(job)
        await _reject_edits(store)
        rollback_started, finish_rollback = asyncio.Event(), asyncio.Event()
        original_rollback = store._db().rollback

        async def gated_rollback() -> None:
            rollback_started.set()
            await finish_rollback.wait()
            await original_rollback()

        monkeypatch.setattr(store._db(), "rollback", gated_rollback)
        edit = asyncio.create_task(SchedulerOps(store).update(job.id, name="rejected"))
        other = _job("later writer")
        next_write: asyncio.Task | None = None
        try:
            await asyncio.wait_for(rollback_started.wait(), timeout=2)
            next_write = asyncio.create_task(store.save(other))
            await asyncio.sleep(0)
            assert not next_write.done()
            finish_rollback.set()
            with pytest.raises(aiosqlite.IntegrityError, match="edit rejected"):
                await wait_task(edit)
            await wait_task(next_write)
            assert not store._db().in_transaction
            current = await observer.get(job.id)
            assert current is not None and current.name == "original"
            assert await observer.get(other.id) is not None
        finally:
            finish_rollback.set()
            await drain_tasks(edit, *(t for t in [next_write] if t))


@pytest.mark.parametrize("failure", ["raises", "ineffective"])
async def test_failed_rollback_retires_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as observer:
        job = _job()
        await store.save(job)
        await _reject_edits(store)

        async def broken_rollback() -> None:
            if failure == "raises":
                raise aiosqlite.OperationalError("rollback failed")

        monkeypatch.setattr(store._db(), "rollback", broken_rollback)
        with pytest.raises((aiosqlite.OperationalError, RuntimeError), match="rollback"):
            await SchedulerOps(store).update(job.id, name="rejected")
        assert store._conn is None
        with pytest.raises(RuntimeError, match="not opened"):
            await store.save(_job("must not commit poisoned state"))
        current = await observer.get(job.id)
        assert current is not None and current.name == "original"
        await asyncio.wait_for(observer.save(_job("writer still usable")), timeout=2)


@pytest.mark.parametrize("failure", ["raises", "ineffective"])
async def test_commit_failure_rolls_back_and_releases_the_writer(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    async with JobStore(":memory:") as store:
        job = _job()
        await store.save(job)
        commit = store._db().commit

        async def fail_commit() -> None:
            if failure == "raises":
                raise aiosqlite.OperationalError("commit failed")

        monkeypatch.setattr(store._db(), "commit", fail_commit)
        with pytest.raises((aiosqlite.OperationalError, RuntimeError), match="commit"):
            await SchedulerOps(store).update(job.id, name="not committed")
        assert not store._db().in_transaction
        current = await store.get(job.id)
        assert current is not None and current.name == "original"
        monkeypatch.setattr(store._db(), "commit", commit)
        await asyncio.wait_for(store.save(_job("next writer")), timeout=2)


async def test_nested_batch_writes_commit_once_and_roll_back_together(tmp_path: Path) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as observer:
        first, second = _job("first"), _job("second")
        async with store.transaction():
            await store.save_no_commit(first)
            async with store.transaction():
                await store.save(second)
            assert await observer.get(first.id) is None
            assert await observer.get(second.id) is None
        assert await observer.get(first.id) is not None
        assert await observer.get(second.id) is not None
        discarded = _job("discarded")
        with pytest.raises(ValueError, match="invalid batch"):
            async with store.transaction():
                await store.save_no_commit(discarded)
                raise ValueError("invalid batch")
        assert not store._db().in_transaction
        assert await observer.get(discarded.id) is None


async def test_catching_a_failed_nested_write_cannot_commit_a_partial_batch(tmp_path: Path) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as observer:
        existing = _job()
        await store.save(existing)
        await _reject_edits(store)
        first, last = _job("first"), _job("last")
        with pytest.raises(RuntimeError, match="failed write"):
            async with store.transaction():
                await store.save_no_commit(first)
                with pytest.raises(aiosqlite.IntegrityError):
                    await SchedulerOps(store).update(existing.id, name="rejected")
                await store.save(last)
        assert not store._db().in_transaction
        assert await observer.get(first.id) is None
        assert await observer.get(last.id) is None
        current = await observer.get(existing.id)
        assert current is not None and current.name == "original"


async def test_save_no_commit_requires_its_documented_transaction_owner() -> None:
    async with JobStore(":memory:") as store:
        job = _job()
        with pytest.raises(RuntimeError, match=r"requires transaction\(\)"):
            await store.save_no_commit(job)
        assert await store.get(job.id) is None
        assert not store._db().in_transaction


async def test_nested_create_failure_does_not_roll_back_and_resume_the_batch(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as observer:
        await store._db().execute(
            """CREATE TRIGGER reject_create BEFORE INSERT ON scheduler_jobs
               WHEN NEW.name = 'rejected'
               BEGIN SELECT RAISE(ABORT, 'create rejected'); END""",
        )
        await store._db().commit()
        first, rejected, last = _job("first"), _job("rejected"), _job("last")
        rejected.idempotency_key = "unique-rejected"
        with pytest.raises(RuntimeError, match="failed write"):
            async with store.transaction():
                await store.save(first)
                with pytest.raises(aiosqlite.IntegrityError, match="create rejected"):
                    await store.create_or_get(rejected)
                await store.save(last)
        assert not store._db().in_transaction
        assert await observer.list_active() == []


async def test_cancelled_explicit_transaction_drains_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as observer:
        saved, rollback_started, finish_rollback = asyncio.Event(), asyncio.Event(), asyncio.Event()
        rollback = store._db().rollback
        job = _job()

        async def gated_rollback() -> None:
            rollback_started.set()
            await finish_rollback.wait()
            await rollback()

        async def batch() -> None:
            async with store.transaction():
                await store.save_no_commit(job)
                saved.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(store._db(), "rollback", gated_rollback)
        pending = asyncio.create_task(batch())
        try:
            await asyncio.wait_for(saved.wait(), timeout=2)
            pending.cancel()
            await asyncio.wait_for(rollback_started.wait(), timeout=2)
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            finish_rollback.set()
            with pytest.raises(asyncio.CancelledError):
                await wait_task(pending)
            assert not store._db().in_transaction
            assert await observer.get(job.id) is None
            await asyncio.wait_for(store.save(_job("next writer")), timeout=2)
        finally:
            finish_rollback.set()
            await drain_tasks(pending)


async def test_shutdown_cancels_owner_and_rollback_child_without_abandoning_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as observer:
        job = _job()
        await store.save(job)
        await _reject_edits(store)
        rollback_started, release_rollback = asyncio.Event(), asyncio.Event()
        rollback_tasks: list[asyncio.Task] = []
        rollback = store._db().rollback

        async def controlled_rollback() -> None:
            rollback_tasks.append(asyncio.current_task())
            rollback_started.set()
            await release_rollback.wait()
            await rollback()

        monkeypatch.setattr(store._db(), "rollback", controlled_rollback)
        edit = asyncio.create_task(SchedulerOps(store).update(job.id, name="rejected"))
        try:
            await asyncio.wait_for(rollback_started.wait(), timeout=2)
            assert store._write_owner is not None
            # Model all_tasks shutdown: both owner and already existing cleanup
            # tasks can be cancelled, not merely the public RPC's awaiter.
            store._write_owner.cancel()
            rollback_tasks[0].cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not edit.done()
            release_rollback.set()
            with pytest.raises(asyncio.CancelledError):
                await wait_task(edit)
            assert len(rollback_tasks) >= 2
            assert not store._db().in_transaction
            current = await observer.get(job.id)
            assert current is not None and current.name == "original"
            await asyncio.wait_for(store.save(_job("next writer")), timeout=2)
        finally:
            release_rollback.set()
            await drain_tasks(edit)


async def test_cancelled_owner_rolls_back_a_queued_physical_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as writer:
        job = _job()
        await store.save(job)
        await writer._db().execute("BEGIN IMMEDIATE")
        started = asyncio.Event()
        execute = store._db().execute

        def observed_execute(sql, params=()):
            if sql.lstrip().startswith("UPDATE scheduler_jobs"):
                started.set()
            return execute(sql, params)

        monkeypatch.setattr(store._db(), "execute", observed_execute)
        edit = asyncio.create_task(SchedulerOps(store).update(job.id, name="cancelled owner"))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert store._write_owner is not None
            store._write_owner.cancel()
            await asyncio.sleep(0)
            await writer._db().rollback()
            with pytest.raises(asyncio.CancelledError):
                await wait_task(edit)
            assert not store._db().in_transaction
            current = await writer.get(job.id)
            assert current is not None and current.name == "original"
            await asyncio.wait_for(store.save(_job("next writer")), timeout=2)
        finally:
            await writer._db().rollback()
            await drain_tasks(edit)


async def test_close_waits_for_the_current_writer_without_early_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as observer:
        job = _job()
        await store.save(job)
        commit_started, release_commit = asyncio.Event(), asyncio.Event()
        commit = store._db().commit

        async def gated_commit() -> None:
            commit_started.set()
            await release_commit.wait()
            await commit()

        monkeypatch.setattr(store._db(), "commit", gated_commit)
        edit = asyncio.create_task(SchedulerOps(store).update(job.id, name="saved before close"))
        closing: asyncio.Task | None = None
        try:
            await asyncio.wait_for(commit_started.wait(), timeout=2)
            closing = asyncio.create_task(store.close())
            await asyncio.sleep(0)
            assert not closing.done()
            current = await observer.get(job.id)
            assert current is not None and current.name == "original"
            release_commit.set()
            await wait_task(edit)
            await wait_task(closing)
            assert store._conn is None
            current = await observer.get(job.id)
            assert current is not None and current.name == "saved before close"
        finally:
            release_commit.set()
            await drain_tasks(edit, *(t for t in [closing] if t))


async def test_repeated_caller_cancellation_waits_for_close_to_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(":memory:")
    await store.open()
    close_started, release_close = asyncio.Event(), asyncio.Event()
    close = store._db().close

    async def gated_close() -> None:
        close_started.set()
        await release_close.wait()
        await close()

    monkeypatch.setattr(store._db(), "close", gated_close)
    closing = asyncio.create_task(store.close())
    try:
        await asyncio.wait_for(close_started.wait(), timeout=2)
        assert store._conn is None
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        assert store._write_lock.locked()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await wait_task(closing)
        assert not store._write_lock.locked()
        with pytest.raises(RuntimeError, match="not opened"):
            await store.save(_job())
    finally:
        release_close.set()
        await drain_tasks(closing)
