from __future__ import annotations

import asyncio
import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from opensquilla.env import trust_env
from opensquilla.gateway import rpc_tools
from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext
from opensquilla.sandbox import integration as integration_mod
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime, sandboxed
from opensquilla.sandbox.network_guard import NetworkDecision
from opensquilla.sandbox.network_proxy import SandboxProxyServer as RealSandboxProxyServer
from opensquilla.sandbox.run_context import (
    DomainGrant,
    RunContext,
    TemporaryGrant,
    get_run_context,
)
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import (
    DenialResult,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SecurityLevel,
)
from opensquilla.tools.builtin import web as web_mod
from opensquilla.tools.builtin import web_fetch as web_fetch_mod
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture(autouse=True)
def sandbox_runtime(tmp_path: Path) -> Iterator[None]:
    reset_approval_queue()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            network_default="proxy_allowlist",
        ),
        workspace=workspace,
    )
    try:
        yield
    finally:
        reset_approval_queue()
        reset_runtime()


@pytest.fixture
def managed_context(tmp_path: Path) -> Iterator[ToolContext]:
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(tmp_path),
        session_key="s1",
        run_mode="standard",
        sandbox_run_context=RunContext(
            run_mode=RunMode.STANDARD,
            domains=(DomainGrant(domain="allowed.test"),),
        ),
    )
    token = current_tool_context.set(ctx)
    try:
        yield ctx
    finally:
        current_tool_context.reset(token)


async def _send_current_managed_proxy_request(request: bytes) -> bytes:
    proxy_url = integration_mod.current_managed_network_proxy_url()
    assert proxy_url is not None
    parsed = urlsplit(proxy_url)
    assert parsed.hostname is not None
    assert parsed.port is not None
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
    try:
        writer.write(request)
        await writer.drain()
        return await reader.read(4096)
    finally:
        writer.close()
        await writer.wait_closed()


def _install_trusted_session_handles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from opensquilla.tools.builtin import sessions as sessions_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    manager = SimpleNamespace()
    manager.node = SimpleNamespace(
        session_key="s1",
        agent_id="main",
        origin={
            "sandbox_run_context": RunContext(run_mode=RunMode.TRUSTED).to_origin_payload(),
        },
    )

    async def _get_session(session_key: str):
        return manager.node if session_key == manager.node.session_key else None

    async def _update(session_key: str, **fields):
        for key, value in fields.items():
            setattr(manager.node, key, value)
        return manager.node

    manager.get_session = _get_session
    manager.update = _update
    config = SimpleNamespace(
        workspace_dir=str(workspace),
        agents=[],
        sandbox=SimpleNamespace(
            run_mode="trusted",
            sandbox=True,
            security_grading=True,
            backend="noop",
            network_default="proxy_allowlist",
        ),
        permissions=SimpleNamespace(default_mode="off"),
    )
    monkeypatch.setattr(sessions_mod, "_session_manager", manager)
    monkeypatch.setattr(sessions_mod, "_gateway_config", config)
    return workspace, manager, config


def test_request_with_managed_network_proxy_env_overrides_user_proxy_env(
    tmp_path: Path,
) -> None:
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.PROXY_ALLOWLIST,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=("PATH",),
        require_approval=False,
        network_proxy=integration_mod.NetworkProxySpec(host="127.0.0.1", port=18080),
    )
    request = integration_mod.SandboxRequest(
        argv=("sh", "-lc", "curl https://example.com"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        env={
            "PATH": "/bin",
            "HTTP_PROXY": "http://attacker.invalid:1",
            "http_proxy": "http://attacker.invalid:3",
            "HTTPS_PROXY": "http://attacker.invalid:2",
            "https_proxy": "http://attacker.invalid:4",
            "npm_config_https_proxy": "http://attacker.invalid:5",
            "PIP_PROXY": "http://attacker.invalid:6",
            "WS_PROXY": "http://attacker.invalid:7",
            "NO_PROXY": "*",
            "no_proxy": "*",
        },
    )

    updated = integration_mod.request_with_managed_network_proxy_env(request)

    assert updated.env["PATH"] == "/bin"
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "npm_config_https_proxy",
        "PIP_PROXY",
        "WS_PROXY",
    ):
        assert updated.env[key] == "http://127.0.0.1:18080"
    for key in ("NO_PROXY", "no_proxy"):
        assert updated.env[key]
    assert "http://attacker.invalid:1" not in updated.env.values()
    assert "http://attacker.invalid:3" not in updated.env.values()
    assert "http://attacker.invalid:5" not in updated.env.values()
    assert "http://attacker.invalid:6" not in updated.env.values()
    assert "http://attacker.invalid:7" not in updated.env.values()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "PIP_PROXY"):
        assert key in updated.policy.env_allowlist
    assert request.env["HTTP_PROXY"] == "http://attacker.invalid:1"


