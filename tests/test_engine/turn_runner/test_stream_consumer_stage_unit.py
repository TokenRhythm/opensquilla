"""Unit tests for ``StreamConsumerStage`` driven directly (no full
TurnRunner stack).

Drives the stage through ``StreamConsumerStage.run`` with recording
fakes for all five ports + the warning transformer, plus per-handler
unit tests for the eight internal handler classes.

Raising-fake cases exercise the exception-propagation contracts without the
runtime wrapper.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.contracts.turn_execution import (
    SurfaceCapabilities,
    TurnExecutionContext,
    TurnIdentity,
)
from opensquilla.engine.agent_injection import ListPendingInputProvider
from opensquilla.engine.turn_runner.stream_consumer_stage import (
    _SUPPRESS,
    StreamConsumerStage,
    StreamConsumerStageInput,
    _ArtifactHandler,
    _CompactionHandler,
    _DoneHandler,
    _ErrorHandler,
    _StreamState,
    _TextDeltaHandler,
    _ToolResultHandler,
    _ToolUseStartHandler,
    _WarningHandler,
)
from opensquilla.engine.types import (
    AnswerGenerationResetEvent,
    ArtifactEvent,
    CompactionEvent,
    DoneEvent,
    EnsembleProgressEvent,
    ErrorEvent,
    TextDeltaEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseStartEvent,
    WarningEvent,
)
from opensquilla.provider.types import EnsembleProgressEvent as ProviderEnsembleProgressEvent
from opensquilla.silent_reply import (
    SILENT_REPLY_NOT_ALLOWED_CODE,
    SILENT_REPLY_NOT_ALLOWED_MESSAGE,
)
from opensquilla.tools.types import ToolContext

# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAgentRun:
    events: list[Any] = field(default_factory=list)
    raises: type[BaseException] | None = None
    received: list[dict[str, Any]] = field(default_factory=list)

    def run_turn(
        self,
        agent: Any,
        *,
        turn_input: str,
        extra_messages: list[Any] | None,
        semantic_message: str | None,
        pending_input_provider: Any | None = None,
    ) -> AsyncIterator[Any]:
        self.received.append(
            {
                "agent": agent,
                "turn_input": turn_input,
                "extra_messages": extra_messages,
                "semantic_message": semantic_message,
                "pending_input_provider": pending_input_provider,
            }
        )
        events = list(self.events)
        raises = self.raises

        async def _iter():
            for ev in events:
                yield ev
            if raises is not None:
                raise raises("recording agent boom")

        return _iter()


@dataclass
class _RecordingCompactionPersist:
    calls: list[dict[str, Any]] = field(default_factory=list)
    raises: type[BaseException] | None = None
    result: bool | None = None

    async def persist_and_notify(
        self,
        *,
        session_key: str,
        summary: str,
        kept_entries: list[Any],
        summary_payload: dict[str, Any] | None = None,
        summary_format: str = "text",
        coverage_status: str = "unknown",
        missing_obligations: list[str] | None = None,
        critical_carry_forward: list[str] | None = None,
        compaction_id: str | None = None,
        compaction_deadline_at_monotonic: float | None = None,
        compaction_timeout_seconds: float | None = None,
        removed_count: int = 0,
        source_entries: tuple[Any, ...] | None = None,
        source_preimage: tuple[tuple[Any, ...], ...] | None = None,
        source_boundary_message_id: str | None = None,
        source_boundary_entry_id: int | None = None,
    ) -> bool | None:
        self.calls.append(
            {
                "session_key": session_key,
                "summary": summary,
                "kept_entries": kept_entries,
                "summary_payload": summary_payload,
                "summary_format": summary_format,
                "coverage_status": coverage_status,
                "missing_obligations": missing_obligations,
                "critical_carry_forward": critical_carry_forward,
                "compaction_id": compaction_id,
                "compaction_deadline_at_monotonic": (
                    compaction_deadline_at_monotonic
                ),
                "compaction_timeout_seconds": compaction_timeout_seconds,
                "removed_count": removed_count,
                "source_entries": source_entries,
                "source_preimage": source_preimage,
                "source_boundary_message_id": source_boundary_message_id,
                "source_boundary_entry_id": source_boundary_entry_id,
            }
        )
        if self.raises is not None:
            raise self.raises("recording persist boom")
        return self.result


@dataclass
class _RecordingMemorySnapshotRefresh:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def refresh_snapshot(
        self,
        *,
        agent_id: str,
        session_key: str,
        private_memory_allowed: bool,
    ) -> None:
        self.calls.append(
            {
                "agent_id": agent_id,
                "session_key": session_key,
                "private_memory_allowed": private_memory_allowed,
            }
        )


@dataclass
class _RecordingSystemPromptRefresh:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def refresh_system_prompt(
        self,
        *,
        agent: Any,
        agent_id: str,
        tool_defs: list[Any],
        session_key: str,
        bootstrap_context_mode: str | None,
    ) -> None:
        self.calls.append(
            {
                "agent": agent,
                "agent_id": agent_id,
                "tool_defs": tool_defs,
                "session_key": session_key,
                "bootstrap_context_mode": bootstrap_context_mode,
            }
        )


@dataclass
class _RecordingMemorySyncNotify:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def notify_message_bytes(
        self,
        sync_manager: Any | None,
        runtime_message: str,
    ) -> None:
        self.calls.append(
            {
                "sync_manager_present": sync_manager is not None,
                "runtime_message": runtime_message,
            }
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_turn(metadata: dict[str, Any] | None = None, tool_defs: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        metadata=metadata if metadata is not None else {},
        tool_defs=tool_defs if tool_defs is not None else [],
    )


def _make_state() -> _StreamState:
    return _StreamState(
        current_text_parts=[],
        final_text_parts=[],
        turn_segments=[],
        turn_artifacts=[],
        artifact_delivery_failures=[],
    )


def _make_input(
    *,
    state: _StreamState | None = None,
    turn: Any | None = None,
    run_kind: str = "default",
    input_mode: str = "user",
    session_manager_present: bool = True,
    private_memory_allowed: bool = True,
    sync_manager: Any | None = None,
    input_provenance: dict[str, Any] | None = None,
    pending_input_provider: Any | None = None,
    tool_context: Any | None = None,
    compaction_source_entries: tuple[Any, ...] | None = None,
    compaction_source_preimage: tuple[tuple[Any, ...], ...] | None = None,
    compaction_source_boundary_message_id: str | None = None,
    compaction_source_boundary_entry_id: int | None = None,
    execution_context: TurnExecutionContext | None = None,
) -> StreamConsumerStageInput:
    return StreamConsumerStageInput(
        agent=SimpleNamespace(),
        agent_id="agent:main",
        sync_manager=sync_manager,
        private_memory_allowed=private_memory_allowed,
        turn=turn if turn is not None else _make_turn(),
        tool_defs=[],
        turn_input="hi",
        extra_messages=None,
        semantic_input="hi",
        effective_runtime_message="hello there",
        input_provenance=input_provenance,
        session_key="agent:main:s1",
        run_kind=run_kind,
        heartbeat_ack_max_chars=300,
        bootstrap_context_mode=None,
        router_cfg=None,
        session_manager_present=session_manager_present,
        state=state if state is not None else _make_state(),
        pending_input_provider=pending_input_provider,
        tool_context=tool_context,
        compaction_source_entries=compaction_source_entries,
        compaction_source_preimage=compaction_source_preimage,
        compaction_source_boundary_message_id=(
            compaction_source_boundary_message_id
        ),
        compaction_source_boundary_entry_id=compaction_source_boundary_entry_id,
        input_mode=input_mode,
        execution_context=execution_context,
    )


def _make_stage(
    *,
    agent_run: _RecordingAgentRun | None = None,
    compaction_persist: _RecordingCompactionPersist | None = None,
    memory_snapshot_refresh: _RecordingMemorySnapshotRefresh | None = None,
    system_prompt_refresh: _RecordingSystemPromptRefresh | None = None,
    memory_sync_notify: _RecordingMemorySyncNotify | None = None,
    warning_transformer=None,
) -> tuple[StreamConsumerStage, dict[str, Any]]:
    agent_run = agent_run or _RecordingAgentRun()
    compaction_persist = compaction_persist or _RecordingCompactionPersist()
    memory_snapshot_refresh = (
        memory_snapshot_refresh or _RecordingMemorySnapshotRefresh()
    )
    system_prompt_refresh = (
        system_prompt_refresh or _RecordingSystemPromptRefresh()
    )
    memory_sync_notify = memory_sync_notify or _RecordingMemorySyncNotify()
    if warning_transformer is None:
        warning_transformer = lambda event: event  # noqa: E731

    stage = StreamConsumerStage(
        agent_run=agent_run,
        compaction_persist=compaction_persist,
        memory_snapshot_refresh=memory_snapshot_refresh,
        system_prompt_refresh=system_prompt_refresh,
        memory_sync_notify=memory_sync_notify,
        warning_transformer=warning_transformer,
    )
    recordings = {
        "agent_run": agent_run,
        "compaction_persist": compaction_persist,
        "memory_snapshot_refresh": memory_snapshot_refresh,
        "system_prompt_refresh": system_prompt_refresh,
        "memory_sync_notify": memory_sync_notify,
    }
    return stage, recordings


async def _drain(stage: StreamConsumerStage, inp: StreamConsumerStageInput) -> list[Any]:
    yielded: list[Any] = []
    async for event in stage.run(inp):
        yielded.append(event)
    return yielded


@pytest.mark.asyncio
async def test_stream_consumer_forwards_pending_input_provider_to_agent_run() -> None:
    pending = ListPendingInputProvider()
    pending.append("interrupt while tool runs")
    stage, recordings = _make_stage()

    await _drain(stage, _make_input(pending_input_provider=pending))

    assert recordings["agent_run"].received[0]["pending_input_provider"] is pending


@pytest.mark.asyncio
async def test_stream_consumer_normalizes_provider_ensemble_progress_events() -> None:
    stage, _recordings = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                ProviderEnsembleProgressEvent(
                    event_type="proposer_start",
                    proposer_index=1,
                    proposer_label="proposer_2",
                    proposer_model="z-ai/glm-5.2",
                    proposer_provider="openrouter",
                    sample_index=0,
                    elapsed_ms=12,
                    input_tokens=3,
                    output_tokens=4,
                    cost_usd=0.005,
                    error="",
                ),
            ],
        )
    )

    events = await _drain(stage, _make_input())

    assert isinstance(events[0], EnsembleProgressEvent)
    assert events[0].event_type == "proposer_start"
    assert events[0].proposer_index == 1
    assert events[0].proposer_label == "proposer_2"
    assert events[0].proposer_model == "z-ai/glm-5.2"
    assert events[0].proposer_provider == "openrouter"
    assert events[0].sample_index == 0
    assert events[0].elapsed_ms == 12
    assert events[0].input_tokens == 3
    assert events[0].output_tokens == 4
    assert events[0].cost_usd == 0.005
    assert events[0].error == ""


@pytest.mark.asyncio
async def test_stream_consumer_reset_clears_answer_and_keeps_completed_tool_timeline() -> None:
    state = _make_state()
    stage, _recordings = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="old answer"),
                ToolUseStartEvent(tool_use_id="done-call", tool_name="record"),
                ToolResultEvent(
                    tool_use_id="done-call",
                    tool_name="record",
                    result="completed",
                ),
                TextDeltaEvent(text="old suffix"),
                AnswerGenerationResetEvent(
                    turn_id="turn-reset",
                    assistant_message_id="assistant-reset",
                    old_generation_epoch=0,
                    new_generation_epoch=1,
                    safe_reason="takeover",
                    sequence=5,
                ),
                TextDeltaEvent(text="new answer", generation_epoch=1),
                ThinkingEvent(text="new reasoning", generation_epoch=1),
                DoneEvent(text="new answer", generation_epoch=1),
            ],
        )
    )

    events = await _drain(stage, _make_input(state=state))

    assert any(isinstance(event, AnswerGenerationResetEvent) for event in events)
    assert state.final_text_parts == ["new answer"]
    assert state.reasoning == ["new reasoning"]
    assert [segment["type"] for segment in state.turn_segments] == [
        "tool_use",
        "tool_result",
    ]


@pytest.mark.asyncio
async def test_terminal_reset_is_only_public_outcome_and_keeps_accounting_done() -> None:
    state = _make_state()
    terminal_text = "The fixed model could not complete this answer."
    stage, _recordings = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="partial fixed answer"),
                AnswerGenerationResetEvent(
                    turn_id="turn-terminal-reset",
                    assistant_message_id="assistant-terminal-reset",
                    old_generation_epoch=1,
                    new_generation_epoch=2,
                    safe_reason="fixed provider final failure",
                    sequence=7,
                    terminal=True,
                    terminal_text_snapshot=terminal_text,
                    terminal_error_message="The provider rejected the request.",
                    terminal_error_code="provider_bad_request",
                    terminal_failure_kind="bad_request",
                ),
                DoneEvent(
                    text="",
                    text_snapshot="",
                    input_tokens=9,
                    output_tokens=3,
                    generation_epoch=2,
                ),
            ]
        )
    )

    events = await _drain(stage, _make_input(state=state))

    assert [type(event) for event in events] == [
        TextDeltaEvent,
        AnswerGenerationResetEvent,
    ]
    assert not any(isinstance(event, (DoneEvent, ErrorEvent)) for event in events)
    assert state.final_text_parts == [terminal_text]
    assert state.current_text_parts == [terminal_text]
    assert state.turn_segments == []
    assert state.done_event is not None
    assert state.done_event.text_snapshot == terminal_text
    assert state.done_event.input_tokens == 9
    assert state.done_event.output_tokens == 3
    assert state.error_message == "The provider rejected the request."
    assert state.pending_error_event is not None
    assert state.pending_error_event.code == "provider_bad_request"
    assert state.pending_error_event.failure_kind == "bad_request"
    assert state.pending_error_event.generation_epoch == 2
    public_reset = next(
        event for event in events if isinstance(event, AnswerGenerationResetEvent)
    )
    assert public_reset.terminal_error_message == "The provider rejected the request."
    assert public_reset.terminal_error_code == "provider_bad_request"
    assert public_reset.terminal_failure_kind == "bad_request"


@pytest.mark.asyncio
async def test_non_reset_surface_projects_terminal_reset_to_one_legacy_error() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity(
            "turn-terminal-legacy",
            "assistant-terminal-legacy",
            "agent:main:terminal-legacy",
        ),
        surface=SurfaceCapabilities(
            supports_streaming=False,
            supports_edit=False,
            supports_generation_reset=False,
        ),
    )
    state = _make_state()
    terminal_text = "The model could not complete this answer."
    stage, _recordings = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="superseded partial"),
                AnswerGenerationResetEvent(
                    turn_id="turn-terminal-legacy",
                    assistant_message_id="assistant-terminal-legacy",
                    old_generation_epoch=1,
                    new_generation_epoch=2,
                    safe_reason="fixed provider final failure",
                    sequence=8,
                    terminal=True,
                    terminal_text_snapshot=terminal_text,
                    terminal_error_message="internal provider detail",
                    terminal_error_code="provider_bad_request",
                    terminal_failure_kind="bad_request",
                ),
                DoneEvent(input_tokens=7, output_tokens=2, generation_epoch=2),
            ]
        )
    )

    events = await _drain(
        stage,
        _make_input(state=state, execution_context=context),
    )

    assert [type(event) for event in events] == [ErrorEvent]
    assert events[0].message == terminal_text
    assert events[0].code == "ensemble_fixed_error"
    assert events[0].failure_kind == ""
    assert state.error_message == "internal provider detail"
    assert state.pending_error_event is not None
    assert state.pending_error_event.code == "provider_bad_request"
    assert state.done_event is not None
    assert state.done_event.input_tokens == 7
    assert state.done_event.output_tokens == 2
    assert state.final_text_parts == [terminal_text]


@pytest.mark.asyncio
async def test_non_editable_surface_hides_replaced_text_until_fixed_done() -> None:
    context = TurnExecutionContext.create(
        TurnIdentity(
            "turn-non-edit",
            "assistant-non-edit",
            "agent:main:non-edit",
        ),
        surface=SurfaceCapabilities(
            supports_streaming=False,
            supports_edit=False,
            supports_generation_reset=False,
        ),
    )
    stage, _recordings = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="old answer", generation_epoch=0),
                ThinkingEvent(text="old reasoning", generation_epoch=0),
                AnswerGenerationResetEvent(
                    turn_id="turn-non-edit",
                    assistant_message_id="assistant-non-edit",
                    old_generation_epoch=0,
                    new_generation_epoch=1,
                    safe_reason="fixed takeover",
                    sequence=3,
                ),
                TextDeltaEvent(text="fixed answer", generation_epoch=1),
                DoneEvent(
                    text="fixed answer",
                    text_snapshot="fixed answer",
                    generation_epoch=1,
                ),
            ],
        )
    )

    events = await _drain(
        stage,
        _make_input(execution_context=context),
    )

    assert [event.text for event in events if isinstance(event, TextDeltaEvent)] == [
        "fixed answer"
    ]
    assert not any(
        isinstance(event, ThinkingEvent) and event.text == "old reasoning"
        for event in events
    )
    assert not any(isinstance(event, AnswerGenerationResetEvent) for event in events)
    assert sum(isinstance(event, DoneEvent) for event in events) == 1


# ---------------------------------------------------------------------------
# Per-handler tests
# ---------------------------------------------------------------------------


def test_text_delta_handler_appends_to_both_buffers() -> None:
    state = _make_state()
    handler = _TextDeltaHandler()
    out = handler.handle(TextDeltaEvent(text="hi"), state)
    assert out.text == "hi"
    assert state.final_text_parts == ["hi"]
    assert state.current_text_parts == ["hi"]


def test_text_delta_handler_preserves_protocol_like_html_as_canonical_text() -> None:
    state = _make_state()
    handler = _TextDeltaHandler()
    payload = (
        "Let me write the dashboard now.\n\n"
        '<tvoe_calls><invoke name="write_file">'
        '<parameter name="path">index.html</parameter>'
        '<parameter name="content"><!DOCTYPE html><html><body>app</body></html>'
        "</parameter></invoke></tvoe_calls>"
    )

    out = handler.handle(TextDeltaEvent(text=payload), state)

    assert out.text == payload
    assert state.final_text_parts == [payload]
    assert state.current_text_parts == [payload]


def test_text_delta_handler_preserves_split_protocol_like_html() -> None:
    state = _make_state()
    handler = _TextDeltaHandler()

    first_chunk = "Let me write the dashboard now.\n\n<tvoe"
    second_chunk = (
        '_calls><invoke name="write_file">'
        '<parameter name="content"><!DOCTYPE html><html></html>'
    )
    first = handler.handle(TextDeltaEvent(text=first_chunk), state)
    second = handler.handle(TextDeltaEvent(text=second_chunk), state)

    assert first.text == first_chunk
    assert second.text == second_chunk
    assert "".join(state.final_text_parts) == first_chunk + second_chunk


def test_text_delta_handler_strips_cumulative_post_tool_snapshot() -> None:
    state = _make_state()
    text_handler = _TextDeltaHandler()
    text_handler.handle(TextDeltaEvent(text="prefix"), state)
    _ToolUseStartHandler().handle(
        ToolUseStartEvent(tool_use_id="tool-1", tool_name="web_search"),
        state,
    )
    _ToolResultHandler().handle(
        ToolResultEvent(tool_use_id="tool-1", tool_name="web_search", result="ok"),
        state,
    )

    out = text_handler.handle(TextDeltaEvent(text="prefixsuffix"), state)

    assert out.text == "suffix"
    assert state.final_text_parts == ["prefix", "suffix"]
    assert state.current_text_parts == ["suffix"]


def test_text_delta_handler_preserves_plain_post_tool_delta() -> None:
    state = _make_state()
    text_handler = _TextDeltaHandler()
    text_handler.handle(TextDeltaEvent(text="prefix"), state)
    _ToolUseStartHandler().handle(
        ToolUseStartEvent(tool_use_id="tool-1", tool_name="web_search"),
        state,
    )
    _ToolResultHandler().handle(
        ToolResultEvent(tool_use_id="tool-1", tool_name="web_search", result="ok"),
        state,
    )

    out = text_handler.handle(TextDeltaEvent(text="suffix"), state)

    assert out.text == "suffix"
    assert state.final_text_parts == ["prefix", "suffix"]
    assert state.current_text_parts == ["suffix"]


def test_text_delta_handler_preserves_cumulative_text_before_tool_boundary() -> None:
    state = _make_state()
    handler = _TextDeltaHandler()
    handler.handle(TextDeltaEvent(text="prefix"), state)

    out = handler.handle(TextDeltaEvent(text="prefixsuffix"), state)

    assert out.text == "prefixsuffix"
    assert state.final_text_parts == ["prefix", "prefixsuffix"]
    assert state.current_text_parts == ["prefix", "prefixsuffix"]


def test_text_delta_handler_drops_duplicate_post_tool_snapshot() -> None:
    state = _make_state()
    text_handler = _TextDeltaHandler()
    text_handler.handle(TextDeltaEvent(text="prefix"), state)
    _ToolUseStartHandler().handle(
        ToolUseStartEvent(tool_use_id="tool-1", tool_name="web_search"),
        state,
    )
    _ToolResultHandler().handle(
        ToolResultEvent(tool_use_id="tool-1", tool_name="web_search", result="ok"),
        state,
    )

    out = text_handler.handle(TextDeltaEvent(text="prefix"), state)

    assert out.text == ""
    assert state.final_text_parts == ["prefix"]
    assert state.current_text_parts == []


def test_tool_use_start_handler_preserves_canonical_details_text_segment() -> None:
    state = _make_state()
    text_handler = _TextDeltaHandler()
    tool_handler = _ToolUseStartHandler()

    first = text_handler.handle(
        TextDeltaEvent(
            text=(
                "Let me read the specific problematic areas to fix them.\n\n"
                "<details>"
            )
        ),
        state,
    )
    second = text_handler.handle(
        TextDeltaEvent(
            text=(
                "<summary>View areas around line 10393, 14751, and nearby"
                "</summary>"
            )
        ),
        state,
    )
    tool_handler.handle(
        ToolUseStartEvent(
            tool_use_id="t1",
            tool_name="exec_command",
            synthetic_from_text=False,
        ),
        state,
    )

    expected = (
        "Let me read the specific problematic areas to fix them.\n\n"
        "<details><summary>View areas around line 10393, 14751, and nearby"
        "</summary>"
    )
    assert first.text == expected[: expected.index("<summary>")]
    assert second.text == expected[expected.index("<summary>") :]
    assert state.turn_segments[0] == {
        "type": "text",
        "text": expected,
        "presentation": "answer",
    }
    assert "".join(state.final_text_parts) == expected


def test_tool_use_start_handler_flushes_text_and_appends_segment() -> None:
    state = _make_state()
    state.current_text_parts = ["pre"]
    state.final_text_parts = ["pre"]
    handler = _ToolUseStartHandler()
    handler.handle(
        ToolUseStartEvent(
            tool_use_id="t1",
            tool_name="echo",
            synthetic_from_text=False,
        ),
        state,
    )
    assert state.turn_segments == [
        {"type": "text", "text": "pre", "presentation": "answer"},
        {"type": "tool_use", "tool_use_id": "t1", "name": "echo", "input": ""},
    ]
    assert state.current_text_parts == []
    assert state.final_text_parts == ["pre"]  # unchanged when not synthetic


def test_tool_result_handler_projects_large_write_file_arguments() -> None:
    state = _make_state()
    state.turn_segments.append(
        {
            "type": "tool_use",
            "tool_use_id": "write-1",
            "name": "write_file",
            "input": "",
        }
    )
    large_content = "HTML_START\n" + ("x" * 6000)

    _ToolResultHandler().handle(
        ToolResultEvent(
            tool_use_id="write-1",
            tool_name="write_file",
            result="Written 6011 bytes to index.html",
            arguments={"path": "index.html", "content": large_content},
        ),
        state,
    )

    tool_use = state.turn_segments[0]
    assert tool_use["input"]["path"] == "index.html"
    projected_content = tool_use["input"]["content"]
    assert projected_content.startswith("[historical_tool_argument_omitted]\n")
    assert "tool: write_file" in projected_content
    assert "field: content" in projected_content
    assert "path: index.html" in projected_content
    assert "sha256:" in projected_content
    assert large_content not in projected_content
    assert len(projected_content) < len(large_content)
    assert state.turn_segments[1] == {
        "type": "tool_result",
        "tool_use_id": "write-1",
        "name": "write_file",
        "result": "Written 6011 bytes to index.html",
        "is_error": False,
    }


def test_tool_result_handler_keeps_small_write_file_arguments() -> None:
    state = _make_state()
    state.turn_segments.append(
        {
            "type": "tool_use",
            "tool_use_id": "write-1",
            "name": "write_file",
            "input": "",
        }
    )
    arguments = {"path": "index.html", "content": "<h1>ok</h1>"}

    _ToolResultHandler().handle(
        ToolResultEvent(
            tool_use_id="write-1",
            tool_name="write_file",
            result="Written 11 bytes to index.html",
            arguments=arguments,
        ),
        state,
    )

    assert state.turn_segments[0]["input"] is arguments


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("publish_artifact", {"path": "deck.pptx"}),
        ("create_pptx", {"name": "deck.pptx", "slides": [{"title": "Deck"}]}),
    ],
)
def test_tool_result_handler_clears_delivery_failure_after_same_target_succeeds(
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    state = _make_state()
    handler = _ToolResultHandler()
    handler.handle(
        ToolResultEvent(
            tool_use_id="failed",
            tool_name=tool_name,
            result=(
                '{"status":"error","error_class":"RetryableToolInputError",'
                '"user_message":"regenerate","retry_allowed":true}'
            ),
            is_error=True,
            arguments=arguments,
        ),
        state,
    )
    handler.handle(
        ToolResultEvent(
            tool_use_id="succeeded",
            tool_name=tool_name,
            result='{"status":"published"}',
            arguments=arguments,
        ),
        state,
    )

    assert state.artifact_delivery_failures == []
    assert state.artifact_delivery_failures_by_target == {}


def test_tool_result_handler_keeps_unrelated_delivery_failure_after_retry() -> None:
    state = _make_state()
    handler = _ToolResultHandler()
    for target in ("first.pptx", "second.pptx"):
        handler.handle(
            ToolResultEvent(
                tool_use_id=f"failed-{target}",
                tool_name="publish_artifact",
                result='{"status":"error","user_message":"regenerate"}',
                is_error=True,
                arguments={"path": target},
            ),
            state,
        )
    handler.handle(
        ToolResultEvent(
            tool_use_id="succeeded-first",
            tool_name="publish_artifact",
            result='{"status":"published"}',
            arguments={"path": "first.pptx"},
        ),
        state,
    )

    assert state.artifact_delivery_failures == ["regenerate"]
    assert set(state.artifact_delivery_failures_by_target) == {"path:second.pptx"}


@pytest.mark.parametrize(
    "failed_path",
    [
        "/workspace/reports/deck.pptx",
        r"C:\workspace\reports\deck.pptx",
        "reports/deck.pptx",
    ],
)
def test_tool_result_handler_matches_publish_target_across_workspace_path_forms(
    tmp_path,
    failed_path: str,
) -> None:
    workspace = tmp_path / "workspace"
    ctx = SimpleNamespace(workspace_dir=str(workspace))
    canonical_path = workspace / "reports" / "deck.pptx"
    state = _make_state()
    handler = _ToolResultHandler()

    handler.handle(
        ToolResultEvent(
            tool_use_id="failed",
            tool_name="publish_artifact",
            result='{"status":"error","user_message":"regenerate"}',
            is_error=True,
            arguments={"path": failed_path},
        ),
        state,
        tool_context=ctx,
    )
    handler.handle(
        ToolResultEvent(
            tool_use_id="succeeded",
            tool_name="publish_artifact",
            result='{"status":"published"}',
            arguments={"path": str(canonical_path)},
        ),
        state,
        tool_context=ctx,
    )

    assert state.artifact_delivery_failures == []
    assert state.artifact_delivery_failures_by_target == {}


def test_tool_result_handler_uses_create_pptx_effective_basename_and_suffix() -> None:
    state = _make_state()
    handler = _ToolResultHandler()

    handler.handle(
        ToolResultEvent(
            tool_use_id="failed",
            tool_name="create_pptx",
            result='{"status":"error","user_message":"regenerate"}',
            is_error=True,
            arguments={"name": "reports/deck"},
        ),
        state,
    )
    handler.handle(
        ToolResultEvent(
            tool_use_id="succeeded",
            tool_name="create_pptx",
            result='{"status":"published"}',
            arguments={"name": "deck.pptx"},
        ),
        state,
    )

    assert state.artifact_delivery_failures == []
    assert state.artifact_delivery_failures_by_target == {}


def test_publish_success_clears_create_name_failure_but_not_other_path_failure(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    ctx = SimpleNamespace(workspace_dir=str(workspace))
    state = _make_state()
    handler = _ToolResultHandler()

    handler.handle(
        ToolResultEvent(
            tool_use_id="create-failed",
            tool_name="create_pptx",
            result='{"status":"error","user_message":"create failed"}',
            is_error=True,
            arguments={"name": "deck.pptx"},
        ),
        state,
        tool_context=ctx,
    )
    handler.handle(
        ToolResultEvent(
            tool_use_id="root-publish-failed",
            tool_name="publish_artifact",
            result='{"status":"error","user_message":"root path failed"}',
            is_error=True,
            arguments={"path": "deck.pptx"},
        ),
        state,
        tool_context=ctx,
    )
    handler.handle(
        ToolResultEvent(
            tool_use_id="nested-publish-succeeded",
            tool_name="publish_artifact",
            result='{"status":"published","artifact":{"name":"deck.pptx"}}',
            arguments={"path": "reports/deck.pptx"},
        ),
        state,
        tool_context=ctx,
    )

    assert state.artifact_delivery_failures == ["root path failed"]
    assert len(state.artifact_delivery_failures_by_target) == 1
    assert next(iter(state.artifact_delivery_failures_by_target)).startswith("path:")


@pytest.mark.parametrize(
    ("failed_path_factory", "cleared"),
    [
        (lambda workspace: "deck.pptx", True),
        (lambda workspace: str(workspace / "deck.pptx"), True),
        (lambda workspace: "/workspace/deck.pptx", True),
        (lambda workspace: "reports/deck.pptx", False),
    ],
)
def test_create_pptx_success_only_clears_matching_root_publish_failure(
    tmp_path,
    failed_path_factory,
    cleared: bool,
) -> None:
    workspace = tmp_path / "workspace"
    ctx = SimpleNamespace(workspace_dir=str(workspace))
    state = _make_state()
    handler = _ToolResultHandler()
    failed_path = failed_path_factory(workspace)

    handler.handle(
        ToolResultEvent(
            tool_use_id="publish-failed",
            tool_name="publish_artifact",
            result='{"status":"error","user_message":"regenerate"}',
            is_error=True,
            arguments={"path": failed_path},
        ),
        state,
        tool_context=ctx,
    )
    handler.handle(
        ToolResultEvent(
            tool_use_id="create-succeeded",
            tool_name="create_pptx",
            result='{"status":"published","artifact":{"name":"deck.pptx"}}',
            arguments={"name": "deck.pptx", "slides": [{"title": "Deck"}]},
        ),
        state,
        tool_context=ctx,
    )

    if cleared:
        assert state.artifact_delivery_failures == []
        assert state.artifact_delivery_failures_by_target == {}
    else:
        assert state.artifact_delivery_failures == ["regenerate"]
        assert len(state.artifact_delivery_failures_by_target) == 1


@pytest.mark.parametrize(
    ("source_path", "explicit_name", "created_name", "cleared"),
    [
        ("reports/source.bin", "deck.pptx", "deck.pptx", True),
        ("reports/source.pptx", "deck", "deck.pptx", True),
        ("reports/source.bin", "deck", "deck.pptx", False),
        ("reports/source.pptx", "  deck  ", "deck.pptx", True),
        ("reports/deck.pptx", "", "deck.pptx", True),
        ("reports/source.pptx", "de:ck", "de_ck.pptx", True),
    ],
)
def test_explicit_publish_name_is_the_single_logical_failure_identity(
    tmp_path,
    source_path: str,
    explicit_name: str,
    created_name: str,
    cleared: bool,
) -> None:
    workspace = tmp_path / "workspace"
    ctx = SimpleNamespace(workspace_dir=str(workspace))
    state = _make_state()
    handler = _ToolResultHandler()

    handler.handle(
        ToolResultEvent(
            tool_use_id="publish-failed",
            tool_name="publish_artifact",
            result='{"status":"error","user_message":"regenerate"}',
            is_error=True,
            arguments={"path": source_path, "name": explicit_name},
        ),
        state,
        tool_context=ctx,
    )

    assert len(state.artifact_delivery_failures_by_target) == 1
    assert next(iter(state.artifact_delivery_failures_by_target)).startswith("name:")

    handler.handle(
        ToolResultEvent(
            tool_use_id="create-succeeded",
            tool_name="create_pptx",
            result=(
                '{"status":"published","artifact":{"name":"'
                + created_name
                + '"}}'
            ),
            arguments={"name": created_name, "slides": [{"title": "Deck"}]},
        ),
        state,
        tool_context=ctx,
    )

    if cleared:
        assert state.artifact_delivery_failures == []
        assert state.artifact_delivery_failures_by_target == {}
    else:
        assert state.artifact_delivery_failures == ["regenerate"]
        assert len(state.artifact_delivery_failures_by_target) == 1


def test_tool_result_handler_updates_tool_use_name_after_runtime_coercion() -> None:
    state = _make_state()
    state.turn_segments.append(
        {
            "type": "tool_use",
            "tool_use_id": "meta-1",
            "name": "skill_view",
            "input": "",
        }
    )

    _ToolResultHandler().handle(
        ToolResultEvent(
            tool_use_id="meta-1",
            tool_name="meta_invoke",
            result="meta-skill 'meta-travel-planner' completed.",
            arguments={"name": "meta-travel-planner"},
        ),
        state,
    )

    assert state.turn_segments[0]["name"] == "meta_invoke"
    assert state.turn_segments[0]["input"] == {"name": "meta-travel-planner"}
    assert state.turn_segments[1]["name"] == "meta_invoke"


def test_tool_result_handler_replaces_intermediate_approval_result() -> None:
    state = _make_state()
    state.turn_segments.append(
        {
            "type": "tool_use",
            "tool_use_id": "call-approval",
            "name": "write_file",
            "input": "",
        }
    )
    handler = _ToolResultHandler()

    handler.handle(
        ToolResultEvent(
            tool_use_id="call-approval",
            tool_name="write_file",
            result='{"status":"approval_required","approval_id":"approval-1"}',
            arguments={"path": "outside.txt", "content": "hello"},
            execution_status={"status": "unknown", "reason": "approval_pending"},
        ),
        state,
    )
    handler.handle(
        ToolResultEvent(
            tool_use_id="call-approval",
            tool_name="write_file",
            result='{"status":"approval_denied","approval_id":"approval-1"}',
            is_error=True,
            arguments={"path": "outside.txt", "content": "hello", "approval_id": "approval-1"},
            execution_status={"status": "error", "reason": "approval_denied"},
        ),
        state,
    )

    tool_results = [
        segment for segment in state.turn_segments if segment.get("type") == "tool_result"
    ]
    assert len(tool_results) == 1
    assert tool_results[0]["tool_use_id"] == "call-approval"
    assert tool_results[0]["result"] == '{"status":"approval_denied","approval_id":"approval-1"}'
    assert tool_results[0]["is_error"] is True
    assert tool_results[0]["execution_status"]["reason"] == "approval_denied"
    assert state.turn_segments[0]["input"]["approval_id"] == "approval-1"


def test_tool_result_handler_preserves_initial_user_input_request() -> None:
    state = _make_state()
    handler = _ToolResultHandler()
    request = {
        "status": "input_required",
        "kind": "user_input",
        "paused": True,
        "request_id": "request-1",
        "run_id": "run-1",
        "step": "plan",
        "clarify_schema": {
            "mode": "form",
            "presentation": "plan_questionnaire_v1",
            "fields": [{"name": "scope", "required": True}],
        },
    }

    handler.handle(
        ToolResultEvent(
            tool_use_id="call-input",
            tool_name="request_user_input",
            result=json.dumps(request),
        ),
        state,
    )
    handler.handle(
        ToolResultEvent(
            tool_use_id="call-input",
            tool_name="request_user_input",
            result=json.dumps(
                {
                    "status": "answered",
                    "kind": "user_input",
                    "paused": False,
                    "request_id": "request-1",
                    "answers": {"scope": "focused"},
                }
            ),
        ),
        state,
    )

    assert state.turn_segments == [
        {
            "type": "tool_result",
            "tool_use_id": "call-input",
            "name": "request_user_input",
            "result": json.dumps(
                {
                    "status": "answered",
                    "kind": "user_input",
                    "paused": False,
                    "request_id": "request-1",
                    "answers": {"scope": "focused"},
                }
            ),
            "is_error": False,
            "user_input_request": request,
        }
    ]


def test_artifact_handler_appends_payload() -> None:
    state = _make_state()
    handler = _ArtifactHandler()
    event = ArtifactEvent(
        id="art-a1",
        sha256="deadbeef",
        name="x.png",
        mime="image/png",
        size=10,
        session_id="s1",
        session_key="agent:main:s1",
        source="tool",
        created_at="2026-05-15T00:00:00Z",
        download_url="https://x/y",
    )
    handler.handle(event, state)
    assert len(state.turn_artifacts) == 1


@pytest.mark.asyncio
async def test_generated_artifact_is_adopted_before_public_yield() -> None:
    order: list[tuple[str, str]] = []

    async def adopt(event: ArtifactEvent) -> None:
        order.append(("adopt", event.id))

    artifact = ArtifactEvent(
        id="art-editable",
        name="page.html",
        mime="text/html",
        size=32,
    )
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[artifact, DoneEvent(text="ready", text_snapshot="ready")]
        )
    )
    inp = _make_input(
        tool_context=ToolContext(generated_artifact_adopter=adopt),
    )

    yielded: list[Any] = []
    async for event in stage.run(inp):
        if isinstance(event, ArtifactEvent):
            order.append(("yield", event.id))
        yielded.append(event)

    assert order == [("adopt", "art-editable"), ("yield", "art-editable")]
    assert any(isinstance(event, ArtifactEvent) for event in yielded)
    assert inp.state.turn_artifacts[0]["id"] == "art-editable"


@pytest.mark.asyncio
async def test_generated_artifact_adoption_failure_keeps_delivery() -> None:
    async def fail_adoption(_event: ArtifactEvent) -> None:
        raise RuntimeError("synthetic adoption failure")

    artifact = ArtifactEvent(
        id="art-fallback",
        name="page.html",
        mime="text/html",
        size=32,
    )
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[artifact, DoneEvent(text="ready", text_snapshot="ready")]
        )
    )
    inp = _make_input(
        tool_context=ToolContext(generated_artifact_adopter=fail_adoption),
    )

    yielded = await _drain(stage, inp)

    assert any(isinstance(event, ArtifactEvent) for event in yielded)
    assert inp.state.turn_artifacts[0]["id"] == "art-fallback"


def test_error_handler_rewrites_timeout_envelope() -> None:
    state = _make_state()
    handler = _ErrorHandler()
    result = handler.handle(ErrorEvent(message="x", code="timeout"), state)
    assert result is _SUPPRESS
    assert state.pending_error_event is not None
    assert state.pending_error_event.code == "llm_timeout"


def test_error_handler_drops_unpaired_tool_use_on_incomplete_stream() -> None:
    state = _make_state()
    state.turn_segments[:] = [
        {"type": "tool_use", "tool_use_id": "t1", "name": "x", "input": ""},
    ]
    handler = _ErrorHandler()
    result = handler.handle(
        ErrorEvent(message="boom", code="incomplete_tool_stream"),
        state,
    )
    assert result is _SUPPRESS
    assert state.turn_segments == []  # unpaired tool_use dropped


def test_error_handler_drops_unpaired_tool_use_on_output_truncation() -> None:
    state = _make_state()
    state.turn_segments[:] = [
        {"type": "text", "text": "partial"},
        {"type": "tool_use", "tool_use_id": "t1", "name": "x", "input": ""},
    ]
    handler = _ErrorHandler()
    result = handler.handle(
        ErrorEvent(message="boom", code="provider_output_truncated"),
        state,
    )
    assert result is _SUPPRESS
    assert state.turn_segments == [{"type": "text", "text": "partial"}]


def test_error_handler_drops_unpaired_tool_use_for_provider_specific_error() -> None:
    state = _make_state()
    state.turn_segments[:] = [
        {"type": "tool_use", "tool_use_id": "t1", "name": "x", "input": "{"},
    ]
    handler = _ErrorHandler()
    result = handler.handle(
        ErrorEvent(message="stream ended before terminal evidence", code="incomplete_stream"),
        state,
    )
    assert result is _SUPPRESS
    assert state.turn_segments == []


def test_error_handler_preserves_tool_use_already_paired_with_result() -> None:
    state = _make_state()
    state.turn_segments[:] = [
        {"type": "tool_use", "tool_use_id": "t1", "name": "x", "input": "{}"},
        {"type": "tool_result", "tool_use_id": "t1", "result": "ok"},
    ]
    handler = _ErrorHandler()
    result = handler.handle(
        ErrorEvent(message="later provider failure", code="provider_error"),
        state,
    )
    assert result is _SUPPRESS
    assert [segment["type"] for segment in state.turn_segments] == [
        "tool_use",
        "tool_result",
    ]


def test_warning_handler_forwards_through_transformer() -> None:
    state = _make_state()
    state.final_text_parts = ["keep"]
    state.current_text_parts = ["keep"]
    captured: list[WarningEvent] = []

    def transformer(event: WarningEvent) -> WarningEvent:
        captured.append(event)
        return WarningEvent(code="rewritten", message="from-transformer")

    handler = _WarningHandler(transformer)
    out = handler.handle(WarningEvent(code="orig", message="m"), state)
    assert captured == [WarningEvent(code="orig", message="m")]
    assert out.code == "rewritten"
    assert state.final_text_parts == ["keep"]
    assert state.current_text_parts == ["keep"]


@pytest.mark.parametrize(
    "warning_code",
    ["workspace_diff_recovery", "plan_run_reconciliation"],
)
def test_warning_handler_discards_superseded_recovery_text(
    warning_code: str,
) -> None:
    state = _make_state()
    state.final_text_parts = ["Earlier.", "Implemented the fix."]
    state.current_text_parts = ["Implemented the fix."]

    handler = _WarningHandler(lambda event: event)
    out = handler.handle(WarningEvent(code=warning_code, message="m"), state)

    assert out.code == warning_code
    assert state.final_text_parts == ["Earlier."]
    assert state.current_text_parts == []


def test_done_handler_normalizes_and_emits_done() -> None:
    state = _make_state()
    handler = _DoneHandler()
    inp = _make_input(
        state=state,
        turn=_make_turn(
            metadata={
                "routed_tier": "L1",
                "routing_applied": False,
                "rollout_phase": "observe",
            }
        ),
    )
    done = DoneEvent(text="result", input_tokens=10, output_tokens=5)
    transformed, extra = handler.handle(done, inp, state)
    assert isinstance(transformed, DoneEvent)
    assert transformed.routed_tier == "L1"
    assert transformed.routing_applied is False
    assert transformed.rollout_phase == "observe"
    assert state.done_event is transformed
    assert extra == []


def test_done_handler_carries_vision_followup_metadata() -> None:
    state = _make_state()
    handler = _DoneHandler()
    inp = _make_input(
        state=state,
        turn=_make_turn(
            metadata={
                "image_route_reason": "gate_history",
                "router_vision_followup_gate_decision": "needs_image",
                "router_vision_followup_gate_confidence": 0.92,
                "router_vision_followup_gate_reason": (
                    "references previous image with private detail"
                ),
                "router_vision_followup_gate_source": "llm",
                "router_vision_followup_gate_model": "deepseek/deepseek-v4-flash",
                "router_vision_followup_needs_image": True,
            }
        ),
    )

    transformed, extra = handler.handle(DoneEvent(text="ok"), inp, state)

    assert getattr(transformed, "image_route_reason") == "gate_history"
    assert getattr(transformed, "vision_followup_gate_decision") == "needs_image"
    assert getattr(transformed, "vision_followup_gate_confidence") == 0.92
    assert getattr(transformed, "vision_followup_gate_reason") == "llm_needs_image"
    assert "private detail" not in getattr(transformed, "vision_followup_gate_reason")
    assert getattr(transformed, "vision_followup_gate_source") == "llm"
    assert getattr(transformed, "vision_followup_gate_model") == "deepseek/deepseek-v4-flash"
    assert getattr(transformed, "vision_followup_needs_image") is True
    assert state.done_event is transformed
    assert extra == []


_ROUTE_SAVINGS_METADATA = {
    "routed_tier": "c1",
    "routing_source": "squilla_router",
    "savings_pct": 62.0,
    "savings_max_price_per_m": 5.0,
    "savings_routed_price_per_m": 0.5,
}


def test_done_handler_zeroes_savings_for_ensemble_turns() -> None:
    state = _make_state()
    handler = _DoneHandler()
    inp = _make_input(
        state=state,
        turn=_make_turn(metadata=dict(_ROUTE_SAVINGS_METADATA)),
    )

    transformed, _ = handler.handle(
        DoneEvent(
            text="ok",
            input_tokens=1_000_000,
            output_tokens=500,
            ensemble_trace={"profile": "router_dynamic", "mode": "llm_ensemble"},
        ),
        inp,
        state,
    )

    assert transformed.savings_pct == 0.0
    assert transformed.savings_usd == 0.0
    assert transformed.total_savings_pct == 0.0
    assert transformed.total_savings_usd == 0.0


def test_done_handler_zeroes_savings_when_ensemble_metadata_flag_is_set() -> None:
    state = _make_state()
    handler = _DoneHandler()
    inp = _make_input(
        state=state,
        turn=_make_turn(metadata={**_ROUTE_SAVINGS_METADATA, "ensemble_enabled": True}),
    )

    transformed, _ = handler.handle(
        DoneEvent(text="ok", input_tokens=1_000_000, output_tokens=500), inp, state
    )

    assert transformed.savings_pct == 0.0
    assert transformed.savings_usd == 0.0
    assert transformed.total_savings_pct == 0.0
    assert transformed.total_savings_usd == 0.0


def test_done_handler_keeps_route_savings_for_single_model_turns() -> None:
    state = _make_state()
    handler = _DoneHandler()
    inp = _make_input(
        state=state,
        turn=_make_turn(metadata=dict(_ROUTE_SAVINGS_METADATA)),
    )

    transformed, _ = handler.handle(
        DoneEvent(text="ok", input_tokens=1_000_000, output_tokens=500), inp, state
    )

    assert transformed.savings_pct == 62.0
    assert transformed.savings_usd == pytest.approx(4.5)


@pytest.mark.asyncio
async def test_compaction_handler_runs_persist_snapshot_prompt_in_order() -> None:
    persist = _RecordingCompactionPersist()
    snapshot = _RecordingMemorySnapshotRefresh()
    prompt = _RecordingSystemPromptRefresh()
    handler = _CompactionHandler(
        persist=persist,
        memory_snapshot=snapshot,
        system_prompt=prompt,
    )
    source_entries = (SimpleNamespace(message_id="source-boundary", id=7),)
    source_preimage = ((7, "source-boundary"),)
    inp = _make_input(
        compaction_source_entries=source_entries,
        compaction_source_preimage=source_preimage,
        compaction_source_boundary_message_id="source-boundary",
        compaction_source_boundary_entry_id=7,
    )
    await handler.handle(
        CompactionEvent(
            compaction_id="cmp_inline_1",
            summary="s",
            kept_entries=[1, 2],
            removed_count=4,
        ),
        inp,
    )
    assert len(persist.calls) == 1
    assert persist.calls[0]["summary"] == "s"
    assert persist.calls[0]["kept_entries"] == [1, 2]
    assert persist.calls[0]["compaction_id"] == "cmp_inline_1"
    assert persist.calls[0]["removed_count"] == 4
    assert persist.calls[0]["source_entries"] is source_entries
    assert persist.calls[0]["source_preimage"] is source_preimage
    assert persist.calls[0]["source_boundary_message_id"] == "source-boundary"
    assert persist.calls[0]["source_boundary_entry_id"] == 7
    assert len(snapshot.calls) == 1
    assert len(prompt.calls) == 1


@pytest.mark.asyncio
async def test_compaction_handler_does_not_refresh_after_stale_source() -> None:
    persist = _RecordingCompactionPersist(result=False)
    snapshot = _RecordingMemorySnapshotRefresh()
    prompt = _RecordingSystemPromptRefresh()
    handler = _CompactionHandler(
        persist=persist,
        memory_snapshot=snapshot,
        system_prompt=prompt,
    )

    await handler.handle(
        CompactionEvent(summary="stale", kept_entries=[], removed_count=1),
        _make_input(
            compaction_source_entries=(),
            compaction_source_preimage=(),
        ),
    )

    assert len(persist.calls) == 1
    assert snapshot.calls == []
    assert prompt.calls == []


@pytest.mark.asyncio
async def test_compaction_handler_skips_persist_when_session_manager_absent() -> None:
    persist = _RecordingCompactionPersist()
    snapshot = _RecordingMemorySnapshotRefresh()
    prompt = _RecordingSystemPromptRefresh()
    handler = _CompactionHandler(
        persist=persist,
        memory_snapshot=snapshot,
        system_prompt=prompt,
    )
    inp = _make_input(session_manager_present=False)
    await handler.handle(CompactionEvent(summary="s", kept_entries=[]), inp)
    assert persist.calls == []
    # Snapshot + prompt still fire; the persist guard is the only conditional.
    assert len(snapshot.calls) == 1
    assert len(prompt.calls) == 1


@pytest.mark.asyncio
async def test_compaction_handler_preserves_recoverable_state_on_persist_failure() -> None:
    persist = _RecordingCompactionPersist(raises=RuntimeError)
    snapshot = _RecordingMemorySnapshotRefresh()
    prompt = _RecordingSystemPromptRefresh()
    handler = _CompactionHandler(
        persist=persist,
        memory_snapshot=snapshot,
        system_prompt=prompt,
    )
    inp = _make_input()
    # Must NOT raise, but failed durable persistence must not refresh runtime state
    # into a false post-compaction view.
    await handler.handle(CompactionEvent(summary="s", kept_entries=[]), inp)
    assert len(persist.calls) == 1
    assert snapshot.calls == []
    assert prompt.calls == []


# ---------------------------------------------------------------------------
# Outer-stage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outer_stage_yields_text_then_done_and_notifies_post_stream() -> None:
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="hi"),
            TextDeltaEvent(text=" world"),
            DoneEvent(text="hi world"),
        ]
    )
    stage, recs = _make_stage(agent_run=agent_run)
    inp = _make_input(sync_manager=object())
    yielded = await _drain(stage, inp)
    kinds = [type(e).__name__ for e in yielded]
    assert kinds == ["TextDeltaEvent", "TextDeltaEvent", "DoneEvent"]
    assert inp.state.final_text_parts == ["hi", " world"]
    assert len(recs["memory_sync_notify"].calls) == 1
    assert recs["memory_sync_notify"].calls[0]["runtime_message"] == "hello there"
    assert recs["memory_sync_notify"].calls[0]["sync_manager_present"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sentinel", "chunks"),
    [
        ("NO_REPLY", ["NO_REPLY"]),
        ("NO_REPLY", list("NO_REPLY")),
        ("HEARTBEAT_OK", ["HEARTBEAT_OK"]),
        ("HEARTBEAT_OK", list("HEARTBEAT_OK")),
        ("  NO_REPLY\n", ["  ", "NO_", "REPLY\n"]),
        ("\tHEARTBEAT_OK  ", ["\tHEART", "BEAT_OK  "]),
    ],
)
async def test_human_sentinel_only_done_becomes_pending_error_without_text_flash(
    sentinel: str,
    chunks: list[str],
) -> None:
    agent_run = _RecordingAgentRun(
        events=[
            *(TextDeltaEvent(text=chunk) for chunk in chunks),
            DoneEvent(
                text=sentinel,
                text_snapshot=sentinel,
                input_tokens=13,
                output_tokens=2,
            ),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input(input_mode="user")

    yielded = await _drain(stage, inp)

    assert yielded == []
    assert inp.state.final_text_parts == []
    assert inp.state.current_text_parts == []
    assert inp.state.pending_error_event == ErrorEvent(
        message=SILENT_REPLY_NOT_ALLOWED_MESSAGE,
        code=SILENT_REPLY_NOT_ALLOWED_CODE,
    )
    assert inp.state.error_message == SILENT_REPLY_NOT_ALLOWED_MESSAGE
    assert inp.state.done_event is not None
    assert inp.state.done_event.input_tokens == 13
    assert inp.state.done_event.output_tokens == 2
    assert inp.state.done_event.delivery == "suppressed"


@pytest.mark.asyncio
async def test_human_done_only_sentinel_becomes_pending_error() -> None:
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                DoneEvent(
                    text="NO_REPLY",
                    text_snapshot="NO_REPLY",
                    input_tokens=5,
                    output_tokens=1,
                )
            ]
        )
    )
    inp = _make_input(input_mode="user")

    yielded = await _drain(stage, inp)

    assert yielded == []
    assert inp.state.pending_error_event is not None
    assert inp.state.pending_error_event.code == SILENT_REPLY_NOT_ALLOWED_CODE
    assert inp.state.done_event is not None
    assert inp.state.done_event.input_tokens == 5


@pytest.mark.asyncio
async def test_human_short_normal_prefix_releases_at_done() -> None:
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO"),
                DoneEvent(text="NO", text_snapshot="NO"),
            ]
        )
    )
    inp = _make_input(input_mode="user")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == [
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[0].text == "NO"
    assert yielded[1].text_snapshot == "NO"
    assert inp.state.pending_error_event is None


@pytest.mark.asyncio
async def test_human_prefix_crossing_tool_boundary_keeps_live_and_history_order() -> None:
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO"),
                ToolUseStartEvent(tool_use_id="tool-1", tool_name="lookup"),
                ToolResultEvent(
                    tool_use_id="tool-1",
                    tool_name="lookup",
                    result="ok",
                ),
                TextDeltaEvent(text=" results"),
                DoneEvent(text="NO results", text_snapshot="NO results"),
            ]
        )
    )
    inp = _make_input(input_mode="user")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == [
        "ToolUseStartEvent",
        "ToolResultEvent",
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[2].text == "NO results"
    assert [segment["type"] for segment in inp.state.turn_segments] == [
        "tool_use",
        "tool_result",
    ]
    assert inp.state.current_text_parts == ["NO results"]


@pytest.mark.asyncio
async def test_human_short_prefix_done_after_tool_moves_text_to_history_tail() -> None:
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO"),
                ToolUseStartEvent(tool_use_id="tool-1", tool_name="lookup"),
                ToolResultEvent(
                    tool_use_id="tool-1",
                    tool_name="lookup",
                    result="ok",
                ),
                DoneEvent(text="NO", text_snapshot="NO"),
            ]
        )
    )
    inp = _make_input(input_mode="user")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == [
        "ToolUseStartEvent",
        "ToolResultEvent",
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[2].text == "NO"
    assert [segment["type"] for segment in inp.state.turn_segments] == [
        "tool_use",
        "tool_result",
    ]
    assert inp.state.current_text_parts == ["NO"]


@pytest.mark.asyncio
async def test_human_usage_done_after_provider_error_keeps_original_error() -> None:
    provider_error = ErrorEvent(message="provider failed", code="provider_error")
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO_REP"),
                provider_error,
                DoneEvent(
                    text="NO_REPLY",
                    text_snapshot="NO_REPLY",
                    input_tokens=8,
                    output_tokens=1,
                ),
            ]
        )
    )
    inp = _make_input(input_mode="user")

    yielded = await _drain(stage, inp)

    assert yielded == []
    assert inp.state.pending_error_event is provider_error
    assert inp.state.error_message == "provider failed"
    assert inp.state.final_text_parts == []
    assert inp.state.done_event is not None
    assert inp.state.done_event.input_tokens == 8
    assert inp.state.done_event.delivery == "suppressed"


@pytest.mark.asyncio
async def test_human_mixed_sentinel_prose_releases_once_and_completes() -> None:
    body = "This is a normal human-facing reply."
    source = f"NO_REPLY\n{body}"
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO_"),
                TextDeltaEvent(text="REPLY\n"),
                TextDeltaEvent(text=body),
                DoneEvent(text=source, text_snapshot=source),
            ]
        )
    )
    inp = _make_input(input_mode="user")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == [
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[0].text == source
    assert yielded[1].text_snapshot == source
    assert inp.state.pending_error_event is None


@pytest.mark.asyncio
async def test_human_sentinel_failure_keeps_tool_and_artifact_activity() -> None:
    artifact = ArtifactEvent(name="report.txt", mime="text/plain", size=4)
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO_REPLY"),
                ToolUseStartEvent(tool_use_id="tool-1", tool_name="lookup"),
                ToolResultEvent(
                    tool_use_id="tool-1",
                    tool_name="lookup",
                    result="ok",
                ),
                artifact,
                DoneEvent(
                    text="NO_REPLY",
                    text_snapshot="NO_REPLY",
                    input_tokens=7,
                    output_tokens=1,
                ),
            ]
        )
    )
    inp = _make_input(input_mode="user")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == [
        "ToolUseStartEvent",
        "ToolResultEvent",
        "ArtifactEvent",
    ]
    assert [segment["type"] for segment in inp.state.turn_segments] == [
        "tool_use",
        "tool_result",
    ]
    assert len(inp.state.turn_artifacts) == 1
    assert inp.state.turn_artifacts[0]["name"] == "report.txt"
    assert inp.state.turn_artifacts[0]["mime"] == "text/plain"
    assert inp.state.turn_artifacts[0]["size"] == 4
    assert inp.state.pending_error_event is not None
    assert inp.state.pending_error_event.code == SILENT_REPLY_NOT_ALLOWED_CODE
    assert inp.state.done_event is not None
    assert inp.state.done_event.input_tokens == 7


@pytest.mark.asyncio
async def test_system_event_buffers_split_sentinel_and_suppresses_terminal_text() -> None:
    agent_run = _RecordingAgentRun(
        events=[
            *(TextDeltaEvent(text=char) for char in "NO_REPLY"),
            DoneEvent(text="NO_REPLY", text_snapshot="NO_REPLY"),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)

    yielded = await _drain(
        stage,
        _make_input(run_kind="goal", input_mode="system_event"),
    )

    assert [type(event).__name__ for event in yielded] == ["DoneEvent"]
    done = yielded[0]
    assert isinstance(done, DoneEvent)
    assert done.text == ""
    assert done.text_snapshot == ""
    assert done.delivery == "suppressed"
    assert done.suppression_reason == "no_reply"


@pytest.mark.asyncio
async def test_system_event_buffers_mixed_sentinel_and_releases_only_body_once() -> None:
    body = "The synthetic background check is still pending."
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="NO_"),
            TextDeltaEvent(text="REPLY\r\n"),
            TextDeltaEvent(text=body),
            DoneEvent(
                text=f"NO_REPLY\r\n{body}",
                text_snapshot=f"NO_REPLY\r\n{body}",
            ),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)

    yielded = await _drain(
        stage,
        _make_input(run_kind="goal", input_mode="system_event"),
    )

    assert [type(event).__name__ for event in yielded] == [
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[0].text == body
    done = yielded[1]
    assert isinstance(done, DoneEvent)
    assert done.text == body
    assert done.text_snapshot == body
    assert done.delivery == "visible"
    assert done.suppression_reason is None


@pytest.mark.asyncio
async def test_system_event_keeps_tool_events_live_while_text_is_buffered() -> None:
    body = "The check completed."
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="HEARTBEAT_OK\n"),
            ToolUseStartEvent(tool_use_id="tool-1", tool_name="lookup"),
            ToolResultEvent(
                tool_use_id="tool-1",
                tool_name="lookup",
                result="ok",
            ),
            TextDeltaEvent(text=body),
            DoneEvent(
                text=f"HEARTBEAT_OK\n{body}",
                text_snapshot=f"HEARTBEAT_OK\n{body}",
            ),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)

    yielded = await _drain(
        stage,
        _make_input(run_kind="goal", input_mode="system_event"),
    )

    assert [type(event).__name__ for event in yielded] == [
        "ToolUseStartEvent",
        "ToolResultEvent",
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[2].text == body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_text",
    ["NO_REPLY\nPreparing.Finished.", "Preparing.Finished."],
)
async def test_system_event_normalization_preserves_text_around_tool_boundary(
    terminal_text: str,
) -> None:
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="NO_REPLY\nPreparing."),
            ToolUseStartEvent(tool_use_id="tool-1", tool_name="lookup"),
            ToolResultEvent(
                tool_use_id="tool-1",
                tool_name="lookup",
                result="ok",
            ),
            TextDeltaEvent(text="Finished."),
            DoneEvent(text=terminal_text, text_snapshot=terminal_text),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input(run_kind="goal", input_mode="system_event")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == [
        "ToolUseStartEvent",
        "ToolResultEvent",
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[2].text == "Preparing.Finished."
    assert [segment["type"] for segment in inp.state.turn_segments] == [
        "text",
        "tool_use",
        "tool_result",
    ]
    assert inp.state.turn_segments[0] == {
        "type": "text",
        "text": "Preparing.",
        "presentation": "answer",
    }
    assert inp.state.current_text_parts == ["Finished."]
    assert "NO_REPLY" not in str(inp.state.turn_segments)


@pytest.mark.asyncio
async def test_system_event_removes_bare_marker_before_tool_without_newline() -> None:
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="NO_REPLY"),
            ToolUseStartEvent(tool_use_id="tool-1", tool_name="lookup"),
            ToolResultEvent(
                tool_use_id="tool-1",
                tool_name="lookup",
                result="ok",
            ),
            TextDeltaEvent(text="Visible body."),
            DoneEvent(
                text="NO_REPLYVisible body.",
                text_snapshot="NO_REPLYVisible body.",
            ),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input(run_kind="goal", input_mode="system_event")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == [
        "ToolUseStartEvent",
        "ToolResultEvent",
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[2].text == "Visible body."
    assert yielded[3].text_snapshot == "Visible body."
    assert [segment["type"] for segment in inp.state.turn_segments] == [
        "tool_use",
        "tool_result",
    ]
    assert inp.state.current_text_parts == ["Visible body."]
    assert inp.state.final_text_parts == ["Visible body."]


@pytest.mark.asyncio
async def test_system_event_removes_bare_marker_after_tool_without_newline() -> None:
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="Visible body."),
            ToolUseStartEvent(tool_use_id="tool-1", tool_name="lookup"),
            ToolResultEvent(
                tool_use_id="tool-1",
                tool_name="lookup",
                result="ok",
            ),
            TextDeltaEvent(text="NO_REPLY"),
            DoneEvent(
                text="Visible body.NO_REPLY",
                text_snapshot="Visible body.NO_REPLY",
            ),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input(run_kind="goal", input_mode="system_event")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == [
        "ToolUseStartEvent",
        "ToolResultEvent",
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[2].text == "Visible body."
    assert yielded[3].text_snapshot == "Visible body."
    assert inp.state.turn_segments[0] == {
        "type": "text",
        "text": "Visible body.",
        "presentation": "answer",
    }
    assert inp.state.current_text_parts == []
    assert inp.state.final_text_parts == ["Visible body."]


@pytest.mark.asyncio
async def test_system_event_keeps_bare_marker_on_a_middle_tool_boundary() -> None:
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="Before."),
            ToolUseStartEvent(tool_use_id="tool-1", tool_name="lookup"),
            ToolResultEvent(
                tool_use_id="tool-1",
                tool_name="lookup",
                result="one",
            ),
            TextDeltaEvent(text="NO_REPLY"),
            ToolUseStartEvent(tool_use_id="tool-2", tool_name="lookup"),
            ToolResultEvent(
                tool_use_id="tool-2",
                tool_name="lookup",
                result="two",
            ),
            TextDeltaEvent(text="After."),
            DoneEvent(
                text="Before.NO_REPLYAfter.",
                text_snapshot="Before.NO_REPLYAfter.",
            ),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input(run_kind="goal", input_mode="system_event")

    yielded = await _drain(stage, inp)

    text_events = [event.text for event in yielded if isinstance(event, TextDeltaEvent)]
    assert text_events == ["Before.NO_REPLYAfter."]
    done = next(event for event in yielded if isinstance(event, DoneEvent))
    assert done.text_snapshot == "Before.NO_REPLYAfter."
    assert any(
        segment.get("type") == "text" and segment.get("text") == "NO_REPLY"
        for segment in inp.state.turn_segments
    )


@pytest.mark.asyncio
async def test_system_event_runtime_notice_overrides_suppressed_model_delivery() -> None:
    state = _make_state()
    state.turn_segments.append(
        {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "name": "background_process",
            "result": "status: running",
            "execution_status": {
                "status": "unknown",
                "reason": "background_running",
            },
        }
    )
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO_REPLY"),
                DoneEvent(text="NO_REPLY", text_snapshot="NO_REPLY"),
            ]
        )
    )

    yielded = await _drain(
        stage,
        _make_input(
            state=state,
            run_kind="goal",
            input_mode="system_event",
        ),
    )

    assert [type(event).__name__ for event in yielded] == [
        "TextDeltaEvent",
        "DoneEvent",
    ]
    notice = yielded[0].text
    assert "could not confirm" in notice
    done = yielded[1]
    assert isinstance(done, DoneEvent)
    assert done.text == notice
    assert done.text_snapshot == notice
    assert done.delivery == "visible"
    assert done.suppression_reason is None


@pytest.mark.asyncio
async def test_system_event_retry_releases_only_authoritative_done_snapshot() -> None:
    final_body = "Canonical successful status."
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO_REPLY\nDiscarded retry status."),
                TextDeltaEvent(text=final_body),
                DoneEvent(text=final_body, text_snapshot=final_body),
            ]
        )
    )

    yielded = await _drain(
        stage,
        _make_input(run_kind="goal", input_mode="system_event"),
    )

    assert [type(event).__name__ for event in yielded] == [
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[0].text == final_body
    assert yielded[1].text_snapshot == final_body


@pytest.mark.asyncio
@pytest.mark.parametrize("partial_marker", ["NO", "NO_REP", "HEARTBEAT_O"])
async def test_system_event_error_does_not_release_partial_sentinel(
    partial_marker: str,
) -> None:
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text=partial_marker),
                ErrorEvent(message="provider failed", code="provider_error"),
            ]
        )
    )

    inp = _make_input(run_kind="goal", input_mode="system_event")
    yielded = await _drain(stage, inp)

    assert yielded == []
    assert inp.state.final_text_parts == []
    assert inp.state.current_text_parts == []
    assert all(segment.get("type") != "text" for segment in inp.state.turn_segments)


@pytest.mark.asyncio
async def test_partial_sentinel_error_then_complete_usage_done_stays_suppressed() -> None:
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO_REP"),
                ErrorEvent(message="provider failed", code="provider_error"),
                DoneEvent(text="NO_REPLY", text_snapshot="NO_REPLY"),
            ]
        )
    )
    inp = _make_input(run_kind="goal", input_mode="system_event")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == ["DoneEvent"]
    done = yielded[0]
    assert isinstance(done, DoneEvent)
    assert done.text == ""
    assert done.text_snapshot == ""
    assert done.delivery == "suppressed"
    assert done.suppression_reason == "no_reply"
    assert inp.state.final_text_parts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_text", ["NO_REPLY", ""])
async def test_complete_sentinel_error_then_usage_done_stays_suppressed(
    terminal_text: str,
) -> None:
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO_REPLY"),
                ErrorEvent(message="provider failed", code="provider_error"),
                DoneEvent(text=terminal_text, text_snapshot=terminal_text),
            ]
        )
    )
    inp = _make_input(run_kind="goal", input_mode="system_event")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == ["DoneEvent"]
    done = yielded[0]
    assert isinstance(done, DoneEvent)
    assert done.text == ""
    assert done.text_snapshot == ""
    assert done.delivery == "suppressed"
    assert done.suppression_reason == "no_reply"
    assert inp.state.final_text_parts == []


@pytest.mark.asyncio
async def test_system_event_error_releases_normal_partial_text_once() -> None:
    body = "A useful partial status."
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text=body),
                ErrorEvent(message="provider failed", code="provider_error"),
            ]
        )
    )

    inp = _make_input(run_kind="goal", input_mode="system_event")
    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == ["TextDeltaEvent"]
    assert yielded[0].text == body
    assert inp.state.final_text_parts == [body]


@pytest.mark.asyncio
async def test_system_event_usage_done_does_not_release_error_body_twice() -> None:
    body = "A useful partial status."
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text=body),
                ErrorEvent(message="provider failed", code="provider_error"),
                DoneEvent(text=body, text_snapshot=body),
            ]
        )
    )

    yielded = await _drain(
        stage,
        _make_input(run_kind="goal", input_mode="system_event"),
    )

    assert [type(event).__name__ for event in yielded] == [
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[0].text == body
    assert yielded[1].text_snapshot == body


@pytest.mark.asyncio
async def test_tool_boundary_marker_error_then_usage_done_releases_body_once() -> None:
    body = "A useful partial status."
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO_REPLY"),
                ToolUseStartEvent(tool_use_id="tool-1", tool_name="lookup"),
                ToolResultEvent(
                    tool_use_id="tool-1",
                    tool_name="lookup",
                    result="ok",
                ),
                TextDeltaEvent(text=body),
                ErrorEvent(message="provider failed", code="provider_error"),
                DoneEvent(
                    text=f"NO_REPLY{body}",
                    text_snapshot=f"NO_REPLY{body}",
                ),
            ]
        )
    )
    inp = _make_input(run_kind="goal", input_mode="system_event")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == [
        "ToolUseStartEvent",
        "ToolResultEvent",
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[2].text == body
    assert yielded[3].text_snapshot == body
    assert inp.state.final_text_parts == [body]
    assert "NO_REPLY" not in str(inp.state.turn_segments)


@pytest.mark.asyncio
async def test_error_release_does_not_mask_conflicting_authoritative_done() -> None:
    partial = "A useful partial status."
    corrected = "Authoritative corrected status."
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="NO_REPLY"),
                ToolUseStartEvent(tool_use_id="tool-1", tool_name="lookup"),
                ToolResultEvent(
                    tool_use_id="tool-1",
                    tool_name="lookup",
                    result="ok",
                ),
                TextDeltaEvent(text=partial),
                ErrorEvent(message="provider failed", code="provider_error"),
                DoneEvent(text=corrected, text_snapshot=corrected),
            ]
        )
    )
    inp = _make_input(run_kind="goal", input_mode="system_event")

    yielded = await _drain(stage, inp)

    assert [type(event).__name__ for event in yielded] == [
        "ToolUseStartEvent",
        "ToolResultEvent",
        "TextDeltaEvent",
        "DoneEvent",
    ]
    assert yielded[2].text == partial
    assert yielded[3].text_snapshot == corrected
    assert inp.state.final_text_parts == [corrected]


@pytest.mark.asyncio
async def test_outer_stage_runs_only_publish_off_the_event_loop() -> None:
    """The done handler's artifact publish re-reads and fully validates
    deliverables (PPTX inflation plus deck parse), so the stage must run
    THAT phase in a worker thread. The state-mutating pre/post-publish
    phases stay on the event loop so the shared, by-reference stream
    accumulators are never mutated concurrently with a cancellation."""
    agent_run = _RecordingAgentRun(events=[DoneEvent(text="hi world")])
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input()

    pre_threads: list[threading.Thread] = []
    publish_threads: list[threading.Thread] = []
    post_threads: list[threading.Thread] = []
    real_pre = stage._done_handler.pre_publish
    real_publish = stage._done_handler.run_publish
    real_post = stage._done_handler.post_publish

    def recording_pre(event: Any, inner_inp: Any, state: Any) -> Any:
        pre_threads.append(threading.current_thread())
        return real_pre(event, inner_inp, state)

    def recording_publish(inner_inp: Any, accumulated_text: str) -> Any:
        publish_threads.append(threading.current_thread())
        return real_publish(inner_inp, accumulated_text)

    def recording_post(pre: Any, result: Any, inner_inp: Any, state: Any) -> Any:
        post_threads.append(threading.current_thread())
        return real_post(pre, result, inner_inp, state)

    stage._done_handler.pre_publish = recording_pre  # type: ignore[method-assign]
    stage._done_handler.run_publish = recording_publish  # type: ignore[method-assign]
    stage._done_handler.post_publish = recording_post  # type: ignore[method-assign]
    loop_thread = threading.current_thread()
    yielded = await _drain(stage, inp)

    assert [type(e).__name__ for e in yielded] == ["DoneEvent"]
    # Blocking publish ran off the loop thread.
    assert publish_threads
    assert all(thread is not loop_thread for thread in publish_threads)
    # State mutations stayed on the loop thread.
    assert pre_threads == [loop_thread]
    assert post_threads == [loop_thread]


@pytest.mark.asyncio
async def test_done_publish_cancellation_does_not_race_finalizer() -> None:
    """Cancelling a turn while the artifact publish is in flight must not
    tear the shared stream accumulators or leave a half-applied result.

    The publish runs in a worker thread that cannot be interrupted, so the
    stage must (a) keep every ``_StreamState`` mutation on the event loop --
    the pre-publish mutations are applied deterministically before the
    publish starts and the post-publish result is applied only after it
    completes -- and (b) wait for the worker to drain before the
    CancelledError unwinds, so a steered follow-up turn's finalizer never
    reads the accumulators while the worker is still writing to disk.
    """
    from opensquilla.engine.artifact_delivery import OmittedArtifactPublishResult

    publish_started = threading.Event()
    release_publish = threading.Event()
    publish_finished = threading.Event()

    def blocking_publish(inner_inp: Any, accumulated_text: str) -> Any:
        # Runs in a worker thread. Announce entry, then block until the test
        # releases us, mimicking a slow ArtifactStore write.
        publish_started.set()
        assert release_publish.wait(timeout=5.0), "publish was never released"
        publish_finished.set()
        # A post-publish result that WOULD mutate shared state if applied.
        return OmittedArtifactPublishResult(
            failure_summaries=["would-be delivery failure"],
        )

    agent_run = _RecordingAgentRun(events=[DoneEvent(text="hi world")])
    stage, _ = _make_stage(agent_run=agent_run)
    stage._done_handler.run_publish = blocking_publish  # type: ignore[method-assign]
    state = _make_state()
    inp = _make_input(state=state)

    task = asyncio.ensure_future(_drain(stage, inp))

    # Handshake: wait until the worker thread has entered the publish.
    assert await asyncio.to_thread(publish_started.wait, 5.0)

    # Pre-publish ran on the loop and is fully applied before the publish;
    # post-publish has not run, so its result is not yet in shared state.
    assert state.done_event is not None
    assert state.turn_artifacts == []
    assert state.artifact_delivery_failures == []

    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)

    # The cancel is pending on the shielded wait for the worker: the stage
    # has NOT unwound yet because the worker is still blocked. (Under the
    # whole-handler offload this task would already be done.)
    assert not task.done()
    assert not publish_finished.is_set()

    # Release the worker; the stage drains it, then re-raises CancelledError.
    release_publish.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The worker completed before the turn unwound -- a finalizer running
    # after this point cannot race an in-flight store write.
    assert publish_finished.is_set()
    # The drained result's side effects ARE recorded (its failure summary
    # reaches the shared state the finalizer persists from), while the
    # notice/warning phases of post_publish stay skipped. It published no
    # artifacts, so turn_artifacts stays empty.
    assert state.turn_artifacts == []
    assert state.artifact_delivery_failures == ["would-be delivery failure"]


def _make_publish_tool_context(tmp_path: Path) -> tuple[ToolContext, Path]:
    """Real publish fixtures: a workspace file the model created and named
    in its final text, plus an ArtifactStore media root under tmp_path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media_root = tmp_path / "media"
    media_root.mkdir()
    report = workspace / "report.csv"
    report.write_text("col_a,col_b\n1,2\n", encoding="utf-8")
    ctx = ToolContext(
        workspace_dir=str(workspace),
        artifact_media_root=str(media_root),
        artifact_session_id="sess-artifact-1",
        session_key="agent:main:s1",
        workspace_file_writes=[
            {
                "path": str(report),
                "relative_path": "report.csv",
                "name": "report.csv",
                "suffix": ".csv",
                "operation": "write",
                "created": True,
            }
        ],
    )
    return ctx, media_root


