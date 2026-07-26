from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from opensquilla.engine.usage_accounting import (
    UsageAccountingUnavailableError,
    UsageCallItem,
    UsageCallResult,
    UsageCallStart,
)
from opensquilla.gateway.boot import _auto_propose_usage_execution_context
from opensquilla.gateway.usage_ledger_runtime import (
    SessionUsageEventSink,
    UsageLedgerStorageError,
)
from opensquilla.provider.types import ProviderBillingReceipt
from opensquilla.session.storage import SessionStorage


def _call(**overrides) -> UsageCallStart:
    values = {
        "event_id": "event-1",
        "execution_id": "turn-1",
        "call_index": 1,
        "agent_run_id": "run-1",
        "turn_id": "turn-1",
        "parent_turn_id": None,
        "session_id": "session-1",
        "session_epoch": 3,
        "agent_id": "main",
        "run_kind": "agent",
        "provider": "provider-a",
        "model": "model-a",
        "started_at_ms": 1_000,
    }
    values.update(overrides)
    return UsageCallStart(**values)


def _result(*, source: str = "mixed") -> UsageCallResult:
    return UsageCallResult(
        completed_at_ms=2_000,
        input_tokens=11,
        output_tokens=7,
        reasoning_tokens=3,
        cache_read_tokens=2,
        cache_write_tokens=1,
        billed_cost_nanos=4,
        estimated_cost_nanos=5,
        cost_source=source,
        estimate_basis="cache_aware",
        price_source="catalog",
        items=(
            UsageCallItem(
                ordinal=0,
                provider="provider-a",
                model="model-a",
                input_tokens=11,
                output_tokens=7,
                reasoning_tokens=3,
                cache_read_tokens=2,
                cache_write_tokens=1,
                billed_cost_nanos=4,
                estimated_cost_nanos=5,
                cost_source=source,
                estimate_basis="cache_aware",
                price_source="catalog",
            ),
        ),
    )


def test_auto_propose_uses_stable_synthetic_session_and_unique_runs() -> None:
    sink = object()
    first = _auto_propose_usage_execution_context("main", sink)
    second = _auto_propose_usage_execution_context("main", sink)

    assert first is not None and second is not None
    assert first.execution_id != second.execution_id
    assert first.session_id == second.session_id
    assert first.run_kind == second.run_kind == "auto_propose"
    assert _auto_propose_usage_execution_context("main", None) is None


class _Storage:
    def __init__(self) -> None:
        self.started = []
        self.finalized = []
        self.finalized_receipts = []
        self.unknown = []
        self.fail_finalize = 0

    async def start_usage_event(self, event):
        self.started.append(event)

    async def finalize_usage_event(self, event_id, completion, *, items=(), receipts=()):
        if self.fail_finalize:
            self.fail_finalize -= 1
            raise RuntimeError("busy")
        self.finalized.append((event_id, completion, tuple(items)))
        self.finalized_receipts.append(tuple(receipts))

    async def mark_usage_event_unknown(self, event_id, *, completed_at_ms, reason=None):
        self.unknown.append((event_id, completed_at_ms, reason))


@pytest.mark.asyncio
async def test_start_persists_identity_before_provider_dispatch() -> None:
    storage = _Storage()
    sink = SessionUsageEventSink(storage)

    await sink.start(_call())

    event = storage.started[0]
    assert event.event_id == "event-1"
    assert event.execution_id == "turn-1"
    assert event.session_id == "session-1"
    assert event.origin == "live_provider"


@pytest.mark.asyncio
async def test_start_without_durable_session_fails_closed() -> None:
    storage = _Storage()
    sink = SessionUsageEventSink(storage)

    with pytest.raises(UsageLedgerStorageError, match="durable session identity"):
        await sink.start(replace(_call(), session_id=None))

    assert storage.started == []
    assert issubclass(UsageLedgerStorageError, UsageAccountingUnavailableError)


@pytest.mark.asyncio
async def test_finalize_writes_envelope_and_items_without_double_counting() -> None:
    storage = _Storage()
    sink = SessionUsageEventSink(storage)

    await sink.finalize(_call(), _result())

    event_id, completion, items = storage.finalized[0]
    assert event_id == "event-1"
    assert completion.total_tokens == 18
    assert completion.cost_nanos == 9
    assert completion.cost_nanos == (
        completion.billed_cost_nanos + completion.estimated_cost_nanos
    )
    assert sum(item.cost_nanos for item in items) == completion.cost_nanos


