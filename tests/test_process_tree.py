from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wintypes
import io
import os
import shutil
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from opensquilla import process_tree


@pytest.mark.asyncio
async def test_posix_anchor_creation_waits_for_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = asyncio.Event()

    class Stream:
        async def readexactly(self, _size: int) -> bytes:
            await ready.wait()
            return process_tree._POSIX_ANCHOR_READY

    class Process:
        pid = 4141
        returncode = None
        stdout = Stream()

    async def fake_spawn(*_argv: str, **_kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", fake_spawn)
    creation = asyncio.create_task(process_tree._create_posix_anchor())
    await asyncio.sleep(0)
    assert creation.done() is False
    ready.set()
    anchor = await asyncio.wait_for(creation, timeout=0.2)
    assert anchor.pgid == 4141


@pytest.mark.asyncio
async def test_posix_anchor_ready_timeout_stops_unarmed_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[Process] = []

    class Stream:
        async def readexactly(self, _size: int) -> bytes:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    class Input:
        def __init__(self, process: Process) -> None:
            self.process = process
            self.closed = False

        def is_closing(self) -> bool:
            return self.closed

        def close(self) -> None:
            self.closed = True
            self.process.returncode = 125

    class Process:
        pid = 4242

        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdout = Stream()
            self.stdin = Input(self)

    async def fake_spawn(*_argv: str, **_kwargs: object) -> Process:
        process = Process()
        spawned.append(process)
        return process

    monkeypatch.setattr(process_tree, "_CONTROL_READY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", fake_spawn)

    with pytest.raises(
        process_tree.ProcessTreeOwnershipError,
        match="did not become ready",
    ):
        await process_tree._create_posix_anchor()

    assert len(spawned) == 1
    assert spawned[0].stdin.closed is True
    assert spawned[0].returncode == 125


@pytest.mark.skipif(os.name != "posix", reason="process group behavior is POSIX-specific")
@pytest.mark.asyncio
async def test_immediate_stop_after_ready_cannot_kill_anchor_before_ignored_target(
    tmp_path,
) -> None:
    for attempt in range(20):
        survived = tmp_path / f"immediate-stop-{attempt}"

        def ignore_term() -> None:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)

        process = await process_tree.create_owned_subprocess_exec(
            sys.executable,
            "-c",
            (
                "import pathlib, time; time.sleep(0.4); "
                f"pathlib.Path({str(survived)!r}).write_text('leaked')"
            ),
            preexec_fn=ignore_term,
        )
        owner = process_tree.capture_process_tree_owner(process, isolated=True)
        assert await owner.terminate(graceful_timeout=0.01, kill_timeout=1.0)
        await asyncio.wait_for(process.wait(), timeout=1.0)
        await asyncio.sleep(0.01)
        assert survived.exists() is False


@pytest.mark.skipif(os.name != "posix", reason="PGID lifecycle is POSIX-specific")
def test_posix_anchor_prevents_signals_after_ownership_lifecycle_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_tree.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )
    anchor_process = SimpleNamespace(returncode=None)
    owner = process_tree.ProcessTreeOwner(
        process=SimpleNamespace(returncode=0),
        pid=4242,
        pgid=4242,
        posix_anchor=process_tree._PosixGroupAnchor(
            process=anchor_process,
            pgid=4242,
        ),
    )

    assert owner.is_active() is True
    owner._signal_posix_group(process_tree.signal.SIGTERM)
    assert signals == [(4242, process_tree.signal.SIGTERM)]

    # Reaping the parent-owned anchor permanently closes this owner. Even if a
    # later unrelated group receives the same numeric PGID, it is never probed
    # or signalled through the expired ownership token.
    anchor_process.returncode = 0
    assert owner.is_active() is False
    owner._signal_posix_group(process_tree.signal.SIGKILL)
    assert signals == [(4242, process_tree.signal.SIGTERM)]


