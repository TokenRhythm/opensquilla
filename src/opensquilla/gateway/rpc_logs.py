"""Logs domain RPC handlers."""

from __future__ import annotations

import asyncio
from typing import Any, cast

from opensquilla.application.observability import (
    LogReader,
    LogReaderPort,
    LogStatusResult,
    LogTailQuery,
    LogTailResult,
)
from opensquilla.gateway.adapters.observability_contract import (
    register_observability_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.log_status_runtime import _configured_trace_log_dir, find_log_file
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.observability.trace import load_trace_events
from opensquilla.observability.trace_details import build_trace_details, load_turn_call_records
from opensquilla.observability.trace_lookup import find_turn_traces
from opensquilla.observability.trace_projection import build_trace_projection
from opensquilla.observability.turn_call_log import (
    is_turn_call_log_enabled,
    resolve_turn_call_log_dir_with_source,
)
from opensquilla.safety.secret_redaction import redact_secret_value

_d = get_dispatcher()


class _GatewayLogReaderRuntime(LogReaderPort):
    """Read log projections directly from configured filesystem/runtime state."""

    def __init__(self, ctx: RpcContext) -> None:
        self._ctx = ctx

    async def status(self) -> LogStatusResult:
        from opensquilla.gateway.log_status_runtime import read_log_status

        return cast(
            LogStatusResult,
            read_log_status(
                config=getattr(self._ctx, "config", None),
                diagnostics_state=getattr(self._ctx, "diagnostics_state", None),
            ),
        )

    async def tail(self, query: LogTailQuery) -> LogTailResult:
        return cast(LogTailResult, read_log_tail(query))


async def _logs_status_contract(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Report log-related runtime switches without mutating filesystem state."""
    return dict(await LogReader(_GatewayLogReaderRuntime(ctx)).status())


@_d.method("logs.trace", scope="operator.read")
async def _handle_logs_trace(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Return safe trace events, or the UI projection, for one trace id.

    ``view=projection`` is additive to the original response contract.  A
    dedicated ``logs.trace_projection`` alias below makes the intent explicit
    for new clients while preserving old clients that consume ``events``.
    """

    p = params or {}
    trace_id = str(p.get("trace_id") or "").strip()
    try:
        limit = max(1, min(int(p.get("limit", 1000)), 5000))
    except (TypeError, ValueError):
        limit = 1000
    if not trace_id:
        if str(p.get("view") or "").lower() in {"projection", "timeline"}:
            return build_trace_projection([], trace_id="")
        return {"trace_id": "", "events": [], "count": 0, "total": 0}

    events = await asyncio.to_thread(load_trace_events, trace_id)
    view = str(p.get("view") or "").lower()
    if view in {"projection", "timeline"}:
        after_seq = _optional_non_negative_int(p.get("after_seq"))
        projection = build_trace_projection(
            events, trace_id=trace_id, after_seq=after_seq, limit=limit
        )
        projection["source"] = "trace_events"
        return projection
    limited = events[-limit:]
    return {
        "trace_id": trace_id,
        "events": [event.to_dict() for event in limited],
        "count": len(limited),
        "total": len(events),
    }


@_d.method("logs.turn_traces", scope="operator.read")
async def _handle_logs_turn_traces(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Resolve all trace attempts for one chat turn, including an active turn."""

    p = params or {}
    session_key = str(p.get("session_key") or "").strip()
    turn_id = str(p.get("turn_id") or "").strip()
    raw_enabled = is_turn_call_log_enabled(getattr(ctx, "diagnostics_state", None))
    traces: list[dict[str, Any]] = []
    if session_key and turn_id:
        trace_dir, _ = _configured_trace_log_dir()
        raw_dir = resolve_turn_call_log_dir_with_source()[0] if raw_enabled else None
        traces = await asyncio.to_thread(
            find_turn_traces,
            session_key,
            turn_id,
            trace_dir=trace_dir,
            raw_dir=raw_dir,
        )
    return {
        "session_key": session_key,
        "turn_id": turn_id,
        "traces": traces,
        "count": len(traces),
        "raw_enabled": raw_enabled,
    }


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _trace_limit(value: Any) -> int:
    try:
        return max(1, min(int(value), 5000))
    except (TypeError, ValueError):
        return 1000


@_d.method("logs.trace_projection", scope="operator.read")
async def _handle_logs_trace_projection(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Return the privacy-preserving timeline projection for one trace id."""

    p = params or {}
    trace_id = str(p.get("trace_id") or "").strip()
    if not trace_id:
        return build_trace_projection([], trace_id="")
    events = await asyncio.to_thread(load_trace_events, trace_id)
    projection = build_trace_projection(
        events,
        trace_id=trace_id,
        after_seq=_optional_non_negative_int(p.get("after_seq")),
        limit=_trace_limit(p.get("limit", 1000)),
    )
    projection["source"] = "trace_events"
    return projection


@_d.method("logs.trace_details", scope="operator.admin")
async def _handle_logs_trace_details(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Return the replay-oriented model/tool input-output rows for one trace.

    Full payloads are available only while the explicit raw diagnostics mode is
    enabled.  The durable ``turn-calls`` file is never rewritten by this
    endpoint; the response is a bounded, redacted projection for the UI.
    """

    p = params or {}
    trace_id = str(p.get("trace_id") or "").strip()
    if not trace_id:
        return build_trace_details([], trace_id="")
    diagnostics_state = getattr(ctx, "diagnostics_state", None)
    if not is_turn_call_log_enabled(diagnostics_state):
        return {
            "trace_id": trace_id,
            "available": False,
            "source": "turn_call_log",
            "reason": "raw_diagnostics_disabled",
            "rows": [],
            "count": 0,
            "total": 0,
        }
    try:
        limit = max(1, min(int(p.get("limit", 500)), 1000))
    except (TypeError, ValueError):
        limit = 500
    records = await asyncio.to_thread(load_turn_call_records, trace_id)
    return build_trace_details(
        records,
        trace_id=trace_id,
        after_seq=_optional_non_negative_int(p.get("after_seq")),
        limit=limit,
    )


@_d.method("logs.trace_payload", scope="operator.admin")
async def _handle_logs_trace_payload(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Fetch one complete redacted turn-call payload by trace and sequence."""

    p = params or {}
    trace_id = str(p.get("trace_id") or "").strip()
    if not trace_id or not is_turn_call_log_enabled(getattr(ctx, "diagnostics_state", None)):
        return {"trace_id": trace_id, "available": False, "payload": None}
    try:
        seq = int(p.get("seq"))
    except (TypeError, ValueError):
        return {"trace_id": trace_id, "available": False, "payload": None}
    records = await asyncio.to_thread(load_turn_call_records, trace_id)
    record = next(
        (item for item in records if int(item.get("seq") or -1) == seq),
        None,
    )
    if record is None:
        return {"trace_id": trace_id, "available": False, "payload": None}
    return {
        "trace_id": trace_id,
        "available": True,
        "payload": redact_secret_value(record),
    }


def read_log_tail(query: LogTailQuery) -> dict[str, Any]:
    """Read one bounded log batch after Application-level normalization."""
    log_file = find_log_file()
    if log_file is None or not log_file.exists():
        return {"lines": [], "cursor": 0, "has_more": False}

    file_size = log_file.stat().st_size
    if query.cursor >= file_size:
        return {"lines": [], "cursor": file_size, "has_more": False}

    with open(log_file, encoding="utf-8", errors="replace") as f:
        f.seek(query.cursor)
        raw_lines = f.readlines()
        new_cursor = f.tell()

    # Apply level filter if specified
    if query.level:
        filtered = [ln for ln in raw_lines if query.level in ln.upper()]
    else:
        filtered = raw_lines

    # Limit output
    has_more = len(filtered) > query.limit
    lines = [ln.rstrip() for ln in filtered[-query.limit :]]

    return {"lines": lines, "cursor": new_cursor, "has_more": has_more}


async def _logs_tail_contract(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Tail log file with cursor-based pagination and level filter."""
    p = params or {}
    reader = LogReader(_GatewayLogReaderRuntime(ctx))
    return dict(
        await reader.tail(
            LogTailQuery(
                cursor=int(p.get("cursor", 0)),
                limit=int(p.get("limit", 100)),
                level=str(p.get("level") or "") or None,
            )
        )
    )


_handle_logs_status = register_observability_contract(
    _d,
    "logs.status",
    _logs_status_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
_handle_logs_tail = register_observability_contract(
    _d,
    "logs.tail",
    _logs_tail_contract,
    internal_error=RpcHandlerError,
    guest_allowed_checker=is_guest_rpc_method_allowed,
)
