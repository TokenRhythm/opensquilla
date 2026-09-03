"""Strict contracts for product-reliability telemetry events."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import StringConstraints, TypeAdapter, model_validator

from opensquilla.telemetry.contracts.common import (
    AppVersion,
    ClientSurface,
    Counter,
    DurationMs,
    EventSource,
    ExecutionMode,
    ReliabilityEventBase,
    ResultOutcome,
    validate_success_error_pair,
)


class AppStartFailureStage(StrEnum):
    PROFILE = "profile"
    GATEWAY_START = "gateway_start"
    GATEWAY_HEALTH = "gateway_health"
    CONTROL_UI = "control_ui"
    READY = "ready"


class AppStartErrorCode(StrEnum):
    PROFILE_RECOVERY_REQUIRED = "profile_recovery_required"
    KEYCHAIN_UNAVAILABLE = "keychain_unavailable"
    PROFILE_IN_USE = "profile_in_use"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    SPAWN_FAILED = "spawn_failed"
    HEALTH_TIMEOUT = "health_timeout"
    CONTROL_UI_TIMEOUT = "control_ui_timeout"
    OWNERSHIP_UNVERIFIED = "ownership_unverified"
    RENDERER_LOAD_FAILED = "renderer_load_failed"
    STARTUP_CANCELLED = "startup_cancelled"
    INTERNAL_ERROR = "internal_error"


class GatewayStartFailureStage(StrEnum):
    SPAWN = "spawn"
    HEALTH = "health"
    CONTROL_UI = "control_ui"
    OWNERSHIP = "ownership"


class GatewayStartupMode(StrEnum):
    SPAWNED = "spawned"
    REUSED = "reused"
    EXTERNAL = "external"


class GatewayStartErrorCode(StrEnum):
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    SPAWN_FAILED = "spawn_failed"
    HEALTH_TIMEOUT = "health_timeout"
    CONTROL_UI_TIMEOUT = "control_ui_timeout"
    OWNERSHIP_UNVERIFIED = "ownership_unverified"
    STARTUP_CANCELLED = "startup_cancelled"
    INTERNAL_ERROR = "internal_error"


class CrashComponent(StrEnum):
    DESKTOP_MAIN = "desktop_main"
    DESKTOP_RENDERER = "desktop_renderer"
    GATEWAY = "gateway"
    GPU = "gpu"
    UTILITY = "utility"
    UNKNOWN = "unknown"


class CrashErrorCode(StrEnum):
    UNCAUGHT_EXCEPTION = "uncaught_exception"
    RENDERER_CRASHED = "renderer_crashed"
    RENDERER_KILLED = "renderer_killed"
    GATEWAY_UNEXPECTED_EXIT = "gateway_unexpected_exit"
    CHILD_PROCESS_CRASHED = "child_process_crashed"
    STALE_SESSION_MARKER = "stale_session_marker"
    UNKNOWN = "unknown"


class TurnErrorCode(StrEnum):
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_AUTH = "provider_auth"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CONTEXT_LIMIT = "context_limit"
    REQUEST_TOO_LARGE = "request_too_large"
    OUTPUT_TRUNCATED = "output_truncated"
    BUDGET_EXHAUSTED = "budget_exhausted"
    HARD_DEADLINE = "hard_deadline"
    CANCELLED_BY_USER = "cancelled_by_user"
    CANCELLED_BEFORE_START = "cancelled_before_start"
    SHUTDOWN = "shutdown"
    PLATFORM_VALIDATION = "platform_validation"
    SAFETY_BLOCKED = "safety_blocked"
    INTERNAL_ERROR = "internal_error"
    UNKNOWN = "unknown"


class TurnFailureStage(StrEnum):
    TURN_SETUP = "turn_setup"
    INPUT_PROCESSING = "input_processing"
    PROVIDER_AND_TOOLS = "provider_and_tools"
    ATTACHMENT_PROCESSING = "attachment_processing"
    PROMPT_ASSEMBLY = "prompt_assembly"
    AGENT_BOOTSTRAP = "agent_bootstrap"
    CONTEXT_PREPARATION = "context_preparation"
    AGENT_EXECUTION = "agent_execution"
    RESULT_FINALIZATION = "result_finalization"


class ToolOutcome(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    TIMEOUT = "timeout"
    CANCEL = "cancel"
    DENIED = "denied"


class ToolCategory(StrEnum):
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    SHELL = "shell"
    CODE_EXECUTION = "code_execution"
    WEB = "web"
    SEARCH = "search"
    BROWSER = "browser"
    COMPUTER_USE = "computer_use"
    ARTIFACT = "artifact"
    DOCUMENT = "document"
    COLLABORATION = "collaboration"
    MCP_EXTENSION = "mcp_extension"
    OTHER = "other"


class ToolErrorCode(StrEnum):
    INJECTION_REJECTED = "injection_rejected"
    TOOL_UNAVAILABLE = "tool_unavailable"
    INVALID_ARGUMENTS = "invalid_arguments"
    POLICY_DENIED = "policy_denied"
    BUDGET_EXHAUSTED = "budget_exhausted"
    HANDLER_ERROR = "handler_error"
    FINALIZATION_ERROR = "finalization_error"
    TOOL_TIMEOUT = "tool_timeout"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


class FileType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    EMAIL = "email"
    TEXT = "text"


class FileSizeBucket(StrEnum):
    LT_100_KIB = "lt_100_kib"
    KIB_100_TO_1_MIB = "100_kib_1_mib"
    MIB_1_TO_10 = "1_10_mib"
    MIB_10_TO_50 = "10_50_mib"
    GTE_50_MIB = "gte_50_mib"


class FileParseErrorCode(StrEnum):
    INVALID_UTF8 = "invalid_utf8"
    MALFORMED_PDF = "malformed_pdf"
    NO_EXTRACTABLE_TEXT = "no_extractable_text"
    INVALID_OFFICE_CONTAINER = "invalid_office_container"
    DECOMPRESSION_LIMIT = "decompression_limit"
    PARSER_DEPENDENCY_MISSING = "parser_dependency_missing"
    PARSE_TIMEOUT = "parse_timeout"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


class UpdateStage(StrEnum):
    CHECK = "check"
    DOWNLOAD = "download"
    INSTALL = "install"
    RESTART = "restart"


class UpdateCheckResult(StrEnum):
    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"


class UpdateErrorCode(StrEnum):
    SOURCE_UNREACHABLE = "source_unreachable"
    MANIFEST_INVALID = "manifest_invalid"
    CHECKSUM_UNAVAILABLE = "checksum_unavailable"
    INTEGRITY_FAILED = "integrity_failed"
    DOWNLOAD_FAILED = "download_failed"
    INSTALL_FAILED = "install_failed"
    GATEWAY_SHUTDOWN_TIMEOUT = "gateway_shutdown_timeout"
    VERSION_UNCHANGED = "version_unchanged"
    RESTART_NOT_READY = "restart_not_ready"
    OPERATION_CANCELLED = "operation_cancelled"
    INTERNAL_ERROR = "internal_error"


class PerformanceSummaryKind(StrEnum):
    SESSION_END = "session_end"
    RECOVERED_ABNORMAL = "recovered_abnormal"


class PerformanceCoverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


ErrorFingerprint = Annotated[
    str,
    StringConstraints(min_length=64, max_length=64, pattern=r"[a-f0-9]{64}"),
]


class AppStartResult(ReliabilityEventBase):
    event_name: Literal["app_start_result"]
    source: Literal[EventSource.DESKTOP]
    outcome: ResultOutcome
    error_code: AppStartErrorCode | None
    duration_ms: DurationMs
    failure_stage: AppStartFailureStage | None

    @model_validator(mode="after")
    def _validate_terminal_fields(self) -> Self:
        validate_success_error_pair(outcome=self.outcome, error_code=self.error_code)
        if self.outcome is ResultOutcome.SUCCESS:
            if self.failure_stage is not None:
                raise ValueError("successful app start cannot include failure_stage")
        elif self.failure_stage is None:
            raise ValueError("non-success app start requires failure_stage")
        return self


class GatewayStartResult(ReliabilityEventBase):
    event_name: Literal["gateway_start_result"]
    source: Literal[EventSource.DESKTOP, EventSource.GATEWAY]
    outcome: ResultOutcome
    error_code: GatewayStartErrorCode | None
    duration_ms: DurationMs
    failure_stage: GatewayStartFailureStage | None
    startup_mode: GatewayStartupMode

    @model_validator(mode="after")
    def _validate_terminal_fields(self) -> Self:
        validate_success_error_pair(outcome=self.outcome, error_code=self.error_code)
        if self.outcome is ResultOutcome.SUCCESS:
            if self.failure_stage is not None:
                raise ValueError("successful gateway start cannot include failure_stage")
        elif self.failure_stage is None:
            raise ValueError("non-success gateway start requires failure_stage")
        return self


class AppCrashDetected(ReliabilityEventBase):
    event_name: Literal["app_crash_detected"]
    source: Literal[EventSource.DESKTOP]
    outcome: Literal["detected"]
    error_code: CrashErrorCode
    duration_ms: None
    component: CrashComponent
    error_fingerprint: ErrorFingerprint
    runtime_ms: DurationMs


class TurnResult(ReliabilityEventBase):
    event_name: Literal["turn_result"]
    source: Literal[EventSource.GATEWAY]
    outcome: ResultOutcome
    error_code: TurnErrorCode | None
    duration_ms: DurationMs
    ttft_ms: DurationMs | None
    stall_count: Counter
    stall_threshold_ms: Literal[15000]

    @model_validator(mode="after")
    def _validate_terminal_fields(self) -> Self:
        validate_success_error_pair(outcome=self.outcome, error_code=self.error_code)
        if self.ttft_ms is not None and self.ttft_ms > self.duration_ms:
            raise ValueError("ttft_ms cannot exceed duration_ms")
        return self


class TurnResultV2(TurnResult):
    """Turn result with a trusted client surface and execution topology."""

    event_version: Literal[2]  # type: ignore[assignment]
    surface: ClientSurface
    execution_mode: ExecutionMode


class TurnResultV3(TurnResult):
    """Turn result with a closed failure stage and optional client dimensions."""

    event_version: Literal[3]  # type: ignore[assignment]
    surface: ClientSurface | None = None
    execution_mode: ExecutionMode | None = None
    failure_stage: TurnFailureStage | None

    @model_validator(mode="after")
    def _validate_v3_terminal_fields(self) -> Self:
        if (self.surface is None) != (self.execution_mode is None):
            raise ValueError("surface and execution_mode must be supplied together")
        if self.outcome is ResultOutcome.SUCCESS:
            if self.failure_stage is not None:
                raise ValueError("successful turn cannot include failure_stage")
        elif self.failure_stage is None:
            raise ValueError("non-success turn requires failure_stage")
        return self


class ToolCallResult(ReliabilityEventBase):
    event_name: Literal["tool_call_result"]
    source: Literal[EventSource.RUNTIME]
    outcome: ToolOutcome
    error_code: ToolErrorCode | None
    duration_ms: DurationMs
    tool_category: ToolCategory
    retry_count: Counter

    @model_validator(mode="after")
    def _validate_terminal_fields(self) -> Self:
        validate_success_error_pair(outcome=self.outcome, error_code=self.error_code)
        return self


class ToolCallResultV2(ToolCallResult):
    """Tool result with the surface of its enclosing public turn."""

    event_version: Literal[2]  # type: ignore[assignment]
    surface: ClientSurface
    execution_mode: ExecutionMode


class FileParseResult(ReliabilityEventBase):
    event_name: Literal["file_parse_result"]
    source: Literal[EventSource.RUNTIME]
    outcome: ResultOutcome
    error_code: FileParseErrorCode | None
    duration_ms: DurationMs
    file_type: FileType
    size_bucket: FileSizeBucket

    @model_validator(mode="after")
    def _validate_terminal_fields(self) -> Self:
        validate_success_error_pair(outcome=self.outcome, error_code=self.error_code)
        return self


class FileParseResultV2(FileParseResult):
    """File parse result with the surface of its enclosing public turn."""

    event_version: Literal[2]  # type: ignore[assignment]
    surface: ClientSurface
    execution_mode: ExecutionMode


class UpdateResult(ReliabilityEventBase):
    event_name: Literal["update_result"]
    source: Literal[EventSource.UPDATER]
    outcome: ResultOutcome
    error_code: UpdateErrorCode | None
    duration_ms: DurationMs
    update_stage: UpdateStage
    old_version: AppVersion
    new_version: AppVersion | None
    result: UpdateCheckResult | None

    @model_validator(mode="after")
    def _validate_terminal_fields(self) -> Self:
        validate_success_error_pair(outcome=self.outcome, error_code=self.error_code)

        if self.update_stage is UpdateStage.CHECK:
            if self.outcome is ResultOutcome.SUCCESS and self.result is None:
                raise ValueError("successful update check requires result")
            if self.outcome is not ResultOutcome.SUCCESS and self.result is not None:
                raise ValueError("failed update check cannot include result")
            if self.result is UpdateCheckResult.AVAILABLE and self.new_version is None:
                raise ValueError("available update requires new_version")
            if self.result is UpdateCheckResult.NOT_AVAILABLE and self.new_version is not None:
                raise ValueError("not-available update cannot include new_version")
        else:
            if self.result is not None:
                raise ValueError("only update checks can include result")
            if self.new_version is None:
                raise ValueError("download/install/restart events require new_version")
        return self


class PerformanceSummary(ReliabilityEventBase):
    event_name: Literal["performance_summary"]
    source: Literal[EventSource.DESKTOP]
    outcome: Literal["success"]
    error_code: None
    duration_ms: DurationMs
    sample_rate: Literal[1]
    summary_kind: PerformanceSummaryKind
    coverage: PerformanceCoverage
    turn_count: Counter
    stalled_turn_count: Counter
    stall_count: Counter
    stall_threshold_ms: Literal[15000]
    monitored_request_count: Counter
    slow_request_count: Counter
    slow_request_threshold_ms: Literal[30000]
    foreground_duration_ms: DurationMs
    background_duration_ms: DurationMs

    @model_validator(mode="after")
    def _validate_summary(self) -> Self:
        if self.stalled_turn_count > self.turn_count:
            raise ValueError("stalled_turn_count cannot exceed turn_count")
        if self.stall_count < self.stalled_turn_count:
            raise ValueError("stall_count cannot be lower than stalled_turn_count")
        if self.slow_request_count > self.monitored_request_count:
            raise ValueError("slow_request_count cannot exceed monitored_request_count")
        if self.foreground_duration_ms + self.background_duration_ms > self.duration_ms:
            raise ValueError("foreground and background durations cannot exceed duration_ms")
        if (
            self.summary_kind is PerformanceSummaryKind.SESSION_END
            and self.coverage is not PerformanceCoverage.COMPLETE
        ):
            raise ValueError("session_end summaries require complete coverage")
        if (
            self.summary_kind is PerformanceSummaryKind.RECOVERED_ABNORMAL
            and self.coverage is not PerformanceCoverage.PARTIAL
        ):
            raise ValueError("recovered abnormal summaries require partial coverage")
        return self


ReliabilityEvent = (
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
)

RELIABILITY_EVENT_ADAPTER: TypeAdapter[ReliabilityEvent] = TypeAdapter(ReliabilityEvent)


__all__ = [
    "AppCrashDetected",
    "AppStartErrorCode",
    "AppStartFailureStage",
    "AppStartResult",
    "CrashComponent",
    "CrashErrorCode",
    "FileParseErrorCode",
    "FileParseResult",
    "FileParseResultV2",
    "FileSizeBucket",
    "FileType",
    "GatewayStartErrorCode",
    "GatewayStartFailureStage",
    "GatewayStartResult",
    "GatewayStartupMode",
    "PerformanceCoverage",
    "PerformanceSummary",
    "PerformanceSummaryKind",
    "RELIABILITY_EVENT_ADAPTER",
    "ReliabilityEvent",
    "ToolCallResult",
    "ToolCallResultV2",
    "ToolCategory",
    "ToolErrorCode",
    "ToolOutcome",
    "TurnErrorCode",
    "TurnFailureStage",
    "TurnResult",
    "TurnResultV2",
    "TurnResultV3",
    "UpdateCheckResult",
    "UpdateErrorCode",
    "UpdateResult",
    "UpdateStage",
]
