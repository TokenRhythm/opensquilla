"""Rolling checkpoints use the same source, wire projection and settled writer."""
from __future__ import annotations

import asyncio
import functools
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.agent import _CompactionSummaryMessage
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.harness import _TurnRunnerCompactionPersistAdapter
from opensquilla.engine.turn_runner.stream_consumer_stage import _CompactionHandler, _StreamState
from opensquilla.engine.types import CompactionEvent, DoneEvent, ErrorEvent
from opensquilla.provider import Message, OpenAIProvider, ProviderMessageLimitProof
from opensquilla.session.compaction import CompactionResult
from opensquilla.session.compaction_state import StructuredCompactionSummary
from opensquilla.session.context_view import (
    build_compaction_context_records,
    format_compaction_summary_context,
)
from opensquilla.session.manager import CompactionPersistResult, SessionManager
from opensquilla.session.storage import SessionStorage
from tests.test_engine.test_agent_llm_budget import (
    _ContextOverflowProvider,
    _FinalProofBudgetProvider,
)


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    yield SessionManager(storage, inject_time_prefix=False)
    await storage.close()


@asynccontextmanager
async def _write_context(_key):
    yield


def _payload(text, *, replaces=True):
    return StructuredCompactionSummary(
        current_status=text, source_coverage={"replaces_prior_context": replaces},
    ).model_dump(mode="json")


def _agent():
    return Agent(provider=_FinalProofBudgetProvider(), config=AgentConfig(
        context_window_tokens=32768, max_tokens=1024,
        request_context_prompt="DYNAMIC_CONTEXT", max_provider_retries=0,
    ))


def _handler(manager):
    runner = SimpleNamespace(_session_manager=manager, _session_write_context=_write_context)
    adapter = _TurnRunnerCompactionPersistAdapter(runner)
    memory = SimpleNamespace(refresh_snapshot=lambda **_kw: None)
    prompt = SimpleNamespace(refresh_system_prompt=lambda **_kw: None)
    handler = _CompactionHandler(persist=adapter, memory_snapshot=memory, system_prompt=prompt)
    return handler, adapter


def _input(agent, node, source):
    return SimpleNamespace(
        agent=agent, agent_id="main", session_key=node.session_key,
        session_manager_present=True, expected_session_id=node.session_id,
        expected_session_epoch=node.epoch,
        compaction_source_entries=source.entries, compaction_source_preimage=source.preimage,
        compaction_source_context_fingerprint=source.context_fingerprint,
        compaction_source_boundary_message_id=source.boundary_message_id,
        compaction_source_boundary_entry_id=source.boundary_entry_id,
        state=_StreamState(compaction_source_snapshot=source),
        private_memory_allowed=False, tool_defs=[], bootstrap_context_mode=None,
    )


async def _seed(manager):
    node = await manager.create("agent:main:rolling")
    for role, content in [
        ("user", "old question"), ("assistant", "old answer"),
        ("user", "new question"), ("assistant", "new answer"),
        ("user", "CURRENT_USER"),
    ]:
        await manager.append_message(node.session_key, role, content)
    raw = await manager.get_transcript(node.session_key)
    initial = await manager.persist_compaction_result(
        node.session_key, "CHECKPOINT_ZERO", [e.model_dump() for e in raw],
        summary_payload=_payload("CHECKPOINT_ZERO", replaces=False), summary_format="structured_v1",
    )
    assert initial.applied
    raw = await manager.get_transcript(node.session_key)
    source = await manager.capture_compaction_source(
        node.session_key, boundary_message_id=raw[-1].message_id,
    )
    return node, raw, source


