from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.elevation import ElevationGateResult
from opensquilla.sandbox.integration import (
    configure_runtime,
    consume_backend_denial_retry,
    escalate_backend_denial,
    get_runtime,
    reset_runtime,
)
from opensquilla.sandbox.run_context import RunContext
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import (
    DenialReason,
    DenialResult,
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRequest,
    SandboxResult,
    SecurityLevel,
)
from opensquilla.tools.types import ToolContext, current_tool_context


def _policy(workspace: Path) -> SandboxPolicy:
    return SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(MountSpec(host_path=workspace, sandbox_path=Path("/workspace"), mode="rw"),),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(),
        env_allowlist=("PATH",),
        require_approval=False,
    )


def _request(workspace: Path, policy: SandboxPolicy) -> SandboxRequest:
    return SandboxRequest(
        argv=("sh", "-c", "echo hi"),
        cwd=workspace,
        action_kind="shell.exec",
        policy=policy,
    )


def _result_with_notes(notes: tuple[str, ...]) -> SandboxResult:
    return SandboxResult(
        returncode=1,
        stdout="",
        stderr="sandbox-exec: execvp() of '/opt/brew/bin/uv' failed: Operation not permitted",
        wall_time_s=0.1,
        backend_used="seatbelt",
        backend_notes=notes,
    )


class _ApproveQueue:
    def __init__(self, approve: bool) -> None:
        self._approve = approve
        self.last_params: dict | None = None
        self._entry: SimpleNamespace | None = None

    def request(self, namespace: str = "exec", params: dict | None = None) -> str:
        self.last_params = params
        self._entry = SimpleNamespace(
            namespace=namespace,
            params=dict(params or {}),
            resolved=False,
            approved=False,
            consumed=False,
        )
        return "approval:test"

    def list_pending(self, namespace: str = "exec") -> list[dict]:
        if self._entry is None or self._entry.resolved:
            return []
        return [{"id": "approval:test", "params": self._entry.params}]

    def get(self, approval_id: str) -> SimpleNamespace:
        if self._entry is None or approval_id != "approval:test":
            raise KeyError(approval_id)
        return self._entry

    def consume(self, approval_id: str) -> None:
        self.get(approval_id).consumed = True

    async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
        return self._approve

    def resolve(self, approval_id: str, approved: bool) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset() -> None:
    yield
    reset_runtime()


@pytest.mark.asyncio
async def test_sandbox_backend_denial_requests_exact_broader_retry(tmp_path: Path) -> None:
    queue = _ApproveQueue(approve=True)
    configure_runtime(
        SandboxSettings(sandbox=True, backend="noop", security_grading=False),
        approval_queue=queue,
        workspace=tmp_path,
    )
    policy = _policy(tmp_path)
    request = _request(tmp_path, policy)
    result = _result_with_notes(("execve.denied: sandbox blocked execve of /opt/brew/bin/uv",))

    decision = await escalate_backend_denial(result, request, policy)

    assert isinstance(decision, ElevationGateResult)
    assert decision.status == "approval_required"
    assert queue.last_params is not None
    assert queue.last_params["backendRetry"] is True
    assert queue.last_params["sandboxOriginalOutput"].endswith("Operation not permitted")


@pytest.mark.asyncio
async def test_full_host_access_does_not_route_backend_failure_to_host_once(
    tmp_path: Path,
) -> None:
    queue = _ApproveQueue(approve=True)
    configure_runtime(
        SandboxSettings(
            sandbox=False,
            backend="noop",
            security_grading=False,
            run_mode="full",
        ),
        approval_queue=queue,
        workspace=tmp_path,
    )
    policy = _policy(tmp_path)
    request = _request(tmp_path, policy)
    result = _result_with_notes(("execve.denied: sandbox blocked execve of /bin/sh",))

    decision = await escalate_backend_denial(result, request, policy)

    assert isinstance(decision, DenialResult)
    assert queue.last_params is None


