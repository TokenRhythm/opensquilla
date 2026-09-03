"""Logs domain RPC handlers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from opensquilla.application.observability import LogReader, LogTailQuery
from opensquilla.gateway.adapters.observability import GatewayLogReaderPort
from opensquilla.gateway.adapters.observability_contract import (
    register_observability_contract,
)
from opensquilla.gateway.diagnostics import diagnostics_status_payload
from opensquilla.gateway.guest_rpc_policy import is_guest_rpc_method_allowed
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.observability.trace import load_trace_events
from opensquilla.observability.turn_call_log import (
    LOG_DIR_ENV,
    TURN_CALL_LOG_DIR_ENV,
    TURN_CALL_LOG_ENABLED_VALUES,
    TURN_CALL_LOG_ENV,
    is_turn_call_log_enabled,
    resolve_turn_call_log_dir_with_source,
)
from opensquilla.paths import default_opensquilla_home

_d = get_dispatcher()


def _non_empty_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value


def _find_log_file() -> Path | None:
    """Find the structlog output file."""
    env_log_dir = _non_empty_env(LOG_DIR_ENV)
    if env_log_dir:
        candidates = [Path(env_log_dir) / "debug.log"]
    else:
        # Check common locations
        candidates = [
            default_opensquilla_home() / "logs" / "debug.log",
            Path("data") / "debug.log",
            Path("debug.log"),
        ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _env_status(name: str, *, truthy_values: frozenset[str] | None = None) -> dict[str, Any]:
    value = os.environ.get(name)
    stripped = value.strip() if value is not None else ""
    result: dict[str, Any] = {
        "name": name,
        "set": value is not None,
        "empty": value is not None and stripped == "",
    }
    if truthy_values is not None:
        result["truthy"] = stripped.lower() in truthy_values
    return result


def _configured_debug_log_path() -> tuple[Path, str]:
    log_dir = _non_empty_env(LOG_DIR_ENV)
    if log_dir is not None:
        return Path(log_dir) / "debug.log", LOG_DIR_ENV
    return default_opensquilla_home() / "logs" / "debug.log", "default"


def _configured_trace_log_dir() -> tuple[Path, str]:
    log_dir = _non_empty_env(LOG_DIR_ENV)
    if log_dir is not None:
        return Path(log_dir), LOG_DIR_ENV
    return default_opensquilla_home() / "logs", "default"


def _config_value(ctx: RpcContext, name: str, default: Any) -> Any:
    config = getattr(ctx, "config", None)
    if config is None:
        return default
    return getattr(config, name, default)


def read_log_status(ctx: RpcContext) -> dict[str, Any]:
    raw_dir, raw_dir_source = resolve_turn_call_log_dir_with_source()
    configured_debug_log, configured_debug_log_source = _configured_debug_log_path()
    trace_dir, trace_dir_source = _configured_trace_log_dir()
    trace_files = sorted(trace_dir.glob("traces-*.jsonl")) if trace_dir.is_dir() else []
    active_tail_path = _find_log_file()

    diagnostics_status = diagnostics_status_payload(
        getattr(ctx, "diagnostics_state", None),
        getattr(ctx, "config", None),
    )

    return {
        "raw_turn_call_log": {
            "enabled": is_turn_call_log_enabled(getattr(ctx, "diagnostics_state", None)),
            "source": diagnostics_status["raw_turn_call"]["source"],
            "enable_env": _env_status(
                TURN_CALL_LOG_ENV,
                truthy_values=TURN_CALL_LOG_ENABLED_VALUES,
            ),
            "enabled_values": sorted(TURN_CALL_LOG_ENABLED_VALUES),
            "directory": {
                "path": str(raw_dir),
                "source": raw_dir_source,
                "exists": raw_dir.exists(),
            },
        },
        "gateway_file_log": {
            "enabled": bool(_config_value(ctx, "log_file_enabled", True)),
            "level": str(_config_value(ctx, "log_level", "DEBUG")),
            "path": str(configured_debug_log),
            "path_source": configured_debug_log_source,
            "exists": configured_debug_log.exists(),
            "active_tail_path": str(active_tail_path) if active_tail_path is not None else None,
            "active_tail_path_exists": active_tail_path.exists() if active_tail_path else False,
        },
        "trace_log": {
            "directory": {
                "path": str(trace_dir),
                "source": trace_dir_source,
                "exists": trace_dir.exists(),
            },
            "file_count": len(trace_files),
            "latest_path": str(trace_files[-1]) if trace_files else None,
        },
        "diagnostics_enabled": {
            "configured": bool(_config_value(ctx, "diagnostics_enabled", False)),
            "effective": diagnostics_status["enabled"],
            "detail": diagnostics_status["detail"],
            "controls_raw_turn_call": diagnostics_status["raw_turn_call"]["source"] == "runtime",
            "raw_source": diagnostics_status["raw_turn_call"]["source"],
        },
        "diagnostics": diagnostics_status,
        "env": {
            TURN_CALL_LOG_ENV: _env_status(
                TURN_CALL_LOG_ENV,
                truthy_values=TURN_CALL_LOG_ENABLED_VALUES,
            ),
            TURN_CALL_LOG_DIR_ENV: _env_status(TURN_CALL_LOG_DIR_ENV),
            LOG_DIR_ENV: _env_status(LOG_DIR_ENV),
        },
    }


async def _logs_status_contract(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Report log-related runtime switches without mutating filesystem state."""
    return await LogReader(
        GatewayLogReaderPort(
            ctx,
            status_reader=read_log_status,
            tail_reader=read_log_tail,
        )
    ).status()


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
    log_file = _find_log_file()
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
    reader = LogReader(
        GatewayLogReaderPort(
            ctx,
            status_reader=read_log_status,
            tail_reader=read_log_tail,
        )
    )
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
