"""Unit tests for ``TurnFinalizerStage`` driven directly (no full
TurnRunner stack).

Drives the stage through ``TurnFinalizerStage.run`` with recording
fakes for all four ports, exercising each branch (transcript-yes /
transcript-no, memory-yes / memory-raise, error-yes / error-no,
rollup-yes / rollup-raise) and the heartbeat-empty edge.

Raising-fake cases for ``TurnMemoryCapturePort`` and ``SessionTotalsPort`` are
included so the log-and-continue arms in the stage body are exercised without
the runtime wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from opensquilla.engine.turn_runner.turn_finalizer_stage import (
    CostRollupResult,
    TranscriptAppendResult,
    TurnFinalizerStage,
    TurnFinalizerStageInput,
)
from opensquilla.engine.types import DoneEvent, ErrorEvent

# ---------------------------------------------------------------------------
# Recording fakes
# ---------------------------------------------------------------------------


@dataclass
class _RecordingTranscriptAppend:
    return_value: TranscriptAppendResult | bool = field(
        default_factory=lambda: TranscriptAppendResult(
            appended=True,
            message_id="assistant-message-1",
        )
    )
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def append_message(
        self,
        session_key: str,
        *,
        role: str,
        content: str,
        tool_calls: list[Any] | None,
        reasoning_content: str | None,
        turn_usage: dict[str, Any] | None,
        token_count: int | None,
    ) -> TranscriptAppendResult | bool:
        self.calls.append(
            {
                "session_key": session_key,
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
                "reasoning_content": reasoning_content,
                "turn_usage": turn_usage,
                "token_count": token_count,
            }
        )
        if self.raises is not None:
            raise self.raises("recording transcript boom")
        return self.return_value


@dataclass
class _RecordingTurnMemoryCapture:
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def capture_turn(
        self,
        *,
        agent_id: str,
        session_key: str,
        runtime_message: str,
        final_text: str,
        input_mode: str,
        tool_context: Any,
        input_provenance: dict[str, Any] | None,
        run_kind: str,
        no_memory_capture: bool,
    ) -> None:
        self.calls.append(
            {
                "agent_id": agent_id,
                "session_key": session_key,
                "runtime_message": runtime_message,
                "final_text": final_text,
                "input_mode": input_mode,
                "tool_context": tool_context,
                "input_provenance": input_provenance,
                "run_kind": run_kind,
                "no_memory_capture": no_memory_capture,
            }
        )
        if self.raises is not None:
            raise self.raises("recording memory boom")


@dataclass
class _RecordingSessionTotals:
    return_value: CostRollupResult | None = None
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def rollup(
        self,
        *,
        session_key: str,
        done_event: DoneEvent,
        resolved_model: str,
    ) -> CostRollupResult | None:
        self.calls.append(
            {
                "session_key": session_key,
                "done_event": done_event,
                "resolved_model": resolved_model,
            }
        )
        if self.raises is not None:
            raise self.raises("recording rollup boom")
        return self.return_value


@dataclass
class _RecordingTurnErrorPersist:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def persist_error(
        self,
        *,
        session_key: str,
        event: ErrorEvent | None,
    ) -> None:
        self.calls.append(
            {
                "session_key": session_key,
                "event": event,
            }
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stage(
    *,
    transcript_append: _RecordingTranscriptAppend | None = None,
    turn_memory_capture: _RecordingTurnMemoryCapture | None = None,
    session_totals: _RecordingSessionTotals | None = None,
    turn_error_persist: _RecordingTurnErrorPersist | None = None,
) -> tuple[TurnFinalizerStage, dict[str, Any]]:
    transcript_append = transcript_append or _RecordingTranscriptAppend()
    turn_memory_capture = turn_memory_capture or _RecordingTurnMemoryCapture()
    session_totals = session_totals or _RecordingSessionTotals()
    turn_error_persist = turn_error_persist or _RecordingTurnErrorPersist()
    stage = TurnFinalizerStage(
        transcript_append=transcript_append,
        turn_memory_capture=turn_memory_capture,
        session_totals=session_totals,
        turn_error_persist=turn_error_persist,
    )
    recordings = {
        "transcript_append": transcript_append,
        "turn_memory_capture": turn_memory_capture,
        "session_totals": session_totals,
        "turn_error_persist": turn_error_persist,
    }
    return stage, recordings


def _make_input(
    *,
    final_text_parts: list[str] | None = None,
    turn_segments: list[dict] | None = None,
    turn_artifacts: list[dict[str, Any]] | None = None,
    error_message: str | None = None,
    pending_error_event: ErrorEvent | None = None,
    done_event: DoneEvent | None = None,
    runtime_message: str = "hi",
    input_mode: str = "user",
    input_provenance: dict[str, Any] | None = None,
    resolved_model: str = "synthetic-turn-model-4.5",
    agent_id: str = "agent:main",
    session_key: str = "agent:main:s1",
    run_kind: str = "default",
    heartbeat_ack_max_chars: int = 300,
    no_memory_capture: bool = False,
) -> TurnFinalizerStageInput:
    return TurnFinalizerStageInput(
        final_text_parts=final_text_parts if final_text_parts is not None else [],
        turn_segments=turn_segments if turn_segments is not None else [],
        turn_artifacts=turn_artifacts if turn_artifacts is not None else [],
        error_message=error_message,
        pending_error_event=pending_error_event,
        done_event=done_event,
        runtime_message=runtime_message,
        input_mode=input_mode,
        input_provenance=input_provenance,
        resolved_model=resolved_model,
        agent_id=agent_id,
        session_key=session_key,
        tool_context=None,
        run_kind=run_kind,
        heartbeat_ack_max_chars=heartbeat_ack_max_chars,
        no_memory_capture=no_memory_capture,
    )


# ---------------------------------------------------------------------------
# Stage class-level tests
# ---------------------------------------------------------------------------


def test_stage_name() -> None:
    assert TurnFinalizerStage.name == "turn_finalizer_stage"


@pytest.mark.asyncio
async def test_simple_text_no_done_event_appends_and_captures() -> None:
    stage, recs = _make_stage()
    inp = _make_input(final_text_parts=["hi"])
    outcome = await stage.run(inp)
    out = outcome.output
    assert out.final_text == "hi"
    assert out.transcript_appended is True
    assert out.assistant_message_id == "assistant-message-1"
    assert out.assistant_message_content == "hi"
    assert out.memory_captured is True
    assert out.cost_rollup is None
    assert len(recs["transcript_append"].calls) == 1
    assert recs["transcript_append"].calls[0]["role"] == "assistant"
    assert recs["transcript_append"].calls[0]["content"] == "hi"
    assert recs["transcript_append"].calls[0]["tool_calls"] is None
    assert recs["transcript_append"].calls[0]["reasoning_content"] is None
    assert recs["transcript_append"].calls[0]["token_count"] is None
    assert len(recs["turn_memory_capture"].calls) == 1
    assert recs["turn_error_persist"].calls == []
    assert recs["session_totals"].calls == []


@pytest.mark.asyncio
async def test_legacy_boolean_transcript_port_remains_compatible() -> None:
    stage, _ = _make_stage(
        transcript_append=_RecordingTranscriptAppend(return_value=True),
    )

    outcome = await stage.run(_make_input(final_text_parts=["legacy adapter reply"]))

    assert outcome.output.transcript_appended is True
    assert outcome.output.assistant_message_id is None
    assert outcome.output.assistant_message_content == "legacy adapter reply"
    assert outcome.output.memory_captured is True


@pytest.mark.asyncio
async def test_simple_text_with_done_event_fires_rollup() -> None:
    rollup_value = CostRollupResult(
        input_tokens=5,
        output_tokens=3,
        total_tokens=8,
        estimated_cost_usd=0.001,
        total_cost_usd=0.001,
        billed_cost_usd=0.001,
        estimated_cost_component_usd=0.0,
        cost_source="provider",
        missing_cost_entries=0,
        cache_read=0,
        cache_write=0,
        model_override="synthetic-turn-model-4.5",
    )
    stage, recs = _make_stage(
        session_totals=_RecordingSessionTotals(return_value=rollup_value),
    )
    done = DoneEvent(
        text="hi",
        input_tokens=5,
        output_tokens=3,
        model="synthetic-turn-model-4.5",
        routed_tier="c2",
        routing_applied=False,
        rollout_phase="observe",
    )
    inp = _make_input(final_text_parts=["hi"], done_event=done)
    outcome = await stage.run(inp)
    out = outcome.output
    assert out.transcript_appended is True
    assert out.memory_captured is True
    assert out.cost_rollup is rollup_value
    assert len(recs["session_totals"].calls) == 1
    assert recs["session_totals"].calls[0]["done_event"] is done
    assert recs["transcript_append"].calls[0]["token_count"] == 3
    assert recs["transcript_append"].calls[0]["turn_usage"]["input_tokens"] == 5
    assert recs["transcript_append"].calls[0]["turn_usage"]["output_tokens"] == 3
    assert recs["transcript_append"].calls[0]["turn_usage"]["model"] == "synthetic-turn-model-4.5"
    assert recs["transcript_append"].calls[0]["turn_usage"]["routed_tier"] == "c2"
    assert recs["transcript_append"].calls[0]["turn_usage"]["routing_applied"] is False
    assert recs["transcript_append"].calls[0]["turn_usage"]["rollout_phase"] == "observe"


@pytest.mark.asyncio
async def test_turn_usage_persists_ensemble_breakdown_and_trace() -> None:
    stage, recs = _make_stage()
    done = DoneEvent(
        text="ensemble answer",
        input_tokens=11,
        output_tokens=7,
        model="z-ai/glm-5.2",
        model_usage_breakdown=[
            {
                "role": "proposer",
                "label": "proposer_1",
                "provider": "openrouter",
                "model": "deepseek/deepseek-v4-pro",
                "input_tokens": 3,
                "output_tokens": 2,
                "billed_cost": 0.01,
            },
            {
                "role": "aggregator",
                "label": "aggregator",
                "provider": "openrouter",
                "model": "z-ai/glm-5.2",
                "input_tokens": 8,
                "output_tokens": 5,
                "billed_cost": 0.02,
            },
        ],
        ensemble_trace={
            "mode": "b5_fusion",
            "profile": "default",
            "llm_request_count": 2,
            "fallback_used": False,
        },
    )
    inp = _make_input(final_text_parts=["ensemble answer"], done_event=done)

    await stage.run(inp)

    usage = recs["transcript_append"].calls[0]["turn_usage"]
    assert usage["model_usage_breakdown"][0]["model"] == "deepseek/deepseek-v4-pro"
    assert usage["model_usage_breakdown"][1]["role"] == "aggregator"
    assert usage["ensemble_trace"]["profile"] == "default"
    assert usage["ensemble_trace"]["llm_request_count"] == 2


@pytest.mark.asyncio
async def test_turn_usage_persists_vision_followup_metadata() -> None:
    stage, recs = _make_stage()
    done = DoneEvent(text="ok", input_tokens=5, output_tokens=3)
    done.image_route_reason = "gate_history"
    done.vision_followup_gate_decision = "needs_image"
    done.vision_followup_gate_confidence = 0.92
    done.vision_followup_gate_reason = "references previous image with private detail"
    done.vision_followup_gate_source = "llm"
    done.vision_followup_gate_model = "deepseek/deepseek-v4-flash"
    done.vision_followup_needs_image = True
    inp = _make_input(final_text_parts=["ok"], done_event=done)

    await stage.run(inp)

    usage = recs["transcript_append"].calls[0]["turn_usage"]
    assert usage["image_route_reason"] == "gate_history"
    assert usage["vision_followup_gate_decision"] == "needs_image"
    assert usage["vision_followup_gate_confidence"] == 0.92
    assert usage["vision_followup_gate_reason"] == "llm_needs_image"
    assert "private detail" not in usage["vision_followup_gate_reason"]
    assert usage["vision_followup_gate_source"] == "llm"
    assert usage["vision_followup_gate_model"] == "deepseek/deepseek-v4-flash"
    assert usage["vision_followup_needs_image"] is True


@pytest.mark.asyncio
async def test_disclosed_subagent_outcome_persists_once_and_captures_same_text() -> None:
    disclosure = "Subagents: 1/2 succeeded; failures: child failed."
    final_text = f"Parent synthesis.\n\n{disclosure}"
    input_provenance = {
        "kind": "internal_system",
        "runtime_partial_failure_disclosure_required": True,
        "subagent_group_outcome": {
            "total": 2,
            "succeeded": 1,
            "failed": 1,
            "non_success": 1,
            "failed_children": [{"child_session_key": "child", "status": "failed"}],
        },
    }
    stage, recs = _make_stage()
    inp = _make_input(
        final_text_parts=[final_text],
        done_event=DoneEvent(text=final_text, output_tokens=5),
        input_provenance=input_provenance,
    )

    outcome = await stage.run(inp)

    assert outcome.output.final_text == final_text
    assert recs["transcript_append"].calls[0]["content"] == final_text
    assert recs["transcript_append"].calls[0]["content"].count(disclosure) == 1
    assert recs["turn_memory_capture"].calls[0]["final_text"] == final_text
    assert recs["turn_memory_capture"].calls[0]["input_provenance"] == input_provenance


@pytest.mark.asyncio
async def test_turn_with_artifacts_persists_json_wrapped_content() -> None:
    artifact = {"id": "a1", "mime": "image/png"}
    stage, recs = _make_stage()
    inp = _make_input(
        final_text_parts=["got it"],
        turn_artifacts=[artifact],
    )
    await stage.run(inp)
    assert len(recs["transcript_append"].calls) == 1
    content = recs["transcript_append"].calls[0]["content"]
    assert content.startswith("{") and content.endswith("}")
    assert "got it" in content
    assert "a1" in content


@pytest.mark.asyncio
async def test_tool_use_segments_persist_with_tool_calls() -> None:
    segments: list[dict[str, Any]] = [
        {"type": "tool_use", "tool_use_id": "c1", "name": "echo", "input": ""},
    ]
    stage, recs = _make_stage()
    inp = _make_input(turn_segments=segments)
    await stage.run(inp)
    assert len(recs["transcript_append"].calls) == 1
    assert recs["transcript_append"].calls[0]["tool_calls"] == segments
    assert recs["transcript_append"].calls[0]["content"] == ""


@pytest.mark.asyncio
async def test_unknown_background_tool_status_adds_confirmation_guard() -> None:
    segments: list[dict[str, Any]] = [
        {
            "type": "tool_result",
            "tool_use_id": "c1",
            "name": "background_process",
            "result": "session: open-browser\nstatus: running",
            "is_error": False,
            "execution_status": {
                "version": 1,
                "status": "unknown",
                "exit_code": None,
                "timed_out": False,
                "truncated": False,
                "reason": "background_running",
                "source": "adapter",
                "preservation_class": "ephemeral",
            },
        },
    ]
    stage, recs = _make_stage()
    inp = _make_input(
        final_text_parts=["Opened it in the default browser."],
        turn_segments=segments,
    )

    outcome = await stage.run(inp)

    assert "Opened it in the default browser." in outcome.output.final_text
    assert "could not confirm" in outcome.output.final_text
    assert "background_process" in outcome.output.final_text
    assert recs["transcript_append"].calls[0]["content"] == outcome.output.final_text


@pytest.mark.asyncio
async def test_successful_background_tool_status_does_not_add_confirmation_guard() -> None:
    segments: list[dict[str, Any]] = [
        {
            "type": "tool_result",
            "tool_use_id": "c1",
            "name": "background_process",
            "result": "session: open-browser\nstatus: complete",
            "is_error": False,
            "execution_status": {
                "version": 1,
                "status": "success",
                "exit_code": 0,
                "timed_out": False,
                "truncated": False,
                "reason": "exit_code_zero",
                "source": "adapter",
                "preservation_class": "durable",
            },
        },
    ]
    stage, recs = _make_stage()
    inp = _make_input(
        final_text_parts=["Opened it in the default browser."],
        turn_segments=segments,
    )

    outcome = await stage.run(inp)

    assert outcome.output.final_text == "Opened it in the default browser."
    assert recs["transcript_append"].calls[0]["content"] == outcome.output.final_text


@pytest.mark.asyncio
async def test_pending_error_persists_via_error_port() -> None:
    err = ErrorEvent(message="boom", code="agent_error")
    stage, recs = _make_stage()
    inp = _make_input(
        final_text_parts=["partial"],
        error_message="boom",
        pending_error_event=err,
    )
    await stage.run(inp)
    assert len(recs["turn_error_persist"].calls) == 1
    assert recs["turn_error_persist"].calls[0]["event"] is err


@pytest.mark.asyncio
async def test_heartbeat_empty_clears_text_and_segments() -> None:
    """Sentinel-only final text drops to empty and clears all-text segments."""

    stage, recs = _make_stage()
    segments: list[dict[str, Any]] = [
        {"type": "text", "text": "HEARTBEAT_OK"},
    ]
    inp = _make_input(
        final_text_parts=["HEARTBEAT_OK"],
        turn_segments=segments,
        run_kind="heartbeat",
    )
    outcome = await stage.run(inp)
    out = outcome.output
    # Sentinel-only payload normalizes to empty; all-text segments drop.
    assert out.final_text == ""
    assert out.turn_segments == []
    # No transcript persistence since (final_text, segments, artifacts) all empty.
    assert out.transcript_appended is False
    assert out.memory_captured is False
    assert recs["transcript_append"].calls == []
    assert recs["turn_memory_capture"].calls == []


@pytest.mark.asyncio
async def test_heartbeat_long_think_block_before_ack_is_not_delivered() -> None:
    """OSQ-505: private reasoning must not bypass heartbeat ACK suppression."""

    stage, recs = _make_stage()
    private_reasoning = "internal reasoning must stay private. " * 20
    model_output = f"<think>{private_reasoning}</think>\nHEARTBEAT_OK"
    segments: list[dict[str, Any]] = [{"type": "text", "text": model_output}]
    inp = _make_input(
        final_text_parts=[model_output],
        turn_segments=segments,
        run_kind="heartbeat",
    )

    outcome = await stage.run(inp)

    assert outcome.output.final_text == ""
    assert outcome.output.turn_segments == []
    assert recs["transcript_append"].calls == []
    assert recs["turn_memory_capture"].calls == []


@pytest.mark.asyncio
async def test_heartbeat_think_block_is_removed_from_real_alert() -> None:
    stage, recs = _make_stage()
    model_output = "<think>check disk usage</think>\nDisk usage reached 95%."
    inp = _make_input(
        final_text_parts=[model_output],
        turn_segments=[{"type": "text", "text": model_output}],
        run_kind="heartbeat",
    )

    outcome = await stage.run(inp)

    assert outcome.output.final_text == "Disk usage reached 95%."
    assert outcome.output.turn_segments == [
        {"type": "text", "text": "Disk usage reached 95%."}
    ]
    assert recs["transcript_append"].calls[0]["content"] == "Disk usage reached 95%."
    assert recs["transcript_append"].calls[0]["tool_calls"] == [
        {"type": "text", "text": "Disk usage reached 95%."}
    ]


@pytest.mark.asyncio
async def test_non_heartbeat_think_block_is_unchanged() -> None:
    stage, recs = _make_stage()
    model_output = "<think>visible protocol text</think>\nRegular reply."
    segments = [{"type": "text", "text": model_output}]
    inp = _make_input(
        final_text_parts=[model_output],
        turn_segments=segments,
        run_kind="default",
    )

    outcome = await stage.run(inp)

    assert outcome.output.final_text == model_output
    assert outcome.output.turn_segments == segments
    assert recs["transcript_append"].calls[0]["content"] == model_output


@pytest.mark.asyncio
async def test_reasoning_content_included_for_deepseek_model() -> None:
    stage, recs = _make_stage()
    done = DoneEvent(
        text="hi",
        input_tokens=1,
        output_tokens=1,
        model="deepseek-r1",
        reasoning_content="thinking...",
    )
    inp = _make_input(
        final_text_parts=["hi"],
        done_event=done,
        resolved_model="deepseek-r1",
    )
    await stage.run(inp)
    assert recs["transcript_append"].calls[0]["reasoning_content"] == "thinking..."


@pytest.mark.asyncio
async def test_reasoning_content_excluded_for_non_deepseek_model() -> None:
    stage, recs = _make_stage()
    done = DoneEvent(
        text="hi",
        input_tokens=1,
        output_tokens=1,
        model="synthetic-long-model-4",
        reasoning_content="thinking...",
    )
    inp = _make_input(
        final_text_parts=["hi"],
        done_event=done,
        resolved_model="synthetic-long-model-4",
    )
    await stage.run(inp)
    assert recs["transcript_append"].calls[0]["reasoning_content"] is None


@pytest.mark.asyncio
async def test_no_session_manager_skips_all_writes() -> None:
    stage, recs = _make_stage(
        transcript_append=_RecordingTranscriptAppend(
            return_value=TranscriptAppendResult(appended=False)
        ),
    )
    done = DoneEvent(text="hi", input_tokens=1, output_tokens=1)
    inp = _make_input(
        final_text_parts=["hi"],
        done_event=done,
        error_message="some err",
        pending_error_event=ErrorEvent(message="m", code="x"),
    )
    outcome = await stage.run(inp)
    out = outcome.output
    assert out.transcript_appended is False
    assert out.assistant_message_id is None
    assert out.assistant_message_content is None
    # Memory NOT captured because transcript port returned False (no manager).
    assert out.memory_captured is False
    assert recs["turn_memory_capture"].calls == []
    # Error persist still fires (helper guards internally).
    assert len(recs["turn_error_persist"].calls) == 1
    # Totals rollup still fires (adapter guards internally).
    assert len(recs["session_totals"].calls) == 1


@pytest.mark.asyncio
async def test_memory_capture_raises_log_and_continue() -> None:
    stage, recs = _make_stage(
        turn_memory_capture=_RecordingTurnMemoryCapture(raises=RuntimeError),
    )
    done = DoneEvent(text="hi", input_tokens=1, output_tokens=1)
    inp = _make_input(
        final_text_parts=["hi"],
        done_event=done,
        error_message="boom",
        pending_error_event=ErrorEvent(message="m", code="x"),
    )
    # Must NOT raise -- log-and-continue per legacy.
    outcome = await stage.run(inp)
    out = outcome.output
    assert out.transcript_appended is True
    assert out.memory_captured is False
    # Error persist + rollup still fire after memory failure.
    assert len(recs["turn_error_persist"].calls) == 1
    assert len(recs["session_totals"].calls) == 1


@pytest.mark.asyncio
async def test_session_totals_raises_log_and_continue() -> None:
    stage, recs = _make_stage(
        session_totals=_RecordingSessionTotals(raises=RuntimeError),
    )
    done = DoneEvent(text="hi", input_tokens=1, output_tokens=1)
    inp = _make_input(final_text_parts=["hi"], done_event=done)
    # Must NOT raise -- log-and-continue per legacy.
    outcome = await stage.run(inp)
    out = outcome.output
    assert out.transcript_appended is True
    assert out.memory_captured is True
    assert out.cost_rollup is None
    assert len(recs["session_totals"].calls) == 1


@pytest.mark.asyncio
async def test_transcript_raises_propagates() -> None:
    stage, _ = _make_stage(
        transcript_append=_RecordingTranscriptAppend(raises=RuntimeError),
    )
    inp = _make_input(final_text_parts=["hi"])
    # No try/except in the stage body around the transcript port.
    with pytest.raises(RuntimeError):
        await stage.run(inp)


@pytest.mark.asyncio
async def test_no_content_skips_transcript_and_memory() -> None:
    stage, recs = _make_stage()
    inp = _make_input(final_text_parts=[], turn_segments=[], turn_artifacts=[])
    outcome = await stage.run(inp)
    out = outcome.output
    assert out.transcript_appended is False
    assert out.memory_captured is False
    assert recs["transcript_append"].calls == []
    assert recs["turn_memory_capture"].calls == []
