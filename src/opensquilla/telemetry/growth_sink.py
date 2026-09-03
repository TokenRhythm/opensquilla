"""Durable, once-only producer for Gateway-owned growth milestones."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from opensquilla import __version__
from opensquilla.profile_operation_lock import ProfileOperationLock
from opensquilla.telemetry.consent import ConsentCheckpoint, TelemetryScope
from opensquilla.telemetry.contracts import CURRENT_NOTICE_VERSION_BY_SCOPE
from opensquilla.telemetry.contracts.common import (
    ClientEntrypoint,
    ClientSurface,
    ConsentScope,
    EventSource,
    ExecutionMode,
    Platform,
)
from opensquilla.telemetry.contracts.growth import (
    ClientLaunch,
    FirstTurnStarted,
    FirstTurnSucceeded,
)
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.growth.state import (
    GrowthStateError,
    client_launch_state_path,
    gateway_growth_milestone_state_path,
    growth_cohort_state_path,
    read_active_growth_cohort,
    read_growth_state_object,
    write_growth_state_object,
)
from opensquilla.telemetry.identity import (
    IdentityStateError,
    TelemetryIdentityKind,
    identity_state_path,
    read_identity,
)
from opensquilla.telemetry.ids import new_event_id
from opensquilla.telemetry.recorder import RecordStatus
from opensquilla.telemetry.reliability_sink import current_platform
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime

log = logging.getLogger(__name__)

GATEWAY_GROWTH_MILESTONE_SCHEMA_VERSION = 1
_MARKER_KIND = "growth_gateway_milestones"
_STATE_LOCK_TIMEOUT_SECONDS = 5.0
CLIENT_LAUNCH_SCHEMA_VERSION = 1
_CLIENT_LAUNCH_MARKER_KIND = "growth_client_launches"
_MAX_CLIENT_LAUNCH_RECORDS = 8

GrowthMilestoneEvent = FirstTurnStarted | FirstTurnSucceeded | ClientLaunch
GrowthMilestoneName = Literal["first_turn_started", "first_turn_result"]


class GrowthMilestoneStatus(StrEnum):
    PENDING = "pending"
    ENQUEUED = "enqueued"


@dataclass(frozen=True, slots=True)
class GrowthMilestoneRecord:
    status: GrowthMilestoneStatus
    event: GrowthMilestoneEvent


@dataclass(frozen=True, slots=True)
class GatewayGrowthMilestoneState:
    first_turn_started: GrowthMilestoneRecord | None = None
    first_turn_result: GrowthMilestoneRecord | None = None

    def record_for(self, name: GrowthMilestoneName) -> GrowthMilestoneRecord | None:
        if name == "first_turn_started":
            return self.first_turn_started
        return self.first_turn_result

    def with_record(
        self,
        name: GrowthMilestoneName,
        record: GrowthMilestoneRecord,
    ) -> GatewayGrowthMilestoneState:
        if name == "first_turn_started":
            return GatewayGrowthMilestoneState(
                first_turn_started=record,
                first_turn_result=self.first_turn_result,
            )
        return GatewayGrowthMilestoneState(
            first_turn_started=self.first_turn_started,
            first_turn_result=record,
        )


class GrowthEventSink:
    """Adapt two content-free turn boundaries into strict Growth events.

    The sink never creates cohort eligibility or an analytics identity.  Those
    are Electron-owned because only desktop startup can distinguish a fresh
    profile from an upgrade or import.
    """

    def __init__(
        self,
        runtime: ScopedTelemetryRuntime,
        *,
        config: object,
        app_version: str = __version__,
        platform: Platform | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runtime = runtime
        self._config = config
        self._app_version = app_version
        self._platform = platform or current_platform()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._coordinator = scope_consent_coordinator_for(config)
        self._marker_path = gateway_growth_milestone_state_path(config=config)
        self._client_launch_path = client_launch_state_path(config=config)
        self._cohort_path = growth_cohort_state_path(config=config)
        self._identity_path = identity_state_path(
            TelemetryIdentityKind.ANALYTICS_USER,
            config=config,
        )
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._first_turn_started_at: datetime | None = None
        self._closed = False

    @property
    def marker_path(self) -> Path:
        return self._marker_path

    def observe_turn_started(self) -> None:
        """Capture the public-user turn boundary without receiving its content."""

        if self._closed:
            return
        occurred_at = self._safe_now()
        if occurred_at is None:
            return
        if self._first_turn_started_at is None:
            self._first_turn_started_at = occurred_at
        self._schedule(self.record_turn_started(occurred_at))

    def observe_turn_succeeded(self) -> None:
        """Capture a successful terminal boundary without receiving its output."""

        if self._closed:
            return
        occurred_at = self._safe_now()
        if occurred_at is None:
            return
        self._schedule(self.record_turn_succeeded(occurred_at))

    async def record_turn_started(self, occurred_at: datetime) -> None:
        await self._record_milestone("first_turn_started", occurred_at)

    async def record_turn_succeeded(self, occurred_at: datetime) -> None:
        # A crash or scheduling race must never produce a success with no
        # started predecessor. Reusing the captured start time also preserves
        # event order when both callbacks settle concurrently.
        started_at = self._first_turn_started_at or occurred_at
        await self._record_milestone("first_turn_started", started_at)
        await self._record_milestone("first_turn_result", occurred_at)

    async def record_client_launch(
        self,
        *,
        surface: ClientSurface,
        entrypoint: ClientEntrypoint,
        execution_mode: ExecutionMode,
    ) -> bool:
        """Enqueue one usable-launch observation per identity/surface/UTC day."""

        if self._closed:
            return False
        if not isinstance(surface, ClientSurface):
            raise TypeError("surface must be a ClientSurface")
        if not isinstance(entrypoint, ClientEntrypoint):
            raise TypeError("entrypoint must be a ClientEntrypoint")
        if not isinstance(execution_mode, ExecutionMode):
            raise TypeError("execution_mode must be an ExecutionMode")
        occurred_at = self._safe_now()
        if occurred_at is None:
            return False
        async with self._lock:
            try:
                prepared = await self._prepare_client_launch(
                    occurred_at=occurred_at,
                    surface=surface,
                    entrypoint=entrypoint,
                    execution_mode=execution_mode,
                )
                if prepared is None:
                    return False
                key, event = prepared
                result = await self._runtime.record(event)
                if result.status not in {RecordStatus.RECORDED, RecordStatus.DUPLICATE}:
                    return False
                await self._acknowledge_client_launch(key, event)
                return True
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("client launch recording failed", exc_info=True)
                return False

    async def _prepare_client_launch(
        self,
        *,
        occurred_at: datetime,
        surface: ClientSurface,
        entrypoint: ClientEntrypoint,
        execution_mode: ExecutionMode,
    ) -> tuple[str, ClientLaunch] | None:
        notice_version = CURRENT_NOTICE_VERSION_BY_SCOPE[TelemetryScope.GROWTH.value]
        async with self._coordinator.authorized(
            TelemetryScope.GROWTH,
            checkpoint=ConsentCheckpoint.ENQUEUE,
            notice_version=notice_version,
        ) as permit:
            if permit is None:
                return None
            identity_value = self._active_identity_value()
            if identity_value is None:
                return None
            day = occurred_at.astimezone(UTC).date().isoformat()
            key = f"{identity_value}:{surface.value}:{day}"
            with ProfileOperationLock(
                self._client_launch_path,
                timeout=_STATE_LOCK_TIMEOUT_SECONDS,
            ):
                records = read_client_launch_state(self._client_launch_path)
                existing = records.get(key)
                if existing is not None:
                    if existing.status is GrowthMilestoneStatus.ENQUEUED:
                        return None
                    return key, existing.event
                event = ClientLaunch(
                    event_name="client_launch",
                    event_version=1,
                    event_id=new_event_id(),
                    occurred_at_utc=occurred_at,
                    source=EventSource.GATEWAY,
                    app_version=self._app_version,
                    platform=self._platform,
                    outcome=None,
                    error_code=None,
                    duration_ms=None,
                    consent_scope=ConsentScope.GROWTH,
                    notice_version=notice_version,
                    sample_rate=1,
                    analytics_user_id=UUID(identity_value),
                    surface=surface,
                    entrypoint=entrypoint,
                    execution_mode=execution_mode,
                )
                records[key] = GrowthMilestoneRecord(
                    status=GrowthMilestoneStatus.PENDING,
                    event=event,
                )
                write_client_launch_state(self._client_launch_path, records)
                return key, event

    async def _acknowledge_client_launch(
        self,
        key: str,
        event: ClientLaunch,
    ) -> None:
        notice_version = CURRENT_NOTICE_VERSION_BY_SCOPE[TelemetryScope.GROWTH.value]
        async with self._coordinator.authorized(
            TelemetryScope.GROWTH,
            checkpoint=ConsentCheckpoint.ENQUEUE,
            notice_version=notice_version,
        ) as permit:
            if permit is None or self._active_identity_value() != str(event.analytics_user_id):
                return
            with ProfileOperationLock(
                self._client_launch_path,
                timeout=_STATE_LOCK_TIMEOUT_SECONDS,
            ):
                records = read_client_launch_state(self._client_launch_path)
                existing = records.get(key)
                if existing is None or existing.event.event_id != event.event_id:
                    return
                records[key] = GrowthMilestoneRecord(
                    status=GrowthMilestoneStatus.ENQUEUED,
                    event=event,
                )
                write_client_launch_state(self._client_launch_path, records)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = tuple(self._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    def _schedule(self, operation: Coroutine[Any, Any, None]) -> None:
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(operation, name="telemetry-growth-milestone")
        except RuntimeError:
            operation.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            log.debug("growth milestone recording failed", exc_info=True)

    async def _record_milestone(
        self,
        name: GrowthMilestoneName,
        occurred_at: datetime,
    ) -> None:
        if not _valid_utc_datetime(occurred_at):
            return
        async with self._lock:
            try:
                event = await self._prepare_event(name, occurred_at)
                if event is None:
                    return
                result = await self._runtime.record(event)
                if result.status not in {RecordStatus.RECORDED, RecordStatus.DUPLICATE}:
                    return
                await self._acknowledge_event(name, event)
            except asyncio.CancelledError:
                raise
            except (GrowthStateError, IdentityStateError, OSError, ValueError, TypeError):
                log.debug("growth milestone state rejected", exc_info=True)
            except Exception:
                log.debug("growth milestone persistence failed", exc_info=True)

    async def _prepare_event(
        self,
        name: GrowthMilestoneName,
        occurred_at: datetime,
    ) -> GrowthMilestoneEvent | None:
        notice_version = CURRENT_NOTICE_VERSION_BY_SCOPE[TelemetryScope.GROWTH.value]
        async with self._coordinator.authorized(
            TelemetryScope.GROWTH,
            checkpoint=ConsentCheckpoint.ENQUEUE,
            notice_version=notice_version,
        ) as permit:
            if permit is None:
                return None
            identity_value = self._active_identity_value()
            if identity_value is None:
                return None
            with ProfileOperationLock(
                self._marker_path,
                timeout=_STATE_LOCK_TIMEOUT_SECONDS,
            ):
                state = read_gateway_growth_milestone_state(self._marker_path)
                existing = state.record_for(name)
                if existing is not None:
                    if str(existing.event.analytics_user_id) != identity_value:
                        raise GrowthStateError(
                            "growth milestone belongs to another analytics identity"
                        )
                    if existing.status is GrowthMilestoneStatus.ENQUEUED:
                        return None
                    return existing.event
                event = self._build_event(name, occurred_at, identity_value)
                pending = state.with_record(
                    name,
                    GrowthMilestoneRecord(
                        status=GrowthMilestoneStatus.PENDING,
                        event=event,
                    ),
                )
                write_gateway_growth_milestone_state(self._marker_path, pending)
                return event

    async def _acknowledge_event(
        self,
        name: GrowthMilestoneName,
        event: GrowthMilestoneEvent,
    ) -> None:
        notice_version = CURRENT_NOTICE_VERSION_BY_SCOPE[TelemetryScope.GROWTH.value]
        async with self._coordinator.authorized(
            TelemetryScope.GROWTH,
            checkpoint=ConsentCheckpoint.ENQUEUE,
            notice_version=notice_version,
        ) as permit:
            if permit is None:
                return
            if self._active_identity_value() != str(event.analytics_user_id):
                return
            with ProfileOperationLock(
                self._marker_path,
                timeout=_STATE_LOCK_TIMEOUT_SECONDS,
            ):
                state = read_gateway_growth_milestone_state(self._marker_path)
                existing = state.record_for(name)
                if existing is None or existing.event.event_id != event.event_id:
                    return
                acknowledged = state.with_record(
                    name,
                    GrowthMilestoneRecord(
                        status=GrowthMilestoneStatus.ENQUEUED,
                        event=event,
                    ),
                )
                write_gateway_growth_milestone_state(
                    self._marker_path,
                    acknowledged,
                )

    def _active_identity_value(self) -> str | None:
        if read_active_growth_cohort(self._cohort_path) is None:
            return None
        identity = read_identity(
            self._identity_path,
            expected_kind=TelemetryIdentityKind.ANALYTICS_USER,
        )
        return None if identity is None else identity.value

    def _build_event(
        self,
        name: GrowthMilestoneName,
        occurred_at: datetime,
        analytics_user_id: str,
    ) -> GrowthMilestoneEvent:
        event_id = new_event_id()
        analytics_id = UUID(analytics_user_id)
        notice_version = CURRENT_NOTICE_VERSION_BY_SCOPE[TelemetryScope.GROWTH.value]
        if name == "first_turn_started":
            return FirstTurnStarted(
                event_name=name,
                event_version=1,
                event_id=event_id,
                occurred_at_utc=occurred_at,
                source=EventSource.GATEWAY,
                app_version=self._app_version,
                platform=self._platform,
                outcome=None,
                error_code=None,
                duration_ms=None,
                consent_scope=ConsentScope.GROWTH,
                notice_version=notice_version,
                sample_rate=1,
                analytics_user_id=analytics_id,
            )
        return FirstTurnSucceeded(
            event_name=name,
            event_version=1,
            event_id=event_id,
            occurred_at_utc=occurred_at,
            source=EventSource.RUNTIME,
            app_version=self._app_version,
            platform=self._platform,
            outcome="success",
            error_code=None,
            duration_ms=None,
            consent_scope=ConsentScope.GROWTH,
            notice_version=notice_version,
            sample_rate=1,
            analytics_user_id=analytics_id,
        )

    def _safe_now(self) -> datetime | None:
        try:
            value = self._clock()
        except Exception:
            return None
        return value if _valid_utc_datetime(value) else None


def read_gateway_growth_milestone_state(
    path: str | Path,
) -> GatewayGrowthMilestoneState:
    payload = read_growth_state_object(path)
    if payload is None:
        return GatewayGrowthMilestoneState()
    expected_keys = {
        "schema_version",
        "marker_kind",
        "first_turn_started",
        "first_turn_result",
    }
    if set(payload) != expected_keys:
        raise GrowthStateError("growth milestone state has unknown or missing fields")
    if payload.get("schema_version") != GATEWAY_GROWTH_MILESTONE_SCHEMA_VERSION:
        raise GrowthStateError("unsupported growth milestone schema version")
    if payload.get("marker_kind") != _MARKER_KIND:
        raise GrowthStateError("unknown growth milestone marker kind")
    return GatewayGrowthMilestoneState(
        first_turn_started=_parse_record(
            "first_turn_started",
            payload.get("first_turn_started"),
        ),
        first_turn_result=_parse_record(
            "first_turn_result",
            payload.get("first_turn_result"),
        ),
    )


def write_gateway_growth_milestone_state(
    path: str | Path,
    state: GatewayGrowthMilestoneState,
) -> None:
    write_growth_state_object(
        path,
        {
            "schema_version": GATEWAY_GROWTH_MILESTONE_SCHEMA_VERSION,
            "marker_kind": _MARKER_KIND,
            "first_turn_started": _serialize_record(state.first_turn_started),
            "first_turn_result": _serialize_record(state.first_turn_result),
        },
    )


def read_client_launch_state(path: str | Path) -> dict[str, GrowthMilestoneRecord]:
    payload = read_growth_state_object(path)
    if payload is None:
        return {}
    if set(payload) != {"schema_version", "marker_kind", "records"}:
        raise GrowthStateError("client launch state has unknown or missing fields")
    if payload.get("schema_version") != CLIENT_LAUNCH_SCHEMA_VERSION:
        raise GrowthStateError("unsupported client launch state schema version")
    if payload.get("marker_kind") != _CLIENT_LAUNCH_MARKER_KIND:
        raise GrowthStateError("unknown client launch marker kind")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or len(raw_records) > _MAX_CLIENT_LAUNCH_RECORDS:
        raise GrowthStateError("client launch records are invalid")
    records: dict[str, GrowthMilestoneRecord] = {}
    for raw_record in raw_records:
        if not isinstance(raw_record, dict) or set(raw_record) != {
            "key",
            "status",
            "event",
        }:
            raise GrowthStateError("client launch record is invalid")
        key = raw_record.get("key")
        status_value = raw_record.get("status")
        if not isinstance(key, str) or not isinstance(status_value, str) or key in records:
            raise GrowthStateError("client launch record key is invalid")
        try:
            status = GrowthMilestoneStatus(status_value)
            event = ClientLaunch.model_validate_json(
                json.dumps(raw_record.get("event"), separators=(",", ":")),
                strict=True,
            )
        except Exception as exc:
            raise GrowthStateError("client launch event is invalid") from exc
        expected_key = (
            f"{event.analytics_user_id}:{event.surface.value}:"
            f"{event.occurred_at_utc.astimezone(UTC).date().isoformat()}"
        )
        if key != expected_key:
            raise GrowthStateError("client launch record key does not match its event")
        records[key] = GrowthMilestoneRecord(status=status, event=event)
    return records


def write_client_launch_state(
    path: str | Path,
    records: dict[str, GrowthMilestoneRecord],
) -> None:
    ordered = sorted(
        records.items(),
        key=lambda item: item[1].event.occurred_at_utc,
    )[-_MAX_CLIENT_LAUNCH_RECORDS:]
    write_growth_state_object(
        path,
        {
            "schema_version": CLIENT_LAUNCH_SCHEMA_VERSION,
            "marker_kind": _CLIENT_LAUNCH_MARKER_KIND,
            "records": [
                {
                    "key": key,
                    "status": record.status.value,
                    "event": record.event.model_dump(mode="json"),
                }
                for key, record in ordered
            ],
        },
    )


def _parse_record(
    expected_name: GrowthMilestoneName,
    value: object,
) -> GrowthMilestoneRecord | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"status", "event"}:
        raise GrowthStateError("growth milestone record is invalid")
    raw_status = value.get("status")
    if not isinstance(raw_status, str):
        raise GrowthStateError("growth milestone status is invalid")
    try:
        status = GrowthMilestoneStatus(raw_status)
    except (TypeError, ValueError) as exc:
        raise GrowthStateError("growth milestone status is invalid") from exc
    raw_event = value.get("event")
    try:
        encoded = json.dumps(raw_event, separators=(",", ":"))
        if expected_name == "first_turn_started":
            event: GrowthMilestoneEvent = FirstTurnStarted.model_validate_json(
                encoded,
                strict=True,
            )
        else:
            event = FirstTurnSucceeded.model_validate_json(encoded, strict=True)
    except Exception as exc:
        raise GrowthStateError("growth milestone event is invalid") from exc
    if event.event_name != expected_name:
        raise GrowthStateError("growth milestone event is stored in the wrong slot")
    return GrowthMilestoneRecord(status=status, event=event)


def _serialize_record(record: GrowthMilestoneRecord | None) -> dict[str, object] | None:
    if record is None:
        return None
    return {
        "status": record.status.value,
        "event": record.event.model_dump(mode="json"),
    }


def _valid_utc_datetime(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == UTC.utcoffset(value)
    )


__all__ = [
    "GATEWAY_GROWTH_MILESTONE_SCHEMA_VERSION",
    "CLIENT_LAUNCH_SCHEMA_VERSION",
    "GatewayGrowthMilestoneState",
    "GrowthEventSink",
    "GrowthMilestoneRecord",
    "GrowthMilestoneStatus",
    "read_gateway_growth_milestone_state",
    "read_client_launch_state",
    "write_gateway_growth_milestone_state",
    "write_client_launch_state",
]