@pytest.mark.asyncio
async def test_url_shaped_inprocess_network_action_sets_context_proxy_without_env_mutation(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    events: list[str] = []
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide
            events.append("proxy.init")

        async def start(self) -> None:
            events.append("proxy.start")
            decision = self._decide("allowed.test")
            assert isinstance(decision, NetworkDecision)
            seen["decision"] = decision.status

        async def stop(self) -> None:
            events.append("proxy.stop")

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    monkeypatch.setenv("HTTP_PROXY", "http://user.invalid:1")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setenv("http_proxy", "http://user-lower.invalid:1")
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("ALL_PROXY", "http://all.invalid:1")
    monkeypatch.delenv("all_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("OPENSQUILLA_TRUST_ENV", "0")
    expected_env = {
        key: os.environ.get(key)
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
            "NO_PROXY",
            "no_proxy",
            "OPENSQUILLA_TRUST_ENV",
        )
    }

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        seen["url"] = url
        seen["proxy_url"] = integration_mod.current_managed_network_proxy_url()
        seen["httpx_kwargs"] = integration_mod.managed_network_httpx_kwargs()
        seen["trust_env"] = trust_env()
        return "ok"

    result = await dummy_http_request("http://allowed.test/path")

    assert result == "ok"
    assert seen["decision"] == "allow"
    assert seen["proxy_url"] == "http://127.0.0.1:28080"
    assert seen["httpx_kwargs"] == {
        "proxy": "http://127.0.0.1:28080",
        "trust_env": False,
    }
    assert seen["trust_env"] is False
    assert events == ["proxy.init", "proxy.start", "proxy.stop"]
    for key, expected in expected_env.items():
        if expected is None:
            assert key not in os.environ
        else:
            assert os.environ[key] == expected
    assert integration_mod.current_managed_network_proxy_url() is None


@pytest.mark.asyncio
async def test_http_request_uses_explicit_context_proxy_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            decision = self._decide("allowed.test")
            assert isinstance(decision, NetworkDecision)
            assert decision.status == "allow"

        async def stop(self) -> None:
            return None

    class FakeAsyncClient:
        def __init__(self, **kwargs: object) -> None:
            seen["client_kwargs"] = kwargs

        async def __aenter__(self) -> FakeAsyncClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, **kwargs: object) -> object:
            seen["request_kwargs"] = kwargs
            return SimpleNamespace(
                status_code=200,
                url=kwargs["url"],
                headers={"content-type": "text/plain"},
                content=b"ok",
                text="ok",
            )

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    monkeypatch.setattr(web_mod.httpx, "AsyncClient", FakeAsyncClient)

    payload = json.loads(await web_mod.http_request("http://allowed.test/path"))

    assert payload["status"] == 200
    assert seen["client_kwargs"] == {
        "timeout": 30.0,
        "proxy": "http://127.0.0.1:28080",
        "trust_env": False,
    }


@pytest.mark.asyncio
async def test_unknown_explicit_target_does_not_preflight_without_network_request(
    managed_context: ToolContext,
) -> None:
    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        return "ok"

    result = await dummy_http_request("http://unknown.test/path")

    assert result == "ok"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_parallel_unknown_targets_do_not_preflight_without_network_request(
    managed_context: ToolContext,
) -> None:
    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        return "ok"

    first, second = await asyncio.gather(
        dummy_http_request("http://first-unknown.test/path"),
        dummy_http_request("http://second-unknown.test/path"),
    )

    assert first == "ok"
    assert second == "ok"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_temporary_network_grant_allows_retry_for_explicit_target(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            decision = self._decide("unknown.test")
            assert isinstance(decision, NetworkDecision)
            assert decision.status == "allow"

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)

    request = integration_mod.build_request(
        action_kind="network.http",
        argv=("http_request", "GET", "http://unknown.test/path"),
        cwd=Path(managed_context.workspace_dir or "."),
        policy=SandboxPolicy(
            level=SecurityLevel.STANDARD,
            network=NetworkMode.PROXY_ALLOWLIST,
            mounts=(),
            workspace_rw=True,
            tmp_writable=True,
            limits=ResourceLimits(),
            env_allowlist=("PATH",),
            require_approval=False,
        ),
    )
    managed_context.sandbox_run_context = RunContext(
        run_mode=RunMode.STANDARD,
        temporary_grants=(
            TemporaryGrant(
                kind="domain",
                value="unknown.test",
                fingerprint=integration_mod.action_fingerprint(request),
            ),
        ),
    )

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        return "ok"
    result = await dummy_http_request("http://unknown.test/path")

    assert result == "ok"


