"""sessions_spawn enforces per-agent subagent policy gates from PR 4.

Covers four gates:
  - allow_agents (None=skip, []=self-only, ["*"]=any, list=exact)
  - max_children_per_session (None=skip; reject when active >= cap)
  - model fallback chain (explicit > target.subagents.model > caller's None)
  - enforce_disabled_agents flag (off=skip; on=reject enabled=False targets)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from opensquilla.gateway.routing import tool_context_from_envelope
from opensquilla.provider.correlation_context import bind_provider_request_correlation
from opensquilla.provider.types import ProviderRequestCorrelation
from opensquilla.sandbox.run_context import (
    DomainGrant,
    MountGrant,
    PackageBundleGrant,
    PublicNetworkGrant,
    RunContext,
    TemporaryGrant,
)
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.tools.builtin import sessions as sessions_tool
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@dataclass
class _StubGatewayConfig:
    agents_defaults: object | None = None

    class _Subagents:
        enforce_disabled_agents = False

    subagents = _Subagents()


class _ConfigurableConfig:
    """Standalone config object that can flip enforce_disabled_agents."""

    class _SubagentsBlock:
        def __init__(self, enforce: bool) -> None:
            self.enforce_disabled_agents = enforce

    def __init__(self, *, enforce_disabled: bool = False) -> None:
        self.subagents = self._SubagentsBlock(enforce_disabled)
        self.agents_defaults = None


class _StubSessionManager:
    """Minimal session manager: drives get_agent_config + list_sessions
    + get_current_session + create + append_message.
    """

    def __init__(
        self,
        agents: dict[str, dict],
        active_children_count: int = 0,
    ) -> None:
        self._agents = agents
        self._active_children = active_children_count
        self.created: list[dict] = []

    async def get_agent_config(self, agent_id: str) -> dict | None:
        return self._agents.get(agent_id)

    async def get_current_session(self):
        return None

    async def get_session(self, session_key: str):
        return SimpleSession(
            session_key=session_key,
            session_id="session-stub",
            status="running",
            model="model-stub",
            model_provider="provider-stub",
            input_tokens=3,
            output_tokens=4,
            cache_read=5,
            cache_write=6,
            compaction_count=1,
            context_tokens=10,
            spawn_depth=0,
            started_at=100,
            runtime_ms=200,
        )

    async def list_sessions(self, agent_id=None, status=None, limit=100, offset=0):
        # Return ``self._active_children`` rows that look like running children
        # of ``agent:caller:main``.
        return [
            {"spawned_by": "agent:caller:main", "status": "running"}
            for _ in range(self._active_children)
        ]

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


@dataclass
class SimpleSession:
    session_key: str
    session_id: str
    status: str
    model: str
    model_provider: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    compaction_count: int
    context_tokens: int
    spawn_depth: int
    started_at: int
    runtime_ms: int
    estimated_cost_usd: float = 0.0


def _ctx(session_key: str = "agent:caller:main", agent_id: str = "caller") -> ToolContext:
    return ToolContext(
        is_owner=True,
        caller_kind=CallerKind.AGENT,
        subagent_depth=0,
        agent_id=agent_id,
        session_key=session_key,
        task_id="task-parent",
    )


@pytest.mark.asyncio
async def test_sessions_yield_does_not_end_turn_without_current_task_subagents() -> None:
    class _NoChildrenSessionManager:
        async def list_sessions(
            self,
            *,
            spawned_by: str | None = None,
            limit: int = 100,
            offset: int = 0,
        ) -> list[dict]:
            assert spawned_by == "agent:caller:main"
            return []

    sessions_tool.set_session_manager(_NoChildrenSessionManager())
    token = current_tool_context.set(_ctx())
    try:
        payload = json.loads(await sessions_tool.sessions_yield())
    finally:
        current_tool_context.reset(token)

    assert payload == {
        "status": "no_pending_subagents",
        "waited": False,
        "message": (
            "No subagents were spawned by the current task. Continue the current turn; "
            "do not wait for a previous task's subagents."
        ),
    }


@pytest.fixture(autouse=True)
def _wire_stubs(request):
    # Default no-op config; tests overwrite as needed.
    sessions_tool.set_gateway_config(_ConfigurableConfig())

    yield

    sessions_tool.set_session_manager(None)
    sessions_tool.set_task_runtime(None)
    sessions_tool.set_gateway_config(None)


# ── allow_agents ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_allow_agents_unset_permits_cross_agent_spawn() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {"id": "caller", "name": "Caller", "enabled": True},
            "worker": {"id": "worker", "name": "Worker", "enabled": True},
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_ctx())
    try:
        await sessions_tool.sessions_spawn(agent_id="worker", task="hi")
    finally:
        current_tool_context.reset(token)

    assert len(rt.enqueued) == 1


@pytest.mark.asyncio
async def test_sessions_spawn_propagates_owner_full_host_context_to_child() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {"id": "caller", "enabled": True},
            "worker": {"id": "worker", "enabled": True},
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)
    parent_ctx = _ctx()
    parent_ctx.run_mode = "full"
    parent_ctx.elevated = "full"
    parent_ctx.sandbox_run_context = RunContext(
        run_mode=RunMode.FULL,
        workspace="/tmp/opensquilla-workspace",
    )

    token = current_tool_context.set(parent_ctx)
    try:
        await sessions_tool.sessions_spawn(agent_id="worker", task="hi")
    finally:
        current_tool_context.reset(token)

    envelope = rt.enqueued[0]["envelope"]
    child_ctx = tool_context_from_envelope(envelope, is_owner=True)
    assert envelope.metadata["principal_is_owner"] is True
    assert envelope.metadata["run_mode"] == "full"
    assert envelope.metadata["elevated"] == "full"
    assert envelope.metadata["sandbox_run_context"]["run_mode"] == "full"
    assert child_ctx.run_mode == "full"
    assert child_ctx.elevated == "full"
    assert child_ctx.sandbox_run_context is not None
    assert child_ctx.sandbox_run_context.run_mode is RunMode.FULL


@pytest.mark.asyncio
async def test_sessions_spawn_does_not_propagate_once_or_temporary_grants(
    tmp_path: Path,
) -> None:
    mgr = _StubSessionManager(
        {
            "caller": {"id": "caller", "enabled": True},
            "worker": {"id": "worker", "enabled": True},
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)
    workspace = str(tmp_path / "workspace")
    durable_path = str(tmp_path / "durable")
    one_shot_path = str(tmp_path / "one-shot")
    temporary_path = str(tmp_path / "temporary")
    parent_ctx = _ctx()
    parent_ctx.run_mode = "trusted"
    parent_ctx.sandbox_mounts = [
        {"path": durable_path, "access": "ro", "scope": "chat"},
        {"path": one_shot_path, "access": "rw", "scope": "once"},
    ]
    parent_ctx.sandbox_run_context = RunContext(
        run_mode=RunMode.TRUSTED,
        workspace=workspace,
        mounts=(
            MountGrant(durable_path, scope="chat"),
            MountGrant(one_shot_path, access="rw", scope="once"),
        ),
        domains=(
            DomainGrant("durable.example", scope="workspace"),
            DomainGrant("once.example", scope="once"),
        ),
        bundles=(
            PackageBundleGrant("python-package-install", scope="workspace"),
            PackageBundleGrant("node-package-install", scope="once"),
        ),
        public_network=(
            PublicNetworkGrant(scope="chat"),
            PublicNetworkGrant(scope="once"),
        ),
        temporary_grants=(TemporaryGrant("mount", temporary_path, "fingerprint-1"),),
    )

    token = current_tool_context.set(parent_ctx)
    try:
        await sessions_tool.sessions_spawn(agent_id="worker", task="hi")
    finally:
        current_tool_context.reset(token)

    envelope = rt.enqueued[0]["envelope"]
    payload = envelope.metadata["sandbox_run_context"]
    assert all(item["scope"] != "once" for item in payload["mounts"])
    assert all(item["scope"] != "once" for item in payload["domains"])
    assert all(item["scope"] != "once" for item in payload["bundles"])
    assert all(item["scope"] != "once" for item in payload["public_network"])
    assert payload["temporary_grants"] == []
    assert envelope.metadata["sandbox_mounts"] == [
        {"path": durable_path, "access": "ro", "scope": "chat"}
    ]

    child_ctx = tool_context_from_envelope(envelope, is_owner=True)
    child_run_context = child_ctx.sandbox_run_context
    assert child_run_context is not None
    assert child_run_context.run_mode is RunMode.TRUSTED
    assert {grant.path for grant in child_run_context.mounts} == {durable_path}
    assert {grant.domain for grant in child_run_context.domains} == {"durable.example"}
    assert {grant.bundle_id for grant in child_run_context.bundles} == {"python-package-install"}
    assert child_run_context.temporary_grants == ()


@pytest.mark.asyncio
async def test_session_status_falls_back_to_context_session_key() -> None:
    mgr = _StubSessionManager({"caller": {"id": "caller", "enabled": True}})
    sessions_tool.set_session_manager(mgr)
    ctx = _ctx(session_key="agent:caller:webchat:status")

    token = current_tool_context.set(ctx)
    try:
        payload = await sessions_tool.session_status()
    finally:
        current_tool_context.reset(token)

    data = json.loads(payload)
    assert data["session_key"] == "agent:caller:webchat:status"
    assert data["total_tokens"] == 7
    assert data["cache_read"] == 5


@pytest.mark.asyncio
async def test_allow_agents_self_only_blocks_other_target() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {
                "id": "caller",
                "enabled": True,
                "subagents": {"allow_agents": []},
            },
            "worker": {"id": "worker", "enabled": True},
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_ctx())
    try:
        with pytest.raises(Exception, match="Cross-agent spawn not allowed"):
            await sessions_tool.sessions_spawn(agent_id="worker", task="hi")
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_allow_agents_self_only_permits_self_target() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {
                "id": "caller",
                "enabled": True,
                "subagents": {"allow_agents": []},
            },
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_ctx())
    try:
        await sessions_tool.sessions_spawn(agent_id="caller", task="hi")
    finally:
        current_tool_context.reset(token)
    assert len(rt.enqueued) == 1


@pytest.mark.asyncio
async def test_allow_agents_wildcard_permits_any() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {
                "id": "caller",
                "enabled": True,
                "subagents": {"allow_agents": ["*"]},
            },
            "worker": {"id": "worker", "enabled": True},
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_ctx())
    try:
        await sessions_tool.sessions_spawn(agent_id="worker", task="hi")
    finally:
        current_tool_context.reset(token)
    assert len(rt.enqueued) == 1


# ── max_children_per_session ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_max_children_rejects_when_at_cap() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {
                "id": "caller",
                "enabled": True,
                "subagents": {"max_children_per_session": 2},
            },
        },
        active_children_count=2,
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


@pytest.mark.asyncio
async def test_max_children_permits_below_cap() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {
                "id": "caller",
                "enabled": True,
                "subagents": {"max_children_per_session": 5},
            },
        },
        active_children_count=2,
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_ctx())
    try:
        await sessions_tool.sessions_spawn(task="hi")
    finally:
        current_tool_context.reset(token)
    assert len(rt.enqueued) == 1


@pytest.mark.asyncio
async def test_sessions_spawn_reuses_run_id_as_task_and_provider_execution() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {"id": "caller", "enabled": True},
            "worker": {"id": "worker", "enabled": True},
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)
    root = ProviderRequestCorrelation(
        session_id="parent-durable-session",
        turn_id="parent-root-turn",
        execution_id="parent-execution",
        call_kind="agent.chat",
    )

    token = current_tool_context.set(_ctx())
    try:
        with bind_provider_request_correlation(root):
            result = json.loads(
                await sessions_tool.sessions_spawn(agent_id="worker", task="hi")
            )
    finally:
        current_tool_context.reset(token)

    queued = rt.enqueued[0]
    run_id = queued["task_id"]
    correlation = queued["provider_request_correlation"]
    assert isinstance(correlation, ProviderRequestCorrelation)
    assert result["task_id"] == run_id
    assert queued["envelope"].metadata["run_id"] == run_id
    assert queued["envelope"].input_provenance["run_id"] == run_id
    assert correlation.session_id == root.session_id
    assert correlation.turn_id == root.turn_id
    assert correlation.execution_id == run_id
    assert correlation.call_kind == "subagent.chat"


# ── model fallback chain ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_fallback_uses_target_subagents_model() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {"id": "caller", "enabled": True},
            "worker": {
                "id": "worker",
                "enabled": True,
                "subagents": {"model": "haiku"},
            },
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_ctx())
    try:
        await sessions_tool.sessions_spawn(agent_id="worker", task="hi")
    finally:
        current_tool_context.reset(token)

    assert mgr.created
    assert mgr.created[0]["model"] == "haiku"


@pytest.mark.asyncio
async def test_explicit_model_wins_over_fallback() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {"id": "caller", "enabled": True},
            "worker": {
                "id": "worker",
                "enabled": True,
                "subagents": {"model": "haiku"},
            },
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)

    token = current_tool_context.set(_ctx())
    try:
        await sessions_tool.sessions_spawn(agent_id="worker", task="hi", model="opus")
    finally:
        current_tool_context.reset(token)

    assert mgr.created[0]["model"] == "opus"


# ── enforce_disabled_agents flag ────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_target_allowed_when_flag_off() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {"id": "caller", "enabled": True},
            "worker": {"id": "worker", "enabled": False},
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)
    sessions_tool.set_gateway_config(_ConfigurableConfig(enforce_disabled=False))

    token = current_tool_context.set(_ctx())
    try:
        await sessions_tool.sessions_spawn(agent_id="worker", task="hi")
    finally:
        current_tool_context.reset(token)
    assert len(rt.enqueued) == 1


@pytest.mark.asyncio
async def test_disabled_target_rejected_when_flag_on() -> None:
    mgr = _StubSessionManager(
        {
            "caller": {"id": "caller", "enabled": True},
            "worker": {"id": "worker", "enabled": False},
        }
    )
    rt = _StubTaskRuntime()
    sessions_tool.set_session_manager(mgr)
    sessions_tool.set_task_runtime(rt)
    sessions_tool.set_gateway_config(_ConfigurableConfig(enforce_disabled=True))

    token = current_tool_context.set(_ctx())
    try:
        with pytest.raises(Exception, match="is disabled"):
            await sessions_tool.sessions_spawn(agent_id="worker", task="hi")
    finally:
        current_tool_context.reset(token)
