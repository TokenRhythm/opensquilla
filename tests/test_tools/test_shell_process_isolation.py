from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import structlog.testing

from opensquilla.sandbox.types import (
    DenialReason,
    DenialResult,
    SandboxResult,
    SecurityLevel,
    SuggestedNextStep,
)
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context


def _python_shell_command(script: str) -> str:
    argv = [sys.executable, "-c", script]
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return " ".join(shlex.quote(part) for part in argv)


class _FakeStdin:
    def __init__(self) -> None:
        self.closed = False
        self.close_waited = False
        self.writes: list[bytes] = []

    def is_closing(self) -> bool:
        return self.closed

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.close_waited = True


@dataclass
class _FakeProcess:
    returncode: int | None = None
    stdin: _FakeStdin | None = None

    def __post_init__(self) -> None:
        if self.stdin is None:
            self.stdin = _FakeStdin()


def _ctx(
    session_key: str,
    *,
    is_owner: bool = False,
    agent_id: str = "agent",
    caller_kind: CallerKind = CallerKind.AGENT,
    task_id: str | None = None,
) -> ToolContext:
    return ToolContext(
        is_owner=is_owner,
        caller_kind=caller_kind,
        session_key=session_key,
        agent_id=agent_id,
        task_id=task_id,
    )


def _process_test_python() -> str:
    if os.path.exists("/usr/bin/python3"):
        return "/usr/bin/python3"
    return sys.executable


@pytest.fixture(autouse=True)
def _reset_bg_sessions():
    previous = dict(shell._bg_sessions)
    shell._bg_sessions.clear()
    yield
    shell._bg_sessions.clear()
    shell._bg_sessions.update(previous)


def _session(
    session_id: str,
    session_key: str | None,
    *,
    agent_id: str | None = "agent",
    done: bool = False,
    command: str | None = None,
    local_urls: list[str] | None = None,
) -> shell._BgSession:
    return shell._BgSession(
        session_id=session_id,
        command=command or f"cmd {session_id}",
        process=_FakeProcess(returncode=0 if done else None),  # type: ignore[arg-type]
        session_key=session_key,
        agent_id=agent_id,
        done=done,
        returncode=0 if done else None,
        local_urls=local_urls or [],
    )


def test_background_process_result_surfaces_local_http_server_url() -> None:
    session = _session(
        "server",
        "agent:main:one",
        command="cd /workspace && python3 -m http.server 8080",
        local_urls=shell._local_server_urls_from_command(
            "cd /workspace && python3 -m http.server 8080"
        ),
    )

    result = shell._background_process_result(session)

    assert "local_urls:" in result
    assert "- http://127.0.0.1:8080/" in result
    assert "include the local URL" in result


def test_bg_session_payload_surfaces_codetask_status_with_spaced_path(tmp_path) -> None:
    run_dir = tmp_path / "Application Support" / "code-task" / "run-1"
    run_dir.mkdir(parents=True)
    status_path = run_dir / "status.json"
    status_path.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "phase": "agent_running",
                "updated": "2026-06-29T00:00:00Z",
                "log_paths": {"stdout": str(run_dir / "agent_stdout.log")},
            }
        ),
        encoding="utf-8",
    )
    session = _session(
        "code",
        "agent:main:one",
        command="opensquilla-gateway code-task solve --task-file /tmp/x --yes",
    )
    session.output_lines.append(
        "[code-task] run started: run_id=run-1 "
        f"artifact_dir={run_dir} status={status_path} "
        "(work happens in the run dir)\n"
    )

    payload = shell._bg_session_payload(session)

    code_task = payload["code_task"]
    assert isinstance(code_task, dict)
    assert code_task["run_id"] == "run-1"
    assert code_task["artifact_dir"] == str(run_dir)
    assert code_task["status_path"] == str(status_path)
    assert code_task["phase"] == "agent_running"
    assert code_task["log_paths"] == {"stdout": str(run_dir / "agent_stdout.log")}


