"""Resolve a chat turn to its persisted trace identities without exposing content."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_TERMINAL_STATUS = {
    "turn_end": "success",
    "turn_error": "error",
    "turn_cancelled": "cancelled",
}


def find_turn_traces(
    session_key: str,
    turn_id: str,
    *,
    trace_dir: Path,
    raw_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """List every trace attempt for an exact session and turn identity.

    The safe stream is available as soon as turn setup begins. Only raw records
    explicitly marked for Agent Trace supplement it when the caller passes an
    authorized raw directory. Partial lines from an active writer are retried
    on the next read.
    """

    if not session_key or not turn_id:
        return []
    traces: dict[str, dict[str, Any]] = {}
    sources = [(trace_dir, "traces-*.jsonl", False)]
    if raw_dir is not None:
        sources.append((raw_dir, "turn-calls-*.jsonl", True))
    for directory, pattern, raw in sources:
        for path in sorted(directory.glob(pattern)):
            try:
                with path.open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        if (
                            record.get("session_key") != session_key
                            or record.get("turn_id") != turn_id
                            or (not raw and record.get("privacy") == "raw")
                            or (raw and record.get("agent_trace") is not True)
                        ):
                            continue
                        trace_id = record.get("trace_id")
                        if not isinstance(trace_id, str) or not trace_id.strip():
                            continue
                        timestamp = record.get("ts")
                        timestamp = timestamp if isinstance(timestamp, str) else None
                        trace = traces.setdefault(
                            trace_id,
                            {
                                "trace_id": trace_id,
                                "started_at": timestamp,
                                "status": "running",
                                "complete": False,
                                "raw_available": False,
                            },
                        )
                        if timestamp and (
                            trace["started_at"] is None or timestamp < trace["started_at"]
                        ):
                            trace["started_at"] = timestamp
                        if raw:
                            trace["raw_available"] = True
                        status = _TERMINAL_STATUS.get(str(record.get("kind") or ""))
                        if status is not None:
                            trace["status"] = status
                            trace["complete"] = True
            except OSError:
                continue
    return sorted(traces.values(), key=lambda trace: trace["started_at"] or "")
