"""Scheduler package — cron job management with stagger strategy."""

from .engine import SchedulerEngine
from .ops import SchedulerOps
from .parser import CronExpression, CronParseError, parse_cron
from .persistence import JobStore
from .reaper import SessionReaper
from .stagger import compute_jitter, jitter_for_minute_boundary, spread_jobs
from .timer import SchedulerTimer
from .types import (
    CronJob,
    JobExecution,
    JobReservation,
    JobReservationRejected,
    JobStatus,
    ManualRunResult,
    ManualRunStatus,
    ReservationRejectionReason,
    ScheduleKind,
    SessionTarget,
)

__all__ = [
    "CronExpression",
    "CronJob",
    "CronParseError",
    "JobExecution",
    "JobReservation",
    "JobReservationRejected",
    "JobStatus",
    "JobStore",
    "ManualRunResult",
    "ManualRunStatus",
    "ReservationRejectionReason",
    "ScheduleKind",
    "SchedulerEngine",
    "SchedulerOps",
    "SchedulerTimer",
    "SessionReaper",
    "SessionTarget",
    "compute_jitter",
    "jitter_for_minute_boundary",
    "parse_cron",
    "spread_jobs",
]
