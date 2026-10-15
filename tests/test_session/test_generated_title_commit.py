"""Background title commits must not overwrite newer session state."""

from __future__ import annotations

import asyncio

import pytest

from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionIntent, SessionNode
from opensquilla.session.storage import SessionStorage


@pytest.fixture
async def title_store():
    storage = SessionStorage(":memory:")
    await storage.connect()
    node = SessionNode(
        session_key="agent:main:webchat:title-commit", session_id="original",
        display_name="WebChat",
    )
    await storage.upsert_session(node)
    try:
        yield storage, SessionManager(storage, inject_time_prefix=False), node
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_title_commit_preserves_other_fields_and_is_single_winner(title_store):
    storage, manager, original = title_store
    await manager.update(original.session_key, subject="Changed while naming")
    assert await manager.set_generated_title_if_unchanged(original, "PTY 命令能力")
    assert not await manager.set_generated_title_if_unchanged(original, "Second result")
    current = await storage.get_session(original.session_key)
    assert current.derived_title == "PTY 命令能力"
    assert current.subject == "Changed while naming"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("subject", "Changed while naming"), ("display_name", "My own title")],
)
async def test_field_update_does_not_overwrite_title_when_stale_snapshot_races(
    title_store, field, value,
):
    """A field patch must not write back a stale whole-session snapshot.

    The gate freezes the field-level writer before its UPDATE. The title writer
    then commits through its conditional field update. A legacy
    read/modify/upsert implementation would overwrite ``derived_title`` with
    the stale NULL value when the gate is released.
    """
    storage, manager, original = title_store
    original_update = storage.update_session_fields
    write_started = asyncio.Event()
    release_read = asyncio.Event()

    async def gated_update(*args, **kwargs):
        write_started.set()
        await release_read.wait()
        return await original_update(*args, **kwargs)

    storage.update_session_fields = gated_update
    try:
        update_task = asyncio.create_task(
            manager.update(original.session_key, **{field: value})
        )
        await asyncio.wait_for(write_started.wait(), timeout=1)

        assert await manager.set_generated_title_if_unchanged(
            original, "PTY 命令能力"
        )
        release_read.set()
        await update_task
    finally:
        storage.update_session_fields = original_update

    current = await storage.get_session(original.session_key)
    assert current is not None
    assert current.derived_title == "PTY 命令能力"
    assert getattr(current, field) == value


@pytest.mark.asyncio
async def test_concurrent_field_updates_preserve_each_other(title_store):
    """Two stale readers must not overwrite each other's independent fields."""
    storage, manager, original = title_store
    original_get = storage.get_session
    reads = 0
    both_read = asyncio.Event()
    release = asyncio.Event()

    async def gated_get(session_key):
        nonlocal reads
        node = await original_get(session_key)
        if session_key == original.session_key:
            reads += 1
            if reads == 2:
                both_read.set()
            await release.wait()
        return node

    storage.get_session = gated_get
    try:
        subject_task = asyncio.create_task(
            manager.update(original.session_key, subject="PTY command")
        )
        display_task = asyncio.create_task(
            manager.update(original.session_key, display_name="My own title")
        )
        await asyncio.wait_for(both_read.wait(), timeout=1)
        release.set()
        await asyncio.gather(subject_task, display_task)
    finally:
        storage.get_session = original_get

    current = await storage.get_session(original.session_key)
    assert current is not None
    assert current.subject == "PTY command"
    assert current.display_name == "My own title"


@pytest.mark.asyncio
async def test_compat_compaction_rewrite_does_not_restore_stale_session_fields(title_store):
    """The source-less compaction path owns metadata, not the whole session row."""
    storage, manager, original = title_store
    stale = await storage.get_session(original.session_key)
    assert stale is not None
    await manager.update(
        original.session_key,
        derived_title="PTY 命令能力",
        subject="Current subject",
    )
    stale.compaction_count = 1

    assert await storage.rewrite_compacted_session(
        node=stale,
        summary=None,
        entries=[],
    )

    current = await storage.get_session(original.session_key)
    assert current is not None
    assert current.derived_title == "PTY 命令能力"
    assert current.subject == "Current subject"
    assert current.compaction_count == 1


@pytest.mark.asyncio
async def test_stale_field_update_is_fenced_after_session_reset(title_store):
    """A delayed field patch cannot update a reset/recreated session generation."""
    storage, manager, original = title_store
    original_update = storage.update_session_fields
    write_started = asyncio.Event()
    release_read = asyncio.Event()

    async def gated_update(*args, **kwargs):
        write_started.set()
        await release_read.wait()
        return await original_update(*args, **kwargs)

    storage.update_session_fields = gated_update
    try:
        update_task = asyncio.create_task(
            manager.update(
                original.session_key,
                expected_session_id=original.session_id,
                expected_session_epoch=int(original.epoch or 0),
                subject="stale update",
            )
        )
        await asyncio.wait_for(write_started.wait(), timeout=1)
        replacement, rotated = await manager.apply_intent(
            original.session_key, SessionIntent.RESET_SAME_KEY
        )
        assert rotated
        release_read.set()
        with pytest.raises(KeyError, match="generation|stale|Session"):
            await update_task
    finally:
        storage.update_session_fields = original_update

    current = await storage.get_session(original.session_key)
    assert current is not None
    assert current.session_id == replacement.session_id
    assert current.subject is None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["rename", "epoch", "replacement", "deleted"])
async def test_title_commit_rejects_stale_snapshot(title_store, change):
    storage, manager, original = title_store
    if change == "rename":
        await manager.update(original.session_key, display_name="My own title")
    elif change == "epoch":
        await manager.update(original.session_key, epoch=original.epoch + 1)
    elif change == "replacement":
        await storage.delete_session(original.session_key)
        await storage.upsert_session(SessionNode(
            session_key=original.session_key, session_id="replacement", display_name="WebChat",
        ))
    else:
        await storage.delete_session(original.session_key)
    assert not await manager.set_generated_title_if_unchanged(original, "Stale title")
    current = await storage.get_session(original.session_key)
    if current is not None:
        assert current.derived_title is None
        if change == "rename":
            assert current.display_name == "My own title"