def test_verified_channel_admin_can_manage_background_sessions_across_sessions() -> None:
    own = _session("own", "agent:main:feishu:direct:owner")
    other = _session("other", "agent:main:feishu:direct:other")
    shell._bg_sessions.update({own.session_id: own, other.session_id: other})
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            channel_admin_verified=True,
            caller_kind=CallerKind.CHANNEL,
            session_key=own.session_key,
        )
    )
    try:
        assert shell._iter_visible_bg_sessions() == [own, other]
        assert shell.get_bg_session(other.session_id) is other
    finally:
        current_tool_context.reset(token)


def test_unverified_channel_owner_cannot_manage_other_background_sessions() -> None:
    own = _session("own", "agent:main:feishu:direct:owner")
    other = _session("other", "agent:main:feishu:direct:other")
    shell._bg_sessions.update({own.session_id: own, other.session_id: other})
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CHANNEL,
            session_key=own.session_key,
        )
    )
    try:
        assert shell._iter_visible_bg_sessions() == [own]
        assert shell.get_bg_session(other.session_id) is None
    finally:
        current_tool_context.reset(token)


@pytest.mark.skipif(os.name != "posix", reason="process group behavior is POSIX-specific")
@pytest.mark.asyncio
async def test_exec_command_returns_when_shell_exits_even_if_descendant_holds_pipe() -> None:
    python = _process_test_python()
    child_script = "import time; time.sleep(5)"
    parent_script = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', "
            f"{child_script!r}], stdout=sys.stdout, stderr=sys.stderr)"
    )
    command = f"{shlex.quote(python)} -c {shlex.quote(parent_script)}"

    started = time.monotonic()
    result = await shell.exec_command(command, timeout=1.0)
    elapsed = time.monotonic() - started

    assert result.startswith("exit_code=0\n")
    assert elapsed < 1.0


@pytest.mark.skipif(os.name != "posix", reason="process group behavior is POSIX-specific")
@pytest.mark.asyncio
async def test_exec_command_cleans_descendant_after_shell_exits(tmp_path) -> None:
    python = _process_test_python()
    marker = tmp_path / "descendant-ran"
    child_script = (
        "import pathlib, time; "
        f"time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('ran')"
    )
    parent_script = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', "
            f"{child_script!r}])"
    )
    command = f"{shlex.quote(python)} -c {shlex.quote(parent_script)}"

    result = await shell.exec_command(command, timeout=1.0)
    await asyncio.sleep(0.8)

    assert result.startswith("exit_code=0\n")
    assert not marker.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX shell quoting is required")
async def test_exec_command_timeout_still_stops_foreground_process() -> None:
    python = _process_test_python()
    command = f"{shlex.quote(python)} -c {shlex.quote('import time; time.sleep(5)')}"

    started = time.monotonic()
    result = await shell.exec_command(command, timeout=0.1)
    elapsed = time.monotonic() - started

    assert "[timeout after 0.1s]" in result
    assert elapsed < 1.0


