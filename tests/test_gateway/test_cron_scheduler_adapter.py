from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from opensquilla.application.cron_scheduler import (
    CronJobMutation,
    CronJobTarget,
    CronListQuery,
    CronRunQuery,
    CronSchedulerPort,
    CronSubscriptionPort,
    CronTopic,
)
from opensquilla.gateway.adapters.cron_scheduler import (
    GatewayCronSchedulerAdapter,
)


def _ports() -> tuple[AsyncMock, AsyncMock]:
    scheduler = AsyncMock(spec=CronSchedulerPort)
    scheduler.list_jobs.return_value = [{"id": "daily"}]
    scheduler.get_job.return_value = {"id": "daily"}
    scheduler.create_job.return_value = {"id": "daily"}
    scheduler.update_job.return_value = {"id": "daily", "enabled": False}
    scheduler.run_job.return_value = {"runId": "run-1", "success": True}
    scheduler.list_runs.return_value = [{"id": "run-1", "success": True}]
    subscriptions = AsyncMock(spec=CronSubscriptionPort)
    subscriptions.subscribe.return_value = {"ok": True, "topic": "cron:daily"}
    subscriptions.unsubscribe.return_value = {"ok": True, "topic": "cron:*"}
    return scheduler, subscriptions


@pytest.mark.asyncio
async def test_cron_adapter_projects_typed_queries_and_mutations() -> None:
    scheduler, subscriptions = _ports()
    adapter = GatewayCronSchedulerAdapter(scheduler, subscriptions)

    assert await adapter.list_jobs({"agentId": " main "}) == [{"id": "daily"}]
    assert await adapter.status({"id": " daily "}) == {"id": "daily"}
    assert await adapter.create({"name": "Daily"}) == {"id": "daily"}
    assert await adapter.update({"id": " daily ", "enabled": False}) == {
        "id": "daily",
        "enabled": False,
    }

    scheduler.list_jobs.assert_awaited_once_with(CronListQuery("main"))
    scheduler.get_job.assert_awaited_once_with(CronJobTarget("daily"))
    scheduler.create_job.assert_awaited_once_with(CronJobMutation({"name": "Daily"}))
    scheduler.update_job.assert_awaited_once_with(
        CronJobMutation({"enabled": False}, "daily")
    )


@pytest.mark.asyncio
async def test_cron_add_and_create_share_one_application_implementation() -> None:
    scheduler, subscriptions = _ports()
    adapter = GatewayCronSchedulerAdapter(scheduler, subscriptions)

    await adapter.create({"name": "Daily"})
    await adapter.create({"name": "Weekly"})

    assert scheduler.create_job.await_count == 2
    assert scheduler.update_job.await_count == 0


@pytest.mark.asyncio
async def test_cron_adapter_preserves_legacy_run_id_and_subscription_topics() -> None:
    scheduler, subscriptions = _ports()
    adapter = GatewayCronSchedulerAdapter(scheduler, subscriptions)

    assert await adapter.runs({"job_id": " daily ", "limit": 3}) == [
        {"id": "run-1", "success": True}
    ]
    await adapter.subscribe({"jobId": " daily "})
    await adapter.unsubscribe(None)

    scheduler.list_runs.assert_awaited_once_with(CronRunQuery("daily", 3))
    subscriptions.subscribe.assert_awaited_once_with(CronTopic("daily"))
    subscriptions.unsubscribe.assert_awaited_once_with(CronTopic())
