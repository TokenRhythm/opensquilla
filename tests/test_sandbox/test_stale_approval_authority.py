from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.gateway.config import GatewayConfig
from opensquilla.project_workspaces import ProjectWorkspaceStateError, project_path_key
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderTextDelta
from opensquilla.sandbox.escalation import (
    apply_sandbox_approval_choice,
    build_network_approval_params,
    build_package_bundle_approval_params,
    build_path_approval_params,
    consume_temporary_network_grant,
    current_tool_run_context,
    prune_once_mount_grants,
    request_sandbox_approval,
    reset_resolved_run_context_overlays,
    resolved_run_context_overlay,
)
from opensquilla.sandbox.network_guard import NetworkDecision
from opensquilla.sandbox.path_validation import MountDecision, decide_path_access
from opensquilla.sandbox.run_context import (
    RUN_CONTEXT_ORIGIN_KEY,
    DomainGrant,
    MountGrant,
    PackageBundleGrant,
    PublicNetworkGrant,
    RunContext,
    persist_run_context,
    run_context_from_origin_payload,
)
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import ToolContext, current_tool_context


@pytest.fixture(autouse=True)
def _reset_approval_state():
    from opensquilla.gateway.approval_queue import reset_approval_queue

    reset_approval_queue()
    reset_resolved_run_context_overlays()
    yield
    reset_approval_queue()
    reset_resolved_run_context_overlays()


class _RecordingSessionManager:
    def __init__(self, manager: SessionManager) -> None:
        self._manager = manager
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def storage(self) -> SessionStorage:
        return self._manager.storage

    async def get_session(self, session_key: str):
        return await self._manager.get_session(session_key)

    async def update(self, session_key: str, **fields: Any):
        self.update_calls.append((session_key, fields))
        return await self._manager.update(session_key, **fields)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)


class _LifecycleProvider:
    provider_name = "fake"

    def __init__(self, outcome: str) -> None:
        self._outcome = outcome
        self.started = asyncio.Event()

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        self.started.set()
        if self._outcome == "error":
            raise RuntimeError("provider lifecycle failure")
        if self._outcome == "cancel":
            await asyncio.Event().wait()
            return
        yield ProviderTextDelta(text="done")
        yield ProviderDone(
            stop_reason="stop",
            input_tokens=1,
            output_tokens=1,
        )

    async def list_models(self) -> list[Any]:
        return []


def _config(workspace: Path) -> GatewayConfig:
    return GatewayConfig(
        workspace_dir=str(workspace),
        sandbox={"run_mode": "standard"},
        memory={"flush_enabled": False},
        naming={"enabled": False},
    )