@pytest.mark.asyncio
async def test_auto_published_artifact_is_adopted_before_public_yield(
    tmp_path: Path,
) -> None:
    order: list[tuple[str, str]] = []

    async def adopt(event: ArtifactEvent) -> None:
        order.append(("adopt", event.id))

    ctx, _media_root = _make_publish_tool_context(tmp_path)
    ctx.generated_artifact_adopter = adopt
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="Wrote report.csv"),
                DoneEvent(text="Wrote report.csv"),
            ]
        )
    )

    yielded: list[Any] = []
    async for event in stage.run(_make_input(tool_context=ctx)):
        if isinstance(event, ArtifactEvent):
            order.append(("yield", event.id))
        yielded.append(event)
    artifact_events = [event for event in yielded if isinstance(event, ArtifactEvent)]

    assert len(artifact_events) == 1
    assert order == [
        ("adopt", artifact_events[0].id),
        ("yield", artifact_events[0].id),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "run_status",
        "current_step_id",
        "step_status",
        "active_task_id",
        "expected_artifact_count",
    ),
    [
        pytest.param(
            "running",
            "step-1",
            "in_progress",
            "task-artifact",
            0,
            id="running-current-step",
        ),
        pytest.param(
            "running",
            None,
            "completed",
            "task-artifact",
            1,
            id="running-delivery-ready-owner",
        ),
        pytest.param(
            "running",
            None,
            "completed",
            "other-task",
            0,
            id="running-delivery-ready-other-owner",
        ),
        pytest.param(
            "completed",
            None,
            "completed",
            None,
            1,
            id="completed",
        ),
    ],
)
async def test_plan_run_auto_publish_requires_live_delivery_ready_state(
    tmp_path: Path,
    run_status: str,
    current_step_id: str | None,
    step_status: str,
    active_task_id: str | None,
    expected_artifact_count: int,
) -> None:
    class PlanStorage:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_plan_run(self, run_id: str) -> SimpleNamespace:
            self.calls.append(run_id)
            return SimpleNamespace(
                status=run_status,
                current_step_id=current_step_id,
                active_task_id=active_task_id,
                step_states=[
                    {
                        "step_id": "step-1",
                        "title": "Create report",
                        "status": step_status,
                    }
                ],
            )

    ctx, _media_root = _make_publish_tool_context(tmp_path)
    storage = PlanStorage()
    ctx.task_id = "task-artifact"
    ctx.plan_run_id = "run-artifact"
    ctx.plan_storage = storage
    state = _make_state()
    stage, _ = _make_stage(
        agent_run=_RecordingAgentRun(
            events=[
                TextDeltaEvent(text="Wrote report.csv"),
                DoneEvent(text="Wrote report.csv"),
            ]
        )
    )

    await _drain(stage, _make_input(state=state, tool_context=ctx))

    assert storage.calls == ["run-artifact"]
    assert len(ctx.published_artifacts) == expected_artifact_count
    assert state.turn_artifacts == ctx.published_artifacts


