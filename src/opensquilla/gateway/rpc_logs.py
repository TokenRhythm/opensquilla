"""Logs domain RPC handlers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opensquilla.application.observability import LogReader, LogReaderPort, LogTailQuery
from opensquilla.gateway.adapters.observability_contract import (
    register_observability_contract,
)
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.log_status_runtime import find_log_file
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.observability.trace import load_trace_events

_d = get_dispatcher()


class _GatewayLogReaderRuntime(LogReaderPort):
    """Read log projections directly from configured filesystem/runtime state."""

    def __init__(self, ctx: RpcContext) -> None:
        self._ctx = ctx

    async def status(self) -> Mapping[str, Any]:
        from opensquilla.gateway.log_status_runtime import read_log_status

        return read_log_status(
            config=getattr(self._ctx, "config", None),
            diagnostics_state=getattr(self._ctx, "diagnostics_state", None),
        )

    async def tail(self, query: LogTailQuery) -> Mapping[str, Any]:
        return read_log_tail(query)


async def _logs_status_contract(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Report log-related runtime switches without mutating filesystem state."""
    return await LogReader(_GatewayLogReaderRuntime(ctx)).status()


@_d.method("logs.trace", scope="operator.read")
async def _handle_logs_trace(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Return safe trace events for one trace id."""

    p = params or {}
    trace_id = str(p.get("trace_id") or "").strip()
    try:
        limit = max(1, min(int(p.get("limit", 1000)), 5000))
    except (TypeError, ValueError):
        limit = 1000
    if not trace_id:
        return {"trace_id": "", "events": [], "count": 0, "total": 0}

    events = load_trace_events(trace_id)
    limited = events[-limit:]
    return {
        "trace_id": trace_id,
        "events": [event.to_dict() for event in limited],
        "count": len(limited),
        "total": len(events),
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
    return await reader.tail(
        LogTailQuery(
            cursor=int(p.get("cursor", 0)),
            limit=int(p.get("limit", 100)),
            level=str(p.get("level") or "") or None,
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