@pytest.mark.skipif(os.name != "posix", reason="process group behavior is POSIX-specific")
@pytest.mark.asyncio
async def test_caller_cancel_stops_foreground_process_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    marker = tmp_path / "cancelled-descendant-ran"
    child_script = (
        "import pathlib, time; "
        f"time.sleep(0.6); pathlib.Path({str(marker)!r}).write_text('ran')"
    )
    parent_script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        "time.sleep(30)"
    )
    command = _python_shell_command(parent_script)
    created: list[asyncio.subprocess.Process] = []
    original_create = shell._create_host_shell_subprocess

    async def capture_process(*args, **kwargs):
        proc = await original_create(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(shell, "_create_host_shell_subprocess", capture_process)
    running = asyncio.create_task(
        shell._run_host_shell_command(
            command,
            cwd=None,
            env=dict(os.environ),
            stdin_bytes=None,
            effective_timeout=30.0,
        )
    )
    for _attempt in range(100):
        if created:
            break
        await asyncio.sleep(0.01)
    assert created

    running.cancel()
    cancelled = await asyncio.gather(running, return_exceptions=True)
    assert isinstance(cancelled[0], asyncio.CancelledError)

    assert created[0].returncode is not None
    await asyncio.sleep(0.8)
    assert not marker.exists()


@pytest.mark.asyncio
async def test_task_cancel_stops_only_owned_background_process() -> None:
    token = current_tool_context.set(
        _ctx("agent:main:background-owner", task_id="task-owned")
    )
    try:
        result = await shell._start_host_background_process(
            _python_shell_command("import time; time.sleep(30)"),
            cwd=None,
            effective_timeout=30.0,
            runtime=None,
        )
    finally:
        current_tool_context.reset(token)

    session_id = result.splitlines()[0].split("=", 1)[1]
    session = shell._bg_sessions[session_id]
    assert session.task_id == "task-owned"
    assert session.process.returncode is None
    assert (
        "agent:main:background-owner",
        "task-owned",
    ) in shell.active_background_process_task_owners()

    cancelled = await shell.cancel_background_processes_for_task(
        "agent:main:background-owner",
        "task-owned",
    )

    assert cancelled == 1
    assert session.killed is True
    if session.collector_task is not None:
        await asyncio.wait_for(session.collector_task, timeout=2.0)
    assert session.process.returncode is not None
    assert (
        "agent:main:background-owner",
        "task-owned",
    ) not in shell.active_background_process_task_owners()


@pytest.mark.skipif(os.name != "posix", reason="process group behavior is POSIX-specific")
@pytest.mark.asyncio
async def test_task_cancel_stops_descendant_after_background_leader_exits(tmp_path) -> None:
    marker = tmp_path / "leader-exit-descendant-ran"
    child_script = (
        "import pathlib, time; "
        f"time.sleep(1.0); pathlib.Path({str(marker)!r}).write_text('ran'); "
        "time.sleep(30)"
    )
    parent_script = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}])"
    )
    token = current_tool_context.set(
        _ctx("agent:main:leader-exit", task_id="task-leader-exit")
    )
    try:
        result = await shell._start_host_background_process(
            _python_shell_command(parent_script),
            cwd=None,
            effective_timeout=30.0,
            runtime=None,
        )
    finally:
        current_tool_context.reset(token)

    session_id = result.splitlines()[0].split("=", 1)[1]
    session = shell._bg_sessions[session_id]
    for _attempt in range(200):
        if session.process.returncode is not None:
            break
        await asyncio.sleep(0.01)
    assert session.process.returncode == 0
    assert session.process_tree is not None
    assert session.process_tree.is_active() is True
    assert (
        "agent:main:leader-exit",
        "task-leader-exit",
    ) in shell.active_background_process_task_owners()

    assert await shell.cancel_background_processes_for_task(
        "agent:main:leader-exit",
        "task-leader-exit",
    ) == 1
    await asyncio.sleep(1.2)

    assert session.process_tree.is_active() is False
    assert not marker.exists()


