"""Gateway adapter between provider-call accounting and ``SessionStorage``.

The engine deliberately knows nothing about SQLite.  This adapter keeps the
important ordering guarantee at that boundary: a provider request is only sent
after its durable ``started`` row commits.  Terminal writes are idempotent and
receive a short background retry without delaying an already-produced model
response.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any

import structlog

from opensquilla.asyncio_utils import create_background_task
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageAccountingUnavailableError,
    UsageCallItem,
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
)
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.session.usage_ledger import (
    UsageEventCompletion,
    UsageEventItem,
    UsageEventStart,
    UsageItemBillingReceipt,
)
from opensquilla.usage_reasons import normalize_usage_unknown_reason

log = structlog.get_logger(__name__)


class UsageLedgerStorageError(UsageAccountingUnavailableError):
    """A provider call could not be durably reserved before dispatch."""


async def build_session_usage_scope(
    usage_event_sink: Any | None,
    session_manager: Any,
    session_key: str,
    *,
    run_kind: str,
    parent_turn_id: str | None = None,
) -> UsageAccountingScope | None:
    """Create a child accounting scope resolved from one durable session.

    The session key is used only for lookup and is never copied into the
    ledger.  If a configured sink cannot resolve a durable session, the scope
    deliberately carries no session id so its start barrier fails closed.
    """

    if usage_event_sink is None:
        return None
    storage = get_session_storage(session_manager)
    node = await storage.get_session(session_key) if storage is not None else None
    execution_id = uuid.uuid4().hex
    return UsageAccountingScope(
        sink=usage_event_sink,
        context=UsageExecutionContext(
            execution_id=execution_id,
            agent_run_id=execution_id,
            turn_id=execution_id,
            parent_turn_id=parent_turn_id,
            session_id=str(getattr(node, "session_id", "") or "") or None,
            session_epoch=max(0, int(getattr(node, "epoch", 0) or 0)),
            agent_id=str(getattr(node, "agent_id", "") or "main"),
            run_kind=run_kind,
        ),
    )


def _completion(call: UsageCallStart, result: UsageCallResult) -> UsageEventCompletion:
    billed = max(0, int(result.billed_cost_nanos))
    estimated = max(0, int(result.estimated_cost_nanos))
    # Envelope cost_source rolls up the components and can therefore be
    # ``provider_billed`` even when another ensemble member has a receipt but
    # no usable price.  Count unavailable item receipts directly so a known
    # priced leg never masks incomplete pricing on a sibling leg.
    missing = sum(1 for item in result.items if item.cost_source == "unavailable")
    if not result.items and result.cost_source == "unavailable":
        missing = 1
    missing_usage = max(0, int(result.missing_usage_entries))
    item_providers = {item.provider for item in result.items if item.provider}
    item_models = {item.model for item in result.items if item.model}
    return UsageEventCompletion(
        completed_at_ms=max(call.started_at_ms, int(result.completed_at_ms)),
        input_tokens=max(0, int(result.input_tokens)),
        output_tokens=max(0, int(result.output_tokens)),
        reasoning_tokens=max(0, int(result.reasoning_tokens)),
        cache_read_tokens=max(0, int(result.cache_read_tokens)),
        cache_write_tokens=max(0, int(result.cache_write_tokens)),
        total_tokens=max(0, int(result.input_tokens)) + max(0, int(result.output_tokens)),
        cost_nanos=billed + estimated,
        billed_cost_nanos=billed,
        estimated_cost_nanos=estimated,
        cost_source=result.cost_source or "unavailable",
        provider=call.provider
        or (next(iter(item_providers)) if len(item_providers) == 1 else None),
        model=call.model or (next(iter(item_models)) if len(item_models) == 1 else None),
        estimate_basis=result.estimate_basis,
        price_source=result.price_source,
        coverage_status=(
            "usage_missing"
            if missing_usage
            else "pricing_missing"
            if missing
            else "complete"
        ),
        missing_cost_entries=missing + missing_usage,
    )


def _item(event_id: str, value: UsageCallItem) -> UsageEventItem:
    billed = max(0, int(value.billed_cost_nanos))
    estimated = max(0, int(value.estimated_cost_nanos))
    input_tokens = max(0, int(value.input_tokens))
    output_tokens = max(0, int(value.output_tokens))
    return UsageEventItem(
        event_id=event_id,
        ordinal=max(0, int(value.ordinal)),
        provider=value.provider or None,
        model=value.model or None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=max(0, int(value.reasoning_tokens)),
        cache_read_tokens=max(0, int(value.cache_read_tokens)),
        cache_write_tokens=max(0, int(value.cache_write_tokens)),
        total_tokens=input_tokens + output_tokens,
        cost_nanos=billed + estimated,
        billed_cost_nanos=billed,
        estimated_cost_nanos=estimated,
        cost_source=value.cost_source or "unavailable",
        estimate_basis=value.estimate_basis,
        price_source=value.price_source,
    )


def _receipt(event_id: str, value: UsageCallItem) -> UsageItemBillingReceipt | None:
    receipt = value.billing_receipt
    if receipt is None:
        return None
    return UsageItemBillingReceipt(
        event_id=event_id,
        ordinal=max(0, int(value.ordinal)),
        currency=receipt.currency,
        status=receipt.status,
        amount_nanos=receipt.amount_nanos,
        usd_equivalent_nanos=receipt.usd_equivalent_nanos,
        fx_native_per_usd_nanos=receipt.fx_native_per_usd_nanos,
        schema_version=receipt.schema_version,
    )


def _reconciled_items(
    call: UsageCallStart,
    result: UsageCallResult,
) -> tuple[UsageCallItem, ...]:
    items = tuple(result.items)
    sums = (
        sum(item.input_tokens for item in items),
        sum(item.output_tokens for item in items),
        sum(item.reasoning_tokens for item in items),
        sum(item.cache_read_tokens for item in items),
        sum(item.cache_write_tokens for item in items),
        sum(item.billed_cost_nanos for item in items),
        sum(item.estimated_cost_nanos for item in items),
    )
    envelope = (
        result.input_tokens,
        result.output_tokens,
        result.reasoning_tokens,
        result.cache_read_tokens,
        result.cache_write_tokens,
        result.billed_cost_nanos,
        result.estimated_cost_nanos,
    )
    if items and sums == envelope:
        return items
    billing_receipt = items[0].billing_receipt if len(items) == 1 else None
    if billing_receipt is not None:
        receipt_reconciles = (
            billing_receipt.status == "confirmed"
            and billing_receipt.usd_equivalent_nanos == result.billed_cost_nanos
            and result.estimated_cost_nanos == 0
            and result.cost_source == "provider_billed"
        ) or (
            billing_receipt.status == "pending"
            and result.billed_cost_nanos == 0
            and result.cost_source != "provider_billed"
        )
        if not receipt_reconciles:
            billing_receipt = None
    # Persistence is the last defensive boundary: never store dimensions
    # whose model rows add up to less or more than the billed envelope.
    return (
        UsageCallItem(
            ordinal=0,
            provider=call.provider,
            model=call.model,
            input_tokens=max(0, int(result.input_tokens)),
            output_tokens=max(0, int(result.output_tokens)),
            reasoning_tokens=max(0, int(result.reasoning_tokens)),
            cache_read_tokens=max(0, int(result.cache_read_tokens)),
            cache_write_tokens=max(0, int(result.cache_write_tokens)),
            billed_cost_nanos=max(0, int(result.billed_cost_nanos)),
            estimated_cost_nanos=max(0, int(result.estimated_cost_nanos)),
            cost_source=result.cost_source or "unavailable",
            estimate_basis=result.estimate_basis,
            price_source=result.price_source,
            billing_receipt=billing_receipt,
        ),
    )


class SessionUsageEventSink:
    """Persist normalized engine usage through the additive ledger API."""

    def __init__(
        self,
        storage: Any,
        *,
        retry_delays: tuple[float, ...] = (0.05, 0.2, 1.0, 5.0),
    ) -> None:
        self._storage = storage
        self._retry_delays = retry_delays
        self._tasks: set[asyncio.Task[Any]] = set()

    @staticmethod
    def _start_record(call: UsageCallStart) -> UsageEventStart:
        session_id = str(call.session_id or "").strip()
        if not session_id:
            raise UsageLedgerStorageError(
                "usage accounting requires a durable session identity"
            )
        return UsageEventStart(
            event_id=call.event_id,
            execution_id=call.execution_id,
            call_index=call.call_index,
            session_id=session_id,
            started_at_ms=call.started_at_ms,
            agent_id=call.agent_id or "main",
            session_epoch=max(0, int(call.session_epoch)),
            turn_id=call.turn_id,
            agent_run_id=call.agent_run_id,
            parent_turn_id=call.parent_turn_id,
            run_kind=call.run_kind or "agent",
            provider=call.provider or None,
            model=call.model or None,
            origin="live_provider",
        )

    async def start(self, call: UsageCallStart) -> None:
        try:
            await self._storage.start_usage_event(self._start_record(call))
        except UsageLedgerStorageError:
            raise
        except Exception as exc:
            raise UsageLedgerStorageError(
                "usage ledger is temporarily unavailable; provider request was not sent"
            ) from exc

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        completion = _completion(call, result)
        reconciled = _reconciled_items(call, result)
        items = tuple(_item(call.event_id, value) for value in reconciled)
        receipts = tuple(
            receipt
            for value in reconciled
            if (receipt := _receipt(call.event_id, value)) is not None
        )
        try:
            kwargs: dict[str, Any] = {"items": items}
            if receipts:
                kwargs["receipts"] = receipts
            await self._storage.finalize_usage_event(call.event_id, completion, **kwargs)
        except Exception:
            self._schedule_retry(
                self._retry_finalize(call.event_id, completion, items, receipts),
                event_id=call.event_id,
                operation="finalize",
            )
            raise

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        completed_at_ms = max(call.started_at_ms, time.time_ns() // 1_000_000)
        stable_reason = normalize_usage_unknown_reason(reason)
        try:
            await self._storage.mark_usage_event_unknown(
                call.event_id,
                completed_at_ms=completed_at_ms,
                reason=stable_reason,
            )
        except Exception:
            self._schedule_retry(
                self._retry_unknown(call.event_id, completed_at_ms, stable_reason),
                event_id=call.event_id,
                operation="mark_unknown",
            )
            raise

    def _schedule_retry(
        self,
        coroutine: Any,
        *,
        event_id: str,
        operation: str,
    ) -> None:
        task = create_background_task(coroutine)
        if not isinstance(task, asyncio.Task):
            return
        self._tasks.add(task)

        def _done(completed: asyncio.Task[Any]) -> None:
            self._tasks.discard(completed)
            if completed.cancelled():
                return
            with contextlib.suppress(Exception):
                completed.result()

        task.add_done_callback(_done)
        log.info(
            "usage.ledger_retry_scheduled",
            event_id=event_id,
            operation=operation,
        )

    async def _retry_finalize(
        self,
        event_id: str,
        completion: UsageEventCompletion,
        items: tuple[UsageEventItem, ...],
        receipts: tuple[UsageItemBillingReceipt, ...],
    ) -> None:
        for delay in self._retry_delays:
            await asyncio.sleep(delay)
            try:
                kwargs: dict[str, Any] = {"items": items}
                if receipts:
                    kwargs["receipts"] = receipts
                await self._storage.finalize_usage_event(event_id, completion, **kwargs)
                return
            except Exception:  # noqa: BLE001 - bounded retry; recovery handles residue.
                continue
        log.warning("usage.ledger_finalize_retry_exhausted", event_id=event_id)

    async def _retry_unknown(
        self,
        event_id: str,
        completed_at_ms: int,
        reason: str,
    ) -> None:
        for delay in self._retry_delays:
            await asyncio.sleep(delay)
            try:
                await self._storage.mark_usage_event_unknown(
                    event_id,
                    completed_at_ms=completed_at_ms,
                    reason=reason,
                )
                return
            except Exception:  # noqa: BLE001 - bounded retry; recovery handles residue.
                continue
        log.warning("usage.ledger_unknown_retry_exhausted", event_id=event_id)

    async def close(self, *, drain_timeout: float | None = None) -> None:
        tasks = tuple(self._tasks)
        if not tasks:
            return
        timeout = (
            max(0.0, float(drain_timeout))
            if drain_timeout is not None
            else sum(max(0.0, delay) for delay in self._retry_delays) + 0.5
        )
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()


__all__ = [
    "SessionUsageEventSink",
    "UsageLedgerStorageError",
    "build_session_usage_scope",
]
