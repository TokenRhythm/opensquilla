from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from opensquilla.sandbox.backend.seatbelt_resources import DarwinResourceGuard
from opensquilla.sandbox.types import ResourceLimits, SandboxBackendError


def _process(pid: int, *, pgid: int, ppid: int = 1, identity: str = "") -> SimpleNamespace:
    return SimpleNamespace(pid=pid, pgid=pgid, ppid=ppid, start_identity=identity or str(pid))


@pytest.fixture
def monitored_tree(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    anchor = _process(100, pgid=100)
    target = _process(200, pgid=100)
    child = _process(300, pgid=100, ppid=200)
    state = SimpleNamespace(
        snapshot={item.pid: item for item in (anchor, target, child)},
        current={},
        rss={100: 100 * 1024 * 1024, 200: 2 * 1024 * 1024, 300: 1024 * 1024},
        active=True,
    )
    owner = SimpleNamespace(pid=target.pid, pgid=anchor.pid, is_active=lambda: state.active)
    guard = object.__new__(DarwinResourceGuard)
    guard.library = object()
    guard.limits = ResourceLimits(cpu_seconds=1, memory_mb=4, pids=2)
    guard.known = {}
    monkeypatch.setattr("opensquilla.process_tree._darwin_process_snapshot", lambda: state.snapshot)
    monkeypatch.setattr(
        "opensquilla.process_tree._darwin_process_info",
        lambda pid, _library: state.current.get(pid, state.snapshot.get(pid)),
    )
    monkeypatch.setattr(
        guard, "_read_task", lambda pid: SimpleNamespace(resident_size=state.rss[pid]),
    )
    state.owner, state.guard = owner, guard
    return state


def test_monitor_follows_anchor_after_target_exit(monitored_tree: SimpleNamespace) -> None:
    state = monitored_tree
    del state.snapshot[state.owner.pid]

    state.guard._check(state.owner)

    assert state.guard.known == {300: "300"}


def test_monitor_excludes_reused_target_pid(monitored_tree: SimpleNamespace) -> None:
    state = monitored_tree
    state.guard._check(state.owner)
    state.snapshot[200] = _process(200, pgid=900, identity="unrelated")
    state.rss[200] = 100 * 1024 * 1024

    state.guard._check(state.owner)

    assert state.guard.known == {300: "300"}


def test_monitor_retains_detached_descendant_identity(monitored_tree: SimpleNamespace) -> None:
    state = monitored_tree
    state.guard._check(state.owner)
    state.snapshot[300] = _process(300, pgid=300)
    state.rss[300] = 5 * 1024 * 1024

    with pytest.raises(SandboxBackendError, match="resident memory limit exceeded"):
        state.guard._check(state.owner)


def test_monitor_ignores_pid_reused_during_memory_read(monitored_tree: SimpleNamespace) -> None:
    state = monitored_tree
    state.current[300] = _process(300, pgid=900, identity="reused")
    state.rss[300] = 100 * 1024 * 1024

    state.guard._check(state.owner)


@pytest.mark.parametrize("missing", [True, False], ids=["missing", "changed-group"])
def test_monitor_rejects_missing_or_changed_anchor(
    monitored_tree: SimpleNamespace, missing: bool,
) -> None:
    state = monitored_tree
    if missing:
        del state.snapshot[100]
    else:
        state.snapshot[100] = _process(100, pgid=900)

    with pytest.raises(SandboxBackendError, match="lost its process owner"):
        state.guard._check(state.owner)


def test_monitor_ignores_closed_owner(monitored_tree: SimpleNamespace) -> None:
    state = monitored_tree
    state.active = False
    state.snapshot.clear()

    state.guard._check(state.owner)


def test_monitor_counts_target_memory_and_processes(monitored_tree: SimpleNamespace) -> None:
    state = monitored_tree
    state.rss[200] = 5 * 1024 * 1024
    with pytest.raises(SandboxBackendError, match="resident memory limit exceeded"):
        state.guard._check(state.owner)

    state.rss[200] = 2 * 1024 * 1024
    state.snapshot[400] = _process(400, pgid=100)
    with pytest.raises(SandboxBackendError, match="process count limit exceeded"):
        state.guard._check(state.owner)


@pytest.mark.skipif(sys.platform != "darwin", reason="Native macOS process monitor probe")
async def test_native_monitor_tracks_child_after_parent_exit(tmp_path) -> None:
    from opensquilla.process_tree import create_owned_subprocess_exec

    release_path = tmp_path / "release-child"
    child_code = (
        "import pathlib,sys,time; "
        "p=pathlib.Path(sys.argv[1]); "
        "print('child-ready',flush=True); "
        "exec('while not p.exists(): time.sleep(.01)'); "
        "print('child-finished',flush=True)"
    )
    parent_code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])"
    )
    process = await create_owned_subprocess_exec(
        sys.executable, "-c", parent_code, child_code, str(release_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    owner = process._opensquilla_process_tree_owner
    try:
        assert await asyncio.wait_for(process.stdout.readline(), timeout=5) == b"child-ready\n"
        async with asyncio.timeout(5):
            while process.returncode is None:
                await asyncio.sleep(0.01)
        assert process.returncode == 0
        assert owner.is_active()
        guard = DarwinResourceGuard(ResourceLimits(cpu_seconds=5, memory_mb=512, pids=8))

        guard._check(owner)

        release_path.touch()
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        assert stdout == b"child-finished\n"
        assert stderr == b""
    finally:
        release_path.touch()
        await owner.terminate(graceful_timeout=0, kill_timeout=1)
