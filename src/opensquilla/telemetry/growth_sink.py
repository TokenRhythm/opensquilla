"""Durable, once-only producer for Gateway-owned growth milestones."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
    CodingModeUsage,
    FirstTurnStarted,
    FirstTurnSucceeded,
    MetaSkillUsage,
)
from opensquilla.telemetry.coordination import scope_consent_coordinator_for
from opensquilla.telemetry.growth.state import (
    GrowthStateError,
    client_launch_state_path,
    coding_mode_usage_state_path,
    gateway_growth_milestone_state_path,
    growth_cohort_state_path,
    metaskill_usage_state_path,
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
METASKILL_USAGE_SCHEMA_VERSION = 1
_METASKILL_USAGE_MARKER_KIND = "growth_metaskill_usage"
CODING_MODE_USAGE_SCHEMA_VERSION = 1
_CODING_MODE_USAGE_MARKER_KIND = "growth_coding_mode_usage"
_MAX_ENQUEUED_FEATURE_USAGE_RECORDS = 24
_FEATURE_USAGE_RUN_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RETRY_INITIAL_SECONDS = 1.0
_RETRY_MAX_SECONDS = 60.0

FeatureUsageEvent = MetaSkillUsage | CodingModeUsage
GrowthMilestoneEvent = FirstTurnStarted | FirstTurnSucceeded | ClientLaunch | FeatureUsageEvent
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
        self._metaskill_usage_path = metaskill_usage_state_path(config=config)
        self._coding_mode_usage_path = coding_mode_usage_state_path(config=config)
        self._cohort_path = growth_cohort_state_path(config=config)
        self._identity_path = identity_state_path(
            TelemetryIdentityKind.ANALYTICS_USER,
            config=config,
        )
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._retry_requested = asyncio.Event()
        self._retry_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def marker_path(self) -> Path:
        return self._marker_path

    @property
    def metaskill_usage_path(self) -> Path:
        """Path of the bounded local deduplication state for MetaSkill runs."""

        return self._metaskill_usage_path

    @property
    def coding_mode_usage_path(self) -> Path:
        """Path of the bounded local deduplication state for Coding Mode runs."""

        return self._coding_mode_usage_path

    async def start(self) -> None:
        """Recover pending milestones and keep retrying without another turn."""

        if self._closed or self._retry_task is not None:
            return
        self._retry_task = asyncio.create_task(
            self._retry_loop(), name="telemetry-growth-replay"
        )
        self._retry_requested.set()

    def observe_turn_started(self) -> None:
        """Capture the public-user turn boundary without receiving its content."""

        if self._closed:
            return
        occurred_at = self._safe_now()
        if occurred_at is None:
            return
        self._schedule(self.record_turn_started(occurred_at))

    def observe_turn_succeeded(self) -> None:
        """Capture a successful terminal boundary without receiving its output."""

        if self._closed:
            return
        occurred_at = self._safe_now()
        if occurred_at is None:
            return
        self._schedule(self.record_turn_succeeded(occurred_at))

    def observe_metaskill_usage(self, run_id: str) -> None:
        """Count one newly admitted MetaSkill run without collecting its content.

        ``run_id`` is an internal persistence key used only to make retries and
        duplicate callbacks idempotent. It is never included in the wire event.
        """

        if self._closed or not _valid_feature_usage_run_key(run_id):
            return
        occurred_at = self._safe_now()
        if occurred_at is None:
            return
        # Schedule the already-admitted operation directly. ``close()`` stops
        # accepting new observations but drains this coroutine before returning.
        self._schedule(
            self._record_feature_usage(
                run_id=run_id,
                occurred_at=occurred_at,
                event_name="metaskill_usage",
            )
        )

    def observe_coding_mode_usage(self, run_id: str) -> None:
        """Count one Coding Mode run after its coding agent process starts.

        ``run_id`` is used only by the local deduplication ledger and is never
        serialized into the event payload.
        """

        if self._closed or not _valid_feature_usage_run_key(run_id):
            return
        occurred_at = self._safe_now()
        if occurred_at is None:
            return
        self._schedule(
            self._record_feature_usage(
                run_id=run_id,
                occurred_at=occurred_at,
                event_name="coding_mode_usage",
            )
        )

    async def replay_pending(self) -> None:
        """Retry only existing payloads, preserving their IDs and timestamps."""

        if self._closed:
            return
        try:
            state = read_gateway_growth_milestone_state(self._marker_path)
            for name in ("first_turn_started", "first_turn_result"):
                record = state.record_for(name)
                if record is not None and record.status is GrowthMilestoneStatus.PENDING:
                    await self._record_milestone(
                        name, record.event.occurred_at_utc, replay_only=True
                    )
        except (GrowthStateError, IdentityStateError, OSError, ValueError, TypeError):
            log.debug("growth milestone replay rejected", exc_info=True)

    async def record_turn_started(self, occurred_at: datetime) -> None:
        await self._record_milestone("first_turn_started", occurred_at)

    async def record_turn_succeeded(self, occurred_at: datetime) -> None:
        await self._record_milestone("first_turn_result", occurred_at)

    async def record_metaskill_usage(
        self,
        run_id: str,
        occurred_at: datetime,
    ) -> bool:
        """Persist one accepted MetaSkill usage observation.

        The local record keeps a stable event ID while an upload is pending;
        an evicted outbox item can therefore be retried without inflating the
        server-side count.
        """

        if self._closed:
            return False
        return await self._record_feature_usage(
            run_id=run_id,
            occurred_at=occurred_at,
            event_name="metaskill_usage",
        )

    async def record_coding_mode_usage(
        self,
        run_id: str,
        occurred_at: datetime,
    ) -> bool:
        """Persist one actual Coding Mode execution observation."""

        if self._closed:
            return False
        return await self._record_feature_usage(
            run_id=run_id,
            occurred_at=occurred_at,
            event_name="coding_mode_usage",
        )

    async def _retry_loop(self) -> None:
        while True:
            await self._retry_requested.wait()
            self._retry_requested.clear()
            delay = _RETRY_INITIAL_SECONDS
            while await self._has_pending():
                await self.replay_pending()
                if not await self._has_pending():
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_SECONDS)

    async def _has_pending(self) -> bool:
        notice_version = CURRENT_NOTICE_VERSION_BY_SCOPE[TelemetryScope.GROWTH.value]
        try:
            async with self._coordinator.authorized(
                TelemetryScope.GROWTH,
                checkpoint=ConsentCheckpoint.ENQUEUE,
                notice_version=notice_version,
            ) as permit:
                if permit is None or self._closed:
                    return False
                identity_value = self._active_identity_value()
                if identity_value is None:
                    return False
                state = read_gateway_growth_milestone_state(self._marker_path)
                return any(
                    record is not None
                    and record.status is GrowthMilestoneStatus.PENDING
                    and str(record.event.analytics_user_id) == identity_value
                    for record in (state.first_turn_started, state.first_turn_result)
                )
        except (GrowthStateError, IdentityStateError, OSError, ValueError, TypeError):
            log.debug("growth milestone retry state rejected", exc_info=True)
            return False

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
                # A prior feature observation may have been durably written to
                # its local ledger just before a crash. A normal app restart
                # emits client_launch even if the user starts no new turn, so
                # use that boundary to resume the pending enqueue.
                await self._retry_pending_feature_usage_locked()
                prepared = await self._prepare_client_launch(
                    occurred_at=occurred_at,
                    surface=surface,
                    entrypoint=entrypoint,
                    execution_mode=execution_mode,
                )
                if prepared is None:
                    return False
                key, event, consent_revision = prepared
                result = await self._runtime.record(
                    event,
                    expected_consent_revision=consent_revision,
                )
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
    ) -> tuple[str, ClientLaunch, int] | None:
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
                    if not isinstance(existing.event, ClientLaunch):
                        raise GrowthStateError("client launch state contains another event type")
                    return key, existing.event, permit.revision
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
                return key, event, permit.revision

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

    async def _record_feature_usage(
        self,
        *,
        run_id: str,
        occurred_at: datetime,
        event_name: Literal["metaskill_usage", "coding_mode_usage"],
    ) -> bool:
        if not _valid_feature_usage_run_key(run_id):
            return False
        if not _valid_utc_datetime(occurred_at):
            return False
        async with self._lock:
            try:
                await self._retry_pending_feature_usage_locked(
                    excluded=(event_name, run_id),
                )
                prepared = await self._prepare_feature_usage(
                    run_id=run_id,
                    occurred_at=occurred_at,
                    event_name=event_name,
                )
                if prepared is None:
                    return False
                key, event, consent_revision = prepared
                result = await self._runtime.record(
                    event,
                    expected_consent_revision=consent_revision,
                )
                if result.status not in {RecordStatus.RECORDED, RecordStatus.DUPLICATE}:
                    return False
                await self._acknowledge_feature_usage(key, event)
                return True
            except asyncio.CancelledError:
                raise
            except (GrowthStateError, IdentityStateError, OSError, ValueError, TypeError):
                log.debug("feature usage state rejected", exc_info=True)
            except Exception:
                log.debug("feature usage persistence failed", exc_info=True)
        return False

    async def _prepare_feature_usage(
        self,
        *,
        run_id: str,
        occurred_at: datetime,
        event_name: Literal["metaskill_usage", "coding_mode_usage"],
    ) -> tuple[str, FeatureUsageEvent, int] | None:
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
            state_path = self._feature_usage_path(event_name)
            with ProfileOperationLock(
                state_path,
                timeout=_STATE_LOCK_TIMEOUT_SECONDS,
            ):
                records = self._read_feature_usage_state(event_name)
                existing = records.get(run_id)
                if existing is not None:
                    if existing.event.event_name != event_name:
                        raise GrowthStateError("feature usage state contains another event type")
                    if str(existing.event.analytics_user_id) != identity_value:
                        raise GrowthStateError(
                            "feature usage belongs to another analytics identity"
                        )
                    if existing.status is GrowthMilestoneStatus.ENQUEUED:
                        return None
                    return run_id, existing.event, permit.revision
                event_fields: dict[str, Any] = {
                    "event_name": event_name,
                    "event_version": 1,
                    "event_id": new_event_id(),
                    "occurred_at_utc": occurred_at,
                    "source": EventSource.RUNTIME,
                    "app_version": self._app_version,
                    "platform": self._platform,
                    "outcome": None,
                    "error_code": None,
                    "duration_ms": None,
                    "consent_scope": ConsentScope.GROWTH,
                    "notice_version": notice_version,
                    "sample_rate": 1,
                    "analytics_user_id": UUID(identity_value),
                }
                event: FeatureUsageEvent
                if event_name == "metaskill_usage":
                    event = MetaSkillUsage(**event_fields)
                else:
                    event = CodingModeUsage(**event_fields)
                records[run_id] = GrowthMilestoneRecord(
                    status=GrowthMilestoneStatus.PENDING,
                    event=event,
                )
                self._write_feature_usage_state(event_name, records)
                return run_id, event, permit.revision

    async def _acknowledge_feature_usage(
        self,
        key: str,
        event: FeatureUsageEvent,
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
            event_name = event.event_name
            state_path = self._feature_usage_path(event_name)
            with ProfileOperationLock(
                state_path,
                timeout=_STATE_LOCK_TIMEOUT_SECONDS,
            ):
                records = self._read_feature_usage_state(event_name)
                existing = records.get(key)
                if existing is None or existing.event.event_id != event.event_id:
                    return
                records[key] = GrowthMilestoneRecord(
                    status=GrowthMilestoneStatus.ENQUEUED,
                    event=event,
                )
                self._write_feature_usage_state(event_name, records)

    async def _retry_pending_feature_usage_locked(
        self,
        *,
        excluded: tuple[str, str] | None = None,
    ) -> None:
        """Retry durable pending usage with its original event ID.

        This runs at later telemetry observations, so a crash or a temporarily
        full outbox after writing the local ledger does not permanently lose a
        demonstrated feature use.
        """

        notice_version = CURRENT_NOTICE_VERSION_BY_SCOPE[TelemetryScope.GROWTH.value]
        async with self._coordinator.authorized(
            TelemetryScope.GROWTH,
            checkpoint=ConsentCheckpoint.ENQUEUE,
            notice_version=notice_version,
        ) as permit:
            if permit is None:
                return
            identity_value = self._active_identity_value()
            if identity_value is None:
                return
            pending: list[tuple[str, str, FeatureUsageEvent]] = []
            for event_name in ("metaskill_usage", "coding_mode_usage"):
                state_path = self._feature_usage_path(event_name)
                try:
                    with ProfileOperationLock(
                        state_path,
                        timeout=_STATE_LOCK_TIMEOUT_SECONDS,
                    ):
                        records = self._read_feature_usage_state(event_name)
                except (GrowthStateError, OSError, ValueError, TypeError):
                    # The two feature ledgers are purpose-isolated. Corruption
                    # in one must not suppress a valid observation in the other.
                    log.debug(
                        "pending feature usage ledger rejected",
                        exc_info=True,
                        extra={"event_name": event_name},
                    )
                    continue
                for key, record in records.items():
                    if excluded == (event_name, key):
                        continue
                    if not isinstance(record.event, (MetaSkillUsage, CodingModeUsage)):
                        raise GrowthStateError(
                            "feature usage state contains another event type"
                        )
                    if (
                        record.status is GrowthMilestoneStatus.PENDING
                        and str(record.event.analytics_user_id) == identity_value
                    ):
                        pending.append((event_name, key, record.event))
        for _event_name, key, event in pending:
            try:
                result = await self._runtime.record(
                    event,
                    expected_consent_revision=permit.revision,
                )
                if result.status in {RecordStatus.RECORDED, RecordStatus.DUPLICATE}:
                    await self._acknowledge_feature_usage(key, event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("pending feature usage retry failed", exc_info=True)

    def _feature_usage_path(self, event_name: str) -> Path:
        if event_name == "metaskill_usage":
            return self._metaskill_usage_path
        if event_name == "coding_mode_usage":
            return self._coding_mode_usage_path
        raise ValueError("unknown feature usage event")

    def _read_feature_usage_state(
        self,
        event_name: str,
    ) -> dict[str, GrowthMilestoneRecord]:
        if event_name == "metaskill_usage":
            return read_metaskill_usage_state(self._metaskill_usage_path)
        if event_name == "coding_mode_usage":
            return read_coding_mode_usage_state(self._coding_mode_usage_path)
        raise ValueError("unknown feature usage event")

    def _write_feature_usage_state(
        self,
        event_name: str,
        records: dict[str, GrowthMilestoneRecord],
    ) -> None:
        if event_name == "metaskill_usage":
            write_metaskill_usage_state(self._metaskill_usage_path, records)
            return
        if event_name == "coding_mode_usage":
            write_coding_mode_usage_state(self._coding_mode_usage_path, records)
            return
        raise ValueError("unknown feature usage event")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        retry_task = self._retry_task
        self._retry_task = None
        if retry_task is not None:
            retry_task.cancel()
            await asyncio.gather(retry_task, return_exceptions=True)
        pending = tuple(self._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    def _schedule(self, operation: Coroutine[Any, Any, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(operation, name="telemetry-growth-milestone")
        except RuntimeError:
            operation.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[Any]) -> None:
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
        *,
        replay_only: bool = False,
    ) -> None:
        if not _valid_utc_datetime(occurred_at):
            return
        async with self._lock:
            try:
                await self._retry_pending_feature_usage_locked()
                prepared = await self._prepare_event(name, occurred_at, replay_only=replay_only)
                if prepared is None:
                    return
                event, consent_revision = prepared
                result = await self._runtime.record(
                    event,
                    expected_consent_revision=consent_revision,
                )
                if result.status not in {RecordStatus.RECORDED, RecordStatus.DUPLICATE}:
                    return
                await self._acknowledge_event(name, event)
            except asyncio.CancelledError:
                raise
            except (GrowthStateError, IdentityStateError, OSError, ValueError, TypeError):
                log.debug("growth milestone state rejected", exc_info=True)
            except Exception:
                log.debug("growth milestone persistence failed", exc_info=True)
            finally:
                if not self._closed and asyncio.current_task() is not self._retry_task:
                    self._retry_requested.set()

    async def _prepare_event(
        self,
        name: GrowthMilestoneName,
        occurred_at: datetime,
        *,
        replay_only: bool,
    ) -> tuple[GrowthMilestoneEvent, int] | None:
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
                predecessor = state.first_turn_started
                if name == "first_turn_result" and predecessor is None:
                    # A success cannot reconstruct a start that was never
                    # captured under consent, including after revocation.
                    return None
                if existing is not None:
                    if str(existing.event.analytics_user_id) != identity_value:
                        raise GrowthStateError(
                            "growth milestone belongs to another analytics identity"
                        )
                    if existing.status is GrowthMilestoneStatus.ENQUEUED:
                        return None
                    event = existing.event
                else:
                    if replay_only:
                        return None
                    event = self._build_event(name, occurred_at, identity_value)
                    pending = state.with_record(
                        name,
                        GrowthMilestoneRecord(
                            status=GrowthMilestoneStatus.PENDING,
                            event=event,
                        ),
                    )
                    write_gateway_growth_milestone_state(self._marker_path, pending)
                if name == "first_turn_result" and (
                    predecessor is None
                    or predecessor.status is not GrowthMilestoneStatus.ENQUEUED
                    or str(predecessor.event.analytics_user_id) != identity_value
                ):
                    return None
                return event, permit.revision

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


def read_metaskill_usage_state(path: str | Path) -> dict[str, GrowthMilestoneRecord]:
    """Read the bounded, local-only MetaSkill usage deduplication ledger."""

    return _read_feature_usage_state(
        path,
        schema_version=METASKILL_USAGE_SCHEMA_VERSION,
        marker_kind=_METASKILL_USAGE_MARKER_KIND,
        event_type=MetaSkillUsage,
        event_name="metaskill_usage",
        label="metaskill",
    )


def write_metaskill_usage_state(
    path: str | Path,
    records: dict[str, GrowthMilestoneRecord],
) -> None:
    """Write the bounded MetaSkill usage ledger atomically."""

    _write_feature_usage_state(
        path,
        records,
        schema_version=METASKILL_USAGE_SCHEMA_VERSION,
        marker_kind=_METASKILL_USAGE_MARKER_KIND,
    )


def read_coding_mode_usage_state(path: str | Path) -> dict[str, GrowthMilestoneRecord]:
    """Read the bounded, local-only Coding Mode usage deduplication ledger."""

    return _read_feature_usage_state(
        path,
        schema_version=CODING_MODE_USAGE_SCHEMA_VERSION,
        marker_kind=_CODING_MODE_USAGE_MARKER_KIND,
        event_type=CodingModeUsage,
        event_name="coding_mode_usage",
        label="coding mode",
    )


def write_coding_mode_usage_state(
    path: str | Path,
    records: dict[str, GrowthMilestoneRecord],
) -> None:
    """Write the bounded Coding Mode usage ledger atomically."""

    _write_feature_usage_state(
        path,
        records,
        schema_version=CODING_MODE_USAGE_SCHEMA_VERSION,
        marker_kind=_CODING_MODE_USAGE_MARKER_KIND,
    )


def _read_feature_usage_state(
    path: str | Path,
    *,
    schema_version: int,
    marker_kind: str,
    event_type: type[MetaSkillUsage] | type[CodingModeUsage],
    event_name: Literal["metaskill_usage", "coding_mode_usage"],
    label: str,
) -> dict[str, GrowthMilestoneRecord]:
    payload = read_growth_state_object(path)
    if payload is None:
        return {}
    if set(payload) != {"schema_version", "marker_kind", "records"}:
        raise GrowthStateError(f"{label} usage state has unknown or missing fields")
    if payload.get("schema_version") != schema_version:
        raise GrowthStateError(f"unsupported {label} usage state schema version")
    if payload.get("marker_kind") != marker_kind:
        raise GrowthStateError(f"unknown {label} usage marker kind")
    raw_records = payload.get("records")
    # The shared reader already rejects files larger than 16 KiB. Do not impose
    # a record-count limit here: every PENDING observation must survive until it
    # reaches the outbox, while only acknowledged dedupe history is bounded.
    if not isinstance(raw_records, list):
        raise GrowthStateError(f"{label} usage records are invalid")
    records: dict[str, GrowthMilestoneRecord] = {}
    for raw_record in raw_records:
        if not isinstance(raw_record, dict) or set(raw_record) != {
            "key",
            "status",
            "event",
        }:
            raise GrowthStateError(f"{label} usage record is invalid")
        key = raw_record.get("key")
        status_value = raw_record.get("status")
        if (
            not isinstance(key, str)
            or not _valid_feature_usage_run_key(key)
            or not isinstance(status_value, str)
            or key in records
        ):
            raise GrowthStateError(f"{label} usage record key is invalid")
        try:
            status = GrowthMilestoneStatus(status_value)
            raw_event = raw_record.get("event")
            if (
                isinstance(raw_event, dict)
                and raw_event.get("event_name") == event_name
                and raw_event.get("notice_version") == "growth-v1"
            ):
                # A pre-release client could persist these usage events before
                # the consent copy explicitly disclosed ongoing feature counts.
                # They must never be uploaded under a later grant. Ignore them
                # here; the next successful write atomically removes them.
                continue
            event = event_type.model_validate_json(
                json.dumps(raw_event, separators=(",", ":")),
                strict=True,
            )
        except Exception as exc:
            raise GrowthStateError(f"{label} usage event is invalid") from exc
        records[key] = GrowthMilestoneRecord(status=status, event=event)
    return records


def _write_feature_usage_state(
    path: str | Path,
    records: dict[str, GrowthMilestoneRecord],
    *,
    schema_version: int,
    marker_kind: str,
) -> None:
    pending = [
        item
        for item in records.items()
        if item[1].status is GrowthMilestoneStatus.PENDING
    ]
    enqueued = [
        item
        for item in records.items()
        if item[1].status is GrowthMilestoneStatus.ENQUEUED
    ]
    enqueued = sorted(
        enqueued,
        key=lambda item: item[1].event.occurred_at_utc,
    )[-_MAX_ENQUEUED_FEATURE_USAGE_RECORDS:]
    # Never evict an observation that has not reached the outbox. The atomic
    # state writer fails closed if the bounded file-size contract is exhausted.
    ordered = sorted(
        [*pending, *enqueued],
        key=lambda item: item[1].event.occurred_at_utc,
    )
    write_growth_state_object(
        path,
        {
            "schema_version": schema_version,
            "marker_kind": marker_kind,
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


def _valid_feature_usage_run_key(value: object) -> bool:
    return isinstance(value, str) and bool(_FEATURE_USAGE_RUN_KEY_RE.fullmatch(value))


__all__ = [
    "GATEWAY_GROWTH_MILESTONE_SCHEMA_VERSION",
    "CLIENT_LAUNCH_SCHEMA_VERSION",
    "CODING_MODE_USAGE_SCHEMA_VERSION",
    "METASKILL_USAGE_SCHEMA_VERSION",
    "GatewayGrowthMilestoneState",
    "GrowthEventSink",
    "GrowthMilestoneRecord",
    "GrowthMilestoneStatus",
    "read_gateway_growth_milestone_state",
    "read_client_launch_state",
    "read_coding_mode_usage_state",
    "read_metaskill_usage_state",
    "write_gateway_growth_milestone_state",
    "write_client_launch_state",
    "write_coding_mode_usage_state",
    "write_metaskill_usage_state",
]
