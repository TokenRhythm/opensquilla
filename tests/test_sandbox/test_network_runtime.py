from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.sandbox import integration as integration_mod
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.escalation import (
    build_network_approval_params,
    request_sandbox_approval,
)
from opensquilla.sandbox.network_guard import NetworkDecision
from opensquilla.sandbox.network_proxy import SandboxProxyServer
from opensquilla.sandbox.network_runtime import (
    NetworkApprovalService,
    NetworkPolicyRequest,
    NetworkProtocol,
)
from opensquilla.sandbox.run_context import DomainGrant, RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import (
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


def _proxy_policy() -> SandboxPolicy:
    return SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.PROXY_ALLOWLIST,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=("PATH",),
        require_approval=False,
    )


async def _send_proxy_request(server: SandboxProxyServer, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection(server.host, server.port)
    try:
        writer.write(request)
        await writer.drain()
        return await asyncio.wait_for(reader.read(4096), timeout=2.0)
    finally:
        writer.close()
        await writer.wait_closed()


async def _wait_for_pending_network_approval() -> dict:
    for _ in range(100):
        pending = get_approval_queue().list_pending("exec")
        if pending:
            assert len(pending) == 1
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError("network approval was not queued")


@pytest.mark.asyncio
async def test_default_open_network_request_completes_without_approval(
    tmp_path: Path,
) -> None:
    reset_approval_queue()
    request = SandboxRequest(
        argv=("exec_command", "curl", "https://cancelled.example"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_proxy_policy(),
        session_id="cancelled-network",
        run_mode="standard",
    )
    context = RunContext(
        run_mode=RunMode.SAFE,
        workspace=str(tmp_path),
    )
    service = NetworkApprovalService(
        context=context,
        request=request,
        runtime=SimpleNamespace(workspace=tmp_path),
    )

    decision = await service.decide(
        NetworkPolicyRequest(
            protocol=NetworkProtocol.HTTPS_CONNECT,
            host="cancelled.example",
            port=443,
            method="CONNECT",
        )
    )

    assert decision.status == "allow"
    assert decision.reason == "public_default"
    assert get_approval_queue().list_pending("exec") == []


async def test_proxy_runtime_default_open_forwards_without_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reset_approval_queue()
    upstream_requests: list[bytes] = []

    async def handle_upstream(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        upstream_requests.append(await reader.readuntil(b"\r\n\r\n"))
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

    real_open_connection = asyncio.open_connection

    async def fake_open_connection(host: str, port: int, *args: object, **kwargs: object):
        if host == "93.184.216.34" and port == upstream_port:
            return await real_open_connection(str(upstream_host), upstream_port, *args, **kwargs)
        return await real_open_connection(host, port, *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    request = SandboxRequest(
        argv=("exec_command", "curl", "http://unknown.test/path"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_proxy_policy(),
        session_id="s1",
        run_mode="standard",
    )
    runtime = SimpleNamespace(workspace=tmp_path)
    tool_context = ToolContext(
        workspace_dir=str(tmp_path),
        session_key="s1",
        sandbox_run_context=RunContext(
            run_mode=RunMode.SAFE,
            workspace=str(tmp_path),
        ),
    )
    service = NetworkApprovalService(
        context=tool_context.sandbox_run_context,
        request=request,
        runtime=runtime,
        approval_timeout_seconds=2.0,
    )
    server = SandboxProxyServer(
        policy_decider=service,
        resolver=lambda host, port: ("93.184.216.34", upstream_port),
    )
    context_token = current_tool_context.set(tool_context)
    await server.start()
    try:
        response_task = asyncio.create_task(
            _send_proxy_request(
                server,
                b"GET http://unknown.test/path HTTP/1.1\r\n"
                b"Host: unknown.test\r\n"
                b"\r\n",
            )
        )
        response = await response_task
    finally:
        current_tool_context.reset(context_token)
        await server.stop()
        upstream.close()
        await upstream.wait_closed()

    assert response.startswith(b"HTTP/1.1 200 OK")
    assert b"ok" in response
    assert upstream_requests == [
        b"GET /path HTTP/1.1\r\n"
        b"Host: unknown.test\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    ]
    assert get_approval_queue().list_pending("exec") == []


async def test_trusted_runtime_network_decider_allows_without_approval(
    tmp_path: Path,
) -> None:
    reset_approval_queue()
    request = SandboxRequest(
        argv=("exec_command", "curl", "https://new-public.example"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_proxy_policy(),
        session_id="s1",
        run_mode="trusted",
    )
    service = NetworkApprovalService(
        context=RunContext(run_mode=RunMode.SAFE),
        request=request,
        runtime=SimpleNamespace(workspace=tmp_path),
        approval_timeout_seconds=0.01,
    )

    decision = await service.decide(
        NetworkPolicyRequest(
            protocol=NetworkProtocol.HTTPS_CONNECT,
            host="new-public.example",
            port=443,
            method="CONNECT",
            tool_name="exec_command",
            command="curl https://new-public.example",
        )
    )

    assert decision.status == "allow"
    assert decision.reason == "public_default"
    assert get_approval_queue().list_pending("exec") == []


async def test_default_open_does_not_request_approval_payload(tmp_path: Path) -> None:
    request = SandboxRequest(
        argv=("http_request", "GET", "https://standard-human-only.invalid/path"),
        cwd=tmp_path,
        action_kind="network.http",
        policy=_proxy_policy(),
        session_id="network-missing-approval",
        run_mode="standard",
    )
    service = NetworkApprovalService(
        context=RunContext(run_mode=RunMode.SAFE),
        request=request,
        runtime=SimpleNamespace(workspace=tmp_path),
        approval_requester=lambda *_args, **_kwargs: None,
    )

    decision = await service.decide(
        NetworkPolicyRequest(
            protocol=NetworkProtocol.HTTPS_CONNECT,
            host="unknown.test",
            port=443,
            method="CONNECT",
        )
    )

    assert decision.status == "allow"
    assert decision.reason == "public_default"


@pytest.mark.asyncio
async def test_safe_default_open_does_not_create_human_review(
    tmp_path: Path,
) -> None:
    reset_approval_queue()
    seen_params: dict[str, object] = {}
    auto_review_called = False
    runtime = SimpleNamespace(
        workspace=tmp_path,
        settings=SandboxSettings(approvals_reviewer="auto_review"),
    )
    request = SandboxRequest(
        argv=("http_request", "GET", "https://standard-human-only.invalid/path"),
        cwd=tmp_path,
        action_kind="network.http",
        policy=_proxy_policy(),
        session_id="network-standard-human",
        run_mode="standard",
    )

    def _request(params: dict[str, object], **kwargs: object) -> dict[str, object]:
        from opensquilla.sandbox.escalation import request_sandbox_approval

        seen_params.update(params)
        payload = request_sandbox_approval(params, **kwargs)
        ctx.sandbox_run_context = RunContext(
            run_mode=RunMode.SAFE,
            workspace=str(tmp_path),
            domains=(
                DomainGrant(
                    domain="standard-human-only.invalid",
                    scope="once",
                    source="temporary",
                ),
            ),
        )
        get_approval_queue().resolve(str(payload["approval_id"]), True)
        return payload

    async def _auto_review(_payload: dict[str, object]) -> None:
        nonlocal auto_review_called
        auto_review_called = True

    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(tmp_path),
        session_key="network-standard-human",
        run_mode="standard",
        sandbox_run_context=RunContext(run_mode=RunMode.SAFE),
        on_sandbox_auto_review=_auto_review,
    )
    token = current_tool_context.set(ctx)
    try:
        decision = await NetworkApprovalService(
            context=ctx.sandbox_run_context,
            request=request,
            runtime=runtime,
            approval_timeout_seconds=0.1,
            approval_requester=_request,
        ).decide(
            NetworkPolicyRequest(
                protocol=NetworkProtocol.HTTPS_CONNECT,
                host="standard-human-only.invalid",
                port=443,
                method="CONNECT",
            )
        )
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    assert decision.status == "allow"
    assert decision.reason == "public_default"
    assert seen_params == {}
    assert auto_review_called is False


@pytest.mark.asyncio
async def test_auto_review_network_request_is_hidden_and_canonical(
    tmp_path: Path,
) -> None:
    reset_approval_queue()
    seen: list[dict[str, object]] = []
    runtime = SimpleNamespace(
        workspace=tmp_path,
        settings=SandboxSettings(approvals_reviewer="auto_review"),
    )
    request = SandboxRequest(
        argv=("http_request", "GET", "https://unknown.test/path"),
        cwd=tmp_path,
        action_kind="network.http",
        policy=_proxy_policy(),
        session_id="network-auto",
        run_mode="trusted",
    )

    async def _review(payload: dict[str, object]) -> None:
        seen.append(payload)
        entry = get_approval_queue().get(str(payload["approval_id"]))
        params = entry.params
        assert params["approvalKind"] == "sandbox_network"
        assert params["reviewer"] == "auto_review"
        assert params["humanActionable"] is False
        assert "choices" not in params
        assert params["action"]["network_targets"] == ["unknown.test"]
        assert params["action"]["content_digest"] == params["fingerprint"]
        get_approval_queue().resolve(entry.approval_id, True)

    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(tmp_path),
        session_key="network-auto",
        run_mode="trusted",
        sandbox_run_context=RunContext(run_mode=RunMode.SAFE),
    )
    setattr(ctx, "on_sandbox_auto_review", _review)
    service = NetworkApprovalService(
        context=ctx.sandbox_run_context,
        request=request,
        runtime=runtime,
        approval_timeout_seconds=0.1,
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="unknown.test",
            reason="unknown_domain",
            source=None,
        ),
        session_key="network-auto",
        workspace=str(tmp_path),
        fingerprint=service.fingerprint,
        reviewer="auto_review",
    )
    assert params is not None
    payload = request_sandbox_approval(params, message="Review the exact network request.")
    token = current_tool_context.set(ctx)
    try:
        await service._run_auto_review(payload, str(payload["approval_id"]))
    finally:
        current_tool_context.reset(token)

    assert len(seen) == 1
    assert get_approval_queue().get(str(payload["approval_id"])).approved is True
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_auto_review_network_without_reviewer_callback_fails_closed(
    tmp_path: Path,
) -> None:
    reset_approval_queue()
    runtime = SimpleNamespace(
        workspace=tmp_path,
        settings=SandboxSettings(approvals_reviewer="auto_review"),
    )
    request = SandboxRequest(
        argv=("http_request", "GET", "https://unknown.test/path"),
        cwd=tmp_path,
        action_kind="network.http",
        policy=_proxy_policy(),
        session_id="network-no-reviewer",
        run_mode="trusted",
    )
    service = NetworkApprovalService(
        context=RunContext(run_mode=RunMode.SAFE),
        request=request,
        runtime=runtime,
        approval_timeout_seconds=0.1,
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="unknown.test",
            reason="unknown_domain",
            source=None,
        ),
        session_key="network-no-reviewer",
        workspace=str(tmp_path),
        fingerprint=service.fingerprint,
        reviewer="auto_review",
    )
    assert params is not None
    payload = request_sandbox_approval(params, message="Review the exact network request.")

    await service._run_auto_review(payload, str(payload["approval_id"]))

    entry = get_approval_queue().get(str(payload["approval_id"]))
    assert entry.params["humanActionable"] is False
    assert entry.resolved is True
    assert entry.approved is False
    assert "failed closed" in str(entry.params["reviewRationale"])


@pytest.mark.asyncio
async def test_auto_review_network_converted_to_human_stays_pending(
    tmp_path: Path,
) -> None:
    reset_approval_queue()
    runtime = SimpleNamespace(
        workspace=tmp_path,
        settings=SandboxSettings(approvals_reviewer="auto_review"),
    )
    request = SandboxRequest(
        argv=("http_request", "GET", "https://legacy.test/path"),
        cwd=tmp_path,
        action_kind="network.http",
        policy=_proxy_policy(),
        session_id="network-legacy",
        run_mode="standard",
    )

    async def _convert_to_human(payload: dict[str, object]) -> None:
        queue = get_approval_queue()
        approval_id = str(payload["approval_id"])
        entry = queue.get(approval_id)
        params = dict(entry.params)
        params.update({"reviewer": "user", "humanActionable": True})
        queue.update_params(approval_id, params)

    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.CLI,
        workspace_dir=str(tmp_path),
        session_key="network-legacy",
        run_mode="standard",
        sandbox_run_context=RunContext(run_mode=RunMode.SAFE),
        on_sandbox_auto_review=_convert_to_human,
    )
    service = NetworkApprovalService(
        context=ctx.sandbox_run_context,
        request=request,
        runtime=runtime,
        approval_timeout_seconds=0.01,
    )
    params = build_network_approval_params(
        NetworkDecision(
            status="ask",
            normalized_host="legacy.test",
            reason="unknown_domain",
            source=None,
        ),
        session_key="network-legacy",
        workspace=str(tmp_path),
        fingerprint=service.fingerprint,
        reviewer="auto_review",
    )
    assert params is not None
    payload = request_sandbox_approval(params, message="Review the exact network request.")
    token = current_tool_context.set(ctx)
    try:
        await service._run_auto_review(payload, str(payload["approval_id"]))
        entry = get_approval_queue().get(str(payload["approval_id"]))
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    assert entry.resolved is False
    assert entry.params["reviewer"] == "user"
    assert entry.params["humanActionable"] is True


async def test_subprocess_preflight_leaves_explicit_url_approval_to_proxy_runtime(
    tmp_path: Path,
) -> None:
    reset_approval_queue()
    request = SandboxRequest(
        argv=("exec_command", "curl", "https://unknown.test/path"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_proxy_policy(),
        session_id="s1",
        run_mode="standard",
    )
    runtime = SimpleNamespace(
        backend=SimpleNamespace(name="noop"),
        workspace=tmp_path,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="standard",
            sandbox_run_context=RunContext(
                run_mode=RunMode.SAFE,
                workspace=str(tmp_path),
            ),
        )
    )
    try:
        result = await integration_mod.preflight_subprocess_managed_network(
            request,
            runtime,
        )
    finally:
        current_tool_context.reset(token)

    assert result is None
    assert get_approval_queue().list_pending("exec") == []
