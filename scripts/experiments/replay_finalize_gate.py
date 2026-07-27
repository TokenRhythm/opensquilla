#!/usr/bin/env python3
"""Offline transcript replay for the finalize-time red-evidence gate.

Feeds recorded run transcripts through the exact same pure tracker the
live agent loop uses (``opensquilla.engine.finalize_evidence_gate``) and
reports whether the gate would have challenged the run's final state. This
lets the gate be validated offline: it should fire on runs whose transcripts
show red self-evidence at finalization while staying quiet on runs that
finished green.

Usage:
    # Single runs, one JSON report line per run dir
    python scripts/experiments/replay_finalize_gate.py RUN_DIR [RUN_DIR ...]

    # Driver mode over a replay-set manifest
    python scripts/experiments/replay_finalize_gate.py --sets replay_sets.json

The replay-set manifest maps set names to ``[label, run_dir]`` pairs; the set
named ``positives`` is expected to fire, all other sets are controls.

Each RUN_DIR must contain ``transcript.jsonl`` (persisted session messages)
and is expected to contain ``git.patch`` (final workspace diff; missing or
blank means no diff, which suppresses the gate exactly as in the live loop).

Known live-vs-replay divergences:

- ``has_workspace_diff`` comes from the harness-cleaned ``git.patch``; the
  live gate uses ``git status --porcelain --untracked-files=all``, which also
  sees untracked scratch files. Replay therefore under-fires on runs whose
  only diff was untracked files.
- The live gate evaluates only when the model emits a zero-tool-call
  assistant message (a finalize attempt). Transcripts that end mid-loop
  (iteration cap, abort) never reach the gate live, so replay reports them
  as ``should_challenge: false`` with ``suppressed_reason:
  no_finalize_attempt_at_transcript_end`` (raw triggers stay in the report
  for diagnostics).

Every report line carries BOTH modes: the base-gate fields plus
``strict_triggers`` / ``strict_should_challenge`` / ``strict_primary_reason``
from a strict tracker fed the identical event stream, so strict-mode
calibration compares trigger sets on the same traces in one pass.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from opensquilla.engine.finalize_evidence_gate import (
    EXECUTION_TOOL_NAMES,
    WRITE_TOOL_NAMES,
    FinalizeEvidenceTracker,
    execution_signals_from_result,
)

_ANCHOR_MARKERS = (
    "failed",
    "failure",
    "error",
    "exception",
    "traceback",
    "assert",
    "expected",
    "actual",
)


def _failure_anchor_lines(text: str, limit: int = 3) -> list[str]:
    """Lightweight stand-in for the live loop's anchor extraction.

    Anchors only decorate challenge messages and dedup keys; they do not
    affect whether a trigger fires, so replay uses a simplified extraction.
    """

    anchors: list[str] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        lowered = line.lower()
        if "no failures" in lowered or "no errors" in lowered:
            continue
        if not any(marker in lowered for marker in _ANCHOR_MARKERS):
            continue
        anchors.append(line[:220])
        if len(anchors) >= limit:
            break
    return anchors


def _string_arg(arguments: dict[str, Any] | None, *names: str) -> str | None:
    if not isinstance(arguments, dict):
        return None
    for name in names:
        value = arguments.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _command_for_tool_call(tool_name: str, arguments: dict[str, Any] | None) -> str | None:
    # Mirrors Agent._execution_command_for_progress.
    if tool_name == "execute_code":
        return _string_arg(arguments, "code")
    return _string_arg(arguments, "command", "cmd")


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return ""


def replay_run(run_dir: Path) -> dict[str, Any]:
    """Replay one run directory; returns a JSON-safe report."""

    transcript_path = run_dir / "transcript.jsonl"
    if not transcript_path.is_file():
        return {"run_dir": str(run_dir), "error": "missing transcript.jsonl"}

    git_patch = run_dir / "git.patch"
    has_workspace_diff = git_patch.is_file() and bool(
        git_patch.read_text(errors="replace").strip()
    )

    # Feed the identical event stream to a base tracker and a strict one so
    # each report line carries both trigger sets for side-by-side calibration.
    tracker = FinalizeEvidenceTracker()
    strict_tracker = FinalizeEvidenceTracker(strict=True)
    pending_calls: dict[str, tuple[str, dict[str, Any] | None]] = {}
    iteration = 0
    parse_errors = 0
    last_assistant_had_tool_calls: bool | None = None

    with transcript_path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "assistant":
                content = message.get("content")
                saw_tool_call = False
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "toolCall":
                            saw_tool_call = True
                            call_id = str(block.get("id") or "")
                            pending_calls[call_id] = (
                                str(block.get("name") or ""),
                                block.get("arguments")
                                if isinstance(block.get("arguments"), dict)
                                else None,
                            )
                if saw_tool_call:
                    iteration += 1
                last_assistant_had_tool_calls = saw_tool_call
                continue
            if role != "toolResult":
                continue
            tool_name = str(message.get("toolName") or "")
            call_id = str(message.get("toolCallId") or "")
            # Look up without popping: an approval retry emits TWO toolResult
            # messages for the same call id (approval_pending, then the real
            # retried outcome); the second must still see the arguments.
            _, arguments = pending_calls.get(call_id, (tool_name, None))
            is_error = bool(message.get("isError"))
            if tool_name in WRITE_TOOL_NAMES:
                write_path = _string_arg(arguments, "path", "file_path")
                for observer in (tracker, strict_tracker):
                    observer.observe_write(
                        write_path,
                        is_error=is_error,
                        iteration=iteration,
                        scratch=(tool_name == "write_scratch"),
                    )
            elif tool_name in EXECUTION_TOOL_NAMES:
                command = _command_for_tool_call(tool_name, arguments)
                if command is None:
                    continue
                content_text = _content_text(message.get("content"))
                execution_status = message.get("executionStatus")
                red, exit_code, timed_out, status_reason = execution_signals_from_result(
                    tool_name=tool_name,
                    content_text=content_text,
                    execution_status=(
                        execution_status if isinstance(execution_status, dict) else None
                    ),
                    is_error=is_error,
                )
                for observer in (tracker, strict_tracker):
                    observer.observe_execution(
                        command,
                        red=red,
                        exit_code=exit_code,
                        timed_out=timed_out,
                        status_reason=status_reason,
                        failure_anchors=_failure_anchor_lines(content_text) if red else (),
                        iteration=iteration,
                    )

    observation = tracker.build_observation(has_workspace_diff=has_workspace_diff)
    strict_observation = strict_tracker.build_observation(
        has_workspace_diff=has_workspace_diff
    )
    # Live parity: the gate only runs on a zero-tool-call assistant message.
    # A transcript that ends mid-loop (iteration cap, abort) never reached it.
    finalize_attempt_at_end = last_assistant_had_tool_calls is False
    report = {
        "run_dir": str(run_dir),
        "instance": run_dir.name,
        "iterations": iteration,
        "parse_errors": parse_errors,
        "has_workspace_diff": has_workspace_diff,
        "has_workspace_diff_source": "git.patch",
        "finalize_attempt_at_end": finalize_attempt_at_end,
        **observation.to_event_details(),
        "strict_triggers": strict_observation.triggers,
        "strict_should_challenge": strict_observation.should_challenge,
        "strict_primary_reason": strict_observation.primary_reason,
        "strict_red_first_satisfied": strict_observation.red_first_satisfied,
        "strict_red_first_candidate_count": strict_observation.red_first_candidate_count,
        "strict_verification_command_count": strict_observation.verification_command_count,
    }
    if not finalize_attempt_at_end:
        report["should_challenge"] = False
        report["strict_should_challenge"] = False
        report["suppressed_reason"] = "no_finalize_attempt_at_transcript_end"
    return report


def _print_report(report: dict[str, Any]) -> None:
    print(json.dumps(report, ensure_ascii=False))


def _run_sets(sets_path: Path) -> int:
    manifest = json.loads(sets_path.read_text())
    summary: dict[str, dict[str, Any]] = {}
    exit_code = 0
    for set_name, entries in manifest.items():
        expected_fire = set_name == "positives"
        fired: list[str] = []
        quiet: list[str] = []
        errored: list[str] = []
        trigger_counts: dict[str, int] = {}
        strict_fired: list[str] = []
        strict_trigger_counts: dict[str, int] = {}
        for entry in entries:
            label, run_dir = entry[0], Path(entry[1])
            report = replay_run(run_dir)
            report["set"] = set_name
            report["label"] = label
            _print_report(report)
            if report.get("error"):
                errored.append(label)
                continue
            if report.get("should_challenge"):
                fired.append(label)
                for trigger in report.get("triggers") or []:
                    trigger_counts[trigger] = trigger_counts.get(trigger, 0) + 1
            else:
                quiet.append(label)
            if report.get("strict_should_challenge"):
                strict_fired.append(label)
                for trigger in report.get("strict_triggers") or []:
                    strict_trigger_counts[trigger] = (
                        strict_trigger_counts.get(trigger, 0) + 1
                    )
        summary[set_name] = {
            "expected_fire": expected_fire,
            "total": len(entries),
            "fired": len(fired),
            "quiet": len(quiet),
            "errored": len(errored),
            "fired_labels": fired if expected_fire else fired,
            "trigger_counts": trigger_counts,
            "strict_fired": len(strict_fired),
            "strict_trigger_counts": strict_trigger_counts,
        }
    print("\n=== finalize-evidence-gate replay summary ===", file=sys.stderr)
    for set_name, stats in summary.items():
        rate = stats["fired"] / stats["total"] if stats["total"] else 0.0
        print(
            f"{set_name}: fired {stats['fired']}/{stats['total']} ({rate:.0%})"
            f" errored={stats['errored']} triggers={stats['trigger_counts']}",
            file=sys.stderr,
        )
        print(
            f"  strict: fired {stats['strict_fired']}/{stats['total']}"
            f" triggers={stats['strict_trigger_counts']}",
            file=sys.stderr,
        )
        if stats["expected_fire"] and stats["fired"] != stats["total"]:
            missing = stats["total"] - stats["fired"] - stats["errored"]
            print(f"  positives not fired: {missing}", file=sys.stderr)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="*", type=Path, help="Run directories to replay")
    parser.add_argument("--sets", type=Path, help="Replay-set manifest (replay_sets.json)")
    args = parser.parse_args(argv)
    if args.sets:
        return _run_sets(args.sets)
    if not args.run_dirs:
        parser.error("provide RUN_DIR arguments or --sets")
    for run_dir in args.run_dirs:
        _print_report(replay_run(run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
