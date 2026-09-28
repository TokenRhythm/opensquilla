"""A replacement one-shot occurrence survives completion of its active predecessor."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from opensquilla.scheduler import timer
from opensquilla.scheduler.engine import SchedulerEngine
from opensquilla.scheduler.jobs import apply_reserved_result
from opensquilla.scheduler.ops import SchedulerOps
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import (
    CronJob,
    JobExecution,
    JobReservation,
    JobStatus,
    ManualRunStatus,
    ScheduleKind,
)


def _one_shot(at: datetime, *, delete_after_run: bool = True) -> CronJob:
    return CronJob(
        name="one-shot report",
        cron_expr=at.isoformat(),
        schedule_raw=at.isoformat(),
        schedule_kind=ScheduleKind.AT,
        next_run_at=at,
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "original report", "agent_id": "main"},
        delete_after_run=delete_after_run,
    )


def _assert_replacement(job: CronJob | None, at: datetime, delete_after_run: bool) -> None:
    assert job is not None, "completion must preserve the replacement occurrence"
    assert job.status == JobStatus.PENDING
    assert job.enabled is True
    assert job.next_run_at == at
    assert job.delete_after_run is delete_after_run
    assert job.backoff_until is None
    assert job.consecutive_errors == 0
    assert job.reservation_token == ""
    assert job.reserved_at is None
    assert job.reserved_by == ""
    assert job.reservation_source == ""
    assert job.scheduled_run_at is None


@pytest.mark.parametrize(
    ("error", "previous_errors", "delete_after_run", "overdue"),
    [
        (None, 0, True, False),
        (None, 0, False, True),
        ("network timeout", 0, True, False),
        ("invalid api key", 0, False, False),
        ("network timeout", 2, False, False),
    ],
    ids=["delete", "disable-overdue", "retry", "permanent", "retry-exhausted"],
)
async def test_old_result_preserves_replacement_occurrence(
    error: str | None, previous_errors: int, delete_after_run: bool, overdue: bool,
) -> None:
    now = datetime.now(UTC)
    replacement = now + (timedelta(minutes=-1) if overdue else timedelta(hours=1))
    async with JobStore(":memory:") as store:
        job = _one_shot(now - timedelta(minutes=2), delete_after_run=delete_after_run)
        job.consecutive_errors = previous_errors
        job.error_count = previous_errors
        job.run_count = previous_errors
        await store.save(job)
        reservation = await store.reserve_manual_job(job.id, now)
        assert isinstance(reservation, JobReservation)
        # An occurrence can become overdue while the preceding handler is still running.
        edit_time = replacement - timedelta(seconds=1) if overdue else now
        edited = await SchedulerOps(store, clock=lambda: edit_time).update(
            job.id, schedule_kind=ScheduleKind.AT, schedule_value=replacement.isoformat(),
        )
        assert edited is not None
        assert edited.reservation_token == reservation.token

        execution = JobExecution(job_id=job.id, success=error is None, error=error)
        assert await apply_reserved_result(job.id, reservation.token, execution, store)

        current = await store.get(job.id)
        _assert_replacement(current, replacement, delete_after_run)
        assert current is not None
        assert current.run_count == previous_errors + 1
        assert current.error_count == previous_errors + int(error is not None)
        assert current.last_error == error
        if error == "invalid api key":
            rerun = await store.reserve_due_job(job.id, replacement)
            assert isinstance(rerun, JobReservation)
            assert await apply_reserved_result(
                job.id, rerun.token, JobExecution(job_id=job.id, success=False, error=error), store,
            )
            failed_replacement = await store.get(job.id)
            assert failed_replacement is not None
            assert failed_replacement.status == JobStatus.DISABLED
            assert failed_replacement.enabled is False
            assert failed_replacement.next_run_at is None
            assert failed_replacement.run_count == 2
            assert failed_replacement.error_count == 2


@pytest.mark.parametrize(("source", "delete_after_run"), [("timer", True), ("manual", False)])
async def test_replacement_waits_for_active_run_then_runs_once(
    monkeypatch: pytest.MonkeyPatch, source: str, delete_after_run: bool,
) -> None:
    now = datetime.now(UTC)
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def handler(job: CronJob) -> str:
        calls.append(job.payload["task"])
        if len(calls) == 1:
            started.set()
            await release.wait()
        return "completed"

    async with JobStore(":memory:") as store:
        engine = SchedulerEngine(store)
        engine.register_handler("agent_run", handler)
        original = now + (timedelta(hours=3) if source == "manual" else timedelta(minutes=-1))
        replacement = now + timedelta(hours=2)
        job = _one_shot(original, delete_after_run=delete_after_run)
        await store.save(job)
        active: asyncio.Task | None = None
        try:
            if source == "manual":
                active = asyncio.create_task(engine.run_job_now(job.id))
            else:
                await engine._timer._tick()
                active = engine._timer._running[job.id]
            await asyncio.wait_for(started.wait(), timeout=2)
            edited = await engine.update_job(
                job.id, schedule_kind=ScheduleKind.AT, schedule_value=replacement.isoformat(),
                payload={"task": "replacement report"},
            )
            assert edited is not None
            assert edited.reservation_token

            class AfterReplacement(datetime):
                @classmethod
                def now(cls, tz=None):
                    return (replacement + timedelta(seconds=1)).astimezone(tz)

            monkeypatch.setattr(timer, "datetime", AfterReplacement)
            await engine._timer._tick()
            duplicate = await engine.run_job_now(job.id)
            assert duplicate.status == ManualRunStatus.BUSY
            assert calls == ["original report"]

            release.set()
            await asyncio.wait_for(active, timeout=2)
            current = await store.get(job.id)
            _assert_replacement(current, replacement, delete_after_run)
            await engine._timer._tick()
            await asyncio.wait_for(engine._timer._running[job.id], timeout=2)
            await engine._timer._tick()
            assert calls == ["original report", "replacement report"]
            completed = await store.get(job.id)
            if delete_after_run:
                assert completed is None
            else:
                assert completed is not None
                assert completed.status == JobStatus.DISABLED
                assert completed.run_count == 2
        finally:
            release.set()
            if active is not None:
                await asyncio.gather(active, return_exceptions=True)
            await engine.stop()


async def test_cancellation_releases_the_reservation_without_consuming_replacement() -> None:
    now = datetime.now(UTC)
    replacement = now + timedelta(hours=1)
    started = asyncio.Event()

    async def handler(_job: CronJob) -> None:
        started.set()
        await asyncio.Event().wait()

    async with JobStore(":memory:") as store:
        engine = SchedulerEngine(store)
        engine.register_handler("agent_run", handler)
        job = _one_shot(now - timedelta(minutes=1))
        await store.save(job)
        try:
            await engine._timer._tick()
            active = engine._timer._running[job.id]
            await asyncio.wait_for(started.wait(), timeout=2)
            await engine.update_job(
                job.id, schedule_kind=ScheduleKind.AT, schedule_value=replacement.isoformat(),
            )
            active.cancel()
            with pytest.raises(asyncio.CancelledError):
                await active
            current = await store.get(job.id)
            _assert_replacement(current, replacement, True)
            assert current is not None
            assert current.run_count == 0
            assert current.error_count == 0
        finally:
            await engine.stop()


@pytest.mark.parametrize("outcome", ["success", "failure", "missing_handler"])
async def test_finalization_reloads_a_concurrent_one_shot_reschedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    now = datetime.now(UTC)
    replacement = now + timedelta(hours=1)
    db_path = str(tmp_path / "scheduler.db")
    async with JobStore(db_path) as store, JobStore(db_path) as writer:
        job = _one_shot(now - timedelta(minutes=1))
        await store.save(job)
        reservation = await store.reserve_manual_job(job.id, now)
        assert isinstance(reservation, JobReservation)
        await writer._db().execute("BEGIN IMMEDIATE")
        write_started = asyncio.Event()
        execute = store._db().execute

        def observe_write(sql, params=()):
            if sql.lstrip().startswith(("UPDATE", "DELETE")) and "scheduler_jobs" in sql:
                write_started.set()
            return execute(sql, params)

        monkeypatch.setattr(store._db(), "execute", observe_write)
        error = {
            "success": None,
            "failure": "invalid api key",
            "missing_handler": "No handler registered",
        }[outcome]
        if outcome == "missing_handler":
            assert error is not None
            operation = store.finalize_reserved_missing_handler(job.id, reservation.token, error)
        else:
            operation = apply_reserved_result(
                job.id, reservation.token,
                JobExecution(job_id=job.id, success=error is None, error=error), store,
            )
        completion = asyncio.create_task(operation)
        try:
            await asyncio.wait_for(write_started.wait(), timeout=5)
            edited = await SchedulerOps(writer, clock=lambda: now + timedelta(seconds=1)).update(
                job.id, schedule_kind=ScheduleKind.AT, schedule_value=replacement.isoformat(),
                name="replacement occurrence", payload={"task": "replacement report"},
            )
            assert edited is not None
            assert await asyncio.wait_for(completion, timeout=5)
            current = await writer.get(job.id)
            _assert_replacement(current, replacement, True)
            assert current is not None
            assert current.name == "replacement occurrence"
            assert current.payload["task"] == "replacement report"
            assert current.run_count == int(outcome != "missing_handler")
            assert current.error_count == int(error is not None)
            assert current.last_error == error
        finally:
            await writer._db().rollback()
            await asyncio.gather(completion, return_exceptions=True)


@pytest.mark.parametrize(
    ("edit", "error", "previous_errors", "delete_after_run"),
    [
        ("none", None, 0, True),
        ("offset", None, 0, False),
        ("metadata", None, 0, True),
        ("offset", "network timeout", 0, True),
        ("metadata", "invalid api key", 0, False),
        ("none", "network timeout", 2, False),
    ],
    ids=["early-manual", "same-instant", "metadata", "retry", "permanent", "exhausted"],
)
async def test_unchanged_occurrence_keeps_existing_completion_behavior(
    edit: str, error: str | None, previous_errors: int, delete_after_run: bool,
) -> None:
    now = datetime.now(UTC)
    at = now + timedelta(hours=1)
    async with JobStore(":memory:") as store:
        job = _one_shot(at, delete_after_run=delete_after_run)
        job.consecutive_errors = previous_errors
        await store.save(job)
        reservation = await store.reserve_manual_job(job.id, now)
        assert isinstance(reservation, JobReservation)
        ops = SchedulerOps(store)
        if edit == "offset":
            await ops.update(
                job.id, schedule_kind=ScheduleKind.AT,
                schedule_value=at.astimezone(timezone(timedelta(hours=8))).isoformat(),
            )
        elif edit == "metadata":
            await ops.update(job.id, name="renamed report", payload={"task": "edited report"})
        assert await apply_reserved_result(
            job.id, reservation.token,
            JobExecution(job_id=job.id, success=error is None, error=error), store,
        )
        current = await store.get(job.id)
        if error is None and delete_after_run:
            assert current is None
            return
        assert current is not None
        assert current.reservation_token == ""
        if error == "network timeout" and previous_errors == 0:
            assert current.status == JobStatus.PENDING
            assert current.backoff_until is not None
            assert current.consecutive_errors == 1
        else:
            assert current.status == JobStatus.DISABLED
            assert current.enabled is False


@pytest.mark.parametrize("status", [JobStatus.PAUSED, JobStatus.DISABLED])
async def test_reschedule_does_not_override_a_paused_or_disabled_job(status: JobStatus) -> None:
    now = datetime.now(UTC)
    replacement = now + timedelta(hours=1)
    async with JobStore(":memory:") as store:
        job = _one_shot(now - timedelta(minutes=1))
        await store.save(job)
        reservation = await store.reserve_manual_job(job.id, now)
        assert isinstance(reservation, JobReservation)
        await SchedulerOps(store).update(
            job.id, schedule_kind=ScheduleKind.AT, schedule_value=replacement.isoformat(),
            enabled=False,
        )
        if status == JobStatus.DISABLED:
            disabled = await store.get(job.id)
            assert disabled is not None
            disabled.status = status
            await store.save(disabled)
        assert await apply_reserved_result(
            job.id, reservation.token, JobExecution(job_id=job.id, success=True), store,
        )
        current = await store.get(job.id)
        assert current is not None
        assert current.status == status
        assert current.enabled is False
        assert current.next_run_at == replacement
        assert current.reservation_token == ""
        assert current.run_count == 0


@pytest.mark.parametrize("changed_time", [False, True])
async def test_idle_reschedule_resets_backoff_only_for_a_different_occurrence(
    changed_time: bool,
) -> None:
    now = datetime.now(UTC)
    at = now + timedelta(hours=1)
    replacement = at + timedelta(hours=1) if changed_time else at
    async with JobStore(":memory:") as store:
        job = _one_shot(at)
        job.backoff_until = at + timedelta(hours=2)
        job.consecutive_errors = 2
        job.error_count = 2
        job.last_error = "network timeout"
        await store.save(job)
        await SchedulerOps(store).update(
            job.id, schedule_kind=ScheduleKind.AT,
            schedule_value=replacement.astimezone(timezone(timedelta(hours=8))).isoformat(),
        )
        current = await store.get(job.id)
        assert current is not None
        assert current.next_run_at == replacement
        assert current.backoff_until == (None if changed_time else job.backoff_until)
        assert current.consecutive_errors == (0 if changed_time else 2)
        assert current.error_count == 2
        assert current.last_error == "network timeout"
