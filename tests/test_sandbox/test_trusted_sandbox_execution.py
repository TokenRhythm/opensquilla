from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.mark.asyncio
async def test_trusted_sandbox_does_not_mark_shell_host_elevated(monkeypatch) -> None:
    from opensquilla.tools.builtin import shell

    calls: list[tuple[str, object]] = []

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    async def _fake_gate_action(**kwargs):
        calls.append(("gate", kwargs))
        policy = SimpleNamespace()
        request = SimpleNamespace(cwd="/tmp", action_kind="shell.exec", policy=policy)
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        calls.append(("backend", request))
        return SimpleNamespace(
            returncode=0,
            stdout="sandboxed\n",
            stderr="",
            backend_notes=(),
        )

    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(is_owner=True, caller_kind=CallerKind.WEB, session_key="s1", run_mode="trusted")
    )
    try:
        result = await shell.exec_command("echo hi")
    finally:
        current_tool_context.reset(token)

    assert "sandboxed" in result
    assert [name for name, _ in calls] == ["gate", "backend"]


@pytest.mark.asyncio
async def test_ordinary_approval_result_does_not_carry_elevated_mode(monkeypatch) -> None:
    from opensquilla.application.approval_queue import ApprovalQueue

    queue = ApprovalQueue(db_path=":memory:")
    try:
        approval_id = queue.request(
            namespace="exec",
            params={"sessionKey": "s1", "command": "rm x"},
        )
        queue.resolve(approval_id, True)
        status = queue.status(approval_id)
        assert "elevatedMode" not in status["params"]
    finally:
        queue.close()


@pytest.mark.asyncio
async def test_warnlist_shell_uses_sandbox_gate_without_exec_approval(monkeypatch) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.tools.builtin import shell

    calls: list[tuple[str, object]] = []
    reset_approval_queue()

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    async def _fake_gate_action(**kwargs):
        calls.append(("gate", kwargs))
        policy = SimpleNamespace()
        request = SimpleNamespace(cwd="/tmp", action_kind="shell.exec", policy=policy)
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        calls.append(("backend", request))
        return SimpleNamespace(
            returncode=0,
            stdout="sandboxed after approval\n",
            stderr="",
            backend_notes=(),
        )

    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(
            allowed=True,
            needs_approval=True,
            reason="command requires approval",
        ),
    )

    token = current_tool_context.set(
        ToolContext(is_owner=True, caller_kind=CallerKind.CLI, session_key="s1")
    )
    try:
        result = await shell.exec_command("rm x")
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    assert "sandboxed after approval" in result
    assert get_approval_queue().list_pending("exec") == []
    assert [name for name, _ in calls] == ["gate", "backend"]
    hints = calls[0][1]["hints"]  # type: ignore[index]
    assert hints.high_impact is True


@pytest.mark.asyncio
async def test_trusted_workspace_shell_cleanup_stays_out_of_locked_approval(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.tools.builtin import shell

    calls: list[tuple[str, object]] = []
    reset_approval_queue()

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    async def _fake_gate_action(**kwargs):
        calls.append(("gate", kwargs))
        policy = SimpleNamespace()
        request = SimpleNamespace(cwd=tmp_path, action_kind="shell.exec", policy=policy)
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        calls.append(("backend", request))
        return SimpleNamespace(
            returncode=0,
            stdout="shell-workspace-ok\n",
            stderr="",
            backend_notes=(),
        )

    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(
            allowed=True,
            needs_approval=True,
            reason="command requires approval",
        ),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.WEB,
            session_key="s1",
            run_mode="trusted",
            workspace_dir=str(tmp_path),
        )
    )
    try:
        result = await shell.exec_command(
            'printf "%s\\n" shell-workspace-ok > sandbox_probe_shell.txt '
            "&& cat sandbox_probe_shell.txt && rm sandbox_probe_shell.txt",
            workdir=str(tmp_path),
        )
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    assert "shell-workspace-ok" in result
    assert get_approval_queue().list_pending("exec") == []
    assert [name for name, _ in calls] == ["gate", "backend"]
    hints = calls[0][1]["hints"]  # type: ignore[index]
    assert hints.high_impact is False


