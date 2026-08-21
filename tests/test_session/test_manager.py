"""Tests for SessionManager lifecycle operations."""

import asyncio
import contextlib
import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from opensquilla.session import manager as session_manager_module
from opensquilla.session.compaction import CompactionConfig, CompactionResult
from opensquilla.session.context_view import (
    build_compaction_context_records,
    format_compaction_summary_context,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    PlanRunRecord,
    SessionContextState,
    SessionIntent,
    SessionStatus,
    SessionSummary,
    TranscriptEntry,
)
from opensquilla.session.plans import new_plan_revision
from opensquilla.session.storage import (
    CANONICAL_FORK_PROOF_SCHEMA_VERSION,
    SessionStorage,
    StaleEpochError,
)
from opensquilla.turn_outcome_projection import (
    attach_fork_terminal_outcome_projection,
    build_fork_terminal_outcome_projection,
    extract_fork_terminal_outcome_projection,
)


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    yield mgr
    await storage.close()


@pytest.mark.asyncio
async def test_create_session(manager):
    node = await manager.create("agent:main:main")
    assert node.session_key == "agent:main:main"
    assert node.status == SessionStatus.RUNNING
    assert node.session_id is not None


@pytest.mark.asyncio
async def test_get_session_returns_existing_without_touching(manager):
    node = await manager.create("agent:main:main")

    fetched = await manager.get_session("agent:main:main")
    missing = await manager.get_session("agent:main:missing")

    assert fetched is not None
    assert fetched.session_key == node.session_key
    assert fetched.session_id == node.session_id
    assert missing is None


@pytest.mark.asyncio
async def test_create_duplicate_raises(manager):
    await manager.create("agent:main:main")
    with pytest.raises(ValueError):
        await manager.create("agent:main:main")


@pytest.mark.asyncio
async def test_get_or_create_creates(manager):
    node, created = await manager.get_or_create("agent:main:main")
    assert created is True


@pytest.mark.asyncio
async def test_get_or_create_returns_existing(manager):
    await manager.create("agent:main:main")
    node, created = await manager.get_or_create("agent:main:main")
    assert created is False


@pytest.mark.asyncio
async def test_apply_intent_continue_preserves_existing_identity_and_transcript(manager):
    node = await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "hello")
    before = await manager.get_session("agent:main:main")
    assert before is not None
    before.updated_at = 123
    await manager._storage.upsert_session(before)
    upsert_spy = AsyncMock(wraps=manager._storage.upsert_session)
    manager._storage.upsert_session = upsert_spy

    applied, rotated = await manager.apply_intent("agent:main:main", SessionIntent.CONTINUE)

    assert rotated is False
    assert applied.session_id == node.session_id
    assert len(await manager.get_transcript("agent:main:main")) == 1
    upsert_spy.assert_not_awaited()
    persisted = await manager.get_session("agent:main:main")
    assert persisted is not None
    assert persisted.updated_at == 123


@pytest.mark.asyncio
async def test_apply_intent_new_chat_rejects_existing_key(manager):
    await manager.create("agent:main:main")

    with pytest.raises(ValueError, match="session_key conflict"):
        await manager.apply_intent("agent:main:main", SessionIntent.NEW_CHAT)


@pytest.mark.asyncio
async def test_apply_intent_reset_same_key_rotates_identity_and_clears_state(
    manager, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    node = await manager.create("agent:main:main")
    old_session_id = node.session_id
    node.total_tokens = 123
    node.input_tokens = 10
    node.output_tokens = 20
    node.estimated_cost_usd = 0.42
    node.total_cost_usd = 0.42
    node.billed_cost_usd = 0.30
    node.estimated_cost_component_usd = 0.12
    node.cost_source = "mixed"
    node.missing_cost_entries = 1
    node.cache_read = 7
    node.cache_write = 8
    await manager._storage.upsert_session(node)
    revision = await manager._storage.create_plan_revision(
        new_plan_revision(
            source_session_key=node.session_key,
            source_session_id=node.session_id,
            source_epoch=int(node.epoch or 0),
            title="Old task plan",
            markdown="## Old task plan",
            steps=[{"step_id": "old", "title": "Old step"}],
        ),
        expected_parent_revision_id=None,
    )
    current = await manager.get_session(node.session_key)
    assert current is not None
    await manager._storage.set_collaboration_mode(
        node.session_key,
        "plan",
        expected_revision=int(current.collaboration_revision or 0),
    )
    old_run = await manager._storage.start_plan_run(
        PlanRunRecord(
            run_id="old-task-run",
            session_key=node.session_key,
            session_id=node.session_id,
            session_epoch=int(node.epoch or 0),
            plan_revision_id=revision.revision_id,
            driver_kind="manual",
            status="queued",
        )
    )
    await manager.append_message("agent:main:main", "user", "hello")
    await manager._storage.save_summary(
        SessionSummary(
            session_id=old_session_id,
            session_key="agent:main:main",
            summary_text="old summary",
        )
    )
    await manager.save_context_state(
        SessionContextState(
            session_id=old_session_id,
            session_key="agent:main:main",
            provider="portable",
            state_kind="structured_summary_v1",
            payload={"user_goal": "old task"},
            covered_through_id=1,
            valid=True,
        )
    )

    applied, rotated = await manager.apply_intent("agent:main:main", SessionIntent.RESET_SAME_KEY)

    assert rotated is True
    assert applied.session_key == "agent:main:main"
    assert applied.session_id != old_session_id
    assert await manager._storage.count_transcript_entries(old_session_id) == 0
    assert await manager._storage.count_transcript_entries(applied.session_id) == 0
    assert await manager._storage.get_all_summaries(old_session_id) == []
    assert await manager.get_context_states("agent:main:main") == []
    invalidated_states = await manager.get_context_states("agent:main:main", valid_only=False)
    assert len(invalidated_states) == 1
    assert invalidated_states[0].valid is False
    assert invalidated_states[0].invalid_reason == "session_reset"
    assert applied.total_tokens == 0
    assert applied.input_tokens == 0
    assert applied.output_tokens == 0
    assert applied.estimated_cost_usd == 0.0
    assert applied.total_cost_usd == 0.0
    assert applied.billed_cost_usd == 0.0
    assert applied.estimated_cost_component_usd == 0.0
    assert applied.cost_source == "none"
    assert applied.missing_cost_entries == 0
    assert applied.cache_read == 0
    assert applied.cache_write == 0
    assert applied.collaboration_mode == "default"
    assert applied.collaboration_revision == 0
    assert applied.active_plan_revision_id is None
    superseded = await manager._storage.get_plan_run(old_run.run_id)
    assert superseded is not None
    assert superseded.status == "superseded"
    assert superseded.terminal_reason == "session_reset"
    archive_files = list((tmp_path / "archives").glob("*.json"))
    assert len(archive_files) == 1
    archived = json.loads(archive_files[0].read_text(encoding="utf-8"))
    assert archived["session_key"] == "agent:main:main"
    assert archived["session_id"] == old_session_id
    assert archived["transcript_entries"][0]["content"] == "hello"
    assert archived["summaries"][0]["summary_text"] == "old summary"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics only")
async def test_reset_archive_is_owner_only(manager, tmp_path, monkeypatch):
    archive_root = tmp_path / "session-archive"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(archive_root))
    old_umask = os.umask(0o022)
    try:
        await manager.create("agent:main:main")
        await manager.append_message("agent:main:main", "user", "hello")

        _, rotated = await manager.apply_intent("agent:main:main", SessionIntent.RESET_SAME_KEY)
        assert rotated is True
    finally:
        os.umask(old_umask)

    archives = list(archive_root.glob("*.json"))
    assert len(archives) == 1
    # The archive holds the full raw transcript, so it must match the
    # sessions.db hardening (0600 file inside a 0700 directory).
    assert stat.S_IMODE(archive_root.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(archives[0].stat().st_mode) & 0o077 == 0


@pytest.mark.asyncio
async def test_reset_same_key_fences_appends_that_read_the_old_epoch(
    manager, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    node = await manager.create("agent:main:main")
    pre_epoch = await manager._storage.get_epoch("agent:main:main")

    applied, rotated = await manager.apply_intent("agent:main:main", SessionIntent.RESET_SAME_KEY)

    assert rotated is True
    # The rotation itself bumps the epoch, so a writer holding the pre-reset
    # node cannot land its entry (or roll back the reset via a stale upsert).
    assert await manager._storage.get_epoch("agent:main:main") > pre_epoch
    with pytest.raises(StaleEpochError):
        await manager._storage.append_transcript_entry(
            TranscriptEntry(
                session_id=node.session_id,
                session_key="agent:main:main",
                role="user",
                content="stale in-flight message",
            ),
            expected_epoch=pre_epoch,
        )
    row = await manager._storage.get_session("agent:main:main")
    assert row is not None
    assert row.session_id == applied.session_id
    assert [e.content for e in await manager.get_transcript("agent:main:main")] == []


@pytest.mark.asyncio
async def test_reset_same_key_archive_preserves_compacted_canonical_transcript(
    manager, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    node = await manager.create("agent:main:main")
    old_session_id = node.session_id
    for index in range(4):
        await manager.append_message("agent:main:main", "user", f"msg {index}", token_count=5)
    await manager.persist_compaction_result(
        "agent:main:main",
        "short summary",
        [{"role": "assistant", "content": "latest reply"}],
        compaction_id="cmp_reset_archive",
    )

    canonical_before_reset = [
        entry.content for entry in await manager.get_canonical_transcript("agent:main:main")
    ]

    applied, rotated = await manager.apply_intent("agent:main:main", SessionIntent.RESET_SAME_KEY)

    assert rotated is True
    assert applied.session_id != old_session_id
    archive_files = list((tmp_path / "archives").glob("*.json"))
    assert len(archive_files) == 1
    archived = json.loads(archive_files[0].read_text(encoding="utf-8"))
    assert [entry["content"] for entry in archived["transcript_entries"]] == (
        canonical_before_reset
    )
    assert archived["summaries"][0]["compaction_id"] == "cmp_reset_archive"


@pytest.mark.asyncio
async def test_apply_intent_reset_same_key_missing_creates_session(manager):
    applied, rotated = await manager.apply_intent(
        "agent:main:missing", SessionIntent.RESET_SAME_KEY
    )

    assert rotated is True
    assert applied.session_key == "agent:main:missing"
    assert applied.session_id


@pytest.mark.asyncio
async def test_apply_intent_reset_same_key_archive_failure_preserves_history(
    manager, tmp_path, monkeypatch
):
    archive_file = tmp_path / "not-a-directory"
    archive_file.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(archive_file))
    node = await manager.create("agent:main:main")
    old_epoch = int(node.epoch or 0)
    manager.set_cached_epoch(node.session_key, old_epoch)
    old_session_id = node.session_id
    await manager.append_message("agent:main:main", "user", "hello")
    await manager._storage.save_summary(
        SessionSummary(
            session_id=old_session_id,
            session_key="agent:main:main",
            summary_text="preserve me",
        )
    )

    with pytest.raises(OSError):
        await manager.apply_intent("agent:main:main", SessionIntent.RESET_SAME_KEY)

    persisted = await manager.get_session("agent:main:main")
    assert persisted is not None
    assert persisted.session_id == old_session_id
    assert persisted.epoch == old_epoch
    assert manager.get_cached_epoch(node.session_key) == old_epoch
    assert await manager._storage.count_transcript_entries(old_session_id) == 1
    summaries = await manager._storage.get_all_summaries(old_session_id)
    assert [summary.summary_text for summary in summaries] == ["preserve me"]


@pytest.mark.asyncio
async def test_reset_storage_failure_preserves_identity_history_and_cache(
    manager,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    node = await manager.create("agent:main:main")
    old_session_id = node.session_id
    old_epoch = int(node.epoch or 0)
    manager.set_cached_epoch(node.session_key, old_epoch)
    await manager.append_message("agent:main:main", "user", "hello")
    monkeypatch.setattr(
        manager._storage,
        "reset_session",
        AsyncMock(side_effect=OSError("reset unavailable")),
    )

    with pytest.raises(OSError, match="reset unavailable"):
        await manager.apply_intent("agent:main:main", SessionIntent.RESET_SAME_KEY)

    persisted = await manager.get_session("agent:main:main")
    assert persisted is not None
    assert persisted.session_id == old_session_id
    assert persisted.epoch == old_epoch
    assert node.session_id == old_session_id
    assert node.epoch == old_epoch
    assert manager.get_cached_epoch(node.session_key) == old_epoch
    assert await manager._storage.count_transcript_entries(old_session_id) == 1


@pytest.mark.asyncio
async def test_reset_archive_capture_failure_preserves_identity_and_history(
    manager,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    node = await manager.create("agent:main:main")
    old_session_id = node.session_id
    await manager.append_message("agent:main:main", "user", "hello")
    monkeypatch.setattr(
        manager._storage,
        "_select_canonical_transcript",
        AsyncMock(side_effect=OSError("transcript unavailable")),
    )

    with pytest.raises(OSError, match="transcript unavailable"):
        await manager.apply_intent("agent:main:main", SessionIntent.RESET_SAME_KEY)

    persisted = await manager.get_session("agent:main:main")
    assert persisted is not None
    assert persisted.session_id == old_session_id
    assert await manager._storage.count_transcript_entries(old_session_id) == 1


@pytest.mark.asyncio
async def test_reset_mid_delete_failure_rolls_back_every_database_change(
    manager,
    tmp_path,
    monkeypatch,
):
    archive_dir = tmp_path / "archives"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(archive_dir))
    node = await manager.create("agent:main:main")
    old_session_id = node.session_id
    old_epoch = int(node.epoch or 0)
    manager.set_cached_epoch(node.session_key, old_epoch)
    await manager.append_message("agent:main:main", "user", "preserve transcript")
    await manager._storage.save_summary(
        SessionSummary(
            session_id=old_session_id,
            session_key=node.session_key,
            summary_text="preserve summary",
        )
    )
    await manager.save_context_state(
        SessionContextState(
            session_id=old_session_id,
            session_key=node.session_key,
            provider="portable",
            state_kind="structured_summary_v1",
            payload={"current_status": "preserve context"},
            covered_through_id=1,
            valid=True,
        )
    )

    async def fail_after_transcript_delete(conn, session_id: str) -> None:
        await conn.execute(
            "DELETE FROM transcript_entries WHERE session_id = ?",
            (session_id,),
        )
        await conn.execute(
            "DELETE FROM compacted_transcript_entries WHERE session_id = ?",
            (session_id,),
        )
        raise OSError("injected mid-delete failure")

    monkeypatch.setattr(
        manager._storage,
        "_delete_reset_history",
        fail_after_transcript_delete,
    )

    with pytest.raises(OSError, match="injected mid-delete failure"):
        await manager.apply_intent(node.session_key, SessionIntent.RESET_SAME_KEY)

    persisted = await manager.get_session(node.session_key)
    assert persisted is not None
    assert persisted.session_id == old_session_id
    assert persisted.epoch == old_epoch
    assert node.session_id == old_session_id
    assert node.epoch == old_epoch
    assert manager.get_cached_epoch(node.session_key) == old_epoch
    transcript = await manager._storage.get_transcript(old_session_id)
    assert [entry.content for entry in transcript] == ["preserve transcript"]
    summaries = await manager._storage.get_all_summaries(old_session_id)
    assert [summary.summary_text for summary in summaries] == ["preserve summary"]
    context_states = await manager.get_context_states(node.session_key)
    assert len(context_states) == 1
    assert context_states[0].valid is True

    archive_files = list(archive_dir.glob("*.json"))
    assert len(archive_files) == 1
    archived = json.loads(archive_files[0].read_text(encoding="utf-8"))
    assert archived["session_id"] == old_session_id
    assert archived["transcript_entries"][0]["content"] == "preserve transcript"
    assert archived["summaries"][0]["summary_text"] == "preserve summary"


@pytest.mark.asyncio
async def test_reset_archive_fsyncs_before_atomic_publish_and_avoids_collisions(
    manager,
    tmp_path,
    monkeypatch,
):
    from opensquilla.session import manager as manager_module

    archive_dir = tmp_path / "archives"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(archive_dir))
    monkeypatch.setattr(manager_module, "_now_ms", lambda: 123456789)
    node = await manager.create("agent:main:main")
    await manager.append_message(node.session_key, "user", "durable archive")
    entries = await manager._storage.get_canonical_transcript(node.session_id)

    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def tracking_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def tracking_replace(source, destination) -> None:
        assert "fsync" in events
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(manager_module.os, "fsync", tracking_fsync)
    monkeypatch.setattr(manager_module.os, "replace", tracking_replace)

    await manager.write_session_archive(node, entries, [])
    await manager.write_session_archive(node, entries, [])

    assert events.count("replace") == 2
    assert events[0] == "fsync"
    archives = list(archive_dir.glob("*.json"))
    assert len(archives) == 2
    assert archives[0].name != archives[1].name
    assert list(archive_dir.glob("*.tmp")) == []
    for archive in archives:
        payload = json.loads(archive.read_text(encoding="utf-8"))
        assert payload["transcript_entries"][0]["content"] == "durable archive"


@pytest.mark.asyncio
async def test_reset_archive_publish_failure_removes_temporary_file(
    manager,
    tmp_path,
    monkeypatch,
):
    from opensquilla.session import manager as manager_module

    archive_dir = tmp_path / "archives"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(archive_dir))
    node = await manager.create("agent:main:main")
    await manager.append_message(node.session_key, "user", "keep source")
    entries = await manager._storage.get_canonical_transcript(node.session_id)

    def fail_replace(_source, _destination) -> None:
        raise OSError("replace unavailable")

    monkeypatch.setattr(manager_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace unavailable"):
        await manager.write_session_archive(node, entries, [])

    assert list(archive_dir.iterdir()) == []
    persisted = await manager.get_session(node.session_key)
    assert persisted is not None
    assert persisted.session_id == node.session_id
    transcript = await manager._storage.get_transcript(node.session_id)
    assert [entry.content for entry in transcript] == ["keep source"]


@pytest.mark.asyncio
async def test_reset_archive_bounds_filename_but_preserves_full_session_key(
    manager,
    tmp_path,
    monkeypatch,
):
    from opensquilla.paths import native_io_path

    archive_dir = tmp_path / "archives"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(archive_dir))
    node = await manager.create("agent:main:main")
    await manager.append_message(node.session_key, "user", "long key archive")
    entries = await manager._storage.get_canonical_transcript(node.session_id)
    long_key = "agent:main:" + ("long-key-" * 80)
    archive_node = node.model_copy(deep=True)
    archive_node.session_key = long_key

    await manager.write_session_archive(archive_node, entries, [])

    archives = list(archive_dir.glob("*.json"))
    assert len(archives) == 1
    archive = archives[0]
    assert len(archive.name) <= 255
    payload = json.loads(native_io_path(archive).read_text(encoding="utf-8"))
    assert payload["session_key"] == long_key
    assert payload["transcript_entries"][0]["content"] == "long key archive"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
async def test_reset_archive_supports_long_windows_path(
    manager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.paths import native_io_path

    long_root = tmp_path / "long-session-archive"
    archive_root = long_root
    index = 0
    while len(str(archive_root)) <= 280:
        archive_root /= f"segment-{index:02d}-" + ("a" * 40)
        index += 1
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(archive_root))

    try:
        node = await manager.create("agent:main:main")
        old_session_id = node.session_id
        await manager.append_message("agent:main:main", "user", "long archive")
        await manager._storage.save_summary(
            SessionSummary(
                session_id=old_session_id,
                session_key="agent:main:main",
                summary_text="long archive summary",
            )
        )

        applied, rotated = await manager.apply_intent(
            "agent:main:main",
            SessionIntent.RESET_SAME_KEY,
        )

        assert rotated is True
        assert applied.session_id != old_session_id
        archive_files = list(native_io_path(archive_root).glob("*.json"))
        assert len(archive_files) == 1
        archived = json.loads(archive_files[0].read_text(encoding="utf-8"))
        assert archived["session_id"] == old_session_id
        assert archived["transcript_entries"][0]["content"] == "long archive"
        assert archived["summaries"][0]["summary_text"] == "long archive summary"
    finally:
        native_root = native_io_path(long_root)
        if native_root.exists():
            shutil.rmtree(native_root)


@pytest.mark.asyncio
async def test_resume_touches_updated_at(manager):
    node = await manager.create("agent:main:main")
    old_ts = node.updated_at
    import asyncio

    await asyncio.sleep(0.01)
    resumed = await manager.resume("agent:main:main")
    assert resumed.updated_at >= old_ts


@pytest.mark.asyncio
async def test_resume_missing_raises(manager):
    with pytest.raises(KeyError):
        await manager.resume("agent:main:nope")


@pytest.mark.parametrize("operation", ["resume", "update", "finish"])
@pytest.mark.asyncio
async def test_existing_session_mutations_do_not_recreate_deleted_generation(
    manager,
    monkeypatch,
    operation,
):
    key = "agent:main:generation-race"
    await manager.create(key)
    original_upsert = manager._storage.upsert_session

    async def delete_before_upsert(node, **kwargs):
        await manager._storage.delete_session(key)
        return await original_upsert(node, **kwargs)

    monkeypatch.setattr(manager._storage, "upsert_session", delete_before_upsert)

    with pytest.raises(KeyError, match="Session generation changed"):
        if operation == "resume":
            await manager.resume(key)
        elif operation == "update":
            await manager.update(key, derived_title="Late title")
        else:
            await manager.finish(key)

    assert await manager._storage.get_session(key) is None


@pytest.mark.asyncio
async def test_update_fields(manager):
    await manager.create("agent:main:main")
    updated = await manager.update("agent:main:main", model="claude-opus-4-6", channel="telegram")
    assert updated.model == "claude-opus-4-6"
    assert updated.channel == "telegram"


@pytest.mark.asyncio
async def test_finish_sets_status(manager):
    await manager.create("agent:main:main")
    node = await manager.finish("agent:main:main")
    assert node.status == SessionStatus.DONE
    assert node.ended_at is not None
    assert node.runtime_ms is not None


@pytest.mark.asyncio
async def test_finish_failed(manager):
    await manager.create("agent:main:main")
    node = await manager.finish("agent:main:main", status=SessionStatus.FAILED)
    assert node.status == SessionStatus.FAILED


@pytest.mark.asyncio
async def test_append_message(manager):
    await manager.create("agent:main:main")
    entry = await manager.append_message("agent:main:main", "user", "Hello!")
    assert entry.role == "user"
    assert entry.content == "Hello!"


def _submit_plan_segments(
    *,
    tool_use_id: str = "submit-plan-1",
    title: str = "Implement Plan mode",
    markdown: str = "## Plan\n\nImplement and verify the collaboration flow.",
) -> list[dict[str, Any]]:
    return [
        {
            "type": "tool_use",
            "tool_use_id": tool_use_id,
            "name": "submit_plan",
            "input": {
                "title": title,
                "markdown": markdown,
                "steps": [
                    {"step_id": "inspect", "title": "Inspect the current flow"},
                    {"step_id": "implement", "title": "Implement and verify"},
                ],
            },
        },
        {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "name": "submit_plan",
            "result": json.dumps(
                {
                    "status": "plan_submitted",
                    "title": title,
                    "step_count": 2,
                }
            ),
            "is_error": False,
        },
    ]


@pytest.mark.asyncio
async def test_successful_submit_plan_atomically_creates_typed_revision(manager):
    await manager.create("agent:main:main")
    await manager._storage.set_collaboration_mode(
        "agent:main:main",
        "plan",
        expected_revision=0,
    )

    entry = await manager.append_message(
        "agent:main:main",
        "assistant",
        "Plan ready.",
        tool_calls=_submit_plan_segments(),
    )

    node = await manager.get_session("agent:main:main")
    assert node is not None
    assert node.collaboration_mode == "plan"
    assert node.collaboration_revision == 2
    assert node.active_plan_revision_id is not None
    revision = await manager._storage.get_current_plan_revision("agent:main:main")
    assert revision is not None
    assert revision.revision_id == node.active_plan_revision_id
    assert revision.generation == 1
    assert [step["step_id"] for step in revision.steps] == ["inspect", "implement"]
    assert entry.turn_context is not None
    assert entry.turn_context["plan_revision_id"] == revision.revision_id
    plan_segments = [
        segment for segment in entry.tool_calls or [] if segment.get("type") == "plan"
    ]
    assert plan_segments == [
        {
            "type": "plan",
            "snapshot": {
                **plan_segments[0]["snapshot"],
                "current": True,
            },
        }
    ]
    assert plan_segments[0]["snapshot"]["revisionId"] == revision.revision_id


@pytest.mark.asyncio
async def test_replan_is_full_immutable_replacement_with_parent_link(manager):
    await manager.create("agent:main:main")
    first_entry = await manager.append_message(
        "agent:main:main",
        "assistant",
        "First plan.",
        tool_calls=_submit_plan_segments(),
    )
    first = await manager._storage.get_current_plan_revision("agent:main:main")
    assert first is not None

    second_entry = await manager.append_message(
        "agent:main:main",
        "assistant",
        "Revised plan.",
        tool_calls=_submit_plan_segments(
            tool_use_id="submit-plan-2",
            title="Revised Plan mode",
            markdown="## Revised plan\n\nUse a complete replacement.",
        ),
    )
    second = await manager._storage.get_current_plan_revision("agent:main:main")

    assert second is not None
    assert second.revision_id != first.revision_id
    assert second.plan_id == first.plan_id
    assert second.parent_revision_id == first.revision_id
    assert second.generation == 2
    assert first_entry.message_id != second_entry.message_id
    persisted_first = await manager._storage.get_plan_revision(first.revision_id)
    assert persisted_first == first


@pytest.mark.asyncio
async def test_failed_submit_plan_result_does_not_create_revision(manager):
    await manager.create("agent:main:main")
    segments = _submit_plan_segments()
    segments[-1]["is_error"] = True

    await manager.append_message(
        "agent:main:main",
        "assistant",
        "Submission failed.",
        tool_calls=segments,
    )

    assert await manager._storage.get_current_plan_revision("agent:main:main") is None


@pytest.mark.asyncio
async def test_append_message_updates_tokens(manager):
    await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "hi", token_count=10)
    node = await manager._storage.get_session("agent:main:main")
    assert node.total_tokens == 10


