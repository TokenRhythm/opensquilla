from __future__ import annotations

import json
import os
import shlex
import subprocess
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.execution_status import execution_status_for_tool_result
from opensquilla.sandbox.backend import windows_default
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import ToolContext, current_tool_context


@pytest.mark.parametrize("full_access", [False, True])
@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("guest", [False, True])
async def test_missing_declared_runtime_returns_actionable_result_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    full_access: bool,
    background: bool,
    guest: bool,
) -> None:
    executed: list[str] = []

    async def run(command: str, **_kwargs: object) -> str:
        executed.append(command)
        return "exit_code=127\nnode: command not found\n"

    async def spawn(command: str, **_kwargs: object) -> None:
        executed.append(command)
        raise AssertionError("a missing runtime must not start a background process")

    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: full_access)
    monkeypatch.setattr(shell, "_base_shell_environment", lambda: {"PATH": str(tmp_path)})
    monkeypatch.setattr(shell, "_runtime_shell_environment", lambda env, **_kw: env)
    monkeypatch.setattr(shell, "_managed_skill_environment", lambda env: env)
    monkeypatch.setattr(shell, "_run_host_shell_command", run)
    monkeypatch.setattr(shell, "_create_host_shell_subprocess", spawn)
    token = current_tool_context.set(
        ToolContext(is_owner=True, guest_safe=guest, workspace_dir=str(tmp_path))
    )
    try:
        tool = shell.background_process if background else shell.exec_command
        result = await tool("node app.js", workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)

    assert executed == []
    payload = json.loads(result)
    assert payload["code"] == "RUNTIME_UNAVAILABLE"
    assert payload["componentId"] == "node"
    assert payload["retryable"] is False
    assert "text" in payload["recovery"].lower()
    assert str(tmp_path) not in result


@pytest.mark.parametrize("windows_backend", [False, True])
@pytest.mark.parametrize("background", [False, True])
async def test_missing_runtime_does_not_request_elevation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    windows_backend: bool,
    background: bool,
) -> None:
    runtime = (
        SimpleNamespace(
            backend=SimpleNamespace(name="windows_default"),
            effective=SimpleNamespace(sandbox_enabled=True),
        )
        if windows_backend else None
    )
    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)
    monkeypatch.setattr(windows_default, "_common_windows_tool_dirs", lambda _env: ())
    monkeypatch.setattr(shell, "_base_shell_environment", lambda: {"PATH": str(tmp_path)})
    monkeypatch.setattr(shell, "_runtime_shell_environment", lambda env, **_kw: env)
    monkeypatch.setattr(shell, "_managed_skill_environment", lambda env: env)

    def no_elevation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("missing runtime is not an elevation failure")

    monkeypatch.setattr(shell, "_gate_shell_elevation", no_elevation)
    token = current_tool_context.set(ToolContext(is_owner=True, workspace_dir=str(tmp_path)))
    try:
        tool = shell.background_process if background else shell.exec_command
        result = await tool(
            "node app.js", workdir=str(tmp_path), sandbox_permissions="require_escalated"
        )
    finally:
        current_tool_context.reset(token)

    assert json.loads(result)["code"] == "RUNTIME_UNAVAILABLE"


@pytest.mark.parametrize("command", ["node app.js", "npm test", "python3 -c 'pass'"])
def test_ordinary_preflight_classifies_missing_runtime(
    command: str, tmp_path: Path
) -> None:
    result = shell._runtime_unavailable_envelope(command, {"PATH": str(tmp_path)})
    assert result is not None
    assert result["code"] == "RUNTIME_UNAVAILABLE"
    assert result["retryable"] is False


@pytest.mark.parametrize("command", ["command node app.js", "exec node app.js", "env node app.js"])
def test_simple_wrappers_keep_missing_runtime_classification(command: str, tmp_path: Path) -> None:
    result = shell._runtime_unavailable_envelope(
        command, {"PATH": str(tmp_path)}, windows=False
    )
    assert result is not None and result["componentId"] == "node"


