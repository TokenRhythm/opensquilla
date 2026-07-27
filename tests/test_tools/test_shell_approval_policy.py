from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.tools.builtin import code_exec, filesystem, shell
from opensquilla.tools.builtin.code_exec import execute_code
from opensquilla.tools.builtin.shell_policy import PolicyResult
from opensquilla.tools.types import (
    CallerKind,
    InteractionMode,
    ToolContext,
    ToolError,
    current_tool_context,
)


def _original_async(fn):
    while hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__  # type: ignore[attr-defined]
    return fn


@pytest.fixture(autouse=True)
def reset_approval_state():
    reset_approval_queue()
    reset_runtime()
    token = current_tool_context.set(
        ToolContext(is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:test")
    )
    yield
    current_tool_context.reset(token)
    reset_approval_queue()
    reset_runtime()


def test_audit_command_preserves_long_commands_until_cap() -> None:
    command = "rm " + ("x" * 120)
    assert shell._audit_command(command) == command

    huge = "rm " + ("x" * 5000)
    audited = shell._audit_command(huge)
    assert len(audited) > 80
    assert audited.endswith("...[truncated]")


@pytest.mark.parametrize(
    ("interpreter", "script_name"),
    (("bash", "job.sh"), ("python3", "job.py")),
)
def test_shell_approval_fingerprint_changes_when_script_content_changes(
    tmp_path: Path,
    interpreter: str,
    script_name: str,
) -> None:
    script = tmp_path / script_name
    script.write_text("print('before')\n", encoding="utf-8")
    command = f"{interpreter} {script_name}"
    profile = shell._profile_shell_command(command, workdir=str(tmp_path))

    before = shell._shell_elevation_action(
        tool_name="exec_command",
        action_kind="shell.exec",
        command=command,
        cwd=str(tmp_path),
        profile=profile,
        justification="Run the requested script.",
    )
    script.write_text("print('after')\n", encoding="utf-8")
    after = shell._shell_elevation_action(
        tool_name="exec_command",
        action_kind="shell.exec",
        command=command,
        cwd=str(tmp_path),
        profile=profile,
        justification="Run the requested script.",
    )

    assert before.fingerprint() != after.fingerprint()


@pytest.mark.asyncio
async def test_exec_approval_deny_pattern_blocks_shell_command(tmp_path: Path) -> None:
    get_approval_queue().set_settings("prompt", deny_patterns=["rm *"])

    result = await shell.exec_command("rm target.txt", workdir=str(tmp_path))
    payload = json.loads(result)

    assert payload["status"] == "approval_denied"
    assert payload["command"] == "rm target.txt"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_exec_approval_deny_pattern_blocks_wrapped_shell_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_approval_queue().set_settings("prompt", deny_patterns=["rm *"])
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda _command: PolicyResult(allowed=True, reason="", needs_approval=False),
    )

    async def fail_host_execution(*args: object, **kwargs: object) -> str:
        raise AssertionError("denied wrapped command should not run")

    monkeypatch.setattr(shell, "_run_host_shell_command", fail_host_execution)

    result = await shell.exec_command(
        'powershell -NoProfile -Command "rm target.txt"',
        workdir=str(tmp_path),
    )
    payload = json.loads(result)

    assert payload["status"] == "approval_denied"
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_exec_approval_deny_pattern_does_not_depend_on_warnlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_approval_queue().set_settings("prompt", deny_patterns=["git status"])
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda _command: PolicyResult(allowed=True, reason="", needs_approval=False),
    )

    result = await shell.exec_command("git status", workdir=str(tmp_path))
    payload = json.loads(result)

    assert payload["status"] == "approval_denied"
    assert payload["command"] == "git status"