@pytest.mark.asyncio
async def test_two_rolling_commits_replace_context_and_refresh_exact_source(manager, monkeypatch):
    node, raw, source = await _seed(manager)
    agent = _agent()
    agent.bind_compaction_source(source)
    agent.config.compaction_summary_context = format_compaction_summary_context(
        [source.previous_summary],
    )
    frozen_context = agent._current_request_context_message()
    runtime_context = Message(role="user", content="RUNTIME_CONTEXT")
    handler, _adapter = _handler(manager)
    inp = _input(agent, node, source)
    calls = []

    async def summarize(request):
        calls.append(request)
        text = f"CHECKPOINT_{len(calls)}"
        return CompactionResult(
            summary=text, summary_payload=_payload(text), summary_format="structured_v1",
            kept_entries=request.entries[2:], kept_start_index=2, removed_count=2,
            chunks_processed=1, replaced_previous_summary=True,
        )

    monkeypatch.setattr(agent, "_execute_compaction_request", summarize)
    messages = [Message(role=e.role, content=e.content) for e in raw]
    index = 4
    events = []
    for generation in (1, 2):
        outcome = await agent._check_context_overflow(
            messages, 40000, protected_turn_start_index=index,
            request_context_insert_index=index, runtime_context_insert_index=index,
            request_context_message=frozen_context, runtime_context_message=runtime_context,
        )
        assert outcome is not None and outcome.compacted and not outcome.ephemeral_only
        assert outcome.removed_count == 2  # internal summary + ack never become source rows
        agent._stage_pending_durable_compaction(outcome)
        event = agent._pending_durable_compaction_event
        assert event is not None
        await handler.handle(event, inp)
        assert event.persist_result.applied
        events.append(event)
        messages = outcome.messages
        index = outcome.protected_turn_start_index
        wire = agent._provider_request_messages_for_count_projection(
            messages, request_context_message=frozen_context,
            request_context_insert_index=index, runtime_context_message=runtime_context,
            runtime_context_insert_index=index,
        )
        visible = "\n".join(str(m.content) for m in wire)
        assert f"CHECKPOINT_{generation}" in visible
        assert "CHECKPOINT_ZERO" not in visible
        if generation == 2:
            assert "CHECKPOINT_1" not in visible
        assert "DYNAMIC_CONTEXT" in visible and "CURRENT_USER" in visible
        assert len(inp.state.compaction_source_snapshot.entries) == 5 - generation * 2

    assert "CHECKPOINT_ZERO" in calls[0].previous_summary
    assert "CHECKPOINT_1" in calls[1].previous_summary
    assert all("CHECKPOINT_" not in str(request.entries) for request in calls)
    records = build_compaction_context_records(
        context_states=await manager.get_context_states(node.session_key),
        summaries=await manager.get_summaries(node.session_key),
    )
    assert len(records) == 1 and "CHECKPOINT_2" in records[0].text
    assert [e.model_dump() for e in await manager.get_canonical_transcript(node.session_key)] == [
        e.model_dump() for e in raw
    ]
    # A delayed event keeps its original generation; it cannot borrow S2's source.
    await handler.handle(events[0], inp)
    assert events[0].persist_result.reason == "stale_candidate_source"
    assert len(await manager.get_summaries(node.session_key)) == 3


