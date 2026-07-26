from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


def _windows_runtime() -> SimpleNamespace:
    return SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        backend=SimpleNamespace(name="windows_default"),
    )


def _configure_approval_queue(
    monkeypatch,
    tmp_path,
    mode: str,
    *,
    allow_patterns: list[str] | None = None,
    deny_patterns: list[str] | None = None,
) -> None:
    from opensquilla.application import approval_queue as approval_queue_mod
    from opensquilla.gateway.approval_queue import get_approval_queue, reset_approval_queue

    monkeypatch.setattr(
        approval_queue_mod,
        "_DEFAULT_APPROVAL_QUEUE_PATH",
        tmp_path / "approval_queue.sqlite",
    )
    reset_approval_queue()
    get_approval_queue().set_settings(
        mode,
        allow_patterns=allow_patterns,
        deny_patterns=deny_patterns,
    )


def _allow_exact_elevation(monkeypatch, shell) -> list[object]:
    actions: list[object] = []

    def allow(action, **_kwargs):
        actions.append(action)
        return SimpleNamespace(allowed=True)

    monkeypatch.setattr(shell, "gate_elevated_action", allow)
    return actions


def test_windows_exec_command_uses_shell_host_wrapper(monkeypatch, tmp_path) -> None:
    from opensquilla.tools.builtin import shell

    runtime = _windows_runtime()

    monkeypatch.setattr(
        shell,
        "_trusted_windows_powershell_path",
        lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )

    argv = shell._sandbox_shell_backend_argv("Write-Output ok", runtime, cwd=tmp_path)

    assert argv[:3] == (
        sys.executable,
        "-c",
        shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
    )
    assert argv[3] == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert argv[4] == "Write-Output ok"
    assert argv[5] == str(tmp_path)
    assert argv[6] == str(tmp_path / ".opensquilla-cache" / "shell-host")


@pytest.mark.asyncio
async def test_windows_host_shell_runs_powershell_syntax_directly(monkeypatch) -> None:
    if os.name != "nt":
        pytest.skip("Windows host shell selection is Windows-only")

    from opensquilla.tools.builtin import shell

    calls: list[tuple[str, ...]] = []

    class _Process:
        returncode = 0

    async def fake_create_subprocess_exec(*argv, **_kwargs):
        calls.append(argv)
        return _Process()

    async def fail_create_subprocess_shell(*_args, **_kwargs):
        raise AssertionError("Windows host execution must not use cmd.exe")

    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(shell.asyncio, "create_subprocess_shell", fail_create_subprocess_shell)
    monkeypatch.setattr(shell, "_trusted_windows_powershell_path", lambda: "powershell.exe")

    await shell._create_host_shell_subprocess('Write-Output "ok"')

    assert calls
    assert calls[0][0] == "powershell.exe"
    expected_command = shell._windows_with_powershell_proxy_defaults('Write-Output "ok"')
    assert calls[0][-2:] == ("-Command", expected_command)


def test_windows_exec_command_unwraps_nested_powershell_command(monkeypatch, tmp_path) -> None:
    from opensquilla.tools.builtin import shell

    runtime = _windows_runtime()

    monkeypatch.setattr(
        shell,
        "_trusted_windows_powershell_path",
        lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )

    argv = shell._sandbox_shell_backend_argv(
        (
            "& 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe' "
            '-NoProfile -Command "Write-Output child-ok"'
        ),
        runtime,
        cwd=tmp_path,
    )

    assert argv[:3] == (
        sys.executable,
        "-c",
        shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
    )
    assert argv[3] == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert argv[4] == "Write-Output child-ok"

    direct_argv = shell._sandbox_shell_backend_argv(
        'powershell.exe -NoProfile -Command "Write-Output child-ok"',
        runtime,
        cwd=tmp_path,
    )

    assert direct_argv[4] == "Write-Output child-ok"


