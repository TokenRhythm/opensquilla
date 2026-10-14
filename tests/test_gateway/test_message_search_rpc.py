"""Current-session find preserves read authority and bounded navigation."""

from __future__ import annotations

import json
import time

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.guest_rpc_policy import GuestRpcPolicy, GuestRpcPolicyError
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError, get_dispatcher
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.recovery_reads import recovery_read_scope
from opensquilla.session.storage import SessionStorage, StorageBusyError

KEY = "agent:main:webchat:message-search-rpc"


@pytest.fixture
async def context(tmp_path):
    storage = await SessionStorage.open(tmp_path / "search-rpc.db")
    manager = SessionManager(storage, inject_time_prefix=False)
    session = await manager.create(KEY)
    ctx = RpcContext(conn_id="search-rpc", session_manager=manager)
    try:
        yield ctx, storage, session
    finally:
        await storage.close()


async def add(storage, session, mid, timestamp, text="visible needle"):
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_key=session.session_key,
            session_id=session.session_id,
            message_id=mid,
            role="assistant",
            content=text,
            created_at=timestamp,
        )
    )


async def page(ctx, **params):
    return await rpc_sessions._handle_sessions_history_page_v2(
        {
            "key": KEY,
            "direction": "before",
            "target_items": 2,
            "target_bytes": 65536,
            **params,
        },
        ctx,
    )


async def test_search_resolves_identity_and_validates_expected_identity(context):
    ctx, storage, session = context
    await add(storage, session, "mid", 1)
    result = await rpc_sessions._handle_sessions_messages_search(
        {"key": KEY, "query": "needle"}, ctx
    )
    assert (result["session_id"], result["session_epoch"]) == (session.session_id, 0)
    assert result["matches"][0]["message_id"] == "mid"
    for expected in ({"session_id": "foreign"}, {"session_epoch": 1}):
        with pytest.raises(RpcHandlerError) as error:
            await rpc_sessions._handle_sessions_messages_search(
                {"key": KEY, "query": "needle", **expected},
                ctx,
            )
        assert error.value.code == "REBASE_REQUIRED"


async def test_search_rechecks_epoch_after_scanning(context, monkeypatch):
    ctx, storage, session = context
    await add(storage, session, "mid", 1)
    original = storage.read_message_search_body

    async def reset_after_read(*args, **kwargs):
        entry = await original(*args, **kwargs)
        await storage.conn.execute(
            "UPDATE sessions SET epoch = epoch + 1 WHERE session_key = ?", (KEY,)
        )
        await storage.conn.commit()
        return entry

    monkeypatch.setattr(storage, "read_message_search_body", reset_after_read)
    with pytest.raises(RpcHandlerError) as error:
        await rpc_sessions._handle_sessions_messages_search({"key": KEY, "query": "needle"}, ctx)
    assert error.value.code == "REBASE_REQUIRED"


@pytest.mark.parametrize("archived", [False, True])
async def test_anchor_window_is_bounded_with_both_usable_cursors(context, archived):
    ctx, storage, session = context
    for index in range(8):
        await add(storage, session, f"m-{index}", index)
    if archived:
        async with storage._write_transaction("archive-search-hit") as conn:
            await conn.execute("""
                INSERT INTO compacted_transcript_entries
                (session_id, session_key, message_id, role, content, created_at,
                 original_entry_id, content_byte_length, content_revision, archived_at)
                SELECT session_id, session_key, message_id, role, content, created_at,
                       id, content_byte_length, content_revision, 10
                FROM transcript_entries WHERE created_at <= 4
            """)
            await conn.execute("DELETE FROM transcript_entries WHERE created_at <= 4")
    result = await page(ctx, anchor_message_id="m-4")
    assert [item["message_id"] for item in result["items"]] == ["m-3", "m-4"]
    assert result["has_more_before"] and result["has_more_after"]
    assert len(json.dumps(result).encode()) < 65536
    earlier = await page(ctx, cursor=result["before_cursor"])
    later = await page(ctx, direction="after", cursor=result["after_cursor"])
    assert [item["message_id"] for item in earlier["items"]] == ["m-1", "m-2"]
    assert [item["message_id"] for item in later["items"]] == ["m-5", "m-6"]
    latest = await page(ctx, anchor_message_id="m-7")
    assert [item["message_id"] for item in latest["items"]] == ["m-6", "m-7"]
    assert not latest["has_more_after"]


