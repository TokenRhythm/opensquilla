from __future__ import annotations

import asyncio
import os
import plistlib
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
from opensquilla.project_workspaces import project_path_key
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import ProjectWorkspace, SessionNode
from opensquilla.session.storage import SessionStorage


class _SessionManager:
    def __init__(self):
        self.node = SimpleNamespace(
            session_key="agent:main:webchat:abc",
            session_id="session-abc",
            agent_id="main",
            epoch=0,
            workspace_id=None,
            origin=None,
        )
        self.sessions = {self.node.session_key: self.node}
        self.created: list[str] = []
        self.storage = self

    async def get_session(self, session_key: str):
        return self.sessions.get(session_key)

    async def get_or_create(self, session_key: str, agent_id: str = "main", **kwargs):
        existing = self.sessions.get(session_key)
        if existing is not None:
            return existing, False
        node = SimpleNamespace(
            session_key=session_key,
            agent_id=agent_id,
            origin=None,
            **kwargs,
        )
        self.sessions[session_key] = node
        self.created.append(session_key)
        return node, True

    async def update(self, session_key: str, **fields):
        node = self.sessions[session_key]
        for key, value in fields.items():
            setattr(node, key, value)
        return node

    async def compare_and_set_session_origin(
        self,
        *,
        expected_session,
        expected_origin,
        origin,
        workspace_guard,
    ):
        del workspace_guard
        current = self.sessions.get(expected_session.session_key)
        if (
            current is None
            or current.session_id != expected_session.session_id
            or current.epoch != expected_session.epoch
            or current.workspace_id != expected_session.workspace_id
            or current.origin != expected_origin
        ):
            return None
        current.origin = origin
        return current


def _ctx(
    manager: _SessionManager,
    *,
    is_owner: bool = True,
    run_mode: str = "standard",
    sandbox: bool = True,
    security_grading: bool = True,
    permissions_default_mode: str = "off",
    scopes: frozenset[str] | None = None,
):
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext

    config = SimpleNamespace(
        workspace_dir="/tmp/ws",
        agents=[],
        sandbox=SimpleNamespace(
            run_mode=run_mode,
            sandbox=sandbox,
            security_grading=security_grading,
            backend="noop",
            network_default="proxy_allowlist",
        ),
        permissions=SimpleNamespace(default_mode=permissions_default_mode),
    )
    return RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=scopes or frozenset(["operator.read", "operator.write"]),
            is_owner=is_owner,
            authenticated=True,
        ),
        session_manager=manager,
        config=config,
    )


def _request_generated_sandbox_approval(
    manager: _SessionManager,
    params: dict[str, object],
) -> str:
    from opensquilla.sandbox.escalation import request_sandbox_approval
    from opensquilla.sandbox.run_context import RunContext
    from opensquilla.sandbox.run_mode import RunMode
    from opensquilla.tools.types import ToolContext, current_tool_context

    workspace = str(params.get("workspace") or "/tmp/ws")
    Path(workspace).mkdir(parents=True, exist_ok=True)
    tool_context = ToolContext(
        is_owner=True,
        session_key=manager.node.session_key,
        workspace_dir=workspace,
        sandbox_run_context=RunContext(
            run_mode=RunMode.STANDARD,
            workspace=workspace,
            source="saved",
        ),
        artifact_session_id=manager.node.session_id,
        session_epoch=manager.node.epoch,
        workspace_id=manager.node.workspace_id,
        execution_id=f"execution-{params.get('fingerprint') or params.get('path')}",
    )
    setattr(tool_context, "_sandbox_run_context_fresh", True)
    token = current_tool_context.set(tool_context)
    try:
        payload = request_sandbox_approval(
            params,
            message="Approve the exact sandbox target.",
        )
    finally:
        current_tool_context.reset(token)
    assert payload is not None
    return str(payload["approval_id"])


@pytest.fixture(autouse=True)
def _reset_resolved_overlays() -> None:
    from opensquilla.sandbox.escalation import reset_resolved_run_context_overlays

    reset_resolved_run_context_overlays()
    yield
    reset_resolved_run_context_overlays()