def _gate_real_publish(
    stage: StreamConsumerStage,
) -> tuple[threading.Event, threading.Event, threading.Event]:
    """Wrap the bound ``run_publish`` with an Event handshake: signal entry,
    block until released, then run the REAL publish (real ArtifactStore
    writes) and signal completion."""
    publish_started = threading.Event()
    release_publish = threading.Event()
    publish_finished = threading.Event()
    real_publish = stage._done_handler.run_publish

    def gated_publish(inner_inp: Any, accumulated_text: str) -> Any:
        publish_started.set()
        assert release_publish.wait(timeout=5.0), "publish was never released"
        result = real_publish(inner_inp, accumulated_text)
        publish_finished.set()
        return result

    stage._done_handler.run_publish = gated_publish  # type: ignore[method-assign]
    return publish_started, release_publish, publish_finished


@pytest.mark.asyncio
async def test_single_cancel_records_completed_publish(tmp_path: Path) -> None:
    """A publish that COMPLETES during a cancelled turn must be recorded.

    The worker's store write and ``ctx.published_artifacts`` append cannot be
    undone, so the cancel path must still record the result into
    ``state.turn_artifacts`` (the by-reference accumulator the cancel
    finalizer persists the transcript from). Invariant: a completed publish
    is never orphaned -- otherwise the file exists on disk, counts against
    the disk budget, and is never surfaced to the user.
    """
    ctx, media_root = _make_publish_tool_context(tmp_path)
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="Wrote report.csv"),
            DoneEvent(text="Wrote report.csv"),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    publish_started, release_publish, publish_finished = _gate_real_publish(stage)
    state = _make_state()
    inp = _make_input(state=state, tool_context=ctx)

    task = asyncio.ensure_future(_drain(stage, inp))
    assert await asyncio.to_thread(publish_started.wait, 5.0)

    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    # The stage is draining the shielded worker; it has not unwound yet.
    assert not task.done()

    release_publish.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert publish_finished.is_set()
    # The real publish took effect: bytes exist under the media root and the
    # tool context saw the published payload.
    assert [p for p in media_root.rglob("*") if p.is_file()]
    assert len(ctx.published_artifacts) == 1
    assert ctx.published_artifacts[0]["name"] == "report.csv"
    # ...so the shared turn state must carry the same payload: the cancel
    # finalizer's transcript persists from state.turn_artifacts.
    assert state.turn_artifacts == ctx.published_artifacts


