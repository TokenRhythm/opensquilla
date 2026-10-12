"""Per-session token usage tracking and cost estimation."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import ClassVar, cast

from .pricing import CostEstimate, estimate_cost, lookup_price, resolve_model_price

_current_usage_scope: ContextVar[str | None] = ContextVar(
    "opensquilla_usage_scope",
    default=None,
)


@contextmanager
def usage_scope(scope_key: str | None) -> Iterator[None]:
    """Attribute UsageTracker.add calls in this context to scope_key."""
    if not scope_key:
        yield
        return
    token = _current_usage_scope.set(scope_key)
    try:
        yield
    finally:
        _current_usage_scope.reset(token)


@dataclass
class ModelUsage:
    """Token usage for a single model within a session."""

    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # Provider-billed cost accumulated across every raw provider call attributed
    # to this model. New field appended at the end so existing positional
    # callers (ModelUsage(model_id, in, out)) continue to align. When > 0 the
    # model_breakdown serializer prefers this over the pricing-table estimate,
    # avoiding cache-discount drift in the per-model split.
    billed_cost: float = 0.0
    # Configured provider id (e.g. "ollama"). Appended at the end to keep
    # existing positional ModelUsage(model_id, in, out) callers aligned. Local
    # providers are free, so the estimate must not apply the cloud default.
    provider: str = ""
    # UNBILLED token buckets: accumulated only for calls without an explicit
    # provider-billed source/receipt. Pure counters — pricing stays lazy in the
    # cost properties/serializers so ``add()`` never does a price lookup (no
    # network on the event loop, and per-turn deltas stay exact via clone
    # subtraction). Appended at the end for positional-caller safety.
    unbilled_input_tokens: int = 0
    unbilled_output_tokens: int = 0
    unbilled_cache_read_tokens: int = 0
    unbilled_cache_write_tokens: int = 0
    # Receipt presence is deliberately tracked separately from the amount.
    # A provider-confirmed free request has a real receipt whose billed cost is
    # exactly zero and must not be re-priced as an unbilled request.
    provider_billed_entries: int = 0
    unbilled_entries: int = 0

    @property
    def cost(self) -> float:
        """Cache-aware pricing-table estimate over ALL of this model's tokens.

        Billed-blind by design: this is the "what would it cost at list
        price" figure. ``total_cost`` mixes in real billed data instead.
        """
        price = lookup_price(self.model_id, self.provider)
        return estimate_cost(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            price=price,
        ).cost_usd

    def unbilled_estimate(self) -> CostEstimate:
        """Cache-aware estimate of only the unbilled token buckets."""
        price = lookup_price(self.model_id, self.provider)
        return estimate_cost(
            input_tokens=self.unbilled_input_tokens,
            output_tokens=self.unbilled_output_tokens,
            cache_read_tokens=self.unbilled_cache_read_tokens,
            cache_write_tokens=self.unbilled_cache_write_tokens,
            price=price,
        )

    @property
    def total_cost(self) -> float:
        """Canonical cost: real billed spend plus the estimate of unbilled calls."""
        return max(0.0, float(self.billed_cost or 0.0)) + self.unbilled_estimate().cost_usd


@dataclass
class SessionUsage:
    """Accumulated token usage and cost for a single session."""

    input_tokens: int = 0
    output_tokens: int = 0
    model_id: str = ""
    _per_model: dict[str, ModelUsage] | None = None
    # New cache counters appended at the end so existing positional callers
    # (e.g. SessionUsage(1, 2, "model")) keep aligning with `model_id`.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    provider: str = ""
    # Additive exact-attribution index. The legacy ``_per_model`` map remains
    # intact for compatibility, while this map prevents equal model ids on
    # different providers from collapsing into one usage/cost row.
    _per_deployment: dict[tuple[str, str], ModelUsage] | None = None
    provider_billed_entries: int = 0
    unbilled_entries: int = 0

    def _cost_entries(self) -> dict[object, ModelUsage] | None:
        if self._per_deployment:
            return cast(dict[object, ModelUsage], self._per_deployment)
        if self._per_model:
            return cast(dict[object, ModelUsage], self._per_model)
        return None

    @property
    def cost(self) -> float:
        """Cache-aware pricing-table estimate over the session's tokens."""
        entries = self._cost_entries()
        if entries:
            return sum(m.cost for m in entries.values())
        price = lookup_price(self.model_id, self.provider)
        return estimate_cost(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            price=price,
        ).cost_usd

    @property
    def billed_cost(self) -> float:
        """Sum of provider-billed cost across every model in this session.

        Returns 0.0 when no per-model billed data has been captured (e.g.
        provider returned no cost, or session is estimate-only). Callers
        use this to decide whether the session-level row should display
        the actual billed total or fall back to the pricing-table estimate.
        """
        entries = self._cost_entries()
        if not entries:
            return 0.0
        return sum(float(getattr(m, "billed_cost", 0.0) or 0.0) for m in entries.values())

    @property
    def total_cost(self) -> float:
        """Best per-session cost: real billed spend plus estimated unbilled spend.

        Mixed-source sessions need this so the row total doesn't under-report
        the unbilled portion. Each model contributes its billed total plus the
        cache-aware estimate of its unbilled token buckets — a model mixing
        billed and unbilled calls no longer collapses to billed-only. Sum
        equals the breakdown's per-model ``costUsd`` sum by construction
        (since the breakdown serializer makes the same per-model decision).
        """
        entries = self._cost_entries()
        if not entries:
            return self.cost
        return sum(m.total_cost for m in entries.values())

    @property
    def cost_source(self) -> str:
        """Aggregate cost source for the session row.

        - ``provider_billed``: every per-model entry has a real billed total.
        - ``mixed``: some models billed, others estimate-only.
        - ``opensquilla_estimate``: no billed data at all, or provider returned
          no cost for any call.
        """
        entries = self._cost_entries()
        if not entries:
            if self.provider_billed_entries and self.unbilled_entries:
                return "mixed"
            if self.provider_billed_entries:
                return "provider_billed"
            return "opensquilla_estimate"
        billed_count = sum(max(0, m.provider_billed_entries) for m in entries.values())
        unbilled_count = sum(max(0, m.unbilled_entries) for m in entries.values())
        if billed_count == 0:
            return "opensquilla_estimate"
        if unbilled_count == 0:
            return "provider_billed"
        return "mixed"

    def add(
        self,
        input_tokens: int,
        output_tokens: int,
        model_id: str = "",
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        billed_cost: float = 0.0,
        provider: str = "",
        cost_source: str | None = None,
    ) -> None:
        """Accumulate token counts, tracking per-model breakdown.

        ``billed_cost`` is the provider-reported real billed cost for this
        accumulation (typically one provider call). Forwarded into the per-model
        ``ModelUsage`` so the breakdown serializer can return the actual billed
        figure instead of the cache-blind pricing-table estimate.

        ``provider`` is the configured provider id; it lets local providers
        (Ollama, …) estimate as free instead of the cloud default price.
        """
        normalized_source = str(cost_source or "").strip().lower()
        provider_billed = normalized_source in {"provider_billed", "openrouter_usage"}
        mixed_source = normalized_source == "mixed"
        if normalized_source in {"", "none"}:
            # Compatibility for existing callers that predate explicit source
            # propagation. DoneEvent historically defaulted this field to the
            # literal "none", so positive legacy bills need the same bridge.
            # New provider paths carry an explicit source/receipt, including
            # confirmed zero-cost requests.
            provider_billed = billed_cost > 0.0

        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_read_tokens += cache_read_tokens
        self.cache_write_tokens += cache_write_tokens
        if provider:
            self.provider = provider
        if provider_billed or mixed_source:
            self.provider_billed_entries += 1
        if not provider_billed or mixed_source:
            self.unbilled_entries += 1
        mid = model_id or self.model_id
        if mid:
            def accumulate(mu: ModelUsage) -> None:
                mu.input_tokens += input_tokens
                mu.output_tokens += output_tokens
                mu.cache_read_tokens += cache_read_tokens
                mu.cache_write_tokens += cache_write_tokens
                mu.billed_cost += billed_cost
                if provider_billed or mixed_source:
                    mu.provider_billed_entries += 1
                if not provider_billed or mixed_source:
                    mu.unbilled_entries += 1
                if not provider_billed and not mixed_source:
                    # Pure counters only — pricing stays lazy in the cost
                    # properties/serializers so add() never blocks the event loop.
                    mu.unbilled_input_tokens += input_tokens
                    mu.unbilled_output_tokens += output_tokens
                    mu.unbilled_cache_read_tokens += cache_read_tokens
                    mu.unbilled_cache_write_tokens += cache_write_tokens
                if provider:
                    mu.provider = provider

            if self._per_model is None:
                self._per_model = {}
            mu = self._per_model.get(mid)
            if mu is None:
                mu = ModelUsage(model_id=mid)
                self._per_model[mid] = mu
            accumulate(mu)

            if self._per_deployment is None:
                self._per_deployment = {}
            deployment_key = (str(provider or ""), mid)
            deployment_usage = self._per_deployment.get(deployment_key)
            if deployment_usage is None:
                deployment_usage = ModelUsage(model_id=mid, provider=str(provider or ""))
                self._per_deployment[deployment_key] = deployment_usage
            accumulate(deployment_usage)

    @staticmethod
    def _breakdown_cost_fields(mu_or_self: ModelUsage | SessionUsage) -> dict:
        """Pick the canonical cost + source for a single breakdown row.

        Billed spend is the source of truth where present; unbilled calls
        contribute the cache-aware estimate of their token buckets. This is
        what lets the WebUI show per-model values that actually sum to the
        row total without prorating.
        """
        receipt_count = int(getattr(mu_or_self, "provider_billed_entries", 0) or 0)
        usage_entry_count = receipt_count + int(
            getattr(mu_or_self, "unbilled_entries", 0) or 0
        )
        fields = model_usage_cost_fields(
            model_id=getattr(mu_or_self, "model_id", ""),
            input_tokens=int(getattr(mu_or_self, "input_tokens", 0) or 0),
            output_tokens=int(getattr(mu_or_self, "output_tokens", 0) or 0),
            billed_cost=float(getattr(mu_or_self, "billed_cost", 0.0) or 0.0),
            provider=str(getattr(mu_or_self, "provider", "") or ""),
            cache_read_tokens=int(getattr(mu_or_self, "cache_read_tokens", 0) or 0),
            cache_write_tokens=int(getattr(mu_or_self, "cache_write_tokens", 0) or 0),
            unbilled_input_tokens=getattr(mu_or_self, "unbilled_input_tokens", None),
            unbilled_output_tokens=getattr(mu_or_self, "unbilled_output_tokens", None),
            unbilled_cache_read_tokens=getattr(mu_or_self, "unbilled_cache_read_tokens", None),
            unbilled_cache_write_tokens=getattr(mu_or_self, "unbilled_cache_write_tokens", None),
            # ``None`` retains inference for ModelUsage instances constructed
            # by older clients/tests before entry counters existed.
            has_billed_receipt=(receipt_count > 0) if usage_entry_count else None,
        )
        return {
            "costUsd": fields["costUsd"],
            "billedCostUsd": fields["billedCostUsd"],
            "estimatedCostUsd": fields["estimatedCostUsd"],
            "costSource": fields["costSource"],
            # Additive provenance keys (dual-case like the other cost fields).
            "estimateBasis": fields["estimateBasis"],
            "estimate_basis": fields["estimate_basis"],
            "priceSource": fields["priceSource"],
            "price_source": fields["price_source"],
        }

    @property
    def model_breakdown(self) -> list[dict]:
        """Per-model usage breakdown for RPC serialisation."""
        if not self._per_model:
            if self.model_id:
                return [
                    {
                        "model": self.model_id,
                        "inputTokens": self.input_tokens,
                        "outputTokens": self.output_tokens,
                        "cacheReadTokens": self.cache_read_tokens,
                        "cacheWriteTokens": self.cache_write_tokens,
                        **SessionUsage._breakdown_cost_fields(self),
                    }
                ]
            return []
        return [
            {
                "model": mu.model_id,
                "inputTokens": mu.input_tokens,
                "outputTokens": mu.output_tokens,
                "cacheReadTokens": mu.cache_read_tokens,
                "cacheWriteTokens": mu.cache_write_tokens,
                **SessionUsage._breakdown_cost_fields(mu),
            }
            # Sort by the canonical cost (billed plus estimate-of-unbilled) so
            # the row order stays predictable even when some models lack
            # billed data.
            for mu in sorted(
                self._per_model.values(),
                key=lambda m: m.total_cost,
                reverse=True,
            )
        ]

    @property
    def deployment_breakdown(self) -> list[dict]:
        """Usage grouped by exact ``(provider, model)`` deployment identity."""
        if not self._per_deployment:
            if not self.model_id:
                return []
            return [
                {
                    "provider": self.provider,
                    "model": self.model_id,
                    "inputTokens": self.input_tokens,
                    "outputTokens": self.output_tokens,
                    "cacheReadTokens": self.cache_read_tokens,
                    "cacheWriteTokens": self.cache_write_tokens,
                    **SessionUsage._breakdown_cost_fields(self),
                }
            ]
        return [
            {
                "provider": mu.provider,
                "model": mu.model_id,
                "inputTokens": mu.input_tokens,
                "outputTokens": mu.output_tokens,
                "cacheReadTokens": mu.cache_read_tokens,
                "cacheWriteTokens": mu.cache_write_tokens,
                **SessionUsage._breakdown_cost_fields(mu),
            }
            for mu in sorted(
                self._per_deployment.values(),
                key=lambda item: (item.total_cost, item.provider, item.model_id),
                reverse=True,
            )
        ]


