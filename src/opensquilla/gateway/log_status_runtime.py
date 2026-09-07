"""Shared runtime projection for Gateway and offline log diagnostics."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from opensquilla.gateway.diagnostics import diagnostics_status_payload
from opensquilla.observability.turn_call_log import (
    LOG_DIR_ENV,
    TURN_CALL_LOG_DIR_ENV,
    TURN_CALL_LOG_ENABLED_VALUES,
    TURN_CALL_LOG_ENV,
    is_turn_call_log_enabled,
    resolve_turn_call_log_dir_with_source,
)
from opensquilla.paths import default_opensquilla_home


def _non_empty_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value


def find_log_file() -> Path | None:
    """Find the active structlog output file without creating directories."""
    env_log_dir = _non_empty_env(LOG_DIR_ENV)
    if env_log_dir:
        candidates = [Path(env_log_dir) / "debug.log"]
    else:
        candidates = [
            default_opensquilla_home() / "logs" / "debug.log",
            Path("data") / "debug.log",
            Path("debug.log"),
        ]
    return next((path for path in candidates if path.exists()), None)


def _env_status(
    name: str,
    *,
    truthy_values: frozenset[str] | None = None,
) -> dict[str, Any]:
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


def _config_value(config: object | None, name: str, default: Any) -> Any:
    if config is None:
        return default
    return getattr(config, name, default)


def read_log_status(
    *,
    config: Any | None,
    diagnostics_state: Any | None,
) -> dict[str, Any]:
    """Build the stable logs-status projection from narrow runtime inputs."""
    raw_dir, raw_dir_source = resolve_turn_call_log_dir_with_source()
    configured_debug_log, configured_debug_log_source = _configured_debug_log_path()
    trace_dir, trace_dir_source = _configured_trace_log_dir()
    trace_files = sorted(trace_dir.glob("traces-*.jsonl")) if trace_dir.is_dir() else []
    active_tail_path = find_log_file()
    diagnostics_status = diagnostics_status_payload(diagnostics_state, config)

    return {
        "raw_turn_call_log": {
            "enabled": is_turn_call_log_enabled(diagnostics_state),
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
            "enabled": bool(_config_value(config, "log_file_enabled", True)),
            "level": str(_config_value(config, "log_level", "DEBUG")),
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
            "configured": bool(_config_value(config, "diagnostics_enabled", False)),
            "effective": diagnostics_status["enabled"],
            "detail": diagnostics_status["detail"],
            "controls_raw_turn_call": diagnostics_status["raw_turn_call"]["source"]
            == "runtime",
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


__all__ = ["find_log_file", "read_log_status"]