async def _create_session(
    tmp_path: Path,
    context: RunContext,
    *,
    workspace_id: str | None = None,
    suffix: str = "approval",
) -> tuple[SessionStorage, SessionManager, Any]:
    storage = await SessionStorage.open(str(tmp_path / f"{suffix}.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create(
        f"agent:main:webchat:{suffix}",
        workspace_id=workspace_id,
        origin={RUN_CONTEXT_ORIGIN_KEY: context.to_origin_payload()},
    )
    return storage, manager, node


def _fresh_tool_context(
    node: Any,
    context: RunContext,
    *,
    execution_id: str,
) -> ToolContext:
    tool_context = ToolContext(
        is_owner=True,
        session_key=node.session_key,
        workspace_dir=context.workspace,
        sandbox_run_context=context,
        artifact_session_id=node.session_id,
    )
    setattr(tool_context, "session_epoch", int(node.epoch or 0))
    setattr(tool_context, "workspace_id", node.workspace_id)
    setattr(tool_context, "execution_id", execution_id)
    setattr(tool_context, "_sandbox_run_context_fresh", True)
    return tool_context


def _request(
    params: dict[str, object],
    *,
    node: Any,
    context: RunContext,
    execution_id: str,
) -> dict[str, object]:
    token = current_tool_context.set(
        _fresh_tool_context(
            node,
            context,
            execution_id=execution_id,
        )
    )
    try:
        approval = request_sandbox_approval(
            params,
            message="Approve the exact sandbox target.",
        )
    finally:
        current_tool_context.reset(token)
    assert approval is not None
    return approval


async def _durable_context(manager: SessionManager, session_key: str) -> RunContext:
    node = await manager.get_session(session_key)
    assert node is not None
    assert node.origin is not None
    context = run_context_from_origin_payload(
        node.origin[RUN_CONTEXT_ORIGIN_KEY],
        source="saved",
        preserve_materialized_user_grants=True,
    )
    assert context is not None
    return context


def _state_context(
    workspace: Path,
    *,
    mount: Path,
    domain: str,
    bundle: str,
    public_scope: str,
    public_source: str,
    run_mode: RunMode,
    run_mode_source: str,
) -> RunContext:
    return RunContext(
        run_mode=run_mode,
        workspace=str(workspace),
        mounts=(MountGrant(path=str(mount), access="ro", scope="chat"),),
        domains=(DomainGrant(domain=domain, scope="chat", source="manual"),),
        bundles=(
            PackageBundleGrant(
                bundle_id=bundle,
                scope="chat",
                source="manual",
            ),
        ),
        public_network=(
            PublicNetworkGrant(
                scope=public_scope,
                source=public_source,
            ),
        ),
        run_mode_source=run_mode_source,
        source="saved",
    )


@pytest.mark.asyncio
async def test_stale_approval_cannot_mutate_recreated_same_key_session(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    requested = tmp_path / "requested"
    workspace.mkdir()
    requested.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, original = await _create_session(
        tmp_path,
        initial,
        suffix="same-key",
    )
    params = build_path_approval_params(
        MountDecision(
            status="request",
            normalized_path=str(requested),
            access="rw",
            reason="outside_sandbox_mounts",
        ),
        session_key=original.session_key,
        workspace=str(workspace),
    )
    assert params is not None
    approval = _request(
        params,
        node=original,
        context=initial,
        execution_id="execution-old",
    )

    try:
        await storage.delete_session(original.session_key)
        replacement = await manager.create(
            original.session_key,
            origin={RUN_CONTEXT_ORIGIN_KEY: initial.to_origin_payload()},
        )
        assert replacement.session_id != original.session_id
        before = copy.deepcopy(replacement.origin)

        with pytest.raises(ProjectWorkspaceStateError, match="unavailable"):
            await apply_sandbox_approval_choice(
                params,
                approval_id=str(approval["approval_id"]),
                choice="allow_same_type",
                approved=True,
                session_manager=manager,
                config=_config(workspace),
            )

        current = await manager.get_session(original.session_key)
        assert current is not None
        assert current.session_id == replacement.session_id
        assert current.origin == before
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_raw_queue_approval_id_without_generation_fails_closed(
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="raw-queue-id",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="raw.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-raw",
    )
    assert params is not None
    approval_id = get_approval_queue().request(namespace="exec", params=params)

    try:
        with pytest.raises(ProjectWorkspaceStateError, match="unavailable"):
            await apply_sandbox_approval_choice(
                params,
                approval_id=approval_id,
                choice="allow_once",
                approved=True,
                session_manager=manager,
                config=_config(workspace),
            )
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_generation_reset_expires_real_rpc_approval(tmp_path: Path) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="generation-reset",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="reset.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-reset",
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-reset",
    )
    approval_id = str(approval["approval_id"])
    reset_resolved_run_context_overlays()
    rpc_context = RpcContext(
        conn_id="stale-approval-test",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.approvals"}),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=_config(workspace),
    )

    try:
        payload = await _handle_exec_approval_resolve(
            {
                "id": approval_id,
                "approved": True,
                "choice": "allow_same_type",
            },
            rpc_context,
        )

        expired = get_approval_queue().get(approval_id)
        assert expired.resolved is True
        assert expired.approved is False
        assert expired.resolution == "expired"
        assert expired.claim_token is None
        assert payload["resolution"] == "expired"
        assert (await _durable_context(manager, node.session_key)).domains == ()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_turn_cleanup_first_prevents_real_rpc_same_type_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve
    from opensquilla.sandbox import escalation as escalation_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="cleanup-first-rpc",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="cleanup-first.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-cleanup-first",
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-cleanup-first",
    )
    approval_id = str(approval["approval_id"])
    rpc_context = RpcContext(
        conn_id="cleanup-first-rpc",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.approvals"}),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=_config(workspace),
    )
    cas_entered = asyncio.Event()
    real_compare_and_set = storage.compare_and_set_session_origin

    async def _paused_compare_and_set(**kwargs: Any):
        cas_entered.set()
        return await real_compare_and_set(**kwargs)

    monkeypatch.setattr(
        storage,
        "compare_and_set_session_origin",
        _paused_compare_and_set,
    )

    try:
        cleanup_task = asyncio.create_task(
            escalation_module.clear_approval_run_context_deltas_for_tool_context(
                _fresh_tool_context(
                    node,
                    initial,
                    execution_id="execution-cleanup-first",
                )
            )
        )
        await cleanup_task

        payload = await _handle_exec_approval_resolve(
            {
                "id": approval_id,
                "approved": True,
                "choice": "allow_same_type",
            },
            rpc_context,
        )

        assert not cas_entered.is_set()
        expired = get_approval_queue().get(approval_id)
        assert expired.resolved is True
        assert expired.approved is False
        assert expired.resolution == "expired"
        assert expired.claim_token is None
        assert payload["resolved"] is True
        assert payload["approved"] is False
        assert payload["resolution"] == "expired"
        assert (await _durable_context(manager, node.session_key)).domains == ()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_real_rpc_same_type_apply_first_blocks_cancelled_turn_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve
    from opensquilla.sandbox import escalation as escalation_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="apply-first-rpc",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="apply-first.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-apply-first",
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-apply-first",
    )
    approval_id = str(approval["approval_id"])
    rpc_context = RpcContext(
        conn_id="apply-first-rpc",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.approvals"}),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=_config(workspace),
    )
    cas_entered = asyncio.Event()
    release_cas = asyncio.Event()
    real_compare_and_set = storage.compare_and_set_session_origin

    async def _paused_compare_and_set(**kwargs: Any):
        cas_entered.set()
        await release_cas.wait()
        return await real_compare_and_set(**kwargs)

    monkeypatch.setattr(
        storage,
        "compare_and_set_session_origin",
        _paused_compare_and_set,
    )
    cleanup_started = asyncio.Event()
    cleanup_completed = asyncio.Event()
    real_cleanup = escalation_module.clear_approval_run_context_deltas_for_tool_context

    def _recording_cleanup(context: ToolContext | None):
        cleanup_started.set()
        result = real_cleanup(context)
        if not inspect.isawaitable(result):
            cleanup_completed.set()
            return result

        async def _record_completion() -> int:
            try:
                return await result
            finally:
                cleanup_completed.set()

        return _record_completion()

    monkeypatch.setattr(
        escalation_module,
        "clear_approval_run_context_deltas_for_tool_context",
        _recording_cleanup,
    )
    rpc_task = asyncio.create_task(
        _handle_exec_approval_resolve(
            {
                "id": approval_id,
                "approved": True,
                "choice": "allow_same_type",
            },
            rpc_context,
        )
    )
    provider = _LifecycleProvider("cancel")
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        session_key=node.session_key,
        tool_context=_fresh_tool_context(
            node,
            initial,
            execution_id="execution-apply-first",
        ),
    )

    async def _run() -> list[Any]:
        return [event async for event in agent.run_turn("cancel this turn")]

    turn_task: asyncio.Task[list[Any]] | None = None
    try:
        await cas_entered.wait()
        turn_task = asyncio.create_task(_run())
        await provider.started.wait()
        turn_task.cancel()
        await cleanup_started.wait()

        assert not cleanup_completed.is_set()
        assert not turn_task.done()

        # Repeated registry cancellation must not pierce the shield and cancel
        # the cleanup task while it is waiting on the approval lock.
        turn_task.cancel()
        await asyncio.sleep(0)
        turn_task.cancel()
        await asyncio.sleep(0)

        release_cas.set()
        payload = await asyncio.wait_for(rpc_task, timeout=1.0)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn_task, timeout=1.0)

        assert payload["resolved"] is True
        assert payload["approved"] is True
        queue_entry = get_approval_queue().get(approval_id)
        assert queue_entry.resolved is True
        assert queue_entry.approved is True
        assert queue_entry.claim_token is None
        assert {
            (grant.domain, grant.scope)
            for grant in (await _durable_context(manager, node.session_key)).domains
        } == {("apply-first.example", "chat")}
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_GENERATIONS
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS
    finally:
        release_cas.set()
        for task in (rpc_task, turn_task):
            if task is not None and not task.done():
                task.cancel()
        await storage.close()