@pytest.mark.asyncio
async def test_double_cancel_waits_for_worker_before_unwind(tmp_path: Path) -> None:
    """A SECOND cancel arriving during the drain must not unwind the turn
    while the worker thread is still writing to the ArtifactStore -- that
    would let a finalizer run concurrently with the in-flight store write."""
    ctx, media_root = _make_publish_tool_context(tmp_path)
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="Wrote report.csv"),
            DoneEvent(text="Wrote report.csv"),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    publish_started, release_publish, publish_finished = _gate_real_publish(stage)
    state = _make_state()
    inp = _make_input(state=state, tool_context=ctx)

    task = asyncio.ensure_future(_drain(stage, inp))
    assert await asyncio.to_thread(publish_started.wait, 5.0)

    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()

    # Second cancel while the drain wait is pending.
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    # The coroutine absorbs the repeated cancel: it must NOT finish while
    # the worker is still blocked mid-publish.
    assert not task.done()
    assert not publish_finished.is_set()

    release_publish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    # By the time the cancellation unwound, the worker had already finished
    # its store write.
    assert publish_finished.is_set()

    # Nothing mutates after the task completed: every store write and
    # ctx.published_artifacts append happened strictly before the unwind.
    published_snapshot = list(ctx.published_artifacts)
    files_snapshot = sorted(str(p) for p in media_root.rglob("*") if p.is_file())
    # ``publish_task`` cannot complete until the worker callable returns. The
    # callable runs on asyncio's long-lived default executor, so joining that
    # worker here would either wait for executor shutdown or schedule a
    # self-join on the same executor thread.
    for _ in range(10):
        await asyncio.sleep(0)
    assert list(ctx.published_artifacts) == published_snapshot
    assert sorted(str(p) for p in media_root.rglob("*") if p.is_file()) == files_snapshot


