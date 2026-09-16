"""Cancellation cleanup must fence ownership at the database write boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import CronJob, JobReservation, JobStatus


@pytest.mark.parametrize("concurrent_edit", ["delete", "replace_owner", "pause", "edit"])
async def test_release_does_not_overwrite_concurrent_job_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent_edit: str
) -> None:
    db_path = str(tmp_path / "scheduler.db")
    store, writer = JobStore(db_path), JobStore(db_path)
    await store.open()
    await writer.open()
    cleanup: asyncio.Task[bool] | None = None
    cleanup_updated_at: datetime | None = None
    concurrent_updated_at: datetime | None = None
    try:
        job = CronJob(id="job", name="original", cron_expr="*/5 * * * *", handler_key="test")
        await store.save(job)
        reservation = await store.reserve_manual_job(job.id, datetime.now(UTC))
        assert isinstance(reservation, JobReservation)

        # The competing writer owns the SQLite write lock. Cleanup may read an
        # old snapshot, but its write cannot land until this writer commits.
        await writer._db().execute("BEGIN IMMEDIATE")
        write_started = asyncio.Event()
        execute = store._db().execute

        def observe_write(sql, params=()):
            nonlocal cleanup_updated_at
            statement = sql.lstrip()
            if statement.startswith("UPDATE") and "scheduler_jobs" in statement:
                cleanup_updated_at = datetime.fromisoformat(tuple(params)[2])
            if statement.startswith(("INSERT", "UPDATE")) and "scheduler_jobs" in statement:
                write_started.set()
            return execute(sql, params)

        monkeypatch.setattr(store._db(), "execute", observe_write)
        cleanup = asyncio.create_task(store.release_reservation(job.id, reservation.token))
        async with asyncio.timeout(2):
            await write_started.wait()

        if concurrent_edit == "delete":
            await writer.delete(job.id)
        else:
            changed = await writer.get(job.id)
            assert changed is not None
            assert cleanup_updated_at is not None
            changed.name = "edited while cleanup waited"
            changed.payload = {"message": "keep this new configuration"}
            changed.updated_at = cleanup_updated_at + timedelta(seconds=1)
            concurrent_updated_at = changed.updated_at
            if concurrent_edit == "replace_owner":
                changed.reservation_token = "new-owner"
                changed.reserved_by = "replacement-worker"
            elif concurrent_edit == "pause":
                changed.status = JobStatus.PAUSED
                changed.enabled = False
            await writer.save(changed)

        released = await asyncio.wait_for(cleanup, timeout=2)
        current = await writer.get(job.id)
        if concurrent_edit == "delete":
            assert current is None, "cleanup must not recreate a deleted job"
            assert released is False
        else:
            assert current is not None
            assert current.name == "edited while cleanup waited"
            assert current.payload == {"message": "keep this new configuration"}
            assert concurrent_updated_at is not None
            assert current.updated_at == concurrent_updated_at
            if concurrent_edit == "replace_owner":
                assert released is False
                assert current.reservation_token == "new-owner"
                assert current.reserved_by == "replacement-worker"
                assert current.status == JobStatus.RUNNING
            else:
                assert released is True
                assert current.reservation_token == ""
                assert current.reserved_at is None
                assert current.reserved_by == ""
                assert current.reservation_source == ""
                assert current.scheduled_run_at is None
                expected = JobStatus.PAUSED if concurrent_edit == "pause" else JobStatus.PENDING
                assert current.status == expected
                assert current.enabled is (concurrent_edit != "pause")
    finally:
        await writer._db().rollback()
        if cleanup is not None:
            await asyncio.gather(cleanup, return_exceptions=True)
        await writer.close()
        await store.close()
