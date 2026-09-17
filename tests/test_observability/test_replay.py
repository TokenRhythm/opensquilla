from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opensquilla.cli.replay import replay_app
from opensquilla.engine.pipeline import TurnContext, run_pipeline
from opensquilla.engine.steps.inject_subagent_grounding import inject_subagent_grounding
from opensquilla.observability.decision_log import (
    DecisionEntry,
    PipelineStepRecord,
    write_decision_entry,
)
from opensquilla.observability.replay import format_transcript, load_turn


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


async def test_pipeline_status_survives_private_logs_and_cli_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENSQUILLA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSQUILLA_DEBUG_LOG", "1")
    error_message = "Synthetic pipeline failure details"

    async def succeed(ctx: TurnContext) -> TurnContext:
        return ctx

    async def fail(ctx: TurnContext) -> TurnContext:
        raise RuntimeError(error_message)

    async def empty_fail(ctx: TurnContext) -> TurnContext:
        raise RuntimeError()

    ctx = TurnContext(
        message="Synthetic input",
        session_key="agent:main:webchat:default",
        config=None,
        provider=None,
        model="test-model",
        tool_defs=[],
        system_prompt="Synthetic system prompt",
    )
    ctx = await run_pipeline(ctx, [succeed, inject_subagent_grounding, fail, empty_fail])
    records = ctx.metadata["pipeline_steps"]
    entry = _entry_with_step(records[0])
    entry.pipeline_steps = records
    path = write_decision_entry(entry)
    debug_path = path.parent / "debug" / f"{path.stem}-raw.jsonl"

    expected_statuses = ["ok", "skipped", "failed", "failed"]
    for log_path in (path, debug_path):
        raw = log_path.read_text(encoding="utf-8")
        assert error_message not in raw
        payload = json.loads(raw)
        if log_path == debug_path:
            payload = payload["entry"]
        assert [step["status"] for step in payload["pipeline_steps"]] == expected_statuses
        assert all("fallback_reason" not in step for step in payload["pipeline_steps"])

    loaded = load_turn(entry.session_key, entry.turn_id)
    assert loaded is not None
    assert [step.status for step in loaded.pipeline_steps] == expected_statuses
    assert all(step.fallback_reason is None for step in loaded.pipeline_steps)

    result = CliRunner().invoke(
        replay_app, ["--session", entry.session_key, "--turn", entry.turn_id],
    )
    assert result.exit_code == 0, result.output
    for name, status in (
        ("succeed", "OK"),
        ("inject_subagent_grounding", "SKIPPED"),
        ("fail", "FAIL"),
        ("empty_fail", "FAIL"),
    ):
        assert f"- {name} [{status}]" in result.output
    assert error_message not in result.output


@pytest.mark.parametrize(
    ("step_fields", "expected_status"),
    [
        ({"applied": True}, "OK"),
        ({"applied": False}, "UNKNOWN"),
        ({"applied": False, "fallback_reason": None}, "SKIPPED"),
        ({"applied": False, "fallback_reason": ""}, "FAIL()"),
        ({"applied": False, "fallback_reason": "synthetic-error"}, "FAIL(synthetic-error)"),
        ({"applied": False, "status": None}, "UNKNOWN"),
        ({"applied": False, "status": None, "fallback_reason": None}, "SKIPPED"),
        ({"applied": False, "status": "failed"}, "FAIL"),
        ({"applied": False, "status": "skipped"}, "SKIPPED"),
        ({"applied": False, "status": "unknown", "fallback_reason": None}, "UNKNOWN"),
        ({"applied": False, "status": "future-status"}, "UNKNOWN"),
    ],
    ids=(
        "legacy-success",
        "legacy-redacted-unknown",
        "legacy-explicit-skip",
        "legacy-empty-failure",
        "legacy-failure",
        "null-status-redacted-unknown",
        "null-status-explicit-skip",
        "explicit-redacted-failure",
        "explicit-redacted-skip",
        "explicit-unknown",
        "future-status",
    ),
)
def test_replay_hydrates_legacy_and_explicit_statuses(
    tmp_path: Path,
    step_fields: dict[str, object],
    expected_status: str,
) -> None:
    entry = _entry_with_step(PipelineStepRecord(step_name="step", applied=True))
    payload = asdict(entry)
    payload["schema_version"] = 16
    payload["pipeline_steps"] = [{"step_name": "step", **step_fields}]
    path = tmp_path / "decisions-20260903.jsonl"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    loaded = load_turn(entry.session_key, entry.turn_id, log_dir=tmp_path)

    assert loaded is not None
    assert f"    - step [{expected_status}]" in format_transcript(loaded)
