"""SQLite-backed persistence for scheduler jobs via aiosqlite."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import structlog

from opensquilla.compat import aiosqlite

from .payloads import DeliveryReport, normalize_contract, normalize_origin_session_key
from .types import (
    CronJob,
    CronWakeMode,
    DeliveryConfig,
    DeliveryMode,
    FailureDestination,
    JobExecution,
    JobReservation,
    JobReservationRejected,
    JobStatus,
    ReplyTargetSnapshot,
    ReservationRejectionReason,
    ScheduleKind,
    SessionTarget,
    clear_reservation,
)

__all__ = ["DeliveryReport", "JobStore"]
log = structlog.get_logger(__name__)

_CREATE_RUNS_TABLE = """
CREATE TABLE IF NOT EXISTS scheduler_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    summary TEXT,
    FOREIGN KEY (job_id) REFERENCES scheduler_jobs(id) ON DELETE CASCADE
)
"""

_CREATE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS scheduler_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    cron_expr TEXT NOT NULL,
    handler_key TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT,
    next_run_at TEXT,
    run_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    max_retries INTEGER NOT NULL DEFAULT 3,
    jitter_seconds REAL NOT NULL DEFAULT 0.0
)
"""

# New columns added via migration — not in CREATE TABLE to keep backward compat
_NEW_COLUMNS: list[tuple[str, str]] = [
    ("schedule_kind", "TEXT NOT NULL DEFAULT 'cron'"),
    ("schedule_raw", "TEXT NOT NULL DEFAULT ''"),
    ("session_target", "TEXT NOT NULL DEFAULT 'isolated'"),
    ("session_key", "TEXT NOT NULL DEFAULT ''"),
    ("timeout_seconds", "REAL NOT NULL DEFAULT 600.0"),
    ("wake_mode", "TEXT NOT NULL DEFAULT 'now'"),
    ("delete_after_run", "INTEGER NOT NULL DEFAULT 0"),
    ("enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("backoff_until", "TEXT"),
    ("consecutive_errors", "INTEGER NOT NULL DEFAULT 0"),
    ("delivery_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("origin_session_key", "TEXT NOT NULL DEFAULT ''"),
    ("reservation_token", "TEXT NOT NULL DEFAULT ''"),
    ("reserved_at", "TEXT"),
    ("reserved_by", "TEXT NOT NULL DEFAULT ''"),
    ("reservation_source", "TEXT NOT NULL DEFAULT ''"),
    ("scheduled_run_at", "TEXT"),
    ("tool_policy_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("tz", "TEXT NOT NULL DEFAULT ''"),
    ("anchor_at", "TEXT"),
    ("creator_session_key", "TEXT NOT NULL DEFAULT ''"),
    ("creator_sender_id", "TEXT NOT NULL DEFAULT ''"),
    ("creator_is_owner", "INTEGER NOT NULL DEFAULT 0"),
    ("run_mode", "TEXT NOT NULL DEFAULT ''"),
    ("elevated", "TEXT NOT NULL DEFAULT ''"),
    ("execution_target", "TEXT NOT NULL DEFAULT ''"),
    ("idempotency_key", "TEXT NOT NULL DEFAULT ''"),
]

_DATETIME_COLUMNS: tuple[str, ...] = (
    "created_at",
    "updated_at",
    "last_run_at",
    "next_run_at",
    "backoff_until",
    "reserved_at",
    "scheduled_run_at",
    "anchor_at",
)


def _storage_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _normalize_datetime_text(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _storage_iso(parsed)


def _row_to_job(row: aiosqlite.Row) -> CronJob:
    def _dt(s: str | None) -> datetime | None:
        return datetime.fromisoformat(s) if s else None

    def _get(key: str, default=None):
        try:
            return row[key]
        except (IndexError, KeyError):
            return default

    raw_payload = json.loads(row["payload"])
    raw_target = SessionTarget(_get("session_target", "isolated"))
    raw_session_key = _get("session_key", "") or ""
    raw_origin_session_key = _get("origin_session_key", "") or ""
    handler_key, payload, session_target, session_key = normalize_contract(
        handler_key=row["handler_key"],
        payload=raw_payload,
        session_target=raw_target,
        session_key=raw_session_key,
        origin_session_key=raw_origin_session_key,
        strict=False,
    )

    origin_session_key = normalize_origin_session_key(session_target, raw_origin_session_key)

    return CronJob(
        id=row["id"],
        name=row["name"],
        cron_expr=row["cron_expr"],
        handler_key=handler_key,
        payload=payload,
        status=JobStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_run_at=_dt(row["last_run_at"]),
        next_run_at=_dt(row["next_run_at"]),
        run_count=row["run_count"],
        error_count=row["error_count"],
        last_error=row["last_error"],
        max_retries=row["max_retries"],
        jitter_seconds=row["jitter_seconds"],
        schedule_kind=ScheduleKind(_get("schedule_kind", "cron")),
        schedule_raw=_get("schedule_raw", "") or "",
        tz=_get("tz", "") or "",
        anchor_at=_dt(_get("anchor_at")),
        creator_session_key=_get("creator_session_key", "") or "",
        creator_sender_id=_get("creator_sender_id", "") or "",
        creator_is_owner=bool(_get("creator_is_owner", 0)),
        run_mode=_get("run_mode", "") or "",
        elevated=_get("elevated", "") or "",
        execution_target=_get("execution_target", "") or "",
        idempotency_key=_get("idempotency_key", "") or "",
        session_target=session_target,
        session_key=session_key,
        timeout_seconds=_get("timeout_seconds", 600.0) or 600.0,
        wake_mode=_parse_wake_mode(_get("wake_mode", "now")),
        delete_after_run=bool(_get("delete_after_run", 0)),
        enabled=bool(_get("enabled", 1)),
        backoff_until=_dt(_get("backoff_until")),
        consecutive_errors=_get("consecutive_errors", 0) or 0,
        delivery=_effective_delivery_for_target(
            session_target,
            _parse_delivery(_get("delivery_json", "{}")),
        ),
        origin_session_key=origin_session_key,
        reservation_token=_get("reservation_token", "") or "",
        reserved_at=_dt(_get("reserved_at")),
        reserved_by=_get("reserved_by", "") or "",
        reservation_source=_get("reservation_source", "") or "",
        scheduled_run_at=_dt(_get("scheduled_run_at")),
        tool_policy=_parse_json_object(_get("tool_policy_json", "{}")),
    )


def _parse_wake_mode(raw: object) -> CronWakeMode:
    value = getattr(raw, "value", raw)
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if normalized in {CronWakeMode.NOW.value, CronWakeMode.NEXT_HEARTBEAT.value}:
        return CronWakeMode(normalized)
    if normalized:
        log.warning("scheduler.persistence.invalid_wake_mode", wake_mode=value)
    return CronWakeMode.NOW


def _effective_delivery_for_target(
    session_target: SessionTarget,
    delivery: DeliveryConfig,
) -> DeliveryConfig:
    if (
        session_target == SessionTarget.MAIN
        and delivery.mode != DeliveryMode.NONE
        and delivery.mode != DeliveryMode.WEBHOOK
    ):
        # Webhook delivery is permitted for main targets; other modes are
        # wiped because main heartbeat handles its own routing.
        return DeliveryConfig()
    return delivery


def _parse_json_object(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _serialize_failure_destination(fd: FailureDestination | None) -> dict | None:
    if fd is None:
        return None
    return {
        "mode": getattr(fd.mode, "value", str(fd.mode)),
        "channel_name": fd.channel_name,
        "channel_id": fd.channel_id,
        "account_id": fd.account_id,
        "thread_id": fd.thread_id,
        "webhook_url": fd.webhook_url,
        "webhook_token": fd.webhook_token,
    }


def _serialize_delivery(delivery: DeliveryConfig) -> str:
    snapshot = delivery.originating_reply_target
    return json.dumps(
        {
            "schema_version": 4,
            "mode": getattr(delivery.mode, "value", str(delivery.mode)),
            "channel_name": delivery.channel_name,
            "channel_id": delivery.channel_id,
            "account_id": delivery.account_id,
            "thread_id": delivery.thread_id,
            "ws_topic": delivery.ws_topic,
            "webhook_url": delivery.webhook_url,
            "webhook_token": delivery.webhook_token,
            "best_effort": delivery.best_effort,
            "failure_destination": _serialize_failure_destination(delivery.failure_destination),
            "originating_reply_target": (
                {
                    "channel_name": snapshot.channel_name,
                    "channel_type": snapshot.channel_type,
                    "to": snapshot.to,
                    "account_id": snapshot.account_id,
                    "thread_id": snapshot.thread_id,
                    "request_id": snapshot.request_id,
                }
                if snapshot is not None
                else None
            ),
        }
    )


def _parse_failure_destination(raw: object) -> FailureDestination | None:
    if not isinstance(raw, dict):
        return None
    try:
        mode = DeliveryMode(raw.get("mode", "none"))
    except ValueError:
        return None
    return FailureDestination(
        mode=mode,
        channel_name=raw.get("channel_name", "") or "",
        channel_id=raw.get("channel_id", "") or "",
        account_id=raw.get("account_id", "") or "",
        thread_id=raw.get("thread_id", "") or "",
        webhook_url=raw.get("webhook_url", "") or "",
        webhook_token=raw.get("webhook_token", "") or "",
    )


def _parse_delivery(raw: str | None) -> DeliveryConfig:
    if not raw:
        return DeliveryConfig()
    try:
        d = json.loads(raw)
        schema_version = int(d.get("schema_version", 1))
        if schema_version > 4:
            log.warning(
                "scheduler.delivery_json_unknown_schema",
                schema_version=schema_version,
            )
        snapshot = None
        if schema_version >= 2 and isinstance(d.get("originating_reply_target"), dict):
            target = d["originating_reply_target"]
            snapshot = ReplyTargetSnapshot(
                channel_name=target.get("channel_name", ""),
                channel_type=target.get("channel_type", ""),
                to=target.get("to", ""),
                account_id=target.get("account_id", ""),
                thread_id=target.get("thread_id", ""),
                request_id=target.get("request_id"),
            )
        failure_destination = (
            _parse_failure_destination(d.get("failure_destination"))
            if schema_version >= 4
            else None
        )
        return DeliveryConfig(
            mode=DeliveryMode(d.get("mode", "none")),
            channel_name=d.get("channel_name", ""),
            channel_id=d.get("channel_id", ""),
            account_id=d.get("account_id", ""),
            thread_id=d.get("thread_id", ""),
            ws_topic=d.get("ws_topic", ""),
            webhook_url=d.get("webhook_url", "") or "",
            webhook_token=d.get("webhook_token", "") or "",
            best_effort=bool(d.get("best_effort", False)),
            originating_reply_target=snapshot,
            failure_destination=failure_destination,
        )
    except (json.JSONDecodeError, ValueError):
        return DeliveryConfig()


class JobStore:
    """Async SQLite store for CronJob records."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._idempotent_create_lock = asyncio.Lock()

    async def open(self) -> None:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self._db_path)
            self._conn.row_factory = aiosqlite.Row
        db = self._db()
        await db.execute(_CREATE_JOBS_TABLE)
        await db.execute(_CREATE_RUNS_TABLE)
        await db.commit()
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await self._migrate()

    async def _migrate(self) -> None:
        """Add any missing columns to scheduler_jobs and scheduler_runs (idempotent)."""
        db = self._db()
        async with db.execute("PRAGMA table_info(scheduler_jobs)") as cur:
            rows = await cur.fetchall()
        existing = {r["name"] for r in rows}
        for col_name, col_def in _NEW_COLUMNS:
            if col_name not in existing:
                await db.execute(f"ALTER TABLE scheduler_jobs ADD COLUMN {col_name} {col_def}")
        await db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                idx_scheduler_jobs_idempotency_key
            ON scheduler_jobs(idempotency_key)
            WHERE idempotency_key <> ''
            """
        )

        # Migrate scheduler_runs — add missing columns
        async with db.execute("PRAGMA table_info(scheduler_runs)") as cur:
            rows = await cur.fetchall()
        runs_existing = {r["name"] for r in rows}
        _runs_migrations = [
            ("summary", "TEXT"),
            ("session_key", "TEXT NOT NULL DEFAULT ''"),
            ("delivery_status", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col_name, col_def in _runs_migrations:
            if col_name not in runs_existing:
                await db.execute(f"ALTER TABLE scheduler_runs ADD COLUMN {col_name} {col_def}")

        await self._normalize_legacy_jobs()
        await self._normalize_datetime_columns()

        await db.commit()

    async def _normalize_legacy_jobs(self) -> None:
        """Normalize old scheduler rows to the current cron contract."""
        db = self._db()
        async with db.execute(
            """
            SELECT id, handler_key, payload, session_target, session_key, origin_session_key
            FROM scheduler_jobs
            """
        ) as cur:
            rows = await cur.fetchall()

        for row in rows:
            try:
                raw_payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                raw_payload = {}

            raw_session_key = row["session_key"] or ""
            raw_origin_session_key = row["origin_session_key"] or ""
            session_target = SessionTarget(row["session_target"] or "isolated")
            handler_key, payload, session_target, session_key = normalize_contract(
                handler_key=row["handler_key"],
                payload=raw_payload,
                session_target=session_target,
                session_key=raw_session_key,
                origin_session_key=raw_origin_session_key,
                strict=False,
            )
            origin_session_key = normalize_origin_session_key(
                session_target,
                raw_origin_session_key,
            )
            payload_json = json.dumps(payload)

            if (
                handler_key == row["handler_key"]
                and payload_json == (row["payload"] or "{}")
                and session_key == raw_session_key
                and origin_session_key == raw_origin_session_key
            ):
                continue

            await db.execute(
                """
                UPDATE scheduler_jobs
                SET handler_key = ?, payload = ?, session_key = ?, origin_session_key = ?
                WHERE id = ?
                """,
                (handler_key, payload_json, session_key, origin_session_key, row["id"]),
            )

    async def _normalize_datetime_columns(self) -> None:
        """Store scheduler datetime text in UTC so lexical due checks are stable."""
        db = self._db()
        columns = ", ".join(_DATETIME_COLUMNS)
        async with db.execute(f"SELECT id, {columns} FROM scheduler_jobs") as cur:
            rows = await cur.fetchall()

        for row in rows:
            updates: dict[str, str] = {}
            for column in _DATETIME_COLUMNS:
                raw_value = row[column]
                normalized = _normalize_datetime_text(raw_value)
                if normalized is not None and normalized != raw_value:
                    updates[column] = normalized
            if not updates:
                continue
            set_clause = ", ".join(f"{column} = ?" for column in updates)
            await db.execute(
                f"UPDATE scheduler_jobs SET {set_clause} WHERE id = ?",
                [*updates.values(), row["id"]],
            )

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> JobStore:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("JobStore not opened")
        return self._conn

    def _iso(self, dt: datetime | None) -> str | None:
        return _storage_iso(dt)

    async def _execute_save(self, job: CronJob) -> None:
        handler_key, payload, session_target, session_key = normalize_contract(
            handler_key=job.handler_key,
            payload=job.payload,
            session_target=job.session_target,
            session_key=job.session_key,
            origin_session_key=job.origin_session_key,
            strict=False,
        )
        origin_session_key = normalize_origin_session_key(
            session_target,
            job.origin_session_key,
        )
        await self._db().execute(
            """
            INSERT INTO scheduler_jobs
                (id, name, cron_expr, handler_key, payload, status,
                 created_at, updated_at, last_run_at, next_run_at,
                 run_count, error_count, last_error, max_retries, jitter_seconds,
                 schedule_kind, schedule_raw, session_target, session_key,
                 timeout_seconds, wake_mode, delete_after_run, enabled, backoff_until,
                 consecutive_errors, delivery_json, origin_session_key,
                 reservation_token, reserved_at, reserved_by, reservation_source,
                 scheduled_run_at, tool_policy_json, tz, anchor_at,
                 creator_session_key, creator_sender_id, creator_is_owner,
                 run_mode, elevated, execution_target, idempotency_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                cron_expr=excluded.cron_expr,
                handler_key=excluded.handler_key,
                payload=excluded.payload,
                status=excluded.status,
                updated_at=excluded.updated_at,
                last_run_at=excluded.last_run_at,
                next_run_at=excluded.next_run_at,
                run_count=excluded.run_count,
                error_count=excluded.error_count,
                last_error=excluded.last_error,
                max_retries=excluded.max_retries,
                jitter_seconds=excluded.jitter_seconds,
                schedule_kind=excluded.schedule_kind,
                schedule_raw=excluded.schedule_raw,
                session_target=excluded.session_target,
                session_key=excluded.session_key,
                timeout_seconds=excluded.timeout_seconds,
                wake_mode=excluded.wake_mode,
                delete_after_run=excluded.delete_after_run,
                enabled=excluded.enabled,
                backoff_until=excluded.backoff_until,
                consecutive_errors=excluded.consecutive_errors,
                delivery_json=excluded.delivery_json,
                origin_session_key=excluded.origin_session_key,
                reservation_token=excluded.reservation_token,
                reserved_at=excluded.reserved_at,
                reserved_by=excluded.reserved_by,
                reservation_source=excluded.reservation_source,
                scheduled_run_at=excluded.scheduled_run_at,
                tool_policy_json=excluded.tool_policy_json,
                tz=excluded.tz,
                anchor_at=excluded.anchor_at,
                creator_session_key=excluded.creator_session_key,
                creator_sender_id=excluded.creator_sender_id,
                creator_is_owner=excluded.creator_is_owner,
                run_mode=excluded.run_mode,
                elevated=excluded.elevated,
                execution_target=excluded.execution_target,
                idempotency_key=excluded.idempotency_key
            """,
            (
                job.id,
                job.name,
                job.cron_expr,
                handler_key,
                json.dumps(payload),
                job.status.value,
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
                self._iso(job.last_run_at),
                self._iso(job.next_run_at),
                job.run_count,
                job.error_count,
                job.last_error,
                job.max_retries,
                job.jitter_seconds,
                getattr(job.schedule_kind, "value", str(job.schedule_kind)),
                job.schedule_raw,
                getattr(session_target, "value", str(session_target)),
                session_key,
                job.timeout_seconds,
                getattr(job.wake_mode, "value", str(job.wake_mode)),
                1 if job.delete_after_run else 0,
                1 if job.enabled else 0,
                self._iso(job.backoff_until),
                job.consecutive_errors,
                _serialize_delivery(job.delivery),
                origin_session_key,
                job.reservation_token,
                self._iso(job.reserved_at),
                job.reserved_by,
                job.reservation_source,
                self._iso(job.scheduled_run_at),
                json.dumps(job.tool_policy or {}),
                job.tz or "",
                self._iso(job.anchor_at),
                job.creator_session_key or "",
                job.creator_sender_id or "",
                1 if job.creator_is_owner else 0,
                job.run_mode or "",
                job.elevated or "",
                job.execution_target or "",
                job.idempotency_key or "",
            ),
        )

    async def save(self, job: CronJob) -> None:
        await self._execute_save(job)
        await self._db().commit()

    async def create_or_get(self, job: CronJob) -> CronJob:
        """Atomically create an idempotent job or return the existing row."""
        if not job.idempotency_key:
            await self.save(job)
            return job

        async with self._idempotent_create_lock:
            existing = await self.get_by_idempotency_key(job.idempotency_key)
            if existing is not None:
                existing.deduplicated = True
                return existing
            try:
                await self._execute_save(job)
                await self._db().commit()
            except aiosqlite.IntegrityError:
                await self._db().rollback()
                existing = await self.get_by_idempotency_key(job.idempotency_key)
                if existing is None:
                    raise
                existing.deduplicated = True
                return existing
            return job

    async def save_no_commit(self, job: CronJob) -> None:
        """Insert/update a job without committing — use inside transaction()."""
        await self._execute_save(job)

    @asynccontextmanager
    async def transaction(self):
        """Batch multiple save_no_commit() calls into a single commit."""
        yield self
        await self._db().commit()

    async def get(self, job_id: str) -> CronJob | None:
        async with self._db().execute(
            "SELECT * FROM scheduler_jobs WHERE id = ?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
            return _row_to_job(row) if row else None

    async def get_by_idempotency_key(self, idempotency_key: str) -> CronJob | None:
        if not idempotency_key:
            return None
        async with self._db().execute(
            "SELECT * FROM scheduler_jobs WHERE idempotency_key = ?",
            (idempotency_key,),
        ) as cur:
            row = await cur.fetchone()
            return _row_to_job(row) if row else None

    async def reserve_due_job(
        self,
        job_id: str,
        now: datetime,
        *,
        source: str = "timer",
        owner: str = "timer",
    ) -> JobReservation | JobReservationRejected:
        return await self._reserve_job_for_run(
            job_id,
            now,
            require_due=True,
            source=source,
            owner=owner,
        )

    async def reserve_manual_job(
        self,
        job_id: str,
        now: datetime,
        *,
        source: str = "manual",
        owner: str = "manual",
    ) -> JobReservation | JobReservationRejected:
        return await self._reserve_job_for_run(
            job_id,
            now,
            require_due=False,
            source=source,
            owner=owner,
        )

    async def _reserve_job_for_run(
        self,
        job_id: str,
        now: datetime,
        *,
        require_due: bool,
        source: str,
        owner: str,
    ) -> JobReservation | JobReservationRejected:
        token = str(uuid.uuid4())
        now_iso = now.isoformat()
        due_predicate = "AND next_run_at IS NOT NULL AND next_run_at <= ?" if require_due else ""
        params: list[object] = [
            token,
            now_iso,
            owner,
            source,
            now_iso,
            now_iso,
            job_id,
            JobStatus.PENDING.value,
            now_iso,
        ]
        if require_due:
            params.append(now_iso)

        cur = await self._db().execute(
            f"""
            UPDATE scheduler_jobs
            SET status = ?,
                reservation_token = ?,
                reserved_at = ?,
                reserved_by = ?,
                reservation_source = ?,
                scheduled_run_at = next_run_at,
                last_run_at = ?,
                updated_at = ?,
                last_error = NULL
            WHERE id = ?
              AND enabled = 1
              AND status = ?
              AND reservation_token = ''
              AND (backoff_until IS NULL OR backoff_until <= ?)
              {due_predicate}
            """,
            [JobStatus.RUNNING.value, *params],
        )
        await self._db().commit()

        if cur.rowcount == 1:
            job = await self.get(job_id)
            if job is None:
                return JobReservationRejected(
                    job_id=job_id,
                    reason=ReservationRejectionReason.NOT_FOUND,
                    message="Job not found after reservation",
                )
            return JobReservation(
                job=job,
                token=token,
                reserved_at=job.reserved_at or now,
                reserved_by=owner,
                reservation_source=source,
                scheduled_run_at=job.scheduled_run_at,
            )

        return await self._classify_reservation_rejection(
            job_id,
            now,
            require_due=require_due,
        )

    async def _classify_reservation_rejection(
        self,
        job_id: str,
        now: datetime,
        *,
        require_due: bool,
    ) -> JobReservationRejected:
        job = await self.get(job_id)
        if job is None:
            return JobReservationRejected(job_id, ReservationRejectionReason.NOT_FOUND)
        if job.reservation_token or job.status == JobStatus.RUNNING:
            return JobReservationRejected(job_id, ReservationRejectionReason.BUSY, job)
        if not job.enabled or job.status == JobStatus.DISABLED:
            return JobReservationRejected(job_id, ReservationRejectionReason.DISABLED, job)
        if job.backoff_until and job.backoff_until > now:
            return JobReservationRejected(job_id, ReservationRejectionReason.BACKING_OFF, job)
        if job.status != JobStatus.PENDING:
            return JobReservationRejected(
                job_id,
                ReservationRejectionReason.STATUS_CONFLICT,
                job,
            )
        if require_due and (job.next_run_at is None or job.next_run_at > now):
            return JobReservationRejected(job_id, ReservationRejectionReason.NOT_DUE, job)
        return JobReservationRejected(
            job_id,
            ReservationRejectionReason.STATUS_CONFLICT,
            job,
            message="Job could not be reserved",
        )

    async def finalize_reserved_missing_handler(
        self,
        job_id: str,
        reservation_token: str,
        error: str,
    ) -> bool:
        current = await self.get(job_id)
        if current is None or current.reservation_token != reservation_token:
            return False
        current.status = JobStatus.FAILED
        current.error_count += 1
        current.consecutive_errors += 1
        current.last_error = error
        current.next_run_at = None
        current.backoff_until = None
        current.updated_at = datetime.now(UTC)
        clear_reservation(current)
        await self.save(current)
        return True

    async def release_reservation(
        self,
        job_id: str,
        reservation_token: str,
    ) -> bool:
        current = await self.get(job_id)
        if current is None or current.reservation_token != reservation_token:
            return False
        clear_reservation(current)
        if current.status == JobStatus.RUNNING:
            current.status = JobStatus.PENDING
        current.updated_at = datetime.now(UTC)
        await self.save(current)
        return True

    async def delete(self, job_id: str) -> None:
        await self._db().execute("DELETE FROM scheduler_jobs WHERE id = ?", (job_id,))
        await self._db().commit()

    async def list_active(self) -> list[CronJob]:
        """Return all jobs that are not deleted."""
        async with self._db().execute(
            "SELECT * FROM scheduler_jobs WHERE status != ? ORDER BY created_at",
            (JobStatus.DELETED.value,),
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_job(r) for r in rows]

    async def list_by_status(self, status: JobStatus) -> list[CronJob]:
        """Return all jobs with the given status (e.g. RUNNING for startup cleanup)."""
        async with self._db().execute(
            "SELECT * FROM scheduler_jobs WHERE status = ? ORDER BY created_at",
            (status.value,),
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_job(r) for r in rows]

    async def next_due_at(self) -> datetime | None:
        """Return the earliest next_run_at among pending, enabled jobs."""
        async with self._db().execute(
            """
            SELECT MIN(next_run_at) FROM scheduler_jobs
            WHERE status = ? AND enabled = 1 AND next_run_at IS NOT NULL
            """,
            (JobStatus.PENDING.value,),
        ) as cur:
            row = await cur.fetchone()
            if row and row[0]:
                return datetime.fromisoformat(row[0])
            return None

    async def save_execution(self, execution: JobExecution) -> None:
        await self._db().execute(
            """
            INSERT INTO scheduler_runs
                (id, job_id, started_at, finished_at, success, error,
                 summary, session_key, delivery_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution.id,
                execution.job_id,
                execution.started_at.isoformat(),
                self._iso(execution.finished_at),
                1 if execution.success else 0,
                execution.error,
                execution.summary,
                execution.session_key,
                execution.delivery_status,
            ),
        )
        await self._db().commit()

    async def list_executions(self, job_id: str, limit: int = 20) -> list[JobExecution]:
        async with self._db().execute(
            "SELECT * FROM scheduler_runs WHERE job_id = ? ORDER BY started_at DESC LIMIT ?",
            (job_id, limit),
        ) as cur:
            rows = await cur.fetchall()

            def _safe_get(row, key, default=None):
                try:
                    return row[key]
                except (IndexError, KeyError):
                    return default

            return [
                JobExecution(
                    id=r["id"],
                    job_id=r["job_id"],
                    started_at=datetime.fromisoformat(r["started_at"]),
                    finished_at=(
                        datetime.fromisoformat(r["finished_at"]) if r["finished_at"] else None
                    ),
                    success=bool(r["success"]),
                    error=r["error"],
                    summary=_safe_get(r, "summary"),
                    session_key=_safe_get(r, "session_key", ""),
                    delivery_status=_safe_get(r, "delivery_status", ""),
                )
                for r in rows
            ]

    async def prune_runs(self, max_age_days: int = 30, max_per_job: int = 100) -> int:
        """Delete old execution records. Returns total rows deleted."""
        from datetime import UTC, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=max_age_days)).isoformat()

        # Delete by age
        cur = await self._db().execute(
            "DELETE FROM scheduler_runs WHERE started_at < ?",
            (cutoff,),
        )
        deleted = cur.rowcount

        # Delete excess per job (keep newest max_per_job)
        async with self._db().execute("SELECT DISTINCT job_id FROM scheduler_runs") as cur2:
            job_ids = [r[0] for r in await cur2.fetchall()]

        for job_id in job_ids:
            cur3 = await self._db().execute(
                """
                DELETE FROM scheduler_runs
                WHERE job_id = ? AND id NOT IN (
                    SELECT id FROM scheduler_runs
                    WHERE job_id = ?
                    ORDER BY started_at DESC
                    LIMIT ?
                )
                """,
                (job_id, job_id, max_per_job),
            )
            deleted += cur3.rowcount

        await self._db().commit()
        return deleted

    async def iter_due(self, now: datetime) -> AsyncIterator[CronJob]:
        """Yield jobs whose next_run_at <= now, status pending, enabled, and not backing off."""
        now_iso = now.isoformat()
        async with self._db().execute(
            """
            SELECT * FROM scheduler_jobs
            WHERE status = ? AND enabled = 1 AND next_run_at IS NOT NULL AND next_run_at <= ?
              AND (backoff_until IS NULL OR backoff_until <= ?)
            ORDER BY next_run_at
            """,
            (JobStatus.PENDING.value, now_iso, now_iso),
        ) as cur:
            async for row in cur:
                yield _row_to_job(row)
