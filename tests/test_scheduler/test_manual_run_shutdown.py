"""Manual cron shutdown owns admission and exact-token reservation cleanup."""

from __future__ import annotations

import asyncio

import pytest

from opensquilla.scheduler.engine import SchedulerEngine
from opensquilla.scheduler.payloads import make_agent_turn_payload
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import ManualRunStatus, ScheduleKind


async def _job(engine):
    return await engine.add_job(name="manual", schedule_kind=ScheduleKind.CRON,
        schedule_value="0 9 * * *", payload=make_agent_turn_payload("synthetic"),
        jitter_seconds=0)


@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
async def test_stop_waits_for_reservation_result_and_releases_its_token(
    tmp_path, monkeypatch, phase
):
    async with JobStore(str(tmp_path / "cron.db")) as store:
        engine = SchedulerEngine(store)
        entered, release = asyncio.Event(), asyncio.Event()
        handler_called = False

        async def handler(job):
            nonlocal handler_called
            handler_called = True

        engine.register_handler("agent_run", handler)
        job = await _job(engine)
        reserve = store.reserve_manual_job

        async def delayed_reserve(*args, **kwargs):
            if phase == "before_commit":
                entered.set()
                await release.wait()
            result = await reserve(*args, **kwargs)
            if phase == "after_commit":
                entered.set()
                await release.wait()
            return result

        monkeypatch.setattr(store, "reserve_manual_job", delayed_reserve)
        run = asyncio.create_task(engine.run_job_now(job.id))
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            stopping = asyncio.create_task(engine.stop())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not stopping.done()
            rejected = await engine.run_job_now(job.id)
            assert rejected.status == ManualRunStatus.BLOCKED
            assert rejected.reason == "scheduler_stopping"
            release.set()
            await asyncio.wait_for(stopping, 2)
            with pytest.raises(asyncio.CancelledError):
                await run
            assert not handler_called
            assert not (await store.get(job.id)).reservation_token
            assert await engine.get_runs(job.id) == []
            assert not engine._manual_run_tasks
        finally:
            release.set()
            await engine.stop()
            await asyncio.gather(run, *([stopping] if stopping else []), return_exceptions=True)


@pytest.mark.parametrize("edit", ["unchanged", "deleted", "replacement"])
async def test_stop_cancels_manual_handler_without_clearing_another_owner(tmp_path, edit):
    async with JobStore(str(tmp_path / "cron.db")) as store:
        engine = SchedulerEngine(store)
        entered = asyncio.Event()

        async def handler(job):
            entered.set()
            await asyncio.Event().wait()

        engine.register_handler("agent_run", handler)
        job = await _job(engine)
        run = asyncio.create_task(engine.run_job_now(job.id))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if edit == "deleted":
                await store.delete(job.id)
            elif edit == "replacement":
                current = await store.get(job.id)
                current.reservation_token = "replacement-owner"
                await store.save(current)
            await asyncio.wait_for(engine.stop(), 2)
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(run, 2)
            current = await store.get(job.id)
            if edit == "deleted":
                assert current is None
            else:
                assert current.reservation_token == (
                    "replacement-owner" if edit == "replacement" else ""
                )
            assert not engine._manual_run_tasks
        finally:
            await engine.stop()
            if not run.done():
                run.cancel()
            await asyncio.gather(run, return_exceptions=True)
