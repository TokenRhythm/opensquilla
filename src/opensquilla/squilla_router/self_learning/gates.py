"""Trigger gating for ``maybe_run_update_router``.

The cron/post-dream hook only provides the *opportunity* to check; this AND-gate
chain decides whether training actually runs. Every gate must pass. Keeping this
a pure function (state + stats + config + now -> decision) makes the policy fully
unit-testable and side-effect free.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from opensquilla.squilla_router.self_learning.state import (
    EventStoreStats,
    TrainState,
    scan_event_store,
)
from opensquilla.squilla_router.self_learning.store import self_learning_disabled_by_env

# Gate reason codes (stable strings for receipts/telemetry).
READY = "ready"
DISABLED = "disabled"
NO_DATA = "no_data"
AGENT_ACTIVE = "agent_active"
COOLDOWN = "cooldown"
INSUFFICIENT_DATA = "insufficient_data"
INSUFFICIENT_CLASS_DIVERSITY = "insufficient_class_diversity"

_MIN_CLASSES = 2


@dataclass
class GateResult:
    should_train: bool
    reason: str
    effective_min_samples: int = 0
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


def evaluate_training_gates(
    *,
    config: Any,
    state: TrainState,
    stats: EventStoreStats,
    now: datetime | None = None,
) -> GateResult:
    """Decide whether to train. Pure: no IO, no clock unless ``now`` omitted."""

    now = now or datetime.now(UTC)

    # Failure backoff: each consecutive failed/rejected attempt doubles the data
    # bar, so a stream of uninformative labels stops retraining every cycle.
    base_min = int(getattr(config, "train_min_samples", 200))
    effective_min = base_min * (2**max(0, int(state.consecutive_failures)))

    def result(should: bool, reason: str) -> GateResult:
        return GateResult(
            should_train=should,
            reason=reason,
            effective_min_samples=effective_min,
            stats={
                "total": stats.total,
                "high_value": stats.high_value,
                "feedback_down": stats.feedback_down,
                "distinct_classes": stats.distinct_classes,
                "last_ts": stats.last_ts,
                "consecutive_failures": state.consecutive_failures,
            },
        )

    if not getattr(config, "enabled", False) or self_learning_disabled_by_env():
        return result(False, DISABLED)

    if stats.total <= 0:
        return result(False, NO_DATA)

    # Idle gate: do not contend with the router on the hot path. If the most
    # recent captured turn is within idle_hours, the agent is in use -> defer.
    idle_hours = float(getattr(config, "idle_hours", 2.0))
    since_activity = _hours_since(stats.last_ts, now)
    if idle_hours > 0 and since_activity is not None and since_activity < idle_hours:
        return result(False, AGENT_ACTIVE)

    # Cooldown gate: at most one train per cooldown window.
    cooldown_hours = float(getattr(config, "cooldown_hours", 72.0))
    since_train = _hours_since(state.last_train_ts, now)
    if cooldown_hours > 0 and since_train is not None and since_train < cooldown_hours:
        return result(False, COOLDOWN)

    # Volume gate (high-value correction signals, scaled by failure backoff).
    if stats.high_value + stats.feedback_down < effective_min:
        return result(False, INSUFFICIENT_DATA)

    # Quality floor: need at least two classes or there is nothing to separate.
    if stats.distinct_classes < _MIN_CLASSES:
        return result(False, INSUFFICIENT_CLASS_DIVERSITY)

    return result(True, READY)


def merge_feedback_into_stats(stats: Any, agent_id: str, home: Any = None) -> Any:
    """Return ``stats`` with single-model down-votes counted for the volume gate.

    Ensemble down-votes never become training labels (alignment excludes
    them), so they are not counted. The count is a deliberate approximation:
    it does not verify each rating joins a live sample or survives the
    high-tier exclusion (both would need a full sample scan at gate time),
    and a down-voted complaint turn also counts once in high_value. A gate
    opened on non-training ratings yields an uninformative candidate, which
    the promotion gate rejects and the consecutive-failure backoff then
    doubles the bar — bounded churn, not silent corruption. Fail-open: a
    broken sidecar returns ``stats`` unchanged. Shared by the orchestrator
    and the status RPC so both gates see identical inputs.
    """

    try:
        from opensquilla.squilla_router.self_learning.feedback import scan_feedback_stats

        fb = scan_feedback_stats(agent_id, home=home)
    except Exception:  # noqa: BLE001 — feedback must never block the gate
        return stats
    if fb.down_single == 0:
        return stats
    from dataclasses import replace

    return replace(stats, feedback_down=fb.down_single)


def evaluate_gates_for_agent(
    agent_id: str,
    *,
    config: Any,
    state: TrainState,
    home: Any = None,
    now: datetime | None = None,
) -> GateResult:
    """Convenience wrapper that scans the store then evaluates the gates."""

    stats = scan_event_store(agent_id, home=home)
    return evaluate_training_gates(config=config, state=state, stats=stats, now=now)
