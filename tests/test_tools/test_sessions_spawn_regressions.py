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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.types import DoneEvent
from opensquilla.gateway.boot import dispatch_task_runtime_turn
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.task_runtime import TaskRun
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    DomainGrant,
    MountGrant,
    RunContext,
    TemporaryGrant,
)
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
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
                "message": message,
                "mode": mode,
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


@pytest.mark.asyncio
async def test_spawned_child_restart_uses_persisted_inherited_authority_at_boot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.escalation import (
        current_tool_run_context,
        reset_resolved_run_context_overlays,
    )
    from opensquilla.sandbox.network_guard import decide_network_access
    from opensquilla.sandbox.operation_runtime import SandboxOperationResult
    from opensquilla.tools.builtin import filesystem

    reset_resolved_run_context_overlays()
    database = tmp_path / "sessions.db"
    workspace = tmp_path / "global-workspace"
    mounted = tmp_path / "parent-scoped-mount"
    one_shot = tmp_path / "parent-once-mount"
    for path in (workspace, mounted, one_shot):
        path.mkdir()
    config = GatewayConfig(
        workspace_dir=str(workspace),
        sandbox={"run_mode": "full"},
        memory={"flush_enabled": False},
        naming={"enabled": False},
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )
    storage = await SessionStorage.open(str(database))
    manager = SessionManager(storage, inject_time_prefix=False)
    parent_key = "agent:main:webchat:spawn-parent-restart"
    parent_context = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        mounts=(
            MountGrant(path=str(mounted), access="rw", scope="chat"),
            MountGrant(path=str(one_shot), access="rw", scope="once"),
        ),
        domains=(
            DomainGrant(
                domain="inherited.example",
                scope="chat",
                source="manual",
            ),
            DomainGrant(
                domain="parent-once.example",
                scope="once",
                source="manual",
            ),
        ),
        temporary_grants=(
            TemporaryGrant(
                kind="domain",
                value="temporary.example",
                fingerprint="parent-fingerprint",
            ),
        ),
        run_mode_source="user",
        source="resolved_overlay",
    )
    await manager.create(
        parent_key,
        origin={RUN_CONTEXT_ORIGIN_KEY: parent_context.to_origin_payload()},
    )
    runtime = _StubTaskRuntime()
    sessions_tool.set_gateway_config(config)
    sessions_tool.set_session_manager(manager)
    sessions_tool.set_task_runtime(runtime)
    parent_tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        agent_id="main",
        workspace_dir=str(workspace),
        run_mode="standard",
        sandbox_mounts=parent_context.to_origin_payload()["mounts"],
        sandbox_run_context=parent_context,
        session_key=parent_key,
        task_id="parent-task-restart",
    )
    setattr(parent_tool_context, "_sandbox_run_context_fresh", True)
    token = current_tool_context.set(parent_tool_context)
    try:
        spawned = json.loads(
            await sessions_tool.sessions_spawn(task="exercise inherited authority")
        )
    finally:
        current_tool_context.reset(token)
    child_key = spawned["session_key"]
    queued = runtime.enqueued[0]
    queued_run = TaskRun(
        task_id=spawned["task_id"],
        envelope=queued["envelope"],
        message=queued["message"],
        queue_mode=queued["mode"],
        run_kind=queued["run_kind"],
    )

    # Mutating the parent after spawn must not affect the isolated child row.
    await manager.update(
        parent_key,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: RunContext(
                run_mode=RunMode.FULL,
                workspace=str(workspace),
                source="saved",
            ).to_origin_payload()
        },
    )
    await storage.close()

    restarted_storage = await SessionStorage.open(str(database))
    restarted_manager = SessionManager(restarted_storage, inject_time_prefix=False)
    backend_operations: list[Any] = []

    class RecordingFilesystemBackend:
        name = "recording-filesystem"

        def operation_domains_supported(self) -> frozenset[str]:
            return frozenset({"filesystem"})

        async def run_operation(self, operation: Any) -> SandboxOperationResult:
            backend_operations.append(operation)
            request = operation.request
            assert request.path is not None
            request.path.write_text(request.content, encoding="utf-8")
            return SandboxOperationResult(
                message=f"sandboxed write: {request.path}",
                created=True,
            )

    monkeypatch.setattr(
        filesystem,
        "get_runtime",
        lambda: SimpleNamespace(
            effective=SimpleNamespace(sandbox_enabled=True),
            backend=RecordingFilesystemBackend(),
            settings=SimpleNamespace(host_root_readonly=False),
            workspace=workspace,
        ),
    )
    observations: dict[str, Any] = {}
    target = mounted / "child-write.txt"

    class Runner:
        async def run(self, message: str, session_key: str, **kwargs: Any):
            tool_context = kwargs["tool_context"]
            child_token = current_tool_context.set(tool_context)
            try:
                effective = current_tool_run_context()
                assert effective is not None
                observations.update(
                    {
                        "mode": effective.run_mode,
                        "source": effective.source,
                        "run_mode_source": effective.run_mode_source,
                        "mounts": effective.mounts,
                        "granted_network": decide_network_access(
                            "inherited.example",
                            effective,
                        ),
                        "unknown_network": decide_network_access(
                            "not-inherited.example",
                            effective,
                        ),
                        "write": await filesystem.write_file(
                            str(target),
                            "inherited",
                        ),
                        "caller_kind": tool_context.caller_kind,
                        "subagent_depth": tool_context.subagent_depth,
                    }
                )
            finally:
                current_tool_context.reset(child_token)
            yield DoneEvent()

    async def emit(*args: Any, **kwargs: Any) -> None:
        return None

    try:
        await dispatch_task_runtime_turn(
            queued_run,
            config=config,
            session_manager=restarted_manager,
            turn_runner=Runner(),
            event_emitter=emit,
        )

        assert observations["mode"] is RunMode.STANDARD
        assert observations["source"] == "route_metadata"
        assert observations["run_mode_source"] == "user"
        assert [(grant.path, grant.access, grant.scope) for grant in observations["mounts"]] == [
            (str(mounted), "rw", "chat")
        ]
        assert observations["granted_network"].reason == "domain_grant"
        assert observations["unknown_network"].status == "ask"
        assert observations["unknown_network"].reason == "unknown_domain"
        assert observations["caller_kind"] is CallerKind.SUBAGENT
        assert observations["subagent_depth"] == 1
        assert len(backend_operations) == 1
        assert target.read_text(encoding="utf-8") == "inherited"

        child = await restarted_storage.get_session(child_key)
        assert child is not None
        assert child.spawn_depth == 1
        assert child.parent_session_key == parent_key
        assert child.origin is not None
        assert child.origin["parent_task_id"] == "parent-task-restart"
        assert child.origin[RUN_CONTEXT_ORIGIN_KEY]["run_mode"] == "standard"
        assert child.origin[RUN_CONTEXT_ORIGIN_KEY]["run_mode_source"] == "user"
        assert child.origin[RUN_CONTEXT_ORIGIN_KEY]["mounts"] == [
            {"path": str(mounted), "access": "rw", "scope": "chat"}
        ]
        assert child.origin[RUN_CONTEXT_ORIGIN_KEY]["domains"] == [
            {
                "domain": "inherited.example",
                "scope": "chat",
                "source": "manual",
            }
        ]
        assert child.origin[RUN_CONTEXT_ORIGIN_KEY]["temporary_grants"] == []
        assert queued_run.envelope.input_provenance == {
            "kind": "subagent_task",
            "parent_session_key": parent_key,
            "run_id": spawned["task_id"],
            "parent_task_id": "parent-task-restart",
        }
    finally:
        await restarted_storage.close()
        reset_resolved_run_context_overlays()