@pytest_asyncio.fixture
async def project_sandbox_ctx(
    tmp_path: Path,
) -> AsyncIterator[tuple[RpcContext, SessionNode, ProjectWorkspace]]:
    storage = await SessionStorage.open(str(tmp_path / "sandbox-project.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    session = await manager.create(
        "agent:main:webchat:project-sandbox-fixture",
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "workspace": project.path,
            }
        },
    )
    ctx = RpcContext(
        conn_id="project-sandbox-fixture",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.read", "operator.write"}),
            is_owner=True,
            authenticated=True,
        ),
        config=GatewayConfig(workspace_dir=str(tmp_path / "agent-default")),
        session_manager=manager,
    )
    try:
        yield ctx, session, project
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sandbox_context_get_uses_bound_project_not_agent_default(
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_get

    storage = await SessionStorage.open(str(tmp_path / "sessions.db"))
    manager = SessionManager(storage)
    project_path = tmp_path / "project"
    project_path.mkdir()
    project = await storage.create_or_restore_project_workspace(
        path=str(project_path.resolve()),
        path_key=project_path_key(project_path, strict=True),
        display_name="project",
        trusted_at=1,
    )
    session = await manager.create(
        "agent:main:webchat:project-sandbox-context",
        workspace_id=project.workspace_id,
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "run_mode": "standard",
                "workspace": project.path,
            }
        },
    )
    ctx = RpcContext(
        conn_id="project-sandbox-context",
        principal=Principal(
            role="operator",
            scopes=frozenset({"operator.admin"}),
            is_owner=True,
            authenticated=True,
        ),
        config=GatewayConfig(workspace_dir=str(tmp_path / "default")),
        session_manager=manager,
    )
    try:
        payload = await _handle_sandbox_run_context_get(
            {"sessionKey": session.session_key},
            ctx,
        )
        assert payload["workspace"] == project.path
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_sandbox_context_get_defaults_bound_owner_project_to_full(
    project_sandbox_ctx: tuple[RpcContext, SessionNode, ProjectWorkspace],
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_get

    ctx, project_session, project = project_sandbox_ctx
    await ctx.session_manager.update(project_session.session_key, origin=None)

    payload = await _handle_sandbox_run_context_get(
        {"sessionKey": project_session.session_key},
        ctx,
    )

    assert payload["workspace"] == project.path
    assert payload["runMode"] == "full"


@pytest.mark.asyncio
async def test_bound_project_workspace_cannot_be_changed(
    project_sandbox_ctx: tuple[RpcContext, SessionNode, ProjectWorkspace],
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_workspace_set

    ctx, project_session, _project = project_sandbox_ctx
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(RpcHandlerError) as raised:
        await _handle_sandbox_workspace_set(
            {
                "sessionKey": project_session.session_key,
                "workspace": str(other),
            },
            ctx,
        )
    assert raised.value.code == "PROJECT_WORKSPACE_FIXED"


@pytest.mark.asyncio
async def test_project_mount_validation_is_relative_to_authoritative_workspace(
    project_sandbox_ctx: tuple[RpcContext, SessionNode, ProjectWorkspace],
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_mount_add

    ctx, project_session, project = project_sandbox_ctx
    inside = Path(project.path) / "inside"
    inside.mkdir()
    outside = tmp_path / "tampered-origin"
    outside.mkdir()
    project_session.origin = {
        RUN_CONTEXT_ORIGIN_KEY: {
            "run_mode": "standard",
            "workspace": str(outside),
        }
    }
    await ctx.session_manager.update(
        project_session.session_key,
        origin=project_session.origin,
    )

    payload = await _handle_sandbox_mount_add(
        {
            "sessionKey": project_session.session_key,
            "path": str(inside),
            "access": "rw",
            "scope": "chat",
        },
        ctx,
    )

    assert payload["workspace"] == project.path
    assert any(mount["path"] == str(inside) for mount in payload["mounts"])


@pytest.mark.asyncio
async def test_project_run_context_set_preserves_authoritative_workspace(
    project_sandbox_ctx: tuple[RpcContext, SessionNode, ProjectWorkspace],
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_set

    ctx, project_session, project = project_sandbox_ctx
    outside = tmp_path / "tampered-context-origin"
    outside.mkdir()
    project_session.origin = {
        RUN_CONTEXT_ORIGIN_KEY: {
            "run_mode": "standard",
            "workspace": str(outside),
        }
    }
    await ctx.session_manager.update(
        project_session.session_key,
        origin=project_session.origin,
    )

    payload = await _handle_sandbox_run_context_set(
        {
            "sessionKey": project_session.session_key,
            "runMode": "full",
        },
        ctx,
    )
    saved = await ctx.session_manager.get_session(project_session.session_key)

    assert payload["workspace"] == project.path
    assert payload["runMode"] == "full"
    assert saved.origin[RUN_CONTEXT_ORIGIN_KEY]["workspace"] == project.path
    assert saved.origin[RUN_CONTEXT_ORIGIN_KEY]["run_mode_source"] == "user"


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX symlinks")
@pytest.mark.asyncio
async def test_project_sandbox_rpc_fails_when_workspace_becomes_unavailable(
    project_sandbox_ctx: tuple[RpcContext, SessionNode, ProjectWorkspace],
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_get

    ctx, project_session, project = project_sandbox_ctx
    project_path = Path(project.path)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    project_path.rename(tmp_path / "project-old")
    project_path.symlink_to(replacement, target_is_directory=True)

    with pytest.raises(RpcHandlerError) as raised:
        await _handle_sandbox_run_context_get(
            {"sessionKey": project_session.session_key},
            ctx,
        )

    assert raised.value.code == "WORKSPACE_UNAVAILABLE"
    assert raised.value.details == {"reason": "canonical_changed"}


@pytest.mark.asyncio
async def test_project_run_context_set_reports_workspace_before_setup_readiness(
    project_sandbox_ctx: tuple[RpcContext, SessionNode, ProjectWorkspace],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_sandbox

    ctx, project_session, project = project_sandbox_ctx
    Path(project.path).rmdir()
    original_origin = dict(project_session.origin or {})

    async def setup_must_not_run(_config: object) -> object:
        raise AssertionError("setup readiness must follow project revalidation")

    monkeypatch.setattr(
        rpc_sandbox,
        "current_sandbox_setup_status",
        setup_must_not_run,
    )

    with pytest.raises(RpcHandlerError) as raised:
        await rpc_sandbox._handle_sandbox_run_context_set(
            {
                "sessionKey": project_session.session_key,
                "runMode": "standard",
            },
            ctx,
        )

    assert raised.value.code == "WORKSPACE_UNAVAILABLE"
    assert raised.value.details == {"reason": "unavailable"}
    saved = await ctx.session_manager.get_session(project_session.session_key)
    assert saved is not None
    assert saved.origin == original_origin


@pytest.mark.asyncio
async def test_rpc_add_domain_returns_updated_context() -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_domain_add

    manager = _SessionManager()

    result = await _handle_sandbox_domain_add(
        {
            "sessionKey": manager.node.session_key,
            "domain": "https://pypi.org/simple",
            "scope": "workspace",
        },
        _ctx(manager),
    )

    assert result["domains"] == [{"domain": "pypi.org", "scope": "workspace", "source": "manual"}]


@pytest.mark.asyncio
async def test_rpc_add_mount_rejects_non_owner() -> None:
    from opensquilla.gateway.rpc import RpcHandlerError
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_mount_add

    manager = _SessionManager()

    with pytest.raises(RpcHandlerError, match="requires owner principal"):
        await _handle_sandbox_mount_add(
            {"sessionKey": manager.node.session_key, "path": "/tmp/ws/extras"},
            _ctx(manager, is_owner=False),
        )

    assert manager.node.origin is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "params"),
    [
        ("_handle_sandbox_mount_add", {}),
        ("_handle_sandbox_mount_add", {"path": ""}),
        ("_handle_sandbox_mount_add", {"path": "   "}),
        ("_handle_sandbox_mount_remove", {}),
        ("_handle_sandbox_mount_remove", {"path": ""}),
        ("_handle_sandbox_mount_remove", {"path": "   "}),
    ],
)
async def test_rpc_mount_mutations_require_path_without_mutating_origin(
    handler_name: str,
    params: dict[str, str],
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()
    handler = getattr(rpc_sandbox, handler_name)

    with pytest.raises(ValueError, match="params.path is required"):
        await handler(
            {"sessionKey": manager.node.session_key, **params},
            _ctx(manager),
        )

    assert manager.node.origin is None
    assert manager.created == []


@pytest.mark.asyncio
async def test_rpc_mutation_rejects_whitespace_session_key_without_creating_session() -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_mount_add

    manager = _SessionManager()

    with pytest.raises(ValueError, match="params.sessionKey is required"):
        await _handle_sandbox_mount_add(
            {"sessionKey": "   ", "path": "/tmp/ws/extras"},
            _ctx(manager),
        )

    assert "   " not in manager.sessions
    assert manager.created == []


@pytest.mark.asyncio
async def test_rpc_run_context_get_includes_bundles_and_temporary_grants() -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_get
    from opensquilla.sandbox.run_context import (
        PublicNetworkGrant,
        RunContext,
        TemporaryGrant,
        persist_run_context,
    )
    from opensquilla.sandbox.run_mode import RunMode
    from opensquilla.sandbox.user_grants import upsert_bundle_grant

    manager = _SessionManager()
    upsert_bundle_grant(
        {
            "bundle_id": "python-package-install",
            "scope": "workspace",
            "source": "manual",
        }
    )
    await persist_run_context(
        manager,
        manager.node.session_key,
        RunContext(
            run_mode=RunMode.STANDARD,
            workspace="/tmp/ws",
            public_network=(PublicNetworkGrant(scope="chat", source="manual"),),
            temporary_grants=(
                TemporaryGrant(
                    kind="domain",
                    value="pypi.org",
                    fingerprint="abc123",
                ),
            ),
            source="saved",
        ),
    )

    result = await _handle_sandbox_run_context_get(
        {"sessionKey": manager.node.session_key},
        _ctx(manager),
    )

    assert result["bundles"] == [
        {
            "bundle_id": "python-package-install",
            "scope": "workspace",
            "source": "manual",
        }
    ]
    assert result["publicNetwork"] == [
        {
            "scope": "chat",
            "source": "manual",
        }
    ]
    assert result["temporaryGrants"] == [
        {
            "kind": "domain",
            "value": "pypi.org",
            "fingerprint": "abc123",
            "expires_after": "once",
        }
    ]


@pytest.mark.asyncio
async def test_exec_approval_resolve_allows_non_owner_chat_scoped_sandbox_grant() -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision
    from opensquilla.sandbox.run_context import get_run_context

    reset_approval_queue()
    manager = _SessionManager()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    approval_id = _request_generated_sandbox_approval(manager, params)
    result = await _handle_exec_approval_resolve(
        {"id": approval_id, "approved": True, "choice": "allow_same_type"},
        _ctx(
            manager,
            is_owner=False,
            scopes=frozenset(["operator.approvals"]),
        ),
    )

    assert result["resolved"] is True
    assert result["approved"] is True
    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=_ctx(manager).config,
        workspace="/tmp/ws",
    )
    assert ("example.com", "chat") in [(grant.domain, grant.scope) for grant in context.domains]

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_resolve_returns_first_cross_surface_decision() -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision

    reset_approval_queue()
    manager = _SessionManager()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    queue = get_approval_queue()
    approval_id = _request_generated_sandbox_approval(manager, params)
    context = _ctx(manager)

    first = await _handle_exec_approval_resolve(
        {"id": approval_id, "approved": True, "choice": "allow_same_type"},
        context,
    )
    stale_second = await _handle_exec_approval_resolve(
        {"id": approval_id, "approved": False, "choice": "deny"},
        context,
    )

    assert first["resolved"] is True
    assert first["approved"] is True
    assert stale_second["resolved"] is True
    assert stale_second["approved"] is True
    assert queue.get(approval_id).approved is True

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_resolve_joins_active_cross_surface_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_approvals
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision

    reset_approval_queue()
    manager = _SessionManager()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    queue = get_approval_queue()
    approval_id = _request_generated_sandbox_approval(manager, params)
    context = _ctx(manager)
    apply_started = asyncio.Event()
    release_apply = asyncio.Event()

    async def _blocked_apply(*args: object, **kwargs: object) -> None:
        del args, kwargs
        apply_started.set()
        await release_apply.wait()

    monkeypatch.setattr(rpc_approvals, "apply_sandbox_approval_choice", _blocked_apply)
    first_task = asyncio.create_task(
        rpc_approvals._handle_exec_approval_resolve(
            {"id": approval_id, "approved": True, "choice": "allow_same_type"},
            context,
        )
    )
    await apply_started.wait()
    second_task = asyncio.create_task(
        rpc_approvals._handle_exec_approval_resolve(
            {"id": approval_id, "approved": False, "choice": "deny"},
            context,
        )
    )
    await asyncio.sleep(0)
    assert not second_task.done()

    release_apply.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first["resolved"] is True
    assert first["approved"] is True
    assert second["resolved"] is True
    assert second["approved"] is True
    assert queue.get(approval_id).approved is True

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_resolve_rejects_legacy_intent_flags() -> None:
    # "Allow always" / rememberIntent were a removed no-op; a truthy value must
    # now be rejected loudly rather than silently accepted, and must not resolve
    # the approval or apply any grant.
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.gateway.rpc import RpcHandlerError
    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision

    reset_approval_queue()
    manager = _SessionManager()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    queue = get_approval_queue()
    approval_id = _request_generated_sandbox_approval(manager, params)

    with pytest.raises(RpcHandlerError) as excinfo:
        await _handle_exec_approval_resolve(
            {
                "id": approval_id,
                "approved": True,
                "choice": "allow_same_type",
                "allowAlways": True,
                "rememberIntent": True,
            },
            _ctx(
                manager,
                is_owner=False,
                scopes=frozenset(["operator.approvals"]),
            ),
        )

    assert excinfo.value.code == "UNSUPPORTED_PARAM"
    # The approval is untouched — rejection happens before any resolution.
    assert queue.get(approval_id).resolved is False

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_resolve_rejects_non_owner_missing_sandbox_choice() -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.gateway.rpc import RpcHandlerError
    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision

    reset_approval_queue()
    manager = _SessionManager()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    queue = get_approval_queue()
    approval_id = _request_generated_sandbox_approval(manager, params)

    with pytest.raises(RpcHandlerError, match="requires owner principal"):
        await _handle_exec_approval_resolve(
            {"id": approval_id, "approved": True},
            _ctx(
                manager,
                is_owner=False,
                scopes=frozenset(["operator.approvals"]),
            ),
        )

    pending = queue.get(approval_id)
    assert pending.resolved is False

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_resolve_allows_non_owner_chat_scoped_path_mount(tmp_path) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve
    from opensquilla.sandbox.escalation import build_path_approval_params
    from opensquilla.sandbox.path_validation import MountDecision
    from opensquilla.sandbox.run_context import get_run_context

    reset_approval_queue()
    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    params = build_path_approval_params(
        MountDecision(
            status="request",
            normalized_path=str(outside.resolve(strict=False)),
            access="ro",
            reason="outside_sandbox_mounts",
        ),
        session_key=manager.node.session_key,
        workspace=str(workspace),
    )
    assert params is not None
    approval_id = _request_generated_sandbox_approval(manager, params)

    result = await _handle_exec_approval_resolve(
        {"id": approval_id, "approved": True, "choice": "allow_same_type"},
        _ctx(
            manager,
            is_owner=False,
            scopes=frozenset(["operator.approvals"]),
        ),
    )

    assert result["resolved"] is True
    assert result["approved"] is True
    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=_ctx(manager).config,
        workspace=str(workspace),
    )
    assert (str(outside.resolve(strict=False)), "ro", "chat") in [
        (grant.path, grant.access, grant.scope) for grant in context.mounts
    ]

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_resolve_allows_non_owner_sandbox_grant_denial() -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.gateway.rpc_approvals import _handle_exec_approval_resolve
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision
    from opensquilla.sandbox.run_context import get_run_context

    reset_approval_queue()
    manager = _SessionManager()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    approval_id = _request_generated_sandbox_approval(manager, params)

    result = await _handle_exec_approval_resolve(
        {"id": approval_id, "approved": False, "choice": "deny"},
        _ctx(
            manager,
            is_owner=False,
            scopes=frozenset(["operator.approvals"]),
        ),
    )

    assert result["resolved"] is True
    assert result["approved"] is False
    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=_ctx(manager).config,
        workspace="/tmp/ws",
    )
    assert context.domains == ()

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_resolve_leaves_sandbox_approval_pending_when_mutation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.gateway.rpc import get_dispatcher
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision
    from opensquilla.sandbox.run_context import get_run_context

    reset_approval_queue()
    manager = _SessionManager()
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    queue = get_approval_queue()
    approval_id = _request_generated_sandbox_approval(manager, params)

    async def fail_apply(*args, **kwargs) -> None:
        raise RuntimeError("mutation failed")

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_approvals.apply_sandbox_approval_choice",
        fail_apply,
    )

    result = await get_dispatcher().dispatch(
        "r1",
        "exec.approval.resolve",
        {"id": approval_id, "approved": True, "choice": "allow_same_type"},
        _ctx(manager, scopes=frozenset(["operator.approvals"])),
    )

    assert result.error is not None
    assert "mutation failed" in result.error.message
    pending = queue.get(approval_id)
    assert pending.resolved is False
    assert pending.approved is False
    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=_ctx(manager).config,
        workspace="/tmp/ws",
    )
    assert context.domains == ()

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_resolve_claim_prevents_deny_race_from_landing_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.gateway.rpc import get_dispatcher
    from opensquilla.sandbox import escalation as escalation_mod
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision
    from opensquilla.sandbox.run_context import get_run_context

    reset_approval_queue()
    manager = _SessionManager()
    ctx = _ctx(manager, scopes=frozenset(["operator.approvals"]))
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    queue = get_approval_queue()
    approval_id = _request_generated_sandbox_approval(manager, params)

    mutation_started = asyncio.Event()
    release_mutation = asyncio.Event()

    async def delayed_apply(*args, **kwargs) -> None:
        mutation_started.set()
        await release_mutation.wait()
        await escalation_mod.apply_sandbox_approval_choice(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_approvals.apply_sandbox_approval_choice",
        delayed_apply,
    )

    approve_task = asyncio.create_task(
        get_dispatcher().dispatch(
            "approve",
            "exec.approval.resolve",
            {"id": approval_id, "approved": True, "choice": "allow_same_type"},
            ctx,
        )
    )
    await asyncio.wait_for(mutation_started.wait(), timeout=1)

    deny_result = await get_dispatcher().dispatch(
        "deny",
        "exec.approval.resolve",
        {"id": approval_id, "approved": False, "choice": "deny"},
        ctx,
    )
    release_mutation.set()
    approve_result = await approve_task

    assert deny_result.error is None
    assert deny_result.payload["pending"] is True
    assert deny_result.payload["resolutionInProgress"] is True
    assert approve_result.error is None, approve_result.error
    resolved = queue.get(approval_id)
    assert resolved.resolved is True
    assert resolved.approved is True
    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=ctx.config,
        workspace="/tmp/ws",
    )
    assert ("example.com", "chat") in [(grant.domain, grant.scope) for grant in context.domains]

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_wait_and_consume_wait_for_sandbox_grant_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.gateway.rpc import get_dispatcher
    from opensquilla.sandbox import escalation as escalation_mod
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision

    reset_approval_queue()
    manager = _SessionManager()
    ctx = _ctx(manager, scopes=frozenset(["operator.approvals"]))
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    queue = get_approval_queue()
    approval_id = _request_generated_sandbox_approval(manager, params)

    mutation_started = asyncio.Event()
    release_mutation = asyncio.Event()

    async def delayed_apply(*args, **kwargs) -> None:
        mutation_started.set()
        await release_mutation.wait()
        await escalation_mod.apply_sandbox_approval_choice(*args, **kwargs)

    monkeypatch.setattr(
        "opensquilla.gateway.rpc_approvals.apply_sandbox_approval_choice",
        delayed_apply,
    )

    approve_task = asyncio.create_task(
        get_dispatcher().dispatch(
            "approve",
            "exec.approval.resolve",
            {"id": approval_id, "approved": True, "choice": "allow_same_type"},
            ctx,
        )
    )
    await asyncio.wait_for(mutation_started.wait(), timeout=1)

    wait_task = asyncio.create_task(queue.wait(approval_id, timeout=1.0))
    wait_decision_task = asyncio.create_task(
        get_dispatcher().dispatch(
            "wait",
            "exec.approval.waitDecision",
            {"id": approval_id},
            ctx,
        )
    )
    await asyncio.sleep(0.05)

    assert wait_task.done() is False
    assert wait_decision_task.done() is False
    with pytest.raises(ValueError, match="in progress|not approved"):
        queue.consume(approval_id)

    release_mutation.set()
    approve_result = await approve_task
    assert approve_result.error is None, approve_result.error
    assert await wait_task is True
    wait_decision_result = await wait_decision_task
    assert wait_decision_result.error is None, wait_decision_result.error
    assert wait_decision_result.payload["approved"] is True
    assert wait_decision_result.payload["resolved"] is True

    queue.consume(approval_id)
    assert queue.status(approval_id)["consumed"] is True

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_resolve_recovers_complete_failure_after_grant_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.gateway.rpc import get_dispatcher
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision
    from opensquilla.sandbox.run_context import get_run_context

    reset_approval_queue()
    manager = _SessionManager()
    ctx = _ctx(manager, scopes=frozenset(["operator.approvals"]))
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    queue = get_approval_queue()
    approval_id = _request_generated_sandbox_approval(manager, params)
    original_complete = queue.complete_claimed_resolution
    attempts = 0

    def fail_once_then_complete(*args, **kwargs) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient complete failed")
        original_complete(*args, **kwargs)

    monkeypatch.setattr(queue, "complete_claimed_resolution", fail_once_then_complete)

    result = await get_dispatcher().dispatch(
        "approve",
        "exec.approval.resolve",
        {"id": approval_id, "approved": True, "choice": "allow_same_type"},
        ctx,
    )

    assert result.error is None, result.error
    assert attempts == 2
    assert queue.status(approval_id)["resolved"] is True
    assert queue.status(approval_id)["approved"] is True
    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=ctx.config,
        workspace="/tmp/ws",
    )
    assert ("example.com", "chat") in [(grant.domain, grant.scope) for grant in context.domains]

    reset_approval_queue()


