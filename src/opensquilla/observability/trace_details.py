"""Detailed, replay-oriented projection for one agent trace.

The operational trace is intentionally small.  This module joins records
explicitly captured for Agent Trace in the append-only ``turn-calls-*.jsonl`` log into
step rows that retain the model-visible request and the resulting output.  The
raw file remains the source of truth; this projection only bounds what the UI
receives in one response.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from opensquilla.paths import default_opensquilla_home
from opensquilla.safety.secret_redaction import redact_secret_value

_LOG_DIR_ENV = "OPENSQUILLA_LOG_DIR"
_RAW_DIR_ENV = "OPENSQUILLA_TURN_CALL_LOG_DIR"
_MAX_PREVIEW_CHARS = 120_000
_PREPARATION_CHECKPOINTS = frozenset({
    "agent_runtime_budget", "image_input_preflight", "tool_projection_noop",
    "tool_projection_applied",
})


def _log_dir() -> Path:
    raw = os.environ.get(_RAW_DIR_ENV, "").strip()
    if raw:
        return Path(raw)
    raw = os.environ.get(_LOG_DIR_ENV, "").strip()
    return Path(raw) if raw else default_opensquilla_home() / "logs"


def load_turn_call_records(trace_id: str, log_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load raw turn-call records for ``trace_id`` in durable sequence order."""

    if not trace_id.strip():
        return []
    directory = log_dir or _log_dir()
    if not directory.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("turn-calls-*.jsonl")):
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except (TypeError, ValueError):
                        continue
                    if (
                        isinstance(row, dict)
                        and row.get("trace_id") == trace_id
                        and row.get("agent_trace") is True
                    ):
                        records.append(row)
        except OSError:
            continue
    records.sort(key=lambda row: (int(row.get("seq") or 0), str(row.get("ts") or "")))
    return records


def _safe(value: Any) -> Any:
    """Redact secrets before a raw detail leaves the local trace endpoint."""

    try:
        return redact_secret_value(value)
    except Exception:
        return "[redacted]"


def _bounded(value: Any) -> tuple[Any, bool, int]:
    safe = _safe(value)
    encoded = json.dumps(safe, ensure_ascii=False, default=str)
    size = len(encoded)
    if len(encoded) <= _MAX_PREVIEW_CHARS:
        return safe, False, size
    return encoded[:_MAX_PREVIEW_CHARS] + "\n… [内容已截断；原始记录仍保留在本地]", True, size


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


def _phase(kind: str) -> str:
    lowered = kind.lower()
    if kind in {"turn_start", "input_received"}:
        return "intake"
    if kind in {"prompt_report", "context_stage"} or kind in _PREPARATION_CHECKPOINTS:
        return "context"
    if any(token in lowered for token in ("approval", "sandbox", "permission")):
        return "approval_sandbox"
    if any(token in lowered for token in ("compact", "maintenance", "memory", "cache")):
        return "compaction_maintenance"
    if any(token in lowered for token in ("subagent", "child_run", "delegate")):
        return "subagent"
    if any(token in lowered for token in (
        "route", "routing", "router", "retry", "fallback", "ensemble", "decision",
        "generation_reset",
    )):
        return "routing"
    if kind in {"llm_request", "llm_progress", "llm_response", "llm_error"}:
        return "model_execution"
    if kind in {"tool_request", "tool_response"}:
        return "tool_execution"
    if kind in {"turn_end", "turn_error", "turn_cancelled"}:
        return "finalize"
    return "unknown"


def _status(kind: str, payload: dict[str, Any]) -> str:
    explicit = payload.get("status") or payload.get("state")
    if kind in _PREPARATION_CHECKPOINTS:
        return "success"
    if kind == "ensemble_progress":
        # These records describe an observed checkpoint, not an open span.
        # Keep the producer's original activity state in the output payload.
        if (
            payload.get("error") or payload.get("is_error") is True
            or isinstance(explicit, str) and explicit in {"error", "failed", "failure"}
        ):
            return "error"
        return "success"
    if isinstance(explicit, str):
        normalized = explicit.lower()
        if normalized in {"ok", "done", "complete", "completed", "success", "allowed"}:
            return "success"
        if normalized in {"pending", "waiting", "queued"}:
            return "queued"
        if normalized in {"error", "failed", "failure", "denied"}:
            return "error"
        if normalized in {"running", "cancelled", "skipped"}:
            return normalized
    if kind in {"llm_error", "turn_error"} or payload.get("is_error") is True:
        return "error"
    if kind in {"turn_cancelled"}:
        return "cancelled"
    if kind.endswith(("_request", "_start", "_begin")) and kind != "turn_start":
        return "running"
    if kind == "llm_progress":
        return "running"
    if kind in {
        "turn_start", "input_received", "context_stage", "prompt_report",
        "llm_response", "tool_response", "turn_end",
    }:
        return "success"
    if kind.endswith(("_end", "_complete", "_completed", "_resolved", "_decision")):
        return "success"
    if kind in {
        "route_plan", "image_continuation_route", "provider_generation_reset",
        "provider_thinking_fallback", "provider_invalid_response_fallback", "provider_retry",
    }:
        return "success"
    return "unknown"


