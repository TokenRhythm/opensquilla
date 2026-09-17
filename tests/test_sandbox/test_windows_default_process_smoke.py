from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.file_policy import compile_web_guest_file_profile
from opensquilla.sandbox.policy import build_policy
from opensquilla.sandbox.policy_models import SandboxPolicy as StoredSandboxPolicy
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import (
    NetworkMode,
    ResourceLimits,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)
from opensquilla.tools.types import ToolContext, current_tool_context

pytestmark = pytest.mark.skipif(
    sys.platform != "win32"
    or os.environ.get("OPENSQUILLA_RUN_WINDOWS_SANDBOX_SMOKE") != "1",
    reason="Windows sandbox native smoke tests require explicit opt-in",
)


def _policy() -> SandboxPolicy:
    return SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        # Cold Windows ACL projection can take tens of seconds on developer
        # machines with large runtime trees. The native smoke is checking
        # correctness, not an interactive latency budget.
        limits=ResourceLimits(wall_timeout_s=90),
        env_allowlist=(
            "PATH",
            "SystemRoot",
            "WINDIR",
            "ComSpec",
            "TEMP",
            "TMP",
            "ProgramData",
            "ProgramFiles",
            "ProgramFiles(x86)",
        ),
        require_approval=False,
    )


def _request(
    tmp_path: Path, argv: tuple[str, ...], stdin: bytes | None = None
) -> SandboxRequest:
    policy = replace(
        _policy(),
        file_system=build_policy(
            SecurityLevel.STANDARD,
            "shell.exec",
            tmp_path,
            SandboxSettings(),
        ).file_system,
    )
    return SandboxRequest(
        argv=argv,
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=policy,
        stdin=stdin,
        env=dict(os.environ),
        run_mode=RunMode.SAFE.value,
    )