@pytest.mark.asyncio
async def test_task_cancel_is_exact_across_siblings_sessions_and_repeats() -> None:
    sessions: list[shell._BgSession] = []
    identities = (
        ("agent:main:shared", "task-target"),
        ("agent:main:shared", "task-sibling"),
        ("agent:main:other", "task-target"),
    )
    for session_key, task_id in identities:
        token = current_tool_context.set(_ctx(session_key, task_id=task_id))
        try:
            result = await shell._start_host_background_process(
                _python_shell_command("import time; time.sleep(30)"),
                cwd=None,
                effective_timeout=30.0,
                runtime=None,
            )
        finally:
            current_tool_context.reset(token)
        session_id = result.splitlines()[0].split("=", 1)[1]
        sessions.append(shell._bg_sessions[session_id])

    try:
        assert await shell.cancel_background_processes_for_task(
            "agent:main:shared",
            "task-target",
        ) == 1
        assert await shell.cancel_background_processes_for_task(
            "agent:main:shared",
            "task-target",
        ) == 0
        assert sessions[0].process_tree is not None
        assert sessions[0].process_tree.is_active() is False
        assert sessions[1].process_tree is not None
        assert sessions[1].process_tree.is_active() is True
        assert sessions[2].process_tree is not None
        assert sessions[2].process_tree.is_active() is True
    finally:
        await shell.cancel_background_processes_for_task(
            "agent:main:shared",
            "task-sibling",
        )
        await shell.cancel_background_processes_for_task(
            "agent:main:other",
            "task-target",
        )


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job Objects")
@pytest.mark.asyncio
async def test_windows_job_owns_descendant_after_leader_exit(tmp_path) -> None:
    from opensquilla.process_tree import (
        capture_process_tree_owner,
        create_owned_subprocess_exec,
    )

    pid_file = tmp_path / "windows-descendant.pid"
    survived = tmp_path / "windows-descendant-survived"
    child_script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(1); "
        f"pathlib.Path({str(survived)!r}).write_text('survived'); "
        "time.sleep(30)"
    )
    parent_script = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}])"
    )
    proc = await create_owned_subprocess_exec(
        sys.executable,
        "-c",
        parent_script,
    )
    process_tree = capture_process_tree_owner(proc, isolated=True)

    assert process_tree.durable is True
    await asyncio.wait_for(proc.wait(), timeout=5.0)
    for _attempt in range(200):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    assert process_tree.is_active() is True

    assert await process_tree.terminate(graceful_timeout=0.2, kill_timeout=2.0)
    assert process_tree.is_active() is False
    await asyncio.sleep(1.2)
    assert not survived.exists()


@pytest.mark.asyncio
async def test_exec_command_writes_optional_stdin() -> None:
    command = _python_shell_command(
        "import sys; data = sys.stdin.read(); print('STDIN:' + data)"
    )

    result = await shell.exec_command(command, stdin="payload", timeout=5.0)

    exit_line, stdout = result.split("\n", 1)
    assert exit_line == "exit_code=0"
    assert stdout.splitlines() == ["STDIN:payload"]


@pytest.mark.asyncio
async def test_write_exec_stdin_waits_until_eof_is_delivered() -> None:
    stdin = _FakeStdin()
    proc = SimpleNamespace(stdin=stdin)

    await shell._write_exec_stdin(proc, b"payload")

    assert stdin.writes == [b"payload"]
    assert stdin.closed is True
    assert stdin.close_waited is True


@pytest.mark.asyncio
async def test_windows_host_stdin_uses_blocking_communicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _CompletedProcess:
        returncode = None
        pid = 1

        def communicate(self, *, input: bytes) -> tuple[None, None]:
            captured["stdin_bytes"] = input
            self.returncode = 0
            return None, None

        def poll(self) -> int | None:
            return self.returncode

    def fake_create_subprocess(command: str, **kwargs: object):
        captured["command"] = command
        captured["stdin_target"] = kwargs["stdin"]
        return _CompletedProcess()

    monkeypatch.setattr(shell, "_use_windows_blocking_exec_stdin", lambda: True)
    monkeypatch.setattr(shell, "_create_windows_host_shell_process", fake_create_subprocess)

    result = await shell._run_host_shell_command(
        "Write-Output ok",
        cwd=None,
        env={},
        stdin_bytes=b"payload",
        effective_timeout=1.0,
    )

    assert result == "exit_code=0\n"
    assert captured == {
        "command": "Write-Output ok",
        "stdin_target": subprocess.PIPE,
        "stdin_bytes": b"payload",
    }