@pytest.mark.skipif(os.name != "posix", reason="PGID lifecycle is POSIX-specific")
@pytest.mark.asyncio
async def test_posix_anchor_outlives_leader_and_excludes_unrelated_group(tmp_path) -> None:
    child_pid = tmp_path / "owned-child.pid"
    owned_survived = tmp_path / "owned-survived"
    sibling_survived = tmp_path / "sibling-survived"
    child_script = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(0.8); "
        f"pathlib.Path({str(owned_survived)!r}).write_text('survived')"
    )
    parent_script = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}])"
    )
    owned = await process_tree.create_owned_subprocess_exec(
        sys.executable,
        "-c",
        parent_script,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    owner = process_tree.capture_process_tree_owner(owned, isolated=True)
    sibling = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import pathlib, time; time.sleep(0.4); "
            f"pathlib.Path({str(sibling_survived)!r}).write_text('survived')"
        ),
        start_new_session=True,
    )
    try:
        await asyncio.wait_for(owned.wait(), timeout=3.0)
        for _attempt in range(200):
            if child_pid.exists():
                break
            await asyncio.sleep(0.01)
        assert child_pid.exists()
        assert owner.is_active() is True
        assert await owner.terminate(graceful_timeout=0.2, kill_timeout=1.0)
        await asyncio.wait_for(sibling.wait(), timeout=2.0)
        await asyncio.sleep(0.9)
        assert not owned_survived.exists()
        assert sibling_survived.exists()
    finally:
        if owner.is_active():
            await owner.terminate(graceful_timeout=0.1, kill_timeout=1.0)
        if sibling.returncode is None:
            sibling.kill()
            await sibling.wait()


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, "123 123\n"),
        (0, ""),
        (0, "malformed\n"),
        (0, "999 999\n"),
    ],
)
def test_posix_ps_snapshot_failures_never_report_group_empty(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: str,
) -> None:
    monkeypatch.setattr(process_tree.os.path, "isdir", lambda _path: False)
    monkeypatch.setattr(
        process_tree.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
        ),
    )

    assert process_tree._posix_group_members(123) is None


def test_posix_proc_skipped_read_never_reports_group_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_tree.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(process_tree.os, "listdir", lambda _path: ["123"])

    def fail_open(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr("builtins.open", fail_open)

    assert process_tree._posix_group_members(123) is None


def test_posix_proc_ignores_unrelated_pid_disappearing_during_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_tree.os.path, "isdir", lambda _path: True)
    monkeypatch.setattr(process_tree.os, "listdir", lambda _path: ["123", "999"])

    def selective_open(path: str, **_kwargs: object):
        if path.endswith(os.path.join("123", "stat")):
            return io.StringIO("123 (anchor) S 1 123")
        raise FileNotFoundError

    monkeypatch.setattr("builtins.open", selective_open)

    assert process_tree._posix_group_members(123) == (123,)


@pytest.mark.skipif(
    os.name != "posix" or os.path.isdir("/proc"),
    reason="requires the POSIX ps fallback",
)
@pytest.mark.asyncio
async def test_failed_ps_snapshot_keeps_real_leaderless_descendant_owned(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_ps = shutil.which("ps")
    assert real_ps is not None
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    failed_probe = tmp_path / "failed-probe"
    fake_ps = fake_bin / "ps"
    fake_ps.write_text(
        "#!/bin/sh\n"
        f"if [ ! -e {str(failed_probe)!r} ]; then\n"
        f"  : > {str(failed_probe)!r}\n"
        "  exit 1\n"
        "fi\n"
        f"exec {real_ps!r} \"$@\"\n",
        encoding="utf-8",
    )
    fake_ps.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    child_pid = tmp_path / "child.pid"
    survived = tmp_path / "child-survived"
    child_script = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(5); "
        f"pathlib.Path({str(survived)!r}).write_text('leaked')"
    )
    leader_script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}]); "
        "time.sleep(0.1)"
    )
    leader = await process_tree.create_owned_subprocess_exec(
        sys.executable,
        "-c",
        leader_script,
    )
    owner = process_tree.capture_process_tree_owner(leader, isolated=True)
    try:
        for _attempt in range(200):
            if failed_probe.exists() and child_pid.exists():
                break
            await asyncio.sleep(0.01)
        assert failed_probe.exists()
        assert child_pid.exists()
        await asyncio.wait_for(leader.wait(), timeout=2.0)
        assert owner.is_active()
        assert await owner.terminate(graceful_timeout=0.05, kill_timeout=1.0)
        await asyncio.sleep(0.2)
        assert survived.exists() is False
    finally:
        if owner.is_active():
            await owner.terminate(graceful_timeout=0.05, kill_timeout=1.0)


