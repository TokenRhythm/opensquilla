"""Bounded, non-sensitive terminal activity snapshots for retryable barriers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

USAGE_ACCOUNTING_BARRIER_CODES = frozenset(
    {
        "usage_accounting_busy",
        "usage_accounting_unavailable",
    }
)

_ACTIVITY_PHASES = {
    "router": frozenset({"decided"}),
    "state": frozenset({"thinking", "streaming", "tool_calling"}),
    "provider": frozenset(
        {"requesting", "reasoning", "retry_wait", "retrying", "fallback"}
    ),
}
_MAX_ACTIVITY_PHASES = 32
_MAX_RETRY_AFTER_MS = 900_000


def is_usage_accounting_barrier(code: object) -> bool:
    return isinstance(code, str) and code.strip().lower() in USAGE_ACCOUNTING_BARRIER_CODES


def safe_retry_after_ms(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed <= 0:
        return None
    return min(parsed, _MAX_RETRY_AFTER_MS)


def safe_primary_user_message_id(value: object) -> str | None:
    """Return one non-empty authoritative transcript message id."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def usage_barrier_replay_proof(
    *,
    usage_call_index: object,
    no_prior_provider_dispatch: object,
    replay_safe: object,
) -> dict[str, Any]:
    """Normalize the closed proof required before offering whole-turn replay."""

    call_index = (
        usage_call_index
        if isinstance(usage_call_index, int)
        and not isinstance(usage_call_index, bool)
        and usage_call_index > 0
        else None
    )
    no_prior = call_index == 1 and no_prior_provider_dispatch is True
    return {
        **({"usage_call_index": call_index} if call_index is not None else {}),
        "no_prior_provider_dispatch": no_prior,
        "replay_safe": no_prior and replay_safe is True,
    }


def append_activity_phase(
    phases: list[dict[str, Any]],
    *,
    event_kind: object,
    payload: Mapping[str, Any],
    observed_at_ms: int,
) -> None:
    """Append one allowlisted phase without copying arbitrary event fields."""

    kind = str(event_kind or "").strip().lower()
    phase: str
    if kind == "router_decision":
        snapshot_kind = "router"
        phase = "decided"
    elif kind == "state_change":
        snapshot_kind = "state"
        phase = str(payload.get("to_state") or "").strip().lower()
    elif kind == "provider_activity":
        snapshot_kind = "provider"
        phase = str(payload.get("phase") or "").strip().lower()
    else:
        return
    if phase not in _ACTIVITY_PHASES[snapshot_kind]:
        return

    raw_at = payload.get("started_at")
    try:
        event_at = int(raw_at) if raw_at is not None and not isinstance(raw_at, bool) else 0
    except (TypeError, ValueError, OverflowError):
        event_at = 0
    at = event_at if event_at > 0 else max(0, int(observed_at_ms))
    entry = {"kind": snapshot_kind, "phase": phase, "at": at}
    if phases and phases[-1] == entry:
        return
    if len(phases) >= _MAX_ACTIVITY_PHASES:
        phases.pop(0)
    phases.append(entry)


def terminal_activity_snapshot(
    value: object,
    *,
    task_id: str,
    turn_id: str,
) -> dict[str, Any] | None:
    """Return an identity-bound snapshot containing only closed phase enums."""

    raw_phases: object
    if isinstance(value, Mapping):
        raw_phases = value.get("phases")
    else:
        raw_phases = value
    if not isinstance(raw_phases, list):
        return None

    phases: list[dict[str, Any]] = []
    for raw in raw_phases[:_MAX_ACTIVITY_PHASES]:
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or "").strip().lower()
        phase = str(raw.get("phase") or "").strip().lower()
        if phase not in _ACTIVITY_PHASES.get(kind, frozenset()):
            continue
        raw_at = raw.get("at")
        if isinstance(raw_at, bool):
            continue
        try:
            at = int(raw_at)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            continue
        if at <= 0 or at > 10_000_000_000_000:
            continue
        entry = {"kind": kind, "phase": phase, "at": at}
        if not phases or phases[-1] != entry:
            phases.append(entry)
    if not phases:
        return None
    return {
        "version": 1,
        "task_id": task_id,
        "turn_id": turn_id,
        "phases": phases,
    }


__all__ = [
    "USAGE_ACCOUNTING_BARRIER_CODES",
    "append_activity_phase",
    "is_usage_accounting_barrier",
    "safe_primary_user_message_id",
    "safe_retry_after_ms",
    "terminal_activity_snapshot",
    "usage_barrier_replay_proof",
]