async def test_anchor_cannot_escape_session_or_combine_with_cursor(context):
    ctx, storage, session = context
    other = await ctx.session_manager.create("agent:main:webchat:other-search")
    await add(storage, session, "mid", 1)
    await add(storage, other, "foreign", 1)
    for mid in ("foreign", "missing"):
        with pytest.raises(RpcHandlerError) as error:
            await page(ctx, anchor_message_id=mid)
        assert error.value.code == "HISTORY_STALE"
    result = await page(ctx, anchor_message_id="mid")
    with pytest.raises(ValueError):
        await page(ctx, anchor_message_id="mid", cursor=result["before_cursor"])


async def test_anchor_accounts_for_both_cursor_bytes_before_trimming(context):
    ctx, storage, session = context
    for index in range(3):
        await add(storage, session, f"m-{index}", index, "message " * 100)
    full = await page(ctx, anchor_message_id="m-1")
    budget = len(json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode()) - 10
    trimmed = await page(ctx, anchor_message_id="m-1", target_bytes=budget)
    assert [item["message_id"] for item in trimmed["items"]] == ["m-1"]
    assert trimmed["after_cursor"] and trimmed["before_cursor"]
    assert len(json.dumps(trimmed, ensure_ascii=False, separators=(",", ":")).encode()) <= budget


async def test_search_budget_deadline_and_pre_cancel_are_observed(context):
    ctx, storage, session = context
    await add(storage, session, "mid", 1)
    with recovery_read_scope("expired-search", deadline=time.monotonic() - 1):
        with pytest.raises(StorageBusyError) as error:
            await rpc_sessions._handle_sessions_messages_search(
                {"key": KEY, "query": "needle"}, ctx
            )
        assert error.value.stage == "deadline"
    import asyncio

    with recovery_read_scope("cancelled-search", deadline=time.monotonic() + 10) as budget:
        budget.cancel_token.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rpc_sessions._handle_sessions_messages_search(
                {"key": KEY, "query": "needle"}, ctx
            )
    await budget.drain()


async def test_message_search_requires_read_scope_and_guest_owned_key(context):
    ctx, _, _ = context
    assert METHOD_SCOPES["sessions.messages.search"] == READ_SCOPE
    ctx.principal = Principal(
        role="operator", scopes=frozenset(), is_owner=False, authenticated=True
    )
    denied = await get_dispatcher().dispatch(
        "no-read", "sessions.messages.search", {"key": KEY, "query": "needle"}, ctx
    )
    assert not denied.ok and denied.error.code == "UNAUTHORIZED"
    owner = "a" * 64
    ctx.principal = Principal(
        role="operator",
        scopes=frozenset({READ_SCOPE}),
        is_owner=False,
        authenticated=False,
        capabilities=frozenset({"guest.safe"}),
        auth_state="guest",
        guest_owner_id=owner,
        guest_session_key="osqg_" + "A" * 43,
    )
    own_key = f"agent:main:webchat:guest:{owner}:mine"
    assert (
        GuestRpcPolicy.authorize("sessions.messages.search", {"key": own_key}, ctx)["key"]
        == own_key
    )
    for key in (KEY, f"agent:main:webchat:guest:{'b' * 64}:mine"):
        with pytest.raises(GuestRpcPolicyError):
            GuestRpcPolicy.authorize("sessions.messages.search", {"key": key}, ctx)
