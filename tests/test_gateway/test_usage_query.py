from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.gateway.rpc_usage import _handle_usage_query
from opensquilla.gateway.usage_query import (
    UsageQueryValidationError,
    _finish_totals,
    _nanos_decimal,
    _new_totals,
    query_usage_ledger,
    resolve_usage_range,
)
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.session.usage_ledger import (
    UsageBackfillCursor,
    UsageBackfillWrite,
    UsageEventCompletion,
    UsageEventStart,
)


def _ms(value: str, timezone: str = "UTC") -> int:
    parsed = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(timezone))
    return int(parsed.timestamp() * 1000)


def test_native_nanos_decimal_preserves_integer_trailing_zeroes() -> None:
    assert _nanos_decimal(10_000_000_000) == "10"
    assert _nanos_decimal(10_500_000_000) == "10.5"
    assert _nanos_decimal(0) == "0"


def test_totals_keep_mixed_source_for_confirmed_zero_plus_estimate() -> None:
    totals = _new_totals()
    totals["costNanos"] = 1_000
    totals["estimatedCostNanos"] = 1_000
    totals["costSourceCounts"]["mixed"] = 1

    finished = _finish_totals(totals)

    assert finished["billedCostNanos"] == 0
    assert finished["estimatedCostNanos"] == 1_000
    assert finished["costSource"] == "mixed"


@dataclass
class _FakeStorage:
    state: object
    events: list[object]
    items: list[object]
    baselines: list[object]
    receipts: list[object] = field(default_factory=list)
    receipt_state: object | None = None

    async def get_usage_ledger_state(self):
        return self.state

    async def initialize_usage_ledger(self, now_ms=None):  # pragma: no cover - defensive
        return self.state

    async def query_usage_events(self, from_ms, to_ms, *, statuses, session_id=None):
        rows = []
        for event in self.events:
            if event.status not in statuses:
                continue
            if event.occurred_at_ms is None:
                continue
            if from_ms is not None and event.occurred_at_ms < from_ms:
                continue
            if to_ms is not None and event.occurred_at_ms >= to_ms:
                continue
            rows.append(event)
        return rows

    async def query_usage_event_items(self, event_ids):
        selected = set(event_ids)
        return [item for item in self.items if item.event_id in selected]

    async def query_usage_item_billing_receipts(self, event_ids):
        selected = set(event_ids)
        return [receipt for receipt in self.receipts if receipt.event_id in selected]

    async def get_usage_billing_receipt_state(self):
        return self.receipt_state

    async def list_usage_legacy_baselines(self):
        return self.baselines


class _RawItemStorage(_FakeStorage):
    async def query_usage_event_items(self, event_ids):
        return self.items


class _SinglePassItems(list[object]):
    def __init__(self, values: list[object]) -> None:
        super().__init__(values)
        self.iteration_count = 0

    def __iter__(self):
        self.iteration_count += 1
        if self.iteration_count > 1:
            raise AssertionError("usage item rows must be indexed in a single pass")
        return super().__iter__()


class _NoEqualityEvent(SimpleNamespace):
    def __eq__(self, other: object) -> bool:
        raise AssertionError("usage events must be partitioned without equality scans")


def _event(
    event_id: str,
    occurred_at_ms: int,
    *,
    cost_nanos: int,
    origin: str = "live",
    session_id: str = "s1",
    session_epoch: int = 0,
    status: str = "finalized",
    input_tokens: int = 100,
    output_tokens: int = 10,
    reasoning_tokens: int = 0,
    cache_read_tokens: int = 5,
):
    return SimpleNamespace(
        event_id=event_id,
        occurred_at_ms=occurred_at_ms,
        status=status,
        origin=origin,
        session_id=session_id,
        session_epoch=session_epoch,
        provider="test",
        model="model-a",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=0,
        cost_nanos=cost_nanos,
        billed_cost_nanos=0,
        estimated_cost_nanos=cost_nanos,
        cost_source="opensquilla_estimate",
        missing_cost_entries=0,
    )


def _item(
    event_id: str,
    cost_nanos: int,
    *,
    ordinal: int = 0,
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_read_tokens: int = 5,
):
    return SimpleNamespace(
        event_id=event_id,
        ordinal=ordinal,
        provider="test",
        model="model-a",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=0,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=0,
        cost_nanos=cost_nanos,
        billed_cost_nanos=0,
        estimated_cost_nanos=cost_nanos,
        cost_source="opensquilla_estimate",
    )


