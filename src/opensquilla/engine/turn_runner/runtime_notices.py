"""Runtime-authored notices that must agree across live and durable output."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from opensquilla.execution_status import execution_status_for_process_session

_UNCONFIRMED_BACKGROUND_TOOL_NAMES = frozenset({"exec_command", "background_process", "process"})


def _background_receipts(
    name: str, result: Any, execution_status: Mapping[str, Any],
) -> list[tuple[str | None, bool, Mapping[str, Any] | None]]:
    if not isinstance(result, str):
        return [(None, False, execution_status)]
    if name == "background_process":
        first_line = result.partition("\n")[0]
        if first_line.startswith("session_id="):
            session_id = first_line.removeprefix("session_id=").strip() or None
            return [(session_id, False, execution_status)]
        return [(None, False, execution_status)]
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return [(None, False, execution_status)]
    if not isinstance(payload, dict):
        return [(None, False, execution_status)]
    session = payload.get("session")
    if isinstance(session, dict):
        session_id, exited = _session_receipt(session, payload)
        return [(session_id, exited, execution_status)]
    if name == "process" and payload.get("action") == "wait":
        sessions = payload.get("sessions")
        if isinstance(sessions, list) and sessions:
            return [
                (*_session_receipt(item, {}), execution_status_for_process_session(item))
                for item in sessions if isinstance(item, dict)
            ]
    return [(None, False, execution_status)]


def _session_receipt(
    session: dict[str, Any], payload: dict[str, Any],
) -> tuple[str | None, bool]:
    session_id = payload.get("execution_id") or session.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None, False
    if payload.get("exited") is False:
        return session_id.strip(), False
    exited = (
        payload.get("exited") is True
        or session.get("status") == "done"
        or type(session.get("returncode")) is int
        or (
            # Termination flags are set before cleanup starts. Without an exit
            # code, require finalization evidence before clearing the notice.
            session.get("ended_at") is not None
            and (
                session.get("status") in ("timed_out", "killed")
                or session.get("timed_out") is True
                or session.get("killed") is True
            )
        )
    )
    return session_id.strip(), exited


def _unconfirmed_background_tools(
    turn_segments: list[dict[str, Any]],
) -> list[tuple[str, str | None]]:
    """Return background processes without a terminal receipt in this turn."""

    pending: dict[str, tuple[str, str | None]] = {}
    unidentified: list[tuple[str, str | None]] = []
    for segment in turn_segments:
        if not isinstance(segment, dict) or segment.get("type") != "tool_result":
            continue
        name = segment.get("name")
        if not isinstance(name, str) or name not in _UNCONFIRMED_BACKGROUND_TOOL_NAMES:
            continue
        execution_status = segment.get("execution_status")
        if not isinstance(execution_status, dict):
            continue
        for session_id, exited, receipt_status in _background_receipts(
            name, segment.get("result"), execution_status,
        ):
            if receipt_status is None:
                continue
            if (
                receipt_status.get("status") == "unknown"
                and receipt_status.get("reason") == "background_running"
            ):
                entry = (name, session_id)
                if session_id is not None:
                    pending.setdefault(session_id, entry)
                else:
                    unidentified.append(entry)
            elif session_id is not None and exited and receipt_status.get("status") in {
                "success", "error", "timeout", "cancelled",
            }:
                # Aggregate wait(any/all) completion does not describe each
                # child. Settle only the independently confirmed execution.
                pending.pop(session_id, None)
    return [*unidentified, *pending.values()]


def unconfirmed_action_notice(
    final_text: str,
    turn_segments: list[dict[str, Any]],
) -> str | None:
    """Report outstanding process receipts without judging task completion."""

    tools = _unconfirmed_background_tools(turn_segments)
    if not tools:
        return None
    descriptions = ", ".join(
        f"{name} (execution_id={session_id})" if session_id else name
        for name, session_id in tools
    )
    notice = (
        f"Background process status: {descriptions}. "
        "A running process was reported; no exit result was recorded in this turn."
    )
    return None if notice in final_text else notice


def with_unconfirmed_action_notice(
    final_text: str,
    turn_segments: list[dict[str, Any]],
) -> str:
    """Append the guard once while preserving existing assistant text."""

    notice = unconfirmed_action_notice(final_text, turn_segments)
    if notice is None:
        return final_text
    if final_text.strip():
        return f"{final_text.rstrip()}\n\n{notice}"
    return notice


__all__ = ["unconfirmed_action_notice", "with_unconfirmed_action_notice"]
