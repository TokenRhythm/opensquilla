from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from opensquilla.safety.sandbox import HAS_RESOURCE
from opensquilla.sandbox.backend.noop import NoopBackend
from opensquilla.sandbox.types import (
    MountSpec,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)


def _policy(workspace: Path) -> SandboxPolicy:
    return SandboxPolicy(
        level=SecurityLevel.STANDARD,
        network=NetworkMode.NONE,
        mounts=(MountSpec(host_path=workspace, sandbox_path=Path("/workspace"), mode="rw"),),
        workspace_rw=True,
        tmp_writable=True,
        limits=ResourceLimits(wall_timeout_s=5.0),
        env_allowlist=("PATH",),
        require_approval=False,
    )


def _policy_with_env(workspace: Path) -> SandboxPolicy:
    policy = _policy(workspace)
    return SandboxPolicy(
        level=policy.level,
        network=policy.network,
        mounts=policy.mounts,
        workspace_rw=policy.workspace_rw,
        tmp_writable=policy.tmp_writable,
        limits=policy.limits,
        env_allowlist=("PATH", "VISIBLE_REQUEST_ENV"),
        require_approval=policy.require_approval,
        description=policy.description,
    )


@pytest.mark.skipif(not HAS_RESOURCE, reason="noop backend safety runner is POSIX-only")
@pytest.mark.asyncio
async def test_noop_backend_preserves_request_stdin(tmp_path: Path) -> None:
    request = SandboxRequest(
        argv=(
            sys.executable,
            "-c",
            "import sys; print('STDIN:' + sys.stdin.read())",
        ),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_policy(tmp_path),
        stdin=b"payload",
    )

    result = await NoopBackend().run(request)

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["STDIN:payload"]


@pytest.mark.skipif(not HAS_RESOURCE, reason="noop backend safety runner is POSIX-only")
@pytest.mark.asyncio
async def test_noop_backend_preserves_binary_request_stdin(tmp_path: Path) -> None:
    request = SandboxRequest(
        argv=(
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.buffer.read().hex())",
        ),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_policy(tmp_path),
        stdin=b"\xff\x00abc",
    )

    result = await NoopBackend().run(request)

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["ff00616263"]


@pytest.mark.skipif(not HAS_RESOURCE, reason="noop backend safety runner is POSIX-only")
@pytest.mark.asyncio
async def test_noop_backend_forwards_allowlisted_request_env(tmp_path: Path) -> None:
    request = SandboxRequest(
        argv=(
            sys.executable,
            "-c",
            (
                "import os; "
                "print(os.environ.get('VISIBLE_REQUEST_ENV', '')); "
                "print(os.environ.get('HIDDEN_REQUEST_ENV', 'missing'))"
            ),
        ),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_policy_with_env(tmp_path),
        env={
            "VISIBLE_REQUEST_ENV": "visible",
            "HIDDEN_REQUEST_ENV": "hidden",
        },
    )

    result = await NoopBackend().run(request)

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["visible", "missing"]


@pytest.mark.skipif(not HAS_RESOURCE, reason="noop backend safety runner is POSIX-only")
@pytest.mark.asyncio
async def test_noop_backend_caller_cancel_stops_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "noop-descendant-ran"
    child_script = (
        "import pathlib, time; "
        f"time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('ran')"
    )
    parent_script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        "time.sleep(30)"
    )
    request = SandboxRequest(
        argv=(sys.executable, "-c", parent_script),
        cwd=tmp_path,
        action_kind="shell.exec",
        policy=_policy(tmp_path),
        env={"PATH": os.environ.get("PATH", "")},
    )

    running = asyncio.create_task(NoopBackend().run(request))
    await asyncio.sleep(0.2)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    await asyncio.sleep(1.0)

    assert not marker.exists()
