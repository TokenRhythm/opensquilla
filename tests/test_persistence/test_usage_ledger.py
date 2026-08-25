"""Durable usage-ledger storage contract tests."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SCHEMA_VERSION, SessionStorage
from opensquilla.session.usage_ledger import (
    UsageEventCompletion,
    UsageEventItem,
    UsageEventStart,
    UsageItemBillingReceipt,
    UsageLedgerConflictError,
    nanos_to_usd,
    usd_to_nanos,
)


def _start(
    event_id: str = "event-1",
    *,
    execution_id: str = "execution-1",
    call_index: int = 0,
    session_id: str = "session-1",
    started_at_ms: int = 100,
) -> UsageEventStart:
    return UsageEventStart(
        event_id=event_id,
        execution_id=execution_id,
        call_index=call_index,
        session_id=session_id,
        started_at_ms=started_at_ms,
        turn_id="turn-1",
        agent_run_id="run-1",
        provider="openai",
        model="gpt-test",
    )


def _completion(
    *,
    completed_at_ms: int = 200,
    cost_nanos: int = 9_200_000,
) -> UsageEventCompletion:
    return UsageEventCompletion(
        completed_at_ms=completed_at_ms,
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        cost_nanos=cost_nanos,
        billed_cost_nanos=0,
        estimated_cost_nanos=cost_nanos,
        cost_source="estimate",
        provider="openai",
        model="gpt-test",
        estimate_basis="catalog",
    )


def _item(event_id: str = "event-1") -> UsageEventItem:
    return UsageEventItem(
        event_id=event_id,
        ordinal=0,
        provider="openai",
        model="gpt-test",
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        cost_nanos=9_200_000,
        estimated_cost_nanos=9_200_000,
        cost_source="estimate",
    )


def test_nano_usd_conversion_is_decimal_and_bounded() -> None:
    assert usd_to_nanos(0.0092) == 9_200_000
    assert usd_to_nanos(Decimal("0.0000000005")) == 1
    assert nanos_to_usd(9_200_000) == 0.0092
    with pytest.raises(ValueError, match="non-negative"):
        usd_to_nanos(-1)
    with pytest.raises(ValueError, match="finite"):
        usd_to_nanos(float("nan"))
    with pytest.raises(OverflowError):
        usd_to_nanos("10000000000")


def test_session_schema_version_includes_artifact_mutations_and_routing_state() -> None:
    assert SCHEMA_VERSION == 25


async def test_initialize_cutover_snapshots_legacy_totals_once(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:one",
                session_id="session-1",
                epoch=3,
                agent_id="main",
                input_tokens=12,
                output_tokens=4,
                total_tokens=16,
                cache_read=2,
                total_cost_usd=0.0092,
                billed_cost_usd=0.001,
                estimated_cost_component_usd=0.0082,
                cost_source="mixed",
            )
        )
        state = await storage.initialize_usage_ledger(1_000)
        assert state.ledger_started_at_ms == 1_000
        baselines = await storage.list_usage_legacy_baselines()
        assert len(baselines) == 1
        assert baselines[0].session_epoch == 3
        assert baselines[0].cost_nanos == 9_200_000
        assert baselines[0].billed_cost_nanos == 1_000_000
        assert baselines[0].estimated_cost_nanos == 8_200_000

        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:two",
                session_id="session-2",
            )
        )
        repeated = await storage.initialize_usage_ledger(2_000)
        assert repeated.ledger_started_at_ms == 1_000
        baselines = await storage.list_usage_legacy_baselines()
        assert [(row.session_id, row.session_epoch) for row in baselines] == [
            ("session-1", 3),
            ("session-2", 0),
        ]
        assert baselines[1].captured_at_ms >= 1_000
    finally:
        await storage.close()


async def test_initialize_cutover_is_set_based_and_repairs_legacy_components(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    traced: list[str] = []
    try:
        await storage.conn.executemany(
            """
            INSERT INTO sessions (
                session_key, session_id, created_at, updated_at,
                input_tokens, output_tokens, total_tokens,
                total_cost_usd, billed_cost_usd,
                estimated_cost_component_usd, cost_source
            ) VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?, ?, 'mixed')
            """,
            [
                (
                    f"agent:main:webchat:{index}",
                    f"session-{index}",
                    2 if index == 0 else 0,
                    3 if index == 0 else 0,
                    999 if index == 0 else 0,
                    0.01 if index == 0 else 0.0,
                    0.004 if index == 0 else 0.0,
                    0.004 if index == 0 else 0.0,
                )
                for index in range(250)
            ],
        )
        await storage.conn.set_trace_callback(traced.append)

        state = await storage.initialize_usage_ledger(1_000)

        await storage.conn.set_trace_callback(None)
        baseline = (await storage.list_usage_legacy_baselines())[0]
        assert baseline.total_tokens == 5
        assert baseline.cost_nanos == 10_000_000
        assert baseline.billed_cost_nanos == 4_000_000
        assert baseline.estimated_cost_nanos == 6_000_000
        assert baseline.cost_nanos == (
            baseline.billed_cost_nanos + baseline.estimated_cost_nanos
        )
        assert baseline.missing_cost_entries >= 2
        assert state.anomaly_count >= 2
        baseline_inserts = [
            statement
            for statement in traced
            if "INSERT INTO usage_legacy_baselines" in statement
        ]
        assert len(baseline_inserts) == 1
    finally:
        await storage.close()


async def test_event_lifecycle_is_atomic_idempotent_and_half_open(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        started = await storage.start_usage_event(_start())
        assert started.status == "started"
        assert await storage.start_usage_event(_start()) == started

        completion = _completion()
        item = _item()
        finalized = await storage.finalize_usage_event(
            "event-1", completion, items=(item,)
        )
        assert finalized.status == "finalized"
        assert finalized.cost_nanos == 9_200_000
        assert await storage.finalize_usage_event(
            "event-1", completion, items=(item,)
        ) == finalized
        assert await storage.query_usage_event_items(["event-1", "event-1"]) == [item]

        assert await storage.query_usage_events(200, 201) == [finalized]
        assert await storage.query_usage_events(0, 200) == []
        assert await storage.query_usage_events(201, None) == []

        await storage.start_usage_event(
            _start(event_id="event-2", execution_id="execution-2")
        )
        with pytest.raises(ValueError, match="reconcile exactly"):
            await storage.finalize_usage_event(
                "event-2",
                completion,
                items=(replace(item, event_id="event-2", input_tokens=1),),
            )
        still_started = await storage.query_usage_events(
            None,
            None,
            statuses=("started",),
        )
        assert [event.event_id for event in still_started] == ["event-2"]

        with pytest.raises(UsageLedgerConflictError):
            await storage.start_usage_event(
                _start(event_id="event-other", execution_id="execution-1")
            )
        with pytest.raises(UsageLedgerConflictError):
            await storage.finalize_usage_event(
                "event-1", _completion(cost_nanos=1), items=()
            )
    finally:
        await storage.close()


async def test_native_billing_receipts_are_atomic_idempotent_and_cascaded(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        state = await storage.get_usage_billing_receipt_state()
        assert state is not None
        assert state.tracking_started_at_ms >= 0

        await storage.start_usage_event(_start())
        completion = replace(
            _completion(cost_nanos=1_337),
            billed_cost_nanos=1_337,
            estimated_cost_nanos=0,
            cost_source="provider_billed",
        )
        item = replace(
            _item(),
            cost_nanos=1_337,
            billed_cost_nanos=1_337,
            estimated_cost_nanos=0,
            cost_source="provider_billed",
        )
        receipt = UsageItemBillingReceipt(
            event_id="event-1",
            ordinal=0,
            currency="CNY",
            status="confirmed",
            amount_nanos=9_325,
            usd_equivalent_nanos=1_337,
            fx_native_per_usd_nanos=6_975_000_000,
        )

        finalized = await storage.finalize_usage_event(
            "event-1",
            completion,
            items=(item,),
            receipts=(receipt,),
        )
        assert await storage.finalize_usage_event(
            "event-1",
            completion,
            items=(item,),
            receipts=(receipt,),
        ) == finalized
        assert await storage.query_usage_item_billing_receipts(
            ["event-1", "event-1"]
        ) == [receipt]

        with pytest.raises(UsageLedgerConflictError, match="billing receipts"):
            await storage.finalize_usage_event(
                "event-1",
                completion,
                items=(item,),
                receipts=(replace(receipt, amount_nanos=9_326),),
            )

        await storage.conn.execute("DELETE FROM usage_events WHERE event_id = 'event-1'")
        assert await storage.query_usage_event_items(["event-1"]) == []
        assert await storage.query_usage_item_billing_receipts(["event-1"]) == []
    finally:
        await storage.close()


async def test_billing_receipt_settlement_contracts_preserve_half_open_event(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.start_usage_event(_start())
        completion = _completion()
        item = _item()
        pending = UsageItemBillingReceipt(
            event_id="event-1",
            ordinal=0,
            currency="CNY",
            status="pending",
            amount_nanos=None,
            usd_equivalent_nanos=None,
            fx_native_per_usd_nanos=6_975_000_000,
        )

        invalid_confirmed = replace(
            pending,
            status="confirmed",
            amount_nanos=64_000_000,
            usd_equivalent_nanos=1,
        )
        with pytest.raises(ValueError, match="must equal item billed cost"):
            await storage.finalize_usage_event(
                "event-1",
                completion,
                items=(item,),
                receipts=(invalid_confirmed,),
            )
        billed_completion = replace(
            completion,
            billed_cost_nanos=1,
            estimated_cost_nanos=completion.cost_nanos - 1,
            cost_source="mixed",
        )
        billed_item = replace(
            item,
            billed_cost_nanos=1,
            estimated_cost_nanos=item.cost_nanos - 1,
            cost_source="mixed",
        )
        with pytest.raises(ValueError, match="pending.*billed cost must be zero"):
            await storage.finalize_usage_event(
                "event-1",
                billed_completion,
                items=(billed_item,),
                receipts=(pending,),
            )
        assert [
            event.status
            for event in await storage.query_usage_events(None, None, statuses=("started",))
        ] == ["started"]

        await storage.finalize_usage_event(
            "event-1",
            completion,
            items=(item,),
            receipts=(pending,),
        )
        assert await storage.query_usage_item_billing_receipts(["event-1"]) == [pending]

        await storage.start_usage_event(
            _start(event_id="event-2", execution_id="execution-2")
        )
        zero_completion = replace(
            completion,
            cost_nanos=0,
            billed_cost_nanos=0,
            estimated_cost_nanos=0,
            cost_source="provider_billed",
        )
        zero_item = replace(
            item,
            event_id="event-2",
            cost_nanos=0,
            billed_cost_nanos=0,
            estimated_cost_nanos=0,
            cost_source="provider_billed",
        )
        zero_receipt = UsageItemBillingReceipt(
            event_id="event-2",
            ordinal=0,
            currency="USD",
            status="confirmed",
            amount_nanos=0,
            usd_equivalent_nanos=0,
            fx_native_per_usd_nanos=1_000_000_000,
        )
        await storage.finalize_usage_event(
            "event-2",
            zero_completion,
            items=(zero_item,),
            receipts=(zero_receipt,),
        )
        assert await storage.query_usage_item_billing_receipts(["event-2"]) == [
            zero_receipt
        ]
    finally:
        await storage.close()


async def test_completed_range_query_uses_time_ordered_index_without_temp_sort(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        async with storage.conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM usage_events INDEXED BY idx_usage_events_completed
            WHERE status IN ('finalized', 'unknown')
              AND completed_at_ms >= 0
              AND completed_at_ms < 1000
            ORDER BY completed_at_ms, event_id
            """
        ) as cursor:
            details = " ".join(str(row[3]) for row in await cursor.fetchall())

        assert "idx_usage_events_completed" in details
        assert "USE TEMP B-TREE" not in details

        traced: list[str] = []
        await storage.conn.set_trace_callback(traced.append)
        await storage.query_usage_events(0, 1000, statuses=("finalized", "unknown"))
        await storage.conn.set_trace_callback(None)
        select_sql = next(statement for statement in traced if "FROM usage_events" in statement)
        assert "INDEXED BY idx_usage_events_completed" in select_sql
    finally:
        await storage.close()