def _provider_billed_event_and_item(
    event_id: str,
    occurred_at_ms: int,
    *,
    billed_cost_nanos: int,
    session_id: str = "s1",
) -> tuple[object, object]:
    event = _event(
        event_id,
        occurred_at_ms,
        cost_nanos=billed_cost_nanos,
        session_id=session_id,
    )
    event.provider = "tokenrhythm"
    event.billed_cost_nanos = billed_cost_nanos
    event.estimated_cost_nanos = 0
    event.cost_source = "provider_billed"
    item = _item(event_id, billed_cost_nanos)
    item.provider = "tokenrhythm"
    item.billed_cost_nanos = billed_cost_nanos
    item.estimated_cost_nanos = 0
    item.cost_source = "provider_billed"
    return event, item


def _billing_receipt(
    event_id: str,
    *,
    status: str,
    amount_nanos: int | None,
    usd_equivalent_nanos: int | None,
) -> object:
    return SimpleNamespace(
        event_id=event_id,
        ordinal=0,
        currency="CNY",
        status=status,
        amount_nanos=amount_nanos,
        usd_equivalent_nanos=usd_equivalent_nanos,
        fx_native_per_usd_nanos=6_975_000_000,
        schema_version=1,
    )


def test_resolve_calendar_range_uses_local_midnight() -> None:
    now = _ms("2026-07-20T18:00:00", "Asia/Shanghai")
    resolved = resolve_usage_range(
        {
            "schemaVersion": 1,
            "range": {"preset": "last_7_calendar_days"},
            "timezone": "Asia/Shanghai",
        },
        now_ms=now,
    )

    assert resolved.from_ms == _ms("2026-07-14T00:00:00", "Asia/Shanghai")
    assert resolved.to_ms == now


def test_resolve_dst_day_uses_real_zone_boundaries() -> None:
    now = _ms("2026-03-08T18:00:00", "America/New_York")
    resolved = resolve_usage_range(
        {"range": {"preset": "today"}, "timezone": "America/New_York"},
        now_ms=now,
    )

    assert resolved.from_ms == _ms("2026-03-08T00:00:00", "America/New_York")
    # The next local midnight is only 23 real hours after this DST boundary.
    next_midnight = _ms("2026-03-09T00:00:00", "America/New_York")
    assert next_midnight - resolved.from_ms == 23 * 60 * 60 * 1000


def test_invalid_timezone_is_a_validation_error() -> None:
    with pytest.raises(UsageQueryValidationError, match="Unknown IANA timezone"):
        resolve_usage_range(
            {"range": {"preset": "today"}, "timezone": "Not/A_Zone"},
            now_ms=1_000,
        )


@pytest.mark.asyncio
async def test_finite_window_aggregates_only_timestamped_events() -> None:
    cutover = _ms("2026-07-01T00:00:00")
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[
            _event("before", _ms("2026-07-19T23:59:59"), cost_nanos=1_000),
            _event("inside", _ms("2026-07-20T12:00:00"), cost_nanos=9_200_000),
            _event("edge", _ms("2026-07-21T00:00:00"), cost_nanos=2_000),
        ],
        items=[_item("inside", 9_200_000)],
        baselines=[],
    )

    payload = await query_usage_ledger(
        storage,
        {
            "range": {
                "fromMs": _ms("2026-07-20T00:00:00"),
                "toMs": _ms("2026-07-21T00:00:00"),
            },
            "timezone": "UTC",
        },
        now_ms=_ms("2026-07-22T00:00:00"),
    )

    assert payload["totals"]["costNanos"] == 9_200_000
    assert payload["totals"]["costUsd"] == 0.0092
    assert payload["totals"] == payload["attributedTotals"]
    assert payload["coverage"]["status"] == "complete"
    assert payload["days"][0]["date"] == "2026-07-20"
    assert payload["models"][0]["totals"]["costNanos"] == 9_200_000
    assert payload["sessions"][0]["totals"]["costNanos"] == 9_200_000


@pytest.mark.asyncio
async def test_usage_query_serves_the_canonical_receipt_fx_rate() -> None:
    """Clients must render CNY at the rate the billing receipts recorded."""

    cutover = _ms("2026-07-01T00:00:00")
    event, item = _provider_billed_event_and_item(
        "tr-1",
        _ms("2026-07-20T12:00:00"),
        billed_cost_nanos=1_000_000_000,
    )
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[event],
        items=[item],
        baselines=[],
        receipts=[
            _billing_receipt(
                "tr-1",
                status="confirmed",
                amount_nanos=6_975_000_000,
                usd_equivalent_nanos=1_000_000_000,
            )
        ],
        receipt_state=SimpleNamespace(tracking_started_at_ms=cutover),
    )

    payload = await query_usage_ledger(
        storage,
        {"range": {"preset": "today"}, "timezone": "UTC"},
        now_ms=_ms("2026-07-20T18:00:00"),
    )

    assert payload["fxRatesNativePerUsd"] == {"CNY": "6.975"}
    # The served display rate is exactly the normalization rate recorded on
    # the confirmed receipt, so UI conversions from canonical USD reproduce
    # the receipt-exact native amount instead of drifting on their own rate.
    receipt_rates = payload["totals"]["nativeBilledByCurrency"]["CNY"][
        "normalizationRatesNativePerUsd"
    ]
    assert receipt_rates == [payload["fxRatesNativePerUsd"]["CNY"]]


