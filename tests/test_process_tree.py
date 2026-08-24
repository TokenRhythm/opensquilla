from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import ctypes
import ctypes.wintypes as wintypes
import errno
import io
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from opensquilla import private_paths, process_tree


def _synthetic_owner_reference(
    database_path,
    *,
    owner_id: str = "a" * 32,
    session_digest: str = "b" * 64,
    task_digest: str = "c" * 64,
    parent_session_digest: str | None = None,
    parent_task_digest: str | None = None,
    platform: str = "posix",
    controller_pid: int = 4242,
    controller_start_identity: str = "synthetic-start-identity",
):
    return process_tree._PersistedOwnerRef(
        database_path=database_path,
        record=process_tree._PersistedOwnerRecord(
            owner_id=owner_id,
            session_digest=session_digest,
            task_digest=task_digest,
            parent_session_digest=parent_session_digest,
            parent_task_digest=parent_task_digest,
            platform=platform,
            controller_pid=controller_pid,
            controller_start_identity=controller_start_identity,
        ),
    )


@pytest.mark.parametrize(
    ("frozen", "expected_prefix"),
    [
        (False, ("/synthetic/python", "-m", "opensquilla.process_tree")),
        (True, ("/synthetic/gateway", "--internal-child", "process-tree")),
    ],
)
def test_process_tree_child_argv_is_fixed_for_source_and_frozen_modes(
    monkeypatch: pytest.MonkeyPatch,
    frozen: bool,
    expected_prefix: tuple[str, ...],
) -> None:
    executable = "/synthetic/gateway" if frozen else "/synthetic/python"
    monkeypatch.setattr(process_tree.sys, "executable", executable)
    monkeypatch.setattr(process_tree.sys, "frozen", frozen, raising=False)

    argv = process_tree._process_tree_child_argv(
        "--windows-owned-launch",
        "gate",
        "ready",
        "--",
        "cmd",
    )

    assert argv == (
        *expected_prefix,
        "--windows-owned-launch",
        "gate",
        "ready",
        "--",
        "cmd",
    )


def test_windows_frozen_helper_ready_wait_is_extended_and_retried_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    delays: list[float] = []

    class Gate:
        def wait_ready(self, timeout: float) -> None:
            waits.append(timeout)
            if len(waits) == 1:
                raise TimeoutError("synthetic cold start")

    monkeypatch.setattr(process_tree.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process_tree.time, "sleep", delays.append)

    process_tree._wait_for_windows_helper_ready(Gate())

    assert waits == [5.0, 5.0]
    assert delays == [0.25]


def test_windows_frozen_helper_ready_wait_remains_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    delays: list[float] = []

    class Gate:
        def wait_ready(self, timeout: float) -> None:
            waits.append(timeout)
            raise TimeoutError("synthetic frozen timeout")

    monkeypatch.setattr(process_tree.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process_tree.time, "sleep", delays.append)

    with pytest.raises(TimeoutError, match="frozen timeout"):
        process_tree._wait_for_windows_helper_ready(Gate())

    assert waits == [5.0, 5.0]
    assert delays == [0.25]


def test_windows_source_helper_ready_timeout_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []

    class Gate:
        def wait_ready(self, timeout: float) -> None:
            waits.append(timeout)
            raise TimeoutError("synthetic source timeout")

    monkeypatch.delattr(process_tree.sys, "frozen", raising=False)
    monkeypatch.setattr(
        process_tree.time,
        "sleep",
        lambda _delay: pytest.fail("source helper readiness must not retry"),
    )

    with pytest.raises(TimeoutError, match="source timeout"):
        process_tree._wait_for_windows_helper_ready(Gate())

    assert waits == [2.0]


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


@pytest.mark.skipif(os.name != "posix", reason="exec error pipe is POSIX-specific")
@pytest.mark.asyncio
async def test_posix_controlled_launch_preserves_missing_executable_error(tmp_path) -> None:
    with process_tree.task_process_scope(
        tmp_path,
        session_key="synthetic-session",
        task_id="synthetic-task",
    ):
        with pytest.raises(FileNotFoundError):
            await process_tree.create_owned_subprocess_exec(
                "opensquilla-synthetic-command-that-does-not-exist"
            )

    assert process_tree._load_owner_records(tmp_path) == ()


@pytest.mark.skipif(os.name != "posix", reason="PGID lifecycle is POSIX-specific")
@pytest.mark.asyncio
async def test_posix_anchor_owns_signalling_and_closes_with_its_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_tree.os,
        "killpg",
        lambda pgid, sig: gateway_signals.append((pgid, sig)),
    )

    class Input:
        def __init__(self) -> None:
            self.commands: list[bytes] = []
            self.closed = False

        def is_closing(self) -> bool:
            return self.closed

        def write(self, command: bytes) -> None:
            self.commands.append(command)

        async def drain(self) -> None:
            return None

    anchor_process = SimpleNamespace(returncode=None)
    anchor_process.stdin = Input()
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
    assert await owner.posix_anchor.request_signal(
        process_tree._POSIX_ANCHOR_TERMINATE
    )
    assert anchor_process.stdin.commands == [process_tree._POSIX_ANCHOR_TERMINATE]
    assert gateway_signals == []

    # Reaping the parent-owned anchor permanently closes this owner. Even if a
    # later unrelated group receives the same numeric PGID, it is never probed
    # or signalled through the expired ownership token.
    anchor_process.returncode = 0
    assert owner.is_active() is False
    assert not await owner.posix_anchor.request_signal(process_tree._POSIX_ANCHOR_KILL)
    assert gateway_signals == []


@pytest.mark.asyncio
async def test_posix_incomplete_cleanup_remains_failed_after_anchor_exit() -> None:
    anchor = process_tree._PosixGroupAnchor(
        process=SimpleNamespace(returncode=0),
        pgid=4242,
        cleanup_incomplete=True,
    )
    owner = process_tree.ProcessTreeOwner(
        process=SimpleNamespace(returncode=0),
        pid=4242,
        pgid=4242,
        posix_anchor=anchor,
    )

    assert await owner.terminate(graceful_timeout=0.0, kill_timeout=0.0) is False
    assert await owner.terminate(graceful_timeout=0.0, kill_timeout=0.0) is False


