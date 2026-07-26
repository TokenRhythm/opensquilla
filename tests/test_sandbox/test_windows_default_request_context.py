from __future__ import annotations

from pathlib import Path

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import build_request, configure_runtime, reset_runtime
from opensquilla.sandbox.policy import build_policy
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import NetworkMode, SecurityLevel
from opensquilla.tools.run_mode import full_host_access_for_context
from opensquilla.tools.types import ToolContext, current_tool_context


class _FakeApprovalQueue:
    def request(self, namespace: str = "exec.approval", params: dict | None = None) -> str:
        return "approval:test"

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        return False

    def resolve(self, approval_id: str, approved: bool) -> None:
        return None


def teardown_function() -> None:
    reset_runtime()


def test_build_request_uses_runtime_run_mode_when_no_context(tmp_path: Path) -> None:
    settings = SandboxSettings(run_mode="trusted", backend="noop")
    runtime = configure_runtime(
        settings,
        approval_queue=_FakeApprovalQueue(),
        workspace=tmp_path,
    )
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        runtime.settings,
        trusted=True,
    )

    request = build_request(
        action_kind="shell.exec",
        argv=("cmd", "/c", "echo ok"),
        cwd=tmp_path,
        policy=policy,
    )

    assert request.run_mode == RunMode.TRUSTED.value


def test_hybrid_runtime_uses_full_without_context_and_standard_with_context(
    tmp_path: Path,
) -> None:
    try:
        runtime = configure_runtime(
            SandboxSettings(
                run_mode="standard",
                backend="noop",
                allow_legacy_mode=True,
            ),
            default_run_mode=RunMode.FULL,
            workspace=tmp_path,
        )
        assert runtime.default_run_mode is RunMode.FULL
        policy = build_policy(
            SecurityLevel.STANDARD,
            "shell.exec",
            tmp_path,
            runtime.settings,
            trusted=True,
        )
        request = build_request(
            action_kind="shell.exec",
            argv=("cmd", "/c", "echo ok"),
            cwd=tmp_path,
            policy=policy,
        )
        assert request.run_mode == RunMode.FULL.value
        assert full_host_access_for_context(None) is True
        standard_ctx = ToolContext(run_mode="standard")
        assert full_host_access_for_context(standard_ctx) is False
        token = current_tool_context.set(standard_ctx)
        try:
            standard_request = build_request(
                action_kind="shell.exec",
                argv=("cmd", "/c", "echo standard"),
                cwd=tmp_path,
                policy=policy,
            )
        finally:
            current_tool_context.reset(token)
        assert standard_request.run_mode == RunMode.STANDARD.value
    finally:
        reset_runtime()


def test_managed_network_env_preserves_run_mode(tmp_path: Path) -> None:
    from opensquilla.sandbox.integration import request_with_managed_network_proxy_env
    from opensquilla.sandbox.types import (
        NetworkProxySpec,
        ResourceLimits,
        SandboxPolicy,
        SandboxRequest,
    )

    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.PROXY_ALLOWLIST,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=("HTTP_PROXY",),
        require_approval=False,
        network_proxy=NetworkProxySpec(host="127.0.0.1", port=18080),
    )
    request = SandboxRequest(
        argv=("python", "-c", "print('ok')"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        run_mode=RunMode.STANDARD.value,
    )

    updated = request_with_managed_network_proxy_env(request, backend_name="windows_default")

    assert updated.run_mode == RunMode.STANDARD.value