@pytest.mark.asyncio
async def test_append_message_with_turn_usage_does_not_double_count_output_tokens(manager):
    await manager.create("agent:main:main")
    await manager.append_message(
        "agent:main:main",
        "assistant",
        "hi",
        turn_usage={"model": "gpt-test", "input_tokens": 10, "output_tokens": 3},
        token_count=3,
    )
    node = await manager._storage.get_session("agent:main:main")
    assert node.total_tokens == 0


@pytest.mark.asyncio
async def test_get_transcript(manager):
    await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "msg1")
    await manager.append_message("agent:main:main", "assistant", "resp1")
    entries = await manager.get_transcript("agent:main:main")
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_get_transcript_orders_same_timestamp_by_insert_id(manager):
    node = await manager.create("agent:main:main")
    for content in ("first", "second", "third"):
        await manager._storage.append_transcript_entry(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=node.session_key,
                role="user",
                content=content,
                created_at=12345,
            )
        )

    entries = await manager.get_transcript("agent:main:main")

    assert [entry.content for entry in entries] == ["first", "second", "third"]
    assert [entry.id for entry in entries] == sorted(entry.id for entry in entries)


def test_get_transcript_query_uses_id_tiebreaker() -> None:
    source = Path("src/opensquilla/session/storage.py").read_text(encoding="utf-8")

    assert "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?" in source


@pytest.mark.asyncio
async def test_truncate_zero_removes_all_entries(manager):
    await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "msg1")
    await manager.append_message("agent:main:main", "assistant", "resp1")

    result = await manager.truncate("agent:main:main", max_messages=0)

    assert result == {"truncated": True, "before_count": 2, "after_count": 0}
    assert await manager.get_transcript("agent:main:main") == []


@pytest.mark.asyncio
async def test_branch_creates_child(manager):
    await manager.create("agent:main:main")
    child = await manager.branch("agent:main:main", "agent:main:direct:u1")
    assert child.parent_session_key == "agent:main:main"
    assert child.spawn_depth == 1


@pytest.mark.asyncio
async def test_branch_fork_transcript(manager):
    await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "parent msg", token_count=5)
    await manager.append_message(
        "agent:main:main",
        "assistant",
        "parent reply",
        turn_usage={"model": "openai/gpt-test", "input_tokens": 11, "output_tokens": 5},
        token_count=5,
    )
    child = await manager.branch("agent:main:main", "agent:main:direct:u1", fork_transcript=True)
    assert child.forked_from_parent is True
    assert child.schema_version >= CANONICAL_FORK_PROOF_SCHEMA_VERSION
    entries = await manager.get_transcript("agent:main:direct:u1")
    assert len(entries) == 2
    assert entries[0].content == "parent msg"
    assert entries[1].turn_usage == {
        "model": "openai/gpt-test",
        "input_tokens": 11,
        "output_tokens": 5,
    }
    page = await manager.get_canonical_transcript_page(child.session_key, limit=10)
    assert page.canonical_complete is True


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_state", ["complete", "incomplete", "missing"])
async def test_legacy_zero_evidence_fork_fails_closed(
    manager,
    parent_state: str,
):
    parent_key = f"agent:main:legacy-parent-{parent_state}"
    if parent_state != "missing":
        parent = await manager.create(parent_key)
        await manager.append_message(parent_key, "user", "synthetic parent message")
        if parent_state == "incomplete":
            parent.compaction_count = 1
            await manager._storage.upsert_session(parent)

    child = await manager.create(f"agent:main:legacy-child-{parent_state}")
    child.parent_session_key = parent_key
    child.spawned_by = parent_key
    child.forked_from_parent = True
    child.schema_version = CANONICAL_FORK_PROOF_SCHEMA_VERSION - 1
    await manager._storage.upsert_session(child)
    await manager.append_message(child.session_key, "user", "synthetic copied prefix")

    page = await manager.get_canonical_transcript_page(child.session_key, limit=10)

    assert [entry.content for entry in page.entries] == ["synthetic copied prefix"]
    assert page.canonical_complete is False


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["reset", "recreate"])
async def test_parent_identity_replacement_cannot_certify_a_legacy_fork(
    manager,
    replacement: str,
):
    parent = await manager.create(f"agent:main:replace-parent-{replacement}")
    parent.compaction_count = 1
    await manager._storage.upsert_session(parent)

    child = await manager.create(f"agent:main:replace-child-{replacement}")
    child.parent_session_key = parent.session_key
    child.spawned_by = parent.session_key
    child.forked_from_parent = True
    child.schema_version = CANONICAL_FORK_PROOF_SCHEMA_VERSION - 1
    await manager._storage.upsert_session(child)
    before = await manager.get_canonical_transcript_page(child.session_key, limit=10)
    assert before.canonical_complete is False

    if replacement == "reset":
        await manager.apply_intent(parent.session_key, SessionIntent.RESET_SAME_KEY)
    else:
        await manager._storage.delete_session(parent.session_key)
        await manager.create(parent.session_key)

    parent_page = await manager.get_canonical_transcript_page(parent.session_key, limit=10)
    child_page = await manager.get_canonical_transcript_page(child.session_key, limit=10)
    assert parent_page.canonical_complete is True
    assert child_page.canonical_complete is False


@pytest.mark.asyncio
async def test_legacy_fork_compaction_cannot_erase_incomplete_parent_lineage(manager):
    parent = await manager.create("agent:main:legacy-incomplete-parent")
    parent.compaction_count = 1
    await manager._storage.upsert_session(parent)

    child = await manager.create("agent:main:legacy-compacted-child")
    child.parent_session_key = parent.session_key
    child.spawned_by = parent.session_key
    child.forked_from_parent = True
    child.schema_version = CANONICAL_FORK_PROOF_SCHEMA_VERSION - 1
    await manager._storage.upsert_session(child)
    for index in range(3):
        await manager.append_message(
            child.session_key,
            "user",
            f"synthetic child message {index}",
        )

    await manager.persist_compaction_result(
        child.session_key,
        "synthetic child summary",
        [{"role": "assistant", "content": "synthetic child tail"}],
        compaction_id="cmp-legacy-child",
    )
    page = await manager.get_canonical_transcript_page(child.session_key, limit=10)
    current = await manager.get_session(child.session_key)
    summaries = await manager.get_summaries(child.session_key)
    async with manager._storage.conn.execute(
        "SELECT COUNT(*) FROM compacted_transcript_entries WHERE session_id = ?",
        (child.session_id,),
    ) as cur:
        archived_count = int((await cur.fetchone())[0])

    assert current is not None
    assert current.compaction_count == 1
    assert len(summaries) == 1
    assert summaries[0].removed_count == archived_count
    assert page.canonical_complete is False