@pytest.mark.asyncio
async def test_exec_approval_resolve_finalize_failure_does_not_land_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.gateway.rpc import get_dispatcher
    from opensquilla.sandbox.escalation import build_network_approval_params
    from opensquilla.sandbox.network_guard import NetworkDecision
    from opensquilla.sandbox.run_context import get_run_context

    reset_approval_queue()
    manager = _SessionManager()
    ctx = _ctx(manager, scopes=frozenset(["operator.approvals"]))
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="example.com",
            reason="unknown_domain",
            source=None,
        ),
        session_key=manager.node.session_key,
        workspace="/tmp/ws",
        fingerprint="fp123",
    )
    assert params is not None
    queue = get_approval_queue()
    approval_id = _request_generated_sandbox_approval(manager, params)

    def fail_finalize(*args, **kwargs) -> None:
        raise RuntimeError("finalize failed")

    monkeypatch.setattr(queue, "finalize_claimed_resolution", fail_finalize)

    result = await get_dispatcher().dispatch(
        "r1",
        "exec.approval.resolve",
        {"id": approval_id, "approved": True, "choice": "allow_same_type"},
        ctx,
    )

    assert result.error is not None
    assert "finalize failed" in result.error.message
    pending = queue.get(approval_id)
    assert pending.resolved is False
    assert queue.list_pending("exec")[0]["id"] == approval_id
    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=ctx.config,
        workspace="/tmp/ws",
    )
    assert context.domains == ()

    reset_approval_queue()


