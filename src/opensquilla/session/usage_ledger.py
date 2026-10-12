"""Typed records and exact money helpers for the durable usage ledger.

The ledger deliberately stores no prompts, transcript content, channel identifiers,
or session keys.  Callers should persist stable reason/source codes rather than raw
provider error messages.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

USD_NANO_SCALE = 1_000_000_000
_MAX_SQLITE_INTEGER = (1 << 63) - 1

UsageEventStatus = Literal["started", "finalized", "unknown"]
UsageBackfillStatus = Literal["pending", "running", "complete", "partial", "failed"]
UsageBillingReceiptStatus = Literal["confirmed", "pending"]


class UsageLedgerConflictError(ValueError):
    """Raised when an idempotency identity is reused with different data."""


def usd_to_nanos(value: Decimal | float | int | str | None) -> int:
    """Convert a non-negative USD amount to an exact SQLite integer.

    ``Decimal(str(value))`` prevents the binary representation noise of floats
    from leaking into the durable accounting value. Amounts are rounded to the
    nearest nano-dollar, with halves rounded away from zero.
    """

    if value is None:
        return 0
    if isinstance(value, bool):
        raise TypeError("USD amount must be numeric, not bool")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("USD amount must be finite")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("USD amount must be numeric") from exc
    if not decimal_value.is_finite():
        raise ValueError("USD amount must be finite")
    if decimal_value < 0:
        raise ValueError("USD amount must be non-negative")
    nanos = int(
        (decimal_value * USD_NANO_SCALE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )
    if nanos > _MAX_SQLITE_INTEGER:
        raise OverflowError("USD amount exceeds the SQLite integer range")
    return nanos


def nanos_to_usd(value: int) -> float:
    """Convert a non-negative nano-dollar integer to a JSON-friendly float."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("nano-USD amount must be an integer")
    if value < 0:
        raise ValueError("nano-USD amount must be non-negative")
    return float(Decimal(value) / USD_NANO_SCALE)


@dataclass(frozen=True, slots=True)
class UsageEventStart:
    """Identity and attribution recorded before a provider request is sent."""

    event_id: str
    execution_id: str
    call_index: int
    session_id: str
    started_at_ms: int
    agent_id: str = "main"
    session_epoch: int = 0
    turn_id: str | None = None
    agent_run_id: str | None = None
    parent_turn_id: str | None = None
    run_kind: str = "default"
    provider: str | None = None
    model: str | None = None
    origin: str = "live_provider"


@dataclass(frozen=True, slots=True)
class UsageEventCompletion:
    """Normalized provider usage attached to a started ledger event."""

    completed_at_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_nanos: int = 0
    billed_cost_nanos: int = 0
    estimated_cost_nanos: int = 0
    cost_source: str = "none"
    provider: str | None = None
    model: str | None = None
    estimate_basis: str | None = None
    price_source: str | None = None
    coverage_status: str = "complete"
    missing_cost_entries: int = 0


@dataclass(frozen=True, slots=True)
class UsageEventItem:
    """Per-model item belonging to exactly one provider-call envelope."""

    event_id: str
    ordinal: int
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0
    cost_nanos: int = 0
    billed_cost_nanos: int = 0
    estimated_cost_nanos: int = 0
    cost_source: str = "none"
    estimate_basis: str | None = None
    price_source: str | None = None
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class UsageItemBillingReceipt:
    """Provider-native bill attached to one physical usage item.

    Native and USD amounts use nano-units so the persisted receipt can be
    reconciled without a binary floating-point conversion. Pending receipts
    deliberately have no USD equivalent because they are not billed spend.
    """

    event_id: str
    ordinal: int
    currency: str
    status: UsageBillingReceiptStatus
    amount_nanos: int | None
    usd_equivalent_nanos: int | None
    fx_native_per_usd_nanos: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class UsageBillingReceiptState:
    """Singleton cutover for exact provider-native billing coverage."""

    tracking_started_at_ms: int
    schema_version: int


@dataclass(frozen=True, slots=True)
class UsageEventRecord:
    """Complete persisted usage-event envelope."""

    event_id: str
    execution_id: str
    call_index: int
    session_id: str
    agent_id: str
    session_epoch: int
    turn_id: str | None
    agent_run_id: str | None
    parent_turn_id: str | None
    run_kind: str
    provider: str | None
    model: str | None
    started_at_ms: int
    completed_at_ms: int | None
    status: UsageEventStatus
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    cost_nanos: int
    billed_cost_nanos: int
    estimated_cost_nanos: int
    cost_source: str
    estimate_basis: str | None
    price_source: str | None
    coverage_status: str
    missing_cost_entries: int
    unknown_reason: str | None
    origin: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class UsageLedgerState:
    """Singleton cutover and resumable historical-backfill state."""

    ledger_started_at_ms: int
    backfill_status: UsageBackfillStatus
    cursor_created_at_ms: int | None
    cursor_session_id: str | None
    cursor_message_id: str | None
    backfilled_event_count: int
    backfilled_cost_nanos: int
    anomaly_count: int
    last_error_code: str | None
    updated_at_ms: int
    schema_version: int


@dataclass(frozen=True, slots=True)
class UsageLegacyBaseline:
    """Pre-ledger lifetime totals captured per session epoch at cutover."""

    session_id: str
    session_epoch: int
    agent_id: str
    captured_at_ms: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_nanos: int
    billed_cost_nanos: int
    estimated_cost_nanos: int
    cost_source: str
    missing_cost_entries: int
    schema_version: int