def test_windows_exec_command_prefers_cmd_package_manager_shims(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.tools.builtin import shell

    runtime = _windows_runtime()

    monkeypatch.setattr(
        shell,
        "_trusted_windows_powershell_path",
        lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    )

    argv = shell._sandbox_shell_backend_argv("npm view lodash version", runtime, cwd=tmp_path)

    assert argv[4] == "& 'npm.cmd' 'view' 'lodash' 'version'"


def test_windows_shell_host_skips_windowsapps_git_alias(
    monkeypatch,
    tmp_path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows command shims are Windows-only")

    from opensquilla.tools.builtin import shell

    alias_dir = tmp_path / "Microsoft" / "WindowsApps"
    real_dir = tmp_path / "Git" / "cmd"
    alias_dir.mkdir(parents=True)
    real_dir.mkdir(parents=True)
    (alias_dir / "git.cmd").write_text("@echo off\r\necho alias-git\r\n", encoding="utf-8")
    (real_dir / "git.cmd").write_text("@echo off\r\necho real-git %1\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", os.pathsep.join((str(alias_dir), str(real_dir))))

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
            "powershell.exe",
            "git --version",
            str(tmp_path),
            str(tmp_path / ".opensquilla-cache" / "shell-host"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "real-git --version" in completed.stdout
    assert "alias-git" not in completed.stdout


@pytest.mark.asyncio
async def test_auto_approve_mode_does_not_bypass_explicit_elevation(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    _configure_approval_queue(monkeypatch, tmp_path, "auto-approve")
    runtime = _windows_runtime()
    host_calls: list[dict[str, object]] = []

    async def fail_gate_action(**kwargs):
        raise AssertionError("auto host escalation should not enter sandbox gate")

    async def fake_host(command, **kwargs):
        host_calls.append({"command": command, **kwargs})
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fail_gate_action)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command("winget install Tencent.QQ.NT", workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    payload = json.loads(result)
    assert payload["status"] == "elevation_required"
    assert payload["reason"] == "software_management"
    assert host_calls == []


@pytest.mark.asyncio
async def test_trusted_mode_does_not_bypass_explicit_elevation(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    _configure_approval_queue(monkeypatch, tmp_path, "prompt")
    runtime = _windows_runtime()
    host_calls: list[dict[str, object]] = []

    async def fail_gate_action(**kwargs):
        raise AssertionError("trusted host-effect command should not enter sandbox gate")

    async def fake_host(command, **kwargs):
        host_calls.append({"command": command, **kwargs})
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fail_gate_action)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command("winget install Tencent.QQ.NT", workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    payload = json.loads(result)
    assert payload["status"] == "elevation_required"
    assert payload["reason"] == "software_management"
    assert host_calls == []


@pytest.mark.asyncio
async def test_powershell_file_installer_requires_and_uses_exact_elevation(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    script_path = tmp_path / "install_dingtalk.ps1"
    script_path.write_text(
        "\n".join(
            [
                "$installer = Join-Path $PSScriptRoot 'DingTalkSetup.exe'",
                (
                    "Invoke-WebRequest "
                    "https://dtapp-pub.dingtalk.com/desktop/Win/Release/DingTalkSetup.exe "
                    "-OutFile $installer"
                ),
                "Start-Process $installer -Wait",
            ]
        ),
        encoding="utf-8",
    )
    _configure_approval_queue(monkeypatch, tmp_path, "prompt")
    runtime = _windows_runtime()
    host_calls: list[dict[str, object]] = []

    async def fail_gate_action(**kwargs):
        raise AssertionError("trusted installer script should not enter sandbox gate")

    async def fake_host(command, **kwargs):
        host_calls.append({"command": command, **kwargs})
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fail_gate_action)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    actions = _allow_exact_elevation(monkeypatch, shell)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    command = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script_path}"'
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        default_result = await shell.exec_command(command, workdir=str(tmp_path))
        result = await shell.exec_command(
            command,
            workdir=str(tmp_path),
            sandbox_permissions="require_escalated",
            justification="Run the exact installer script requested by the user.",
        )
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    default_payload = json.loads(default_result)
    assert default_payload["status"] == "elevation_required"
    assert default_payload["reason"] == "software_management"
    assert "host-ok" in result
    assert [call["command"] for call in host_calls] == [command]
    assert len(actions) == 1
    assert actions[0].content_digest
    original_fingerprint = actions[0].fingerprint()
    script_path.write_text("Write-Output changed", encoding="utf-8")
    changed_action = shell._shell_elevation_action(
        tool_name="exec_command",
        action_kind="shell.exec",
        command=command,
        cwd=str(tmp_path),
        profile=shell._profile_shell_command(command, workdir=str(tmp_path)),
        justification="Run the exact installer script requested by the user.",
    )
    assert changed_action.fingerprint() != original_fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "Get-WmiObject Win32_Product | Where-Object { $_.Name -like '*DingTalk*' }",
        (
            "Get-ItemProperty HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* "
            "| Where-Object { $_.DisplayName -like '*DingTalk*' }"
        ),
        'Start-Process "C:\\Program Files\\DingTalk\\uninst.exe" -Wait',
        "Stop-Process -Name DingTalk -Force",
    ],
)
async def test_windows_uninstall_flow_commands_require_exact_elevation(
    monkeypatch,
    tmp_path,
    command,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    _configure_approval_queue(monkeypatch, tmp_path, "prompt")
    runtime = _windows_runtime()
    host_calls: list[str] = []

    async def fail_gate_action(**kwargs):
        raise AssertionError("trusted uninstall flow command should not enter sandbox gate")

    async def fake_host(command, **kwargs):
        host_calls.append(command)
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fail_gate_action)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    actions = _allow_exact_elevation(monkeypatch, shell)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        default_result = await shell.exec_command(command, workdir=str(tmp_path))
        result = await shell.exec_command(
            command,
            workdir=str(tmp_path),
            sandbox_permissions="require_escalated",
            justification="Run the exact uninstall operation requested by the user.",
        )
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    assert json.loads(default_result)["status"] == "elevation_required"
    assert "host-ok" in result
    assert host_calls == [command]
    assert len(actions) == 1


@pytest.mark.asyncio
async def test_trusted_host_shell_folds_windows_env_key_duplicates(
    monkeypatch,
    tmp_path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows environment variables are case-insensitive")

    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    _configure_approval_queue(monkeypatch, tmp_path, "prompt")
    runtime = _windows_runtime()
    host_envs: list[dict[str, str]] = []

    async def fake_host(command, **kwargs):
        host_envs.append(dict(kwargs["env"]))
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    _allow_exact_elevation(monkeypatch, shell)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command(
            "winget uninstall DingTalk",
            workdir=str(tmp_path),
            env={"SYSTEMROOT": r"C:\Windows"},
            sandbox_permissions="require_escalated",
            justification="Run the exact uninstall operation requested by the user.",
        )
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    assert "host-ok" in result
    assert host_envs
    systemroot_keys = [key for key in host_envs[0] if key.upper() == "SYSTEMROOT"]
    assert systemroot_keys == ["SystemRoot"]


@pytest.mark.asyncio
async def test_exact_elevation_adds_user_windowsapps_to_host_path(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    _configure_approval_queue(monkeypatch, tmp_path, "auto-approve")
    runtime = _windows_runtime()
    local_appdata = tmp_path / "Users" / "lrk" / "AppData" / "Local"
    windows_apps = local_appdata / "Microsoft" / "WindowsApps"
    windows_apps.mkdir(parents=True)
    (windows_apps / "winget.exe").write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    host_calls: list[dict[str, object]] = []

    async def fail_gate_action(**kwargs):
        raise AssertionError("auto host escalation should not enter sandbox gate")

    async def fake_host(command, **kwargs):
        host_calls.append({"command": command, **kwargs})
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fail_gate_action)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    _allow_exact_elevation(monkeypatch, shell)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command(
            "winget install Tencent.QQ.NT",
            workdir=str(tmp_path),
            sandbox_permissions="require_escalated",
            justification="Install the exact package requested by the user.",
        )
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    assert "host-ok" in result
    assert host_calls
    path_entries = host_calls[0]["env"]["PATH"].split(";")
    assert path_entries[-1] == str(windows_apps)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        "winget install Tencent.QQ.NT",
        'powershell -NoProfile -Command "winget install Tencent.QQ.NT"',
    ],
)
async def test_auto_host_escalation_honors_deny_patterns(
    monkeypatch,
    tmp_path,
    command,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    _configure_approval_queue(
        monkeypatch,
        tmp_path,
        "auto-approve",
        deny_patterns=["winget *"],
    )
    runtime = _windows_runtime()

    async def fail_gate_action(**kwargs):
        raise AssertionError("denied host-effect command should not enter sandbox gate")

    async def fail_host(command, **kwargs):
        raise AssertionError("denied host-effect command should not run on host")

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fail_gate_action)
    monkeypatch.setattr(shell, "_run_host_shell_command", fail_host)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command(command, workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    payload = json.loads(result)
    assert payload["status"] == "approval_denied"


@pytest.mark.asyncio
async def test_exact_elevation_background_process_runs_on_host_once(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    class FakeStdout:
        async def read(self, _size):
            return b""

    class FakeProcess:
        stdout = FakeStdout()
        returncode = 0

        async def wait(self):
            return 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    _configure_approval_queue(monkeypatch, tmp_path, "auto-approve")
    runtime = _windows_runtime()
    spawn_calls: list[dict[str, object]] = []

    async def fail_gate_action(**kwargs):
        raise AssertionError("auto host escalation should not enter sandbox gate")

    async def fake_create_subprocess_exec(*argv, **kwargs):
        spawn_calls.append({"argv": argv, **kwargs})
        return FakeProcess()

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fail_gate_action)
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    _allow_exact_elevation(monkeypatch, shell)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.background_process(
            "msiexec /i app.msi",
            workdir=str(tmp_path),
            sandbox_permissions="require_escalated",
            justification="Run the exact installer requested by the user.",
        )
        session_id = result.splitlines()[0].split("=", 1)[1]
        session = shell._bg_sessions[session_id]
        if session.collector_task is not None:
            await session.collector_task
    finally:
        current_tool_context.reset(token)
        shell._bg_sessions.clear()
        reset_approval_queue()

    assert [call["argv"] for call in spawn_calls] == [
        shell._windows_direct_powershell_argv("msiexec /i app.msi")
    ]


@pytest.mark.asyncio
async def test_background_host_effect_batch_requires_one_exact_elevation(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside-background.txt"
    command = f"msiexec /i app.msi; Set-Content -Path {outside} -Value hi"

    _configure_approval_queue(monkeypatch, tmp_path, "prompt")
    runtime = _windows_runtime()
    spawn_calls: list[tuple[str, ...]] = []

    class FakeStdout:
        async def read(self, _size):
            return b""

    class FakeProcess:
        stdout = FakeStdout()
        returncode = 0

        async def wait(self):
            return 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    async def fake_create_subprocess_exec(*argv, **_kwargs):
        spawn_calls.append(argv)
        return FakeProcess()

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    actions = _allow_exact_elevation(monkeypatch, shell)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        default_result = await shell.background_process(command, workdir=str(workspace))
        result = await shell.background_process(
            command,
            workdir=str(workspace),
            sandbox_permissions="require_escalated",
            justification="Run the exact installer and write the requested output.",
        )
        session_id = result.splitlines()[0].split("=", 1)[1]
        session = shell._bg_sessions[session_id]
        if session.collector_task is not None:
            await session.collector_task
    finally:
        current_tool_context.reset(token)
        shell._bg_sessions.clear()
        reset_approval_queue()

    payload = json.loads(default_result)
    assert payload["status"] == "elevation_required"
    assert spawn_calls == [shell._windows_direct_powershell_argv(command)]
    assert len(actions) == 1
    assert (str(outside.resolve(strict=False)), "write") in actions[0].target_paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "is_owner"),
    [
        ("prompt", False),
        ("auto-approve", False),
    ],
)
async def test_host_effect_command_requires_explicit_elevation_without_owner(
    monkeypatch,
    tmp_path,
    mode,
    is_owner,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.policy import build_policy
    from opensquilla.sandbox.types import SecurityLevel
    from opensquilla.tools.builtin import shell

    _configure_approval_queue(monkeypatch, tmp_path, mode)
    runtime = _windows_runtime()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="windows_default",
            network_default="none",
        ),
        trusted=True,
    )
    request = SimpleNamespace(
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        reason="",
        session_id="s1",
        run_mode="trusted",
    )
    backend_calls: list[object] = []
    host_calls: list[str] = []

    async def fake_gate_action(**kwargs):
        return object(), policy, request

    async def fake_preflight(*args, **kwargs):
        return None

    async def fake_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(returncode=0, stdout="sandbox-ok\n", stderr="", backend_notes=())

    async def fake_host(command, **kwargs):
        host_calls.append(command)
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fake_gate_action)
    monkeypatch.setattr(shell, "preflight_subprocess_managed_network", fake_preflight)
    monkeypatch.setattr(shell, "_run_backend_with_managed_network", fake_backend)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=is_owner,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command("winget list", workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    payload = json.loads(result)
    assert payload["status"] == "elevation_required"
    assert backend_calls == []
    assert host_calls == []


@pytest.mark.asyncio
async def test_standard_auto_approve_host_effect_requires_explicit_elevation(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.policy import build_policy
    from opensquilla.sandbox.types import SecurityLevel
    from opensquilla.tools.builtin import shell

    _configure_approval_queue(monkeypatch, tmp_path, "auto-approve")
    runtime = _windows_runtime()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="windows_default",
            network_default="none",
        ),
        trusted=False,
    )
    request = SimpleNamespace(
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        reason="",
        session_id="s1",
        run_mode="standard",
    )
    backend_calls: list[object] = []
    host_calls: list[str] = []

    async def fake_gate_action(**kwargs):
        return object(), policy, request

    async def fake_preflight(*args, **kwargs):
        return None

    async def fake_backend(request, *, runtime=None):
        backend_calls.append(request)
        return SimpleNamespace(returncode=0, stdout="sandbox-ok\n", stderr="", backend_notes=())

    async def fake_host(command, **kwargs):
        host_calls.append(command)
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fake_gate_action)
    monkeypatch.setattr(shell, "preflight_subprocess_managed_network", fake_preflight)
    monkeypatch.setattr(shell, "_run_backend_with_managed_network", fake_backend)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="standard",
        )
    )
    try:
        result = await shell.exec_command("winget list", workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    payload = json.loads(result)
    assert payload["status"] == "elevation_required"
    assert backend_calls == []
    assert host_calls == []


@pytest.mark.asyncio
async def test_exact_windows_host_probe_adds_user_windowsapps_to_host_env(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    _configure_approval_queue(monkeypatch, tmp_path, "prompt")
    runtime = _windows_runtime()
    local_appdata = tmp_path / "Users" / "lrk" / "AppData" / "Local"
    windows_apps = local_appdata / "Microsoft" / "WindowsApps"
    windows_apps.mkdir(parents=True)
    (windows_apps / "winget.exe").write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    host_calls: list[dict[str, object]] = []

    async def fake_gate_action(**kwargs):
        raise AssertionError("trusted host probe should not enter sandbox gate")

    async def fake_host(command, **kwargs):
        host_calls.append({"command": command, **kwargs})
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", fake_gate_action)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    _allow_exact_elevation(monkeypatch, shell)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command(
            "where winget",
            workdir=str(tmp_path),
            sandbox_permissions="require_escalated",
            justification="Locate the exact host executable requested by the user.",
        )
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    assert "host-ok" in result
    assert host_calls
    assert host_calls[0]["command"] == "where winget"
    assert host_calls[0]["env"]["PATH"].split(";")[-1] == str(windows_apps)


def test_windows_shell_host_blocks_icmp_diagnostics_when_proxy_allowlist(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows command shims are Windows-only")

    from opensquilla.tools.builtin import shell

    env = os.environ.copy()
    env["OPENSQUILLA_SANDBOX_NETWORK"] = "proxy_allowlist"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
            "powershell.exe",
            "ping",
            str(tmp_path),
            str(tmp_path / ".opensquilla-cache" / "shell-host"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode != 0
    assert "blocks ICMP" in completed.stderr


def test_windows_shell_host_fallback_sets_powershell_proxy_defaults(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        pytest.skip("PowerShell proxy defaults are Windows-only")

    from opensquilla.tools.builtin import shell

    proxy_url = "http://127.0.0.1:48123"
    env = os.environ.copy()
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    command = (
        "try { "
        "if ([System.Net.WebRequest]::DefaultWebProxy -and "
        "[System.Net.WebRequest]::DefaultWebProxy.Address) { "
        "Write-Output ([System.Net.WebRequest]::DefaultWebProxy.Address.AbsoluteUri) "
        "} else { Write-Output 'NO_PROXY' } "
        "} catch { Write-Output ('ERR:' + $_.Exception.Message) }"
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            shell._WINDOWS_SANDBOX_SHELL_HOST_CODE,
            "powershell.exe",
            command,
            str(tmp_path),
            str(tmp_path / ".opensquilla-cache" / "shell-host"),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert proxy_url in completed.stdout


@pytest.mark.asyncio
async def test_windows_exec_command_does_not_mount_program_files_tools_per_command(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.policy import build_policy
    from opensquilla.sandbox.types import SecurityLevel
    from opensquilla.tools.builtin import shell

    runtime = _windows_runtime()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="windows_default",
            network_default="none",
        ),
        trusted=True,
    )
    node_root = tmp_path / "Program Files" / "nodejs"
    git_root = tmp_path / "Program Files" / "Git"
    node_root.mkdir(parents=True)
    (git_root / "cmd").mkdir(parents=True)

    (node_root / "npm.cmd").write_text("@echo off\r\n", encoding="utf-8")
    (git_root / "cmd" / "git.exe").write_text("", encoding="utf-8")

    request = SimpleNamespace(
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        reason="",
        session_id="s1",
        run_mode="trusted",
    )

    async def _fake_gate_action(**kwargs):
        return object(), policy, request

    async def _fake_preflight(*args, **kwargs):
        return None

    backend_requests = []

    async def _fake_run_backend(request, *, runtime=None):
        backend_requests.append(request)
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="", backend_notes=())

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "preflight_subprocess_managed_network", _fake_preflight)
    monkeypatch.setattr(shell, "_run_backend_with_managed_network", _fake_run_backend)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command(
            (
                "npm view lodash version && "
                "git ls-remote https://github.com/opensquilla/opensquilla.git HEAD"
            ),
            workdir=str(tmp_path),
            env={"PATH": f"{node_root}{os.pathsep}{git_root / 'cmd'}"},
        )
    finally:
        current_tool_context.reset(token)

    assert "ok" in result
    assert backend_requests
    mount_paths = {mount.host_path for mount in backend_requests[0].policy.mounts}
    assert node_root not in mount_paths
    assert git_root not in mount_paths


@pytest.mark.asyncio
async def test_windows_exec_command_uses_shared_path_envelopes(monkeypatch, tmp_path) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.policy import build_policy
    from opensquilla.sandbox.types import SecurityLevel
    from opensquilla.tools.builtin import shell

    runtime = _windows_runtime()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="windows_default",
            network_default="none",
        ),
        trusted=True,
    )
    request = SimpleNamespace(
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        reason="",
        session_id="s1",
        run_mode="trusted",
    )

    async def _fake_gate_action(**kwargs):
        return object(), policy, request

    async def _fake_preflight(*args, **kwargs):
        return None

    backend_called = False

    async def _fake_run_backend(request, *, runtime=None):
        nonlocal backend_called
        backend_called = True
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="", backend_notes=())

    def _blocked_write_envelope(*args, **kwargs):
        return {
            "status": "blocked",
            "reason": "sandbox_path",
            "message": "shared path envelope blocked this command",
        }

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "preflight_subprocess_managed_network", _fake_preflight)
    monkeypatch.setattr(shell, "_run_backend_with_managed_network", _fake_run_backend)
    monkeypatch.setattr(shell, "_sandbox_write_path_access_envelope", _blocked_write_envelope)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command('where opensquilla 2>nul || echo "missing"')
    finally:
        current_tool_context.reset(token)

    assert "shared path envelope blocked this command" in result
    assert backend_called is False