@pytest.mark.asyncio
async def test_windows_unready_boundary_blocks_decorated_network_tools(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    runtime = integration_mod.get_runtime()
    assert runtime is not None
    runtime.backend = SimpleNamespace(name="windows_default")
    managed_context.sandbox_run_context = RunContext(run_mode=RunMode.TRUSTED)

    class _UnexpectedClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pytest.fail("network tool bypassed Windows managed-network readiness")

    monkeypatch.setattr(
        integration_mod,
        "_windows_proxy_allowlist_enforced",
        lambda runtime: False,
    )
    async def _repair_failed(runtime) -> bool:
        return False

    monkeypatch.setattr(
        integration_mod,
        "_ensure_windows_proxy_allowlist_setup",
        _repair_failed,
    )
    monkeypatch.setattr(web_mod.httpx, "AsyncClient", _UnexpectedClient)
    monkeypatch.setattr(web_fetch_mod.httpx, "AsyncClient", _UnexpectedClient)
    monkeypatch.setattr(web_fetch_mod, "_check_ssrf", lambda url: None)

    http_payload = json.loads(await web_mod.http_request("https://example.com"))
    fetch_payload = json.loads(await web_fetch_mod.web_fetch("https://example.com"))

    assert http_payload["status"] == "denied"
    assert fetch_payload["status"] == "denied"
    assert "Windows sandbox managed network is unavailable" in http_payload["message"]
    assert "Windows sandbox managed network is unavailable" in fetch_payload["message"]


@pytest.mark.asyncio
async def test_windows_unready_boundary_does_not_trigger_elevation_when_not_admin(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    runtime = integration_mod.get_runtime()
    assert runtime is not None
    runtime.backend = SimpleNamespace(name="windows_default")
    managed_context.sandbox_run_context = RunContext(run_mode=RunMode.TRUSTED)

    monkeypatch.setattr(
        integration_mod,
        "_windows_proxy_allowlist_enforced",
        lambda runtime: False,
    )
    monkeypatch.setattr(integration_mod, "_windows_process_is_admin", lambda: False)

    async def fail_if_called(settings):
        pytest.fail("Trusted managed-network repair must not trigger elevation")

    monkeypatch.setattr(integration_mod, "ensure_sandbox_setup", fail_if_called, raising=False)

    assert await integration_mod._windows_proxy_allowlist_ready_or_repaired(runtime) is False


@pytest.mark.asyncio
async def test_windows_unready_boundary_repairs_when_already_admin(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    runtime = integration_mod.get_runtime()
    assert runtime is not None
    runtime.backend = SimpleNamespace(name="windows_default")
    managed_context.sandbox_run_context = RunContext(run_mode=RunMode.TRUSTED)
    calls: list[object] = []

    monkeypatch.setattr(
        integration_mod,
        "_windows_proxy_allowlist_enforced",
        lambda runtime: False,
    )
    monkeypatch.setattr(integration_mod, "_windows_process_is_admin", lambda: True)

    async def _repair(runtime):
        calls.append(runtime)
        return True

    monkeypatch.setattr(integration_mod, "_ensure_windows_proxy_allowlist_setup", _repair)

    assert await integration_mod._windows_proxy_allowlist_ready_or_repaired(runtime) is True
    assert calls == [runtime]


def test_windows_unavailable_message_reports_outdated_network_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend import windows_default_setup as setup_mod
    from opensquilla.sandbox.backend.windows_default_network import (
        WindowsNetworkSetup,
    )

    marker = tmp_path / "setup_marker.json"
    setup_mod.write_setup_marker(
        marker,
        network=WindowsNetworkSetup(
            offline_user_sid="S-1-5-21-100-200-300-400",
            allowed_proxy_ports=(48123,),
            allow_local_binding=False,
            firewall_rule_version=1,
            wfp_rule_version=1,
        ),
    )
    monkeypatch.setattr(setup_mod, "default_setup_marker_path", lambda home=None: marker)
    runtime = SimpleNamespace(backend=SimpleNamespace(name="windows_default"))

    message = integration_mod._windows_managed_network_unavailable_message(runtime)

    assert "network marker is out of date" in message
    assert "firewall=1 required=5" in message
    assert "wfp=1 required=2" in message


@pytest.mark.asyncio
async def test_windows_unready_boundary_blocks_rpc_network_action(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    runtime = integration_mod.get_runtime()
    assert runtime is not None
    runtime.backend = SimpleNamespace(name="windows_default")
    managed_context.sandbox_run_context = RunContext(run_mode=RunMode.TRUSTED)
    called = False

    async def _callback() -> str:
        nonlocal called
        called = True
        return "unexpected"

    monkeypatch.setattr(
        integration_mod,
        "_windows_proxy_allowlist_enforced",
        lambda runtime: False,
    )
    async def _repair_failed(runtime) -> bool:
        return False

    monkeypatch.setattr(
        integration_mod,
        "_ensure_windows_proxy_allowlist_setup",
        _repair_failed,
    )

    result = await integration_mod.run_in_process_network_action(
        action_kind="network.http",
        argv=("http_request", "GET", "https://example.com"),
        callback=_callback,
    )

    assert called is False
    assert isinstance(result, DenialResult)
    assert result.retryable is False
    assert "Windows sandbox managed network is unavailable" in result.message


@pytest.mark.asyncio
async def test_allow_once_resolve_allows_one_retry_then_expires_for_explicit_target(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        return "ok"

    first = await dummy_http_request("http://unknown.test/path")
    second = await dummy_http_request("http://unknown.test/path")

    assert first == "ok"
    assert second == "ok"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_allow_same_type_choice_keeps_later_unknown_public_targets_scoped(
    managed_context: ToolContext,
) -> None:
    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        return "ok"

    result = await dummy_http_request("http://docs.example.com/path")

    assert result == "ok"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_persisted_temporary_grant_from_saved_origin_does_not_allow_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            decision = self._decide("unknown.test")
            assert isinstance(decision, NetworkDecision)
            seen["decision"] = decision.status

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        return "ok"

    persisted = RunContext(
        run_mode=RunMode.STANDARD,
        temporary_grants=(
            TemporaryGrant(
                kind="domain",
                value="unknown.test",
                fingerprint="legacy-fp",
            ),
        ),
        source="saved",
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=persisted,
        )
    )
    try:
        result = await dummy_http_request("http://unknown.test/path")
    finally:
        current_tool_context.reset(token)

    assert result == "ok"
    assert seen["decision"] == "ask"


@pytest.mark.asyncio
async def test_trusted_explicit_target_does_not_auto_add_before_proxy_upstream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            decision = self._decide("api.github.com")
            assert isinstance(decision, NetworkDecision)
            assert decision.status == "allow"
            assert decision.reason == "auto_trusted"

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    workspace, manager, config = _install_trusted_session_handles(monkeypatch, tmp_path)

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        return "ok"

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(run_mode=RunMode.TRUSTED, source="route_metadata"),
        )
    )
    try:
        result = await dummy_http_request("https://api.github.com/repos/openai")
    finally:
        current_tool_context.reset(token)

    assert result == "ok"
    saved = await get_run_context(manager, "s1", config=config, workspace=str(workspace))
    assert DomainGrant(
        domain="api.github.com",
        scope="chat",
        source="auto_trusted",
    ) not in saved.domains


@pytest.mark.asyncio
async def test_trusted_inprocess_auto_trust_does_not_persist_private_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace, manager, config = _install_trusted_session_handles(monkeypatch, tmp_path)
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host: str, port: int, *args: object, **kwargs: object) -> list[tuple]:
        if host == "new-public.example":
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("127.0.0.1", port),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> bytes:
        return await _send_current_managed_proxy_request(
            b"GET http://new-public.example/path HTTP/1.1\r\n\r\n"
        )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(run_mode=RunMode.TRUSTED, source="route_metadata"),
        )
    )
    try:
        response = await dummy_http_request("http://new-public.example/path")
    finally:
        current_tool_context.reset(token)

    assert response.startswith(b"HTTP/1.1 403")
    saved = await get_run_context(manager, "s1", config=config, workspace=str(workspace))
    assert DomainGrant(
        domain="new-public.example",
        scope="chat",
        source="auto_trusted",
    ) not in saved.domains


