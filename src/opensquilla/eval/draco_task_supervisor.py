"""Bounded cleanup for concurrently scheduled DRACO task workers.

The runner owns every worker it schedules.  If one worker (or the result
serialization path) raises, the remaining workers must be cancelled and
awaited before an aborted manifest is published.  This module deliberately
records exception *types* only: provider error text may contain sensitive
request or credential material and does not belong in campaign metadata.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from typing import Any


class DracoTaskSupervisor:
    """Own a fixed task set and provide idempotent cancel-and-drain cleanup."""

    def __init__(
        self,
        tasks: Iterable[asyncio.Task[Any]],
        *,
        scheduled_count: int | None = None,
    ) -> None:
        initial_tasks = tuple(tasks)
        planned = len(initial_tasks) if scheduled_count is None else scheduled_count
        if (
            isinstance(planned, bool)
            or not isinstance(planned, int)
            or planned < len(initial_tasks)
        ):
            raise ValueError("scheduled_count must cover every initially registered task")
        self._tasks = set(initial_tasks)
        self._scheduled_count = planned
        self._task_started = bool(initial_tasks)
        self._cleanup_complete = False
        self._cancel_requested_count = 0
        self._cancelled_count = 0
        self._cleanup_exception_types: tuple[str, ...] = ()

    @property
    def scheduled_count(self) -> int:
        return self._scheduled_count

    @property
    def pending_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    def register(self, task: asyncio.Task[Any]) -> None:
        if self._cleanup_complete:
            raise RuntimeError("cannot register a task after supervisor cleanup")
        if task in self._tasks:
            raise ValueError("task is already registered")
        self._tasks.add(task)
        self._task_started = True

    def release_completed(self, task: asyncio.Task[Any]) -> None:
        """Drop one successfully consumed result so its payload can be freed."""

        if task not in self._tasks:
            raise ValueError("task is not registered")
        if not task.done() or task.cancelled() or task.exception() is not None:
            raise RuntimeError("only a successful completed task can be released")
        self._tasks.remove(task)

    async def cancel_and_wait(self) -> None:
        """Cancel unfinished workers and retrieve every terminal result."""

        if self._cleanup_complete:
            return
        tasks = tuple(self._tasks)
        unfinished = [task for task in tasks if not task.done()]
        self._cancel_requested_count = len(unfinished)
        for task in unfinished:
            task.cancel()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        self._cancelled_count = sum(
            isinstance(outcome, asyncio.CancelledError) for outcome in outcomes
        )
        self._cleanup_exception_types = tuple(
            sorted(
                {
                    type(outcome).__name__
                    for outcome in outcomes
                    if isinstance(outcome, BaseException)
                    and not isinstance(outcome, asyncio.CancelledError)
                }
            )
        )
        self._tasks.clear()
        self._cleanup_complete = True

    def failure_payload(
        self,
        error: BaseException,
        *,
        rows_written: int,
        stage: str = "task_execution",
    ) -> dict[str, Any]:
        """Return a secret-safe terminal manifest failure record."""

        return {
            "schema": "opensquilla.draco-task-supervisor-failure/v1",
            "stage": stage,
            "exception_type": type(error).__name__,
            "scheduled_task_count": self.scheduled_count,
            "rows_written": max(0, int(rows_written)),
            "model_or_judge_started": self._task_started,
            "cleanup": {
                "complete": self._cleanup_complete,
                "cancel_requested_count": self._cancel_requested_count,
                "cancelled_count": self._cancelled_count,
                "remaining_task_count": self.pending_count,
                "exception_types": list(self._cleanup_exception_types),
            },
        }


TaskFactory = Callable[[], Awaitable[Any]]


class DracoRollingTaskWindow:
    """Run a planned worker set without retaining completed task results.

    New workers are admitted only when the caller explicitly releases a
    successfully consumed task. This couples provider work to durable artifact
    throughput and keeps full row payloads bounded by `max_live_tasks`.
    """

    def __init__(
        self,
        factories: Iterable[TaskFactory],
        *,
        max_live_tasks: int,
    ) -> None:
        if (
            isinstance(max_live_tasks, bool)
            or not isinstance(max_live_tasks, int)
            or max_live_tasks <= 0
        ):
            raise ValueError("max_live_tasks must be a positive integer")
        planned = tuple(factories)
        self._factories = deque(planned)
        self._max_live_tasks = max_live_tasks
        self._live: set[asyncio.Task[Any]] = set()
        self._ready: deque[asyncio.Task[Any]] = deque()
        self._ready_event = asyncio.Event()
        self._supervisor = DracoTaskSupervisor(
            (),
            scheduled_count=len(planned),
        )

    @staticmethod
    async def _invoke(factory: TaskFactory) -> Any:
        return await factory()

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        if task in self._live:
            self._ready.append(task)
            self._ready_event.set()

    def _fill_available_slots(self) -> None:
        while self._factories and len(self._live) < self._max_live_tasks:
            factory = self._factories.popleft()
            task = asyncio.create_task(self._invoke(factory))
            self._live.add(task)
            self._supervisor.register(task)
            task.add_done_callback(self._task_done)

    @property
    def scheduled_count(self) -> int:
        return self._supervisor.scheduled_count

    @property
    def live_task_count(self) -> int:
        return len(self._live)

    @property
    def not_started_count(self) -> int:
        return len(self._factories)

    async def next_completed_task(self) -> asyncio.Task[Any] | None:
        """Return one completed task, leaving ownership with the caller."""

        self._fill_available_slots()
        if self._ready:
            task = self._ready.popleft()
            if not self._ready:
                self._ready_event.clear()
            return task
        if not self._live:
            return None
        await self._ready_event.wait()
        task = self._ready.popleft()
        if not self._ready:
            self._ready_event.clear()
        return task

    def release_completed(self, task: asyncio.Task[Any]) -> None:
        """Release one consumed result; the next call may admit a replacement."""

        if task not in self._live:
            raise ValueError("completed task is not owned by this window")
        self._supervisor.release_completed(task)
        self._live.remove(task)
        try:
            self._ready.remove(task)
        except ValueError:
            pass

    async def cancel_and_wait(self) -> None:
        self._factories.clear()
        self._ready.clear()
        self._ready_event.clear()
        await self._supervisor.cancel_and_wait()
        self._live.clear()
        self._ready.clear()
        self._ready_event.clear()

    def failure_payload(
        self,
        error: BaseException,
        *,
        rows_written: int,
        stage: str = "task_execution",
    ) -> dict[str, Any]:
        return self._supervisor.failure_payload(
            error,
            rows_written=rows_written,
            stage=stage,
        )


__all__ = [
    "DracoRollingTaskWindow",
    "DracoTaskSupervisor",
    "TaskFactory",
]
