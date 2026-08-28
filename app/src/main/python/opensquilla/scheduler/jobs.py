"""Job execution: timeout, backoff, and post-execution state machine."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .parser import parse_cron
from .persistence import JobStore
from .types import CronJob, HandlerResult, JobExecution, JobStatus, ScheduleKind, clear_reservation

logger = logging.getLogger(__name__)


# Module-level notifier for schedule-compute auto-disable. The embedding
# process registers a callable via set_schedule_failure_notifier(); when a job
# is FAILED because its next-run computation crashed repeatedly, the notifier
# is invoked best-effort so the auto-disable is not silent.
ScheduleFailureNotifier = Callable[[CronJob, str], None]
_schedule_failure_notifier: ScheduleFailureNotifier | None = None


def set_schedule_failure_notifier(fn: ScheduleFailureNotifier | None) -> None:
    """Register (or clear) the notifier called when a job auto-disables on schedule error."""
    global _schedule_failure_notifier
    _schedule_failure_notifier = fn


# Module-level dispatcher for delivery.failure_destination alerts. The
# embedding process registers an awaitable callable via
# set_failure_dispatcher(); execute_with_timeout invokes it whenever a job
# execution fails (any handler, any error class) and the job configures a
# FailureDestination. The boot wiring typically connects this to
# DeliveryChain.dispatch_failure_alert so the alert lands on the FD target
# without going through the primary delivery path.
FailureDispatcher = Callable[[CronJob, str], Awaitable[None | str]]
_failure_dispatcher: FailureDispatcher | None = None


def set_failure_dispatcher(fn: FailureDispatcher | None) -> None:
    """Register (or clear) the async dispatcher for failure_destination alerts."""
    global _failure_dispatcher
    _failure_dispatcher = fn


TerminalNotifier = Callable[[CronJob, JobExecution], Awaitable[None | str]]
_terminal_notifier: TerminalNotifier | None = None


def set_terminal_notifier(fn: TerminalNotifier | None) -> None:
    """Register the notifier for the single persisted terminal run result."""
    global _terminal_notifier
    _terminal_notifier = fn


async def notify_terminal_result(job: CronJob, execution: JobExecution) -> None:
    """Best-effort terminal notification after persistence/state finalization."""
    if _terminal_notifier is None:
        return
    try:
        await _terminal_notifier(job, execution)
    except Exception:  # noqa: BLE001 - notifications must not alter job state
        logger.warning("terminal_notifier_raised id=%s", job.id, exc_info=True)


# Error patterns indicating a transient failure (retry-eligible).
# Permanent errors disable the job immediately; transient errors retry with backoff.
_TRANSIENT_ERROR_PATTERNS = (
    "rate limit",
    "too many requests",
    "resource exhausted",
    "429",
    "529",
    "overload",
    "timeout",
    "timed out",
    "econnreset",
    "fetch failed",
    "socket",
    "network",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "502",
    "503",
    "504",
    "cloudflare",
)
_PERMANENT_ERROR_PATTERNS = (
    "unauthorized",
    "invalid api key",
    "invalid_api_key",
    "forbidden",
    "401",
    "403",
    "validation",
    "invalid request",
    "no handler registered",
)


def classify_error(error_text: str | None) -> str:
    """Return 'transient' or 'permanent' for a job error string.

    Defaults to 'transient' on ambiguous text so the existing recurring-job
    backoff schedule remains the safe fallback. Use 'permanent' only when
    the text matches a known auth/config-style signature.
    """
    if not error_text:
        return "transient"
    lowered = error_text.lower()
    for pattern in _PERMANENT_ERROR_PATTERNS:
        if pattern in lowered:
            return "permanent"
    for pattern in _TRANSIENT_ERROR_PATTERNS:
        if pattern in lowered:
            return "transient"
    return "transient"

# Exponential backoff schedule for retryable jobs.
BACKOFF_SCHEDULE: list[int] = [30, 60, 300, 900, 3600]  # 30s, 1m, 5m, 15m, 60m
MAX_CONSECUTIVE_ERRORS: int = 5
_ONE_SHOT_MAX_RETRIES: int = 3

HandlerFn = Callable[[CronJob], Awaitable[HandlerResult | str | tuple | None]]


def compute_backoff(consecutive_errors: int) -> float:
    """Return backoff delay in seconds for the given consecutive error count.

    Returns 0.0 for zero errors, otherwise indexes into BACKOFF_SCHEDULE
    (capped at the last entry).
    """
    if consecutive_errors <= 0:
        return 0.0
    idx = min(consecutive_errors - 1, len(BACKOFF_SCHEDULE) - 1)
    return float(BACKOFF_SCHEDULE[idx])


async def execute_with_timeout(job: CronJob, handler: HandlerFn) -> JobExecution:
    """Wrap a handler call with asyncio timeout, returning a JobExecution record.

    The handler may return:
    - str: treated as summary text
    - (str, str): treated as (summary, session_key)
    - None: no summary
    """
    execution = JobExecution(job_id=job.id, started_at=datetime.now(UTC))
    try:
        task = handler(job)
        if job.timeout_seconds <= 0:
            result = await task
        else:
            result = await asyncio.wait_for(task, timeout=job.timeout_seconds)
        execution.success = True
        if isinstance(result, HandlerResult):
            execution.summary = result.summary[:500] if result.summary else None
            execution.session_key = result.session_key
            execution.delivery_status = result.delivery_status
        elif isinstance(result, tuple) and len(result) >= 2:
            execution.summary = result[0][:500] if isinstance(result[0], str) else None
            execution.session_key = result[1] or ""
            if len(result) >= 3:
                execution.delivery_status = result[2] or ""
        elif isinstance(result, str):
            execution.summary = result[:500]
        else:
            execution.summary = None
    except TimeoutError:
        execution.success = False
        execution.error = f"Timeout after {job.timeout_seconds}s"
        logger.warning("job_timeout id=%s timeout=%.1f", job.id, job.timeout_seconds)
    except Exception as exc:
        execution.success = False
        execution.error = str(exc)
        logger.exception("job_error id=%s", job.id)
    finally:
        execution.finished_at = datetime.now(UTC)

    # Failure-destination alert: any failed handler run (agent_run raise,
    # system_event raise, timeout, generic exception) routes its error to the
    # job's configured FailureDestination, if any. This is the single dispatch
    # site so all failure paths reach the FD uniformly.
    if (
        not execution.success
        and job.delivery.failure_destination is not None
        and _failure_dispatcher is not None
    ):
        try:
            await _failure_dispatcher(job, execution.error or "")
        except Exception:  # noqa: BLE001 — dispatcher is best-effort
            logger.warning(
                "failure_dispatcher_raised id=%s", job.id, exc_info=True
            )

    return execution


def _next_run(job: CronJob, after: datetime) -> datetime:
    """Compute the next execution time for a recurring job after *after*.

    When ``job.tz`` is a valid IANA name, cron field matching evaluates the
    wall time in that zone instead of UTC. The returned datetime is in UTC so
    callers can compare it against other UTC instants directly.

    EVERY+interval schedules align to ``job.anchor_at`` when set:
    next_run = anchor + ceil((after-anchor)/interval) * interval. This keeps
    fire times stable across restarts and slow ticks.
    """
    if job.schedule_kind == ScheduleKind.EVERY and job.cron_expr.isdigit():
        interval_seconds = int(job.cron_expr)
        if job.anchor_at is not None:
            anchor = job.anchor_at
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=UTC)
            elapsed = (after - anchor).total_seconds()
            if elapsed < 0:
                return anchor
            # Smallest positive integer k such that anchor + k*interval > after.
            steps = int(elapsed // interval_seconds) + 1
            return anchor + timedelta(seconds=steps * interval_seconds)
        return after + timedelta(seconds=interval_seconds)

    # Standard cron: scan forward minute-by-minute
    expr = parse_cron(job.cron_expr)
    tz_name = (job.tz or "").strip()
    tz = ZoneInfo(tz_name) if tz_name else None
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(2_102_400):
        wall = candidate.astimezone(tz) if tz is not None else candidate
        if expr.matches(wall):
            return candidate + timedelta(seconds=job.jitter_seconds)
        candidate += timedelta(minutes=1)
    raise ValueError(f"No valid next run found for expression '{job.cron_expr}'")


async def apply_result(job: CronJob, execution: JobExecution, store: JobStore) -> None:
    """Compatibility wrapper for tests and non-reserved state transitions.

    Production scheduler execution should use ``apply_reserved_result`` so the
    caller proves ownership of a persisted reservation token.
    """
    current = await store.get(job.id)
    if current is None:
        return
    if current.status in (JobStatus.PAUSED, JobStatus.DISABLED):
        return

    delete_job = _apply_result_state(current, execution, datetime.now(UTC))
    if delete_job:
        await store.delete(current.id)
    else:
        await store.save(current)


async def apply_reserved_result(
    job_id: str,
    reservation_token: str,
    execution: JobExecution,
    store: JobStore,
) -> bool:
    """Apply an execution result only when the reservation token still owns the job."""
    current = await store.get(job_id)
    if current is None:
        return False
    if current.reservation_token != reservation_token:
        return False
    if current.status in (JobStatus.PAUSED, JobStatus.DISABLED):
        clear_reservation(current)
        await store.save(current)
        return True

    delete_job = _apply_result_state(current, execution, datetime.now(UTC))
    if delete_job:
        await store.delete(current.id)
    else:
        await store.save(current)
    return True


def _apply_result_state(job: CronJob, execution: JobExecution, now: datetime) -> bool:
    """Post-execution state machine: update job state based on execution outcome.

    Handles:
    - Deleted/paused while running (no-op)
    - Success: reset error counters, reschedule or disable/delete
    - Failure: increment counters, backoff, mark FAILED or DISABLED at thresholds

    Returns True when the job should be deleted.
    """
    if execution.success:
        job.consecutive_errors = 0
        job.backoff_until = None
        job.last_error = None
        job.run_count += 1
        job.updated_at = now

        if job.schedule_kind == ScheduleKind.AT:
            clear_reservation(job)
            if job.delete_after_run:
                return True
            else:
                job.status = JobStatus.DISABLED
                job.enabled = False
        else:
            try:
                job.next_run_at = _next_run(job, now)
                job.status = JobStatus.PENDING
            except Exception as exc:
                _mark_schedule_compute_failed(job, exc, now, increment_error=True)
            else:
                clear_reservation(job)

    else:
        job.consecutive_errors += 1
        job.error_count += 1
        job.last_error = execution.error
        job.run_count += 1
        job.updated_at = now

        classification = classify_error(execution.error)
        if classification == "permanent":
            # Auth/config errors disable immediately rather than cycling through
            # transient-error backoff. One-shot AT jobs already short-circuit on
            # permanent errors via _ONE_SHOT_MAX_RETRIES; for recurring jobs we
            # eagerly disable to avoid burning retries on something only a human
            # can fix.
            job.status = JobStatus.DISABLED
            job.enabled = False
            job.next_run_at = None
            job.backoff_until = None
            clear_reservation(job)
            logger.error(
                "job_permanent_error_disabled id=%s reason=%s",
                job.id,
                (execution.error or "")[:120],
            )
            return False

        is_recurring = job.schedule_kind in (ScheduleKind.CRON, ScheduleKind.EVERY)
        if is_recurring:
            if job.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                job.status = JobStatus.FAILED
                job.next_run_at = None
                job.backoff_until = None
                clear_reservation(job)
                logger.error(
                    "job_failed_permanently id=%s consecutive_errors=%d",
                    job.id,
                    job.consecutive_errors,
                )
            else:
                backoff_secs = compute_backoff(job.consecutive_errors)
                try:
                    job.next_run_at = _next_run(job, now)
                except Exception as exc:
                    _mark_schedule_compute_failed(job, exc, now, increment_error=False)
                else:
                    job.backoff_until = now + timedelta(seconds=backoff_secs)
                    job.status = JobStatus.PENDING
                    clear_reservation(job)
        else:
            # One-shot AT job
            if job.consecutive_errors >= _ONE_SHOT_MAX_RETRIES:
                job.status = JobStatus.DISABLED
                job.enabled = False
                clear_reservation(job)
                logger.error(
                    "job_one_shot_disabled id=%s consecutive_errors=%d",
                    job.id,
                    job.consecutive_errors,
                )
            else:
                backoff_secs = compute_backoff(job.consecutive_errors)
                job.backoff_until = now + timedelta(seconds=backoff_secs)
                job.status = JobStatus.PENDING
                clear_reservation(job)

    return False


def _mark_schedule_compute_failed(
    job: CronJob,
    exc: Exception,
    now: datetime,
    *,
    increment_error: bool,
) -> None:
    if increment_error:
        job.error_count += 1
        job.consecutive_errors += 1
    job.status = JobStatus.FAILED
    job.last_error = f"schedule compute failed: {exc}"
    job.updated_at = now
    job.backoff_until = None
    job.next_run_at = None
    clear_reservation(job)
    notifier = _schedule_failure_notifier
    if notifier is not None:
        try:
            notifier(job, str(exc))
        except Exception:  # noqa: BLE001 — notifier is best-effort
            logger.warning("schedule_failure_notifier_raised id=%s", job.id, exc_info=True)