@pytest.mark.asyncio
async def test_confirmed_cny_receipts_include_real_zero_in_every_totals_dimension() -> None:
    cutover = _ms("2026-07-20T00:00:00")
    nonzero_event, nonzero_item = _provider_billed_event_and_item(
        "cny-nonzero",
        _ms("2026-07-20T10:00:00"),
        billed_cost_nanos=2_000_000,
    )
    zero_event, zero_item = _provider_billed_event_and_item(
        "cny-zero",
        _ms("2026-07-20T11:00:00"),
        billed_cost_nanos=0,
    )
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[nonzero_event, zero_event],
        items=[nonzero_item, zero_item],
        baselines=[],
        receipts=[
            _billing_receipt(
                "cny-nonzero",
                status="confirmed",
                amount_nanos=13_950_000,
                usd_equivalent_nanos=2_000_000,
            ),
            _billing_receipt(
                "cny-zero",
                status="confirmed",
                amount_nanos=0,
                usd_equivalent_nanos=0,
            ),
        ],
        receipt_state=SimpleNamespace(
            tracking_started_at_ms=cutover,
            schema_version=1,
        ),
    )

    payload = await query_usage_ledger(
        storage,
        {
            "range": {
                "fromMs": cutover,
                "toMs": _ms("2026-07-21T00:00:00"),
            },
            "timezone": "UTC",
        },
        now_ms=_ms("2026-07-22T00:00:00"),
    )

    expected_native = {
        "CNY": {
            "amountNanos": "13950000",
            "amount": "0.01395",
            "usdEquivalentNanos": "2000000",
            "receiptCount": 2,
            "normalizationRatesNativePerUsd": ["6.975"],
        }
    }
    aggregate_totals = [
        payload["totals"],
        payload["attributedTotals"],
        payload["days"][0]["totals"],
        payload["models"][0]["totals"],
        payload["sessions"][0]["totals"],
        payload["sessions"][0]["modelBreakdown"][0]["totals"],
    ]
    for totals in aggregate_totals:
        assert totals["nativeBilledByCurrency"] == expected_native
        assert totals["pendingBillingReceiptCount"] == 0
        assert totals["nativeBillingExpectedReceiptCount"] == 2
        assert totals["nativeBillingMissingConfirmedReceiptCount"] == 0
        assert totals["billedCostNanos"] == 2_000_000
        assert totals["estimatedCostNanos"] == 0
        assert totals["costSource"] == "provider_billed"
        assert totals["costSourceCounts"]["provider_billed"] == 2

    assert payload["coverage"]["nativeBilling"] == {
        "status": "complete",
        "exactFromMs": cutover,
        "reasonCodes": [],
        "missingConfirmedReceiptCount": 0,
        "pendingReceiptCount": 0,
    }


@pytest.mark.asyncio
async def test_pending_receipt_is_estimated_and_disclosed_in_every_totals_dimension() -> None:
    cutover = _ms("2026-07-20T00:00:00")
    occurred_at = _ms("2026-07-20T10:00:00")
    event = _event("pending", occurred_at, cost_nanos=3_000_000)
    event.provider = "tokenrhythm"
    item = _item("pending", 3_000_000)
    item.provider = "tokenrhythm"
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[event],
        items=[item],
        baselines=[],
        receipts=[
            _billing_receipt(
                "pending",
                status="pending",
                amount_nanos=None,
                usd_equivalent_nanos=None,
            )
        ],
        receipt_state=SimpleNamespace(
            tracking_started_at_ms=cutover,
            schema_version=1,
        ),
    )

    payload = await query_usage_ledger(
        storage,
        {
            "range": {"fromMs": cutover, "toMs": occurred_at + 1},
            "timezone": "UTC",
        },
        now_ms=occurred_at + 2,
    )

    aggregate_totals = [
        payload["totals"],
        payload["attributedTotals"],
        payload["days"][0]["totals"],
        payload["models"][0]["totals"],
        payload["sessions"][0]["totals"],
        payload["sessions"][0]["modelBreakdown"][0]["totals"],
    ]
    for totals in aggregate_totals:
        assert totals["nativeBilledByCurrency"] == {}
        assert totals["pendingBillingReceiptCount"] == 1
        assert totals["nativeBillingExpectedReceiptCount"] == 1
        assert totals["nativeBillingMissingConfirmedReceiptCount"] == 0
        assert totals["billedCostNanos"] == 0
        assert totals["estimatedCostNanos"] == 3_000_000
        assert totals["costSource"] == "opensquilla_estimate"

    native_coverage = payload["coverage"]["nativeBilling"]
    assert native_coverage["status"] == "partial"
    assert native_coverage["reasonCodes"] == ["pending_billing_receipt"]
    assert native_coverage["missingConfirmedReceiptCount"] == 0
    assert native_coverage["pendingReceiptCount"] == 1


