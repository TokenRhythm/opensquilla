"""Per-job stagger override + retry transient/permanent classification.

* ``--exact`` / ``jitterSeconds`` lets a caller override the auto-jitter so
  jobs can fire on the exact second (e.g. scheduled reports).
* Retry classification: permanent errors (auth/validation) disable the job
  immediately instead of burning the full recurring-job backoff schedule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.rpc_cron import (
    _cron_create_contract as _handle_cron_add,
)
from opensquilla.scheduler.jobs import (
    _apply_result_state,
    classify_error,
)
from opensquilla.scheduler.ops import SchedulerOps
from opensquilla.scheduler.payloads import (
    AGENT_TURN_KIND,
    make_agent_turn_payload,
)
from opensquilla.scheduler.persistence import JobStore
from opensquilla.scheduler.types import (
    CronJob,
    DeliveryConfig,
    JobExecution,
    JobStatus,
    ScheduleKind,
    SessionTarget,
)

# --- exact / jitter override ---------------------------------------------


async def test_ops_add_with_jitter_zero_yields_no_jitter(tmp_path: Path) -> None:
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store)
        job = await ops.add(
            name="exact",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="0 9 * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("x"),
            session_target=SessionTarget.ISOLATED,
            jitter_seconds=0,
        )
        assert job.jitter_seconds == 0.0
    finally:
        await store.close()


async def test_ops_add_with_explicit_jitter_uses_that_value(tmp_path: Path) -> None:
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store)
        job = await ops.add(
            name="stag",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="0 9 * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("x"),
            session_target=SessionTarget.ISOLATED,
            jitter_seconds=15.5,
        )
        assert job.jitter_seconds == 15.5
    finally:
        await store.close()


async def test_ops_add_with_none_jitter_uses_auto_compute(tmp_path: Path) -> None:
    """Legacy behaviour preserved when caller does not pass jitter_seconds."""
    db = tmp_path / "cron.db"
    store = JobStore(str(db))
    await store.open()
    try:
        ops = SchedulerOps(store)
        job = await ops.add(
            name="auto",
            schedule_kind=ScheduleKind.CRON,
            schedule_value="0 9 * * *",
            handler_key="agent_run",
            payload=make_agent_turn_payload("x"),
            session_target=SessionTarget.ISOLATED,
        )
        # compute_jitter caps at max_jitter (30s by default).
        assert 0.0 <= job.jitter_seconds <= 30.0
    finally:
        await store.close()


# --- RPC exposes exact/jitter ---------------------------------------------


class _FakeScheduler:
    def __init__(self) -> None:
        self.kwargs: dict | None = None

    async def add_job(self, **kwargs):
        self.kwargs = kwargs
        return CronJob(
            id="job-x",
            name=kwargs["name"],
            cron_expr=kwargs.get("schedule_value") or kwargs.get("schedule_raw", ""),
            schedule_raw=kwargs.get("schedule_value") or kwargs.get("schedule_raw", ""),
            jitter_seconds=kwargs.get("jitter_seconds") or 0.0,
            handler_key=kwargs["handler_key"],
            payload=kwargs["payload"],
            session_target=kwargs["session_target"],
            session_key=kwargs["session_key"],
            origin_session_key=kwargs["origin_session_key"],
            delivery=kwargs.get("delivery") or DeliveryConfig(),
        )

    async def update_job(self, job_id, **patch):
        return None


async def test_rpc_cron_add_exact_flag_zeroes_jitter() -> None:
    scheduler = _FakeScheduler()
    await _handle_cron_add(
        {
            "name": "X",
            "expression": "0 9 * * *",
            "payloadKind": AGENT_TURN_KIND,
            "text": "x",
            "sessionTarget": "isolated",
            "exact": True,
        },
        RpcContext(conn_id="t", cron_scheduler=scheduler),
    )
    assert scheduler.kwargs is not None
    assert scheduler.kwargs["jitter_seconds"] == 0.0


async def test_rpc_cron_add_jitter_seconds_overrides_exact() -> None:
    scheduler = _FakeScheduler()
    await _handle_cron_add(
        {
            "name": "X",
            "expression": "0 9 * * *",
            "payloadKind": AGENT_TURN_KIND,
            "text": "x",
            "sessionTarget": "isolated",
            "exact": True,
            "jitterSeconds": 7,
        },
        RpcContext(conn_id="t", cron_scheduler=scheduler),
    )
    assert scheduler.kwargs["jitter_seconds"] == 7.0


async def test_rpc_cron_add_without_jitter_leaves_auto() -> None:
    scheduler = _FakeScheduler()
    await _handle_cron_add(
        {
            "name": "X",
            "expression": "0 9 * * *",
            "payloadKind": AGENT_TURN_KIND,
            "text": "x",
            "sessionTarget": "isolated",
        },
        RpcContext(conn_id="t", cron_scheduler=scheduler),
    )
    # When neither exact nor jitterSeconds is provided, jitter_seconds passes
    # through as None so the scheduler auto-computes.
    assert scheduler.kwargs["jitter_seconds"] is None


# --- retry classification -------------------------------------------------


def test_classify_error_transient_signatures() -> None:
    assert classify_error("rate limit exceeded") == "transient"
    assert classify_error("429 Too Many Requests") == "transient"
    assert classify_error("anthropic 529 overloaded_error") == "transient"
    assert classify_error("connection timeout") == "transient"
    assert classify_error("ECONNRESET") == "transient"
    assert classify_error("HTTP 503 service unavailable") == "transient"


def test_classify_error_permanent_signatures() -> None:
    assert classify_error("401 Unauthorized") == "permanent"
    assert classify_error("Invalid API key") == "permanent"
    assert classify_error("validation error: payload missing") == "permanent"
    assert classify_error("403 Forbidden") == "permanent"
    assert classify_error("no handler registered for key 'agent_run'") == "permanent"


def test_classify_error_defaults_to_transient() -> None:
    assert classify_error("") == "transient"
    assert classify_error(None) == "transient"
    assert classify_error("something obscure") == "transient"


def _recurring_failure(error: str) -> CronJob:
    return CronJob(
        id="job-1",
        name="recurring",
        cron_expr="*/5 * * * *",
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "x", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.EVERY,
        enabled=True,
        status=JobStatus.PENDING,
    )


def test_permanent_error_disables_recurring_job_immediately() -> None:
    job = _recurring_failure("Invalid API key")
    execution = JobExecution(job_id=job.id, success=False, error="Invalid API key")
    _apply_result_state(job, execution, datetime.now(UTC))
    assert job.status == JobStatus.DISABLED
    assert job.enabled is False
    assert job.next_run_at is None


def test_transient_error_keeps_recurring_job_pending_with_backoff() -> None:
    job = _recurring_failure("HTTP 503 service unavailable")
    execution = JobExecution(
        job_id=job.id, success=False, error="HTTP 503 service unavailable"
    )
    _apply_result_state(job, execution, datetime.now(UTC))
    assert job.status == JobStatus.PENDING
    assert job.enabled is True
    assert job.backoff_until is not None


def test_permanent_error_disables_one_shot_at_job() -> None:
    job = CronJob(
        id="one-shot",
        name="report",
        cron_expr="2030-01-01T00:00:00Z",
        handler_key="agent_run",
        payload={"kind": "agent_turn", "task": "x", "agent_id": "main"},
        session_target=SessionTarget.ISOLATED,
        schedule_kind=ScheduleKind.AT,
        enabled=True,
        status=JobStatus.PENDING,
    )
    execution = JobExecution(job_id=job.id, success=False, error="Invalid API key")
    _apply_result_state(job, execution, datetime.now(UTC))
    assert job.status == JobStatus.DISABLED
    assert job.enabled is False