async def test_unknown_never_downgrades_finalized_and_survives_session_delete(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        session = SessionNode(
            session_key="agent:main:webchat:one",
            session_id="session-1",
        )
        await storage.upsert_session(session)
        await storage.start_usage_event(_start())
        unknown = await storage.mark_usage_event_unknown(
            "event-1", completed_at_ms=150, reason="provider_error"
        )
        assert unknown.status == "unknown"
        assert unknown.missing_cost_entries == 1

        finalized = await storage.finalize_usage_event("event-1", _completion(), items=(_item(),))
        raced = await storage.mark_usage_event_unknown(
            "event-1", completed_at_ms=250, reason="cancelled"
        )
        assert raced == finalized

        await storage.delete_session(session.session_key)
        assert await storage.query_usage_events(None, None) == [finalized]
    finally:
        await storage.close()


async def test_session_boundaries_clean_meta_state_but_preserve_billing_audit(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        session = SessionNode(
            session_key="agent:main:webchat:combined-storage",
            session_id="combined-storage-session",
        )
        await storage.upsert_session(session)
        await storage.start_usage_event(_start(session_id=session.session_id))
        item = _item()
        receipt = UsageItemBillingReceipt(
            event_id="event-1",
            ordinal=0,
            currency="CNY",
            status="pending",
            amount_nanos=None,
            usd_equivalent_nanos=None,
            fx_native_per_usd_nanos=6_975_000_000,
        )
        finalized = await storage.finalize_usage_event(
            "event-1",
            _completion(),
            items=(item,),
            receipts=(receipt,),
        )

        async def stage(client_request_id: str) -> None:
            launch_text = f"/meta meta-paper-write -- {client_request_id}"
            await storage.stage_meta_launch_draft(
                session_key=session.session_key,
                client_request_id=client_request_id,
                meta_skill_name="meta-paper-write",
                launch_text=launch_text,
            )
            await storage.promote_meta_launch_draft(
                session_key=session.session_key,
                client_request_id=client_request_id,
                meta_skill_name="meta-paper-write",
                launch_text=launch_text,
            )

        async def assert_billing_audit_survives() -> None:
            assert await storage.query_usage_events(None, None) == [finalized]
            assert await storage.query_usage_event_items(["event-1"]) == [item]
            assert await storage.query_usage_item_billing_receipts(["event-1"]) == [receipt]

        await stage("before-reset")
        assert await storage.advance_reset_epoch(session.session_key) == 1
        assert await storage.list_meta_launch_drafts(session_key=session.session_key) == []
        assert await storage.get_meta_control_intent(
            session_key=session.session_key,
            control_kind="manual",
            correlation_id="request:before-reset",
        ) is None
        await assert_billing_audit_survives()

        await stage("before-delete")
        await storage.delete_session(session.session_key)
        assert await storage.list_meta_launch_drafts(session_key=session.session_key) == []
        assert await storage.get_meta_control_intent(
            session_key=session.session_key,
            control_kind="manual",
            correlation_id="request:before-delete",
        ) is None
        async with storage.conn.execute(
            """
            SELECT client_request_id
            FROM meta_launch_discard_tombstones
            WHERE session_key = ?
            """,
            (session.session_key,),
        ) as cur:
            assert {str(row[0]) for row in await cur.fetchall()} == {
                "before-reset",
                "before-delete",
            }
        await assert_billing_audit_survives()
    finally:
        await storage.close()


async def test_storage_never_persists_arbitrary_unknown_reason(tmp_path: Path) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:one",
                session_id="session-1",
            )
        )
        await storage.start_usage_event(_start())

        record = await storage.mark_usage_event_unknown(
            "event-1",
            completed_at_ms=150,
            reason="provider_error:https://provider.invalid/?api_key=sk-secret\nnext",
        )

        assert record.unknown_reason == "provider_error"
    finally:
        await storage.close()


