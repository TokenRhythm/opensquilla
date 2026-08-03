"""CRUD operations for the scheduler — delegation layer between engine and store."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from opensquilla.session.keys import normalize_agent_id

from .delivery import validate_webhook_url
from .jobs import _next_run
from .parser import parse_cron, parse_iso_at, validate_tz
from .payloads import normalize_contract, normalize_origin_session_key, payload_agent_id
from .persistence import JobStore
from .stagger import compute_jitter
from .types import (
    CronJob,
    CronWakeMode,
    DeliveryConfig,
    DeliveryMode,
    JobExecution,
    JobStatus,
    ScheduleKind,
    SessionTarget,
)

_RUN_MODE_ALIASES = {
    "bypass": "full",
    "standard-sandbox": "standard",
    "standard_sandbox": "standard",
}
_PERSISTED_RUN_MODES = frozenset({"standard", "trusted", "full"})


def _validate_structured_schedule(
    kind: ScheduleKind | str,
    value: str,
) -> tuple[ScheduleKind, str]:
    """Validate (kind, value) per-kind and return canonical (kind, value).

    Raises ``ValueError`` (or subclasses) on invalid input. ``value`` is always
    returned as a string to match how the EVERY interval is stored elsewhere.
    """
    if isinstance(kind, str):
        kind = ScheduleKind(kind)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("schedule_value must be a non-empty string")
    value = value.strip()
    if kind == ScheduleKind.CRON:
        parse_cron(value)
        return kind, value
    if kind == ScheduleKind.AT:
        parse_iso_at(value)
        return kind, value
    if kind == ScheduleKind.EVERY:
        try:
            seconds = int(value)
        except ValueError as exc:
            raise ValueError(
                f"schedule_value for kind=every must be integer seconds; got {value!r}"
            ) from exc
        if seconds < 1:
            raise ValueError("schedule_value for kind=every must be >= 1 second")
        return kind, str(seconds)
    raise ValueError(f"Unsupported schedule_kind: {kind!r}")


def _coerce_wake_mode(value: CronWakeMode | str) -> CronWakeMode:
    if isinstance(value, CronWakeMode):
        return value
    return CronWakeMode(str(value or CronWakeMode.NOW.value).strip().lower())


def _persisted_run_mode(value: str, *, creator_is_owner: bool) -> str:
    """Resolve the already-authorized execution mode at the scheduler boundary."""

    normalized = str(value or "").strip().lower()
    normalized = _RUN_MODE_ALIASES.get(normalized, normalized)
    if not normalized:
        normalized = "full" if creator_is_owner else "trusted"
    if normalized not in _PERSISTED_RUN_MODES:
        raise ValueError(f"unsupported cron run_mode: {value!r}")
    if normalized == "full" and not creator_is_owner:
        return "trusted"
    return normalized


def _delivery_requested(delivery: DeliveryConfig | None) -> bool:
    return delivery is not None and delivery.mode != DeliveryMode.NONE


def _validate_main_agent(payload: dict | None, session_target: SessionTarget) -> None:
    if session_target != SessionTarget.MAIN:
        return
    agent_id = payload_agent_id(payload)
    if normalize_agent_id(agent_id) != "main":
        raise ValueError(
            'cron: sessionTarget "main" is only valid for the default agent. '
            'Use sessionTarget "isolated" with an agent_turn payload for non-default agents '
            f"(agent_id: {agent_id})"
        )


def _normalize_delivery_for_target(
    *,
    session_target: SessionTarget,
    delivery: DeliveryConfig,
    explicit_delivery: bool,
) -> DeliveryConfig:
    if delivery is not None and delivery.mode == DeliveryMode.WEBHOOK:
        validate_webhook_url(delivery.webhook_url)
    if session_target != SessionTarget.MAIN:
        return delivery
    # Webhook delivery is allowed for any sessionTarget — the heartbeat
    # pipeline ignores it and the webhook POST is independent of session.
    if delivery is not None and delivery.mode == DeliveryMode.WEBHOOK:
        return delivery
    if _delivery_requested(delivery):
        if explicit_delivery:
            raise ValueError(
                'cron channel delivery config is only supported for sessionTarget="isolated"'
            )
        return DeliveryConfig()
    return delivery


class SchedulerOps:
    """CRUD + validation layer over the JobStore."""

    def __init__(
        self,
        store: JobStore,
        max_jitter: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._max_jitter = max_jitter
        self._clock = clock

    def _now(self) -> datetime:
        now = self._clock() if self._clock is not None else datetime.now().astimezone()
        if now.tzinfo is None:
            return now.astimezone()
        return now

    async def add(
        self,
        name: str,
        *,
        schedule_kind: ScheduleKind | str,
        schedule_value: str,
        schedule_tz: str = "",
        handler_key: str = "",
        payload: dict | None = None,
        session_target: SessionTarget = SessionTarget.ISOLATED,
        session_key: str = "",
        timeout_seconds: float = 600.0,
        wake_mode: CronWakeMode | str = CronWakeMode.NOW,
        max_retries: int = 3,
        delivery: DeliveryConfig | None = None,
        origin_session_key: str = "",
        tool_policy: dict[str, Any] | None = None,
        tz: str = "",
        jitter_seconds: float | None = None,
        creator_session_key: str = "",
        creator_sender_id: str = "",
        creator_is_owner: bool = False,
        run_mode: str = "",
        idempotency_key: str = "",
    ) -> CronJob:
        """Validate the structured schedule, compute jitter, persist a new CronJob.

        ``schedule_kind`` + ``schedule_value`` are required; the value is
        validated per kind via ``parse_cron`` / ``parse_iso_at`` / integer
        check. No natural-language detection.

        ``jitter_seconds`` controls stagger:
          * ``None`` (default) → auto-computed via compute_jitter (legacy behaviour).
          * ``0`` → exact timing, no stagger.
          * ``>0`` → explicit fixed offset.
        """
        now_local = self._now()
        kind, cron_expr = _validate_structured_schedule(schedule_kind, schedule_value)
        schedule_raw = cron_expr
        tz = (schedule_tz or tz or "").strip()
        validate_tz(tz)
        if jitter_seconds is None:
            jitter = compute_jitter(handler_key + name, self._max_jitter)
        else:
            jitter = max(0.0, float(jitter_seconds))
        now = now_local.astimezone(UTC)

        # Coerce string to enum if needed
        if isinstance(session_target, str):
            session_target = SessionTarget(session_target)
        wake_mode = _coerce_wake_mode(wake_mode)

        # If sessionTarget=current is requested but no binding is available,
        # fall back to ISOLATED instead of failing creation. Headless cron
        # callers (no session context) get an isolated run rather than a hard
        # error.
        if (
            session_target == SessionTarget.CURRENT
            and not session_key
            and not origin_session_key
        ):
            session_target = SessionTarget.ISOLATED

        origin_session_key = normalize_origin_session_key(session_target, origin_session_key)
        handler_key, normalized_payload, session_target, session_key = normalize_contract(
            handler_key=handler_key,
            payload=payload,
            session_target=session_target,
            session_key=session_key,
            origin_session_key=origin_session_key,
            strict=True,
        )
        _validate_main_agent(normalized_payload, session_target)
        delivery = _normalize_delivery_for_target(
            session_target=session_target,
            delivery=delivery or DeliveryConfig(),
            explicit_delivery=delivery is not None,
        )
        effective_run_mode = _persisted_run_mode(
            run_mode,
            creator_is_owner=creator_is_owner,
        )

        job = CronJob(
            name=name,
            schedule_raw=schedule_raw,
            schedule_kind=kind,
            cron_expr=cron_expr,
            tz=tz,
            handler_key=handler_key,
            payload=normalized_payload,
            session_target=session_target,
            session_key=session_key,
            timeout_seconds=timeout_seconds,
            wake_mode=wake_mode,
            max_retries=max_retries,
            jitter_seconds=jitter,
            delivery=delivery,
            origin_session_key=origin_session_key,
            tool_policy=dict(tool_policy or {}),
            creator_session_key=creator_session_key or "",
            creator_sender_id=creator_sender_id or "",
            creator_is_owner=bool(creator_is_owner),
            run_mode=effective_run_mode,
            elevated="full" if effective_run_mode == "full" else "",
            execution_target="host" if effective_run_mode == "full" else "sandbox",
            idempotency_key=idempotency_key,
        )

        if kind == ScheduleKind.AT:
            job.delete_after_run = True
            job.next_run_at = datetime.fromisoformat(cron_expr)
        elif kind == ScheduleKind.EVERY and cron_expr.isdigit():
            # Anchor-based interval: record the anchor so subsequent fires
            # align to it rather than drifting with each run.
            job.anchor_at = now
            job.next_run_at = now + timedelta(seconds=int(cron_expr))
        else:
            # CRON or EVERY with cron expression: scan forward
            job.next_run_at = _next_run(job, now)

        return await self._store.create_or_get(job)

    async def update(self, job_id: str, **patch) -> CronJob | None:
        """Apply a partial update to an existing job. Returns None if not found."""
        job = await self._store.get(job_id)
        if job is None:
            return None

        now_local = self._now()
        now = now_local.astimezone(UTC)
        payload_patch = patch.pop("payload", None)
        delivery_was_patched = "delivery" in patch

        if "tz" in patch:
            raw_tz = (patch.pop("tz") or "").strip()
            validate_tz(raw_tz)
            job.tz = raw_tz

        structured_kind = patch.pop("schedule_kind", None)
        structured_value = patch.pop("schedule_value", None)
        structured_tz = patch.pop("schedule_tz", None)
        if structured_kind is not None and structured_value is not None:
            kind, cron_expr = _validate_structured_schedule(structured_kind, structured_value)
            if structured_tz is not None:
                raw_tz = (structured_tz or "").strip()
                validate_tz(raw_tz)
                job.tz = raw_tz
            job.schedule_raw = cron_expr
            job.schedule_kind = kind
            job.cron_expr = cron_expr
            if kind == ScheduleKind.AT:
                job.anchor_at = None
                job.next_run_at = datetime.fromisoformat(cron_expr)
            elif kind == ScheduleKind.EVERY:
                job.anchor_at = now
                job.next_run_at = now + timedelta(seconds=int(cron_expr))
            else:
                job.anchor_at = None
                job.next_run_at = _next_run(job, now)
        elif "schedule_raw" in patch:
            raise ValueError(
                "ops.update no longer accepts schedule_raw; "
                "pass schedule_kind + schedule_value instead"
            )

        for field in ("name", "timeout_seconds", "enabled", "origin_session_key"):
            if field in patch:
                setattr(job, field, patch.pop(field))
        if "tool_policy" in patch:
            job.tool_policy = dict(patch.pop("tool_policy") or {})
        if "wake_mode" in patch:
            raw_wake_mode = patch.pop("wake_mode")
            job.wake_mode = _coerce_wake_mode(raw_wake_mode)

        if "session_target" in patch:
            raw_target = patch.pop("session_target")
            job.session_target = (
                raw_target if isinstance(raw_target, SessionTarget) else SessionTarget(raw_target)
            )
        if "session_key" in patch:
            job.session_key = patch.pop("session_key") or ""

        if payload_patch:
            job.payload = {**job.payload, **payload_patch}
        if "delivery" in patch:
            job.delivery = patch.pop("delivery")

        (
            job.handler_key,
            job.payload,
            job.session_target,
            job.session_key,
        ) = normalize_contract(
            handler_key=job.handler_key,
            payload=job.payload,
            session_target=job.session_target,
            session_key=job.session_key,
            origin_session_key=job.origin_session_key,
            strict=True,
        )
        _validate_main_agent(job.payload, job.session_target)
        job.delivery = _normalize_delivery_for_target(
            session_target=job.session_target,
            delivery=job.delivery,
            explicit_delivery=delivery_was_patched,
        )
        job.origin_session_key = normalize_origin_session_key(
            job.session_target,
            job.origin_session_key,
        )

        job.updated_at = now
        await self._store.save(job)
        return job

    async def remove(self, job_id: str) -> bool:
        """Delete a job. Returns True if it existed."""
        job = await self._store.get(job_id)
        if job is None:
            return False
        await self._store.delete(job_id)
        return True

    async def pause(self, job_id: str) -> CronJob | None:
        """Set job status to PAUSED. Returns None if not found."""
        job = await self._store.get(job_id)
        if job is None:
            return None
        job.status = JobStatus.PAUSED
        job.updated_at = datetime.now(UTC)
        await self._store.save(job)
        return job

    async def resume(self, job_id: str) -> CronJob | None:
        """Set job status to PENDING and recompute next_run_at. Returns None if not found."""
        job = await self._store.get(job_id)
        if job is None:
            return None

        now = datetime.now(UTC)
        job.status = JobStatus.PENDING
        # Reviving a DISABLED/FAILED job must clear the flags the due-job query
        # filters on, or the job would stay excluded despite the status change.
        job.enabled = True
        job.backoff_until = None
        job.consecutive_errors = 0
        job.updated_at = now

        if job.schedule_kind == ScheduleKind.AT:
            # Keep existing next_run_at for one-shot jobs
            pass
        elif job.schedule_kind == ScheduleKind.EVERY and job.cron_expr.isdigit():
            # Use anchor-aligned next_run when an anchor exists; otherwise
            # match the historical "now + interval" behaviour.
            if job.anchor_at is not None:
                job.next_run_at = _next_run(job, now)
            else:
                job.next_run_at = now + timedelta(seconds=int(job.cron_expr))
        else:
            job.next_run_at = _next_run(job, now)

        await self._store.save(job)
        return job

    async def get(self, job_id: str) -> CronJob | None:
        """Retrieve a job by ID."""
        return await self._store.get(job_id)

    async def list_all(self) -> list[CronJob]:
        """Return all non-deleted jobs."""
        return await self._store.list_active()

    async def get_runs(self, job_id: str, limit: int = 20) -> list[JobExecution]:
        """Return recent execution records for a job."""
        return await self._store.list_executions(job_id, limit)
