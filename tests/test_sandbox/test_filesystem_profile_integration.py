from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

from opensquilla.application.approval_queue import ApprovalQueue
from opensquilla.sandbox.backend.bubblewrap import BubblewrapBackend
from opensquilla.sandbox.backend.seatbelt import SeatbeltBackend
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.sandbox.operation_runtime import SandboxOperation
from opensquilla.sandbox.path_validation import decide_path_access
from opensquilla.sandbox.permissions import FileSystemPermissionProfile
from opensquilla.sandbox.policy import build_policy
from opensquilla.sandbox.types import (
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)
from opensquilla.tools.builtin import filesystem as fs
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


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
            session_key="native-filesystem-profile",
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


PosixNativeBackend = BubblewrapBackend | SeatbeltBackend


def _native_policy(
    workspace: Path,
    *,
    denied_read_roots: tuple[Path, ...] = (),
) -> SandboxPolicy:
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(denied_read_roots=[str(path) for path in denied_read_roots]),
    )
    assert policy.file_system is not None
    return policy


async def _assert_posix_workspace_write_parity(
    backend: PosixNativeBackend,
    workspace: Path,
) -> None:
    worker_target = workspace / "worker-created.txt"
    shell_target = workspace / "shell-created.txt"
    policy = _native_policy(workspace)
    assert policy.file_system is not None

    worker = await backend.run_operation(
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
    shell = await backend.run(
        SandboxRequest(
            argv=(
                "/bin/sh",
                "-c",
                'printf shell-write > "$1"',
                "opensquilla-native-smoke",
                str(shell_target),
            ),
            cwd=workspace,
            action_kind="shell.exec",
            policy=policy,
            run_mode="standard",
        )
    )

    assert worker.created is True
    assert worker_target.read_text(encoding="utf-8") == "worker-write"
    assert shell.returncode == 0
    assert shell_target.read_text(encoding="utf-8") == "shell-write"


async def _assert_posix_external_write_denied(
    backend: PosixNativeBackend,
    *,
    workspace: Path,
    target: Path,
) -> None:
    policy = _native_policy(workspace)
    assert policy.file_system is not None
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
        with pytest.raises(SandboxBackendError):
            await backend.run_operation(
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
        shell = await backend.run(
            SandboxRequest(
                argv=(
                    "/bin/sh",
                    "-c",
                    'printf must-not-write > "$1"',
                    "opensquilla-native-smoke",
                    str(target),
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


async def _assert_posix_explicit_read_deny(
    backend: PosixNativeBackend,
    *,
    workspace: Path,
    denied: Path,
) -> None:
    sentinel = denied / "sentinel.txt"
    sentinel.write_text("must-not-appear", encoding="utf-8")
    policy = _native_policy(workspace, denied_read_roots=(denied,))
    assert policy.file_system is not None
    direct = decide_path_access(
        sentinel,
        workspace=workspace,
        profile=policy.file_system,
    )

    with pytest.raises(SandboxBackendError) as worker_error:
        await backend.run_operation(
            SandboxOperation.filesystem(
                kind="read_file",
                workspace=workspace,
                run_mode="standard",
                path=sentinel,
                paths=(sentinel,),
                file_system_profile=policy.file_system,
            )
        )
    shell = await backend.run(
        SandboxRequest(
            argv=("/bin/cat", str(sentinel)),
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
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native smoke")
@pytest.mark.parametrize("target", [Path("/etc"), Path("/home"), Path("/var"), Path.home()])
async def test_bubblewrap_shell_and_worker_read_codex_host_view(
    target: Path,
    tmp_path: Path,
) -> None:
    backend = BubblewrapBackend()
    if not backend.available() or not target.exists():
        pytest.skip("bubblewrap or target is unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(),
    )
    assert policy.file_system is not None

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
            argv=("/bin/ls", str(target)),
            cwd=workspace,
            action_kind="shell.exec",
            policy=policy,
            run_mode="standard",
        )
    )

    assert isinstance(worker.message, str)
    assert worker.message
    assert shell.returncode == 0


@pytest.mark.asyncio
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native smoke")
async def test_bubblewrap_shell_and_worker_write_workspace(tmp_path: Path) -> None:
    backend = BubblewrapBackend()
    if not backend.available():
        pytest.skip("bubblewrap is unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    await _assert_posix_workspace_write_parity(backend, workspace)


@pytest.mark.asyncio
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native smoke")
async def test_bubblewrap_external_write_requires_elevation_and_raw_backends_deny(
    tmp_path: Path,
) -> None:
    backend = BubblewrapBackend()
    if not backend.available():
        pytest.skip("bubblewrap is unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = Path.home() / f".opensquilla-bwrap-external-{uuid.uuid4().hex}.txt"

    await _assert_posix_external_write_denied(
        backend,
        workspace=workspace,
        target=target,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux native smoke")
async def test_bubblewrap_explicit_deny_blocks_shell_and_worker_reads(
    tmp_path: Path,
) -> None:
    backend = BubblewrapBackend()
    if not backend.available():
        pytest.skip("bubblewrap is unavailable")
    workspace = tmp_path / "workspace"
    denied = tmp_path / "denied"
    workspace.mkdir()
    denied.mkdir()

    await _assert_posix_explicit_read_deny(
        backend,
        workspace=workspace,
        denied=denied,
    )


def test_explicit_denied_read_is_blocked_before_backend(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    denied = tmp_path / "secret"
    profile = FileSystemPermissionProfile.workspace(
        workspace=workspace,
        denied_read_roots=(denied,),
    )

    decision = decide_path_access(
        denied / "token",
        workspace=workspace,
        profile=profile,
    )

    assert decision.status == "blocked"
    assert decision.reason == "denied_read"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
@pytest.mark.parametrize("target", [Path("/etc"), Path.home()])
async def test_seatbelt_shell_and_worker_share_host_read_profile(
    target: Path,
    tmp_path: Path,
) -> None:
    backend = SeatbeltBackend()
    if not backend.available() or not target.exists():
        pytest.skip("Seatbelt or target is unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = build_policy(
        SecurityLevel.STANDARD,
        "shell.exec",
        workspace,
        SandboxSettings(),
    )
    assert policy.file_system is not None

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
            argv=("/bin/ls", str(target)),
            cwd=workspace,
            action_kind="shell.exec",
            policy=policy,
            run_mode="standard",
        )
    )

    assert worker.message
    assert shell.returncode == 0


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
async def test_seatbelt_shell_and_worker_write_workspace(tmp_path: Path) -> None:
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("Seatbelt is unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    await _assert_posix_workspace_write_parity(backend, workspace)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
async def test_seatbelt_external_write_requires_elevation_and_raw_backends_deny(
    tmp_path: Path,
) -> None:
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("Seatbelt is unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = Path.home() / f".opensquilla-seatbelt-external-{uuid.uuid4().hex}.txt"

    await _assert_posix_external_write_denied(
        backend,
        workspace=workspace,
        target=target,
    )


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt required")
async def test_seatbelt_explicit_deny_blocks_shell_and_worker_reads(
    tmp_path: Path,
) -> None:
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("Seatbelt is unavailable")
    workspace = tmp_path / "workspace"
    denied = tmp_path / "denied"
    workspace.mkdir()
    denied.mkdir()

    await _assert_posix_explicit_read_deny(
        backend,
        workspace=workspace,
        denied=denied,
    )