def _clone_session_usage(usage: SessionUsage) -> SessionUsage:
    clone = SessionUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        model_id=usage.model_id,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        provider=usage.provider,
        provider_billed_entries=usage.provider_billed_entries,
        unbilled_entries=usage.unbilled_entries,
    )
    if usage._per_model:
        clone._per_model = {
            mid: ModelUsage(
                model_id=mu.model_id,
                input_tokens=mu.input_tokens,
                output_tokens=mu.output_tokens,
                cache_read_tokens=mu.cache_read_tokens,
                cache_write_tokens=mu.cache_write_tokens,
                billed_cost=mu.billed_cost,
                provider=mu.provider,
                unbilled_input_tokens=mu.unbilled_input_tokens,
                unbilled_output_tokens=mu.unbilled_output_tokens,
                unbilled_cache_read_tokens=mu.unbilled_cache_read_tokens,
                unbilled_cache_write_tokens=mu.unbilled_cache_write_tokens,
                provider_billed_entries=mu.provider_billed_entries,
                unbilled_entries=mu.unbilled_entries,
            )
            for mid, mu in usage._per_model.items()
        }
    if usage._per_deployment:
        clone._per_deployment = {
            deployment: ModelUsage(
                model_id=mu.model_id,
                input_tokens=mu.input_tokens,
                output_tokens=mu.output_tokens,
                cache_read_tokens=mu.cache_read_tokens,
                cache_write_tokens=mu.cache_write_tokens,
                billed_cost=mu.billed_cost,
                provider=mu.provider,
                unbilled_input_tokens=mu.unbilled_input_tokens,
                unbilled_output_tokens=mu.unbilled_output_tokens,
                unbilled_cache_read_tokens=mu.unbilled_cache_read_tokens,
                unbilled_cache_write_tokens=mu.unbilled_cache_write_tokens,
                provider_billed_entries=mu.provider_billed_entries,
                unbilled_entries=mu.unbilled_entries,
            )
            for deployment, mu in usage._per_deployment.items()
        }
    return clone


