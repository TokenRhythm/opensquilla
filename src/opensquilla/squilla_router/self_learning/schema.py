"""Training-sample schema and feature (de)serialization for self-learning.

A :class:`RouterTrainSample` is one JSON line appended per completed turn to a
per-agent event store. Feature vectors are stored as base64-encoded ``float16``
to stay compact (``features_390`` is ~0.78 KB/sample) and dependency-free at
runtime (numpy only; no parquet/pyarrow on the hot path).
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, field, fields
from typing import Any

import numpy as np

# Additive-only; bump when fields are added. Readers tolerate unknown fields.
SCHEMA_VERSION = 1

FEATURES_390_DIM = 390
RAW_BGE_DIM = 1536


def encode_features(arr: Any) -> str:
    """Encode a float vector as base64 of its ``float16`` byte representation."""

    vec = np.asarray(arr, dtype=np.float16).reshape(-1)
    return base64.b64encode(vec.tobytes()).decode("ascii")


def decode_features(b64: str, dim: int | None = None) -> np.ndarray:
    """Decode a base64 ``float16`` blob back into a 1-D ``float32`` array.

    Returns ``float32`` because the LightGBM/MLP heads consume float32; the
    float16 storage is purely a size optimization.
    """

    raw = base64.b64decode(b64.encode("ascii"))
    vec = np.frombuffer(raw, dtype=np.float16).astype(np.float32)
    if dim is not None and vec.shape[0] != dim:
        raise ValueError(f"feature length {vec.shape[0]} != expected {dim}")
    return vec


@dataclass
class RouterTrainSample:
    """One captured turn: features the model used + the routing decision.

    Label alignment (immediate complaint / retrospective under-routing /
    confidence backoff) happens offline from the sequence of these rows, so the
    online side only records raw signals, never a final training label.
    """

    # --- Locators (offline aligner re-sorts a session by ``ts``) ---
    session_key: str
    turn_index: int
    ts: str  # ISO8601 UTC

    # --- Training-source features (float16, base64) ---
    feature_schema_version: str  # ties features to the fitted projections
    features_390_b64: str
    raw_bge_1536_b64: str | None = None  # only when enable_mlp

    # --- Runtime routing decision (used for label alignment) ---
    route_class: str = "R1"  # raw model prediction
    final_route_class: str = "R1"  # after heuristics
    routed_tier: str = "c1"  # final served tier
    probabilities: list[float] = field(default_factory=list)
    margin: float = 0.0
    confidence: float = 0.0

    # --- Heuristic markers (label-alignment inputs) ---
    complaint_detected: bool = False
    anti_downgrade_applied: bool = False
    confidence_gate_applied: bool = False
    large_context_floor_applied: bool = False
    image_route: bool = False  # offline drops these from text classifier data
    exploration: bool = False  # served a counterfactual tier (see RFC §5.5)

    # --- Opt-in audit sidecar (default off); never used for training ---
    audit_summary: str | None = None

    # V017 decision-record id for this turn. The explicit-feedback join key:
    # (session_key, turn_index) is NOT unique (the routing-history-derived
    # turn_index saturates at the history cap and resets after idle), so
    # ratings attach to samples through this id. None on legacy rows — those
    # simply never match feedback.
    decision_id: str | None = None

    schema_version: int = SCHEMA_VERSION

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json_dict(cls, payload: dict[str, Any]) -> RouterTrainSample:
        allowed = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in allowed})