@pytest.mark.asyncio
async def test_repeated_turn_cancel_revokes_generation_and_expires_rpc_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve
    from opensquilla.sandbox import escalation as escalation_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="repeat-cancel-reopen",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="repeat-cancel.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-repeat-cancel",
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-repeat-cancel",
    )
    approval_id = str(approval["approval_id"])
    rpc_context = RpcContext(
        conn_id="repeat-cancel-reopen",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.approvals"}),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=_config(workspace),
    )
    cas_entered = asyncio.Event()
    release_first_cas = asyncio.Event()
    cas_attempts = 0
    real_compare_and_set = storage.compare_and_set_session_origin

    async def _fail_first_compare_and_set(**kwargs: Any):
        nonlocal cas_attempts
        cas_attempts += 1
        if cas_attempts == 1:
            cas_entered.set()
            await release_first_cas.wait()
            return None
        return await real_compare_and_set(**kwargs)

    monkeypatch.setattr(
        storage,
        "compare_and_set_session_origin",
        _fail_first_compare_and_set,
    )
    cleanup_started = asyncio.Event()
    real_cleanup = escalation_module.clear_approval_run_context_deltas_for_tool_context

    def _recording_cleanup(context: ToolContext | None):
        cleanup_started.set()
        return real_cleanup(context)

    monkeypatch.setattr(
        escalation_module,
        "clear_approval_run_context_deltas_for_tool_context",
        _recording_cleanup,
    )
    rpc_task = asyncio.create_task(
        _handle_exec_approval_resolve(
            {
                "id": approval_id,
                "approved": True,
                "choice": "allow_same_type",
            },
            rpc_context,
        )
    )
    provider = _LifecycleProvider("cancel")
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        session_key=node.session_key,
        tool_context=_fresh_tool_context(
            node,
            initial,
            execution_id="execution-repeat-cancel",
        ),
    )

    async def _run() -> list[Any]:
        return [event async for event in agent.run_turn("cancel this turn")]

    turn_task: asyncio.Task[list[Any]] | None = None
    try:
        await cas_entered.wait()
        turn_task = asyncio.create_task(_run())
        await provider.started.wait()
        turn_task.cancel()
        await cleanup_started.wait()

        turn_task.cancel()
        await asyncio.sleep(0)
        turn_task.cancel()
        await asyncio.sleep(0)

        release_first_cas.set()
        payload = await asyncio.wait_for(rpc_task, timeout=1.0)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn_task, timeout=1.0)

        expired = get_approval_queue().get(approval_id)
        assert expired.resolved is True
        assert expired.approved is False
        assert expired.resolution == "expired"
        assert expired.claim_token is None
        assert payload["resolution"] == "expired"

        retry_payload = await _handle_exec_approval_resolve(
            {
                "id": approval_id,
                "approved": True,
                "choice": "allow_same_type",
            },
            rpc_context,
        )

        assert (await _durable_context(manager, node.session_key)).domains == ()
        assert retry_payload["resolution"] == "expired"
        assert cas_attempts == 1
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_GENERATIONS
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS
        expired = get_approval_queue().get(approval_id)
        assert expired.resolved is True
        assert expired.approved is False
        assert expired.claim_token is None
    finally:
        release_first_cas.set()
        for task in (rpc_task, turn_task):
            if task is not None and not task.done():
                task.cancel()
        await storage.close()


