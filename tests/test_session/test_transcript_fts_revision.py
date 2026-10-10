"""Content revisions must not make FTS process a replacement twice."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from opensquilla.compat import aiosqlite
from opensquilla.session.models import SessionNode, TranscriptEntry
from opensquilla.session.storage import SessionStorage

KEY = "agent:main:webchat:fts-revision"
OLD_TRIGGER = """
CREATE TRIGGER transcript_fts_au AFTER UPDATE ON transcript_entries BEGIN
    INSERT INTO transcript_fts(transcript_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content);
END
"""


@pytest.fixture(params=[False, True], ids=["aiosqlite", "sqlite3-fallback"])
async def storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request
) -> AsyncIterator[SessionStorage]:
    monkeypatch.setattr(aiosqlite, "_FORCE_SQLITE3_FALLBACK", request.param)
    storage = await SessionStorage.open(tmp_path / "fts-revision.db")
    await storage.upsert_session(SessionNode(session_key=KEY, session_id="sid", agent_id="main"))
    await storage.append_transcript_entry(TranscriptEntry(
        session_key=KEY, session_id="sid", message_id="mid", role="user",
        content="synthetic history", created_at=100,
    ), expected_epoch=0)
    try:
        yield storage
    finally:
        await storage.close()


async def assert_index(storage: SessionStorage, *, revision: int, replacement: bool) -> None:
    async with storage.conn.execute("SELECT content_revision FROM transcript_entries") as cursor:
        assert (await cursor.fetchone())[0] == revision
    assert bool(await storage.search_transcript("replacement")) is replacement
    assert len(await storage.search_transcript("synthetic")) == 1
    await storage.conn.execute(
        "INSERT INTO transcript_fts(transcript_fts, rank) VALUES('integrity-check', 1)"
    )


async def replace_body(storage: SessionStorage) -> None:
    assert await storage.upsert_transcript_entry_and_touch(TranscriptEntry(
        session_key=KEY, session_id="sid", message_id="mid", role="user",
        content="replacement synthetic history", created_at=100,
    ), expected_epoch=0, updated_at=200) is False


async def test_public_upsert_with_overlapping_terms_preserves_revision_and_search(
    storage: SessionStorage,
) -> None:
    await replace_body(storage)
    await assert_index(storage, revision=1, replacement=True)
    # Replaying identical text and changing only metadata must not advance the
    # content revision or apply a second FTS deletion for the changed body.
    await replace_body(storage)
    await storage.conn.execute("UPDATE transcript_entries SET token_count = 17")
    await assert_index(storage, revision=1, replacement=True)
    # FTS external-content row identity must still follow a deliberate id move.
    await storage.conn.execute("UPDATE transcript_entries SET id = id + 1000")
    await assert_index(storage, revision=1, replacement=True)


async def test_reopen_upgrades_existing_broad_fts_trigger_without_rebuilding_index(
    storage: SessionStorage,
) -> None:
    async with storage.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'trg_transcript_entries_content_revision'"
    ) as cursor:
        revision_trigger = (await cursor.fetchone())[0]
    # Match the old startup creation order: revision maintenance was created
    # after FTS and therefore can fire first. Do not rely on trigger ordering.
    await storage.conn.execute("DROP TRIGGER trg_transcript_entries_content_revision")
    await storage.conn.execute("DROP TRIGGER transcript_fts_au")
    await storage.conn.execute(OLD_TRIGGER)
    await storage.conn.execute(revision_trigger)
    await storage.close()
    await storage.connect()
    await assert_index(storage, revision=0, replacement=False)
    await replace_body(storage)
    await assert_index(storage, revision=1, replacement=True)
    # A second open is idempotent and preserves already committed search data.
    await storage.close()
    await storage.connect()
    await assert_index(storage, revision=1, replacement=True)


async def test_trigger_upgrade_is_bounded_and_repeat_startup_does_not_rewrite_schema(
    storage: SessionStorage,
) -> None:
    await storage.conn.execute("DROP TRIGGER transcript_fts_au")
    await storage.conn.execute(OLD_TRIGGER)
    statements: list[str] = []
    await storage.conn.set_trace_callback(statements.append)
    try:
        await storage._migrate_transcript_fts_update_trigger()
        assert any(sql.startswith("SAVEPOINT") for sql in statements)
        assert any(sql.startswith("DROP TRIGGER") for sql in statements)
        assert not any(sql.lstrip().upper().startswith("INSERT INTO") for sql in statements)
        assert not any("REBUILD" in sql.upper() for sql in statements)
        statements.clear()
        await storage._migrate_transcript_fts_update_trigger()
        assert len(statements) == 1
        assert "sqlite_master" in statements[0]
    finally:
        await storage.conn.set_trace_callback(None)
    await assert_index(storage, revision=0, replacement=False)


async def test_failed_trigger_upgrade_rolls_back_the_old_definition(
    storage: SessionStorage, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await storage.conn.execute("DROP TRIGGER transcript_fts_au")
    await storage.conn.execute(OLD_TRIGGER)
    original_execute = storage.conn.execute

    def fail_create(sql, *args, **kwargs):
        if sql.lstrip().startswith("CREATE TRIGGER IF NOT EXISTS transcript_fts_au"):
            raise RuntimeError("synthetic trigger creation failure")
        return original_execute(sql, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(storage.conn, "execute", fail_create)
        with pytest.raises(RuntimeError, match="synthetic trigger creation failure"):
            await storage._migrate_transcript_fts_update_trigger()
    assert not storage.conn.in_transaction
    async with storage.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='transcript_fts_au'"
    ) as cursor:
        assert (await cursor.fetchone())[0].strip() == OLD_TRIGGER.strip()
    await assert_index(storage, revision=0, replacement=False)