@pytest.mark.asyncio
async def test_outer_stage_persists_literal_text_before_native_tool_segment() -> None:
    literal = (
        '<tool_call>{"name":"search","arguments":{"query":"synthetic"}}'
        "</tool_call>"
    )
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text=literal),
            ToolUseStartEvent(
                tool_use_id="native-1",
                tool_name="search",
                synthetic_from_text=False,
            ),
            ToolResultEvent(
                tool_use_id="native-1",
                tool_name="search",
                result="synthetic result",
                arguments={"query": "native"},
            ),
            DoneEvent(text=literal),
        ]
    )
    state = _make_state()
    stage, _ = _make_stage(agent_run=agent_run)

    await _drain(stage, _make_input(state=state))

    assert state.turn_segments[:2] == [
        {"type": "text", "text": literal, "presentation": "answer"},
        {
            "type": "tool_use",
            "tool_use_id": "native-1",
            "name": "search",
            "input": {"query": "native"},
        },
    ]


@pytest.mark.asyncio
async def test_outer_stage_surfaces_completed_meta_when_done_text_is_empty() -> None:
    agent_run = _RecordingAgentRun(
        events=[
            ToolResultEvent(
                tool_use_id="meta-1",
                tool_name="meta_invoke",
                result="meta-skill 'AwesomeWebpageMetaSkill' completed.",
                is_error=False,
                arguments={"name": "AwesomeWebpageMetaSkill"},
            ),
            DoneEvent(text=""),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input()

    yielded = await _drain(stage, inp)

    kinds = [type(e).__name__ for e in yielded]
    assert kinds == ["ToolResultEvent", "TextDeltaEvent", "DoneEvent"]
    fallback = yielded[1]
    assert isinstance(fallback, TextDeltaEvent)
    assert "AwesomeWebpageMetaSkill" in fallback.text
    assert "没有生成可展示的最终回答" in fallback.text
    done = yielded[2]
    assert isinstance(done, DoneEvent)
    assert done.text == "".join(inp.state.final_text_parts)
    assert done.text == fallback.text


@pytest.mark.asyncio
async def test_outer_stage_preserves_meta_text_when_done_text_is_empty() -> None:
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="Final meta answer"),
            ToolResultEvent(
                tool_use_id="meta-1",
                tool_name="meta_invoke",
                result="meta-skill 'meta-kid-project-planner' completed.",
                is_error=False,
                arguments={"name": "meta-kid-project-planner"},
            ),
            DoneEvent(text=""),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input()

    yielded = await _drain(stage, inp)

    kinds = [type(e).__name__ for e in yielded]
    assert kinds == ["TextDeltaEvent", "ToolResultEvent", "DoneEvent"]
    done = yielded[2]
    assert isinstance(done, DoneEvent)
    assert done.text == "Final meta answer"
    assert inp.state.final_text_parts == ["Final meta answer"]


@pytest.mark.asyncio
async def test_outer_stage_injects_partial_failure_disclosure_before_done() -> None:
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="Parent synthesis."),
            DoneEvent(text="Parent synthesis."),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input(
        input_provenance={
            "kind": "internal_system",
            "runtime_partial_failure_disclosure_required": True,
            "subagent_group_outcome": {
                "total": 2,
                "succeeded": 1,
                "failed": 1,
                "timeout": 0,
                "cancelled": 0,
                "abandoned": 0,
                "non_success": 1,
                "failed_children": [
                    {
                        "child_session_key": "agent:worker:subagent:failed",
                        "task_id": "task-failed",
                        "agent_id": "worker-b",
                        "status": "failed",
                        "terminal_reason": "tool_error",
                        "error_class": "RuntimeError",
                        "error_message": "boom",
                    }
                ],
            },
        }
    )

    yielded = await _drain(stage, inp)

    kinds = [type(e).__name__ for e in yielded]
    assert kinds == ["TextDeltaEvent", "TextDeltaEvent", "DoneEvent"]
    disclosure = yielded[1]
    assert isinstance(disclosure, TextDeltaEvent)
    assert "Subagents: 1/2 succeeded" in disclosure.text
    assert "agent:worker:subagent:failed" in disclosure.text
    assert "RuntimeError: boom" in disclosure.text
    done = yielded[2]
    assert isinstance(done, DoneEvent)
    assert done.text == "".join(inp.state.final_text_parts)
    assert "Subagents: 1/2 succeeded" in done.text