@pytest.mark.asyncio
async def test_trusted_inprocess_auto_trust_persists_after_safe_proxy_upstream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def handle_upstream(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Length: 2\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"ok"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(handle_upstream, "127.0.0.1", 0)
    upstream_socket = next(iter(upstream.sockets or ()), None)
    assert upstream_socket is not None
    upstream_host, upstream_port = upstream_socket.getsockname()[:2]
    public_upstream_host = "93.184.216.34"
    real_open_connection = asyncio.open_connection

    async def fake_open_connection(
        host: str,
        port: int,
        *args: object,
        **kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if str(host) == public_upstream_host and int(port) == int(upstream_port):
            return await real_open_connection(
                str(upstream_host),
                int(upstream_port),
                *args,
                **kwargs,
            )
        return await real_open_connection(host, port, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    def proxy_factory(decide: object, **kwargs: object) -> RealSandboxProxyServer:
        return RealSandboxProxyServer(
            decide,
            resolver=lambda host, port: (public_upstream_host, int(upstream_port)),
            **kwargs,
        )

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", proxy_factory)
    workspace, manager, config = _install_trusted_session_handles(monkeypatch, tmp_path)

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> bytes:
        return await _send_current_managed_proxy_request(
            b"GET http://new-public.example/path HTTP/1.1\r\n\r\n"
        )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(run_mode=RunMode.TRUSTED, source="route_metadata"),
        )
    )
    try:
        response = await dummy_http_request("http://new-public.example/path")
    finally:
        current_tool_context.reset(token)
        upstream.close()
        await upstream.wait_closed()

    assert response.startswith(b"HTTP/1.1 200 OK")
    saved = await get_run_context(manager, "s1", config=config, workspace=str(workspace))
    assert DomainGrant(
        domain="new-public.example",
        scope="chat",
        source="auto_trusted",
    ) in saved.domains


@pytest.mark.asyncio
async def test_trusted_explicit_target_auto_adds_chat_domain_grant_in_production_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import sessions as sessions_mod

    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide
            self._on_upstream_opened = kwargs.get("on_upstream_opened")

        async def start(self) -> None:
            decision = self._decide("api.github.com")
            assert isinstance(decision, NetworkDecision)
            assert decision.status == "allow"
            assert self._on_upstream_opened is not None
            await self._on_upstream_opened(decision)
            seen["callback_called"] = True

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    manager = SimpleNamespace()
    manager.node = SimpleNamespace(
        session_key="s1",
        agent_id="main",
        origin={
            "sandbox_run_context": RunContext(run_mode=RunMode.TRUSTED).to_origin_payload(),
        },
    )

    async def _get_session(session_key: str):
        return manager.node if session_key == manager.node.session_key else None

    async def _update(session_key: str, **fields):
        for key, value in fields.items():
            setattr(manager.node, key, value)
        return manager.node

    manager.get_session = _get_session
    manager.update = _update
    config = SimpleNamespace(
        workspace_dir=str(workspace),
        agents=[],
        sandbox=SimpleNamespace(
            run_mode="trusted",
            sandbox=True,
            security_grading=True,
            backend="noop",
            network_default="proxy_allowlist",
        ),
        permissions=SimpleNamespace(default_mode="off"),
    )
    monkeypatch.setattr(sessions_mod, "_session_manager", manager)
    monkeypatch.setattr(sessions_mod, "_gateway_config", config)

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        return "ok"

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(run_mode=RunMode.TRUSTED, source="route_metadata"),
        )
    )
    try:
        result = await dummy_http_request("https://api.github.com/repos/openai")
    finally:
        current_tool_context.reset(token)

    assert result == "ok"
    assert seen["callback_called"] is True
    saved = await get_run_context(manager, "s1", config=config, workspace=str(workspace))
    assert DomainGrant(
        domain="api.github.com",
        scope="chat",
        source="auto_trusted",
    ) in saved.domains


