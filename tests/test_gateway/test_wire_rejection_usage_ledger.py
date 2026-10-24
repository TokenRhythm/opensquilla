from __future__ import annotations

import time

import pytest

from opensquilla.engine.usage_accounting import UsageCallStart
from opensquilla.gateway.usage_ledger_runtime import SessionUsageEventSink
from opensquilla.gateway.usage_query import query_usage_ledger
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.session.usage_ledger import (
    LOCAL_PRE_DISPATCH_ORIGIN,
    LOCAL_WIRE_BODY_REJECTION_REASON,
    UsageEventCompletion,
    UsageLedgerConflictError,
)


def _call(index: int, started_at_ms: int) -> UsageCallStart:
    return UsageCallStart(
        event_id=f"synthetic-event-{index}",
        execution_id="synthetic-execution",
        call_index=index,
        agent_run_id="synthetic-run",
        turn_id=f"synthetic-turn-{index}",
        parent_turn_id=None,
        session_id="synthetic-session",
        agent_id="main",
        run_kind="agent",
        provider="synthetic-provider",
        model="synthetic-model",
        started_at_ms=started_at_ms,
    )


async def test_local_wire_rejection_is_durable_idempotent_and_excluded_from_usage() -> None:
    storage = SessionStorage(":memory:")
    await storage.connect()
    sink = SessionUsageEventSink(storage)
    try:
        await storage.upsert_session(SessionNode(
            session_key="agent:main:webchat:synthetic",
            session_id="synthetic-session",
        ))
        started_at = int(time.time() * 1_000) - 100
        await storage.initialize_usage_ledger(now_ms=started_at - 10)
        local = _call(1, started_at)
        await sink.start(local)
        await sink.mark_not_sent(local)
        await sink.mark_not_sent(local)

        rows = await storage.query_usage_events(
            None, None, statuses=("unknown",), session_id="synthetic-session",
        )
        assert len(rows) == 1
        assert rows[0].origin == LOCAL_PRE_DISPATCH_ORIGIN
        assert rows[0].unknown_reason == LOCAL_WIRE_BODY_REJECTION_REASON
        assert rows[0].missing_cost_entries == 0
        assert rows[0].transition_applied is False
        assert await storage.get_turn_usage_projection(
            session_id="synthetic-session",
            session_epoch=0,
            turn_id=local.turn_id or "",
        ) is None
        assert await storage.get_turn_usage_projections(
            session_id="synthetic-session",
            session_epoch=0,
            turn_ids=[local.turn_id or ""],
        ) == {}
        session_totals = await storage.reconcile_session_usage_totals_from_ledger(
            session_key="agent:main:webchat:synthetic",
            expected_epoch=0,
            expected_session_id="synthetic-session",
        )
        assert session_totals is not None
        assert session_totals.missing_cost_entries == 0

        local_only = await query_usage_ledger(
            storage,
            {"range": {"fromMs": started_at - 20, "toMs": started_at + 2_000}},
            now_ms=started_at + 3_000,
        )
        assert local_only["eventCount"] == 0
        assert local_only["missingCostEntries"] == 0

        upstream = _call(2, started_at + 1)
        await sink.start(upstream)
        await sink.mark_unknown(upstream, "provider_error:413")
        await sink.mark_unknown(upstream, "provider_error:413")
        upstream_projection = await storage.get_turn_usage_projection(
            session_id="synthetic-session",
            session_epoch=0,
            turn_id=upstream.turn_id or "",
        )
        assert upstream_projection is not None
        assert upstream_projection["unknown_usage_events"] == 1
        assert upstream_projection["missing_cost_entries"] == 1

        report = await query_usage_ledger(
            storage,
            {"range": {"fromMs": started_at - 20, "toMs": started_at + 2_000}},
            now_ms=started_at + 3_000,
        )
        assert report["eventCount"] == 1
        assert report["missingCostEntries"] == 1
        assert report["totals"]["costNanos"] == 0
        assert report["totals"]["eventCount"] == 1

        with pytest.raises(UsageLedgerConflictError):
            await storage.finalize_usage_event(
                local.event_id,
                # A late receipt must not silently turn an unsubmitted call
                # into a billed provider request.
                UsageEventCompletion(completed_at_ms=started_at + 1),
            )
    finally:
        await sink.close()
        await storage.close()
