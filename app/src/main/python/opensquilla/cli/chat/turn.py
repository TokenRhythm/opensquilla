"""Shared chat turn result and usage models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opensquilla.engine.usage import SessionTotalsSnapshot

__all__ = [
    "TurnResult",
    "UsageCounter",
    "UsageSummary",
]


@dataclass
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    billed_cost: float = 0.0
    cost_source: str = "none"
    model: str = ""
    aggregate: bool = False
    session_totals: SessionTotalsSnapshot | None = None
    model_usage_breakdown: list[dict[str, Any]] = field(default_factory=list)
    ensemble_trace: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_done_event(cls, event: object) -> UsageSummary:
        raw_breakdown = getattr(event, "model_usage_breakdown", None)
        raw_trace = getattr(event, "ensemble_trace", None)
        return cls(
            input_tokens=int(getattr(event, "input_tokens", 0) or 0),
            output_tokens=int(getattr(event, "output_tokens", 0) or 0),
            reasoning_tokens=int(getattr(event, "reasoning_tokens", 0) or 0),
            cached_tokens=int(getattr(event, "cached_tokens", 0) or 0),
            cost_usd=float(getattr(event, "cost_usd", 0.0) or 0.0),
            billed_cost=float(getattr(event, "billed_cost", 0.0) or 0.0),
            cost_source=str(getattr(event, "cost_source", "none") or "none"),
            model=str(getattr(event, "model", "") or ""),
            model_usage_breakdown=(
                [dict(row) for row in raw_breakdown if isinstance(row, dict)]
                if isinstance(raw_breakdown, list)
                else []
            ),
            ensemble_trace=dict(raw_trace) if isinstance(raw_trace, dict) else {},
        )

    @classmethod
    def from_gateway_payload(cls, payload: dict[str, Any]) -> UsageSummary:
        from opensquilla.engine.usage import SessionTotalsSnapshot  # noqa: PLC0415

        raw_totals = payload.get("session_totals")
        session_totals: SessionTotalsSnapshot | None = None
        if isinstance(raw_totals, dict):
            session_totals = SessionTotalsSnapshot(
                input_tokens=int(raw_totals.get("input_tokens") or 0),
                output_tokens=int(raw_totals.get("output_tokens") or 0),
                cache_read_tokens=int(raw_totals.get("cache_read_tokens") or 0),
                cache_write_tokens=int(raw_totals.get("cache_write_tokens") or 0),
                cost_usd=float(raw_totals.get("cost_usd") or 0.0),
                billed_cost=float(raw_totals.get("billed_cost") or 0.0),
            )
        raw_breakdown = payload.get("model_usage_breakdown")
        if raw_breakdown is None:
            raw_breakdown = payload.get("modelUsageBreakdown")
        raw_trace = payload.get("ensemble_trace")
        if raw_trace is None:
            raw_trace = payload.get("ensembleTrace")
        return cls(
            input_tokens=int(payload.get("input_tokens") or payload.get("inputTokens") or 0),
            output_tokens=int(payload.get("output_tokens") or payload.get("outputTokens") or 0),
            reasoning_tokens=int(
                payload.get("reasoning_tokens") or payload.get("reasoningTokens") or 0
            ),
            cached_tokens=int(payload.get("cached_tokens") or payload.get("cachedTokens") or 0),
            cost_usd=float(payload.get("cost_usd") or payload.get("costUsd") or 0.0),
            billed_cost=float(payload.get("billed_cost") or payload.get("billedCost") or 0.0),
            cost_source=str(
                payload.get("cost_source") or payload.get("costSource") or "none"
            ),
            model=str(payload.get("model") or ""),
            session_totals=session_totals,
            model_usage_breakdown=(
                [dict(row) for row in raw_breakdown if isinstance(row, dict)]
                if isinstance(raw_breakdown, list)
                else []
            ),
            ensemble_trace=dict(raw_trace) if isinstance(raw_trace, dict) else {},
        )

    def has_values(self) -> bool:
        return bool(
            self.input_tokens
            or self.output_tokens
            or self.reasoning_tokens
            or self.cached_tokens
            or self.cost_usd
            or self.billed_cost
            or self.model
        )


@dataclass
class UsageCounter:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, usage: UsageSummary | None) -> None:
        if usage is None:
            return
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.reasoning_tokens += usage.reasoning_tokens
        self.cached_tokens += usage.cached_tokens
        self.cost_usd += usage.cost_usd

    def apply(self, usage: UsageSummary | None) -> None:
        """Update counter from a turn's UsageSummary."""
        if usage is None:
            return
        snapshot = getattr(usage, "session_totals", None)
        if snapshot is not None:
            self.input_tokens = snapshot.input_tokens
            self.output_tokens = snapshot.output_tokens
            self.cached_tokens = snapshot.cache_read_tokens
            self.cost_usd = snapshot.cost_usd
            self.reasoning_tokens += usage.reasoning_tokens
        else:
            self.add(usage)

    def reset(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.reasoning_tokens = 0
        self.cached_tokens = 0
        self.cost_usd = 0.0

    def render(self) -> str:
        total = self.input_tokens + self.output_tokens
        return (
            f"{total:,} tok ({self.input_tokens:,} in / {self.output_tokens:,} out)"
            f" · cache {self.cached_tokens:,}"
            f" · ${self.cost_usd:.6f}"
        )


@dataclass
class TurnResult:
    text: str = ""
    usage: UsageSummary | None = None
    error: str | None = None
    cancelled: bool = False
    artifacts: list[dict[str, Any]] | None = None
    model_after: str | None = None
