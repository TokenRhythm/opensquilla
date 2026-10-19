"""Exact, provider-neutral usage facts and immutable valuation inputs.

Token compatibility counters are projections. An absent measurement is unknown,
not zero, and a settlement revision replaces its predecessor rather than adding
another provider call. These types have no persistence or provider dependency.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import Any, Literal

AmountAuthority = Literal["provider_confirmed", "provider_estimate", "local_estimate", "unknown"]


class _FrozenDimensions(dict):
    """A JSON/dataclasses-compatible immutable copy of small dimensions."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("usage dimensions are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable


def exact_decimal(value: Any, *, label: str = "quantity") -> str:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be a finite non-negative decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a decimal") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{label} must be finite and non-negative")
    # Bound untrusted exponents before materializing a decimal string.
    if number.adjusted() > 24 or number.as_tuple().exponent < -30:
        raise ValueError(f"{label} exceeds supported precision/range")
    return format(number, "f")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


@dataclass(frozen=True, slots=True)
class UsageMeter:
    metric: str
    unit: str
    quantity: str | None
    known: bool = True
    dimensions: Mapping[str, str] = field(default_factory=dict)
    source: str = "provider"

    def __post_init__(self) -> None:
        if not self.metric or not self.unit or not self.source:
            raise ValueError("meter metric, unit and source are required")
        if self.known != (self.quantity is not None):
            raise ValueError("unknown meters require quantity=None; known zero must be explicit")
        if self.quantity is not None:
            object.__setattr__(self, "quantity", exact_decimal(self.quantity))
        if any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in self.dimensions.items()
        ):
            raise ValueError("meter dimensions must be string pairs")
        object.__setattr__(self, "dimensions", _FrozenDimensions(self.dimensions))


@dataclass(frozen=True, slots=True)
class UsagePriceRule:
    metric: str
    unit: str
    amount: str
    per: str = "1"
    dimensions: Mapping[str, str] = field(default_factory=dict)
    subtract_metrics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.metric or not self.unit:
            raise ValueError("price rule metric and unit are required")
        object.__setattr__(self, "amount", exact_decimal(self.amount, label="price"))
        object.__setattr__(self, "per", exact_decimal(self.per, label="price unit"))
        if Decimal(self.per) == 0:
            raise ValueError("price unit must be positive")
        if any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in self.dimensions.items()
        ):
            raise ValueError("price dimensions must be string pairs")
        object.__setattr__(self, "dimensions", _FrozenDimensions(self.dimensions))
        object.__setattr__(self, "subtract_metrics", tuple(self.subtract_metrics))