@pytest.mark.asyncio
async def test_current_run_context_full_host_access_skips_backend_host_once(
    tmp_path: Path,
) -> None:
    queue = _ApproveQueue(approve=True)
    configure_runtime(
        SandboxSettings(
            sandbox=True,
            backend="noop",
            security_grading=True,
            run_mode="standard",
        ),
        approval_queue=queue,
        workspace=tmp_path,
    )
    token = current_tool_context.set(
        ToolContext(
            workspace_dir=str(tmp_path),
            sandbox_run_context=RunContext(run_mode=RunMode.FULL),
        )
    )
    try:
        policy = _policy(tmp_path)
        request = _request(tmp_path, policy)
        result = _result_with_notes(("execve.denied: sandbox blocked execve of /bin/sh",))

        decision = await escalate_backend_denial(result, request, policy)
    finally:
        current_tool_context.reset(token)

    assert isinstance(decision, DenialResult)
    assert queue.last_params is None


@pytest.mark.asyncio
async def test_current_tool_context_full_host_access_skips_backend_host_once(
    tmp_path: Path,
) -> None:
    queue = _ApproveQueue(approve=True)
    configure_runtime(
        SandboxSettings(
            sandbox=True,
            backend="noop",
            security_grading=True,
            run_mode="standard",
        ),
        approval_queue=queue,
        workspace=tmp_path,
    )
    token = current_tool_context.set(
        ToolContext(
            workspace_dir=str(tmp_path),
            run_mode="full",
        )
    )
    try:
        policy = _policy(tmp_path)
        request = _request(tmp_path, policy)
        result = _result_with_notes(("execve.denied: sandbox blocked execve of /bin/sh",))

        decision = await escalate_backend_denial(result, request, policy)
    finally:
        current_tool_context.reset(token)

    assert isinstance(decision, DenialResult)
    assert queue.last_params is None


@pytest.mark.asyncio
async def test_escalate_returns_pending_retry_review(tmp_path: Path) -> None:
    configure_runtime(
        SandboxSettings(sandbox=True, backend="noop", security_grading=False),
        approval_queue=_ApproveQueue(approve=True),
        workspace=tmp_path,
    )
    policy = _policy(tmp_path)
    result = _result_with_notes(("execve.denied: sandbox blocked execve of /bin/sh",))

    decision = await escalate_backend_denial(result, _request(tmp_path, policy), policy)

    assert isinstance(decision, ElevationGateResult)
    assert decision.status == "approval_required"


@pytest.mark.asyncio
async def test_escalate_never_waits_inside_tool_handler(tmp_path: Path) -> None:
    configure_runtime(
        SandboxSettings(sandbox=True, backend="noop", security_grading=False),
        approval_queue=_ApproveQueue(approve=False),
        workspace=tmp_path,
    )
    policy = _policy(tmp_path)
    result = _result_with_notes(("filesystem.read: sandbox blocked access to /etc/ssl/cert.pem",))

    decision = await escalate_backend_denial(result, _request(tmp_path, policy), policy)

    assert isinstance(decision, ElevationGateResult)
    assert decision.status == "approval_required"


@pytest.mark.asyncio
async def test_backend_retry_request_reuses_one_pending_review(
    tmp_path: Path,
) -> None:
    configure_runtime(
        SandboxSettings(
            sandbox=True,
            backend="noop",
            security_grading=False,
            denial_threshold=3,
        ),
        approval_queue=_ApproveQueue(approve=False),
        workspace=tmp_path,
    )
    policy = _policy(tmp_path)
    result = _result_with_notes(("tmp.denied: sandbox denied a tmp-directory operation",))

    for _ in range(3):
        decision = await escalate_backend_denial(result, _request(tmp_path, policy), policy)
        assert isinstance(decision, ElevationGateResult)

    runtime = get_runtime()
    assert runtime is not None
    assert await runtime.ledger.count_session("default") == 0
    assert await runtime.ledger.threshold_reached("default") is False


@pytest.mark.asyncio
async def test_escalate_no_runtime_returns_seatbelt_denied(tmp_path: Path) -> None:
    reset_runtime()
    policy = _policy(tmp_path)
    result = _result_with_notes(("execve.denied: sandbox blocked execve of /bin/uv",))

    decision = await escalate_backend_denial(result, _request(tmp_path, policy), policy)

    assert isinstance(decision, DenialResult)
    assert decision.reason == DenialReason.SEATBELT_DENIED
    assert decision.retryable is False


