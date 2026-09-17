from __future__ import annotations

from types import SimpleNamespace

import pytest

from opensquilla.sandbox.run_mode import RunMode


class _RuntimePreferenceStorage:
    def __init__(self, run_mode: str | None = None) -> None:
        self.run_mode = run_mode

    async def get_runtime_preference(self, key: str) -> str | None:
        assert key == "sandbox.run_mode"
        return self.run_mode


class _SessionManager:
    def __init__(
        self,
        *,
        run_mode_preference: str | None = None,
        storage: object | None = None,
    ):
        self.storage = storage or _RuntimePreferenceStorage(run_mode_preference)
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


def test_run_context_round_trips_mode_source() -> None:
    from opensquilla.sandbox.run_context import (
        RunContext,
        run_context_from_origin_payload,
    )

    context = RunContext(
        run_mode=RunMode.SAFE,
        workspace="/tmp/project",
        run_mode_source="project_default",
    )
    restored = run_context_from_origin_payload(context.to_origin_payload())
    assert restored is not None
    assert restored.run_mode_source == "project_default"


def test_run_context_rejects_unknown_mode_source() -> None:
    from opensquilla.sandbox.run_context import run_context_from_origin_payload

    restored = run_context_from_origin_payload(
        {
            "run_mode": "full",
            "workspace": "/tmp/project",
            "run_mode_source": "persisted_origin_claim",
        }
    )

    assert restored is not None
    assert restored.run_mode_source is None


def test_run_context_rejects_unhashable_mode_source() -> None:
    from opensquilla.sandbox.run_context import run_context_from_origin_payload

    restored = run_context_from_origin_payload(
        {
            "run_mode": "full",
            "workspace": "/tmp/project",
            "run_mode_source": {"forged": "user"},
        }
    )

    assert restored is not None
    assert restored.run_mode_source is None


def test_effective_legacy_project_full_remains_full() -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.sandbox.run_context import RunContext, effective_project_run_mode

    resolved = effective_project_run_mode(
        RunContext(run_mode=RunMode.FULL, workspace="/tmp/project"),
        GatewayConfig(),
    )

    assert resolved.run_mode is RunMode.FULL
    assert resolved.run_mode_source is None


def test_effective_legacy_project_preserves_explicit_full() -> None:
    from opensquilla.gateway.config import GatewayConfig
    from opensquilla.sandbox.run_context import RunContext, effective_project_run_mode

    resolved = effective_project_run_mode(
        RunContext(run_mode=RunMode.FULL, workspace="/tmp/project"),
        GatewayConfig(sandbox={"run_mode": "full"}),
    )

    assert resolved.run_mode is RunMode.FULL
    assert resolved.run_mode_source is None


@pytest.mark.asyncio
async def test_fresh_owner_default_run_context_is_full() -> None:
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
@pytest.mark.parametrize(
    ("stored_mode", "updated_config_mode", "expected"),
    [
        pytest.param("safe", "full", RunMode.SAFE, id="stored-safe"),
        pytest.param("full", "safe", RunMode.FULL, id="stored-full"),
    ],
)
async def test_persisted_owner_preference_wins_over_direct_config_update(
    tmp_path,
    stored_mode: str,
    updated_config_mode: str,
    expected: RunMode,
) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.run_context import get_run_context
    from opensquilla.session.storage import SessionStorage

    database = tmp_path / f"preferences-{stored_mode}.sqlite"
    storage = SessionStorage(str(database))
    await storage.connect()
    await storage.set_runtime_preference("sandbox.run_mode", stored_mode)
    await storage.close()

    restarted = SessionStorage(str(database))
    await restarted.connect()
    manager = _SessionManager(storage=restarted)
    config = SimpleNamespace(
        sandbox=SandboxSettings(run_mode=updated_config_mode),
        permissions=SimpleNamespace(default_mode="off"),
    )

    try:
        context = await get_run_context(
            manager,
            manager.node.session_key,
            config=config,
            workspace="/workspace",
        )
    finally:
        await restarted.close()

    assert context.run_mode is expected
    assert context.source == "default"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_mode", "updated_config_mode", "expected"),
    [
        pytest.param("trusted", "full", RunMode.SAFE, id="legacy-trusted"),
        pytest.param("managed", "full", RunMode.SAFE, id="legacy-managed"),
        pytest.param("bypass", "safe", RunMode.FULL, id="legacy-full"),
    ],
)
async def test_persisted_legacy_preference_names_resolve_to_current_modes(
    stored_mode: str,
    updated_config_mode: str,
    expected: RunMode,
) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager(run_mode_preference=stored_mode)
    context = await get_run_context(
        manager,
        manager.node.session_key,
        config=SimpleNamespace(
            sandbox=SandboxSettings(run_mode=updated_config_mode),
            permissions=SimpleNamespace(default_mode="off"),
        ),
        workspace="/workspace",
    )

    assert context.run_mode is expected
    assert context.run_mode.value in {"safe", "full"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_key",
    [
        pytest.param("agent:main:cli:fresh", id="cli"),
        pytest.param("agent:main:cron:nightly", id="background"),
        pytest.param("agent:main:task:no-hint", id="no-hint"),
    ],
)
async def test_no_hint_owner_tasks_use_persisted_preference(session_key: str) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager(run_mode_preference="safe")
    context = await get_run_context(
        manager,
        session_key,
        config=SimpleNamespace(
            sandbox=SandboxSettings(run_mode="full"),
            permissions=SimpleNamespace(default_mode="full"),
        ),
        workspace=None,
    )

    assert context.run_mode is RunMode.SAFE
    assert context.source == "default"