@pytest.mark.asyncio
async def test_non_durable_owner_never_widens_cleanup_to_a_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_tree.os,
        "killpg",
        lambda pgid, sig: group_signals.append((pgid, sig)),
        raising=False,
    )

    class DirectProcess:
        pid = 5151
        returncode: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

    proc = DirectProcess()
    owner = process_tree.capture_process_tree_owner(proc, isolated=False)

    assert owner.durable is False
    assert await owner.terminate(graceful_timeout=0.1, kill_timeout=0.1)
    assert proc.returncode == 0
    assert group_signals == []


def test_windows_unowned_process_never_attempts_racy_post_spawn_job_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 6262
        returncode = None
    proc = Process()
    monkeypatch.setattr(process_tree.os, "name", "nt")

    owner = process_tree.capture_process_tree_owner(proc, isolated=True)

    assert not hasattr(process_tree._WindowsJob, "assign")
    assert owner.durable is False
    assert owner.ownership_error is not None
    assert "controlled Job Object" in owner.ownership_error


@pytest.mark.asyncio
async def test_windows_controlled_launcher_assignment_failure_stops_unreleased_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Gate:
        gate_name = "test-gate"
        ready_name = "test-ready"

        def wait_ready(self, _timeout: float) -> None:
            events.append("ready")

        def release(self) -> None:
            events.append("released")

        def close(self) -> None:
            events.append("gate-closed")

    class Job:
        def assign_pid(self, _pid: int) -> None:
            events.append("assign-failed")
            raise OSError("denied")

        def close(self) -> None:
            events.append("job-closed")

    class Process:
        pid = 7373
        returncode: int | None = None

        def terminate(self) -> None:
            events.append("terminated")
            self.returncode = -15

    process = Process()

    async def fake_spawn(*_argv: str, **_kwargs: object) -> Process:
        events.append("spawned-helper")
        return process

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(
        process_tree._WindowsLaunchGate,
        "create",
        classmethod(lambda _cls: Gate()),
    )
    monkeypatch.setattr(
        process_tree._WindowsJob,
        "create",
        classmethod(lambda _cls: Job()),
    )
    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", fake_spawn)

    with pytest.raises(process_tree.ProcessTreeOwnershipError, match="failed closed"):
        await process_tree.create_owned_subprocess_exec("command.exe")

    assert events == [
        "spawned-helper",
        "assign-failed",
        "terminated",
        "job-closed",
        "gate-closed",
    ]


@pytest.mark.asyncio
async def test_windows_controlled_launcher_waits_for_helper_ready_before_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Gate:
        gate_name = "test-gate"
        ready_name = "test-ready"

        def wait_ready(self, _timeout: float) -> None:
            events.append("helper-ready")

        def release(self) -> None:
            events.append("released")

        def close(self) -> None:
            events.append("gate-closed")

    class Job:
        def assign_pid(self, _pid: int) -> None:
            events.append("assigned")

        def close(self) -> None:
            events.append("job-closed")

    class Process:
        pid = 7474
        returncode = None

    spawn_kwargs: dict[str, object] = {}

    async def fake_spawn(*_argv: str, **kwargs: object) -> Process:
        events.append("spawned-helper")
        spawn_kwargs.update(kwargs)
        return Process()

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(
        process_tree._WindowsLaunchGate,
        "create",
        classmethod(lambda _cls: Gate()),
    )
    monkeypatch.setattr(
        process_tree._WindowsJob,
        "create",
        classmethod(lambda _cls: Job()),
    )
    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", fake_spawn)

    process = await process_tree.create_owned_subprocess_exec(
        "command.exe",
        creationflags=0x20,
        env={"TARGET_ONLY": "yes"},
    )

    assert process_tree.capture_process_tree_owner(process, isolated=True).durable
    assert int(spawn_kwargs["creationflags"]) & 0x01000000
    assert int(spawn_kwargs["creationflags"]) & 0x20
    assert process_tree._windows_target_env_from_helper(spawn_kwargs["env"]) == {
        "TARGET_ONLY": "yes"
    }
    assert events == [
        "spawned-helper",
        "assigned",
        "helper-ready",
        "released",
        "gate-closed",
    ]


