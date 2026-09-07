from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.cron_scheduler import (
    CronJobMutation,
    CronListQuery,
    CronRunQuery,
    CronScheduler,
    CronSubscriptions,
)


@pytest.mark.asyncio
async def test_cron_scheduler_normalizes_queries_and_job_identity() -> None:
    port = AsyncMock()
    port.list_jobs.return_value = ()
    port.get_job.return_value = {"id": "daily"}
    scheduler = CronScheduler(port)

    await scheduler.list_jobs(CronListQuery(" main "))
    await scheduler.get_job(" daily ")

    assert port.list_jobs.await_args.args[0] == CronListQuery("main")
    assert port.get_job.await_args.args[0].job_id == "daily"


@pytest.mark.asyncio
async def test_cron_scheduler_rejects_invalid_targets_before_the_runtime_port() -> None:
    port = AsyncMock()
    scheduler = CronScheduler(port)

    with pytest.raises(ValueError, match="cron job id required"):
        await scheduler.run_job("  ")
    with pytest.raises(ValueError, match="limit must be positive"):
        await scheduler.list_runs(CronRunQuery("daily", 0))

    port.run_job.assert_not_awaited()
    port.list_runs.assert_not_awaited()


@pytest.mark.asyncio
async def test_cron_scheduler_keeps_create_and_update_as_explicit_use_cases() -> None:
    port = AsyncMock()
    port.create_job.return_value = {"id": "daily"}
    port.update_job.return_value = {"id": "daily", "enabled": False}
    scheduler = CronScheduler(port)

    created = await scheduler.create_job(CronJobMutation({"name": "Daily"}))
    updated = await scheduler.update_job(
        CronJobMutation({"enabled": False}, job_id=" daily ")
    )

    assert created == {"id": "daily"}
    assert updated == {"id": "daily", "enabled": False}
    assert port.update_job.await_args.args[0] == CronJobMutation(
        {"enabled": False}, job_id="daily"
    )


@pytest.mark.asyncio
async def test_cron_subscriptions_normalize_optional_job_topics() -> None:
    port = AsyncMock()
    port.subscribe.return_value = {"ok": True, "topic": "cron:daily"}
    port.unsubscribe.return_value = {"ok": True, "topic": "cron:*"}
    subscriptions = CronSubscriptions(port)

    await subscriptions.subscribe(" daily ")
    await subscriptions.unsubscribe("   ")

    assert port.subscribe.await_args.args[0].job_id == "daily"
    assert port.unsubscribe.await_args.args[0].job_id is None