@pytest.mark.asyncio
async def test_outer_stage_disclosure_summarizes_current_turn_exhaustion() -> None:
    agent_run = _RecordingAgentRun(events=[DoneEvent(text="Parent synthesis.")])
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input(
        input_provenance={
            "kind": "internal_system",
            "runtime_partial_failure_disclosure_required": True,
            "subagent_group_outcome": {
                "total": 2,
                "succeeded": 1,
                "failed": 1,
                "non_success": 1,
                "failed_children": [
                    {
                        "child_session_key": "agent:main:subagent:failed",
                        "status": "failed",
                        "terminal_reason": "error",
                        "error_class": "current_turn_context_exhausted",
                        "error_message": (
                            "Context overflow is in the current turn's recent tool calls "
                            "or reasoning tail; history compaction cannot reduce it."
                        ),
                    }
                ],
            },
        }
    )

    yielded = await _drain(stage, inp)

    disclosure = yielded[0]
    assert isinstance(disclosure, TextDeltaEvent)
    assert "Subagents: 1/2 succeeded" in disclosure.text
    assert "provider_request_too_large" in disclosure.text
    assert "current_turn_context_exhausted" not in disclosure.text
    assert "history compaction cannot reduce it" not in disclosure.text


@pytest.mark.asyncio
async def test_outer_stage_injects_disclosure_for_all_failed_group() -> None:
    agent_run = _RecordingAgentRun(events=[DoneEvent(text="No usable result.")])
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input(
        input_provenance={
            "kind": "internal_system",
            "runtime_partial_failure_disclosure_required": True,
            "subagent_group_outcome": {
                "total": 2,
                "succeeded": 0,
                "failed": 2,
                "timeout": 0,
                "cancelled": 0,
                "abandoned": 0,
                "non_success": 2,
                "failed_children": [
                    {
                        "child_session_key": "agent:worker:subagent:a",
                        "status": "failed",
                        "terminal_reason": "error",
                    },
                    {
                        "child_session_key": "agent:worker:subagent:b",
                        "status": "failed",
                        "terminal_reason": "error",
                    },
                ],
            },
        }
    )

    yielded = await _drain(stage, inp)

    kinds = [type(e).__name__ for e in yielded]
    assert kinds == ["TextDeltaEvent", "DoneEvent"]
    disclosure = yielded[0]
    assert isinstance(disclosure, TextDeltaEvent)
    assert "Subagents: 0/2 succeeded" in disclosure.text
    done = yielded[1]
    assert isinstance(done, DoneEvent)
    assert done.text == "".join(inp.state.final_text_parts)
    assert "Subagents: 0/2 succeeded" in done.text


