from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from opensquilla import process_tree


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


@pytest.mark.asyncio
async def test_non_durable_owner_never_widens_cleanup_to_a_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group_signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        process_tree.os,
        "killpg",
        lambda pgid, sig: group_signals.append((pgid, sig)),
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


def test_windows_job_assignment_failure_is_a_fail_closed_platform_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 6262
        returncode = None
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

    proc = Process()
    monkeypatch.setattr(process_tree.os, "name", "nt")
    monkeypatch.setattr(
        process_tree._WindowsJob,
        "assign",
        classmethod(lambda _cls, _pid: (_ for _ in ()).throw(OSError("denied"))),
    )

    with pytest.raises(process_tree.ProcessTreeOwnershipError, match="Job Object"):
        process_tree.capture_process_tree_owner(proc, isolated=True)

    assert proc.terminated is True


@pytest.mark.asyncio
async def test_windows_controlled_launcher_assignment_failure_stops_unreleased_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Gate:
        name = "test-gate"

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
