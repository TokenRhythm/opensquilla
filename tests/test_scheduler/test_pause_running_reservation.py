from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from opensquilla.scheduler.engine import SchedulerEngine
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import (
    CronJob,
    JobStatus,
    ManualRunStatus,
    ScheduleKind,
    SessionTarget,
)


async def test_pause_running_job_clears_reservation_and_allows_resume(tmp_path) -> None:
    store = JobStore(str(tmp_path / "cron.db"))
    await store.open()
    engine = SchedulerEngine(store)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_job: CronJob) -> str:
        started.set()
        await release.wait()
        return "ok"

    engine.register_handler("agent_run", handler)
    job = CronJob(
        name="workspace audit",
        cron_expr="60",
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "audit", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.EVERY,
        next_run_at=datetime.now(UTC) - timedelta(seconds=1),
        status=JobStatus.PENDING,
    )
    await store.save(job)

    try:
        await engine._timer._tick()
        await asyncio.wait_for(started.wait(), timeout=1)
        running = await store.get(job.id)
        assert running is not None
        assert running.reservation_token

        paused = await engine.pause_job(job.id)
        assert paused is not None
        assert paused.status == JobStatus.PAUSED
        assert paused.reservation_token == ""

        resumed = await engine.resume_job(job.id)
        assert resumed is not None
        assert resumed.status == JobStatus.PENDING
        assert resumed.reservation_token == ""

        release.set()
        result = await engine.run_job_now(job.id)
        assert result.status == ManualRunStatus.ACCEPTED
        assert result.success is True
    finally:
        release.set()
        await engine.stop()
        await store.close()
