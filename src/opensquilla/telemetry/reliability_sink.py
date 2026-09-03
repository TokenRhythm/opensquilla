"""Adapt content-free runtime facts into strict reliability events."""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from opensquilla import __version__
from opensquilla.telemetry.contracts import CURRENT_NOTICE_VERSION_BY_SCOPE
from opensquilla.telemetry.contracts.common import (
    ConsentScope,
    EventSource,
    Platform,
    ResultOutcome,
)
from opensquilla.telemetry.contracts.reliability import (
    FileParseResult,
    FileParseResultV2,
    ToolCallResult,
    ToolCallResultV2,
    ToolCategory,
    ToolErrorCode,
    ToolOutcome,
    TurnErrorCode,
    TurnFailureStage,
    TurnResultV3,
)
from opensquilla.telemetry.file_parse_facts import FileParseReliabilityFacts
from opensquilla.telemetry.ids import new_app_session_id, new_event_id
from opensquilla.telemetry.runtime import ScopedTelemetryRuntime
from opensquilla.telemetry.runtime_facts import current_client_runtime_dimensions


class TurnFacts(Protocol):
    outcome: ResultOutcome
    error_code: TurnErrorCode | None
    failure_stage: TurnFailureStage | None
    duration_ms: int
    ttft_ms: int | None
    stall_count: int


class ToolCallFacts(Protocol):
    outcome: ToolOutcome
    error_code: ToolErrorCode | None
    duration_ms: int
    tool_category: ToolCategory
    retry_count: int


class ReliabilityEventSink:
    """Process-session adapter exposed to content-free engine observers."""

    def __init__(
        self,
        runtime: ScopedTelemetryRuntime,
        *,
        app_version: str = __version__,
        platform: Platform | None = None,
        app_session_id: UUID | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._runtime = runtime
        self._app_version = app_version
        self._platform = platform or current_platform()
        self._app_session_id = app_session_id or new_app_session_id()
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def app_session_id(self) -> UUID:
        return self._app_session_id

    def observe_turn(self, facts: TurnFacts) -> None:
        """Build one closed-schema turn event and schedule best-effort storage."""

        try:
            dimensions = current_client_runtime_dimensions()
            event = TurnResultV3(
                event_name="turn_result",
                event_version=3,
                event_id=new_event_id(),
                occurred_at_utc=self._clock(),
                source=EventSource.GATEWAY,
                app_version=self._app_version,
                platform=self._platform,
                outcome=facts.outcome,
                error_code=facts.error_code,
                failure_stage=facts.failure_stage,
                duration_ms=facts.duration_ms,
                consent_scope=ConsentScope.RELIABILITY,
                notice_version=CURRENT_NOTICE_VERSION_BY_SCOPE["reliability"],
                sample_rate=1.0,
                app_session_id=self._app_session_id,
                ttft_ms=facts.ttft_ms,
                stall_count=facts.stall_count,
                stall_threshold_ms=15_000,
                **(
                    {}
                    if dimensions is None
                    else {
                        "surface": dimensions.surface,
                        "execution_mode": dimensions.execution_mode,
                    }
                ),
            )
        except Exception:
            return
        try:
            self._runtime.record_background(event)
        except Exception:
            return

    def observe_tool_call(self, facts: ToolCallFacts) -> None:
        """Build one closed-schema tool event without receiving tool content."""

        try:
            dimensions = current_client_runtime_dimensions()
            model = ToolCallResult if dimensions is None else ToolCallResultV2
            event = model(
                event_name="tool_call_result",
                event_version=1 if dimensions is None else 2,
                event_id=new_event_id(),
                occurred_at_utc=self._clock(),
                source=EventSource.RUNTIME,
                app_version=self._app_version,
                platform=self._platform,
                outcome=facts.outcome,
                error_code=facts.error_code,
                duration_ms=facts.duration_ms,
                consent_scope=ConsentScope.RELIABILITY,
                notice_version=CURRENT_NOTICE_VERSION_BY_SCOPE["reliability"],
                sample_rate=1.0,
                app_session_id=self._app_session_id,
                tool_category=facts.tool_category,
                retry_count=facts.retry_count,
                **(
                    {}
                    if dimensions is None
                    else {
                        "surface": dimensions.surface,
                        "execution_mode": dimensions.execution_mode,
                    }
                ),
            )
        except Exception:
            return
        try:
            self._runtime.record_background(event)
        except Exception:
            return

    def observe_file_parse(self, facts: FileParseReliabilityFacts) -> None:
        """Build one parser result after the worker returns to the event loop."""

        try:
            dimensions = current_client_runtime_dimensions()
            model = FileParseResult if dimensions is None else FileParseResultV2
            event = model(
                event_name="file_parse_result",
                event_version=1 if dimensions is None else 2,
                event_id=new_event_id(),
                occurred_at_utc=self._clock(),
                source=EventSource.RUNTIME,
                app_version=self._app_version,
                platform=self._platform,
                outcome=facts.outcome,
                error_code=facts.error_code,
                duration_ms=facts.duration_ms,
                consent_scope=ConsentScope.RELIABILITY,
                notice_version=CURRENT_NOTICE_VERSION_BY_SCOPE["reliability"],
                sample_rate=1.0,
                app_session_id=self._app_session_id,
                file_type=facts.file_type,
                size_bucket=facts.size_bucket,
                **(
                    {}
                    if dimensions is None
                    else {
                        "surface": dimensions.surface,
                        "execution_mode": dimensions.execution_mode,
                    }
                ),
            )
        except Exception:
            return
        try:
            self._runtime.record_background(event)
        except Exception:
            return


def current_platform() -> Platform:
    if sys.platform == "darwin":
        return Platform.MACOS
    if sys.platform.startswith("win"):
        return Platform.WINDOWS
    if sys.platform.startswith("linux"):
        return Platform.LINUX
    return Platform.UNKNOWN


__all__ = [
    "ReliabilityEventSink",
    "ToolCallFacts",
    "TurnFacts",
    "current_platform",
]
