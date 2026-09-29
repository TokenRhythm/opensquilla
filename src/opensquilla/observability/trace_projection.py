"""Safe, UI-oriented projection of the append-only trace event stream.

The JSONL trace remains the source of truth.  This module deliberately keeps
the projection free of raw prompt/tool payloads so it can be served to the
web UI without turning the timeline endpoint into a transcript endpoint.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from opensquilla.observability.trace import TraceEvent

_TERMINAL_KINDS = frozenset({"turn_end", "turn_error", "turn_cancelled"})
_ERROR_KINDS = frozenset({"turn_error", "error", "failed", "failure"})


def _value(event: TraceEvent, *names: str) -> Any:
    """Read a scalar from event attrs/payload using the additive conventions."""

    for name in names:
        for source in (event.attrs, event.payload):
            value = source.get(name)
            if value is not None:
                return value
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _phase_for(event: TraceEvent) -> str:
    if event.phase:
        return event.phase
    explicit = _text(_value(event, "phase", "stage"))
    if explicit:
        return explicit
    kind = event.kind.lower()
    if kind in {"turn_start", "input_received", "session_start"}:
        return "intake"
    if kind in _TERMINAL_KINDS or kind in {"finalize", "response_ready"}:
        return "finalize"
    if any(token in kind for token in ("approval", "sandbox", "permission")):
        return "approval_sandbox"
    if any(token in kind for token in ("compact", "memory", "maintenance", "flush")):
        return "compaction_maintenance"
    if any(token in kind for token in ("subagent", "child_run", "delegate")):
        return "subagent"
    if any(token in kind for token in (
        "route", "routing", "router", "retry", "fallback", "ensemble", "plan", "decision",
        "generation_reset",
    )):
        return "routing"
    if any(token in kind for token in ("context", "prompt", "history")):
        return "context"
    if any(token in kind for token in ("tool", "function", "command")):
        return "tool_execution"
    if any(token in kind for token in ("llm", "model", "provider", "generation", "response")):
        return "model_execution"
    return "unknown"


def _status_for(event: TraceEvent) -> str:
    explicit = _text(_value(event, "status", "state"))
    if explicit:
        normalized = explicit.lower()
        if normalized in {"ok", "done", "complete", "completed", "success"}:
            return "success"
        if normalized in {"pending", "waiting"}:
            return "queued"
        return normalized
    kind = event.kind.lower()
    if kind in _ERROR_KINDS or kind.endswith("_error"):
        return "error"
    if "cancel" in kind:
        return "cancelled"
    if kind in {"turn_start", "input_received", "context_stage", "prompt_report"}:
        return "success"
    if kind.endswith("_start") or kind.endswith("_begin"):
        return "running"
    if kind.endswith(("_end", "_complete", "_completed", "_resolved", "_decision")):
        return "success"
    if kind in {
        "route_plan", "image_continuation_route", "provider_generation_reset",
        "provider_thinking_fallback", "provider_invalid_response_fallback", "provider_retry",
    }:
        return "success"
    return "unknown"


def _safe_summary(event: TraceEvent) -> str:
    for name in ("summary", "title", "operation_key", "tool_name", "model", "provider"):
        value = _text(_value(event, name))
        if value:
            return value
    return event.kind


def _span_row(event: TraceEvent, index: int, trace_id: str) -> dict[str, Any]:
    event_id = f"{trace_id}:{event.seq}" if event.seq is not None else f"{trace_id}:event-{index}"
    span_id = event.span_id or _text(_value(event, "span_id")) or event_id
    parent_span_id = event.parent_span_id or _text(_value(event, "parent_span_id"))
    # ``duration_ms`` is the width of this event's execution interval.  A
    # producer may also attach ``elapsed_ms`` as a turn-relative checkpoint
    # position; that value is not a duration and must never be used as one.
    # Duration fields in the trace contract are always milliseconds.  Keep
    # this separate from ``elapsed_ms`` (the event's turn-relative position).
    duration = _value(event, "duration_ms", "durationMs")
    elapsed = _value(event, "elapsed_ms", "elapsedMs")
    try:
        duration_ms = max(0.0, float(duration)) if duration is not None else None
    except (TypeError, ValueError):
        duration_ms = None

    attrs = event.attrs
    return {
        "event_id": event_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "seq": event.seq,
        "kind": event.kind,
        "phase": _phase_for(event),
        "status": _status_for(event),
        "ts": event.ts,
        "duration_ms": duration_ms,
        "elapsed_ms": elapsed,
        "summary": _safe_summary(event),
        "run_id": event.context.run_id,
        "parent_run_id": event.context.parent_run_id,
        "task_id": event.context.task_id,
        "agent_id": event.context.agent_id,
        "role": _text(_value(event, "role", "call_role")),
        "provider": _text(_value(event, "provider", "provider_id")),
        "model": _text(_value(event, "model", "model_id")),
        "tool_name": _text(_value(event, "tool_name", "tool")),
        "requested_mode": _text(_value(event, "requested_mode", "routing_mode")),
        "effective_mode": _text(_value(event, "effective_mode", "executed_kind")),
        "logical_call_id": _text(_value(event, "logical_call_id", "call_id")),
        "physical_attempt_id": _text(_value(event, "physical_attempt_id", "attempt_id")),
        "attempt_index": _value(event, "attempt_index"),
        "payload_keys": sorted(str(key) for key in event.payload),
        "payload_ref": _text(_value(event, "payload_ref", "artifact_ref", "external_ref")),
        "attrs": {
            key: attrs[key]
            for key in (
                "route_plan_id",
                "decision_id",
                "fallback_hop",
                "fallback_hops",
                "cache_hit",
                "error_code",
            )
            if key in attrs
        },
    }


def build_trace_projection(
    events: Iterable[TraceEvent],
    *,
    trace_id: str | None = None,
    after_seq: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build a stable, privacy-preserving projection for the Trace Inspector.

    ``after_seq`` is intended for live-tail consumers.  Summary fields are
    computed from the complete event collection supplied by the caller; only
    rows after the cursor are omitted from ``spans``.  ``limit`` limits the
    returned rows while retaining complete summary metadata.
    """

    ordered = list(events)
    ordered.sort(key=lambda event: (event.seq is None, event.seq or 0, event.ts))
    resolved_trace_id = trace_id or (ordered[0].trace_id if ordered else "")
    all_rows = [_span_row(event, index, resolved_trace_id) for index, event in enumerate(ordered)]
    visible_rows = [
        row for row in all_rows if after_seq is None or row["seq"] is None or row["seq"] > after_seq
    ]
    has_more = False
    if limit is not None:
        safe_limit = max(1, min(int(limit), 5000))
        has_more = len(visible_rows) > safe_limit
        visible_rows = visible_rows[:safe_limit]

    phase_counts = Counter(row["phase"] for row in all_rows)
    phases: list[dict[str, Any]] = []
    for phase in sorted(phase_counts, key=lambda name: min(
        (index for index, row in enumerate(all_rows) if row["phase"] == name),
        default=0,
    )):
        phase_rows = [row for row in all_rows if row["phase"] == phase]
        phases.append(
            {
                "phase": phase,
                "count": len(phase_rows),
                "first_ts": phase_rows[0]["ts"],
                "last_ts": phase_rows[-1]["ts"],
                "error_count": sum(row["status"] == "error" for row in phase_rows),
            }
        )

    terminal = next((row for row in reversed(all_rows) if row["kind"] in _TERMINAL_KINDS), None)
    if terminal is not None:
        status = terminal["status"]
    elif any(row["status"] == "error" for row in all_rows):
        status = "error"
    elif all_rows:
        status = "running"
    else:
        status = "unknown"

    first = all_rows[0] if all_rows else {}
    latest_seq = max((row["seq"] for row in all_rows if row["seq"] is not None), default=None)
    requested_mode = next(
        (row["requested_mode"] for row in all_rows if row["requested_mode"]), None
    )
    effective_mode = next(
        (row["effective_mode"] for row in all_rows if row["effective_mode"]), None
    )
    return {
        "trace_id": resolved_trace_id,
        "run_id": first.get("run_id"),
        "turn_id": ordered[0].context.turn_id if ordered else None,
        "status": status,
        "complete": terminal is not None,
        "requested_mode": requested_mode,
        "effective_mode": effective_mode,
        "current_seq": latest_seq,
        "phases": phases,
        "spans": visible_rows,
        "count": len(visible_rows),
        "total": len(all_rows),
        "has_more": has_more,
    }
