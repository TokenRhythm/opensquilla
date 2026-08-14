import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.memory.checkpoint import checkpoint_coverage_hash, checkpoint_turn_id
from opensquilla.memory.session_flush import FlushReceipt, SessionFlushService
from opensquilla.provider import Message
from opensquilla.session.compaction import CompactionConfig, compaction_remaining_seconds
from opensquilla.session.compaction_lifecycle import CompactionTimeoutError
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import MemoryDurableReceipt, SessionNode
from opensquilla.session.storage import SessionStorage
from opensquilla.tool_boundary import ToolCall, ToolResult


async def test_memory_durable_receipt_upsert_is_idempotent(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        receipt = MemoryDurableReceipt(
            receipt_id="r1",
            session_key="agent:main:webchat:abc",
            session_id="session-1",
            turn_id="turn-1",
            scope="checkpoint",
            source_path="memory/.checkpoints/agent-main-webchat-abc/turn-1.jsonl",
            target_path=None,
            content_hash="h1",
            idempotency_key="checkpoint:agent:main:webchat:abc:turn-1:h1",
            status="checkpoint_saved",
            reason=None,
            attempt_count=0,
            next_retry_at_ms=None,
        )

        await storage.upsert_memory_durable_receipt(receipt)
        await storage.upsert_memory_durable_receipt(receipt)

        rows = await storage.list_memory_durable_receipts(
            session_key="agent:main:webchat:abc"
        )
        assert len(rows) == 1
        assert rows[0].status == "checkpoint_saved"
    finally:
        await storage.close()


async def test_memory_durable_receipt_generation_fence_rejects_deleted_session(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        session_key = "agent:main:webchat:deleted"
        session_id = "session-old"
        await storage.upsert_session(
            SessionNode(session_key=session_key, session_id=session_id)
        )
        await storage.delete_session(session_key)

        with pytest.raises(KeyError, match="Session generation changed"):
            await storage.upsert_memory_durable_receipt(
                MemoryDurableReceipt(
                    receipt_id="stale-deleted",
                    session_key=session_key,
                    session_id=session_id,
                    scope="checkpoint",
                    idempotency_key="checkpoint:stale-deleted",
                    status="checkpoint_saved",
                ),
                expected_session_id=session_id,
            )

        assert await storage.list_memory_durable_receipts(session_key=session_key) == []
    finally:
        await storage.close()


async def test_memory_durable_receipt_generation_fence_preserves_reused_key(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        session_key = "agent:main:webchat:reused"
        old_session_id = "session-old"
        new_session_id = "session-new"
        idempotency_key = "checkpoint:shared-idempotency-key"
        await storage.upsert_session(
            SessionNode(session_key=session_key, session_id=old_session_id)
        )
        await storage.delete_session(session_key)
        await storage.upsert_session(
            SessionNode(session_key=session_key, session_id=new_session_id)
        )
        current = await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                receipt_id="current",
                session_key=session_key,
                session_id=new_session_id,
                scope="checkpoint",
                idempotency_key=idempotency_key,
                status="checkpoint_saved",
            )
        )

        with pytest.raises(KeyError, match="Session generation changed"):
            await storage.upsert_memory_durable_receipt(
                MemoryDurableReceipt(
                    receipt_id="stale",
                    session_key=session_key,
                    session_id=old_session_id,
                    scope="checkpoint",
                    idempotency_key=idempotency_key,
                    status="checkpoint_failed",
                    reason="stale callback",
                ),
                expected_session_id=old_session_id,
            )

        rows = await storage.list_memory_durable_receipts(session_key=session_key)
        assert [row.receipt_id for row in rows] == [current.receipt_id]
        assert rows[0].session_id == new_session_id
        assert rows[0].status == "checkpoint_saved"
        assert rows[0].reason is None
    finally:
        await storage.close()


async def test_memory_durable_receipt_generation_fence_rejects_mismatched_payload(
    tmp_path,
):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        session_key = "agent:main:webchat:payload-mismatch"
        current_session_id = "session-current"
        await storage.upsert_session(
            SessionNode(session_key=session_key, session_id=current_session_id)
        )

        with pytest.raises(KeyError, match="Session generation changed"):
            await storage.upsert_memory_durable_receipt(
                MemoryDurableReceipt(
                    receipt_id="stale-payload",
                    session_key=session_key,
                    session_id="session-stale",
                    scope="checkpoint",
                    idempotency_key="checkpoint:stale-payload",
                    status="checkpoint_saved",
                ),
                expected_session_id=current_session_id,
            )

        assert await storage.list_memory_durable_receipts(session_key=session_key) == []
    finally:
        await storage.close()


async def test_memory_durable_receipt_filters_and_update(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        receipt = MemoryDurableReceipt(
            receipt_id="r2",
            session_key="agent:main:webchat:abc",
            session_id="session-1",
            turn_id="turn-2",
            scope="checkpoint",
            source_path="memory/.checkpoints/agent-main-webchat-abc/turn-2.jsonl",
            target_path=None,
            content_hash="h2",
            idempotency_key="checkpoint:agent:main:webchat:abc:turn-2:h2",
            status="checkpoint_failed",
            reason="write failed",
            attempt_count=1,
            next_retry_at_ms=None,
        )

        saved = await storage.upsert_memory_durable_receipt(receipt)
        updated = await storage.update_memory_durable_receipt(
            saved.receipt_id,
            status="checkpoint_saved",
            reason=None,
            attempt_count=2,
            next_retry_at_ms=123,
        )

        assert updated.status == "checkpoint_saved"
        assert updated.reason is None
        assert updated.attempt_count == 2
        assert updated.next_retry_at_ms == 123

        by_status = await storage.list_memory_durable_receipts(
            session_key="agent:main:webchat:abc",
            status="checkpoint_saved",
        )
        by_idempotency = await storage.list_memory_durable_receipts(
            idempotency_key="checkpoint:agent:main:webchat:abc:turn-2:h2",
        )

        assert [row.receipt_id for row in by_status] == ["r2"]
        assert [row.receipt_id for row in by_idempotency] == ["r2"]

        with pytest.raises(ValueError):
            await storage.update_memory_durable_receipt(saved.receipt_id, unknown=True)
        with pytest.raises(KeyError):
            await storage.update_memory_durable_receipt("missing", status="failed")
    finally:
        await storage.close()


async def test_memory_durable_receipt_update_canonicalizes_session_key(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        saved = await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                receipt_id="r3",
                session_key="agent:main:webchat:abc",
                session_id="session-1",
                turn_id="turn-3",
                scope="checkpoint",
                source_path="memory/.checkpoints/agent-main-webchat-abc/turn-3.jsonl",
                target_path=None,
                content_hash="h3",
                idempotency_key="checkpoint:agent:main:webchat:abc:turn-3:h3",
                status="checkpoint_saved",
                reason=None,
                attempt_count=0,
                next_retry_at_ms=None,
            )
        )

        updated = await storage.update_memory_durable_receipt(
            saved.receipt_id,
            session_key="webchat:default",
        )
        rows = await storage.list_memory_durable_receipts(
            session_key="webchat:default"
        )

        assert updated.session_key == "agent:main:webchat:default"
        assert [row.receipt_id for row in rows] == ["r3"]
    finally:
        await storage.close()


async def test_memory_durable_receipt_conflict_updates_mutable_fields(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        first = await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                receipt_id="r4-original",
                session_key="agent:main:webchat:abc",
                session_id="session-1",
                turn_id="turn-4",
                scope="checkpoint",
                source_path="memory/.checkpoints/agent-main-webchat-abc/turn-4.jsonl",
                target_path=None,
                content_hash="h4",
                idempotency_key="checkpoint:agent:main:webchat:abc:turn-4:h4",
                status="checkpoint_failed",
                reason="initial failure",
                attempt_count=1,
                next_retry_at_ms=100,
                created_at=1000,
                updated_at=1000,
            )
        )

        second = await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                receipt_id="r4-conflict",
                session_key="agent:main:webchat:abc",
                session_id="session-1",
                turn_id="turn-4",
                scope="checkpoint",
                source_path="memory/.checkpoints/agent-main-webchat-abc/turn-4.jsonl",
                target_path=None,
                content_hash="h4",
                idempotency_key="checkpoint:agent:main:webchat:abc:turn-4:h4",
                status="checkpoint_saved",
                reason=None,
                attempt_count=2,
                next_retry_at_ms=200,
            )
        )
        rows = await storage.list_memory_durable_receipts(
            idempotency_key="checkpoint:agent:main:webchat:abc:turn-4:h4"
        )

        assert len(rows) == 1
        assert second.receipt_id == "r4-original"
        assert rows[0].receipt_id == "r4-original"
        assert rows[0].created_at == first.created_at
        assert rows[0].status == "checkpoint_saved"
        assert rows[0].reason is None
        assert rows[0].attempt_count == 2
        assert rows[0].next_retry_at_ms == 200
        assert rows[0].updated_at >= first.updated_at
    finally:
        await storage.close()