def _record_row(
    record: dict[str, Any], *, input_value: Any = None, output_value: Any = None
) -> dict[str, Any]:
    kind = str(record.get("kind") or "event")
    payload = _payload(record)
    source_bytes = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    safe_input, input_truncated, input_size = (
        _bounded(input_value) if input_value is not None else (None, False, 0)
    )
    safe_output, output_truncated, output_size = (
        _bounded(output_value) if output_value is not None else (None, False, 0)
    )
    row: dict[str, Any] = {
        "id": f"detail:{record.get('seq', 0)}:{kind}",
        "seq": record.get("seq"),
        "ts": record.get("ts"),
        # Relative monotonic timing is additive and survives wall-clock
        # rounding/adjustments. Older records simply omit these fields.
        "elapsed_ms": record.get("elapsed_ms"),
        "kind": kind,
        "phase": _phase(kind),
        "status": _status(kind, payload),
        "iteration": payload.get("iteration"),
        "attempt": payload.get("attempt"),
        "call_id": payload.get("call_id") or payload.get("tool_use_id"),
        "tool_name": payload.get("name"),
        "provider": payload.get("provider") or record.get("provider"),
        "model": payload.get("model") or record.get("model"),
        "parent_id": payload.get("parent_span_id"),
        "input": safe_input,
        "output": safe_output,
        "input_chars": input_size,
        "output_chars": output_size,
        "input_truncated": input_truncated,
        "output_truncated": output_truncated,
        "payload_ref": f"turn-calls:{record.get('seq', 0)}",
        "payload_digest": hashlib.sha256(source_bytes).hexdigest(),
        "payload_size": len(source_bytes),
    }
    # Preserve the machine-owned routing/link metadata next to the full,
    # redacted payload. New event kinds remain inspectable without changing
    # the persisted call-log or the request/response pairing contract.
    row["attrs"] = _safe({
        key: payload[key]
        for key in (
            "requested_mode", "effective_mode", "decision_id", "route_plan_id",
            "selected_model", "previous_model", "next_model", "from_model", "to_model",
            "parent_span_id", "parent_run_id", "run_id", "agent_id", "reason_code",
        )
        if key in payload
    })
    duration = payload.get("duration_ms")
    if (
        kind not in {"llm_request", "llm_progress", "tool_request"}
        and isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and math.isfinite(duration)
        and duration >= 0
    ):
        row["duration_ms"] = duration
    if kind == "llm_progress":
        tools = payload.get("tool_calls") or []
        row["output_truncated"] = bool(
            output_truncated
            or payload.get("text_truncated")
            or payload.get("reasoning_truncated")
            or any(tool.get("arguments_truncated") for tool in tools if isinstance(tool, dict))
        )
        row["output_chars"] = max(
            output_size,
            int(payload.get("text_chars") or 0)
            + int(payload.get("reasoning_chars") or 0)
            + sum(
                int(tool.get("arguments_chars") or 0) for tool in tools if isinstance(tool, dict)
            ),
        )
    if kind == "context_stage":
        row["stage"] = payload.get("stage")
    if kind in {"llm_response", "llm_error"}:
        row["usage"] = _safe(payload.get("usage"))
    if kind == "tool_response":
        row["is_error"] = payload.get("is_error") is True
    return row


