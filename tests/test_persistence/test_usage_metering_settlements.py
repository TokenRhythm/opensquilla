"""Exact metering, replacement settlement and durable replay contracts."""

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from opensquilla.engine.usage_accounting import normalize_provider_usage
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.session.usage_ledger import (
    UsageEventCompletion,
    UsageEventItem,
    UsageEventStart,
    UsageLedgerConflictError,
)
from opensquilla.usage_metering import (
    UsageItemMetering,
    UsageMeter,
    UsagePriceRule,
    UsagePriceSnapshot,
    UsageSettlementRevision,
    estimate_metered_cost,
)


def test_snapshots_are_content_identified_and_dimensions_immutable():
    original = {"resolution": "720p"}
    rule = UsagePriceRule("video", "second", "0.1", dimensions=original)
    first = UsagePriceSnapshot("catalog", "v1", (rule,))
    original["resolution"] = "1080p"
    assert rule.dimensions["resolution"] == "720p"
    with pytest.raises(TypeError):
        rule.dimensions["resolution"] = "1080p"
    assert first.snapshot_id == replace(first).snapshot_id
    assert first.snapshot_id != replace(first, source_revision="v2").snapshot_id


def test_pending_provider_candidate_is_revision_evidence_not_an_extra_charge():
    quote = UsageSettlementRevision(
        "initial",
        "local_estimate",
        "pending",
        native_amount="0.1",
        usd_amount="0.1",
        provider_pending_native_amount="999.0000000001",
        provider_pending_currency="CNY",
    )
    assert quote.provider_pending_native_amount == "999.0000000001"
    assert quote.cost_fields()["estimated_cost_nanos"] == 100_000_000
    assert quote.cost_fields()["billed_cost_nanos"] == 0
    for invalid in (
        {"provider_pending_currency": None},
        {"provider_pending_native_amount": None},
        {"provider_pending_currency": "cny"},
        {"provider_pending_currency": "ＵＳＤ"},
        {"provider_pending_native_amount": "NaN"},
        {"provider_pending_native_amount": "-1"},
    ):
        with pytest.raises(ValueError):
            replace(quote, **invalid)
    assert replace(quote, provider_pending_native_amount="0").provider_pending_native_amount == "0"


def test_official_media_catalog_prices_and_unknown_skus(monkeypatch):
    from opensquilla.provider import media_pricing

    monkeypatch.setattr(media_pricing, "_FEEDS", {})
    rows = [
        {
            "id": "qwen-image",
            "currency": "CNY",
            "billingMode": "per_image",
            "billingUnit": 1,
            "pricePerImage": "0.2",
        },
        {
            "id": "wan",
            "videoPricing": {
                "pricingScheme": "wan_total_seconds",
                "outputPerSecondCny": {"720P": "0.6"},
                "inputVideoPerSecondCny": {"720P": "0.6"},
                "inputImagePerImageCny": "0",
                "inputAudioPerSecondCny": "0",
            },
        },
    ]
    base = "https://tokenrhythm.studio/v1"
    assert media_pricing.register_media_catalog("tokenrhythm", rows, base_url=base) == 2
    image = media_pricing.resolve_media_price(
        "tokenrhythm", "qwen-image", "image_generation", base, {}
    )
    assert image.currency == "CNY"
    assert estimate_metered_cost((UsageMeter("output_images", "image", "2"),), image)[0] == Decimal(
        "0.4"
    )
    video = media_pricing.resolve_media_price(
        "tokenrhythm", "wan", "video_generation", base, {"resolution": "720P"}
    )
    meters = (
        UsageMeter("video_seconds", "second", "4", dimensions={"resolution": "720p"}),
        UsageMeter("input_video_seconds", "second", "0", dimensions={"resolution": "720p"}),
    )
    assert estimate_metered_cost(meters, video) == (Decimal("2.4"), ())
    assert (
        media_pricing.resolve_media_price(
            "tokenrhythm",
            "wan",
            "video_generation",
            "https://custom.invalid/v1",
            {"resolution": "720p"},
        )
        is None
    )
    assert (
        media_pricing.resolve_media_price("tokenrhythm", "wan", "video_generation", base, {})
        is None
    )
    or_base = "https://openrouter.ai/api/v1"
    or_rows = [
        {
            "id": "video",
            "pricing_skus": {
                "cents_per_video_output_second_720p": "3",
                "cents_per_image_input": "1",
            },
        },
        {
            "id": "minimum",
            "pricing_skus": {"cents_per_second_output": "3", "minimum_cents_per_generation": "56"},
        },
    ]
    assert media_pricing.register_media_catalog("openrouter", or_rows, base_url=or_base) == 1
    assert (
        media_pricing.resolve_media_price("openrouter", "minimum", "video_generation", or_base, {})
        is None
    )
    assert (
        media_pricing.resolve_media_price(
            "openrouter", "video", "video_generation", or_base, {"resolution": "720p"}
        )
        .rules[0]
        .amount
        == "0.03"
    )


