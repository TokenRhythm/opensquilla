"""In-memory rolling provider-call latency samples for gateway diagnostics.

``ProviderStatsStore`` keeps a bounded per-provider window of provider-call
outcomes (time-to-first-token and total duration) recorded by the turn loop
through the ``provider_call_observer`` seam. It is process-local, stdlib-only,
and never persisted: the data answers "how is this provider behaving right
now" for ``providers.status``, nothing more.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable
from typing import Any, NamedTuple

_MAX_SAMPLES_PER_PROVIDER = 200
# Below this many in-window samples a snapshot is noise, not signal.
_MIN_SNAPSHOT_SAMPLES = 5
# p95 needs more mass than p50 before it stops being a single-outlier readout.
_MIN_P95_SAMPLES = 10


class _CallSample(NamedTuple):
    ts: float
    model: str
    ttft_ms: int | None
    duration_ms: int
    ok: bool
    failure_kind: str


def _percentile(sorted_values: list[int], fraction: float) -> int:
    """Nearest-rank percentile over an already-sorted non-empty list."""
    index = max(0, math.ceil(fraction * len(sorted_values)) - 1)
    return sorted_values[min(index, len(sorted_values) - 1)]


class ProviderStatsStore:
    """Bounded per-provider deque of recent provider-call samples."""

    def __init__(self, *, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._samples: dict[str, deque[_CallSample]] = {}

    def record(
        self,
        *,
        provider_id: str,
        model: str,
        ttft_ms: int | None,
        duration_ms: int,
        ok: bool,
        failure_kind: str = "",
    ) -> None:
        if not provider_id:
            return
        bucket = self._samples.get(provider_id)
        if bucket is None:
            bucket = deque(maxlen=_MAX_SAMPLES_PER_PROVIDER)
            self._samples[provider_id] = bucket
        bucket.append(
            _CallSample(
                ts=self._now(),
                model=model,
                ttft_ms=ttft_ms,
                duration_ms=duration_ms,
                ok=ok,
                failure_kind=failure_kind,
            )
        )

    def snapshot(
        self,
        provider_id: str,
        *,
        window_seconds: float = 3600.0,
    ) -> dict[str, Any] | None:
        """Latency snapshot over the recent window, or ``None`` on thin data.

        TTFT percentiles are computed only over samples that carry a
        ``ttft_ms``; ``p95TtftMs`` additionally stays ``None`` until the
        window holds enough samples for the tail to be meaningful.
        """
        bucket = self._samples.get(provider_id)
        if not bucket:
            return None
        cutoff = self._now() - window_seconds
        recent = [sample for sample in bucket if sample.ts >= cutoff]
        if len(recent) < _MIN_SNAPSHOT_SAMPLES:
            return None
        ttfts = sorted(
            sample.ttft_ms for sample in recent if sample.ttft_ms is not None
        )
        p50 = _percentile(ttfts, 0.50) if ttfts else None
        p95 = _percentile(ttfts, 0.95) if len(ttfts) >= _MIN_P95_SAMPLES else None
        return {
            "p50TtftMs": p50,
            "p95TtftMs": p95,
            "samples": len(recent),
            "windowMinutes": int(round(window_seconds / 60.0)),
        }