@pytest.mark.asyncio
async def test_full_host_access_warnlisted_exec_skips_approval_and_records_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.run_mode = "full"
    get_approval_queue().set_settings("auto-deny")
    calls: list[str] = []

    async def fake_host_execution(*args: object, **kwargs: object) -> str:
        calls.append("host")
        return "exit_code=0\nhost\n"

    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host_execution)
    def fail_preflight(*args: object, **kwargs: object) -> object:
        raise AssertionError("Full Host Access must skip shell safety preflight")

    monkeypatch.setattr(shell, "_runtime_readonly_shell_block", fail_preflight)
    monkeypatch.setattr(shell, "_profile_shell_command", fail_preflight)
    monkeypatch.setattr(shell, "check_safe_bin", fail_preflight)
    monkeypatch.setattr(shell, "_approval_policy_denial", fail_preflight)
    monkeypatch.setattr(shell, "snapshot_current_workspace_mutations", lambda: {})
    mutation_records: list[dict[str, object]] = []
    monkeypatch.setattr(
        shell,
        "record_observed_workspace_mutations",
        lambda **kwargs: mutation_records.append(kwargs),
    )

    result = await shell.exec_command("pip install requests", workdir=str(tmp_path))

    assert result == "exit_code=0\nhost\n"
    assert calls == ["host"]
    assert mutation_records == [
        {
            "tool_name": "exec_command",
            "before": {},
            "metadata": {"command_hash": shell.mutation_ledger_text_hash("pip install requests")},
        }
    ]
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_full_host_write_file_skips_safety_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.run_mode = "full"
    ctx.workspace_dir = str(tmp_path / "workspace")
    ctx.file_edit_requires_fresh_read = True
    ctx.workspace_write_deny_globs = ["**"]
    target = tmp_path / "outside" / "large.txt"
    target.parent.mkdir()
    target.write_text("old" * 4096, encoding="utf-8")

    def fail_safety_tracking(*args: object, **kwargs: object) -> object:
        raise AssertionError("Full Host Access must skip file safety guards")

    monkeypatch.setattr(filesystem, "_gate_out_of_workspace_write", fail_safety_tracking)
    monkeypatch.setattr(filesystem, "require_fresh_workspace_file_read", fail_safety_tracking)
    monkeypatch.setattr(filesystem, "_gate_write_file_destructive_overwrite", fail_safety_tracking)
    monkeypatch.setattr(filesystem, "fingerprint_path", fail_safety_tracking)
    monkeypatch.setattr(filesystem, "record_semantic_mutation_receipt", fail_safety_tracking)

    result = await _original_async(filesystem.write_file)(str(target), "new")

    assert result == f"Written 3 bytes to {target}"
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.asyncio
async def test_sandbox_disabled_warnlisted_exec_skips_approval_and_uses_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.run_mode = "standard"
    ctx.workspace_dir = str(tmp_path)
    configure_runtime(
        SandboxSettings(sandbox=False, security_grading=False),
        workspace=tmp_path,
    )
    get_approval_queue().set_settings("auto-deny")
    calls: list[str] = []

    async def fake_host_execution(*args: object, **kwargs: object) -> str:
        calls.append("host")
        return "exit_code=0\nhost\n"

    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host_execution)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: PolicyResult(
            allowed=True,
            reason=f"command requires approval: {command}",
            needs_approval=True,
        ),
    )

    result = await shell.exec_command("pip install requests", workdir=str(tmp_path))

    assert result == "exit_code=0\nhost\n"
    assert calls == ["host"]
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_warnlisted_exec_allow_pattern_skips_prompt_when_sandbox_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.workspace_dir = str(tmp_path)
    get_approval_queue().set_settings(
        "prompt",
        allow_patterns=["pip install requests"],
    )
    configure_runtime(
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="noop",
            allow_legacy_mode=True,
        ),
        workspace=tmp_path,
    )
    calls: list[tuple[str, object]] = []

    async def fake_gate_action(**kwargs: object) -> tuple[object, object, object]:
        calls.append(("gate", kwargs))
        policy = SimpleNamespace(network=None)
        request = SimpleNamespace(cwd=tmp_path, action_kind="shell.exec", policy=policy)
        return object(), policy, request

    async def fake_sandbox(request: object, *, runtime: object = None) -> object:
        calls.append(("backend", request))
        return SimpleNamespace(
            returncode=0,
            stdout="sandboxed\n",
            stderr="",
            timed_out=False,
            backend_notes=(),
        )

    monkeypatch.setattr(shell, "gate_action", fake_gate_action)
    monkeypatch.setattr(shell, "run_under_backend", fake_sandbox)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: PolicyResult(
            allowed=True,
            reason=f"command requires approval: {command}",
            needs_approval=True,
        ),
    )

    result = await shell.exec_command("pip install requests", workdir=str(tmp_path))

    assert "sandboxed" in result
    assert [name for name, _ in calls] == ["gate", "backend"]
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_warnlisted_exec_allow_pattern_runs_without_approval_when_no_sandbox_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_approval_queue().set_settings(
        "prompt",
        allow_patterns=["pip install requests"],
    )
    calls: list[str] = []

    async def fake_host_execution(*args: object, **kwargs: object) -> str:
        calls.append("host")
        return "exit_code=0\nhost\n"

    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host_execution)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: PolicyResult(
            allowed=True,
            reason=f"command requires approval: {command}",
            needs_approval=True,
        ),
    )

    result = await shell.exec_command("pip install requests", workdir=str(tmp_path))

    assert result == "exit_code=0\nhost\n"
    assert get_approval_queue().list_pending("exec") == []
    assert calls == ["host"]