@pytest.mark.asyncio
async def test_b5_native_coverage_counts_physical_items_not_envelopes() -> None:
    cutover = _ms("2026-07-20T00:00:00")
    occurred_at = _ms("2026-07-20T10:00:00")
    event, _ = _provider_billed_event_and_item(
        "b5-zero",
        occurred_at,
        billed_cost_nanos=0,
    )
    items = []
    for ordinal in range(5):
        item = _item(
            "b5-zero",
            0,
            ordinal=ordinal,
            input_tokens=20,
            output_tokens=2,
            cache_read_tokens=1,
        )
        item.provider = "tokenrhythm"
        item.billed_cost_nanos = 0
        item.estimated_cost_nanos = 0
        item.cost_source = "provider_billed"
        items.append(item)
    receipts = []
    for ordinal in range(4):
        receipt = _billing_receipt(
            "b5-zero",
            status="confirmed",
            amount_nanos=0,
            usd_equivalent_nanos=0,
        )
        receipt.ordinal = ordinal
        receipts.append(receipt)
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[event],
        items=items,
        baselines=[],
        receipts=receipts,
        receipt_state=SimpleNamespace(
            tracking_started_at_ms=cutover,
            schema_version=1,
        ),
    )

    payload = await query_usage_ledger(
        storage,
        {
            "range": {"fromMs": cutover, "toMs": occurred_at + 1},
            "timezone": "UTC",
        },
        now_ms=occurred_at + 2,
    )

    aggregate_totals = [
        payload["totals"],
        payload["days"][0]["totals"],
        payload["models"][0]["totals"],
        payload["sessions"][0]["totals"],
        payload["sessions"][0]["modelBreakdown"][0]["totals"],
    ]
    for totals in aggregate_totals:
        assert totals["nativeBilledByCurrency"]["CNY"]["receiptCount"] == 4
        assert totals["nativeBillingExpectedReceiptCount"] == 5
        assert totals["nativeBillingMissingConfirmedReceiptCount"] == 1
    assert payload["coverage"]["nativeBilling"]["missingConfirmedReceiptCount"] == 1


@pytest.mark.asyncio
async def test_native_coverage_counts_only_post_cutover_missing_confirmed_receipts() -> None:
    native_cutover = _ms("2026-07-20T12:00:00")
    before_event = _event("before-native-cutover", native_cutover - 1, cost_nanos=1)
    before_item = _item("before-native-cutover", 1)
    estimated_event = _event("estimated-no-receipt", native_cutover + 1, cost_nanos=2)
    estimated_item = _item("estimated-no-receipt", 2)
    unavailable_event = _event("invalid-no-receipt", native_cutover + 2, cost_nanos=0)
    unavailable_event.cost_source = "unavailable"
    unavailable_event.missing_cost_entries = 1
    unavailable_item = _item("invalid-no-receipt", 0)
    unavailable_item.cost_source = "unavailable"
    pending_event = _event("pending-not-missing", native_cutover + 3, cost_nanos=3)
    pending_item = _item("pending-not-missing", 3)
    unrelated_event, unrelated_item = _provider_billed_event_and_item(
        "other-provider",
        native_cutover + 4,
        billed_cost_nanos=4,
    )
    unrelated_event.provider = "anthropic"
    unrelated_item.provider = "anthropic"
    openrouter_estimated_event = _event(
        "openrouter-estimated",
        native_cutover + 5,
        cost_nanos=5,
    )
    openrouter_estimated_event.provider = "openrouter"
    openrouter_estimated_item = _item("openrouter-estimated", 5)
    openrouter_estimated_item.provider = "openrouter"
    for event in (before_event, estimated_event, unavailable_event, pending_event):
        event.provider = "tokenrhythm"
    for item in (before_item, estimated_item, unavailable_item, pending_item):
        item.provider = "tokenrhythm"
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=_ms("2026-07-20T00:00:00"),
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[
            before_event,
            estimated_event,
            unavailable_event,
            pending_event,
            unrelated_event,
            openrouter_estimated_event,
        ],
        items=[
            before_item,
            estimated_item,
            unavailable_item,
            pending_item,
            unrelated_item,
            openrouter_estimated_item,
        ],
        baselines=[],
        receipts=[
            _billing_receipt(
                "pending-not-missing",
                status="pending",
                amount_nanos=None,
                usd_equivalent_nanos=None,
            )
        ],
        receipt_state=SimpleNamespace(
            tracking_started_at_ms=native_cutover,
            schema_version=1,
        ),
    )

    payload = await query_usage_ledger(
        storage,
        {
            "range": {
                "fromMs": _ms("2026-07-20T00:00:00"),
                "toMs": _ms("2026-07-21T00:00:00"),
            },
            "timezone": "UTC",
        },
        now_ms=_ms("2026-07-22T00:00:00"),
    )

    native_coverage = payload["coverage"]["nativeBilling"]
    assert native_coverage == {
        "status": "partial",
        "exactFromMs": native_cutover,
        "reasonCodes": [
            "window_before_native_billing_receipts",
            "missing_confirmed_billing_receipt",
            "pending_billing_receipt",
        ],
        "missingConfirmedReceiptCount": 2,
        "pendingReceiptCount": 1,
    }