@pytest.mark.parametrize(
    "command",
    [
        "echo ready",
        "unknown_program app.js",
        "/custom/node app.js",
        "PATH=/custom node app.js",
        "env PATH=/custom node app.js",
        "source setup.sh && node app.js",
        "sh -lc 'node app.js'",
        "node $(prepare-runtime)",
        "node `prepare-runtime`",
        'node "${PATH:=./runtime}"',
        "command -v node",
    ],
)
def test_unknown_or_setup_commands_are_left_to_shell(command: str, tmp_path: Path) -> None:
    assert shell._runtime_unavailable_envelope(
        command, {"PATH": str(tmp_path)}, windows=False
    ) is None


@pytest.mark.parametrize("windows", [False, True])
def test_relative_path_resolves_against_child_workdir(tmp_path: Path, windows: bool) -> None:
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / ("node.EXE" if windows else "node")
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    assert shell._runtime_unavailable_envelope(
        "node app.js", {"PATH": "absent;bin" if windows else "absent:bin", "PATHEXT": ".EXE"},
        cwd=str(tmp_path), windows=windows,
    ) is None


def test_unset_path_defers_to_shell_default() -> None:
    assert shell._runtime_unavailable_envelope("node app.js", {}, windows=False) is None


def test_windows_uses_child_path_and_pathext(tmp_path: Path) -> None:
    (tmp_path / "node.EXE").write_bytes(b"synthetic executable")
    environment = {"Path": str(tmp_path), "PathExt": ".EXE;.CMD"}
    assert shell._runtime_unavailable_envelope(
        "node app.js", environment, windows=True
    ) is None
    result = shell._runtime_unavailable_envelope(
        "node app.js", {"Path": str(tmp_path), "PATHEXT": ".CMD"}, windows=True
    )
    assert result is not None and result["code"] == "RUNTIME_UNAVAILABLE"


def test_windows_does_not_search_workdir_implicitly(tmp_path: Path) -> None:
    (tmp_path / "node.EXE").write_bytes(b"synthetic executable")
    result = shell._runtime_unavailable_envelope(
        "node app.js", {"PATH": ""}, cwd=str(tmp_path), windows=True
    )
    assert result is not None and result["code"] == "RUNTIME_UNAVAILABLE"


def test_windows_powershell_script_is_available(tmp_path: Path) -> None:
    (tmp_path / "node.ps1").write_text("Write-Output ready", encoding="utf-8")
    assert shell._runtime_unavailable_envelope(
        "node app.js", {"PATH": str(tmp_path)}, windows=True
    ) is None


@pytest.mark.parametrize("command", ["npm", "npx"])
@pytest.mark.parametrize("extension", [".cmd", ".exe"])
def test_windows_sandbox_preserves_supplied_python_function_and_runtime_shims(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str, extension: str
) -> None:
    monkeypatch.setattr(shell, "_windows_sandbox_backend_active", lambda _runtime: True)
    monkeypatch.setattr(windows_default, "_common_windows_tool_dirs", lambda _env: ())
    kwargs = {"cwd": str(tmp_path), "runtime": object(), "host_execution": False}
    assert shell._shell_runtime_preflight("python -V", {"PATH": ""}, **kwargs) is None
    result = shell._shell_runtime_preflight(f"{command} test", {"PATH": str(tmp_path)}, **kwargs)
    assert result is not None and result["code"] == "RUNTIME_UNAVAILABLE"
    (tmp_path / f"{command}{extension}").write_bytes(b"synthetic executable")
    assert shell._shell_runtime_preflight(
        f"{command} test", {"PATH": str(tmp_path), "PATHEXT": ".BAT"}, **kwargs
    ) is None


