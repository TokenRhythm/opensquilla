from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from opensquilla import __version__
from opensquilla.telemetry.contracts.common import (
    ClientSurface,
    ExecutionMode,
    Platform,
    ResultOutcome,
)
from opensquilla.telemetry.contracts.reliability import (
    FileParseErrorCode,
    FileSizeBucket,
    FileType,
    ToolCategory,
    ToolErrorCode,
    ToolOutcome,
    TurnErrorCode,
    TurnFailureStage,
)
from opensquilla.telemetry.file_parse_facts import FileParseReliabilityFacts
from opensquilla.telemetry.reliability_sink import ReliabilityEventSink
from opensquilla.telemetry.runtime_facts import (
    reset_client_runtime_dimensions,
    set_client_runtime_dimensions,
)


class CapturingRuntime:
    def __init__(self) -> None:
        self.events = []

    def record_background(self, event, *, priority=None) -> None:
        self.events.append(event)


@dataclass(frozen=True)
class TurnFacts:
    outcome: ResultOutcome
    error_code: TurnErrorCode | None
    failure_stage: TurnFailureStage | None
    duration_ms: int
    ttft_ms: int | None
    stall_count: int


@dataclass(frozen=True)
class ToolFacts:
    outcome: ToolOutcome
    error_code: ToolErrorCode | None
    duration_ms: int
    tool_category: ToolCategory
    retry_count: int


def _sink(runtime: CapturingRuntime) -> ReliabilityEventSink:
    return ReliabilityEventSink(
        runtime,  # type: ignore[arg-type]
        app_version="1.2.3",
        platform=Platform.LINUX,
        app_session_id=UUID("00000000-0000-4000-8000-000000000900"),
        clock=lambda: datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC),
    )


def test_turn_facts_become_one_closed_contract_event() -> None:
    runtime = CapturingRuntime()
    sink = _sink(runtime)

    sink.observe_turn(
        TurnFacts(
            outcome=ResultOutcome.TIMEOUT,
            error_code=TurnErrorCode.PROVIDER_TIMEOUT,
            failure_stage=TurnFailureStage.AGENT_EXECUTION,
            duration_ms=31_000,
            ttft_ms=400,
            stall_count=2,
        )
    )

    assert len(runtime.events) == 1
    event = runtime.events[0]
    assert event.event_name == "turn_result"
    assert event.outcome == "timeout"
    assert event.error_code is TurnErrorCode.PROVIDER_TIMEOUT
    assert event.app_session_id == sink.app_session_id
    assert event.model_dump().keys() == {
        "event_name",
        "event_version",
        "event_id",
        "occurred_at_utc",
        "source",
        "app_version",
        "platform",
        "outcome",
        "error_code",
        "failure_stage",
        "duration_ms",
        "consent_scope",
        "notice_version",
        "sample_rate",
        "app_session_id",
        "ttft_ms",
            "stall_count",
            "stall_threshold_ms",
            "surface",
            "execution_mode",
        }


def test_default_app_version_includes_source_commit_for_runtime_events(monkeypatch) -> None:
    runtime = CapturingRuntime()
    commit_id = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(
        "opensquilla.telemetry.reliability_sink.reliability_app_version",
        lambda version: f"{version}+source.{commit_id}",
    )
    sink = ReliabilityEventSink(
        runtime,  # type: ignore[arg-type]
        platform=Platform.LINUX,
        app_session_id=UUID("00000000-0000-4000-8000-000000000900"),
        clock=lambda: datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC),
    )

    sink.observe_turn(
        TurnFacts(
            outcome=ResultOutcome.SUCCESS,
            error_code=None,
            failure_stage=None,
            duration_ms=10,
            ttft_ms=2,
            stall_count=0,
        )
    )
    sink.observe_tool_call(
        ToolFacts(
            outcome=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=5,
            tool_category=ToolCategory.SHELL,
            retry_count=0,
        )
    )
    sink.observe_file_parse(
        FileParseReliabilityFacts(
            file_type=FileType.TEXT,
            size_bucket=FileSizeBucket.LT_100_KIB,
            outcome=ResultOutcome.SUCCESS,
            error_code=None,
            duration_ms=3,
        )
    )

    assert len(runtime.events) == 3
    assert {event.app_version for event in runtime.events} == {
        f"{__version__}+source.{commit_id}"
    }


