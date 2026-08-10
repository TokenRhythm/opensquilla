"""Bounded cleanup for concurrently scheduled DRACO task workers.

The runner owns every worker it schedules.  If one worker (or the result
serialization path) raises, the remaining workers must be cancelled and
awaited before an aborted manifest is published.  This module deliberately
records exception *types* only: provider error text may contain sensitive
request or credential material and does not belong in campaign metadata.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any


class DracoTaskSupervisor:
    """Own a fixed task set and provide idempotent cancel-and-drain cleanup."""

    def __init__(self, tasks: Iterable[asyncio.Task[Any]]) -> None:
        self._tasks = tuple(tasks)
        self._cleanup_complete = False
        self._cancel_requested_count = 0
        self._cancelled_count = 0
        self._cleanup_exception_types: tuple[str, ...] = ()

    @property
    def scheduled_count(self) -> int:
        return len(self._tasks)

    @property
    def pending_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    async def cancel_and_wait(self) -> None:
        """Cancel unfinished workers and retrieve every terminal result."""

        if self._cleanup_complete:
            return
        unfinished = [task for task in self._tasks if not task.done()]
        self._cancel_requested_count = len(unfinished)
        for task in unfinished:
            task.cancel()
        outcomes = await asyncio.gather(*self._tasks, return_exceptions=True)
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
            "model_or_judge_started": bool(self._tasks),
            "cleanup": {
                "complete": self._cleanup_complete,
                "cancel_requested_count": self._cancel_requested_count,
                "cancelled_count": self._cancelled_count,
                "remaining_task_count": self.pending_count,
                "exception_types": list(self._cleanup_exception_types),
            },
        }