@pytest.mark.asyncio
async def test_posix_unacknowledged_anchor_exit_fails_closed() -> None:
    class Process:
        returncode: int | None = None

        async def wait(self) -> int:
            self.returncode = 1
            return 1

    class Stream:
        async def read(self, _size: int) -> bytes:
            return b""

    anchor = process_tree._PosixGroupAnchor(process=Process(), pgid=4242)
    await anchor._watch_empty(Stream())
    owner = process_tree.ProcessTreeOwner(
        process=SimpleNamespace(returncode=None),
        pid=4242,
        pgid=4242,
        posix_anchor=anchor,
    )

    assert anchor.cleanup_incomplete is True
    assert await owner.terminate(graceful_timeout=0.0, kill_timeout=0.0) is False
    assert owner.process.returncode is None


@pytest.mark.asyncio
async def test_posix_anchor_transport_failure_fails_closed() -> None:
    anchor_process = SimpleNamespace(returncode=None)

    class Input:
        def is_closing(self) -> bool:
            return False

        def write(self, _command: bytes) -> None:
            anchor_process.returncode = 1
            raise BrokenPipeError

        async def drain(self) -> None:
            return None

    anchor_process.stdin = Input()
    anchor = process_tree._PosixGroupAnchor(process=anchor_process, pgid=4242)
    owner = process_tree.ProcessTreeOwner(
        process=SimpleNamespace(returncode=None),
        pid=4242,
        pgid=4242,
        posix_anchor=anchor,
    )

    assert await owner.terminate(graceful_timeout=0.0, kill_timeout=0.0) is False
    assert anchor.cleanup_incomplete is True
    assert owner.process.returncode is None


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


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="process ancestry capture is Linux/macOS-specific",
)
@pytest.mark.asyncio
async def test_posix_stop_kills_new_session_descendants_and_preserves_sentinel(
    tmp_path,
) -> None:
    state_dir = tmp_path / "state"
    pid_file = tmp_path / "owned-pids"
    sleeper = "import time; time.sleep(30)"
    grandchild = (
        "import os,pathlib,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(pid_file)!r}).open('a').write(str(os.getpid())+'\\n'); "
        "time.sleep(30)"
    )
    child = (
        "import os,pathlib,signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(pid_file)!r}).open('a').write(str(os.getpid())+'\\n'); "
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}],start_new_session=True); "
        "time.sleep(30)"
    )
    background = (
        "import os,pathlib,signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"pathlib.Path({str(pid_file)!r}).open('a').write(str(os.getpid())+'\\n'); "
        f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True); "
        "time.sleep(30)"
    )
    foreground = (
        "import os,pathlib,subprocess,sys,time; "
        f"pathlib.Path({str(pid_file)!r}).open('a').write(str(os.getpid())+'\\n'); "
        f"subprocess.Popen([sys.executable,'-c',{background!r}],start_new_session=True); "
        "time.sleep(30)"
    )
    with process_tree.task_process_scope(
        state_dir,
        session_key="synthetic-session",
        task_id="synthetic-task",
    ):
        owned = await process_tree.create_owned_subprocess_exec(
            sys.executable,
            "-c",
            foreground,
        )
    owner = process_tree.capture_process_tree_owner(owned, isolated=True)
    sentinel = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        sleeper,
        start_new_session=True,
    )
    owned_pids: list[int] = []
    owned_identities: dict[int, process_tree._PosixProcessInfo] = {}
    try:
        for _attempt in range(500):
            if pid_file.exists():
                owned_pids = [int(value) for value in pid_file.read_text().splitlines()]
                if len(owned_pids) == 4:
                    break
            await asyncio.sleep(0.01)
        assert len(owned_pids) == 4
        owned_identities = {
            pid: info
            for pid in owned_pids
            if (info := process_tree._posix_process_info(pid)) is not None
        }

        owner_stopped, persisted_stopped = await asyncio.gather(
            owner.terminate(graceful_timeout=0.2, kill_timeout=1.0),
            process_tree.cancel_persisted_processes_for_task(
                state_dir,
                "synthetic-session",
                "synthetic-task",
            ),
        )
        assert owner_stopped is True
        assert persisted_stopped in {0, 1}
        await asyncio.wait_for(owned.wait(), timeout=2.0)
        for _attempt in range(300):
            if all(
                process_tree._strict_process_start_identity(pid) is None
                for pid in owned_pids
            ):
                break
            await asyncio.sleep(0.01)

        assert all(
            process_tree._strict_process_start_identity(pid) is None
            for pid in owned_pids
        )
        assert sentinel.returncode is None
    finally:
        if owner.is_active():
            await owner.terminate(graceful_timeout=0.1, kill_timeout=1.0)
        if sentinel.returncode is None:
            sentinel.kill()
            await sentinel.wait()
        for pid, captured_info in owned_identities.items():
            current_info = process_tree._posix_process_info(pid)
            if (
                current_info is not None
                and current_info.uid == captured_info.uid
                and current_info.start_identity == captured_info.start_identity
            ):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


def test_posix_captured_pid_identity_change_is_not_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = process_tree._CapturedPosixProcess(
        pid=4242,
        uid=501,
        start_identity="original-start",
        depth=1,
    )
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_tree,
        "_posix_process_info",
        lambda _pid: process_tree._PosixProcessInfo(
            pid=4242,
            ppid=1,
            pgid=4242,
            uid=501,
            start_identity="replacement-start",
        ),
    )
    monkeypatch.setattr(
        process_tree.os,
        "kill",
        lambda pid, sig: signalled.append((pid, sig)),
    )

    process_tree._signal_captured_posix_processes((captured,), signal.SIGTERM)

    assert signalled == []


def test_linux_descendant_capture_does_not_fall_back_to_numeric_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = 501
    anchor = process_tree._PosixProcessInfo(100, 1, 100, uid, "anchor")
    root = process_tree._PosixProcessInfo(101, 100, 100, uid, "root")
    escaped = process_tree._PosixProcessInfo(102, 101, 102, uid, "escaped")
    monkeypatch.setattr(process_tree.sys, "platform", "linux")
    monkeypatch.setattr(process_tree.os, "geteuid", lambda: uid, raising=False)
    monkeypatch.setattr(
        process_tree,
        "_posix_process_snapshot",
        lambda: {100: anchor, 101: root, 102: escaped},
    )
    monkeypatch.setattr(
        process_tree,
        "_posix_process_info",
        lambda pid: {100: anchor, 101: root, 102: escaped}.get(pid),
    )
    monkeypatch.setattr(
        process_tree.os,
        "pidfd_open",
        lambda _pid, _flags: (_ for _ in ()).throw(OSError(errno.EMFILE, "full")),
        raising=False,
    )
    monkeypatch.setattr(
        process_tree.signal,
        "pidfd_send_signal",
        lambda *_args: None,
        raising=False,
    )

    capture = process_tree._capture_posix_group_descendants(100, 100)

    assert capture.complete is False
    assert capture.processes == ()