def test_explicit_app_version_is_not_rewritten_even_when_it_matches_package_version(
    monkeypatch,
) -> None:
    runtime = CapturingRuntime()
    monkeypatch.setattr(
        "opensquilla.telemetry.reliability_sink.reliability_app_version",
        lambda _version: "unexpected",
    )
    sink = ReliabilityEventSink(
        runtime,  # type: ignore[arg-type]
        app_version=__version__,
        platform=Platform.LINUX,
        app_session_id=UUID("00000000-0000-4000-8000-000000000900"),
        clock=lambda: datetime(2026, 9, 2, 1, 2, 3, tzinfo=UTC),
    )

    sink.observe_turn(
        TurnFacts(
            outcome=ResultOutcome.SUCCESS,
            error_code=None,
            failure_stage=None,
            duration_ms=10,
            ttft_ms=2,
            stall_count=0,
        )
    )

    assert runtime.events[0].app_version == __version__


def test_tool_facts_never_require_name_arguments_or_result_content() -> None:
    runtime = CapturingRuntime()
    sink = _sink(runtime)

    sink.observe_tool_call(
        ToolFacts(
            outcome=ToolOutcome.DENIED,
            error_code=ToolErrorCode.POLICY_DENIED,
            duration_ms=5,
            tool_category=ToolCategory.SHELL,
            retry_count=0,
        )
    )

    assert len(runtime.events) == 1
    event = runtime.events[0]
    assert event.event_name == "tool_call_result"
    assert event.tool_category is ToolCategory.SHELL
    assert "name" not in event.model_dump()
    assert "arguments" not in event.model_dump()
    assert "content" not in event.model_dump()


def test_invalid_facts_are_dropped_without_reaching_runtime() -> None:
    runtime = CapturingRuntime()
    sink = _sink(runtime)

    sink.observe_turn(
        TurnFacts(
            outcome=ResultOutcome.SUCCESS,
            error_code=TurnErrorCode.INTERNAL_ERROR,
            failure_stage=None,
            duration_ms=10,
            ttft_ms=None,
            stall_count=0,
        )
    )

    assert runtime.events == []


def test_file_parse_fact_becomes_content_free_contract_event() -> None:
    runtime = CapturingRuntime()
    sink = _sink(runtime)

    sink.observe_file_parse(
        FileParseReliabilityFacts(
            file_type=FileType.DOCX,
            size_bucket=FileSizeBucket.MIB_1_TO_10,
            outcome=ResultOutcome.FAIL,
            error_code=FileParseErrorCode.INVALID_OFFICE_CONTAINER,
            duration_ms=22,
        )
    )

    assert len(runtime.events) == 1
    event = runtime.events[0]
    assert event.event_name == "file_parse_result"
    assert event.file_type is FileType.DOCX
    assert event.size_bucket is FileSizeBucket.MIB_1_TO_10
    assert "filename" not in event.model_dump()
    assert "path" not in event.model_dump()
    assert "content" not in event.model_dump()


def test_runtime_failure_does_not_escape_engine_observer() -> None:
    class FailingRuntime(CapturingRuntime):
        def record_background(self, event, *, priority=None) -> None:
            raise RuntimeError("synthetic")

    sink = _sink(FailingRuntime())

    # Scheduling is a best-effort boundary and must be exception-safe too.
    sink.observe_tool_call(
        ToolFacts(
            outcome=ToolOutcome.SUCCESS,
            error_code=None,
            duration_ms=1,
            tool_category=ToolCategory.OTHER,
            retry_count=0,
        )
    )


def test_trusted_runtime_dimensions_select_v2_contract() -> None:
    runtime = CapturingRuntime()
    sink = _sink(runtime)
    token = set_client_runtime_dimensions(ClientSurface.TUI, ExecutionMode.GATEWAY)
    try:
        sink.observe_turn(
            TurnFacts(
                outcome=ResultOutcome.SUCCESS,
                error_code=None,
                failure_stage=None,
                duration_ms=10,
                ttft_ms=2,
                stall_count=0,
            )
        )
    finally:
        reset_client_runtime_dimensions(token)

    event = runtime.events[0]
    assert event.event_version == 3
    assert event.surface is ClientSurface.TUI
    assert event.execution_mode is ExecutionMode.GATEWAY