@dataclass(frozen=True, slots=True)
class UsagePriceSnapshot:
    source: str
    source_revision: str
    rules: tuple[UsagePriceRule, ...]
    currency: str = "USD"
    provider: str = ""
    model: str = ""
    source_url: str = ""
    effective_at_ms: int | None = None
    formula_version: str = "meter-rules-v1"

    def __post_init__(self) -> None:
        if not self.source or not self.source_revision:
            raise ValueError("price snapshot source and revision are required")
        object.__setattr__(self, "rules", tuple(self.rules))

    @property
    def snapshot_id(self) -> str:
        return hashlib.sha256(canonical_json(asdict(self)).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class UsageSettlementRevision:
    revision_id: str
    authority: AmountAuthority
    status: Literal["final", "pending", "unknown"]
    currency: str = "USD"
    native_amount: str | None = None
    usd_amount: str | None = None
    previous_revision_id: str | None = None
    reason_code: str = ""
    price_snapshot_id: str | None = None
    fx_native_per_usd: str | None = None
    fx_source: str | None = None
    fx_source_revision: str | None = None
    provider_pending_native_amount: str | None = None
    provider_pending_currency: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.revision_id
            or self.authority
            not in {"provider_confirmed", "provider_estimate", "local_estimate", "unknown"}
            or self.status not in {"final", "pending", "unknown"}
        ):
            raise ValueError("invalid settlement identity, authority or status")
        if (
            len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or self.currency != self.currency.upper()
        ):
            raise ValueError("currency must be an uppercase ISO-style three-letter code")
        for name in ("native_amount", "usd_amount"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, exact_decimal(value, label=name))
        if (self.provider_pending_native_amount is None) != (
            self.provider_pending_currency is None
        ):
            raise ValueError("pending provider amount and currency must be supplied together")
        if self.provider_pending_currency is not None:
            currency = self.provider_pending_currency
            if (
                len(currency) != 3
                or not currency.isascii()
                or not currency.isalpha()
                or currency != currency.upper()
            ):
                raise ValueError(
                    "pending currency must be an uppercase ISO-style three-letter code"
                )
            object.__setattr__(
                self,
                "provider_pending_native_amount",
                exact_decimal(
                    self.provider_pending_native_amount, label="pending provider native amount"
                ),
            )
        if self.fx_native_per_usd is not None:
            rate = exact_decimal(self.fx_native_per_usd, label="FX native per USD")
            if Decimal(rate) <= 0 or not self.fx_source or not self.fx_source_revision:
                raise ValueError("FX normalization requires positive rate and immutable source")
            if self.currency == "USD" and Decimal(rate) != 1:
                raise ValueError("USD normalization rate must be one")
            object.__setattr__(self, "fx_native_per_usd", rate)
        elif self.fx_source is not None or self.fx_source_revision is not None:
            raise ValueError("FX source requires a normalization rate")
        if self.authority == "provider_confirmed" and (
            self.status != "final" or self.native_amount is None
        ):
            raise ValueError("confirmed settlement requires final native amount")
        if self.authority == "unknown" and self.usd_amount is not None:
            raise ValueError("unknown settlement cannot carry a priced USD amount")
        if self.previous_revision_id == self.revision_id:
            raise ValueError("settlement cannot replace itself")

    def cost_fields(self) -> dict[str, Any]:
        amount = self.usd_amount
        with localcontext() as ctx:
            ctx.prec = 64
            nanos = (
                0
                if amount is None
                else int(
                    (Decimal(amount) * 1_000_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
                )
            )
        if nanos > (1 << 63) - 1:
            raise OverflowError("settlement exceeds SQLite USD range")
        confirmed = self.authority == "provider_confirmed" and self.status == "final"
        known = amount is not None and self.authority != "unknown"
        return {
            "cost_nanos": nanos,
            "billed_cost_nanos": nanos if confirmed else 0,
            "estimated_cost_nanos": nanos if known and not confirmed else 0,
            "cost_source": "provider_billed"
            if known and confirmed
            else "opensquilla_estimate"
            if known
            else "unavailable",
            "missing_cost_entries": 0 if known else 1,
        }


@dataclass(frozen=True, slots=True)
class UsageItemMetering:
    meters: tuple[UsageMeter, ...] = ()
    price_snapshot: UsagePriceSnapshot | None = None
    settlement: UsageSettlementRevision | None = None
    token_usage_known: bool = True


def coerce_meter(value: UsageMeter | Mapping[str, Any]) -> UsageMeter:
    return value if isinstance(value, UsageMeter) else UsageMeter(**dict(value))


def coerce_settlement(value: Any) -> UsageSettlementRevision | None:
    if value is None or isinstance(value, UsageSettlementRevision):
        return value
    if isinstance(value, Mapping):
        return UsageSettlementRevision(**dict(value))
    raise ValueError("invalid settlement revision")


def estimate_metered_cost(
    meters: tuple[UsageMeter, ...], snapshot: UsagePriceSnapshot
) -> tuple[Decimal | None, tuple[str, ...]]:
    """Evaluate explicit disjoint rules; refuse missing/ambiguous measurements.

    Inclusive totals use subtract_metrics (e.g. input minus cache buckets).
    Modality/cache intersections must be supplied explicitly by the adapter.
    """
    total = Decimal(0)
    missing: set[str] = set()
    consumed: set[int] = set()
    for rule in snapshot.rules:
        matches = [
            (i, m)
            for i, m in enumerate(meters)
            if m.metric == rule.metric
            and m.unit == rule.unit
            and all(m.dimensions.get(k) == v for k, v in rule.dimensions.items())
        ]
        if not matches and Decimal(rule.amount) == 0:
            # Zero price establishes zero cost, not a known-zero quantity.
            continue
        if len(matches) != 1 or not matches[0][1].known:
            missing.add(rule.metric)
            continue
        index, meter = matches[0]
        if index in consumed:
            raise ValueError("multiple price rules cover the same measurement")
        consumed.add(index)
        quantity = Decimal(meter.quantity or "0")
        for sub in rule.subtract_metrics:
            children = [
                m
                for m in meters
                if m.metric == sub
                and m.unit == rule.unit
                and all(m.dimensions.get(k) == v for k, v in rule.dimensions.items())
            ]
            if len(children) != 1 or not children[0].known:
                missing.add(sub)
            else:
                quantity -= Decimal(children[0].quantity or "0")
        if quantity < 0:
            raise ValueError("usage child buckets exceed their parent")
        total += quantity * Decimal(rule.amount) / Decimal(rule.per)
    for i, meter in enumerate(meters):
        if i not in consumed and (not meter.known or Decimal(meter.quantity or "0") > 0):
            missing.add(meter.metric)
    return (None if missing else total), tuple(sorted(missing))
