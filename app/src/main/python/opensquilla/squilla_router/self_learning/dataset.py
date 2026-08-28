"""Build a denoised training dataset from captured router samples.

Pipeline (offline, consumes the per-agent event store):

    iter_samples -> keep dominant feature_schema_version -> drop bypass routes
    -> group by session -> align labels -> evidence-ledger weighting
    -> (X, y, sample_weight) with session/turn provenance preserved.

Evidence-ledger weighting (adapted from the Dream promotion ranking): correction
signals (immediate/retrospective complaints) are rare and valuable so they keep a
high base weight; confirmation signals (normal/backoff) are flood-damped by how
often the identical feature vector recurs; anything corroborated across multiple
days is boosted. This is what keeps a session full of "好的"/"thanks" from
drowning the rare R2/R3 corrections.

Output arrays are plain numpy, exported as ``.npz`` (no parquet/pyarrow needed).
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from opensquilla.squilla_router.self_learning.alignment import (
    EXCLUDED_REASONS,
    REASON_CONFIDENCE_BACKOFF,
    REASON_EXPLICIT_DOWNVOTE,
    REASON_EXPLICIT_UPVOTE,
    REASON_EXPLICIT_UPVOTE_ENSEMBLE,
    REASON_IMMEDIATE_COMPLAINT,
    REASON_NORMAL,
    REASON_RETROSPECTIVE,
    AlignedSample,
    align_session,
)
from opensquilla.squilla_router.self_learning.store import iter_samples, router_data_root

# Base weight by alignment reason (correction signals dominate). Explicit
# down-votes outrank retrospective inference (a deliberate user action beats a
# heuristic); explicit up-votes are confirmations, deliberately below every
# correction. Ensemble up-votes fall to normal level — the endorsement covers
# the whole candidates+aggregator chain, so its information about the tier
# choice is diluted. Excluded reasons (ensemble/high-tier down-votes) get
# weight 0: the user said the outcome was bad, so the turn must not train as
# a confirmation, yet the blame cannot be pinned on the tier.
_BASE_WEIGHT = {
    REASON_EXPLICIT_DOWNVOTE: 1.2,
    REASON_RETROSPECTIVE: 1.0,
    REASON_IMMEDIATE_COMPLAINT: 0.9,
    REASON_EXPLICIT_UPVOTE: 0.6,
    REASON_CONFIDENCE_BACKOFF: 0.5,
    REASON_NORMAL: 0.3,
    REASON_EXPLICIT_UPVOTE_ENSEMBLE: 0.3,
}
# Reasons that merely confirm the served decision; these can flood, so damp them
# by frequency of the identical feature vector. Up-votes are confirmations too
# (polite users up-vote everything).
_FLOOD_DAMPED = {
    REASON_NORMAL,
    REASON_CONFIDENCE_BACKOFF,
    REASON_EXPLICIT_UPVOTE,
    REASON_EXPLICIT_UPVOTE_ENSEMBLE,
}
_UNCONFIRMED_RETRO_FACTOR = 0.5
_CROSS_DAY_PER_DAY = 0.15
_CROSS_DAY_CAP = 1.6
_WEIGHT_CAP = 2.0


@dataclass
class TrainingDataset:
    """Materialized, weighted training matrix plus provenance and diagnostics."""

    X: np.ndarray  # (N, 390) float32
    y: np.ndarray  # (N,) int64 corrected route-class index (training target)
    w: np.ndarray  # (N,) float32 sample weight
    served: np.ndarray | None = None  # (N,) int64 served route idx (cost reference)
    session_keys: list[str] = field(default_factory=list)
    turn_indices: list[int] = field(default_factory=list)
    days: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    feature_schema_version: str = "unknown"
    n_sessions: int = 0
    skipped_schema_mismatch: int = 0
    skipped_bypass: int = 0

    def __len__(self) -> int:
        return int(self.X.shape[0])

    def class_distribution(self) -> dict[int, int]:
        return {int(k): int(v) for k, v in zip(*np.unique(self.y, return_counts=True))}

    def reason_distribution(self) -> dict[str, int]:
        return dict(Counter(self.reasons))

    @classmethod
    def load_npz(cls, npz_path: Path) -> TrainingDataset:
        """Reload a dataset written by :func:`export_training_dataset`.

        Reads the ``.meta.json`` sidecar for fields not stored in the npz
        (schema version, session count). Used by the training subprocess.
        """

        npz_path = Path(npz_path)
        data = np.load(npz_path, allow_pickle=True)
        meta_path = npz_path.with_name(f"{npz_path.stem}.meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
        return cls(
            X=data["X"].astype(np.float32),
            y=data["y"].astype(np.int64),
            w=data["w"].astype(np.float32),
            served=(data["served"].astype(np.int64) if "served" in data.files else None),
            session_keys=[str(s) for s in data["session_keys"]],
            turn_indices=[int(t) for t in data["turn_indices"]],
            days=[str(d) for d in data["days"]],
            reasons=[str(r) for r in data["reasons"]],
            feature_schema_version=str(meta.get("feature_schema_version", "unknown")),
            n_sessions=int(meta.get("n_sessions", 0)),
            skipped_schema_mismatch=int(meta.get("skipped_schema_mismatch", 0)),
            skipped_bypass=int(meta.get("skipped_bypass", 0)),
        )


def _empty_dataset() -> TrainingDataset:
    return TrainingDataset(
        X=np.zeros((0, 390), dtype=np.float32),
        y=np.zeros((0,), dtype=np.int64),
        w=np.zeros((0,), dtype=np.float32),
        served=np.zeros((0,), dtype=np.int64),
    )


def build_training_dataset(
    agent_id: str,
    *,
    home: Path | None = None,
    since_ts: str | None = None,
) -> TrainingDataset:
    """Assemble a denoised, weighted dataset for one agent."""

    samples = list(iter_samples(agent_id, since_ts=since_ts, home=home))
    if not samples:
        return _empty_dataset()

    # Only one projection basis may be mixed; pick the dominant schema version.
    version_counts = Counter(s.feature_schema_version for s in samples)
    target_version = version_counts.most_common(1)[0][0]
    skipped_schema = sum(c for v, c in version_counts.items() if v != target_version)

    in_version = [s for s in samples if s.feature_schema_version == target_version]
    kept = [s for s in in_version if not s.image_route]
    skipped_bypass = len(in_version) - len(kept)
    if not kept:
        ds = _empty_dataset()
        ds.feature_schema_version = target_version
        ds.skipped_schema_mismatch = skipped_schema
        ds.skipped_bypass = skipped_bypass
        return ds

    by_session: dict[str, list] = defaultdict(list)
    for s in kept:
        by_session[s.session_key].append(s)

    from opensquilla.squilla_router.self_learning.feedback import load_feedback_map

    feedback = load_feedback_map(agent_id, home=home) or None

    aligned: list[AlignedSample] = []
    for session_samples in by_session.values():
        aligned.extend(align_session(session_samples, feedback))

    # Drop excluded rows from the matrix entirely, not just from the weights:
    # their target is the served tier the user rejected, and the unweighted
    # holdout/cost metrics in evaluate.py would otherwise score agreement with
    # a label the pipeline explicitly declared untrainable-because-bad.
    aligned = [a for a in aligned if a.reason not in EXCLUDED_REASONS]
    if not aligned:
        ds = _empty_dataset()
        ds.feature_schema_version = target_version
        ds.skipped_schema_mismatch = skipped_schema
        ds.skipped_bypass = skipped_bypass
        return ds

    weights = _compute_weights(aligned)

    feature_matrix = np.vstack([a.features_390.astype(np.float32) for a in aligned])
    labels = np.asarray([a.target_idx for a in aligned], dtype=np.int64)
    sample_weights = np.asarray(weights, dtype=np.float32)
    served = np.asarray([a.served_idx for a in aligned], dtype=np.int64)

    return TrainingDataset(
        X=feature_matrix,
        y=labels,
        w=sample_weights,
        served=served,
        session_keys=[a.session_key for a in aligned],
        turn_indices=[a.turn_index for a in aligned],
        days=[a.day for a in aligned],
        reasons=[a.reason for a in aligned],
        feature_schema_version=target_version,
        n_sessions=len(by_session),
        skipped_schema_mismatch=skipped_schema,
        skipped_bypass=skipped_bypass,
    )


def _compute_weights(aligned: list[AlignedSample]) -> list[float]:
    """Evidence-ledger sample weights (see module docstring)."""

    freq: Counter[str] = Counter(a.feature_hash for a in aligned)
    days_seen: dict[str, set[str]] = defaultdict(set)
    for a in aligned:
        days_seen[a.feature_hash].add(a.day)

    weights: list[float] = []
    for a in aligned:
        if a.reason in EXCLUDED_REASONS:
            weights.append(0.0)
            continue
        w = _BASE_WEIGHT.get(a.reason, _BASE_WEIGHT[REASON_NORMAL])
        if (
            a.reason in (REASON_RETROSPECTIVE, REASON_EXPLICIT_DOWNVOTE)
            and not a.confirmed
        ):
            w *= _UNCONFIRMED_RETRO_FACTOR
        if a.reason in _FLOOD_DAMPED:
            w /= math.sqrt(freq[a.feature_hash])
        distinct_days = len(days_seen[a.feature_hash])
        w *= min(1.0 + _CROSS_DAY_PER_DAY * (distinct_days - 1), _CROSS_DAY_CAP)
        weights.append(min(w, _WEIGHT_CAP))
    return weights


def datasets_dir(agent_id: str, home: Path | None = None) -> Path:
    from opensquilla.squilla_router.self_learning.store import _safe_agent_id

    return router_data_root(home) / "datasets" / _safe_agent_id(agent_id)


def export_training_dataset(
    dataset: TrainingDataset,
    agent_id: str,
    *,
    home: Path | None = None,
) -> Path:
    """Write the dataset to ``<root>/datasets/<agent>/<schema>.npz`` plus a JSON
    sidecar of diagnostics. Returns the npz path."""

    out_dir = datasets_dir(agent_id, home)
    out_dir.mkdir(parents=True, exist_ok=True)
    npz_path = out_dir / f"{dataset.feature_schema_version}.npz"
    np.savez_compressed(
        npz_path,
        X=dataset.X,
        y=dataset.y,
        w=dataset.w,
        served=(dataset.served if dataset.served is not None else np.zeros(len(dataset), np.int64)),
        session_keys=np.asarray(dataset.session_keys, dtype=object),
        turn_indices=np.asarray(dataset.turn_indices, dtype=np.int64),
        days=np.asarray(dataset.days, dtype=object),
        reasons=np.asarray(dataset.reasons, dtype=object),
    )
    (out_dir / f"{dataset.feature_schema_version}.meta.json").write_text(
        json.dumps(
            {
                "feature_schema_version": dataset.feature_schema_version,
                "n_samples": len(dataset),
                "n_sessions": dataset.n_sessions,
                "class_distribution": dataset.class_distribution(),
                "reason_distribution": dataset.reason_distribution(),
                "skipped_schema_mismatch": dataset.skipped_schema_mismatch,
                "skipped_bypass": dataset.skipped_bypass,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return npz_path
