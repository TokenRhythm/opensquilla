"""Canonical execution-status sidecar for tool results."""

from __future__ import annotations

import json
import re
from typing import Any, Literal, TypedDict

from opensquilla.search_tool_outcome import parse_web_tool_outcome

ExecutionStatusValue = Literal["success", "error", "timeout", "cancelled", "unknown"]
ExecutionStatusSource = Literal["tool_runtime", "adapter", "replay", "legacy", "unknown"]
ExecutionStatusPreservation = Literal[
    "normal",
    "diagnostic",
    "retain_summary",
    "retain_full",
    "ephemeral",
]


class ExecutionStatus(TypedDict):
    version: int
    status: ExecutionStatusValue
    exit_code: int | None
    timed_out: bool
    truncated: bool
    reason: str | None
    source: ExecutionStatusSource
    preservation_class: ExecutionStatusPreservation


_VALID_STATUSES = {"success", "error", "timeout", "cancelled", "unknown"}
_VALID_SOURCES = {"tool_runtime", "adapter", "replay", "legacy", "unknown"}
_VALID_PRESERVATION = {"normal", "diagnostic", "retain_summary", "retain_full", "ephemeral"}
_ERROR_STATUSES = {"error", "timeout", "cancelled"}
_EXEC_EXIT_RE = re.compile(r"^exit_code=(-?\d+)\n", re.DOTALL)
_MASKED_PIPELINE_FAILURE_MARKER = "[shell_warning:masked_pipeline_failure]"


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _as_exit_code(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _as_str_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    return None


def normalize_execution_status(value: Any) -> ExecutionStatus:
    """Return a canonical v1 execution status dict."""

    if not isinstance(value, dict):
        return {
            "version": 1,
            "status": "unknown",
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "reason": None,
            "source": "unknown",
            "preservation_class": "normal",
        }

    status = value.get("status")
    reason = _as_str_or_none(value.get("reason"))
    if status not in _VALID_STATUSES:
        status = "unknown"
        reason = "invalid_status"

    source = value.get("source")
    if source not in _VALID_SOURCES:
        source = "unknown"

    preservation_class = value.get("preservation_class")
    if preservation_class not in _VALID_PRESERVATION:
        preservation_class = "normal"

    return {
        "version": 1,
        "status": status,  # type: ignore[typeddict-item]
        "exit_code": _as_exit_code(value.get("exit_code")),
        "timed_out": _as_bool(value.get("timed_out")),
        "truncated": _as_bool(value.get("truncated")),
        "reason": reason,
        "source": source,  # type: ignore[typeddict-item]
        "preservation_class": preservation_class,  # type: ignore[typeddict-item]
    }


def normalize_legacy_execution_status(*, is_error: bool) -> ExecutionStatus:
    return {
        "version": 1,
        "status": "error" if is_error else "unknown",
        "exit_code": None,
        "timed_out": False,
        "truncated": False,
        "reason": "legacy_missing_status",
        "source": "legacy",
        "preservation_class": "diagnostic" if is_error else "normal",
    }


def runtime_execution_status(
    status: ExecutionStatusValue,
    *,
    reason: str | None,
    timed_out: bool = False,
) -> ExecutionStatus:
    return {
        "version": 1,
        "status": status,
        "exit_code": None,
        "timed_out": timed_out,
        "truncated": False,
        "reason": reason,
        "source": "tool_runtime",
        "preservation_class": "diagnostic",
    }


def derive_is_error(status: Any) -> bool:
    if not isinstance(status, dict):
        return False
    return status.get("status") in _ERROR_STATUSES


def compact_provider_status(status: Any) -> dict[str, Any]:
    normalized = normalize_execution_status(status)
    return {
        "version": normalized["version"],
        "status": normalized["status"],
        "exit_code": normalized["exit_code"],
        "timed_out": normalized["timed_out"],
        "truncated": normalized["truncated"],
        "reason": normalized["reason"],
    }


def execution_status_for_tool_result(tool_name: str, content: Any) -> ExecutionStatus | None:
    """Map trusted built-in tool payloads to canonical execution status."""

    web_outcome = parse_web_tool_outcome(tool_name, content)
    if web_outcome is not None:
        timed_out = web_outcome.error_kind == "timeout"
        return {
            "version": 1,
            "status": "timeout" if timed_out else "error",
            "exit_code": None,
            "timed_out": timed_out,
            "truncated": False,
            "reason": f"search_{web_outcome.error_kind}",
            "source": "adapter",
            "preservation_class": "diagnostic",
        }

    if not isinstance(content, str):
        return None

    if tool_name == "exec_command":
        if content.startswith("[timeout after "):
            return {
                "version": 1,
                "status": "timeout",
                "exit_code": None,
                "timed_out": True,
                "truncated": False,
                "reason": "tool_timeout",
                "source": "adapter",
                "preservation_class": "diagnostic",
            }
        match = _EXEC_EXIT_RE.match(content)
        if match is None:
            return None
        exit_code = int(match.group(1))
        masked_failure = exit_code == 0 and _MASKED_PIPELINE_FAILURE_MARKER in content
        failed = exit_code != 0 or masked_failure
        return {
            "version": 1,
            "status": "error" if failed else "success",
            "exit_code": exit_code,
            "timed_out": False,
            "truncated": False,
            "reason": (
                "masked_pipeline_failure"
                if masked_failure
                else "nonzero_exit"
                if failed
                else None
            ),
            "source": "adapter",
            "preservation_class": "diagnostic" if failed else "normal",
        }

    if tool_name == "execute_code":
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        code_exit: int | None = _as_exit_code(payload.get("exit_code"))
        timed_out = _as_bool(payload.get("timed_out"))
        if code_exit is None and not timed_out:
            return None
        if timed_out:
            status: ExecutionStatusValue = "timeout"
            reason = "tool_timeout"
            preservation_class: ExecutionStatusPreservation = "diagnostic"
        elif code_exit is not None and code_exit != 0:
            status = "error"
            reason = "nonzero_exit"
            preservation_class = "diagnostic"
        else:
            status = "success"
            reason = None
            preservation_class = "normal"
        return {
            "version": 1,
            "status": status,
            "exit_code": code_exit,
            "timed_out": timed_out,
            "truncated": False,
            "reason": reason,
            "source": "adapter",
            "preservation_class": preservation_class,
        }

    if tool_name == "background_process":
        if "\nstatus: running" not in content:
            return None
        return {
            "version": 1,
            "status": "unknown",
            "exit_code": None,
            "timed_out": False,
            "truncated": False,
            "reason": "background_running",
            "source": "adapter",
            "preservation_class": "ephemeral",
        }

    if tool_name == "process":
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        session = payload.get("session")
        if not isinstance(session, dict):
            return None
        session_status = session.get("status")
        returncode = _as_exit_code(session.get("returncode"))
        timed_out = _as_bool(session.get("timed_out"))
        killed = _as_bool(session.get("killed"))
        if session_status == "running":
            return {
                "version": 1,
                "status": "unknown",
                "exit_code": None,
                "timed_out": False,
                "truncated": False,
                "reason": "background_running",
                "source": "adapter",
                "preservation_class": "ephemeral",
            }
        if timed_out or session_status == "timed_out":
            return {
                "version": 1,
                "status": "timeout",
                "exit_code": returncode,
                "timed_out": True,
                "truncated": False,
                "reason": "tool_timeout",
                "source": "adapter",
                "preservation_class": "diagnostic",
            }
        if killed or session_status == "killed":
            return {
                "version": 1,
                "status": "cancelled",
                "exit_code": returncode,
                "timed_out": False,
                "truncated": False,
                "reason": "killed",
                "source": "adapter",
                "preservation_class": "diagnostic",
            }
        if returncode is None:
            return None
        failed = returncode != 0
        return {
            "version": 1,
            "status": "error" if failed else "success",
            "exit_code": returncode,
            "timed_out": False,
            "truncated": False,
            "reason": "nonzero_exit" if failed else None,
            "source": "adapter",
            "preservation_class": "diagnostic" if failed else "normal",
        }

    return None


def mark_execution_status_truncated(status: Any) -> ExecutionStatus:
    normalized = normalize_execution_status(status)
    normalized["truncated"] = True
    if normalized["preservation_class"] not in {"retain_full", "ephemeral"}:
        normalized["preservation_class"] = "retain_summary"
    return normalized