@pytest.mark.asyncio
async def test_host_effect_exec_batch_requires_one_exact_elevation(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    command = f"where winget; Set-Content -Path {outside} -Value hi"

    _configure_approval_queue(monkeypatch, tmp_path, "prompt")
    runtime = _windows_runtime()
    host_calls: list[str] = []

    async def fake_host(command, **kwargs):
        host_calls.append(command)
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    actions = _allow_exact_elevation(monkeypatch, shell)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        default_result = await shell.exec_command(command, workdir=str(workspace))
        result = await shell.exec_command(
            command,
            workdir=str(workspace),
            sandbox_permissions="require_escalated",
            justification="Probe the host and write the exact requested file.",
        )
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    payload = json.loads(default_result)
    assert payload["status"] == "elevation_required"
    assert "host-ok" in result
    assert host_calls == [command]
    assert len(actions) == 1
    assert (str(outside.resolve(strict=False)), "write") in actions[0].target_paths


@pytest.mark.asyncio
async def test_auto_host_exec_batch_routes_protected_metadata_write_to_approval(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.gateway.approval_queue import reset_approval_queue
    from opensquilla.tools.builtin import shell

    workspace = tmp_path / "workspace"
    protected = workspace / ".git"
    protected.mkdir(parents=True)
    target = protected / "config"
    command = f"where winget; Set-Content -Path {target} -Value hi"

    _configure_approval_queue(monkeypatch, tmp_path, "prompt")
    runtime = _windows_runtime()
    host_calls: list[str] = []

    async def fake_host(command, **kwargs):
        host_calls.append(command)
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "_run_host_shell_command", fake_host)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        default_result = await shell.exec_command(command, workdir=str(workspace))
        result = await shell.exec_command(
            command,
            workdir=str(workspace),
            sandbox_permissions="require_escalated",
            justification="Probe the host and update the requested metadata.",
        )
    finally:
        current_tool_context.reset(token)
        reset_approval_queue()

    default_payload = json.loads(default_result)
    approval_payload = json.loads(result)
    assert default_payload["status"] == "elevation_required"
    assert default_payload["reason"] == "host_probe"
    assert approval_payload["status"] == "approval_required"
    assert approval_payload["approval_id"]
    assert host_calls == []


@pytest.mark.asyncio
async def test_windows_exec_command_merges_shell_active_mounts(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.policy import build_policy
    from opensquilla.sandbox.types import SecurityLevel
    from opensquilla.tools.builtin import shell

    runtime = _windows_runtime()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="windows_default",
            network_default="none",
        ),
        trusted=True,
    )
    request = SimpleNamespace(
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        reason="",
        session_id="s1",
        run_mode="trusted",
    )

    async def _fake_gate_action(**kwargs):
        return object(), policy, request

    async def _fake_preflight(*args, **kwargs):
        return None

    backend_requests = []

    async def _fake_run_backend(request, *, runtime=None):
        backend_requests.append(request)
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="", backend_notes=())

    mounted = tmp_path / "external"
    mounted.mkdir()

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "preflight_subprocess_managed_network", _fake_preflight)
    monkeypatch.setattr(shell, "_run_backend_with_managed_network", _fake_run_backend)
    monkeypatch.setattr(
        shell,
        "_active_sandbox_mounts",
        lambda: [{"path": str(mounted), "access": "rw"}],
    )
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command("Write-Output ok")
    finally:
        current_tool_context.reset(token)

    assert "ok" in result
    assert backend_requests
    assert any(mount.host_path == mounted for mount in backend_requests[0].policy.mounts)