@pytest.mark.asyncio
async def test_trusted_workspace_code_delete_stays_out_of_locked_approval(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
    from opensquilla.tools.builtin import code_exec

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, object]] = []
    reset_approval_queue()

    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        workspace=workspace,
    )

    async def _fake_gate_action(**kwargs):
        calls.append(("gate", kwargs))
        policy = SimpleNamespace()
        request = SimpleNamespace(
            cwd=workspace,
            action_kind="code.exec",
            policy=policy,
            reason="",
            session_id="s1",
            run_mode="trusted",
        )
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        calls.append(("backend", request))
        return SimpleNamespace(
            returncode=0,
            stdout="exists=False\n",
            stderr="",
            backend_notes=(),
            timed_out=False,
        )

    monkeypatch.setattr(code_exec, "get_runtime", lambda: runtime)
    monkeypatch.setattr(code_exec, "gate_action", _fake_gate_action)
    monkeypatch.setattr(code_exec, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        code_exec,
        "_resolve_python_bin",
        lambda *, sandbox_enabled: "/usr/bin/python3",
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.WEB,
            session_key="s1",
            run_mode="trusted",
            workspace_dir=str(workspace),
        )
    )
    try:
        result = await code_exec.execute_code(
            "from pathlib import Path\n"
            "p = Path('rpo-contract-generator.html')\n"
            "if p.exists():\n"
            "    p.unlink()\n"
            "print('exists=' + str(p.exists()))"
        )
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    payload = json.loads(result)
    assert payload["exit_code"] == 0
    assert get_approval_queue().list_pending("exec") == []
    assert [name for name, _ in calls] == ["gate", "backend"]
    hints = calls[0][1]["hints"]  # type: ignore[index]
    assert hints.high_impact is False


async def _capture_trusted_code_exec_high_impact(
    monkeypatch,
    tmp_path,
    code: str,
) -> bool:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import code_exec

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls: list[tuple[str, object]] = []
    reset_approval_queue()

    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        workspace=workspace,
    )

    async def _fake_gate_action(**kwargs):
        calls.append(("gate", kwargs))
        policy = SimpleNamespace()
        request = SimpleNamespace(
            cwd=workspace,
            action_kind="code.exec",
            policy=policy,
            reason="",
            session_id="s1",
            run_mode="trusted",
        )
        return object(), policy, request

    async def _fake_run_under_backend(request, *, runtime=None):
        calls.append(("backend", request))
        return SimpleNamespace(
            returncode=0,
            stdout="ok\n",
            stderr="",
            backend_notes=(),
            timed_out=False,
        )

    monkeypatch.setattr(code_exec, "get_runtime", lambda: runtime)
    monkeypatch.setattr(code_exec, "gate_action", _fake_gate_action)
    monkeypatch.setattr(code_exec, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(
        code_exec,
        "_resolve_python_bin",
        lambda *, sandbox_enabled: "/usr/bin/python3",
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.WEB,
            session_key="s1",
            run_mode="trusted",
            workspace_dir=str(workspace),
        )
    )
    try:
        result = await code_exec.execute_code(code)
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    payload = json.loads(result)
    assert payload["exit_code"] == 0
    assert [name for name, _ in calls] == ["gate", "backend"]
    hints = calls[0][1]["hints"]  # type: ignore[index]
    return bool(hints.high_impact)


@pytest.mark.parametrize(
    "code",
    [
        (
            "from pathlib import Path\n"
            "p = Path('/some/extra-rw-mount/file')\n"
            "def unused():\n"
            "    p = Path('safe.txt')\n"
            "p.unlink()\n"
        ),
        (
            "from pathlib import Path\n"
            "p = Path('/some/extra-rw-mount/file')\n"
            "if cond:\n"
            "    p = Path('safe.txt')\n"
            "p.unlink()\n"
        ),
        (
            "import os\n"
            "fd = os.open('/some/extra-rw-mount', os.O_RDONLY)\n"
            "os.remove('target', dir_fd=fd)\n"
        ),
        (
            "class Path:\n"
            "    def __init__(self, value):\n"
            "        self.value = value\n"
            "    def unlink(self):\n"
            "        pass\n"
            "p = Path('safe.txt')\n"
            "p.unlink()\n"
        ),
    ],
)
@pytest.mark.asyncio
async def test_trusted_code_delete_keeps_high_impact_when_target_proof_is_unsound(
    monkeypatch,
    tmp_path,
    code: str,
) -> None:
    assert await _capture_trusted_code_exec_high_impact(monkeypatch, tmp_path, code)