@pytest.mark.asyncio
async def test_approved_backend_retry_consumes_same_request_once(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    try:
        runtime = configure_runtime(
            SandboxSettings(sandbox=True, backend="noop", security_grading=False),
            approval_queue=queue,
            workspace=tmp_path,
        )
        policy = _policy(tmp_path)
        request = _request(tmp_path, policy)
        pending = await escalate_backend_denial(
            _result_with_notes(("filesystem.write.denied: /outside",)),
            request,
            policy,
            runtime=runtime,
        )
        assert isinstance(pending, ElevationGateResult)
        queue.resolve(pending.approval_id or "", True)

        approved = consume_backend_denial_retry(
            pending.approval_id,
            request,
            policy,
            runtime=runtime,
        )

        assert approved is not None and approved.allowed is True
        assert queue.get(pending.approval_id or "").consumed is True
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_backend_retry_rejects_changed_request_without_consuming(tmp_path: Path) -> None:
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    try:
        runtime = configure_runtime(
            SandboxSettings(sandbox=True, backend="noop", security_grading=False),
            approval_queue=queue,
            workspace=tmp_path,
        )
        policy = _policy(tmp_path)
        request = _request(tmp_path, policy)
        pending = await escalate_backend_denial(
            _result_with_notes(("filesystem.write.denied: /outside",)),
            request,
            policy,
            runtime=runtime,
        )
        assert isinstance(pending, ElevationGateResult)
        queue.resolve(pending.approval_id or "", True)
        changed = SandboxRequest(
            argv=("sh", "-c", "rm -rf /"),
            cwd=request.cwd,
            action_kind=request.action_kind,
            policy=policy,
        )

        rejected = consume_backend_denial_retry(
            pending.approval_id,
            changed,
            policy,
            runtime=runtime,
        )

        assert rejected is not None and rejected.status == "approval_action_mismatch"
        assert queue.get(pending.approval_id or "").consumed is False
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_shell_backend_denial_resumes_same_command_as_one_host_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))
    backend_calls = 0
    host_calls = 0

    async def fake_backend(*args, **kwargs) -> SandboxResult:
        nonlocal backend_calls
        backend_calls += 1
        return SandboxResult(
            returncode=1,
            stdout="sandbox-prefix\n",
            stderr="Read-only file system\n",
            wall_time_s=0.1,
            backend_used="bubblewrap",
        )

    async def fake_host(*args, **kwargs) -> str:
        nonlocal host_calls
        host_calls += 1
        return "exit_code=0\nhost-result\n"

    monkeypatch.setattr(shell, "_run_backend_with_managed_network", fake_backend)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    try:
        configure_runtime(
            SandboxSettings(sandbox=True, backend="noop", security_grading=False),
            approval_queue=queue,
            workspace=tmp_path,
        )
        token = current_tool_context.set(
            ToolContext(
                is_owner=True,
                workspace_dir=str(tmp_path),
                session_key="retry-session",
                run_mode="standard",
            )
        )
        try:
            first = await shell.exec_command("printf hello", workdir=str(tmp_path))
            pending = json.loads(first)
            queue.resolve(pending["approval_id"], True)
            second = await shell.exec_command(
                "printf hello",
                workdir=str(tmp_path),
                approval_id=pending["approval_id"],
            )
        finally:
            current_tool_context.reset(token)

        assert pending["status"] == "approval_required"
        assert second == "exit_code=0\nhost-result\n"
        assert backend_calls == 1
        assert host_calls == 1
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_generic_shell_nonzero_does_not_request_or_run_host_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.tools.builtin import shell

    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.sqlite"))

    async def fake_backend(*args, **kwargs) -> SandboxResult:
        return SandboxResult(
            returncode=1,
            stdout="",
            stderr="tests failed: assertion mismatch\n",
            wall_time_s=0.1,
            backend_used="bubblewrap",
        )

    async def fail_host(*args, **kwargs) -> str:
        raise AssertionError("generic failure must not run on the host")

    monkeypatch.setattr(shell, "_run_backend_with_managed_network", fake_backend)
    monkeypatch.setattr(shell, "_run_host_shell_command", fail_host)
    try:
        configure_runtime(
            SandboxSettings(sandbox=True, backend="noop", security_grading=False),
            approval_queue=queue,
            workspace=tmp_path,
        )
        token = current_tool_context.set(
            ToolContext(is_owner=True, workspace_dir=str(tmp_path), run_mode="standard")
        )
        try:
            result = await shell.exec_command("pytest", workdir=str(tmp_path))
        finally:
            current_tool_context.reset(token)

        assert result.startswith("exit_code=1\n")
        assert queue.list_pending("exec") == []
    finally:
        queue.close()
