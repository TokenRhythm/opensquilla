"""Offline acceptance of manual compaction through Gateway and file-backed SQLite."""

import asyncio
import sqlite3
import sys
from contextlib import closing
from textwrap import dedent
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio

from opensquilla.engine.cache_break_monitor import active_compaction_ids, cancel_active_compactions
from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.adapters.session_maintenance import (
    build_gateway_session_maintenance_adapter,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.rpc.registry import RpcContext
from opensquilla.gateway.rpc_sessions import _task_state_summary
from opensquilla.gateway.session_streams import get_session_streams
from opensquilla.provider.model_catalog import ModelCatalog
from opensquilla.provider.openai import OpenAIProvider
from opensquilla.provider.selector import ProviderConfig
from opensquilla.provider.types import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.session.compaction_state import extract_compaction_obligations
from opensquilla.session.context_view import (
    build_compaction_context_records,
    compaction_replay_is_complete,
    format_compaction_summary_context,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


@pytest_asyncio.fixture
async def manual_compaction(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSQUILLA_STATE_DIR", str(tmp_path / "profile"))
    config = GatewayConfig(
        workspace_dir=str(tmp_path / "workspace"),
        llm={
            "provider": "openai", "model": "synthetic-manual", "api_key": "test-only",
            "context_window_tokens": 100_000, "max_tokens": 1024, "thinking": "off",
        },
        compaction={"protected_recent_messages": 2},
        prompt={"mode": "minimal"},
    )
    provider_config = ProviderConfig(
        provider="openai", model="synthetic-manual", api_key="test-only",
    )
    selector = SimpleNamespace(current_config=provider_config)
    database = tmp_path / "sessions.sqlite"
    storage = SessionStorage(str(database))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create(f"agent:main:webchat:compact-{uuid4().hex}")
    for index in range(12):
        await manager.append_message(
            node.session_key, "user" if index % 2 == 0 else "assistant",
            "Earlier discussion details were reviewed. " * 100, token_count=250,
        )
    calls = []

    async def summary_reply(self, messages, tools=None, config=None):
        calls.append(messages)
        yield TextDeltaEvent(text="Earlier discussion reviewed; continue the current task.")
        yield DoneEvent(stop_reason="end_turn")

    # Only the physical summary reply is substituted. Budget projection,
    # selection, quality gates, checkpointing, SQLite rewrite and lifecycle run.
    monkeypatch.setattr(OpenAIProvider, "chat", summary_reply)
    runner = TurnRunner(
        provider_selector=selector, session_manager=manager, config=config,
        model_catalog=ModelCatalog(),
    )
    context = RpcContext(
        conn_id="offline-manual-contract", config=config, session_manager=manager,
        provider_selector=selector, turn_runner=runner,
    )
    try:
        yield SimpleNamespace(
            adapter=build_gateway_session_maintenance_adapter(context),
            storage=storage, database=database, manager=manager, node=node, calls=calls,
            config=config, context=context,
        )
    finally:
        await storage.close()


def _operation_events(key, compaction_id):
    replay = get_session_streams().replay(key, since_stream_seq=0)
    assert replay.replay_complete
    return [
        event.payload for event in replay.events
        if event.event_name == "session.event.compaction"
        and event.payload.get("compaction_id") == compaction_id
    ]


def _assert_operation_terminal(key, compaction_id, status):
    events = _operation_events(key, compaction_id)
    assert events[0]["status"] == "started"
    assert events[-1]["status"] == status
    assert [
        event["status"] for event in events
        if event["status"] in {"completed", "skipped", "failed", "cancelled"}
    ] == [status]


async def _assert_idle(case):
    rows = await case.storage.list_agent_tasks(session_key=case.node.session_key)
    task_state = _task_state_summary(rows)
    assert task_state["run_status"] == "idle"
    assert task_state["active_task"] is None
    assert active_compaction_ids(case.node.session_key) == ()
    snapshot = get_session_streams().live_snapshot(case.node.session_key)
    assert snapshot.task_id is None


def _assert_durable_message_ids(case, expected):
    # A separate SQLite connection must observe the committed state, not just
    # SessionManager's in-process view or the original writer connection.
    with closing(sqlite3.connect(case.database)) as reader:
        reader.execute("PRAGMA query_only = ON")
        rows = reader.execute(
            "SELECT message_id FROM transcript_entries WHERE session_id = ? ORDER BY id",
            (case.node.session_id,),
        ).fetchall()
    assert [row[0] for row in rows] == [entry.message_id for entry in expected]


@pytest.mark.asyncio
async def test_manual_rpc_below_threshold_commits_then_repeat_skips_without_model(
    manual_compaction,
):
    case = manual_compaction
    key = case.node.session_key
    before = await case.manager.get_transcript(key)
    response = await get_dispatcher().dispatch(
        "manual-first", "sessions.contextCompact", {"key": key}, case.context,
    )
    assert response.ok, response.error
    first = response.payload
    after = await case.manager.get_transcript(key)

    assert first["status"] == "completed"
    assert first["durability"] == "durable"
    assert first["tokens_before"] < first["quality_report"]["auto_trigger_tokens"]
    assert first["removed_count"] == len(before) - len(after) > 0
    assert [entry.message_id for entry in after] == [entry.message_id for entry in before[-2:]]
    assert await case.storage.get_all_summaries(case.node.session_id)
    _assert_durable_message_ids(case, after)
    _assert_operation_terminal(key, first["compaction_id"], "completed")
    await _assert_idle(case)
    call_count = len(case.calls)

    response = await get_dispatcher().dispatch(
        "manual-repeat", "sessions.contextCompact", {"key": key}, case.context,
    )
    assert response.ok, response.error
    second = response.payload
    assert second["status"] == "skipped"
    assert second["reason"] == "protected_tail_exhausts_compaction_window"
    assert second["compaction_id"] != first["compaction_id"]
    assert len(case.calls) == call_count
    assert await case.manager.get_transcript(key) == after
    _assert_operation_terminal(key, second["compaction_id"], "skipped")
    await _assert_idle(case)


@pytest.mark.asyncio
async def test_summary_does_not_fit_preserves_sqlite_and_returns_idle(manual_compaction):
    case = manual_compaction
    key = case.node.session_key
    before = await case.manager.get_transcript(key)
    result = await case.adapter.compact({"key": key, "contextWindowTokens": 128})

    # The known history target cannot hold even the checkpoint wrapper. G's
    # precheck must reject it before spending a summary call.
    assert case.calls == []
    assert result["status"] == "skipped"
    assert result["reason"] == "summary_does_not_fit"
    assert result["applied"] is False
    assert result["durability"] == "none"
    assert await case.manager.get_transcript(key) == before
    _assert_durable_message_ids(case, before)
    assert await case.storage.get_all_summaries(case.node.session_id) == []
    current = await case.manager.get_session(key)
    assert current.compaction_count == 0
    _assert_operation_terminal(key, result["compaction_id"], "skipped")
    await _assert_idle(case)


@pytest.mark.asyncio
async def test_manual_missing_constraints_are_diagnostic_without_synthetic_backfill(
    manual_compaction,
):
    case = manual_compaction
    key = case.node.session_key
    constraints = [
        f"Constraint: Component {index:02d} must preserve existing route ownership and "
        "authorization boundaries while adding the requested application screen. Validate "
        "navigation, persisted state, error recovery, keyboard controls and provider replay "
        "before releasing changes."
        for index in range(64)
    ]
    await case.manager.append_message(key, "user", "\n".join(constraints))
    await case.manager.append_message(key, "assistant", "All component requirements recorded.")
    await case.manager.append_message(key, "user", "Continue with the implementation.")
    await case.manager.append_message(key, "assistant", "Starting the current implementation.")
    before = await case.manager.get_transcript(key)
    obligations = extract_compaction_obligations(before[:-2])
    assert len(obligations) == 64

    result = await case.adapter.compact({"key": key})

    assert result["status"] == "completed"
    # The fixture model omits the constraints. A durable write is not semantic
    # success, and code must not manufacture a successful summary by copying
    # obligation strings back into the model-authored checkpoint.
    assert result["coverage_status"] == "fail_reported"
    assert result["missing_obligation_count"] > 0
    assert result["critical_carry_forward_count"] == 0
    assert result["tokens_before"] < result["quality_report"]["auto_trigger_tokens"]
    assert len(case.calls) == 1
    after = await case.manager.get_transcript(key)
    assert [entry.message_id for entry in after] == [entry.message_id for entry in before[-2:]]
    _assert_durable_message_ids(case, after)
    records = build_compaction_context_records(
        context_states=await case.manager.get_context_states(key),
        summaries=await case.storage.get_all_summaries(case.node.session_id),
    )
    texts = [record.text for record in records]
    replay = format_compaction_summary_context(texts)
    assert compaction_replay_is_complete(texts, replay)
    assert "Earlier discussion reviewed; continue the current task." in replay
    assert all(obligation.value not in replay for obligation in obligations)
    canonical = await case.manager.get_canonical_transcript(key)
    assert [entry.model_dump() for entry in canonical] == [entry.model_dump() for entry in before]
    canonical_text = "\n".join(str(entry.content) for entry in canonical)
    assert all(constraint in canonical_text for constraint in constraints)
    _assert_operation_terminal(key, result["compaction_id"], "completed")
    await _assert_idle(case)


async def _assert_unchanged(case, before):
    assert await case.manager.get_transcript(case.node.session_key) == before
    assert await case.manager.get_canonical_transcript(case.node.session_key) == before
    _assert_durable_message_ids(case, before)
    assert await case.storage.get_all_summaries(case.node.session_id) == []
    assert await case.manager.get_context_states(case.node.session_key) == []
    assert (await case.manager.get_session(case.node.session_key)).compaction_count == 0
    await _assert_idle(case)


def _latest_operation_id(case):
    events = get_session_streams().replay(case.node.session_key, since_stream_seq=0).events
    return next(
        event.payload["compaction_id"] for event in reversed(events)
        if event.event_name == "session.event.compaction"
        and event.payload.get("status") == "started"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error_event", "transport_exception", "truncated_stream"])
async def test_provider_failure_never_commits_partial_summary(
    manual_compaction, monkeypatch, failure,
):
    case = manual_compaction
    before = await case.manager.get_transcript(case.node.session_key)
    retry_delays = []

    async def skip_retry_wait(delay):
        retry_delays.append(delay)

    monkeypatch.setattr("opensquilla.session.compaction.sleep_before_retry", skip_retry_wait)
    monkeypatch.setattr("opensquilla.provider.recovery.random.randint", lambda *_args: 0)

    async def failing_reply(self, messages, tools=None, config=None):
        case.calls.append(messages)
        yield TextDeltaEvent(text="Partial summary before provider failure.")
        if failure == "error_event":
            yield ErrorEvent(message="Controlled service unavailable", code="503")
        elif failure == "transport_exception":
            raise ConnectionError("Controlled provider disconnect")
        # A clean iterator end without DoneEvent is also a failed summary.

    monkeypatch.setattr(OpenAIProvider, "chat", failing_reply)
    result = await case.adapter.compact({"key": case.node.session_key})

    assert result["status"] == "skipped"
    assert result["reason"] == "summary_failed"
    assert len(case.calls) == (4 if failure == "error_event" else 1)
    assert retry_delays == ([1, 2, 4] if failure == "error_event" else [])
    assert case.context.turn_runner._compaction_failures[case.node.session_key].count == 1
    _assert_operation_terminal(case.node.session_key, result["compaction_id"], "skipped")
    await _assert_unchanged(case, before)


@pytest.mark.asyncio
async def test_manual_recovery_progress_keeps_epoch_and_contiguous_replay(
    manual_compaction, monkeypatch,
):
    from opensquilla.engine.cache_break_monitor import add_compaction_listener
    from opensquilla.gateway.rpc_sessions import _v2_replay_snapshot

    case = manual_compaction
    key = case.node.session_key
    streams = get_session_streams()
    base_seq = streams.current_seq(key)

    async def skip_retry_wait(_delay):
        return None

    async def limited_reply(self, messages, tools=None, config=None):
        yield ErrorEvent(message="Controlled temporary limit", code="429", http_status=429)

    # Match boot's synchronous listener: it records directly, without the
    # RPC publisher's asynchronous epoch preparation. Missing epochs would
    # burn stream sequence numbers while dropping manual recovery events.
    observed = []

    def record_runtime_event(session_key, payload):
        if session_key == key:
            observed.append(payload)
            streams.record(session_key, "session.event.compaction", payload)

    monkeypatch.setattr("opensquilla.session.compaction.sleep_before_retry", skip_retry_wait)
    monkeypatch.setattr("opensquilla.provider.recovery.random.randint", lambda *_args: 0)
    monkeypatch.setattr(OpenAIProvider, "chat", limited_reply)
    remove_listener = add_compaction_listener(record_runtime_event)
    try:
        result = await case.adapter.compact({"key": key})
    finally:
        remove_listener()

    assert result["reason"] == "summary_failed"
    assert observed and all(payload.get("epoch") == case.node.epoch for payload in observed)
    replay, proof, _batches = _v2_replay_snapshot(
        key=key, stream_generation=streams.stream_generation, base_seq=base_seq,
    )
    assert replay.replay_complete
    assert [event.stream_seq for event in replay.events] == list(
        range(base_seq + 1, proof["target_seq"] + 1)
    )
    recovery = [event.payload["recovery"] for event in replay.events
                if "recovery" in event.payload]
    assert recovery and any(item.get("http_status") == 429 for item in recovery)
    assert replay.events[-1].payload["failure_detail"] == {
        "failure_kind": "rate_limited", "http_status": 429,
    }
    await _assert_idle(case)


@pytest.mark.asyncio
async def test_successful_manual_compaction_preserves_snapshot_owner_and_refreshes_history(
    manual_compaction,
):
    import json

    from starlette.websockets import WebSocketState

    from opensquilla.gateway.rpc import get_dispatcher
    from opensquilla.gateway.rpc_sessions import (
        _handle_sessions_history_page_v2,
        _handle_sessions_read_install_v2,
        _handle_sessions_read_open_v2,
    )
    from opensquilla.gateway.websocket import (
        SubscriptionManager,
        WsConnection,
        _admit_session_mutation,
        get_registry,
    )

    class Socket:
        client_state = WebSocketState.CONNECTED
        application_state = WebSocketState.CONNECTED

        def __init__(self):
            self.frames = []

        async def send_text(self, text):
            self.frames.append(json.loads(text))

        async def close(self, **_kwargs):
            return None

    case = manual_compaction
    key = case.node.session_key
    before = await case.manager.get_canonical_transcript(key)
    conn = WsConnection(f"manual-snapshot-{uuid4().hex}", Socket())
    conn._recovery_enabled = True
    conn._recovery_runtime = case.storage
    conn._subscriptions = SubscriptionManager()
    conn._enable_flow()
    conn._start_writer(maxsize=512, enabled=True)
    get_registry().register(conn)
    case.context.conn_id = conn.conn_id
    case.context.principal = conn.principal
    case.context.subscription_manager = conn._subscriptions
    conn._subscriptions.subscribe_messages(conn.conn_id, key)

    async def request(method, params):
        response = await get_dispatcher().dispatch("review", method, params, case.context)
        assert response.ok, response.error
        return response

    completed = None
    try:
        opened = await _handle_sessions_read_open_v2({"key": key}, case.context)
        response = await request("sessions.messages.snapshot.read", {
            "key": key, "sync_revision": "before-compaction",
        })
        snapshot = response.payload
        await conn.send_res(response)
        await asyncio.sleep(0)
        await request("transport.flow.update", {
            "delivery_epoch": snapshot["delivery"]["delivery_epoch"],
            "ack_delivery_id": 0,
            "staged_delivery_ids": [snapshot["delivery"]["delivery_id"]],
        })
        _, completed = _admit_session_mutation(
            "sessions.contextCompact", {"key": key}, case.context,
        )
        result = await case.adapter.compact({"key": key})
        assert result["status"] == "completed"
        completed.set_result(None)
        await asyncio.sleep(0)
        current = await case.manager.get_session(key)
        assert (current.session_id, current.epoch) == (case.node.session_id, case.node.epoch)
        assert await case.manager.get_canonical_transcript(key) == before
        history = await _handle_sessions_history_page_v2({
            "key": key, "direction": "before", "target_items": 64,
        }, case.context)
        assert history["canonical_complete"]
        assert len(history["compaction_summaries"]) == 1
        # The snapshot's owner remains valid; replay carries the new lifecycle
        # including the durable terminal that asks clients to refresh history.
        resumed = await request("sessions.messages.resume", {
            **{name: snapshot[name] for name in (
                "key", "snapshot_id", "sync_revision", "stream_generation",
            )},
            "stream_seq": snapshot["current_stream_seq"],
        })
        target = get_session_streams().current_seq(key)
        assert resumed.payload["replay_to_seq"] == target
        installed = await _handle_sessions_read_install_v2({
            "key": key, "lease_id": opened["lease_id"],
            "base_applied": True, "consumed_through_seq": target,
        }, case.context)
        assert installed["status"] == "installed"
    finally:
        if completed is not None and not completed.done():
            completed.set_result(None)
        await conn._stop_writer()
        conn._cleanup_transport()
        get_registry().unregister(conn.conn_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_cancel_running_summary_closes_provider_and_preserves_history(
    manual_compaction, monkeypatch, background,
):
    case = manual_compaction
    before = await case.manager.get_transcript(case.node.session_key)
    started, closed = asyncio.Event(), asyncio.Event()

    async def blocked_reply(self, messages, tools=None, config=None):
        started.set()
        try:
            await asyncio.Event().wait()
            yield DoneEvent(stop_reason="end_turn")
        finally:
            closed.set()

    monkeypatch.setattr(OpenAIProvider, "chat", blocked_reply)
    task = asyncio.create_task(case.adapter.compact({
        "key": case.node.session_key, "wait": not background,
    }))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        if background:
            accepted = await task
            operation_id = accepted["compaction_id"]
            assert accepted["status"] == "started"
            cancelled = cancel_active_compactions(case.node.session_key)
            assert len(cancelled) == 1
            await asyncio.wait_for(asyncio.gather(*cancelled, return_exceptions=True), timeout=5)
        else:
            operation_id = _latest_operation_id(case)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert closed.is_set()
        _assert_operation_terminal(case.node.session_key, operation_id, "cancelled")
        assert _operation_events(case.node.session_key, operation_id)[-1]["reason"] == "cancelled"
        await _assert_unchanged(case, before)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        remaining = cancel_active_compactions(case.node.session_key)
        await asyncio.gather(*remaining, return_exceptions=True)


# The one-second deadline is the contract under test. Keep this out of the
# xdist worker pool so host scheduling cannot consume that budget before the
# synthetic provider generator is first advanced.
@pytest.mark.ci_serial
@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_absolute_deadline_closes_provider_and_emits_one_skipped_terminal(
    manual_compaction, monkeypatch, background,
):
    case = manual_compaction
    case.config.compaction.total_timeout_seconds = 1.0
    case.config.compaction.heartbeat_interval_seconds = 0.1
    before = await case.manager.get_transcript(case.node.session_key)
    started, closed = asyncio.Event(), asyncio.Event()

    async def blocked_reply(self, messages, tools=None, config=None):
        started.set()
        try:
            await asyncio.Event().wait()
            yield DoneEvent(stop_reason="end_turn")
        finally:
            closed.set()

    monkeypatch.setattr(OpenAIProvider, "chat", blocked_reply)
    result = await asyncio.wait_for(case.adapter.compact({
        "key": case.node.session_key, "wait": not background,
    }), timeout=5)
    if background:
        assert result["status"] == "started"
        async with asyncio.timeout(5):
            while active_compaction_ids(case.node.session_key):
                await asyncio.sleep(0.01)
    else:
        assert result["status"] == "skipped"
        assert result["applied"] is False
        assert result["durability"] == "none"
    assert started.is_set() and closed.is_set()
    operation_id = result["compaction_id"]
    events = _operation_events(case.node.session_key, operation_id)
    assert any(event.get("heartbeat") for event in events)
    assert events[-1]["reason"] == "compaction_deadline_exceeded"
    assert events[-1]["applied"] is False
    assert events[-1]["durability"] == "none"
    _assert_operation_terminal(case.node.session_key, operation_id, "skipped")
    await _assert_unchanged(case, before)


@pytest.mark.asyncio
async def test_concurrent_append_rejects_stale_summary_without_losing_new_message(
    manual_compaction, monkeypatch,
):
    case = manual_compaction
    before = await case.manager.get_transcript(case.node.session_key)
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_reply(self, messages, tools=None, config=None):
        started.set()
        await release.wait()
        yield TextDeltaEvent(text="Earlier discussion reviewed.")
        yield DoneEvent(stop_reason="end_turn")

    monkeypatch.setattr(OpenAIProvider, "chat", delayed_reply)
    task = asyncio.create_task(case.adapter.compact({"key": case.node.session_key}))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        appended = await case.manager.append_message(
            case.node.session_key, "user", "A new request arrived during compaction.",
        )
        expected = await case.manager.get_transcript(case.node.session_key)
        assert expected[:-1] == before
        assert expected[-1].message_id == appended.message_id
        assert expected[-1].content == "A new request arrived during compaction."
        release.set()
        result = await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert result["status"] == "skipped"
    assert result["reason"] == "stale_preimage"
    _assert_operation_terminal(case.node.session_key, result["compaction_id"], "skipped")
    await _assert_unchanged(case, expected)


@pytest.mark.asyncio
async def test_simultaneous_manual_requests_commit_once_and_skip_second(
    manual_compaction, monkeypatch,
):
    case = manual_compaction
    before = await case.manager.get_transcript(case.node.session_key)
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed_reply(self, messages, tools=None, config=None):
        case.calls.append(messages)
        started.set()
        await release.wait()
        yield TextDeltaEvent(text="Earlier discussion reviewed.")
        yield DoneEvent(stop_reason="end_turn")

    monkeypatch.setattr(OpenAIProvider, "chat", delayed_reply)
    first = asyncio.create_task(case.adapter.compact({"key": case.node.session_key}))
    second = None
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        second = asyncio.create_task(case.adapter.compact({"key": case.node.session_key}))
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    finally:
        release.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert [result["status"] for result in results] == ["completed", "skipped"]
    assert len({result["compaction_id"] for result in results}) == 2
    assert len(case.calls) == 1
    assert (await case.manager.get_session(case.node.session_key)).compaction_count == 1
    assert len(await case.storage.get_all_summaries(case.node.session_id)) == 1
    assert await case.manager.get_canonical_transcript(case.node.session_key) == before
    _assert_durable_message_ids(case, before[-2:])
    for result in results:
        _assert_operation_terminal(case.node.session_key, result["compaction_id"], result["status"])
    await _assert_idle(case)


@pytest.mark.asyncio
async def test_sqlite_commit_failure_rolls_back_all_rows_and_emits_failed(
    manual_compaction, monkeypatch,
):
    case = manual_compaction
    before = await case.manager.get_transcript(case.node.session_key)
    original_commit = case.storage._commit_transaction
    attempted = False

    async def fail_compaction_commit(conn, operation, deadline, started):
        nonlocal attempted
        if operation == "rewrite_compacted_session":
            attempted = True
            assert conn.in_transaction
            raise sqlite3.OperationalError("Controlled disk I/O failure during commit")
        return await original_commit(conn, operation, deadline, started)

    monkeypatch.setattr(case.storage, "_commit_transaction", fail_compaction_commit)
    with pytest.raises(sqlite3.OperationalError, match="Controlled disk I/O"):
        await case.adapter.compact({"key": case.node.session_key})

    assert attempted
    operation_id = _latest_operation_id(case)
    _assert_operation_terminal(case.node.session_key, operation_id, "failed")
    assert case.storage.conn.in_transaction is False
    await _assert_unchanged(case, before)
    with closing(sqlite3.connect(case.database)) as reader:
        count = reader.execute("SELECT COUNT(*) FROM compacted_transcript_entries").fetchone()[0]
        assert count == 0


@pytest.mark.asyncio
async def test_cancel_during_sqlite_commit_reports_actual_durable_outcome(
    manual_compaction, monkeypatch,
):
    case = manual_compaction
    before = await case.manager.get_transcript(case.node.session_key)
    original_commit = case.storage._commit_transaction
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_commit(conn, operation, deadline, started):
        if operation == "rewrite_compacted_session":
            entered.set()
            await release.wait()
        return await original_commit(conn, operation, deadline, started)

    monkeypatch.setattr(case.storage, "_commit_transaction", delayed_commit)
    task = asyncio.create_task(case.adapter.compact({"key": case.node.session_key}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        operation_id = _latest_operation_id(case)
        task.cancel()
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
        outcome = outcomes[0]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert isinstance(outcome, (dict, asyncio.CancelledError))
    _assert_durable_message_ids(case, before[-2:])
    assert await case.manager.get_canonical_transcript(case.node.session_key) == before
    assert len(await case.storage.get_all_summaries(case.node.session_id)) == 1
    _assert_operation_terminal(case.node.session_key, operation_id, "completed")
    await _assert_idle(case)


@pytest.mark.ci_serial
@pytest.mark.asyncio
async def test_deadline_during_sqlite_commit_waits_for_actual_durable_outcome(
    manual_compaction, monkeypatch,
):
    case = manual_compaction
    case.config.compaction.total_timeout_seconds = 1.0
    before = await case.manager.get_canonical_transcript(case.node.session_key)
    original_commit = case.storage._commit_transaction
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_commit(conn, operation, deadline, started):
        if operation == "rewrite_compacted_session":
            entered.set()
            await release.wait()
        return await original_commit(conn, operation, deadline, started)

    monkeypatch.setattr(case.storage, "_commit_transaction", delayed_commit)
    task = asyncio.create_task(case.adapter.compact({"key": case.node.session_key}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        operation_id = _latest_operation_id(case)
        await asyncio.sleep(1.05)
        assert not task.done(), "The entered atomic commit must settle before reporting a result"
        assert not any(
            event["status"] in {"completed", "skipped", "failed", "cancelled"}
            for event in _operation_events(case.node.session_key, operation_id)
        )
        release.set()
        result = await asyncio.wait_for(task, timeout=5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert result["status"] == "completed" and result["applied"] is True
    assert result["durability"] == "durable"
    assert await case.manager.get_canonical_transcript(case.node.session_key) == before
    assert len(await case.storage.get_all_summaries(case.node.session_id)) == 1
    _assert_operation_terminal(case.node.session_key, operation_id, "completed")
    await _assert_idle(case)


@pytest.mark.asyncio
async def test_disconnected_subscriber_recovers_completed_operation_from_replay(
    manual_compaction, monkeypatch,
):
    from opensquilla.gateway import websocket

    case = manual_compaction
    sent = []

    async def disconnected_send(event_name, payload):
        sent.append((event_name, payload["status"]))
        raise ConnectionError("Controlled client disconnection")

    registry = websocket.ConnectionRegistry()
    registry.register(SimpleNamespace(conn_id="disconnected", send_event=disconnected_send))
    subscriptions = websocket.SubscriptionManager()
    subscriptions.subscribe_messages("disconnected", case.node.session_key)
    case.context.subscription_manager = subscriptions
    monkeypatch.setattr(websocket, "get_registry", lambda: registry)
    cursor = get_session_streams().current_seq(case.node.session_key)
    result = await case.adapter.compact({"key": case.node.session_key})

    assert result["status"] == "completed"
    assert sent[-1] == ("session.event.compaction", "completed")
    replay = get_session_streams().replay(case.node.session_key, since_stream_seq=cursor)
    assert replay.replay_complete
    assert [
        event.payload["status"] for event in replay.events
        if event.payload.get("compaction_id") == result["compaction_id"]
        and event.payload.get("status") in {"completed", "skipped", "failed"}
    ] == ["completed"]
    await _assert_idle(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_stage", ["uncommitted_archive", "committed_before_event"])
async def test_process_crash_reopens_atomic_compaction_and_idle_session(
    manual_compaction, crash_stage,
):
    case = manual_compaction
    before = await case.manager.get_transcript(case.node.session_key)
    # Run a separate Python process and hard-exit at the actual SQLite boundary.
    # Unlike cancellation this cannot execute rollback/finally in application code.
    script = dedent('''
        import asyncio, os, sys
        from types import SimpleNamespace
        from opensquilla.engine.runtime import TurnRunner
        from opensquilla.gateway.adapters.session_maintenance import (
            build_gateway_session_maintenance_adapter,
        )
        from opensquilla.gateway.config import GatewayConfig
        from opensquilla.gateway.rpc.registry import RpcContext
        from opensquilla.provider.model_catalog import ModelCatalog
        from opensquilla.provider.openai import OpenAIProvider
        from opensquilla.provider.selector import ProviderConfig
        from opensquilla.provider.types import DoneEvent, TextDeltaEvent
        from opensquilla.session.manager import SessionManager
        from opensquilla.session.storage import SessionStorage

        async def main():
            database, key, stage, workspace = sys.argv[1:]
            config = GatewayConfig(
                workspace_dir=workspace,
                llm=dict(provider="openai", model="synthetic-manual", api_key="test-only",
                         context_window_tokens=100000, max_tokens=1024, thinking="off"),
                compaction=dict(protected_recent_messages=2), prompt=dict(mode="minimal"),
            )
            selector = SimpleNamespace(current_config=ProviderConfig(
                provider="openai", model="synthetic-manual", api_key="test-only",
            ))
            async def reply(self, messages, tools=None, config=None):
                yield TextDeltaEvent(text="Earlier discussion reviewed; continue current task.")
                yield DoneEvent(stop_reason="end_turn")
            OpenAIProvider.chat = reply
            storage = SessionStorage(database)
            await storage.connect()
            manager = SessionManager(storage, inject_time_prefix=False)
            if stage == "uncommitted_archive":
                original = storage._archive_transcript_entries
                async def crash(**kwargs):
                    await original(**kwargs)
                    os._exit(72)
                storage._archive_transcript_entries = crash
            else:
                original = storage.rewrite_compacted_session
                async def crash(**kwargs):
                    assert await original(**kwargs)
                    os._exit(73)
                storage.rewrite_compacted_session = crash
            runner = TurnRunner(provider_selector=selector, session_manager=manager,
                                config=config, model_catalog=ModelCatalog())
            context = RpcContext(conn_id="crash-probe", config=config, session_manager=manager,
                                 provider_selector=selector, turn_runner=runner)
            await build_gateway_session_maintenance_adapter(context).compact({"key": key})
            raise AssertionError("Requested crash boundary was not reached")
        asyncio.run(main())
    ''')
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(case.database), case.node.session_key,
        crash_stage, case.config.workspace_dir,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, error = await asyncio.wait_for(process.communicate(), timeout=30)
    except BaseException:
        process.kill()
        await process.communicate()
        raise
    assert process.returncode == (72 if crash_stage == "uncommitted_archive" else 73), (
        error.decode(errors="replace")
    )

    reopened = SessionStorage(str(case.database))
    await reopened.connect()
    try:
        manager = SessionManager(reopened, inject_time_prefix=False)
        session = await manager.get_session(case.node.session_key)
        committed = crash_stage == "committed_before_event"
        assert session.compaction_count == int(committed)
        assert await manager.get_canonical_transcript(case.node.session_key) == before
        _assert_durable_message_ids(case, before[-2:] if committed else before)
        summaries = await manager.get_summaries(case.node.session_key)
        assert len(summaries) == int(committed)
        if committed:
            assert summaries[0].compaction_id
            assert summaries[0].coverage_status == "unknown"  # Seed has no extracted obligations.
            assert summaries[0].missing_obligations == []
            assert summaries[0].summary_payload["source_coverage"]["checked_obligations"] == 0
        assert _task_state_summary(
            await reopened.list_agent_tasks(session_key=case.node.session_key),
        )["run_status"] == "idle"
        from opensquilla.gateway.session_streams import SessionStreamRegistry
        restarted_streams = SessionStreamRegistry()
        assert restarted_streams.live_snapshot(case.node.session_key).task_id is None
        runner = TurnRunner(
            provider_selector=case.context.provider_selector, session_manager=manager,
            config=case.config, model_catalog=ModelCatalog(),
        )
        context = RpcContext(
            conn_id="restarted-offline-manual", config=case.config, session_manager=manager,
            provider_selector=case.context.provider_selector, turn_runner=runner,
        )
        retry = await build_gateway_session_maintenance_adapter(context).compact({
            "key": case.node.session_key,
        })
        assert retry["status"] == ("skipped" if committed else "completed")
        assert len(case.calls) == (0 if committed else 1)
        _assert_operation_terminal(case.node.session_key, retry["compaction_id"], retry["status"])
        assert (await manager.get_session(case.node.session_key)).compaction_count == 1
        assert await manager.get_canonical_transcript(case.node.session_key) == before
        _assert_durable_message_ids(case, before[-2:])
    finally:
        await reopened.close()