@pytest.mark.asyncio
async def test_trusted_mode_allows_without_hidden_approval_wait(
    tmp_path,
) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.integration import configure_runtime, gate_action, reset_runtime
    from opensquilla.sandbox.policy import LevelHints
    from opensquilla.sandbox.types import ALLOW

    class _Queue:
        requests: list[dict | None]

        def __init__(self) -> None:
            self.requests = []

        def request(self, namespace: str = "exec", params: dict | None = None) -> str:
            self.requests.append(params)
            return "approval:hidden"

        async def wait(self, approval_id: str, timeout: float | None = None) -> bool:
            raise AssertionError("trusted mode must not wait on hidden approval")

        def resolve(self, approval_id: str, approved: bool) -> None:
            return None

    queue = _Queue()
    configure_runtime(
        SandboxSettings(run_mode="standard", backend="noop", allow_legacy_mode=True),
        approval_queue=queue,
        workspace=tmp_path,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.WEB,
            session_key="s1",
            run_mode="trusted",
            workspace_dir=str(tmp_path),
        )
    )
    try:
        decision, policy, _request = await gate_action(
            action_kind="code.exec",
            argv=("python", "-c", "import shutil; shutil.rmtree('build')"),
            cwd=tmp_path,
            hints=LevelHints(high_impact=True),
        )
    finally:
        current_tool_context.reset(token)
        reset_runtime()

    assert policy.require_approval is False
    assert decision is ALLOW
    assert _request.run_mode == "trusted"
    assert queue.requests == []


@pytest.mark.asyncio
async def test_backend_denial_suspends_before_any_host_retry(monkeypatch) -> None:
    from opensquilla.sandbox.elevation import ElevationGateResult
    from opensquilla.tools.builtin import shell

    calls: list[str] = []

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    class _Proc:
        pid = 999999
        returncode = 0

        async def wait(self):
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    async def _fake_gate_action(**kwargs):
        calls.append("gate")
        policy = SimpleNamespace()
        request = SimpleNamespace(
            cwd="/tmp",
            action_kind="shell.exec",
            policy=policy,
            reason="",
        )
        return object(), policy, request

    backend_results = [
        SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="denied",
            backend_notes=("exec denied",),
        ),
        SimpleNamespace(
            returncode=0,
            stdout="sandboxed again\n",
            stderr="",
            backend_notes=(),
        ),
    ]

    async def _fake_run_under_backend(request, *, runtime=None):
        calls.append("backend")
        return backend_results.pop(0)

    async def _fake_escalate_backend_denial(*args, **kwargs):
        calls.append("escalate")
        return ElevationGateResult(
            requested=True,
            allowed=False,
            status="approval_required",
            approval_id="approval:test",
        )

    async def _fake_create_subprocess_shell(*args, **kwargs):
        calls.append("host")
        assert kwargs["stdout"] != shell.asyncio.subprocess.PIPE
        assert kwargs["stderr"] == shell.asyncio.subprocess.STDOUT
        if shell.os.name == "posix":
            assert kwargs["start_new_session"] is True
        return _Proc()

    class _FakeTemporaryFile:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def flush(self) -> None:
            return None

        def seek(self, offset: int) -> None:
            return None

        def read(self) -> bytes:
            return b"host fallback should not run\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", _fake_run_under_backend)
    monkeypatch.setattr(shell, "escalate_backend_denial", _fake_escalate_backend_denial)
    monkeypatch.setattr(shell.asyncio, "create_subprocess_shell", _fake_create_subprocess_shell)
    monkeypatch.setattr(shell.tempfile, "TemporaryFile", lambda: _FakeTemporaryFile())
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(is_owner=True, caller_kind=CallerKind.CLI, session_key="s1")
    )
    try:
        result = await shell.exec_command("echo hi")
    finally:
        current_tool_context.reset(token)

    assert json.loads(result)["status"] == "approval_required"
    assert calls == ["gate", "backend", "escalate"]


