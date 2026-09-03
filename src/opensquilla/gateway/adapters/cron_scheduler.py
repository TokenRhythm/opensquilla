"""Gateway Adapter for the scheduled-job Application boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from opensquilla.application.cron_scheduler import (
    CronJobMutation,
    CronJobTarget,
    CronListQuery,
    CronRunQuery,
    CronScheduler,
    CronSchedulerPort,
    CronSubscriptionPort,
    CronSubscriptions,
    CronTopic,
)
from opensquilla.gateway.rpc import RpcContext

type CronExecutor = Callable[[dict[str, Any] | None, RpcContext], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class GatewayCronCallbacks:
    list_jobs: CronExecutor
    status: CronExecutor
    create: CronExecutor
    update: CronExecutor
    remove: CronExecutor
    run: CronExecutor
    runs: CronExecutor
    subscribe: CronExecutor
    unsubscribe: CronExecutor


class GatewayCronSchedulerRuntime(CronSchedulerPort):
    def __init__(self, context: RpcContext, callbacks: GatewayCronCallbacks) -> None:
        self._context = context
        self._callbacks = callbacks

    async def list_jobs(self, query: CronListQuery) -> Sequence[Mapping[str, Any]]:
        result = await self._callbacks.list_jobs(
            {"agentId": query.agent_id} if query.agent_id else None,
            self._context,
        )
        return result if isinstance(result, list) else ()

    async def get_job(self, target: CronJobTarget) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], await self._callbacks.status(
            {"id": target.job_id}, self._context
        ))

    async def create_job(self, command: CronJobMutation) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], await self._callbacks.create(
            dict(command.values), self._context
        ))

    async def update_job(self, command: CronJobMutation) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], await self._callbacks.update(
            {**dict(command.values), "id": command.job_id}, self._context
        ))

    async def remove_job(self, target: CronJobTarget) -> None:
        await self._callbacks.remove({"id": target.job_id}, self._context)

    async def run_job(self, target: CronJobTarget) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], await self._callbacks.run(
            {"id": target.job_id}, self._context
        ))

    async def list_runs(self, query: CronRunQuery) -> Sequence[Mapping[str, Any]]:
        result = await self._callbacks.runs(
            {"id": query.job_id, "limit": query.limit}, self._context
        )
        return result if isinstance(result, list) else ()


class GatewayCronSubscriptionRuntime(CronSubscriptionPort):
    def __init__(self, context: RpcContext, callbacks: GatewayCronCallbacks) -> None:
        self._context = context
        self._callbacks = callbacks

    @staticmethod
    def _params(topic: CronTopic) -> dict[str, Any] | None:
        return {"jobId": topic.job_id} if topic.job_id else None

    async def subscribe(self, topic: CronTopic) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], await self._callbacks.subscribe(
            self._params(topic), self._context
        ))

    async def unsubscribe(self, topic: CronTopic) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], await self._callbacks.unsubscribe(
            self._params(topic), self._context
        ))


class GatewayCronSchedulerAdapter:
    def __init__(self, context: RpcContext, callbacks: GatewayCronCallbacks) -> None:
        self._scheduler = CronScheduler(GatewayCronSchedulerRuntime(context, callbacks))
        self._subscriptions = CronSubscriptions(
            GatewayCronSubscriptionRuntime(context, callbacks)
        )

    async def list_jobs(self, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        raw = params if isinstance(params, dict) else {}
        rows = await self._scheduler.list_jobs(
            CronListQuery(cast(str | None, raw.get("agentId")))
        )
        return [dict(row) for row in rows]

    async def status(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._scheduler.get_job(self._id(params)))

    async def create(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        return dict(await self._scheduler.create_job(CronJobMutation(raw)))

    async def update(self, params: dict[str, Any] | None) -> dict[str, Any]:
        raw = params if isinstance(params, dict) else {}
        return dict(await self._scheduler.update_job(
            CronJobMutation(
                {key: value for key, value in raw.items() if key != "id"},
                self._id(raw),
            )
        ))

    async def remove(self, params: dict[str, Any] | None) -> None:
        await self._scheduler.remove_job(self._id(params))

    async def run(self, params: dict[str, Any] | None) -> dict[str, Any]:
        return dict(await self._scheduler.run_job(self._id(params)))

    async def runs(self, params: dict[str, Any] | None) -> list[dict[str, Any]]:
        raw = params if isinstance(params, dict) else {}
        job_id = raw.get("id") or raw.get("job_id")
        limit = raw.get("limit", 20)
        rows = await self._scheduler.list_runs(
            CronRunQuery(cast(str, job_id), int(limit))
        )
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


__all__ = ["GatewayCronCallbacks", "GatewayCronSchedulerAdapter"]
