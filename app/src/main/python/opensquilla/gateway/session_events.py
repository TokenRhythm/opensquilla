"""Shared gateway session event payload builders."""

from __future__ import annotations

from typing import Any

_SESSIONS_CHANGED_SCHEMA_VERSION = 1


def build_sessions_changed_payload(
    session_key: str,
    reason: str,
    **state: Any,
) -> dict[str, Any]:
    """Build the WS payload for a ``sessions.changed`` broadcast."""

    run_status = state.pop("run_status", None)
    if run_status is None and reason in {
        "turn_complete",
        "cron_result",
        "channel_event",
    }:
        run_status = "idle"

    payload: dict[str, Any] = {
        "schema_version": _SESSIONS_CHANGED_SCHEMA_VERSION,
        "key": session_key,
        "reason": reason,
    }
    if run_status is not None:
        payload["run_status"] = run_status
    payload.update({key: value for key, value in state.items() if value is not None})
    return payload
