"""Resumable, best-effort attribution of pre-ledger transcript usage.

Backfill never changes the cutover baseline.  It only turns trustworthy
historical ``turn_usage`` payloads into dated ledger events so finite ranges
can attribute more of the already-known lifetime total.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog

from opensquilla.session.usage_ledger import (
    UsageBackfillCursor,
    UsageBackfillEntry,
    UsageBackfillWrite,
    UsageEventCompletion,
    UsageEventItem,
    UsageEventStart,
    usd_to_nanos,
)

log = structlog.get_logger(__name__)


class UsageBackfillAnomalyError(ValueError):
    """A historical row cannot be converted without inventing accounting data."""


def _field(source: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in source and source[name] is not None:
            return source[name]
    return default


def _count(source: Mapping[str, Any], *names: str) -> int:
    raw = _field(source, *names, default=0)
    if isinstance(raw, bool):
        raise UsageBackfillAnomalyError(f"invalid boolean counter for {names[0]}")
    try:
        parsed = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise UsageBackfillAnomalyError(f"invalid counter for {names[0]}") from exc
    if (
        not parsed.is_finite()
        or parsed < 0
        or parsed != parsed.to_integral_value()
        or parsed > (1 << 63) - 1
    ):
        raise UsageBackfillAnomalyError(f"invalid counter for {names[0]}")
    return int(parsed)


def _cost_components(source: Mapping[str, Any]) -> tuple[int, int, int]:
    total_raw = _field(source, "cost_usd", "costUsd", "total_cost_usd", "totalCostUsd")
    billed_raw = _field(
        source,
        "billed_cost",
        "billed_cost_usd",
        "billedCostUsd",
        default=0,
    )
    estimated_raw = _field(
        source,
        "estimated_cost_component_usd",
        "estimated_cost_usd",
        "estimatedCostUsd",
    )
    try:
        billed = usd_to_nanos(billed_raw)
        if total_raw is None and estimated_raw is None:
            total = billed
            estimated = 0
        elif total_raw is None:
            estimated = usd_to_nanos(estimated_raw)
            total = billed + estimated
        else:
            total = usd_to_nanos(total_raw)
            if estimated_raw is None:
                if billed > total:
                    raise UsageBackfillAnomalyError(
                        "billed historical cost exceeds total cost"
                    )
                estimated = total - billed
            else:
                estimated = usd_to_nanos(estimated_raw)
    except (TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, UsageBackfillAnomalyError):
            raise
        raise UsageBackfillAnomalyError("invalid historical cost") from exc
    if total != billed + estimated:
        raise UsageBackfillAnomalyError(
            "historical total cost does not equal billed plus estimated"
        )
    return total, billed, estimated


def _stable_legacy_identity(entry: UsageBackfillEntry) -> tuple[str, str | None]:
    turn_context = entry.turn_context if isinstance(entry.turn_context, Mapping) else {}
    turn_id_raw = _field(turn_context, "turn_id", "turnId")
    turn_id = str(turn_id_raw).strip() if turn_id_raw is not None else ""
    if entry.forked_from_parent and not turn_id:
        raise UsageBackfillAnomalyError(
            "fork-inherited historical row has no stable causal turn identity"
        )
    if turn_id:
        identity = f"turn:{turn_id}"
    else:
        identity = (
            f"message:{entry.cursor.session_id}:{entry.cursor.message_id}:"
            f"{entry.cursor.created_at_ms}"
        )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"legacy-{digest}", turn_id or None


def _normalise_item(
    row: Mapping[str, Any],
    *,
    event_id: str,
    ordinal: int,
    default_provider: str | None,
    default_model: str | None,
) -> UsageEventItem:
    total_cost, billed_cost, estimated_cost = _cost_components(row)
    input_tokens = _count(row, "input_tokens", "inputTokens")
    output_tokens = _count(row, "output_tokens", "outputTokens")
    return UsageEventItem(
        event_id=event_id,
        ordinal=ordinal,
        provider=str(_field(row, "provider", default=default_provider) or "") or None,
        model=str(_field(row, "model", default=default_model) or "") or None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=_count(row, "reasoning_tokens", "reasoningTokens"),
        cache_read_tokens=_count(
            row,
            "cache_read_tokens",
            "cached_tokens",
            "cacheReadTokens",
        ),
        cache_write_tokens=_count(row, "cache_write_tokens", "cacheWriteTokens"),
        total_tokens=input_tokens + output_tokens,
        cost_nanos=total_cost,
        billed_cost_nanos=billed_cost,
        estimated_cost_nanos=estimated_cost,
        cost_source=str(_field(row, "cost_source", "costSource", default="none") or "none"),
        estimate_basis=(
            str(value)
            if (value := _field(row, "estimate_basis", "estimateBasis")) is not None
            else None
        ),
        price_source=(
            str(value)
            if (value := _field(row, "price_source", "priceSource")) is not None
            else None
        ),
    )


def normalize_usage_backfill_entry(entry: UsageBackfillEntry) -> UsageBackfillWrite:
    """Convert one canonical assistant record or raise a coded anomaly."""

    if entry.session_metadata_missing:
        raise UsageBackfillAnomalyError(
            "historical row no longer has trustworthy session attribution"
        )
    if not isinstance(entry.turn_usage, Mapping):
        raise UsageBackfillAnomalyError(
            "historical row has no structured turn_usage"
        )
    usage = entry.turn_usage
    event_id, turn_id = _stable_legacy_identity(entry)
    total_cost, billed_cost, estimated_cost = _cost_components(usage)
    input_tokens = _count(usage, "input_tokens", "inputTokens")
    output_tokens = _count(usage, "output_tokens", "outputTokens")
    provider = str(_field(usage, "provider", "provider_id", "providerId", default="") or "")
    model = str(_field(usage, "model", "model_id", "modelId", default="") or "")
    cost_source = str(_field(usage, "cost_source", "costSource", default="none") or "none")
    missing = _count(usage, "missing_cost_entries", "missingCostEntries")
    if total_cost == 0 and (input_tokens or output_tokens) and cost_source == "none":
        cost_source = "unavailable"
        missing = max(1, missing)

    start = UsageEventStart(
        event_id=event_id,
        execution_id=event_id,
        call_index=0,
        session_id=entry.cursor.session_id,
        started_at_ms=entry.cursor.created_at_ms,
        agent_id=entry.agent_id or "main",
        session_epoch=max(0, entry.session_epoch),
        turn_id=turn_id,
        run_kind="legacy_backfill",
        provider=provider or None,
        model=model or None,
        origin="backfilled_turn",
    )
    completion = UsageEventCompletion(
        completed_at_ms=entry.cursor.created_at_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=_count(usage, "reasoning_tokens", "reasoningTokens"),
        cache_read_tokens=_count(
            usage,
            "cache_read_tokens",
            "cached_tokens",
            "cacheReadTokens",
        ),
        cache_write_tokens=_count(usage, "cache_write_tokens", "cacheWriteTokens"),
        total_tokens=input_tokens + output_tokens,
        cost_nanos=total_cost,
        billed_cost_nanos=billed_cost,
        estimated_cost_nanos=estimated_cost,
        cost_source=cost_source,
        provider=provider or None,
        model=model or None,
        estimate_basis=(
            str(value)
            if (value := _field(usage, "estimate_basis", "estimateBasis")) is not None
            else None
        ),
        price_source=(
            str(value)
            if (value := _field(usage, "price_source", "priceSource")) is not None
            else None
        ),
        coverage_status="legacy",
        missing_cost_entries=missing,
    )

    breakdown = _field(usage, "model_usage_breakdown", "modelUsageBreakdown", default=[])
    items: list[UsageEventItem] = []
    if breakdown and not isinstance(breakdown, list):
        raise UsageBackfillAnomalyError("historical model breakdown is malformed")
    if isinstance(breakdown, list) and breakdown:
        for ordinal, row in enumerate(breakdown):
            if not isinstance(row, Mapping):
                raise UsageBackfillAnomalyError(
                    "historical model breakdown is malformed"
                )
            items.append(
                _normalise_item(
                    row,
                    event_id=event_id,
                    ordinal=ordinal,
                    default_provider=provider or None,
                    default_model=model or None,
                )
            )
        component_pairs = (
            ("input_tokens", completion.input_tokens),
            ("output_tokens", completion.output_tokens),
            ("reasoning_tokens", completion.reasoning_tokens),
            ("cache_read_tokens", completion.cache_read_tokens),
            ("cache_write_tokens", completion.cache_write_tokens),
            ("total_tokens", completion.total_tokens),
            ("cost_nanos", completion.cost_nanos),
            ("billed_cost_nanos", completion.billed_cost_nanos),
            ("estimated_cost_nanos", completion.estimated_cost_nanos),
        )
        mismatched = [
            field
            for field, expected in component_pairs
            if sum(getattr(item, field) for item in items) != expected
        ]
        if mismatched:
            # A model split that disagrees with its envelope would make the
            # model/session dimensions diverge from totals. Skip the historical
            # event and retain it in the immutable legacy residual instead.
            raise UsageBackfillAnomalyError(
                "historical model breakdown disagrees with envelope: "
                + ",".join(mismatched)
            )
    if not items:
        items.append(
            UsageEventItem(
                event_id=event_id,
                ordinal=0,
                provider=provider or None,
                model=model or None,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                reasoning_tokens=completion.reasoning_tokens,
                cache_read_tokens=completion.cache_read_tokens,
                cache_write_tokens=completion.cache_write_tokens,
                total_tokens=completion.total_tokens,
                cost_nanos=completion.cost_nanos,
                billed_cost_nanos=completion.billed_cost_nanos,
                estimated_cost_nanos=completion.estimated_cost_nanos,
                cost_source=completion.cost_source,
                estimate_basis=completion.estimate_basis,
                price_source=completion.price_source,
            )
        )
    return UsageBackfillWrite(start=start, completion=completion, items=tuple(items))


async def run_usage_backfill(
    storage: Any,
    *,
    batch_size: int = 500,
) -> None:
    """Resume backfill until the cutover prefix is exhausted.

    The storage layer owns the atomic ``events + cursor`` transaction.  Any
    failure is converted into durable ``partial`` state and never propagates
    into gateway readiness.
    """

    cursor = None
    try:
        state = await storage.get_usage_ledger_state()
        if state is None or state.backfill_status == "complete":
            return
        prepare_indexes = getattr(storage, "prepare_usage_backfill_indexes", None)
        if callable(prepare_indexes):
            await prepare_indexes()
        if state.cursor_created_at_ms is not None:
            cursor = UsageBackfillCursor(
                created_at_ms=state.cursor_created_at_ms,
                session_id=state.cursor_session_id or "",
                message_id=state.cursor_message_id or "",
            )
        await storage.update_usage_backfill_progress(
            status="running",
            cursor=cursor,
            now_ms=int(time.time() * 1000),
        )
        while True:
            batch = await storage.get_usage_backfill_batch(
                before_ms=state.ledger_started_at_ms,
                after=cursor,
                limit=batch_size,
            )
            writes: list[UsageBackfillWrite] = []
            anomalies = 0
            for entry in batch.entries:
                try:
                    writes.append(normalize_usage_backfill_entry(entry))
                except UsageBackfillAnomalyError:
                    anomalies += 1
            cursor = batch.next_cursor or cursor
            await storage.apply_usage_backfill_batch(
                writes,
                cursor=cursor,
                exhausted=batch.exhausted,
                anomaly_delta=anomalies,
                now_ms=int(time.time() * 1000),
            )
            if batch.exhausted:
                return
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - backfill must never break gateway readiness
        log.warning("usage.backfill_failed", error=type(exc).__name__)
        try:
            await storage.update_usage_backfill_progress(
                status="failed",
                cursor=cursor,
                last_error_code=type(exc).__name__,
                now_ms=int(time.time() * 1000),
            )
        except Exception:  # pragma: no cover - storage is already unhealthy
            log.debug("usage.backfill_state_update_failed", exc_info=True)


__all__ = [
    "UsageBackfillAnomalyError",
    "normalize_usage_backfill_entry",
    "run_usage_backfill",
]
