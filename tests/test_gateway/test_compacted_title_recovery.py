"""Historical refusal title recovery follows canonical, compacted chat history."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from opensquilla.gateway import rpc_sessions  # noqa: F401 - register session handlers
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

_REFUSAL = "I'm unable to provide assistance with this request"
_TRUNCATED_REFUSAL = "I'm unable to provide assistance with this reque"
_FIRST_MESSAGE = "Inspect sample database layout"


def _context(manager: SessionManager) -> RpcContext:
    context = RpcContext(
        conn_id="compacted-title-test",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        config=GatewayConfig(),
    )
    context.session_manager = manager
    return context


@pytest_asyncio.fixture
async def manager(tmp_path: Path) -> AsyncIterator[SessionManager]:
    storage = SessionStorage(str(tmp_path / "compacted-titles.db"))
    await storage.connect()
    try:
        yield SessionManager(storage, inject_time_prefix=False)
    finally:
        await storage.close()


async def _compact(manager: SessionManager, key: str, *, keep_tail: bool = True) -> None:
    source = await manager.capture_compaction_source(key)
    kept_entries = (
        [{"role": source.entries[-1].role, "content": source.entries[-1].content}]
        if keep_tail
        else []
    )
    assert await manager.persist_compaction_result(
        key,
        "Earlier synthetic tasks are complete.",
        kept_entries,
        removed_count=len(source.entries) - len(kept_entries),
        source_entries=source.entries,
        source_preimage=source.preimage,
        source_context_fingerprint=source.context_fingerprint,
    )


async def _seed_compacted(
    manager: SessionManager,
    key: str,
    *,
    title: str = _REFUSAL,
    display_name: str | None = None,
) -> str:
    node = await manager.create(key, display_name=display_name)
    await manager.append_message(key, "user", _FIRST_MESSAGE)
    await manager.append_message(key, "assistant", "The sample has two tables.")
    await manager.append_message(key, "user", "Inspect network sample settings")
    await manager.update(key, derived_title=title)
    await _compact(manager, key)
    assert (await manager.get_transcript(key))[0].content != _FIRST_MESSAGE
    assert (await manager.get_canonical_transcript(key))[0].content == _FIRST_MESSAGE
    return node.session_id


async def _dispatch(manager: SessionManager, method: str, params: Any = None) -> dict[str, Any]:
    result = await get_dispatcher().dispatch(method, method, params, _context(manager))
    assert result.ok, result.error
    return result.payload


async def _assert_display_paths(manager: SessionManager, key: str) -> None:
    listed = await _dispatch(manager, "sessions.list")
    assert next(row for row in listed["sessions"] if row["key"] == key)["title"] == (
        _FIRST_MESSAGE
    )

    previews = await _dispatch(manager, "sessions.preview", {"keys": [key]})
    assert previews["previews"][0]["title"] == _FIRST_MESSAGE

    # The persisted title still matches search; only the projected title changes.
    titles = await _dispatch(manager, "sessions.search", {"query": "assistance"})
    assert next(row for row in titles["sessions"] if row["key"] == key)["title"] == (
        _FIRST_MESSAGE
    )
    messages = await _dispatch(manager, "sessions.search", {"query": "network"})
    assert next(row for row in messages["messages"] if row["key"] == key)["title"] == (
        _FIRST_MESSAGE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [_REFUSAL, _TRUNCATED_REFUSAL])
async def test_compaction_recovery_survives_repeated_compaction_reopen_and_fork(
    manager: SessionManager,
    refusal: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "agent:main:webchat:archived-first-message"
    await _seed_compacted(manager, key, title=refusal)
    storage = manager._storage

    for round_index in range(2):
        if round_index:
            await manager.append_message(key, "assistant", "The network sample is valid.")
            await manager.append_message(key, "user", "Inspect updated network sample settings")
            await _compact(manager, key)
        before = await storage.get_session(key)
        canonical_before = await manager.get_canonical_transcript(key)
        assert before is not None
        with monkeypatch.context() as patch:
            full_active = AsyncMock(side_effect=AssertionError("unbounded active history read"))
            full_canonical = AsyncMock(
                side_effect=AssertionError("unbounded canonical history read")
            )
            patch.setattr(storage, "get_transcript", full_active)
            patch.setattr(storage, "get_canonical_transcript", full_canonical)
            await _assert_display_paths(manager, key)
            full_active.assert_not_awaited()
            full_canonical.assert_not_awaited()
        assert await storage.get_session(key) == before
        assert await manager.get_canonical_transcript(key) == canonical_before

    await storage.close()
    await storage.connect()
    reopened_manager = SessionManager(storage, inject_time_prefix=False)
    await _assert_display_paths(reopened_manager, key)
    assert await storage.get_session(key) == before

    forked = await _dispatch(reopened_manager, "sessions.fork", {"key": key})
    child = await reopened_manager.get_session(forked["key"])
    assert child is not None
    assert child.display_name == f"{_FIRST_MESSAGE} (2)"
    parent = await storage.get_session(key)
    assert parent is not None
    assert parent.derived_title == refusal
    assert parent.display_name is None


@pytest.mark.asyncio
async def test_compacted_title_batch_contains_only_affected_sessions(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = [
        ("refused", _REFUSAL, None, _FIRST_MESSAGE),
        ("truncated", _TRUNCATED_REFUSAL, "WebChat", _FIRST_MESSAGE),
        ("manual-refusal", _REFUSAL, _REFUSAL, _REFUSAL),
        ("manual", _REFUSAL, "Chosen sample name", "Chosen sample name"),
        ("valid", "I cannot log in", None, "I cannot log in"),
    ]
    expected: dict[str, str] = {}
    affected_ids: set[str] = set()
    for index, (suffix, title, display_name, expected_title) in enumerate(cases):
        key = f"agent:main:webchat:batch-{suffix}"
        session_id = await _seed_compacted(manager, key, title=title, display_name=display_name)
        expected[key] = expected_title
        if index < 2:
            affected_ids.add(session_id)
    storage = manager._storage
    before = await storage.list_sessions()
    canonical_batch = AsyncMock(wraps=storage.list_canonical_user_transcript_content_batch)
    active_batch = AsyncMock(wraps=storage.list_user_transcript_content_batch)
    full_active = AsyncMock(side_effect=AssertionError("per-session active history read"))
    full_canonical = AsyncMock(side_effect=AssertionError("per-session canonical history read"))
    monkeypatch.setattr(storage, "list_canonical_user_transcript_content_batch", canonical_batch)
    monkeypatch.setattr(storage, "list_user_transcript_content_batch", active_batch)
    monkeypatch.setattr(storage, "get_transcript", full_active)
    monkeypatch.setattr(storage, "get_canonical_transcript", full_canonical)

    for method, collection in [("sessions.list", "sessions"), ("sessions.preview", "previews")]:
        result = await _dispatch(manager, method)
        assert {row["key"]: row["title"] for row in result[collection]} == expected
        canonical_batch.assert_awaited_once()
        assert set(canonical_batch.await_args.args[0]) == affected_ids
        assert canonical_batch.await_args.kwargs == {"limit_per_session": 3}
        canonical_batch.reset_mock()
        if method == "sessions.list":
            active_batch.assert_awaited_once()
            assert not (set(active_batch.await_args.args[0]) & affected_ids)
            active_batch.reset_mock()
        else:
            active_batch.assert_not_awaited()
    full_active.assert_not_awaited()
    full_canonical.assert_not_awaited()
    assert await storage.list_sessions() == before


@pytest.mark.asyncio
async def test_normal_titles_do_not_add_canonical_reads(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "agent:main:webchat:normal-compacted-title"
    await _seed_compacted(manager, key, title="Sample database guide")
    storage = manager._storage
    canonical_batch = AsyncMock(side_effect=AssertionError("unnecessary canonical title read"))
    active_batch = AsyncMock(wraps=storage.list_user_transcript_content_batch)
    monkeypatch.setattr(storage, "list_canonical_user_transcript_content_batch", canonical_batch)
    monkeypatch.setattr(storage, "list_user_transcript_content_batch", active_batch)

    listed = await _dispatch(manager, "sessions.list")
    assert listed["sessions"][0]["title"] == "Sample database guide"
    active_batch.assert_awaited_once()
    active_batch.reset_mock()
    preview = await _dispatch(manager, "sessions.preview")
    assert preview["previews"][0]["title"] == "Sample database guide"
    active_batch.assert_not_awaited()
    canonical_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_only_title_does_not_fall_back_to_per_session_reads(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "agent:main:webchat:archive-only-title"
    await _seed_compacted(manager, key)
    await _compact(manager, key, keep_tail=False)
    assert await manager.get_transcript(key) == []
    storage = manager._storage
    before = await storage.get_session(key)
    full_active = AsyncMock(side_effect=AssertionError("per-session active history read"))
    full_canonical = AsyncMock(side_effect=AssertionError("per-session canonical history read"))
    active_batch = AsyncMock(side_effect=AssertionError("unnecessary active title read"))
    monkeypatch.setattr(storage, "get_transcript", full_active)
    monkeypatch.setattr(storage, "get_canonical_transcript", full_canonical)
    monkeypatch.setattr(storage, "list_user_transcript_content_batch", active_batch)

    listed = await _dispatch(manager, "sessions.list")
    assert listed["sessions"][0]["title"] == _FIRST_MESSAGE
    preview = await _dispatch(manager, "sessions.preview")
    assert preview["previews"][0]["title"] == _FIRST_MESSAGE
    assert preview["previews"][0]["lastMessage"] == ""
    full_active.assert_not_awaited()
    full_canonical.assert_not_awaited()
    active_batch.assert_not_awaited()
    assert await storage.get_session(key) == before
