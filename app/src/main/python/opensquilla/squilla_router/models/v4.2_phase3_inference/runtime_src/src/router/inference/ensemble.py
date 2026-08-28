from __future__ import annotations

import numpy as np


def fuse_probabilities(
    p_main: np.ndarray, p_mlp: np.ndarray, alpha: np.ndarray
) -> np.ndarray:
    p_main = np.asarray(p_main, dtype=np.float64)
    p_mlp = np.asarray(p_mlp, dtype=np.float64)
    alpha = np.asarray(alpha, dtype=np.float64)

    if p_main.shape != (4,) or p_mlp.shape != (4,) or alpha.shape != (4,):
        raise ValueError("Phase 3 ensemble expects 4-class vectors")
    if not (
        np.isfinite(p_main).all()
        and np.isfinite(p_mlp).all()
        and np.isfinite(alpha).all()
    ):
        raise ValueError("ensemble inputs must be finite")
    if not ((alpha >= 0).all() and (alpha <= 1).all()):
        raise ValueError("alpha must stay within [0, 1]")

    mixed = alpha * p_main + (1.0 - alpha) * p_mlp
    if not np.isfinite(mixed).all():
        raise ValueError("fused probability vector must be finite")
    total = mixed.sum()
    if total <= 0:
        raise ValueError("fused probability mass must be positive")

    return mixed / total
