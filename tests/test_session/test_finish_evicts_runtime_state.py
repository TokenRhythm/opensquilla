"""Session runtime disposal drops process-wide per-session bookkeeping."""

from __future__ import annotations

import importlib

import pytest

from opensquilla.engine import cache_break_monitor
from opensquilla.engine.steps.squilla_router import _history_store
from opensquilla.gateway.subagent_announce import _tracker
from opensquilla.provider import ChatConfig, Message
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionStatus


class _MemoryStorage:
    def __init__(self) -> None:
        self._sessions: dict[str, object] = {}

    async def get_session(self, session_key: str):
        return self._sessions.get(session_key)

    async def upsert_session(self, node, *, expected_session_id=None) -> None:
        assert expected_session_id is None or expected_session_id == node.session_id
        self._sessions[node.session_key] = node


def _seed_cache_break_state(session_key: str) -> cache_break_monitor.CacheBreakMonitor:
    monitor = cache_break_monitor.default_cache_break_monitor
    snapshot = monitor.record_prompt_state(
        messages=[Message(role="user", content="old"), Message(role="user", content="now")],
        tools=None,
        config=ChatConfig(system="stable system"),
        model="model-a",
    )
    monitor.check_response_for_cache_break(session_key, snapshot, 5000)
    monitor.notify_compaction(session_key)
    return monitor


@pytest.mark.asyncio
async def test_finish_evicts_spawn_group_tracker_and_routing_history() -> None:
    from opensquilla.session.models import SessionNode

    storage = _MemoryStorage()
    node = SessionNode(
        session_key="agent:main:main",
        session_id="abc",
        agent_id="main",
        created_at=1,
        updated_at=1,
        started_at=1,
        status=SessionStatus.RUNNING,
    )
    await storage.upsert_session(node)

    _tracker.mark_closed("agent:main:main", "task-X")
    _history_store.set("agent:main:main", [{"turn_index": 0}])
    assert _tracker.is_closed("agent:main:main", "task-X")
    assert _history_store.get("agent:main:main") is not None

    mgr = SessionManager(storage)  # type: ignore[arg-type]
    await mgr.finish("agent:main:main", status=SessionStatus.DONE)

    assert not _tracker.is_closed("agent:main:main", "task-X")
    assert _history_store.get("agent:main:main") is None


@pytest.mark.asyncio
async def test_finish_evicts_cache_break_monitor_state() -> None:
    from opensquilla.session.models import SessionNode

    storage = _MemoryStorage()
    session_key = "agent:main:cache-break-finish"
    node = SessionNode(
        session_key=session_key,
        session_id="cache-break-generation",
        agent_id="main",
        created_at=1,
        updated_at=1,
        started_at=1,
        status=SessionStatus.RUNNING,
    )
    await storage.upsert_session(node)
    monitor = _seed_cache_break_state(session_key)

    try:
        manager = SessionManager(storage)  # type: ignore[arg-type]
        await manager.finish(session_key, status=SessionStatus.DONE)

        assert monitor.evict(session_key) is False
    finally:
        monitor.evict(session_key)


def test_explicit_runtime_eviction_drops_all_session_identity_caches() -> None:
    from opensquilla.tools.builtin import sessions as sessions_tool

    meta_resolution = importlib.import_module(
        "opensquilla.engine.steps.meta_resolution"
    )
    session_key = "agent:main:webchat:history-eviction"
    session_id = "history-eviction-generation"
    manager = SessionManager(_MemoryStorage())  # type: ignore[arg-type]
    manager.set_cached_epoch(session_key, 7)
    _tracker.mark_closed(session_key, "child-task")
    _history_store.set(session_key, [{"turn_index": 3}])
    sessions_tool._get_spawn_lock(session_key)
    meta_resolution._sticky_put(session_id, "meta-skill", "follow up")

    manager.evict_session_runtime_state(
        session_key,
        session_id=session_id,
    )

    assert manager.get_cached_epoch(session_key) is None
    assert not _tracker.is_closed(session_key, "child-task")
    assert _history_store.get(session_key) is None
    assert session_key not in sessions_tool._spawn_locks
    assert session_id not in meta_resolution._meta_sticky_cache


def test_runtime_eviction_drops_session_key_cache_without_generation_id() -> None:
    session_key = "agent:main:webchat:cache-break-no-generation"
    manager = SessionManager(_MemoryStorage())  # type: ignore[arg-type]
    monitor = _seed_cache_break_state(session_key)

    try:
        manager.evict_session_runtime_state(session_key)

        assert monitor.evict(session_key) is False
    finally:
        monitor.evict(session_key)