@pytest.mark.asyncio
async def test_windows_blocking_stdin_timeout_terminates_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    actions: list[str] = []

    class _HangingProcess:
        returncode = None
        pid = 1

        def communicate(self, *, input: bytes) -> tuple[None, None]:
            assert input == b"payload"
            assert release.wait(timeout=2)
            return None, None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            actions.append("terminate")
            self.returncode = -1
            release.set()

        def wait(self, *, timeout: float) -> int:
            assert timeout == shell._EXEC_TERMINATE_TIMEOUT
            return -1

    monkeypatch.setattr(shell, "_use_windows_blocking_exec_stdin", lambda: True)
    monkeypatch.setattr(
        shell,
        "_create_windows_host_shell_process",
        lambda _command, **_kwargs: _HangingProcess(),
    )

    result = await shell._run_host_shell_command(
        "Write-Output ok",
        cwd=None,
        env={},
        stdin_bytes=b"payload",
        effective_timeout=0.01,
    )

    assert result.startswith("[timeout after 0.01s]")
    assert actions == ["terminate"]


@pytest.mark.asyncio
async def test_wait_exec_stdin_writer_accepts_process_exit_before_pipe_close() -> None:
    proc = _FakeProcess(returncode=None)
    release_writer = asyncio.Event()

    async def wait_for_pipe_close() -> None:
        await release_writer.wait()

    writer_task = asyncio.create_task(wait_for_pipe_close())

    async def finish_process() -> None:
        await asyncio.sleep(0.02)
        proc.returncode = 0

    process_task = asyncio.create_task(finish_process())
    started = time.monotonic()
    try:
        assert await shell._wait_exec_stdin_writer(proc, writer_task, timeout=0.5)
        assert time.monotonic() - started < 0.25
        assert not writer_task.done()
    finally:
        await process_task
        await shell._cancel_exec_stdin_writer(proc, writer_task)

    assert writer_task.cancelled()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="large pipe backpressure is POSIX-specific")
async def test_exec_command_stdin_write_obeys_timeout() -> None:
    command = _python_shell_command("import time; time.sleep(5)")

    started = time.monotonic()
    result = await shell.exec_command(command, stdin="x" * 1_000_000, timeout=0.2)
    elapsed = time.monotonic() - started

    assert "[timeout after 0.2s]" in result
    assert elapsed < 3.0, "timeout path should return before the 5s child sleep"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX shell quoting is required")
