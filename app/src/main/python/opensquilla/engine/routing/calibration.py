"""Deterministic, on-device router calibration.

The bundled ML classifier is FROZEN. This module never retrains anything: it
aggregates local, prompt-free router decision records into a small, hard-clamped
adjustment to a single scalar — the confidence-gate ``confidence_threshold`` —
plus a per-tier confidence bias. The adjustment file lives in the runtime state
dir (``router_calibration.json``); the routing policy reads it as an argument
(never a file) and applies it as a bias in :func:`confidence_gate`.

Signals (all already recorded by ``router_decision_record`` / the V017
``router_decisions`` table, enum tokens + numbers only, no prompt text):

* ``confidence_gate`` applied  — the gate downgraded a proposed tier.
* ``complaint_upgrade`` applied — the user pushed back (routed too weak).
* ``source == "router_control_hold"`` — an operator pinned a tier.

Deliberately *out of scope* (kept conservative, per the overhaul's OQ#7):

* No "regeneration within N seconds" signal is derived — regeneration is NOT a
  calibration input this pass.
* No savings/cost figure is read or written — calibration adjusts the routing
  threshold/bias only, never the cost math (hard constraint C2).

Determinism: :func:`aggregate_calibration` is pure and takes an injected ``now``
(epoch ms) — no wall clock, no network, no I/O. Load/save tolerate a missing or
corrupt file by returning a neutral (zero-adjustment) state.

Hard clamps (non-negotiable, enforced at *read* time as well as write time):

* ``|per_class_bias[t]| <= 0.15``
* the adjusted ``confidence_threshold`` stays within ``[0.3, 0.7]``
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from opensquilla.paths import state_dir
from opensquilla.router_tiers import TEXT_TIERS, normalize_text_tier

log = structlog.get_logger(__name__)

_SCHEMA_VERSION = 1
_CALIBRATION_FILENAME = "router_calibration.json"

# --- Hard clamps (pinned) --------------------------------------------------
BIAS_CLAMP = 0.15
THRESHOLD_FLOOR = 0.3
THRESHOLD_CEIL = 0.7
# Bound the stored threshold adjustment too; the effective-threshold read-time
# clamp to [0.3, 0.7] is the authoritative guarantee, this just keeps the file
# sane and the base-config independent.
THRESHOLD_ADJUST_CLAMP = 0.20

# --- Aggregation shape (conservative gains + small-sample shrinkage) --------
_BIAS_GAIN = 0.15
_THRESHOLD_GAIN = 0.20
_MIN_SAMPLES_PER_CLASS = 20
_MIN_SAMPLES_GLOBAL = 50
_PRIOR_WEIGHT = 0.5
_ROUND_DP = 4
# Only records within this age (relative to the injected ``now``) contribute.
_WINDOW_MS = 30 * 24 * 60 * 60 * 1000


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    as_float = float(value)
    if as_float != as_float or as_float in (float("inf"), float("-inf")):
        return None
    return as_float


@dataclass(frozen=True)
class CalibrationState:
    """A small, clamped router-threshold adjustment.

    ``per_class_bias`` maps a canonical text tier (``c0``-``c3``) to a bias in
    ``[-0.15, 0.15]`` added to that tier's classifier confidence before the
    gate compares. ``threshold_adjust`` shifts the base ``confidence_threshold``
    (clamped into ``[0.3, 0.7]`` at read time). ``sample_count`` is how many
    decision records fed the aggregation.
    """

    per_class_bias: dict[str, float] = field(default_factory=dict)
    threshold_adjust: float = 0.0
    sample_count: int = 0
    generated_at_ms: int = 0

    @classmethod
    def neutral(cls) -> CalibrationState:
        """A zero-adjustment state — applying it is a no-op."""
        return cls()

    def is_neutral(self) -> bool:
        return self.threshold_adjust == 0.0 and not any(
            value != 0.0 for value in self.per_class_bias.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _SCHEMA_VERSION,
            "per_class_bias": {
                key: float(value) for key, value in sorted(self.per_class_bias.items())
            },
            "threshold_adjust": float(self.threshold_adjust),
            "sample_count": int(self.sample_count),
            "generated_at_ms": int(self.generated_at_ms),
        }

    @classmethod
    def from_dict(cls, data: object) -> CalibrationState:
        """Parse (and re-clamp) a stored payload; never raise on bad input."""
        if not isinstance(data, Mapping):
            return cls.neutral()
        per_class_bias: dict[str, float] = {}
        raw_bias = data.get("per_class_bias")
        if isinstance(raw_bias, Mapping):
            for key, value in raw_bias.items():
                tier = normalize_text_tier(key)
                bias = _finite_float(value)
                if tier is None or bias is None:
                    continue
                clamped = _clamp(bias, -BIAS_CLAMP, BIAS_CLAMP)
                if clamped != 0.0:
                    per_class_bias[tier] = clamped
        raw_threshold = _finite_float(data.get("threshold_adjust"))
        threshold_adjust = (
            _clamp(raw_threshold, -THRESHOLD_ADJUST_CLAMP, THRESHOLD_ADJUST_CLAMP)
            if raw_threshold is not None
            else 0.0
        )
        raw_samples = _finite_float(data.get("sample_count"))
        sample_count = int(raw_samples) if raw_samples is not None and raw_samples >= 0 else 0
        raw_generated = _finite_float(data.get("generated_at_ms"))
        generated_at_ms = (
            int(raw_generated) if raw_generated is not None and raw_generated >= 0 else 0
        )
        return cls(
            per_class_bias=per_class_bias,
            threshold_adjust=threshold_adjust,
            sample_count=sample_count,
            generated_at_ms=generated_at_ms,
        )


# ---------------------------------------------------------------------------
# Read-time application (pure; used by the routing policy)
# ---------------------------------------------------------------------------


def effective_threshold(base: float, state: CalibrationState | None) -> float:
    """Return the confidence-gate threshold after calibration.

    With ``state is None`` this is exactly ``base`` (the byte-identical default
    path). With a state, the adjusted threshold is HARD-clamped into
    ``[0.3, 0.7]`` — the pinned guarantee, applied here at read time regardless
    of what the file claims.
    """
    if state is None:
        return base
    return _clamp(base + state.threshold_adjust, THRESHOLD_FLOOR, THRESHOLD_CEIL)


def apply_bias(confidence: float, tier: str, state: CalibrationState | None) -> float:
    """Return ``confidence`` biased by the tier's clamped per-class adjustment.

    With ``state is None`` this returns ``confidence`` unchanged (byte-identical
    default path). Otherwise the bias is HARD-clamped to ``[-0.15, 0.15]`` at
    read time and the result is kept a valid probability in ``[0, 1]``.
    """
    if state is None:
        return confidence
    key = normalize_text_tier(tier) or tier
    bias = _clamp(state.per_class_bias.get(key, 0.0), -BIAS_CLAMP, BIAS_CLAMP)
    return _clamp(confidence + bias, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Aggregation (pure, deterministic)
# ---------------------------------------------------------------------------


def _stage_applied(trail: object, stage: str) -> bool:
    if not isinstance(trail, (list, tuple)):
        return False
    for entry in trail:
        if (
            isinstance(entry, Mapping)
            and entry.get("stage") == stage
            and bool(entry.get("applied"))
        ):
            return True
    return False


def _flag_present(flags: object, token: str) -> bool:
    return isinstance(flags, (list, tuple)) and token in flags


def _record_signals(record: Mapping[str, Any]) -> tuple[bool, bool, bool]:
    """Extract (gated, complained, pinned) from one decision record.

    Reads the structured ``trail`` first (as built by
    ``router_decision_record.build_trail``) and falls back to enum ``flags``
    tokens and the ``source`` column. No free text is ever consulted.
    """
    trail = record.get("trail")
    flags = record.get("flags")
    source = record.get("source")
    gated = _stage_applied(trail, "confidence_gate") or _flag_present(
        flags, "confidence_gate_applied"
    )
    complained = _stage_applied(trail, "complaint_upgrade") or _flag_present(
        flags, "complaint_upgrade_applied"
    )
    pinned = (
        (isinstance(source, str) and source == "router_control_hold")
        or _flag_present(flags, "router_control_hold_applied")
        or _flag_present(flags, "router_control_hold")
    )
    return gated, complained, pinned


def aggregate_calibration(
    records: Iterable[Mapping[str, Any]],
    *,
    now: int,
    prior: CalibrationState | None = None,
) -> CalibrationState:
    """Turn decision records into a clamped :class:`CalibrationState`.

    Pure and deterministic given ``records``, ``now`` (epoch ms), and ``prior``.
    Records older than ``_WINDOW_MS`` relative to ``now`` are ignored; records
    without a recognizable ``proposed_tier`` do not contribute.

    Direction (see module docstring for the full rationale):

    * A gate downgrade the user then complained about (a *false* downgrade) is
      evidence the gate is too aggressive for that tier -> positive per-class
      bias (trust the tier), and global pressure to LOWER the threshold.
    * A clean gate downgrade (no complaint) is evidence the classifier
      over-proposes that tier -> negative per-class bias, and pressure to RAISE
      the threshold.
    * Complaints and operator pins are global under-route pressure -> LOWER the
      threshold.

    ``prior`` (when given and non-neutral) is blended 50/50 for run-to-run
    stability; a ``None`` or neutral prior yields the freshly computed state.
    """
    window_start = int(now) - _WINDOW_MS
    per_class_vote: dict[str, float] = {tier: 0.0 for tier in TEXT_TIERS}
    per_class_count: dict[str, int] = {tier: 0 for tier in TEXT_TIERS}
    global_vote = 0.0
    considered = 0

    for record in records:
        if not isinstance(record, Mapping):
            continue
        ts = _finite_float(record.get("ts_ms"))
        if ts is not None and ts < window_start:
            continue
        tier = normalize_text_tier(record.get("proposed_tier"))
        if tier is None or tier not in per_class_vote:
            continue
        gated, complained, pinned = _record_signals(record)
        considered += 1
        per_class_count[tier] += 1
        if gated and complained:
            per_class_vote[tier] += 1.0
        elif gated and not complained:
            per_class_vote[tier] -= 1.0
        if complained:
            global_vote -= 1.0
        if pinned:
            global_vote -= 1.0
        if gated and not complained:
            global_vote += 1.0

    per_class_bias: dict[str, float] = {}
    for tier in TEXT_TIERS:
        count = per_class_count[tier]
        if count == 0:
            continue
        raw = _BIAS_GAIN * per_class_vote[tier] / max(count, _MIN_SAMPLES_PER_CLASS)
        bias = round(_clamp(raw, -BIAS_CLAMP, BIAS_CLAMP), _ROUND_DP)
        if bias != 0.0:
            per_class_bias[tier] = bias

    if considered == 0:
        threshold_adjust = 0.0
    else:
        raw_threshold = _THRESHOLD_GAIN * global_vote / max(considered, _MIN_SAMPLES_GLOBAL)
        threshold_adjust = round(
            _clamp(raw_threshold, -THRESHOLD_ADJUST_CLAMP, THRESHOLD_ADJUST_CLAMP), _ROUND_DP
        )

    computed = CalibrationState(
        per_class_bias=per_class_bias,
        threshold_adjust=threshold_adjust,
        sample_count=considered,
        generated_at_ms=int(now),
    )
    if prior is None or prior.is_neutral():
        return computed
    return _blend(prior, computed, now=int(now))


def _blend(prior: CalibrationState, computed: CalibrationState, *, now: int) -> CalibrationState:
    tiers = set(prior.per_class_bias) | set(computed.per_class_bias)
    blended_bias: dict[str, float] = {}
    for tier in sorted(tiers):
        value = _PRIOR_WEIGHT * prior.per_class_bias.get(tier, 0.0) + (
            1.0 - _PRIOR_WEIGHT
        ) * computed.per_class_bias.get(tier, 0.0)
        clamped = round(_clamp(value, -BIAS_CLAMP, BIAS_CLAMP), _ROUND_DP)
        if clamped != 0.0:
            blended_bias[tier] = clamped
    blended_threshold = round(
        _clamp(
            _PRIOR_WEIGHT * prior.threshold_adjust
            + (1.0 - _PRIOR_WEIGHT) * computed.threshold_adjust,
            -THRESHOLD_ADJUST_CLAMP,
            THRESHOLD_ADJUST_CLAMP,
        ),
        _ROUND_DP,
    )
    return CalibrationState(
        per_class_bias=blended_bias,
        threshold_adjust=blended_threshold,
        sample_count=computed.sample_count,
        generated_at_ms=now,
    )


# ---------------------------------------------------------------------------
# Load / save (atomic; tolerate missing/corrupt)
# ---------------------------------------------------------------------------


def calibration_path() -> Path:
    """Absolute path to ``router_calibration.json`` under the state dir."""
    return state_dir(_CALIBRATION_FILENAME)


def load_calibration(path: str | os.PathLike[str] | None = None) -> CalibrationState:
    """Load the calibration state; a missing or corrupt file -> neutral state."""
    target = Path(path) if path is not None else calibration_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return CalibrationState.neutral()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("router_calibration.load_corrupt", path=str(target))
        return CalibrationState.neutral()
    try:
        return CalibrationState.from_dict(data)
    except Exception:  # noqa: BLE001 - a bad file must never crash a turn or the job
        log.warning("router_calibration.parse_failed", path=str(target), exc_info=True)
        return CalibrationState.neutral()


def save_calibration(
    state: CalibrationState, path: str | os.PathLike[str] | None = None
) -> Path:
    """Atomically write ``state`` to the calibration file; return the path."""
    target = Path(path) if path is not None else calibration_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_dict(), sort_keys=True, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".router_calibration.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target
