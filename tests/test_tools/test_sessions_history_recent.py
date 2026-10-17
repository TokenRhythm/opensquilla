from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
import pytest_asyncio

from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionIntent
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin import sessions
from opensquilla.tools.builtin.session_history_preview import RECENT_HISTORY_MAX_BYTES
from opensquilla.tools.types import ToolContext, ToolError, current_tool_context

PARENT = "agent:main:webchat:history-parent"
OTHER = "agent:main:webchat:history-other"


@pytest_asyncio.fixture
async def manager(monkeypatch):
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    monkeypatch.setattr(sessions, "_session_manager", manager)
    yield manager
    await storage.close()


@contextmanager
def caller(key=PARENT, *, owner=True, guest=False, session_id=None, epoch=None):
    token = current_tool_context.set(
        ToolContext(
            is_owner=owner,
            guest_safe=guest,
            session_key=key,
            session_id=session_id,
            session_epoch=epoch,
        )
    )
    try:
        yield
    finally:
        current_tool_context.reset(token)


async def seed(manager, key=PARENT):
    node = await manager.create(key)
    await manager.append_message(key, "user", "first question")
    await manager.append_message(key, "assistant", "first answer")
    return node


@pytest.mark.asyncio
async def test_legacy_default_and_explicit_false_keep_prefix_and_shape(manager):
    await seed(manager)
    await manager.append_message(PARENT, "user", "latest question")
    # Legacy direct calls without ToolContext retain their behavior.
    expected = await manager.read_transcript(PARENT, limit=2)
    default = await sessions.sessions_history(PARENT, limit=2)
    explicit = await sessions.sessions_history(PARENT, limit=2, recent=False)
    assert default == explicit
    assert json.loads(default) == {
        "session_key": PARENT,
        "message_count": 2,
        "messages": expected,
    }
    assert [row["content"] for row in expected] == ["first question", "first answer"]


@pytest.mark.asyncio
async def test_owner_recent_reads_latest_cross_archive_visible_rows(manager):
    parent = await seed(manager)
    await manager.persist_compaction_result(
        PARENT,
        "summary",
        [{"role": "assistant", "content": "first answer"}],
        compaction_id="history-tool-archive",
    )
    await manager.append_message(PARENT, "user", "latest question")
    await manager.append_message(PARENT, "assistant", "latest answer")
    await manager.append_message(PARENT, "user", "hidden reminder")
    async with manager._storage._write_transaction("hide_history_reminder") as conn:
        await conn.execute(
            "UPDATE transcript_entries SET turn_context = ? WHERE session_id = ? AND content = ?",
            ('{"visibility":"model-only"}', parent.session_id, "hidden reminder"),
        )
    await manager.append_message(PARENT, "tool", "private raw tool result")
    with caller(OTHER):
        result = json.loads(await sessions.sessions_history(PARENT, limit=3, recent=True))
    assert [row["content"] for row in result["messages"]] == [
        "first answer",
        "latest question",
        "latest answer",
    ]
    assert result["has_more"] is True
    assert result["complete"] is False
    assert result["truncated"] is False
    assert result["live_output_included"] is False
    with caller():
        complete = json.loads(await sessions.sessions_history(PARENT, recent=True))
    assert [row["content"] for row in complete["messages"]] == [
        "first question",
        "first answer",
        "latest question",
        "latest answer",
    ]
    assert complete["complete"] is True
    # A gap in the archive remains explicitly incomplete, even with a short page.
    async with manager._storage._write_transaction("missing_history_archive") as conn:
        await conn.execute(
            "DELETE FROM compacted_transcript_entries WHERE session_id = ?", (parent.session_id,)
        )
    with caller():
        incomplete = json.loads(await sessions.sessions_history(PARENT, recent=True))
    assert incomplete["complete"] is False


