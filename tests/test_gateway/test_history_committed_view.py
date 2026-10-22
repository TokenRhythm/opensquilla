"""History reads keep a committed owner-bound view while maintenance holds its lock."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.adapters import session_history_projection
from opensquilla.gateway.adapters.session_history import SessionHistoryStorageAdapter
from opensquilla.gateway.adapters.session_history_projection import read_chat_history_v4
from opensquilla.gateway.rpc.registry import RpcContext, RpcHandlerError
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, SessionSummary
from opensquilla.session.recovery_reads import recovery_read_scope
from opensquilla.session.storage import SessionStorage, StorageBusyError
from opensquilla.session.turn_context import turn_context_scope
from tests.test_session.test_recovery_reads import NativeGate, install_gate


async def admitted_history(params, ctx):
    """Enter the read budget supplied by the WebSocket recovery scheduler."""
    with recovery_read_scope(params["sessionKey"], deadline=time.monotonic() + 5):
        return await read_chat_history_v4(params, ctx)


@pytest.fixture
async def history_case(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "history-view.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create("agent:main:webchat:history-view")
    await manager.append_message(node.session_key, "user", "Keep this original request.")
    await manager.append_message(node.session_key, "assistant", "Original answer.")

    async def turn_handler(_run):
        raise AssertionError("History tests must not execute a model turn")

    runtime = TaskRuntime(storage=storage, turn_handler=turn_handler)
    runner = TurnRunner(provider_selector=SimpleNamespace(current_config=None),
                        session_manager=manager)
    # Use exactly the boot.py Gateway lock wiring, not the bare runner fallback.
    runner.set_session_lock_provider(runtime._get_session_lock_for_turn)
    context = RpcContext(
        conn_id="history-view", session_manager=manager, turn_runner=runner,
        task_runtime=runtime,
    )
    try:
        yield SimpleNamespace(storage=storage, manager=manager, node=node,
                              runtime=runtime, runner=runner, context=context)
    finally:
        await storage.close()


@pytest.mark.parametrize("version", ["v4", "v2"])
async def test_history_reads_while_real_gateway_mutation_lock_is_held(history_case, version):
    case = history_case
    lock = case.runtime._get_session_lock_for_turn(case.node.session_key)
    assert lock is case.runner.get_session_lock(case.node.session_key)
    async with lock:
        if version == "v4":
            result = await asyncio.wait_for(admitted_history(
                {"sessionKey": case.node.session_key}, case.context,
            ), 0.75)
            messages = result["messages"]
        else:
            result = await asyncio.wait_for(rpc_sessions._handle_sessions_history_page_v2(
                {"key": case.node.session_key, "direction": "before"}, case.context,
            ), 0.75)
            messages = [item["message"] for item in result["items"]]
            assert (result["session_id"], result["session_epoch"]) == (
                case.node.session_id, case.node.epoch,
            )
        assert lock.locked(), "A successful read must not release the writer's lock"
        assert [message["role"] for message in messages] == ["user", "assistant"]
        assert result["canonical_available"] and result["canonical_complete"]


async def test_page_and_summaries_share_one_committed_snapshot(history_case, monkeypatch):
    case = history_case
    await case.storage.save_summary(SessionSummary(
        session_id=case.node.session_id, session_key=case.node.session_key,
        summary_text="First checkpoint.", compaction_id="first", created_at=1,
    ))
    page_read, release_page = asyncio.Event(), asyncio.Event()
    original = case.storage.get_canonical_transcript_page
    readers = []

    async def held_page(*args, **kwargs):
        result = await original(*args, **kwargs)
        readers.append(case.storage.conn)
        assert case.storage.conn.in_transaction
        page_read.set()
        await release_page.wait()
        return result

    monkeypatch.setattr(case.storage, "get_canonical_transcript_page", held_page)
    reading = asyncio.create_task(admitted_history(
        {"sessionKey": case.node.session_key}, case.context,
    ))
    try:
        await asyncio.wait_for(page_read.wait(), 2)
        await case.storage.save_summary(SessionSummary(
            session_id=case.node.session_id, session_key=case.node.session_key,
            summary_text="Second checkpoint.", compaction_id="second", created_at=2,
        ))
        release_page.set()
        response = await asyncio.wait_for(reading, 2)
        assert [
            summary["compaction_id"] for summary in response["compaction_summaries"]
        ] == ["first"]
        following = await admitted_history({"sessionKey": case.node.session_key}, case.context)
        assert [
            summary["compaction_id"] for summary in following["compaction_summaries"]
        ] == ["first", "second"]
        assert readers and all(reader is not case.storage._conn for reader in readers)
        assert all(reader is not case.storage._transcript_reader for reader in readers)
        assert all(not reader.in_transaction for reader in readers)
    finally:
        release_page.set()
        await asyncio.gather(reading, return_exceptions=True)


@pytest.mark.parametrize("anchor", [False, True])
async def test_v2_rejects_owner_changed_before_publication(history_case, monkeypatch, anchor):
    case = history_case
    reset_requested, reset_done = asyncio.Event(), asyncio.Event()

    async def reset_owner():
        await reset_requested.wait()
        case.node.epoch += 1
        await case.storage.upsert_session(case.node)
        reset_done.set()

    # Create the writer outside the read task so it does not inherit a query-only lease.
    reset = asyncio.create_task(reset_owner())
    original = rpc_sessions.read_chat_history_v4

    async def reset_after_page(params, context):
        response = await original(params, context)
        reset_requested.set()
        await reset_done.wait()
        return response

    monkeypatch.setattr(rpc_sessions, "read_chat_history_v4", reset_after_page)
    params = {"key": case.node.session_key, "direction": "before"}
    if anchor:
        entries = await case.manager.get_canonical_transcript(case.node.session_key)
        params["anchor_message_id"] = entries[0].message_id
    try:
        with pytest.raises(RpcHandlerError) as caught:
            await rpc_sessions._handle_sessions_history_page_v2(params, case.context)
        assert caught.value.code == "REBASE_REQUIRED"
    finally:
        reset_requested.set()
        await asyncio.gather(reset, return_exceptions=True)


async def test_reader_sees_old_history_during_actual_uncommitted_compaction(
    history_case, monkeypatch,
):
    case = history_case
    source = await case.manager.capture_compaction_source(case.node.session_key)
    original = case.storage._archive_transcript_entries
    archived, release = asyncio.Event(), asyncio.Event()

    async def held_archive(*args, **kwargs):
        await original(*args, **kwargs)
        archived.set()
        await release.wait()

    monkeypatch.setattr(case.storage, "_archive_transcript_entries", held_archive)
    compacting = asyncio.create_task(case.manager.persist_compaction_result(
        case.node.session_key, "Original request and answer are retained.", [],
        removed_count=2, source_snapshot=source, compaction_id="held-commit",
    ))
    try:
        await asyncio.wait_for(archived.wait(), 2)
        before = await asyncio.wait_for(admitted_history(
            {"sessionKey": case.node.session_key}, case.context,
        ), 1)
        assert [message["role"] for message in before["messages"]] == ["user", "assistant"]
        assert before["compaction_summaries"] == []
        assert not release.is_set()
        release.set()
        assert (await asyncio.wait_for(compacting, 2)).applied
        after = await admitted_history({"sessionKey": case.node.session_key}, case.context)
        assert [message["message_id"] for message in before["messages"]] == [
            message["message_id"] for message in after["messages"]
        ]
        assert [summary["compaction_id"] for summary in after["compaction_summaries"]] == [
            "held-commit",
        ]
    finally:
        release.set()
        await asyncio.gather(compacting, return_exceptions=True)


@pytest.mark.parametrize("failure", ["cancel", "deadline"])
async def test_snapshot_retains_reader_until_cancelled_native_work_cleans_up(
    history_case, monkeypatch, failure,
):
    case = history_case
    gate = NativeGate()
    await install_gate(case.storage, monkeypatch, gate)
    adapter = SessionHistoryStorageAdapter(case.manager)

    async def capture(_owner):
        return await case.storage._read_history_query("SELECT recovery_test_gate()", ())

    with recovery_read_scope(
        case.node.session_key, deadline=time.monotonic() + (0.15 if failure == "deadline" else 5),
    ) as budget:
        reading = asyncio.create_task(adapter.read_view(case.node.session_key, capture))
    try:
        await gate.wait()
        if failure == "cancel":
            reading.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(reading, 1)
        else:
            with pytest.raises(StorageBusyError) as caught:
                await asyncio.wait_for(reading, 1)
            assert caught.value.stage == "deadline"
        pool = case.storage._recovery_read_pool
        assert pool is not None and pool.active_count == pool.retiring_count == 1
        assert not adapter.in_committed_view
        # Query-only recovery must not reserve or poison the canonical writer.
        await asyncio.wait_for(case.storage.upsert_session(case.node), 1)
    finally:
        gate.release.set()
        await asyncio.gather(reading, return_exceptions=True)
        await asyncio.wait_for(budget.drain(), 2)
    assert pool.active_count == pool.retiring_count == 0
    assert all(not reader.in_transaction for reader in pool._idle)
    assert not case.storage._poisoned


async def test_legacy_without_snapshot_capability_keeps_bounded_mutation_lock(
    history_case, monkeypatch,
):
    case = history_case
    monkeypatch.setattr(
        SessionStorage, "supports_committed_history_view", property(lambda _: False),
    )
    monkeypatch.setattr(session_history_projection, "_CHAT_HISTORY_LOCK_BUDGET_SECONDS", 0.02)
    async with case.runner.get_session_lock(case.node.session_key):
        with pytest.raises(StorageBusyError) as caught:
            await admitted_history({"sessionKey": case.node.session_key}, case.context)
    assert caught.value.stage == "lock_acquire"
    assert caught.value.resource == "session_mutation_lock"


async def test_ordinary_v4_retains_bounded_mutation_lock(history_case, monkeypatch):
    case = history_case
    monkeypatch.setattr(session_history_projection, "_CHAT_HISTORY_LOCK_BUDGET_SECONDS", 0.02)
    async with case.runner.get_session_lock(case.node.session_key):
        with pytest.raises(StorageBusyError) as caught:
            await read_chat_history_v4({"sessionKey": case.node.session_key}, case.context)
    assert caught.value.stage == "lock_acquire"
    assert caught.value.resource == "session_mutation_lock"
    assert case.storage._recovery_read_pool is None


@pytest.mark.parametrize("existing_ledger", [False, True])
async def test_turn_outcomes_use_snapshot_reader_without_schema_writes(
    history_case, monkeypatch, existing_ledger,
):
    case = history_case
    if existing_ledger:
        service = await ArtifactSessionService.from_session_storage(case.storage)
        await service.close()
    turn_id = "history-turn"
    with turn_context_scope({"turn_id": turn_id}):
        await case.manager.append_message(case.node.session_key, "assistant", "Turn result.")
    await case.storage.create_agent_task(AgentTaskRecord(
        task_id=turn_id, session_key=case.node.session_key, status=AgentTaskStatus.SUCCEEDED,
        details={"turn_id": turn_id, "turn_outcome": {"kind": "completed"}},
    ))
    initialization_calls = []
    ledger_reads = []
    original = ArtifactSessionService.list_mutation_attempts_by_turn_ids

    async def forbid_initialize(*args, **kwargs):
        initialization_calls.append(True)
        raise AssertionError("History must not initialize the artifact schema")

    async def read_ledger(self, **kwargs):
        ledger_reads.append(case.storage.conn)
        assert case.storage.in_committed_history_view and case.storage.conn.in_transaction
        assert case.storage.conn is not case.storage._conn
        return await original(self, **kwargs)

    monkeypatch.setattr(ArtifactSessionService, "from_session_storage", forbid_initialize)
    monkeypatch.setattr(ArtifactSessionService, "list_mutation_attempts_by_turn_ids", read_ledger)
    response = await admitted_history({"sessionKey": case.node.session_key}, case.context)
    assert [outcome["turn_id"] for outcome in response["turn_outcomes"]] == [turn_id]
    assert initialization_calls == []
    assert bool(ledger_reads) is existing_ledger


async def test_snapshot_summary_failure_is_not_hidden_as_empty_success(history_case, monkeypatch):
    case = history_case

    async def failed_summaries(*args, **kwargs):
        raise StorageBusyError("summary-read", waited_ms=1, retry_after_ms=100)

    monkeypatch.setattr(case.manager, "get_display_summaries", failed_summaries)
    with pytest.raises(StorageBusyError):
        await admitted_history({"sessionKey": case.node.session_key}, case.context)


async def test_post_snapshot_alias_projection_still_obeys_page_byte_budget(
    history_case, monkeypatch,
):
    case = history_case
    params = {"key": case.node.session_key, "direction": "before"}
    original = await rpc_sessions._handle_sessions_history_page_v2(params, case.context)
    target_bytes = len(json.dumps(
        original, ensure_ascii=False, separators=(",", ":"),
    ).encode()) + 64

    async def expand_aliases(messages, **kwargs):
        return [dict(message, projected_alias="x" * 500) for message in messages]

    monkeypatch.setattr(rpc_sessions, "_project_fork_workspace_previews", expand_aliases)
    response = await rpc_sessions._handle_sessions_history_page_v2(
        dict(params, target_bytes=target_bytes), case.context,
    )
    assert len(json.dumps(
        response, ensure_ascii=False, separators=(",", ":"),
    ).encode()) <= target_bytes
    assert len(response["items"]) == 1
    assert response["before_cursor"]
    assert response["has_more_before"] and not response["complete_for_requested_window"]