def _model_delta_cost(
    *,
    model_id: str,
    billed_cost: float,
    provider: str = "",
    unbilled_input_tokens: int = 0,
    unbilled_output_tokens: int = 0,
    unbilled_cache_read_tokens: int = 0,
    unbilled_cache_write_tokens: int = 0,
) -> float:
    """Cost of one model's per-turn delta: billed delta plus the cache-aware
    estimate of the unbilled delta. A billed call no longer collapses the
    same model's unbilled calls to $0."""
    estimate = estimate_cost(
        input_tokens=unbilled_input_tokens,
        output_tokens=unbilled_output_tokens,
        cache_read_tokens=unbilled_cache_read_tokens,
        cache_write_tokens=unbilled_cache_write_tokens,
        price=resolve_model_price(model_id, provider).entry,
    )
    return max(0.0, float(billed_cost or 0.0)) + estimate.cost_usd


def model_usage_cost_fields(
    *,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    billed_cost: float,
    provider: str = "",
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    unbilled_input_tokens: int | None = None,
    unbilled_output_tokens: int | None = None,
    unbilled_cache_read_tokens: int | None = None,
    unbilled_cache_write_tokens: int | None = None,
    has_billed_receipt: bool | None = None,
) -> dict[str, float | str | None]:
    """Return canonical cost fields for a per-model usage row.

    Provider-billed cost remains the source of truth for billed calls; the
    unbilled token buckets are priced with the cache-aware estimator at the
    layered-resolver price, so ``costUsd = billed + estimate`` and a model
    mixing billed and unbilled calls reports ``mixed`` instead of collapsing
    to billed-only.

    When the caller has no per-call billed/unbilled split (all ``unbilled_*``
    left as ``None``), the split is inferred from ``billed_cost``: a billed
    row's tokens are covered by the billed figure, an unbilled row estimates
    everything.

    Additive provenance keys: ``estimateBasis``/``estimate_basis`` disclose
    the estimator's quality label (``None`` when nothing was estimated) and
    ``priceSource``/``price_source`` name the resolver layer that priced the
    model.
    """

    billed = max(0.0, float(billed_cost or 0.0))
    has_billed = billed > 0.0 if has_billed_receipt is None else bool(has_billed_receipt)
    if unbilled_input_tokens is None:
        if has_billed:
            unb_input = unb_output = unb_read = unb_write = 0
        else:
            unb_input = max(0, int(input_tokens or 0))
            unb_output = max(0, int(output_tokens or 0))
            unb_read = max(0, int(cache_read_tokens or 0))
            unb_write = max(0, int(cache_write_tokens or 0))
    else:
        unb_input = max(0, int(unbilled_input_tokens or 0))
        unb_output = max(0, int(unbilled_output_tokens or 0))
        unb_read = max(0, int(unbilled_cache_read_tokens or 0))
        unb_write = max(0, int(unbilled_cache_write_tokens or 0))

    resolved = resolve_model_price(model_id, provider)
    est = estimate_cost(
        input_tokens=unb_input,
        output_tokens=unb_output,
        cache_read_tokens=unb_read,
        cache_write_tokens=unb_write,
        price=resolved.entry,
    )
    estimate = est.cost_usd
    cost = billed + estimate
    if has_billed and estimate > 0.0:
        source = "mixed"
    elif has_billed:
        source = "provider_billed"
    elif estimate > 0.0:
        source = "opensquilla_estimate"
    else:
        source = "unavailable"
    estimate_basis = est.basis if (unb_input or unb_output or unb_read or unb_write) else None

    rounded_cost = round(cost, 6)
    rounded_billed = round(billed, 6)
    rounded_estimate = round(estimate, 6)
    return {
        "costUsd": rounded_cost,
        "cost_usd": rounded_cost,
        "billedCostUsd": rounded_billed,
        "billed_cost_usd": rounded_billed,
        "estimatedCostUsd": rounded_estimate,
        "estimated_cost_usd": rounded_estimate,
        "costSource": source,
        "cost_source": source,
        "estimateBasis": estimate_basis,
        "estimate_basis": estimate_basis,
        "priceSource": resolved.source,
        "price_source": resolved.source,
    }