@pytest.fixture(autouse=True)
def _use_installed_windows_sandbox(
    _isolate_opensquilla_state,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del _isolate_opensquilla_state
    monkeypatch.delenv("OPENSQUILLA_STATE_DIR", raising=False)


@pytest.mark.asyncio
async def test_windows_default_runs_powershell_write_output(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend.windows_default import WindowsDefaultBackend

    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    result = await WindowsDefaultBackend().run(
        _request(
            tmp_path,
            (
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "Write-Output ok",
            ),
        )
    )

    assert result.returncode == 0
    assert "ok" in result.stdout


@pytest.mark.asyncio
async def test_windows_default_runs_cmd_echo(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default import WindowsDefaultBackend

    cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
    result = await WindowsDefaultBackend().run(
        _request(tmp_path, (str(cmd), "/c", "echo ok"))
    )

    assert result.returncode == 0
    assert "ok" in result.stdout.lower()


@pytest.mark.asyncio
async def test_windows_default_passes_stdin(tmp_path: Path) -> None:
    from opensquilla.sandbox.backend.windows_default import WindowsDefaultBackend

    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    result = await WindowsDefaultBackend().run(
        _request(
            tmp_path,
            (
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "$input | ForEach-Object { Write-Output $_ }",
            ),
            stdin=b"stdin-ok\r\n",
        )
    )

    assert result.returncode == 0
    assert "stdin-ok" in result.stdout


@pytest.mark.asyncio
async def test_windows_guest_process_is_rejected_without_launch(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend.windows_default import WindowsDefaultBackend
    from opensquilla.sandbox.integration import run_under_backend

    marker = tmp_path / "must-not-launch.txt"
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    request = _request(
        tmp_path,
        (
            str(powershell),
            "-NoProfile",
            "-Command",
            f"Set-Content -LiteralPath '{marker}' -Value launched",
        ),
    )
    request = replace(
        request,
        env={**request.env, "OPENSQUILLA_GUEST_SAFE": "0"},
    )

    token = current_tool_context.set(ToolContext(guest_safe=True))
    try:
        with pytest.raises(
            SandboxBackendError,
            match="GUEST_WINDOWS_PROCESS_UNAVAILABLE",
        ):
            await run_under_backend(
                request,
                runtime=SimpleNamespace(backend=WindowsDefaultBackend()),
            )
    finally:
        current_tool_context.reset(token)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_windows_guest_filesystem_worker_is_confined_to_managed_workspace(
    tmp_path: Path,
) -> None:
    from opensquilla.sandbox.backend.windows_default import WindowsDefaultBackend
    from opensquilla.sandbox.operation_runtime import SandboxOperation

    workspace = tmp_path / "managed-workspace"
    workspace.mkdir()
    target = workspace / "allowed.txt"
    host_sentinel = Path(__file__).resolve()
    profile = compile_web_guest_file_profile(
        StoredSandboxPolicy(),
        workspace=workspace,
        platform="windows",
        env=os.environ,
    )
    backend = WindowsDefaultBackend()

    result = await backend.run_operation(
        SandboxOperation.filesystem(
            kind="write_text",
            workspace=workspace,
            run_mode=RunMode.SAFE.value,
            path=target,
            paths=(target,),
            content="managed-ok",
            file_system_profile=profile,
        )
    )

    assert result.message
    assert target.read_text(encoding="utf-8") == "managed-ok"
    with pytest.raises(SandboxBackendError, match="denies read access"):
        await backend.run_operation(
            SandboxOperation.filesystem(
                kind="read_text",
                workspace=workspace,
                run_mode=RunMode.SAFE.value,
                path=host_sentinel,
                paths=(host_sentinel,),
                file_system_profile=profile,
            )
        )


@pytest.mark.asyncio
async def test_windows_default_runs_shell_host_nested_powershell_env_probe(
) -> None:
    from opensquilla.sandbox.backend.windows_default import WindowsDefaultBackend
    from opensquilla.tools.builtin import shell

    workspace = Path.home() / ".opensquilla" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    runtime = SimpleNamespace(backend=SimpleNamespace(name="windows_default"))
    command = (
        "powershell -NoProfile -Command "
        "\"Write-Output ('HTTP_PROXY=' + $env:HTTP_PROXY); "
        "Write-Output ('HTTPS_PROXY=' + $env:HTTPS_PROXY); "
        "Write-Output ('NO_PROXY=' + $env:NO_PROXY); "
        "Write-Output ('OPENSQUILLA_SANDBOX_NETWORK=' + "
        "$env:OPENSQUILLA_SANDBOX_NETWORK); "
        "Write-Output ('PWD=' + (Get-Location).Path)\""
    )
    policy = SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=20),
        env_allowlist=(
            "PATH",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "OPENSQUILLA_SANDBOX_NETWORK",
        ),
        require_approval=False,
    )
    policy = replace(
        policy,
        file_system=build_policy(
            SecurityLevel.STANDARD,
            "shell.exec",
            workspace,
            SandboxSettings(),
        ).file_system,
    )
    policy = shell._policy_with_windows_shell_runtime_mounts(policy, runtime)
    result = await WindowsDefaultBackend().run(
        SandboxRequest(
            argv=shell._sandbox_shell_backend_argv(command, runtime, cwd=workspace),
            cwd=workspace,
            action_kind="shell.exec",
            policy=policy,
            env={
                **os.environ,
                "HTTP_PROXY": "http://127.0.0.1:48123",
                "HTTPS_PROXY": "http://127.0.0.1:48123",
                "NO_PROXY": "localhost,127.0.0.1",
                "OPENSQUILLA_SANDBOX_NETWORK": "proxy_allowlist",
            },
            run_mode=RunMode.SAFE.value,
        )
    )

    assert result.returncode == 0
    assert "HTTP_PROXY=http://127.0.0.1:48123" in result.stdout
    assert "HTTPS_PROXY=http://127.0.0.1:48123" in result.stdout
    assert "OPENSQUILLA_SANDBOX_NETWORK=proxy_allowlist" in result.stdout
    assert f"PWD={workspace}" in result.stdout
    assert result.stderr == ""