@pytest.mark.asyncio
async def test_refresh_excludes_queued_user_and_failure_disables_next_writer(manager, monkeypatch):
    node, raw, source = await _seed(manager)
    agent = _agent()
    agent.bind_compaction_source(source)
    handler, adapter = _handler(manager)
    inp = _input(agent, node, source)
    await manager.append_message(node.session_key, "user", "FUTURE_QUEUED_USER")
    event = CompactionEvent(
        summary="S1", summary_payload=_payload("S1"), summary_format="structured_v1",
        kept_entries=[], removed_count=2, source_snapshot=source,
    )
    await handler.handle(event, inp)
    assert event.persist_result.applied
    assert [e.message_id for e in inp.state.compaction_source_snapshot.entries] == [
        e.message_id for e in raw[2:]
    ]
    refreshed_source = inp.state.compaction_source_snapshot
    monkeypatch.setattr(
        adapter, "refresh_source", AsyncMock(side_effect=RuntimeError("read failed")),
    )
    second = replace(event, summary="S2", summary_payload=_payload("S2"),
                     source_snapshot=refreshed_source)
    await handler.handle(second, inp)
    assert second.persist_result.applied
    assert inp.state.compaction_source_snapshot is None
    assert agent._durable_compaction_source_bound and agent._durable_compaction_source is None
    # Failure to refresh does not undo the successful transaction or consume queued input.
    remaining = await manager.get_transcript(node.session_key)
    assert [e.content for e in remaining] == ["CURRENT_USER", "FUTURE_QUEUED_USER"]


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_result", [
    False, None, CompactionPersistResult(False, "stale"), RuntimeError("writer lost"),
])
async def test_unconfirmed_writer_does_not_refresh_or_allow_s2(manager, monkeypatch, writer_result):
    node, _raw, source = await _seed(manager)
    agent = _agent()
    agent.bind_compaction_source(source)
    handler, adapter = _handler(manager)
    async def persist(*_args, expected_session_id=None, expected_session_epoch=None, **_kwargs):
        if isinstance(writer_result, Exception):
            raise writer_result
        return writer_result

    monkeypatch.setattr(manager, "persist_compaction_result", persist)
    refresh = AsyncMock()
    monkeypatch.setattr(adapter, "refresh_source", refresh)
    inp = _input(agent, node, source)
    event = CompactionEvent(summary="S1", removed_count=2, source_snapshot=source)
    if writer_result is None:
        with pytest.raises(RuntimeError, match="did not confirm"):
            await handler.handle(event, inp)
    elif isinstance(writer_result, Exception):
        with pytest.raises(RuntimeError, match="writer lost"):
            await handler.handle(event, inp)
    else:
        await handler.handle(event, inp)
    assert not event.persist_result.applied
    assert inp.state.compaction_source_snapshot is None
    assert agent._durable_compaction_source is None
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_empty_raw_checkpoint_replacement_requires_a_valid_capture(manager):
    node = await manager.create("agent:main:empty")
    assert (await manager.persist_compaction_result(
        node.session_key, "S0", [], summary_payload=_payload("S0", replaces=False),
        summary_format="structured_v1",
    )).applied
    source = await manager.capture_compaction_source(node.session_key)
    assert source.valid and not source.entries and source.previous_summary
    result = await manager.persist_compaction_result(
        node.session_key, "S1", [], summary_payload=_payload("S1"),
        summary_format="structured_v1", source_snapshot=source, removed_count=0,
    )
    assert result.applied
    invalid = await manager.capture_compaction_source(
        node.session_key, boundary_message_id="missing",
    )
    result = await manager.persist_compaction_result(
        node.session_key, "S2", [], summary_payload=_payload("S2"),
        summary_format="structured_v1", source_snapshot=invalid, removed_count=0,
    )
    assert not result.applied and result.reason == "invalid_source_boundary"


@pytest.mark.asyncio
@pytest.mark.parametrize("coverage", [None, []])
async def test_empty_raw_malformed_coverage_is_a_known_rejection(manager, coverage):
    node = await manager.create("agent:main:malformed")
    assert (await manager.persist_compaction_result(node.session_key, "S0", [])).applied
    source = await manager.capture_compaction_source(node.session_key)
    payload = _payload("S1")
    payload["source_coverage"] = coverage
    result = await manager.persist_compaction_result(
        node.session_key, "S1", [], summary_payload=payload,
        summary_format="structured_v1", source_snapshot=source, removed_count=0,
    )
    assert not result.applied and result.reason == "invalid_source_boundary"