@dataclass
class SessionTotalsSnapshot:
    """Point-in-time aggregate of a session's token usage and cost.

    Embedded in `DoneEvent` so consumers do not need a follow-up
    `usage.status` RPC to render session totals. `None` on `DoneEvent`
    means "no snapshot available" (legacy replay), distinct from a
    populated snapshot whose numeric fields happen to be zero.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    billed_cost: float = 0.0

    # Internal provenance accompanies the in-process snapshot without changing
    # its long-standing six-field dataclass/wire shape.  ClassVars are ignored
    # by dataclasses.asdict(); individual snapshots shadow them via setattr.
    cost_source: ClassVar[str] = "none"
    provider_billed_entries: ClassVar[int] = 0
    unbilled_entries: ClassVar[int] = 0

    @classmethod
    def from_session(cls, usage: SessionUsage) -> SessionTotalsSnapshot:
        snapshot = cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_usd=usage.total_cost,
            billed_cost=usage.billed_cost,
        )
        setattr(snapshot, "cost_source", usage.cost_source)
        setattr(snapshot, "provider_billed_entries", usage.provider_billed_entries)
        setattr(snapshot, "unbilled_entries", usage.unbilled_entries)
        return snapshot


class UsageTracker:
    """Tracks per-session token usage and cost."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionUsage] = {}
        self._scopes: dict[tuple[str, str], SessionUsage] = {}

    def add(
        self,
        session_key: str,
        input_tokens: int,
        output_tokens: int,
        model_id: str = "",
        *,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        billed_cost: float = 0.0,
        provider: str = "",
        cost_source: str | None = None,
    ) -> None:
        """Record token usage for a session.

        ``billed_cost`` flows through to :py:attr:`ModelUsage.billed_cost` so
        the per-model breakdown can report real provider-billed figures
        instead of the cache-blind pricing-table estimate. ``provider`` lets
        local runtimes (Ollama, …) estimate as free.

        Invariant: keep this method synchronous (no await/yield). On the
        single event loop a sync ``add`` is atomic, so concurrent turns in
        one session accumulate without interleaving or a lock.
        """
        usage = self._sessions.get(session_key)
        if usage is None:
            usage = SessionUsage(model_id=model_id)
            self._sessions[session_key] = usage
        usage.add(
            input_tokens,
            output_tokens,
            model_id=model_id,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            billed_cost=billed_cost,
            provider=provider,
            cost_source=cost_source,
        )
        if model_id:
            usage.model_id = model_id
        scope_key = _current_usage_scope.get()
        if scope_key:
            scoped = self._scopes.get((session_key, scope_key))
            if scoped is None:
                scoped = SessionUsage(model_id=model_id)
                self._scopes[(session_key, scope_key)] = scoped
            scoped.add(
                input_tokens,
                output_tokens,
                model_id=model_id,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                provider=provider,
                billed_cost=billed_cost,
                cost_source=cost_source,
            )
            if model_id:
                scoped.model_id = model_id

    def get(self, session_key: str) -> SessionUsage | None:
        """Return accumulated usage for a session, or None."""
        return self._sessions.get(session_key)

    def session_checkpoint(self, session_key: str) -> SessionUsage | None:
        """Return an immutable-enough copy for later per-turn delta accounting."""
        usage = self._sessions.get(session_key)
        if usage is None:
            return None
        return _clone_session_usage(usage)

    def get_scope(self, session_key: str, scope_key: str) -> SessionUsage | None:
        """Return accumulated usage for a session within one attribution scope."""
        return self._scopes.get((session_key, scope_key))

    def session_snapshot(self, session_key: str) -> SessionTotalsSnapshot | None:
        """Return the current SessionTotalsSnapshot for *session_key*, or None if unknown."""
        usage = self._sessions.get(session_key)
        if usage is None:
            return None
        return SessionTotalsSnapshot.from_session(usage)

    def session_delta_snapshot(
        self,
        session_key: str,
        checkpoint: SessionUsage | None,
    ) -> SessionTotalsSnapshot | None:
        """Return usage added since *checkpoint*.

        Cost is computed from per-model deltas instead of subtracting two
        session totals, because a later provider-billed call can change a
        model's aggregate cost source from estimate to billed.
        """
        usage = self._sessions.get(session_key)
        if usage is None:
            return None
        input_tokens = usage.input_tokens - (checkpoint.input_tokens if checkpoint else 0)
        output_tokens = usage.output_tokens - (checkpoint.output_tokens if checkpoint else 0)
        cache_read_tokens = usage.cache_read_tokens - (
            checkpoint.cache_read_tokens if checkpoint else 0
        )
        cache_write_tokens = usage.cache_write_tokens - (
            checkpoint.cache_write_tokens if checkpoint else 0
        )
        billed_cost = usage.billed_cost - (checkpoint.billed_cost if checkpoint else 0.0)
        billed_entries = usage.provider_billed_entries - (
            checkpoint.provider_billed_entries if checkpoint else 0
        )
        unbilled_entries = usage.unbilled_entries - (
            checkpoint.unbilled_entries if checkpoint else 0
        )
        cost_usd = 0.0

        usage_entries = usage._cost_entries()
        if usage_entries:
            before_entries = checkpoint._cost_entries() if checkpoint else None
            for deployment, mu in usage_entries.items():
                before = before_entries.get(deployment) if before_entries else None
                delta_billed = mu.billed_cost - (before.billed_cost if before else 0.0)
                delta_unb_input = mu.unbilled_input_tokens - (
                    before.unbilled_input_tokens if before else 0
                )
                delta_unb_output = mu.unbilled_output_tokens - (
                    before.unbilled_output_tokens if before else 0
                )
                delta_unb_read = mu.unbilled_cache_read_tokens - (
                    before.unbilled_cache_read_tokens if before else 0
                )
                delta_unb_write = mu.unbilled_cache_write_tokens - (
                    before.unbilled_cache_write_tokens if before else 0
                )
                delta_billed_entries = mu.provider_billed_entries - (
                    before.provider_billed_entries if before else 0
                )
                delta_unbilled_entries = mu.unbilled_entries - (
                    before.unbilled_entries if before else 0
                )
                if (
                    delta_billed
                    or delta_unb_input
                    or delta_unb_output
                    or delta_unb_read
                    or delta_unb_write
                    or delta_billed_entries
                    or delta_unbilled_entries
                ):
                    cost_usd += _model_delta_cost(
                        model_id=mu.model_id,
                        billed_cost=max(0.0, delta_billed),
                        provider=mu.provider,
                        unbilled_input_tokens=max(0, delta_unb_input),
                        unbilled_output_tokens=max(0, delta_unb_output),
                        unbilled_cache_read_tokens=max(0, delta_unb_read),
                        unbilled_cache_write_tokens=max(0, delta_unb_write),
                    )
        else:
            # No per-model split (model_id never provided): infer the
            # billed/unbilled split from the billed delta, matching
            # model_usage_cost_fields' legacy-caller behavior.
            billed_delta = max(0.0, billed_cost)
            fully_billed = billed_entries > 0 and unbilled_entries == 0
            cost_usd = _model_delta_cost(
                model_id=usage.model_id,
                billed_cost=billed_delta,
                provider=usage.provider,
                unbilled_input_tokens=0 if fully_billed else max(0, input_tokens),
                unbilled_output_tokens=0 if fully_billed else max(0, output_tokens),
                unbilled_cache_read_tokens=0 if fully_billed else max(0, cache_read_tokens),
                unbilled_cache_write_tokens=(
                    0 if fully_billed else max(0, cache_write_tokens)
                ),
            )

        if billed_entries > 0 and unbilled_entries > 0:
            delta_cost_source = "mixed"
        elif billed_entries > 0:
            delta_cost_source = "provider_billed"
        elif cost_usd > 0.0:
            delta_cost_source = "opensquilla_estimate"
        else:
            delta_cost_source = "unavailable"

        snapshot = SessionTotalsSnapshot(
            input_tokens=max(0, input_tokens),
            output_tokens=max(0, output_tokens),
            cache_read_tokens=max(0, cache_read_tokens),
            cache_write_tokens=max(0, cache_write_tokens),
            cost_usd=max(0.0, cost_usd),
            billed_cost=max(0.0, billed_cost),
        )
        setattr(snapshot, "cost_source", delta_cost_source)
        setattr(snapshot, "provider_billed_entries", max(0, billed_entries))
        setattr(snapshot, "unbilled_entries", max(0, unbilled_entries))
        return snapshot

    def get_cost(self, session_key: str) -> float:
        """Return accumulated cost in USD for a session."""
        usage = self._sessions.get(session_key)
        if usage is None:
            return 0.0
        return usage.cost

    def format_usage(self, session_key: str) -> str:
        """Human-readable usage summary for a session."""
        usage = self._sessions.get(session_key)
        if usage is None:
            return "Tokens: 0 in / 0 out | Cost: $0.00"
        return (
            f"Tokens: {usage.input_tokens:,} in / {usage.output_tokens:,} out "
            f"| Cost: ${usage.cost:,.4f}"
        )

    def total_cost(self) -> float:
        """Sum of costs across all sessions."""
        return sum(u.cost for u in self._sessions.values())

    def all_sessions(self) -> dict[str, SessionUsage]:
        """Return all tracked sessions."""
        return dict(self._sessions)

    def check_warning(self, session_key: str, threshold: float = 5.0) -> str | None:
        """Return a warning if session cost exceeds threshold, else None."""
        usage = self._sessions.get(session_key)
        if usage is None:
            return None
        if usage.cost >= threshold:
            return f"Session cost ${usage.cost:,.2f} has exceeded the ${threshold:,.2f} threshold."
        return None