def test_claimed_approval_reappears_after_claim_lease_expires(tmp_path) -> None:
    from opensquilla.application.approval_queue import ApprovalQueue

    db_path = tmp_path / "approval_queue.sqlite"
    queue = ApprovalQueue(db_path=str(db_path), claim_ttl_seconds=0)
    approval_id = queue.request(namespace="exec", params={"command": "echo ok"})
    queue.claim_resolution(approval_id)
    queue.close()

    reloaded = ApprovalQueue(db_path=str(db_path), claim_ttl_seconds=0)

    assert [item["id"] for item in reloaded.list_pending("exec")] == [approval_id]
    reloaded.resolve(approval_id, False)
    assert reloaded.status(approval_id)["resolved"] is True
    assert reloaded.status(approval_id)["approved"] is False
    reloaded.close()


@pytest.mark.asyncio
async def test_rpc_once_rw_mount_preserves_durable_ro_through_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import json

    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_mount_add
    from opensquilla.sandbox.escalation import (
        current_tool_run_context,
        prune_once_mount_grants,
        reset_resolved_run_context_overlays,
        resolved_run_context_overlay,
    )
    from opensquilla.sandbox.operation_runtime import SandboxOperationResult
    from opensquilla.sandbox.run_context import (
        MountGrant,
        RunContext,
        get_run_context,
    )
    from opensquilla.sandbox.run_mode import RunMode
    from opensquilla.tools.builtin import filesystem
    from opensquilla.tools.types import ToolContext, current_tool_context

    manager = _SessionManager()
    ctx = _ctx(manager)
    workspace = tmp_path / "workspace"
    mounted = tmp_path / "mounted"
    workspace.mkdir()
    mounted.mkdir()
    base = RunContext(
        run_mode=RunMode.STANDARD,
        workspace=str(workspace),
        mounts=(MountGrant(path=str(mounted), access="ro", scope="chat"),),
        run_mode_source="user",
        source="saved",
    )
    manager.node.origin = {RUN_CONTEXT_ORIGIN_KEY: base.to_origin_payload()}

    result = await _handle_sandbox_mount_add(
        {
            "sessionKey": manager.node.session_key,
            "path": str(mounted),
            "access": "rw",
            "scope": "once",
        },
        ctx,
    )

    assert result["mounts"] == [
        {"path": str(mounted), "access": "ro", "scope": "chat"},
        {"path": str(mounted), "access": "rw", "scope": "once"},
    ]
    assert manager.node.origin[RUN_CONTEXT_ORIGIN_KEY]["mounts"] == [
        {"path": str(mounted), "access": "ro", "scope": "chat"}
    ]

    backend_operations: list[object] = []

    class RecordingFilesystemBackend:
        name = "recording-filesystem"

        def operation_domains_supported(self) -> frozenset[str]:
            return frozenset({"filesystem"})

        async def run_operation(self, operation: object) -> SandboxOperationResult:
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
    active_context = ToolContext(
        is_owner=True,
        session_key=manager.node.session_key,
        workspace_dir=str(workspace),
        sandbox_run_context=base,
    )
    setattr(active_context, "_sandbox_run_context_fresh", True)
    token = current_tool_context.set(active_context)
    try:
        active = current_tool_run_context()
        assert active is not None
        assert [(grant.access, grant.scope) for grant in active.mounts] == [("rw", "once")]
        allowed_target = mounted / "rpc-allowed-once.txt"
        allowed = await filesystem.write_file(
            str(allowed_target),
            "allowed",
        )
        assert allowed == f"sandboxed write: {allowed_target}"
        assert allowed_target.read_text(encoding="utf-8") == "allowed"
    finally:
        current_tool_context.reset(token)

    assert prune_once_mount_grants(manager.node.session_key) == 1
    overlay = resolved_run_context_overlay(
        manager.node.session_key,
        str(workspace),
    )
    assert overlay is not None
    assert [(grant.access, grant.scope) for grant in overlay.mounts] == [("ro", "chat")]

    reset_resolved_run_context_overlays()
    restored = await get_run_context(
        manager,
        manager.node.session_key,
        config=ctx.config,
        workspace=str(workspace),
    )
    assert [(grant.access, grant.scope) for grant in restored.mounts] == [("ro", "chat")]
    expired_context = ToolContext(
        is_owner=True,
        session_key=manager.node.session_key,
        workspace_dir=str(workspace),
        sandbox_run_context=restored,
    )
    setattr(expired_context, "_sandbox_run_context_fresh", True)
    expired_token = current_tool_context.set(expired_context)
    try:
        blocked_target = mounted / "rpc-blocked-after-expiry.txt"
        blocked = json.loads(
            await filesystem.write_file(
                str(blocked_target),
                "blocked",
            )
        )
        assert blocked["status"] == "elevation_required"
        assert blocked["reason"] == "mount_requires_write_access"
        assert not blocked_target.exists()
    finally:
        current_tool_context.reset(expired_token)
    assert len(backend_operations) == 1


@pytest.mark.asyncio
async def test_rpc_mount_remove_updates_resolved_overlay_for_current_tool_mounts() -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_mount_remove
    from opensquilla.sandbox.escalation import current_tool_mounts, remember_resolved_run_context
    from opensquilla.sandbox.run_context import MountGrant, RunContext
    from opensquilla.sandbox.run_mode import RunMode
    from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

    manager = _SessionManager()
    ctx = _ctx(manager)

    remembered = RunContext(
        run_mode=RunMode.STANDARD,
        workspace="/tmp/ws",
        mounts=(MountGrant(path="/tmp/ws/extras", access="ro", scope="chat"),),
        source="saved",
    )
    remember_resolved_run_context(
        manager.node.session_key,
        "/tmp/ws",
        remembered,
        session_manager=manager,
        config=ctx.config,
    )
    manager.node.origin = remembered.to_origin_payload() and {
        "sandbox_run_context": remembered.to_origin_payload()
    }
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir="/tmp/ws",
            session_key=manager.node.session_key,
            sandbox_mounts=[{"path": "/tmp/ws/extras", "access": "ro"}],
            sandbox_run_context=remembered,
        )
    )
    try:
        assert current_tool_mounts() == [{"path": "/tmp/ws/extras", "access": "ro"}]

        result = await _handle_sandbox_mount_remove(
            {"sessionKey": manager.node.session_key, "path": "/tmp/ws/extras"},
            ctx,
        )
    finally:
        current_tool_context.reset(token)

    assert result["mounts"] == []

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir="/tmp/ws",
            session_key=manager.node.session_key,
            sandbox_mounts=[{"path": "/tmp/ws/extras", "access": "ro"}],
            sandbox_run_context=remembered,
        )
    )
    try:
        assert current_tool_mounts() == []
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_rpc_mount_remove_chat_scope_leaves_user_scope_mount_visible() -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_mount_remove
    from opensquilla.sandbox.path_validation import normalize_path
    from opensquilla.sandbox.run_context import MountGrant, RunContext, get_run_context
    from opensquilla.sandbox.run_mode import RunMode
    from opensquilla.sandbox.user_grants import upsert_mount_grant

    manager = _SessionManager()
    ctx = _ctx(manager)
    path = "/tmp/shared-mount"
    normalized_path = str(normalize_path(path))
    upsert_mount_grant({"path": path, "access": "ro", "scope": "workspace"})
    manager.node.origin = {
        "sandbox_run_context": RunContext(
            run_mode=RunMode.STANDARD,
            workspace="/tmp/ws",
            mounts=(MountGrant(path=path, access="rw", scope="chat"),),
            source="saved",
        ).to_origin_payload()
    }

    result = await _handle_sandbox_mount_remove(
        {"sessionKey": manager.node.session_key, "path": path, "scope": "chat"},
        ctx,
    )

    assert result["mounts"] == [{"path": normalized_path, "access": "ro", "scope": "workspace"}]
    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=ctx.config,
        workspace="/tmp/ws",
    )
    assert [(grant.path, grant.access, grant.scope) for grant in context.mounts] == [
        (normalized_path, "ro", "workspace")
    ]


