"""Scheduler engine: thin facade delegating to ops, timer, reaper, and jobs."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .jobs import HandlerFn, apply_reserved_result, execute_with_timeout
from .ops import SchedulerOps
from .parser import CronExpression
from .persistence import JobStore
from .reaper import SessionReaper
from .timer import SchedulerTimer
from .types import (
    CronJob,
    CronWakeMode,
    DeliveryConfig,
    JobExecution,
    JobReservationRejected,
    ManualRunResult,
    ManualRunStatus,
    ReservationRejectionReason,
    ScheduleKind,
    SessionTarget,
)

logger = logging.getLogger(__name__)


def _next_run(expr: CronExpression, after: datetime, jitter: float = 0.0) -> datetime:
    """Find the next datetime >= after+1min that matches the cron expression.

    Kept for backward compatibility with rpc_cron.py. New code should use
    ``jobs._next_run(job, after)`` instead.
    """
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(2_102_400):
        if expr.matches(candidate):
            return candidate + timedelta(seconds=jitter)
        candidate += timedelta(minutes=1)
    raise ValueError(f"No valid next run found for expression '{expr.raw}'")


class SchedulerEngine:
    """Facade over the scheduler subsystem.

    Delegates to:
    - SchedulerOps   — CRUD + validation
    - SchedulerTimer — tick loop, burst protection, catchup
    - SessionReaper  — expired session cleanup
    """

    def __init__(
        self,
        store: JobStore,
        session_store=None,
        config: dict | None = None,
        # Legacy kwargs for backward compatibility
        tick_interval: float | None = None,
        max_jitter_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        cfg = config or {}

        # Legacy kwargs override config dict
        if max_jitter_seconds is not None:
            max_jitter = max_jitter_seconds
        else:
            max_jitter = cfg.get("max_jitter", 30.0)

        self._store = store
        self._ops = SchedulerOps(store, max_jitter=max_jitter, clock=clock)

        self._reaper: SessionReaper | None = None
        if session_store is not None:
            self._reaper = SessionReaper(
                session_store,
                retention_seconds=cfg.get("session_retention", 86400),
            )

        self._timer = SchedulerTimer(
            store=store,
            handlers={},
            max_concurrent=cfg.get("max_concurrent_runs", 3),
            max_catchup=cfg.get("max_catchup_jobs", 5),
            reaper=self._reaper,
        )

    # ------------------------------------------------------------------
    # Handler registry — delegates to timer
    # ------------------------------------------------------------------

    def register_handler(self, key: str, fn: HandlerFn) -> None:
        """Register an async callable for a handler key."""
        self._timer.register_handler(key, fn)

    def handler(self, key: str) -> Callable[[HandlerFn], HandlerFn]:
        """Decorator: @engine.handler('my_key')"""

        def decorator(fn: HandlerFn) -> HandlerFn:
            self.register_handler(key, fn)
            return fn

        return decorator

    # ------------------------------------------------------------------
    # Job CRUD — delegates to ops + nudges timer
    # ------------------------------------------------------------------

    async def add_job(
        self,
        name: str,
        *,
        schedule_kind: ScheduleKind | str,
        schedule_value: str,
        schedule_tz: str = "",
        handler_key: str = "agent_run",
        payload: dict | None = None,
        session_target: SessionTarget = SessionTarget.ISOLATED,
        session_key: str = "",
        timeout_seconds: float = 600.0,
        wake_mode: CronWakeMode | str = CronWakeMode.NOW,
        max_retries: int = 3,
        origin_session_key: str = "",
        delivery: DeliveryConfig | None = None,
        tool_policy: dict[str, Any] | None = None,
        tz: str = "",
        jitter_seconds: float | None = None,
        creator_session_key: str = "",
        creator_sender_id: str = "",
        creator_is_owner: bool = False,
        run_mode: str = "",
        idempotency_key: str = "",
    ) -> CronJob:
        """Create and persist a new job; compute initial next_run_at.

        Schedule contract: ``schedule_kind`` + ``schedule_value`` (and optional
        ``schedule_tz`` for CRON). The value is validated per kind via
        ``parse_cron`` / ``parse_iso_at`` / integer check; no free-form text.
        """
        job = await self._ops.add(
            name=name,
            handler_key=handler_key,
            payload=payload,
            session_target=session_target,
            session_key=session_key,
            timeout_seconds=timeout_seconds,
            wake_mode=wake_mode,
            max_retries=max_retries,
            delivery=delivery,
            origin_session_key=origin_session_key,
            tool_policy=tool_policy,
            tz=tz,
            jitter_seconds=jitter_seconds,
            creator_session_key=creator_session_key,
            creator_sender_id=creator_sender_id,
            creator_is_owner=creator_is_owner,
            run_mode=run_mode,
            idempotency_key=idempotency_key,
            schedule_kind=schedule_kind,
            schedule_value=schedule_value,
            schedule_tz=schedule_tz,
        )
        self._timer.nudge()
        return job

    async def update_job(self, job_id: str, **patch) -> CronJob | None:
        """Apply a partial update to an existing job.

        Schedule patch: ``schedule_kind`` + ``schedule_value`` (and optional
        ``schedule_tz``).
        """
        result = await self._ops.update(job_id, **patch)
        if result is not None:
            self._timer.nudge()
        return result

    async def pause_job(self, job_id: str) -> CronJob | None:
        """Pause a job."""
        return await self._ops.pause(job_id)

    async def resume_job(self, job_id: str) -> CronJob | None:
        """Resume a paused job."""
        result = await self._ops.resume(job_id)
        if result is not None:
            self._timer.nudge()
        return result

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job and cancel it if running."""
        self._timer.cancel_running(job_id)
        result = await self._ops.remove(job_id)
        if result:
            self._timer.nudge()
        return result

    async def remove_job(self, job_id: str) -> bool:
        """Alias for delete_job (used by admin tool)."""
        return await self.delete_job(job_id)

    async def run_job_now(self, job_id: str) -> ManualRunResult:
        """Trigger immediate execution of a job."""
        job = await self._ops.get(job_id)
        if job is None:
            return ManualRunResult(
                status=ManualRunStatus.NOT_FOUND,
                reason=ReservationRejectionReason.NOT_FOUND.value,
                error="Cron job not found",
            )
        handler = self._timer._handlers.get(job.handler_key)
        if handler is None:
            return ManualRunResult(
                status=ManualRunStatus.NO_HANDLER,
                reason="no_handler",
                error=f"No handler registered for key '{job.handler_key}'",
                current_status=getattr(job.status, "value", str(job.status)),
            )
        reservation = await self._store.reserve_manual_job(
            job_id,
            datetime.now(UTC),
            source="manual",
            owner="scheduler-manual",
        )
        if isinstance(reservation, JobReservationRejected):
            return _manual_result_from_rejection(reservation)
        exe = await execute_with_timeout(job, handler)
        await self._store.save_execution(exe)
        await apply_reserved_result(job.id, reservation.token, exe, self._store)
        return ManualRunResult(status=ManualRunStatus.ACCEPTED, execution=exe)

    # ------------------------------------------------------------------
    # Query — delegates to ops
    # ------------------------------------------------------------------

    async def list_jobs(self) -> list[CronJob]:
        """Return all non-deleted jobs."""
        return await self._ops.list_all()

    async def get_job(self, job_id: str) -> CronJob | None:
        """Retrieve a job by ID."""
        return await self._ops.get(job_id)

    async def get_runs(self, job_id: str, limit: int = 20) -> list[JobExecution]:
        """Return recent execution records for a job."""
        return await self._ops.get_runs(job_id, limit)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the scheduler: run catchup then begin the tick loop."""
        await self._timer.startup_catchup()
        await self._timer.start()

    async def stop(self) -> None:
        """Stop the scheduler tick loop and cancel all running tasks."""
        await self._timer.stop()

    async def __aenter__(self) -> SchedulerEngine:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()


def _manual_result_from_rejection(rejection: JobReservationRejected) -> ManualRunResult:
    job = rejection.job
    current_status = getattr(job.status, "value", str(job.status)) if job else ""
    reason = rejection.reason.value
    backoff_until = job.backoff_until if job else None
    if rejection.reason == ReservationRejectionReason.NOT_FOUND:
        status = ManualRunStatus.NOT_FOUND
        error = "Cron job not found"
    elif rejection.reason == ReservationRejectionReason.BUSY:
        status = ManualRunStatus.BUSY
        error = "Cron job is already running"
    elif rejection.reason == ReservationRejectionReason.DISABLED:
        status = ManualRunStatus.DISABLED
        error = "Cron job is disabled"
    else:
        status = ManualRunStatus.BLOCKED
        error = f"Cron job cannot run now: {reason}"
    return ManualRunResult(
        status=status,
        reason=reason,
        error=error,
        current_status=current_status,
        backoff_until=backoff_until,
    )
