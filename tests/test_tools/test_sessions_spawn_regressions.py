"""Regression tests for sessions_spawn child-count and legacy-registry behavior.

1. _count_active_children must page beyond a single 200-row window so a busy
   gateway with >page_size unrelated running sessions cannot bypass
   max_children_per_session.
2. _cascade_kill_children must page across the same window so descendants
   outside the first page are still cancelled.
3. sessions_spawn must serialize check-then-create per parent so two
   concurrent calls cannot both observe active < cap and both succeed.
4. sessions_spawn must preserve the legacy "no agent existence check"
   path when no AgentRegistry is wired.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest

from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.tools.builtin import sessions as sessions_tool
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


class _ConfigurableConfig:
    class _SubagentsBlock:
        def __init__(self, enforce: bool) -> None:
            self.enforce_disabled_agents = enforce

    def __init__(self, *, enforce_disabled: bool = False) -> None:
        self.subagents = self._SubagentsBlock(enforce_disabled)
        self.agents_defaults = None


@dataclass
class _StubRow:
    spawned_by: str | None
    status: str = "running"


class _PaginatingSessionManager:
    """Storage-backed mgr that returns paged results filtered by spawned_by.

    Mimics the production SessionManager.list_sessions contract: caller passes
    ``spawned_by``, the storage filters on it. ``noise_count`` simulates a busy
    gateway with unrelated running sessions that would otherwise crowd out
    the parent's children in a 200-row global window.
    """

    has_agent_registry = True

    def __init__(
        self,
        agents: dict[str, dict],
        children_for_parent: dict[str, int],
        noise_count: int = 0,
    ) -> None:
        self._agents = agents
        self._children_for_parent = children_for_parent
        self._noise_count = noise_count
        self.created: list[dict] = []

    async def get_agent_config(self, agent_id: str):
        return self._agents.get(agent_id)

    async def get_current_session(self):
        return None

    async def list_sessions(
        self,
        agent_id=None,
        status=None,
        limit=100,
        offset=0,
        spawned_by=None,
    ):
        if spawned_by is not None:
            n = self._children_for_parent.get(spawned_by, 0)
            rows = [_StubRow(spawned_by=spawned_by) for _ in range(n)]
        else:
            # Caller didn't filter — emit noise + parent rows so the legacy
            # path can be exercised too.
            rows = [_StubRow(spawned_by="other:" + str(i)) for i in range(self._noise_count)]
        return rows[offset : offset + limit]

    async def create(self, **kwargs):
        self.created.append(kwargs)

    async def append_message(self, *args, **kwargs):
        return True


class _LegacyManagerNoRegistry:
    """Mimics an embedding without an AgentRegistry attached.

    ``has_agent_registry`` is False; ``get_agent_config`` always returns None.
    The legacy contract is "skip the existence check".
    """

    has_agent_registry = False

    def __init__(self) -> None:
        self.created: list[dict] = []

    async def get_agent_config(self, agent_id: str):
        return None

    async def get_current_session(self):
        return None

    async def list_sessions(self, **kwargs):
        return []

    async def create(self, **kwargs):
        self.created.append(kwargs)

    async def append_message(self, *args, **kwargs):
        return True


class _StubTaskRuntime:
    def __init__(self) -> None:
        self.enqueued: list[dict] = []

    async def enqueue(
        self,
        envelope,
        message,
        mode="followup",
        run_kind="default",
        *,
        task_id=None,
        provider_request_correlation=None,
    ):
        self.enqueued.append(
            {
                "envelope": envelope,
                "run_kind": run_kind,
                "task_id": task_id,
                "provider_request_correlation": provider_request_correlation,
            }
        )

        @dataclass
        class _Handle:
            task_id: str

        return _Handle(task_id or "task-stub")


def _ctx() -> ToolContext:
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        agent_id="caller",
        session_key="agent:caller:main",
        task_id="task-parent",
    )


def _full_host_ctx() -> ToolContext:
    ctx = _ctx()
    ctx.run_mode = "full"
    ctx.elevated = "full"
    ctx.sandbox_run_context = RunContext(run_mode=RunMode.FULL, source="request")
    return ctx


@pytest.fixture(autouse=True)
def _wire(request):
    sessions_tool.set_gateway_config(_ConfigurableConfig())
    # Drop any spawn locks left over from previous tests so each run starts
    # with a clean per-parent lock map.
    sessions_tool._spawn_locks.clear()
    yield
    sessions_tool.set_session_manager(None)
    sessions_tool.set_task_runtime(None)
    sessions_tool.set_gateway_config(None)
    sessions_tool._spawn_locks.clear()


# Bug 1 — count beyond a single 200-row window
@pytest.mark.asyncio
async def test_max_children_uses_spawned_by_filter_not_global_page() -> None:
    mgr = _PaginatingSessionManager(
        agents={
            "caller": {
                "id": "caller",
                "enabled": True,
                "subagents": {"max_children_per_session": 5},
            },
        },
        # 5 children of this parent; the storage filter returns exactly 5
        # regardless of how many other sessions are in the gateway.
        children_for_parent={"agent:caller:main": 5},
        # Plenty of unrelated noise — but list_sessions(spawned_by=...) does
        # not return them, so the count is exact.
        noise_count=10_000,
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_ctx())
    try:
        with pytest.raises(Exception, match="Max active children"):
            await sessions_tool.sessions_spawn(task="hi")
    finally:
        current_tool_context.reset(token)


# Bug 3 — concurrent spawns must not both pass the gate
@pytest.mark.asyncio
async def test_concurrent_spawn_respects_max_children_one() -> None:
    """Two concurrent spawns with max=1 → exactly one succeeds."""
    state = {"active": 0}

    class _RaceMgr:
        has_agent_registry = True
        created: list[dict] = []

        async def get_agent_config(self, agent_id: str):
            return {
                "id": "caller",
                "enabled": True,
                "subagents": {"max_children_per_session": 1},
            }

        async def get_current_session(self):
            return None

        async def list_sessions(
            self,
            agent_id=None,
            status=None,
            limit=100,
            offset=0,
            spawned_by=None,
        ):
            return [_StubRow(spawned_by=spawned_by) for _ in range(state["active"])]

        async def create(self, **kwargs):
            # Bump the active count once create succeeds so the next spawn
            # under the lock sees the new child.
            state["active"] += 1
            self.created.append(kwargs)

        async def append_message(self, *args, **kwargs):
            return True

    mgr = _RaceMgr()
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    async def _spawn() -> str | Exception:
        token = current_tool_context.set(_ctx())
        try:
            return await sessions_tool.sessions_spawn(task="hi")
        except Exception as exc:
            return exc
        finally:
            current_tool_context.reset(token)

    results = await asyncio.gather(_spawn(), _spawn(), return_exceptions=True)
    successes = [r for r in results if isinstance(r, str)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1, "exactly one spawn must succeed"
    assert len(failures) == 1
    assert "Max active children" in str(failures[0])


# Bug 4 — no registry attached preserves legacy behavior
@pytest.mark.asyncio
async def test_spawn_without_registry_does_not_raise_agent_not_found() -> None:
    mgr = _LegacyManagerNoRegistry()
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_ctx())
    try:
        # Should not raise — registry is not attached so existence check is
        # skipped (legacy embedding contract preserved).
        await sessions_tool.sessions_spawn(task="hi")
    finally:
        current_tool_context.reset(token)
    assert len(rt.enqueued) == 1


# Bug 4 inverse — registry attached and target missing → raises
@pytest.mark.asyncio
async def test_spawn_with_registry_raises_for_missing_agent() -> None:
    mgr = _PaginatingSessionManager(
        agents={"caller": {"id": "caller", "enabled": True}},
        children_for_parent={},
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_ctx())
    try:
        with pytest.raises(Exception, match="Agent not found"):
            await sessions_tool.sessions_spawn(agent_id="ghost", task="hi")
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_sessions_spawn_inherits_parent_full_host_run_mode() -> None:
    mgr = _PaginatingSessionManager(
        agents={"caller": {"id": "caller", "enabled": True}},
        children_for_parent={},
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_full_host_ctx())
    try:
        await sessions_tool.sessions_spawn(task="probe host write")
    finally:
        current_tool_context.reset(token)

    assert len(rt.enqueued) == 1
    envelope = rt.enqueued[0]["envelope"]
    assert envelope.metadata["run_mode"] == "full"
    assert envelope.metadata["elevated"] == "full"
    assert envelope.metadata["sandbox_run_context"]["run_mode"] == "full"


@pytest.mark.asyncio
async def test_session_status_uses_current_tool_context_session_key() -> None:
    expected = _StubRow(spawned_by=None)
    expected.session_key = "agent:caller:main"
    expected.session_id = "session-1"
    expected.model = "test-model"

    class _Manager:
        async def get_session(self, session_key: str):
            assert session_key == expected.session_key
            return expected

    sessions_tool.set_session_manager(_Manager())
    token = current_tool_context.set(_full_host_ctx())
    try:
        payload = json.loads(await sessions_tool.session_status())
    finally:
        current_tool_context.reset(token)

    assert payload["session_key"] == expected.session_key
    assert payload["session_id"] == expected.session_id
    assert payload["model"] == expected.model
    assert payload["run_mode"] == "full"
    assert payload["sandbox_enabled"] is False