@pytest.mark.asyncio
async def test_path_allow_once_is_zero_write_latest_delta_and_execution_scoped(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    revoked_mount = tmp_path / "revoked-mount"
    added_mount = tmp_path / "added-mount"
    requested = tmp_path / "requested"
    for path in (workspace, revoked_mount, added_mount, requested):
        path.mkdir()
    initial = _state_context(
        workspace,
        mount=revoked_mount,
        domain="revoked.example",
        bundle="python-package-install",
        public_scope="chat",
        public_source="captured",
        run_mode=RunMode.STANDARD,
        run_mode_source="operator_default",
    )
    latest = _state_context(
        workspace,
        mount=added_mount,
        domain="added.example",
        bundle="node-package-install",
        public_scope="chat",
        public_source="latest",
        run_mode=RunMode.TRUSTED,
        run_mode_source="user",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="path-once",
    )
    params = build_path_approval_params(
        MountDecision(
            status="request",
            normalized_path=str(requested),
            access="rw",
            reason="outside_sandbox_mounts",
        ),
        session_key=node.session_key,
        workspace=str(workspace),
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-path",
    )
    await persist_run_context(manager, node.session_key, latest)
    before = await manager.get_session(node.session_key)
    assert before is not None
    before_origin = copy.deepcopy(before.origin)
    recording = _RecordingSessionManager(manager)

    try:
        await apply_sandbox_approval_choice(
            params,
            approval_id=str(approval["approval_id"]),
            choice="allow_once",
            approved=True,
            session_manager=recording,
            config=_config(workspace),
        )

        assert recording.update_calls == []
        after = await manager.get_session(node.session_key)
        assert after is not None
        assert after.origin == before_origin

        active_token = current_tool_context.set(
            _fresh_tool_context(
                after,
                latest,
                execution_id="execution-path",
            )
        )
        try:
            effective = current_tool_run_context()
        finally:
            current_tool_context.reset(active_token)
        assert effective is not None
        assert effective.run_mode is RunMode.TRUSTED
        assert {grant.path for grant in effective.mounts} == {
            str(added_mount),
            str(requested),
        }
        assert {grant.domain for grant in effective.domains} == {"added.example"}
        assert {grant.bundle_id for grant in effective.bundles} == {"node-package-install"}
        assert {(grant.scope, grant.source) for grant in effective.public_network} == {
            ("chat", "latest")
        }

        other_token = current_tool_context.set(
            _fresh_tool_context(
                after,
                latest,
                execution_id="execution-other",
            )
        )
        try:
            other_execution = current_tool_run_context()
        finally:
            current_tool_context.reset(other_token)
        assert other_execution is not None
        assert {grant.path for grant in other_execution.mounts} == {str(added_mount)}

        cleanup_token = current_tool_context.set(
            _fresh_tool_context(
                after,
                latest,
                execution_id="execution-path",
            )
        )
        try:
            assert prune_once_mount_grants(node.session_key) == 1
        finally:
            current_tool_context.reset(cleanup_token)
        expired_token = current_tool_context.set(
            _fresh_tool_context(
                after,
                latest,
                execution_id="execution-path",
            )
        )
        try:
            expired = current_tool_run_context()
        finally:
            current_tool_context.reset(expired_token)
        assert expired is not None
        assert {grant.path for grant in expired.mounts} == {str(added_mount)}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_path_once_cleanup_is_execution_scoped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    requested_a = tmp_path / "requested-a"
    requested_b = tmp_path / "requested-b"
    for path in (workspace, requested_a, requested_b):
        path.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="parallel-path-cleanup",
    )

    async def _approve(path: Path, execution_id: str) -> None:
        params = build_path_approval_params(
            MountDecision(
                status="request",
                normalized_path=str(path),
                access="ro",
                reason="outside_sandbox_mounts",
            ),
            session_key=node.session_key,
            workspace=str(workspace),
        )
        assert params is not None
        approval = _request(
            params,
            node=node,
            context=initial,
            execution_id=execution_id,
        )
        await apply_sandbox_approval_choice(
            params,
            approval_id=str(approval["approval_id"]),
            choice="allow_once",
            approved=True,
            session_manager=manager,
            config=_config(workspace),
        )

    await _approve(requested_a, "execution-a")
    await _approve(requested_b, "execution-b")
    current = await manager.get_session(node.session_key)
    assert current is not None

    try:
        token_a = current_tool_context.set(
            _fresh_tool_context(
                current,
                initial,
                execution_id="execution-a",
            )
        )
        try:
            assert prune_once_mount_grants(node.session_key) == 1
            effective_a = current_tool_run_context()
        finally:
            current_tool_context.reset(token_a)
        assert effective_a is not None
        assert effective_a.mounts == ()

        token_b = current_tool_context.set(
            _fresh_tool_context(
                current,
                initial,
                execution_id="execution-b",
            )
        )
        try:
            effective_b = current_tool_run_context()
        finally:
            current_tool_context.reset(token_b)
        assert effective_b is not None
        assert {grant.path for grant in effective_b.mounts} == {str(requested_b)}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_network_allow_once_consumes_only_target_without_captured_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    revoked_mount = tmp_path / "revoked-mount"
    added_mount = tmp_path / "added-mount"
    for path in (workspace, revoked_mount, added_mount):
        path.mkdir()
    initial = _state_context(
        workspace,
        mount=revoked_mount,
        domain="revoked.example",
        bundle="python-package-install",
        public_scope="chat",
        public_source="captured",
        run_mode=RunMode.STANDARD,
        run_mode_source="operator_default",
    )
    latest = _state_context(
        workspace,
        mount=added_mount,
        domain="added.example",
        bundle="node-package-install",
        public_scope="chat",
        public_source="latest",
        run_mode=RunMode.TRUSTED,
        run_mode_source="user",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="network-once",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="target.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-network",
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-network",
    )
    await persist_run_context(manager, node.session_key, latest)
    before = await manager.get_session(node.session_key)
    assert before is not None
    before_origin = copy.deepcopy(before.origin)
    recording = _RecordingSessionManager(manager)

    try:
        await apply_sandbox_approval_choice(
            params,
            approval_id=str(approval["approval_id"]),
            choice="allow_once",
            approved=True,
            session_manager=recording,
            config=_config(workspace),
        )
        assert recording.update_calls == []
        after = await manager.get_session(node.session_key)
        assert after is not None
        assert after.origin == before_origin

        token = current_tool_context.set(
            _fresh_tool_context(
                after,
                latest,
                execution_id="execution-network",
            )
        )
        try:
            effective = current_tool_run_context()
            assert effective is not None
            assert {grant.domain for grant in effective.domains} == {"added.example"}
            assert {
                (grant.kind, grant.value, grant.fingerprint) for grant in effective.temporary_grants
            } == {
                (
                    "domain",
                    "target.example",
                    "fingerprint-network",
                )
            }
            assert consume_temporary_network_grant(
                session_key=node.session_key,
                workspace=str(workspace),
                host="target.example",
                fingerprint="fingerprint-network",
            )
            consumed = current_tool_run_context()
        finally:
            current_tool_context.reset(token)
        assert consumed is not None
        assert consumed.temporary_grants == ()
        assert {grant.path for grant in consumed.mounts} == {str(added_mount)}
        assert {grant.domain for grant in consumed.domains} == {"added.example"}
        assert {grant.bundle_id for grant in consumed.bundles} == {"node-package-install"}

        other_token = current_tool_context.set(
            _fresh_tool_context(
                after,
                latest,
                execution_id="execution-other",
            )
        )
        try:
            other_execution = current_tool_run_context()
        finally:
            current_tool_context.reset(other_token)
        assert other_execution is not None
        assert other_execution.temporary_grants == ()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_network_once_consumption_is_execution_scoped(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="parallel-network-consume",
    )

    async def _approve(
        host: str,
        fingerprint: str,
        execution_id: str,
    ) -> None:
        from opensquilla.gateway.approval_queue import get_approval_queue

        params = build_network_approval_params(
            NetworkDecision(
                status="ask",
                normalized_host=host,
                reason="unknown_domain",
                source=None,
            ),
            session_key=node.session_key,
            workspace=str(workspace),
            fingerprint=fingerprint,
        )
        assert params is not None
        approval = _request(
            params,
            node=node,
            context=initial,
            execution_id=execution_id,
        )
        await apply_sandbox_approval_choice(
            params,
            approval_id=str(approval["approval_id"]),
            choice="allow_once",
            approved=True,
            session_manager=manager,
            config=_config(workspace),
        )
        get_approval_queue().resolve(str(approval["approval_id"]), True)

    await _approve("a.example", "fingerprint-a", "execution-a")
    await _approve("b.example", "fingerprint-b", "execution-b")
    current = await manager.get_session(node.session_key)
    assert current is not None

    try:
        token_a = current_tool_context.set(
            _fresh_tool_context(
                current,
                initial,
                execution_id="execution-a",
            )
        )
        try:
            assert consume_temporary_network_grant(
                session_key=node.session_key,
                workspace=str(workspace),
                host="a.example",
                fingerprint="fingerprint-a",
            )
            effective_a = current_tool_run_context()
        finally:
            current_tool_context.reset(token_a)
        assert effective_a is not None
        assert effective_a.temporary_grants == ()

        token_b = current_tool_context.set(
            _fresh_tool_context(
                current,
                initial,
                execution_id="execution-b",
            )
        )
        try:
            effective_b = current_tool_run_context()
        finally:
            current_tool_context.reset(token_b)
        assert effective_b is not None
        assert {(grant.value, grant.fingerprint) for grant in effective_b.temporary_grants} == {
            ("b.example", "fingerprint-b")
        }
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_auto_review_once_uses_only_generation_bound_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine import agent as agent_module
    from opensquilla.engine.elevation_triage import RuleAssessment
    from opensquilla.gateway.approval_queue import get_approval_queue
    from opensquilla.provider import Message
    from opensquilla.sandbox import escalation as escalation_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="auto-review-delta",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="auto.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-auto",
        reviewer="auto_review",
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-auto",
    )
    approval_id = str(approval["approval_id"])
    propagated_ids: list[str | None] = []
    real_grant = escalation_module.grant_auto_review_network_once

    async def _recording_grant(
        reviewed_params: dict[str, Any],
        *,
        approval_id: str | None = None,
        session_manager: Any | None = None,
        config: Any | None = None,
    ) -> bool:
        propagated_ids.append(approval_id)
        return await real_grant(
            reviewed_params,
            approval_id=approval_id,
            session_manager=session_manager,
            config=config,
        )

    monkeypatch.setattr(
        escalation_module,
        "grant_auto_review_network_once",
        _recording_grant,
    )
    monkeypatch.setattr(
        agent_module,
        "local_elevation_assessment",
        lambda action, transcript: RuleAssessment(
            risk_level="low",
            user_authorization="high",
            outcome="allow",
            rationale="The exact target was authorized.",
        ),
    )

    try:
        review_context = _fresh_tool_context(
            node,
            initial,
            execution_id="execution-auto",
        )
        review_context.run_mode = "trusted"
        setattr(review_context, "sandbox_session_manager", manager)
        setattr(review_context, "sandbox_gateway_config", _config(workspace))
        review_token = current_tool_context.set(review_context)
        try:
            assessment = await agent_module._review_pending_elevation_if_configured(
                approval,
                transcript=[
                    Message(
                        role="user",
                        content="Access only auto.example for this operation.",
                    )
                ],
                runtime_events_path=None,
            )
        finally:
            current_tool_context.reset(review_token)
        assert assessment is not None
        assert assessment.outcome == "allow"
        assert propagated_ids == [approval_id]
        queue_entry = get_approval_queue().get(approval_id)
        assert queue_entry.approved is True
        assert queue_entry.claim_token is None
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_GENERATIONS
        assert (
            resolved_run_context_overlay(
                node.session_key,
                str(workspace),
            )
            is None
        )

        active_token = current_tool_context.set(
            _fresh_tool_context(
                node,
                initial,
                execution_id="execution-auto",
            )
        )
        try:
            active = current_tool_run_context()
            assert consume_temporary_network_grant(
                session_key=node.session_key,
                workspace=str(workspace),
                host="auto.example",
                fingerprint="fingerprint-auto",
            )
            consumed = current_tool_run_context()
        finally:
            current_tool_context.reset(active_token)
        assert active is not None
        assert {(grant.value, grant.fingerprint) for grant in active.temporary_grants} == {
            ("auto.example", "fingerprint-auto")
        }
        assert consumed is not None
        assert consumed.temporary_grants == ()
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS

        other_token = current_tool_context.set(
            _fresh_tool_context(
                node,
                initial,
                execution_id="execution-other",
            )
        )
        try:
            other = current_tool_run_context()
        finally:
            current_tool_context.reset(other_token)
        assert other is not None
        assert other.temporary_grants == ()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_auto_review_binding_failure_cleans_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine import agent as agent_module
    from opensquilla.engine.elevation_triage import RuleAssessment
    from opensquilla.gateway.approval_queue import get_approval_queue
    from opensquilla.sandbox import escalation as escalation_module

    workspace = tmp_path / "workspace"
    replacement = tmp_path / "replacement"
    workspace.mkdir()
    replacement.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="auto-review-cleanup",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="auto.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-auto-cleanup",
        reviewer="auto_review",
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-auto-cleanup",
    )
    approval_id = str(approval["approval_id"])
    original = tmp_path / "workspace-original"
    workspace.rename(original)
    workspace.symlink_to(replacement, target_is_directory=True)
    monkeypatch.setattr(
        agent_module,
        "local_elevation_assessment",
        lambda action, transcript: RuleAssessment(
            risk_level="low",
            user_authorization="high",
            outcome="allow",
            rationale="The exact target was authorized.",
        ),
    )

    try:
        review_context = _fresh_tool_context(
            node,
            initial,
            execution_id="execution-auto-cleanup",
        )
        review_context.run_mode = "trusted"
        setattr(review_context, "sandbox_session_manager", manager)
        setattr(review_context, "sandbox_gateway_config", _config(workspace))
        review_token = current_tool_context.set(review_context)
        try:
            assessment = await agent_module._review_pending_elevation_if_configured(
                approval,
                transcript=[
                    Message(
                        role="user",
                        content="Access only auto.example for this operation.",
                    )
                ],
                runtime_events_path=None,
            )
        finally:
            current_tool_context.reset(review_token)

        assert assessment is not None
        assert assessment.outcome == "deny"
        queue_entry = get_approval_queue().get(approval_id)
        assert queue_entry.resolved is True
        assert queue_entry.approved is False
        assert queue_entry.claim_token is None
        assert queue_entry.params["reviewOutcome"] == "deny"
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_GENERATIONS
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS
    finally:
        workspace.unlink()
        original.rename(workspace)
        await storage.close()


