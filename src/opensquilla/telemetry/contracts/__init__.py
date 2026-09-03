"""Public telemetry wire contracts.

The event variants are closed and versioned independently. Ingress code selects the leaf
model through :data:`EVENT_MODELS` using both name and version before accepting
future protocol revisions.  Untrusted bytes must enter through
:func:`parse_telemetry_wire`; exported adapters remain construction helpers for
already-trusted in-process data and are not raw-wire parsers.
"""

from __future__ import annotations

from types import MappingProxyType

from pydantic import TypeAdapter

from opensquilla.telemetry.contracts.batch import (
    GROWTH_BATCH_ADAPTER as GROWTH_BATCH_ADAPTER,
)
from opensquilla.telemetry.contracts.batch import (
    RELIABILITY_BATCH_ADAPTER as RELIABILITY_BATCH_ADAPTER,
)
from opensquilla.telemetry.contracts.batch import (
    TELEMETRY_BATCH_ADAPTER as TELEMETRY_BATCH_ADAPTER,
)
from opensquilla.telemetry.contracts.batch import (
    GrowthEventBatch,
    ReliabilityEventBatch,
    TelemetryBatch,
)
from opensquilla.telemetry.contracts.canonical import canonical_json, canonical_json_bytes
from opensquilla.telemetry.contracts.common import (
    ClientEntrypoint,
    ClientSurface,
    ConsentScope,
    EventSource,
    ExecutionMode,
    Platform,
)
from opensquilla.telemetry.contracts.growth import (
    GROWTH_EVENT_ADAPTER as GROWTH_EVENT_ADAPTER,
)
from opensquilla.telemetry.contracts.growth import (
    ClientLaunch,
    DownloadClick,
    DownloadServed,
    FirstAppReady,
    FirstTurnStarted,
    FirstTurnSucceeded,
    GrowthEvent,
    InstallResult,
    InstallStarted,
    LandingView,
    OnboardingCompleted,
    RegistrationResult,
    RegistrationStarted,
)
from opensquilla.telemetry.contracts.manifest import (
    CURRENT_NOTICE_VERSION_BY_SCOPE,
    MAX_GROWTH_BATCH_BYTES,
    MAX_RELIABILITY_BATCH_BYTES,
    MAX_TELEMETRY_EVENT_BYTES,
    MAX_TELEMETRY_NESTING_DEPTH,
    TELEMETRY_PROTOCOL_FINGERPRINT_SHA256,
    TELEMETRY_PROTOCOL_MANIFEST_JSON,
    telemetry_protocol_manifest,
)
from opensquilla.telemetry.contracts.reliability import (
    RELIABILITY_EVENT_ADAPTER as RELIABILITY_EVENT_ADAPTER,
)
from opensquilla.telemetry.contracts.reliability import (
    AppCrashDetected,
    AppStartResult,
    FileParseResult,
    FileParseResultV2,
    GatewayStartResult,
    PerformanceSummary,
    ReliabilityEvent,
    ToolCallResult,
    ToolCallResultV2,
    TurnResult,
    TurnResultV2,
    TurnResultV3,
    UpdateResult,
)
from opensquilla.telemetry.contracts.wire import (
    TelemetryWireError,
    TelemetryWireErrorCode,
    TelemetryWireModel,
    TelemetryWireTarget,
    parse_telemetry_wire,
)

TelemetryEvent = (
    AppStartResult
    | GatewayStartResult
    | AppCrashDetected
    | TurnResult
    | TurnResultV2
    | TurnResultV3
    | ToolCallResult
    | ToolCallResultV2
    | FileParseResult
    | FileParseResultV2
    | UpdateResult
    | PerformanceSummary
    | LandingView
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
    | ClientLaunch
)

TELEMETRY_EVENT_ADAPTER: TypeAdapter[TelemetryEvent] = TypeAdapter(TelemetryEvent)

EVENT_MODELS = MappingProxyType(
    {
        ("app_start_result", 1): AppStartResult,
        ("gateway_start_result", 1): GatewayStartResult,
        ("app_crash_detected", 1): AppCrashDetected,
        ("turn_result", 1): TurnResult,
        ("turn_result", 2): TurnResultV2,
        ("turn_result", 3): TurnResultV3,
        ("tool_call_result", 1): ToolCallResult,
        ("tool_call_result", 2): ToolCallResultV2,
        ("file_parse_result", 1): FileParseResult,
        ("file_parse_result", 2): FileParseResultV2,
        ("update_result", 1): UpdateResult,
        ("performance_summary", 1): PerformanceSummary,
        ("landing_view", 1): LandingView,
        ("download_click", 1): DownloadClick,
        ("download_served", 1): DownloadServed,
        ("install_started", 1): InstallStarted,
        ("install_result", 1): InstallResult,
        ("registration_started", 1): RegistrationStarted,
        ("registration_result", 1): RegistrationResult,
        ("onboarding_result", 1): OnboardingCompleted,
        ("first_app_ready", 1): FirstAppReady,
        ("first_turn_started", 1): FirstTurnStarted,
        ("first_turn_result", 1): FirstTurnSucceeded,
        ("client_launch", 1): ClientLaunch,
    }
)

__all__ = [
    "CURRENT_NOTICE_VERSION_BY_SCOPE",
    "EVENT_MODELS",
    "MAX_GROWTH_BATCH_BYTES",
    "MAX_RELIABILITY_BATCH_BYTES",
    "MAX_TELEMETRY_EVENT_BYTES",
    "MAX_TELEMETRY_NESTING_DEPTH",
    "TELEMETRY_PROTOCOL_FINGERPRINT_SHA256",
    "TELEMETRY_PROTOCOL_MANIFEST_JSON",
    "AppCrashDetected",
    "AppStartResult",
    "ClientLaunch",
    "ClientEntrypoint",
    "ClientSurface",
    "ConsentScope",
    "DownloadClick",
    "DownloadServed",
    "EventSource",
    "ExecutionMode",
    "FileParseResult",
    "FileParseResultV2",
    "FirstAppReady",
    "FirstTurnStarted",
    "FirstTurnSucceeded",
    "GatewayStartResult",
    "GrowthEvent",
    "GrowthEventBatch",
    "InstallResult",
    "InstallStarted",
    "LandingView",
    "OnboardingCompleted",
    "PerformanceSummary",
    "Platform",
    "ReliabilityEvent",
    "ReliabilityEventBatch",
    "RegistrationResult",
    "RegistrationStarted",
    "TelemetryBatch",
    "TelemetryEvent",
    "TelemetryWireError",
    "TelemetryWireErrorCode",
    "TelemetryWireModel",
    "TelemetryWireTarget",
    "ToolCallResult",
    "ToolCallResultV2",
    "TurnResult",
    "TurnResultV2",
    "TurnResultV3",
    "UpdateResult",
    "canonical_json",
    "canonical_json_bytes",
    "parse_telemetry_wire",
    "telemetry_protocol_manifest",
]