@pytest.mark.asyncio
async def test_native_coverage_marks_finalized_tokenrhythm_event_without_item_missing() -> None:
    cutover = _ms("2026-07-20T00:00:00")
    event = _event("missing-item", cutover + 1, cost_nanos=2)
    event.provider = "tokenrhythm"
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[event],
        items=[],
        baselines=[],
        receipt_state=SimpleNamespace(
            tracking_started_at_ms=cutover,
            schema_version=1,
        ),
    )

    payload = await query_usage_ledger(
        storage,
        {"range": {"fromMs": cutover, "toMs": cutover + 2}, "timezone": "UTC"},
        now_ms=cutover + 3,
    )

    assert payload["coverage"]["nativeBilling"]["missingConfirmedReceiptCount"] == 1
    assert "missing_confirmed_billing_receipt" in payload["coverage"][
        "nativeBilling"
    ]["reasonCodes"]


@pytest.mark.asyncio
async def test_unpriced_item_keeps_model_pricing_coverage_partial() -> None:
    now = _ms("2026-07-20T12:00:00")
    event = _event("unpriced", now - 1, cost_nanos=0)
    event.cost_source = "unavailable"
    event.missing_cost_entries = 1
    item = _item("unpriced", 0)
    item.cost_source = "unavailable"
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=_ms("2026-07-20T00:00:00"),
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[event],
        items=[item],
        baselines=[],
    )

    payload = await query_usage_ledger(
        storage,
        {"range": {"preset": "today"}, "timezone": "UTC"},
        now_ms=now,
    )

    assert payload["coverage"]["pricing"] == "partial"
    assert payload["attributedTotals"]["missingCostEntries"] == 1
    assert payload["models"][0]["totals"]["missingCostEntries"] == 1
    assert payload["sessions"][0]["totals"]["missingCostEntries"] == 1


@pytest.mark.asyncio
async def test_model_and_session_aggregation_indexes_item_rows_once() -> None:
    now = _ms("2026-07-20T12:00:00")
    events = [
        _event(
            f"event-{index}",
            now - index - 1,
            cost_nanos=index + 1,
            session_id=f"session-{index}",
        )
        for index in range(64)
    ]
    items = _SinglePassItems(
        [_item(event.event_id, event.cost_nanos) for event in events]
    )
    storage = _RawItemStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=_ms("2026-07-20T00:00:00"),
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=events,
        items=items,
        baselines=[],
    )

    payload = await query_usage_ledger(
        storage,
        {
            "range": {"preset": "today"},
            "timezone": "UTC",
            "include": {"days": False, "models": True, "sessions": True},
        },
        now_ms=now,
    )

    assert items.iteration_count == 1
    assert len(payload["sessions"]) == len(events)
    assert sum(row["totals"]["eventCount"] for row in payload["sessions"]) == len(events)
    assert sum(row["eventCount"] for row in payload["models"]) == len(events)


@pytest.mark.asyncio
async def test_status_partition_does_not_compare_event_records() -> None:
    now = _ms("2026-07-20T12:00:00")
    finalized = _event("finalized", now - 2, cost_nanos=1)
    unknown = _event("unknown", now - 1, cost_nanos=0, status="unknown")
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=_ms("2026-07-20T00:00:00"),
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[
            _NoEqualityEvent(**vars(finalized)),
            _NoEqualityEvent(**vars(unknown)),
        ],
        items=[_item("finalized", 1)],
        baselines=[],
    )

    payload = await query_usage_ledger(
        storage,
        {"range": {"preset": "today"}, "timezone": "UTC"},
        now_ms=now,
    )

    assert payload["totals"]["eventCount"] == 2


