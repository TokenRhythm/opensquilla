"""Cron create/update ``enabled`` behavior stays effective and composable."""

from __future__ import annotations

from pathlib import Path

from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_cron import _handle_cron_add, _handle_cron_update
from opensquilla.scheduler.engine import SchedulerEngine
from opensquilla.scheduler.jobs import apply_result
from opensquilla.scheduler.payloads import make_agent_turn_payload, payload_text
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import JobExecution, JobStatus, ScheduleKind, SessionTarget


async def _make_engine(tmp_path: Path) -> tuple[SchedulerEngine, JobStore]:
    store = JobStore(str(tmp_path / "cron.db"))
    await store.open()
    return SchedulerEngine(store), store


async def _add_recurring_job(engine: SchedulerEngine):
    return await engine.add_job(
        name="daily-report",
        schedule_kind=ScheduleKind.CRON,
        schedule_value="0 9 * * *",
        handler_key="agent_run",
        payload=make_agent_turn_payload("old prompt"),
        session_target=SessionTarget.ISOLATED,
        jitter_seconds=0,
    )


def _ctx(engine: SchedulerEngine) -> RpcContext:
    return RpcContext(conn_id="test", cron_scheduler=engine)


async def test_create_enabled_false_is_persisted_as_paused(tmp_path: Path) -> None:
    engine, store = await _make_engine(tmp_path)
    try:
        result = await _handle_cron_add(
            {
                "name": "paused reminder",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "payloadKind": "reminder",
                "text": "stand up",
                "agentId": "main",
                "enabled": False,
            },
            _ctx(engine),
        )

        assert result["enabled"] is False
        assert result["status"] == JobStatus.PAUSED.value
        job_id = result["id"]
        stored = await store.get(job_id)
        assert stored is not None
        assert stored.enabled is False
        assert stored.status == JobStatus.PAUSED
    finally:
        await store.close()

    reopened = JobStore(str(tmp_path / "cron.db"))
    await reopened.open()
    try:
        persisted = await reopened.get(job_id)
        assert persisted is not None
        assert persisted.enabled is False
        assert persisted.status == JobStatus.PAUSED
    finally:
        await reopened.close()


async def test_update_enabled_true_revives_auto_disabled_job(tmp_path: Path) -> None:
    engine, store = await _make_engine(tmp_path)
    try:
        job = await _add_recurring_job(engine)
        failure = JobExecution(job_id=job.id, success=False, error="provider returned 403")
        await apply_result(job, failure, store)
        disabled = await store.get(job.id)
        assert disabled is not None and disabled.status == JobStatus.DISABLED

        await _handle_cron_update({"id": job.id, "enabled": True}, _ctx(engine))

        after = await store.get(job.id)
        assert after is not None
        assert after.enabled is True
        assert after.status != JobStatus.DISABLED
    finally:
        await store.close()


async def test_update_enabled_true_revives_failed_job(tmp_path: Path) -> None:
    engine, store = await _make_engine(tmp_path)
    try:
        job = await _add_recurring_job(engine)
        for _ in range(5):
            current = await store.get(job.id)
            assert current is not None
            failure = JobExecution(job_id=job.id, success=False, error="network unreachable")
            await apply_result(current, failure, store)
        failed = await store.get(job.id)
        assert failed is not None and failed.status == JobStatus.FAILED

        await _handle_cron_update({"id": job.id, "enabled": True}, _ctx(engine))

        after = await store.get(job.id)
        assert after is not None
        assert after.status != JobStatus.FAILED
        assert after.next_run_at is not None
    finally:
        await store.close()


async def test_update_enabled_true_applies_sibling_fields(tmp_path: Path) -> None:
    engine, store = await _make_engine(tmp_path)
    try:
        job = await _add_recurring_job(engine)
        await engine.pause_job(job.id)

        await _handle_cron_update(
            {"id": job.id, "enabled": True, "text": "new prompt"}, _ctx(engine)
        )

        after = await store.get(job.id)
        assert after is not None
        assert after.status == JobStatus.PENDING
        assert payload_text(after.payload, after.session_target) == "new prompt"
    finally:
        await store.close()


async def test_update_enabled_false_applies_sibling_fields(tmp_path: Path) -> None:
    engine, store = await _make_engine(tmp_path)
    try:
        job = await _add_recurring_job(engine)

        await _handle_cron_update(
            {"id": job.id, "enabled": False, "text": "new prompt"}, _ctx(engine)
        )

        after = await store.get(job.id)
        assert after is not None
        assert after.status == JobStatus.PAUSED
        assert payload_text(after.payload, after.session_target) == "new prompt"
    finally:
        await store.close()
