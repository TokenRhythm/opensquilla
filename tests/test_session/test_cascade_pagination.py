"""Cascade kill walks past a single page so >page_size descendants are caught."""

from __future__ import annotations

import pytest

from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode, SessionStatus


class _PaginatingStorage:
    """In-memory storage that paginates list_sessions(spawned_by=...) at 100/page."""

    PAGE_SIZE = 100

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
        limit=PAGE_SIZE,
        offset=0,
        spawned_by=None,
    ):
        rows = list(self.sessions.values())
        if status is not None:
            rows = [r for r in rows if str(r.status) == str(status)]
        if spawned_by is not None:
            rows = [r for r in rows if r.spawned_by == spawned_by]
        return rows[offset : offset + limit]


def _running_node(session_key: str, *, spawned_by: str | None = None) -> SessionNode:
    return SessionNode(
        session_key=session_key,
        session_id=session_key.replace(":", "-"),
        agent_id="main",
        created_at=1,
        updated_at=1,
        started_at=1,
        status=SessionStatus.RUNNING,
        spawned_by=spawned_by,
    )


@pytest.mark.asyncio
async def test_cascade_kills_descendants_beyond_first_page() -> None:
    """A parent with 250 running children — all must end killed after cascade."""
    storage = _PaginatingStorage()
    storage.sessions["agent:main:main"] = _running_node("agent:main:main")
    for i in range(250):
        key = f"agent:main:subagent:{i:04d}"
        storage.sessions[key] = _running_node(key, spawned_by="agent:main:main")

    class _Registry:
        async def list_agents(self, *, include_builtin: bool = True):
            return [{"id": "main", "enabled": True}]

    mgr = SessionManager(storage, agent_registry=_Registry())  # type: ignore[arg-type]

    await mgr.kill_session("agent:main:main")

    killed = sum(
        1 for s in storage.sessions.values() if s.status == SessionStatus.KILLED
    )
    # Parent + 250 children = 251 total kills.
    assert killed == 251
