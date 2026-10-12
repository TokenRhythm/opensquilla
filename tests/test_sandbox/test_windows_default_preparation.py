from __future__ import annotations

import asyncio
import contextvars
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from opensquilla.sandbox.backend import windows_default as backend
from opensquilla.sandbox.backend.windows_default_acl import AclAccess, AclGrant, AclGrantKind
from opensquilla.sandbox.operation_runtime import FilesystemOperationRequest, SandboxOperation
from opensquilla.sandbox.permissions import (
    FileSystemAccess,
    FileSystemPermissionEntry,
    FileSystemPermissionProfile,
)
from opensquilla.sandbox.types import (
    NetworkMode,
    ResourceLimits,
    SandboxBackendError,
    SandboxPolicy,
    SandboxRequest,
    SecurityLevel,
)


class PreparedError(Exception):
    pass


def _request(path: Path) -> SandboxRequest:
    return SandboxRequest(
        argv=("synthetic-no-launch",), cwd=path, action_kind="shell.exec", run_mode="safe",
        env={}, policy=SandboxPolicy(
            level=SecurityLevel.STANDARD, network=NetworkMode.NONE, mounts=(),
            workspace_rw=True, tmp_writable=False, require_approval=False, env_allowlist=(),
            limits=ResourceLimits(wall_timeout_s=2),
            file_system=FileSystemPermissionProfile(
                entries=(FileSystemPermissionEntry(path, FileSystemAccess.WRITE),),
            ),
        ),
    )


def _entry(monkeypatch, tmp_path, filesystem):
    request = _request(tmp_path)
    if filesystem:
        operation = SandboxOperation(
            domain="filesystem", kind="read_file", workspace=tmp_path,
            request=FilesystemOperationRequest(path=tmp_path / "synthetic"),
        )
        monkeypatch.setattr(backend, "_filesystem_operation_request", lambda op: request)
        return lambda: backend.WindowsDefaultBackend().run_operation(operation)
    return lambda: backend.WindowsDefaultBackend().run(request)


@pytest.mark.parametrize("filesystem", [False, True])
async def test_complete_preparation_runs_off_loop_with_context(monkeypatch, tmp_path, filesystem):
    entry = _entry(monkeypatch, tmp_path, filesystem)
    loop_thread = threading.get_ident()
    marker = contextvars.ContextVar("preparation-marker", default=None)
    token = marker.set("caller")
    calls = []

    def check(stage):
        assert threading.get_ident() != loop_thread
        assert marker.get() == "caller"
        calls.append(stage)

    def support():
        check("support")
        return True

    def payload(*args, **kwargs):
        check("payload")
        return {"helperNonce": "synthetic"}

    if filesystem:
        def make_request(operation):
            check("filesystem")
            return _request(tmp_path)
        monkeypatch.setattr(backend, "_filesystem_operation_request", make_request)

    def cache_allowed(request):
        check("cache-policy")
        return True

    async def launch(*args, **kwargs):
        assert threading.get_ident() == loop_thread
        raise PreparedError

    monkeypatch.setattr(backend, "_support_ready", support)
    monkeypatch.setattr(backend, "_payload_for_request", payload)
    monkeypatch.setattr(backend, "_request_allows_cache_write", cache_allowed)
    monkeypatch.setattr(backend, "ensure_cache_dirs", lambda path: check("cache"))
    monkeypatch.setattr(backend, "internal_child_argv", lambda *a, **kw: (check("argv"), "helper"))
    monkeypatch.setattr(backend, "create_owned_subprocess_exec", launch)
    try:
        with pytest.raises(PreparedError):
            await entry()
    finally:
        marker.reset(token)
    assert calls == (
        ["support", "filesystem", "payload", "argv"] if filesystem
        else ["support", "cache-policy", "payload", "cache", "argv"]
    )


@pytest.mark.parametrize("filesystem", [False, True])
async def test_cancelled_preparation_does_not_spawn(monkeypatch, tmp_path, filesystem):
    entry = _entry(monkeypatch, tmp_path, filesystem)
    loop = asyncio.get_running_loop()
    started, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    launches = []

    def support():
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(2)
            return True
        finally:
            loop.call_soon_threadsafe(finished.set)

    async def launch(*args, **kwargs):
        launches.append(True)
        raise PreparedError

    monkeypatch.setattr(backend, "_support_ready", support)
    monkeypatch.setattr(backend, "_payload_for_request", lambda *a, **kw: {"helperNonce": "x"})
    monkeypatch.setattr(backend, "_request_allows_cache_write", lambda request: False)
    monkeypatch.setattr(backend, "create_owned_subprocess_exec", launch)
    task = asyncio.create_task(entry())
    try:
        await asyncio.wait_for(started.wait(), .5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, .5)
        assert not finished.is_set()
        release.set()
        await asyncio.wait_for(finished.wait(), .5)
        await asyncio.sleep(0)
        assert not launches
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("filesystem", [False, True])
async def test_failed_support_never_prepares_or_launches(monkeypatch, tmp_path, filesystem):
    entry = _entry(monkeypatch, tmp_path, filesystem)

    def unexpected(*args, **kwargs):
        raise AssertionError("unavailable backend must fail before payload preparation")

    monkeypatch.setattr(backend, "_support_ready", lambda: False)
    monkeypatch.setattr(backend, "_payload_for_request", unexpected)
    monkeypatch.setattr(backend, "_filesystem_operation_request", unexpected)
    monkeypatch.setattr(backend, "create_owned_subprocess_exec", unexpected)
    with pytest.raises(SandboxBackendError, match="sandbox_setup_required"):
        await entry()