@pytest.mark.asyncio
async def test_cancelled_writer_reports_commit_then_stops_before_more_work(manager, monkeypatch):
    node, _raw, source = await _seed(manager)
    agent = _agent()
    agent.bind_compaction_source(source)
    handler, _adapter = _handler(manager)
    inp = _input(agent, node, source)
    entered, release = asyncio.Event(), asyncio.Event()
    original = manager._storage.rewrite_compacted_session

    async def delayed_commit(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    monkeypatch.setattr(manager._storage, "rewrite_compacted_session", delayed_commit)
    event = CompactionEvent(
        summary="S1", summary_payload=_payload("S1"), summary_format="structured_v1",
        removed_count=2, source_snapshot=source,
    )
    task = asyncio.create_task(handler.handle(event, inp))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert event.persist_result.applied and event.persist_result.cancellation_reconciled
    assert len(await manager.get_transcript(node.session_key)) == 3
    assert "S1" in inp.state.compaction_source_snapshot.previous_summary


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_chars,expected_calls", [(100, 2), (40000, 1)])
async def test_stable_commit_event_can_precede_routed_resend(
    monkeypatch, summary_chars, expected_calls,
):
    async def summarize(request):
        cut = len(request.entries) - request.config.protected_recent_messages
        return CompactionResult(
            summary="s" * summary_chars, kept_entries=request.entries[cut:],
            kept_start_index=cut, removed_count=cut, chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", summarize)
    routed, stable = _FinalProofBudgetProvider(), _FinalProofBudgetProvider()
    agent = Agent(provider=routed, config=AgentConfig(
        context_window_tokens=32768, max_tokens=1024, provider_request_proof_max_chars=32000,
        max_provider_retries=0, max_overflow_retries=1,
    ))
    agent.bind_durable_consumer(
        provider=stable, model_id="stable-128k", context_window_tokens=131072,
        max_output_tokens=1024, provider_request_proof_max_chars=128000,
    )
    agent.set_history([Message(role="user", content="q" * 100000),
                       Message(role="assistant", content="a" * 100000)])
    at_commit = []
    events = []
    async for event in agent.run_turn("CURRENT_USER"):
        events.append(event)
        if isinstance(event, CompactionEvent):
            at_commit.append(len(routed.calls))
    assert at_commit == [1]
    assert len(routed.calls) == expected_calls
    assert any(isinstance(e, DoneEvent if expected_calls == 2 else ErrorEvent) for e in events)


@pytest.mark.asyncio
async def test_ordinary_pending_event_follows_first_provider_progress(monkeypatch):
    async def summarize(request):
        cut = len(request.entries) - request.config.protected_recent_messages
        return CompactionResult(
            summary="S1_COMPACTED", kept_entries=request.entries[cut:],
            kept_start_index=cut, removed_count=cut, chunks_processed=1,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", summarize)
    provider = _ContextOverflowProvider(success_after=1)
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=32768, max_tokens=1024, max_provider_retries=0,
    ))
    agent.set_history([
        Message(role="user", content="question " * 40000),
        Message(role="assistant", content="answer " * 40000),
    ])
    at_commit = []
    async for event in agent.run_turn("CURRENT_USER"):
        if isinstance(event, CompactionEvent):
            at_commit.append(len(provider.calls))
            assert "S1_COMPACTED" in str(provider.calls[-1])
    assert at_commit == [2]


def test_internal_summary_identity_does_not_remove_user_authored_summary_text():
    agent = _agent()
    agent.config.compaction_summary_context = "S0"
    context = agent._current_request_context_message()
    user = Message(role="user", content="[Context summary]\nuser-authored text")
    wire = agent._with_request_context_messages([user], context, 0, Message(
        role="user", content="clock",
    ), 0)
    assert "S0" in str(wire)
    native = _CompactionSummaryMessage.from_summary("S1", replaces_checkpoint=True)
    wire = agent._with_request_context_messages([native, user], context, 1, Message(
        role="user", content="clock",
    ), 1)
    assert "S0" not in str(wire) and "S1" in str(wire) and "user-authored text" in str(wire)


@pytest.mark.asyncio
@pytest.mark.parametrize("inherited", [False, True])
async def test_message_count_projection_only_replaces_checkpoint_it_inherited(
    monkeypatch, inherited,
):
    # Generic adapter projection only; no network or actual OpenAI model call.
    agent = Agent(provider=OpenAIProvider(api_key="synthetic", model="synthetic"),
                  config=_agent().config)
    agent.config.compaction_summary_context = "STORED_S0"
    context = agent._current_request_context_message()
    messages = [Message(role="user" if i % 2 == 0 else "assistant", content=f"raw {i}")
                for i in range(40)]
    if inherited:
        messages[:0] = [
            _CompactionSummaryMessage.from_summary("NEW_S1", replaces_checkpoint=True),
            _CompactionSummaryMessage(role="assistant", content="ack"),
        ]
    messages.append(Message(role="user", content="CURRENT"))
    boundary = len(messages) - 1
    runtime = Message(role="user", content="clock")
    config = agent._provider_admission_chat_config(
        "CURRENT", context_window_tokens=32768, max_output_tokens=1024,
    )
    projected = agent._project_provider_request_message_count(
        messages, config=config, request_context_message=context,
        request_context_insert_index=boundary, runtime_context_message=runtime,
        runtime_context_insert_index=boundary,
    )
    proof = ProviderMessageLimitProof(
        actual_wire_messages=projected.actual_wire_messages, limit=22,
        logical_messages=projected.logical_messages, system_messages=projected.system_messages,
        tool_result_messages=projected.tool_result_messages,
        provider_kind=projected.provider_kind, model=projected.model, base_host=projected.base_host,
    )

    async def summarize(request):
        cut = request.forced_prefix_cut
        text = "COUNT_SUMMARY_NEW_S1" if "NEW_S1" in str(request.entries[:cut]) else "COUNT_PARTIAL"
        return CompactionResult(summary=text, kept_entries=request.entries[cut:],
                                kept_start_index=cut, removed_count=cut, chunks_processed=1)

    monkeypatch.setattr(agent, "_execute_compaction_request", summarize)
    outcome, _reason = await agent._recover_provider_message_count_limit(
        messages, request_suffix_messages=[], proof=proof, config=config,
        request_context_message=context, request_context_insert_index=boundary,
        runtime_context_message=runtime, runtime_context_insert_index=boundary,
        protected_turn_start_index=boundary,
    )
    assert outcome is not None
    wire = agent._provider_request_messages_for_count_projection(
        outcome.messages, request_context_message=context,
        request_context_insert_index=outcome.request_context_insert_index,
        runtime_context_message=runtime,
        runtime_context_insert_index=outcome.runtime_context_insert_index,
    )
    assert ("STORED_S0" in str(wire)) is (not inherited)
    assert "DYNAMIC_CONTEXT" in str(wire) and "CURRENT" in str(wire)


@pytest.mark.asyncio
async def test_unaligned_source_partial_summary_keeps_stored_checkpoint(monkeypatch):
    agent = _agent()
    agent.config.compaction_summary_context = "STORED_S0"
    agent.bind_compaction_source(None)
    context = agent._current_request_context_message()
    messages = [Message(role="user", content="old"), Message(role="assistant", content="answer"),
                Message(role="user", content="CURRENT")]

    async def summarize(request):
        assert request.previous_summary is None
        return CompactionResult(summary="PARTIAL", kept_entries=request.entries[2:],
                                kept_start_index=2, removed_count=2, chunks_processed=1)

    monkeypatch.setattr(agent, "_execute_compaction_request", summarize)
    runtime = Message(role="user", content="clock")
    outcome = await agent._check_context_overflow(
        messages, 40000, protected_turn_start_index=2, request_context_insert_index=2,
        runtime_context_insert_index=2, request_context_message=context,
        runtime_context_message=runtime,
    )
    assert outcome is not None and outcome.ephemeral_only
    wire = agent._provider_request_messages_for_count_projection(
        outcome.messages, request_context_message=context,
        request_context_insert_index=outcome.request_context_insert_index,
        runtime_context_message=runtime,
        runtime_context_insert_index=outcome.runtime_context_insert_index,
    )
    assert "STORED_S0" in str(wire) and "PARTIAL" in str(wire)


@pytest.mark.asyncio
async def test_refresh_rejects_same_id_with_changed_raw_content(manager):
    node, raw, source = await _seed(manager)
    _handler_instance, adapter = _handler(manager)
    await manager._storage.conn.execute(
        "UPDATE transcript_entries SET content = ? WHERE session_id = ? AND message_id = ?",
        ("CONCURRENT_CHANGE", node.session_id, raw[-1].message_id),
    )
    await manager._storage.conn.commit()
    with pytest.raises(ValueError, match="source changed"):
        await adapter.refresh_source(
            session_key=node.session_key, source_snapshot=source, removed_count=0,
            expected_session_id=node.session_id, expected_session_epoch=node.epoch,
        )


@pytest.mark.asyncio
async def test_source_binding_keeps_admitted_request_only_checkpoint_projection(manager):
    node = await manager.create("agent:main:source-projection")
    summary = "S0 detail; " * 20000
    assert (await manager.persist_compaction_result(
        node.session_key, summary, [], summary_payload=_payload(summary, replaces=False),
        summary_format="structured_v1",
    )).applied
    for role, text in [("user", "current goal"), ("assistant", "understood")]:
        await manager.append_message(node.session_key, role, text)
    entries = await manager.get_transcript(node.session_key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    window = await runner._prepare_request_window(
        node.session_key, entries, 32768, compaction_id="projection", phase="preflight",
        reason="summary_failed", consumer_admission=lambda text, kept: "S0 detail" not in text,
    )
    assert window is not None and len(window.kept_entries) == len(entries)
    agent = _agent()
    context = await runner._load_history(
        agent, node.session_key, trim_last_user=False, prepared_window=window,
    )
    assert context and "Temporary history window" in context and "S0 detail" not in context
    agent.config.compaction_summary_context = context
    source = await manager.capture_compaction_source(node.session_key)
    assert all(entry.content == message.content for entry, message in zip(
        source.entries, agent.history_snapshot(), strict=True,
    ))
    agent.bind_compaction_source(source)
    assert agent.config.compaction_summary_context == context
    assert summary.strip() in agent._durable_compaction_source.previous_summary


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["ordinary", "stable", "stable_no_resend"])
@pytest.mark.parametrize("writer_mode", ["applied", "unknown", "cancelled", "timeout", "read_busy"])
async def test_real_writer_result_controls_both_provider_commit_timings(
    manager, monkeypatch, timing, writer_mode,
):
    node = await manager.create(f"agent:main:writer-{timing}-{writer_mode}")
    assert (await manager.persist_compaction_result(
        node.session_key, "STORED_S0", [], summary_payload=_payload("STORED_S0", replaces=False),
        summary_format="structured_v1",
    )).applied
    for role, text in [("user", "q" * 100000), ("assistant", "a" * 100000),
                       ("user", "CURRENT_USER")]:
        await manager.append_message(node.session_key, role, text)
    source = await manager.capture_compaction_source(node.session_key)
    raw = await manager.get_canonical_transcript(node.session_key)
    provider = (_ContextOverflowProvider(success_after=1) if timing == "ordinary"
                else _FinalProofBudgetProvider())
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=32768, max_tokens=1024, max_provider_retries=0,
        max_overflow_retries=1,
        provider_request_proof_max_chars=32000 if timing != "ordinary" else 0,
    ))
    if timing != "ordinary":
        agent.bind_durable_consumer(
            provider=_FinalProofBudgetProvider(), model_id="stable-128k",
            context_window_tokens=131072, max_output_tokens=1024,
            provider_request_proof_max_chars=128000,
        )
    agent.set_history([Message(role=e.role, content=e.content) for e in source.entries[:-1]])
    agent.config.compaction_summary_context = format_compaction_summary_context(
        [source.previous_summary],
    )
    agent.bind_compaction_source(source)
    summary = "S1 " + "s" * (40000 if timing == "stable_no_resend" else 100)

    async def summarize(request):
        assert "STORED_S0" in request.previous_summary
        cut = len(request.entries) - request.config.protected_recent_messages
        return CompactionResult(
            summary=summary, summary_payload=_payload(summary), summary_format="structured_v1",
            kept_entries=request.entries[cut:], kept_start_index=cut, removed_count=cut,
            chunks_processed=1, replaced_previous_summary=True,
        )

    monkeypatch.setattr("opensquilla.engine.agent.compact_context", summarize)
    handler, _adapter = _handler(manager)
    inp = _input(agent, node, source)
    at_commit, events = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    if writer_mode == "unknown":
        original = manager.persist_compaction_result

        @functools.wraps(original)
        async def commit_without_result(*args, **kwargs):
            await original(*args, **kwargs)
            return None

        monkeypatch.setattr(manager, "persist_compaction_result", commit_without_result)
    elif writer_mode == "cancelled":
        original_rewrite = manager._storage.rewrite_compacted_session

        async def commit_after_cancellation(**kwargs):
            entered.set()
            await release.wait()
            return await original_rewrite(**kwargs)

        monkeypatch.setattr(
            manager._storage, "rewrite_compacted_session", commit_after_cancellation,
        )
    elif writer_mode == "read_busy":
        import sqlite3

        original_read = manager._storage.get_all_summaries
        read_failed = False

        async def fail_one_precommit_read(*args, **kwargs):
            nonlocal read_failed
            if not read_failed:
                read_failed = True
                raise sqlite3.OperationalError("database is locked")
            return await original_read(*args, **kwargs)

        monkeypatch.setattr(manager._storage, "get_all_summaries", fail_one_precommit_read)

    async def consume():
        async for event in agent.run_turn("CURRENT_USER"):
            events.append(event)
            if isinstance(event, CompactionEvent):
                at_commit.append(len(provider.calls))
                if writer_mode == "timeout":
                    event.compaction_deadline_at_monotonic = asyncio.get_running_loop().time() - 1
                await handler.handle(event, inp)

    if writer_mode == "unknown":
        with pytest.raises(RuntimeError, match="did not confirm"):
            await consume()
    elif writer_mode == "cancelled":
        task = asyncio.create_task(consume())
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await consume()

    assert at_commit == [2 if timing == "ordinary" else 1]
    event = next(event for event in events if isinstance(event, CompactionEvent))
    if writer_mode == "unknown":
        assert event.persist_result.reason == "commit_unconfirmed"
    elif writer_mode in {"timeout", "read_busy"}:
        assert not event.persist_result.applied
        assert event.persist_result.reason == (
            "compaction_deadline_exceeded" if writer_mode == "timeout" else "precommit_read_failed"
        )
    else:
        assert event.persist_result.applied
        assert event.persist_result.cancellation_reconciled is (writer_mode == "cancelled")
    assert len(await manager.get_summaries(node.session_key)) == (
        1 if writer_mode in {"timeout", "read_busy"} else 2
    )
    assert len(await manager.get_transcript(node.session_key)) == (
        3 if writer_mode in {"timeout", "read_busy"} else 1
    )
    assert [entry.model_dump() for entry in await manager.get_canonical_transcript(
        node.session_key,
    )] == [entry.model_dump() for entry in raw]
    expected_calls = 2 if timing == "ordinary" else (
        2 if timing == "stable" and writer_mode in {"applied", "timeout", "read_busy"} else 1
    )
    assert len(provider.calls) == expected_calls
    if writer_mode in {"applied", "timeout", "read_busy"}:
        assert any(isinstance(event, ErrorEvent if timing == "stable_no_resend" else DoneEvent)
                   for event in events)
    else:
        assert not any(isinstance(event, DoneEvent) for event in events)