@pytest.mark.asyncio
async def test_finalize_propagates_confirmed_and_pending_physical_receipts() -> None:
    storage = _Storage()
    sink = SessionUsageEventSink(storage)
    confirmed = ProviderBillingReceipt(
        currency="CNY",
        status="confirmed",
        amount_nanos=27_900_000,
        usd_equivalent_nanos=4_000_000,
        fx_native_per_usd_nanos=6_975_000_000,
    )
    pending = ProviderBillingReceipt(
        currency="CNY",
        status="pending",
        amount_nanos=None,
        usd_equivalent_nanos=None,
        fx_native_per_usd_nanos=6_975_000_000,
    )
    items = (
        UsageCallItem(
            ordinal=0,
            provider="tokenrhythm",
            model="model-a",
            input_tokens=5,
            output_tokens=3,
            reasoning_tokens=1,
            cache_read_tokens=1,
            cache_write_tokens=0,
            billed_cost_nanos=4_000_000,
            estimated_cost_nanos=0,
            cost_source="provider_billed",
            estimate_basis=None,
            price_source=None,
            billing_receipt=confirmed,
        ),
        UsageCallItem(
            ordinal=1,
            provider="tokenrhythm",
            model="model-b",
            input_tokens=6,
            output_tokens=4,
            reasoning_tokens=2,
            cache_read_tokens=1,
            cache_write_tokens=1,
            billed_cost_nanos=0,
            estimated_cost_nanos=5_000_000,
            cost_source="opensquilla_estimate",
            estimate_basis="cache_aware",
            price_source="catalog",
            billing_receipt=pending,
        ),
    )
    result = replace(
        _result(),
        billed_cost_nanos=4_000_000,
        estimated_cost_nanos=5_000_000,
        cost_source="mixed",
        items=items,
    )

    await sink.finalize(_call(provider="tokenrhythm", model=""), result)

    _, completion, persisted_items = storage.finalized[0]
    persisted_receipts = storage.finalized_receipts[0]
    assert completion.cost_source == "mixed"
    assert [item.ordinal for item in persisted_items] == [0, 1]
    assert persisted_receipts[0].event_id == "event-1"
    assert persisted_receipts[0].ordinal == 0
    assert persisted_receipts[0].amount_nanos == 27_900_000
    assert persisted_receipts[0].usd_equivalent_nanos == 4_000_000
    assert persisted_receipts[1].event_id == "event-1"
    assert persisted_receipts[1].ordinal == 1
    assert persisted_receipts[1].status == "pending"
    assert persisted_receipts[1].usd_equivalent_nanos is None


@pytest.mark.asyncio
async def test_finalize_marks_partial_member_receipt_coverage() -> None:
    storage = _Storage()
    sink = SessionUsageEventSink(storage)

    await sink.finalize(_call(), replace(_result(), missing_usage_entries=2))

    _, completion, items = storage.finalized[0]
    assert completion.coverage_status == "usage_missing"
    assert completion.missing_cost_entries == 2
    assert completion.cost_nanos == sum(item.cost_nanos for item in items)


@pytest.mark.asyncio
async def test_priced_ensemble_member_does_not_hide_unpriced_sibling() -> None:
    storage = _Storage()
    sink = SessionUsageEventSink(storage)
    priced = UsageCallItem(
        ordinal=0,
        provider="provider-a",
        model="model-a",
        input_tokens=5,
        output_tokens=0,
        reasoning_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        billed_cost_nanos=4,
        estimated_cost_nanos=0,
        cost_source="provider_billed",
        estimate_basis=None,
        price_source=None,
    )
    unpriced = UsageCallItem(
        ordinal=1,
        provider="provider-b",
        model="model-b",
        input_tokens=6,
        output_tokens=7,
        reasoning_tokens=3,
        cache_read_tokens=2,
        cache_write_tokens=1,
        billed_cost_nanos=0,
        estimated_cost_nanos=0,
        cost_source="unavailable",
        estimate_basis=None,
        price_source=None,
    )
    result = replace(
        _result(),
        billed_cost_nanos=4,
        estimated_cost_nanos=0,
        cost_source="provider_billed",
        items=(priced, unpriced),
    )

    await sink.finalize(_call(), result)

    _, completion, items = storage.finalized[0]
    assert completion.coverage_status == "pricing_missing"
    assert completion.missing_cost_entries == 1
    assert completion.cost_nanos == sum(item.cost_nanos for item in items)


@pytest.mark.asyncio
async def test_finalize_collapses_non_reconciling_items_to_envelope() -> None:
    storage = _Storage()
    sink = SessionUsageEventSink(storage)
    result = _result()
    incomplete_item = replace(result.items[0], input_tokens=1, billed_cost_nanos=1)

    await sink.finalize(_call(), replace(result, items=(incomplete_item,)))

    _, completion, items = storage.finalized[0]
    assert len(items) == 1
    assert items[0].input_tokens == completion.input_tokens == 11
    assert items[0].cost_nanos == completion.cost_nanos == 9


