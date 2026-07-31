"""The gate AND-chain: every reason code, in the order the chain checks them.

The producer runs only when disabled? no -> sessions? yes -> idle long enough ->
cooldown elapsed -> enough sessions. Each test isolates one failing gate with all
earlier ones passing, so a reordering or an inverted comparison is caught.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from opensquilla.squilla_router.user_profile.gates import (
    AGENT_ACTIVE,
    COOLDOWN,
    DISABLED,
    INSUFFICIENT_SESSIONS,
    NO_SESSIONS,
    READY,
    evaluate_profile_gates,
    user_profile_disabled_by_env,
)

_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)


@dataclass
class _State:
    last_attempt_ts: str | None = None
    last_run_ts: str | None = None
    consecutive_failures: int = 0


def _ts(hours_ago: float) -> str:
    from datetime import timedelta

    return (_NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _eval(*, state: _State, sessions: int, latest_hours_ago: float | None):
    latest = _ts(latest_hours_ago) if latest_hours_ago is not None else None
    return evaluate_profile_gates(
        state=state,
        session_count=sessions,
        latest_activity_ts=latest,
        now=_NOW,
    )


def test_all_gates_pass_is_ready() -> None:
    result = _eval(state=_State(), sessions=25, latest_hours_ago=5)
    assert result.should_run is True
    assert result.reason == READY


def test_no_sessions_in_window() -> None:
    result = _eval(state=_State(), sessions=0, latest_hours_ago=None)
    assert result.reason == NO_SESSIONS


def test_a_live_agent_defers() -> None:
    # Most recent session is 1h old, inside the 2h idle window.
    result = _eval(state=_State(), sessions=25, latest_hours_ago=1)
    assert result.reason == AGENT_ACTIVE


def test_cooldown_blocks_a_second_run_too_soon() -> None:
    state = _State(last_attempt_ts=_ts(3))  # attempted 3h ago, cooldown is 24h
    result = _eval(state=state, sessions=25, latest_hours_ago=5)
    assert result.reason == COOLDOWN


def test_cooldown_falls_back_to_legacy_success_timestamp() -> None:
    state = _State(last_run_ts=_ts(3))
    result = _eval(state=state, sessions=25, latest_hours_ago=5)
    assert result.reason == COOLDOWN


def test_too_few_sessions_is_insufficient() -> None:
    result = _eval(state=_State(), sessions=5, latest_hours_ago=5)
    assert result.reason == INSUFFICIENT_SESSIONS


def test_idle_gate_precedes_cooldown_and_volume() -> None:
    # Live agent AND stale cooldown AND too few sessions: idle reported first.
    state = _State(last_attempt_ts=_ts(1))
    result = _eval(state=state, sessions=1, latest_hours_ago=0.5)
    assert result.reason == AGENT_ACTIVE


def test_env_kill_switch_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_USER_PROFILE_DISABLED", "1")
    assert user_profile_disabled_by_env() is True
    result = _eval(state=_State(), sessions=25, latest_hours_ago=5)
    assert result.reason == DISABLED


def test_stats_are_reported_for_telemetry() -> None:
    result = _eval(state=_State(), sessions=25, latest_hours_ago=5)
    assert result.stats["session_count"] == 25
    assert result.stats["min_sessions"] == 20
    assert "last_attempt_ts" in result.stats
