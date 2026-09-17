"""SessionManager.kill_session cascades to descendants by default and can opt out."""

from __future__ import annotations

import pytest

from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionNode, SessionStatus


class _MemoryStorage:
    def __init__(self) -> None:
        self.sessions: dict[str, SessionNode] = {}

    async def get_session(self, session_key: str) -> SessionNode | None:
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
        if agent_id is not None:
            rows = [r for r in rows if r.agent_id == agent_id]
        if spawned_by is not None:
            rows = [r for r in rows if r.spawned_by == spawned_by]
        return rows[offset : offset + limit]


class _StubAgentRegistry:
    def __init__(self, agent_subagents: dict[str, dict | None]) -> None:
        self._agents = agent_subagents

    async def list_agents(self, *, include_builtin: bool = True):
        out = []
        for agent_id, sub in self._agents.items():
            entry: dict = {"id": agent_id, "enabled": True}
            if sub is not None:
                entry["subagents"] = sub
            out.append(entry)
        return out


class _RecordingTaskRuntime:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel(self, *, session_key: str | None = None, task_id: str | None = None) -> int:
        if session_key:
            self.cancelled.append(session_key)
        return 1


def _running_node(session_key: str, agent_id: str, *, spawned_by: str | None = None) -> SessionNode:
    return SessionNode(
        session_key=session_key,
        session_id=session_key.replace(":", "-"),
        agent_id=agent_id,
        created_at=1,
        updated_at=1,
        started_at=1,
        status=SessionStatus.RUNNING,
        spawned_by=spawned_by,
    )


@pytest.mark.asyncio
async def test_kill_cascades_three_levels_by_default() -> None:
    storage = _MemoryStorage()
    storage.sessions["agent:main:main"] = _running_node("agent:main:main", "main")
    storage.sessions["agent:main:subagent:c1"] = _running_node(
        "agent:main:subagent:c1", "main", spawned_by="agent:main:main"
    )
    storage.sessions["agent:main:subagent:c2"] = _running_node(
        "agent:main:subagent:c2", "main", spawned_by="agent:main:main"
    )
    storage.sessions["agent:main:subagent:gc1"] = _running_node(
        "agent:main:subagent:gc1", "main", spawned_by="agent:main:subagent:c1"
    )

    registry = _StubAgentRegistry({"main": None})  # No policy → default cascade=True
    rt = _RecordingTaskRuntime()
    mgr = SessionManager(storage, agent_registry=registry, task_runtime=rt)  # type: ignore[arg-type]

    await mgr.kill_session("agent:main:main")

    for key in [
        "agent:main:main",
        "agent:main:subagent:c1",
        "agent:main:subagent:c2",
        "agent:main:subagent:gc1",
    ]:
        assert storage.sessions[key].status == SessionStatus.KILLED, key

    # Task runtime cancel called for every running descendant before its kill.
    assert sorted(rt.cancelled) == [
        "agent:main:subagent:c1",
        "agent:main:subagent:c2",
        "agent:main:subagent:gc1",
    ]


@pytest.mark.asyncio
async def test_cascade_opt_out_preserves_children() -> None:
    storage = _MemoryStorage()
    storage.sessions["agent:main:main"] = _running_node("agent:main:main", "main")
    storage.sessions["agent:main:subagent:c1"] = _running_node(
        "agent:main:subagent:c1", "main", spawned_by="agent:main:main"
    )

    registry = _StubAgentRegistry(
        {"main": {"cascade_on_parent_kill": False}},
    )
    rt = _RecordingTaskRuntime()
    mgr = SessionManager(storage, agent_registry=registry, task_runtime=rt)  # type: ignore[arg-type]

    await mgr.kill_session("agent:main:main")

    assert storage.sessions["agent:main:main"].status == SessionStatus.KILLED
    assert storage.sessions["agent:main:subagent:c1"].status == SessionStatus.RUNNING
    assert rt.cancelled == []


@pytest.mark.asyncio
async def test_kill_works_when_task_runtime_not_attached() -> None:
    storage = _MemoryStorage()
    storage.sessions["agent:main:main"] = _running_node("agent:main:main", "main")
    storage.sessions["agent:main:subagent:c1"] = _running_node(
        "agent:main:subagent:c1", "main", spawned_by="agent:main:main"
    )

    registry = _StubAgentRegistry({"main": None})
    mgr = SessionManager(storage, agent_registry=registry)  # type: ignore[arg-type]

    await mgr.kill_session("agent:main:main")

    assert storage.sessions["agent:main:main"].status == SessionStatus.KILLED
    assert storage.sessions["agent:main:subagent:c1"].status == SessionStatus.KILLED