def test_available_remains_synchronous(monkeypatch):
    monkeypatch.setattr(backend, "_support_ready", lambda: True)
    assert backend.WindowsDefaultBackend().available() is True
    monkeypatch.setattr(backend, "_support_ready", lambda: False)
    assert backend.WindowsDefaultBackend().available() is False


async def test_cancelled_safe_preparation_keeps_shared_slots_until_worker_finishes(
    monkeypatch, tmp_path,
):
    from opensquilla.tools.builtin import shell

    loop = asyncio.get_running_loop()
    started = [asyncio.Event(), asyncio.Event()]
    finished = [asyncio.Event(), asyncio.Event()]
    release = [threading.Event(), threading.Event()]
    mutex = threading.Lock()
    count = 0
    launches = []

    def support():
        nonlocal count
        with mutex:
            index = count
            count += 1
        loop.call_soon_threadsafe(started[index].set)
        try:
            assert release[index].wait(3)
            return True
        finally:
            loop.call_soon_threadsafe(finished[index].set)

    async def launch(*args, **kwargs):
        launches.append(True)
        raise PreparedError

    monkeypatch.setattr(backend, "_support_ready", support)
    monkeypatch.setattr(backend, "_payload_for_request", lambda *a, **kw: {"helperNonce": "x"})
    monkeypatch.setattr(backend, "_request_allows_cache_write", lambda request: False)
    monkeypatch.setattr(backend, "create_owned_subprocess_exec", launch)
    tasks = [asyncio.create_task(backend.WindowsDefaultBackend().run(_request(tmp_path)))
             for _ in range(2)]
    full_started = threading.Event()
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 1)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert not any(event.is_set() for event in finished)
        cancelled_waiter = asyncio.create_task(shell._prepare_shell_runtime(full_started.set))
        tasks.append(cancelled_waiter)
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        full_waiter = asyncio.create_task(shell._prepare_shell_runtime(full_started.set))
        tasks.append(full_waiter)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "spare"), .5) == "spare"
        assert not full_started.is_set()
        release[0].set()
        await asyncio.wait_for(full_waiter, 1)
        assert finished[0].is_set() and full_started.is_set()
        assert not launches
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in finished)), 1)


@pytest.mark.skipif(os.name != "nt", reason="native Windows file lock")
async def test_capability_file_lock_wait_does_not_block_loop(monkeypatch, tmp_path):
    for name, value in {
        "_support_ready": lambda: True,
        "_process_base_env": lambda request: {},
        "_windows_network_boundary_payload": lambda request: None,
        "_request_needs_host_tool_paths": lambda request: False,
        "process_executable_rx_roots": lambda *args: (),
        "runtime_rx_roots": lambda *args: (),
        "_workspace_traversal_roots": lambda *args: (),
        "_expansion_grants_from_env": lambda *args: (),
        "_deny_write_paths_for_request": lambda *args, **kwargs: (),
        "_profile_denied_read_paths": lambda *args: (),
        "_acl_sensitive_marker": lambda *args: None,
        "_profile_acl_grants": lambda *args: (
            AclGrant(tmp_path, AclAccess.RWX, AclGrantKind.REQUIRED),
        ),
        "_capability_store_path": lambda: tmp_path / "cap_sids.json",
        "_deny_acl_state_path": lambda: tmp_path / "deny.json",
        "internal_child_argv": lambda *args, **kwargs: ("synthetic-no-launch",),
    }.items():
        monkeypatch.setattr(backend, name, value)

    async def launch(*args, **kwargs):
        raise PreparedError

    monkeypatch.setattr(backend, "create_owned_subprocess_exec", launch)
    holder_code = (
        "import msvcrt,time,sys; f=open(sys.argv[1],'a+b'); "
        "f.seek(0); f.write(b'0'); f.flush(); f.seek(0); "
        "msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); "
        "print('locked',flush=True); time.sleep(.3); f.seek(0); "
        "msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1); f.close()"
    )
    beats = []

    async def heartbeat():
        while True:
            beats.append(time.monotonic())
            await asyncio.sleep(.01)

    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code, str(tmp_path / ".cap_sids.json.lock")],
        stdout=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    ticker = None
    try:
        assert holder.stdout.readline().strip() == "locked"
        ticker = asyncio.create_task(heartbeat())
        await asyncio.sleep(.02)
        with pytest.raises(PreparedError):
            await backend.WindowsDefaultBackend()._run(
                _request(tmp_path), prepare_cache=False,
                rehome_user_state=False, private_mounts_are_required=False,
            )
        await asyncio.sleep(.02)
        assert max(b - a for a, b in zip(beats, beats[1:])) < .3
    finally:
        if ticker is not None:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=2)
        holder.stdout.close()
