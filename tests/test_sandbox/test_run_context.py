from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.sandbox.run_mode import RunMode


class _SessionManager:
    def __init__(self):
        self.node = SimpleNamespace(
            session_key="agent:main:webchat:abc",
            agent_id="main",
            origin=None,
        )
        self.sessions = {self.node.session_key: self.node}
        self.created: list[tuple[str, str]] = []

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
        self.created.append((session_key, agent_id))
        return node, True

    async def update(self, session_key: str, **fields):
        node = self.sessions[session_key]
        for key, value in fields.items():
            setattr(node, key, value)
        return node


@pytest.mark.asyncio
async def test_default_run_context_is_full_host_access() -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager()
    config = SimpleNamespace(
        sandbox=SandboxSettings(),
        permissions=SimpleNamespace(default_mode="off"),
    )

    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=config,
        workspace="/workspace",
    )

    assert context.run_mode is RunMode.FULL
    assert context.source == "default"


@pytest.mark.asyncio
async def test_run_context_initializes_from_global_default_and_persists_override() -> None:
    from opensquilla.sandbox.run_context import get_run_context, set_run_mode

    manager = _SessionManager()
    config = SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="standard", sandbox=True, security_grading=True),
        permissions=SimpleNamespace(default_mode="off"),
    )

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=config,
        workspace="/tmp/ws",
    )
    assert ctx.run_mode == RunMode.STANDARD
    assert ctx.source == "default"

    updated = await set_run_mode(manager, manager.node.session_key, RunMode.TRUSTED, config=config)
    assert updated.run_mode == RunMode.TRUSTED
    assert manager.node.origin["sandbox_run_context"]["run_mode"] == "trusted"


@pytest.mark.asyncio
async def test_set_run_mode_persists_first_workspace_and_preserves_origin_keys() -> None:
    from opensquilla.sandbox.run_context import normalize_workspace_path, set_run_mode

    manager = _SessionManager()
    manager.node.origin = {"other": {"kept": True}}
    config = SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="standard", sandbox=True, security_grading=True),
        permissions=SimpleNamespace(default_mode="off"),
    )
    expected_workspace = normalize_workspace_path("/tmp/ws")

    updated = await set_run_mode(
        manager,
        manager.node.session_key,
        RunMode.TRUSTED,
        config=config,
        workspace="/tmp/ws",
    )

    assert updated.workspace == expected_workspace
    assert manager.node.origin["other"] == {"kept": True}
    assert manager.node.origin["sandbox_run_context"]["workspace"] == expected_workspace


@pytest.mark.asyncio
async def test_globally_disabled_sandbox_overrides_saved_restricted_mode() -> None:
    from opensquilla.sandbox.run_context import get_run_context, normalize_workspace_path

    manager = _SessionManager()
    manager.node.origin = {"sandbox_run_context": {"run_mode": "standard", "workspace": "/tmp/old"}}
    config = SimpleNamespace(
        sandbox=SimpleNamespace(run_mode="full", sandbox=False, security_grading=False),
        permissions=SimpleNamespace(default_mode="full"),
    )
    expected_workspace = normalize_workspace_path("/tmp/old")

    ctx = await get_run_context(
        manager,
        manager.node.session_key,
        config=config,
        workspace="/tmp/new",
    )

    assert ctx.run_mode == RunMode.FULL
    assert ctx.workspace == expected_workspace
    assert ctx.source == "global_full"


@pytest.mark.asyncio
async def test_rpc_run_context_get_reports_missing_session() -> None:
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_get

    manager = _SessionManager()
    config = SimpleNamespace(
        workspace_dir="/tmp/ws",
        agents=[],
        sandbox=SimpleNamespace(run_mode="standard", sandbox=True, security_grading=True),
        permissions=SimpleNamespace(default_mode="off"),
    )
    ctx = RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.read"]),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=config,
    )

    with pytest.raises(KeyError, match="Session not found"):
        await _handle_sandbox_run_context_get(
            {"sessionKey": "agent:main:webchat:missing"},
            ctx,
        )


