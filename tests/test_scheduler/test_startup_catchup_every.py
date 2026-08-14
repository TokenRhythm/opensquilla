"""Startup catchup must fast-forward EVERY-interval jobs as well as CRON jobs.

EVERY jobs persist `cron_expr` as a stringified seconds integer (e.g. "60"),
not a 5-field cron expression. The pre-fix overflow branch parsed it as a
cron expression unconditionally, raised, swallowed the error, and left
`next_run_at` stale — which made overdue EVERY jobs reappear on the very next
tick and fire in a burst.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.timer import SchedulerTimer
from opensquilla.scheduler.types import CronJob, JobStatus, ScheduleKind, SessionTarget


def _every_job(
    job_id: str,
    interval_seconds: int,
    anchor_at: datetime,
    next_run_at: datetime,
) -> CronJob:
    return CronJob(
        id=job_id,
        name=job_id,
        cron_expr=str(interval_seconds),
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "noop", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.EVERY,
        anchor_at=anchor_at,
        next_run_at=next_run_at,
        status=JobStatus.PENDING,
    )


def _cron_job(job_id: str, expr: str, next_run_at: datetime) -> CronJob:
    return CronJob(
        id=job_id,
        name=job_id,
        cron_expr=expr,
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "noop", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.CRON,
        next_run_at=next_run_at,
        status=JobStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_startup_catchup_fast_forwards_every_jobs() -> None:
    """Every overflow EVERY job must end with next_run_at strictly in the future."""
    now = datetime.now(UTC)
    anchor = now - timedelta(hours=2)
    stale = now - timedelta(minutes=30)
    interval = 60

    async with JobStore(":memory:") as store:
        jobs = [_every_job(f"every-{i}", interval, anchor, stale) for i in range(3)]
        for job in jobs:
            await store.save(job)

        # max_catchup=0 forces every overdue job into the fast-forward branch.
        timer = SchedulerTimer(store, handlers={}, max_catchup=0)
        await timer.startup_catchup()

        for job_id in ("every-0", "every-1", "every-2"):
            reloaded = await store.get(job_id)
            assert reloaded is not None
            assert reloaded.next_run_at is not None
            # Must be strictly in the future — the pre-fix bug left it stale.
            assert reloaded.next_run_at > now, (
                f"{job_id} not fast-forwarded: next_run_at={reloaded.next_run_at} now={now}"
            )
            # Must align to the anchor grid: anchor + k*interval.
            offset = (reloaded.next_run_at - anchor).total_seconds()
            assert offset % interval == 0


@pytest.mark.asyncio
async def test_startup_catchup_fast_forwards_cron_jobs() -> None:
    """Regression guard: the unified fast-forward still works for CRON jobs."""
    now = datetime.now(UTC)
    stale = now - timedelta(hours=1)

    async with JobStore(":memory:") as store:
        # `* * * * *` fires every minute, so the next future minute is always close.
        await store.save(_cron_job("cron-1", "* * * * *", stale))

        timer = SchedulerTimer(store, handlers={}, max_catchup=0)
        await timer.startup_catchup()

        reloaded = await store.get("cron-1")
        assert reloaded is not None
        assert reloaded.next_run_at is not None
        assert reloaded.next_run_at > now


@pytest.mark.asyncio
async def test_startup_catchup_uses_registered_handler_for_overdue_job() -> None:
    """An overdue boot job must run through its real handler, not fail lookup."""
    now = datetime.now(UTC)
    called: list[str] = []

    async def handler(job: CronJob) -> str:
        called.append(job.id)
        return "caught up"

    async with JobStore(":memory:") as store:
        await store.save(_cron_job("overdue", "* * * * *", now - timedelta(minutes=1)))
        timer = SchedulerTimer(store, handlers={"agent_run": handler}, max_catchup=1)

        await timer.startup_catchup()
        await asyncio.gather(*timer._running.values())

        reloaded = await store.get("overdue")
        assert called == ["overdue"]
        assert reloaded is not None
        assert reloaded.status == JobStatus.PENDING
        assert reloaded.last_error is None
        runs = await store.list_executions("overdue", limit=10)
        assert len(runs) == 1
        assert runs[0].success is True
