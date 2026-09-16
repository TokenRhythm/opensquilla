"""Cron create/update ``enabled`` behavior stays effective and composable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_cron import (
    _cron_create_contract as _handle_cron_add,
)
from opensquilla.gateway.rpc_cron import (
    _cron_update_contract as _handle_cron_update,
)
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


@pytest.mark.parametrize(
    ("status", "enabled"),
    [
        (JobStatus.PENDING, False),
        (JobStatus.PAUSED, True),
        (JobStatus.DISABLED, True),
        (JobStatus.FAILED, True),
    ],
)
@pytest.mark.parametrize(
    ("patch", "error"),
    [
        ({"schedule": {"kind": "at", "at": "2035-01-01T19:59:59+08:00"}}, "in the past"),
        ({"tz": "Invalid/Timezone"}, "Unknown timezone"),
    ],
    ids=["past-at", "invalid-timezone"],
)
async def test_update_invalid_patch_does_not_change_enabled_or_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: JobStatus,
    enabled: bool,
    patch: dict,
    error: str,
) -> None:
    now = datetime(2035, 1, 1, 12, tzinfo=UTC)
    store = JobStore(str(tmp_path / "cron.db"))
    await store.open()
    engine = SchedulerEngine(store, clock=lambda: now)
    try:
        job = await engine.add_job(
            name="original",
            schedule_kind=ScheduleKind.AT,
            schedule_value=(now + timedelta(hours=1)).isoformat(),
            payload=make_agent_turn_payload("original reminder"),
        )
        job.status = status
        job.enabled = not enabled
        job.consecutive_errors = 2
        job.backoff_until = now + timedelta(minutes=1)
        await store.save(job)
        before = await store.get(job.id)
        save = AsyncMock(wraps=store.save)
        monkeypatch.setattr(store, "save", save)

        with pytest.raises(ValueError, match=error):
            await _handle_cron_update(
                {"id": job.id, "enabled": enabled, "name": "changed", **patch},
                _ctx(engine),
            )

        assert await store.get(job.id) == before
        save.assert_not_awaited()
    finally:
        await store.close()


@pytest.mark.parametrize("enabled", [False, True])
async def test_update_enabled_and_schedule_persist_together_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool,
) -> None:
    now = datetime(2035, 1, 1, 12, tzinfo=UTC)
    store = JobStore(str(tmp_path / "cron.db"))
    await store.open()
    engine = SchedulerEngine(store, clock=lambda: now)
    try:
        job = await engine.add_job(
            name="original",
            enabled=not enabled,
            schedule_kind=ScheduleKind.AT,
            schedule_value=(now + timedelta(hours=1)).isoformat(),
            payload=make_agent_turn_payload("original reminder"),
        )
        save = AsyncMock(wraps=store.save)
        monkeypatch.setattr(store, "save", save)
        new_at = (now + timedelta(hours=2)).isoformat()

        await _handle_cron_update(
            {
                "id": job.id,
                "enabled": enabled,
                "text": "updated reminder",
                "schedule": {"kind": "at", "at": new_at},
            },
            _ctx(engine),
        )

        after = await store.get(job.id)
        assert after is not None
        assert after.status == (JobStatus.PENDING if enabled else JobStatus.PAUSED)
        assert after.enabled is enabled
        assert after.next_run_at == now + timedelta(hours=2)
        assert payload_text(after.payload, after.session_target) == "updated reminder"
        save.assert_awaited_once()
    finally:
        await store.close()