@pytest.mark.asyncio
async def test_new_full_fork_preserves_legacy_zero_count_incompleteness(manager):
    parent = await manager.create("agent:main:ambiguous-parent")
    parent.parent_session_key = "agent:main:missing-grandparent"
    parent.spawned_by = parent.parent_session_key
    parent.forked_from_parent = True
    parent.schema_version = CANONICAL_FORK_PROOF_SCHEMA_VERSION - 1
    await manager._storage.upsert_session(parent)
    await manager.append_message(parent.session_key, "user", "synthetic parent tail")
    parent_page = await manager.get_canonical_transcript_page(parent.session_key, limit=10)
    assert parent_page.canonical_complete is False

    child = await manager.branch(
        parent.session_key,
        "agent:main:new-child-from-ambiguous-parent",
        fork_transcript=True,
    )
    child_page = await manager.get_canonical_transcript_page(child.session_key, limit=10)

    assert child.schema_version >= CANONICAL_FORK_PROOF_SCHEMA_VERSION
    assert child.compaction_count == 1
    assert [entry.content for entry in child_page.entries] == ["synthetic parent tail"]
    assert child_page.canonical_complete is False


@pytest.mark.asyncio
async def test_reset_legacy_fork_starts_a_proven_empty_canonical_identity(manager):
    child = await manager.create("agent:main:legacy-reset-child")
    child.parent_session_key = "agent:main:missing-parent"
    child.spawned_by = child.parent_session_key
    child.forked_from_parent = True
    child.schema_version = CANONICAL_FORK_PROOF_SCHEMA_VERSION - 1
    await manager._storage.upsert_session(child)
    before = await manager.get_canonical_transcript_page(child.session_key, limit=10)
    assert before.canonical_complete is False

    reset, rotated = await manager.apply_intent(
        child.session_key,
        SessionIntent.RESET_SAME_KEY,
    )
    after = await manager.get_canonical_transcript_page(child.session_key, limit=10)

    assert rotated is True
    assert reset.schema_version >= CANONICAL_FORK_PROOF_SCHEMA_VERSION
    assert after.entries == []
    assert after.canonical_complete is True


@pytest.mark.asyncio
async def test_prepared_reset_legacy_fork_starts_a_proven_canonical_identity(manager):
    child = await manager.create("agent:main:prepared-legacy-reset-child")
    child.parent_session_key = "agent:main:missing-parent"
    child.spawned_by = child.parent_session_key
    child.forked_from_parent = True
    child.schema_version = CANONICAL_FORK_PROOF_SCHEMA_VERSION - 1
    child.compaction_count = 2
    await manager._storage.upsert_session(child)

    plan = await manager.prepare_intent(
        child.session_key,
        SessionIntent.RESET_SAME_KEY,
    )

    assert plan.action == "reset"
    assert plan.node.session_id != child.session_id
    assert plan.node.compaction_count == 0
    assert plan.node.schema_version >= CANONICAL_FORK_PROOF_SCHEMA_VERSION


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_complete", [True, False])
async def test_prepared_prefix_branch_preserves_parent_canonical_coverage(
    manager,
    parent_complete: bool,
):
    parent = await manager.create("agent:main:prepared-prefix-parent")
    await manager.append_message(parent.session_key, "user", "message 0")
    await manager.append_message(parent.session_key, "assistant", "message 1")
    fork_before = await manager.append_message(parent.session_key, "user", "message 2")
    if not parent_complete:
        parent.compaction_count = 1
        await manager._storage.upsert_session(parent)

    plan = await manager.prepare_prefix_branch(
        parent.session_key,
        "agent:main:prepared-prefix-child",
        fork_before_message_id=fork_before.message_id,
    )
    await manager._storage.upsert_session(plan.node)
    for entry in plan.initial_transcript_entries:
        await manager._storage.append_transcript_entry(entry)
    page = await manager.get_canonical_transcript_page(plan.node.session_key, limit=10)

    assert [entry.content for entry in page.entries] == ["message 0", "message 1"]
    assert plan.node.compaction_count == (0 if parent_complete else 1)
    assert plan.node.schema_version >= CANONICAL_FORK_PROOF_SCHEMA_VERSION
    assert page.canonical_complete is parent_complete


@pytest.mark.asyncio
async def test_branch_before_message_copies_only_prefix(manager):
    await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "A marker", token_count=5)
    await manager.append_message("agent:main:main", "assistant", "ack A", token_count=5)
    before_entry = await manager.append_message(
        "agent:main:main", "user", "B marker", token_count=5
    )
    await manager.append_message("agent:main:main", "assistant", "ack B", token_count=5)
    await manager.append_message("agent:main:main", "user", "C marker must not leak", token_count=5)

    child = await manager.branch(
        "agent:main:main",
        "agent:main:direct:edited",
        fork_transcript=True,
        fork_before_message_id=before_entry.message_id,
    )

    assert child.forked_from_parent is True
    child_contents = [
        entry.content for entry in await manager.get_transcript("agent:main:direct:edited")
    ]
    assert child_contents == [
        "A marker",
        "ack A",
    ]
    assert [entry.content for entry in await manager.get_transcript("agent:main:main")] == [
        "A marker",
        "ack A",
        "B marker",
        "ack B",
        "C marker must not leak",
    ]
    assert await manager.get_summaries("agent:main:direct:edited") == []
    assert await manager.get_context_states("agent:main:direct:edited") == []


@pytest.mark.asyncio
async def test_branch_before_message_missing_id_does_not_create_child(manager):
    await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "A marker")

    with pytest.raises(KeyError, match="Transcript message not found"):
        await manager.branch(
            "agent:main:main",
            "agent:main:direct:missing",
            fork_transcript=True,
            fork_before_message_id="missing-message-id",
        )

    assert await manager.get_session("agent:main:direct:missing") is None


@pytest.mark.asyncio
async def test_branch_through_terminal_turn_is_inclusive_and_excludes_promoted_future_input(
    manager,
):
    parent = await manager.create("agent:main:main")
    turn_id = "turn-complete"
    promoted_turn_id = "turn-promoted-next"
    entries = [
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="user",
            content="question",
            turn_context={"turn_id": turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="user",
            content="future promoted input must not leak",
            turn_context={
                "disposition": "promoted",
                "promoted_turn_id": promoted_turn_id,
                "turn_id": turn_id,
                "target_turn_id": turn_id,
            },
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="calling tool",
            tool_calls=[{"id": "call-1", "name": "lookup", "arguments": {}}],
            turn_context={"turn_id": turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="tool",
            content="tool result",
            tool_call_id="call-1",
            turn_context={"turn_id": turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="final answer",
            turn_context={"turn_id": turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="system",
            content="unscoped trailing system row must not be guessed into the turn",
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="later answer must not leak",
            turn_context={"turn_id": promoted_turn_id},
        ),
    ]
    for entry in entries:
        await manager._storage.append_transcript_entry(entry)
    await manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id=turn_id,
            session_key=parent.session_key,
            status=AgentTaskStatus.SUCCEEDED,
        )
    )
    parent.compaction_count = 1
    await manager._storage.upsert_session(parent)
    await manager._storage.save_summary(
        SessionSummary(
            session_id=parent.session_id,
            session_key=parent.session_key,
            compaction_id="zero-row-compaction",
            summary_text="must not be inherited by a historical prefix",
        )
    )
    await manager.save_context_state(
        SessionContextState(
            session_id=parent.session_id,
            session_key=parent.session_key,
            provider="portable",
            state_kind="structured_summary_v1",
            payload={"user_goal": "later parent state"},
            portable=True,
            cacheable=True,
        )
    )

    child = await manager.branch(
        parent.session_key,
        "agent:main:direct:through-turn",
        fork_transcript=True,
        fork_through_turn_id=turn_id,
    )

    assert child.forked_from_parent is True
    assert [
        (entry.role, entry.content)
        for entry in await manager.get_transcript(child.session_key)
    ] == [
        ("user", "question"),
        ("assistant", "calling tool"),
        ("tool", "tool result"),
        ("assistant", "final answer"),
    ]
    assert [entry.content for entry in await manager.get_transcript(parent.session_key)] == [
        "question",
        "future promoted input must not leak",
        "calling tool",
        "tool result",
        "final answer",
        "unscoped trailing system row must not be guessed into the turn",
        "later answer must not leak",
    ]
    assert await manager.get_summaries(child.session_key) == []
    assert await manager.get_context_states(child.session_key) == []


@pytest.mark.asyncio
async def test_branch_through_promoted_turn_uses_promoted_turn_identity(manager):
    parent = await manager.create("agent:main:main")
    first_turn_id = "turn-first"
    promoted_turn_id = "turn-promoted"
    entries = [
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="user",
            content="first question",
            turn_context={"turn_id": first_turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="user",
            content="promoted question",
            turn_context={
                "disposition": "promoted",
                "promoted_turn_id": promoted_turn_id,
                "turn_id": first_turn_id,
                "target_turn_id": first_turn_id,
            },
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="first answer finishes after promotion",
            turn_context={"turn_id": first_turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="promoted answer",
            turn_context={"turn_id": promoted_turn_id},
        ),
    ]
    for entry in entries:
        await manager._storage.append_transcript_entry(entry)
    await manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id=promoted_turn_id,
            session_key=parent.session_key,
            status=AgentTaskStatus.SUCCEEDED,
        )
    )

    child = await manager.branch(
        parent.session_key,
        "agent:main:direct:promoted-turn",
        fork_transcript=True,
        fork_through_turn_id=promoted_turn_id,
    )

    assert [entry.content for entry in await manager.get_transcript(child.session_key)] == [
        "first question",
        "promoted question",
        "first answer finishes after promotion",
        "promoted answer",
    ]


@pytest.mark.asyncio
async def test_branch_through_turn_materializes_target_from_canonical_archive(manager):
    parent = await manager.create("agent:main:main")
    target_turn_id = "turn-in-archive"
    later_turn_id = "turn-active-tail"
    entries = [
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="user",
            content="archived question",
            turn_context={"turn_id": target_turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="archived tool request",
            tool_calls=[{"id": "archived-call", "name": "lookup", "arguments": {}}],
            turn_context={"turn_id": target_turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="tool",
            content="archived tool output",
            tool_call_id="archived-call",
            turn_context={"turn_id": target_turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="archived final answer",
            turn_context={"turn_id": target_turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="user",
            content="active later question",
            turn_context={"turn_id": later_turn_id},
        ),
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="active later answer",
            turn_context={"turn_id": later_turn_id},
        ),
    ]
    for entry in entries:
        await manager._storage.append_transcript_entry(entry)
    installed = await manager.persist_compaction_result(
        parent.session_key,
        "Archived target turn summary",
        [
            {"role": "user", "content": "active later question"},
            {"role": "assistant", "content": "active later answer"},
        ],
        compaction_id="cmp-archived-through-turn",
    )
    assert installed is True
    await manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id=target_turn_id,
            session_key=parent.session_key,
            status=AgentTaskStatus.SUCCEEDED,
        )
    )
    async with manager._storage.conn.execute(
        "SELECT COUNT(*) FROM compacted_transcript_entries "
        "WHERE session_id = ? AND compaction_id = ?",
        (parent.session_id, "cmp-archived-through-turn"),
    ) as cursor:
        archived_target_count = int((await cursor.fetchone())[0])
    assert archived_target_count == 4
    assert [entry.content for entry in await manager.get_transcript(parent.session_key)] == [
        "active later question",
        "active later answer",
    ]

    child = await manager.branch(
        parent.session_key,
        "agent:main:direct:archived-through-turn",
        fork_transcript=True,
        fork_through_turn_id=target_turn_id,
    )

    assert [entry.content for entry in await manager.get_transcript(child.session_key)] == [
        "archived question",
        "archived tool request",
        "archived tool output",
        "archived final answer",
    ]
    child_page = await manager.get_canonical_transcript_page(child.session_key, limit=20)
    assert child_page.canonical_complete is True
    assert child.compaction_count == 0
    assert await manager.get_summaries(parent.session_key)
    assert await manager.get_context_states(parent.session_key)
    assert await manager.get_summaries(child.session_key) == []
    assert await manager.get_context_states(child.session_key) == []


@pytest.mark.asyncio
async def test_full_fork_rebinds_archived_terminal_outcome_for_later_nested_fork(manager):
    parent = await manager.create("agent:main:archived-outcome-parent")
    turn_id = "turn-archived-outcome"
    later_turn_id = "turn-archived-outcome-later"
    for role, content, entry_turn_id in (
        ("user", "archived question", turn_id),
        ("assistant", "archived answer", turn_id),
        ("user", "active question", later_turn_id),
    ):
        await manager._storage.append_transcript_entry(
            TranscriptEntry(
                session_id=parent.session_id,
                session_key=parent.session_key,
                role=role,
                content=content,
                turn_context={"turn_id": entry_turn_id},
            )
        )
    await manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id=turn_id,
            session_key=parent.session_key,
            status=AgentTaskStatus.SUCCEEDED,
            details={
                "turn_id": turn_id,
                "activity_snapshot": {
                    "version": 2,
                    "task_id": turn_id,
                    "turn_id": turn_id,
                    "complete": True,
                    "reasoning_utf16_length": 0,
                    "entries": [{
                        "type": "phase",
                        "id": "provider:requesting:4",
                        "order": 4,
                        "kind": "provider",
                        "phase": "requesting",
                        "at": 1_000,
                        "ended_at": 2_000,
                    }],
                },
            },
        )
    )
    assert await manager.persist_compaction_result(
        parent.session_key,
        "archived outcome summary",
        [
            {
                "role": "user",
                "content": "active question",
                "turn_context": {"turn_id": later_turn_id},
            }
        ],
        compaction_id="cmp-archived-outcome",
    )

    child = await manager.branch(
        parent.session_key,
        "agent:main:archived-outcome-child",
        fork_transcript=True,
    )
    child_page = await manager.get_canonical_transcript_page(child.session_key, limit=20)
    child_entries = child_page.entries
    child_projection = extract_fork_terminal_outcome_projection(
        child_entries[1].turn_context,
        session_id=child.session_id,
        session_key=child.session_key,
        turn_id=turn_id,
    )
    assert child_projection is not None
    assert child_projection["activity_snapshot"]["entries"][0]["order"] == 4
    await manager._storage.delete_session(parent.session_key)
    assert await manager._storage.get_agent_task(turn_id) is None

    nested = await manager.branch(
        child.session_key,
        "agent:main:archived-outcome-grandchild",
        fork_transcript=True,
        fork_through_turn_id=turn_id,
    )
    assert [entry.content for entry in await manager.get_transcript(nested.session_key)] == [
        "archived question",
        "archived answer",
    ]
    nested_entries = await manager.get_transcript(nested.session_key)
    nested_projection = extract_fork_terminal_outcome_projection(
        nested_entries[1].turn_context,
        session_id=nested.session_id,
        session_key=nested.session_key,
        turn_id=turn_id,
    )
    assert nested_projection is not None
    assert nested_projection["activity_snapshot"] == child_projection["activity_snapshot"]