@pytest.mark.asyncio
async def test_windows_exec_command_blocks_runtime_readonly_write_target(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.sandbox.config import SandboxSettings
    from opensquilla.sandbox.policy import build_policy
    from opensquilla.sandbox.types import SecurityLevel
    from opensquilla.tools.builtin import shell

    runtime = _windows_runtime()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        tmp_path,
        SandboxSettings(
            sandbox=True,
            security_grading=True,
            backend="windows_default",
            network_default="none",
        ),
        trusted=True,
    )
    request = SimpleNamespace(
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        reason="",
        session_id="s1",
        run_mode="trusted",
    )
    runtime_root = tmp_path / "runtime-src"
    runtime_root.mkdir()
    target = runtime_root / "__write_should_not_happen__.txt"

    async def _fake_gate_action(**kwargs):
        return object(), policy, request

    async def _fake_preflight(*args, **kwargs):
        return None

    async def _fake_run_backend(request, *, runtime=None):
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="", backend_notes=())

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "gate_action", _fake_gate_action)
    monkeypatch.setattr(shell, "preflight_subprocess_managed_network", _fake_preflight)
    monkeypatch.setattr(shell, "_run_backend_with_managed_network", _fake_run_backend)
    monkeypatch.setattr(shell, "_windows_runtime_readonly_roots", lambda: (runtime_root,))
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="trusted",
        )
    )
    try:
        result = await shell.exec_command(f'echo "test" > "{target}"')
    finally:
        current_tool_context.reset(token)

    payload = json.loads(result)
    assert payload["reason"] == "runtime_readonly"
    assert payload["resolved_path"] == str(target)


