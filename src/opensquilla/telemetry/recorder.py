"""Consent-gated entry point for durable telemetry collection.

Application instrumentation must use :class:`TelemetryRecorder` instead of
writing directly to :class:`~opensquilla.telemetry.outbox.TelemetryOutbox`.
The outbox intentionally remains a low-level persistence primitive so import
and recovery code can be tested independently; this facade owns the live
consent checkpoint and notice-version boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from opensquilla.telemetry.consent import ConsentCheckpoint, TelemetryScope
from opensquilla.telemetry.contracts import CURRENT_NOTICE_VERSION_BY_SCOPE
from opensquilla.telemetry.contracts.common import StrictTelemetryModel
from opensquilla.telemetry.coordination import (
    scope_consent_coordinator_for,
)
from opensquilla.telemetry.outbox import EnqueueResult, OutboxPriority, TelemetryOutbox


class RecordStatus(StrEnum):
    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    EVICTED = "evicted"
    CONSENT_BLOCKED = "consent_blocked"
    NOTICE_MISMATCH = "notice_mismatch"


@dataclass(frozen=True)
class RecordResult:
    status: RecordStatus


class TelemetryRecorder:
    """Re-evaluate one scope immediately before its event is persisted."""

    def __init__(
        self,
        outbox: TelemetryOutbox,
        *,
        config: object,
    ) -> None:
        self._outbox = outbox
        self._config = config
        self._coordinator = scope_consent_coordinator_for(config)

    @property
    def scope(self) -> TelemetryScope:
        """The single consent scope this recorder can durably commit."""

        return self._outbox.scope

    def is_bound_to_config(self, config: object) -> bool:
        """Return whether this recorder shares the live config's coordinator."""

        return self._config is config

    async def record(
        self,
        event: StrictTelemetryModel,
        *,
        priority: OutboxPriority | int | None = None,
    ) -> RecordResult:
        """Persist one validated event only under its current saved notice."""

        event_scope = getattr(event, "consent_scope", None)
        if str(event_scope) != self._outbox.scope.value:
            raise ValueError("event consent scope does not match recorder scope")

        event_notice = getattr(event, "notice_version", None)
        if (
            not isinstance(event_notice, str)
            or event_notice != CURRENT_NOTICE_VERSION_BY_SCOPE[self._outbox.scope.value]
        ):
            return RecordResult(RecordStatus.NOTICE_MISMATCH)
        async with self._coordinator.authorized(
            self._outbox.scope,
            checkpoint=ConsentCheckpoint.ENQUEUE,
            notice_version=event_notice,
        ) as permit:
            if permit is None:
                return RecordResult(RecordStatus.CONSENT_BLOCKED)
            result = await self._outbox.enqueue(event, priority=priority)
            return RecordResult(_RECORD_STATUS_BY_ENQUEUE_RESULT[result])


_RECORD_STATUS_BY_ENQUEUE_RESULT = {
    EnqueueResult.ENQUEUED: RecordStatus.RECORDED,
    EnqueueResult.DUPLICATE: RecordStatus.DUPLICATE,
    EnqueueResult.EVICTED: RecordStatus.EVICTED,
}


__all__ = [
    "RecordResult",
    "RecordStatus",
    "TelemetryRecorder",
]