@pytest.mark.asyncio
async def test_branch_through_turn_rejects_missing_active_and_incomplete_history(manager):
    parent = await manager.create("agent:main:main")
    active_turn_id = "turn-active"
    await manager._storage.append_transcript_entry(
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="user",
            content="still running",
            turn_context={"turn_id": active_turn_id},
        )
    )
    await manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id=active_turn_id,
            session_key=parent.session_key,
            status=AgentTaskStatus.RUNNING,
        )
    )

    with pytest.raises(KeyError, match="Transcript turn not found"):
        await manager.branch(
            parent.session_key,
            "agent:main:direct:missing-turn",
            fork_transcript=True,
            fork_through_turn_id="turn-without-id",
        )
    with pytest.raises(ValueError, match="active transcript turn"):
        await manager.branch(
            parent.session_key,
            "agent:main:direct:active-turn",
            fork_transcript=True,
            fork_through_turn_id=active_turn_id,
        )

    active = await manager._storage.get_agent_task(active_turn_id)
    assert active is not None
    await manager._storage.update_agent_task(
        active_turn_id,
        status=AgentTaskStatus.SUCCEEDED,
    )
    parent.compaction_count = 1
    await manager._storage.upsert_session(parent)
    with pytest.raises(ValueError, match="canonical transcript history is incomplete"):
        await manager.branch(
            parent.session_key,
            "agent:main:direct:incomplete-history",
            fork_transcript=True,
            fork_through_turn_id=active_turn_id,
        )

    for child_key in (
        "agent:main:direct:missing-turn",
        "agent:main:direct:active-turn",
        "agent:main:direct:incomplete-history",
    ):
        assert await manager.get_session(child_key) is None


@pytest.mark.asyncio
async def test_branch_rejects_conflicting_history_anchors(manager):
    await manager.create("agent:main:main")

    with pytest.raises(ValueError, match="mutually exclusive"):
        await manager.branch(
            "agent:main:main",
            "agent:main:direct:conflicting-anchor",
            fork_transcript=True,
            fork_before_message_id="message-1",
            fork_through_turn_id="turn-1",
        )

    assert await manager.get_session("agent:main:direct:conflicting-anchor") is None


@pytest.mark.asyncio
async def test_branch_through_turn_rejects_completion_state_owned_by_another_session(manager):
    parent = await manager.create("agent:main:main")
    other = await manager.create("agent:main:other")
    turn_id = "turn-owned-elsewhere"
    await manager._storage.append_transcript_entry(
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content="copied-looking row",
            turn_context={"turn_id": turn_id},
        )
    )
    await manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id=turn_id,
            session_key=other.session_key,
            status=AgentTaskStatus.SUCCEEDED,
        )
    )

    with pytest.raises(KeyError, match="Completion state not found"):
        await manager.branch(
            parent.session_key,
            "agent:main:direct:wrong-task-owner",
            fork_transcript=True,
            fork_through_turn_id=turn_id,
        )

    assert await manager.get_session("agent:main:direct:wrong-task-owner") is None


@pytest.mark.asyncio
async def test_branch_through_turn_rejects_forged_projection_on_unrelated_session(manager):
    unrelated = await manager.create("agent:main:unrelated-projection")
    turn_id = "turn-forged-projection"
    forged_projection = build_fork_terminal_outcome_projection(
        session_id=unrelated.session_id,
        session_key=unrelated.session_key,
        turn_id=turn_id,
        task_id=turn_id,
        status="succeeded",
        started_at=10,
        finished_at=20,
        outcome={"kind": "completed", "reason": "succeeded"},
    )
    await manager._storage.append_transcript_entry(
        TranscriptEntry(
            session_id=unrelated.session_id,
            session_key=unrelated.session_key,
            role="assistant",
            content="copied projection-shaped row",
            turn_context=attach_fork_terminal_outcome_projection(
                {"turn_id": turn_id},
                forged_projection,
            ),
        )
    )

    with pytest.raises(KeyError, match="Completion state not found"):
        await manager.branch(
            unrelated.session_key,
            "agent:main:direct:forged-projection-child",
            fork_transcript=True,
            fork_through_turn_id=turn_id,
        )

    assert await manager.get_session("agent:main:direct:forged-projection-child") is None


@pytest.mark.asyncio
async def test_branch_through_turn_rejects_unscoped_row_inside_target_turn(manager):
    parent = await manager.create("agent:main:main")
    turn_id = "turn-with-ambiguous-middle"
    for role, content, turn_context in (
        ("user", "question", {"turn_id": turn_id}),
        ("tool", "unscoped tool output", None),
        ("assistant", "answer", {"turn_id": turn_id}),
    ):
        await manager._storage.append_transcript_entry(
            TranscriptEntry(
                session_id=parent.session_id,
                session_key=parent.session_key,
                role=role,
                content=content,
                turn_context=turn_context,
            )
        )
    await manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id=turn_id,
            session_key=parent.session_key,
            status=AgentTaskStatus.SUCCEEDED,
        )
    )

    with pytest.raises(ValueError, match="unscoped canonical rows"):
        await manager.branch(
            parent.session_key,
            "agent:main:direct:ambiguous-middle",
            fork_transcript=True,
            fork_through_turn_id=turn_id,
        )

    assert await manager.get_session("agent:main:direct:ambiguous-middle") is None


@pytest.mark.asyncio
async def test_branch_fork_transcript_copies_compaction_summaries(manager):
    parent = await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "kept tail", token_count=5)
    await manager._storage.save_summary(
        SessionSummary(
            session_id=parent.session_id,
            session_key="agent:main:main",
            compaction_id="cmp_branch_1",
            trigger_reason="preflight_compaction",
            summary_text="older compacted context",
            summary_payload={"user_goal": "continue the parent task"},
            summary_format="structured_v1",
            summary_source="llm",
            coverage_status="pass",
            missing_obligations=["none"],
            critical_carry_forward=["remember file path"],
            tokens_before=1000,
            tokens_after=300,
            removed_count=3,
            kept_count=1,
            chunk_count=2,
            flush_receipt_status="safe",
            covered_through_id=123,
        )
    )
    await manager.save_context_state(
        SessionContextState(
            session_id=parent.session_id,
            session_key="agent:main:main",
            provider="portable",
            state_kind="structured_summary_v1",
            payload={"user_goal": "continue the parent task"},
            covered_through_id=123,
            portable=True,
            cacheable=True,
        )
    )

    child = await manager.branch("agent:main:main", "agent:main:direct:u1", fork_transcript=True)

    assert child.forked_from_parent is True
    child_summaries = await manager.get_summaries("agent:main:direct:u1")
    assert len(child_summaries) == 1
    assert child_summaries[0].summary_text == "older compacted context"
    assert child_summaries[0].compaction_id == "cmp_branch_1"
    assert child_summaries[0].trigger_reason == "preflight_compaction"
    assert child_summaries[0].summary_payload == {"user_goal": "continue the parent task"}
    assert child_summaries[0].summary_format == "structured_v1"
    assert child_summaries[0].summary_source == "llm"
    assert child_summaries[0].coverage_status == "pass"
    assert child_summaries[0].missing_obligations == ["none"]
    assert child_summaries[0].critical_carry_forward == ["remember file path"]
    assert child_summaries[0].tokens_before == 1000
    assert child_summaries[0].tokens_after == 300
    assert child_summaries[0].removed_count == 3
    assert child_summaries[0].kept_count == 1
    assert child_summaries[0].chunk_count == 2
    assert child_summaries[0].flush_receipt_status == "safe"
    assert child_summaries[0].covered_through_id == 123
    assert child_summaries[0].session_id == child.session_id
    assert child_summaries[0].session_key == "agent:main:direct:u1"
    child_states = await manager.get_context_states("agent:main:direct:u1")
    assert len(child_states) == 1
    assert child_states[0].session_id == child.session_id
    assert child_states[0].session_key == "agent:main:direct:u1"
    assert child_states[0].payload == {"user_goal": "continue the parent task"}
    assert child_states[0].portable is True
    assert child_states[0].cacheable is True


@pytest.mark.asyncio
async def test_branch_fork_transcript_copies_compacted_archive(manager):
    await manager.create("agent:main:main")
    for index in range(4):
        await manager.append_message("agent:main:main", "user", f"msg {index}", token_count=5)
    await manager.persist_compaction_result(
        "agent:main:main",
        "short summary",
        [{"role": "assistant", "content": "latest reply"}],
        compaction_id="cmp_branch_archive",
        trigger_reason="agent_inline_overflow",
    )
    parent_canonical = [
        entry.content for entry in await manager.get_canonical_transcript("agent:main:main")
    ]

    child = await manager.branch("agent:main:main", "agent:main:direct:u1", fork_transcript=True)

    assert child.parent_session_key == "agent:main:main"
    assert [entry.content for entry in await manager.get_transcript("agent:main:direct:u1")] == [
        "latest reply"
    ]
    assert [
        entry.content for entry in await manager.get_canonical_transcript("agent:main:direct:u1")
    ] == parent_canonical
    child_summaries = await manager.get_summaries("agent:main:direct:u1")
    assert child_summaries[0].compaction_id == "cmp_branch_archive"
    assert child_summaries[0].trigger_reason == "agent_inline_overflow"
    child_page = await manager.get_canonical_transcript_page(
        "agent:main:direct:u1",
        limit=10,
    )
    assert child_page.canonical_complete is True


@pytest.mark.asyncio
async def test_full_branch_preserves_incomplete_parent_compaction_evidence(manager):
    parent = await manager.create("agent:main:main")
    for index in range(4):
        await manager.append_message(
            parent.session_key,
            "user",
            f"message {index}",
            token_count=5,
        )
    await manager.persist_compaction_result(
        parent.session_key,
        "legacy summary",
        [{"role": "user", "content": "message 3"}],
        compaction_id="cmp-incomplete-full-fork",
    )
    await manager._storage.conn.execute(
        "DELETE FROM session_summaries WHERE session_id = ?",
        (parent.session_id,),
    )
    await manager._storage.conn.execute(
        "DELETE FROM compacted_transcript_entries WHERE session_id = ?",
        (parent.session_id,),
    )
    await manager._storage.conn.commit()

    parent_page = await manager.get_canonical_transcript_page(parent.session_key, limit=10)
    assert parent_page.canonical_complete is False

    child = await manager.branch(
        parent.session_key,
        "agent:main:direct:full-incomplete",
        fork_transcript=True,
    )
    child_page = await manager.get_canonical_transcript_page(child.session_key, limit=10)

    assert child.compaction_count == 1
    assert [entry.content for entry in child_page.entries] == ["message 3"]
    assert child_page.canonical_complete is False


@pytest.mark.asyncio
async def test_full_branch_uses_current_parent_compaction_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db_path = tmp_path / "branch-current-coverage.db"
    writer_storage = SessionStorage(str(db_path))
    await writer_storage.connect()
    writer = SessionManager(writer_storage, inject_time_prefix=False)
    parent = await writer.create("agent:main:main")
    for index in range(4):
        await writer.append_message(parent.session_key, "user", f"message {index}")

    reader_storage = SessionStorage(str(db_path))
    await reader_storage.connect()
    reader = SessionManager(reader_storage, inject_time_prefix=False)
    original_coverage = reader_storage.get_canonical_transcript_coverage
    compaction_injected = False

    async def coverage_after_incomplete_compaction(session_id: str):
        nonlocal compaction_injected
        if not compaction_injected:
            compaction_injected = True
            await writer.persist_compaction_result(
                parent.session_key,
                "legacy summary",
                [{"role": "user", "content": "message 3"}],
                compaction_id="cmp-branch-current-coverage",
            )
            await writer_storage.conn.execute(
                "DELETE FROM session_summaries WHERE session_id = ?",
                (parent.session_id,),
            )
            await writer_storage.conn.execute(
                "DELETE FROM compacted_transcript_entries WHERE session_id = ?",
                (parent.session_id,),
            )
            await writer_storage.conn.commit()
        return await original_coverage(session_id)

    monkeypatch.setattr(
        reader_storage,
        "get_canonical_transcript_coverage",
        coverage_after_incomplete_compaction,
    )
    try:
        child = await reader.branch(
            parent.session_key,
            "agent:main:direct:concurrent-incomplete",
            fork_transcript=True,
        )
        child_page = await reader.get_canonical_transcript_page(child.session_key, limit=10)
    finally:
        await reader_storage.close()
        await writer_storage.close()

    assert compaction_injected is True
    assert child.compaction_count == 1
    assert [entry.content for entry in child_page.entries] == ["message 3"]
    assert child_page.canonical_complete is False


@pytest.mark.asyncio
async def test_full_branch_blocks_compaction_after_parent_transcript_snapshot(
    manager,
    monkeypatch: pytest.MonkeyPatch,
):
    parent = await manager.create("agent:main:main")
    for index in range(4):
        await manager.append_message(parent.session_key, "user", f"message {index}")

    mutation_lock = asyncio.Lock()
    transcript_read = asyncio.Event()
    compaction_attempted = asyncio.Event()
    original_get_transcript = manager._storage.get_transcript
    parent_snapshot_seen = False

    async def get_transcript_then_release_compaction(session_id: str, *args: Any, **kwargs: Any):
        nonlocal parent_snapshot_seen
        entries = await original_get_transcript(session_id, *args, **kwargs)
        if session_id == parent.session_id and not parent_snapshot_seen:
            parent_snapshot_seen = True
            transcript_read.set()
            await compaction_attempted.wait()
        return entries

    monkeypatch.setattr(manager._storage, "get_transcript", get_transcript_then_release_compaction)

    async def compact_after_parent_read() -> None:
        await transcript_read.wait()
        compaction_attempted.set()
        async with mutation_lock:
            await manager.persist_compaction_result(
                parent.session_key,
                "concurrent summary",
                [{"role": "user", "content": "message 3"}],
                compaction_id="cmp-concurrent-full-fork",
            )

    compaction_task = asyncio.create_task(compact_after_parent_read())
    try:
        child = await manager.branch(
            parent.session_key,
            "agent:main:direct:locked-full-fork",
            fork_transcript=True,
            mutation_context=lambda: mutation_lock,
        )
        await compaction_task
    finally:
        if not compaction_task.done():
            compaction_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await compaction_task

    child_page = await manager.get_canonical_transcript_page(child.session_key, limit=10)

    assert parent_snapshot_seen is True
    assert [entry.content for entry in child_page.entries] == [
        "message 0",
        "message 1",
        "message 2",
        "message 3",
    ]
    assert len({entry.message_id for entry in child_page.entries}) == 4
    assert child_page.canonical_complete is True


@pytest.mark.asyncio
async def test_prefix_branch_remains_incomplete_when_parent_archive_is_incomplete(manager):
    parent = await manager.create("agent:main:main")
    entries = []
    for index in range(5):
        entries.append(
            await manager.append_message(
                parent.session_key,
                "user",
                f"message {index}",
                token_count=5,
            )
        )
    await manager.persist_compaction_result(
        parent.session_key,
        "legacy summary",
        [
            {"role": "user", "content": "message 2"},
            {"role": "user", "content": "message 3"},
            {"role": "user", "content": "message 4"},
        ],
        compaction_id="cmp-incomplete-prefix-fork",
    )
    await manager._storage.conn.execute(
        "DELETE FROM compacted_transcript_entries WHERE session_id = ?",
        (parent.session_id,),
    )
    await manager._storage.conn.commit()

    child = await manager.branch(
        parent.session_key,
        "agent:main:direct:prefix-incomplete",
        fork_transcript=True,
        fork_before_message_id=entries[4].message_id,
    )
    child_page = await manager.get_canonical_transcript_page(child.session_key, limit=10)

    assert child.compaction_count == 1
    assert [entry.content for entry in child_page.entries] == ["message 2", "message 3"]
    assert child_page.canonical_complete is False


@pytest.mark.asyncio
async def test_prefix_branch_from_complete_parent_has_complete_raw_transcript(manager):
    parent = await manager.create("agent:main:main")
    entries = []
    for index in range(5):
        entries.append(
            await manager.append_message(
                parent.session_key,
                "user",
                f"message {index}",
                token_count=5,
            )
        )
    await manager.persist_compaction_result(
        parent.session_key,
        "complete summary",
        [
            {"role": "user", "content": "message 2"},
            {"role": "user", "content": "message 3"},
            {"role": "user", "content": "message 4"},
        ],
        compaction_id="cmp-complete-prefix-fork",
    )

    child = await manager.branch(
        parent.session_key,
        "agent:main:direct:prefix-complete",
        fork_transcript=True,
        fork_before_message_id=entries[4].message_id,
    )
    child_page = await manager.get_canonical_transcript_page(child.session_key, limit=10)

    assert child.compaction_count == 0
    assert [entry.content for entry in child_page.entries] == [
        "message 0",
        "message 1",
        "message 2",
        "message 3",
    ]
    assert child_page.canonical_complete is True


