from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.backend import noop as noop_mod
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


@pytest.mark.asyncio
async def test_noop_backend_caller_cancel_stops_process_tree(tmp_path: Path) -> None:
    marker = tmp_path / "noop-descendant-ran"
    child_script = (
        f"import pathlib, time; time.sleep(0.8); pathlib.Path({str(marker)!r}).write_text('ran')"
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
    cancelled = await asyncio.gather(running, return_exceptions=True)
    assert isinstance(cancelled[0], asyncio.CancelledError)
    await asyncio.sleep(1.0)

    assert not marker.exists()


@pytest.mark.asyncio
async def test_noop_backend_windows_without_stdin_uses_file_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    terminations: list[tuple[float, float]] = []

    class Process:
        pid = 8181
        returncode: int | None = None

        async def wait(self) -> int:
            stdout_file = captured["stdout"]
            stderr_file = captured["stderr"]
            stdout_file.write(b"stdout payload\n")  # type: ignore[union-attr]
            stderr_file.write(b"stderr payload\n")  # type: ignore[union-attr]
            self.returncode = 7
            return 7

        async def communicate(self, *, input: bytes | None = None):
            raise AssertionError("Windows file capture must wait for process completion")

    class Owner:
        async def terminate(self, *, graceful_timeout: float, kill_timeout: float) -> bool:
            terminations.append((graceful_timeout, kill_timeout))
            return True

    async def fake_spawn(*_argv: str, **kwargs: object) -> Process:
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(noop_mod, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(noop_mod, "HAS_RESOURCE", False)
    monkeypatch.setattr(noop_mod, "create_owned_subprocess_exec", fake_spawn)
    monkeypatch.setattr(noop_mod, "capture_process_tree_owner", lambda *_args, **_kwargs: Owner())

    result = await NoopBackend().run(
        SandboxRequest(
            argv=("powershell.exe", "-Command", "exit 7"),
            cwd=tmp_path,
            action_kind="shell.exec",
            policy=_policy(tmp_path),
        )
    )

    assert result.returncode == 7
    assert result.stdout == "stdout payload\n"
    assert result.stderr == "stderr payload\n"
    assert result.timed_out is False
    assert captured["stdin"] is None
    assert captured["stdout"] is not asyncio.subprocess.PIPE
    assert captured["stderr"] is not asyncio.subprocess.PIPE
    assert captured["stdout"] is not captured["stderr"]
    assert captured["stdout"].closed is True  # type: ignore[union-attr]
    assert captured["stderr"].closed is True  # type: ignore[union-attr]
    assert terminations == [(0.2, 1.0)]


@pytest.mark.asyncio
async def test_noop_backend_windows_file_capture_timeout_preserves_partial_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    termination_count = 0
    first_wait_entered = asyncio.Event()

    class Process:
        pid = 8181
        returncode: int | None = None
        wait_count = 0

        async def wait(self) -> int:
            self.wait_count += 1
            if self.wait_count == 1:
                captured["stdout"].write(b"partial output\n")  # type: ignore[union-attr]
                first_wait_entered.set()
                await asyncio.Future()
            self.returncode = 1
            return 1

        async def communicate(self, *, input: bytes | None = None):
            raise AssertionError("Windows file capture must not use communicate")

    class Owner:
        async def terminate(self, *, graceful_timeout: float, kill_timeout: float) -> bool:
            nonlocal termination_count
            termination_count += 1
            return True

    async def fake_spawn(*_argv: str, **kwargs: object) -> Process:
        captured.update(kwargs)
        return process

    process = Process()
    monkeypatch.setattr(noop_mod, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(noop_mod, "HAS_RESOURCE", False)
    monkeypatch.setattr(
        noop_mod,
        "_limits_from_policy",
        lambda _request: SimpleNamespace(wall_seconds=0.01, env_whitelist=("PATH",)),
    )
    monkeypatch.setattr(noop_mod, "create_owned_subprocess_exec", fake_spawn)
    monkeypatch.setattr(noop_mod, "capture_process_tree_owner", lambda *_args, **_kwargs: Owner())

    result = await NoopBackend().run(
        SandboxRequest(
            argv=("powershell.exe", "-Command", "Start-Sleep 60"),
            cwd=tmp_path,
            action_kind="shell.exec",
            policy=_policy(tmp_path),
        )
    )

    assert result.returncode == 1
    assert result.stdout == "partial output\n"
    assert result.timed_out is True
    assert first_wait_entered.is_set()
    assert process.wait_count == 2
    assert termination_count == 2
    assert captured["stdout"].closed is True  # type: ignore[union-attr]
    assert captured["stderr"].closed is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_noop_backend_windows_file_capture_cancel_closes_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    wait_entered = asyncio.Event()
    terminated = asyncio.Event()

    class Process:
        pid = 8181
        returncode: int | None = None

        async def wait(self) -> int:
            wait_entered.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def communicate(self, *, input: bytes | None = None):
            raise AssertionError("Windows file capture must not use communicate")

    class Owner:
        async def terminate(self, *, graceful_timeout: float, kill_timeout: float) -> bool:
            terminated.set()
            return True

    async def fake_spawn(*_argv: str, **kwargs: object) -> Process:
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(noop_mod, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(noop_mod, "HAS_RESOURCE", False)
    monkeypatch.setattr(noop_mod, "create_owned_subprocess_exec", fake_spawn)
    monkeypatch.setattr(noop_mod, "capture_process_tree_owner", lambda *_args, **_kwargs: Owner())

    running = asyncio.create_task(
        NoopBackend().run(
            SandboxRequest(
                argv=("powershell.exe", "-Command", "Start-Sleep 60"),
                cwd=tmp_path,
                action_kind="shell.exec",
                policy=_policy(tmp_path),
            )
        )
    )
    await asyncio.wait_for(wait_entered.wait(), timeout=1.0)
    running.cancel()
    cancelled = await asyncio.gather(running, return_exceptions=True)

    assert isinstance(cancelled[0], asyncio.CancelledError)
    assert terminated.is_set()
    assert captured["stdout"].closed is True  # type: ignore[union-attr]
    assert captured["stderr"].closed is True  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_noop_backend_without_resource_module_omits_posix_preexec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Process:
        pid = 8181
        returncode: int | None = None

        async def communicate(self, *, input: bytes | None = None):
            assert input is None
            self.returncode = 0
            return b"ok\n", b""

    async def fake_spawn(*_argv: str, **kwargs: object) -> Process:
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(noop_mod, "HAS_RESOURCE", False)
    monkeypatch.setattr(noop_mod, "create_owned_subprocess_exec", fake_spawn)

    result = await NoopBackend().run(
        SandboxRequest(
            argv=(sys.executable, "-c", "print('ok')"),
            cwd=tmp_path,
            action_kind="shell.exec",
            policy=_policy(tmp_path),
        )
    )

    assert result.returncode == 0
    assert result.stdout == "ok\n"
    assert "preexec_fn" not in captured