@pytest.mark.asyncio
async def test_rpc_domain_remove_updates_resolved_overlay_for_current_tool_context() -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_domain_remove
    from opensquilla.sandbox.escalation import (
        current_tool_run_context,
        remember_resolved_run_context,
    )
    from opensquilla.sandbox.network_guard import decide_network_access
    from opensquilla.sandbox.run_context import DomainGrant, RunContext
    from opensquilla.sandbox.run_mode import RunMode
    from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

    manager = _SessionManager()
    ctx = _ctx(manager)
    remembered = RunContext(
        run_mode=RunMode.STANDARD,
        workspace="/tmp/ws",
        domains=(DomainGrant(domain="example.com", scope="chat"),),
        source="saved",
    )
    remember_resolved_run_context(
        manager.node.session_key,
        "/tmp/ws",
        remembered,
        session_manager=manager,
        config=ctx.config,
    )
    manager.node.origin = {"sandbox_run_context": remembered.to_origin_payload()}
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir="/tmp/ws",
            session_key=manager.node.session_key,
            sandbox_run_context=remembered,
        )
    )
    try:
        merged = current_tool_run_context()
        assert merged is not None
        assert decide_network_access("example.com", merged).status == "allow"

        result = await _handle_sandbox_domain_remove(
            {"sessionKey": manager.node.session_key, "domain": "example.com"},
            ctx,
        )
    finally:
        current_tool_context.reset(token)

    assert result["domains"] == []

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir="/tmp/ws",
            session_key=manager.node.session_key,
            sandbox_run_context=remembered,
        )
    )
    try:
        merged = current_tool_run_context()
        assert merged is not None
        assert decide_network_access("example.com", merged).status == "ask"
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_rpc_domain_remove_chat_scope_leaves_user_scope_domain_visible() -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_domain_remove
    from opensquilla.sandbox.run_context import DomainGrant, RunContext, get_run_context
    from opensquilla.sandbox.run_mode import RunMode
    from opensquilla.sandbox.user_grants import upsert_domain_grant

    manager = _SessionManager()
    ctx = _ctx(manager)
    upsert_domain_grant({"domain": "example.com", "scope": "workspace", "source": "manual"})
    manager.node.origin = {
        "sandbox_run_context": RunContext(
            run_mode=RunMode.STANDARD,
            workspace="/tmp/ws",
            domains=(DomainGrant(domain="example.com", scope="chat", source="manual"),),
            source="saved",
        ).to_origin_payload()
    }

    result = await _handle_sandbox_domain_remove(
        {"sessionKey": manager.node.session_key, "domain": "example.com", "scope": "chat"},
        ctx,
    )

    assert result["domains"] == [
        {"domain": "example.com", "scope": "workspace", "source": "manual"}
    ]
    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=ctx.config,
        workspace="/tmp/ws",
    )
    assert [(grant.domain, grant.scope) for grant in context.domains] == [
        ("example.com", "workspace")
    ]


@pytest.mark.asyncio
async def test_rpc_sandbox_status_reports_backend_managed_network_and_run_mode() -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_status

    manager = _SessionManager()

    result = await _handle_sandbox_status({}, _ctx(manager))

    assert result["run_mode"] == "standard"
    assert result["run_mode_label"] == "Standard-Sandbox"
    assert result["execution_target"] == "sandbox"
    assert result["posture"] == "standard"
    assert result["backend"] == "noop"
    assert result["managed_network"] == "ready"
    assert result["sandbox"] == {
        "sandbox": True,
        "security_grading": True,
        "network_default": "proxy_allowlist",
    }
    catalog_by_id = {
        bundle["bundle_id"]: set(bundle["domains"]) for bundle in result["bundle_catalog"]
    }
    expected_catalog_subsets = {
        "python-package-install": {
            "pypi.org",
            "files.pythonhosted.org",
            "pypi.python.org",
            "bootstrap.pypa.io",
        },
        "node-package-install": {
            "registry.npmjs.org",
            "registry.yarnpkg.com",
            "yarnpkg.com",
            "nodejs.org",
        },
        "rust-package-install": {
            "crates.io",
            "static.crates.io",
            "index.crates.io",
            "github.com",
            "objects.githubusercontent.com",
        },
        "go-package-install": {
            "proxy.golang.org",
            "sum.golang.org",
            "go.dev",
            "golang.org",
            "storage.googleapis.com",
        },
        "github-default": {
            "github.com",
            "api.github.com",
            "raw.githubusercontent.com",
            "codeload.github.com",
            "objects.githubusercontent.com",
        },
    }
    for bundle_id, expected_domains in expected_catalog_subsets.items():
        assert bundle_id in catalog_by_id
        assert expected_domains.issubset(catalog_by_id[bundle_id])
    assert result["permissions"] == {"default_mode": "off"}


@pytest.mark.asyncio
async def test_rpc_sandbox_status_reports_full_host_access_without_managed_controls() -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_status

    result = await _handle_sandbox_status(
        {},
        _ctx(
            _SessionManager(),
            run_mode="full",
            sandbox=False,
            security_grading=False,
            permissions_default_mode="full",
        ),
    )

    assert result["run_mode"] == "full"
    assert result["run_mode_label"] == "Full Host Access"
    assert result["execution_target"] == "host"
    assert result["posture"] == "full"
    assert result["managed_network"] == "inactive"


