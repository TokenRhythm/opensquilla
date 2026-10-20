"""Precommit read refusal and manual cancellation retain their exact writer outcome."""
import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.engine.types import CompactionEvent
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from tests.test_engine.test_compaction_rolling_source import (
    _agent,
    _handler,
    _input,
    _payload,
    _seed,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("read_method", [
    "get_session", "get_all_summaries", "get_context_states", "get_canonical_transcript",
])
async def test_transient_precommit_read_is_confirmed_unapplied_and_source_is_reusable(
    monkeypatch, read_method,
):
    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        node, raw, source = await _seed(manager)
        initial_node = await manager.get_session(node.session_key)
        before = await manager.get_canonical_transcript(node.session_key)
        old_summaries = await manager.get_summaries(node.session_key)
        agent = _agent()  # 32K local provider, no actual API.
        agent.bind_compaction_source(source)
        handler, _ = _handler(manager)
        inp = _input(agent, node, source)
        event = CompactionEvent(
            summary="S1", summary_payload=_payload("S1"), summary_format="structured_v1",
            kept_entries=[entry.model_dump() for entry in raw[2:]], removed_count=2,
            source_snapshot=source,
        )
        original_read = getattr(storage, read_method)
        read_count = 0

        async def fail_once(*args, **kwargs):
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                raise sqlite3.OperationalError("database is locked")
            return await original_read(*args, **kwargs)

        rewrite = AsyncMock(wraps=storage.rewrite_compacted_session)
        monkeypatch.setattr(storage, read_method, fail_once)
        monkeypatch.setattr(storage, "rewrite_compacted_session", rewrite)
        await handler.handle(event, inp)
        assert rewrite.await_count == 0
        assert event.persist_result.reason == "precommit_read_failed"
        assert not event.persist_result.applied
        persisted_node = await manager.get_session(node.session_key)
        assert persisted_node.compaction_count == initial_node.compaction_count
        assert persisted_node.updated_at == initial_node.updated_at
        assert await manager.get_summaries(node.session_key) == old_summaries
        assert await manager.get_canonical_transcript(node.session_key) == before
        refreshed = await manager.capture_compaction_source(node.session_key)
        assert refreshed == source
        # It was a single transient read failure: normal writes are available.
        await manager.append_message(node.session_key, "user", "Follow-up remains writable.")
        assert (await manager.get_transcript(node.session_key))[-1].content.endswith("writable.")
        assert inp.state.compaction_source_snapshot is None
        # A new turn captures the unchanged durable predecessor; failed S1 is
        # never inherited. Its new candidate can commit against that source.
        fresh = await manager.capture_compaction_source(node.session_key)
        next_agent = _agent()
        next_agent.bind_compaction_source(fresh)
        next_event = CompactionEvent(
            summary="S2", summary_payload=_payload("S2"), summary_format="structured_v1",
            kept_entries=[entry.model_dump() for entry in fresh.entries[2:]], removed_count=2,
            source_snapshot=fresh,
        )
        await handler.handle(next_event, _input(next_agent, node, fresh))
        assert next_event.persist_result.applied
        assert rewrite.await_count == 1
        refreshed = await manager.capture_compaction_source(node.session_key)
        assert "S2" in refreshed.previous_summary and "S1" not in refreshed.previous_summary
        canonical = await manager.get_canonical_transcript(node.session_key)
        assert canonical[:len(before)] == before
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["active", "inline"])
@pytest.mark.parametrize("failure_point", [
    "read_corrupt", "read_cancel", "commit_busy", "after_commit_busy", "unbound_busy",
])
async def test_only_bound_precommit_busy_reads_are_recoverable(monkeypatch, writer, failure_point):
    from opensquilla.session.compaction import CompactionConfig, CompactionResult

    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        node, raw, source = await _seed(manager)
        before = await manager.get_summaries(node.session_key)
        generated = False

        async def summarize(request):
            nonlocal generated
            generated = True
            return CompactionResult(
                summary="S1", summary_payload=_payload("S1"), summary_format="structured_v1",
                kept_entries=request.entries[2:], kept_start_index=2, removed_count=2,
                chunks_processed=1, replaced_previous_summary=True,
            )

        monkeypatch.setattr("opensquilla.session.manager.compact_context", summarize)
        original_read = storage.get_all_summaries
        original_write = storage.rewrite_compacted_session
        failed = False

        async def read(*args, **kwargs):
            nonlocal failed
            eligible = writer == "inline" or generated or failure_point == "unbound_busy"
            if eligible and not failed and failure_point in {
                "read_corrupt", "read_cancel", "unbound_busy",
            }:
                failed = True
                if failure_point == "read_cancel":
                    raise asyncio.CancelledError
                raise sqlite3.OperationalError(
                    "database disk image is malformed" if failure_point == "read_corrupt"
                    else "database is locked"
                )
            return await original_read(*args, **kwargs)

        async def write(**kwargs):
            if failure_point == "after_commit_busy":
                await original_write(**kwargs)
            if failure_point in {"commit_busy", "after_commit_busy"}:
                raise sqlite3.OperationalError("database is locked")
            return await original_write(**kwargs)

        monkeypatch.setattr(storage, "get_all_summaries", read)
        rewrite = AsyncMock(side_effect=write)
        monkeypatch.setattr(storage, "rewrite_compacted_session", rewrite)
        expected_error = (
            asyncio.CancelledError if failure_point == "read_cancel" else sqlite3.OperationalError
        )
        with pytest.raises(expected_error):
            if writer == "active":
                await manager.compact_with_result(
                    node.session_key, 32768, CompactionConfig(), trigger_reason="manual",
                    expected_session_id=node.session_id, expected_session_epoch=node.epoch,
                )
            else:
                await manager.persist_compaction_result(
                    node.session_key, "S1", [entry.model_dump() for entry in raw[2:]],
                    summary_payload=_payload("S1"), summary_format="structured_v1",
                    source_snapshot=None if failure_point == "unbound_busy" else source,
                    removed_count=2, expected_session_id=node.session_id,
                    expected_session_epoch=node.epoch,
                )
        committed = failure_point == "after_commit_busy"
        assert len(await manager.get_summaries(node.session_key)) == len(before) + committed
        assert rewrite.await_count == (1 if "commit_busy" in failure_point else 0)
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("manual_owner", [False, True], ids=["adapter", "complete-owner"])
@pytest.mark.parametrize("commit_applied", [True, False], ids=["applied", "stale"])
async def test_manual_commit_barrier_cancel_reaches_adapter_and_owner(
    monkeypatch, manual_owner, commit_applied,
):
    from opensquilla.application.session_maintenance import CompactSession
    from opensquilla.gateway.adapters.session_maintenance import GatewaySessionMaintenancePorts
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.gateway.rpc.registry import RpcContext
    from opensquilla.provider.selector import ProviderConfig
    from opensquilla.session.compaction import CompactionResult

    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        node, _, _ = await _seed(manager)
        before = await manager.get_summaries(node.session_key)
        candidates = []

        async def summarize(request):
            result = CompactionResult(
                summary="S1", summary_payload=_payload("S1"), summary_format="structured_v1",
                kept_entries=request.entries[2:], kept_start_index=2, removed_count=2,
                chunks_processed=1, replaced_previous_summary=True,
            )
            candidates.append(result)
            return result

        monkeypatch.setattr("opensquilla.session.manager.compact_context", summarize)
        entered = asyncio.Event()
        release = asyncio.Event()
        real_rewrite = storage.rewrite_compacted_session

        async def blocked_commit(**kwargs):
            entered.set()
            await release.wait()
            return await real_rewrite(**kwargs) if commit_applied else False

        monkeypatch.setattr(storage, "rewrite_compacted_session", blocked_commit)
        config = GatewayConfig(llm={
            "provider": "openrouter", "model": "qwen/qwen-2.5-72b-instruct",
            "api_key": "test-not-a-real-key", "context_window_tokens": 32768,
            "max_tokens": 1024,
        })
        ports = GatewaySessionMaintenancePorts(RpcContext(
            conn_id="review-cancellation", config=config, session_manager=manager,
            provider_selector=SimpleNamespace(current_config=ProviderConfig(
                provider="openrouter", model="qwen/qwen-2.5-72b-instruct",
                api_key="test-not-a-real-key",
            )),
        ))
        plan = ports.build_plan(None, 32768, "review-cancellation", time.monotonic() + 30)
        owner_ports = None
        if manual_owner:
            from opensquilla.application.session_maintenance import SessionCompactionSession
            from opensquilla.gateway.adapters.session_maintenance import (
                GatewaySessionMaintenanceAdapter,
            )
            from tests.test_application.test_session_maintenance import _application, _Ports

            owner_ports = _Ports()
            owner_ports.session = SessionCompactionSession(node.session_id, "main", node)
            owner_ports.resolve_context_window_tokens = lambda *_: 32768
            owner_ports.build_plan = lambda *_: plan
            owner_ports.compact = ports.compact
            adapter = GatewaySessionMaintenanceAdapter(_application(owner_ports))
            task = asyncio.create_task(adapter.compact({"key": node.session_key, "wait": True}))
        else:
            task = asyncio.create_task(ports._compact(CompactSession(node.session_key), plan))
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        if manual_owner:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
            terminal = owner_ports.events[-1]
            assert terminal.status == ("completed" if commit_applied else "cancelled")
            if commit_applied:
                assert terminal.reason == "cancelled_after_commit"
                assert terminal.cancellation_reconciled
                assert terminal.result.applied
            assert len([event for event in owner_ports.events if event.terminal]) == 1
        else:
            result = await task
            assert result.applied is commit_applied and result.cancellation_reconciled
        assert candidates[0].cancellation_reconciled
        assert len(candidates) == 1
        assert len(await manager.get_summaries(node.session_key)) == len(before) + commit_applied

    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_method", [
    "get_session", "get_transcript", "get_all_summaries", "get_context_states",
    "get_canonical_transcript",
])
async def test_active_writer_precommit_read_returns_known_unapplied(monkeypatch, read_method):
    from opensquilla.session.compaction import CompactionConfig, CompactionResult

    storage = SessionStorage(":memory:")
    await storage.connect()
    try:
        manager = SessionManager(storage, inject_time_prefix=False)
        node, _, source = await _seed(manager)
        initial_node = await manager.get_session(node.session_key)
        canonical_before = await manager.get_canonical_transcript(node.session_key)
        summaries_before = await manager.get_summaries(node.session_key)

        candidate_generated = False

        async def summarize(request):
            nonlocal candidate_generated
            candidate_generated = True
            return CompactionResult(
                summary="S1", summary_payload=_payload("S1"), summary_format="structured_v1",
                kept_entries=request.entries[2:], kept_start_index=2, removed_count=2,
                chunks_processed=1, replaced_previous_summary=True,
            )

        monkeypatch.setattr("opensquilla.session.manager.compact_context", summarize)
        real_read = getattr(storage, read_method)
        reads = 0

        async def fail_after_snapshot(*args, **kwargs):
            nonlocal reads
            if candidate_generated and not reads:
                reads += 1
                raise sqlite3.OperationalError("database is locked (active validation only)")
            return await real_read(*args, **kwargs)

        monkeypatch.setattr(storage, read_method, fail_after_snapshot)
        rewrite = AsyncMock(wraps=storage.rewrite_compacted_session)
        monkeypatch.setattr(storage, "rewrite_compacted_session", rewrite)
        result = await manager.compact_with_result(
            node.session_key, 32768, CompactionConfig(), trigger_reason="manual",
            expected_session_id=node.session_id, expected_session_epoch=node.epoch,
        )
        assert rewrite.await_count == 0
        assert not result.summary and result.removed_count == 0
        assert result.skip_reason == "precommit_read_failed"
        assert result.failure_kind == "storage_error"
        persisted_node = await manager.get_session(node.session_key)
        assert persisted_node.compaction_count == initial_node.compaction_count
        assert persisted_node.updated_at == initial_node.updated_at
        assert await manager.get_summaries(node.session_key) == summaries_before
        assert await manager.get_canonical_transcript(node.session_key) == canonical_before
        assert await manager.capture_compaction_source(node.session_key) == source
        await manager.append_message(node.session_key, "user", "normal write remains available")

    finally:
        await storage.close()
