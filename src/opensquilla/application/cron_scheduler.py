"""Transport-neutral scheduled-job use cases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CronListQuery:
    agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class CronJobTarget:
    job_id: str


@dataclass(frozen=True, slots=True)
class CronRunQuery:
    job_id: str
    limit: int = 20


@dataclass(frozen=True, slots=True)
class CronJobMutation:
    values: Mapping[str, Any]
    job_id: str | None = None


@dataclass(frozen=True, slots=True)
class CronTopic:
    job_id: str | None = None


class CronSchedulerPort(Protocol):
    async def list_jobs(self, query: CronListQuery) -> Sequence[Mapping[str, Any]]: ...

    async def get_job(self, target: CronJobTarget) -> Mapping[str, Any]: ...

    async def create_job(self, command: CronJobMutation) -> Mapping[str, Any]: ...

    async def update_job(self, command: CronJobMutation) -> Mapping[str, Any]: ...

    async def remove_job(self, target: CronJobTarget) -> None: ...

    async def run_job(self, target: CronJobTarget) -> Mapping[str, Any]: ...

    async def list_runs(self, query: CronRunQuery) -> Sequence[Mapping[str, Any]]: ...


class CronSubscriptionPort(Protocol):
    async def subscribe(self, topic: CronTopic) -> Mapping[str, Any]: ...

    async def unsubscribe(self, topic: CronTopic) -> Mapping[str, Any]: ...


class CronScheduler:
    def __init__(self, port: CronSchedulerPort) -> None:
        self._port = port

    async def list_jobs(self, query: CronListQuery) -> Sequence[Mapping[str, Any]]:
        agent_id = self._optional_text(query.agent_id)
        return await self._port.list_jobs(CronListQuery(agent_id))

    async def get_job(self, job_id: str) -> Mapping[str, Any]:
        return await self._port.get_job(CronJobTarget(self._job_id(job_id)))

    async def create_job(self, command: CronJobMutation) -> Mapping[str, Any]:
        if not isinstance(command.values, Mapping):
            raise ValueError("cron job input must be an object")
        return await self._port.create_job(command)

    async def update_job(self, command: CronJobMutation) -> Mapping[str, Any]:
        return await self._port.update_job(
            CronJobMutation(command.values, self._job_id(command.job_id))
        )

    async def remove_job(self, job_id: str) -> None:
        await self._port.remove_job(CronJobTarget(self._job_id(job_id)))

    async def run_job(self, job_id: str) -> Mapping[str, Any]:
        return await self._port.run_job(CronJobTarget(self._job_id(job_id)))

    async def list_runs(self, query: CronRunQuery) -> Sequence[Mapping[str, Any]]:
        if query.limit < 1:
            raise ValueError("limit must be positive")
        return await self._port.list_runs(
            CronRunQuery(self._job_id(query.job_id), query.limit)
        )

    @staticmethod
    def _job_id(value: str | None) -> str:
        job_id = value.strip() if isinstance(value, str) else ""
        if not job_id:
            raise ValueError("cron job id required")
        return job_id

    @staticmethod
    def _optional_text(value: str | None) -> str | None:
        text = value.strip() if isinstance(value, str) else ""
        return text or None


class CronSubscriptions:
    def __init__(self, port: CronSubscriptionPort) -> None:
        self._port = port

    async def subscribe(self, job_id: str | None = None) -> Mapping[str, Any]:
        return await self._port.subscribe(CronTopic(self._optional_job_id(job_id)))

    async def unsubscribe(self, job_id: str | None = None) -> Mapping[str, Any]:
        return await self._port.unsubscribe(CronTopic(self._optional_job_id(job_id)))

    @staticmethod
    def _optional_job_id(value: str | None) -> str | None:
        text = value.strip() if isinstance(value, str) else ""
        return text or None


__all__ = [
    "CronJobMutation",
    "CronJobTarget",
    "CronListQuery",
    "CronRunQuery",
    "CronScheduler",
    "CronSchedulerPort",
    "CronSubscriptionPort",
    "CronSubscriptions",
    "CronTopic",
]
