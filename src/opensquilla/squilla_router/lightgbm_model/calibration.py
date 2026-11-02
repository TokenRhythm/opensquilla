"""Exact temperature calibration arithmetic extracted from training/common.py."""
import math
from typing import cast

import numpy as np


class TrainingError(ValueError):
    """Invalid calibration inputs."""

def _as_logits(values: object) -> np.ndarray:
    logits = np.asarray(values, dtype=np.float64)
    if logits.ndim != 2 or logits.shape[0] == 0 or logits.shape[1] < 2:
        raise TrainingError(
            f"logits must have shape [rows, classes], got {logits.shape}"
        )
    if not np.isfinite(logits).all():
        raise TrainingError("logits contain non-finite values")
    return logits

def _log_softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - np.max(logits, axis=1, keepdims=True)
    return cast(np.ndarray, shifted - np.log(np.exp(shifted).sum(axis=1, keepdims=True)))

def probabilities_to_logits(probabilities: object) -> np.ndarray:
    values = np.asarray(probabilities, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] < 2:
        raise TrainingError("probabilities must have shape [rows, classes]")
    if not np.isfinite(values).all() or np.any(values < 0):
        raise TrainingError("probabilities must be finite and non-negative")
    row_sums = values.sum(axis=1, keepdims=True)
    if np.any(row_sums <= 0):
        raise TrainingError("each probability row must have positive mass")
    normalized = values / row_sums
    return cast(np.ndarray, np.log(np.clip(normalized, 1e-12, 1.0)))

def apply_temperature(logits: object, temperature: float) -> np.ndarray:
    matrix = _as_logits(logits)
    if not math.isfinite(temperature) or temperature <= 0.0:
        raise TrainingError("temperature must be finite and positive")
    return cast(np.ndarray, np.exp(_log_softmax(matrix / temperature)))
