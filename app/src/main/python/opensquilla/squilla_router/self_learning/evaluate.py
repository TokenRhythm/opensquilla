"""Promotion evaluation: rolling holdout, route metrics, and the gate.

The progress metric is a **rolling, resampled** session-level holdout of the
agent's own data (re-drawn each cycle so we never overfit a static eval set);
the regression tripwire is an optional **frozen golden set**. A candidate is
promoted only when quality does not regress and cost (mean routed tier) stays
within tolerance of what actually ran — directly countering the over-routing
bias of complaint-driven labels.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from opensquilla.squilla_router.self_learning.dataset import TrainingDataset

# A label at or above this index "demands" a strong tier; under-routing these is
# the quality failure we most want to avoid.
_CRITICAL_IDX = 2


@dataclass
class RouteMetrics:
    n: int
    agreement: float  # fraction predicted == corrected target
    critical_under_routing_rate: float  # on target>=R2: fraction predicted lower
    mean_pred_idx: float
    served_mean_idx: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def predict_indices(booster, x: np.ndarray) -> np.ndarray:
    """Argmax route index from a multiclass LightGBM booster."""

    proba = np.asarray(booster.predict(x.astype(np.float64)))
    if proba.ndim == 1:  # single row
        proba = proba.reshape(1, -1)
    return np.asarray(proba.argmax(axis=1), dtype=np.int64)


def route_metrics(
    pred_idx: np.ndarray,
    target_idx: np.ndarray,
    served_idx: np.ndarray,
) -> RouteMetrics:
    n = int(pred_idx.shape[0])
    if n == 0:
        return RouteMetrics(0, 0.0, 0.0, 0.0, 0.0)
    agreement = float(np.mean(pred_idx == target_idx))
    critical = target_idx >= _CRITICAL_IDX
    if critical.any():
        under = float(np.mean(pred_idx[critical] < target_idx[critical]))
    else:
        under = 0.0
    return RouteMetrics(
        n=n,
        agreement=agreement,
        critical_under_routing_rate=under,
        mean_pred_idx=float(np.mean(pred_idx)),
        served_mean_idx=float(np.mean(served_idx)),
    )


def session_holdout_splits(
    dataset: TrainingDataset,
    *,
    holdout_pct: float,
    repeats: int,
    min_size: int,
    seed: int = 0,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Resampled session-level train/holdout index splits.

    Whole sessions go to one side (never split a session) so retrospective
    cross-turn links never leak across the train/holdout boundary. Returns ``[]``
    when there is not enough data/sessions to form a meaningful holdout.
    """

    n = len(dataset)
    sessions = list(dict.fromkeys(dataset.session_keys))
    if n < 2 or len(sessions) < 2:
        return []

    idx_by_session: dict[str, list[int]] = {}
    for i, sk in enumerate(dataset.session_keys):
        idx_by_session.setdefault(sk, []).append(i)

    target_holdout = max(1, int(round(n * holdout_pct)))
    splits: list[tuple[np.ndarray, np.ndarray]] = []
    for r in range(repeats):
        rng = np.random.RandomState(seed + r)
        order = list(sessions)
        rng.shuffle(order)
        hold: list[int] = []
        for sk in order:
            if len(hold) >= target_holdout:
                break
            hold.extend(idx_by_session[sk])
        hold_set = set(hold)
        train = [i for i in range(n) if i not in hold_set]
        if not train or len(hold) < 1:
            continue
        splits.append((np.asarray(train, dtype=np.int64), np.asarray(hold, dtype=np.int64)))

    # Reject the whole CV if even the pooled holdout is below the floor.
    pooled = sum(len(h) for _, h in splits)
    if pooled < min_size:
        return []
    return splits