@pytest.mark.asyncio
async def test_windows_async_helper_ready_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Gate:
        gate_name = "test-gate"
        ready_name = "test-ready"

        def wait_ready(self, _timeout: float) -> None:
            events.append("ready-timeout")
            raise TimeoutError

        def release(self) -> None:
            events.append("released")

        def close(self) -> None:
            events.append("gate-closed")

    class Job:
        def assign_pid(self, _pid: int) -> None:
            events.append("assigned")

        def close(self) -> None:
            events.append("job-closed")

    class Process:
        pid = 7676
        returncode: int | None = None

        def terminate(self) -> None:
            events.append("terminated")
            self.returncode = -15

    async def fake_spawn(*_argv: str, **_kwargs: object) -> Process:
        return Process()

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(
        process_tree._WindowsLaunchGate,
        "create",
        classmethod(lambda _cls: Gate()),
    )
    monkeypatch.setattr(
        process_tree._WindowsJob,
        "create",
        classmethod(lambda _cls: Job()),
    )
    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", fake_spawn)

    with pytest.raises(process_tree.ProcessTreeOwnershipError, match="failed closed"):
        await process_tree.create_owned_subprocess_exec("command.exe")

    assert events == [
        "assigned",
        "ready-timeout",
        "terminated",
        "job-closed",
        "gate-closed",
    ]


def test_windows_sync_launcher_waits_for_helper_ready_before_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Gate:
        gate_name = "test-gate"
        ready_name = "test-ready"

        def wait_ready(self, _timeout: float) -> None:
            events.append("helper-ready")

        def release(self) -> None:
            events.append("released")

        def close(self) -> None:
            events.append("gate-closed")

    class Job:
        def assign_pid(self, _pid: int) -> None:
            events.append("assigned")

        def close(self) -> None:
            events.append("job-closed")

    class Process:
        pid = 7575
        returncode = None

        def poll(self) -> None:
            return None

    spawn_kwargs: dict[str, object] = {}

    def fake_popen(_argv: list[str], **kwargs: object) -> Process:
        events.append("spawned-helper")
        spawn_kwargs.update(kwargs)
        return Process()

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(
        process_tree._WindowsLaunchGate,
        "create",
        classmethod(lambda _cls: Gate()),
    )
    monkeypatch.setattr(
        process_tree._WindowsJob,
        "create",
        classmethod(lambda _cls: Job()),
    )
    monkeypatch.setattr(process_tree.subprocess, "Popen", fake_popen)

    process = process_tree.create_owned_popen(
        ("command.exe",),
        creationflags=0x20,
        env={"TARGET_ONLY": "yes"},
    )

    assert process_tree.capture_process_tree_owner(process, isolated=True).durable
    assert int(spawn_kwargs["creationflags"]) & 0x01000000
    assert int(spawn_kwargs["creationflags"]) & 0x20
    assert process_tree._windows_target_env_from_helper(spawn_kwargs["env"]) == {
        "TARGET_ONLY": "yes"
    }
    assert events == [
        "spawned-helper",
        "assigned",
        "helper-ready",
        "released",
        "gate-closed",
    ]


def test_windows_sync_helper_ready_timeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Gate:
        gate_name = "test-gate"
        ready_name = "test-ready"

        def wait_ready(self, _timeout: float) -> None:
            events.append("ready-timeout")
            raise TimeoutError

        def close(self) -> None:
            events.append("gate-closed")

    class Job:
        def assign_pid(self, _pid: int) -> None:
            events.append("assigned")

        def close(self) -> None:
            events.append("job-closed")

    class Process:
        pid = 7777
        returncode: int | None = None

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            events.append("terminated")
            self.returncode = -15

        def wait(self, timeout: float) -> int:
            assert timeout == 0.5
            events.append("waited")
            return -15

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(
        process_tree._WindowsLaunchGate,
        "create",
        classmethod(lambda _cls: Gate()),
    )
    monkeypatch.setattr(
        process_tree._WindowsJob,
        "create",
        classmethod(lambda _cls: Job()),
    )
    monkeypatch.setattr(
        process_tree.subprocess,
        "Popen",
        lambda _argv, **_kwargs: Process(),
    )

    with pytest.raises(process_tree.ProcessTreeOwnershipError, match="failed closed"):
        process_tree.create_owned_popen(("command.exe",))

    assert events == [
        "assigned",
        "ready-timeout",
        "terminated",
        "waited",
        "job-closed",
        "gate-closed",
    ]