@pytest.mark.asyncio
async def test_all_uses_baseline_plus_live_without_double_counting_backfill() -> None:
    cutover = _ms("2026-07-20T00:00:00")
    historical = _event(
        "historical",
        _ms("2026-07-10T12:00:00"),
        cost_nanos=4_000_000,
        origin="legacy_turn",
    )
    live = _event("live", _ms("2026-07-20T12:00:00"), cost_nanos=2_000_000)
    baseline = SimpleNamespace(
        session_id="s1",
        input_tokens=500,
        output_tokens=50,
        cache_read_tokens=5,
        cache_write_tokens=0,
        cost_nanos=10_000_000,
        billed_cost_nanos=0,
        estimated_cost_nanos=10_000_000,
        cost_source="opensquilla_estimate",
        missing_cost_entries=0,
    )
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[historical, live],
        items=[_item("historical", 4_000_000), _item("live", 2_000_000)],
        baselines=[baseline],
    )

    payload = await query_usage_ledger(
        storage,
        {"range": {"preset": "all"}, "timezone": "UTC"},
        now_ms=_ms("2026-07-21T00:00:00"),
    )

    assert payload["attributedTotals"]["costNanos"] == 6_000_000
    legacy = payload["coverage"]["legacyUnattributed"]
    assert legacy["totals"]["costNanos"] == 6_000_000
    assert legacy["includedInTotals"] is True
    # Baseline 10m + live 2m; the 4m backfill is attribution inside the baseline.
    assert payload["totals"]["costNanos"] == 12_000_000
    assert payload["coverage"]["status"] == "partial"


@pytest.mark.asyncio
async def test_all_counts_residual_and_attributed_session_epochs_as_a_union() -> None:
    cutover = _ms("2026-07-20T00:00:00")
    baselines = [
        SimpleNamespace(
            session_id=session_id,
            session_epoch=0,
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=5,
            cache_write_tokens=0,
            cost_nanos=10_000_000,
            billed_cost_nanos=0,
            estimated_cost_nanos=10_000_000,
            cost_source="opensquilla_estimate",
            missing_cost_entries=0,
        )
        for session_id in ("s1", "s2")
    ]
    live_events = [
        _event(
            "live-overlap",
            _ms("2026-07-20T12:00:00"),
            cost_nanos=1_000_000,
            session_id="s1",
        ),
        _event(
            "live-distinct",
            _ms("2026-07-20T12:01:00"),
            cost_nanos=1_000_000,
            session_id="s3",
        ),
    ]
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=live_events,
        items=[_item(event.event_id, event.cost_nanos) for event in live_events],
        baselines=baselines,
    )

    payload = await query_usage_ledger(
        storage,
        {"range": {"preset": "all"}, "timezone": "UTC"},
        now_ms=_ms("2026-07-21T00:00:00"),
    )

    assert payload["attributedTotals"]["sessionCount"] == 2
    assert payload["legacyUnattributed"]["totals"]["sessionCount"] == 2
    assert payload["totals"]["sessionCount"] == 3
    assert payload["sessionCount"] == 3
    assert payload["totals"]["eventCount"] == 2
    assert payload["legacyUnattributed"]["totals"]["eventCount"] == 0


@pytest.mark.asyncio
async def test_all_reconciles_each_session_epoch_and_every_component() -> None:
    cutover = _ms("2026-07-20T00:00:00")
    bad_tokens = _event(
        "bad-tokens",
        _ms("2026-07-10T12:00:00"),
        cost_nanos=8_000_000,
        origin="legacy_turn",
        session_id="s1",
        input_tokens=101,
        output_tokens=0,
        cache_read_tokens=0,
    )
    trusted = _event(
        "trusted",
        _ms("2026-07-11T12:00:00"),
        cost_nanos=4_000_000,
        origin="legacy_turn",
        session_id="s1",
        session_epoch=1,
        input_tokens=20,
        output_tokens=5,
        cache_read_tokens=2,
    )
    live = _event(
        "live",
        _ms("2026-07-20T12:00:00"),
        cost_nanos=2_000_000,
        session_id="s1",
        input_tokens=3,
        output_tokens=1,
        cache_read_tokens=0,
    )
    baselines = [
        SimpleNamespace(
            session_id="s1",
            session_epoch=0,
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=5,
            cache_write_tokens=0,
            cost_nanos=10_000_000,
            billed_cost_nanos=0,
            estimated_cost_nanos=10_000_000,
            cost_source="opensquilla_estimate",
            missing_cost_entries=0,
        ),
        SimpleNamespace(
            session_id="s1",
            session_epoch=1,
            input_tokens=50,
            output_tokens=10,
            cache_read_tokens=5,
            cache_write_tokens=0,
            cost_nanos=10_000_000,
            billed_cost_nanos=0,
            estimated_cost_nanos=10_000_000,
            cost_source="opensquilla_estimate",
            missing_cost_entries=0,
        ),
    ]
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[bad_tokens, trusted, live],
        items=[
            _item("bad-tokens", 8_000_000),
            _item(
                "trusted",
                4_000_000,
                input_tokens=20,
                output_tokens=5,
                cache_read_tokens=2,
            ),
            _item(
                "live",
                2_000_000,
                input_tokens=3,
                output_tokens=1,
                cache_read_tokens=0,
            ),
        ],
        baselines=baselines,
    )

    payload = await query_usage_ledger(
        storage,
        {"range": {"preset": "all"}, "timezone": "UTC"},
        now_ms=_ms("2026-07-21T00:00:00"),
    )

    # The globally affordable 8m event is still rejected because its own
    # session epoch exceeds the baseline input-token component.
    assert payload["attributedTotals"]["costNanos"] == 6_000_000
    assert payload["legacyUnattributed"]["totals"]["costNanos"] == 16_000_000
    assert payload["totals"]["costNanos"] == 22_000_000
    assert payload["totals"]["costNanos"] == (
        payload["totals"]["billedCostNanos"]
        + payload["totals"]["estimatedCostNanos"]
    )
    assert "backfill_component_conflict" in payload["coverage"]["reasonCodes"]
    assert payload["coverage"]["anomalyCount"] == 1
    assert sum(row["totals"]["costNanos"] for row in payload["days"]) == 6_000_000
    assert sum(row["totals"]["costNanos"] for row in payload["models"]) == 6_000_000
    assert sum(row["totals"]["costNanos"] for row in payload["sessions"]) == 6_000_000


