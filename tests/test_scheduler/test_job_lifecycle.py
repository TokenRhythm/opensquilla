"""Scheduler job lifecycle contracts for active and future runs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opensquilla.scheduler import jobs
from opensquilla.scheduler.engine import SchedulerEngine
from opensquilla.scheduler.jobs import apply_reserved_result
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import (
    CronJob,
    JobExecution,
    JobReservation,
    JobStatus,
    ManualRunStatus,
    ScheduleKind,
    SessionTarget,
)


def _due_job() -> CronJob:
    return CronJob(
        name="workspace audit",
        cron_expr="60",
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "audit", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.EVERY,
        next_run_at=datetime.now(UTC) - timedelta(seconds=1),
        status=JobStatus.PENDING,
    )


@pytest.mark.parametrize(
    "outcome", ["success", "missing_handler", "delete_after_run", "schedule_error"],
)
@pytest.mark.parametrize("edit", ["delete", "new_owner", "pause", "reschedule"])
async def test_finalization_preserves_concurrent_job_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, edit: str,
) -> None:
    db_path = str(tmp_path / "scheduler.db")
    async with JobStore(db_path) as store, JobStore(db_path) as writer:
        job = _due_job()
        if outcome == "delete_after_run":
            job.schedule_kind = ScheduleKind.AT
            job.delete_after_run = True
        elif outcome == "schedule_error":
            job.cron_expr = "invalid"
        notifications: list[str] = []
        monkeypatch.setattr(
            jobs, "_schedule_failure_notifier", lambda _job, error: notifications.append(error),
        )
        await store.save(job)
        reservation = await store.reserve_manual_job(job.id, datetime.now(UTC))
        assert isinstance(reservation, JobReservation)

        await writer._db().execute("BEGIN IMMEDIATE")
        write_started = asyncio.Event()
        execute = store._db().execute

        def observe_write(sql, params=()):
            if sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) and "scheduler_jobs" in sql:
                write_started.set()
            return execute(sql, params)

        monkeypatch.setattr(store._db(), "execute", observe_write)
        if outcome == "missing_handler":
            operation = store.finalize_reserved_missing_handler(
                job.id, reservation.token, "missing",
            )
        else:
            operation = apply_reserved_result(
                job.id, reservation.token, JobExecution(job_id=job.id, success=True), store,
            )
        completion = asyncio.create_task(operation)
        try:
            async with asyncio.timeout(5):
                await write_started.wait()
            changed = await writer.get(job.id)
            assert changed is not None
            if edit == "delete":
                await writer.delete(job.id)
            else:
                changed.name = "updated by user"
                changed.payload["task"] = "keep the new task"
                changed.updated_at += timedelta(seconds=1)
                if edit == "new_owner":
                    changed.reservation_token = "replacement-owner"
                elif edit == "pause":
                    changed.status = JobStatus.PAUSED
                    changed.enabled = False
                else:
                    changed.schedule_kind = ScheduleKind.EVERY
                    changed.cron_expr = "3600"
                    changed.delete_after_run = False
                await writer.save(changed)

            applied = await asyncio.wait_for(completion, timeout=5)
            assert notifications == [], "a superseded schedule must not emit a failure notification"
            current = await writer.get(job.id)
            if edit == "delete":
                assert current is None, "completion must not recreate a deleted job"
                assert applied is False
                return
            assert current is not None, "completion must not delete a rescheduled job"
            assert current.name == changed.name
            assert current.payload == changed.payload
            if edit == "new_owner":
                assert applied is False
                assert current.reservation_token == "replacement-owner"
                assert current.run_count == 0
                assert current.error_count == 0
            else:
                assert applied is True
                assert current.reservation_token == ""
                if edit == "pause":
                    assert current.status == JobStatus.PAUSED
                    assert current.enabled is False
                    assert current.run_count == 0
                    assert current.error_count == 0
                elif outcome == "missing_handler":
                    assert current.status == JobStatus.FAILED
                    assert current.error_count == 1
                else:
                    assert current.status == JobStatus.PENDING
                    assert current.run_count == 1
                    assert current.next_run_at is not None
                    assert current.next_run_at > datetime.now(UTC) + timedelta(minutes=59)
        finally:
            await writer._db().rollback()
            await asyncio.gather(completion, return_exceptions=True)


@pytest.mark.asyncio
async def test_startup_clears_stale_reservations_without_changing_lifecycle_state() -> None:
    now = datetime.now(UTC)
    async with JobStore(":memory:") as store:
        paused = _due_job()
        paused.status = JobStatus.PAUSED
        paused.next_run_at = now + timedelta(hours=1)
        paused.reservation_token = "stale-paused"
        paused.reserved_at = now - timedelta(minutes=5)
        pending = _due_job()
        pending.status = JobStatus.PENDING
        pending.next_run_at = now + timedelta(hours=1)
        pending.reservation_token = "stale-pending"
        pending.reserved_at = now - timedelta(minutes=5)
        await store.save(paused)
        await store.save(pending)

        engine = SchedulerEngine(store)
        await engine._timer.startup_catchup()

        recovered_paused = await store.get(paused.id)
        assert recovered_paused is not None
        assert recovered_paused.status == JobStatus.PAUSED
        assert recovered_paused.reservation_token == ""

        recovered_pending = await store.get(pending.id)
        assert recovered_pending is not None
        assert recovered_pending.status == JobStatus.PENDING
        assert recovered_pending.reservation_token == ""


@pytest.mark.asyncio
async def test_pause_and_resume_keep_active_run_reserved_until_completion() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(_job: CronJob) -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "ok"

    async with JobStore(":memory:") as store:
        engine = SchedulerEngine(store)
        engine.register_handler("agent_run", handler)
        job = _due_job()
        await store.save(job)

        try:
            await engine._timer._tick()
            running_task = engine._timer._running[job.id]
            await asyncio.wait_for(started.wait(), timeout=1)

            running = await store.get(job.id)
            assert running is not None
            reservation_token = running.reservation_token
            assert reservation_token

            paused = await engine.pause_job(job.id)
            assert paused is not None
            assert paused.status == JobStatus.PAUSED
            assert paused.reservation_token == reservation_token
            assert not running_task.done()

            resumed = await engine.resume_job(job.id)
            assert resumed is not None
            assert resumed.status == JobStatus.PENDING
            assert resumed.reservation_token == reservation_token

            duplicate = await engine.run_job_now(job.id)
            assert duplicate.status == ManualRunStatus.BUSY
            assert calls == 1

            release.set()
            await asyncio.wait_for(running_task, timeout=1)

            completed = await store.get(job.id)
            assert completed is not None
            assert completed.status == JobStatus.PENDING
            assert completed.reservation_token == ""

            rerun = await engine.run_job_now(job.id)
            assert rerun.status == ManualRunStatus.ACCEPTED
            assert rerun.success is True
            assert calls == 2
        finally:
            release.set()
            await engine.stop()


@pytest.mark.asyncio
async def test_pause_does_not_interrupt_active_manual_run() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_job: CronJob) -> str:
        started.set()
        await release.wait()
        return "ok"

    async with JobStore(":memory:") as store:
        engine = SchedulerEngine(store)
        engine.register_handler("agent_run", handler)
        job = _due_job()
        job.next_run_at = datetime.now(UTC) + timedelta(hours=1)
        await store.save(job)

        manual_task = asyncio.create_task(engine.run_job_now(job.id))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            running = await store.get(job.id)
            assert running is not None
            reservation_token = running.reservation_token
            assert reservation_token

            paused = await engine.pause_job(job.id)
            assert paused is not None
            assert paused.status == JobStatus.PAUSED
            assert paused.reservation_token == reservation_token
            assert not manual_task.done()

            release.set()
            result = await asyncio.wait_for(manual_task, timeout=1)
            assert result.status == ManualRunStatus.ACCEPTED
            assert result.success is True

            completed = await store.get(job.id)
            assert completed is not None
            assert completed.status == JobStatus.PAUSED
            assert completed.reservation_token == ""
        finally:
            release.set()
            await asyncio.gather(manual_task, return_exceptions=True)
            await engine.stop()


@pytest.mark.asyncio
async def test_delete_stops_future_runs_but_allows_active_run_to_finish() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_job: CronJob) -> str:
        started.set()
        await release.wait()
        return "ok"

    async with JobStore(":memory:") as store:
        engine = SchedulerEngine(store)
        engine.register_handler("agent_run", handler)
        job = _due_job()
        await store.save(job)

        try:
            await engine._timer._tick()
            running_task = engine._timer._running[job.id]
            await asyncio.wait_for(started.wait(), timeout=1)

            assert await engine.delete_job(job.id) is True
            assert await store.get(job.id) is None
            assert engine._timer._running[job.id] is running_task
            assert not running_task.done()

            rejected = await engine.run_job_now(job.id)
            assert rejected.status == ManualRunStatus.NOT_FOUND

            release.set()
            await asyncio.wait_for(running_task, timeout=1)
            runs = await store.list_executions(job.id, limit=10)
            assert len(runs) == 1
            assert runs[0].success is True

            await engine._timer._tick()
            assert job.id not in engine._timer._running
        finally:
            release.set()
            await engine.stop()