@pytest.mark.asyncio
async def test_standard_explicit_target_does_not_auto_add_recognized_default_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.tools.builtin import sessions as sessions_mod

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            decision = self._decide("api.github.com")
            assert isinstance(decision, NetworkDecision)
            assert decision.status == "allow"
            assert decision.reason == "default_allowlist"

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    manager = SimpleNamespace()
    manager.node = SimpleNamespace(
        session_key="s1",
        agent_id="main",
        origin={
            "sandbox_run_context": RunContext(run_mode=RunMode.STANDARD).to_origin_payload(),
        },
    )

    async def _get_session(session_key: str):
        return manager.node if session_key == manager.node.session_key else None

    async def _update(session_key: str, **fields):
        for key, value in fields.items():
            setattr(manager.node, key, value)
        return manager.node

    manager.get_session = _get_session
    manager.update = _update
    config = SimpleNamespace(
        workspace_dir=str(workspace),
        agents=[],
        sandbox=SimpleNamespace(
            run_mode="standard",
            sandbox=True,
            security_grading=True,
            backend="noop",
            network_default="proxy_allowlist",
        ),
        permissions=SimpleNamespace(default_mode="off"),
    )
    monkeypatch.setattr(sessions_mod, "_session_manager", manager)
    monkeypatch.setattr(sessions_mod, "_gateway_config", config)

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        return "ok"

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(run_mode=RunMode.STANDARD, source="route_metadata"),
        )
    )
    try:
        result = await dummy_http_request("https://api.github.com/repos/openai")
    finally:
        current_tool_context.reset(token)

    assert result == "ok"
    saved = await get_run_context(manager, "s1", config=config, workspace=str(workspace))
    assert saved.domains == ()


@pytest.mark.asyncio
async def test_run_with_managed_network_proxy_honors_temporary_domain_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            decision = self._decide("temp-allowed.test")
            assert isinstance(decision, NetworkDecision)
            seen["decision"] = decision

        async def stop(self) -> None:
            return None

    class FakeBackend:
        name = "fake"

        async def run(self, request):
            seen["policy"] = request.policy
            return SimpleNamespace(
                returncode=0,
                stdout="ok",
                stderr="",
                wall_time_s=0.1,
                backend_used="fake",
                backend_notes=(),
            )

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
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

    request = integration_mod.build_request(
        action_kind="network.http",
        argv=("http_request", "GET", "http://temp-allowed.test/path"),
        cwd=workspace,
        policy=policy,
    )
    runtime = SimpleNamespace(
        backend=FakeBackend(),
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(
                run_mode=RunMode.STANDARD,
                temporary_grants=(
                    TemporaryGrant(
                        kind="domain",
                        value="temp-allowed.test",
                        fingerprint=integration_mod.action_fingerprint(request),
                    ),
                ),
            ),
        )
    )
    try:
        result = await integration_mod._run_with_managed_network_proxy(request, runtime)
    finally:
        current_tool_context.reset(token)

    assert result.stdout == "ok"
    decision = seen["decision"]
    assert isinstance(decision, NetworkDecision)
    assert decision.status == "allow"
    assert decision.reason == "domain_grant"


