"""Strict upload-batch contracts for reliability and growth events."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self

from pydantic import (
    UUID4,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from opensquilla.telemetry.contracts.common import StrictTelemetryModel
from opensquilla.telemetry.contracts.growth import GrowthEvent
from opensquilla.telemetry.contracts.reliability import ReliabilityEvent

MAX_RELIABILITY_BATCH_EVENTS = 100
MAX_GROWTH_BATCH_EVENTS = 50

_RFC3339_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")


class BatchBase(StrictTelemetryModel):
    batch_version: Literal[1]
    batch_id: UUID4
    sent_at_utc: datetime

    @field_validator("events", mode="before", check_fields=False)
    @classmethod
    def _freeze_wire_event_array(cls, value: Any, info: ValidationInfo) -> Any:
        if info.mode == "json" and isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("batch_version", mode="before")
    @classmethod
    def _reject_boolean_batch_version(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("batch_version must be an integer")
        return value

    @field_validator("sent_at_utc", mode="before")
    @classmethod
    def _require_rfc3339_z_for_wire_strings(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, str):
            if info.mode != "json":
                return value
            if not _RFC3339_UTC_RE.fullmatch(value):
                raise ValueError("sent_at_utc must be RFC 3339 UTC ending in Z")
            return datetime.fromisoformat(value[:-1] + "+00:00")
        return value

    @field_validator("sent_at_utc")
    @classmethod
    def _require_utc_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("sent_at_utc must be timezone-aware UTC")
        value = value.astimezone(UTC)
        return value.replace(microsecond=(value.microsecond // 1000) * 1000)

    @field_serializer("sent_at_utc", when_used="json")
    def _serialize_utc(self, value: datetime) -> str:
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ReliabilityEventBatch(BatchBase):
    events: Annotated[
        tuple[ReliabilityEvent, ...],
        Field(min_length=1, max_length=MAX_RELIABILITY_BATCH_EVENTS),
    ]

    @model_validator(mode="after")
    def _require_unique_event_ids(self) -> Self:
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("batch event_id values must be unique")
        return self


class GrowthEventBatch(BatchBase):
    events: Annotated[
        tuple[GrowthEvent, ...],
        Field(min_length=1, max_length=MAX_GROWTH_BATCH_EVENTS),
    ]

    @model_validator(mode="after")
    def _require_unique_event_ids(self) -> Self:
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("batch event_id values must be unique")
        return self


TelemetryBatch = ReliabilityEventBatch | GrowthEventBatch

RELIABILITY_BATCH_ADAPTER: TypeAdapter[ReliabilityEventBatch] = TypeAdapter(ReliabilityEventBatch)
GROWTH_BATCH_ADAPTER: TypeAdapter[GrowthEventBatch] = TypeAdapter(GrowthEventBatch)
TELEMETRY_BATCH_ADAPTER: TypeAdapter[TelemetryBatch] = TypeAdapter(TelemetryBatch)


__all__ = [
    "GROWTH_BATCH_ADAPTER",
    "MAX_GROWTH_BATCH_EVENTS",
    "MAX_RELIABILITY_BATCH_EVENTS",
    "RELIABILITY_BATCH_ADAPTER",
    "TELEMETRY_BATCH_ADAPTER",
    "BatchBase",
    "GrowthEventBatch",
    "ReliabilityEventBatch",
    "TelemetryBatch",
]