@pytest.mark.asyncio
async def test_warnlisted_exec_prompt_runs_without_approval_when_no_sandbox_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_approval_queue().set_settings("prompt")
    calls: list[str] = []

    async def fake_host_execution(*args: object, **kwargs: object) -> str:
        calls.append("host")
        return "exit_code=0\nhost\n"

    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host_execution)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: PolicyResult(
            allowed=True,
            reason=f"command requires approval: {command}",
            needs_approval=True,
        ),
    )

    result = await shell.exec_command("pip install requests", workdir=str(tmp_path))

    assert result == "exit_code=0\nhost\n"
    assert get_approval_queue().list_pending("exec") == []
    assert calls == ["host"]


@pytest.mark.asyncio
async def test_warnlisted_exec_auto_approve_runs_without_approval_when_no_sandbox_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_approval_queue().set_settings("auto-approve")
    calls: list[str] = []

    async def fake_host_execution(*args: object, **kwargs: object) -> str:
        calls.append("host")
        return "exit_code=0\nhost\n"

    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host_execution)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: PolicyResult(
            allowed=True,
            reason=f"command requires approval: {command}",
            needs_approval=True,
        ),
    )

    result = await shell.exec_command("pip install requests", workdir=str(tmp_path))

    assert result == "exit_code=0\nhost\n"
    assert get_approval_queue().list_pending("exec") == []
    assert calls == ["host"]


