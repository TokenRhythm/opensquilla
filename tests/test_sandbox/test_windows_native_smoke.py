from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.sandbox.backend.windows_default import WindowsDefaultBackend
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.operation_runtime import SandboxOperation
from opensquilla.sandbox.path_validation import decide_path_access
from opensquilla.sandbox.permissions import FileSystemAccess, FileSystemPermissionProfile
from opensquilla.sandbox.policy import build_policy
from opensquilla.sandbox.run_mode import RunMode
from opensquilla.sandbox.types import (
    NetworkMode,
    ResourceLimits,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)
from opensquilla.tools.builtin import filesystem as fs
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="native Windows sandbox required",
)


@pytest.fixture(scope="module", autouse=True)
def _require_windows_native_setup() -> None:
    if sys.platform.startswith("win") and not WindowsDefaultBackend().available():
        pytest.skip("Windows native sandbox setup or identity marker is unavailable")


async def _direct_write_preflight(
    *,
    workspace: Path,
    target: Path,
    policy_profile: FileSystemPermissionProfile,
) -> dict[str, object]:
    queue = ApprovalQueue(db_path=str(workspace / "native-preflight-approvals.sqlite"))
    configure_runtime(
        SandboxSettings(),
        approval_queue=queue,
        workspace=workspace,
    )
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            workspace_dir=str(workspace),
            run_mode="standard",
            session_key="windows-native-filesystem-profile",
            sandbox_file_system_profile=policy_profile,
        )
    )
    try:
        payload = json.loads(await fs.write_file(str(target), "must-not-write"))
        assert isinstance(payload, dict)
        return payload
    finally:
        current_tool_context.reset(token)
        reset_runtime()
        queue.close()


def _readonly_profile_parent(profile: FileSystemPermissionProfile) -> Path:
    home = Path(os.environ["USERPROFILE"])
    candidates = (home / "Documents", *(child for child in home.iterdir() if child.is_dir()))
    for candidate in candidates:
        if candidate.exists() and profile.resolve(candidate) is FileSystemAccess.READ:
            return candidate
    pytest.skip("no existing read-only USERPROFILE child is available for native smoke")


def _policy() -> SandboxPolicy:
    return SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=90),
        env_allowlist=(
            "PATH",
            "TEMP",
            "TMP",
            "PIP_CACHE_DIR",
            "SystemRoot",
            "WINDIR",
            "ComSpec",
        ),
        require_approval=False,
    )


def _request(
    tmp_path: Path,
    argv: tuple[str, ...],
    *,
    run_mode: RunMode = RunMode.TRUSTED,
) -> SandboxRequest:
    return SandboxRequest(
        argv=argv,
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_policy(),
        env=dict(os.environ),
        run_mode=run_mode.value,
    )


@pytest.mark.asyncio
async def test_windows_default_python_can_execute(tmp_path: Path) -> None:
    backend = WindowsDefaultBackend()
    assert backend.available()

    result = await backend.run(_request(tmp_path, (sys.executable, "-c", "print('python-ok')")))

    assert result.returncode == 0
    assert "python-ok" in result.stdout


@pytest.mark.asyncio
async def test_windows_default_workspace_write_succeeds(tmp_path: Path) -> None:
    backend = WindowsDefaultBackend()
    target = tmp_path / "created.txt"

    result = await backend.run(
        _request(
            tmp_path,
            (
                sys.executable,
                "-c",
                f"from pathlib import Path; Path(r'{target}').write_text('ok', encoding='utf-8')",
            ),
        )
    )

    assert result.returncode == 0
    assert target.read_text(encoding="utf-8") == "ok"


@pytest.mark.asyncio
async def test_windows_default_workspace_venv_creation_succeeds(tmp_path: Path) -> None:
    backend = WindowsDefaultBackend()

    result = await backend.run(
        _request(
            tmp_path,
            (sys.executable, "-m", "venv", "--without-pip", str(tmp_path / ".venv")),
        )
    )

    assert result.returncode == 0
    assert (tmp_path / ".venv").exists()


@pytest.mark.asyncio
async def test_windows_default_runtime_readonly_blocks_nested_powershell_set_content(
    tmp_path: Path,
) -> None:
    backend = WindowsDefaultBackend()
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    target = Path(sys.executable).resolve().parent / (
        f"_opensquilla_runtime_denied_{uuid.uuid4().hex}.txt"
    )
    quoted_target = str(target).replace("'", "''")
    command = (
        "try { "
        f"Set-Content -LiteralPath '{quoted_target}' -Value blocked -ErrorAction Stop; "
        "Write-Output 'UNEXPECTED_WRITE_SUCCEEDED' "
        "} catch { "
        "Write-Output ('ERROR: ' + $_.Exception.GetType().Name + ': ' + "
        "$_.Exception.Message) "
        "}"
    )

    try:
        result = await backend.run(
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
                    command,
                ),
            )
        )
    finally:
        target.unlink(missing_ok=True)

    assert "CreateProcessWithLogonW" not in result.stderr
    assert "windows_default process launch failed" not in result.stderr
    assert "UNEXPECTED_WRITE_SUCCEEDED" not in result.stdout
    assert not target.exists()
    assert result.returncode != 0 or "ERROR:" in result.stdout