def test_windows_helper_runtime_env_is_removed_before_target_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SystemRoot", r"C:\\Windows")
    monkeypatch.setenv("WINDIR", r"C:\\Windows")
    monkeypatch.setenv("ComSpec", r"C:\\Windows\\System32\\cmd.exe")

    helper_env = process_tree._windows_helper_env({"TARGET_ONLY": "yes"})

    assert helper_env["SystemRoot"] == r"C:\\Windows"
    assert helper_env["WINDIR"] == r"C:\\Windows"
    assert helper_env["ComSpec"] == r"C:\\Windows\\System32\\cmd.exe"
    assert helper_env[process_tree._WINDOWS_HELPER_STRIP_ENV] == (
        "SystemRoot;WINDIR;ComSpec"
    )
    assert process_tree._windows_target_env_from_helper(helper_env) == {
        "TARGET_ONLY": "yes"
    }
    assert process_tree._windows_target_env_from_helper(
        {key.upper(): value for key, value in helper_env.items()}
    ) == {"TARGET_ONLY": "yes"}


def test_windows_helper_preserves_allowlisted_runtime_env_for_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SystemRoot", r"C:\\HostWindows")

    helper_env = process_tree._windows_helper_env(
        {"SystemRoot": r"D:\\AllowedWindows", "TARGET_ONLY": "yes"}
    )

    assert process_tree._windows_target_env_from_helper(helper_env) == {
        "SystemRoot": r"D:\\AllowedWindows",
        "TARGET_ONLY": "yes",
    }


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows events and Job Objects")
@pytest.mark.asyncio
async def test_windows_owned_launch_boots_helper_with_restricted_target_env() -> None:
    process = await process_tree.create_owned_subprocess_exec(
        sys.executable,
        "-c",
        (
            "import os; "
            "print(os.environ.get('TARGET_ONLY')); "
            "print(os.environ.get('SystemRoot', 'missing')); "
            f"print(os.environ.get({process_tree._WINDOWS_HELPER_STRIP_ENV!r}, 'missing'))"
        ),
        env={"PATH": os.environ.get("PATH", ""), "TARGET_ONLY": "yes"},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)

    assert process.returncode == 0, stderr.decode(errors="replace")
    assert stdout.decode().splitlines() == ["yes", "missing", "missing"]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows events and Job Objects")
def test_windows_owned_popen_boots_helper_with_restricted_target_env() -> None:
    process = process_tree.create_owned_popen(
        (
            sys.executable,
            "-c",
            (
                "import os; "
                "print(os.environ.get('TARGET_ONLY')); "
                "print(os.environ.get('SystemRoot', 'missing')); "
                f"print(os.environ.get({process_tree._WINDOWS_HELPER_STRIP_ENV!r}, 'missing'))"
            ),
        ),
        env={"PATH": os.environ.get("PATH", ""), "TARGET_ONLY": "yes"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout, stderr = process.communicate(timeout=10.0)

    assert process.returncode == 0, stderr.decode(errors="replace")
    assert stdout.decode().splitlines() == ["yes", "missing", "missing"]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows events and Job Objects")
@pytest.mark.asyncio
async def test_windows_job_kills_descendant_after_direct_leader_exits(tmp_path) -> None:
    child_pid = tmp_path / "child.pid"
    child_script = tmp_path / "child.py"
    leader_script = tmp_path / "leader.py"
    child_script.write_text(
        "import os\n"
        "import pathlib\n"
        "import time\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    leader_script.write_text(
        "import subprocess\n"
        "import sys\n"
        f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n",
        encoding="utf-8",
    )

    leader = await process_tree.create_owned_subprocess_exec(
        sys.executable,
        str(leader_script),
    )
    owner = process_tree.capture_process_tree_owner(leader, isolated=True)
    await asyncio.wait_for(leader.wait(), timeout=10.0)
    for _attempt in range(200):
        if child_pid.exists():
            break
        await asyncio.sleep(0.01)
    assert child_pid.exists()
    assert owner.is_active()

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    child_handle = kernel32.OpenProcess(
        0x00100000,  # SYNCHRONIZE
        False,
        int(child_pid.read_text(encoding="utf-8")),
    )
    assert child_handle
    try:
        assert await owner.terminate(graceful_timeout=0.1, kill_timeout=5.0)
        assert kernel32.WaitForSingleObject(child_handle, 5000) == 0
    finally:
        kernel32.CloseHandle(child_handle)
