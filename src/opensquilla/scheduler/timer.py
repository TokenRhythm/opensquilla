"""Scheduler tick loop — burst protection, startup catchup, precise timing, nudge."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from .jobs import (
    HandlerFn,
    _next_run,
    apply_reserved_result,
    execute_with_timeout,
    notify_terminal_result,
)
from .persistence import JobStore
from .stagger import spread_jobs
from .types import (
    CronJob,
    JobExecution,
    JobReservation,
    JobStatus,
    ScheduleKind,
    clear_reservation,
)

logger = logging.getLogger(__name__)

MIN_REFIRE_GAP_SECONDS = 2.0


class SchedulerTimer:
    """The scheduler's tick loop — heart of execution orchestration.

    Handles burst protection (max_concurrent), startup catchup with stagger,
    precise sleep-to-next-due timing, and nudge-based wake.
    """

    MAX_TIMER_DELAY = 60.0
    CATCHUP_STAGGER_SECONDS = 10.0

    def __init__(
        self,
        store: JobStore,
        handlers: dict[str, HandlerFn],
        max_concurrent: int = 3,
        max_catchup: int = 5,
        reaper: object | None = None,
    ) -> None:
        self._store = store
        self._handlers = handlers
        self._max_concurrent = max_concurrent
        self._max_catchup = max_catchup
        self._reaper = reaper
        self._running: dict[str, asyncio.Task] = {}
        self._loop_task: asyncio.Task | None = None
        self._started = False
        self._nudge_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Handler registry
    # ------------------------------------------------------------------

    def register_handler(self, key: str, fn: HandlerFn) -> None:
        """Add a handler to the handlers dict."""
        self._handlers[key] = fn

    # ------------------------------------------------------------------
    # Startup catchup
    # ------------------------------------------------------------------

    async def startup_catchup(self) -> None:
        """Called once at start to recover from downtime.

        1. Clear reservations left by the previous process; reset RUNNING to PENDING
        2. Collect missed (overdue) jobs
        3. First max_catchup: run with stagger
        4. Remaining: fast-forward next_run_at (except AT one-shots)
        """
        now = datetime.now(UTC)

        # Step 1: recover reservations left by a previous process. Active
        # executions cannot survive a restart, but pause/resume may have kept
        # their reservation while preserving the requested lifecycle state.
        recovered_ids: set[str] = set()
        for job in await self._store.list_active():
            was_running = job.status == JobStatus.RUNNING
            if not was_running and not job.reservation_token:
                continue
            if was_running:
                recovered_ids.add(job.id)
                job.status = JobStatus.PENDING
            job.updated_at = now
            clear_reservation(job)
            await self._store.save(job)
            logger.info(
                "startup_reset_stale id=%s name=%s previous_status=%s",
                job.id,
                job.name,
                JobStatus.RUNNING.value if was_running else job.status.value,
            )

        # Step 2: collect missed jobs
        missed: list[CronJob] = []
        async for job in self._store.iter_due(now):
            if job.id in recovered_ids:
                logger.info("startup_skip_recovered_running id=%s", job.id)
                continue
            missed.append(job)

        if not missed:
            return

        # Step 3: first max_catchup jobs — run with stagger
        catchup_jobs = missed[: self._max_catchup]
        remaining_jobs = missed[self._max_catchup :]

        if catchup_jobs:
            reservations: list[JobReservation] = []
            for job in catchup_jobs:
                reservation = await self._store.reserve_due_job(
                    job.id,
                    now,
                    source="startup",
                    owner="scheduler-startup",
                )
                if isinstance(reservation, JobReservation):
                    reservations.append(reservation)
                else:
                    logger.info(
                        "startup_reservation_skipped id=%s reason=%s",
                        job.id,
                        reservation.reason,
                    )

            delays = spread_jobs(
                [r.job.id for r in reservations],
                window_seconds=self.CATCHUP_STAGGER_SECONDS,
            )

            for reservation in reservations:
                delay = delays.get(reservation.job.id, 0.0)
                task = asyncio.create_task(self._staggered_run(reservation, delay))
                self._running[reservation.job.id] = task

        # Step 4: fast-forward remaining (except AT one-shots). Delegates to
        # the shared next-run helper so EVERY+anchor jobs use the interval-grid
        # ceil math and CRON jobs use the field-matching scan with tz support.
        for job in remaining_jobs:
            if job.schedule_kind == ScheduleKind.AT:
                # Leave AT jobs as-is — they'll be picked up on next tick
                continue
            try:
                job.next_run_at = _next_run(job, now)
                job.updated_at = now
                await self._store.save(job)
                logger.info("startup_fast_forward id=%s next_run=%s", job.id, job.next_run_at)
            except Exception:
                logger.exception("startup_fast_forward_error id=%s", job.id)

    async def _staggered_run(self, reservation: JobReservation, delay: float) -> None:
        """Run a catchup job after a stagger delay."""
        if not isinstance(reservation, JobReservation):
            raise TypeError("_staggered_run requires a JobReservation")
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._run_single(reservation)
        except asyncio.CancelledError:
            # Cancellation can happen during the stagger sleep, before
            # _run_single has a chance to own cleanup.
            await self._store.release_reservation(reservation.job.id, reservation.token)
            raise

    # ------------------------------------------------------------------
    # Tick loop
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """Main tick: collect due jobs, enforce burst limit, launch tasks."""
        now = datetime.now(UTC)

        # Collect due jobs
        due_jobs: list[CronJob] = []
        async for job in self._store.iter_due(now):
            due_jobs.append(job)

        # Refire gap guard — skip jobs that ran within MIN_REFIRE_GAP_SECONDS
        # (primarily protects against nudge-triggered rapid re-execution)
        refire_cutoff = now - timedelta(seconds=MIN_REFIRE_GAP_SECONDS)
        filtered = []
        for job in due_jobs:
            if job.last_run_at and job.last_run_at > refire_cutoff:
                logger.warning("refire_gap_skip id=%s last_run=%s", job.id, job.last_run_at)
                continue
            filtered.append(job)
        due_jobs = filtered

        # Clean up finished tasks from _running
        finished_ids = [jid for jid, task in self._running.items() if task.done()]
        for jid in finished_ids:
            del self._running[jid]

        # Calculate available slots
        active_count = len(self._running)
        available_slots = self._max_concurrent - active_count
        if available_slots <= 0:
            if due_jobs:
                logger.warning(
                    "tick_no_slots due=%d running=%d max=%d",
                    len(due_jobs),
                    active_count,
                    self._max_concurrent,
                )
            return

        # Take first available_slots jobs, skip rest
        to_run = due_jobs[:available_slots]
        throttled = due_jobs[available_slots:]

        if throttled:
            logger.warning(
                "tick_throttled count=%d (max_concurrent=%d)",
                len(throttled),
                self._max_concurrent,
            )

        if to_run:
            for job in to_run:
                reservation = await self._store.reserve_due_job(
                    job.id,
                    now,
                    source="timer",
                    owner="scheduler-timer",
                )
                if not isinstance(reservation, JobReservation):
                    logger.info(
                        "tick_reservation_skipped id=%s reason=%s",
                        job.id,
                        reservation.reason,
                    )
                    continue
                task = asyncio.create_task(self._run_single(reservation))
                self._running[job.id] = task

    async def _run_single(self, reservation: JobReservation) -> None:
        """Execute one job: handler lookup, timeout, persist execution, apply result."""
        if not isinstance(reservation, JobReservation):
            raise TypeError("_run_single requires a JobReservation")
        job = reservation.job
        try:
            handler = self._handlers.get(job.handler_key)

            if handler is None:
                error = f"No handler registered for key '{job.handler_key}'"
                await self._store.finalize_reserved_missing_handler(
                    job.id,
                    reservation.token,
                    error=error,
                )
                finished_at = datetime.now(UTC)
                exe = JobExecution(
                    job_id=job.id,
                    started_at=finished_at,
                    finished_at=finished_at,
                    success=False,
                    error=error,
                )
                await self._store.save_execution(exe)
                await notify_terminal_result(job, exe)
                return

            exe = await execute_with_timeout(job, handler)
            await self._store.save_execution(exe)
            await apply_reserved_result(job.id, reservation.token, exe, self._store)
            await notify_terminal_result(job, exe)
        except asyncio.CancelledError:
            # A paused/deleted job must not retain ownership of a persisted
            # reservation after its execution task has been cancelled.
            await self._store.release_reservation(job.id, reservation.token)
            raise

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        """Main loop: sleep until next due, tick, reap, repeat."""
        while self._started:
            try:
                next_due = await self._store.next_due_at()
                now = datetime.now(UTC)

                if next_due is not None:
                    delay = (next_due - now).total_seconds()
                    delay = max(0.1, min(delay, self.MAX_TIMER_DELAY))
                else:
                    delay = self.MAX_TIMER_DELAY

                # Wait on nudge event with timeout — nudge wakes us early
                self._nudge_event.clear()
                try:
                    await asyncio.wait_for(self._nudge_event.wait(), timeout=delay)
                except TimeoutError:
                    pass

                await self._tick()

                # Reap if reaper exists
                if self._reaper is not None and hasattr(self._reaper, "maybe_reap"):
                    await self._reaper.maybe_reap()

            except Exception:
                logger.exception("scheduler_loop_error")
                await asyncio.sleep(5.0)

    # ------------------------------------------------------------------
    # Nudge
    # ------------------------------------------------------------------

    def nudge(self) -> None:
        """Wake the loop immediately (e.g. after adding a new job)."""
        self._nudge_event.set()

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    async def cancel_running(self, job_id: str) -> bool:
        """Cancel and await a running task so reservation cleanup completes."""
        task = self._running.get(job_id)
        if task is None:
            return False
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Pausing/deleting must still progress if the task had already
            # terminated abnormally; SchedulerOps will clear the reservation.
            logger.exception("cancel_running_task_failed id=%s", job_id)
        finally:
            if self._running.get(job_id) is task:
                self._running.pop(job_id, None)
        return True

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the timer loop."""
        if self._started:
            return
        self._started = True
        self._loop_task = asyncio.create_task(self._loop())
        logger.info("scheduler_timer_started")

    async def stop(self) -> None:
        """Stop the timer loop and cancel all running tasks."""
        self._started = False
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        # Cancel all running job tasks
        running = list(self._running.values())
        for task in running:
            if not task.done():
                task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self._running.clear()
        logger.info("scheduler_timer_stopped")
