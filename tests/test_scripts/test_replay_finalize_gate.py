"""Tests for scripts/experiments/replay_finalize_gate.py live-parity reporting.

The replay harness must mirror the live gate's firing condition: the gate
only evaluates on a zero-tool-call assistant message (a finalize attempt),
so transcripts that end mid-loop are reported quiet with an explicit
suppression reason while keeping the raw triggers for diagnostics.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


def _load_script():
    path = Path(__file__).parents[2] / "scripts" / "experiments" / "replay_finalize_gate.py"
    spec = importlib.util.spec_from_file_location("replay_finalize_gate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assistant_tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "message": {
            "role": "assistant",
            "content": [
                {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}
            ],
        }
    }


def _tool_result(
    call_id: str,
    name: str,
    text: str,
    *,
    is_error: bool = False,
    execution_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "toolResult",
        "toolName": name,
        "toolCallId": call_id,
        "isError": is_error,
        "content": text,
    }
    if execution_status is not None:
        message["executionStatus"] = execution_status
    return {"message": message}


_RED_EXEC_ENTRIES = [
    _assistant_tool_call("w1", "edit_file", {"path": "src/main.py"}),
    _tool_result("w1", "edit_file", "edited"),
    _assistant_tool_call("x1", "exec_command", {"command": "python repro.py"}),
    _tool_result(
        "x1",
        "exec_command",
        "exit_code=1\nFAILED: assertion did not hold",
        is_error=True,
        execution_status={
            "version": 1,
            "status": "error",
            "exit_code": 1,
            "timed_out": False,
            "reason": "nonzero_exit",
        },
    ),
]

_FINAL_TEXT_ENTRY = {
    "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "The fix is complete."}],
    }
}


def _write_run_dir(
    tmp_path: Path,
    entries: list[dict[str, Any]],
    *,
    git_patch: str | None = "diff --git a/src/main.py b/src/main.py\n+new\n",
) -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "transcript.jsonl").write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )
    if git_patch is not None:
        (run_dir / "git.patch").write_text(git_patch, encoding="utf-8")
    return run_dir


def test_replay_fires_on_finalize_attempt_with_red_evidence(tmp_path) -> None:
    module = _load_script()
    run_dir = _write_run_dir(tmp_path, [*_RED_EXEC_ENTRIES, _FINAL_TEXT_ENTRY])

    report = module.replay_run(run_dir)

    assert report["finalize_attempt_at_end"] is True
    assert report["should_challenge"] is True
    assert report["triggers"] == ["red_execution_after_final_edit"]
    assert report["has_workspace_diff_source"] == "git.patch"
    # Both-modes reporting: strict fields ride the same line. The base
    # trigger set is a prefix of the strict one (strict only appends);
    # red-first state is reported but never a trigger, so with one
    # verification command strict adds nothing here.
    assert report["strict_should_challenge"] is True
    assert report["strict_triggers"] == [
        "red_execution_after_final_edit",
    ]
    assert report["strict_primary_reason"] == "red_execution_after_final_edit"
    assert report["strict_red_first_satisfied"] is False
    assert report["strict_verification_command_count"] == 1


def test_replay_strict_quiet_on_red_then_green_trace(tmp_path) -> None:
    # A demonstrated red-then-green pair on the same command satisfies
    # red-first: both base and strict trackers stay quiet at finalize.
    module = _load_script()
    green_result = _tool_result(
        "x2",
        "exec_command",
        "exit_code=0\nok",
        execution_status={
            "version": 1,
            "status": "success",
            "exit_code": 0,
            "timed_out": False,
            "reason": None,
        },
    )
    entries = [
        *_RED_EXEC_ENTRIES,
        _assistant_tool_call("x2", "exec_command", {"command": "python repro.py"}),
        green_result,
        _FINAL_TEXT_ENTRY,
    ]
    run_dir = _write_run_dir(tmp_path, entries)

    report = module.replay_run(run_dir)

    assert report["should_challenge"] is False
    assert report["strict_should_challenge"] is False
    assert report["strict_triggers"] == []
    assert report["strict_red_first_satisfied"] is True


def test_replay_strict_fires_on_zero_execution_trace(tmp_path) -> None:
    module = _load_script()
    entries = [
        _assistant_tool_call("w1", "edit_file", {"path": "src/main.py"}),
        _tool_result("w1", "edit_file", "edited"),
        _FINAL_TEXT_ENTRY,
    ]
    run_dir = _write_run_dir(tmp_path, entries)

    report = module.replay_run(run_dir)

    # Base gate already fires no_execution_after_final_edit; strict adds its
    # whole-run triggers on the same line.
    assert report["triggers"] == ["no_execution_after_final_edit"]
    assert report["strict_triggers"] == [
        "no_execution_after_final_edit",
        "zero_verification",
    ]
    assert report["strict_verification_command_count"] == 0


def test_replay_strict_suppressed_mid_loop(tmp_path) -> None:
    module = _load_script()
    run_dir = _write_run_dir(tmp_path, list(_RED_EXEC_ENTRIES))

    report = module.replay_run(run_dir)

    assert report["should_challenge"] is False
    assert report["strict_should_challenge"] is False
    assert report["suppressed_reason"] == "no_finalize_attempt_at_transcript_end"


def test_replay_suppresses_transcript_ending_mid_loop(tmp_path) -> None:
    # Same evidence, but the transcript ends on a tool-call assistant message
    # (iteration cap / abort): the live gate would never have evaluated.
    module = _load_script()
    run_dir = _write_run_dir(tmp_path, list(_RED_EXEC_ENTRIES))

    report = module.replay_run(run_dir)

    assert report["finalize_attempt_at_end"] is False
    assert report["should_challenge"] is False
    assert report["suppressed_reason"] == "no_finalize_attempt_at_transcript_end"
    # Raw triggers stay visible for diagnostics.
    assert report["triggers"] == ["red_execution_after_final_edit"]


def test_replay_quiet_without_workspace_diff(tmp_path) -> None:
    module = _load_script()
    run_dir = _write_run_dir(
        tmp_path, [*_RED_EXEC_ENTRIES, _FINAL_TEXT_ENTRY], git_patch=""
    )

    report = module.replay_run(run_dir)

    assert report["has_workspace_diff"] is False
    assert report["should_challenge"] is False
    assert report["triggers"] == []


def test_replay_denied_execution_is_not_red_evidence(tmp_path) -> None:
    # Live/replay parity for the non-verification skip: a policy-denied
    # command after a green verification must not challenge (live
    # defect: sandbox denials counted as trailing reds).
    module = _load_script()
    entries = [
        _assistant_tool_call("w1", "edit_file", {"path": "src/main.py"}),
        _tool_result("w1", "edit_file", "edited"),
        _assistant_tool_call("x1", "exec_command", {"command": "pytest tests/test_main.py"}),
        _tool_result(
            "x1",
            "exec_command",
            "exit_code=0\n4 passed",
            execution_status={
                "version": 1,
                "status": "success",
                "exit_code": 0,
                "timed_out": False,
                "reason": None,
            },
        ),
        _assistant_tool_call(
            "x2",
            "exec_command",
            {"command": "javac -cp /opt/gradle/caches Probe.java"},
        ),
        _tool_result(
            "x2",
            "exec_command",
            '{"status": "policy_denied", "message": "blocked by sandbox policy"}',
            is_error=True,
            execution_status={
                "version": 1,
                "status": "error",
                "exit_code": None,
                "timed_out": False,
                "reason": "denied",
            },
        ),
        _FINAL_TEXT_ENTRY,
    ]
    run_dir = _write_run_dir(tmp_path, entries)

    report = module.replay_run(run_dir)

    assert report["finalize_attempt_at_end"] is True
    assert report["should_challenge"] is False
    assert report["triggers"] == []


def test_replay_approval_retry_keeps_arguments_for_second_result(tmp_path) -> None:
    # The approval flow emits TWO toolResult messages for the same call id:
    # first ``approval_pending`` (no outcome), then the real retried result.
    # The retried result must still see the original arguments — losing them
    # would drop the command and silently erase genuine red evidence.
    module = _load_script()
    entries = [
        _assistant_tool_call("w1", "edit_file", {"path": "src/main.py"}),
        _tool_result("w1", "edit_file", "edited"),
        _assistant_tool_call("x1", "exec_command", {"command": "python repro.py"}),
        _tool_result(
            "x1",
            "exec_command",
            "Approval requested; awaiting decision",
            is_error=True,
            execution_status={
                "version": 1,
                "status": "error",
                "exit_code": None,
                "timed_out": False,
                "reason": "approval_pending",
            },
        ),
        _tool_result(
            "x1",
            "exec_command",
            "exit_code=1\nFAILED: assertion did not hold",
            is_error=True,
            execution_status={
                "version": 1,
                "status": "error",
                "exit_code": 1,
                "timed_out": False,
                "reason": "nonzero_exit",
            },
        ),
        _FINAL_TEXT_ENTRY,
    ]
    run_dir = _write_run_dir(tmp_path, entries)

    report = module.replay_run(run_dir)

    assert report["finalize_attempt_at_end"] is True
    assert report["should_challenge"] is True
    assert report["triggers"] == ["red_execution_after_final_edit"]
