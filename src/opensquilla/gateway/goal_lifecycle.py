"""Production lifecycle fan-out for session projection and Goal settlement."""

from __future__ import annotations

from typing import Protocol

import structlog

from opensquilla.gateway.session_lifecycle import TaskLifecycleEvent, TaskLifecycleListener

log = structlog.get_logger(__name__)


class GoalLifecycleObserver(Protocol):
    async def on_task_lifecycle(self, event: TaskLifecycleEvent) -> None: ...


def make_goal_lifecycle_listener(
    session_listener: TaskLifecycleListener,
    goal_service: GoalLifecycleObserver,
) -> TaskLifecycleListener:
    """Keep observer isolation identical in production and persistence tests.

    GoalService owns failed Goal settlement reconciliation. Runtime lifecycle
    retries may invalidate the directory, but must not replay an old task's
    session projection over a successor.
    """

    async def ordered(event: TaskLifecycleEvent) -> None:
        projection_error: Exception | None = None
        try:
            await session_listener(event)
        except Exception as exc:
            projection_error = exc
            log.warning(
                "gateway.session_lifecycle_projection_failed",
                session_key=event.session_key,
                task_id=event.task_id,
                exc_info=True,
            )
        try:
            await goal_service.on_task_lifecycle(event)
        except Exception:
            log.warning(
                "gateway.goal_lifecycle_settlement_failed",
                session_key=event.session_key,
                task_id=event.task_id,
                exc_info=True,
            )
        if projection_error is not None:
            raise projection_error

    return ordered