async def test_record_memory_checkpoint_preserves_operation_and_coverage_ids(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    manager = SessionManager(storage, checkpoint_workspace_dir=tmp_path / "workspace")
    try:
        key = "agent:main:webchat:abc"
        session = await manager.create(key)
        await manager.append_message(key, role="user", content="checkpoint body")
        entries = await storage.get_transcript(session.session_id)

        receipt = await manager.record_memory_checkpoint(key, turn_id="cmp_123")

        assert receipt.turn_id == "cmp_123"
        assert receipt.coverage_turn_id == checkpoint_turn_id(entries)
        assert receipt.coverage_hash == checkpoint_coverage_hash(entries)
        assert receipt.coverage_entry_count == len(entries)

        rows = await storage.list_memory_durable_receipts(
            session_key=key,
            session_id=session.session_id,
            scope="checkpoint",
            status="checkpoint_saved",
            coverage_turn_id=checkpoint_turn_id(entries),
            coverage_hash=checkpoint_coverage_hash(entries),
            coverage_entry_count=len(entries),
            limit=1,
        )
        assert [row.receipt_id for row in rows] == [receipt.receipt_id]
    finally:
        await storage.close()


async def test_record_memory_checkpoint_does_not_restore_deleted_session_receipt(
    tmp_path,
    monkeypatch,
):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    manager = SessionManager(storage, checkpoint_workspace_dir=tmp_path / "workspace")
    try:
        session_key = "agent:main:webchat:checkpoint-race"
        session = await manager.create(session_key)
        await manager.append_message(session_key, role="user", content="checkpoint body")
        original_upsert = storage.upsert_memory_durable_receipt

        async def delete_before_receipt_upsert(receipt, **kwargs):
            await storage.delete_session(session_key)
            return await original_upsert(receipt, **kwargs)

        monkeypatch.setattr(
            storage,
            "upsert_memory_durable_receipt",
            delete_before_receipt_upsert,
        )

        with pytest.raises(KeyError, match="Session generation changed"):
            await manager.record_memory_checkpoint(session_key, turn_id="turn-race")

        assert await storage.get_session(session_key) is None
        assert (
            await storage.list_memory_durable_receipts(
                session_key=session_key,
                session_id=session.session_id,
            )
            == []
        )
    finally:
        await storage.close()


async def test_memory_durable_receipt_coverage_lookup_is_targeted(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        for idx in range(105):
            await storage.upsert_memory_durable_receipt(
                MemoryDurableReceipt(
                    receipt_id=f"old-{idx}",
                    session_key="agent:main:webchat:abc",
                    session_id="session-1",
                    turn_id=f"cmp-old-{idx}",
                    scope="checkpoint",
                    source_path=f"memory/.checkpoints/old-{idx}.jsonl",
                    content_hash=f"h-old-{idx}",
                    coverage_turn_id=f"through-{idx}",
                    coverage_hash=f"coverage-old-{idx}",
                    coverage_entry_count=1,
                    idempotency_key=f"checkpoint:old:{idx}",
                    status="checkpoint_saved",
                )
            )
        target = await storage.upsert_memory_durable_receipt(
            MemoryDurableReceipt(
                receipt_id="target",
                session_key="agent:main:webchat:abc",
                session_id="session-1",
                turn_id="cmp-target",
                scope="checkpoint",
                source_path="memory/.checkpoints/target.jsonl",
                content_hash="h-target",
                coverage_turn_id="through-999",
                coverage_hash="coverage-target",
                coverage_entry_count=2,
                idempotency_key="checkpoint:target",
                status="checkpoint_saved",
            )
        )

        rows = await storage.list_memory_durable_receipts(
            session_key="agent:main:webchat:abc",
            session_id="session-1",
            scope="checkpoint",
            status="checkpoint_saved",
            coverage_turn_id="through-999",
            coverage_hash="coverage-target",
            coverage_entry_count=2,
            limit=1,
        )

        assert [row.receipt_id for row in rows] == [target.receipt_id]
    finally:
        await storage.close()


async def test_record_memory_checkpoint_preserves_distinct_failure_receipts(
    tmp_path, monkeypatch
):
    import opensquilla.memory.checkpoint as checkpoint

    storage = await SessionStorage.open(tmp_path / "sessions.db")
    manager = SessionManager(storage, checkpoint_workspace_dir=tmp_path / "workspace")
    try:
        key = "agent:main:webchat:abc"
        await manager.create(key)
        await manager.append_message(key, role="user", content="same checkpoint body")
        errors = iter([RuntimeError("disk full"), RuntimeError("permission denied")])

        def _fail_append(*args, **kwargs):
            raise next(errors)

        monkeypatch.setattr(checkpoint, "append_checkpoint_events", _fail_append)

        with pytest.raises(RuntimeError, match="disk full"):
            await manager.record_memory_checkpoint(key, turn_id="turn-failed")
        with pytest.raises(RuntimeError, match="permission denied"):
            await manager.record_memory_checkpoint(key, turn_id="turn-failed")

        rows = await storage.list_memory_durable_receipts(
            session_key=key,
            status="checkpoint_failed",
        )

        assert len(rows) == 2
        assert {row.reason for row in rows} == {"disk full", "permission denied"}
        assert len({row.idempotency_key for row in rows}) == 2
    finally:
        await storage.close()


async def test_record_memory_checkpoint_requires_explicit_checkpoint_workspace(tmp_path):
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    manager = SessionManager(storage)
    manager.workspace_dir = tmp_path / "dynamic-attribute"
    try:
        key = "agent:main:webchat:abc"
        await manager.create(key)
        await manager.append_message(key, role="user", content="checkpoint body")

        with pytest.raises(RuntimeError, match="checkpoint workspace_dir is not configured"):
            await manager.record_memory_checkpoint(key, turn_id="turn-no-workspace")

        rows = await storage.list_memory_durable_receipts(
            session_key=key,
            status="checkpoint_failed",
        )
        assert len(rows) == 1
        assert rows[0].reason == "checkpoint workspace_dir is not configured"
        assert not (tmp_path / "dynamic-attribute").exists()
    finally:
        await storage.close()


async def test_record_memory_checkpoint_writes_checkpoint_off_event_loop(
    tmp_path, monkeypatch
):
    import opensquilla.memory.checkpoint as checkpoint

    storage = await SessionStorage.open(tmp_path / "sessions.db")
    manager = SessionManager(storage, checkpoint_workspace_dir=tmp_path / "workspace")
    event_loop_thread = threading.get_ident()
    writer_thread_ids: list[int] = []
    original_append = checkpoint.append_checkpoint_events
    try:
        key = "agent:main:webchat:abc"
        await manager.create(key)
        await manager.append_message(key, role="user", content="checkpoint body")

        def _spy_append(*args, **kwargs):
            writer_thread_ids.append(threading.get_ident())
            return original_append(*args, **kwargs)

        monkeypatch.setattr(checkpoint, "append_checkpoint_events", _spy_append)

        await manager.record_memory_checkpoint(key, turn_id="turn-threaded")

        assert writer_thread_ids
        assert writer_thread_ids[0] != event_loop_thread
    finally:
        await storage.close()


async def test_record_memory_checkpoint_thread_write_respects_shared_deadline(
    tmp_path, monkeypatch
):
    import opensquilla.memory.checkpoint as checkpoint

    storage = await SessionStorage.open(tmp_path / "sessions.db")
    manager = SessionManager(storage, checkpoint_workspace_dir=tmp_path / "workspace")
    writer_started = threading.Event()
    release_writer = threading.Event()
    writer_finished = threading.Event()
    original_append = checkpoint.append_checkpoint_events
    try:
        key = "agent:main:webchat:checkpoint-thread-timeout"
        await manager.create(key)
        await manager.append_message(key, role="user", content="checkpoint body")

        def _blocking_append(*args, **kwargs):
            writer_started.set()
            release_writer.wait(timeout=2.0)
            try:
                return original_append(*args, **kwargs)
            finally:
                writer_finished.set()

        monkeypatch.setattr(checkpoint, "append_checkpoint_events", _blocking_append)
        config = CompactionConfig(total_timeout_seconds=0.1)

        with pytest.raises(CompactionTimeoutError) as exc_info:
            await manager.record_memory_checkpoint(
                key,
                turn_id="cmp_checkpoint_thread_timeout",
                compaction_config=config,
            )

        assert exc_info.value.phase == "checkpointing"
        assert writer_started.is_set()
        assert config.deadline_at_monotonic is not None
    finally:
        release_writer.set()
        assert await asyncio.to_thread(writer_finished.wait, 2.0)
        rows = await storage.list_memory_durable_receipts(
            session_key="agent:main:webchat:checkpoint-thread-timeout"
        )
        assert rows == []
        await storage.close()


async def test_record_memory_checkpoint_receipt_db_uses_remaining_deadline(
    tmp_path, monkeypatch
):
    import opensquilla.memory.checkpoint as checkpoint

    class Clock:
        now = 100.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    monkeypatch.setattr("opensquilla.session.compaction.time", clock)
    storage = await SessionStorage.open(tmp_path / "sessions.db")
    manager = SessionManager(storage, checkpoint_workspace_dir=tmp_path / "workspace")
    original_append = checkpoint.append_checkpoint_events
    never_release = asyncio.Event()
    remaining_at_upsert: list[float] = []
    config = CompactionConfig(total_timeout_seconds=0.2)
    try:
        key = "agent:main:webchat:checkpoint-db-timeout"
        node = await manager.create(key)
        await manager.append_message(key, role="user", content="checkpoint body")

        def _consume_checkpoint_budget(*args, **kwargs):
            result = original_append(*args, **kwargs)
            clock.now += 0.15
            return result

        async def _blocking_upsert(_receipt, *, expected_session_id=None):
            assert expected_session_id == node.session_id
            remaining = compaction_remaining_seconds(config)
            assert remaining is not None
            remaining_at_upsert.append(remaining)
            await never_release.wait()
            raise AssertionError("cancelled receipt upsert must not resume")

        monkeypatch.setattr(checkpoint, "append_checkpoint_events", _consume_checkpoint_budget)
        monkeypatch.setattr(storage, "upsert_memory_durable_receipt", _blocking_upsert)

        with pytest.raises(CompactionTimeoutError) as exc_info:
            await manager.record_memory_checkpoint(
                key,
                turn_id="cmp_checkpoint_db_timeout",
                compaction_config=config,
            )

        assert exc_info.value.phase == "checkpointing"
        assert remaining_at_upsert == pytest.approx([0.05])
        assert config.deadline_at_monotonic == pytest.approx(100.2)
        rows = await storage.list_memory_durable_receipts(session_key=key)
        assert rows == []
    finally:
        await storage.close()


async def test_session_flush_repair_receipt_writer_records_raw_fallback_in_ledger(
    tmp_path,
):
    class InvalidJsonProvider:
        async def complete(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(content='{"candidates": [')

    storage = await SessionStorage.open(tmp_path / "sessions.db")
    try:
        session_key = "agent:main:webchat:abc"
        session_id = "session-1"

        async def handler(call: ToolCall) -> ToolResult:
            raise AssertionError(f"raw fallback must not call {call.tool_name}")

        async def receipt_writer(receipt: FlushReceipt, **row: Any) -> None:
            await storage.upsert_memory_durable_receipt(
                MemoryDurableReceipt(
                    session_key=row["session_key"],
                    session_id=session_id,
                    scope=row["scope"],
                    target_path=row["target_path"],
                    idempotency_key=(
                        f"{row['scope']}:{row['session_key']}:{row['status']}:"
                        f"{row['target_path']}"
                    ),
                    status=row["status"],
                    reason=row["reason"],
                    attempt_count=1,
                )
            )

        service = SessionFlushService(
            provider_selector=lambda _agent_id: InvalidJsonProvider(),
            tool_registry=SimpleNamespace(
                to_tool_definitions=lambda: [SimpleNamespace(name="memory_save")]
            ),
            tool_handler=handler,
            receipt_writer=receipt_writer,
            archive_workspace_resolver=lambda _agent_id: tmp_path,
        )

        receipt = await service.execute(
            [Message(role="user", content="temporary transcript")],
            session_key,
            agent_id="main",
        )

        rows = await storage.list_memory_durable_receipts(session_key=session_key)

        assert receipt.result_status == "parse_failed_archived"
        assert len(rows) == 2
        assert rows[0].scope == "preimage"
        assert rows[1].scope == "repair"
        assert rows[1].status == "repair_pending"
        assert rows[1].reason == "parse_failed_archived"
        assert rows[1].target_path == receipt.flushed_paths[0]
    finally:
        await storage.close()
