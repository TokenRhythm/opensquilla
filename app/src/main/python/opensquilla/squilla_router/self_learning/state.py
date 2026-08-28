"""Per-agent training state and a cheap event-store stats scan.

``TrainState`` persists what the trigger gates need across runs (last successful
train, last attempt, consecutive failures, last promoted version). The stats scan
reads the JSONL event store *without decoding feature vectors*, so the gates stay
cheap enough to evaluate on every post-dream hook.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from opensquilla.squilla_router.self_learning.store import agent_data_dir

_HIGH_VALUE_KEYS = ("complaint_detected", "confidence_gate_applied", "anti_downgrade_applied")


@dataclass
class TrainState:
    """Persisted trigger/promotion bookkeeping for one agent."""

    last_train_ts: str | None = None  # last successful candidate build
    last_attempt_ts: str | None = None
    consecutive_failures: int = 0
    last_version: str | None = None  # last candidate version built
    # Promotion / rollback monitoring (M3/M4)
    active_version: str | None = None  # currently promoted learned version
    promoted_at: str | None = None  # ts of the last promotion (monitor window start)
    pre_promotion_complaint_rate: float | None = None  # baseline before the swap
    # Explicit-feedback baseline (single-model down-vote rate) captured at
    # promotion; the rollback monitor's second trigger compares against it.
    pre_promotion_downvote_rate: float | None = None

    def to_json(self) -> dict:
        return asdict(self)


def _state_path(agent_id: str, home: Path | None = None) -> Path:
    return agent_data_dir(agent_id, home) / ".train_state.json"


def load_train_state(agent_id: str, home: Path | None = None) -> TrainState:
    path = _state_path(agent_id, home)
    if not path.is_file():
        return TrainState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return TrainState()
    allowed = TrainState().to_json().keys()
    return TrainState(**{k: v for k, v in payload.items() if k in allowed})


def save_train_state(state: TrainState, agent_id: str, home: Path | None = None) -> Path:
    path = _state_path(agent_id, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@dataclass
class EventStoreStats:
    """Cheap summary of captured samples for gate evaluation."""

    total: int = 0
    high_value: int = 0  # complaint / confidence-gate / anti-downgrade turns
    complaints: int = 0  # complaint_detected turns (rollback monitor)
    distinct_classes: int = 0  # distinct final_route_class values
    last_ts: str | None = None  # most recent sample ts (idle signal)
    dominant_schema_version: str | None = None
    # Explicit down-votes with a joinable sample (filled by the orchestrator
    # from the feedback sidecar, not by scan_event_store). They are correction
    # signals, so the volume gate counts them toward high_value.
    feedback_down: int = 0

    @property
    def complaint_rate(self) -> float:
        return (self.complaints / self.total) if self.total else 0.0


def scan_event_store(
    agent_id: str,
    *,
    home: Path | None = None,
    since_ts: str | None = None,
) -> EventStoreStats:
    """Summarize the JSONL store without decoding feature blobs."""

    data_dir = agent_data_dir(agent_id, home)
    if not data_dir.is_dir():
        return EventStoreStats()

    total = 0
    high_value = 0
    complaints = 0
    classes: set[str] = set()
    last_ts: str | None = None
    schema_counts: dict[str, int] = {}

    for path in sorted(data_dir.glob("samples-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = str(row.get("ts", ""))
            if since_ts is not None and ts <= since_ts:
                continue
            if row.get("image_route"):
                continue
            total += 1
            if any(row.get(k) for k in _HIGH_VALUE_KEYS):
                high_value += 1
            if row.get("complaint_detected"):
                complaints += 1
            cls = row.get("final_route_class") or row.get("route_class")
            if cls:
                classes.add(str(cls))
            if last_ts is None or ts > last_ts:
                last_ts = ts
            ver = row.get("feature_schema_version")
            if ver:
                schema_counts[str(ver)] = schema_counts.get(str(ver), 0) + 1

    dominant = max(schema_counts, key=lambda k: schema_counts[k]) if schema_counts else None
    return EventStoreStats(
        total=total,
        high_value=high_value,
        complaints=complaints,
        distinct_classes=len(classes),
        last_ts=last_ts,
        dominant_schema_version=dominant,
    )