def test_exported_runtime_function_defers_to_shell(tmp_path: Path) -> None:
    assert shell._runtime_unavailable_envelope(
        "node --version",
        {"PATH": str(tmp_path), "BASH_FUNC_node%%": "() { echo synthetic-function; }"},
        windows=False,
    ) is None


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize(
    ("command", "filename", "discovered_path"),
    [
        ("node", "node.exe", False),
        ("git", "git.exe", False),
        ("git", "git.cmd", False),
        ("npm", "npm.exe", True),
    ],
)
async def test_windows_sandbox_preflight_uses_backend_path_and_executable_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    background: bool, command: str, filename: str, discovered_path: bool,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / filename).write_bytes(b"synthetic executable")
    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        backend=SimpleNamespace(name="windows_default"),
    )
    environment = {"PATH": "" if discovered_path else str(runtime_dir), "PATHEXT": ".BAT"}
    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)
    monkeypatch.setattr(shell, "_base_shell_environment", lambda: dict(environment))
    monkeypatch.setattr(shell, "_runtime_shell_environment", lambda env, **_kw: env)
    monkeypatch.setattr(shell, "_managed_skill_environment", lambda env: env)
    monkeypatch.setattr(
        shell, "_runtime_unavailable_envelope",
        partial(shell._runtime_unavailable_envelope, windows=True),
    )
    monkeypatch.setattr(
        windows_default, "_common_windows_tool_dirs", lambda _env: (runtime_dir,),
    )
    monkeypatch.setattr(
        shell, "_sandbox_shell_backend_argv", lambda command, _runtime, **_kw: (command,),
    )
    observed: list[object] = []

    async def gate(**kwargs: object) -> tuple[object, object, object]:
        policy = SimpleNamespace(network=None, env_allowlist=("PATH", "PATHEXT"))
        request = SimpleNamespace(cwd=tmp_path, action_kind=kwargs["action_kind"], policy=policy)
        return object(), policy, request

    async def no_network(*_args: object, **_kwargs: object) -> None:
        return None

    async def prepare(request: object, **_kwargs: object) -> object:
        return SimpleNamespace(request=request, cleanup=no_network)

    async def run(request: object, **_kwargs: object) -> object:
        observed.append(request)
        raise RuntimeError("synthetic backend reached")

    async def spawn(*, request: object, **_kwargs: object) -> object:
        return await run(request)

    monkeypatch.setattr(shell, "gate_action", gate)
    monkeypatch.setattr(shell, "preflight_subprocess_managed_network", no_network)
    monkeypatch.setattr(shell, "prepare_subprocess_managed_network_proxy", prepare)
    monkeypatch.setattr(shell, "_run_backend_with_managed_network", run)
    monkeypatch.setattr(shell, "_spawn_sandboxed_background_process", spawn)
    token = current_tool_context.set(ToolContext(is_owner=True, workspace_dir=str(tmp_path)))
    try:
        tool = shell.background_process if background else shell.exec_command
        with pytest.raises(Exception, match="synthetic backend reached"):
            await tool(f"{command} --version", workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)

    assert len(observed) == 1


@pytest.mark.parametrize("guest", [False, True])
def test_windows_explicit_empty_path_preserves_backend_host_path_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, guest: bool,
) -> None:
    (tmp_path / "node.exe").write_bytes(b"synthetic executable")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(
        windows_default, "_common_windows_tool_dirs", lambda _env: (tmp_path,) if guest else (),
    )
    environment = {"PATH": "", "OPENSQUILLA_GUEST_SAFE": "1" if guest else "0"}
    runtime = SimpleNamespace(backend=SimpleNamespace(name="windows_default"))
    request = SimpleNamespace(
        env=environment, action_kind="shell.exec",
        policy=SimpleNamespace(env_allowlist=("PATH",)),
    )

    assert windows_default._process_base_env(request)["PATH"] == ""
    result = shell._shell_runtime_preflight(
        "node --version", environment, cwd=str(tmp_path), runtime=runtime, host_execution=False,
    )
    assert result is not None and result["code"] == "RUNTIME_UNAVAILABLE"
    assert environment["PATH"] == ""


@pytest.mark.parametrize("command", ["node", "git"])
def test_windows_sandbox_keeps_powershell_script_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str,
) -> None:
    (tmp_path / f"{command}.ps1").write_text("Write-Output ready", encoding="utf-8")
    monkeypatch.setattr(windows_default, "_common_windows_tool_dirs", lambda _env: ())
    runtime = SimpleNamespace(backend=SimpleNamespace(name="windows_default"))
    assert shell._shell_runtime_preflight(
        f"{command} --version", {"PATH": str(tmp_path)},
        cwd=str(tmp_path), runtime=runtime, host_execution=False,
    ) is None