@pytest.mark.parametrize("session_change", ["same_key_recreate", "epoch"])
@pytest.mark.asyncio
async def test_auto_review_revalidates_current_session_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_change: str,
) -> None:
    from opensquilla.engine import agent as agent_module
    from opensquilla.engine.elevation_triage import RuleAssessment
    from opensquilla.gateway.approval_queue import get_approval_queue
    from opensquilla.sandbox import escalation as escalation_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix=f"auto-review-session-{session_change}",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="auto.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint=f"fingerprint-auto-{session_change}",
        reviewer="auto_review",
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id=f"execution-auto-{session_change}",
    )
    approval_id = str(approval["approval_id"])
    monkeypatch.setattr(
        agent_module,
        "local_elevation_assessment",
        lambda action, transcript: RuleAssessment(
            risk_level="low",
            user_authorization="high",
            outcome="allow",
            rationale="The exact target was authorized.",
        ),
    )

    try:
        if session_change == "same_key_recreate":
            await storage.delete_session(node.session_key)
            replacement = await manager.create(
                node.session_key,
                origin={RUN_CONTEXT_ORIGIN_KEY: initial.to_origin_payload()},
            )
            assert replacement.session_id != node.session_id
        else:
            assert await storage.increment_epoch(node.session_key) > int(node.epoch or 0)

        review_context = _fresh_tool_context(
            node,
            initial,
            execution_id=f"execution-auto-{session_change}",
        )
        review_context.run_mode = "trusted"
        setattr(review_context, "sandbox_session_manager", manager)
        setattr(review_context, "sandbox_gateway_config", _config(workspace))
        token = current_tool_context.set(review_context)
        try:
            assessment = await agent_module._review_pending_elevation_if_configured(
                approval,
                transcript=[
                    Message(
                        role="user",
                        content="Access only auto.example for this operation.",
                    )
                ],
                runtime_events_path=None,
            )
        finally:
            current_tool_context.reset(token)

        assert assessment is not None
        assert assessment.outcome == "deny"
        entry = get_approval_queue().get(approval_id)
        assert entry.resolved is True
        assert entry.approved is False
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_GENERATIONS
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS
    finally:
        await storage.close()


