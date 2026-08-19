"""Durable terminal-turn snapshots shared across fork transcript surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

FORK_TERMINAL_OUTCOME_CONTEXT_KEY = "_opensquilla_fork_terminal_outcome_v1"
FORK_TERMINAL_OUTCOME_VERSION = 1

TERMINAL_AGENT_TASK_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "cancelled",
        "timeout",
        "abandoned",
    }
)
_ACCEPTED_ROUTING_MODES = frozenset({"direct", "router", "ensemble"})
_MISSING = object()


def turn_id_from_context(turn_context: object) -> str | None:
    """Return the causal turn id represented by one transcript context."""

    if not isinstance(turn_context, Mapping):
        return None
    keys = (
        ("promoted_turn_id", "turn_id", "target_turn_id")
        if turn_context.get("disposition") == "promoted"
        else ("turn_id",)
    )
    for key in keys:
        value = turn_context.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if value:
            return value
    return None


def terminal_turn_outcome(status: str, outcome: object) -> dict[str, Any] | None:
    """Normalize a typed or legacy terminal task outcome."""

    if status not in TERMINAL_AGENT_TASK_STATUSES:
        return None
    if isinstance(outcome, Mapping):
        return deepcopy(dict(outcome))
    legacy_kind = {
        "succeeded": "completed",
        "failed": "failed",
        "cancelled": "interrupted",
        "timeout": "interrupted",
        "abandoned": "interrupted",
    }[status]
    return {
        "kind": legacy_kind,
        "reason": status,
    }


def build_fork_terminal_outcome_projection(
    *,
    session_id: str,
    session_key: str,
    turn_id: str,
    task_id: str,
    status: str,
    started_at: int | None,
    finished_at: int | None,
    outcome: Mapping[str, Any],
    accepted_routing_mode: str | None = None,
) -> dict[str, Any]:
    """Build a projection bound to one fork child identity."""

    if accepted_routing_mode is not None and (
        not isinstance(accepted_routing_mode, str)
        or accepted_routing_mode not in _ACCEPTED_ROUTING_MODES
    ):
        raise ValueError(
            "accepted_routing_mode must be direct, router, ensemble, or None"
        )

    projection = {
        "version": FORK_TERMINAL_OUTCOME_VERSION,
        "session_id": session_id,
        "session_key": session_key,
        "turn_id": turn_id,
        "task_id": task_id,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "outcome": deepcopy(dict(outcome)),
    }
    if accepted_routing_mode is not None:
        projection["accepted_routing_mode"] = accepted_routing_mode
    return projection


def extract_fork_terminal_outcome_projection(
    turn_context: object,
    *,
    session_id: str,
    session_key: str,
    turn_id: str,
) -> dict[str, Any] | None:
    """Return a valid child-owned projection, rejecting cross-session reuse."""

    if not isinstance(turn_context, Mapping):
        return None
    raw = turn_context.get(FORK_TERMINAL_OUTCOME_CONTEXT_KEY)
    if not isinstance(raw, Mapping):
        return None
    if raw.get("version") != FORK_TERMINAL_OUTCOME_VERSION:
        return None
    if raw.get("session_id") != session_id or raw.get("session_key") != session_key:
        return None
    if raw.get("turn_id") != turn_id:
        return None

    task_id = raw.get("task_id")
    status = raw.get("status")
    started_at = raw.get("started_at")
    finished_at = raw.get("finished_at")
    accepted_routing_mode = raw.get("accepted_routing_mode", _MISSING)
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    if not isinstance(status, str) or status not in TERMINAL_AGENT_TASK_STATUSES:
        return None
    if started_at is not None and (
        not isinstance(started_at, int) or isinstance(started_at, bool)
    ):
        return None
    if finished_at is not None and (
        not isinstance(finished_at, int) or isinstance(finished_at, bool)
    ):
        return None
    if accepted_routing_mode is not _MISSING and (
        not isinstance(accepted_routing_mode, str)
        or accepted_routing_mode not in _ACCEPTED_ROUTING_MODES
    ):
        return None
    outcome = terminal_turn_outcome(status, raw.get("outcome"))
    if outcome is None:
        return None
    projection: dict[str, Any] = {
        "turn_id": turn_id,
        "task_id": task_id.strip(),
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "outcome": outcome,
    }
    if accepted_routing_mode is not _MISSING:
        projection["accepted_routing_mode"] = accepted_routing_mode
    return projection


def attach_fork_terminal_outcome_projection(
    turn_context: object,
    projection: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Replace any inherited snapshot with the target child's projection."""

    context = dict(turn_context) if isinstance(turn_context, Mapping) else {}
    context.pop(FORK_TERMINAL_OUTCOME_CONTEXT_KEY, None)
    if projection is not None:
        context[FORK_TERMINAL_OUTCOME_CONTEXT_KEY] = deepcopy(dict(projection))
    return context or None


def public_turn_context(turn_context: object) -> dict[str, Any] | None:
    """Remove the internal durable projection from a public turn context."""

    if not isinstance(turn_context, Mapping):
        return None
    context = dict(turn_context)
    context.pop(FORK_TERMINAL_OUTCOME_CONTEXT_KEY, None)
    return context or None


__all__ = [
    "FORK_TERMINAL_OUTCOME_CONTEXT_KEY",
    "TERMINAL_AGENT_TASK_STATUSES",
    "attach_fork_terminal_outcome_projection",
    "build_fork_terminal_outcome_projection",
    "extract_fork_terminal_outcome_projection",
    "public_turn_context",
    "terminal_turn_outcome",
    "turn_id_from_context",
]