@pytest.mark.asyncio
async def test_outer_stage_fails_when_disclosure_required_without_outcome() -> None:
    agent_run = _RecordingAgentRun(events=[DoneEvent(text="Parent synthesis.")])
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input(
        input_provenance={
            "kind": "internal_system",
            "runtime_partial_failure_disclosure_required": True,
        }
    )

    with pytest.raises(RuntimeError, match="outcome metadata is missing"):
        await _drain(stage, inp)


@pytest.mark.asyncio
async def test_outer_stage_suppresses_compaction_event_and_refreshes_runtime_state() -> None:
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="hi"),
            CompactionEvent(summary="sum", kept_entries=[1, 2, 3]),
            TextDeltaEvent(text=" after"),
            DoneEvent(text="hi after"),
        ]
    )
    stage, recs = _make_stage(agent_run=agent_run)
    inp = _make_input()
    yielded = await _drain(stage, inp)
    kinds = [type(e).__name__ for e in yielded]
    # CompactionEvent must NOT be yielded.
    assert "CompactionEvent" not in kinds
    assert kinds == ["TextDeltaEvent", "TextDeltaEvent", "DoneEvent"]
    # In-turn compaction refreshes fired in order.
    assert len(recs["compaction_persist"].calls) == 1
    assert recs["compaction_persist"].calls[0]["kept_entries"] == [1, 2, 3]
    assert len(recs["memory_snapshot_refresh"].calls) == 1
    assert len(recs["system_prompt_refresh"].calls) == 1


