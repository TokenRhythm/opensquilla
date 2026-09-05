from __future__ import annotations

import pytest

from opensquilla.observability.decision_log import DecisionEntry, PipelineStepRecord
from opensquilla.observability.replay import format_transcript


def _entry_with_step(step: PipelineStepRecord) -> DecisionEntry:
    return DecisionEntry(
        turn_id="turn-1",
        session_key="agent:main:webchat:default",
        prompt_hash="prompt",
        system_prompt_hash="system",
        tool_list_hash="tools",
        tool_choice="auto",
        tokens_input=1,
        tokens_output=1,
        model="test-model",
        provider="test-provider",
        latency_ms=1,
        ts="2026-09-03T00:00:00Z",
        pipeline_steps=[step],
    )


@pytest.mark.parametrize(
    ("step", "expected_status"),
    [
        (PipelineStepRecord(step_name="step", applied=True), "OK"),
        (PipelineStepRecord(step_name="step", applied=False), "SKIPPED"),
        (
            PipelineStepRecord(
                step_name="step",
                applied=False,
                fallback_reason="boom",
            ),
            "FAIL(boom)",
        ),
        (
            PipelineStepRecord(
                step_name="step",
                applied=False,
                fallback_reason="",
            ),
            "FAIL()",
        ),
    ],
    ids=("applied", "skipped", "failed", "empty-failure-reason"),
)
def test_format_transcript_renders_pipeline_step_status(
    step: PipelineStepRecord,
    expected_status: str,
) -> None:
    transcript = format_transcript(_entry_with_step(step))

    assert f"    - step [{expected_status}]" in transcript