def test_other_posix_descendant_capture_preserves_group_only_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process_tree.sys, "platform", "freebsd")
    monkeypatch.setattr(
        process_tree,
        "_posix_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("unsupported native snapshot")),
    )

    capture = process_tree._capture_posix_group_descendants(100, 100)

    assert capture == process_tree._PosixDescendantCapture((), True)


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
    os.name != "posix" or os.path.isdir("/proc") or sys.platform == "darwin",
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
        for _attempt in range(500):
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
        await asyncio.wait_for(leader.wait(), timeout=2.0)


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
async def test_windows_named_job_is_persisted_before_target_release(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    job_names: list[str] = []
    reference = _synthetic_owner_reference(
        tmp_path / "registry.sqlite3",
        platform="windows",
    )

    class Gate:
        gate_name = "synthetic-gate"
        ready_name = "synthetic-ready"

        def wait_ready(self, _timeout: float) -> None:
            events.append("helper-ready")

        def release(self) -> None:
            events.append("target-released")

        def close(self) -> None:
            events.append("gate-closed")

    class Job:
        def assign_pid(self, _pid: int) -> None:
            events.append("job-assigned")

        def close(self) -> None:
            events.append("job-closed")

    async def spawn(*_argv: str, **_kwargs: object):
        events.append("gated-helper-spawned")
        return SimpleNamespace(pid=7475, returncode=None)

    def create_job(_cls: object, name: str | None = None) -> Job:
        assert name is not None
        job_names.append(name)
        events.append("named-job-created")
        return Job()

    def persist(*_args: object, **_kwargs: object):
        events.append("owner-persisted")
        return reference

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(
        process_tree._WindowsLaunchGate,
        "create",
        classmethod(lambda _cls: Gate()),
    )
    monkeypatch.setattr(process_tree._WindowsJob, "create", classmethod(create_job))
    monkeypatch.setattr(process_tree, "_insert_owner_record", persist)
    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", spawn)

    with process_tree.task_process_scope(
        tmp_path / "synthetic-runtime-state",
        session_key="synthetic-session",
        task_id="synthetic-task",
    ):
        await process_tree.create_owned_subprocess_exec("synthetic.exe")

    assert len(job_names) == 1
    assert job_names[0].startswith(process_tree._WINDOWS_JOB_PREFIX)
    assert events == [
        "named-job-created",
        "gated-helper-spawned",
        "job-assigned",
        "helper-ready",
        "owner-persisted",
        "target-released",
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


@pytest.mark.asyncio
async def test_windows_launch_cancellation_cleans_before_reraising_same_exception(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    cancelled = asyncio.CancelledError("synthetic caller cancellation")
    reference = _synthetic_owner_reference(
        tmp_path / "synthetic-registry.sqlite3",
        platform="windows",
    )

    class Gate:
        gate_name = "synthetic-gate"
        ready_name = "synthetic-ready"

        def wait_ready(self, _timeout: float) -> None:
            events.append("helper-ready")

        def release(self) -> None:
            events.append("release-cancelled")
            raise cancelled

        def close(self) -> None:
            events.append("gate-closed")

    class Job:
        def assign_pid(self, _pid: int) -> None:
            events.append("job-assigned")

        def close(self) -> None:
            events.append("job-closed")

    class Process:
        pid = 7677
        returncode: int | None = None

        def terminate(self) -> None:
            events.append("target-stopped")
            self.returncode = -15

        async def wait(self) -> int:
            return int(self.returncode or 0)

    async def spawn(*_argv: str, **_kwargs: object) -> Process:
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
        classmethod(lambda _cls, _name=None: Job()),
    )
    monkeypatch.setattr(process_tree, "_insert_owner_record", lambda *_args, **_kwargs: reference)
    monkeypatch.setattr(
        process_tree,
        "_delete_owner_record",
        lambda _ref: events.append("row-deleted"),
    )
    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", spawn)

    with process_tree.task_process_scope(
        tmp_path / "synthetic-runtime-state",
        session_key="synthetic-session",
        task_id="synthetic-task",
    ):
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await process_tree.create_owned_subprocess_exec("synthetic.exe")

    assert exc_info.value is cancelled
    assert events.index("target-stopped") < events.index("row-deleted")
    assert events.index("job-closed") < events.index("row-deleted")
    assert "release-cancelled" in events
    assert "gate-closed" in events


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


@pytest.mark.skipif(os.name != "nt", reason="requires a native packaged Windows Gateway")
@pytest.mark.asyncio
async def test_windows_packaged_gateway_first_exec_command(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = os.environ.get("OPENSQUILLA_PACKAGED_GATEWAY_BINARY", "")
    if not gateway or not os.path.isfile(gateway):
        pytest.skip("requires OPENSQUILLA_PACKAGED_GATEWAY_BINARY")

    from opensquilla.tools.builtin import shell
    from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context

    monkeypatch.setattr(process_tree.sys, "executable", gateway)
    monkeypatch.setattr(process_tree.sys, "frozen", True, raising=False)
    token = current_tool_context.set(
        ToolContext(
            is_owner=True,
            caller_kind=CallerKind.CLI,
            session_key="packaged-first-exec-command",
            run_mode="full",
            workspace_dir=str(tmp_path),
        )
    )
    try:
        result = await shell.exec_command(
            "Write-Output opensquilla-packaged-first-exec-ok",
            workdir=str(tmp_path),
        )
    finally:
        current_tool_context.reset(token)

    assert "exit_code=0" in result
    assert "opensquilla-packaged-first-exec-ok" in result


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows Job crash semantics")
@pytest.mark.asyncio
async def test_windows_gateway_crash_kills_job_and_reconcile_removes_stale_row(
    tmp_path,
) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    child_pid = tmp_path / "synthetic-child-pid"
    child = (
        "import os,pathlib,time; "
        f"pathlib.Path({str(child_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    crashed_gateway = textwrap.dedent(
        f"""\
        import asyncio
        import os
        import sys
        from pathlib import Path
        from opensquilla import process_tree

        async def run():
            with process_tree.task_process_scope(
                {str(state_dir)!r},
                session_key="synthetic-session",
                task_id="synthetic-task",
            ):
                await process_tree.create_owned_subprocess_exec(
                    sys.executable,
                    "-c",
                    {child!r},
                )
            for _attempt in range(500):
                if Path({str(child_pid)!r}).exists():
                    break
                await asyncio.sleep(0.01)
            os._exit(0)

        asyncio.run(run())
        """
    )

    worker = await asyncio.create_subprocess_exec(sys.executable, "-c", crashed_gateway)
    await asyncio.wait_for(worker.wait(), timeout=15.0)
    pid = int(child_pid.read_text())
    for _attempt in range(500):
        if process_tree._strict_process_start_identity(pid) is None:
            break
        await asyncio.sleep(0.01)
    assert process_tree._strict_process_start_identity(pid) is None
    assert len(process_tree._load_owner_records(state_dir)) == 1
    assert await process_tree.reconcile_persisted_processes(state_dir) == 0
    assert process_tree._load_owner_records(state_dir) == ()


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


def test_owner_registry_is_private_and_stores_only_stable_digests(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    monkeypatch.setattr(
        process_tree,
        "_strict_process_start_identity",
        lambda _pid: "synthetic-start-identity",
    )
    with process_tree.task_process_scope(
        state_dir,
        session_key="synthetic-session-raw",
        task_id="synthetic-task-raw",
        parent_session_key="synthetic-parent-session-raw",
        parent_task_id="synthetic-parent-task-raw",
    ):
        scope = process_tree._CURRENT_TASK_PROCESS_SCOPE.get()
        assert scope is not None
        process_tree._insert_owner_record(
            scope,
            owner_id="1" * 32,
            platform="posix",
            controller_pid=4242,
        )

    database_path = state_dir / process_tree._OWNER_DATABASE_FILENAME
    raw_database = database_path.read_bytes()
    if os.name == "nt":
        assert private_paths.windows_path_has_private_dacl(
            state_dir,
            directory=True,
            require_protected=True,
        )
        assert private_paths.windows_path_has_private_dacl(
            database_path,
            directory=False,
            require_protected=True,
        )
    else:
        assert database_path.stat().st_mode & 0o777 == 0o600
        assert state_dir.stat().st_mode & 0o777 == 0o700
    for forbidden in (
        b"synthetic-session-raw",
        b"synthetic-task-raw",
        b"synthetic-parent-session-raw",
        b"synthetic-parent-task-raw",
        b"synthetic-command",
        b"synthetic-prompt",
        b"SYNTHETIC_SECRET_VALUE",
        os.fsencode(tmp_path),
    ):
        assert forbidden not in raw_database
    records = process_tree._load_owner_records(state_dir)
    assert len(records) == 1
    assert records[0].record.session_digest == process_tree._owner_digest(
        "session", "synthetic-session-raw"
    )
    assert records[0].record.task_digest == process_tree._owner_digest(
        "task", "synthetic-task-raw"
    )


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows DACL inheritance")
def test_windows_owner_registry_sidecar_inherits_private_dacl(tmp_path) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    database_path = state_dir / process_tree._OWNER_DATABASE_FILENAME

    with process_tree._connect_owner_database(database_path) as connection:
        connection.execute("CREATE TABLE synthetic_values (value INTEGER NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO synthetic_values VALUES (1)")
        journal_path = database_path.with_name(f"{database_path.name}-journal")
        assert journal_path.is_file()
        assert private_paths.windows_path_has_private_dacl(
            state_dir,
            directory=True,
            require_protected=True,
        )
        assert private_paths.windows_path_has_private_dacl(
            database_path,
            directory=False,
            require_protected=True,
        )
        assert private_paths.windows_path_has_private_dacl(
            journal_path,
            directory=False,
            require_protected=False,
        )
        connection.rollback()

    assert not journal_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX symlink semantics")
def test_owner_registry_refuses_preexisting_symlink(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    state_dir.mkdir()
    unrelated = tmp_path / "synthetic-unrelated-file"
    unrelated.write_text("unchanged", encoding="utf-8")
    (state_dir / process_tree._OWNER_DATABASE_FILENAME).symlink_to(unrelated)
    monkeypatch.setattr(
        process_tree,
        "_strict_process_start_identity",
        lambda _pid: "synthetic-start-identity",
    )

    with process_tree.task_process_scope(
        state_dir,
        session_key="synthetic-session",
        task_id="synthetic-task",
    ):
        scope = process_tree._CURRENT_TASK_PROCESS_SCOPE.get()
        assert scope is not None
        with pytest.raises(
            process_tree.ProcessTreeOwnershipError,
            match="private regular file",
        ):
            process_tree._insert_owner_record(
                scope,
                owner_id="9" * 32,
                platform="posix",
                controller_pid=4242,
            )

    assert unrelated.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.ci_serial
def test_owner_registry_supports_concurrent_process_writers(tmp_path) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    worker = textwrap.dedent(
        """\
        import os
        import sys
        from pathlib import Path
        from opensquilla import process_tree

        state_dir = Path(sys.argv[1])
        owner_id = sys.argv[2]
        with process_tree.task_process_scope(
            state_dir,
            session_key="synthetic-session",
            task_id=owner_id,
        ):
            scope = process_tree._CURRENT_TASK_PROCESS_SCOPE.get()
            process_tree._insert_owner_record(
                scope,
                owner_id=owner_id,
                platform=process_tree._platform_kind(),
                controller_pid=os.getpid(),
            )
        """
    )

    def write(index: int) -> None:
        subprocess.run(
            [sys.executable, "-c", worker, str(state_dir), f"{index + 1:032x}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15.0,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(12)))

    assert len(process_tree._load_owner_records(state_dir)) == 12


def test_windows_registry_retries_only_transient_file_sharing_failures(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def prepare(_path: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            error = PermissionError("synthetic sharing violation")
            error.winerror = 32
            raise error

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(process_tree, "_prepare_private_file_once", prepare)
    monkeypatch.setattr(process_tree.time, "sleep", lambda _delay: None)

    process_tree._prepare_private_file(tmp_path / "synthetic-registry.sqlite3")

    assert attempts == 3


def test_windows_registry_retries_transient_directory_acl_sharing_failures(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    state_dir.mkdir()
    directory_attempts = 0

    def apply_acl(
        *_args: object,
        directory: bool,
        **_kwargs: object,
    ) -> None:
        nonlocal directory_attempts
        if not directory:
            return
        directory_attempts += 1
        if directory_attempts < 3:
            error = PermissionError("synthetic sharing violation")
            error.winerror = 32
            raise error

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(process_tree, "apply_windows_private_dacl", apply_acl)
    monkeypatch.setattr(process_tree.time, "sleep", lambda _delay: None)

    database_path = state_dir / process_tree._OWNER_DATABASE_FILENAME
    process_tree._prepare_private_file(database_path)

    assert directory_attempts == 3
    assert database_path.is_file()


def test_windows_registry_retries_sidecar_identity_change(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = tmp_path / "synthetic-registry.sqlite3-journal"
    sidecar.write_bytes(b"first")
    attempts = 0

    def replace_once(
        path: object,
        *,
        directory: bool,
        **_kwargs: object,
    ) -> None:
        nonlocal attempts
        assert directory is False
        attempts += 1
        if attempts == 1:
            replacement = sidecar.with_name("replacement.sqlite3-journal")
            replacement.write_bytes(b"replacement")
            os.replace(replacement, sidecar)

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(process_tree, "apply_windows_private_dacl", replace_once)
    monkeypatch.setattr(process_tree.time, "sleep", lambda _delay: None)

    process_tree._prepare_existing_private_file(sidecar)

    assert attempts == 2
    assert sidecar.read_bytes() == b"replacement"


def test_windows_registry_retries_sidecar_identity_change_before_acl(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = tmp_path / "synthetic-registry.sqlite3-journal"
    sidecar.write_bytes(b"first")
    attempts = 0

    def replace_then_fail(
        _path: object,
        *,
        directory: bool,
        **_kwargs: object,
    ) -> None:
        nonlocal attempts
        assert directory is False
        attempts += 1
        if attempts == 1:
            replacement = sidecar.with_name("replacement.sqlite3-journal")
            replacement.write_bytes(b"replacement")
            os.replace(replacement, sidecar)
            raise OSError("synthetic bound path changed")

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(process_tree, "apply_windows_private_dacl", replace_then_fail)
    monkeypatch.setattr(process_tree.time, "sleep", lambda _delay: None)

    process_tree._prepare_existing_private_file(sidecar)

    assert attempts == 2
    assert sidecar.read_bytes() == b"replacement"


def test_windows_registry_sidecar_identity_change_remains_fail_closed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = tmp_path / "synthetic-registry.sqlite3-journal"
    sidecar.write_bytes(b"initial")
    attempts = 0

    def replace_always(
        path: object,
        *,
        directory: bool,
        **_kwargs: object,
    ) -> None:
        nonlocal attempts
        assert directory is False
        attempts += 1
        replacement = sidecar.with_name(f"replacement-{attempts}.sqlite3-journal")
        replacement.write_bytes(str(attempts).encode())
        os.replace(replacement, sidecar)

    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(process_tree, "apply_windows_private_dacl", replace_always)
    monkeypatch.setattr(process_tree.time, "sleep", lambda _delay: None)

    with pytest.raises(
        process_tree.ProcessTreeOwnershipError,
        match="sidecar changed during privacy hardening",
    ):
        process_tree._prepare_existing_private_file(sidecar)

    assert attempts == len(process_tree._WINDOWS_REGISTRY_RETRY_DELAYS_SECONDS) + 1


def test_owner_registry_hardens_preexisting_sqlite_sidecars(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "synthetic-registry.sqlite3"
    existing = [
        database_path,
        *(
            database_path.with_name(f"{database_path.name}{suffix}")
            for suffix in ("-journal", "-shm")
        ),
    ]
    for path in existing:
        path.touch()
    prepared_main: list[object] = []
    prepared_sidecars: list[object] = []
    monkeypatch.setattr(process_tree, "_prepare_private_file", prepared_main.append)
    monkeypatch.setattr(
        process_tree,
        "_prepare_existing_private_file",
        prepared_sidecars.append,
    )

    process_tree._prepare_owner_database_paths(database_path)

    assert prepared_main == [database_path]
    assert prepared_sidecars == existing[1:]


def test_owner_registry_never_recreates_disappeared_sqlite_sidecar(tmp_path) -> None:
    sidecar = tmp_path / "synthetic-registry.sqlite3-journal"

    process_tree._prepare_existing_private_file(sidecar)

    assert not sidecar.exists()


@pytest.mark.parametrize("preexisting", [False, True])
def test_windows_owner_registry_file_acl_failure_is_fail_closed(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    preexisting: bool,
) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    state_dir.mkdir()
    database_path = state_dir / process_tree._OWNER_DATABASE_FILENAME
    if preexisting:
        database_path.write_bytes(b"synthetic-existing-registry")
    monkeypatch.setattr(process_tree.os, "name", "nt")

    def fail_file_acl(
        *_args: object,
        directory: bool,
        **_kwargs: object,
    ) -> None:
        if not directory:
            raise OSError("synthetic ACL failure")

    monkeypatch.setattr(
        process_tree,
        "apply_windows_private_dacl",
        fail_file_acl,
    )

    with pytest.raises(OSError, match="synthetic ACL failure"):
        process_tree._prepare_private_file_once(database_path)

    if preexisting:
        assert database_path.read_bytes() == b"synthetic-existing-registry"
    else:
        assert not database_path.exists()


def test_windows_new_private_directory_acl_failure_removes_empty_directory(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(
        process_tree,
        "create_windows_private_directory",
        lambda path: path.mkdir(),
    )
    monkeypatch.setattr(
        process_tree,
        "apply_windows_private_dacl",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic ACL failure")),
    )

    with pytest.raises(
        process_tree.ProcessTreeOwnershipError,
        match="could not be prepared",
    ):
        process_tree._prepare_private_directory(state_dir)

    assert not state_dir.exists()


def test_owner_claim_serializes_concurrent_cleanup_and_reclaims_dead_claimant(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    database_path = process_tree._owner_database_path(state_dir)
    reference = _synthetic_owner_reference(database_path)
    current_pid = os.getpid()
    monkeypatch.setattr(
        process_tree,
        "_strict_process_start_identity",
        lambda pid: "live-claimant" if pid == current_pid else None,
    )
    with process_tree._connect_owner_database(database_path) as connection:
        connection.execute(
            "INSERT INTO task_process_owners VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reference.record.owner_id,
                process_tree._OWNER_SCHEMA_VERSION,
                reference.record.session_digest,
                reference.record.task_digest,
                None,
                None,
                reference.record.platform,
                reference.record.controller_pid,
                reference.record.controller_start_identity,
            ),
        )
        connection.execute(
            "INSERT INTO task_process_owner_claims VALUES (?, ?, ?, ?)",
            (reference.record.owner_id, "stale-claim", 999999, "dead-claimant"),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                lambda _index: process_tree._claim_owner_record(reference),
                range(2),
            )
        )

    acquired = [claim for claim in claims if claim is not None]
    assert len(acquired) == 1
    assert acquired[0].claim_id != "stale-claim"
    process_tree._release_owner_claim(acquired[0])


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX launch containment")
@pytest.mark.asyncio
async def test_posix_registry_failure_prevents_target_spawn(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Anchor:
        pgid = 5151
        process = SimpleNamespace(pid=5151, returncode=None)

        async def arm(self) -> None:
            events.append("armed")

        def bind(self, _owner: object) -> None:
            events.append("bound")

    async def create_anchor(*_args: object, **_kwargs: object) -> Anchor:
        events.append("anchor-ready")
        return Anchor()

    def fail_persist(*_args: object, **_kwargs: object) -> None:
        events.append("persist-failed")
        raise process_tree.ProcessTreeOwnershipError("synthetic registry failure")

    async def stop_anchor(_anchor: object) -> None:
        events.append("anchor-stopped")

    async def target_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("target helper must remain unspawned")

    monkeypatch.setattr(process_tree, "_create_posix_anchor", create_anchor)
    monkeypatch.setattr(process_tree, "_insert_owner_record", fail_persist)
    monkeypatch.setattr(process_tree, "_stop_unarmed_posix_anchor", stop_anchor)
    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", target_spawn)

    with process_tree.task_process_scope(
        tmp_path / "synthetic-runtime-state",
        session_key="synthetic-session",
        task_id="synthetic-task",
    ):
        with pytest.raises(
            process_tree.ProcessTreeOwnershipError,
            match="failed closed",
        ):
            await process_tree.create_owned_subprocess_exec("synthetic-executable")

    assert events == ["anchor-ready", "persist-failed", "anchor-stopped"]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX launch containment")
@pytest.mark.asyncio
async def test_posix_target_is_released_only_after_persist_and_anchor_arm(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    reference = _synthetic_owner_reference(tmp_path / "registry.sqlite3")

    class Anchor:
        pgid = 5252
        process = SimpleNamespace(pid=5252, returncode=None)

        async def arm(self) -> None:
            events.append("anchor-armed")

        def bind(self, _owner: object) -> None:
            events.append("owner-bound")

    class Gate:
        read_fd = 91

        def child_pass_fds(self, _existing: object) -> tuple[int, ...]:
            return (91,)

        def close_child_end(self) -> None:
            events.append("child-gate-closed")

        def release(self) -> None:
            events.append("target-released")

        def close(self) -> None:
            events.append("gate-closed")

    async def create_anchor(*_args: object, **_kwargs: object) -> Anchor:
        events.append("anchor-ready")
        return Anchor()

    def persist(*_args: object, **_kwargs: object):
        events.append("owner-persisted")
        return reference

    async def spawn(*_args: object, **_kwargs: object):
        events.append("gated-helper-spawned")
        return SimpleNamespace(pid=5353, returncode=None)

    monkeypatch.setattr(process_tree, "_create_posix_anchor", create_anchor)
    monkeypatch.setattr(process_tree, "_insert_owner_record", persist)
    monkeypatch.setattr(
        process_tree._PosixLaunchGate,
        "create",
        classmethod(lambda _cls: Gate()),
    )
    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", spawn)

    with process_tree.task_process_scope(
        tmp_path / "synthetic-runtime-state",
        session_key="synthetic-session",
        task_id="synthetic-task",
    ):
        await process_tree.create_owned_subprocess_exec("synthetic-executable")

    assert events == [
        "anchor-ready",
        "owner-persisted",
        "gated-helper-spawned",
        "child-gate-closed",
        "owner-bound",
        "anchor-armed",
        "target-released",
        "gate-closed",
    ]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX launch containment")
@pytest.mark.asyncio
async def test_posix_launch_cancellation_cleans_before_reraising_same_exception(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    cancelled = asyncio.CancelledError("synthetic caller cancellation")
    reference = _synthetic_owner_reference(tmp_path / "synthetic-registry.sqlite3")

    class Process:
        pid = 5353
        returncode: int | None = None

        def terminate(self) -> None:
            events.append("target-stopped")
            self.returncode = -15

        async def wait(self) -> int:
            return int(self.returncode or 0)

    class Anchor:
        pgid = 5252
        process = SimpleNamespace(pid=5252, returncode=None)

        async def arm(self) -> None:
            events.append("anchor-arm-cancelled")
            raise cancelled

        def bind(self, _owner: object) -> None:
            events.append("owner-bound")

    class Gate:
        read_fd = 91

        def child_pass_fds(self, _existing: object) -> tuple[int, ...]:
            return (91,)

        def close_child_end(self) -> None:
            events.append("child-gate-closed")

        def release(self) -> None:
            raise AssertionError("cancelled target must not be released")

        def close(self) -> None:
            events.append("gate-closed")

    async def create_anchor(*_args: object, **_kwargs: object) -> Anchor:
        return Anchor()

    async def spawn(*_args: object, **_kwargs: object) -> Process:
        events.append("target-spawned")
        return Process()

    async def stop_anchor(_anchor: object) -> None:
        events.append("anchor-stopped")

    monkeypatch.setattr(process_tree, "_create_posix_anchor", create_anchor)
    monkeypatch.setattr(process_tree, "_insert_owner_record", lambda *_args, **_kwargs: reference)
    monkeypatch.setattr(
        process_tree,
        "_delete_owner_record",
        lambda _ref: events.append("row-deleted"),
    )
    monkeypatch.setattr(process_tree, "_stop_unarmed_posix_anchor", stop_anchor)
    monkeypatch.setattr(
        process_tree._PosixLaunchGate,
        "create",
        classmethod(lambda _cls: Gate()),
    )
    monkeypatch.setattr(process_tree.asyncio, "create_subprocess_exec", spawn)

    with process_tree.task_process_scope(
        tmp_path / "synthetic-runtime-state",
        session_key="synthetic-session",
        task_id="synthetic-task",
    ):
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await process_tree.create_owned_subprocess_exec("synthetic-executable")

    assert exc_info.value is cancelled
    assert events.index("target-stopped") < events.index("row-deleted")
    assert events.index("anchor-stopped") < events.index("row-deleted")
    assert "owner-bound" in events
    assert "target-spawned" in events
    assert "child-gate-closed" in events


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX owner lifecycle")
@pytest.mark.asyncio
async def test_natural_process_tree_exit_reclaims_persisted_row(tmp_path) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    with process_tree.task_process_scope(
        state_dir,
        session_key="synthetic-session",
        task_id="synthetic-task",
    ):
        process = await process_tree.create_owned_subprocess_exec(
            sys.executable,
            "-c",
            "pass",
        )
    owner = process_tree.capture_process_tree_owner(process, isolated=True)
    await asyncio.wait_for(process.wait(), timeout=5.0)
    for _attempt in range(300):
        if not process_tree._load_owner_records(state_dir):
            break
        await asyncio.sleep(0.01)
    assert process_tree._load_owner_records(state_dir) == ()
    assert await owner.terminate(graceful_timeout=0.0, kill_timeout=1.0)


@pytest.mark.asyncio
async def test_windows_job_completion_monitor_reclaims_persisted_row(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[str] = []

    class Job:
        def __init__(self) -> None:
            self.counts = iter((1, 0))
            self.closed = False

        def active_process_count(self) -> int:
            return next(self.counts)

        def close_if_empty(self) -> bool:
            self.closed = True
            return True

    job = Job()
    reference = _synthetic_owner_reference(
        tmp_path / "registry.sqlite3",
        platform="windows",
    )
    monkeypatch.setattr(process_tree, "_WindowsJob", Job)
    monkeypatch.setattr(
        process_tree,
        "_delete_owner_record",
        lambda row: deleted.append(row.record.owner_id),
    )
    owner = process_tree.ProcessTreeOwner(
        process=SimpleNamespace(returncode=0),
        pid=reference.record.controller_pid,
        windows_job=job,
        persisted_owner=reference,
    )

    owner.start_completion_monitor()
    assert owner._completion_monitor is not None
    await asyncio.wait_for(owner._completion_monitor, timeout=1.0)

    assert job.closed is True
    assert deleted == [reference.record.owner_id]


def test_pid_identity_mismatch_reclaims_stale_row_without_signalling(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _synthetic_owner_reference(tmp_path / "registry.sqlite3")
    deleted: list[str] = []
    monkeypatch.setattr(process_tree, "_posix_controller_matches", lambda _record: False)
    monkeypatch.setattr(
        process_tree,
        "_delete_owner_record",
        lambda row: deleted.append(row.record.owner_id),
    )
    monkeypatch.setattr(
        process_tree.socket,
        "socket",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale PID must not reach process control")
        ),
    )

    assert process_tree._terminate_persisted_posix_owner_sync(reference) is False
    assert deleted == [reference.record.owner_id]


@pytest.mark.parametrize(
    ("term_markers", "kill_markers", "stop_command", "expected"),
    [
        (
            [process_tree._POSIX_ANCHOR_LEGACY_TERMINATED],
            [],
            process_tree._POSIX_ANCHOR_KILL,
            True,
        ),
        (
            [process_tree._POSIX_ANCHOR_CAPTURED],
            [process_tree._POSIX_ANCHOR_CAPTURED, process_tree._POSIX_ANCHOR_INCOMPLETE],
            process_tree._POSIX_ANCHOR_KILL,
            False,
        ),
    ],
)
def test_persisted_posix_stop_handles_anchor_cleanup_markers(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    term_markers: list[bytes],
    kill_markers: list[bytes],
    stop_command: bytes,
    expected: bool,
) -> None:
    reference = _synthetic_owner_reference(tmp_path / "registry.sqlite3")
    deleted: list[str] = []
    stopped = False
    responses = {
        process_tree._POSIX_ANCHOR_TERMINATE: list(term_markers),
        process_tree._POSIX_ANCHOR_KILL: list(kill_markers),
    }

    class Socket:
        command = b""

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.command = b""

        def __enter__(self) -> Socket:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def settimeout(self, _timeout: float) -> None:
            return None

        def connect(self, _path: str) -> None:
            return None

        def sendall(self, command: bytes) -> None:
            nonlocal stopped
            self.command = command
            if command == stop_command and not responses[command]:
                stopped = True

        def recv(self, _size: int) -> bytes:
            nonlocal stopped
            markers = responses[self.command]
            if not markers:
                return b""
            marker = markers.pop(0)
            if self.command == stop_command and not markers:
                stopped = True
            return marker

    monkeypatch.setattr(process_tree.socket, "AF_UNIX", 1, raising=False)
    monkeypatch.setattr(process_tree.socket, "socket", Socket)
    monkeypatch.setattr(process_tree, "_posix_controller_matches", lambda _record: not stopped)
    monkeypatch.setattr(
        process_tree,
        "_delete_owner_record",
        lambda row: deleted.append(row.record.owner_id),
    )
    monotonic_values = iter(float(value) for value in range(10))
    monkeypatch.setattr(process_tree.time, "monotonic", lambda: next(monotonic_values))

    assert process_tree._terminate_persisted_posix_owner_sync(reference) is expected
    assert deleted == [reference.record.owner_id]


@pytest.mark.skipif(os.name != "posix", reason="AF_UNIX control endpoint is POSIX-specific")
def test_live_posix_owner_with_unavailable_control_endpoint_retains_row(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _synthetic_owner_reference(tmp_path / "registry.sqlite3")
    deleted: list[str] = []
    monkeypatch.setattr(process_tree, "_posix_controller_matches", lambda _record: True)
    monkeypatch.setattr(
        process_tree,
        "_delete_owner_record",
        lambda row: deleted.append(row.record.owner_id),
    )

    assert process_tree._terminate_persisted_posix_owner_sync(reference) is False
    assert deleted == []


def test_windows_controller_identity_mismatch_reclaims_row_without_opening_job(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _synthetic_owner_reference(
        tmp_path / "registry.sqlite3",
        platform="windows",
    )
    deleted: list[str] = []
    monkeypatch.setattr(process_tree, "_controller_identity_matches", lambda _record: False)
    monkeypatch.setattr(
        process_tree,
        "_delete_owner_record",
        lambda row: deleted.append(row.record.owner_id),
    )
    monkeypatch.setattr(
        process_tree._WindowsJob,
        "open",
        classmethod(
            lambda _cls, _name: (_ for _ in ()).throw(
                AssertionError("reused PID must not open a named Job")
            )
        ),
    )

    assert process_tree._terminate_persisted_windows_owner_sync(reference) is False
    assert deleted == [reference.record.owner_id]


@pytest.mark.parametrize(
    ("mode", "stopped", "row_deleted"),
    [
        ("unavailable", False, False),
        ("membership-mismatch", False, False),
        ("terminated", True, True),
    ],
)
def test_windows_recovered_job_cleanup_is_fail_safe(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    stopped: bool,
    row_deleted: bool,
) -> None:
    reference = _synthetic_owner_reference(
        tmp_path / "registry.sqlite3",
        platform="windows",
    )
    deleted: list[str] = []

    class Job:
        def contains_pid(self, _pid: int) -> bool:
            return mode != "membership-mismatch"

        def terminate(self) -> None:
            return None

        def active_process_count(self) -> int:
            return 0

        def close(self) -> None:
            return None

    def open_job(_cls: object, _name: str) -> Job:
        if mode == "unavailable":
            raise OSError("synthetic unavailable job")
        return Job()

    monkeypatch.setattr(process_tree, "_controller_identity_matches", lambda _row: True)
    monkeypatch.setattr(process_tree._WindowsJob, "open", classmethod(open_job))
    monkeypatch.setattr(
        process_tree,
        "_delete_owner_record",
        lambda row: deleted.append(row.record.owner_id),
    )

    assert process_tree._terminate_persisted_windows_owner_sync(reference) is stopped
    assert bool(deleted) is row_deleted


def test_task_lineage_requires_parent_session_and_task_identity(tmp_path) -> None:
    root_session = "1" * 64
    root_task = "2" * 64
    child_session = "3" * 64
    child_task = "4" * 64
    references = (
        _synthetic_owner_reference(
            tmp_path / "registry.sqlite3",
            owner_id="1" * 32,
            session_digest=root_session,
            task_digest=root_task,
        ),
        _synthetic_owner_reference(
            tmp_path / "registry.sqlite3",
            owner_id="2" * 32,
            session_digest=child_session,
            task_digest=child_task,
            parent_session_digest=root_session,
            parent_task_digest=root_task,
        ),
        _synthetic_owner_reference(
            tmp_path / "registry.sqlite3",
            owner_id="3" * 32,
            session_digest="5" * 64,
            task_digest="6" * 64,
            parent_session_digest="7" * 64,
            parent_task_digest=root_task,
        ),
    )

    selected = process_tree._task_owned_records(
        references,
        session_digest=root_session,
        task_digest=root_task,
    )

    assert [row.record.owner_id for row in selected] == ["1" * 32, "2" * 32]


@pytest.mark.asyncio
async def test_unsupported_platform_record_is_retained_fail_safe(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = _synthetic_owner_reference(
        tmp_path / "registry.sqlite3",
        platform="windows",
    )
    deleted: list[str] = []
    monkeypatch.setattr(process_tree, "_platform_kind", lambda: "posix")
    monkeypatch.setattr(
        process_tree,
        "_delete_owner_record",
        lambda row: deleted.append(row.record.owner_id),
    )

    assert await process_tree._terminate_persisted_owner(reference) is False
    assert deleted == []


@pytest.mark.asyncio
async def test_absent_and_newer_owner_registry_are_upgrade_safe(tmp_path) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    assert (
        await process_tree.cancel_persisted_processes_for_task(
            state_dir,
            "synthetic-session",
            "synthetic-task",
        )
        == 0
    )
    database_path = process_tree._owner_database_path(state_dir)
    reference = _synthetic_owner_reference(database_path)
    with process_tree._connect_owner_database(database_path) as connection:
        connection.execute(
            "INSERT INTO task_process_owners VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                reference.record.owner_id,
                process_tree._OWNER_SCHEMA_VERSION + 1,
                reference.record.session_digest,
                reference.record.task_digest,
                None,
                None,
                reference.record.platform,
                reference.record.controller_pid,
                reference.record.controller_start_identity,
            ),
        )
        connection.execute(
            "INSERT INTO task_process_owners VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "d" * 32,
                "synthetic-invalid-version",
                reference.record.session_digest,
                reference.record.task_digest,
                None,
                None,
                reference.record.platform,
                reference.record.controller_pid,
                reference.record.controller_start_identity,
            ),
        )

    assert process_tree._load_owner_records(state_dir) == ()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM task_process_owners").fetchone() == (2,)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX crash recovery")
@pytest.mark.asyncio
async def test_concurrent_stop_after_gateway_crash_kills_deep_tree_and_isolates_other_task(
    tmp_path,
) -> None:
    state_dir = tmp_path / "synthetic-runtime-state"
    pid_file = tmp_path / "synthetic-owned-pids"
    grandchild = (
        "import os,pathlib,time; "
        f"pathlib.Path({str(pid_file)!r}).open('a').write(str(os.getpid())+'\\n'); "
        "time.sleep(30)"
    )
    child = (
        "import os,pathlib,subprocess,sys,time; "
        f"pathlib.Path({str(pid_file)!r}).open('a').write(str(os.getpid())+'\\n'); "
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]); "
        "time.sleep(30)"
    )
    target = (
        "import os,pathlib,subprocess,sys,time; "
        f"pathlib.Path({str(pid_file)!r}).open('a').write(str(os.getpid())+'\\n'); "
        f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
        "time.sleep(30)"
    )
    crashed_gateway = textwrap.dedent(
        f"""\
        import asyncio
        import os
        import sys
        from pathlib import Path
        from opensquilla import process_tree

        async def run():
            with process_tree.task_process_scope(
                {str(state_dir)!r},
                session_key="synthetic-session",
                task_id="synthetic-task",
            ):
                await process_tree.create_owned_subprocess_exec(
                    sys.executable,
                    "-c",
                    {target!r},
                )
            for _attempt in range(500):
                path = Path({str(pid_file)!r})
                if path.exists() and len(path.read_text().splitlines()) >= 3:
                    break
                await asyncio.sleep(0.01)
            os._exit(0)

        asyncio.run(run())
        """
    )
    worker = await asyncio.create_subprocess_exec(sys.executable, "-c", crashed_gateway)
    other_process = None
    owned_pids: list[int] = []
    try:
        await asyncio.wait_for(worker.wait(), timeout=10.0)
        owned_pids = [int(value) for value in pid_file.read_text().splitlines()]
        assert len(owned_pids) == 3
        with process_tree.task_process_scope(
            state_dir,
            session_key="synthetic-other-session",
            task_id="synthetic-other-task",
        ):
            other_process = await process_tree.create_owned_subprocess_exec(
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            )

        results = await asyncio.gather(
            process_tree.cancel_persisted_processes_for_task(
                state_dir,
                "synthetic-session",
                "synthetic-task",
            ),
            process_tree.cancel_persisted_processes_for_task(
                state_dir,
                "synthetic-session",
                "synthetic-task",
            ),
        )
        assert sum(results) == 1
        assert other_process.returncode is None
        for _attempt in range(300):
            if all(not process_tree._strict_process_start_identity(pid) for pid in owned_pids):
                break
            await asyncio.sleep(0.01)
        assert all(not process_tree._strict_process_start_identity(pid) for pid in owned_pids)
        remaining = process_tree._load_owner_records(state_dir)
        assert len(remaining) == 1
        assert remaining[0].record.session_digest == process_tree._owner_digest(
            "session", "synthetic-other-session"
        )
    finally:
        await process_tree.reconcile_persisted_processes(state_dir)
        if other_process is not None and other_process.returncode is None:
            other_owner = process_tree.capture_process_tree_owner(other_process, isolated=True)
            await other_owner.terminate(graceful_timeout=0.1, kill_timeout=1.0)
        if other_process is not None:
            await asyncio.wait_for(other_process.wait(), timeout=2.0)
        for pid in owned_pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


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
