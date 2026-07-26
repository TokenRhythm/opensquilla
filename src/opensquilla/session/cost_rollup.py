"""Cost source rollup helpers for session usage aggregates."""

from __future__ import annotations

from typing import Literal

EventCostSource = Literal[
    "provider_billed",
    "opensquilla_estimate",
    "unavailable",
    "mixed",
    "none",
]
SessionCostSource = Literal[
    "provider_billed",
    "opensquilla_estimate",
    "unavailable",
    "mixed",
    "none",
]


def normalize_event_cost_source(
    source: str | None,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float = 0.0,
    billed_cost_usd: float = 0.0,
) -> EventCostSource:
    """Normalize per-turn cost source using final DoneEvent precision."""

    raw = (source or "none").strip().lower()
    if raw == "mixed":
        return "mixed"
    if raw in {"provider_billed", "openrouter_usage"}:
        return "provider_billed"
    if raw == "opensquilla_estimate":
        return "opensquilla_estimate"
    if raw in {"unavailable", "unpriced"}:
        return "unavailable"

    if billed_cost_usd > 0.0 and cost_usd > billed_cost_usd:
        return "mixed"
    if billed_cost_usd > 0.0:
        return "provider_billed"
    if cost_usd > 0.0:
        return "opensquilla_estimate"
    if input_tokens or output_tokens or cache_read_tokens or cache_write_tokens:
        return "unavailable"
    return "none"


def rollup_cost_source(
    *,
    billed_cost_usd: float,
    estimated_cost_component_usd: float,
    missing_cost_entries: int,
    provider_billed_entries: int = 0,
    estimated_cost_entries: int = 0,
) -> SessionCostSource:
    """Classify aggregate session cost provenance from persisted components."""

    has_billed = provider_billed_entries > 0 or billed_cost_usd > 0.0
    has_estimate = estimated_cost_entries > 0 or estimated_cost_component_usd > 0.0
    has_unavailable = missing_cost_entries > 0
    present = sum((has_billed, has_estimate, has_unavailable))

    if present > 1:
        return "mixed"
    if has_billed:
        return "provider_billed"
    if has_estimate:
        return "opensquilla_estimate"
    if has_unavailable:
        return "unavailable"
    return "none"