@pytest.mark.asyncio
async def test_project_spawned_child_persists_binding_and_revalidates_queued_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.project_workspaces import (
        ProjectWorkspaceStateError,
        project_path_key,
    )
    from opensquilla.sandbox.escalation import (
        current_tool_run_context,
        reset_resolved_run_context_overlays,
    )
    from opensquilla.sandbox.network_guard import decide_network_access
    from opensquilla.sandbox.operation_runtime import SandboxOperationResult
    from opensquilla.tools.builtin import filesystem

    reset_resolved_run_context_overlays()
    database = tmp_path / "project-sessions.db"
    global_workspace = tmp_path / "global-workspace"
    project_path = tmp_path / "project"
    stale_workspace = tmp_path / "stale-parent-origin"
    mounted = tmp_path / "project-parent-scoped-mount"
    for path in (global_workspace, project_path, stale_workspace, mounted):
        path.mkdir()
    config = GatewayConfig(
        workspace_dir=str(global_workspace),
        sandbox={"run_mode": "full"},
        memory={"flush_enabled": False},
        naming={"enabled": False},
        agent_stream_heartbeat_interval_seconds=0.0,
        agent_stream_idle_timeout_seconds=1.0,
    )
    storage = await SessionStorage.open(str(database))
    manager = SessionManager(storage, inject_time_prefix=False)
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path),
        path_key=project_path_key(project_path, strict=True),
        display_name="Project",
        trusted_at=1,
    )
    parent_key = "agent:main:webchat:project-spawn-parent"
    await manager.create(
        parent_key,
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: RunContext(
                run_mode=RunMode.FULL,
                workspace=str(stale_workspace),
                run_mode_source="user",
                source="saved",
            ).to_origin_payload()
        },
    )
    authoritative_parent = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(project_path),
        mounts=(MountGrant(path=str(mounted), access="rw", scope="chat"),),
        domains=(
            DomainGrant(
                domain="project-inherited.example",
                scope="chat",
                source="manual",
            ),
        ),
        run_mode_source="project_default",
        source="resolved_overlay",
    )
    runtime = _StubTaskRuntime()
    sessions_tool.set_gateway_config(config)
    sessions_tool.set_session_manager(manager)
    sessions_tool.set_task_runtime(runtime)
    parent_tool_context = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        agent_id="main",
        workspace_dir=str(project_path),
        run_mode="standard",
        sandbox_mounts=authoritative_parent.to_origin_payload()["mounts"],
        sandbox_run_context=authoritative_parent,
        session_key=parent_key,
        task_id="project-parent-task",
    )
    setattr(parent_tool_context, "_sandbox_run_context_fresh", True)
    token = current_tool_context.set(parent_tool_context)
    try:
        spawned = json.loads(
            await sessions_tool.sessions_spawn(task="exercise project authority")
        )
    finally:
        current_tool_context.reset(token)
    child_key = spawned["session_key"]
    queued = runtime.enqueued[0]
    queued_run = TaskRun(
        task_id=spawned["task_id"],
        envelope=queued["envelope"],
        message=queued["message"],
        queue_mode=queued["mode"],
        run_kind=queued["run_kind"],
    )

    # Parent rebinding after spawn cannot change the child's isolated binding.
    await storage.bind_session_workspace(parent_key, None)
    await storage.close()
    restarted_storage = await SessionStorage.open(str(database))
    restarted_manager = SessionManager(restarted_storage, inject_time_prefix=False)
    backend_operations: list[Any] = []

    class RecordingFilesystemBackend:
        name = "recording-filesystem"

        def operation_domains_supported(self) -> frozenset[str]:
            return frozenset({"filesystem"})

        async def run_operation(self, operation: Any) -> SandboxOperationResult:
            backend_operations.append(operation)
            request = operation.request
            assert request.path is not None
            request.path.write_text(request.content, encoding="utf-8")
            return SandboxOperationResult(
                message=f"sandboxed write: {request.path}",
                created=True,
            )

    monkeypatch.setattr(
        filesystem,
        "get_runtime",
        lambda: SimpleNamespace(
            effective=SimpleNamespace(sandbox_enabled=True),
            backend=RecordingFilesystemBackend(),
            settings=SimpleNamespace(host_root_readonly=False),
            workspace=project_path,
        ),
    )
    observations: list[dict[str, Any]] = []
    target = mounted / "project-child-write.txt"

    class Runner:
        async def run(self, message: str, session_key: str, **kwargs: Any):
            tool_context = kwargs["tool_context"]
            child_token = current_tool_context.set(tool_context)
            try:
                effective = current_tool_run_context()
                assert effective is not None
                observations.append(
                    {
                        "mode": effective.run_mode,
                        "workspace": effective.workspace,
                        "run_mode_source": effective.run_mode_source,
                        "network": decide_network_access(
                            "project-inherited.example",
                            effective,
                        ),
                        "write": await filesystem.write_file(
                            str(target),
                            "project-inherited",
                        ),
                    }
                )
            finally:
                current_tool_context.reset(child_token)
            yield DoneEvent()

    emitted: list[tuple[Any, ...]] = []

    async def emit(*args: Any, **kwargs: Any) -> None:
        emitted.append(args)

    try:
        await dispatch_task_runtime_turn(
            queued_run,
            config=config,
            session_manager=restarted_manager,
            turn_runner=Runner(),
            event_emitter=emit,
        )
        child = await restarted_storage.get_session(child_key)
        assert child is not None
        assert child.workspace_id == project.workspace_id
        assert child.origin is not None
        assert child.origin[RUN_CONTEXT_ORIGIN_KEY]["workspace"] == str(project_path)
        assert child.origin[RUN_CONTEXT_ORIGIN_KEY]["run_mode"] == "standard"
        assert observations == [
            {
                "mode": RunMode.STANDARD,
                "workspace": str(project_path),
                "run_mode_source": "project_default",
                "network": observations[0]["network"],
                "write": f"sandboxed write: {target}",
            }
        ]
        assert observations[0]["network"].reason == "domain_grant"
        assert len(backend_operations) == 1
        assert target.read_text(encoding="utf-8") == "project-inherited"

        await restarted_storage.remove_project_workspace(project.workspace_id)
        with pytest.raises(ProjectWorkspaceStateError, match="removed"):
            await dispatch_task_runtime_turn(
                TaskRun(
                    task_id="project-child-retry",
                    envelope=queued_run.envelope,
                    message="queued after project removal",
                    run_kind="subagent",
                ),
                config=config,
                session_manager=restarted_manager,
                turn_runner=Runner(),
                event_emitter=emit,
            )
        assert len(observations) == 1
        assert emitted
    finally:
        await restarted_storage.close()
        reset_resolved_run_context_overlays()