async def test_live_start_resolves_session_attribution_and_boot_recovers_started(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key="agent:worker:webchat:one",
                session_id="session-1",
                agent_id="worker",
                epoch=4,
            )
        )
        started = await storage.start_usage_event(_start(started_at_ms=500))
        assert started.agent_id == "worker"
        assert started.session_epoch == 4

        assert await storage.recover_started_usage_events(
            completed_at_ms=600,
            reason="process_restarted",
        ) == 1
        recovered = await storage.query_usage_events(
            0,
            None,
            statuses=("unknown",),
        )
        assert len(recovered) == 1
        assert recovered[0].unknown_reason == "process_restarted"
        assert recovered[0].completed_at_ms == 600
        assert await storage.recover_started_usage_events(completed_at_ms=700) == 0

        # The idempotent start replay keeps the original epoch after a reset.
        session = await storage.get_session("agent:worker:webchat:one")
        assert session is not None
        session.epoch = 5
        await storage.upsert_session(session)
        replay = await storage.start_usage_event(_start(started_at_ms=500))
        assert replay.session_epoch == 4
    finally:
        await storage.close()


async def test_cancelled_turn_projection_keeps_finalized_usage_and_unknown_coverage(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    try:
        await storage.upsert_session(
            SessionNode(
                session_key="agent:main:webchat:cancelled",
                session_id="session-1",
            )
        )
        await storage.initialize_usage_ledger(1)
        await storage.start_usage_event(_start())
        await storage.finalize_usage_event("event-1", _completion(), items=(_item(),))
        await storage.start_usage_event(
            _start(
                event_id="event-2",
                execution_id="execution-1",
                call_index=1,
                started_at_ms=300,
            )
        )
        await storage.mark_usage_event_unknown(
            "event-2",
            completed_at_ms=350,
            reason="cancelled",
        )

        projection = await storage.get_turn_usage_projection(
            session_id="session-1",
            session_epoch=0,
            turn_id="turn-1",
        )

        assert projection is not None
        assert projection["input_tokens"] == 100
        assert projection["output_tokens"] == 20
        assert projection["total_tokens"] == 120
        assert projection["coverage_status"] == "usage_unknown"
        assert projection["usage_unknown"] is True
        assert projection["unknown_usage_events"] == 1
        assert projection["missing_cost_entries"] == 1
        batch = await storage.get_turn_usage_projections(
            session_id="session-1",
            session_epoch=0,
            turn_ids=["turn-1"],
        )
        assert batch == {"turn-1": projection}
    finally:
        await storage.close()


async def test_session_usage_ledger_reconciliation_is_absolute_and_idempotent(
    tmp_path: Path,
) -> None:
    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    session_key = "agent:main:webchat:reconcile"
    try:
        await storage.initialize_usage_ledger(1)
        await storage.upsert_session(
            SessionNode(
                session_key=session_key,
                session_id="session-1",
                input_tokens=12,
                output_tokens=4,
                total_tokens=16,
                cache_read=2,
                total_cost_usd=0.001,
                estimated_cost_usd=0.001,
                estimated_cost_component_usd=0.001,
                cost_source="opensquilla_estimate",
            )
        )
        baselines = await storage.list_usage_legacy_baselines()
        assert [(row.session_id, row.session_epoch) for row in baselines] == [
            ("session-1", 0)
        ]
        await storage.start_usage_event(_start())
        await storage.finalize_usage_event("event-1", _completion(), items=(_item(),))

        first = await storage.reconcile_session_usage_totals_from_ledger(
            session_key=session_key,
            expected_epoch=0,
        )
        second = await storage.reconcile_session_usage_totals_from_ledger(
            session_key=session_key,
            expected_epoch=0,
        )

        assert first is not None
        assert second is not None
        assert (second.input_tokens, second.output_tokens, second.total_tokens) == (112, 24, 136)
        assert second.cache_read == 2
        assert second.total_cost_usd == pytest.approx(0.0102)
        assert second.estimated_cost_component_usd == pytest.approx(0.0102)
        assert second.model_override == "gpt-test"
        assert second.model_provider == "openai"
        assert first.input_tokens == second.input_tokens
        assert first.total_cost_usd == second.total_cost_usd

        assert await storage.advance_reset_epoch(session_key) == 1
        baselines = await storage.list_usage_legacy_baselines()
        assert {(row.session_id, row.session_epoch) for row in baselines} == {
            ("session-1", 0),
            ("session-1", 1),
        }
    finally:
        await storage.close()


async def test_existing_cutover_repairs_post_cutover_epoch_without_double_counting(
    tmp_path: Path,
) -> None:
    """Upgrade repair uses ledger ancestry, not already-rolled session totals."""

    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    session_key = "agent:main:webchat:pre-fix-mixed-turns"
    session_id = "session-pre-fix-mixed-turns"
    try:
        await storage.initialize_usage_ledger(100)
        # Simulate an old atomic generation path after cutover. Reset preserves
        # created_at, so the cutover transaction's complete baseline snapshot --
        # not this timestamp -- is the proof that the generation is post-cutover.
        await storage.conn.execute(
            """
            INSERT INTO sessions (session_key, session_id, created_at, updated_at)
            VALUES (?, ?, 50, 200)
            """,
            (session_key, session_id),
        )
        await storage.conn.commit()

        epoch_zero = replace(
            _start(
                "event-epoch-zero-done",
                execution_id="execution-epoch-zero-done",
                session_id=session_id,
                started_at_ms=300,
            ),
            turn_id="turn-epoch-zero-done",
        )
        assert (await storage.start_usage_event(epoch_zero)).session_epoch == 0
        await storage.finalize_usage_event(
            epoch_zero.event_id,
            _completion(completed_at_ms=400),
            items=(_item(epoch_zero.event_id),),
        )
        # The pre-upgrade Done path already copied this event into the mutable
        # compatibility totals.
        await storage.conn.execute(
            """
            UPDATE sessions
            SET input_tokens = 100, output_tokens = 20, total_tokens = 120,
                total_tokens_fresh = 1,
                estimated_cost_usd = 0.0092, total_cost_usd = 0.0092,
                estimated_cost_component_usd = 0.0092,
                cost_source = 'opensquilla_estimate', epoch = 1
            WHERE session_key = ?
            """,
            (session_key,),
        )
        await storage.conn.commit()

        epoch_one_done = replace(
            _start(
                "event-epoch-one-done",
                execution_id="execution-epoch-one-done",
                session_id=session_id,
                started_at_ms=500,
            ),
            turn_id="turn-epoch-one-done",
        )
        assert (await storage.start_usage_event(epoch_one_done)).session_epoch == 1
        await storage.finalize_usage_event(
            epoch_one_done.event_id,
            _completion(completed_at_ms=600),
            items=(_item(epoch_one_done.event_id),),
        )
        await storage.conn.execute(
            """
            UPDATE sessions
            SET input_tokens = 200, output_tokens = 40, total_tokens = 240,
                estimated_cost_usd = 0.0184, total_cost_usd = 0.0184,
                estimated_cost_component_usd = 0.0184
            WHERE session_key = ?
            """,
            (session_key,),
        )
        await storage.conn.commit()

        # A cancelled turn can still have a complete provider receipt. The old
        # path finalized this ledger event but never emitted Done, so it is not
        # present in the compatibility totals above.
        epoch_one_cancelled = replace(
            _start(
                "event-epoch-one-cancelled",
                execution_id="execution-epoch-one-cancelled",
                session_id=session_id,
                started_at_ms=700,
            ),
            turn_id="turn-epoch-one-cancelled",
        )
        assert (await storage.start_usage_event(epoch_one_cancelled)).session_epoch == 1
        await storage.finalize_usage_event(
            epoch_one_cancelled.event_id,
            _completion(completed_at_ms=800),
            items=(_item(epoch_one_cancelled.event_id),),
        )

        assert await storage.list_usage_legacy_baselines() == []
        await storage.close()
        storage = await SessionStorage.open(str(db_path))

        state = await storage.initialize_usage_ledger(1_000)
        assert state.ledger_started_at_ms == 100
        baseline = (await storage.list_usage_legacy_baselines())[0]
        assert (baseline.session_id, baseline.session_epoch) == (session_id, 1)
        assert baseline.captured_at_ms > state.ledger_started_at_ms
        # Epoch zero is carried exactly once from the ledger. Snapshotting the
        # current 200/40 totals here would double-count both normal Done turns.
        assert (baseline.input_tokens, baseline.output_tokens, baseline.total_tokens) == (
            100,
            20,
            120,
        )
        assert baseline.cost_nanos == 9_200_000

        first = await storage.reconcile_session_usage_totals_from_ledger(
            session_key=session_key,
            expected_epoch=1,
        )
        second = await storage.reconcile_session_usage_totals_from_ledger(
            session_key=session_key,
            expected_epoch=1,
        )
        assert first is not None
        assert second is not None
        assert (second.input_tokens, second.output_tokens, second.total_tokens) == (
            300,
            60,
            360,
        )
        assert second.total_cost_usd == pytest.approx(0.0276)
        assert first.input_tokens == second.input_tokens
        assert first.total_cost_usd == second.total_cost_usd

        baseline_before_retry = (await storage.list_usage_legacy_baselines())[0]
        repeated = await storage.initialize_usage_ledger(2_000)
        baseline_after_retry = (await storage.list_usage_legacy_baselines())[0]
        assert repeated.ledger_started_at_ms == 100
        assert baseline_after_retry == baseline_before_retry == baseline
    finally:
        await storage.close()