def test_shell_blocks_runtime_python_environment_bootstrap(monkeypatch, tmp_path) -> None:
    from opensquilla.tools.builtin import shell

    runtime_root = tmp_path / "runtime-venv"
    runtime_root.mkdir()

    monkeypatch.setattr(shell, "_runtime_readonly_roots", lambda runtime=None: (runtime_root,))
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)

    payload = shell._runtime_readonly_shell_block(
        "exec_command",
        "python -m ensurepip --upgrade",
        str(tmp_path),
        runtime=SimpleNamespace(backend=SimpleNamespace(name="bubblewrap")),
    )

    assert payload is not None
    assert payload["reason"] == "runtime_readonly"
    assert payload["runtime_operation"] == "python -m ensurepip"
    assert payload["readonly_root"] == str(runtime_root)


def test_shell_blocks_runtime_python_package_install(monkeypatch, tmp_path) -> None:
    from opensquilla.tools.builtin import shell

    runtime_root = tmp_path / "runtime-venv"
    runtime_python = runtime_root / "bin" / "python"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(shell, "_runtime_readonly_roots", lambda runtime=None: (runtime_root,))
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)

    payload = shell._runtime_readonly_shell_block(
        "exec_command",
        f"{runtime_python} -m pip install requests",
        str(tmp_path),
        runtime=SimpleNamespace(backend=SimpleNamespace(name="bubblewrap")),
    )

    assert payload is not None
    assert payload["reason"] == "runtime_readonly"
    assert payload["runtime_operation"] == "python -m pip install"