@pytest.mark.asyncio
async def test_windows_fixed_proxy_port_subprocesses_are_serialized(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
    tmp_path: Path,
) -> None:
    runtime = integration_mod.get_runtime()
    assert runtime is not None
    runtime.backend = SimpleNamespace(name="windows_default")
    managed_context.sandbox_run_context = RunContext(
        run_mode=RunMode.STANDARD,
        domains=(DomainGrant(domain="example.com"),),
    )
    monkeypatch.setattr(integration_mod, "_windows_allowed_proxy_ports", lambda _rt: (48123,))
    monkeypatch.setattr(
        integration_mod,
        "_windows_proxy_allowlist_enforced",
        lambda _rt, *, proxy_ports=(): True,
    )

    events: list[str] = []
    active = 0

    class FakeProxy:
        host = "127.0.0.1"

        def __init__(self, _service: object, *, port: int = 0, **_kwargs: object) -> None:
            self.port = port

        async def start(self) -> None:
            nonlocal active
            if active:
                raise AssertionError("fixed Windows proxy port was started concurrently")
            active += 1
            events.append(f"start:{self.port}")

        async def stop(self) -> None:
            nonlocal active
            active -= 1
            events.append("stop")

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    first_ready = asyncio.Event()
    release_first = asyncio.Event()
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

    async def run_managed(name: str) -> None:
        request = integration_mod.build_request(
            action_kind="shell.exec",
            argv=("curl", f"https://example.com/{name}"),
            cwd=tmp_path,
            policy=policy,
        )
        managed = await integration_mod.prepare_subprocess_managed_network_proxy(
            request,
            runtime=runtime,
        )
        events.append(f"ready:{name}")
        try:
            if name == "first":
                first_ready.set()
                await release_first.wait()
        finally:
            await managed.cleanup()

    first = asyncio.create_task(run_managed("first"))
    await first_ready.wait()
    second = asyncio.create_task(run_managed("second"))
    await asyncio.sleep(0)

    assert events == ["start:48123", "ready:first"]

    release_first.set()
    await asyncio.gather(first, second)

    assert events == [
        "start:48123",
        "ready:first",
        "stop",
        "start:48123",
        "ready:second",
        "stop",
    ]
    assert active == 0


@pytest.mark.asyncio
async def test_run_with_managed_network_proxy_does_not_auto_add_before_proxy_upstream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide
            seen["has_callback"] = kwargs.get("on_upstream_opened") is not None

        async def start(self) -> None:
            decision = self._decide("api.github.com")
            assert isinstance(decision, NetworkDecision)
            assert decision.status == "allow"
            assert decision.reason == "auto_trusted"

        async def stop(self) -> None:
            return None

    class FakeBackend:
        name = "fake"

        async def run(self, request):
            seen["policy"] = request.policy
            return SimpleNamespace(
                returncode=0,
                stdout="ok",
                stderr="",
                wall_time_s=0.1,
                backend_used="fake",
                backend_notes=(),
            )

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    workspace, manager, config = _install_trusted_session_handles(monkeypatch, tmp_path)
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
    request = integration_mod.build_request(
        action_kind="network.http",
        argv=("http_request", "GET", "https://api.github.com/repos/openai"),
        cwd=workspace,
        policy=policy,
    )
    runtime = SimpleNamespace(
        backend=FakeBackend(),
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
            run_mode="trusted",
            sandbox_run_context=RunContext(run_mode=RunMode.TRUSTED, source="route_metadata"),
        )
    )
    try:
        result = await integration_mod._run_with_managed_network_proxy(request, runtime)
    finally:
        current_tool_context.reset(token)

    assert result.stdout == "ok"
    assert seen["has_callback"] is True
    saved = await get_run_context(manager, "s1", config=config, workspace=str(workspace))
    assert DomainGrant(
        domain="api.github.com",
        scope="chat",
        source="auto_trusted",
    ) not in saved.domains


