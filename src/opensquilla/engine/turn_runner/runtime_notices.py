"""Runtime-authored notices that must agree across live and durable output."""

from __future__ import annotations

import json
from typing import Any

_UNCONFIRMED_BACKGROUND_TOOL_NAMES = frozenset({"background_process", "process"})


def _background_receipt(name: str, result: Any) -> tuple[str | None, bool]:
    if not isinstance(result, str):
        return None, False
    if name == "background_process":
        first_line = result.partition("\n")[0]
        if first_line.startswith("session_id="):
            return first_line.removeprefix("session_id=").strip() or None, False
        return None, False
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return None, False
    if not isinstance(payload, dict) or not isinstance(payload.get("session"), dict):
        return None, False
    session = payload["session"]
    session_id = session.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None, False
    exited = (
        payload.get("exited") is True
        or session.get("status") == "done"
        or type(session.get("returncode")) is int
    )
    return session_id.strip(), exited


def _unconfirmed_background_tool_names(
    turn_segments: list[dict[str, Any]],
) -> list[str]:
    pending: dict[str, str] = {}
    unidentified: list[str] = []
    for segment in turn_segments:
        if not isinstance(segment, dict) or segment.get("type") != "tool_result":
            continue
        name = segment.get("name")
        if not isinstance(name, str) or name not in _UNCONFIRMED_BACKGROUND_TOOL_NAMES:
            continue
        execution_status = segment.get("execution_status")
        if not isinstance(execution_status, dict):
            continue
        session_id, exited = _background_receipt(name, segment.get("result"))
        if (
            execution_status.get("status") == "unknown"
            and execution_status.get("reason") == "background_running"
        ):
            if session_id is not None:
                pending[session_id] = name
            else:
                unidentified.append(name)
        elif session_id is not None and exited and execution_status.get("status") in {
            "success", "error", "timeout", "cancelled",
        }:
            # A later receipt settles only the process it identifies. Tool
            # failure is a confirmed end too, not an unknown running action.
            pending.pop(session_id, None)
    return [*unidentified, *pending.values()]


def unconfirmed_action_notice(
    final_text: str,
    turn_segments: list[dict[str, Any]],
) -> str | None:
    """Return the deterministic visibility guard for an unfinished action."""

    tool_names = _unconfirmed_background_tool_names(turn_segments)
    if not tool_names or "could not confirm" in final_text.lower():
        return None
    tools = ", ".join(dict.fromkeys(tool_names))
    return (
        f"Note: I started {tools}, but the tool reported that it was still "
        "running, so I could not confirm the action completed."
    )


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
