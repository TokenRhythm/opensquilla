"""Shared, privacy-bounded telemetry event fields."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    UUID4,
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from opensquilla.telemetry.privacy import assert_no_forbidden_fields

EVENT_VERSION = 1
BATCH_VERSION = 1
MAX_DURATION_MS = 365 * 24 * 60 * 60 * 1000
MAX_COUNTER = 2**31 - 1

_RFC3339_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z")
_SAFE_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}\Z")


def _validate_safe_version(value: str) -> str:
    if not _SAFE_VERSION_RE.fullmatch(value):
        raise ValueError("version must contain only safe version characters")
    return value


AppVersion = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}",
    ),
    AfterValidator(_validate_safe_version),
]
NoticeVersion = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}",
    ),
    AfterValidator(_validate_safe_version),
]
DurationMs = Annotated[int, Field(strict=True, ge=0, le=MAX_DURATION_MS)]
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]
PositiveCounter = Annotated[int, Field(strict=True, ge=1, le=MAX_COUNTER)]
SampleRate = Annotated[
    float,
    Field(strict=True, gt=0.0, le=1.0, allow_inf_nan=False),
]


class Platform(StrEnum):
    MACOS = "macos"
    WINDOWS = "windows"
    LINUX = "linux"
    UNKNOWN = "unknown"


class EventSource(StrEnum):
    WEBSITE = "website"
    CDN = "cdn"
    INSTALLER = "installer"
    DESKTOP = "desktop"
    GATEWAY = "gateway"
    RUNTIME = "runtime"
    UPDATER = "updater"
    ACCOUNT_SERVICE = "account_service"


class ClientSurface(StrEnum):
    """Closed product surfaces safe for aggregate usage segmentation."""

    TUI = "tui"
    CLI = "cli"
    DESKTOP = "desktop"
    WEB = "web"


class ClientEntrypoint(StrEnum):
    """Entry commands that constitute a usable OpenSquilla launch."""

    CHAT = "chat"
    AGENT = "agent"
    GATEWAY_RUN = "gateway_run"


class ExecutionMode(StrEnum):
    """Closed execution topology, independent of the producing component."""

    GATEWAY = "gateway"
    STANDALONE = "standalone"
    ONE_SHOT = "one_shot"


class ConsentScope(StrEnum):
    RELIABILITY = "reliability"
    GROWTH = "growth"


class ResultOutcome(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    TIMEOUT = "timeout"
    CANCEL = "cancel"


class StrictTelemetryModel(BaseModel):
    """Base model that rejects extensions and privacy-forbidden keys."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=False,
        validate_default=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_forbidden_fields(cls, value: Any) -> Any:
        assert_no_forbidden_fields(value)
        return value


class UtcTimestampModel(StrictTelemetryModel):
    """Model mixin for a required RFC 3339 UTC timestamp."""

    occurred_at_utc: datetime

    @field_validator("occurred_at_utc", mode="before")
    @classmethod
    def _require_rfc3339_z_for_wire_strings(cls, value: Any, info: ValidationInfo) -> Any:
        if isinstance(value, str):
            if info.mode != "json":
                return value
            if not _RFC3339_UTC_RE.fullmatch(value):
                raise ValueError("timestamp must be RFC 3339 UTC ending in Z")
            return datetime.fromisoformat(value[:-1] + "+00:00")
        return value

    @field_validator("occurred_at_utc")
    @classmethod
    def _require_utc_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        value = value.astimezone(UTC)
        return value.replace(microsecond=(value.microsecond // 1000) * 1000)

    @field_serializer("occurred_at_utc", when_used="json")
    def _serialize_utc(self, value: datetime) -> str:
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class EventBase(UtcTimestampModel):
    """Fields present on every telemetry event."""

    event_name: str
    event_version: Literal[1]
    event_id: UUID4
    source: EventSource
    # Non-application producers such as the website and account service have
    # no honest application version to report.  The field remains required on
    # the wire and must be explicit ``null`` for those sources.  Application
    # event bases narrow it back to a required ``AppVersion`` below.
    app_version: AppVersion | None
    platform: Platform
    outcome: str | None
    error_code: str | None
    duration_ms: DurationMs | None
    consent_scope: ConsentScope
    notice_version: NoticeVersion
    sample_rate: SampleRate

    @field_validator("event_version", mode="before")
    @classmethod
    def _reject_boolean_event_version(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("event_version must be an integer")
        return value

    @field_validator("sample_rate", mode="before")
    @classmethod
    def _reject_boolean_sample_rate(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("sample_rate must be a JSON number")
        return value


class ReliabilityEventBase(EventBase):
    consent_scope: Literal[ConsentScope.RELIABILITY]
    app_version: AppVersion
    app_session_id: UUID4


class GrowthEventEnvelopeBase(EventBase):
    consent_scope: Literal[ConsentScope.GROWTH]
    sample_rate: Literal[1]

    @field_validator("sample_rate", mode="before")
    @classmethod
    def _require_unsampled_growth(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != 1:
            raise ValueError("growth events must use sample_rate 1")
        return value


class GrowthEventBase(GrowthEventEnvelopeBase):
    """Envelope for application-owned growth milestones."""

    app_version: AppVersion
    analytics_user_id: UUID4
    duration_ms: None
    error_code: None


def validate_success_error_pair(
    *, outcome: StrEnum | str, error_code: StrEnum | str | None
) -> None:
    """Enforce the common success/no-error and failure/error invariant."""

    if str(outcome) == ResultOutcome.SUCCESS.value:
        if error_code is not None:
            raise ValueError("successful events cannot include error_code")
        return
    if error_code is None:
        raise ValueError("non-success events require error_code")


def require_uuid4(value: UUID) -> UUID:
    """Runtime helper for callers that already hold a UUID instance."""

    if value.version != 4:
        raise ValueError("identifier must be UUID version 4")
    return value


__all__ = [
    "AppVersion",
    "BATCH_VERSION",
    "ClientEntrypoint",
    "ClientSurface",
    "ConsentScope",
    "Counter",
    "DurationMs",
    "EVENT_VERSION",
    "EventBase",
    "EventSource",
    "ExecutionMode",
    "GrowthEventBase",
    "GrowthEventEnvelopeBase",
    "MAX_COUNTER",
    "MAX_DURATION_MS",
    "NoticeVersion",
    "Platform",
    "PositiveCounter",
    "ReliabilityEventBase",
    "ResultOutcome",
    "SampleRate",
    "StrictTelemetryModel",
    "UtcTimestampModel",
    "validate_success_error_pair",
]