@pytest.mark.asyncio
async def test_outer_stage_suppresses_error_event_and_records_pending() -> None:
    agent_run = _RecordingAgentRun(
        events=[
            TextDeltaEvent(text="partial"),
            ErrorEvent(message="boom", code="agent_error"),
        ]
    )
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input()
    yielded = await _drain(stage, inp)
    # ErrorEvent is NOT yielded; the stream continues without yielding it.
    kinds = [type(e).__name__ for e in yielded]
    assert kinds == ["TextDeltaEvent"]
    assert inp.state.pending_error_event is not None
    assert inp.state.pending_error_event.code == "agent_error"
    assert inp.state.error_message == "boom"


@pytest.mark.asyncio
async def test_outer_stage_propagates_agent_run_exception() -> None:
    agent_run = _RecordingAgentRun(
        events=[TextDeltaEvent(text="partial")],
        raises=RuntimeError,
    )
    stage, _ = _make_stage(agent_run=agent_run)
    inp = _make_input()
    with pytest.raises(RuntimeError):
        await _drain(stage, inp)
    assert inp.state.final_text_parts == ["partial"]


@pytest.mark.asyncio
async def test_outer_stage_empty_stream_still_notifies() -> None:
    agent_run = _RecordingAgentRun(events=[])
    stage, recs = _make_stage(agent_run=agent_run)
    inp = _make_input(sync_manager=object())
    yielded = await _drain(stage, inp)
    assert yielded == []
    assert len(recs["memory_sync_notify"].calls) == 1


def test_stage_name() -> None:
    assert StreamConsumerStage.name == "stream_consumer_stage"


def test_turn_context_surface_kind_defaults_to_unknown() -> None:
    """PR3: surface_kind is "unknown" unless gateway/CLI/channel sets it."""
    from opensquilla.engine.pipeline import TurnContext

    # TurnContext requires: message, session_key, config, provider, model,
    # tool_defs, system_prompt — check the actual signature in pipeline.py
    # if this construction fails; pass minimal-but-valid args.
    ctx = TurnContext(
        message="hi",
        session_key="S",
        config=None,
        provider=None,
        model="",
        tool_defs=[],
        system_prompt="",
    )
    assert ctx.surface_kind == "unknown"


def test_done_handler_stamps_decision_id() -> None:
    state = _make_state()
    handler = _DoneHandler()
    inp = _make_input(
        state=state,
        turn=_make_turn(metadata={"router_decision_id": "b" * 32}),
    )
    done = DoneEvent(text="result", input_tokens=1, output_tokens=1)
    transformed, _ = handler.handle(done, inp, state)
    assert transformed.decision_id == "b" * 32

    # Missing/empty metadata -> None, never "".
    inp2 = _make_input(state=_make_state(), turn=_make_turn(metadata={}))
    transformed2, _ = handler.handle(
        DoneEvent(text="r", input_tokens=1, output_tokens=1), inp2, _make_state()
    )
    assert transformed2.decision_id is None
