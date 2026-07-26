from __future__ import annotations

from pathlib import Path

import pytest

from opensquilla.sandbox.escalation import request_sandbox_approval
from opensquilla.sandbox.governance import (
    ALLOW,
    ApprovalGate,
    gate_execution,
    on_successful_exec,
)
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)
from opensquilla.tools.types import ToolContext, current_tool_context


class _RecordingQueue:
    """Fake approval queue that records whether an approval was enqueued."""

    def __init__(self) -> None:
        self.requested = False

    def request(self, namespace: str = "exec", params: dict | None = None) -> str:
        self.requested = True
        return "approval-1"

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        raise AssertionError("approval should not be awaited in this test")

    def resolve(self, approval_id: str, approved: bool) -> None:  # pragma: no cover
        raise AssertionError("resolve should not be called in this test")


def _policy(workspace: Path) -> SandboxPolicy:
    return SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(MountSpec(host_path=workspace, sandbox_path=Path("/workspace"), mode="rw"),),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=5.0),
        env_allowlist=("PATH",),
        require_approval=True,
    )


@pytest.mark.asyncio
async def test_gate_enqueues_when_approval_required(tmp_path: Path) -> None:
    # Every approval-requiring action enqueues a fresh approval and allows only
    # after a human approves — there is no intent-level suppression ("Allow
    # always" was a removed no-op).
    request = SandboxRequest(
        argv=("shell.exec", f"rm {tmp_path / 'x'}"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_policy(tmp_path),
    )

    class _ResolvingQueue(_RecordingQueue):
        async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
            return True

    queue = _ResolvingQueue()
    gate = ApprovalGate(queue)

    decision = await gate.gate(request, request.policy, session_id="s1")

    assert decision is ALLOW
    assert queue.requested is True


@pytest.mark.asyncio
async def test_gate_full_host_does_not_enqueue_required_approval(tmp_path: Path) -> None:
    request = SandboxRequest(
        argv=("shell.exec", f"rm {tmp_path / 'x'}"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_policy(tmp_path),
    )
    queue = _RecordingQueue()
    gate = ApprovalGate(queue)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            run_mode="full",
            elevated="full",
            session_key="s-full",
        )
    )
    try:
        decision = await gate.gate(request, request.policy, session_id="s-full")
    finally:
        current_tool_context.reset(token)

    assert decision is ALLOW
    assert queue.requested is False


@pytest.mark.asyncio
async def test_full_host_skips_ledger_and_success_cache(tmp_path: Path) -> None:
    request = SandboxRequest(
        argv=("shell.exec", "echo host"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_policy(tmp_path),
    )

    class _FailLedger:
        async def is_paused(self, session_id: str) -> bool:
            raise AssertionError("Full Host must not access the denial ledger")

    class _FailCache:
        async def record_success(self, *args: object) -> None:
            raise AssertionError("Full Host must not access the success cache")

    token = current_tool_context.set(
        ToolContext(is_owner=True, run_mode="full", session_key="s-full")
    )
    try:
        decision = await gate_execution(
            request,
            request.policy,
            session_id="s-full",
            ledger=_FailLedger(),  # type: ignore[arg-type]
            approval_gate=ApprovalGate(_RecordingQueue()),
        )
        fingerprint = await on_successful_exec(
            request,
            "output",
            session_id="s-full",
            cache=_FailCache(),  # type: ignore[arg-type]
        )
    finally:
        current_tool_context.reset(token)

    assert decision is ALLOW
    assert fingerprint == ""


def test_full_host_sandbox_approval_helper_returns_without_validation_or_queue() -> None:
    token = current_tool_context.set(
        ToolContext(is_owner=True, run_mode="full", session_key="s-full")
    )
    try:
        result = request_sandbox_approval(
            None,  # type: ignore[arg-type]
            message="must not be used",
        )
    finally:
        current_tool_context.reset(token)

    assert result is None
