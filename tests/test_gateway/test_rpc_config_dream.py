"""Dream scheduling follows its own settings, independently of model routing."""

from __future__ import annotations

import tomllib
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.boot import _register_dream_crons
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.dream_bridge import register_dream_reconciler, reset_dream_reconciler
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_config import (
    _handle_config_apply,
    _handle_config_patch,
    _handle_config_patch_safe,
    _handle_config_set,
)
from opensquilla.scheduler.types import CronJob, JobStatus


@pytest.fixture
def ctx(tmp_path):
    reset_dream_reconciler()
    cfg = GatewayConfig(config_path=str(tmp_path / "config.toml"))
    yield RpcContext(
        conn_id="synthetic",
        config=cfg,
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
    )
    reset_dream_reconciler()


@pytest.mark.parametrize(
    "path,value",
    [
        ("enabled", True),
        ("auto_schedule", True),
        ("interval_h", 7),
        ("cron", "0 3 * * *"),
    ],
)
@pytest.mark.parametrize("operation", ["set", "patch", "merge", "apply"])
async def test_dream_schedule_edits_reconcile_after_persist(ctx, path, value, operation):
    async def reconcile():
        # The live object and durable payload must already contain the edit.
        assert getattr(ctx.config.memory.dream, path) == value
        with open(ctx.config.config_path, "rb") as f:
            assert tomllib.load(f)["memory"]["dream"][path] == value

    reconciler = AsyncMock(side_effect=reconcile)
    register_dream_reconciler(reconciler)
    if operation == "set":
        result = await _handle_config_set({"path": f"memory.dream.{path}", "value": value}, ctx)
    elif operation == "patch":
        result = await _handle_config_patch({"patches": {f"memory.dream.{path}": value}}, ctx)
    elif operation == "merge":
        result = await _handle_config_patch({"patch": {"memory": {"dream": {path: value}}}}, ctx)
    else:
        result = await _handle_config_apply({"config": {"memory": {"dream": {path: value}}}}, ctx)
    reconciler.assert_awaited_once()
    assert result["restartRequired"] is False
    assert "linked" not in result and "linkedLive" not in result
    assert ctx.config.memory.dream.preview_mode is True


@pytest.mark.parametrize("available", [False, True])
async def test_missing_or_failed_live_reconcile_requires_restart(ctx, available):
    if available:
        register_dream_reconciler(AsyncMock(side_effect=OSError("synthetic schedule error")))
    result = await _handle_config_patch_safe(
        {
            "patches": {
                "memory.dream.enabled": True,
                "memory.dream.auto_schedule": True,
            }
        },
        ctx,
    )
    assert ctx.config.memory.dream.enabled
    assert result["restartRequired"] is True
    assert "memory.dream" in result["restartSections"]
    assert "linked" not in result and "linkedLive" not in result


async def test_other_settings_and_unchanged_dream_do_not_reconcile(ctx):
    reconciler = AsyncMock()
    register_dream_reconciler(reconciler)
    await _handle_config_set({"path": "memory.dream.preview_mode", "value": False}, ctx)
    await _handle_config_set({"path": "memory.dream.enabled", "value": False}, ctx)
    await _handle_config_set({"path": "naming.enabled", "value": False}, ctx)
    reconciler.assert_not_awaited()
    assert ctx.config.memory.dream.preview_mode is False


async def test_import_discards_obsolete_subtree_and_safe_writes_reject_it(ctx):
    from opensquilla.application.app_settings import _SAFE_WRITE_PATCH_PATHS

    assert not any("self_learning" in path for path in _SAFE_WRITE_PATCH_PATHS)
    await _handle_config_apply(
        {
            "config": {
                "squilla_router": {"self_learning": {"enabled": True, "unknown": "discard"}},
                "memory": {"dream": {"enabled": False}},
            }
        },
        ctx,
    )
    assert not hasattr(ctx.config.squilla_router, "self_learning")
    with open(ctx.config.config_path, "rb") as f:
        assert "self_learning" not in tomllib.load(f).get("squilla_router", {})
    assert ctx.config.memory.dream.enabled is False