@pytest.mark.asyncio
async def test_full_host_access_code_exec_resolves_host_python(monkeypatch, tmp_path) -> None:
    from opensquilla.tools.builtin import code_exec

    resolve_calls: list[bool] = []
    child_env: dict[str, str] = {}

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)
        workspace = tmp_path

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"host python\n", b""

    def _fake_resolve_python_bin(*, sandbox_enabled: bool) -> str:
        resolve_calls.append(sandbox_enabled)
        return "/host/python"

    async def _fake_create_subprocess_exec(*args, **kwargs):
        assert args[:2] == ("/host/python", "-c")
        child_env.update(kwargs["env"])
        return _Proc()

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("WINDIR", r"C:\Windows")
    monkeypatch.setenv("ComSpec", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setenv("TEMP", str(tmp_path / "temp"))
    monkeypatch.setenv("TMP", str(tmp_path / "temp"))
    monkeypatch.setattr(code_exec, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(code_exec, "_resolve_python_bin", _fake_resolve_python_bin)
    monkeypatch.setattr(code_exec.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    def fail_safety_preflight(*args, **kwargs):
        pytest.fail("Full Host Access code execution must skip safety preflight")

    monkeypatch.setattr(code_exec, "_check_code_sensitive_access", fail_safety_preflight)
    monkeypatch.setattr(code_exec, "_check_code_destructive", fail_safety_preflight)
    monkeypatch.setattr(code_exec, "_code_needs_network", fail_safety_preflight)
    monkeypatch.setattr(code_exec, "snapshot_current_workspace_mutations", lambda: {})
    monkeypatch.setattr(code_exec, "gate_action", fail_safety_preflight)

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            session_key="s1",
            run_mode="full",
            workspace_dir=str(tmp_path),
        )
    )
    try:
        result = await code_exec.execute_code("print('hi')")
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["exit_code"] == 0
    assert payload["stdout"] == "host python\n"
    assert resolve_calls == [False]
    folded_env = {key.upper(): value for key, value in child_env.items()}
    assert folded_env["SYSTEMROOT"] == r"C:\Windows"
    assert folded_env["WINDIR"] == r"C:\Windows"
    assert folded_env["COMSPEC"] == r"C:\Windows\System32\cmd.exe"
    assert folded_env["TEMP"] == str(tmp_path / "temp")
    assert folded_env["TMP"] == str(tmp_path / "temp")


@pytest.mark.asyncio
async def test_full_host_access_shell_uses_host_and_skips_sandbox_gates(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.tools.builtin import shell

    calls: list[str] = []

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    async def _fail_gate_action(**kwargs):
        pytest.fail("Full Host Access shell should not enter sandbox gate_action")

    async def _fake_host_shell_command(*args, **kwargs):
        calls.append("host")
        return "exit_code=1\nhead: cannot open '/etc/shadow' for reading: Permission denied\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell, "gate_action", _fail_gate_action)
    monkeypatch.setattr(shell, "_run_host_shell_command", _fake_host_shell_command)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            session_key="s1",
            run_mode="full",
            workspace_dir=str(tmp_path),
        )
    )
    try:
        result = await shell.exec_command("head -n 1 /etc/shadow 2>&1; echo exit=$?")
    finally:
        current_tool_context.reset(token)

    assert calls == ["host"]
    assert "Permission denied" in result
    assert "sensitive_path" not in result


@pytest.mark.asyncio
async def test_full_host_access_shell_strips_managed_proxy_environment(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.tools.builtin import shell

    seen_env: dict[str, str] = {}

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    async def _fake_host_shell_command(*args, **kwargs):
        seen_env.update(kwargs["env"])
        return "exit_code=0\n"

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:48123")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:48123")
    monkeypatch.setenv("OPENSQUILLA_SANDBOX_NETWORK", "proxy_allowlist")
    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell, "_run_host_shell_command", _fake_host_shell_command)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            session_key="s1",
            run_mode="full",
            workspace_dir=str(tmp_path),
        )
    )
    try:
        await shell.exec_command("env")
    finally:
        current_tool_context.reset(token)

    assert "OPENSQUILLA_SANDBOX_NETWORK" not in seen_env
    assert "HTTP_PROXY" not in seen_env
    assert "HTTPS_PROXY" not in seen_env


@pytest.mark.asyncio
async def test_full_host_access_background_strips_managed_proxy_environment(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.tools.builtin import shell

    seen_env: dict[str, str] = {}

    class _Runtime:
        effective = SimpleNamespace(sandbox_enabled=True)

    class _FakeProcess:
        stdout = None
        stdin = None
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def _fake_create_subprocess_shell(*args, **kwargs):
        seen_env.update(kwargs["env"])
        return _FakeProcess()

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:48123")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:48123")
    monkeypatch.setenv("OPENSQUILLA_SANDBOX_NETWORK", "proxy_allowlist")
    monkeypatch.setattr(shell, "get_runtime", lambda: _Runtime())
    monkeypatch.setattr(shell.asyncio, "create_subprocess_shell", _fake_create_subprocess_shell)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            session_key="s1",
            run_mode="full",
            workspace_dir=str(tmp_path),
        )
    )
    try:
        result = await shell.background_process("env")
        session_id = result.splitlines()[0].split("=", 1)[1]
        session = shell._bg_sessions[session_id]
        assert session.collector_task is not None
        await session.collector_task
    finally:
        current_tool_context.reset(token)

    assert "OPENSQUILLA_SANDBOX_NETWORK" not in seen_env
    assert "HTTP_PROXY" not in seen_env
    assert "HTTPS_PROXY" not in seen_env