@pytest.mark.asyncio
async def test_web_fetch_cache_hit_requires_current_run_context_grant(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    url = "http://blocked.test/page"
    web_fetch_mod._cache.clear()
    web_fetch_mod._cache[(url, "markdown")] = {
        "url": url,
        "final_url": url,
        "status": 200,
        "content_type": "text/html",
        "title": "",
        "extract_mode": "markdown",
        "extractor": "cache",
        "truncated": False,
        "length": 13,
        "text": "cached secret",
    }
    monkeypatch.setattr(web_fetch_mod, "_check_ssrf", lambda value: None)
    monkeypatch.setattr(
        integration_mod,
        "SandboxProxyServer",
        lambda *args, **kwargs: pytest.fail("proxy should not start for denied target"),
    )

    result = await web_fetch_mod.web_fetch(url)

    payload = json.loads(result)
    assert payload["status"] == "approval_required"
    assert payload["approval_id"]
    assert payload["approvalKind"] == "sandbox_network"
    assert payload["host"] == "blocked.test"
    assert "cached secret" not in result


@pytest.mark.asyncio
async def test_rpc_search_query_allows_search_provider_endpoint_under_managed_network(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            provider_decision = self._decide("api.search.brave.com")
            unknown_decision = self._decide("not-approved.example")
            assert isinstance(provider_decision, NetworkDecision)
            assert isinstance(unknown_decision, NetworkDecision)
            seen["provider_decision"] = provider_decision.status
            seen["provider_reason"] = provider_decision.reason
            seen["unknown_decision"] = unknown_decision.status

        async def stop(self) -> None:
            return None

    async def fake_search(*args: object, **kwargs: object) -> dict[str, object]:
        seen["search_called"] = True
        seen["provider_kwargs"] = web_mod._search_provider_kwargs("brave")
        return {
            "ok": True,
            "query": "python packages",
            "provider": "brave",
            "results": [{"title": "PyPI", "url": "https://pypi.org", "snippet": ""}],
        }

    monkeypatch.setattr(rpc_tools, "run_web_search_payload", fake_search)
    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    web_mod.configure_search("brave", api_key="test-key")
    ctx = RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.write", "operator.read"]),
            is_owner=True,
            authenticated=True,
        ),
    )

    result = await rpc_tools._handle_search_query({"query": "python packages"}, ctx)

    assert result["ok"] is True
    assert result["provider"] == "brave"
    assert seen == {
        "provider_decision": "allow",
        "provider_reason": "system_domain_grant",
        "unknown_decision": "ask",
        "provider_kwargs": {
            "proxy": "http://127.0.0.1:28080",
            "use_env_proxy": False,
            "api_key": "test-key",
            "diagnostics": False,
        },
        "search_called": True,
    }


@pytest.mark.asyncio
async def test_rpc_search_query_grants_only_auto_execution_plan_providers(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            for host in ("api.bochaai.com", "api.tavily.com"):
                decision = self._decide(host)
                assert isinstance(decision, NetworkDecision)
                seen[host] = decision.status
            duckduckgo = self._decide("html.duckduckgo.com")
            assert isinstance(duckduckgo, NetworkDecision)
            seen["html.duckduckgo.com"] = duckduckgo.status
            seen["html.duckduckgo.com.reason"] = duckduckgo.reason

        async def stop(self) -> None:
            return None

    async def fake_search(*args: object, **kwargs: object) -> dict[str, object]:
        seen["search_called"] = True
        return {
            "ok": True,
            "query": "python packages",
            "provider": "bocha",
            "results": [],
        }

    for key in (
        "BOCHA_SEARCH_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "IQS_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BOCHA_SEARCH_API_KEY", "bocha-key")
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")
    monkeypatch.setattr(rpc_tools, "run_web_search_payload", fake_search)
    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    web_mod.configure_search("duckduckgo", fallback_policy="network")
    ctx = RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.write", "operator.read"]),
            is_owner=True,
            authenticated=True,
        ),
    )

    try:
        result = await rpc_tools._handle_search_query({"query": "python packages"}, ctx)
    finally:
        web_mod.reset_search_runtime()

    assert result["ok"] is True
    assert seen == {
        "api.bochaai.com": "allow",
        "api.tavily.com": "allow",
        "html.duckduckgo.com": "allow",
        "html.duckduckgo.com.reason": "default_allowlist",
        "search_called": True,
    }


@pytest.mark.asyncio
async def test_rpc_search_query_without_runtime_uses_provider_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_runtime()

    async def fake_search(*args: object, **kwargs: object) -> dict[str, object]:
        return {
            "ok": True,
            "query": "python packages",
            "provider": "fake",
            "results": [{"title": "PyPI", "url": "https://pypi.org", "snippet": "python"}],
        }

    monkeypatch.setattr(rpc_tools, "run_web_search_payload", fake_search)
    ctx = RpcContext(
        conn_id="c",
        principal=Principal(
            role="operator",
            scopes=frozenset(["operator.write", "operator.read"]),
            is_owner=True,
            authenticated=True,
        ),
    )

    result = await rpc_tools._handle_search_query({"query": "python packages"}, ctx)

    assert result["ok"] is True
    assert result["results"] == [
        {"title": "PyPI", "url": "https://pypi.org", "snippet": "python"}
    ]