class RecordingScheduler:
    def __init__(self):
        self.jobs = []

    async def list_jobs(self):
        return list(self.jobs)

    async def add_job(self, **kwargs):
        self.jobs.append(
            CronJob(
                id="synthetic-dream",
                name=kwargs["name"],
                handler_key=kwargs["handler_key"],
                payload=kwargs["payload"],
                session_target=kwargs["session_target"],
                schedule_kind=kwargs["schedule_kind"],
                cron_expr=kwargs["schedule_value"],
            )
        )

    async def update_job(self, job_id, **kwargs):
        job = next(job for job in self.jobs if job.id == job_id)
        job.schedule_kind = kwargs.get("schedule_kind", job.schedule_kind)
        job.cron_expr = kwargs.get("schedule_value", job.cron_expr)
        return job

    async def pause_job(self, job_id):
        job = next(job for job in self.jobs if job.id == job_id)
        job.status = JobStatus.PAUSED
        return job

    async def resume_job(self, job_id):
        job = next(job for job in self.jobs if job.id == job_id)
        job.status = JobStatus.PENDING
        return job


async def test_live_dream_jobs_create_pause_resume_and_update(ctx):
    scheduler = RecordingScheduler()

    async def reconcile():
        assert await _register_dream_crons(
            scheduler=scheduler,
            memory_config=ctx.config.memory,
            agent_ids=["main"],
        )

    register_dream_reconciler(reconcile)
    await _handle_config_patch_safe(
        {
            "patches": {
                "memory.dream.enabled": True,
                "memory.dream.auto_schedule": True,
            }
        },
        ctx,
    )
    assert len(scheduler.jobs) == 1
    job = scheduler.jobs[0]
    for key in ("enabled", "auto_schedule"):
        await _handle_config_set({"path": f"memory.dream.{key}", "value": False}, ctx)
        assert job.status == JobStatus.PAUSED
        await _handle_config_set({"path": f"memory.dream.{key}", "value": True}, ctx)
        assert job.status == JobStatus.PENDING
    await _handle_config_set({"path": "memory.dream.interval_h", "value": 7}, ctx)
    assert job.cron_expr == str(7 * 3600)
    await _handle_config_set({"path": "memory.dream.cron", "value": "0 3 * * *"}, ctx)
    assert job.cron_expr == "0 3 * * *"
    assert len(scheduler.jobs) == 1


@pytest.mark.parametrize("failure", ["list_jobs", "pause_job", "update_job", "resume_job"])
async def test_scheduler_failures_are_not_reported_as_reconciled(ctx, failure):
    scheduler = RecordingScheduler()
    ctx.config.memory.dream.enabled = True
    ctx.config.memory.dream.auto_schedule = True
    assert await _register_dream_crons(
        scheduler=scheduler,
        memory_config=ctx.config.memory,
        agent_ids=["main"],
    )
    setattr(scheduler, failure, AsyncMock(side_effect=OSError("synthetic schedule failure")))
    if failure == "pause_job":
        ctx.config.memory.dream.enabled = False
    elif failure == "update_job":
        ctx.config.memory.dream.interval_h = 7
    elif failure == "resume_job":
        scheduler.jobs[0].status = JobStatus.PAUSED
    if failure in ("list_jobs", "pause_job"):
        assert not await _register_dream_crons(
            scheduler=scheduler,
            memory_config=ctx.config.memory,
            agent_ids=["main"],
        )
    else:
        with pytest.raises(OSError):
            await _register_dream_crons(
                scheduler=scheduler,
                memory_config=ctx.config.memory,
                agent_ids=["main"],
            )


@pytest.mark.parametrize("operation", ["pause_job", "update_job", "resume_job"])
async def test_job_disappearing_during_reconcile_requires_restart(ctx, operation):
    scheduler = RecordingScheduler()
    ctx.config.memory.dream.enabled = True
    ctx.config.memory.dream.auto_schedule = True
    await _register_dream_crons(
        scheduler=scheduler, memory_config=ctx.config.memory, agent_ids=["main"]
    )
    if operation == "resume_job":
        ctx.config.memory.dream.enabled = False
        scheduler.jobs[0].status = JobStatus.PAUSED
    # Scheduler CRUD returns None if another request removes the listed row.
    setattr(scheduler, operation, AsyncMock(return_value=None))

    async def reconcile():
        if not await _register_dream_crons(
            scheduler=scheduler, memory_config=ctx.config.memory, agent_ids=["main"]
        ):
            raise RuntimeError("Dream schedules could not be reconciled")

    register_dream_reconciler(reconcile)
    path, value = {
        "pause_job": ("enabled", False),
        "update_job": ("interval_h", 7),
        "resume_job": ("enabled", True),
    }[operation]
    result = await _handle_config_set({"path": f"memory.dream.{path}", "value": value}, ctx)
    getattr(scheduler, operation).assert_awaited_once()
    assert result["restartRequired"] is True
    assert "memory.dream" in result["restartSections"]
    assert "memory" not in result["liveApplied"]
