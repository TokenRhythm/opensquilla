"""Scheduler edits must not overwrite live ownership or recreate deleted jobs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opensquilla.scheduler.engine import SchedulerEngine
from opensquilla.scheduler.jobs import apply_reserved_result
from opensquilla.scheduler.ops import SchedulerOps
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import (
    CronJob,
    JobExecution,
    JobReservation,
    JobReservationRejected,
    JobStatus,
    ManualRunStatus,
    ReservationRejectionReason,
    ScheduleKind,
)
from tests.test_scheduler._async_helpers import drain_tasks, wait_task


def _job() -> CronJob:
    return CronJob(
        name="original", handler_key="agent_run", cron_expr="60",
        schedule_kind=ScheduleKind.EVERY,
        next_run_at=datetime.now(UTC) + timedelta(minutes=1),
        payload={"kind": "agent_turn", "task": "original task", "agent_id": "main"},
    )


async def test_stale_engine_edit_does_not_admit_a_second_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with JobStore(str(tmp_path / "scheduler.db")) as store:
        engine = SchedulerEngine(store)
        job = _job()
        await store.save(job)
        read_ready, release_edit = asyncio.Event(), asyncio.Event()
        started, finish = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handler(_job: CronJob) -> str:
            nonlocal calls
            calls += 1
            started.set()
            if calls == 1:
                await finish.wait()
            return "done"

        original_get = store.get

        async def gated_get(job_id: str) -> CronJob | None:
            value = await original_get(job_id)
            if asyncio.current_task().get_name() == "stale-editor" and not read_ready.is_set():
                read_ready.set()
                await release_edit.wait()
            return value

        monkeypatch.setattr(store, "get", gated_get)
        engine.register_handler("agent_run", handler)
        edit = asyncio.create_task(engine.update_job(job.id, name="renamed"), name="stale-editor")
        active: asyncio.Task | None = None
        try:
            await asyncio.wait_for(read_ready.wait(), timeout=2)
            active = asyncio.create_task(engine.run_job_now(job.id))
            await asyncio.wait_for(started.wait(), timeout=2)
            reserved = await original_get(job.id)
            assert reserved is not None and reserved.reservation_token
            release_edit.set()
            await wait_task(edit)
            duplicate = await asyncio.wait_for(engine.run_job_now(job.id), timeout=2)
            assert duplicate.status == ManualRunStatus.BUSY
            assert calls == 1
            current = await original_get(job.id)
            assert current is not None
            assert current.name == "renamed"
            assert current.reservation_token == reserved.reservation_token
            finish.set()
            result = await wait_task(active)
            assert result.success
            current = await original_get(job.id)
            assert current is not None and current.run_count == 1
            assert len(await store.list_executions(job.id)) == 1
        finally:
            release_edit.set()
            finish.set()
            await drain_tasks(
                asyncio.create_task(engine.stop()), edit, *(task for task in [active] if task),
            )


@pytest.mark.parametrize("operation", ["update", "pause", "resume"])
@pytest.mark.parametrize("concurrent", ["reserve", "finalize", "release", "delete", "edit"])
async def test_edit_reloads_after_another_connection_changes_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, concurrent: str,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as writer:
        job = _job()
        await store.save(job)
        reservation: JobReservation | None = None
        if concurrent in {"finalize", "release"}:
            reserved = await writer.reserve_manual_job(job.id, datetime.now(UTC))
            assert isinstance(reserved, JobReservation)
            reservation = reserved
            # Finalize/release use MAX(updated_at, now), which can leave this
            # fence unchanged. The ownership token must detect the transition.
            await SchedulerOps(
                writer, clock=lambda: datetime.now(UTC) + timedelta(days=1),
            ).update(job.id, name=job.name)
        await writer._db().execute("BEGIN IMMEDIATE")
        write_started = asyncio.Event()
        original_execute = store._db().execute

        def observe_write(sql, params=()):
            if sql.lstrip().startswith(("INSERT", "UPDATE")) and "scheduler_jobs" in sql:
                write_started.set()
            return original_execute(sql, params)

        monkeypatch.setattr(store._db(), "execute", observe_write)
        ops = SchedulerOps(store)
        edit_call = (
            ops.update(job.id, name="editor name", payload={"task": "editor task"})
            if operation == "update" else getattr(ops, operation)(job.id)
        )
        edit = asyncio.create_task(edit_call)
        try:
            await asyncio.wait_for(write_started.wait(), timeout=2)
            if concurrent == "reserve":
                reserved = await writer.reserve_manual_job(job.id, datetime.now(UTC))
                assert isinstance(reserved, JobReservation)
                reservation = reserved
            elif concurrent == "finalize":
                assert reservation is not None
                assert await apply_reserved_result(
                    job.id, reservation.token, JobExecution(job_id=job.id, success=True), writer,
                )
            elif concurrent == "release":
                assert reservation is not None
                assert await writer.release_reservation(job.id, reservation.token)
            elif concurrent == "delete":
                await writer.delete(job.id)
            else:
                await SchedulerOps(writer).update(
                    job.id, timeout_seconds=123, payload={"agent_id": "worker"},
                )
            edited = await wait_task(edit)
            current = await writer.get(job.id)
            if concurrent == "delete":
                assert edited is None
                assert current is None, "an edit must never recreate a deleted job"
                return
            assert current is not None and edited is not None
            if operation == "update":
                assert current.name == "editor name"
                assert current.payload["task"] == "editor task", "CAS retry must reapply the patch"
            if concurrent == "reserve":
                assert reservation is not None
                assert current.reservation_token == reservation.token
                assert current.reserved_at == reservation.job.reserved_at
                assert current.scheduled_run_at == reservation.job.scheduled_run_at
                duplicate = await writer.reserve_manual_job(job.id, datetime.now(UTC))
                assert isinstance(duplicate, JobReservationRejected)
                assert duplicate.reason == ReservationRejectionReason.BUSY
            else:
                assert current.reservation_token == ""
            assert current.run_count == int(concurrent == "finalize")
            assert current.error_count == 0
            if concurrent in {"finalize", "release"}:
                assert reservation is not None
                assert current.last_run_at == reservation.job.last_run_at
            if concurrent == "edit":
                assert current.timeout_seconds == 123
                assert current.payload["agent_id"] == "worker"
            expected_status = (
                JobStatus.PAUSED if operation == "pause" else
                JobStatus.RUNNING if operation == "update" and concurrent == "reserve" else
                JobStatus.PENDING
            )
            assert current.status == expected_status
        finally:
            await writer._db().rollback()
            await drain_tasks(edit)


async def test_edits_advance_the_fence_with_a_fixed_or_backwards_clock() -> None:
    now = datetime.now(UTC)
    async with JobStore(":memory:") as store:
        job = _job()
        job.updated_at = now
        await store.save(job)
        ops = SchedulerOps(store, clock=lambda: now - timedelta(seconds=1))
        first = await ops.update(job.id, name="first")
        second = await ops.update(job.id, name="second")
        assert first is not None and second is not None
        assert job.updated_at < first.updated_at < second.updated_at


async def test_edit_reloads_after_a_complete_run_with_the_same_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as writer:
        job = _job()
        job.schedule_kind = ScheduleKind.AT
        job.cron_expr = now.isoformat()
        job.next_run_at = now
        job.updated_at = now
        job.delete_after_run = False
        await store.save(job)
        read, release = asyncio.Event(), asyncio.Event()
        original_get = store.get

        async def gated_get(job_id: str) -> CronJob | None:
            result = await original_get(job_id)
            if not read.is_set():
                read.set()
                await release.wait()
            return result

        monkeypatch.setattr(store, "get", gated_get)
        replacement = now + timedelta(hours=1)
        edit = asyncio.create_task(SchedulerOps(store, clock=lambda: now).update(
            job.id, schedule_kind=ScheduleKind.AT,
            schedule_value=replacement.isoformat(), enabled=True,
        ))
        try:
            await asyncio.wait_for(read.wait(), timeout=2)
            reservation = await writer.reserve_manual_job(job.id, now)
            assert isinstance(reservation, JobReservation)
            completed = reservation.job
            completed.status = JobStatus.DISABLED
            completed.enabled = False
            completed.run_count += 1
            completed.next_run_at = None
            completed.updated_at = now
            assert await writer.finalize_reserved_job(completed, reservation.token, now)
            completed = await writer.get(job.id)
            assert completed is not None
            assert completed.updated_at == job.updated_at
            assert completed.reservation_token == job.reservation_token == ""
            release.set()
            await wait_task(edit)
            current = await writer.get(job.id)
            assert current is not None
            assert current.enabled is True
            assert current.status == JobStatus.PENDING
            assert current.run_count == 1
            assert current.next_run_at == replacement
        finally:
            release.set()
            await drain_tasks(edit)