def test_shell_allows_explicit_project_venv_package_install(monkeypatch, tmp_path) -> None:
    from opensquilla.tools.builtin import shell

    runtime_root = tmp_path / "runtime-venv"
    project_python = tmp_path / "project" / ".venv" / "bin" / "python"
    project_python.parent.mkdir(parents=True)
    project_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(shell, "_runtime_readonly_roots", lambda runtime=None: (runtime_root,))
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)

    payload = shell._runtime_readonly_shell_block(
        "exec_command",
        f"{project_python} -m pip install requests",
        str(tmp_path),
        runtime=SimpleNamespace(backend=SimpleNamespace(name="bubblewrap")),
    )

    assert payload is None


def test_shell_allows_explicit_project_venv_ensurepip(monkeypatch, tmp_path) -> None:
    from opensquilla.tools.builtin import shell

    runtime_root = tmp_path / "runtime-venv"
    project_python = tmp_path / "project" / ".venv" / "bin" / "python"
    project_python.parent.mkdir(parents=True)
    project_python.write_text("", encoding="utf-8")

    monkeypatch.setattr(shell, "_runtime_readonly_roots", lambda runtime=None: (runtime_root,))
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)

    payload = shell._runtime_readonly_shell_block(
        "exec_command",
        f"{project_python} -m ensurepip --upgrade",
        str(tmp_path),
        runtime=SimpleNamespace(backend=SimpleNamespace(name="bubblewrap")),
    )

    assert payload is None