def test_missing_measurement_is_not_known_zero():
    assert UsageMeter("images", "image", "0").known
    assert not UsageMeter("images", "image", None, known=False).known
    with pytest.raises(ValueError):
        UsageMeter("images", "image", None)
    normalized = normalize_provider_usage(
        SimpleNamespace(), default_provider="fixture", default_model="fixture", completed_at_ms=1
    )
    assert normalized.items[0].token_usage_known is False
    assert normalized.items[0].meters[0].quantity is None


def test_exact_native_amount_survives_compatibility_rounding():
    revision = UsageSettlementRevision(
        "xai-tick",
        "provider_confirmed",
        "final",
        native_amount="0.0000000001",
        usd_amount="0.0000000001",
    )
    assert revision.native_amount == "0.0000000001"
    assert revision.cost_fields()["cost_nanos"] == 0
    assert revision.cost_fields()["missing_cost_entries"] == 0
    assert revision.cost_fields()["cost_source"] == "provider_billed"


def test_price_rules_respect_units_cache_inclusion_and_missing_price():
    snapshot = UsagePriceSnapshot(
        "fixture",
        "v1",
        (
            UsagePriceRule("input", "token", "3", "1000000", subtract_metrics=("cache",)),
            UsagePriceRule("cache", "token", "0.3", "1000000"),
            UsagePriceRule("video", "second", "0.05"),
        ),
    )
    meters = (
        UsageMeter("input", "token", "1000"),
        UsageMeter("cache", "token", "400"),
        UsageMeter("video", "second", "4"),
    )
    assert estimate_metered_cost(meters, snapshot) == (Decimal("0.20192"), ())
    assert estimate_metered_cost((*meters, UsageMeter("search", "request", "1")), snapshot) == (
        None,
        ("search",),
    )
    assert (
        estimate_metered_cost(
            (replace(meters[0], quantity=None, known=False), *meters[1:]), snapshot
        )[0]
        is None
    )
    with pytest.raises(ValueError, match="exceed"):
        estimate_metered_cost((replace(meters[0], quantity="1"), *meters[1:]), snapshot)
    with pytest.raises(ValueError, match="multiple"):
        estimate_metered_cost(meters, replace(snapshot, rules=(*snapshot.rules, snapshot.rules[0])))