@pytest.mark.asyncio
async def test_side_owner_reads_parent_recent_and_current_child_without_inherited_rows(manager):
    await seed(manager)
    child = await manager.create_sidechat(PARENT, "history-parent-read")
    await manager.append_message(PARENT, "assistant", "parent after snapshot")
    await manager.append_message(child.session_key, "user", "what happened now?")
    with caller(child.session_key, session_id=child.session_id, epoch=child.epoch):
        parent = json.loads(await sessions.sessions_history(PARENT, limit=1, recent=True))
        own = json.loads(await sessions.sessions_history(child.session_key, recent=True))
    assert parent["messages"][0]["content"] == "parent after snapshot"
    assert [row["content"] for row in own["messages"]] == ["what happened now?"]


@pytest.mark.asyncio
@pytest.mark.parametrize("recent", [False, True])
async def test_guest_rejected_before_manager_access(monkeypatch, recent):
    monkeypatch.setattr(sessions, "_get_session_manager", lambda: pytest.fail("manager reached"))
    with caller(guest=True), pytest.raises(ToolError, match="GUEST_TOOL_UNAVAILABLE"):
        await sessions.sessions_history(PARENT, recent=recent)


@pytest.mark.asyncio
async def test_recent_requires_context_and_restricted_callers_cannot_cross_session(manager):
    node = await seed(manager)
    await seed(manager, OTHER)
    with pytest.raises(ToolError, match="trusted session context"):
        await sessions.sessions_history(PARENT, recent=True)
    with caller(owner=False, session_id=node.session_id, epoch=node.epoch):
        own = json.loads(await sessions.sessions_history(PARENT, recent=True))
        assert own["message_count"] == 2
        with pytest.raises(ToolError, match="another session"):
            await sessions.sessions_history(OTHER, recent=True)
        # Authorization tightening is confined to the opt-in read.
        assert json.loads(await sessions.sessions_history(OTHER))["message_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["epoch", "session_id"])
async def test_side_parent_identity_reset_is_rejected(manager, field):
    parent = await seed(manager)
    child = await manager.create_sidechat(PARENT, "history-parent-reset")
    value = parent.epoch + 1 if field == "epoch" else "replacement-parent-owner"
    if field == "session_id":
        async with manager._storage._write_transaction("replace_history_owner") as conn:
            await conn.execute(
                "UPDATE sessions SET session_id = ? WHERE session_key = ?",
                (value, PARENT),
            )
    else:
        await manager._storage.update_session_fields(PARENT, {field: value})
    with caller(child.session_key, session_id=child.session_id, epoch=child.epoch):
        with pytest.raises(ToolError, match="parent session changed"):
            await sessions.sessions_history(PARENT, recent=True)


@pytest.mark.asyncio
async def test_reset_during_recent_read_does_not_return_wrong_owner(manager, monkeypatch):
    parent = await seed(manager)
    original = manager.get_canonical_transcript_page

    async def resetting(*args, **kwargs):
        page = await original(*args, **kwargs)
        await manager._storage.update_session_fields(PARENT, {"epoch": parent.epoch + 1})
        return page

    monkeypatch.setattr(manager, "get_canonical_transcript_page", resetting)
    with caller(), pytest.raises(ToolError, match="target session changed"):
        await sessions.sessions_history(PARENT, recent=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("archived", [False, True])
async def test_sql_preview_omits_large_details_and_decodes_attachment_text(
    manager,
    monkeypatch,
    archived,
):
    parent = await seed(manager)
    envelope = json.dumps(
        {
            "text": "raw text",
            "display_text": "visible attachment question",
            "attachments": [{"data": "B" * 1_000_000}],
        }
    )
    await manager.append_message(PARENT, "user", envelope)
    await manager.append_message(
        PARENT,
        "assistant",
        "visible answer",
        tool_calls=[{"result": "T" * 1_000_000}],
        reasoning_content="R" * 1_000_000,
    )
    if archived:
        await manager.append_message(PARENT, "user", "latest question")
        await manager.persist_compaction_result(
            PARENT,
            "summary",
            [{"role": "user", "content": "latest question"}],
            compaction_id="large-details-archive",
        )
    table = "compacted_transcript_entries" if archived else "transcript_entries"
    async with manager._storage._write_transaction("large_history_details") as conn:
        await conn.execute(
            f"UPDATE {table} SET turn_context = ?, assistant_replay = ?, "
            "turn_usage = ?, provenance_source_tool = ? "
            "WHERE session_id = ? AND role = 'assistant'",
            (
                json.dumps({"extra": "C" * 1_000_000}),
                json.dumps({"extra": "P" * 1_000_000}),
                json.dumps({"extra": "U" * 1_000_000}),
                "S" * 1_000_000,
                parent.session_id,
            ),
        )
    original = manager._storage._decode_read_rows
    observed = []

    async def inspect_sql_boundary(rows):
        observed.extend(dict(row) for row in rows)
        return await original(rows)

    monkeypatch.setattr(manager._storage, "_decode_read_rows", inspect_sql_boundary)
    with caller():
        raw = await sessions.sessions_history(PARENT, recent=True)
    assert observed
    if archived:
        assert any(row["content_source"] == "compacted" for row in observed)
    for row in observed:
        for field in (
            "tool_calls",
            "reasoning_content",
            "turn_context",
            "assistant_replay",
            "turn_usage",
            "provenance_source_tool",
        ):
            assert row[field] is None
        assert len((row["content"] or "").encode()) < 20_000
    result = json.loads(raw)
    assert [row["content"] for row in result["messages"]][2:4] == [
        "visible attachment question",
        "visible answer",
    ]
    assert "attachments" not in raw
    assert "reasoning_content" not in raw
    assert result["tool_details_included"] is False


@pytest.mark.asyncio
async def test_public_parent_reset_cannot_rebind_side_history(manager):
    parent = await seed(manager)
    child = await manager.create_sidechat(PARENT, "history-public-reset")
    replacement, rotated = await manager.apply_intent(PARENT, SessionIntent.RESET_SAME_KEY)
    assert rotated and replacement.session_id != parent.session_id
    await manager.append_message(PARENT, "user", "replacement conversation")
    with caller(child.session_key, session_id=child.session_id, epoch=child.epoch):
        with pytest.raises(ToolError, match="parent session changed"):
            await sessions.sessions_history(PARENT, recent=True)


@pytest.mark.asyncio
async def test_recent_total_serialized_utf8_budget_and_latest_suffix(manager):
    await seed(manager)
    for index in range(30):
        await manager.append_message(PARENT, "assistant", f"answer-{index}:" + '测\n"\\😀' * 3_000)
    with caller():
        raw = await sessions.sessions_history(PARENT, limit=100, recent=True)
    assert len(raw.encode("utf-8")) <= RECENT_HISTORY_MAX_BYTES
    result = json.loads(raw)
    assert result["messages"][-1]["content"].startswith("answer-29:")
    indexes = [
        int(row["content"].split(":", 1)[0].removeprefix("answer-")) for row in result["messages"]
    ]
    assert indexes == sorted(indexes)
    assert result["truncated"] is True
    assert result["has_more"] is True
    assert result["complete"] is False


@pytest.mark.asyncio
async def test_structured_user_blocks_are_not_exposed_as_attachment_json(manager):
    await seed(manager)
    await manager.append_message(
        PARENT, "user", json.dumps([{"type": "image", "data": "B" * 100_000}])
    )
    with caller():
        result = json.loads(await sessions.sessions_history(PARENT, limit=1, recent=True))
    assert result["messages"][0]["content"] == "[Structured message omitted]"
    assert result["truncated"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", 1, None])
async def test_recent_requires_boolean(manager, value):
    with caller(), pytest.raises(ToolError, match="boolean"):
        await sessions.sessions_history(PARENT, recent=value)