@pytest.mark.parametrize("project_change", ["workspace_rebind", "project_removed"])
@pytest.mark.asyncio
async def test_auto_review_revalidates_current_project_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_change: str,
) -> None:
    from opensquilla.engine import agent as agent_module
    from opensquilla.engine.elevation_triage import RuleAssessment
    from opensquilla.gateway.approval_queue import get_approval_queue
    from opensquilla.sandbox import escalation as escalation_module

    global_workspace = tmp_path / "global"
    project_path = tmp_path / "project"
    replacement_path = tmp_path / "replacement-project"
    for path in (global_workspace, project_path, replacement_path):
        path.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(project_path),
        run_mode_source="project_default",
        source="saved",
    )
    storage = await SessionStorage.open(str(tmp_path / f"auto-project-{project_change}.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path),
        path_key=project_path_key(project_path, strict=True),
        display_name="Project",
        trusted_at=1,
    )
    replacement = await storage.create_or_restore_project_workspace(
        path=str(replacement_path),
        path_key=project_path_key(replacement_path, strict=True),
        display_name="Replacement",
        trusted_at=1,
    )
    node = await manager.create(
        f"agent:main:webchat:auto-project-{project_change}",
        workspace_id=project.workspace_id,
        origin={RUN_CONTEXT_ORIGIN_KEY: initial.to_origin_payload()},
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="auto.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(project_path),
        fingerprint=f"fingerprint-project-{project_change}",
        reviewer="auto_review",
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id=f"execution-project-{project_change}",
    )
    approval_id = str(approval["approval_id"])
    monkeypatch.setattr(
        agent_module,
        "local_elevation_assessment",
        lambda action, transcript: RuleAssessment(
            risk_level="low",
            user_authorization="high",
            outcome="allow",
            rationale="The exact target was authorized.",
        ),
    )

    try:
        if project_change == "workspace_rebind":
            await storage.bind_session_workspace(
                node.session_key,
                replacement.workspace_id,
            )
        else:
            await storage.remove_project_workspace(project.workspace_id)

        review_context = _fresh_tool_context(
            node,
            initial,
            execution_id=f"execution-project-{project_change}",
        )
        review_context.run_mode = "trusted"
        setattr(review_context, "sandbox_session_manager", manager)
        setattr(
            review_context,
            "sandbox_gateway_config",
            _config(global_workspace),
        )
        token = current_tool_context.set(review_context)
        try:
            assessment = await agent_module._review_pending_elevation_if_configured(
                approval,
                transcript=[
                    Message(
                        role="user",
                        content="Access only auto.example for this operation.",
                    )
                ],
                runtime_events_path=None,
            )
        finally:
            current_tool_context.reset(token)

        assert assessment is not None
        assert assessment.outcome == "deny"
        entry = get_approval_queue().get(approval_id)
        assert entry.resolved is True
        assert entry.approved is False
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_GENERATIONS
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS
    finally:
        await storage.close()


@pytest.mark.parametrize("approval_target", ["path", "domain", "bundle"])
@pytest.mark.asyncio
async def test_allow_same_type_merges_target_into_latest_durable_state(
    tmp_path: Path,
    approval_target: str,
) -> None:
    workspace = tmp_path / "workspace"
    revoked_mount = tmp_path / "revoked-mount"
    added_mount = tmp_path / "added-mount"
    requested_mount = tmp_path / "requested-mount"
    for path in (workspace, revoked_mount, added_mount, requested_mount):
        path.mkdir()
    initial = _state_context(
        workspace,
        mount=revoked_mount,
        domain="revoked.example",
        bundle="python-package-install",
        public_scope="chat",
        public_source="captured",
        run_mode=RunMode.STANDARD,
        run_mode_source="operator_default",
    )
    latest = _state_context(
        workspace,
        mount=added_mount,
        domain="added.example",
        bundle="node-package-install",
        public_scope="chat",
        public_source="latest",
        run_mode=RunMode.TRUSTED,
        run_mode_source="user",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix=f"same-type-{approval_target}",
    )
    if approval_target == "path":
        params = build_path_approval_params(
            MountDecision(
                status="request",
                normalized_path=str(requested_mount),
                access="rw",
                reason="outside_sandbox_mounts",
            ),
            session_key=node.session_key,
            workspace=str(workspace),
        )
    elif approval_target == "domain":
        params = build_network_approval_params(
            NetworkDecision(
                status="ask",
                normalized_host="target.example",
                reason="unknown_domain",
                source=None,
            ),
            session_key=node.session_key,
            workspace=str(workspace),
            fingerprint="fingerprint-domain",
        )
    else:
        params = build_package_bundle_approval_params(
            "rust-package-install",
            session_key=node.session_key,
            workspace=str(workspace),
            fingerprint="fingerprint-bundle",
        )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id=f"execution-{approval_target}",
    )
    await persist_run_context(manager, node.session_key, latest)

    try:
        await apply_sandbox_approval_choice(
            params,
            approval_id=str(approval["approval_id"]),
            choice="allow_same_type",
            approved=True,
            session_manager=manager,
            config=_config(workspace),
        )

        durable = await _durable_context(manager, node.session_key)
        assert durable.run_mode is RunMode.TRUSTED
        assert durable.run_mode_source == "user"
        assert str(revoked_mount) not in {grant.path for grant in durable.mounts}
        assert "revoked.example" not in {grant.domain for grant in durable.domains}
        assert "python-package-install" not in {grant.bundle_id for grant in durable.bundles}
        assert str(added_mount) in {grant.path for grant in durable.mounts}
        assert "added.example" in {grant.domain for grant in durable.domains}
        assert "node-package-install" in {grant.bundle_id for grant in durable.bundles}
        assert {(grant.scope, grant.source) for grant in durable.public_network} == {
            ("chat", "latest")
        }
        if approval_target == "path":
            assert (str(requested_mount), "rw", "chat") in {
                (grant.path, grant.access, grant.scope) for grant in durable.mounts
            }
        elif approval_target == "domain":
            assert "target.example" in {grant.domain for grant in durable.domains}
        else:
            assert "rust-package-install" in {grant.bundle_id for grant in durable.bundles}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_same_type_overlay_does_not_leak_to_new_session_incarnation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, original = await _create_session(
        tmp_path,
        initial,
        suffix="same-type-overlay-incarnation",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="target.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=original.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-overlay-incarnation",
    )
    assert params is not None
    approval = _request(
        params,
        node=original,
        context=initial,
        execution_id="execution-old",
    )

    try:
        await apply_sandbox_approval_choice(
            params,
            approval_id=str(approval["approval_id"]),
            choice="allow_same_type",
            approved=True,
            session_manager=manager,
            config=_config(workspace),
        )
        await storage.delete_session(original.session_key)
        replacement = await manager.create(
            original.session_key,
            origin={RUN_CONTEXT_ORIGIN_KEY: initial.to_origin_payload()},
        )
        assert replacement.session_id != original.session_id

        token = current_tool_context.set(
            _fresh_tool_context(
                replacement,
                initial,
                execution_id="execution-new",
            )
        )
        try:
            effective = current_tool_run_context()
        finally:
            current_tool_context.reset(token)
        assert effective is not None
        assert effective.domains == ()
    finally:
        await storage.close()


@pytest.mark.parametrize("binding_change", ["workspace_id", "project_removed"])
@pytest.mark.asyncio
async def test_project_approval_revalidates_workspace_binding_at_resolution(
    tmp_path: Path,
    binding_change: str,
) -> None:
    global_workspace = tmp_path / "global"
    project_path = tmp_path / "project"
    replacement_path = tmp_path / "replacement-project"
    requested = tmp_path / "requested"
    for path in (global_workspace, project_path, replacement_path, requested):
        path.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(project_path),
        run_mode_source="project_default",
        source="saved",
    )
    storage = await SessionStorage.open(str(tmp_path / f"project-{binding_change}.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path),
        path_key=project_path_key(project_path, strict=True),
        display_name="Project",
        trusted_at=1,
    )
    replacement = await storage.create_or_restore_project_workspace(
        path=str(replacement_path),
        path_key=project_path_key(replacement_path, strict=True),
        display_name="Replacement",
        trusted_at=1,
    )
    node = await manager.create(
        f"agent:main:webchat:project-{binding_change}",
        workspace_id=project.workspace_id,
        origin={RUN_CONTEXT_ORIGIN_KEY: initial.to_origin_payload()},
    )
    params = build_path_approval_params(
        MountDecision(
            status="request",
            normalized_path=str(requested),
            access="ro",
            reason="outside_sandbox_mounts",
        ),
        session_key=node.session_key,
        workspace=str(project_path),
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id=f"execution-project-{binding_change}",
    )

    try:
        if binding_change == "workspace_id":
            await storage.bind_session_workspace(
                node.session_key,
                replacement.workspace_id,
            )
        else:
            await storage.remove_project_workspace(project.workspace_id)
        before = await manager.get_session(node.session_key)
        assert before is not None
        before_origin = copy.deepcopy(before.origin)

        with pytest.raises(ProjectWorkspaceStateError):
            await apply_sandbox_approval_choice(
                params,
                approval_id=str(approval["approval_id"]),
                choice="allow_same_type",
                approved=True,
                session_manager=manager,
                config=_config(global_workspace),
            )

        after = await manager.get_session(node.session_key)
        assert after is not None
        assert after.origin == before_origin
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_session_origin_cas_rejects_changed_origin(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    latest = RunContext(
        run_mode=RunMode.TRUSTED,
        workspace=str(workspace),
        run_mode_source="user",
        source="saved",
    )
    proposed = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        domains=(
            DomainGrant(
                domain="target.example",
                scope="chat",
                source="manual",
            ),
        ),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="origin-cas",
    )
    expected_origin = copy.deepcopy(node.origin)
    await persist_run_context(manager, node.session_key, latest)

    try:
        result = await storage.compare_and_set_session_origin(
            expected_session=node,
            expected_origin=expected_origin,
            origin={RUN_CONTEXT_ORIGIN_KEY: proposed.to_origin_payload()},
            workspace_guard=None,
        )
        assert result is None
        durable = await _durable_context(manager, node.session_key)
        assert durable == latest
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_same_type_fails_closed_when_storage_cas_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="missing-cas",
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="target.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-missing-cas",
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-missing-cas",
    )
    monkeypatch.setattr(storage, "compare_and_set_session_origin", None)

    try:
        with pytest.raises(ProjectWorkspaceStateError, match="unavailable"):
            await apply_sandbox_approval_choice(
                params,
                approval_id=str(approval["approval_id"]),
                choice="allow_same_type",
                approved=True,
                session_manager=manager,
                config=_config(workspace),
            )
        assert (await _durable_context(manager, node.session_key)).domains == ()
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_turn_runner_artifact_context_carries_session_identity(
    tmp_path: Path,
) -> None:
    from opensquilla.engine.runtime import TurnRunner

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="runtime-identity",
    )
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        config=_config(workspace),
    )

    try:
        context = await runner._with_artifact_context(
            ToolContext(
                session_key=node.session_key,
                workspace_dir=str(workspace),
                sandbox_run_context=initial,
            ),
            node.session_key,
        )
        assert context.artifact_session_id == node.session_id
        assert getattr(context, "session_epoch", None) == node.epoch
        assert getattr(context, "workspace_id", None) == node.workspace_id
    finally:
        await storage.close()