@pytest.mark.asyncio
async def test_inprocess_network_action_without_run_context_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(tmp_path),
        session_key="s1",
        run_mode="standard",
    )
    token = current_tool_context.set(ctx)
    monkeypatch.setattr(
        integration_mod,
        "SandboxProxyServer",
        lambda *args, **kwargs: pytest.fail("proxy should not start without context"),
    )
    called = False

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        nonlocal called
        called = True
        return url

    try:
        result = await dummy_http_request("http://allowed.test/path")
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["status"] == "denied"
    assert payload["reason"] == "policy_denied"
    assert "Run Context" in payload["message"]
    assert called is False


@pytest.mark.asyncio
async def test_web_search_shaped_inprocess_action_uses_search_provider_endpoint_grant(
    monkeypatch: pytest.MonkeyPatch,
    managed_context: ToolContext,
) -> None:
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            decision = self._decide("html.duckduckgo.com")
            assert isinstance(decision, NetworkDecision)
            seen["decision"] = decision.status
            seen["reason"] = decision.reason

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    web_mod.configure_search("duckduckgo")

    @sandboxed(
        "web.fetch",
        argv_factory=lambda a: (
            "web_search",
            str(a.get("query", "")),
            str(a.get("max_results", "")),
        ),
        record_payload=False,
    )
    async def dummy_web_search(query: str, max_results: int | None = None) -> str:
        seen["called"] = True
        return query

    result = await dummy_web_search("python packages", 5)

    assert result == "python packages"
    assert seen == {
        "decision": "allow",
        "reason": "system_domain_grant",
        "called": True,
    }


@pytest.mark.asyncio
async def test_inprocess_network_action_with_network_none_defers_to_proxy_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reset_runtime()
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            network_default="none",
        ),
        workspace=workspace,
    )
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(tmp_path),
        session_key="s1",
        run_mode="standard",
        sandbox_run_context=RunContext(run_mode=RunMode.STANDARD),
    )
    token = current_tool_context.set(ctx)
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            seen["started"] = True

        async def stop(self) -> None:
            seen["stopped"] = True

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)
    called = False

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        nonlocal called
        called = True
        return url

    try:
        result = await dummy_http_request("http://example.com")
    finally:
        current_tool_context.reset(token)

    assert result == "http://example.com"
    assert called is True
    assert seen == {"started": True, "stopped": True}
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_inprocess_network_action_with_network_none_uses_granted_explicit_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reset_runtime()
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    configure_runtime(
        SandboxSettings(
            run_mode="standard",
            backend="noop",
            allow_legacy_mode=True,
            network_default="none",
        ),
        workspace=workspace,
    )
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(tmp_path),
        session_key="s1",
        run_mode="standard",
        sandbox_run_context=RunContext(
            run_mode=RunMode.STANDARD,
            domains=(
                DomainGrant(
                    domain="example.com",
                    scope="chat",
                    source="user",
                ),
            ),
        ),
    )
    token = current_tool_context.set(ctx)
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            decision = self._decide("example.com")
            assert isinstance(decision, NetworkDecision)
            seen["decision"] = decision.status

        async def stop(self) -> None:
            seen["stopped"] = True

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        seen["called"] = True
        return "ok"

    try:
        result = await dummy_http_request("http://example.com/path")
    finally:
        current_tool_context.reset(token)

    assert result == "ok"
    assert seen == {"decision": "allow", "called": True, "stopped": True}


@pytest.mark.asyncio
async def test_trusted_network_none_auto_allows_unknown_explicit_public_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reset_runtime()
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    configure_runtime(
        SandboxSettings(
            run_mode="trusted",
            backend="noop",
            allow_legacy_mode=True,
            network_default="none",
        ),
        workspace=workspace,
    )
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(tmp_path),
        session_key="s1",
        run_mode="trusted",
        sandbox_run_context=RunContext(run_mode=RunMode.TRUSTED),
    )
    token = current_tool_context.set(ctx)
    seen: dict[str, object] = {}

    class FakeProxy:
        host = "127.0.0.1"
        port = 28080

        def __init__(self, decide: object, **kwargs: object) -> None:
            self._decide = decide

        async def start(self) -> None:
            decision = self._decide("new-public.example")
            assert isinstance(decision, NetworkDecision)
            seen["decision"] = decision.status
            seen["reason"] = decision.reason

        async def stop(self) -> None:
            seen["stopped"] = True

    monkeypatch.setattr(integration_mod, "SandboxProxyServer", FakeProxy)

    @sandboxed(
        "network.http",
        argv_factory=lambda a: ("http_request", "GET", str(a["url"])),
        record_payload=False,
    )
    async def dummy_http_request(url: str) -> str:
        seen["called"] = True
        return "ok"

    try:
        result = await dummy_http_request("http://new-public.example/path")
    finally:
        current_tool_context.reset(token)

    assert result == "ok"
    assert seen == {
        "decision": "allow",
        "reason": "auto_trusted",
        "called": True,
        "stopped": True,
    }