@pytest.mark.asyncio
async def test_rpc_sandbox_explain_returns_status_messages_and_optional_context() -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_explain
    from opensquilla.sandbox.run_context import RunContext, persist_run_context
    from opensquilla.sandbox.run_mode import RunMode

    manager = _SessionManager()
    await persist_run_context(
        manager,
        manager.node.session_key,
        RunContext(run_mode=RunMode.TRUSTED, workspace="/tmp/ws", source="saved"),
    )

    result = await _handle_sandbox_explain(
        {"sessionKey": manager.node.session_key},
        _ctx(manager),
    )

    assert result["status"]["run_mode"] == "standard"
    assert result["runContext"]["runMode"] == "trusted"
    assert result["messages"] == [
        {"kind": "run_mode", "message": "Run mode is standard."},
        {
            "kind": "managed_network",
            "message": "Managed network allowlist is ready.",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "params"),
    [
        ("_handle_sandbox_workspace_set", {"workspace": "/tmp/ws/project"}),
        ("_handle_sandbox_mount_remove", {"path": "/tmp/ws/extras"}),
        ("_handle_sandbox_domain_add", {"domain": "pypi.org"}),
        ("_handle_sandbox_domain_remove", {"domain": "pypi.org"}),
        ("_handle_sandbox_bundle_enable", {"bundleId": "python-package-install"}),
        ("_handle_sandbox_bundle_disable", {"bundleId": "python-package-install"}),
    ],
)
async def test_rpc_sandbox_mutations_reject_non_owner(
    handler_name: str,
    params: dict[str, str],
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox
    from opensquilla.gateway.rpc import RpcHandlerError

    manager = _SessionManager()
    handler = getattr(rpc_sandbox, handler_name)

    with pytest.raises(RpcHandlerError, match="requires owner principal"):
        await handler(
            {"sessionKey": manager.node.session_key, **params},
            _ctx(manager, is_owner=False),
        )

    assert manager.node.origin is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "params", "message"),
    [
        ("_handle_sandbox_domain_add", {}, "params.domain is required"),
        ("_handle_sandbox_domain_remove", {"domain": ""}, "params.domain is required"),
        ("_handle_sandbox_bundle_enable", {}, "params.bundleId is required"),
        (
            "_handle_sandbox_bundle_enable",
            {"bundleId": "unknown-package-install"},
            "unknown_package_bundle",
        ),
        (
            "_handle_sandbox_bundle_disable",
            {"bundle_id": "   "},
            "params.bundleId is required",
        ),
        ("_handle_sandbox_workspace_set", {}, "params.workspace is required"),
    ],
)
async def test_rpc_sandbox_invalid_params_do_not_create_missing_session(
    handler_name: str,
    params: dict[str, str],
    message: str,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()
    missing_session_key = "agent:main:webchat:missing"
    handler = getattr(rpc_sandbox, handler_name)

    with pytest.raises(ValueError, match=message):
        await handler(
            {"sessionKey": missing_session_key, **params},
            _ctx(manager),
        )

    assert missing_session_key not in manager.sessions
    assert manager.created == []


@pytest.mark.asyncio
async def test_rpc_sandbox_path_pick_uses_permission_based_workspace_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()

    async def pick_directory(initial_dir=None):
        return "/etc/shadow"

    monkeypatch.setattr(
        rpc_sandbox,
        "_pick_directory_path_async",
        pick_directory,
    )

    result = await rpc_sandbox._handle_sandbox_path_pick(
        {
            "sessionKey": manager.node.session_key,
            "kind": "workspace",
        },
        _ctx(manager),
    )

    expected_path = (
        "/etc/shadow" if os.name == "nt" else str(Path("/etc/shadow").resolve(strict=False))
    )
    assert result == {"path": expected_path, "kind": "workspace"}


@pytest.mark.asyncio
async def test_rpc_sandbox_path_pick_returns_valid_mount_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()
    selected = tmp_path / "external"
    selected.mkdir()

    async def pick_directory(initial_dir=None):
        return str(selected)

    monkeypatch.setattr(
        rpc_sandbox,
        "_pick_directory_path_async",
        pick_directory,
    )

    result = await rpc_sandbox._handle_sandbox_path_pick(
        {
            "sessionKey": manager.node.session_key,
            "kind": "mount",
            "access": "ro",
        },
        _ctx(manager),
    )

    assert result == {"path": str(selected), "kind": "mount"}


@pytest.mark.asyncio
async def test_rpc_sandbox_path_pick_returns_null_when_selection_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()

    async def pick_directory(initial_dir=None):
        return None

    monkeypatch.setattr(
        rpc_sandbox,
        "_pick_directory_path_async",
        pick_directory,
    )

    result = await rpc_sandbox._handle_sandbox_path_pick(
        {
            "sessionKey": manager.node.session_key,
            "kind": "workspace",
        },
        _ctx(manager),
    )

    assert result == {"path": None, "kind": "workspace"}


def test_pick_directory_path_uses_native_macos_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    native_picker = SimpleNamespace(calls=[])

    def pick_native(initial_dir=None):
        native_picker.calls.append(initial_dir)
        return "/Volumes/workspace/project"

    class FakeRoot:
        def withdraw(self) -> None:
            pass

        def destroy(self) -> None:
            pass

    fake_tkinter = SimpleNamespace(
        Tk=FakeRoot,
        filedialog=SimpleNamespace(askdirectory=lambda **_kwargs: "/tk/fallback"),
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        rpc_sandbox,
        "_pick_directory_path_macos",
        pick_native,
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "tkinter", fake_tkinter)

    result = rpc_sandbox._pick_directory_path("/Volumes/workspace")

    assert result == "/Volumes/workspace/project"
    assert native_picker.calls == ["/Volumes/workspace"]


def _install_fake_native_macos_picker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    selected_path: str | None,
):
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    run_calls = []
    observations = {}
    appkit_resources = tmp_path / "AppKitResources"
    (appkit_resources / "en.lproj").mkdir(parents=True)
    (appkit_resources / "zh_CN.lproj").mkdir()
    monkeypatch.setattr(
        rpc_sandbox,
        "_MACOS_APPKIT_RESOURCES",
        appkit_resources,
        raising=False,
    )

    def fake_run(command, **kwargs):
        run_calls.append((command, kwargs))
        if command[0] == "osacompile":
            app_path = Path(command[command.index("-o") + 1])
            resources = app_path / "Contents" / "Resources"
            resources.mkdir(parents=True)
            with (app_path / "Contents" / "Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleName": "applet",
                        "CFBundleSignature": "aplt",
                        "LSRequiresCarbon": True,
                    },
                    handle,
                )
            return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

        app_path = Path(command[-1])
        with (app_path / "Contents" / "Info.plist").open("rb") as handle:
            observations["plist"] = plistlib.load(handle)
        observations["localizations"] = {
            path.name for path in (app_path / "Contents" / "Resources").glob("*.lproj")
        }
        result_path = app_path.parent / "selection.txt"
        result_path.write_text(selected_path or "", encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    return rpc_sandbox, run_calls, observations


def test_native_macos_picker_returns_path_and_launches_a_native_app_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rpc_sandbox, run_calls, _observations = _install_fake_native_macos_picker(
        monkeypatch,
        tmp_path,
        selected_path="/Volumes/workspace/My Project/",
    )

    result = rpc_sandbox._pick_directory_path_macos("/Volumes/workspace")

    assert result == "/Volumes/workspace/My Project/"
    compile_command, compile_kwargs = run_calls[0]
    launch_command, launch_kwargs = run_calls[1]
    assert compile_command[:3] == ["osacompile", "-l", "JavaScript"]
    assert "-o" in compile_command
    assert "/Volumes/workspace" in compile_command[-1]
    assert launch_command[:3] == ["open", "-W", "-n"]
    assert launch_command[-1].endswith("OpenSquilla Directory Picker.app")
    assert compile_kwargs == {
        "capture_output": True,
        "check": False,
        "text": True,
    }
    assert launch_kwargs == {
        "capture_output": True,
        "check": False,
        "text": True,
    }


def test_native_macos_picker_allows_creating_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rpc_sandbox, run_calls, _observations = _install_fake_native_macos_picker(
        monkeypatch,
        tmp_path,
        selected_path="/Volumes/workspace/New Project",
    )

    rpc_sandbox._pick_directory_path_macos("/Volumes/workspace")

    script = run_calls[0][0][-1]
    assert "NSOpenPanel.openPanel" in script
    assert "canChooseFiles = false" in script
    assert "canChooseDirectories = true" in script
    assert "canCreateDirectories = true" in script


def test_native_macos_picker_activates_a_regular_app_before_running_modal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rpc_sandbox, run_calls, _observations = _install_fake_native_macos_picker(
        monkeypatch,
        tmp_path,
        selected_path="/Volumes/workspace/project",
    )

    rpc_sandbox._pick_directory_path_macos("/Volumes/workspace")

    script = run_calls[0][0][-1]
    application = script.index("$.NSApplication.sharedApplication")
    regular_policy = script.index("$.NSApplicationActivationPolicyRegular")
    activate = script.index("activateIgnoringOtherApps(true)")
    run_modal = script.index("panel.runModal")

    assert application < regular_policy < activate < run_modal


def test_native_macos_picker_uses_all_system_localizations_without_overriding_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rpc_sandbox, run_calls, observations = _install_fake_native_macos_picker(
        monkeypatch,
        tmp_path,
        selected_path="/Volumes/workspace/项目",
    )

    rpc_sandbox._pick_directory_path_macos("/Volumes/workspace")

    script = run_calls[0][0][-1]
    assert "Choose a project folder" not in script
    assert "panel.message" not in script
    assert "panel.prompt" not in script
    assert observations["plist"]["CFBundleAllowMixedLocalizations"] is True
    assert "LSRequiresCarbon" not in observations["plist"]
    assert observations["localizations"] == {"en.lproj", "zh_CN.lproj"}


def test_native_macos_picker_treats_user_cancellation_as_no_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rpc_sandbox, _run_calls, _observations = _install_fake_native_macos_picker(
        monkeypatch,
        tmp_path,
        selected_path=None,
    )

    assert rpc_sandbox._pick_directory_path_macos(None) is None


def test_native_macos_picker_reports_an_invalid_app_bundle_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    def fake_run(command, **_kwargs):
        app_path = Path(command[command.index("-o") + 1])
        info_path = app_path / "Contents" / "Info.plist"
        info_path.parent.mkdir(parents=True)
        info_path.write_text("not a plist", encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(
        rpc_sandbox.RpcUnavailableError,
        match="Directory picker is not available",
    ):
        rpc_sandbox._pick_directory_path_macos(None)


@pytest.mark.asyncio
async def test_path_list_omitted_path_uses_agent_workspace_not_process_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    agent_workspace = tmp_path / "agent-workspace"
    unrelated_cwd = tmp_path / "gateway-cwd"
    agent_workspace.mkdir()
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    ctx = _ctx(manager)
    ctx.config.workspace_dir = str(agent_workspace)

    result = await _handle_sandbox_path_list(
        {"sessionKey": manager.node.session_key, "kind": "workspace"},
        ctx,
    )

    assert result["currentPath"] == str(agent_workspace.resolve())
    assert result["path"] == result["currentPath"]


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("linux", False),
        ("darwin", True),
        ("win32", True),
    ],
)
@pytest.mark.asyncio
async def test_path_list_reports_system_picker_availability_for_gateway_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    platform: str,
    expected: bool,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = _ctx(manager)
    ctx.config.workspace_dir = str(workspace)
    monkeypatch.setattr(rpc_sandbox.sys, "platform", platform)

    result = await rpc_sandbox._handle_sandbox_path_list(
        {"sessionKey": manager.node.session_key, "kind": "workspace"},
        ctx,
    )

    assert result["systemPickerAvailable"] is expected