def test_windows_backend_runtime_check_respects_final_environment_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    (tmp_path / "node.exe").write_bytes(b"synthetic executable")
    monkeypatch.setattr(windows_default, "_common_windows_tool_dirs", lambda _env: ())
    environment = {"PATH": str(tmp_path)}
    runtime = SimpleNamespace(backend=SimpleNamespace(name="windows_default"))
    assert shell._shell_runtime_preflight(
        "node --version", environment, cwd=str(tmp_path), runtime=runtime, host_execution=False,
    ) is None
    request = SimpleNamespace(
        env=environment, cwd=tmp_path, action_kind="shell.exec",
        policy=SimpleNamespace(env_allowlist=()),
    )
    result = shell._windows_backend_runtime_preflight("node --version", request, runtime)
    assert result is not None and result["code"] == "RUNTIME_UNAVAILABLE"


@pytest.mark.skipif(os.name == "nt", reason="POSIX Bash function import")
async def test_full_host_exec_preserves_exported_runtime_function(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if not Path("/bin/bash").is_file():
        pytest.skip("Bash is unavailable")
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: True)
    monkeypatch.setattr(shell, "_base_shell_environment", lambda: {"PATH": str(tmp_path)})
    monkeypatch.setattr(shell, "_runtime_shell_environment", lambda env, **_kw: env)
    monkeypatch.setattr(shell, "_managed_skill_environment", lambda env: env)

    async def run(command: str, **kwargs: object) -> object:
        # Select Bash explicitly so Linux's /bin/sh choice cannot change this
        # test's function-import semantics. Production keeps its existing shell.
        return await shell.create_owned_subprocess_shell(
            command, executable="/bin/bash", **kwargs
        )

    monkeypatch.setattr(shell, "_create_host_shell_subprocess", run)
    result = await shell.exec_command(
        "node --version", workdir=str(tmp_path),
        env={"BASH_FUNC_node%%": "() { echo synthetic-function; }"},
    )
    assert result == "exit_code=0\nsynthetic-function\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell parameter assignment")