@pytest.mark.asyncio
async def test_rpc_run_context_set_rejects_non_owner_full_mode_without_mutation() -> None:
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext, RpcHandlerError
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_set

    manager = _SessionManager()
    config = SimpleNamespace(
        workspace_dir="/tmp/ws",
        agents=[],
        sandbox=SimpleNamespace(run_mode="standard", sandbox=True, security_grading=True),
        permissions=SimpleNamespace(default_mode="off"),
    )
    ctx = RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.write", "operator.read"]),
            is_owner=False,
            authenticated=True,
        ),
        session_manager=manager,
        config=config,
    )

    with pytest.raises(RpcHandlerError, match="requires owner principal"):
        await _handle_sandbox_run_context_set(
            {"sessionKey": manager.node.session_key, "runMode": "full"},
            ctx,
        )

    assert manager.node.origin is None


@pytest.mark.asyncio
async def test_rpc_run_context_set_allows_owner_full_mode() -> None:
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_set

    manager = _SessionManager()
    config = SimpleNamespace(
        workspace_dir="/tmp/ws",
        agents=[],
        sandbox=SimpleNamespace(run_mode="standard", sandbox=True, security_grading=True),
        permissions=SimpleNamespace(default_mode="off"),
    )
    ctx = RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.write", "operator.read"]),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=config,
    )

    result = await _handle_sandbox_run_context_set(
        {"sessionKey": manager.node.session_key, "runMode": "full"},
        ctx,
    )

    assert result["runMode"] == "full"
    assert manager.node.origin["sandbox_run_context"]["run_mode"] == "full"


@pytest.mark.asyncio
async def test_rpc_run_context_set_allows_non_owner_trusted_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_set
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    manager = _SessionManager()
    config = SimpleNamespace(
        workspace_dir="/tmp/ws",
        agents=[],
        sandbox=SimpleNamespace(run_mode="standard", sandbox=True, security_grading=True),
        permissions=SimpleNamespace(default_mode="off"),
    )
    ctx = RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.write", "operator.read"]),
            is_owner=False,
            authenticated=True,
        ),
        session_manager=manager,
        config=config,
    )

    async def ready_status(config):
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
            requires_admin=True,
        )

    monkeypatch.setattr(rpc_sandbox, "current_sandbox_setup_status", ready_status)

    result = await _handle_sandbox_run_context_set(
        {"sessionKey": manager.node.session_key, "runMode": "trusted"},
        ctx,
    )

    assert result["runMode"] == "trusted"
    assert manager.node.origin["sandbox_run_context"]["run_mode"] == "trusted"


@pytest.mark.asyncio
async def test_rpc_run_context_get_coerces_non_owner_default_full_to_trusted() -> None:
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_get

    manager = _SessionManager()
    config = SimpleNamespace(
        workspace_dir="/tmp/ws",
        agents=[],
        sandbox=SimpleNamespace(run_mode="full", sandbox=False, security_grading=False),
        permissions=SimpleNamespace(default_mode="full"),
    )
    ctx = RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.read"]),
            is_owner=False,
            authenticated=False,
        ),
        session_manager=manager,
        config=config,
    )

    result = await _handle_sandbox_run_context_get(
        {"sessionKey": manager.node.session_key},
        ctx,
    )

    assert result["runMode"] == "trusted"
    assert result["source"] == "default"
    assert manager.node.origin is None


@pytest.mark.asyncio
async def test_rpc_run_context_set_creates_owner_new_webchat_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.sandbox.setup_state import SandboxSetupState, SetupResult

    manager = _SessionManager()
    session_key = "agent:main:webchat:dkkwi6so"
    config = SimpleNamespace(
        workspace_dir="/tmp/ws",
        agents=[],
        sandbox=SimpleNamespace(run_mode="standard", sandbox=True, security_grading=True),
        permissions=SimpleNamespace(default_mode="off"),
    )
    ctx = RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.write", "operator.read"]),
            is_owner=True,
            authenticated=True,
        ),
        session_manager=manager,
        config=config,
    )

    async def fake_status(config: object) -> SetupResult:
        return SetupResult(
            state=SandboxSetupState.READY,
            platform="win32",
            message="ready",
            requires_admin=True,
        )

    monkeypatch.setattr(rpc_sandbox, "current_sandbox_setup_status", fake_status)

    result = await rpc_sandbox._handle_sandbox_run_context_set(
        {"sessionKey": session_key, "runMode": "trusted"},
        ctx,
    )

    assert result["runMode"] == "trusted"
    assert manager.created == [(session_key, "main")]
    assert manager.sessions[session_key].origin["sandbox_run_context"]["run_mode"] == "trusted"
