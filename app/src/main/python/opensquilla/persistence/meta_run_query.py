"""Read-side helpers for meta-skill run history queries."""

from __future__ import annotations

import time


def parse_since_ms(value: str | None, *, now_ms: int | None = None) -> int | None:
    """Parse a compact relative time like ``5m``, ``24h``, or ``7d``."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    unit = text[-1]
    if unit not in "hHdDmM":
        raise ValueError("--since must end in m/h/d (e.g., 5m, 24h, 7d)")
    try:
        n = int(text[:-1])
    except ValueError as exc:
        raise ValueError("--since amount must be an integer") from exc
    base_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if unit in "hH":
        return base_ms - n * 3600 * 1000
    if unit in "dD":
        return base_ms - n * 86400 * 1000
    return base_ms - n * 60 * 1000