@pytest.mark.asyncio
async def test_context_state_roundtrip_and_invalidate(manager):
    node = await manager.create("agent:main:main")
    state = await manager.save_context_state(
        SessionContextState(
            session_id=node.session_id,
            session_key="agent:main:main",
            provider="portable",
            model=None,
            state_kind="structured_summary_v1",
            payload={"current_status": "summary state"},
            covered_through_id=42,
            portable=True,
            cacheable=True,
        )
    )

    loaded = await manager.get_context_states("agent:main:main")

    assert state.id is not None
    assert len(loaded) == 1
    assert loaded[0].payload == {"current_status": "summary state"}
    assert loaded[0].portable is True
    assert loaded[0].cacheable is True
    assert loaded[0].valid is True

    invalidated = await manager.invalidate_context_states(
        "agent:main:main",
        state_kind="structured_summary_v1",
        reason="provider switched",
    )

    assert invalidated == 1
    assert await manager.get_context_states("agent:main:main") == []
    invalid = await manager.get_context_states("agent:main:main", valid_only=False)
    assert len(invalid) == 1
    assert invalid[0].valid is False
    assert invalid[0].invalid_reason == "provider switched"


@pytest.mark.asyncio
async def test_storage_migrates_legacy_summary_metadata_columns(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                compaction_index INTEGER NOT NULL DEFAULT 0,
                summary_text TEXT NOT NULL,
                covered_through_id INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO session_summaries (
                session_id, session_key, compaction_index, summary_text,
                covered_through_id, created_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("legacy-session", "agent:main:legacy", 0, "legacy summary", 7, 123, 1),
        )

    storage = SessionStorage(str(db_path))
    await storage.connect()
    try:
        async with storage.conn.execute("PRAGMA table_info(session_summaries)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        assert {
            "compaction_id",
            "trigger_reason",
            "summary_payload",
            "summary_format",
            "summary_source",
            "coverage_status",
            "missing_obligations",
            "critical_carry_forward",
            "tokens_before",
            "tokens_after",
            "removed_count",
            "kept_count",
            "chunk_count",
            "flush_receipt_status",
        }.issubset(columns)

        summary = await storage.get_latest_summary("legacy-session")
        assert summary is not None
        assert summary.summary_text == "legacy summary"
        assert summary.summary_payload is None
        assert summary.summary_format == "text"
        assert summary.summary_source == "unknown"
        assert summary.coverage_status == "unknown"
        assert summary.missing_obligations is None
        assert summary.critical_carry_forward is None
        assert summary.compaction_id is None
        assert summary.trigger_reason is None
        assert summary.tokens_before is None
        assert summary.tokens_after is None
        assert summary.removed_count == 0
        assert summary.kept_count == 0
        assert summary.chunk_count == 0
        assert summary.flush_receipt_status == "unknown"
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_legacy_summary_database_compacts_and_replays_after_restart(tmp_path):
    """A pre-metadata summary survives upgrade, replacement, and a cold restart."""
    db_path = tmp_path / "legacy-summary-upgrade.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE session_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                session_key TEXT NOT NULL,
                compaction_index INTEGER NOT NULL DEFAULT 0,
                summary_text TEXT NOT NULL,
                covered_through_id INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                schema_version INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            INSERT INTO session_summaries (
                session_id, session_key, compaction_index, summary_text,
                covered_through_id, created_at, schema_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-session-placeholder",
                "agent:main:legacy-upgrade",
                0,
                "legacy checkpoint before metadata columns",
                0,
                123,
                1,
            ),
        )

    storage = SessionStorage(str(db_path))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create("agent:main:legacy-upgrade")
    await storage.conn.execute(
        "UPDATE session_summaries SET session_id = ? WHERE session_key = ?",
        (node.session_id, node.session_key),
    )
    await storage.conn.commit()
    for index in range(20):
        await manager.append_message(
            node.session_key,
            "user",
            f"legacy-upgrade message {index} " + ("x" * 500),
            token_count=200,
        )

    result = await manager.compact_with_result(
        node.session_key,
        context_window_tokens=1000,
        compaction_id="cmp-legacy-upgrade-replacement",
    )
    assert result.removed_count > 0
    assert result.summary_payload is not None
    assert result.summary_payload["source_coverage"]["replaces_prior_context"] is True
    replacement_compaction_id = "cmp-legacy-upgrade-replacement"
    await storage.close()

    reopened = SessionStorage(str(db_path))
    await reopened.connect()
    try:
        restarted = SessionManager(reopened, inject_time_prefix=False)
        summaries = await restarted.get_summaries(node.session_key)
        states = await restarted.get_context_states(node.session_key)

        assert len(summaries) == 2
        assert summaries[0].summary_format == "text"
        assert summaries[0].summary_payload is None
        assert summaries[1].summary_payload is not None
        assert summaries[1].summary_payload["source_coverage"]["replaces_prior_context"] is True

        active_records = build_compaction_context_records(
            context_states=states,
            summaries=summaries,
        )
        assert len(active_records) == 1
        assert active_records[0].compaction_id == replacement_compaction_id
        assert active_records[0].covered_through_id == summaries[1].covered_through_id
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_storage_adds_compaction_lookup_index_to_existing_database(tmp_path):
    db_path = tmp_path / "existing-compaction-index.db"
    initial = SessionStorage(str(db_path))
    await initial.connect()
    await initial.close()

    # Simulate a database created before the coverage lookup index existed.
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_compacted_transcript_session_compaction")

    upgraded = SessionStorage(str(db_path))
    await upgraded.connect()
    try:
        async with upgraded.conn.execute(
            "PRAGMA index_list(compacted_transcript_entries)"
        ) as cur:
            index_names = {str(row[1]) for row in await cur.fetchall()}
        assert "idx_compacted_transcript_session_compaction" in index_names

        async with upgraded.conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT COUNT(*) FROM compacted_transcript_entries "
            "WHERE session_id = ? AND compaction_id = ?",
            ("session", "compaction"),
        ) as cur:
            query_plan = [str(row[3]) for row in await cur.fetchall()]
        assert any(
            "idx_compacted_transcript_session_compaction" in detail
            for detail in query_plan
        )
    finally:
        await upgraded.close()


@pytest.mark.asyncio
async def test_storage_migrates_session_context_state_table(tmp_path):
    db_path = tmp_path / "legacy-context-state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (session_key TEXT PRIMARY KEY)")

    storage = SessionStorage(str(db_path))
    await storage.connect()
    try:
        async with storage.conn.execute("PRAGMA table_info(session_context_states)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        assert {
            "session_id",
            "session_key",
            "provider",
            "model",
            "state_kind",
            "payload",
            "covered_through_id",
            "created_at",
            "expires_at",
            "portable",
            "cacheable",
            "valid",
            "invalid_reason",
        }.issubset(columns)
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_branch_fork_skipped_if_over_budget(manager):
    await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "msg", token_count=1000)
    child = await manager.branch(
        "agent:main:main", "agent:main:direct:u1", fork_transcript=True, max_fork_tokens=10
    )
    assert child.forked_from_parent is False


@pytest.mark.asyncio
async def test_branch_fork_budget_counts_compaction_summaries(manager):
    parent = await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "kept", token_count=1)
    await manager._storage.save_summary(
        SessionSummary(
            session_id=parent.session_id,
            session_key="agent:main:main",
            summary_text="x" * 400,
        )
    )

    child = await manager.branch(
        "agent:main:main",
        "agent:main:direct:u1",
        fork_transcript=True,
        max_fork_tokens=10,
    )

    assert child.forked_from_parent is False
    assert await manager.get_transcript("agent:main:direct:u1") == []
    assert await manager.get_summaries("agent:main:direct:u1") == []


@pytest.mark.asyncio
async def test_compact_no_op_small_context(manager):
    await manager.create("agent:main:main")
    await manager.append_message("agent:main:main", "user", "hi", token_count=5)
    summary = await manager.compact("agent:main:main", context_window_tokens=100_000)
    assert summary == ""


def test_compaction_payload_suppresses_split_goal_marker_around_tool_pair() -> None:
    raw_segments = [
        {"type": "text", "text": "NO_"},
        {
            "type": "tool_use",
            "tool_use_id": "call-split",
            "name": "read_status",
            "input": {"id": "job-split"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-split",
            "name": "read_status",
            "result": "pending",
            "is_error": False,
        },
        {"type": "text", "text": "REPLY"},
    ]
    entry = TranscriptEntry(
        session_id="session-split",
        session_key="agent:main:split",
        role="assistant",
        content="NO_REPLY",
        tool_calls=raw_segments,
        turn_context={"intent": "goal_continuation"},
    )

    payload = session_manager_module._compaction_entry_payloads([entry])[0]

    assert payload["content"] == ""
    assert payload["tool_calls"] == [raw_segments[1], raw_segments[2]]
    assert payload["turn_context"] == {"intent": "goal_continuation"}
    assert "NO_" not in str(payload["tool_calls"])
    assert "REPLY" not in str(payload["tool_calls"])
    assert entry.content == "NO_REPLY"
    assert entry.tool_calls == raw_segments


@pytest.mark.asyncio
async def test_compaction_payload_sanitizes_silent_replies_without_rewriting_storage(
    manager,
    monkeypatch,
):
    node = await manager.create("agent:main:main")
    raw_segments = [
        {"type": "text", "text": "NO_REPLY\nWaiting for an external update."},
        {
            "type": "tool_use",
            "tool_use_id": "call-status",
            "name": "read_status",
            "input": {},
        },
        {"type": "text", "text": "HEARTBEAT_OK"},
    ]
    goal_entry, expected_epoch = await manager.prepare_message(
        node.session_key,
        "assistant",
        "HEARTBEAT_OK\nWaiting for an external update.\nNO_REPLY",
        tool_calls=raw_segments,
        turn_context={"intent": "goal_continuation"},
    )
    await manager._storage.append_transcript_entry(
        goal_entry,
        expected_epoch=expected_epoch,
    )
    unattributed_entry, expected_epoch = await manager.prepare_message(
        node.session_key,
        "assistant",
        "NO_REPLY\nQuoted protocol example.",
        tool_calls=[
            {"type": "text", "text": "HEARTBEAT_OK\nQuoted segment example."}
        ],
        turn_context={},
    )
    await manager._storage.append_transcript_entry(
        unattributed_entry,
        expected_epoch=expected_epoch,
    )
    exact_entry, expected_epoch = await manager.prepare_message(
        node.session_key,
        "assistant",
        "NO_REPLY",
        turn_context={},
    )
    await manager._storage.append_transcript_entry(
        exact_entry,
        expected_epoch=expected_epoch,
    )

    captured_payloads: list[dict[str, Any]] = []

    async def capture_compaction_request(request):
        captured_payloads.extend(request.entries)
        return CompactionResult(
            summary="",
            kept_entries=request.entries,
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
            skip_reason="within_compaction_budget",
        )

    monkeypatch.setattr(
        session_manager_module,
        "compact_context",
        capture_compaction_request,
    )
    stored_before = await manager._storage.get_transcript(node.session_id)

    await manager.compact_with_result(
        node.session_key,
        context_window_tokens=100_000,
    )

    payloads = captured_payloads

    assert len(payloads) == len(stored_before) == 3
    assert payloads[0]["content"] == "Waiting for an external update."
    assert payloads[0]["tool_calls"] == [
        {"type": "text", "text": "Waiting for an external update."},
        {
            "type": "tool_use",
            "tool_use_id": "call-status",
            "name": "read_status",
            "input": {},
        },
    ]
    assert payloads[1]["content"] == "NO_REPLY\nQuoted protocol example."
    assert payloads[1]["tool_calls"][0]["text"] == (
        "HEARTBEAT_OK\nQuoted segment example."
    )
    assert payloads[2]["content"] == ""
    assert payloads[2]["tool_calls"] is None

    stored_after = await manager._storage.get_transcript(node.session_id)
    assert [entry.content for entry in stored_after] == [
        "HEARTBEAT_OK\nWaiting for an external update.\nNO_REPLY",
        "NO_REPLY\nQuoted protocol example.",
        "NO_REPLY",
    ]
    assert stored_after[0].tool_calls == raw_segments


def test_durable_summary_replay_matches_runtime_formatter() -> None:
    summary = "portable checkpoint"
    rendered = format_compaction_summary_context([summary])

    assert rendered is not None
    assert session_manager_module._durable_summary_replay(summary) == f"{rendered}\n\n"


@pytest.mark.asyncio
async def test_compact_reduces_transcript(manager):
    await manager.create("agent:main:main")
    # Add many large messages
    for i in range(20):
        await manager.append_message("agent:main:main", "user", "x" * 500, token_count=200)
    summary = await manager.compact("agent:main:main", context_window_tokens=1000)
    assert summary != ""
    node = await manager._storage.get_session("agent:main:main")
    assert node.compaction_count == 1
    transcript = await manager.get_transcript("agent:main:main")
    assert transcript
    assert all(entry.role != "system" for entry in transcript)
    summaries = await manager._storage.get_all_summaries(node.session_id)
    assert len(summaries) == 1


@pytest.mark.asyncio
async def test_compact_with_result_returns_source_and_persists(manager):
    await manager.create("agent:main:main")
    for i in range(20):
        await manager.append_message(
            "agent:main:main",
            "user",
            f"msg {i} " + ("x" * 500),
            token_count=200,
        )
    original_contents = [entry.content for entry in await manager.get_transcript("agent:main:main")]

    result = await manager.compact_with_result("agent:main:main", context_window_tokens=1000)

    assert result.summary
    assert result.summary_source == "fallback"
    node = await manager._storage.get_session("agent:main:main")
    assert node.compaction_count == 1
    transcript = await manager.get_transcript("agent:main:main")
    assert transcript
    assert all(entry.role != "system" for entry in transcript)
    summaries = await manager.get_summaries("agent:main:main")
    assert [summary.summary_text for summary in summaries] == [result.summary]
    assert summaries[0].summary_format == "structured_v1"
    assert summaries[0].summary_source == result.summary_source
    assert summaries[0].coverage_status in {"unknown", "pass", "pass_with_backfill"}
    assert summaries[0].summary_payload is not None
    assert summaries[0].summary_payload["schema_version"] == 1
    assert summaries[0].compaction_id
    assert summaries[0].tokens_before == result.tokens_before
    assert summaries[0].tokens_after == result.tokens_after
    assert summaries[0].removed_count == result.removed_count
    assert summaries[0].kept_count == len(result.kept_entries)
    assert summaries[0].chunk_count == result.chunks_processed
    canonical_contents = [
        entry.content for entry in await manager.get_canonical_transcript("agent:main:main")
    ]
    assert canonical_contents == original_contents
    assert [entry.content for entry in transcript] == original_contents[-len(transcript) :]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "expected_protected_recent_messages"),
    [
        pytest.param(
            CompactionConfig(protected_recent_messages=1),
            2,
            id="explicit-protection",
        ),
        pytest.param(
            CompactionConfig(compaction_profile="coding"),
            12,
            id="profile-protection",
        ),
    ],
)
async def test_compact_with_result_recomputes_bound_to_tail_protection(
    manager,
    monkeypatch,
    config,
    expected_protected_recent_messages,
):
    node = await manager.create("agent:main:protected-boundary")
    await manager.append_message(node.session_key, "user", "old question")
    await manager.append_message(node.session_key, "assistant", "old answer")
    active_user = await manager.append_message(node.session_key, "user", "active request")
    await manager.append_message(node.session_key, "user", "queued request")
    observed: dict[str, Any] = {}

    async def compact_context_spy(request):
        observed["protected_recent_messages"] = request.config.protected_recent_messages
        return CompactionResult(
            summary="",
            kept_entries=request.entries,
            removed_count=0,
            chunks_processed=0,
            summary_source="skipped",
        )

    monkeypatch.setattr(session_manager_module, "compact_context", compact_context_spy)

    result = await manager.compact_with_result(
        node.session_key,
        context_window_tokens=1_000,
        config=config,
        protected_boundary_message_id=active_user.message_id,
    )

    assert result.removed_count == 0
    assert observed["protected_recent_messages"] == expected_protected_recent_messages


