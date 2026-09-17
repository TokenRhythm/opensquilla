from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.execution_status import execution_status_for_tool_result
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


async def test_missing_runtime_does_not_request_elevation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(shell, "get_runtime", lambda: None)
    monkeypatch.setattr(shell, "full_host_access_active", lambda: False)
    monkeypatch.setattr(shell, "_base_shell_environment", lambda: {"PATH": str(tmp_path)})
    monkeypatch.setattr(shell, "_runtime_shell_environment", lambda env, **_kw: env)
    monkeypatch.setattr(shell, "_managed_skill_environment", lambda env: env)

    def no_elevation(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("missing runtime is not an elevation failure")

    monkeypatch.setattr(shell, "_gate_shell_elevation", no_elevation)
    token = current_tool_context.set(ToolContext(is_owner=True, workspace_dir=str(tmp_path)))
    try:
        result = await shell.exec_command(
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
        "command -v node",
    ],
)
def test_unknown_or_setup_commands_are_left_to_shell(command: str, tmp_path: Path) -> None:
    assert shell._runtime_unavailable_envelope(
        command, {"PATH": str(tmp_path)}, windows=False
    ) is None


def test_relative_path_resolves_against_child_workdir(tmp_path: Path) -> None:
    directory = tmp_path / "bin"
    directory.mkdir()
    executable = directory / "node"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    assert shell._runtime_unavailable_envelope(
        "node app.js", {"PATH": "bin"}, cwd=str(tmp_path), windows=False
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


def test_windows_sandbox_preserves_supplied_python_function_and_npm_shim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(shell, "_windows_sandbox_backend_active", lambda _runtime: True)
    kwargs = {"cwd": str(tmp_path), "runtime": object(), "host_execution": False}
    assert shell._shell_runtime_preflight("python -V", {"PATH": ""}, **kwargs) is None
    (tmp_path / "npm.EXE").write_bytes(b"synthetic executable")
    result = shell._shell_runtime_preflight("npm test", {"PATH": str(tmp_path)}, **kwargs)
    assert result is not None and result["executable"] == "npm.cmd"
    (tmp_path / "npm.cmd").write_text("@echo ready", encoding="utf-8")
    assert shell._shell_runtime_preflight("npm test", {"PATH": str(tmp_path)}, **kwargs) is None


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
