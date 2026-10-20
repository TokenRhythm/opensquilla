"""Task reports partition actual calls, including independent side-chat owners."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from opensquilla.gateway.usage_query import query_usage_ledger
from opensquilla.gateway.usage_settlement_projection import apply_current_settlements
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage
from opensquilla.session.usage_attribution import backfill_usage_attribution_on_conn
from opensquilla.session.usage_ledger import UsageEventCompletion, UsageEventItem, UsageEventStart
from opensquilla.usage_metering import UsageItemMetering, UsageMeter, UsageSettlementRevision


async def _call(
    storage, owner, run, cents, *, parent=None, kind="agent", model="fixture", completed_at=200
):
    start = UsageEventStart(
        event_id=f"event-{run}",
        execution_id=run,
        call_index=0,
        session_id=owner.session_id,
        session_epoch=owner.epoch,
        turn_id=run,
        parent_turn_id=parent,
        root_turn_id=parent or run,
        started_at_ms=100,
        provider="test",
        model=model,
        run_kind=kind,
    )
    await storage.start_usage_event(start)
    await storage.finalize_usage_event(
        start.event_id,
        UsageEventCompletion(
            completed_at_ms=completed_at,
            input_tokens=10,
            output_tokens=2,
            total_tokens=12,
            cost_nanos=cents * 10_000_000,
            billed_cost_nanos=cents * 10_000_000,
            cost_source="provider_billed",
            provider="test",
            model=model,
        ),
    )
    return start


async def _report(storage):
    return await query_usage_ledger(
        storage,
        {"timezone": "UTC", "range": {"fromMs": 1, "toMs": 1000}},
        now_ms=1000,
    )


def _child(parent, name, *, side=False):
    origin = {"sidechat": {"parentSessionId": parent.session_id, "parentEpoch": parent.epoch}}
    if not side:
        origin = {
            "kind": "subagent",
            "parent_session_id": parent.session_id,
            "parent_session_epoch": parent.epoch,
        }
    return SessionNode(
        session_key=f"agent:main:{'sidechat' if side else 'subagent'}:{name}",
        session_id=name,
        parent_session_key=parent.session_key,
        origin=origin,
        display_name=name,
    )


async def test_parent_inline_children_and_side_branch_sum_exactly_070(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        parent = SessionNode(
            session_key="agent:main:webchat:root", session_id="root", display_name="Root task"
        )
        child = _child(parent, "child")
        side = _child(parent, "side", side=True)
        side_child = _child(side, "side-child")
        for node in (parent, child, side, side_child):
            await storage.upsert_session(node)
        await _call(storage, parent, "parent-run", 10)
        await _call(storage, parent, "inline-run", 10, parent="parent-run", kind="subagent")
        await _call(storage, child, "child-run", 20, parent="parent-run", kind="subagent")
        await _call(storage, side, "side-run", 10)
        start = await _call(
            storage, side_child, "side-child-run", 20, parent="side-run", kind="subagent"
        )
        # Replay the reservation; it must not add a sixth charge.
        await storage.start_usage_event(start)
        report = await _report(storage)
        assert report["totals"]["costUsd"] == pytest.approx(0.70)
        assert report["eventCount"] == 5
        assert report["totals"]["taskCount"] == 1
        assert report["sessionCount"] == 4
        assert len(report["taskGroups"]) == 1
        group = report["taskGroups"][0]
        assert group["title"] == "Root task"
        assert group["totals"]["costUsd"] == pytest.approx(0.70)
        [model] = group["modelBreakdown"]
        assert model["totals"]["costNanos"] == 700_000_000
        assert model["eventCount"] == 5
        assert report["models"][0]["taskCount"] == 1
        assert group["firstUsageAtMs"] == group["lastUsageAtMs"] == 200
        assert {key: value["costNanos"] for key, value in group["branches"].items()} == {
            "self": 100_000_000,
            "inlineSubagents": 100_000_000,
            "subsessions": 200_000_000,
            "sideChats": 300_000_000,
            "unattributed": 0,
        }
        assert sum(row["totals"]["costNanos"] for row in group["runs"]) == 700_000_000
        assert report["coverage"]["attribution"]["status"] == "complete"
        # Child deletion preserves both its charge and immutable task identity.
        await storage.delete_session(child.session_key)
        deleted = await _report(storage)
        assert deleted["taskGroups"][0]["totals"] == group["totals"]
        assert (
            next(row for row in deleted["sessions"] if row["sessionId"] == "child")["sessionKey"]
            is None
        )
    finally:
        await storage.close()


async def test_parent_with_only_child_calls_has_one_complete_row_and_models(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        parent = SessionNode(
            session_key="agent:main:webchat:no-own-call",
            session_id="no-own-call",
            display_name="Parent without a provider call",
        )
        child = _child(parent, "child-only")
        side = _child(parent, "side-only", side=True)
        for node in (parent, child, side):
            await storage.upsert_session(node)
        await _call(storage, child, "child", 20, kind="subagent", model="model-a")
        await _call(storage, side, "side", 30, model="model-b", completed_at=300)
        report = await _report(storage)
        [group] = report["taskGroups"]
        assert report["totals"]["taskCount"] == 1
        assert report["sessionCount"] == 2
        assert group["rootSessionId"] == parent.session_id
        assert group["sessionKey"] == parent.session_key
        assert group["title"] == parent.display_name
        assert group["firstUsageAtMs"] == 200
        assert group["lastUsageAtMs"] == 300
        assert group["branches"]["self"]["eventCount"] == 0
        assert group["totals"]["costNanos"] == 500_000_000
        assert {row["model"]: row["totals"]["costNanos"] for row in group["modelBreakdown"]} == {
            "model-a": 200_000_000,
            "model-b": 300_000_000,
        }
        assert all(row["taskCount"] == 1 for row in report["models"])
    finally:
        await storage.close()


async def test_task_rows_and_model_counts_obey_range_without_merging_same_agent(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        parent = SessionNode(session_key="agent:main:webchat:window", session_id="window")
        child = _child(parent, "window-child")
        unrelated = SessionNode(session_key="agent:main:webchat:other", session_id="other")
        for node in (parent, child, unrelated):
            await storage.upsert_session(node)
        await _call(storage, parent, "before", 10, completed_at=100, model="outside")
        await _call(storage, child, "inside-child", 20, kind="subagent")
        await _call(storage, parent, "after", 40, completed_at=300, model="outside")
        await _call(storage, unrelated, "inside-other", 30)
        report = await query_usage_ledger(
            storage,
            {
                "timezone": "UTC",
                "range": {"fromMs": 150, "toMs": 250},
                "include": {"sessions": False},
            },
            now_ms=1000,
        )
        assert report["eventCount"] == 2
        assert report["totals"]["taskCount"] == 2
        assert report["totals"]["costNanos"] == 500_000_000
        assert report["sessions"] == []
        groups = {row["rootSessionId"]: row for row in report["taskGroups"]}
        assert set(groups) == {parent.session_id, unrelated.session_id}
        assert groups[parent.session_id]["branches"]["self"]["eventCount"] == 0
        assert groups[parent.session_id]["totals"]["costNanos"] == 200_000_000
        assert all(row["firstUsageAtMs"] == row["lastUsageAtMs"] == 200 for row in groups.values())
        assert sum(row["totals"]["costNanos"] for row in groups.values()) == 500_000_000
        [model] = report["models"]
        assert model["model"] == "fixture"
        assert model["taskCount"] == 2
        assert model["totals"]["costNanos"] == 500_000_000
        assert (
            sum(
                detail["totals"]["costNanos"]
                for group in groups.values()
                for detail in group["modelBreakdown"]
            )
            == model["totals"]["costNanos"]
        )
    finally:
        await storage.close()


async def test_root_title_refreshes_only_for_matching_live_generation(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        parent = SessionNode(
            session_key="agent:main:webchat:renamed",
            session_id="renamed",
            display_name="Original title",
        )
        child = _child(parent, "renamed-child")
        await storage.upsert_session(parent)
        await storage.upsert_session(child)
        await _call(storage, child, "old-child", 20, kind="subagent")
        parent.display_name = "Renamed current title"
        await storage.upsert_session(parent)
        [group] = (await _report(storage))["taskGroups"]
        assert group["title"] == "Renamed current title"
        assert group["sessionKey"] == parent.session_key
        parent.display_name = ""
        parent.derived_title = "Generated current title"
        await storage.upsert_session(parent)
        assert (await _report(storage))["taskGroups"][0]["title"] == "Generated current title"

        fresh = parent.model_copy(update={"epoch": 1, "display_name": "New generation"})
        await storage.upsert_session(fresh)
        await _call(storage, fresh, "new-generation", 30)
        groups = {row["rootSessionEpoch"]: row for row in (await _report(storage))["taskGroups"]}
        assert groups[0]["title"] == "Original title"
        assert groups[0]["sessionKey"] is None
        assert groups[0]["totals"]["costNanos"] == 200_000_000
        assert groups[1]["title"] == "New generation"
        assert groups[1]["sessionKey"] == parent.session_key
        assert groups[1]["totals"]["costNanos"] == 300_000_000
    finally:
        await storage.close()


async def test_existing_placeholder_owner_uses_live_generated_title(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage-title.db"))
    try:
        parent = SessionNode(
            session_key="agent:main:webchat:placeholder",
            session_id="placeholder", display_name="WebChat",
        )
        await storage.upsert_session(parent)
        await _call(storage, parent, "before-title", 20)
        parent.derived_title = "制作我的世界复刻游戏"
        await storage.upsert_session(parent)
        [group] = (await _report(storage))["taskGroups"]
        assert group["title"] == parent.derived_title
        assert group["totals"]["costNanos"] == 200_000_000
        assert group["rootSessionEpoch"] == 0
    finally:
        await storage.close()


async def test_usage_owner_captures_transcript_title_before_deletion(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage-transcript-title.db"))
    try:
        parent = SessionNode(
            session_key="agent:main:webchat:unnamed",
            session_id="unnamed", display_name="WebChat",
        )
        await storage.upsert_session(parent)
        await storage.append_transcript_entry(TranscriptEntry(
            session_id=parent.session_id, session_key=parent.session_key,
            role="user", content="Review the release plan",
        ))
        await _call(storage, parent, "first-call", 20)
        assert (await _report(storage))["taskGroups"][0]["title"] == "Review the release plan"
        await storage.delete_session(parent.session_key)
        [group] = (await _report(storage))["taskGroups"]
        assert group["title"] == "Review the release plan"
        assert group["sessionKey"] is None
        assert group["totals"]["costNanos"] == 200_000_000
    finally:
        await storage.close()


@pytest.mark.parametrize("snapshot", ["WebChat", ""])
@pytest.mark.parametrize("retire", ["delete", "reset"])
async def test_legacy_title_snapshot_remains_fenced_after_live_owner_retires(
    tmp_path, snapshot, retire,
):
    storage = await SessionStorage.open(str(tmp_path / "usage-legacy-title.db"))
    try:
        parent = SessionNode(
            session_key="agent:main:webchat:legacy-title",
            session_id="legacy-title", display_name="WebChat",
        )
        await storage.upsert_session(parent)
        await _call(storage, parent, "legacy-call", 20)
        # A pre-fix owner may have captured a generic name or no title at all.
        await storage.conn.execute(
            "UPDATE usage_attribution_owners SET title=? WHERE session_id=?",
            (snapshot, parent.session_id),
        )
        await storage.conn.commit()
        parent.derived_title = "Recovered current title"
        await storage.upsert_session(parent)
        [live] = (await _report(storage))["taskGroups"]
        assert live["title"] == parent.derived_title
        if retire == "delete":
            await storage.delete_session(parent.session_key)
        else:
            await storage.upsert_session(parent.model_copy(update={
                "epoch": 1, "derived_title": "Different task",
            }))
        [old] = (await _report(storage))["taskGroups"]
        assert old["title"] == snapshot
        assert old["sessionKey"] is None
        assert old["totals"] == live["totals"]
    finally:
        await storage.close()


async def test_reset_and_reused_parent_key_cannot_move_old_or_late_child_cost(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        old = SessionNode(session_key="agent:main:webchat:root", session_id="old")
        child = _child(old, "child")
        await storage.upsert_session(old)
        await storage.upsert_session(child)
        reservation = await _call(storage, old, "old-run", 10)
        fresh = SessionNode(session_key=old.session_key, session_id="fresh", epoch=1)
        await storage.upsert_session(fresh)
        await _call(storage, fresh, "fresh-run", 30)
        await _call(storage, child, "late-child-run", 20, parent="old-run", kind="subagent")
        await storage.start_usage_event(reservation)
        groups = {group["rootSessionId"]: group for group in (await _report(storage))["taskGroups"]}
        assert groups["old"]["totals"]["costUsd"] == pytest.approx(0.30)
        assert groups["fresh"]["totals"]["costUsd"] == pytest.approx(0.30)
        assert groups["old"]["sessionKey"] is None
        assert groups["fresh"]["sessionKey"] == old.session_key
    finally:
        await storage.close()


async def test_session_rows_keep_reset_epochs_and_their_own_attribution(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        for epoch, cents in ((0, 10), (1, 30)):
            owner = SessionNode(
                session_key="agent:main:webchat:reused",
                session_id="reused",
                epoch=epoch,
                display_name=f"Generation {epoch}",
            )
            await storage.upsert_session(owner)
            await _call(storage, owner, f"run-{epoch}", cents)

        report = await _report(storage)
        rows = {row["sessionEpoch"]: row for row in report["sessions"]}
        assert report["sessionCount"] == report["attributedTotals"]["sessionCount"] == 2
        assert report["eventCount"] == 2
        assert report["totals"]["taskCount"] == 2
        assert report["models"][0]["taskCount"] == 2
        assert report["totals"]["costNanos"] == 400_000_000
        assert set(rows) == {0, 1}
        assert rows[0]["title"] == "Generation 0"
        assert rows[1]["title"] == "Generation 1"
        assert rows[0]["taskGroupId"] != rows[1]["taskGroupId"]
        assert rows[0]["sessionKey"] is None
        assert rows[1]["sessionKey"] == owner.session_key
        groups = {row["rootSessionEpoch"]: row for row in report["taskGroups"]}
        assert groups[0]["sessionKey"] is None
        assert groups[1]["sessionKey"] == owner.session_key
        assert [rows[epoch]["totals"]["costNanos"] for epoch in (0, 1)] == [
            100_000_000,
            300_000_000,
        ]
        assert [rows[epoch]["modelBreakdown"][0]["totals"]["costNanos"] for epoch in (0, 1)] == [
            100_000_000,
            300_000_000,
        ]
    finally:
        await storage.close()


async def test_next_call_in_existing_execution_keeps_epoch_after_reset(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        owner = SessionNode(session_key="agent:main:webchat:late", session_id="late")
        await storage.upsert_session(owner)
        first = await _call(storage, owner, "old-run", 10)
        current = owner.model_copy(update={"epoch": 1})
        await storage.upsert_session(current)
        late = replace(first, event_id="late-call", call_index=1)
        started = await storage.start_usage_event(late)
        assert started.session_epoch == 0
        await storage.finalize_usage_event(
            late.event_id,
            UsageEventCompletion(
                completed_at_ms=200,
                cost_nanos=200_000_000,
                billed_cost_nanos=200_000_000,
                cost_source="provider_billed",
            ),
        )
        await storage.start_usage_event(late)
        await _call(storage, current, "new-run", 40)
        report = await _report(storage)
        assert report["eventCount"] == 3
        groups = {row["rootSessionEpoch"]: row for row in report["taskGroups"]}
        assert groups[0]["totals"]["costNanos"] == 300_000_000
        assert groups[1]["totals"]["costNanos"] == 400_000_000
    finally:
        await storage.close()


@pytest.mark.parametrize("inline", [False, True])
async def test_ordinary_durable_task_keeps_captured_epoch_without_goal(tmp_path, inline):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        owner = SessionNode(session_key="agent:main:webchat:durable", session_id="durable")
        await storage.upsert_session(owner)
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="parent",
                session_key=owner.session_key,
                details={"session_id": owner.session_id, "session_epoch": 0, "metadata": {}},
            )
        )
        await _call(storage, owner, "parent", 10)
        metadata = (
            {
                "parent_task_id": "parent",
                "parent_session_key": owner.session_key,
                "parent_session_id": owner.session_id,
                "parent_session_epoch": 0,
            }
            if inline
            else {}
        )
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="admitted-before-reset",
                session_key=owner.session_key,
                details={
                    "session_id": owner.session_id,
                    "session_epoch": 0,
                    "metadata": metadata,
                },
            )
        )
        await storage.upsert_session(owner.model_copy(update={"epoch": 1}))
        event = await _call(
            storage,
            owner,
            "admitted-before-reset",
            20,
            parent="parent" if inline else None,
            kind="subagent" if inline else "agent",
        )
        replay = await storage.start_usage_event(event)
        assert replay.session_epoch == 0
        assert replay.goal_id is None
        report = await _report(storage)
        assert report["eventCount"] == 2
        [group] = report["taskGroups"]
        assert group["rootSessionEpoch"] == 0
        assert group["totals"]["costNanos"] == 300_000_000
        assert group["branches"]["inlineSubagents"]["costNanos"] == (200_000_000 if inline else 0)
    finally:
        await storage.close()


async def test_session_service_details_keep_provider_and_item_measurements_separate(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        owner = SessionNode(session_key="agent:main:webchat:search", session_id="search")
        await storage.upsert_session(owner)
        for provider, cents, credits in (("brave", 1, "1"), ("tavily", 2, "2")):
            event_id = f"event-{provider}"
            await storage.start_usage_event(
                UsageEventStart(
                    event_id=event_id,
                    execution_id=f"run-{provider}",
                    call_index=0,
                    session_id=owner.session_id,
                    started_at_ms=100,
                    provider=provider,
                    model="web_search",
                )
            )
            await storage.finalize_usage_event(
                event_id,
                UsageEventCompletion(
                    completed_at_ms=200,
                    cost_nanos=cents * 10_000_000,
                    estimated_cost_nanos=cents * 10_000_000,
                    cost_source="opensquilla_estimate",
                    provider=provider,
                    model="web_search",
                ),
                items=(
                    UsageEventItem(
                        event_id=event_id,
                        ordinal=0,
                        provider=provider,
                        model="web_search",
                        cost_nanos=cents * 10_000_000,
                        estimated_cost_nanos=cents * 10_000_000,
                        cost_source="opensquilla_estimate",
                    ),
                ),
                metering={
                    0: UsageItemMetering(
                        meters=(
                            UsageMeter(
                                "search_credits",
                                "credit",
                                credits,
                                dimensions={"operation": "web_search"},
                            ),
                        ),
                        token_usage_known=False,
                    )
                },
            )
        report = await _report(storage)
        [session] = report["sessions"]
        details = {row["provider"]: row for row in session["modelBreakdown"]}
        assert set(details) == {"brave", "tavily"}
        assert sum(row["totals"]["costNanos"] for row in details.values()) == 30_000_000
        assert session["totals"]["costNanos"] == report["totals"]["costNanos"] == 30_000_000
        for provider, quantity in (("brave", "1"), ("tavily", "2")):
            row = details[provider]
            assert row["model"] == "web_search"
            assert row["capability"] == "web_search"
            assert row["tokenUnknownItemCount"] == 1
            assert row["eventCount"] == 1
            [meter] = row["meters"]
            assert meter["quantity"] == quantity
            assert meter["measurementCount"] == 1
        assert all("meters" not in row and "capability" not in row for row in report["models"])
        assert report["taskGroups"][0]["modelBreakdown"] == session["modelBreakdown"]
        assert all(row["taskCount"] == 1 for row in report["models"])
    finally:
        await storage.close()


async def test_sidechat_snapshot_registers_owner_without_copying_bills(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        parent = await manager.create("agent:main:webchat:side-parent")
        await manager.append_message(parent.session_key, "user", "Synthetic parent")
        await manager.append_message(
            parent.session_key, "assistant", "Answer", turn_usage={"cost_usd": 0.1}
        )
        await _call(storage, parent, "main", 10)
        side = await manager.create_sidechat(parent.session_key, "synthetic-panel")
        assert all(
            entry.turn_usage is None for entry in await manager.get_transcript(side.session_key)
        )
        await _call(storage, side, "side", 20)
        report = await _report(storage)
        assert len(report["taskGroups"]) == 1
        assert report["taskGroups"][0]["branches"]["sideChats"]["costUsd"] == pytest.approx(0.2)
        assert report["totals"]["costUsd"] == pytest.approx(0.3)
    finally:
        await storage.close()


async def test_missing_child_generation_is_partial_and_never_follows_parent_key(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        parent = SessionNode(session_key="agent:main:webchat:root", session_id="current")
        orphan = SessionNode(
            session_key="agent:main:subagent:old",
            session_id="orphan",
            parent_session_key=parent.session_key,
            origin={"kind": "subagent"},
        )
        for node in (parent, orphan):
            await storage.upsert_session(node)
        await _call(storage, parent, "main", 10)
        await _call(storage, orphan, "orphan", 20, kind="subagent")
        report = await _report(storage)
        assert len(report["taskGroups"]) == 2
        assert report["totals"]["costUsd"] == pytest.approx(0.3)
        assert report["coverage"]["attribution"]["missingEventCount"] == 1
        assert report["totals"]["taskCount"] == 2
        assert (
            sum(
                detail["totals"]["costNanos"]
                for group in report["taskGroups"]
                for detail in group["modelBreakdown"]
            )
            == report["totals"]["costNanos"]
        )
        assert (
            next(g for g in report["taskGroups"] if g["rootSessionId"] == "orphan")[
                "attributionStatus"
            ]
            == "partial"
        )
    finally:
        await storage.close()


async def test_metadata_backfill_preserves_existing_cost_and_rebuilds_verified_root(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        owner = SessionNode(session_key="agent:main:webchat:legacy", session_id="legacy")
        await storage.upsert_session(owner)
        await _call(storage, owner, "legacy-run", 10)
        async with storage._write_transaction("test_remove_attribution") as conn:
            await conn.execute("DELETE FROM usage_attribution_runs")
            await conn.execute("DELETE FROM usage_attribution_owners")
            await backfill_usage_attribution_on_conn(conn)
            await backfill_usage_attribution_on_conn(conn)
        report = await _report(storage)
        assert report["eventCount"] == 1
        assert report["taskGroups"][0]["attributionStatus"] == "complete"
        assert report["totals"]["costUsd"] == pytest.approx(0.1)
    finally:
        await storage.close()


async def test_late_settlement_replaces_every_projection_without_another_call(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        owner = SessionNode(session_key="agent:main:webchat:priced", session_id="priced")
        await storage.upsert_session(owner)
        start = UsageEventStart(
            event_id="priced-event",
            execution_id="priced-run",
            call_index=0,
            session_id=owner.session_id,
            started_at_ms=100,
            provider="openrouter",
            model="image-fixture",
        )
        await storage.start_usage_event(start)
        completion = UsageEventCompletion(
            completed_at_ms=200,
            cost_nanos=100_000_000,
            estimated_cost_nanos=100_000_000,
            cost_source="opensquilla_estimate",
            provider="openrouter",
            model="image-fixture",
        )
        item = UsageEventItem(
            event_id=start.event_id,
            ordinal=0,
            provider="openrouter",
            model="image-fixture",
            cost_nanos=100_000_000,
            estimated_cost_nanos=100_000_000,
            cost_source="opensquilla_estimate",
        )
        initial = UsageSettlementRevision(
            revision_id="estimate",
            authority="local_estimate",
            status="final",
            native_amount="0.10",
            usd_amount="0.10",
        )
        await storage.finalize_usage_event(
            start.event_id,
            completion,
            items=(item,),
            metering={
                0: UsageItemMetering(
                    meters=(
                        UsageMeter(
                            "output_images",
                            "image",
                            "1",
                            dimensions={"operation": "image_generation"},
                        ),
                    ),
                    settlement=initial,
                    token_usage_known=False,
                )
            },
        )
        estimated = await _report(storage)
        assert estimated["totals"]["costUsd"] == pytest.approx(0.10)
        await storage.delete_session(owner.session_key)
        settled = UsageSettlementRevision(
            revision_id="receipt",
            previous_revision_id="estimate",
            authority="provider_confirmed",
            status="final",
            native_amount="0.05",
            usd_amount="0.05",
        )
        await storage.append_usage_settlement(start.event_id, 0, settled)
        await storage.append_usage_settlement(start.event_id, 0, settled)
        report = await _report(storage)
        assert report["eventCount"] == 1
        assert report["totals"]["costNanos"] == 50_000_000
        assert report["totals"]["estimatedCostNanos"] == 0
        assert report["totals"]["billedCostNanos"] == 50_000_000
        for dimension in ("days", "models", "sessions", "taskGroups", "capabilities"):
            assert sum(row["totals"]["costNanos"] for row in report[dimension]) == 50_000_000
        assert report["capabilities"][0]["capability"] == "image_generation"
        assert report["meters"][0]["quantity"] == "1"
        assert report["coverage"]["measurements"]["tokenUnknownItemCount"] == 1
        assert report["settlementSummary"]["provider_confirmed"] == 1
        assert report["totals"]["nativeBilledByCurrency"]["USD"]["amount"] == "0.05"
        [detail] = report["sessions"][0]["modelBreakdown"]
        assert detail["totals"]["costNanos"] == 50_000_000
        assert detail["totals"]["nativeBilledByCurrency"]["USD"]["amount"] == "0.05"
        assert detail["eventCount"] == 1
        assert detail["capability"] == "image_generation"
        assert detail["tokenUnknownItemCount"] == 1
        assert detail["meters"][0]["quantity"] == "1"
        assert detail["meters"][0]["measurementCount"] == 1
        assert report["taskGroups"][0]["modelBreakdown"] == [detail]
        assert report["totals"]["taskCount"] == report["models"][0]["taskCount"] == 1
        assert len(await storage.query_usage_settlements([start.event_id], history=True)) == 2
    finally:
        await storage.close()


async def test_fork_is_new_cost_group_and_copies_no_ledger_calls(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        parent = await manager.create("agent:main:webchat:fork-parent")
        await manager.append_message(parent.session_key, "user", "Synthetic fork input")
        await _call(storage, parent, "parent", 10)
        child = await manager.branch(
            parent.session_key, "agent:main:webchat:fork", fork_transcript=True
        )
        await _call(storage, child, "fork", 20)
        report = await _report(storage)
        assert report["eventCount"] == 2
        assert len(report["taskGroups"]) == 2
        assert sorted(group["totals"]["costNanos"] for group in report["taskGroups"]) == [
            100_000_000,
            200_000_000,
        ]
    finally:
        await storage.close()


async def test_query_retries_mixed_accounting_revision_and_fails_bounded(tmp_path, monkeypatch):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        await storage.initialize_usage_ledger(now_ms=1)
        revision = AsyncMock(side_effect=[1, 2, 2, 2])
        monkeypatch.setattr(storage, "get_usage_accounting_revision", revision)
        assert (await _report(storage))["accountingRevision"] == 2
        assert revision.await_count == 4
        revision.side_effect = range(6)
        with pytest.raises(RuntimeError, match="accounting changed"):
            await _report(storage)
        assert revision.await_count == 10
    finally:
        await storage.close()


async def test_subnano_provider_costs_sum_before_rounding(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        owner = SessionNode(session_key="agent:main:webchat:exact", session_id="exact")
        await storage.upsert_session(owner)
        for index in range(3):
            event_id = f"tiny-{index}"
            await storage.start_usage_event(
                UsageEventStart(
                    event_id=event_id,
                    execution_id="tiny",
                    call_index=index,
                    session_id=owner.session_id,
                    started_at_ms=100,
                )
            )
            await storage.finalize_usage_event(
                event_id,
                UsageEventCompletion(
                    completed_at_ms=200,
                    cost_source="provider_billed",
                ),
                items=(
                    UsageEventItem(event_id=event_id, ordinal=0, cost_source="provider_billed"),
                ),
                metering={
                    0: UsageItemMetering(
                        settlement=UsageSettlementRevision(
                            revision_id="tiny",
                            authority="provider_confirmed",
                            status="final",
                            native_amount="0.0000000004",
                            usd_amount="0.0000000004",
                        )
                    )
                },
            )
        report = await _report(storage)
        assert report["totals"]["costUsdExact"] == "0.0000000012"
        assert report["totals"]["costNanos"] == 1
        assert report["totals"]["billedCostNanos"] == 1
        assert report["totals"]["missingCostEntries"] == 0
        assert report["taskGroups"][0]["totals"]["costUsdExact"] == "0.0000000012"
        assert report["totals"]["nativeBilledByCurrency"]["USD"]["amount"] == "0.0000000012"
        assert report["totals"]["nativeBilledByCurrency"]["USD"]["usdEquivalentNanos"] == "1"
    finally:
        await storage.close()


@pytest.mark.parametrize("parent_has_call", [False, True])
async def test_durable_inline_metadata_shares_owner_but_has_own_run(tmp_path, parent_has_call):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        owner = SessionNode(session_key="agent:main:webchat:inline", session_id="inline-owner")
        await storage.upsert_session(owner)
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="parent-inline",
                session_key=owner.session_key,
                details={"session_id": owner.session_id, "session_epoch": 0, "metadata": {}},
            )
        )
        if parent_has_call:
            await _call(storage, owner, "parent-inline", 10)
        await storage.create_agent_task(
            AgentTaskRecord(
                task_id="durable-inline",
                session_key=owner.session_key,
                details={
                    "session_id": owner.session_id,
                    "session_epoch": 0,
                    "metadata": {
                        "parent_task_id": "parent-inline",
                        "parent_session_key": owner.session_key,
                        "parent_session_id": owner.session_id,
                        "parent_session_epoch": 0,
                    },
                },
            )
        )
        original = await _call(
            storage, owner, "durable-inline", 20, parent="parent-inline", kind="subagent"
        )
        report = await _report(storage)
        group = report["taskGroups"][0]
        assert group["branches"]["self"]["costUsd"] == (0.1 if parent_has_call else 0)
        assert group["branches"]["inlineSubagents"]["costUsd"] == 0.2
        assert group["attributionStatus"] == "complete"
        fresh = owner.model_copy(update={"epoch": 1})
        await storage.upsert_session(fresh)
        await storage.start_usage_event(original)
        await _call(storage, fresh, "fresh-main", 40)
        groups = {row["rootSessionEpoch"]: row for row in (await _report(storage))["taskGroups"]}
        assert groups[0]["totals"] == group["totals"]
        assert groups[0]["branches"]["inlineSubagents"]["costUsd"] == 0.2
        assert groups[0]["sessionKey"] is None
        assert groups[1]["totals"]["costUsd"] == 0.4
    finally:
        await storage.close()


async def test_final_native_receipt_keeps_its_fixed_fx_provenance(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "usage.db"))
    try:
        owner = SessionNode(session_key="agent:main:webchat:fx", session_id="fx-owner")
        await storage.upsert_session(owner)
        start = UsageEventStart(
            event_id="fx-event",
            execution_id="fx-run",
            call_index=0,
            session_id=owner.session_id,
            started_at_ms=100,
        )
        await storage.start_usage_event(start)
        revision = UsageSettlementRevision(
            revision_id="cny-receipt",
            authority="provider_confirmed",
            status="final",
            currency="CNY",
            native_amount="1.2",
            usd_amount="0.172043010752688172",
            fx_native_per_usd="6.975",
            fx_source="opensquilla_fixed_provider_normalization",
            fx_source_revision="CNY-per-USD:6.975",
        )
        await storage.finalize_usage_event(
            start.event_id,
            UsageEventCompletion(
                completed_at_ms=200,
                cost_nanos=172_043_011,
                billed_cost_nanos=172_043_011,
                cost_source="provider_billed",
            ),
            items=(
                UsageEventItem(
                    event_id=start.event_id,
                    ordinal=0,
                    cost_nanos=172_043_011,
                    billed_cost_nanos=172_043_011,
                    cost_source="provider_billed",
                ),
            ),
            metering={0: UsageItemMetering(settlement=revision)},
        )
        report = await _report(storage)
        native = report["totals"]["nativeBilledByCurrency"]["CNY"]
        assert native["amount"] == "1.2"
        assert native["usdEquivalentExact"] == "0.172043010752688172"
        assert native["normalizationRatesNativePerUsd"] == ["6.975"]
        assert native["normalizationSources"] == [
            {
                "source": "opensquilla_fixed_provider_normalization",
                "revision": "CNY-per-USD:6.975",
                "rate": "6.975",
            }
        ]
    finally:
        await storage.close()


def test_settlement_does_not_erase_missing_ensemble_usage_without_an_item():
    event = {"event_id": "partial", "missing_cost_entries": 2}
    items = {"partial": [{"event_id": "partial", "ordinal": 0, "cost_source": "unavailable"}]}
    revised, _, _ = apply_current_settlements(
        [event],
        items,
        [],
        [
            {
                "event_id": "partial",
                "ordinal": 0,
                "authority": "provider_confirmed",
                "status": "final",
                "currency": "USD",
                "native_amount": "0.1",
                "usd_amount": "0.1",
                "cost_nanos": 100_000_000,
                "billed_cost_nanos": 100_000_000,
                "estimated_cost_nanos": 0,
                "cost_source": "provider_billed",
                "missing_cost_entries": 0,
            }
        ],
    )
    assert revised[0]["missing_cost_entries"] == 1
    assert revised[0]["cost_nanos"] == 100_000_000
    assert revised[0]["cost_source"] == "mixed"
