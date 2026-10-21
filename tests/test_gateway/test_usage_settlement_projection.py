"""Historical missing-price evidence is corrected only in the read projection."""

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from opensquilla.gateway.rpc_usage import _query_usage
from opensquilla.gateway.usage_settlement_projection import apply_current_settlements
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.session.usage_ledger import (
    UsageEventCompletion,
    UsageEventItem,
    UsageEventStart,
)
from opensquilla.usage_metering import UsageSettlementRevision


def _legacy_row(**overrides):
    return {
        "event_id": "old",
        "ordinal": 0,
        "cost_source": "free",
        "price_source": "custom_free",
        "estimate_basis": "free",
        "cost_nanos": 0,
        "billed_cost_nanos": 0,
        "estimated_cost_nanos": 0,
        "input_tokens": 10,
        **overrides,
    }


@pytest.mark.parametrize("with_items", [False, True])
def test_old_custom_fallback_becomes_unpriced_without_mutation_or_double_count(with_items):
    event = _legacy_row()
    items = {"old": [_legacy_row()]} if with_items else {}
    before = deepcopy((event, items))
    projected = apply_current_settlements([event], items, [], [])
    [updated], updated_items, _ = projected
    assert updated["cost_source"] == "unavailable"
    assert updated["estimate_basis"] is None
    assert updated["missing_cost_entries"] == 1
    assert updated["input_tokens"] == 10 and updated["cost_nanos"] == 0
    if with_items:
        assert updated_items["old"][0]["cost_source"] == "unavailable"
        assert updated_items["old"][0]["missing_cost_entries"] == 1
    assert (event, items) == before
    assert apply_current_settlements(*projected, []) == projected


@pytest.mark.parametrize("price_source", [None, "user_override", "local_free", "default"])
def test_other_or_unproven_free_prices_are_preserved(price_source):
    event = _legacy_row(price_source=price_source)
    items = {"old": [event.copy()]}
    assert apply_current_settlements([event], items, [], []) == ([event], items, [])


@pytest.mark.parametrize("amount", [0, 100_000_000])
def test_confirmed_native_receipt_protects_old_zero_label(amount):
    event = _legacy_row()
    items = {"old": [event.copy()]}
    receipt = {"event_id": "old", "ordinal": 0, "status": "confirmed", "amount_nanos": amount}
    assert apply_current_settlements([event], items, [receipt], []) == ([event], items, [receipt])


@pytest.mark.parametrize("authority", ["local_estimate", "provider_estimate", "provider_confirmed"])
def test_current_settlement_takes_precedence_over_legacy_custom_label(authority):
    revision = UsageSettlementRevision(
        "current", authority, "final", native_amount="0", usd_amount="0"
    )
    row = {**asdict(revision), **revision.cost_fields(), "event_id": "old", "ordinal": 0}
    [event], items, _ = apply_current_settlements(
        [_legacy_row()], {"old": [_legacy_row()]}, [], [row]
    )
    expected = "provider_billed" if authority == "provider_confirmed" else "opensquilla_estimate"
    assert event["cost_source"] == items["old"][0]["cost_source"] == expected
    assert event["missing_cost_entries"] == 0


def test_one_unpriced_item_preserves_billed_sibling_and_missing_usage():
    billed = _legacy_row(
        ordinal=1,
        price_source=None,
        cost_source="provider_billed",
        cost_nanos=10,
        billed_cost_nanos=10,
    )
    event = _legacy_row(
        cost_source="mixed", cost_nanos=10, billed_cost_nanos=10, missing_cost_entries=1
    )
    [updated], _, _ = apply_current_settlements([event], {"old": [_legacy_row(), billed]}, [], [])
    assert updated["cost_source"] == "mixed"
    assert updated["cost_nanos"] == updated["billed_cost_nanos"] == 10
    assert updated["missing_cost_entries"] == 2


@pytest.mark.asyncio
async def test_usage_rpc_marks_historical_custom_price_unknown_without_database_rewrite(
    tmp_path, monkeypatch
):
    storage = await SessionStorage.open(str(tmp_path / "legacy-price.db"))
    try:
        await storage.initialize_usage_ledger(1)
        await storage.upsert_session(
            SessionNode(session_key="agent:main:webchat:legacy-price", session_id="owner")
        )
        await storage.start_usage_event(UsageEventStart("old", "old-run", 1, "owner", 100))
        completion = UsageEventCompletion(
            200,
            input_tokens=10,
            total_tokens=10,
            cost_source="free",
            estimate_basis="free",
            price_source="custom_free",
        )
        item = UsageEventItem(
            "old",
            0,
            provider="custom",
            model="legacy-model",
            input_tokens=10,
            total_tokens=10,
            cost_source="free",
            estimate_basis="free",
            price_source="custom_free",
        )
        await storage.finalize_usage_event("old", completion, items=(item,))
        before = await storage.query_usage_events(None, None)
        monkeypatch.setattr("opensquilla.gateway.usage_query.time.time", lambda: 1.0)
        report = await _query_usage(
            {"range": {"preset": "all"}, "timezone": "UTC"},
            SimpleNamespace(session_manager=SessionManager(storage)),
        )
        assert report["totals"]["eventCount"] == 1
        assert report["totals"]["costSource"] == "unavailable"
        assert report["totals"]["missingCostEntries"] == 1
        assert report["taskGroups"][0]["totals"]["missingCostEntries"] == 1
        assert report["models"][0]["totals"]["costSource"] == "unavailable"
        assert await storage.query_usage_events(None, None) == before
        assert await storage.query_usage_event_items(["old"]) == [item]
        assert await storage.query_usage_settlements(["old"], history=True) == []
    finally:
        await storage.close()