@pytest.mark.parametrize("choice", ["allow_once", "allow_same_type"])
@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
@pytest.mark.asyncio
async def test_agent_turn_finally_clears_only_its_execution_approval_deltas(
    tmp_path: Path,
    choice: str,
    outcome: str,
) -> None:
    from opensquilla.sandbox import escalation as escalation_module

    workspace = tmp_path / "workspace"
    requested = tmp_path / "requested"
    parallel_requested = tmp_path / "parallel-requested"
    for path in (workspace, requested, parallel_requested):
        path.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix=f"turn-cleanup-{choice}-{outcome}",
    )

    async def _grant_path(
        target: Path,
        *,
        execution_id: str,
        base_context: RunContext,
        base_node: Any,
    ) -> str:
        params = build_path_approval_params(
            MountDecision(
                status="request",
                normalized_path=str(target),
                access="rw",
                reason="outside_sandbox_mounts",
            ),
            session_key=base_node.session_key,
            workspace=str(workspace),
        )
        assert params is not None
        approval = _request(
            params,
            node=base_node,
            context=base_context,
            execution_id=execution_id,
        )
        await apply_sandbox_approval_choice(
            params,
            approval_id=str(approval["approval_id"]),
            choice=choice,
            approved=True,
            session_manager=manager,
            config=_config(workspace),
        )
        return str(approval["approval_id"])

    try:
        approval_id = await _grant_path(
            requested,
            execution_id="execution-main",
            base_context=initial,
            base_node=node,
        )
        latest_node = await manager.get_session(node.session_key)
        assert latest_node is not None
        latest_context = await _durable_context(manager, node.session_key)
        parallel_approval_id = await _grant_path(
            parallel_requested,
            execution_id="execution-parallel",
            base_context=latest_context,
            base_node=latest_node,
        )
        latest_node = await manager.get_session(node.session_key)
        assert latest_node is not None
        latest_context = await _durable_context(manager, node.session_key)
        assert approval_id in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS
        assert parallel_approval_id in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS

        provider = _LifecycleProvider(outcome)
        agent = Agent(
            provider=provider,
            config=AgentConfig(max_iterations=1),
            session_key=node.session_key,
            tool_context=_fresh_tool_context(
                latest_node,
                latest_context,
                execution_id="execution-main",
            ),
        )

        async def _run() -> list[Any]:
            return [event async for event in agent.run_turn("finish the turn")]

        if outcome == "success":
            await _run()
        elif outcome == "error":
            with pytest.raises(
                RuntimeError,
                match="provider lifecycle failure",
            ):
                await _run()
        else:
            task = asyncio.create_task(_run())
            await provider.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS
        assert parallel_approval_id in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_cancelled_turn_waits_for_apply_first_then_revokes_delta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import project_workspace_runtime
    from opensquilla.sandbox import escalation as escalation_module

    workspace = tmp_path / "workspace"
    requested = tmp_path / "requested"
    workspace.mkdir()
    requested.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="turn-cleanup-race",
    )
    params = build_path_approval_params(
        MountDecision(
            status="request",
            normalized_path=str(requested),
            access="rw",
            reason="outside_sandbox_mounts",
        ),
        session_key=node.session_key,
        workspace=str(workspace),
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-race",
    )
    approval_id = str(approval["approval_id"])

    authority_entered = asyncio.Event()
    release_authority = asyncio.Event()
    real_authoritative_context = project_workspace_runtime.authoritative_project_run_context

    async def _paused_authoritative_context(*args: Any, **kwargs: Any):
        authority_entered.set()
        await release_authority.wait()
        return await real_authoritative_context(*args, **kwargs)

    monkeypatch.setattr(
        project_workspace_runtime,
        "authoritative_project_run_context",
        _paused_authoritative_context,
    )
    cleanup_started = asyncio.Event()
    cleanup_completed = asyncio.Event()
    real_cleanup = escalation_module.clear_approval_run_context_deltas_for_tool_context

    def _recording_cleanup(context: ToolContext | None):
        cleanup_started.set()
        result = real_cleanup(context)
        if not inspect.isawaitable(result):
            cleanup_completed.set()
            return result

        async def _record_completion() -> int:
            try:
                return await result
            finally:
                cleanup_completed.set()

        return _record_completion()

    monkeypatch.setattr(
        escalation_module,
        "clear_approval_run_context_deltas_for_tool_context",
        _recording_cleanup,
    )

    apply_task = asyncio.create_task(
        apply_sandbox_approval_choice(
            params,
            approval_id=approval_id,
            choice="allow_once",
            approved=True,
            session_manager=manager,
            config=_config(workspace),
        )
    )
    provider = _LifecycleProvider("cancel")
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_iterations=1),
        session_key=node.session_key,
        tool_context=_fresh_tool_context(
            node,
            initial,
            execution_id="execution-race",
        ),
    )

    async def _run() -> list[Any]:
        return [event async for event in agent.run_turn("cancel this turn")]

    turn_task: asyncio.Task[list[Any]] | None = None
    try:
        await authority_entered.wait()
        turn_task = asyncio.create_task(_run())
        await provider.started.wait()
        turn_task.cancel()
        await cleanup_started.wait()
        assert not cleanup_completed.is_set()
        assert not turn_task.done()

        release_authority.set()
        await apply_task
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(turn_task, timeout=1.0)
        assert cleanup_completed.is_set()
        assert approval_id not in escalation_module._APPROVAL_RUN_CONTEXT_DELTAS
    finally:
        release_authority.set()
        if not apply_task.done():
            apply_task.cancel()
        if turn_task is not None and not turn_task.done():
            turn_task.cancel()
        await storage.close()


