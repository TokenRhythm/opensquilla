"""SessionManager.finish drops the per-parent spawn lock to bound memory."""

from __future__ import annotations

import pytest

from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode, SessionStatus
from opensquilla.tools.builtin import sessions as sessions_tool


class _MemoryStorage:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionNode] = {}

    async def get_session(self, session_key: str):
        return self.sessions.get(session_key)

    async def upsert_session(
        self,
        node: SessionNode,
        *,
        expected_session_id: str | None = None,
    ) -> None:
        assert expected_session_id is None or expected_session_id == node.session_id
        self.sessions[node.session_key] = node

    async def list_sessions(
        self,
        agent_id=None,
        status=None,
        limit=100,
        offset=0,
        spawned_by=None,
    ):
        rows = list(self.sessions.values())
        if status is not None:
            rows = [r for r in rows if str(r.status) == str(status)]
        if spawned_by is not None:
            rows = [r for r in rows if r.spawned_by == spawned_by]
        return rows[offset : offset + limit]


@pytest.mark.asyncio
async def test_finish_evicts_spawn_lock() -> None:
    """Locks created during sessions_spawn must drop when the parent ends."""
    storage = _MemoryStorage()
    storage.sessions["agent:main:main"] = SessionNode(
        session_key="agent:main:main",
        session_id="abc",
        agent_id="main",
        created_at=1,
        updated_at=1,
        started_at=1,
        status=SessionStatus.RUNNING,
    )

    # Simulate sessions_spawn having created a per-parent lock.
    sessions_tool._get_spawn_lock("agent:main:main")
    assert "agent:main:main" in sessions_tool._spawn_locks

    mgr = SessionManager(storage)  # type: ignore[arg-type]
    await mgr.finish("agent:main:main", status=SessionStatus.DONE)

    assert "agent:main:main" not in sessions_tool._spawn_locks


def test_evict_spawn_lock_is_idempotent() -> None:
    sessions_tool._spawn_locks.clear()
    assert sessions_tool.evict_spawn_lock("never:set") is False
    sessions_tool._get_spawn_lock("agent:main:main")
    assert sessions_tool.evict_spawn_lock("agent:main:main") is True
    assert sessions_tool.evict_spawn_lock("agent:main:main") is False