async def test_exec_command_sandbox_escalation_stdin_returns_when_shell_exits(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(effective=SimpleNamespace(sandbox_enabled=True))
    child_script = "import time; time.sleep(5)"
    parent_script = (
        "import subprocess, sys; "
        "sys.stdin.read(); "
        "subprocess.Popen([sys.executable, '-c', "
        f"{child_script!r}], stdout=sys.stdout, stderr=sys.stderr)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(parent_script)}"

    async def fake_gate_action(**kwargs: object):
        request = SimpleNamespace(
            cwd=tmp_path,
            action_kind="shell.exec",
            policy=SimpleNamespace(),
            reason="test",
        )
        return object(), SimpleNamespace(), request

    async def fake_run_under_backend(*args: object, **kwargs: object) -> SandboxResult:
        return SandboxResult(
            returncode=1,
            stdout="",
            stderr="",
            wall_time_s=0.0,
            backend_used="seatbelt",
            backend_notes=("sandbox denied",),
        )

    async def fake_escalate_backend_denial(*args: object, **kwargs: object) -> DenialResult:
        return DenialResult(
            reason=DenialReason.SEATBELT_DENIED,
            suggested_next_step=SuggestedNextStep.ASK_USER,
            level=SecurityLevel.STANDARD,
            action_fingerprint="fp",
            message="sandbox denied",
            retryable=False,
        )

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", fake_run_under_backend)
    monkeypatch.setattr(shell, "escalate_backend_denial", fake_escalate_backend_denial)

    started = time.monotonic()
    result = await shell.exec_command(command, stdin="payload", timeout=0.5)
    elapsed = time.monotonic() - started

    payload = json.loads(result)
    assert payload["status"] == "denied"
    assert payload["reason"] == "seatbelt_denied"
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_process_list_filters_to_current_session_and_warns_for_untagged() -> None:
    shell._bg_sessions["own"] = _session("own", "agent:main:one")
    shell._bg_sessions["other"] = _session("other", "agent:main:two")
    shell._bg_sessions["legacy"] = _session("legacy", None)

    token = current_tool_context.set(_ctx("agent:main:one"))
    try:
        with structlog.testing.capture_logs() as captured:
            payload = json.loads(await shell.process("list"))
    finally:
        current_tool_context.reset(token)

    assert [session["session_id"] for session in payload["sessions"]] == ["own"]
    assert any(event["event"] == "shell.bg_session_untagged" for event in captured)


@pytest.mark.asyncio
async def test_process_owner_context_can_list_all_sessions() -> None:
    shell._bg_sessions["own"] = _session("own", "agent:main:one")
    shell._bg_sessions["other"] = _session("other", "agent:main:two")

    token = current_tool_context.set(
        _ctx("agent:main:ops", is_owner=True, caller_kind=CallerKind.CLI)
    )
    try:
        payload = json.loads(await shell.process("list"))
    finally:
        current_tool_context.reset(token)

    assert {session["session_id"] for session in payload["sessions"]} == {"own", "other"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action", ["poll", "wait", "log", "kill", "remove", "write", "submit", "eof"]
)
async def test_process_cross_context_operations_are_denied(action: str) -> None:
    shell._bg_sessions["owned-by-other"] = _session(
        "owned-by-other",
        "agent:main:other",
        done=action == "remove",
    )

    token = current_tool_context.set(_ctx("agent:main:one"))
    kwargs = {"data": "hello"} if action in {"write", "submit"} else {}
    try:
        with pytest.raises(ToolError, match="not accessible"):
            await shell.process(action, session_id="owned-by-other", **kwargs)
    finally:
        current_tool_context.reset(token)


@pytest.mark.asyncio
async def test_process_owner_context_can_poll_other_sessions() -> None:
    shell._bg_sessions["other"] = _session("other", "agent:main:two")

    token = current_tool_context.set(
        _ctx("agent:main:ops", is_owner=True, caller_kind=CallerKind.CLI)
    )
    try:
        payload = json.loads(await shell.process("poll", session_id="other"))
    finally:
        current_tool_context.reset(token)

    assert payload["status"] == "ok"
    assert payload["session"]["session_id"] == "other"


@pytest.mark.asyncio
async def test_process_poll_includes_local_urls_for_server_sessions() -> None:
    shell._bg_sessions["server"] = _session(
        "server",
        "agent:main:one",
        command="python -m http.server 9090",
        local_urls=["http://127.0.0.1:9090/"],
    )

    token = current_tool_context.set(_ctx("agent:main:one"))
    try:
        payload = json.loads(await shell.process("poll", session_id="server"))
    finally:
        current_tool_context.reset(token)

    assert payload["session"]["local_urls"] == ["http://127.0.0.1:9090/"]


@pytest.mark.asyncio
async def test_process_subagent_owner_context_is_not_admin_bypass() -> None:
    shell._bg_sessions["own"] = _session("own", "subagent:agent:main:one")
    shell._bg_sessions["other"] = _session("other", "agent:main:two")

    token = current_tool_context.set(
        _ctx("subagent:agent:main:one", is_owner=True, caller_kind=CallerKind.SUBAGENT)
    )
    try:
        payload = json.loads(await shell.process("list"))
        with pytest.raises(ToolError, match="not accessible"):
            await shell.process("poll", session_id="other")
    finally:
        current_tool_context.reset(token)

    assert [session["session_id"] for session in payload["sessions"]] == ["own"]


# --- process(action="wait") - blocking await replaces poll loops (codex) ---


async def _real_bg_session(session_id: str, session_key: str, argv: list[str]):
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    sess = shell._BgSession(
        session_id=session_id,
        command=" ".join(argv),
        process=proc,
        session_key=session_key,
        agent_id="agent",
    )
    shell._bg_sessions[session_id] = sess
    return sess


def test_process_wait_is_a_valid_action() -> None:
    assert "wait" in shell.PROCESS_ACTIONS


def test_process_tool_declares_wait_timeout_metadata() -> None:
    # Without this metadata the 60s tool watchdog would kill a long wait.
    from opensquilla.tools.registry import get_default_registry

    spec = get_default_registry().get("process").spec
    assert (
        spec.execution_timeout_seconds
        >= shell._MAX_PROCESS_WAIT_TIMEOUT + shell._PROCESS_WAIT_TIMEOUT_PADDING
    )
    assert spec.execution_timeout_argument == "timeout"


def test_process_wait_uses_coding_mode_default_timeout() -> None:
    ctx = ToolContext(coding_mode=True)

    assert ctx.coding_mode is True
    token = current_tool_context.set(ctx)
    try:
        assert (
            shell._resolve_process_wait_timeout(None)
            == shell._CODING_PROCESS_WAIT_TIMEOUT
        )
    finally:
        current_tool_context.reset(token)


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX sleep/true")
@pytest.mark.asyncio
async def test_process_wait_blocks_until_exit() -> None:
    await _real_bg_session("w1", "agent:main:one", ["sleep", "0.3"])
    token = current_tool_context.set(_ctx("agent:main:one"))
    try:
        payload = json.loads(await shell.process("wait", session_id="w1"))
    finally:
        current_tool_context.reset(token)
    assert payload["exited"] is True
    assert payload["session"]["status"] == "done"
    assert payload["session"]["returncode"] == 0


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX true")
@pytest.mark.asyncio
async def test_process_wait_on_already_exited_returns_immediately() -> None:
    sess = await _real_bg_session("w2", "agent:main:one", ["true"])
    await sess.process.wait()
    token = current_tool_context.set(_ctx("agent:main:one"))
    try:
        payload = json.loads(await shell.process("wait", session_id="w2"))
    finally:
        current_tool_context.reset(token)
    assert payload["exited"] is True
    assert payload["session"]["returncode"] == 0


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX sleep")
@pytest.mark.asyncio
async def test_process_wait_timeout_keeps_running_and_not_timed_out() -> None:
    sess = await _real_bg_session("w3", "agent:main:one", ["sleep", "5"])
    token = current_tool_context.set(_ctx("agent:main:one"))
    try:
        payload = json.loads(await shell.process("wait", session_id="w3", timeout=0.2))
        assert payload["exited"] is False
        assert payload["session"]["status"] == "running"
        # The wait-action timeout must NOT flip the process-lifetime timed_out flag.
        assert payload["session"]["timed_out"] is False
    finally:
        sess.process.terminate()
        try:
            await asyncio.wait_for(sess.process.wait(), timeout=2)
        except (TimeoutError, ProcessLookupError):
            pass
        current_tool_context.reset(token)


@pytest.mark.skipif(os.name != "posix", reason="uses POSIX sleep")
@pytest.mark.asyncio
async def test_process_wait_finalizes_when_exit_races_timeout(monkeypatch) -> None:
    # The process exits right at the wait timeout boundary, where
    # _wait_bg_process reports False. The wait action must still finalize and
    # report a consistent (not stale "running") payload (codex review).
    sess = await _real_bg_session("w4", "agent:main:one", ["sleep", "0.1"])
    proc = sess.process

    async def _wait_then_report_timeout(session, timeout):
        await proc.wait()  # the process actually exits...
        return False  # ...but we report a timeout (the boundary race)

    monkeypatch.setattr(shell, "_wait_bg_process", _wait_then_report_timeout)
    token = current_tool_context.set(_ctx("agent:main:one"))
    try:
        payload = json.loads(await shell.process("wait", session_id="w4", timeout=0.01))
    finally:
        current_tool_context.reset(token)
    assert payload["exited"] is True
    assert payload["session"]["status"] == "done"
    assert payload["session"]["returncode"] == 0