@pytest.mark.asyncio
async def test_windows_default_proxy_allowlist_without_proxy_fails_closed(
    tmp_path: Path,
) -> None:
    policy = _policy()
    proxy_policy = SandboxPolicy(
        level=policy.level,
        network=NetworkMode.PROXY_ALLOWLIST,
        mounts=policy.mounts,
        workspace_rw=policy.workspace_rw,
        tmp_writable=policy.tmp_writable,
        limits=policy.limits,
        env_allowlist=policy.env_allowlist,
        require_approval=policy.require_approval,
    )
    request = SandboxRequest(
        argv=(sys.executable, "-c", "print('should-not-run')"),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=proxy_policy,
        env=dict(os.environ),
        run_mode=RunMode.TRUSTED.value,
    )

    result = await WindowsDefaultBackend().run(request)

    assert result.returncode != 0
    assert "requires network_proxy endpoint" in result.stderr


@pytest.mark.asyncio
async def test_windows_shell_and_worker_share_codex_projection(tmp_path: Path) -> None:
    backend = WindowsDefaultBackend()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(),
    )
    assert policy.file_system is not None
    targets = (
        Path(os.environ["SystemRoot"]),
        Path(os.environ["USERPROFILE"]) / "Documents",
        workspace,
    )

    for target in targets:
        if not target.exists():
            continue
        worker = await backend.run_operation(
            SandboxOperation.filesystem(
                kind="list_dir",
                workspace=workspace,
                run_mode="standard",
                path=target,
                paths=(target,),
                display_path=str(target),
                file_system_profile=policy.file_system,
            )
        )
        shell = await backend.run(
            SandboxRequest(
                argv=("cmd.exe", "/d", "/c", "dir", str(target)),
                cwd=workspace,
                action_kind="shell.exec",
                policy=policy,
                run_mode="standard",
            )
        )

        assert worker.message
        assert shell.returncode == 0


@pytest.mark.asyncio
async def test_windows_explicit_deny_blocks_worker_direct_and_shell_reads(
    tmp_path: Path,
) -> None:
    backend = WindowsDefaultBackend()
    workspace = tmp_path / "workspace"
    denied = tmp_path / "denied"
    workspace.mkdir()
    denied.mkdir()
    sentinel = denied / "sentinel.txt"
    sentinel.write_text("must-not-appear", encoding="utf-8")
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(denied_read_roots=[str(denied)]),
    )
    assert policy.file_system is not None

    direct = decide_path_access(
        sentinel,
        workspace=workspace,
        profile=policy.file_system,
    )
    with pytest.raises((PermissionError, SandboxBackendError)) as worker_error:
        await backend.run_operation(
            SandboxOperation.filesystem(
                kind="read_file",
                workspace=workspace,
                run_mode="standard",
                path=sentinel,
                paths=(sentinel,),
                display_path=str(sentinel),
                file_system_profile=policy.file_system,
            )
        )
    shell = await backend.run(
        SandboxRequest(
            argv=("cmd.exe", "/d", "/c", "type", str(sentinel)),
            cwd=workspace,
            action_kind="shell.exec",
            policy=policy,
            run_mode="standard",
        )
    )

    assert direct.status == "blocked"
    assert direct.reason == "denied_read"
    assert "must-not-appear" not in str(worker_error.value)
    assert shell.returncode != 0
    assert "must-not-appear" not in shell.stdout
    assert "must-not-appear" not in shell.stderr


@pytest.mark.asyncio
async def test_windows_shell_and_worker_workspace_writes_succeed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker_target = workspace / "worker-created.txt"
    shell_target = workspace / "shell-created.txt"
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(),
    )
    assert policy.file_system is not None

    result = await WindowsDefaultBackend().run_operation(
        SandboxOperation.filesystem(
            kind="write_text",
            workspace=workspace,
            run_mode="standard",
            path=worker_target,
            paths=(worker_target,),
            content="worker-write",
            file_system_profile=policy.file_system,
        )
    )
    shell = await WindowsDefaultBackend().run(
        SandboxRequest(
            argv=(
                "cmd.exe",
                "/d",
                "/c",
                f'(echo shell-write) > "{shell_target}"',
            ),
            cwd=workspace,
            action_kind="shell.exec",
            policy=policy,
            run_mode="standard",
        )
    )

    assert result.created is True
    assert worker_target.read_text(encoding="utf-8") == "worker-write"
    assert shell.returncode == 0
    assert shell_target.read_text(encoding="utf-8").strip() == "shell-write"


@pytest.mark.asyncio
async def test_windows_external_write_requires_elevation_and_raw_backends_deny(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(),
    )
    assert policy.file_system is not None
    parent = _readonly_profile_parent(policy.file_system)
    target = parent / f"opensquilla-native-external-{uuid.uuid4().hex}.txt"
    decision = decide_path_access(
        target,
        workspace=workspace,
        profile=policy.file_system,
        write=True,
    )
    assert not target.exists()

    try:
        direct = await _direct_write_preflight(
            workspace=workspace,
            target=target,
            policy_profile=policy.file_system,
        )
        with pytest.raises((PermissionError, SandboxBackendError)):
            await WindowsDefaultBackend().run_operation(
                SandboxOperation.filesystem(
                    kind="write_text",
                    workspace=workspace,
                    run_mode="standard",
                    path=target,
                    paths=(target,),
                    content="must-not-write",
                    file_system_profile=policy.file_system,
                )
            )
        shell = await WindowsDefaultBackend().run(
            SandboxRequest(
                argv=(
                    "cmd.exe",
                    "/d",
                    "/c",
                    f'(echo must-not-write) > "{target}"',
                ),
                cwd=workspace,
                action_kind="shell.exec",
                policy=policy,
                run_mode="standard",
            )
        )

        assert decision.status == "request"
        assert decision.reason == "mount_requires_write_access"
        assert direct["status"] == "elevation_required"
        assert shell.returncode != 0
        assert not target.exists()
    finally:
        target.unlink(missing_ok=True)
