"""Deferred scheduler startup cannot overwrite live edits or current owners."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from opensquilla.compat import aiosqlite
from opensquilla.scheduler.engine import SchedulerEngine
from opensquilla.scheduler.ops import SchedulerOps
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.timer import SchedulerTimer
from opensquilla.scheduler.types import (
    CronJob,
    JobReservation,
    JobStatus,
    ManualRunStatus,
    ScheduleKind,
)
from tests.test_scheduler._async_helpers import drain_tasks, wait_task


def _job(*, due: bool = False) -> CronJob:
    return CronJob(
        name="original", handler_key="agent_run", cron_expr="60",
        schedule_kind=ScheduleKind.EVERY,
        next_run_at=datetime.now(UTC) + timedelta(minutes=-1 if due else 10),
        payload={"kind": "agent_turn", "task": "original task", "agent_id": "main"},
    )


async def _reject_execution_records(store: JobStore) -> None:
    await store._db().execute(
        """CREATE TRIGGER reject_runs BEFORE INSERT ON scheduler_runs
           BEGIN SELECT RAISE(ABORT, 'execution record failed'); END""",
    )
    await store._db().commit()


async def _allow_execution_records(store: JobStore) -> None:
    await store._db().execute("DROP TRIGGER reject_runs")
    await store._db().commit()


async def test_deferred_startup_preserves_an_already_running_manual_handler(tmp_path: Path) -> None:
    async with JobStore(str(tmp_path / "scheduler.db")) as store:
        job = _job()
        await store.save(job)
        engine = SchedulerEngine(store)
        started, finish = asyncio.Event(), asyncio.Event()
        calls = 0

        async def handler(_job: CronJob) -> str:
            nonlocal calls
            calls += 1
            started.set()
            if calls == 1:
                await finish.wait()
            return "done"

        engine.register_handler("agent_run", handler)
        active = asyncio.create_task(engine.run_job_now(job.id))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            before = await store.get(job.id)
            assert before is not None and before.reservation_token
            await engine.start()
            duplicate = await asyncio.wait_for(engine.run_job_now(job.id), timeout=2)
            assert duplicate.status == ManualRunStatus.BUSY
            assert calls == 1
            after = await store.get(job.id)
            assert after is not None and after.reservation_token == before.reservation_token
            finish.set()
            assert (await wait_task(active)).success
        finally:
            finish.set()
            await drain_tasks(asyncio.create_task(engine.stop()), active)


@pytest.mark.parametrize("restart_same_engine", [False, True])
async def test_startup_recovers_its_own_owner_after_the_manual_task_has_failed(
    tmp_path: Path, restart_same_engine: bool,
) -> None:
    async with JobStore(str(tmp_path / "scheduler.db")) as store:
        job = _job()
        await store.save(job)
        engine = SchedulerEngine(store)

        async def handler(_job: CronJob) -> str:
            return "done"

        engine.register_handler("agent_run", handler)
        if restart_same_engine:
            await engine.start()
        try:
            await _reject_execution_records(store)
            with pytest.raises(aiosqlite.IntegrityError, match="execution record failed"):
                await engine.run_job_now(job.id)
            await asyncio.sleep(0)
            assert not engine._manual_run_tasks
            abandoned = await store.get(job.id)
            assert abandoned is not None and abandoned.reservation_token
            await _allow_execution_records(store)
            if restart_same_engine:
                await engine.stop()
            await engine.start()
            recovered = await store.get(job.id)
            assert recovered is not None
            assert recovered.reservation_token == ""
            assert recovered.status == JobStatus.PENDING
            assert (await engine.run_job_now(job.id)).status == ManualRunStatus.ACCEPTED
        finally:
            await drain_tasks(asyncio.create_task(engine.stop()))


async def test_busy_retry_does_not_make_an_abandoned_owner_look_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with JobStore(str(tmp_path / "scheduler.db")) as store:
        job = _job()
        await store.save(job)
        engine = SchedulerEngine(store)

        async def handler(_job: CronJob) -> str:
            return "done"

        engine.register_handler("agent_run", handler)
        await _reject_execution_records(store)
        with pytest.raises(aiosqlite.IntegrityError):
            await engine.run_job_now(job.id)
        await _allow_execution_records(store)
        retry_read, release_retry = asyncio.Event(), asyncio.Event()
        original_get = store.get

        async def gated_get(job_id: str) -> CronJob | None:
            result = await original_get(job_id)
            if asyncio.current_task().get_name() == f"cron-manual-{job.id}":
                retry_read.set()
                await release_retry.wait()
            return result

        monkeypatch.setattr(store, "get", gated_get)
        retry = asyncio.create_task(engine.run_job_now(job.id))
        try:
            await asyncio.wait_for(retry_read.wait(), timeout=2)
            await engine._timer.startup_catchup()
            recovered = await original_get(job.id)
            assert recovered is not None and recovered.reservation_token == ""
            release_retry.set()
            assert (await wait_task(retry)).status == ManualRunStatus.ACCEPTED
        finally:
            release_retry.set()
            await drain_tasks(asyncio.create_task(engine.stop()), retry)


async def test_startup_preserves_a_committed_manual_reservation_before_its_caller_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with JobStore(str(tmp_path / "scheduler.db")) as store:
        job = _job()
        await store.save(job)
        engine = SchedulerEngine(store)

        async def handler(_job: CronJob) -> str:
            return "done"

        engine.register_handler("agent_run", handler)
        committed, release = asyncio.Event(), asyncio.Event()
        reserve = store.reserve_manual_job

        async def gated_reserve(*args, **kwargs):
            result = await reserve(*args, **kwargs)
            committed.set()
            await release.wait()
            return result

        monkeypatch.setattr(store, "reserve_manual_job", gated_reserve)
        active = asyncio.create_task(engine.run_job_now(job.id))
        try:
            await asyncio.wait_for(committed.wait(), timeout=2)
            before = await store.get(job.id)
            assert before is not None and before.reservation_token
            await engine._timer.startup_catchup()
            after = await store.get(job.id)
            assert after is not None and after.reservation_token == before.reservation_token
            release.set()
            assert (await wait_task(active)).status == ManualRunStatus.ACCEPTED
        finally:
            release.set()
            await drain_tasks(asyncio.create_task(engine.stop()), active)


async def test_startup_recovers_a_finished_timer_task_with_a_failed_execution_record(
    tmp_path: Path,
) -> None:
    async with JobStore(str(tmp_path / "scheduler.db")) as store:
        job = _job(due=True)
        await store.save(job)
        calls = 0

        async def handler(_job: CronJob) -> str:
            nonlocal calls
            calls += 1
            return "done"

        timer = SchedulerTimer(store, {"agent_run": handler})
        await _reject_execution_records(store)
        await timer._tick()
        with pytest.raises(aiosqlite.IntegrityError):
            await wait_task(timer._running[job.id])
        await _allow_execution_records(store)
        await timer.startup_catchup()
        recovered = await store.get(job.id)
        assert recovered is not None and recovered.reservation_token == ""
        assert recovered.status == JobStatus.PENDING
        assert calls == 1


@pytest.mark.parametrize("concurrent", ["edit", "reserve", "delete"])
async def test_startup_fast_forward_does_not_overwrite_a_concurrent_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent: str,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as writer:
        job = _job(due=True)
        await store.save(job)
        await writer._db().execute("BEGIN IMMEDIATE")
        started = asyncio.Event()
        execute = store._db().execute

        def observed_execute(sql, params=()):
            if sql.lstrip().startswith(("INSERT", "UPDATE")) and "scheduler_jobs" in sql:
                started.set()
            return execute(sql, params)

        monkeypatch.setattr(store._db(), "execute", observed_execute)
        timer = SchedulerTimer(store, {}, max_catchup=0)
        startup = asyncio.create_task(timer.startup_catchup())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            if concurrent == "edit":
                changed = await SchedulerOps(writer).update(
                    job.id, name="new schedule", schedule_kind=ScheduleKind.EVERY,
                    schedule_value="3600", payload={"task": "new task"},
                )
            elif concurrent == "reserve":
                reserved = await writer.reserve_manual_job(job.id, datetime.now(UTC))
                assert isinstance(reserved, JobReservation)
                changed = reserved.job
            else:
                changed = None
                await writer.delete(job.id)
            await wait_task(startup)
            current = await writer.get(job.id)
            if concurrent == "delete":
                assert current is None
            else:
                assert changed is not None and current is not None
                assert current.name == changed.name
                assert current.payload == changed.payload
                assert current.next_run_at == changed.next_run_at
                assert current.reservation_token == changed.reservation_token
                assert current.status == changed.status
        finally:
            await writer._db().rollback()
            await drain_tasks(startup)


@pytest.mark.parametrize("concurrent", ["edit", "pause", "delete", "current_owner"])
async def test_startup_recovery_reloads_changed_old_reservations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, concurrent: str,
) -> None:
    path = str(tmp_path / "scheduler.db")
    async with JobStore(path) as store, JobStore(path) as writer:
        job = _job()
        await store.save(job)
        old = await writer.reserve_manual_job(job.id, datetime.now(UTC), owner="scheduler-manual")
        assert isinstance(old, JobReservation)
        await writer._db().execute("BEGIN IMMEDIATE")
        started = asyncio.Event()
        execute = store._db().execute

        def observed_execute(sql, params=()):
            if sql.lstrip().startswith(("INSERT", "UPDATE")) and "scheduler_jobs" in sql:
                started.set()
            return execute(sql, params)

        monkeypatch.setattr(store._db(), "execute", observed_execute)
        timer = SchedulerTimer(store, {})
        startup = asyncio.create_task(timer.startup_catchup())
        new: JobReservation | None = None
        live_timer: asyncio.Task | None = None
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            async with writer.transaction():
                if concurrent == "delete":
                    await writer.delete(job.id)
                elif concurrent == "current_owner":
                    await writer.release_reservation(job.id, old.token)
                    live_timer = asyncio.create_task(asyncio.Event().wait())
                    timer._running[job.id] = live_timer
                    reserved = await writer.reserve_manual_job(
                        job.id, datetime.now(UTC), owner=timer._reservation_owner,
                    )
                    assert isinstance(reserved, JobReservation)
                    new = reserved
                else:
                    await SchedulerOps(writer).update(job.id, name="edited during startup")
                    if concurrent == "pause":
                        await SchedulerOps(writer).pause(job.id)
            await wait_task(startup)
            current = await writer.get(job.id)
            if concurrent == "delete":
                assert current is None
            elif concurrent == "current_owner":
                assert current is not None and new is not None
                assert current.reservation_token == new.token
                assert current.status == JobStatus.RUNNING
            else:
                assert current is not None
                assert current.name == "edited during startup"
                assert current.reservation_token == "", (
                    "CAS retry must not leave an old owner stuck"
                )
                assert current.status == (
                    JobStatus.PAUSED if concurrent == "pause" else JobStatus.PENDING
                )
        finally:
            await writer._db().rollback()
            if live_timer is not None:
                live_timer.cancel()
                await drain_tasks(live_timer)
            await drain_tasks(startup)


@pytest.mark.parametrize(
    "owner", ["scheduler-manual", "scheduler-startup", "scheduler-timer", "previous_instance"],
)
@pytest.mark.parametrize("paused", [False, True])
async def test_new_engine_recovers_previous_owners_without_losing_pause_or_counters(
    tmp_path: Path, owner: str, paused: bool,
) -> None:
    async with JobStore(str(tmp_path / "scheduler.db")) as store:
        job = _job()
        job.run_count, job.error_count = 3, 2
        await store.save(job)
        previous_engine = SchedulerEngine(store)
        old_owner = previous_engine._reservation_owner if owner == "previous_instance" else owner
        reserved = await store.reserve_manual_job(job.id, datetime.now(UTC), owner=old_owner)
        assert isinstance(reserved, JobReservation)
        if paused:
            await previous_engine.pause_job(job.id)
        restarted = SchedulerEngine(store)
        assert restarted._reservation_owner != previous_engine._reservation_owner
        await restarted._timer.startup_catchup()
        current = await store.get(job.id)
        assert current is not None
        assert current.reservation_token == ""
        assert current.run_count == 3 and current.error_count == 2
        assert current.status == (JobStatus.PAUSED if paused else JobStatus.PENDING)