@pytest.mark.asyncio
async def test_warnlisted_exec_unattended_runs_without_approval_when_no_sandbox_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    get_approval_queue().set_settings("prompt")
    calls: list[str] = []

    async def fake_host_execution(*args: object, **kwargs: object) -> str:
        calls.append("host")
        return "exit_code=0\nhost\n"

    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host_execution)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: PolicyResult(
            allowed=True,
            reason=f"command requires approval: {command}",
            needs_approval=True,
        ),
    )

    result = await shell.exec_command("pip install requests", workdir=str(tmp_path))

    assert result == "exit_code=0\nhost\n"
    assert calls == ["host"]
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_destructive_code_exec_uses_sandbox_gate_when_runtime_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.workspace_dir = str(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("delete me", encoding="utf-8")
    configure_runtime(
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="noop",
            allow_legacy_mode=True,
        ),
        workspace=tmp_path,
    )
    calls: list[tuple[str, object]] = []

    async def fake_gate_action(**kwargs):
        calls.append(("gate", kwargs))
        policy = SimpleNamespace(network=None)
        request = SimpleNamespace(cwd=tmp_path, action_kind="code.exec", policy=policy)
        return object(), policy, request

    async def fake_sandbox(request: object, *, runtime: object = None) -> object:
        calls.append(("backend", request))
        return SimpleNamespace(
            returncode=0,
            stdout="sandboxed\n",
            stderr="",
            timed_out=False,
            backend_notes=(),
        )

    monkeypatch.setattr(code_exec, "gate_action", fake_gate_action)
    monkeypatch.setattr(code_exec, "run_under_backend", fake_sandbox)

    result = await execute_code("import os\nos.remove('target.txt')")
    payload = json.loads(result)

    assert payload["exit_code"] == 0
    assert payload["stdout"] == "sandboxed\n"
    assert [name for name, _ in calls] == ["gate", "backend"]
    hints = calls[0][1]["hints"]  # type: ignore[index]
    assert hints.high_impact is True
    assert target.exists()
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_warnlist_background_process_uses_sandbox_gate_when_runtime_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.workspace_dir = str(tmp_path)
    configure_runtime(
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="noop",
            allow_legacy_mode=True,
        ),
        workspace=tmp_path,
    )
    calls: list[tuple[str, object]] = []

    class _FakeStream:
        async def read(self, size: int) -> bytes:
            return b""

    class _FakeProcess:
        stdout = _FakeStream()
        stdin = None
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def fake_gate_action(**kwargs):
        calls.append(("gate", kwargs))
        policy = SimpleNamespace(network=None, network_proxy=None)
        request = SimpleNamespace(cwd=tmp_path, action_kind="shell.background", policy=policy)
        return object(), policy, request

    async def fake_sandbox_spawn(*args: object, **kwargs: object) -> object:
        calls.append(("backend", kwargs.get("request")))
        return shell._SpawnedBackgroundProcess(process=_FakeProcess())  # type: ignore[arg-type]

    monkeypatch.setattr(shell, "gate_action", fake_gate_action)
    monkeypatch.setattr(shell, "_spawn_sandboxed_background_process", fake_sandbox_spawn)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: PolicyResult(
            allowed=True,
            reason=f"command requires approval: {command}",
            needs_approval=True,
        ),
    )

    result = await shell.background_process("rm target.txt", workdir=str(tmp_path))

    assert "status: running" in result
    session_id = result.splitlines()[0].split("=", 1)[1]
    session = shell._bg_sessions[session_id]
    assert session.collector_task is not None
    await session.collector_task
    assert [name for name, _ in calls] == ["gate", "backend"]
    hints = calls[0][1]["hints"]  # type: ignore[index]
    assert hints.high_impact is True
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_outside_workspace_write_blocks_without_sandbox_path_approval(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.workspace_dir = str(workspace)

    write_file = _original_async(filesystem.write_file)
    payload = json.loads(await write_file(str(outside), "ok"))

    assert payload["status"] == "blocked"
    assert payload["reason"] == "outside_workspace"
    assert not outside.exists()
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_workspace_lockdown_blocks_outside_workspace_write_even_with_bypass(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    write_file = _original_async(filesystem.write_file)
    with pytest.raises(ToolError, match="workspace lockdown"):
        await write_file(str(outside), "ok")

    assert not outside.exists()
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_workspace_lockdown_allows_configured_scratch_dir_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    target = scratch / "debug.py"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.scratch_dir = str(scratch)  # type: ignore[attr-defined]
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    result, elevated = await filesystem._gate_out_of_workspace_write(
        "write_file",
        target.resolve(strict=False),
        str(target),
        None,
    )

    assert result is None
    assert elevated is False
    assert not target.exists()
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_workspace_write_deny_globs_block_file_write(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "blocked" / "generated.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_write_deny_globs = ["blocked/**"]  # type: ignore[attr-defined]

    write_file = _original_async(filesystem.write_file)
    with pytest.raises(ToolError, match="workspace write deny policy"):
        await write_file(str(target), "nope")

    assert not target.exists()
    assert get_approval_queue().list_pending("exec") == []


@pytest.mark.asyncio
async def test_workspace_write_deny_globs_block_nested_test_file_write(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "myproj" / "src" / "test" / "java" / "com" / "example"
    target = target / "SampleTest.java"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_write_deny_globs = ["**/test/**", "**/*Test.java"]  # type: ignore[attr-defined]

    write_file = _original_async(filesystem.write_file)
    with pytest.raises(ToolError, match="workspace write deny policy"):
        await write_file(str(target), "nope")

    assert not target.exists()
    assert len(get_approval_queue().list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_workspace_write_deny_globs_allow_configured_scratch_write_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    target = scratch / "test_bug.py"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.workspace_dir = str(workspace)
    ctx.scratch_dir = str(scratch)  # type: ignore[attr-defined]
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]
    ctx.workspace_write_deny_globs = ["**/test_*.py"]  # type: ignore[attr-defined]

    write_file = _original_async(filesystem.write_file)
    result = await write_file(str(target), "print('scratch')\n")

    assert result.startswith("Written 17 bytes to ")
    assert target.read_text(encoding="utf-8") == "print('scratch')\n"
    assert len(get_approval_queue().list_pending("exec")) == 0


@pytest.mark.asyncio
async def test_configured_scratch_dir_blocks_root_debug_workspace_write_file(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    target = workspace / "debug_regex.php"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.scratch_dir = str(scratch)  # type: ignore[attr-defined]
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    write_file = _original_async(filesystem.write_file)
    with pytest.raises(ToolError, match="configured scratch directory"):
        await write_file(str(target), "<?php echo 'debug';\n")

    assert not target.exists()


@pytest.mark.asyncio
async def test_configured_scratch_dir_allows_plain_root_test_source_name(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    target = workspace / "test_api.py"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.scratch_dir = str(scratch)  # type: ignore[attr-defined]
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    write_file = _original_async(filesystem.write_file)
    result = await write_file(str(target), "def test_api():\n    assert True\n")

    assert "Written 32 bytes" in result
    assert str(target) in result
    assert target.exists()


@pytest.mark.asyncio
async def test_write_file_blocks_large_workspace_file_fragment_overwrite(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "src" / "main.c"
    target.parent.mkdir()
    target.write_text("int keep;\n" * 700, encoding="utf-8")
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)

    await filesystem.read_file(str(target))
    write_file = _original_async(filesystem.write_file)
    with pytest.raises(ToolError, match="write_file refused to overwrite"):
        await write_file(str(target), "int replacement_fragment;\n")

    assert target.read_text(encoding="utf-8").startswith("int keep;\n")


@pytest.mark.asyncio
async def test_write_file_allows_scratch_file_fragment_overwrite(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    target = scratch / "debug.c"
    target.write_text("int keep;\n" * 700, encoding="utf-8")
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.scratch_dir = str(scratch)  # type: ignore[attr-defined]

    write_file = _original_async(filesystem.write_file)
    result = await write_file(str(target), "int replacement_fragment;\n")

    assert result.startswith("Written 26 bytes to ")
    assert target.read_text(encoding="utf-8") == "int replacement_fragment;\n"


@pytest.mark.asyncio
async def test_write_file_allows_small_workspace_file_overwrite(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "README.md"
    target.write_text("old\n", encoding="utf-8")
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)

    await filesystem.read_file(str(target))
    write_file = _original_async(filesystem.write_file)
    result = await write_file(str(target), "new\n")

    assert result.startswith("Written 4 bytes to ")
    assert target.read_text(encoding="utf-8") == "new\n"


def test_tool_definitions_include_scratch_guidance_when_configured(tmp_path: Path) -> None:
    from opensquilla.tools.registry import get_default_registry

    scratch = tmp_path / "scratch"
    ctx = ToolContext(is_owner=True, scratch_dir=str(scratch))

    tools = get_default_registry().to_tool_definitions(ctx)
    descriptions = {tool.name: tool.description for tool in tools}

    assert str(scratch) in descriptions["exec_command"]
    assert str(scratch) in descriptions["write_file"]


def test_exec_command_description_names_windows_powershell_shell() -> None:
    from opensquilla.tools.registry import get_default_registry

    ctx = ToolContext(is_owner=True)

    tools = get_default_registry().to_tool_definitions(ctx)
    descriptions = {tool.name: tool.description for tool in tools}

    assert "PowerShell" in descriptions["exec_command"]
    assert "cmd /c" in descriptions["exec_command"]


@pytest.mark.asyncio
async def test_workspace_lockdown_blocks_obvious_outside_shell_redirection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    result = await shell.exec_command(f"echo ok > {outside}", workdir=str(workspace))
    payload = json.loads(result)

    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_lockdown"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="/dev/null redirection is POSIX-specific")
async def test_workspace_lockdown_allows_dev_null_shell_redirection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    result = await shell.exec_command("printf ok 2>/dev/null", workdir=str(workspace))

    assert result == "exit_code=0\nok"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="/dev/null redirection is POSIX-specific")
async def test_workspace_lockdown_dev_null_does_not_hide_sensitive_operand(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    result = await shell.exec_command(
        "cat /dev/sda 2>/dev/null",
        workdir=str(workspace),
    )
    payload = json.loads(result)

    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"


@pytest.mark.asyncio
async def test_workspace_write_deny_globs_block_shell_redirection(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_write_deny_globs = ["reports/*.txt"]  # type: ignore[attr-defined]

    result = await shell.exec_command("echo ok > reports/out.txt", workdir=str(workspace))

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"
    assert payload["matched_pattern"] == "reports/*.txt"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="heredoc redirection is POSIX-specific")
async def test_workspace_write_deny_globs_allow_shell_redirection_after_cd_to_scratch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    target = scratch / "test_bug.py"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.scratch_dir = str(scratch)  # type: ignore[attr-defined]
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]
    ctx.workspace_write_deny_globs = ["**/test_*.py"]  # type: ignore[attr-defined]

    result = await shell.exec_command(
        f"cd {scratch} && cat > test_bug.py << 'EOF'\nprint('scratch')\nEOF\ncat test_bug.py",
        workdir=str(workspace),
    )

    assert result == "exit_code=0\nprint('scratch')\n"
    assert target.read_text(encoding="utf-8") == "print('scratch')\n"
    assert not (workspace / "test_bug.py").exists()


@pytest.mark.asyncio
async def test_configured_scratch_dir_blocks_root_test_shell_redirection(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "scratch"
    workspace.mkdir()
    scratch.mkdir()
    target = workspace / "test_bug.php"
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.scratch_dir = str(scratch)  # type: ignore[attr-defined]
    ctx.workspace_lockdown = True  # type: ignore[attr-defined]

    result = await shell.exec_command(
        "cat > test_bug.php << 'EOF'\n<?php echo 'debug';\nEOF\n",
        workdir=str(workspace),
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_scratch_artifact"
    assert str(scratch) in payload["message"]
    assert not target.exists()


@pytest.mark.asyncio
async def test_workspace_write_deny_globs_inspect_shell_stdin(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.interaction_mode = InteractionMode.UNATTENDED
    ctx.elevated = "bypass"
    ctx.workspace_dir = str(workspace)
    ctx.workspace_write_deny_globs = ["reports/*.txt"]  # type: ignore[attr-defined]

    result = await shell.exec_command(
        "sh",
        workdir=str(workspace),
        stdin="mkdir -p reports\necho secret > reports/out.txt\n",
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "workspace_write_deny"
    assert payload["matched_pattern"] == "reports/*.txt"


@pytest.mark.asyncio
async def test_bypass_still_blocks_sensitive_shell_targets() -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.elevated = "bypass"

    result = await shell.exec_command("rm ~/.ssh/id_rsa")
    payload = json.loads(result)

    assert payload["status"] == "blocked"
    assert payload["reason"] == "sensitive_path"
    assert shell.full_host_access_active() is False


@pytest.mark.asyncio
async def test_bypass_does_not_override_safe_bin_hard_denies() -> None:
    ctx = current_tool_context.get()
    assert ctx is not None
    ctx.elevated = "bypass"

    with pytest.raises(ToolError, match="command blocked by policy"):
        await shell.exec_command("Clear-Disk")