@pytest.mark.asyncio
async def test_path_list_omitted_path_uses_validated_project_session(
    project_sandbox_ctx: tuple[RpcContext, SessionNode, ProjectWorkspace],
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    ctx, project_session, project = project_sandbox_ctx
    agent_workspace = Path(ctx.config.workspace_dir or "")
    agent_workspace.mkdir()
    assert agent_workspace.resolve(strict=True) != Path(project.path).resolve(strict=True)

    result = await _handle_sandbox_path_list(
        {"sessionKey": project_session.session_key, "kind": "workspace"},
        ctx,
    )

    assert result["currentPath"] == str(Path(project.path).resolve(strict=True))
    assert result["path"] == result["currentPath"]


@pytest.mark.asyncio
async def test_path_list_invalid_bound_project_does_not_fall_back(
    project_sandbox_ctx: tuple[RpcContext, SessionNode, ProjectWorkspace],
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    ctx, project_session, project = project_sandbox_ctx
    Path(project.path).rmdir()
    agent_workspace = Path(ctx.config.workspace_dir or "")
    agent_workspace.mkdir()

    with pytest.raises(RpcHandlerError) as raised:
        await _handle_sandbox_path_list(
            {"sessionKey": project_session.session_key, "kind": "workspace"},
            ctx,
        )

    assert raised.value.code == "WORKSPACE_UNAVAILABLE"
    assert raised.value.details == {"reason": "unavailable"}


@pytest.mark.asyncio
async def test_path_list_explicit_path_precedes_invalid_bound_project(
    project_sandbox_ctx: tuple[RpcContext, SessionNode, ProjectWorkspace],
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    ctx, project_session, project = project_sandbox_ctx
    Path(project.path).rmdir()
    explicit = tmp_path / "explicit"
    explicit.mkdir()

    result = await _handle_sandbox_path_list(
        {
            "sessionKey": project_session.session_key,
            "path": str(explicit),
            "kind": "workspace",
        },
        ctx,
    )

    assert result["currentPath"] == str(explicit.resolve(strict=True))


@pytest.mark.asyncio
async def test_path_list_falls_back_to_home_when_agent_workspace_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()
    home = tmp_path / "home"
    home.mkdir()
    missing_workspace = tmp_path / "missing-agent-workspace"
    ctx = _ctx(manager)
    ctx.config.workspace_dir = str(missing_workspace)
    monkeypatch.setattr(
        rpc_sandbox.Path,
        "home",
        classmethod(lambda cls: home),
    )

    result = await rpc_sandbox._handle_sandbox_path_list(
        {"sessionKey": manager.node.session_key, "kind": "workspace"},
        ctx,
    )

    assert result["currentPath"] == str(home.resolve(strict=True))
    assert result["path"] == result["currentPath"]


@pytest.mark.asyncio
async def test_path_list_falls_back_to_home_when_agent_workspace_symlink_loops(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()
    home = tmp_path / "home"
    home.mkdir()
    looping_workspace = tmp_path / "looping-agent-workspace"
    looping_workspace.symlink_to(looping_workspace, target_is_directory=True)
    ctx = _ctx(manager)
    ctx.config.workspace_dir = str(looping_workspace)
    monkeypatch.setattr(
        rpc_sandbox.Path,
        "home",
        classmethod(lambda cls: home),
    )

    result = await rpc_sandbox._handle_sandbox_path_list(
        {"sessionKey": manager.node.session_key, "kind": "workspace"},
        ctx,
    )

    assert result["currentPath"] == str(home.resolve(strict=True))


@pytest.mark.asyncio
async def test_path_list_explicit_symlink_loop_remains_strict(tmp_path: Path) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    looping_path = tmp_path / "looping-explicit-path"
    looping_path.symlink_to(looping_path, target_is_directory=True)

    with pytest.raises(RuntimeError, match="Symlink loop"):
        await _handle_sandbox_path_list(
            {
                "sessionKey": manager.node.session_key,
                "path": str(looping_path),
                "kind": "workspace",
            },
            _ctx(manager),
        )


@pytest.mark.asyncio
async def test_path_list_falls_back_to_home_when_agent_workspace_is_not_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()
    home = tmp_path / "home"
    home.mkdir()
    workspace_file = tmp_path / "agent-workspace-file"
    workspace_file.write_text("not a directory", encoding="utf-8")
    ctx = _ctx(manager)
    ctx.config.workspace_dir = str(workspace_file)
    monkeypatch.setattr(
        rpc_sandbox.Path,
        "home",
        classmethod(lambda cls: home),
    )

    result = await rpc_sandbox._handle_sandbox_path_list(
        {"sessionKey": manager.node.session_key, "path": None, "kind": "workspace"},
        ctx,
    )

    assert result["currentPath"] == str(home.resolve(strict=True))


@pytest.mark.asyncio
async def test_path_list_parent_and_selectability_contract(
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    root = tmp_path / "root"
    child = root / "child"
    nested = child / "nested"
    file_entry = child / "notes.txt"
    nested.mkdir(parents=True)
    file_entry.write_text("notes", encoding="utf-8")

    result = await _handle_sandbox_path_list(
        {
            "sessionKey": manager.node.session_key,
            "path": str(child),
            "kind": "workspace",
        },
        _ctx(manager),
    )

    assert result["currentPath"] == str(child.resolve())
    assert result["path"] == result["currentPath"]
    assert result["parentPath"] == str(root.resolve())
    assert all(row["name"] != ".." for row in result["entries"])
    file_row = next(row for row in result["entries"] if row["kind"] == "file")
    assert file_row["path"] == str(file_entry.resolve())
    assert file_row["selectable"] is False
    directory_row = next(row for row in result["entries"] if row["kind"] == "directory")
    assert directory_row["path"] == str(nested.resolve())
    assert directory_row["selectable"] is True


@pytest.mark.asyncio
async def test_path_list_mount_files_are_selectable(tmp_path: Path) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    target = tmp_path / "mount"
    target.mkdir()
    file_entry = target / "notes.txt"
    file_entry.write_text("notes", encoding="utf-8")

    result = await _handle_sandbox_path_list(
        {
            "sessionKey": manager.node.session_key,
            "path": str(target),
            "kind": "mount",
        },
        _ctx(manager),
    )

    file_row = next(row for row in result["entries"] if row["kind"] == "file")
    assert file_row["path"] == str(file_entry.resolve())
    assert file_row["selectable"] is True


@pytest.mark.asyncio
async def test_path_list_root_has_null_parent(tmp_path: Path) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    root = Path(tmp_path.anchor)

    result = await _handle_sandbox_path_list(
        {
            "sessionKey": manager.node.session_key,
            "path": str(root),
            "kind": "workspace",
        },
        _ctx(manager),
    )

    assert result["currentPath"] == str(root.resolve(strict=True))
    assert result["path"] == result["currentPath"]
    assert result["parentPath"] is None
    assert all(row["name"] != ".." for row in result["entries"])


@pytest.mark.asyncio
@pytest.mark.parametrize("base_path", [None, "", "relative/base", 42])
async def test_path_list_relative_path_requires_absolute_base(
    tmp_path: Path,
    base_path: object,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    (tmp_path / "child").mkdir()
    params: dict[str, object] = {
        "sessionKey": manager.node.session_key,
        "path": "child",
        "kind": "workspace",
    }
    if base_path is not None:
        params["basePath"] = base_path

    with pytest.raises(
        ValueError,
        match="relative path requires absolute basePath",
    ):
        await _handle_sandbox_path_list(params, _ctx(manager))


@pytest.mark.asyncio
async def test_path_list_relative_path_resolves_against_base(tmp_path: Path) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    target = tmp_path / "child"
    target.mkdir()

    result = await _handle_sandbox_path_list(
        {
            "sessionKey": manager.node.session_key,
            "path": "child",
            "basePath": str(tmp_path),
            "kind": "workspace",
        },
        _ctx(manager),
    )

    assert result["currentPath"] == str(target.resolve(strict=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("base_kind", ["missing", "file"])
async def test_path_list_relative_path_requires_existing_directory_base(
    tmp_path: Path,
    base_kind: str,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    base = tmp_path / "base"
    if base_kind == "file":
        base.write_text("not a directory", encoding="utf-8")

    expected_error = FileNotFoundError if base_kind == "missing" else NotADirectoryError
    with pytest.raises(expected_error):
        await _handle_sandbox_path_list(
            {
                "sessionKey": manager.node.session_key,
                "path": "child",
                "basePath": str(base),
                "kind": "workspace",
            },
            _ctx(manager),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["", "   ", 42, [], {}])
async def test_path_list_rejects_invalid_explicit_path(path: object) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()

    with pytest.raises(ValueError, match="params.path"):
        await _handle_sandbox_path_list(
            {
                "sessionKey": manager.node.session_key,
                "path": path,
                "kind": "workspace",
            },
            _ctx(manager),
        )


@pytest.mark.asyncio
async def test_path_list_missing_or_inaccessible_directory_is_an_error(
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()

    with pytest.raises(FileNotFoundError):
        await _handle_sandbox_path_list(
            {
                "sessionKey": manager.node.session_key,
                "path": str(tmp_path / "missing"),
                "kind": "workspace",
            },
            _ctx(manager),
        )


@pytest.mark.asyncio
async def test_path_list_file_target_is_an_error(tmp_path: Path) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    file_target = tmp_path / "notes.txt"
    file_target.write_text("notes", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        await _handle_sandbox_path_list(
            {
                "sessionKey": manager.node.session_key,
                "path": str(file_target),
                "kind": "workspace",
            },
            _ctx(manager),
        )


@pytest.mark.asyncio
async def test_path_list_does_not_swallow_directory_access_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    target = tmp_path / "blocked"
    target.mkdir()
    original_iterdir = Path.iterdir

    def denied_iterdir(path: Path):
        if path == target:
            raise PermissionError("denied by test")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied_iterdir)

    with pytest.raises(PermissionError, match="denied by test"):
        await _handle_sandbox_path_list(
            {
                "sessionKey": manager.node.session_key,
                "path": str(target),
                "kind": "workspace",
            },
            _ctx(manager),
        )


@pytest.mark.asyncio
async def test_rpc_sandbox_path_list_requires_owner(tmp_path) -> None:
    from opensquilla.gateway.rpc import RpcHandlerError
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    target = tmp_path / "target"
    target.mkdir()

    with pytest.raises(RpcHandlerError, match="requires owner principal"):
        await _handle_sandbox_path_list(
            {
                "sessionKey": manager.node.session_key,
                "path": str(target),
            },
            _ctx(manager, is_owner=False),
        )


@pytest.mark.asyncio
async def test_path_create_directory_creates_single_child(tmp_path: Path) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_create_directory

    manager = _SessionManager()
    parent = tmp_path / "parent"
    parent.mkdir()

    result = await _handle_sandbox_path_create_directory(
        {
            "sessionKey": manager.node.session_key,
            "parentPath": str(parent),
            "name": "new-project",
            "kind": "workspace",
        },
        _ctx(manager),
    )

    created = parent / "new-project"
    assert created.is_dir()
    assert result == {
        "path": str(created.resolve(strict=True)),
        "name": "new-project",
        "kind": "directory",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["", "   ", ".", "..", "../escape", "a/b", "a\\b", "/tmp/x"])
async def test_path_create_directory_rejects_invalid_name(
    tmp_path: Path,
    name: str,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_create_directory

    manager = _SessionManager()
    with pytest.raises(ValueError, match="params.name"):
        await _handle_sandbox_path_create_directory(
            {
                "sessionKey": manager.node.session_key,
                "parentPath": str(tmp_path),
                "name": name,
            },
            _ctx(manager),
        )


@pytest.mark.asyncio
async def test_path_create_directory_requires_owner(tmp_path: Path) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_create_directory

    manager = _SessionManager()
    with pytest.raises(RpcHandlerError, match="requires owner principal"):
        await _handle_sandbox_path_create_directory(
            {
                "sessionKey": manager.node.session_key,
                "parentPath": str(tmp_path),
                "name": "blocked",
            },
            _ctx(manager, is_owner=False),
        )


@pytest.mark.asyncio
async def test_rpc_sandbox_path_list_ignores_legacy_browse_children(
    tmp_path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_path_list

    manager = _SessionManager()
    parent = tmp_path / "parent"
    child = parent / "child"
    grandchild = child / "grandchild"
    child_file = child / "inside.txt"
    child.mkdir(parents=True)
    grandchild.mkdir()
    child_file.write_text("inside", encoding="utf-8")

    result = await _handle_sandbox_path_list(
        {
            "sessionKey": manager.node.session_key,
            "path": str(child),
            "browseChildren": True,
        },
        _ctx(manager),
    )

    assert result["currentPath"] == str(child.resolve())
    assert result["path"] == result["currentPath"]
    assert result["parentPath"] == str(parent.resolve())
    entries_by_name = {entry["name"]: entry for entry in result["entries"]}
    assert ".." not in entries_by_name
    assert entries_by_name["grandchild"]["path"] == str(grandchild)
    assert entries_by_name["grandchild"]["kind"] == "directory"
    assert entries_by_name["grandchild"]["selectable"] is True
    assert entries_by_name["inside.txt"]["path"] == str(child_file)
    assert entries_by_name["inside.txt"]["kind"] == "file"
    assert entries_by_name["inside.txt"]["selectable"] is False


@pytest.mark.asyncio
async def test_rpc_workspace_save_does_not_use_sensitive_path_names(
    tmp_path,
) -> None:
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_workspace_set

    manager = _SessionManager()
    selected = tmp_path / ".aws" / "credentials"
    selected.parent.mkdir()
    selected.write_text("secret", encoding="utf-8")

    result = await _handle_sandbox_workspace_set(
        {
            "sessionKey": manager.node.session_key,
            "workspace": str(selected),
        },
        _ctx(manager),
    )

    assert result["workspace"] == str(selected.resolve(strict=False))
    assert manager.node.origin["sandbox_run_context"]["workspace"] == str(
        selected.resolve(strict=False)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "params", "message"),
    [
        ("_handle_sandbox_domain_add", {"domain": "127.0.0.1"}, "ip_literal"),
        ("_handle_sandbox_domain_remove", {"domain": "*.com"}, "broad_wildcard"),
    ],
)
async def test_rpc_sandbox_semantic_validation_does_not_create_missing_session(
    handler_name: str,
    params: dict[str, str],
    message: str,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()
    missing_session_key = "agent:main:webchat:missing"
    handler = getattr(rpc_sandbox, handler_name)

    with pytest.raises(ValueError, match=message):
        await handler(
            {"sessionKey": missing_session_key, **params},
            _ctx(manager),
        )

    assert missing_session_key not in manager.sessions
    assert manager.created == []


def test_non_owner_route_full_hint_coerces_to_trusted() -> None:
    from opensquilla.gateway.routing import build_web_route_envelope, tool_context_from_envelope
    from opensquilla.sandbox.run_context import RunContext
    from opensquilla.sandbox.run_mode import RunMode

    envelope = build_web_route_envelope(
        session_key="agent:main:webchat:abc",
        principal_is_owner=False,
    )
    envelope.metadata["run_mode"] = "full"
    envelope.metadata["sandbox_run_context"] = RunContext(
        run_mode=RunMode.FULL,
        source="route_metadata",
    ).to_origin_payload()

    ctx = tool_context_from_envelope(envelope, is_owner=False)

    assert ctx.run_mode == "trusted"
    assert ctx.sandbox_run_context is not None
    assert ctx.sandbox_run_context.run_mode is RunMode.TRUSTED


def test_non_owner_route_trusted_hint_is_preserved() -> None:
    from opensquilla.gateway.routing import build_web_route_envelope, tool_context_from_envelope
    from opensquilla.sandbox.run_context import RunContext
    from opensquilla.sandbox.run_mode import RunMode

    envelope = build_web_route_envelope(
        session_key="agent:main:webchat:abc",
        principal_is_owner=False,
    )
    envelope.metadata["run_mode"] = "trusted"
    envelope.metadata["sandbox_run_context"] = RunContext(
        run_mode=RunMode.TRUSTED,
        source="route_metadata",
    ).to_origin_payload()

    ctx = tool_context_from_envelope(envelope, is_owner=False)

    assert ctx.run_mode == "trusted"
    assert ctx.sandbox_run_context is not None
    assert ctx.sandbox_run_context.run_mode is RunMode.TRUSTED


@pytest.mark.asyncio
async def test_rpc_sandbox_resume_clears_denial_pause(monkeypatch) -> None:
    # The owner-scoped resume RPC is the recovery surface for the sticky denial
    # pause (issue #469): it must clear the pause so the run can continue.
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_resume
    from opensquilla.sandbox.governance import DenialLedger

    ledger = DenialLedger(threshold=3)
    manager = _SessionManager()
    key = manager.node.session_key
    await ledger.mark_paused(key)
    monkeypatch.setattr(
        "opensquilla.sandbox.integration.get_runtime",
        lambda: SimpleNamespace(ledger=ledger),
    )

    result = await _handle_sandbox_resume({"sessionKey": key}, _ctx(manager))

    assert result["resumed"] is True
    assert result["autonomousPaused"] is False
    assert await ledger.is_paused(key) is False


@pytest.mark.asyncio
async def test_rpc_sandbox_resume_rejects_non_owner() -> None:
    from opensquilla.gateway.rpc import RpcHandlerError
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_resume

    manager = _SessionManager()
    with pytest.raises(RpcHandlerError, match="requires owner principal"):
        await _handle_sandbox_resume(
            {"sessionKey": manager.node.session_key},
            _ctx(manager, is_owner=False),
        )