async def test_full_host_exec_preserves_parameter_assignment_resolution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir()
    executable = runtime_directory / "node"
    executable.write_text("#!/bin/sh\nprintf 'synthetic-runtime\\n'\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: True)
    monkeypatch.setattr(shell, "_base_shell_environment", lambda: {"PATH": ""})
    monkeypatch.setattr(shell, "_runtime_shell_environment", lambda env, **_kw: env)
    monkeypatch.setattr(shell, "_managed_skill_environment", lambda env: env)

    result = await shell.exec_command(
        'node "${PATH:=./runtime}"', workdir=str(tmp_path),
    )
    assert result == "exit_code=0\nsynthetic-runtime\n"


async def test_explicit_environment_repair_permits_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    for name in ("node", "node.EXE"):
        executable = runtime_dir / name
        executable.write_text("synthetic runtime", encoding="utf-8")
        executable.chmod(0o755)
    calls: list[dict[str, object]] = []

    async def run(_command: str, **kwargs: object) -> str:
        calls.append(kwargs)
        return "exit_code=0\nvalidated\n"

    monkeypatch.setattr(shell, "full_host_access_active", lambda: True)
    monkeypatch.setattr(shell, "_base_shell_environment", lambda: {"PATH": str(tmp_path)})
    monkeypatch.setattr(shell, "_runtime_shell_environment", lambda env, **_kw: env)
    monkeypatch.setattr(shell, "_managed_skill_environment", lambda env: env)
    monkeypatch.setattr(shell, "_run_host_shell_command", run)
    missing = await shell.exec_command("node app.js", workdir=str(tmp_path))
    repaired = await shell.exec_command(
        "node app.js", workdir=str(tmp_path), env={"PATH": str(runtime_dir)}
    )
    assert json.loads(missing)["code"] == "RUNTIME_UNAVAILABLE"
    assert repaired == "exit_code=0\nvalidated\n"
    assert len(calls) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX login-shell behavior")
def test_sandbox_guard_uses_path_after_shell_startup(tmp_path: Path) -> None:
    runtime = SimpleNamespace(backend=SimpleNamespace(name="noop"))
    argv = shell._sandbox_shell_backend_argv("node app.js", runtime)
    assert argv[:2] == ("sh", "-lc")
    assert shell._uses_posix_login_shell(
        SimpleNamespace(effective=SimpleNamespace(sandbox_enabled=True), backend=runtime.backend),
        host_execution=False,
    )
    executable = tmp_path / "node"
    executable.write_text("#!/bin/sh\nprintf 'validated\\n'\n", encoding="utf-8")
    executable.chmod(0o755)

    # Model a login profile that replaces an initially empty PATH before the
    # command argument is interpreted, without relying on machine profiles.
    result = subprocess.run(
        ["/bin/sh", "-c", f"PATH={shlex.quote(str(tmp_path))}\n{argv[-1]}"],
        env={"PATH": ""}, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "validated\n"

    executable.unlink()
    missing = subprocess.run(
        ["/bin/sh", "-c", argv[-1]],
        env={"PATH": str(tmp_path)}, capture_output=True, text=True, check=False,
    )
    payload = shell._runtime_failure_from_shell_output(
        "node app.js", missing.stdout, missing.returncode
    )
    assert payload is not None and payload["code"] == "RUNTIME_UNAVAILABLE"


@pytest.mark.skipif(os.name == "nt", reason="POSIX sandbox shell guard")
async def test_sandbox_runtime_failure_precedes_denial_escalation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = SimpleNamespace(
        effective=SimpleNamespace(sandbox_enabled=True),
        backend=SimpleNamespace(name="noop"),
    )
    monkeypatch.setattr(shell, "get_runtime", lambda: runtime)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)
    monkeypatch.setattr(shell, "_base_shell_environment", lambda: {"PATH": str(tmp_path)})
    monkeypatch.setattr(shell, "_runtime_shell_environment", lambda env, **_kw: env)
    monkeypatch.setattr(shell, "_managed_skill_environment", lambda env: env)
    observed: list[str] = []

    async def gate(**_kwargs: object) -> tuple[object, object, object]:
        policy = SimpleNamespace(network=None)
        request = SimpleNamespace(cwd=tmp_path, action_kind="shell.exec", policy=policy)
        return object(), policy, request

    async def run(request: object, **_kwargs: object) -> object:
        observed.append(request.argv[-1])  # type: ignore[attr-defined]
        return SimpleNamespace(
            returncode=127, stdout=f"{shell._RUNTIME_UNAVAILABLE_MARKER}\n", stderr=""
        )

    def no_escalation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("runtime failure is not a sandbox denial")

    monkeypatch.setattr(shell, "gate_action", gate)
    monkeypatch.setattr(shell, "_run_backend_with_managed_network", run)
    monkeypatch.setattr(shell, "is_likely_sandbox_denied", no_escalation)
    token = current_tool_context.set(ToolContext(is_owner=True, workspace_dir=str(tmp_path)))
    try:
        result = await shell.exec_command("node app.js", workdir=str(tmp_path))
    finally:
        current_tool_context.reset(token)

    assert len(observed) == 1
    assert "command -v node" in observed[0]
    assert json.loads(result)["code"] == "RUNTIME_UNAVAILABLE"


async def test_background_poll_exposes_runtime_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    session = shell._BgSession(
        session_id="synthetic-runtime-session",
        command="node app.js",
        process=SimpleNamespace(returncode=127),  # type: ignore[arg-type]
        returncode=127,
        done=True,
        output_lines=[f"{shell._RUNTIME_UNAVAILABLE_MARKER}\n"],
    )
    monkeypatch.setattr(shell, "_require_bg_session", lambda _session_id: session)
    result = await shell.process(action="poll", session_id=session.session_id)
    payload = json.loads(result)
    assert payload["session"]["runtime_failure"]["code"] == "RUNTIME_UNAVAILABLE"
    status = execution_status_for_tool_result("process", result)
    assert status is not None and status["status"] == "error"
    assert status["reason"] == "runtime_unavailable"
