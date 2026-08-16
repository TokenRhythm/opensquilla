"""Provider-call usage accounting contracts.

The engine owns *when* a provider call starts and finishes, but it must not
know how usage is persisted.  This module is therefore deliberately storage
agnostic: the gateway can inject a durable :class:`UsageEventSink`, while CLI
and tests that do not inject one retain the historical runtime behaviour.

One ``UsageCallStart`` represents one outer ``provider.chat`` invocation.
Retries, fallbacks, and later tool-loop iterations receive distinct call
indices.  An ensemble remains one envelope with per-model ``items`` so a
consumer can aggregate either envelopes or model dimensions without counting
the same spend twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

import structlog

from opensquilla.engine.pricing import estimate_cost, resolve_model_price
from opensquilla.provider.types import ProviderBillingReceipt
from opensquilla.usage_reasons import (
    normalize_usage_unknown_reason,
    provider_error_usage_reason,
)

_NANOS_PER_USD = Decimal("1000000000")
_CANCELLED_USAGE_TERMINAL_GRACE_SECONDS = 0.25
log = structlog.get_logger(__name__)


class UsageAccountingUnavailableError(RuntimeError):
    """A provider request was withheld because its ledger start did not commit.

    This is an engine-level error so every surface can preserve the retryable
    failure contract without importing the gateway's SQLite adapter.
    """

    code = "usage_accounting_unavailable"
    retryable = True

    def __init__(
        self,
        message: str,
        *,
        retry_after_ms: int | None = None,
        usage_call_index: int | None = None,
        no_prior_provider_dispatch: bool = False,
        replay_safe: bool = False,
    ) -> None:
        super().__init__(message)
        self.retry_after_ms = retry_after_ms
        self.usage_call_index = usage_call_index
        self.no_prior_provider_dispatch = no_prior_provider_dispatch
        self.replay_safe = replay_safe

    def bind_usage_call(self, call: UsageCallStart) -> None:
        """Attach fail-closed evidence from the rejected provider admission."""

        call_index = max(1, int(call.call_index))
        self.usage_call_index = call_index
        self.no_prior_provider_dispatch = call_index == 1

    def bind_replay_safety(self, *, no_prior_irreversible_effect: bool) -> None:
        """Prove whole-turn replay safety without weakening retryability."""

        self.replay_safe = (
            self.no_prior_provider_dispatch
            and no_prior_irreversible_effect
        )


class UsageAccountingBusyError(UsageAccountingUnavailableError):
    """A transient ledger lock remained busy after its bounded retry."""

    code = "usage_accounting_busy"


def usd_to_nanos(value: object) -> int:
    """Convert a non-negative USD value to an integer nano-USD amount.

    ``Decimal(str(value))`` avoids importing the binary float's representation
    error into the durable ledger.  Invalid, non-finite, and negative provider
    values are treated as zero, matching the existing usage rollup's defensive
    handling of provider metadata.
    """

    try:
        amount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return 0
    if not amount.is_finite() or amount <= 0:
        return 0
    return int((amount * _NANOS_PER_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class UsageExecutionContext:
    """Stable identity and attribution for one Agent execution."""

    execution_id: str
    agent_run_id: str
    turn_id: str | None = None
    parent_turn_id: str | None = None
    session_id: str | None = None
    session_epoch: int = 0
    agent_id: str = ""
    run_kind: str = "agent"


@dataclass(frozen=True, slots=True)
class UsageCallStart:
    """Immutable identity written before a provider request is sent."""

    event_id: str
    execution_id: str
    call_index: int
    agent_run_id: str
    turn_id: str | None
    parent_turn_id: str | None
    session_id: str | None
    agent_id: str
    run_kind: str
    provider: str
    model: str
    started_at_ms: int
    # Appended with a default so independently constructed sinks/tests keep
    # source compatibility while TurnRunner supplies the real reset epoch.
    session_epoch: int = 0


@dataclass(frozen=True, slots=True)
class UsageCallItem:
    """One model/provider contribution inside a provider-call envelope."""

    ordinal: int
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    billed_cost_nanos: int
    estimated_cost_nanos: int
    cost_source: str
    estimate_basis: str | None
    price_source: str | None
    billing_receipt: ProviderBillingReceipt | None = None


@dataclass(frozen=True, slots=True)
class UsageCallResult:
    """Normalized terminal usage for one provider-call envelope."""

    completed_at_ms: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    billed_cost_nanos: int
    estimated_cost_nanos: int
    cost_source: str
    estimate_basis: str | None
    price_source: str | None
    items: tuple[UsageCallItem, ...]
    missing_usage_entries: int = 0


@runtime_checkable
class UsageEventSink(Protocol):
    """Async durable boundary for provider-call accounting.

    ``start`` is fail-closed: an exception prevents the provider request from
    being sent.  ``finalize`` and ``mark_unknown`` must be idempotent by
    ``event_id`` because cancellation shielding and storage-level retries may
    deliver the same terminal operation more than once.
    """

    async def start(self, call: UsageCallStart) -> None: ...

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None: ...

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None: ...


@dataclass(slots=True)
class UsageAccountingScope:
    """Per-execution provider-leg identity allocator.

    A scope object is intentionally shared by copied asyncio contexts.  A
    synchronous increment cannot interleave on the event loop, so concurrent
    tool/meta tasks still receive distinct call indices without a lock.
    """

    sink: UsageEventSink
    context: UsageExecutionContext
    call_index: int = 0

    def new_call(self, *, provider: str, model: str) -> UsageCallStart:
        self.call_index += 1
        context = self.context
        event_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"opensquilla:usage:{context.execution_id}:{self.call_index}",
        ).hex
        return UsageCallStart(
            event_id=event_id,
            execution_id=context.execution_id,
            call_index=self.call_index,
            agent_run_id=context.agent_run_id,
            turn_id=context.turn_id,
            parent_turn_id=context.parent_turn_id,
            session_id=context.session_id,
            session_epoch=context.session_epoch,
            agent_id=context.agent_id,
            run_kind=context.run_kind,
            provider=str(provider or ""),
            model=str(model or ""),
            started_at_ms=time.time_ns() // 1_000_000,
        )


_ACTIVE_USAGE_SCOPE: ContextVar[UsageAccountingScope | None] = ContextVar(
    "opensquilla_active_usage_accounting_scope",
    default=None,
)


def current_usage_accounting_scope() -> UsageAccountingScope | None:
    """Return the provider-accounting scope inherited by the current task."""

    return _ACTIVE_USAGE_SCOPE.get()


@contextmanager
def bind_usage_accounting_scope(scope: UsageAccountingScope | None) -> Iterator[None]:
    """Bind ``scope`` across an Agent, pipeline helper, or background job."""

    if scope is None:
        yield
        return
    token = _ACTIVE_USAGE_SCOPE.set(scope)
    try:
        yield
    finally:
        _ACTIVE_USAGE_SCOPE.reset(token)


def provider_accounts_physical_usage(provider: object) -> bool:
    """Whether a provider wrapper accounts each underlying physical leg."""

    return getattr(provider, "accounts_physical_usage", False) is True


async def start_usage_call(
    scope: UsageAccountingScope,
    *,
    provider: str,
    model: str,
) -> UsageCallStart:
    """Commit a started envelope before the caller may invoke the provider."""

    call = scope.new_call(provider=provider, model=model)
    start_task = asyncio.create_task(scope.sink.start(call))
    try:
        await asyncio.shield(start_task)
    except UsageAccountingUnavailableError as exc:
        exc.bind_usage_call(call)
        raise
    except asyncio.CancelledError:
        # Cancellation raced the fail-closed barrier.  Resolve the durable
        # decision before unwinding; a committed row is explicitly closed.
        committed = False
        try:
            await start_task
            committed = True
        except Exception:
            pass
        if committed:
            with contextlib.suppress(Exception):
                await scope.sink.mark_unknown(
                    call,
                    "cancelled_before_provider_request",
                )
        raise
    return call


async def finalize_usage_call(
    scope: UsageAccountingScope,
    call: UsageCallStart,
    provider_done: object,
) -> None:
    """Normalize and durably finalize one receipt without dropping output."""

    result = normalize_provider_usage(
        provider_done,
        default_provider=call.provider,
        default_model=call.model,
        completed_at_ms=time.time_ns() // 1_000_000,
    )
    finalize_task = asyncio.create_task(scope.sink.finalize(call, result))
    try:
        await asyncio.shield(finalize_task)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await finalize_task
        raise
    except Exception as exc:  # noqa: BLE001 - sink owns its retry policy
        log.warning(
            "usage_accounting.finalize_failed",
            event_id=call.event_id,
            error=str(exc),
        )


async def mark_usage_call_unknown(
    scope: UsageAccountingScope,
    call: UsageCallStart,
    reason: str,
) -> None:
    """Close one started envelope whose provider supplied no usage receipt."""

    stable_reason = normalize_usage_unknown_reason(reason)
    unknown_task = asyncio.create_task(scope.sink.mark_unknown(call, stable_reason))
    try:
        await asyncio.shield(unknown_task)
    except asyncio.CancelledError:
        # A cancellation-resistant sink must not indefinitely retain the
        # provider transport or the cancelled caller. The durable started row
        # remains available to ledger recovery if this bounded attempt cannot
        # finish.
        unknown_task.cancel()
        unknown_task.add_done_callback(_consume_usage_task_result)
        raise
    except Exception as exc:  # noqa: BLE001 - preserve the provider outcome
        log.warning(
            "usage_accounting.mark_unknown_failed",
            event_id=call.event_id,
            reason=stable_reason,
            error=str(exc),
        )


def _consume_usage_task_result(task: asyncio.Future[Any]) -> None:
    """Consume a detached terminal-write result after bounded cancellation."""

    if task.cancelled():
        return
    with contextlib.suppress(BaseException):
        task.result()


async def _mark_usage_call_unknown_after_cancellation(
    scope: UsageAccountingScope,
    call: UsageCallStart,
    reason: str,
) -> None:
    """Give one terminal write a bounded grace period during cancellation."""

    stable_reason = normalize_usage_unknown_reason(reason)
    unknown_task = asyncio.create_task(scope.sink.mark_unknown(call, stable_reason))
    try:
        done, _pending = await asyncio.wait(
            {unknown_task},
            timeout=_CANCELLED_USAGE_TERMINAL_GRACE_SECONDS,
        )
    except asyncio.CancelledError:
        unknown_task.cancel()
        unknown_task.add_done_callback(_consume_usage_task_result)
        raise
    if unknown_task in done:
        try:
            unknown_task.result()
        except Exception as exc:  # noqa: BLE001 - preserve cancellation
            log.warning(
                "usage_accounting.mark_unknown_failed",
                event_id=call.event_id,
                reason=stable_reason,
                error=str(exc),
            )
        return

    unknown_task.cancel()
    unknown_task.add_done_callback(_consume_usage_task_result)
    log.warning(
        "usage_accounting.cancelled_terminal_timeout",
        event_id=call.event_id,
        reason=stable_reason,
        timeout_seconds=_CANCELLED_USAGE_TERMINAL_GRACE_SECONDS,
    )


async def account_provider_stream(
    stream_factory: Callable[[], AsyncIterator[Any]],
    *,
    provider: str,
    model: str,
) -> AsyncGenerator[Any, None]:
    """Account exactly one physical ``provider.chat`` invocation.

    The current scope is inherited through ``ContextVar`` by tool and meta
    tasks.  Without a scope this is a byte-for-byte streaming pass-through.
    """

    scope = current_usage_accounting_scope()
    if scope is None:
        async for event in stream_factory():
            yield event
        return

    call = await start_usage_call(scope, provider=provider, model=model)
    terminal = False
    cancelled = False
    unknown_reason = "provider_stream_ended_without_usage"
    try:
        async for event in stream_factory():
            kind = str(getattr(event, "kind", "") or "")
            if kind == "done" and not terminal:
                # Preserve the physical deployment for compatibility rollups
                # that consume this same Done event after the ledger boundary.
                with contextlib.suppress(Exception):
                    setattr(event, "_opensquilla_usage_provider", call.provider)
                    setattr(event, "_opensquilla_usage_model", call.model)
                terminal = True
                await finalize_usage_call(scope, call, event)
            elif kind == "error":
                unknown_reason = provider_error_usage_reason(
                    getattr(event, "code", None)
                )
                if has_known_provider_usage_receipt(event) and not terminal:
                    terminal = True
                    await finalize_usage_call(scope, call, event)
            yield event
    except asyncio.CancelledError:
        cancelled = True
        unknown_reason = "cancelled"
        raise
    except Exception:
        unknown_reason = "provider_exception"
        raise
    finally:
        if not terminal:
            if cancelled:
                await _mark_usage_call_unknown_after_cancellation(
                    scope,
                    call,
                    unknown_reason,
                )
            else:
                await mark_usage_call_unknown(scope, call, unknown_reason)


def _usage_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _usage_float(value: Any) -> float:
    try:
        parsed = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(parsed) or parsed <= 0:
        return 0.0
    return parsed


def _raw_nonnegative_number(value: Any) -> bool:
    try:
        parsed = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return parsed.is_finite() and parsed >= 0


def _row_value(
    row: Mapping[str, Any],
    *keys: str,
    default: Any = None,
) -> Any:
    for key in keys:
        if key in row:
            return row[key]
    return default


def _breakdown_reconciles(event: object, rows: list[dict[str, Any]]) -> bool:
    """Return whether every additive Done envelope field equals its rows."""

    event_kind = str(getattr(event, "kind", "") or "")
    is_error = event_kind == "error" or (
        event_kind == "provider_generation_reset"
        and bool(getattr(event, "terminal", False))
    )
    additive_keys = (
        ("input_tokens", ("input_tokens", "inputTokens")),
        ("output_tokens", ("output_tokens", "outputTokens")),
        ("reasoning_tokens", ("reasoning_tokens", "reasoningTokens")),
        (
            "cached_tokens",
            ("cache_read_tokens", "cacheReadTokens", "cached_tokens", "cachedTokens"),
        ),
        ("cache_write_tokens", ("cache_write_tokens", "cacheWriteTokens")),
    )
    for event_key, row_keys in additive_keys:
        row_values = [_row_value(row, *row_keys, default=0) for row in rows]
        if not all(_raw_nonnegative_number(value) for value in row_values):
            return False
        if (
            not is_error
            and sum(_usage_int(value) for value in row_values)
            != _usage_int(getattr(event, event_key, 0))
        ):
            return False

    billed_values = [
        _row_value(
            row,
            "billed_cost",
            "billedCost",
            "billed_cost_usd",
            "billedCostUsd",
            default=0.0,
        )
        for row in rows
    ]
    if not all(_raw_nonnegative_number(value) for value in billed_values):
        return False
    return is_error or sum(
        usd_to_nanos(value) for value in billed_values
    ) == usd_to_nanos(getattr(event, "billed_cost", 0.0))


def _row_has_explicit_usage_receipt(row: dict[str, Any]) -> bool:
    """Distinguish a real zero-valued receipt from an arbitrary empty row."""

    if not str(row.get("model") or row.get("provider") or "").strip():
        return False
    input_key = next((key for key in ("input_tokens", "inputTokens") if key in row), None)
    output_key = next((key for key in ("output_tokens", "outputTokens") if key in row), None)
    return bool(
        input_key
        and output_key
        and _raw_nonnegative_number(row[input_key])
        and _raw_nonnegative_number(row[output_key])
    )


def has_known_provider_usage_receipt(event: object) -> bool:
    """Whether an Error event carries at least one trustworthy usage row."""

    raw_breakdown = getattr(event, "model_usage_breakdown", None)
    if (
        not isinstance(raw_breakdown, list)
        or not raw_breakdown
        or not all(isinstance(row, dict) for row in raw_breakdown)
    ):
        return False
    rows = [dict(row) for row in raw_breakdown]
    return _breakdown_reconciles(event, rows) and any(
        _row_has_explicit_usage_receipt(row) for row in rows
    )


def _receipt_int(value: Any, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("billing receipt nanos must be non-negative integers")
    return int(value)


def _coerce_billing_receipt(value: Any) -> ProviderBillingReceipt | None:
    """Defensively normalize additive receipt rows from wrappers/test doubles."""

    if value is None:
        return None
    if isinstance(value, ProviderBillingReceipt):
        candidate = value
    elif isinstance(value, Mapping):
        try:
            raw_fx = value.get("fx_native_per_usd_nanos")
            raw_schema = value.get("schema_version", 1)
            if (
                isinstance(raw_fx, bool)
                or not isinstance(raw_fx, int)
                or isinstance(raw_schema, bool)
                or not isinstance(raw_schema, int)
            ):
                return None
            candidate = ProviderBillingReceipt(
                currency=str(value.get("currency") or "").upper(),
                status=str(value.get("status") or ""),  # type: ignore[arg-type]
                amount_nanos=_receipt_int(value.get("amount_nanos"), nullable=True),
                usd_equivalent_nanos=_receipt_int(
                    value.get("usd_equivalent_nanos"), nullable=True
                ),
                fx_native_per_usd_nanos=raw_fx,
                schema_version=raw_schema,
            )
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    try:
        amount_nanos = _receipt_int(candidate.amount_nanos, nullable=True)
        usd_nanos = _receipt_int(candidate.usd_equivalent_nanos, nullable=True)
    except ValueError:
        return None
    if (
        not isinstance(candidate.currency, str)
        or len(candidate.currency) != 3
        or candidate.currency != candidate.currency.upper()
        or candidate.status not in {"confirmed", "pending"}
        or isinstance(candidate.fx_native_per_usd_nanos, bool)
        or not isinstance(candidate.fx_native_per_usd_nanos, int)
        or candidate.fx_native_per_usd_nanos <= 0
        or isinstance(candidate.schema_version, bool)
        or not isinstance(candidate.schema_version, int)
        or candidate.schema_version <= 0
    ):
        return None
    if candidate.status == "confirmed" and (amount_nanos is None or usd_nanos is None):
        return None
    if candidate.status == "pending" and usd_nanos is not None:
        return None
    return candidate


def _item_from_row(
    row: dict[str, Any],
    *,
    ordinal: int,
    default_provider: str,
    default_model: str,
    resolve_estimates: bool,
) -> UsageCallItem:
    provider = str(row.get("provider") or default_provider or "")
    model = str(row.get("model") or default_model or "")
    input_tokens = _usage_int(_row_value(row, "input_tokens", "inputTokens"))
    output_tokens = _usage_int(_row_value(row, "output_tokens", "outputTokens"))
    reasoning_tokens = _usage_int(
        _row_value(row, "reasoning_tokens", "reasoningTokens")
    )
    cache_read_tokens = _usage_int(
        _row_value(
            row,
            "cache_read_tokens",
            "cacheReadTokens",
            "cached_tokens",
            "cachedTokens",
        )
    )
    cache_write_tokens = _usage_int(
        _row_value(row, "cache_write_tokens", "cacheWriteTokens")
    )
    billed = _usage_float(
        _row_value(
            row,
            "billed_cost",
            "billedCost",
            "billed_cost_usd",
            "billedCostUsd",
        )
    )
    receipt = _coerce_billing_receipt(
        _row_value(row, "billing_receipt", "billingReceipt")
    )
    row_source = str(
        _row_value(row, "cost_source", "costSource", default="none") or "none"
    ).strip().lower()
    receipt_pending = receipt is not None and receipt.status == "pending"
    confirmed_receipt = receipt is not None and receipt.status == "confirmed"
    # Explicit source retains compatibility with provider adapters and test
    # doubles that have not yet attached the additive receipt object. Amount is
    # only the final legacy fallback; confirmed zero relies on source/receipt.
    provider_billed = (
        not receipt_pending
        and (
            confirmed_receipt
            or row_source in {"provider_billed", "openrouter_usage"}
            or billed > 0.0
        )
    )

    estimate_usd = 0.0
    estimate_basis: str | None = None
    price_source: str | None = None
    if not provider_billed and resolve_estimates:
        resolved = resolve_model_price(model, provider)
        estimate = estimate_cost(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            price=resolved.entry,
        )
        estimate_usd = max(0.0, float(estimate.cost_usd or 0.0))
        estimate_basis = estimate.basis
        price_source = resolved.source

    billed_nanos = (
        max(0, int(receipt.usd_equivalent_nanos or 0))
        if confirmed_receipt and receipt is not None
        else usd_to_nanos(billed)
        if provider_billed
        else 0
    )
    estimated_nanos = usd_to_nanos(estimate_usd)
    if provider_billed and estimated_nanos:
        source = "mixed"
    elif provider_billed:
        source = "provider_billed"
    elif estimated_nanos:
        source = "opensquilla_estimate"
    elif estimate_basis == "free":
        source = "free"
    elif not resolve_estimates:
        # Turn-level billed accounting must stay purely receipt-driven.  In
        # particular, a pending receipt whose compatibility source still says
        # ``provider_billed`` must not be reclassified as a confirmed bill
        # merely because estimate resolution was intentionally skipped.
        source = (
            "free"
            if row_source == "free" and not receipt_pending
            else "unavailable"
        )
    else:
        source = "unavailable" if row_source == "none" else row_source

    return UsageCallItem(
        ordinal=ordinal,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        billed_cost_nanos=billed_nanos,
        estimated_cost_nanos=estimated_nanos,
        cost_source=source,
        estimate_basis=estimate_basis,
        price_source=price_source,
        billing_receipt=receipt,
    )


def normalize_provider_usage(
    event: object,
    *,
    default_provider: str,
    default_model: str,
    completed_at_ms: int,
    resolve_estimates: bool = True,
) -> UsageCallResult:
    """Normalize a provider ``DoneEvent`` without depending on persistence.

    Ensemble breakdown rows are the cost source when present because they
    preserve the billed/unbilled split for every member.  Otherwise a single
    item is synthesized from the envelope.  Envelope token and cost counters
    are always summed from those same items, making every dimension reconcile
    exactly without a second rounding path.  ``resolve_estimates=False`` keeps
    normalization receipt-only so turn budget accounting never performs price
    resolution on the provider stream's synchronous error path.
    """

    raw_breakdown = getattr(event, "model_usage_breakdown", None)
    candidate_rows = (
        [dict(row) for row in raw_breakdown]
        if isinstance(raw_breakdown, list)
        and raw_breakdown
        and all(isinstance(row, dict) for row in raw_breakdown)
        else []
    )
    provider = default_provider or str(getattr(event, "provider", "") or "")
    model = str(getattr(event, "model", "") or default_model or "")
    rows = candidate_rows if _breakdown_reconciles(event, candidate_rows) else []
    if not rows:
        rows = [
            {
                "provider": provider,
                "model": model,
                "input_tokens": getattr(event, "input_tokens", 0),
                "output_tokens": getattr(event, "output_tokens", 0),
                "reasoning_tokens": getattr(event, "reasoning_tokens", 0),
                "cached_tokens": getattr(event, "cached_tokens", 0),
                "cache_write_tokens": getattr(event, "cache_write_tokens", 0),
                "billed_cost": getattr(event, "billed_cost", 0.0),
                "cost_source": getattr(event, "cost_source", "none"),
                "billing_receipt": getattr(event, "billing_receipt", None),
            }
        ]

    items = tuple(
        _item_from_row(
            row,
            ordinal=ordinal,
            default_provider=provider,
            default_model=model,
            resolve_estimates=resolve_estimates,
        )
        for ordinal, row in enumerate(rows)
    )
    billed_nanos = sum(item.billed_cost_nanos for item in items)
    estimated_nanos = sum(item.estimated_cost_nanos for item in items)
    item_sources = {item.cost_source for item in items}
    has_billed = "provider_billed" in item_sources
    has_estimated = bool(item_sources & {"opensquilla_estimate", "mixed"})
    has_unavailable = "unavailable" in item_sources
    if "mixed" in item_sources or has_billed and (has_estimated or has_unavailable):
        cost_source = "mixed"
    elif has_billed:
        cost_source = "provider_billed"
    elif has_estimated:
        cost_source = "opensquilla_estimate"
    elif items and all(item.cost_source == "free" for item in items):
        cost_source = "free"
    else:
        cost_source = "unavailable"

    bases = {item.estimate_basis for item in items if item.estimate_basis}
    estimate_basis = (
        "cache_blind"
        if "cache_blind" in bases
        else "cache_aware"
        if "cache_aware" in bases
        else "free"
        if "free" in bases
        else None
    )
    price_sources = {item.price_source for item in items if item.price_source}
    price_source = (
        next(iter(price_sources))
        if len(price_sources) == 1
        else "mixed"
        if price_sources
        else None
    )

    return UsageCallResult(
        completed_at_ms=max(0, int(completed_at_ms)),
        input_tokens=sum(item.input_tokens for item in items),
        output_tokens=sum(item.output_tokens for item in items),
        reasoning_tokens=sum(item.reasoning_tokens for item in items),
        cache_read_tokens=sum(item.cache_read_tokens for item in items),
        cache_write_tokens=sum(item.cache_write_tokens for item in items),
        billed_cost_nanos=billed_nanos,
        estimated_cost_nanos=estimated_nanos,
        cost_source=cost_source,
        estimate_basis=estimate_basis,
        price_source=price_source,
        items=items,
        missing_usage_entries=_usage_int(
            getattr(event, "usage_missing_count", 0)
        ),
    )


__all__ = [
    "UsageAccountingBusyError",
    "UsageAccountingScope",
    "UsageAccountingUnavailableError",
    "UsageCallItem",
    "UsageCallResult",
    "UsageCallStart",
    "UsageEventSink",
    "UsageExecutionContext",
    "account_provider_stream",
    "bind_usage_accounting_scope",
    "current_usage_accounting_scope",
    "finalize_usage_call",
    "has_known_provider_usage_receipt",
    "mark_usage_call_unknown",
    "normalize_provider_usage",
    "provider_accounts_physical_usage",
    "start_usage_call",
    "usd_to_nanos",
]
