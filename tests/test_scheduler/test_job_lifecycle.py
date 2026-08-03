"""Scheduler job lifecycle contracts for active and future runs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from opensquilla.scheduler.engine import SchedulerEngine
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import (
    CronJob,
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