def cross_validate(dataset: TrainingDataset, *, config: Any) -> dict[str, Any]:
    """Train per fold, predict the held-out rows, return pooled metrics.

    Returns a JSON-able dict (carried through the training subprocess). When the
    data is too small to hold out, returns ``n_holdout=0`` and the promotion gate
    falls back to the golden set.
    """

    from opensquilla.squilla_router.self_learning.train import train_booster_arrays

    served = dataset.served if dataset.served is not None else dataset.y
    splits = session_holdout_splits(
        dataset,
        holdout_pct=float(getattr(config, "holdout_pct", 0.10)),
        repeats=int(getattr(config, "holdout_repeats", 5)),
        min_size=int(getattr(config, "holdout_min_size", 30)),
    )
    if not splits:
        return {
            "n_folds": 0,
            "n_holdout": 0,
            "agreement": None,
            "critical_under_routing_rate": None,
            "mean_pred_idx": None,
            "served_mean_idx": float(np.mean(served)) if len(dataset) else 0.0,
        }

    preds: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    serveds: list[np.ndarray] = []
    for train_idx, hold_idx in splits:
        booster, _ = train_booster_arrays(
            dataset.X[train_idx],
            dataset.y[train_idx],
            dataset.w[train_idx],
            base_model_path=None,
            config=config,
        )
        preds.append(predict_indices(booster, dataset.X[hold_idx]))
        targets.append(dataset.y[hold_idx])
        serveds.append(served[hold_idx])

    metrics = route_metrics(
        np.concatenate(preds), np.concatenate(targets), np.concatenate(serveds)
    )
    out = metrics.to_dict()
    out["n_folds"] = len(splits)
    out["n_holdout"] = metrics.n
    return out


def evaluate_golden(booster, golden_path: Path) -> RouteMetrics | None:
    """Evaluate a booster on a frozen golden ``.npz`` (keys ``X``, ``y``)."""

    golden_path = Path(golden_path)
    if not golden_path.is_file():
        return None
    data = np.load(golden_path, allow_pickle=True)
    x = data["X"].astype(np.float32)
    y = data["y"].astype(np.int64)
    pred = predict_indices(booster, x)
    return route_metrics(pred, y, y)


@dataclass
class PromotionDecision:
    promote: bool
    reason: str
    metrics: dict[str, Any]


def decide_promotion(
    cv: dict[str, Any],
    *,
    golden: RouteMetrics | None,
    baseline_golden: RouteMetrics | None,
    config: Any,
) -> PromotionDecision:
    """Apply the AND gate over CV (progress) and golden (regression) metrics."""

    tol = float(getattr(config, "cost_tolerance_pct", 5.0)) / 100.0
    max_under = float(getattr(config, "max_critical_under_routing", 0.30))
    min_size = int(getattr(config, "holdout_min_size", 30))
    failures: list[str] = []
    have_cv = cv.get("agreement") is not None and int(cv.get("n_holdout", 0)) >= min_size

    if not have_cv and golden is None:
        return PromotionDecision(False, "insufficient_eval", {"cv": cv})

    if have_cv:
        if cv["critical_under_routing_rate"] > max_under:
            failures.append("quality_regression")
        cost_ceiling = cv["served_mean_idx"] * (1.0 + tol) + 0.05
        if cv["mean_pred_idx"] > cost_ceiling:
            failures.append("cost_regression")

    if golden is not None:
        if baseline_golden is not None:
            if golden.agreement < baseline_golden.agreement - 1e-6:
                failures.append("golden_quality_regression")
            if golden.mean_pred_idx > baseline_golden.mean_pred_idx * (1.0 + tol) + 0.05:
                failures.append("golden_cost_regression")
        else:
            min_golden = float(getattr(config, "min_golden_agreement", 0.5))
            if golden.agreement < min_golden:
                failures.append("golden_below_floor")

    metrics = {"cv": cv, "golden": golden.to_dict() if golden else None}
    if failures:
        return PromotionDecision(False, ";".join(failures), metrics)
    return PromotionDecision(True, "ok", metrics)
