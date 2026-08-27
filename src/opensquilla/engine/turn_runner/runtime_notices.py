"""Runtime-authored notices that must agree across live and durable output."""

from __future__ import annotations

from typing import Any

_UNCONFIRMED_BACKGROUND_TOOL_NAMES = frozenset({"background_process", "process"})


def _unconfirmed_background_tool_names(
    turn_segments: list[dict[str, Any]],
) -> list[str]:
    names: list[str] = []
    for segment in turn_segments:
        if not isinstance(segment, dict) or segment.get("type") != "tool_result":
            continue
        name = segment.get("name")
        if not isinstance(name, str) or name not in _UNCONFIRMED_BACKGROUND_TOOL_NAMES:
            continue
        execution_status = segment.get("execution_status")
        if not isinstance(execution_status, dict):
            continue
        if (
            execution_status.get("status") == "unknown"
            and execution_status.get("reason") == "background_running"
        ):
            names.append(name)
    return names


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