@pytest.mark.asyncio
async def test_delegating_manager_view_resolves_persisted_preference() -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.run_context import get_run_context

    manager = _SessionManager(run_mode_preference="safe")

    class _DelegatingManagerView:
        def __getattr__(self, name: str) -> object:
            return getattr(manager, name)

    context = await get_run_context(
        _DelegatingManagerView(),
        manager.node.session_key,
        config=SimpleNamespace(
            sandbox=SandboxSettings(run_mode="full"),
            permissions=SimpleNamespace(default_mode="full"),
        ),
        workspace=None,
    )

    assert context.run_mode is RunMode.SAFE


@pytest.mark.asyncio
async def test_sandbox_run_mode_set_persists_user_provenance() -> None:
    from opensquilla.sandbox.run_context import (
        get_run_context,
        run_context_from_origin_payload,
        set_run_mode,
    )

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
    assert ctx.run_mode == RunMode.SAFE
    assert ctx.source == "default"

    updated = await set_run_mode(manager, manager.node.session_key, RunMode.SAFE, config=config)
    assert updated.run_mode == RunMode.SAFE
    restored = run_context_from_origin_payload(
        manager.node.origin["sandbox_run_context"]
    )
    assert restored is not None
    assert restored.run_mode is RunMode.SAFE
    assert restored.run_mode_source == "user"


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
        RunMode.SAFE,
        config=config,
        workspace="/tmp/ws",
    )

    assert updated.workspace == expected_workspace
    assert manager.node.origin["other"] == {"kept": True}
    assert manager.node.origin["sandbox_run_context"]["workspace"] == expected_workspace


@pytest.mark.asyncio
async def test_saved_restricted_mode_overrides_globally_disabled_sandbox() -> None:
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

    assert ctx.run_mode == RunMode.SAFE
    assert ctx.workspace == expected_workspace
    assert ctx.source == "saved"


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
            capabilities=frozenset({"task.read"}),
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
    assert manager.node.origin["sandbox_run_context"]["run_mode_source"] == "user"


@pytest.mark.asyncio
async def test_rpc_run_context_set_decodes_non_owner_legacy_trusted_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_sandbox import _handle_sandbox_run_context_set
    from opensquilla.sandbox.capability_service import (
        REQUIRED_SAFE_CAPABILITIES,
        WINDOWS_REQUIRED_SAFE_CAPABILITIES,
        CapabilityReport,
    )

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
        return CapabilityReport.available_for(
            backend="windows_native",
            platform="win32",
            capabilities=REQUIRED_SAFE_CAPABILITIES | WINDOWS_REQUIRED_SAFE_CAPABILITIES,
        )

    monkeypatch.setattr(rpc_sandbox, "current_sandbox_capability_report", ready_status)

    result = await _handle_sandbox_run_context_set(
        {"sessionKey": manager.node.session_key, "runMode": "trusted"},
        ctx,
    )

    assert result["runMode"] == "safe"
    assert manager.node.origin["sandbox_run_context"]["run_mode"] == "safe"
    assert manager.node.origin["sandbox_run_context"]["run_mode_source"] == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_state", ["guest", "invalid"])
async def test_rpc_run_context_get_coerces_remote_guest_default_full_to_safe(
    auth_state: str,
) -> None:
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
            capabilities=frozenset({"guest.safe"}),
            auth_state=auth_state,
        ),
        session_manager=manager,
        config=config,
    )

    result = await _handle_sandbox_run_context_get(
        {"sessionKey": manager.node.session_key},
        ctx,
    )

    assert result["runMode"] == "safe"
    assert result["source"] == "default"
    assert manager.node.origin is None


@pytest.mark.asyncio
async def test_rpc_run_context_set_creates_owner_new_webchat_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.gateway import rpc_sandbox
    from opensquilla.gateway.auth import Principal
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.sandbox.capability_service import (
        REQUIRED_SAFE_CAPABILITIES,
        WINDOWS_REQUIRED_SAFE_CAPABILITIES,
        CapabilityReport,
    )

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

    async def fake_status(config: object) -> CapabilityReport:
        return CapabilityReport.available_for(
            backend="windows_native",
            platform="win32",
            capabilities=REQUIRED_SAFE_CAPABILITIES | WINDOWS_REQUIRED_SAFE_CAPABILITIES,
        )

    monkeypatch.setattr(rpc_sandbox, "current_sandbox_capability_report", fake_status)

    result = await rpc_sandbox._handle_sandbox_run_context_set(
        {"sessionKey": session_key, "runMode": "trusted"},
        ctx,
    )

    assert result["runMode"] == "safe"
    assert manager.created == [(session_key, "main")]
    assert manager.sessions[session_key].origin["sandbox_run_context"]["run_mode"] == "safe"
    assert (
        manager.sessions[session_key]
        .origin["sandbox_run_context"]["run_mode_source"]
        == "user"
    )
