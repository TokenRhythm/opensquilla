"""Strict contracts for authoritative growth-funnel milestones."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import UUID4, Field, TypeAdapter, model_validator

from opensquilla.telemetry.contracts.common import (
    AppVersion,
    ClientEntrypoint,
    ClientSurface,
    DurationMs,
    EventSource,
    ExecutionMode,
    GrowthEventBase,
    GrowthEventEnvelopeBase,
    validate_success_error_pair,
)


class InstallOutcome(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    CANCEL = "cancel"


class InstallErrorCode(StrEnum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    PREREQUISITE_MISSING = "prerequisite_missing"
    INSUFFICIENT_SPACE = "insufficient_space"
    PERMISSION_DENIED = "permission_denied"
    PACKAGE_INVALID = "package_invalid"
    INSTALL_CANCELLED = "install_cancelled"
    INTERNAL_ERROR = "internal_error"


class RegistrationOutcome(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    CANCEL = "cancel"


class RegistrationErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    ACCOUNT_EXISTS = "account_exists"
    VERIFICATION_FAILED = "verification_failed"
    RATE_LIMITED = "rate_limited"
    SERVICE_UNAVAILABLE = "service_unavailable"
    REGISTRATION_CANCELLED = "registration_cancelled"
    INTERNAL_ERROR = "internal_error"


class AcquisitionEventBase(GrowthEventEnvelopeBase):
    """Growth envelope keyed only by the acquisition journey identifier."""

    acquisition_id: UUID4


class LandingView(AcquisitionEventBase):
    event_name: Literal["landing_view"]
    source: Literal[EventSource.WEBSITE]
    app_version: None
    outcome: None
    error_code: None
    duration_ms: None


class DownloadClick(AcquisitionEventBase):
    event_name: Literal["download_click"]
    source: Literal[EventSource.WEBSITE]
    app_version: None
    outcome: None
    error_code: None
    duration_ms: None


class DownloadServed(AcquisitionEventBase):
    event_name: Literal["download_served"]
    source: Literal[EventSource.CDN]
    app_version: AppVersion
    outcome: Literal["success"]
    error_code: None
    duration_ms: None


class InstallStarted(AcquisitionEventBase):
    event_name: Literal["install_started"]
    source: Literal[EventSource.INSTALLER]
    app_version: AppVersion
    outcome: None
    error_code: None
    duration_ms: None


class InstallResult(AcquisitionEventBase):
    event_name: Literal["install_result"]
    source: Literal[EventSource.INSTALLER]
    app_version: AppVersion
    outcome: InstallOutcome
    error_code: InstallErrorCode | None
    duration_ms: DurationMs

    @model_validator(mode="after")
    def _validate_terminal_fields(self) -> Self:
        validate_success_error_pair(outcome=self.outcome, error_code=self.error_code)
        return self


class RegistrationStarted(AcquisitionEventBase):
    event_name: Literal["registration_started"]
    source: Literal[EventSource.DESKTOP]
    app_version: AppVersion
    outcome: None
    error_code: None
    duration_ms: None


class RegistrationResult(AcquisitionEventBase):
    event_name: Literal["registration_result"]
    source: Literal[EventSource.ACCOUNT_SERVICE]
    app_version: None
    outcome: RegistrationOutcome
    error_code: RegistrationErrorCode | None
    duration_ms: DurationMs
    analytics_user_id: UUID4 | None

    @model_validator(mode="after")
    def _validate_terminal_fields(self) -> Self:
        validate_success_error_pair(outcome=self.outcome, error_code=self.error_code)
        if self.outcome is RegistrationOutcome.SUCCESS:
            if self.analytics_user_id is None:
                raise ValueError("successful registration requires analytics_user_id")
        elif self.analytics_user_id is not None:
            raise ValueError("unsuccessful registration cannot include analytics_user_id")
        return self


class OnboardingCompleted(GrowthEventBase):
    event_name: Literal["onboarding_result"]
    source: Literal[EventSource.DESKTOP]
    outcome: Literal["completed"]
    flow_version: Annotated[int, Field(strict=True, ge=1, le=1)]


class FirstAppReady(GrowthEventBase):
    event_name: Literal["first_app_ready"]
    source: Literal[EventSource.DESKTOP]
    outcome: None


class FirstTurnStarted(GrowthEventBase):
    event_name: Literal["first_turn_started"]
    source: Literal[EventSource.GATEWAY]
    outcome: None


class FirstTurnSucceeded(GrowthEventBase):
    event_name: Literal["first_turn_result"]
    source: Literal[EventSource.RUNTIME]
    outcome: Literal["success"]


class ClientLaunch(GrowthEventBase):
    """One consented, deduplicated observation of a usable client surface."""

    event_name: Literal["client_launch"]
    source: Literal[EventSource.GATEWAY]
    outcome: None
    surface: ClientSurface
    entrypoint: ClientEntrypoint
    execution_mode: ExecutionMode


GrowthEvent = Annotated[
    LandingView
    | DownloadClick
    | DownloadServed
    | InstallStarted
    | InstallResult
    | RegistrationStarted
    | RegistrationResult
    | OnboardingCompleted
    | FirstAppReady
    | FirstTurnStarted
    | FirstTurnSucceeded
    | ClientLaunch,
    Field(discriminator="event_name"),
]

GROWTH_EVENT_ADAPTER: TypeAdapter[GrowthEvent] = TypeAdapter(GrowthEvent)


__all__ = [
    "AcquisitionEventBase",
    "ClientLaunch",
    "DownloadClick",
    "DownloadServed",
    "FirstAppReady",
    "FirstTurnStarted",
    "FirstTurnSucceeded",
    "GROWTH_EVENT_ADAPTER",
    "GrowthEvent",
    "InstallErrorCode",
    "InstallOutcome",
    "InstallResult",
    "InstallStarted",
    "LandingView",
    "OnboardingCompleted",
    "RegistrationErrorCode",
    "RegistrationOutcome",
    "RegistrationResult",
    "RegistrationStarted",
]