@dataclass(frozen=True, order=True, slots=True)
class UsageBackfillCursor:
    created_at_ms: int
    session_id: str
    message_id: str


@dataclass(frozen=True, slots=True)
class UsageBackfillEntry:
    """Canonical historical assistant row eligible for ledger backfill."""

    cursor: UsageBackfillCursor
    agent_id: str
    session_epoch: int
    forked_from_parent: bool
    turn_usage: dict[str, Any] | None
    turn_context: dict[str, Any] | None
    session_metadata_missing: bool = False


@dataclass(frozen=True, slots=True)
class UsageBackfillBatch:
    entries: tuple[UsageBackfillEntry, ...]
    next_cursor: UsageBackfillCursor | None
    exhausted: bool


@dataclass(frozen=True, slots=True)
class UsageBackfillWrite:
    """One normalized historical event committed with a backfill cursor."""

    start: UsageEventStart
    completion: UsageEventCompletion
    items: tuple[UsageEventItem, ...] = field(default_factory=tuple)


def validate_usage_event_start(event: UsageEventStart) -> None:
    for label, value in (
        ("event_id", event.event_id),
        ("execution_id", event.execution_id),
        ("session_id", event.session_id),
        ("agent_id", event.agent_id),
        ("run_kind", event.run_kind),
        ("origin", event.origin),
    ):
        if not value:
            raise ValueError(f"{label} must not be empty")
    for numeric_label, numeric_value in (
        ("call_index", event.call_index),
        ("session_epoch", event.session_epoch),
        ("started_at_ms", event.started_at_ms),
    ):
        if numeric_value < 0:
            raise ValueError(f"{numeric_label} must be non-negative")


def validate_usage_completion(completion: UsageEventCompletion) -> None:
    numeric_fields = (
        "completed_at_ms",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "cost_nanos",
        "billed_cost_nanos",
        "estimated_cost_nanos",
        "missing_cost_entries",
    )
    for label in numeric_fields:
        if getattr(completion, label) < 0:
            raise ValueError(f"{label} must be non-negative")
    if completion.cost_nanos != (
        completion.billed_cost_nanos + completion.estimated_cost_nanos
    ):
        raise ValueError("cost_nanos must equal billed_cost_nanos + estimated_cost_nanos")
    if not completion.cost_source:
        raise ValueError("cost_source must not be empty")
    if not completion.coverage_status:
        raise ValueError("coverage_status must not be empty")


def validate_usage_item(item: UsageEventItem, *, event_id: str) -> None:
    if item.event_id != event_id:
        raise ValueError("usage item event_id does not match its envelope")
    for label in (
        "ordinal",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "cost_nanos",
        "billed_cost_nanos",
        "estimated_cost_nanos",
    ):
        if getattr(item, label) < 0:
            raise ValueError(f"item {label} must be non-negative")
    if item.cost_nanos != item.billed_cost_nanos + item.estimated_cost_nanos:
        raise ValueError("item cost_nanos must equal billed + estimated cost")
    if not item.cost_source:
        raise ValueError("item cost_source must not be empty")


def validate_usage_billing_receipt(
    receipt: UsageItemBillingReceipt,
    *,
    event_id: str,
) -> None:
    """Validate shape and settlement semantics before durable insertion."""

    if not receipt.event_id:
        raise ValueError("billing receipt event_id must not be empty")
    if receipt.event_id != event_id:
        raise ValueError("billing receipt event_id does not match its envelope")
    if isinstance(receipt.ordinal, bool) or not isinstance(receipt.ordinal, int):
        raise TypeError("billing receipt ordinal must be an integer")
    if receipt.ordinal < 0:
        raise ValueError("billing receipt ordinal must be non-negative")
    if (
        len(receipt.currency) != 3
        or not receipt.currency.isascii()
        or not receipt.currency.isalpha()
        or receipt.currency != receipt.currency.upper()
    ):
        raise ValueError("billing receipt currency must be a three-letter uppercase code")
    if receipt.status not in {"confirmed", "pending"}:
        raise ValueError("billing receipt status must be confirmed or pending")
    for label, value in (
        ("amount_nanos", receipt.amount_nanos),
        ("usd_equivalent_nanos", receipt.usd_equivalent_nanos),
    ):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"billing receipt {label} must be an integer")
        if value < 0:
            raise ValueError(f"billing receipt {label} must be non-negative")
        if value > _MAX_SQLITE_INTEGER:
            raise OverflowError(f"billing receipt {label} exceeds the SQLite integer range")
    if (
        isinstance(receipt.fx_native_per_usd_nanos, bool)
        or not isinstance(receipt.fx_native_per_usd_nanos, int)
    ):
        raise TypeError("billing receipt normalization rate must be an integer")
    if receipt.fx_native_per_usd_nanos <= 0:
        raise ValueError("billing receipt normalization rate must be positive")
    if receipt.fx_native_per_usd_nanos > _MAX_SQLITE_INTEGER:
        raise OverflowError("billing receipt normalization rate exceeds the SQLite integer range")
    if isinstance(receipt.schema_version, bool) or not isinstance(receipt.schema_version, int):
        raise TypeError("billing receipt schema_version must be an integer")
    if receipt.schema_version < 1:
        raise ValueError("billing receipt schema_version must be positive")
    if receipt.status == "confirmed":
        if receipt.amount_nanos is None or receipt.usd_equivalent_nanos is None:
            raise ValueError("confirmed billing receipt requires native and USD amounts")
    elif receipt.usd_equivalent_nanos is not None:
        raise ValueError("pending billing receipt must not have a USD equivalent")