@pytest.mark.asyncio
async def test_all_accepts_reasoning_tokens_absent_from_legacy_baseline() -> None:
    cutover = _ms("2026-07-20T00:00:00")
    historical = _event(
        "reasoning-history",
        _ms("2026-07-10T12:00:00"),
        cost_nanos=4_000_000,
        origin="legacy_turn",
        input_tokens=20,
        output_tokens=5,
        reasoning_tokens=1,
        cache_read_tokens=0,
    )
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[historical],
        items=[],
        baselines=[
            SimpleNamespace(
                session_id="s1",
                session_epoch=0,
                input_tokens=20,
                output_tokens=5,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_nanos=4_000_000,
                billed_cost_nanos=0,
                estimated_cost_nanos=4_000_000,
                cost_source="opensquilla_estimate",
                missing_cost_entries=0,
            )
        ],
    )

    payload = await query_usage_ledger(
        storage,
        {"range": {"preset": "all"}, "timezone": "UTC"},
        now_ms=_ms("2026-07-21T00:00:00"),
    )

    assert payload["attributedTotals"]["reasoningTokens"] == 1
    assert payload["legacyUnattributed"]["totals"]["costNanos"] == 0
    assert payload["coverage"]["anomalyCount"] == 0
    assert payload["coverage"]["status"] == "complete"
    assert payload["coverage"]["reasonCodes"] == []


@pytest.mark.asyncio
async def test_finite_pre_cutover_window_is_exact_after_complete_reconciliation() -> None:
    cutover = _ms("2026-07-20T00:00:00")
    historical = _event(
        "historical-exact",
        _ms("2026-07-10T12:00:00"),
        cost_nanos=4_000_000,
        origin="legacy_turn",
        input_tokens=20,
        output_tokens=5,
        cache_read_tokens=2,
    )
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=cutover,
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[historical],
        items=[
            _item(
                "historical-exact",
                4_000_000,
                input_tokens=20,
                output_tokens=5,
                cache_read_tokens=2,
            )
        ],
        baselines=[
            SimpleNamespace(
                session_id="s1",
                session_epoch=0,
                input_tokens=20,
                output_tokens=5,
                cache_read_tokens=2,
                cache_write_tokens=0,
                cost_nanos=4_000_000,
                billed_cost_nanos=0,
                estimated_cost_nanos=4_000_000,
                cost_source="opensquilla_estimate",
                missing_cost_entries=0,
            )
        ],
    )

    payload = await query_usage_ledger(
        storage,
        {
            "range": {
                "fromMs": _ms("2026-07-10T00:00:00"),
                "toMs": _ms("2026-07-11T00:00:00"),
            },
            "timezone": "UTC",
        },
        now_ms=_ms("2026-07-21T00:00:00"),
    )

    assert payload["totals"] == payload["attributedTotals"]
    assert payload["totals"]["costNanos"] == 4_000_000
    assert payload["legacyUnattributed"]["includedInTotals"] is False
    assert payload["legacyUnattributed"]["totals"]["costNanos"] == 0
    assert payload["coverage"]["timeAttribution"] == "complete"
    assert payload["coverage"]["status"] == "complete"
    assert payload["coverage"]["reasonCodes"] == []


@pytest.mark.asyncio
async def test_unknown_usage_is_successful_partial_data() -> None:
    now = _ms("2026-07-20T12:00:00")
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=_ms("2026-07-20T00:00:00"),
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[_event("unknown", now - 1, cost_nanos=0, status="unknown")],
        items=[],
        baselines=[],
    )

    payload = await query_usage_ledger(
        storage,
        {"range": {"preset": "today"}, "timezone": "UTC"},
        now_ms=now,
    )

    assert payload["coverage"]["status"] == "partial"
    assert "usage_unavailable" in payload["coverage"]["reasonCodes"]
    assert payload["totals"]["missingCostEntries"] == 1


