"""In-process execution for disk-backed profile import jobs."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

from opensquilla.memory.profile_import.models import (
    ProfileImportJob,
    ProfileImportJobStatus,
    ProfileImportPreviewRequest,
)


class ProfileImportJobRunner:
    """Run imports in the background while durable state remains service-owned."""

    def __init__(self) -> None:
        self._tasks: dict[tuple[str, str], asyncio.Task[Any]] = {}
        self._services: dict[tuple[str, str], Any] = {}

    def _key(self, service: Any, job_id: str) -> tuple[str, str]:
        return (str(service.paths.state_dir), job_id)

    def _schedule(self, service: Any, job: ProfileImportJob) -> None:
        if job.status is not ProfileImportJobStatus.QUEUED:
            return
        key = self._key(service, job.job_id)
        current = self._tasks.get(key)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(
            service.run_job(job.job_id),
            name=f"profile-import:{service.paths.agent_id}:{job.job_id}",
        )
        self._tasks[key] = task
        self._services[key] = service

        def finish(done: asyncio.Task[Any]) -> None:
            self._finish(key, done)

        task.add_done_callback(finish)

    def _finish(self, key: tuple[str, str], task: asyncio.Task[Any]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
            self._services.pop(key, None)
        if task.cancelled():
            return
        with contextlib.suppress(BaseException):
            task.exception()

    async def start(
        self,
        service: Any,
        request: ProfileImportPreviewRequest,
    ) -> ProfileImportJob:
        job = cast(ProfileImportJob, await service.prepare_job(request))
        self._schedule(service, job)
        return job

    async def retry(
        self,
        service: Any,
        job_id: str,
        client_request_id: str,
    ) -> ProfileImportJob:
        job = cast(
            ProfileImportJob,
            await service.retry_job(job_id, client_request_id),
        )
        self._schedule(service, job)
        return job

    async def cancel(self, service: Any, job_id: str) -> ProfileImportJob:
        await service.request_cancel(job_id)
        key = self._key(service, job_id)
        task = self._tasks.get(key)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        return cast(ProfileImportJob, await service.finish_cancel(job_id))

    async def wait(self, service: Any, job_id: str) -> ProfileImportJob:
        task = self._tasks.get(self._key(service, job_id))
        if task is not None:
            with contextlib.suppress(Exception):
                await task
        return cast(ProfileImportJob, await service.job_status(job_id))

    async def shutdown(self) -> None:
        """Interrupt all live imports and wait for their durable state transition."""

        active = [
            (key, task, self._services.get(key))
            for key, task in self._tasks.items()
            if not task.done()
        ]
        for _key, task, _service in active:
            task.cancel()
        if active:
            await asyncio.gather(
                *(task for _key, task, _service in active),
                return_exceptions=True,
            )
        for key, _task, service in active:
            if service is not None:
                with contextlib.suppress(Exception):
                    await service.interrupt_job(key[1])


_RUNNERS: dict[int, ProfileImportJobRunner] = {}


def current_profile_import_job_runner() -> ProfileImportJobRunner:
    """Return one runner per event loop without coupling jobs to an RPC connection."""

    loop = asyncio.get_running_loop()
    return _RUNNERS.setdefault(id(loop), ProfileImportJobRunner())


async def shutdown_current_profile_import_job_runner() -> None:
    """Close and forget the runner owned by the current Gateway event loop."""

    loop = asyncio.get_running_loop()
    runner = _RUNNERS.pop(id(loop), None)
    if runner is not None:
        await runner.shutdown()