def test_shell_blocks_windows_runtime_python_environment_bootstrap(monkeypatch) -> None:
    from opensquilla.tools.builtin import shell

    runtime_root = Path(r"X:\workspace\.venv")

    monkeypatch.setattr(shell, "_runtime_readonly_roots", lambda runtime=None: (runtime_root,))
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)

    payload = shell._runtime_readonly_shell_block(
        "exec_command",
        r"X:\workspace\.venv\Scripts\python.exe -m ensurepip --upgrade",
        r"X:\workspace",
        runtime=SimpleNamespace(backend=SimpleNamespace(name="windows_default")),
    )

    assert payload is not None
    assert payload["reason"] == "runtime_readonly"
    assert payload["runtime_operation"] == "python -m ensurepip"
    assert payload["readonly_root"] == str(runtime_root)


def test_shell_allows_windows_project_venv_ensurepip(monkeypatch) -> None:
    from opensquilla.tools.builtin import shell

    runtime_root = Path(r"X:\workspace\.venv")

    monkeypatch.setattr(shell, "_runtime_readonly_roots", lambda runtime=None: (runtime_root,))
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)

    payload = shell._runtime_readonly_shell_block(
        "exec_command",
        r"X:\workspace\.tmp\proj\.venv\Scripts\python.exe -m ensurepip --upgrade",
        r"X:\workspace",
        runtime=SimpleNamespace(backend=SimpleNamespace(name="windows_default")),
    )

    assert payload is None


@pytest.mark.asyncio
async def test_windows_exec_command_full_host_access_skips_runtime_readonly_block(
    monkeypatch,
    tmp_path,
) -> None:
    from opensquilla.tools.builtin import shell

    runtime = _windows_runtime()
    runtime_root = tmp_path / "runtime-src"
    runtime_root.mkdir()
    target = runtime_root / "__full_host_should_reach_host__.txt"
    host_calls = []

    async def _fake_run_host_shell_command(*args, **kwargs):
        host_calls.append((args, kwargs))
        return "exit_code=0\nhost-ok\n"

    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "_windows_runtime_readonly_roots", lambda: (runtime_root,))
    monkeypatch.setattr(shell, "_run_host_shell_command", _fake_run_host_shell_command)
    monkeypatch.setattr(
        shell,
        "check_safe_bin",
        lambda command: SimpleNamespace(allowed=True, needs_approval=False, reason=""),
    )

    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(tmp_path),
            session_key="s1",
            run_mode="full",
        )
    )
    try:
        result = await shell.exec_command(f'echo "test" > "{target}"')
    finally:
        current_tool_context.reset(token)

    assert "host-ok" in result
    assert host_calls
