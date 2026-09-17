"""Read-time recovery of refused automatic titles without changing persisted data."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from opensquilla.gateway import rpc_sessions  # noqa: F401 - register session handlers
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.session_view import (
    build_session_view_item,
    derive_transcript_title,
    has_refused_chat_title,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode
from opensquilla.session.storage import SessionStorage

_REFUSALS = (
    "I cannot assist with that request",
    "I'm unable to provide assistance with this reque",
    "抱歉，我无法协助处理该请求",
)


def _context(manager: SessionManager) -> RpcContext:
    ctx = RpcContext(
        conn_id="title-recovery-test",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.admin"]),
            is_owner=True,
            authenticated=True,
        ),
        config=GatewayConfig(),
    )
    ctx.session_manager = manager
    return ctx


@pytest_asyncio.fixture
async def manager(tmp_path: Path) -> AsyncIterator[SessionManager]:
    storage = SessionStorage(str(tmp_path / "titles.db"))
    await storage.connect()
    try:
        yield SessionManager(storage, inject_time_prefix=False)
    finally:
        await storage.close()


@pytest.mark.parametrize("refusal", _REFUSALS)
@pytest.mark.parametrize("display_name", [None, "WebChat"])
@pytest.mark.asyncio
async def test_historical_refusal_recovers_list_search_and_fork_without_rewrite(
    manager: SessionManager, refusal: str, display_name: str | None
) -> None:
    key = "agent:main:webchat:historical-title"
    first_message = "整理示例文件的目录结构"
    await manager.create(key, display_name=display_name)
    await manager.append_message(key, "user", first_message)
    await manager.append_message(key, "assistant", "可以按照文件类别整理。")
    await manager.update(key, derived_title=refusal)
    storage = manager._storage
    before = await storage.get_session(key)
    assert before is not None
    stored_before = before.model_dump()
    ctx = _context(manager)
    dispatcher = get_dispatcher()

    listed = await dispatcher.dispatch("list", "sessions.list", None, ctx)
    assert listed.ok, listed.error
    row = next(row for row in listed.payload["sessions"] if row["key"] == key)
    assert row["title"] == first_message

    # Title matching remains based on persisted data, while its display recovers.
    title_query = "无法协助" if "无法协助" in refusal else "assist"
    searched = await dispatcher.dispatch(
        "title-search", "sessions.search", {"query": title_query}, ctx
    )
    assert searched.ok, searched.error
    hit = next(row for row in searched.payload["sessions"] if row["key"] == key)
    assert hit["title"] == first_message

    messages = await dispatcher.dispatch(
        "content-search", "sessions.search", {"query": "示例"}, ctx
    )
    assert messages.ok, messages.error
    message = next(row for row in messages.payload["messages"] if row["key"] == key)
    assert message["title"] == first_message
    after_reads = await storage.get_session(key)
    assert after_reads is not None
    assert after_reads.model_dump() == stored_before

    forked = await dispatcher.dispatch("fork", "sessions.fork", {"key": key}, ctx)
    assert forked.ok, forked.error
    child = await manager.get_session(forked.payload["key"])
    assert child is not None
    assert child.display_name == f"{first_message} (2)"
    parent = await storage.get_session(key)
    assert parent is not None
    assert parent.derived_title == refusal
    assert parent.display_name == display_name


@pytest.mark.parametrize(
    ("key", "display_name", "derived_title", "expected", "recover"),
    [
        ("agent:main:webchat:example", None, _REFUSALS[0], "Example first message", True),
        ("agent:main:webchat:example", "WebChat", _REFUSALS[0], "Example first message", True),
        ("agent:main:cli:example", None, _REFUSALS[0], "Example first message", True),
        ("agent:main:feishu:direct:example", None, _REFUSALS[0], "Example first message", True),
        ("agent:main:webchat:example", _REFUSALS[0], _REFUSALS[0], _REFUSALS[0], False),
        ("agent:main:webchat:example", "Chosen name", _REFUSALS[0], "Chosen name", False),
        ("agent:main:feishu:direct:example", "Chosen name", _REFUSALS[0], "Chosen name", False),
        ("agent:main:subagent:example", None, _REFUSALS[0], _REFUSALS[0], False),
        ("cron:example:run", None, _REFUSALS[0], _REFUSALS[0], False),
        ("agent:main:webchat:example", None, "I cannot log in", "I cannot log in", False),
        ("agent:main:webchat:example", None, "Analyze refusal responses",
         "Analyze refusal responses", False),
    ],
)
def test_title_recovery_respects_manual_names_and_session_kind(
    key: str,
    display_name: str | None,
    derived_title: str,
    expected: str,
    recover: bool,
) -> None:
    session = SessionNode(
        session_key=key, display_name=display_name, derived_title=derived_title
    )
    view = build_session_view_item(
        session,
        entry_count=1,
        task_rows=[],
        now_ms=1,
        transcript_title="Example first message",
    )
    assert view["title"] == expected
    assert has_refused_chat_title(session) is recover


@pytest.mark.parametrize(
    ("key", "display_name", "expected"),
    [
        ("agent:main:webchat:empty", None, "Web chat"),
        ("agent:main:webchat:empty", "WebChat", "Web chat"),
        ("agent:main:cli:empty", None, "CLI session"),
        ("agent:main:feishu:direct:empty", None, "Feishu conversation"),
    ],
)
def test_refused_title_without_visible_text_uses_existing_default(
    key: str, display_name: str | None, expected: str
) -> None:
    session = SessionNode(
        session_key=key, display_name=display_name, derived_title=_REFUSALS[0]
    )
    view = build_session_view_item(session, entry_count=0, task_rows=[], now_ms=1)
    assert view["title"] == expected


def test_custom_channel_title_uses_canonical_channel_classification() -> None:
    session = SessionNode(
        session_key="custom-channel-session",
        channel="team-chat",
        derived_title=_REFUSALS[0],
    )
    channel_types = {"team-chat": "feishu"}
    assert has_refused_chat_title(session, channel_types=channel_types)
    view = build_session_view_item(
        session,
        entry_count=1,
        task_rows=[],
        now_ms=1,
        transcript_title="Example first message",
        channel_types=channel_types,
    )
    assert view["title"] == "Example first message"


@pytest.mark.asyncio
async def test_recovery_keeps_batched_list_reads_and_existing_text_cleanup(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = manager._storage
    expected = {}
    for index, title in enumerate(["Valid title", _REFUSALS[0], _REFUSALS[1]]):
        key = f"agent:main:webchat:batch-{index}"
        await manager.create(key)
        text = "  Organize\n" + "example filenames " * 6
        await manager.append_message(key, "user", text)
        await manager.update(key, derived_title=title)
        expected[key] = title if index == 0 else derive_transcript_title(text)
    batch = AsyncMock(wraps=storage.list_user_transcript_content_batch)
    canonical_batch = AsyncMock(wraps=storage.list_canonical_user_transcript_content_batch)
    single = AsyncMock(wraps=storage.get_transcript)
    monkeypatch.setattr(storage, "list_user_transcript_content_batch", batch)
    monkeypatch.setattr(storage, "list_canonical_user_transcript_content_batch", canonical_batch)
    monkeypatch.setattr(storage, "get_transcript", single)

    listed = await get_dispatcher().dispatch("list", "sessions.list", None, _context(manager))

    assert listed.ok, listed.error
    assert {row["key"]: row["title"] for row in listed.payload["sessions"]} == expected
    batch.assert_awaited_once()
    assert len(batch.await_args.args[0]) == 1
    canonical_batch.assert_awaited_once()
    assert len(canonical_batch.await_args.args[0]) == 2
    single.assert_not_awaited()
    assert len(expected["agent:main:webchat:batch-1"]) == 34
