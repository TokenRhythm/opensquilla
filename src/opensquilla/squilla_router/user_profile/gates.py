"""Trigger gating for the offline user-profile producer.

The post-dream hook only provides the *opportunity*; this AND-gate chain decides
whether a production run actually happens. Every gate must pass. Pure function
(config + state + counts + now -> decision) so the policy is unit-testable and
side-effect free, mirroring ``self_learning.gates``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# Gate reason codes (stable strings for telemetry).
READY = "ready"
DISABLED = "disabled"
NO_SESSIONS = "no_sessions"
AGENT_ACTIVE = "agent_active"
COOLDOWN = "cooldown"
INSUFFICIENT_SESSIONS = "insufficient_sessions"

# Independent kill-switch, parallel to ``self_learning``'s env disable.
ENV_DISABLE = "OPENSQUILLA_USER_PROFILE_DISABLED"
_TRUTHY = {"1", "true", "yes", "on"}
MIN_SESSIONS = 20
IDLE_HOURS = 2.0
COOLDOWN_HOURS = 24.0


def user_profile_disabled_by_env() -> bool:
    return os.environ.get(ENV_DISABLE, "").strip().lower() in _TRUTHY


@dataclass
class GateResult:
    should_run: bool
    reason: str
    stats: dict[str, Any] = field(default_factory=dict)


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def _hours_since(ts: str | None, now: datetime) -> float | None:
    parsed = _parse_ts(ts)
    if parsed is None:
        return None
    return (now - parsed).total_seconds() / 3600.0


def evaluate_profile_gates(
    *,
    state: Any,
    session_count: int,
    latest_activity_ts: str | None,
    now: datetime | None = None,
) -> GateResult:
    """Decide whether to produce a profile. Pure: no IO, no clock unless omitted.

    ``latest_activity_ts`` is the most recent in-window session's timestamp in
    ``%Y-%m-%dT%H:%M:%SZ``; ``state.last_attempt_ts`` drives the cooldown so
    failed provider/batch attempts cannot retry every dream window.
    """

    now = now or datetime.now(UTC)
    min_sessions = MIN_SESSIONS

    def result(should: bool, reason: str) -> GateResult:
        return GateResult(
            should_run=should,
            reason=reason,
            stats={
                "session_count": session_count,
                "min_sessions": min_sessions,
                "latest_activity_ts": latest_activity_ts,
                "last_attempt_ts": getattr(state, "last_attempt_ts", None),
                "last_run_ts": getattr(state, "last_run_ts", None),
                "consecutive_failures": getattr(state, "consecutive_failures", 0),
            },
        )

    if user_profile_disabled_by_env():
        return result(False, DISABLED)

    if session_count <= 0:
        return result(False, NO_SESSIONS)

    # Idle gate: do not contend with a live session. If the most recent in-window
    # session is within idle_hours, the agent is in use -> defer.
    idle_hours = IDLE_HOURS
    since_activity = _hours_since(latest_activity_ts, now)
    if idle_hours > 0 and since_activity is not None and since_activity < idle_hours:
        return result(False, AGENT_ACTIVE)

    # Cooldown gate: at most one production attempt per cooldown window.
    cooldown_hours = COOLDOWN_HOURS
    last_attempt_ts = getattr(state, "last_attempt_ts", None) or getattr(state, "last_run_ts", None)
    since_attempt = _hours_since(last_attempt_ts, now)
    if cooldown_hours > 0 and since_attempt is not None and since_attempt < cooldown_hours:
        return result(False, COOLDOWN)

    # Volume gate: too few sessions to infer a stable profile.
    if session_count < min_sessions:
        return result(False, INSUFFICIENT_SESSIONS)

    return result(True, READY)


__all__ = [
    "AGENT_ACTIVE",
    "COOLDOWN",
    "DISABLED",
    "ENV_DISABLE",
    "COOLDOWN_HOURS",
    "IDLE_HOURS",
    "INSUFFICIENT_SESSIONS",
    "MIN_SESSIONS",
    "NO_SESSIONS",
    "READY",
    "GateResult",
    "evaluate_profile_gates",
    "user_profile_disabled_by_env",
]