@pytest.mark.asyncio
async def test_compact_with_result_missing_protected_boundary_fails_closed(
    manager,
    monkeypatch,
):
    node = await manager.create("agent:main:missing-protected-boundary")
    entry = await manager.append_message(node.session_key, "user", "active request")

    async def unexpected_compact_context(request):  # noqa: ARG001
        raise AssertionError("compaction must not run without the protected boundary")

    monkeypatch.setattr(
        session_manager_module,
        "compact_context",
        unexpected_compact_context,
    )

    result = await manager.compact_with_result(
        node.session_key,
        context_window_tokens=1_000,
        protected_boundary_message_id="missing-message-id",
    )

    assert result.summary == ""
    assert result.removed_count == 0
    assert result.skip_reason == "protected_boundary_missing"
    transcript = await manager.get_transcript(node.session_key)
    assert [candidate.message_id for candidate in transcript] == [entry.message_id]
    current = await manager.get_session(node.session_key)
    assert current is not None
    assert current.compaction_count == 0


def test_compaction_singleflight_target_fingerprint_is_credential_aware():
    first = CompactionConfig(provider="provider-a", model="model-a", api_key="secret-a")
    same_deployment_and_credentials = CompactionConfig(
        provider="provider-a",
        model="model-a",
        api_key="secret-a",
    )
    other_credentials = CompactionConfig(
        provider="provider-a",
        model="model-a",
        api_key="secret-b",
    )
    other_model = CompactionConfig(provider="provider-a", model="model-b", api_key="secret-a")

    first_fingerprint = session_manager_module._compaction_target_fingerprint(first)

    assert (
        session_manager_module._compaction_target_fingerprint(same_deployment_and_credentials)
        == first_fingerprint
    )
    assert (
        session_manager_module._compaction_target_fingerprint(other_credentials)
        != first_fingerprint
    )
    assert (
        session_manager_module._compaction_target_fingerprint(other_model)
        != first_fingerprint
    )
    assert "secret-a" not in first_fingerprint
    assert "secret-b" not in session_manager_module._compaction_target_fingerprint(
        other_credentials
    )


@pytest.mark.asyncio
async def test_compact_with_result_singleflight_shares_generation_and_commit(
    manager,
    monkeypatch,
):
    await manager.create("agent:main:main")
    for i in range(20):
        await manager.append_message(
            "agent:main:main",
            "user",
            f"singleflight msg {i} " + ("x" * 500),
            token_count=200,
        )

    generation_started = asyncio.Event()
    release_generation = asyncio.Event()
    waiter_joined = asyncio.Event()
    generation_calls = 0
    original_compact_context = session_manager_module.compact_context
    original_acquire = session_manager_module._acquire_compaction_singleflight

    async def slow_compact_context(request):
        nonlocal generation_calls
        generation_calls += 1
        generation_started.set()
        await release_generation.wait()
        return await original_compact_context(request)

    def track_acquire(*args, **kwargs):
        flight, is_owner = original_acquire(*args, **kwargs)
        if not is_owner:
            waiter_joined.set()
        return flight, is_owner

    monkeypatch.setattr(session_manager_module, "compact_context", slow_compact_context)
    monkeypatch.setattr(
        session_manager_module,
        "_acquire_compaction_singleflight",
        track_acquire,
    )
    shared_config = CompactionConfig()

    owner = asyncio.create_task(
        manager.compact_with_result(
            "agent:main:main",
            context_window_tokens=1000,
            config=shared_config,
        )
    )
    await asyncio.wait_for(generation_started.wait(), timeout=1)
    waiter = asyncio.create_task(
        manager.compact_with_result(
            "agent:main:main",
            context_window_tokens=1000,
            config=shared_config,
        )
    )
    await asyncio.wait_for(waiter_joined.wait(), timeout=1)
    release_generation.set()

    owner_result, waiter_result = await asyncio.gather(owner, waiter)

    assert owner_result is waiter_result
    assert generation_calls == 1
    assert shared_config.operation_id is None
    assert shared_config.deadline_at_monotonic is None
    node = await manager.get_session("agent:main:main")
    assert node is not None
    assert node.compaction_count == 1
    assert len(await manager.get_summaries("agent:main:main")) == 1


@pytest.mark.asyncio
async def test_compact_with_result_singleflight_waiter_cancel_does_not_cancel_owner(
    manager,
    monkeypatch,
):
    await manager.create("agent:main:main")
    for i in range(20):
        await manager.append_message(
            "agent:main:main",
            "user",
            f"cancel waiter msg {i} " + ("x" * 500),
            token_count=200,
        )

    generation_started = asyncio.Event()
    release_generation = asyncio.Event()
    waiter_joined = asyncio.Event()
    generation_calls = 0
    original_compact_context = session_manager_module.compact_context
    original_acquire = session_manager_module._acquire_compaction_singleflight

    async def slow_compact_context(request):
        nonlocal generation_calls
        generation_calls += 1
        generation_started.set()
        await release_generation.wait()
        return await original_compact_context(request)

    def track_acquire(*args, **kwargs):
        flight, is_owner = original_acquire(*args, **kwargs)
        if not is_owner:
            waiter_joined.set()
        return flight, is_owner

    monkeypatch.setattr(session_manager_module, "compact_context", slow_compact_context)
    monkeypatch.setattr(
        session_manager_module,
        "_acquire_compaction_singleflight",
        track_acquire,
    )

    owner = asyncio.create_task(
        manager.compact_with_result("agent:main:main", context_window_tokens=1000)
    )
    await asyncio.wait_for(generation_started.wait(), timeout=1)
    waiter = asyncio.create_task(
        manager.compact_with_result("agent:main:main", context_window_tokens=1000)
    )
    await asyncio.wait_for(waiter_joined.wait(), timeout=1)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not owner.done()

    release_generation.set()
    result = await owner

    assert result.summary
    assert generation_calls == 1
    node = await manager.get_session("agent:main:main")
    assert node is not None
    assert node.compaction_count == 1
    assert len(await manager.get_summaries("agent:main:main")) == 1


@pytest.mark.asyncio
async def test_compact_with_result_skips_rewrite_when_transcript_changes(manager):
    await manager.create("agent:main:main")
    for i in range(20):
        await manager.append_message(
            "agent:main:main",
            "user",
            f"msg {i} " + ("x" * 500),
            token_count=200,
        )
    original_contents = [entry.content for entry in await manager.get_transcript("agent:main:main")]
    context_entries = 0

    @contextlib.asynccontextmanager
    async def mutation_context():
        nonlocal context_entries
        context_entries += 1
        if context_entries == 2:
            await manager.append_message(
                "agent:main:main",
                "user",
                "late queued followup",
                token_count=3,
            )
        yield

    result = await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=1000,
        mutation_context=mutation_context,
    )

    assert result.summary == ""
    assert result.summary_source == "skipped"
    assert result.skip_reason == "stale_preimage"
    assert result.removed_count == 0
    node = await manager._storage.get_session("agent:main:main")
    assert node.compaction_count == 0
    assert await manager.get_summaries("agent:main:main") == []
    transcript = await manager.get_transcript("agent:main:main")
    assert [entry.content for entry in transcript] == original_contents + ["late queued followup"]


@pytest.mark.asyncio
async def test_compact_with_result_marks_unsafe_receipt_as_degraded_forensic(manager):
    await manager.create("agent:main:main")
    for i in range(20):
        await manager.append_message(
            "agent:main:main",
            "user",
            f"unsafe msg {i} " + ("x" * 500),
            token_count=200,
        )
    original_contents = [entry.content for entry in await manager.get_transcript("agent:main:main")]

    result = await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=1000,
        flush_receipt_status="unsafe",
    )

    assert result.removed_count > 0
    summaries = await manager.get_summaries("agent:main:main")
    assert summaries[0].flush_receipt_status == "degraded_forensic"
    canonical_contents = [
        entry.content for entry in await manager.get_canonical_transcript("agent:main:main")
    ]
    assert canonical_contents == original_contents


@pytest.mark.asyncio
async def test_degraded_compaction_preimage_can_be_listed_for_repair(manager):
    await manager.create("agent:main:main")
    for i in range(20):
        await manager.append_message(
            "agent:main:main",
            "user",
            f"repair msg {i} " + ("x" * 500),
            token_count=200,
        )

    await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=1000,
        flush_receipt_status="degraded_forensic",
    )

    pending = await manager.list_degraded_compactions(agent_id="main")
    assert len(pending) == 1
    assert pending[0].flush_receipt_status == "degraded_forensic"
    preimage = await manager.get_compaction_preimage(pending[0])
    assert preimage
    assert preimage[0].content.startswith("repair msg 0")
    await manager.mark_compaction_repair_status(pending[0], "repaired")
    assert await manager.list_degraded_compactions(agent_id="main") == []


@pytest.mark.asyncio
async def test_compaction_flush_status_can_be_backfilled_by_compaction_id(manager):
    await manager.create("agent:main:main")
    for i in range(20):
        await manager.append_message(
            "agent:main:main",
            "user",
            f"background flush msg {i} " + ("x" * 500),
            token_count=200,
        )

    await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=1000,
        compaction_id="cmp-bg-flush",
        flush_receipt_status="degraded_forensic",
    )

    updated = await manager.mark_compaction_flush_receipt_status(
        "agent:main:main",
        "cmp-bg-flush",
        "safe",
    )

    assert updated == 1
    summaries = await manager.get_summaries("agent:main:main")
    assert summaries[0].flush_receipt_status == "safe"
    assert await manager.list_degraded_compactions(agent_id="main") == []


@pytest.mark.asyncio
async def test_noop_memory_flush_compaction_status_does_not_enter_repair_queue(manager):
    await manager.create("agent:main:main")
    for i in range(20):
        await manager.append_message(
            "agent:main:main",
            "user",
            f"noop msg {i} " + ("x" * 500),
            token_count=200,
        )

    result = await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=1000,
        flush_receipt_status="noop_no_memory",
    )

    assert result.removed_count > 0
    summaries = await manager.get_summaries("agent:main:main")
    assert summaries[0].flush_receipt_status == "noop_no_memory"
    assert await manager.list_degraded_compactions(agent_id="main") == []


@pytest.mark.asyncio
async def test_archive_only_memory_flush_compaction_status_does_not_enter_repair_queue(manager):
    await manager.create("agent:main:main")
    for i in range(20):
        await manager.append_message(
            "agent:main:main",
            "user",
            f"archive msg {i} " + ("x" * 500),
            token_count=200,
        )

    result = await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=1000,
        flush_receipt_status="archive_only",
    )

    assert result.removed_count > 0
    summaries = await manager.get_summaries("agent:main:main")
    assert summaries[0].flush_receipt_status == "archive_only"
    assert await manager.list_degraded_compactions(agent_id="main") == []


@pytest.mark.asyncio
async def test_compact_with_result_summarizes_completed_tool_round(manager):
    await manager.create("agent:main:main")
    await manager.append_message(
        "agent:main:main",
        "user",
        (
            "Goal: finish continuity work.\n"
            "Constraint: do not enable coverage blocking by default.\n"
            "Keep src/opensquilla/session/models.py and docs/Long Task Report.md."
        ),
        token_count=500,
    )
    await manager.append_message(
        "agent:main:main",
        "assistant",
        "I will preserve those constraints and continue the continuity work.",
        token_count=500,
    )
    await manager.append_message(
        "agent:main:main",
        "user",
        "Run the focused session test now.",
        token_count=20,
    )
    await manager.append_message(
        "agent:main:main",
        "assistant",
        "Next I will run uv run pytest tests/test_session/test_manager.py.",
        tool_calls=[{"id": "call_exec_1", "type": "function"}],
        token_count=250,
    )
    await manager.append_message(
        "agent:main:main",
        "tool",
        (
            "Command failed: uv run pytest tests/test_session/test_manager.py\n"
            "Exit code 1\n"
            "Error: missing summary_payload column"
        ),
        tool_call_id="call_exec_1",
        token_count=250,
    )
    for i in range(8):
        await manager.append_message(
            "agent:main:main",
            "assistant",
            f"recent tail {i}",
            token_count=20,
        )

    result = await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=1_200,
        config=CompactionConfig(safety_margin=1.2),
    )

    assert result.removed_count > 0
    summaries = await manager.get_summaries("agent:main:main")
    summary = summaries[0]
    assert summary.summary_text == result.summary
    assert summary.summary_format == "structured_v1"
    assert summary.summary_payload is not None
    assert summary.coverage_status == "pass"
    assert summary.missing_obligations == []
    assert summary.critical_carry_forward == []
    payload = summary.summary_payload
    assert payload is not None
    assert "src/opensquilla/session/models.py" in str(payload)
    assert {"id": "call_exec_1"} in payload["tool_results_to_remember"]
    assert any(
        "missing summary_payload column" in failure.get("detail", "")
        for failure in payload["known_failures"]
    )
    assert any(
        "pytest tests/test_session/test_manager.py" in command
        for command in payload["executed_commands_and_tests"]
    )
    assert "call_exec_1" not in payload["pending_tool_and_approval_ids"]
    transcript = await manager.get_transcript("agent:main:main")
    assert not any(entry.tool_call_id == "call_exec_1" for entry in transcript)
    assert not any(
        entry.tool_calls and entry.tool_calls[0]["id"] == "call_exec_1"
        for entry in transcript
    )


@pytest.mark.asyncio
async def test_compact_with_result_strict_coverage_installs_verified_backfill(manager):
    node = await manager.create("agent:main:main")
    late_critical_path = "src/opensquilla/session/critical_continuity.py"
    await manager.append_message(
        "agent:main:main",
        "user",
        "Goal: preserve strict continuity. " + ("padding " * 40) + late_critical_path,
        token_count=650,
    )
    for index in range(4):
        await manager.append_message(
            "agent:main:main",
            "assistant",
            f"recent tail {index}",
            token_count=20,
        )
    original_transcript = await manager.get_transcript("agent:main:main")

    result = await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=300,
        config=CompactionConfig(safety_margin=1.0, coverage_blocking=True),
    )

    assert result.removed_count > 0
    assert result.skip_reason is None
    assert result.coverage_status == "pass"
    assert result.missing_obligations == []
    assert late_critical_path in str(result.summary_payload)
    assert len(await manager.get_transcript("agent:main:main")) < len(
        original_transcript
    )
    summaries = await manager.get_summaries("agent:main:main")
    assert len(summaries) == 1
    assert late_critical_path in str(summaries[0].summary_payload)
    assert await manager.get_context_states("agent:main:main")
    current_node = await manager._storage.get_session("agent:main:main")
    assert current_node is not None
    assert current_node.session_id == node.session_id
    assert current_node.compaction_count == 1


@pytest.mark.asyncio
async def test_compact_with_result_writes_portable_context_state(manager):
    await manager.create("agent:main:main")
    await manager.append_message(
        "agent:main:main",
        "user",
        "Goal: keep portable state. File src/opensquilla/session/models.py.",
        token_count=250,
    )
    for i in range(8):
        await manager.append_message(
            "agent:main:main",
            "assistant",
            f"recent tail {i}",
            token_count=20,
        )

    result = await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=300,
        config=CompactionConfig(safety_margin=1.0),
    )

    states = await manager.get_context_states("agent:main:main")

    assert result.removed_count > 0
    assert len(states) == 1
    state = states[0]
    assert state.provider == "portable"
    assert state.model is None
    assert state.state_kind == "structured_summary_v1"
    summaries = await manager.get_summaries("agent:main:main")
    assert summaries[0].compaction_id
    payload_without_correlation = dict(state.payload)
    assert payload_without_correlation.pop("compaction_id") == summaries[0].compaction_id
    assert payload_without_correlation == result.summary_payload
    assert state.covered_through_id > 0
    assert state.portable is True
    assert state.cacheable is True
    assert state.valid is True
    assert await manager.get_transcript("agent:main:main")