def test_missing_token_usage_never_falls_back_to_known_zero_or_token_price(monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    event = SimpleNamespace(
        input_tokens=0,
        output_tokens=0,
        token_usage_known=False,
        model="gpt-4o",
        usage_meters=(UsageMeter("images", "image", "1"),),
        settlement=UsageSettlementRevision(
            "estimate", "provider_estimate", "final", native_amount="0.04", usd_amount="0.04"
        ),
    )
    result = normalize_provider_usage(
        event, default_provider="openai", default_model="gpt-4o", completed_at_ms=200
    )
    assert result.estimated_cost_nanos == 40_000_000
    assert result.items[0].token_usage_known is False
    assert result.items[0].price_snapshot is None
    assert result.items[0].settlement.authority == "provider_estimate"


@pytest.mark.parametrize("provider", ["openai", "custom"])
def test_unpriced_cloud_and_custom_are_unknown(monkeypatch, provider):
    monkeypatch.setenv("OPENSQUILLA_OPENROUTER_LIVE_PRICING", "0")
    event = SimpleNamespace(input_tokens=10, output_tokens=20, model="fixture-never-priced-model")
    result = normalize_provider_usage(
        event, default_provider=provider, default_model=event.model, completed_at_ms=200
    )
    assert result.cost_source == "unavailable"
    assert result.items[0].settlement.authority == "unknown"


async def _initial(storage):
    await storage.start_usage_event(UsageEventStart("event", "run", 1, "session", 100))
    initial = UsageSettlementRevision(
        "initial", "local_estimate", "pending", native_amount="0.25", usd_amount="0.25"
    )
    await storage.finalize_usage_event(
        "event",
        UsageEventCompletion(
            200,
            cost_nanos=250_000_000,
            estimated_cost_nanos=250_000_000,
            cost_source="opensquilla_estimate",
        ),
        items=(
            UsageEventItem(
                "event",
                0,
                cost_nanos=250_000_000,
                estimated_cost_nanos=250_000_000,
                cost_source="opensquilla_estimate",
            ),
        ),
        metering={
            0: UsageItemMetering(
                (UsageMeter("video", "second", None, known=False),),
                settlement=initial,
                token_usage_known=False,
            )
        },
    )


@pytest.mark.asyncio
async def test_revision_replaces_estimate_preserves_history_and_survives_reopen(tmp_path):
    path = str(tmp_path / "ledger.db")
    storage = await SessionStorage.open(path)
    try:
        initial_revision = await storage.get_usage_accounting_revision()
        await _initial(storage)
        after_initial = await storage.get_usage_accounting_revision()
        assert after_initial > initial_revision
        final = UsageSettlementRevision(
            "receipt-1",
            "provider_confirmed",
            "final",
            "CNY",
            "0.7000000001",
            "0.1",
            previous_revision_id="initial",
        )
        meters = (UsageMeter("video", "second", "4"),)
        current = await storage.append_usage_settlement("event", 0, final, meters=meters)
        assert current["billed_cost_nanos"] == 100_000_000
        assert current["estimated_cost_nanos"] == 0
        after_final = await storage.get_usage_accounting_revision()
        assert after_final > after_initial
        duplicate = await storage.append_usage_settlement("event", 0, final, meters=meters)
        assert duplicate == {**current, "transition_applied": False}
        assert await storage.get_usage_accounting_revision() == after_final
        assert len(await storage.query_usage_settlements(["event"], history=True)) == 2
        assert await storage.get_usage_accounting_revision() == after_final
        with pytest.raises(UsageLedgerConflictError):
            await storage.append_usage_settlement(
                "event", 0, replace(final, native_amount="0.8"), meters=meters
            )
        with pytest.raises(UsageLedgerConflictError):
            await storage.append_usage_settlement("event", 0, replace(final, revision_id="stale"))
        with pytest.raises(UsageLedgerConflictError):
            await storage.append_usage_settlement(
                "event",
                0,
                UsageSettlementRevision(
                    "downgrade",
                    "local_estimate",
                    "final",
                    usd_amount="0.3",
                    previous_revision_id="receipt-1",
                ),
            )
        with pytest.raises(UsageLedgerConflictError):
            await storage.append_usage_settlement(
                "event",
                0,
                replace(final, revision_id="changed", previous_revision_id="receipt-1"),
                meters=(UsageMeter("video", "second", "5"),),
            )
        assert len(await storage.query_usage_settlements(["event"], history=True)) == 2
        async with storage.conn.execute(
            "SELECT cost_nanos FROM usage_event_items WHERE event_id='event'"
        ) as cur:
            assert (await cur.fetchone())[0] == 250_000_000  # immutable original
    finally:
        await storage.close()
    reopened = await SessionStorage.open(path)
    try:
        assert (await reopened.query_usage_settlements(["event"]))[0][
            "native_amount"
        ] == "0.7000000001"
        row = (await reopened.query_usage_item_metering(["event"]))[0]
        assert row["meters"][0]["quantity"] == "4"
        assert row["token_usage_known"] is False
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_independent_writers_share_one_settlement_cas_and_replay(tmp_path):
    path = str(tmp_path / "ledger.db")
    first = await SessionStorage.open(path)
    second = None
    try:
        await _initial(first)
        second = await SessionStorage.open(path)
        initial_revision = await first.get_usage_accounting_revision()
        await asyncio.gather(_initial(first), _initial(second))
        assert await first.get_usage_accounting_revision() == initial_revision
        candidates = [
            UsageSettlementRevision(
                identity,
                "provider_confirmed",
                "final",
                native_amount=amount,
                usd_amount=amount,
                previous_revision_id="initial",
            )
            for identity, amount in (("receipt-a", "0.3"), ("receipt-b", "0.4"))
        ]
        results = await asyncio.gather(
            first.append_usage_settlement("event", 0, candidates[0]),
            second.append_usage_settlement("event", 0, candidates[1]),
            return_exceptions=True,
        )
        assert sum(isinstance(result, UsageLedgerConflictError) for result in results) == 1
        assert (
            sum(isinstance(result, dict) and result["transition_applied"] for result in results)
            == 1
        )
        [current] = await first.query_usage_settlements(["event"])
        winner = next(
            candidate for candidate in candidates if candidate.revision_id == current["revision_id"]
        )
        before_replay = await first.get_usage_accounting_revision()
        replays = await asyncio.gather(
            first.append_usage_settlement("event", 0, winner),
            second.append_usage_settlement("event", 0, winner),
        )
        assert all(result["transition_applied"] is False for result in replays)
        assert await second.get_usage_accounting_revision() == before_replay
        assert len(await first.query_usage_settlements(["event"], history=True)) == 2
        events = await first.query_usage_events(0, 1000)
        assert len(events) == 1
    finally:
        if second is not None:
            await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_revision_measurements_remain_scoped_to_each_event_and_item(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "ledger.db"))
    try:
        for event_index in range(2):
            event_id = f"event-{event_index}"
            await storage.start_usage_event(
                UsageEventStart(event_id, "run", event_index, "session", 100)
            )
            await storage.finalize_usage_event(
                event_id,
                UsageEventCompletion(200, cost_source="unavailable", missing_cost_entries=2),
                items=tuple(
                    UsageEventItem(event_id, ordinal, cost_source="unavailable")
                    for ordinal in range(2)
                ),
                metering={
                    ordinal: UsageItemMetering(
                        meters=(UsageMeter("video", "second", None, known=False),),
                        settlement=UsageSettlementRevision("initial", "unknown", "pending"),
                        token_usage_known=False,
                    )
                    for ordinal in range(2)
                },
            )
            for ordinal in range(2):
                receipt = UsageSettlementRevision(
                    "receipt",
                    "provider_confirmed",
                    "final",
                    native_amount="0.1",
                    usd_amount="0.1",
                    previous_revision_id="initial",
                )
                quantity = str(event_index * 2 + ordinal + 1)
                await storage.append_usage_settlement(
                    event_id, ordinal, receipt, meters=(UsageMeter("video", "second", quantity),)
                )
                await storage.append_usage_settlement(
                    event_id,
                    ordinal,
                    replace(receipt, revision_id="adjustment", previous_revision_id="receipt"),
                )
        rows = await storage.query_usage_item_metering(["event-0", "event-1", "event-0"])
        assert {
            (row["event_id"], row["ordinal"]): row["meters"][0]["quantity"] for row in rows
        } == {("event-0", 0): "1", ("event-0", 1): "2", ("event-1", 0): "3", ("event-1", 1): "4"}
        assert len(rows) == 4
        assert all(len(row["meters"]) == 1 and row["meters"][0]["known"] for row in rows)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_metering_failure_rolls_back_envelope_finalize(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "ledger.db"))
    try:
        await storage.start_usage_event(UsageEventStart("event", "run", 1, "session", 100))
        bad = UsageItemMetering(
            (UsageMeter("images", "image", "1"), UsageMeter("images", "image", "2"))
        )
        with pytest.raises(UsageLedgerConflictError):
            await storage.finalize_usage_event(
                "event",
                UsageEventCompletion(200),
                items=(UsageEventItem("event", 0),),
                metering={0: bad},
            )
        async with storage.conn.execute(
            "SELECT status FROM usage_events WHERE event_id='event'"
        ) as cur:
            assert (await cur.fetchone())[0] == "started"
        async with storage.conn.execute("SELECT COUNT(*) FROM usage_event_items") as cur:
            assert (await cur.fetchone())[0] == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_revision_snapshot_persists_atomically(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "ledger.db"))
    try:
        await _initial(storage)
        snapshot = UsagePriceSnapshot("catalog", "v2", (UsagePriceRule("video", "second", "0.05"),))
        revision = UsageSettlementRevision(
            "reprice",
            "local_estimate",
            "final",
            usd_amount="0.2",
            previous_revision_id="initial",
            price_snapshot_id=snapshot.snapshot_id,
        )
        await storage.append_usage_settlement("event", 0, revision, price_snapshot=snapshot)
        stored = await storage.query_usage_price_snapshots([snapshot.snapshot_id])
        assert stored[snapshot.snapshot_id]["rules"][0]["amount"] == "0.05"
        await storage.append_usage_settlement("event", 0, revision, price_snapshot=snapshot)
        assert len(await storage.query_usage_settlements(["event"], history=True)) == 2
    finally:
        await storage.close()


