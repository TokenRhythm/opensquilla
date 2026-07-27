from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _SessionManager:
    def __init__(self):
        self.node = SimpleNamespace(
            session_key="agent:main:webchat:abc",
            agent_id="main",
            origin=None,
        )
        self.sessions = {self.node.session_key: self.node}
        self.created: list[str] = []

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


@pytest.fixture(autouse=True)
def _reset_resolved_overlays() -> None:
    from opensquilla.sandbox.escalation import reset_resolved_run_context_overlays

    reset_resolved_run_context_overlays()
    yield
    reset_resolved_run_context_overlays()


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

    assert result["domains"] == [
        {"domain": "pypi.org", "scope": "workspace", "source": "manual"}
    ]


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
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
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
    queue = get_approval_queue()
    approval_id = queue.request(namespace="exec", params=params)
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
    approval_id = queue.request(namespace="exec", params=params)
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
    approval_id = queue.request(namespace="exec", params=params)
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
    approval_id = queue.request(namespace="exec", params=params)

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
    approval_id = queue.request(namespace="exec", params=params)

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
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
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
    queue = get_approval_queue()
    approval_id = queue.request(namespace="exec", params=params)

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
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
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
    queue = get_approval_queue()
    approval_id = queue.request(namespace="exec", params=params)

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
    approval_id = queue.request(namespace="exec", params=params)

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
    approval_id = queue.request(namespace="exec", params=params)

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
    approval_id = queue.request(namespace="exec", params=params)

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
    approval_id = queue.request(namespace="exec", params=params)
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
    approval_id = queue.request(namespace="exec", params=params)

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

    assert result["mounts"] == [
        {"path": normalized_path, "access": "ro", "scope": "workspace"}
    ]
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
    upsert_domain_grant(
        {"domain": "example.com", "scope": "workspace", "source": "manual"}
    )
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
        bundle["bundle_id"]: set(bundle["domains"])
        for bundle in result["bundle_catalog"]
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
    monkeypatch.setattr(
        rpc_sandbox,
        "_pick_directory_path",
        lambda initial_dir=None: "/etc/shadow",
    )

    result = await rpc_sandbox._handle_sandbox_path_pick(
        {
            "sessionKey": manager.node.session_key,
            "kind": "workspace",
        },
        _ctx(manager),
    )

    assert result == {"path": "/etc/shadow", "kind": "workspace"}


@pytest.mark.asyncio
async def test_rpc_sandbox_path_pick_returns_valid_mount_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()
    selected = tmp_path / "external"
    selected.mkdir()
    monkeypatch.setattr(
        rpc_sandbox,
        "_pick_directory_path",
        lambda initial_dir=None: str(selected),
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
async def test_rpc_sandbox_path_list_returns_parent_directory_entries(
    tmp_path,
) -> None:
    import opensquilla.gateway.rpc_sandbox as rpc_sandbox

    manager = _SessionManager()
    parent = tmp_path / "parent"
    child = parent / "child"
    sibling = parent / "sibling"
    file_entry = parent / "notes.txt"
    child.mkdir(parents=True)
    sibling.mkdir()
    file_entry.write_text("not a directory", encoding="utf-8")
    handler = getattr(rpc_sandbox, "_handle_sandbox_path_list", None)

    assert callable(handler)

    result = await handler(
        {
            "sessionKey": manager.node.session_key,
            "path": str(child),
        },
        _ctx(manager),
    )

    assert result["path"] == str(child)
    assert result["parentPath"] == str(parent)
    entries_by_path = {entry["path"]: entry for entry in result["entries"]}
    entries_by_name = {entry["name"]: entry for entry in result["entries"]}
    assert entries_by_name[".."] == {
        "name": "..",
        "path": str(parent),
        "kind": "directory",
        "selectable": True,
    }
    assert {
        str(child),
        str(sibling),
        str(file_entry),
    }.issubset(entries_by_path)

    assert entries_by_path[str(child)]["name"] == "child"
    assert entries_by_path[str(child)]["kind"] == "directory"
    assert entries_by_path[str(child)]["selectable"] is True
    assert entries_by_path[str(sibling)]["name"] == "sibling"
    assert entries_by_path[str(sibling)]["kind"] == "directory"
    assert entries_by_path[str(sibling)]["selectable"] is True
    assert entries_by_path[str(file_entry)]["name"] == "notes.txt"
    assert entries_by_path[str(file_entry)]["kind"] == "file"
    assert entries_by_path[str(file_entry)]["selectable"] is True


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
async def test_rpc_sandbox_path_list_supports_parent_row_and_child_drilldown(
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

    assert result["path"] == str(child)
    assert result["parentPath"] == str(child)
    entries_by_name = {entry["name"]: entry for entry in result["entries"]}
    assert entries_by_name[".."] == {
        "name": "..",
        "path": str(parent),
        "kind": "directory",
        "selectable": True,
    }
    assert entries_by_name["grandchild"]["path"] == str(grandchild)
    assert entries_by_name["grandchild"]["kind"] == "directory"
    assert entries_by_name["grandchild"]["selectable"] is True
    assert entries_by_name["inside.txt"]["path"] == str(child_file)
    assert entries_by_name["inside.txt"]["kind"] == "file"
    assert entries_by_name["inside.txt"]["selectable"] is True


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