@pytest.mark.asyncio
async def test_compact_with_result_preserves_tool_metadata_for_boundary_cut(manager):
    await manager.create("agent:main:main")
    await manager.append_message(
        "agent:main:main",
        "user",
        "ancient request",
        token_count=500,
    )
    await manager.append_message(
        "agent:main:main",
        "assistant",
        "ancient answer",
        token_count=500,
    )
    await manager.append_message(
        "agent:main:main",
        "user",
        "tool request",
        token_count=3,
    )
    await manager.append_message(
        "agent:main:main",
        "assistant",
        "calling tool",
        tool_calls=[{"id": "call_1", "type": "function"}],
        token_count=4,
    )
    await manager.append_message(
        "agent:main:main",
        "tool",
        "tool result",
        tool_call_id="call_1",
        token_count=4,
    )
    await manager.append_message("agent:main:main", "user", "next question", token_count=3)
    await manager.append_message("agent:main:main", "assistant", "answer", token_count=3)

    result = await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=500,
        config=CompactionConfig(safety_margin=1.0),
    )

    assert result.removed_count == 2
    assert result.kept_entries[0]["role"] == "user"
    assert result.kept_entries[0]["content"] == "tool request"
    assert result.kept_entries[1]["role"] == "assistant"
    assert result.kept_entries[1]["tool_calls"] == [{"id": "call_1", "type": "function"}]
    transcript = await manager.get_transcript("agent:main:main")
    assert transcript[0].role == "user"
    assert transcript[0].content == "tool request"
    assert transcript[1].role == "assistant"
    assert transcript[1].tool_calls == [{"id": "call_1", "type": "function"}]
    assert transcript[2].role == "tool"
    assert transcript[2].tool_call_id == "call_1"


@pytest.mark.asyncio
async def test_compact_counts_tool_calls_when_token_count_is_underreported(manager):
    await manager.create("agent:main:main")
    large_content = "x" * 5000
    await manager.append_message(
        "agent:main:main",
        "assistant",
        "small visible answer",
        tool_calls=[
            {
                "type": "tool_use",
                "tool_use_id": "write-stale",
                "name": "write_file",
                "input": {"path": "index.html", "content": large_content},
            }
        ],
        token_count=1,
    )

    result = await manager.compact_with_result(
        "agent:main:main",
        context_window_tokens=300,
        config=CompactionConfig(
            safety_margin=1.0,
            # This test isolates the wire-token estimator. Default production
            # policy correctly retains the unresolved tool call as raw state.
            protect_semantic_tail=False,
        ),
    )

    assert result.removed_count == 1
    assert result.summary
    assert large_content not in result.summary
    node = await manager._storage.get_session("agent:main:main")
    assert node.compaction_count == 1


def _fail_next_transcript_insert(monkeypatch: pytest.MonkeyPatch, storage: SessionStorage) -> None:
    original_execute = storage.conn.execute
    failed = False

    def execute(sql: str, params: Any = ()):
        nonlocal failed
        if (
            not failed
            and isinstance(sql, str)
            and sql.lstrip().upper().startswith("INSERT INTO TRANSCRIPT_ENTRIES")
        ):
            failed = True
            raise RuntimeError("rewrite insert failed")
        return original_execute(sql, params)

    monkeypatch.setattr(storage.conn, "execute", execute)


def _fail_next_summary_insert(monkeypatch: pytest.MonkeyPatch, storage: SessionStorage) -> None:
    original_execute = storage.conn.execute
    failed = False

    def execute(sql: str, params: Any = ()):
        nonlocal failed
        if (
            not failed
            and isinstance(sql, str)
            and sql.lstrip().upper().startswith("INSERT INTO SESSION_SUMMARIES")
        ):
            failed = True
            raise RuntimeError("rewrite insert failed")
        return original_execute(sql, params)

    monkeypatch.setattr(storage.conn, "execute", execute)


@pytest.mark.asyncio
async def test_compact_rewrite_failure_keeps_session_state_atomic(
    manager,
    monkeypatch: pytest.MonkeyPatch,
):
    node = await manager.create("agent:main:main")
    for index in range(20):
        await manager.append_message("agent:main:main", "user", f"msg {index} " + ("x" * 500))
    original_transcript = await manager.get_transcript("agent:main:main")
    original_canonical_transcript = await manager.get_canonical_transcript("agent:main:main")
    original_summaries = await manager.get_summaries("agent:main:main")
    original_node = await manager._storage.get_session("agent:main:main")

    _fail_next_summary_insert(monkeypatch, manager._storage)

    with pytest.raises(RuntimeError, match="rewrite insert failed"):
        await manager.compact("agent:main:main", context_window_tokens=1000)

    assert await manager.get_transcript("agent:main:main") == original_transcript
    assert (
        await manager.get_canonical_transcript("agent:main:main") == original_canonical_transcript
    )
    assert await manager.get_summaries("agent:main:main") == original_summaries
    assert await manager.get_context_states("agent:main:main") == []
    current_node = await manager._storage.get_session("agent:main:main")
    assert current_node is not None
    assert original_node is not None
    assert current_node.session_id == node.session_id
    assert current_node.compaction_count == original_node.compaction_count
    assert current_node.updated_at == original_node.updated_at


@pytest.mark.asyncio
async def test_persist_compaction_result_rewrite_failure_keeps_session_state_atomic(
    manager,
    monkeypatch: pytest.MonkeyPatch,
):
    node = await manager.create("agent:main:main")
    for index in range(4):
        await manager.append_message("agent:main:main", "user", f"msg {index}", token_count=5)
    original_transcript = await manager.get_transcript("agent:main:main")
    original_canonical_transcript = await manager.get_canonical_transcript("agent:main:main")
    original_summaries = await manager.get_summaries("agent:main:main")
    original_node = await manager._storage.get_session("agent:main:main")

    _fail_next_transcript_insert(monkeypatch, manager._storage)

    with pytest.raises(RuntimeError, match="rewrite insert failed"):
        await manager.persist_compaction_result(
            "agent:main:main",
            "short summary",
            [{"role": "assistant", "content": "latest reply"}],
        )

    assert await manager.get_transcript("agent:main:main") == original_transcript
    assert (
        await manager.get_canonical_transcript("agent:main:main") == original_canonical_transcript
    )
    assert await manager.get_summaries("agent:main:main") == original_summaries
    assert await manager.get_context_states("agent:main:main") == []
    current_node = await manager._storage.get_session("agent:main:main")
    assert current_node is not None
    assert original_node is not None
    assert current_node.session_id == node.session_id
    assert current_node.compaction_count == original_node.compaction_count
    assert current_node.updated_at == original_node.updated_at


