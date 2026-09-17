from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.types import (
    ALLOW,
    DenialReason,
    DenialResult,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
    SecurityLevel,
    SuggestedNextStep,
)
from opensquilla.tools.builtin import code_exec
from opensquilla.tools.types import ToolContext, current_tool_context


def test_frozen_runtime_keeps_an_explicit_external_interpreter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    external = str(tmp_path / "python")

    assert code_exec._python_execution_argv(external, "print(1)") == (
        external, "-c", "print(1)",
    )


@pytest.mark.parametrize("deny", [False, True], ids=["allowed", "denied"])
@pytest.mark.asyncio
async def test_frozen_code_gate_and_backend_receive_the_same_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, deny: bool,
) -> None:
    code = "print('synthetic frozen execution')"
    expected = (sys.executable, "--internal-child", "python-code", code)
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD, network=NetworkMode.NONE, mounts=(),
        workspace_rw=True, tmp_writable=True, limits=ResourceLimits(),
        env_allowlist=(), require_approval=False,
    )
    gated: list[tuple[str, ...]] = []
    executed: list[SandboxRequest] = []

    async def gate(**kwargs: object) -> tuple[object, SandboxPolicy, SandboxRequest]:
        argv = tuple(kwargs["argv"])
        gated.append(argv)
        request = SandboxRequest(argv=argv, cwd=tmp_path, policy=policy, action_kind="code.exec")
        decision = DenialResult(
            reason=DenialReason.POLICY_DENIED,
            suggested_next_step=SuggestedNextStep.REPLAN,
            level=SecurityLevel.STANDARD,
            action_fingerprint="synthetic-code-denial",
            message="Synthetic denial",
        ) if deny else ALLOW
        return decision, policy, request

    async def backend(request: SandboxRequest, **_kwargs: object) -> SandboxResult:
        executed.append(request)
        return SandboxResult(
            returncode=0, stdout="synthetic frozen execution\n", stderr="",
            wall_time_s=0.01, backend_used="noop",
        )

    configure_runtime(
        SandboxSettings(backend="noop", run_mode="safe", network_default="none"),
        workspace=tmp_path,
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(code_exec, "gate_action", gate)
    monkeypatch.setattr(code_exec, "consume_backend_denial_retry", lambda *_a, **_k: None)
    monkeypatch.setattr(code_exec, "_run_backend_with_managed_network_if_needed", backend)
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path), run_mode="safe"))
    try:
        result = json.loads(await code_exec.execute_code(code))
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert gated == [expected]
    if deny:
        assert executed == []
        assert result["reason"] == "policy_denied"
    else:
        assert len(executed) == 1
        assert executed[0].argv == expected
        assert executed[0].policy.network is NetworkMode.NONE
        assert result["exit_code"] == 0


@pytest.mark.asyncio
async def test_frozen_full_code_uses_owned_process_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    class LaunchObservedError(Exception):
        pass

    launched: list[tuple[str, ...]] = []

    async def launch(*argv: str, **_kwargs: object) -> None:
        launched.append(argv)
        raise LaunchObservedError

    configure_runtime(SandboxSettings(sandbox=False, security_grading=False), workspace=tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(code_exec, "create_owned_subprocess_exec", launch)
    token = current_tool_context.set(ToolContext(workspace_dir=str(tmp_path), run_mode="full"))
    try:
        # The product converts launch errors into its normal tool error result.
        result = json.loads(await code_exec.execute_code("print('owned child')"))
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert launched == [(sys.executable, "--internal-child", "python-code", "print('owned child')")]
    assert result["exit_code"] != 0