@pytest.mark.parametrize("choice", ["allow_once", "allow_same_type"])
@pytest.mark.parametrize("latest_mount_kind", ["exact", "covering"])
@pytest.mark.asyncio
async def test_stale_path_ro_approval_preserves_latest_covering_rw_grant(
    tmp_path: Path,
    choice: str,
    latest_mount_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    covering = tmp_path / "covering"
    requested = covering / "nested"
    workspace.mkdir()
    requested.mkdir(parents=True)
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    latest = replace(
        initial,
        mounts=(
            MountGrant(
                path=str(requested if latest_mount_kind == "exact" else covering),
                access="rw",
                scope="chat",
            ),
        ),
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="path-monotonic",
    )
    params = build_path_approval_params(
        MountDecision(
            status="request",
            normalized_path=str(requested),
            access="ro",
            reason="outside_sandbox_mounts",
        ),
        session_key=node.session_key,
        workspace=str(workspace),
    )
    assert params is not None
    approval = _request(
        params,
        node=node,
        context=initial,
        execution_id="execution-path-monotonic",
    )
    await persist_run_context(manager, node.session_key, latest)

    try:
        await apply_sandbox_approval_choice(
            params,
            approval_id=str(approval["approval_id"]),
            choice=choice,
            approved=True,
            session_manager=manager,
            config=_config(workspace),
        )
        durable = await _durable_context(manager, node.session_key)
        assert {(grant.path, grant.access, grant.scope) for grant in durable.mounts} == {
            (
                str(requested if latest_mount_kind == "exact" else covering),
                "rw",
                "chat",
            )
        }
        active_token = current_tool_context.set(
            _fresh_tool_context(
                node,
                initial,
                execution_id="execution-path-monotonic",
            )
        )
        try:
            effective = current_tool_run_context()
        finally:
            current_tool_context.reset(active_token)
        assert effective is not None
        assert (
            decide_path_access(
                str(requested),
                workspace=effective.workspace,
                mounts=effective.mounts,
                write=True,
            ).status
            == "allowed"
        )
    finally:
        await storage.close()


@pytest.mark.parametrize(
    ("second_host", "second_execution"),
    [
        ("second.example", "execution-network-a"),
        ("first.example", "execution-network-b"),
    ],
)
@pytest.mark.asyncio
async def test_network_approval_cannot_authorize_another_target_or_execution(
    tmp_path: Path,
    second_host: str,
    second_execution: str,
) -> None:
    from types import SimpleNamespace

    from opensquilla.gateway.approval_queue import get_approval_queue
    from opensquilla.sandbox.network_runtime import (
        NetworkApprovalService,
        NetworkPolicyRequest,
        NetworkProtocol,
    )
    from opensquilla.sandbox.types import (
        NetworkMode,
        ResourceLimits,
        SandboxPolicy,
        SandboxRequest,
        SecurityLevel,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, manager, node = await _create_session(
        tmp_path,
        initial,
        suffix=f"network-exact-{second_host}-{second_execution}",
    )
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.PROXY_ALLOWLIST,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=("PATH",),
        require_approval=False,
    )
    sandbox_request = SandboxRequest(
        argv=("http_request", "GET", "https://first.example"),
        cwd=workspace,
        action_kind="network.http",
        policy=policy,
        session_id=node.session_key,
        run_mode="standard",
    )
    runtime = SimpleNamespace(workspace=workspace)

    def _service() -> NetworkApprovalService:
        return NetworkApprovalService(
            context=initial,
            request=sandbox_request,
            runtime=runtime,
            approval_timeout_seconds=2.0,
            consume_temporary_grants=False,
        )

    async def _decide(
        host: str,
        execution_id: str,
    ):
        token = current_tool_context.set(
            _fresh_tool_context(
                node,
                initial,
                execution_id=execution_id,
            )
        )
        try:
            return await _service().decide(
                NetworkPolicyRequest(
                    protocol=NetworkProtocol.HTTPS_CONNECT,
                    host=host,
                    port=443,
                    method="CONNECT",
                )
            )
        finally:
            current_tool_context.reset(token)

    async def _wait_for_pending_count(count: int) -> list[dict[str, Any]]:
        for _ in range(100):
            pending = get_approval_queue().list_pending("exec")
            if len(pending) >= count:
                return pending
            await asyncio.sleep(0)
        return get_approval_queue().list_pending("exec")

    first_task: asyncio.Task[Any] | None = None
    second_task: asyncio.Task[Any] | None = None
    try:
        first_task = asyncio.create_task(_decide("first.example", "execution-network-a"))
        first_pending = await _wait_for_pending_count(1)
        assert len(first_pending) == 1
        first_id = str(first_pending[0]["id"])

        second_task = asyncio.create_task(_decide(second_host, second_execution))
        pending = await _wait_for_pending_count(2)
        second_ids = {str(item["id"]) for item in pending if str(item["id"]) != first_id}

        first_entry = get_approval_queue().get(first_id)
        await apply_sandbox_approval_choice(
            first_entry.params,
            approval_id=first_id,
            choice="allow_once",
            approved=True,
            session_manager=manager,
            config=_config(workspace),
        )
        get_approval_queue().resolve(first_id, True)
        for second_id in second_ids:
            get_approval_queue().resolve(second_id, False)

        first_decision, second_decision = await asyncio.gather(
            first_task,
            second_task,
        )
        assert first_decision.status == "allow"
        assert second_decision.status == "block"
    finally:
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
        await storage.close()


@pytest.mark.asyncio
async def test_unresolved_explicit_approval_id_must_match_exact_generation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initial = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        source="saved",
    )
    storage, _manager, node = await _create_session(
        tmp_path,
        initial,
        suffix="explicit-id-exact",
    )
    first_params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="first.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-first",
    )
    second_params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="second.example",
            reason="unknown_domain",
            source=None,
        ),
        session_key=node.session_key,
        workspace=str(workspace),
        fingerprint="fingerprint-second",
    )
    assert first_params is not None
    assert second_params is not None

    try:
        first = _request(
            first_params,
            node=node,
            context=initial,
            execution_id="execution-explicit",
        )
        first_id = str(first["approval_id"])
        token = current_tool_context.set(
            _fresh_tool_context(
                node,
                initial,
                execution_id="execution-explicit",
            )
        )
        try:
            second = request_sandbox_approval(
                second_params,
                approval_id=first_id,
                message="Approve only the exact network target.",
            )
        finally:
            current_tool_context.reset(token)

        assert second is not None
        assert second["status"] == "approval_required"
        assert second["approval_id"] != first_id
    finally:
        await storage.close()