@pytest.mark.asyncio
async def test_cross_session_append_cannot_commit_a_failed_compaction(
    manager,
    monkeypatch: pytest.MonkeyPatch,
):
    compacted = await manager.create("agent:main:compacted")
    writer = await manager.create("agent:main:writer")
    for index in range(3):
        await manager.append_message(
            compacted.session_key,
            "user",
            f"synthetic message {index}",
            token_count=5,
        )
    original_entries = await manager.get_transcript(compacted.session_key)

    archive_written = asyncio.Event()
    release_rewrite = asyncio.Event()
    append_attempted = asyncio.Event()
    original_archive = manager._storage._archive_transcript_entries
    original_prepare = manager.prepare_message

    async def archive_then_fail(**kwargs: Any) -> None:
        await original_archive(**kwargs)
        archive_written.set()
        await release_rewrite.wait()
        raise RuntimeError("synthetic rewrite failure")

    async def prepare_with_signal(session_key: str, *args: Any, **kwargs: Any):
        if session_key == writer.session_key:
            append_attempted.set()
        return await original_prepare(session_key, *args, **kwargs)

    monkeypatch.setattr(
        manager._storage,
        "_archive_transcript_entries",
        archive_then_fail,
    )
    monkeypatch.setattr(
        manager,
        "prepare_message",
        prepare_with_signal,
    )

    rewrite_task = asyncio.create_task(
        manager.persist_compaction_result(
            compacted.session_key,
            "synthetic summary",
            [{"role": "assistant", "content": "synthetic tail"}],
            compaction_id="cmp-cross-session-rollback",
        )
    )
    append_task: asyncio.Task[TranscriptEntry] | None = None
    try:
        await asyncio.wait_for(archive_written.wait(), timeout=1)
        append_task = asyncio.create_task(
            manager.append_message(writer.session_key, "user", "independent write")
        )
        await asyncio.wait_for(append_attempted.wait(), timeout=1)
        assert append_task.done() is False

        release_rewrite.set()
        with pytest.raises(RuntimeError, match="synthetic rewrite failure"):
            await rewrite_task
        await append_task
    finally:
        release_rewrite.set()
        for task in (rewrite_task, append_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    assert await manager.get_transcript(compacted.session_key) == original_entries
    assert await manager.get_summaries(compacted.session_key) == []
    async with manager._storage.conn.execute(
        "SELECT COUNT(*) FROM compacted_transcript_entries WHERE session_id = ?",
        (compacted.session_id,),
    ) as cur:
        archived_count = int((await cur.fetchone())[0])
    assert archived_count == 0
    assert [entry.content for entry in await manager.get_transcript(writer.session_key)] == [
        "independent write"
    ]


@pytest.mark.asyncio
async def test_write_transaction_commits_one_successful_unit(manager):
    node = await manager.create("agent:main:explicit-writer")

    async with manager._storage._write_transaction("test_successful_write") as conn:
        await conn.execute(
            "UPDATE sessions SET label = ? WHERE session_key = ?",
            ("committed", node.session_key),
        )

    assert manager._storage.conn.in_transaction is False
    current = await manager.get_session(node.session_key)
    assert current is not None
    assert current.label == "committed"


@pytest.mark.asyncio
async def test_close_waits_for_an_active_connection_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    storage = SessionStorage(str(tmp_path / "close-waits.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create("agent:main:close-waits")
    await manager.append_message(node.session_key, "user", "synthetic first")
    await manager.append_message(node.session_key, "assistant", "synthetic second")

    archive_written = asyncio.Event()
    release_rewrite = asyncio.Event()
    original_archive = storage._archive_transcript_entries

    async def archive_then_wait(**kwargs: Any) -> None:
        await original_archive(**kwargs)
        archive_written.set()
        await release_rewrite.wait()

    monkeypatch.setattr(storage, "_archive_transcript_entries", archive_then_wait)
    rewrite_task = asyncio.create_task(
        manager.persist_compaction_result(
            node.session_key,
            "synthetic summary",
            [{"role": "assistant", "content": "synthetic second"}],
            compaction_id="cmp-close-waits",
        )
    )
    close_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(archive_written.wait(), timeout=1)
        close_task = asyncio.create_task(storage.close())
        await asyncio.sleep(0)
        assert close_task.done() is False

        release_rewrite.set()
        await rewrite_task
        await close_task
        assert storage._conn is None
    finally:
        release_rewrite.set()
        for task in (rewrite_task, close_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if storage._conn is not None:
            await storage.close()


@pytest.mark.asyncio
async def test_persist_compaction_result_stores_summary_out_of_band(manager):
    node = await manager.create("agent:main:main")
    for index in range(4):
        await manager.append_message("agent:main:main", "user", f"msg {index}", token_count=5)

    await manager.persist_compaction_result(
        "agent:main:main",
        "short summary",
        [{"role": "assistant", "content": "latest reply"}],
        compaction_id="cmp_inline_1",
        trigger_reason="agent_inline_overflow",
    )

    transcript = await manager.get_transcript("agent:main:main")
    assert all(entry.role != "system" for entry in transcript)
    assert transcript[-1].content == "latest reply"
    canonical = await manager.get_canonical_transcript("agent:main:main")
    assert [entry.content for entry in canonical] == [
        "msg 0",
        "msg 1",
        "msg 2",
        "latest reply",
    ]
    async with manager._storage.conn.execute(
        "SELECT compaction_id, compaction_index FROM compacted_transcript_entries "
        "WHERE session_id = ? ORDER BY original_entry_id ASC",
        (node.session_id,),
    ) as cur:
        archived_rows = await cur.fetchall()
    assert [(row[0], row[1]) for row in archived_rows] == [
        ("cmp_inline_1", 0),
        ("cmp_inline_1", 0),
        ("cmp_inline_1", 0),
    ]
    summaries = await manager._storage.get_all_summaries(node.session_id)
    assert len(summaries) == 1
    assert summaries[0].summary_text == "short summary"
    assert summaries[0].compaction_id == "cmp_inline_1"
    assert summaries[0].trigger_reason == "agent_inline_overflow"
    assert summaries[0].removed_count == 3
    assert summaries[0].kept_count == 1
    states = await manager.get_context_states("agent:main:main")
    assert len(states) == 1
    assert states[0].state_kind == "structured_summary_v1"
    assert states[0].payload is not None
    assert states[0].payload["compaction_id"] == "cmp_inline_1"


@pytest.mark.asyncio
async def test_persist_compaction_result_preserves_structured_tail_metadata(manager):
    node = await manager.create("agent:main:structured-tail")
    await manager.append_message(node.session_key, "user", "old question")
    await manager.append_message(node.session_key, "assistant", "old answer")
    await manager.append_message(
        node.session_key,
        "assistant",
        "",
        tool_calls=[
            {
                "id": "tool-live",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"x"}'},
            }
        ],
        reasoning_content="signed reasoning",
    )
    await manager.append_message(
        node.session_key,
        "tool",
        "result",
        tool_call_id="tool-live",
    )

    await manager.persist_compaction_result(
        node.session_key,
        "older context",
        [
            {"role": "assistant", "content": "[flattened tool request]"},
            {"role": "tool", "content": "[flattened tool result]"},
        ],
        compaction_id="cmp-structured-tail",
    )

    transcript = await manager.get_transcript(node.session_key)
    assert len(transcript) == 2
    assert transcript[0].tool_calls is not None
    assert transcript[0].tool_calls[0]["id"] == "tool-live"
    assert transcript[0].reasoning_content == "signed reasoning"
    assert transcript[1].tool_call_id == "tool-live"
    assert transcript[1].content == "result"


@pytest.mark.asyncio
async def test_capture_compaction_source_uses_supplied_transcript_entries(
    manager,
    monkeypatch,
):
    node = await manager.create("agent:main:capture-snapshot")
    await manager.append_message(node.session_key, "user", "old question")
    active_user = await manager.append_message(node.session_key, "user", "active request")
    transcript = await manager.get_transcript(node.session_key)
    get_transcript = AsyncMock(side_effect=AssertionError("unexpected transcript reread"))
    monkeypatch.setattr(manager, "get_transcript", get_transcript)

    source = await manager.capture_compaction_source(
        node.session_key,
        boundary_message_id=active_user.message_id,
        transcript_entries=transcript,
    )

    get_transcript.assert_not_awaited()
    assert source.entries == tuple(transcript)
    assert source.boundary_message_id == active_user.message_id


@pytest.mark.asyncio
async def test_inline_compaction_preserves_queued_append_only_suffix(manager):
    node = await manager.create("agent:main:inline-queued-suffix")
    old_user = await manager.append_message(node.session_key, "user", "old question")
    old_assistant = await manager.append_message(
        node.session_key,
        "assistant",
        "old answer",
    )
    active_user = await manager.append_message(
        node.session_key,
        "user",
        "active request",
    )
    source = await manager.capture_compaction_source(
        node.session_key,
        boundary_message_id=active_user.message_id,
    )
    queued_user = await manager.append_message(
        node.session_key,
        "user",
        "queued request",
    )
    entries_before = {
        entry.message_id: entry
        for entry in await manager.get_transcript(node.session_key)
    }

    installed = await manager.persist_compaction_result(
        node.session_key,
        "summary of the old exchange",
        [
            {"role": "user", "content": "[Available skills for this turn]"},
            {"role": "user", "content": "active request"},
        ],
        compaction_id="cmp-inline-queued",
        removed_count=2,
        source_entries=source.entries,
        source_preimage=source.preimage,
        source_boundary_message_id=source.boundary_message_id,
        source_boundary_entry_id=source.boundary_entry_id,
    )

    assert installed is True
    transcript = await manager.get_transcript(node.session_key)
    assert [entry.message_id for entry in transcript] == [
        active_user.message_id,
        queued_user.message_id,
    ]
    assert [entry.id for entry in transcript] == [
        entries_before[active_user.message_id].id,
        entries_before[queued_user.message_id].id,
    ]
    assert transcript[0].content == "active request"
    assert transcript[1].content == "queued request"
    queued_before = entries_before[queued_user.message_id]
    assert queued_before.id is not None
    page_before_queued, _ = await manager._storage.get_canonical_transcript_page(
        node.session_id,
        limit=1,
        before=(queued_before.created_at, queued_before.id),
    )
    assert [entry.message_id for entry in page_before_queued] == [
        active_user.message_id,
    ]
    async with manager._storage.conn.execute(
        "SELECT message_id FROM compacted_transcript_entries "
        "WHERE session_id = ? ORDER BY original_entry_id",
        (node.session_id,),
    ) as cur:
        archived = await cur.fetchall()
    assert [row[0] for row in archived] == [
        old_user.message_id,
        old_assistant.message_id,
    ]
    summaries = await manager._storage.get_all_summaries(node.session_id)
    assert len(summaries) == 1
    assert summaries[0].removed_count == 2
    assert summaries[0].kept_count == 1


@pytest.mark.asyncio
async def test_inline_compaction_preserves_metadata_from_concurrent_suffix_append(
    manager,
    monkeypatch,
):
    node = await manager.create("agent:main:inline-concurrent-metadata")
    old_user = await manager.append_message(node.session_key, "user", "old question")
    await manager.append_message(node.session_key, "assistant", "old answer")
    active_user = await manager.append_message(node.session_key, "user", "active request")
    source = await manager.capture_compaction_source(
        node.session_key,
        boundary_message_id=active_user.message_id,
    )
    before = await manager._storage.get_session(node.session_key)
    assert before is not None
    before.total_tokens = 100
    before.total_tokens_fresh = True
    await manager._storage.upsert_session(before)

    original_rewrite = manager._storage.rewrite_compacted_session
    append_metadata: dict[str, Any] = {}

    async def rewrite_after_concurrent_append(**kwargs):
        appended = await manager.append_message(
            node.session_key,
            "user",
            "queued after compaction snapshot",
            token_count=17,
        )
        concurrent = await manager._storage.get_session(node.session_key)
        assert concurrent is not None
        append_metadata["updated_at"] = concurrent.updated_at
        append_metadata["message_id"] = appended.message_id
        return await original_rewrite(**kwargs)

    monkeypatch.setattr(
        manager._storage,
        "rewrite_compacted_session",
        rewrite_after_concurrent_append,
    )

    installed = await manager.persist_compaction_result(
        node.session_key,
        "summary of the old exchange",
        [{"role": "user", "content": "active request"}],
        compaction_id="cmp-inline-concurrent-metadata",
        removed_count=2,
        source_entries=source.entries,
        source_preimage=source.preimage,
        source_boundary_message_id=source.boundary_message_id,
        source_boundary_entry_id=source.boundary_entry_id,
    )

    assert installed is True
    current = await manager._storage.get_session(node.session_key)
    assert current is not None
    assert current.total_tokens == 117
    assert current.total_tokens_fresh is False
    assert current.updated_at >= append_metadata["updated_at"]
    assert current.compaction_count == 1
    transcript = await manager.get_transcript(node.session_key)
    assert [entry.message_id for entry in transcript] == [
        active_user.message_id,
        append_metadata["message_id"],
    ]
    assert old_user.message_id not in {entry.message_id for entry in transcript}


@pytest.mark.asyncio
async def test_inline_compaction_rejects_stale_source_without_partial_install(manager):
    node = await manager.create("agent:main:inline-stale-source")
    old_user = await manager.append_message(node.session_key, "user", "old question")
    active_user = await manager.append_message(
        node.session_key,
        "user",
        "active request",
    )
    source = await manager.capture_compaction_source(
        node.session_key,
        boundary_message_id=active_user.message_id,
    )
    assert await manager.update_message_turn_context(
        node.session_key,
        old_user.message_id,
        {"status": "changed-after-source-capture"},
    )

    installed = await manager.persist_compaction_result(
        node.session_key,
        "stale summary",
        [{"role": "user", "content": "active request"}],
        compaction_id="cmp-inline-stale",
        removed_count=1,
        source_entries=source.entries,
        source_preimage=source.preimage,
        source_boundary_message_id=source.boundary_message_id,
        source_boundary_entry_id=source.boundary_entry_id,
    )

    assert installed is False
    transcript = await manager.get_transcript(node.session_key)
    assert [entry.message_id for entry in transcript] == [
        old_user.message_id,
        active_user.message_id,
    ]
    assert transcript[0].turn_context == {
        "status": "changed-after-source-capture",
    }
    assert await manager._storage.get_all_summaries(node.session_id) == []
    assert await manager.get_context_states(node.session_key) == []
    current_node = await manager.get_session(node.session_key)
    assert current_node is not None
    assert current_node.compaction_count == 0
    async with manager._storage.conn.execute(
        "SELECT COUNT(*) FROM compacted_transcript_entries WHERE session_id = ?",
        (node.session_id,),
    ) as cur:
        assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_canonical_transcript_page_crosses_multiple_compaction_boundaries(manager):
    node = await manager.create("agent:main:main")
    for index in range(10):
        await manager._storage.append_transcript_entry(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=node.session_key,
                message_id=f"msg-{index}",
                role="user",
                content=f"message {index}",
                created_at=1_000 + index // 2,
            )
        )

    await manager.persist_compaction_result(
        node.session_key,
        "first summary",
        [{"role": "user", "content": f"message {index}"} for index in range(4, 10)],
        compaction_id="cmp-page-1",
    )
    await manager.persist_compaction_result(
        node.session_key,
        "second summary",
        [{"role": "user", "content": f"message {index}"} for index in range(7, 10)],
        compaction_id="cmp-page-2",
    )

    loaded: list[TranscriptEntry] = []
    before = None
    while True:
        page = await manager.get_canonical_transcript_page(
            node.session_key,
            limit=3,
            before=before,
        )
        assert page.canonical_complete is True
        loaded = [*page.entries, *loaded]
        if not page.has_more:
            break
        oldest = page.entries[0]
        assert oldest.id is not None
        before = (oldest.created_at, oldest.id)

    assert [entry.content for entry in loaded] == [f"message {index}" for index in range(10)]
    assert len({entry.message_id for entry in loaded}) == 10

    active = await manager.get_transcript(node.session_key)
    assert [entry.content for entry in active] == [f"message {index}" for index in range(7, 10)]

    forward = [loaded[0]]
    after = (loaded[0].created_at, loaded[0].id or 0)
    while True:
        page = await manager.get_canonical_transcript_page(
            node.session_key,
            limit=2,
            after=after,
        )
        forward.extend(page.entries)
        if not page.has_more:
            break
        newest = page.entries[-1]
        assert newest.id is not None
        after = (newest.created_at, newest.id)

    assert [entry.message_id for entry in forward] == [entry.message_id for entry in loaded]


@pytest.mark.asyncio
async def test_canonical_transcript_page_preserves_turn_context(manager):
    """Paged canonical reads must keep turn_context on active and archived rows."""
    node = await manager.create("agent:main:main")
    for index in range(4):
        await manager._storage.append_transcript_entry(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=node.session_key,
                message_id=f"ctx-{index}",
                role="user",
                content=f"message {index}",
                turn_context={"turn_id": f"turn-{index}", "disposition": "steering"},
                created_at=1_000 + index,
            )
        )

    await manager.persist_compaction_result(
        node.session_key,
        "context summary",
        [{"role": "user", "content": f"message {index}"} for index in range(2, 4)],
        compaction_id="cmp-turn-context",
    )

    page = await manager.get_canonical_transcript_page(node.session_key, limit=10)
    assert [entry.message_id for entry in page.entries] == [f"ctx-{i}" for i in range(4)]
    assert [entry.turn_context for entry in page.entries] == [
        {"turn_id": f"turn-{index}", "disposition": "steering"} for index in range(4)
    ]


@pytest.mark.asyncio
async def test_canonical_transcript_page_reads_one_snapshot_during_compaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db_path = tmp_path / "canonical-page-snapshot.db"
    writer_storage = SessionStorage(str(db_path))
    await writer_storage.connect()
    writer = SessionManager(writer_storage, inject_time_prefix=False)
    node = await writer.create("agent:main:main")
    for index in range(4):
        await writer_storage.append_transcript_entry(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=node.session_key,
                message_id=f"snapshot-{index}",
                role="user",
                content=f"message {index}",
                created_at=1_000 + index,
            )
        )

    reader_storage = SessionStorage(str(db_path))
    await reader_storage.connect()
    original_execute = reader_storage.conn.execute
    compaction_injected = False

    class _CompactionAfterFetch:
        def __init__(self, delegate: Any) -> None:
            self._delegate = delegate
            self._cursor: Any = None

        async def __aenter__(self):
            self._cursor = await self._delegate.__aenter__()
            return self

        async def fetchall(self):
            nonlocal compaction_injected
            rows = await self._cursor.fetchall()
            if not compaction_injected:
                compaction_injected = True
                await writer.persist_compaction_result(
                    node.session_key,
                    "snapshot summary",
                    [{"role": "user", "content": "message 3"}],
                    compaction_id="cmp-snapshot-race",
                )
            return rows

        async def __aexit__(self, *args: Any):
            return await self._delegate.__aexit__(*args)

    def execute(sql: str, params: Any = ()):
        result = original_execute(sql, params)
        if "FROM transcript_entries" in sql:
            return _CompactionAfterFetch(result)
        return result

    monkeypatch.setattr(reader_storage.conn, "execute", execute)
    try:
        entries, has_more = await reader_storage.get_canonical_transcript_page(
            node.session_id,
            limit=10,
        )
    finally:
        await reader_storage.close()
        await writer_storage.close()

    assert compaction_injected is True
    assert has_more is False
    assert [entry.message_id for entry in entries] == [
        "snapshot-0",
        "snapshot-1",
        "snapshot-2",
        "snapshot-3",
    ]


@pytest.mark.asyncio
async def test_canonical_page_completeness_uses_post_page_compaction_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    db_path = tmp_path / "canonical-completeness-snapshot.db"
    writer_storage = SessionStorage(str(db_path))
    await writer_storage.connect()
    writer = SessionManager(writer_storage, inject_time_prefix=False)
    node = await writer.create("agent:main:main")
    for index in range(4):
        await writer.append_message(node.session_key, "user", f"message {index}")

    reader_storage = SessionStorage(str(db_path))
    await reader_storage.connect()
    reader = SessionManager(reader_storage, inject_time_prefix=False)
    original_page = reader_storage.get_canonical_transcript_page
    compaction_injected = False

    async def page_then_compact(session_id: str, **kwargs: Any):
        nonlocal compaction_injected
        page = await original_page(session_id, **kwargs)
        if not compaction_injected:
            compaction_injected = True
            await writer.persist_compaction_result(
                node.session_key,
                "current summary",
                [{"role": "user", "content": "message 3"}],
                compaction_id="cmp-completeness-snapshot",
            )
        return page

    monkeypatch.setattr(reader_storage, "get_canonical_transcript_page", page_then_compact)
    try:
        page = await reader.get_canonical_transcript_page(node.session_key, limit=10)
    finally:
        await reader_storage.close()
        await writer_storage.close()

    assert compaction_injected is True
    assert [entry.content for entry in page.entries] == [
        "message 0",
        "message 1",
        "message 2",
        "message 3",
    ]
    assert page.canonical_complete is True


@pytest.mark.asyncio
async def test_canonical_transcript_page_reports_incomplete_legacy_archive(manager):
    node = await manager.create("agent:main:main")
    for index in range(4):
        await manager.append_message(node.session_key, "user", f"message {index}")
    await manager.persist_compaction_result(
        node.session_key,
        "legacy summary",
        [{"role": "user", "content": "message 3"}],
        compaction_id="cmp-legacy",
    )
    await manager._storage.conn.execute(
        "DELETE FROM compacted_transcript_entries "
        "WHERE session_id = ? AND compaction_id = ?",
        (node.session_id, "cmp-legacy"),
    )
    await manager._storage.conn.commit()

    page = await manager.get_canonical_transcript_page(node.session_key, limit=10)

    assert [entry.content for entry in page.entries] == ["message 3"]
    assert page.canonical_complete is False


@pytest.mark.asyncio
async def test_delete_session_removes_compacted_transcript_archive(manager):
    node = await manager.create("agent:main:main")
    for index in range(4):
        await manager.append_message("agent:main:main", "user", f"msg {index}", token_count=5)

    await manager.persist_compaction_result(
        "agent:main:main",
        "short summary",
        [{"role": "assistant", "content": "latest reply"}],
    )
    assert len(await manager.get_canonical_transcript("agent:main:main")) == 4

    await manager._storage.delete_session("agent:main:main")

    async with manager._storage.conn.execute(
        "SELECT COUNT(*) FROM compacted_transcript_entries WHERE session_id = ?",
        (node.session_id,),
    ) as cur:
        archived_count = (await cur.fetchone())[0]
    assert archived_count == 0


@pytest.mark.asyncio
async def test_persist_compaction_result_without_summary_does_not_rewrite_transcript(manager):
    node = await manager.create("agent:main:main")
    for index in range(5):
        await manager.append_message("agent:main:main", "user", f"msg {index}", token_count=5)

    original_transcript = await manager.get_transcript("agent:main:main")
    original_node = await manager._storage.get_session("agent:main:main")

    await manager.persist_compaction_result(
        "agent:main:main",
        "",
        [{"role": "assistant", "content": "latest reply"}],
    )

    assert await manager.get_transcript("agent:main:main") == original_transcript
    assert await manager.get_summaries("agent:main:main") == []
    current_node = await manager._storage.get_session("agent:main:main")
    assert current_node is not None
    assert original_node is not None
    assert current_node.session_id == node.session_id
    assert current_node.compaction_count == original_node.compaction_count
    assert current_node.updated_at == original_node.updated_at


@pytest.mark.asyncio
async def test_prune_stale(manager):
    node = await manager.create("agent:main:main")
    # force old timestamp
    node.updated_at = 1
    await manager._storage.upsert_session(node)
    pruned = await manager.prune_stale(max_age_ms=1000)
    assert pruned == 1


@pytest.mark.asyncio
async def test_cap_entries(manager):
    for i in range(10):
        await manager.create(f"agent:main:direct:u{i}")
    deleted = await manager.cap_entries(max_entries=5)
    assert deleted == 5
    remaining = await manager._storage.count_sessions()
    assert remaining == 5


@pytest.mark.asyncio
async def test_cap_entries_cleans_related_transcript_and_summaries(manager):
    session_ids: dict[str, str] = {}
    for i in range(3):
        key = f"agent:main:direct:u{i}"
        node = await manager.create(key)
        session_ids[key] = node.session_id
        await manager._storage.append_transcript_entry(
            TranscriptEntry(
                session_id=node.session_id,
                session_key=key,
                role="user",
                content="stale",
            )
        )
        await manager._storage.save_summary(
            SessionSummary(
                session_id=node.session_id,
                session_key=key,
                summary_text="summary",
            )
        )
        await manager.save_context_state(
            SessionContextState(
                session_id=node.session_id,
                session_key=key,
                provider="portable",
                state_kind="structured_summary_v1",
                payload={"summary": "state"},
                portable=True,
                cacheable=True,
            )
        )
    deleted = await manager.cap_entries(max_entries=1)
    assert deleted == 2
    remaining = {session.session_key for session in await manager._storage.list_sessions(limit=10)}
    removed = set(session_ids) - remaining
    assert len(removed) == 2
    for key in removed:
        session_id = session_ids[key]
        assert await manager._storage.count_transcript_entries(session_id) == 0
        assert await manager._storage.get_all_summaries(session_id) == []
        assert await manager.get_context_states(key, valid_only=False) == []


@pytest.mark.asyncio
async def test_archive(manager):
    await manager.create("agent:main:main")
    await manager.archive("agent:main:main")
    node = await manager._storage.get_session("agent:main:main")
    assert node.status == SessionStatus.DONE