@pytest.mark.asyncio
async def test_single_item_reconciliation_fallback_preserves_its_receipt() -> None:
    storage = _Storage()
    sink = SessionUsageEventSink(storage)
    receipt = ProviderBillingReceipt(
        currency="CNY",
        status="confirmed",
        amount_nanos=27_900_000,
        usd_equivalent_nanos=4_000_000,
        fx_native_per_usd_nanos=6_975_000_000,
    )
    result = replace(
        _result(),
        billed_cost_nanos=4_000_000,
        estimated_cost_nanos=0,
        cost_source="provider_billed",
        items=(
            replace(
                _result().items[0],
                input_tokens=1,
                billed_cost_nanos=4_000_000,
                estimated_cost_nanos=0,
                cost_source="provider_billed",
                billing_receipt=receipt,
            ),
        ),
    )

    await sink.finalize(_call(provider="tokenrhythm"), result)

    _, completion, [item] = storage.finalized[0]
    [persisted_receipt] = storage.finalized_receipts[0]
    assert item.input_tokens == completion.input_tokens == 11
    assert item.billed_cost_nanos == completion.billed_cost_nanos == 4_000_000
    assert persisted_receipt.ordinal == item.ordinal == 0
    assert persisted_receipt.usd_equivalent_nanos == item.billed_cost_nanos


@pytest.mark.asyncio
async def test_finalize_failure_schedules_bounded_idempotent_retry() -> None:
    storage = _Storage()
    storage.fail_finalize = 1
    sink = SessionUsageEventSink(storage, retry_delays=(0.0,))

    with pytest.raises(RuntimeError, match="busy"):
        await sink.finalize(_call(), _result())

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(storage.finalized) == 1
    await sink.close()


@pytest.mark.asyncio
async def test_finalize_retry_retains_confirmed_zero_receipt() -> None:
    storage = _Storage()
    storage.fail_finalize = 1
    sink = SessionUsageEventSink(storage, retry_delays=(0.0,))
    receipt = ProviderBillingReceipt(
        currency="CNY",
        status="confirmed",
        amount_nanos=0,
        usd_equivalent_nanos=0,
        fx_native_per_usd_nanos=6_975_000_000,
    )
    item = replace(
        _result().items[0],
        billed_cost_nanos=0,
        estimated_cost_nanos=0,
        cost_source="provider_billed",
        billing_receipt=receipt,
    )
    result = replace(
        _result(),
        billed_cost_nanos=0,
        estimated_cost_nanos=0,
        cost_source="provider_billed",
        items=(item,),
    )

    with pytest.raises(RuntimeError, match="busy"):
        await sink.finalize(_call(provider="tokenrhythm"), result)

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert storage.finalized[0][1].cost_source == "provider_billed"
    [persisted_receipt] = storage.finalized_receipts[0]
    assert persisted_receipt.amount_nanos == 0
    assert persisted_receipt.usd_equivalent_nanos == 0
    await sink.close()


@pytest.mark.asyncio
async def test_sink_persists_confirmed_zero_receipt_through_session_storage(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    sink = SessionUsageEventSink(storage)
    receipt = ProviderBillingReceipt(
        currency="CNY",
        status="confirmed",
        amount_nanos=0,
        usd_equivalent_nanos=0,
        fx_native_per_usd_nanos=6_975_000_000,
    )
    item = replace(
        _result().items[0],
        provider="tokenrhythm",
        billed_cost_nanos=0,
        estimated_cost_nanos=0,
        cost_source="provider_billed",
        billing_receipt=receipt,
    )
    result = replace(
        _result(),
        billed_cost_nanos=0,
        estimated_cost_nanos=0,
        cost_source="provider_billed",
        items=(item,),
    )
    call = _call(provider="tokenrhythm")
    try:
        await sink.start(call)
        await sink.finalize(call, result)

        [persisted_item] = await storage.query_usage_event_items([call.event_id])
        [persisted_receipt] = await storage.query_usage_item_billing_receipts(
            [call.event_id]
        )
        assert persisted_item.cost_source == "provider_billed"
        assert persisted_item.billed_cost_nanos == 0
        assert persisted_item.estimated_cost_nanos == 0
        assert persisted_receipt.status == "confirmed"
        assert persisted_receipt.amount_nanos == 0
        assert persisted_receipt.usd_equivalent_nanos == 0
        assert persisted_receipt.fx_native_per_usd_nanos == 6_975_000_000
    finally:
        await sink.close()
        await storage.close()


@pytest.mark.asyncio
async def test_close_drains_retry_before_cancelling_background_tasks() -> None:
    storage = _Storage()
    storage.fail_finalize = 1
    sink = SessionUsageEventSink(storage, retry_delays=(0.01,))

    with pytest.raises(RuntimeError, match="busy"):
        await sink.finalize(_call(), _result())

    await sink.close()

    assert len(storage.finalized) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("provider_error:503", "provider_error:503"),
        ("provider_error:vendor-private-code", "provider_error"),
        ("provider_error:https://provider.invalid/?key=sk-secret", "provider_error"),
        ("provider_error:503\nsecret", "provider_error"),
        ("cancelled", "cancelled"),
        ("arbitrary internal detail", "usage_unknown"),
    ],
)
async def test_unknown_provider_receipt_is_explicitly_closed(
    reason: str,
    expected: str,
) -> None:
    storage = _Storage()
    sink = SessionUsageEventSink(storage)

    await sink.mark_unknown(_call(), reason)

    assert storage.unknown[0][0] == "event-1"
    assert storage.unknown[0][1] >= 1_000
    assert storage.unknown[0][2] == expected
