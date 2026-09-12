"""Gateway Adapter for the scheduled-job Application boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from opensquilla.application.cron_scheduler import (
    CronJobMutation,
    CronJobMutationValues,
    CronListQuery,
    CronRunQuery,
    CronScheduler,
    CronSchedulerPort,
    CronSubscriptionPort,
    CronSubscriptions,
)


class GatewayCronSchedulerAdapter:
    def __init__(
        self,
        scheduler: CronSchedulerPort,
        subscriptions: CronSubscriptionPort,
    ) -> None:
        self._scheduler = CronScheduler(scheduler)
        self._subscriptions = CronSubscriptions(subscriptions)

    async def list_jobs(self, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        raw = params if isinstance(params, dict) else {}
        rows = await self._scheduler.list_jobs(CronListQuery(cast(str | None, raw.get("agentId"))))
        return [dict(row) for row in rows]

    async def status(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._scheduler.get_job(self._id(params)))

    async def create(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        return dict(
            await self._scheduler.create_job(CronJobMutation(cast(CronJobMutationValues, raw)))
        )

    async def update(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        return dict(
            await self._scheduler.update_job(
                CronJobMutation(
                    cast(
                        CronJobMutationValues,
                        {key: value for key, value in raw.items() if key != "id"},
                    ),
                    self._id(raw),
                )
            )
        )

    async def remove(self, params: dict[str, Any] | None) -> None:
        await self._scheduler.remove_job(self._id(params))

    async def run(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._scheduler.run_job(self._id(params)))

    async def runs(self, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        raw = params if isinstance(params, dict) else {}
        job_id = raw.get("id") or raw.get("job_id")
        limit = raw.get("limit", 20)
        rows = await self._scheduler.list_runs(CronRunQuery(cast(str, job_id), int(limit)))
        return [dict(row) for row in rows]

    async def subscribe(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        return dict(await self._subscriptions.subscribe(cast(str | None, raw.get("jobId"))))

    async def unsubscribe(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        return dict(await self._subscriptions.unsubscribe(cast(str | None, raw.get("jobId"))))

    @staticmethod
    def _id(params: Mapping[str, Any] | None) -> str:
        return cast(str, (params or {}).get("id"))


__all__ = ["GatewayCronSchedulerAdapter"]