@pytest.mark.asyncio
async def test_usage_query_rpc_reads_the_additive_storage_surface() -> None:
    now = _ms("2026-07-20T12:00:00")
    storage = _FakeStorage(
        state=SimpleNamespace(
            ledger_started_at_ms=_ms("2026-07-01T00:00:00"),
            backfill_status="complete",
            anomaly_count=0,
        ),
        events=[_event("e1", now - 1, cost_nanos=1_000_000)],
        items=[_item("e1", 1_000_000)],
        baselines=[],
    )
    ctx = RpcContext(
        conn_id="test",
        session_manager=SimpleNamespace(storage=storage),
    )

    payload = await _handle_usage_query(
        {
            "range": {"fromMs": now - 60_000, "toMs": now},
            "timezone": "UTC",
        },
        ctx,
    )

    assert payload["source"] == "usage_ledger"
    assert payload["totals"]["costUsd"] == 0.001


@pytest.mark.asyncio
async def test_usage_query_reconciles_real_storage_baseline_backfill_and_live(
    tmp_path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:one",
                session_id="session-1",
                input_tokens=500,
                output_tokens=50,
                total_tokens=550,
                total_cost_usd=0.01,
                estimated_cost_usd=0.01,
                estimated_cost_component_usd=0.01,
                cost_source="opensquilla_estimate",
            )
        )
        await storage.initialize_usage_ledger(1_000)
        historical = UsageBackfillWrite(
            start=UsageEventStart(
                event_id="historical",
                execution_id="historical",
                call_index=0,
                session_id="session-1",
                started_at_ms=100,
                turn_id="historical-turn",
                origin="backfilled_turn",
            ),
            completion=UsageEventCompletion(
                completed_at_ms=200,
                input_tokens=100,
                output_tokens=10,
                total_tokens=110,
                cost_nanos=4_000_000,
                estimated_cost_nanos=4_000_000,
                cost_source="opensquilla_estimate",
            ),
        )
        await storage.apply_usage_backfill_batch(
            (historical,),
            cursor=UsageBackfillCursor(200, "session-1", "message-1"),
            exhausted=True,
            now_ms=1_050,
        )
        await storage.start_usage_event(
            UsageEventStart(
                event_id="live",
                execution_id="live",
                call_index=0,
                session_id="session-1",
                started_at_ms=1_100,
                origin="live_provider",
            )
        )
        await storage.finalize_usage_event(
            "live",
            UsageEventCompletion(
                completed_at_ms=1_200,
                input_tokens=20,
                output_tokens=5,
                total_tokens=25,
                cost_nanos=2_000_000,
                estimated_cost_nanos=2_000_000,
                cost_source="opensquilla_estimate",
            ),
        )

        payload = await query_usage_ledger(
            storage,
            {"range": {"preset": "all"}, "timezone": "UTC"},
            now_ms=2_000,
        )

        assert payload["attributedTotals"]["costNanos"] == 6_000_000
        assert payload["legacyUnattributed"]["totals"]["costNanos"] == 6_000_000
        assert payload["totals"]["costNanos"] == 12_000_000
        assert sum(row["totals"]["costNanos"] for row in payload["days"]) == 6_000_000
        assert sum(row["totals"]["costNanos"] for row in payload["models"]) == 6_000_000
        assert sum(row["totals"]["costNanos"] for row in payload["sessions"]) == 6_000_000
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_usage_query_exposes_only_real_navigable_session_keys(tmp_path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    session_key = "agent:main:webchat:navigable"
    try:
        await storage.upsert_session(
            SessionNode(session_key=session_key, session_id="session-navigable")
        )
        await storage.initialize_usage_ledger(1_000)
        await storage.start_usage_event(
            UsageEventStart(
                event_id="live-key",
                execution_id="live-key",
                call_index=0,
                session_id="session-navigable",
                started_at_ms=1_100,
            )
        )
        await storage.finalize_usage_event(
            "live-key",
            UsageEventCompletion(
                completed_at_ms=1_200,
                cost_nanos=1,
                estimated_cost_nanos=1,
                cost_source="opensquilla_estimate",
            ),
        )

        params = {
            "range": {"fromMs": 1_000, "toMs": 2_000},
            "timezone": "UTC",
        }
        present = await query_usage_ledger(storage, params, now_ms=2_000)
        assert present["sessions"][0]["sessionId"] == "session-navigable"
        assert present["sessions"][0]["sessionKey"] == session_key

        await storage.delete_session(session_key)
        deleted = await query_usage_ledger(storage, params, now_ms=2_000)
        assert deleted["sessions"][0]["sessionId"] == "session-navigable"
        assert deleted["sessions"][0]["sessionKey"] is None
    finally:
        await storage.close()
