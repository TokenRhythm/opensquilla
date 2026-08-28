"""Stagger/jitter strategy to prevent thundering-herd on cron tick."""

from __future__ import annotations

import hashlib
import random


def compute_jitter(job_id: str, max_jitter_seconds: float = 30.0) -> float:
    """Compute a deterministic-ish jitter based on job_id hash.

    Uses job_id hash to produce a stable base offset so that restarts
    keep the same rough bucket, then adds a small random component.
    """
    if max_jitter_seconds <= 0:
        return 0.0

    digest = hashlib.md5(job_id.encode(), usedforsecurity=False).digest()
    stable_fraction = int.from_bytes(digest[:4], "big") / (2**32)
    stable_jitter = stable_fraction * max_jitter_seconds * 0.7

    random_jitter = random.uniform(0, max_jitter_seconds * 0.3)

    return min(stable_jitter + random_jitter, max_jitter_seconds)


def spread_jobs(job_ids: list[str], window_seconds: float = 60.0) -> dict[str, float]:
    """Evenly spread a list of jobs across a time window.

    Returns a mapping of job_id → delay_seconds so that all jobs
    fire at distinct, evenly-spaced slots within [0, window_seconds).
    """
    if not job_ids:
        return {}

    n = len(job_ids)
    slot_size = window_seconds / n

    # Sort deterministically so restart produces same spread
    sorted_ids = sorted(job_ids)
    result: dict[str, float] = {}
    for i, job_id in enumerate(sorted_ids):
        base = i * slot_size
        # small random sub-slot wobble (±10% of slot)
        wobble = random.uniform(-slot_size * 0.1, slot_size * 0.1)
        result[job_id] = max(0.0, min(base + wobble, window_seconds - 0.001))

    return result


def jitter_for_minute_boundary(job_id: str, max_seconds: float = 10.0) -> float:
    """Quick jitter for a job that fires every minute.

    Keeps the job within the same minute but avoids the exact boundary.
    """
    return compute_jitter(job_id, max_jitter_seconds=max_seconds)
