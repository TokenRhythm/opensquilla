from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Trajectory(StrEnum):
    COLD_START = "COLD_START"
    STABLE_LOW = "STABLE_LOW"
    STABLE_HIGH = "STABLE_HIGH"
    ESCALATING = "ESCALATING"
    DESCALATING = "DESCALATING"
    OSCILLATING = "OSCILLATING"
    UNCLEAR = "UNCLEAR"
    MIXED = "MIXED"


@dataclass(frozen=True)
class TurnDecision:
    turn_index: int
    route_class: str
    difficulty: float
    margin: float
    top1_label: str


_LOW = {"R0", "R1"}
_HIGH = {"R2", "R3"}


def classify(
    history: list[TurnDecision],
    delta_threshold: float = 0.3,
) -> Trajectory:
    if not history:
        return Trajectory.COLD_START
    if len(history) < 2:
        return Trajectory.UNCLEAR

    tiers = [h.route_class for h in history]
    diffs = [h.difficulty for h in history]

    if all(t in _LOW for t in tiers):
        return Trajectory.STABLE_LOW
    if all(t in _HIGH for t in tiers):
        return Trajectory.STABLE_HIGH

    diffs_deltas = [b - a for a, b in zip(diffs, diffs[1:])]
    signs = [
        1 if d > delta_threshold else (-1 if d < -delta_threshold else 0)
        for d in diffs_deltas
    ]
    nonzero = [s for s in signs if s != 0]

    if len(nonzero) >= 2 and all(s == 1 for s in nonzero):
        return Trajectory.ESCALATING
    if len(nonzero) >= 2 and all(s == -1 for s in nonzero):
        return Trajectory.DESCALATING

    direction_changes = sum(
        1 for a, b in zip(nonzero, nonzero[1:]) if a != b
    )
    if direction_changes >= 2:
        return Trajectory.OSCILLATING

    return Trajectory.MIXED