def test_native_receipt_records_fixed_fx_evidence():
    from opensquilla.provider.metered_http import response_measurement

    _, receipt = response_measurement(
        provider="tokenrhythm",
        operation="video_generation",
        base_url="https://tokenrhythm.studio/v1",
        payload={"billing": {"pending": False, "total_cost_cny": "1.2"}},
    )
    assert receipt.native_amount == "1.2"
    assert receipt.usd_amount == "0.172043010752688172"
    assert receipt.fx_native_per_usd == "6.975"
    assert receipt.fx_source == "opensquilla_fixed_provider_normalization"
    assert receipt.fx_source_revision == "CNY-per-USD:6.975"
    with pytest.raises(ValueError, match="positive rate"):
        replace(receipt, fx_native_per_usd="0")
    with pytest.raises(ValueError, match="requires a normalization rate"):
        replace(receipt, fx_native_per_usd=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_kind", ["epoch", "identity"])
async def test_late_settlement_refreshes_spend_gate_but_not_replacement_owner(tmp_path, reset_kind):
    from opensquilla.engine.steps.squilla_router import _session_accumulated_spend
    from opensquilla.provider.fx import settlement_fx_evidence

    storage = await SessionStorage.open(str(tmp_path / "ledger.db"))
    key = "agent:main:webchat:late-video"
    try:
        await storage.initialize_usage_ledger(1)
        await storage.upsert_session(SessionNode(session_key=key, session_id="session"))
        await storage.start_usage_event(UsageEventStart("event", "run", 1, "session", 100))
        await storage.finalize_usage_event(
            "event",
            UsageEventCompletion(
                200,
                cost_source="unavailable",
                missing_cost_entries=1,
                coverage_status="pricing_missing",
            ),
            items=(UsageEventItem("event", 0, cost_source="unavailable"),),
            metering={
                0: UsageItemMetering(
                    settlement=UsageSettlementRevision("initial", "unknown", "pending"),
                    token_usage_known=False,
                )
            },
        )
        pending = await storage.get_session(key)
        assert pending.missing_cost_entries == 1
        final = UsageSettlementRevision(
            "receipt",
            "provider_confirmed",
            "final",
            "CNY",
            "1.2",
            "0.172043010752688172",
            previous_revision_id="initial",
            **settlement_fx_evidence("CNY", "6.975"),
        )
        await storage.append_usage_settlement("event", 0, final)
        current = await storage.get_session(key)
        assert current.total_cost_usd == float(Decimal(final.usd_amount))
        assert current.estimated_cost_component_usd == 0
        assert current.missing_cost_entries == 0
        spend, source = _session_accumulated_spend(
            SimpleNamespace(
                metadata={
                    "session_billed_cost_usd": current.billed_cost_usd,
                    "session_total_cost_usd": current.total_cost_usd,
                    "session_cost_source": current.cost_source,
                }
            )
        )
        assert spend > 0.17 and source == "billed"
        revision = await storage.get_usage_accounting_revision()
        await storage.append_usage_settlement("event", 0, final)
        assert await storage.get_usage_accounting_revision() == revision
        with pytest.raises(UsageLedgerConflictError):
            await storage.append_usage_settlement("event", 0, replace(final, fx_native_per_usd="7"))
        adjustment = replace(
            final,
            revision_id="adjustment",
            previous_revision_id="receipt",
            native_amount="0.6",
            usd_amount="0.086021505376344086",
        )
        await storage.append_usage_settlement("event", 0, adjustment)
        adjusted = await storage.get_session(key)
        assert adjusted.billed_cost_usd == float(Decimal(adjustment.usd_amount))
        assert adjusted.input_tokens == adjusted.output_tokens == 0
        if reset_kind == "epoch":
            await storage.advance_reset_epoch(key)
        else:
            await storage.upsert_session(SessionNode(session_key=key, session_id="replacement"))
        reset = await storage.get_session(key)
        late = replace(
            adjustment,
            revision_id="late-old-owner",
            previous_revision_id="adjustment",
            native_amount="6.975",
            usd_amount="1",
        )
        await storage.append_usage_settlement("event", 0, late)
        after_late = await storage.get_session(key)
        assert (
            after_late.session_id,
            after_late.epoch,
            after_late.billed_cost_usd,
            after_late.total_cost_usd,
        ) == (reset.session_id, reset.epoch, reset.billed_cost_usd, reset.total_cost_usd)
        saved = (await storage.query_usage_settlements(["event"]))[0]
        assert saved["fx_native_per_usd"] == "6.975"
        async with storage.conn.execute(
            "SELECT cost_nanos FROM usage_events WHERE event_id='event'"
        ) as cur:
            assert (await cur.fetchone())[0] == 0
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_initial_media_receipts_refresh_exact_session_spend_without_double_counting(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "ledger.db"))
    key = "agent:main:webchat:initial-media"
    try:
        await storage.initialize_usage_ledger(1)
        await storage.upsert_session(SessionNode(session_key=key, session_id="session"))
        for index in range(2):
            event_id = f"image-{index}"
            await storage.start_usage_event(UsageEventStart(event_id, "run", index, "session", 100))
            receipt = UsageSettlementRevision(
                "initial",
                "provider_confirmed",
                "final",
                native_amount="0.0000000001",
                usd_amount="0.0000000001",
            )
            completion = UsageEventCompletion(200, cost_source="provider_billed")
            items = (UsageEventItem(event_id, 0, cost_source="provider_billed"),)
            metering = {0: UsageItemMetering(settlement=receipt, token_usage_known=False)}
            await storage.finalize_usage_event(event_id, completion, items=items, metering=metering)
            first = await storage.get_session(key)
            revision = await storage.get_usage_accounting_revision()
            await storage.finalize_usage_event(event_id, completion, items=items, metering=metering)
            assert await storage.get_usage_accounting_revision() == revision
            replay = await storage.get_session(key)
            assert replay.total_cost_usd == first.total_cost_usd
        current = await storage.get_session(key)
        assert current.billed_cost_usd == current.total_cost_usd == 0.0000000002
        assert current.cost_source == "provider_billed"
        refreshed = await storage.reconcile_session_usage_totals_from_ledger(
            session_key=key, expected_epoch=0, expected_session_id="session"
        )
        assert refreshed.total_cost_usd == current.total_cost_usd
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_authority", ["unknown", "local_estimate"])
async def test_turn_projections_follow_late_settlement_without_crossing_owners(
    tmp_path, initial_authority
):
    storage = await SessionStorage.open(str(tmp_path / "turn-settlements.db"))
    try:
        initial_amount = "0.25" if initial_authority == "local_estimate" else None
        initial = UsageSettlementRevision(
            "initial", initial_authority, "pending",
            native_amount=initial_amount, usd_amount=initial_amount,
        )
        fields = initial.cost_fields()
        for event_id, session_id, epoch, turn_id in (
            ("video", "session", 0, "turn-video"),
            ("neighbor", "session", 0, "turn-neighbor"),
            ("new-epoch", "session", 1, "turn-video"),
            ("other-owner", "other-session", 0, "turn-video"),
        ):
            await storage.start_usage_event(
                UsageEventStart(
                    event_id, f"run-{event_id}", 0, session_id, 100,
                    session_epoch=epoch, turn_id=turn_id,
                ),
                preserve_session_epoch=True,
            )
            await storage.finalize_usage_event(
                event_id,
                UsageEventCompletion(
                    200, input_tokens=10, output_tokens=2, total_tokens=12, **fields,
                ),
                items=(
                    UsageEventItem(
                        event_id, 0, input_tokens=10, output_tokens=2, total_tokens=12,
                        **{
                            key: value for key, value in fields.items()
                            if key != "missing_cost_entries"
                        },
                    ),
                ),
                metering={0: UsageItemMetering(settlement=initial)},
            )
        owner = {"session_id": "session", "session_epoch": 0}
        pending = await storage.get_turn_usage_projection(**owner, turn_id="turn-video")
        assert pending["billed_cost"] == 0
        assert pending["estimated_cost_component_usd"] == float(initial_amount or 0)
        assert pending["missing_cost_entries"] == int(initial_amount is None)
        final = UsageSettlementRevision(
            "receipt", "provider_confirmed", "final", "CNY",
            "1.2", "0.172043010752688172", previous_revision_id="initial",
        )
        for event_id, amount in (("neighbor", "9"), ("new-epoch", "8"), ("other-owner", "7")):
            await storage.append_usage_settlement(
                event_id, 0,
                replace(final, currency="USD", native_amount=amount, usd_amount=amount),
            )
        await storage.append_usage_settlement("video", 0, final)
        revision = await storage.get_usage_accounting_revision()
        await storage.append_usage_settlement("video", 0, final)
        assert await storage.get_usage_accounting_revision() == revision
        settled = await storage.get_turn_usage_projection(**owner, turn_id="turn-video")
        assert settled["cost_usd"] == settled["billed_cost"] == float(final.usd_amount)
        assert settled["estimated_cost_component_usd"] == settled["missing_cost_entries"] == 0
        assert settled["cost_source"] == "provider_billed"
        assert settled["total_tokens"] == 12

        reader = storage._transcript_reader or storage.conn
        traced = []
        await reader.set_trace_callback(traced.append)
        try:
            single_page = await storage.get_turn_usage_projections(**owner, turn_ids=["turn-video"])
            one_count = sum(sql.lstrip().upper().startswith("SELECT") for sql in traced)
            traced.clear()
            batch = await storage.get_turn_usage_projections(
                **owner, turn_ids=["turn-video", "turn-neighbor", "absent"],
            )
            many_count = sum(sql.lstrip().upper().startswith("SELECT") for sql in traced)
        finally:
            await reader.set_trace_callback(None)
        assert one_count == many_count == 2
        assert single_page == {"turn-video": settled}
        assert batch["turn-video"] == settled
        assert batch["turn-neighbor"]["cost_usd"] == 9
        assert "absent" not in batch
        corrected = replace(
            final, revision_id="adjusted", previous_revision_id="receipt",
            native_amount="0.6", usd_amount="0.086021505376344086",
        )
        await storage.append_usage_settlement("video", 0, corrected)
        adjusted = await storage.get_turn_usage_projection(**owner, turn_id="turn-video")
        assert adjusted["cost_usd"] == float(corrected.usd_amount)
        assert adjusted["total_tokens"] == 12
        assert (await storage.get_turn_usage_projections(
            **owner, turn_ids=["turn-video"],
        ))["turn-video"] == adjusted
        assert len(await storage.query_usage_events(None, None)) == 4
    finally:
        await storage.close()
