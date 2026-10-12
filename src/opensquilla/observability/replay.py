"""Read-only replay API for decision-log entries.

This module NEVER re-executes tools. It only reads
``decisions-YYYYMMDD.jsonl`` files and renders a human-readable transcript.
"""

from __future__ import annotations

from pathlib import Path

from opensquilla.observability.decision_log import (
    DecisionEntry,
    _default_log_dir,
    load_entries,
)


def load_turn(
    session_key: str,
    turn_id: str,
    log_dir: Path | None = None,
) -> DecisionEntry | None:
    """Find the DecisionEntry for ``(session_key, turn_id)`` or return ``None``.

    Scans all ``decisions-*.jsonl`` files under ``log_dir`` in filename order
    so callers don't need to know which day a turn was logged on.
    """

    log_dir = log_dir or _default_log_dir()
    if not log_dir.is_dir():
        return None
    for jsonl in sorted(log_dir.glob("decisions-*.jsonl")):
        for entry in load_entries(jsonl):
            if entry.session_key == session_key and entry.turn_id == turn_id:
                return entry
    return None


def format_transcript(entry: DecisionEntry) -> str:
    """Render a DecisionEntry as a human-readable transcript string."""

    session_label = entry.session_key
    if entry.session_id:
        session_label = f"{entry.session_key}/{entry.session_id}"
    lines = [
        f"Turn {entry.turn_id} (session {session_label})",
        f"  Model: {entry.model} / Provider: {entry.provider}",
        f"  Tokens: in={entry.tokens_input} out={entry.tokens_output}",
        f"  Latency: {entry.latency_ms} ms",
        f"  Tool choice: {entry.tool_choice}",
        (
            f"  Hashes: prompt={entry.prompt_hash} "
            f"system={entry.system_prompt_hash} "
            f"tools={entry.tool_list_hash}"
        ),
        "  Pipeline steps:",
    ]
    for step in entry.pipeline_steps:
        status = "OK" if step.applied else f"FAIL({step.fallback_reason})"
        lines.append(
            f"    - {step.step_name} [{status}] "
            f"tier={step.routed_tier} "
            f"source={step.routing_source} "
            f"confidence={step.confidence}"
        )
    return "\n".join(lines)