def build_trace_details(
    records: Iterable[dict[str, Any]],
    *,
    trace_id: str,
    after_seq: int | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Join request/result pairs while retaining every durable detail row."""

    source = sorted(
        records, key=lambda record: (int(record.get("seq") or 0), str(record.get("ts") or ""))
    )
    rows: list[dict[str, Any]] = []
    requests: dict[str, dict[str, Any]] = {}
    tool_requests: dict[str, dict[str, Any]] = {}
    approval_requests: dict[str, dict[str, Any]] = {}
    latest_model_outputs: dict[str, dict[str, Any]] = {}
    for record in source:
        if record.get("kind") in {"llm_progress", "llm_response", "llm_error"}:
            payload = _payload(record)
            if payload.get("call_id"):
                latest_model_outputs[str(payload["call_id"])] = record
    for record in source:
        kind = str(record.get("kind") or "event")
        payload = _payload(record)
        if kind == "llm_request":
            call_id = str(payload.get("call_id") or record.get("seq") or "")
            requests[call_id] = record
            row = _record_row(
                record,
                input_value={
                    "messages": payload.get("messages", []),
                    "tools": payload.get("tools", []),
                    "config": payload.get("config", {}),
                },
            )
            row.update(
                id=f"step:{call_id}", input_seq=record.get("seq"),
                order_seq=record.get("seq"), started_ts=record.get("ts"),
                started_elapsed_ms=record.get("elapsed_ms"),
            )
            rows.append(row)
        elif kind in {"llm_progress", "llm_response", "llm_error"}:
            call_id = str(payload.get("call_id") or "")
            if call_id and latest_model_outputs.get(call_id) is not record:
                continue
            request = requests.get(call_id)
            row = _record_row(
                record,
                output_value={
                    key: value
                    for key, value in payload.items()
                    if key not in {"call_id", "iteration", "attempt"}
                },
            )
            row["id"] = f"step:{call_id}" if call_id else row["id"]
            if request is not None:
                request_payload = _payload(request)
                row["id"] = f"step:{call_id}"
                row["input"] = _bounded(
                    {
                        "messages": request_payload.get("messages", []),
                        "tools": request_payload.get("tools", []),
                        "config": request_payload.get("config", {}),
                    }
                )[0]
                row["input_seq"] = request.get("seq")
                row["order_seq"] = request.get("seq")
                # The response record is appended after the provider returns.
                # Preserve the request timestamp so the UI can draw the exact
                # observed interval even when the source timestamp is coarse.
                row["started_ts"] = request.get("ts")
                row["started_elapsed_ms"] = request.get("elapsed_ms")
                if kind != "llm_progress":
                    row["ended_elapsed_ms"] = record.get("elapsed_ms")
                row["input_chars"] = len(json.dumps(row["input"], ensure_ascii=False, default=str))
            rows.append(row)
        elif kind == "tool_request":
            tool_id = str(payload.get("tool_use_id") or record.get("seq") or "")
            tool_requests[tool_id] = record
            row = _record_row(record, input_value={"arguments": payload.get("arguments", {})})
            row.update(
                id=f"tool:{tool_id}", input_seq=record.get("seq"),
                order_seq=record.get("seq"), started_ts=record.get("ts"),
                started_elapsed_ms=record.get("elapsed_ms"),
            )
            rows.append(row)
        elif kind == "tool_response":
            tool_id = str(payload.get("tool_use_id") or "")
            request = tool_requests.get(tool_id)
            row = _record_row(
                record,
                output_value={
                    "result": payload.get("result"),
                    "is_error": payload.get("is_error"),
                    "duration_ms": payload.get("duration_ms"),
                },
            )
            if request is not None:
                request_payload = _payload(request)
                row["id"] = f"tool:{tool_id}"
                row["input"] = _bounded({"arguments": request_payload.get("arguments", {})})[0]
                row["input_seq"] = request.get("seq")
                row["order_seq"] = request.get("seq")
                row["started_ts"] = request.get("ts")
                row["started_elapsed_ms"] = request.get("elapsed_ms")
                row["ended_elapsed_ms"] = record.get("elapsed_ms")
            rows.append(row)
        elif kind in {"approval_wait", "approval_resolved"}:
            approval_id = payload.get("approval_id")
            if not isinstance(approval_id, str) or not approval_id.strip():
                rows.append(_record_row(record, output_value=payload))
                continue
            if kind == "approval_wait":
                approval_requests[approval_id] = record
                row = _record_row(record, input_value=payload)
                row.update(
                    id=f"approval:{approval_id}", input_seq=record.get("seq"),
                    order_seq=record.get("seq"), started_ts=record.get("ts"),
                    started_elapsed_ms=record.get("elapsed_ms"),
                )
            else:
                request = approval_requests.get(approval_id)
                row = _record_row(record, output_value=payload)
                row["id"] = f"approval:{approval_id}"
                if request is not None:
                    row["input"], row["input_truncated"], row["input_chars"] = _bounded(
                        _payload(request)
                    )
                    row["input_seq"] = request.get("seq")
                    row["order_seq"] = request.get("seq")
                    row["started_ts"] = request.get("ts")
                    row["started_elapsed_ms"] = request.get("elapsed_ms")
                    row["ended_elapsed_ms"] = record.get("elapsed_ms")
            rows.append(row)
        elif kind == "turn_start":
            rows.append(_record_row(record, input_value=payload))
        elif kind == "turn_end":
            rows.append(_record_row(record, output_value=payload))
        elif kind in {"context_stage", "prompt_report", "turn_error", "turn_cancelled"}:
            rows.append(
                _record_row(
                    record,
                    input_value=payload if kind in {"context_stage", "prompt_report"} else None,
                    output_value=payload
                    if kind not in {"context_stage", "prompt_report"}
                    else None,
                )
            )
        else:
            # Runtime boundary records are checkpoints unless their producer
            # supplies a measured interval. Do not silently discard new kinds
            # or infer a duration from their turn-relative elapsed position.
            rows.append(_record_row(record, output_value=payload))
    # Replace each live request in place through progress and completion. The
    # transport cursor follows the latest update; order_seq keeps its original
    # request position stable in the displayed timeline.
    rows = sorted({row["id"]: row for row in rows}.values(), key=lambda row: int(row["seq"] or 0))
    visible = [row for row in rows if after_seq is None or int(row.get("seq") or 0) > after_seq]
    bounded_limit = max(1, min(int(limit), 1000))
    has_more = len(visible) > bounded_limit
    return {
        "trace_id": trace_id,
        "available": bool(source),
        "source": "turn_call_log",
        "clock_origin": source[0].get("clock_origin", "logger_start") if source else "logger_start",
        "rows": visible[:bounded_limit],
        "count": min(len(visible), bounded_limit),
        "total": len(rows),
        "has_more": has_more,
        "content_hash": hashlib.sha256(
            json.dumps(source, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest(),
    }
