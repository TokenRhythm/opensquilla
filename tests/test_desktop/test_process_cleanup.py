from __future__ import annotations

import asyncio
import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla import desktop_process_cleanup as cleanup
from opensquilla import process_tree

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Desktop independent POSIX cleanup")
_NONCE = "n" * 43
_OTHER_NONCE = "o" * 43


@pytest.fixture(autouse=True)
def isolated_launch_scope(monkeypatch):
    monkeypatch.setattr(cleanup, "_active_scope", None)


def _configure(tmp_path: Path, nonce: str = _NONCE) -> tuple[Path, Path]:
    control, state = tmp_path / "control", tmp_path / "custom runtime"
    cleanup.configure_desktop_process_cleanup(
        ownership_dir=control, instance_nonce=nonce, state_dir=state,
    )
    return control, state


def _cleanup(control: Path, nonce: str = _NONCE, timeout: float = 4.5) -> bool:
    return cleanup.cleanup_desktop_processes(control, nonce, deadline=time.monotonic() + timeout)


def test_cleanup_before_startup_fences_future_activation(tmp_path):
    control = tmp_path / "control"
    assert _cleanup(control)
    with pytest.raises(RuntimeError, match="closed"):
        cleanup.configure_desktop_process_cleanup(
            ownership_dir=control, instance_nonce=_NONCE, state_dir=tmp_path / "state",
        )


def test_signed_manifest_survives_global_record_removal_and_rejects_tampering(tmp_path):
    control, state = _configure(tmp_path)
    (control / "desktop-gateway.json").write_text('{"unrelated":"replacement"}')
    assert _cleanup(control)
    manifest = cleanup._manifest_path(control, cleanup._launch_digest(_NONCE))
    value = json.loads(manifest.read_text())
    value["state_dir"] = str(tmp_path / "unrelated-state")
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="verified"):
        _cleanup(control)
    assert state.is_dir()
    assert not (tmp_path / "unrelated-state").exists()


def test_fallback_scope_and_changed_state_location_fail_closed(tmp_path):
    _control, state = _configure(tmp_path)
    scope = process_tree._current_task_process_scope()
    assert scope is not None and scope.state_dir == state.resolve()
    with process_tree.task_process_scope(
        tmp_path / "changed", session_key="synthetic-session", task_id="synthetic-task",
    ), pytest.raises(process_tree.ProcessTreeOwnershipError, match="state location"):
        process_tree._current_task_process_scope()


def test_closed_launch_rejects_owner_registration_before_target_release(tmp_path):
    control, state = _configure(tmp_path)
    scope = process_tree._current_task_process_scope()
    assert scope is not None
    assert _cleanup(control)
    with pytest.raises(process_tree.ProcessTreeOwnershipError, match="closing"):
        process_tree._insert_owner_record(
            scope, owner_id="a" * 32, platform=process_tree._platform_kind(),
            controller_pid=os.getpid(),
        )
    assert process_tree._load_owner_records(state) == ()


def test_incomplete_owner_receipt_cannot_be_overwritten_by_racing_success(tmp_path):
    control, state = _configure(tmp_path)
    scope = process_tree._current_task_process_scope()
    assert scope is not None
    reference = process_tree._insert_owner_record(
        scope, owner_id="a" * 32, platform=process_tree._platform_kind(),
        controller_pid=os.getpid(),
    )
    process_tree._delete_owner_record(reference, cleanup_complete=False)
    process_tree._delete_owner_record(reference, cleanup_complete=True)
    assert process_tree._load_owner_records(state) == ()
    assert not _cleanup(control)


def test_natural_completion_racing_helper_uses_positive_receipt(tmp_path, monkeypatch):
    control, _state = _configure(tmp_path)
    scope = process_tree._current_task_process_scope()
    assert scope is not None
    reference = process_tree._insert_owner_record(
        scope, owner_id="a" * 32, platform=process_tree._platform_kind(),
        controller_pid=os.getpid(),
    )
    def finish(_reference):
        process_tree._delete_owner_record(reference, cleanup_complete=True)
        return False
    monkeypatch.setattr(process_tree, "_terminate_persisted_posix_owner_sync", finish)
    assert _cleanup(control)


def test_cleanup_waits_for_existing_claim_and_observes_receipt(tmp_path, monkeypatch):
    control, _state = _configure(tmp_path)
    scope = process_tree._current_task_process_scope()
    assert scope is not None
    reference = process_tree._insert_owner_record(
        scope, owner_id="a" * 32, platform=process_tree._platform_kind(),
        controller_pid=os.getpid(),
    )
    attempts = []
    def claimed(_reference):
        attempts.append(True)
        process_tree._delete_owner_record(reference, cleanup_complete=True)
        return None
    monkeypatch.setattr(process_tree, "_claim_owner_record", claimed)
    monkeypatch.setattr(
        process_tree, "_terminate_persisted_posix_owner_sync",
        lambda _reference: pytest.fail("must not compete with a live cleanup claim"),
    )
    assert _cleanup(control)
    assert attempts == [True]


@pytest.mark.parametrize("frozen", [False, True])
def test_anchor_identity_accepts_spaces_and_unmatched_quotes_in_ps_output(monkeypatch, frozen):
    owner_id = "a" * 32
    marker = "--internal-child process-tree" if frozen else "-m opensquilla.process_tree"
    output = f"/synthetic/user's path/Python {marker} --posix-group-anchor {owner_id} /tmp/control"
    monkeypatch.setattr(process_tree.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_tree.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=output,
        ),
    )
    record = SimpleNamespace(controller_pid=4242, owner_id=owner_id)
    assert process_tree._posix_anchor_command_matches(record)
    output = output.replace(owner_id, owner_id + "b")
    assert not process_tree._posix_anchor_command_matches(record)


_PARENT = textwrap.dedent('''
    import asyncio, json, os, sys, threading
    from pathlib import Path
    from opensquilla import process_tree
    from opensquilla.desktop_process_cleanup import configure_desktop_process_cleanup
    async def run():
        control, state, nonce, mode = sys.argv[1:]
        configure_desktop_process_cleanup(
            ownership_dir=Path(control), instance_nonce=nonce, state_dir=state)
        descendant_file = Path(control) / "synthetic-descendant"
        program = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(120)"
        if mode == "nested":
            program = (
                "import subprocess,sys,time,signal;from pathlib import Path;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "p=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "time.sleep(120)'],"
                "start_new_session=True);"
                f"Path({str(descendant_file)!r}).write_text(str(p.pid));time.sleep(120)"
            )
        process = await process_tree.create_owned_subprocess_exec(sys.executable, "-c", program)
        owner = process_tree.capture_process_tree_owner(process, isolated=True)
        if mode == "nested":
            while not descendant_file.exists():
                await asyncio.sleep(.01)
        pids = [process.pid, owner.posix_anchor.process.pid]
        if mode == "nested":
            pids.append(int(descendant_file.read_text()))
        print(json.dumps(pids), flush=True)
        threading.Event().wait(120)
    asyncio.run(run())
''')


def _start_parent(control: Path, state: Path, nonce: str, mode: str):
    parent = subprocess.Popen(
        [sys.executable, "-c", _PARENT, str(control), str(state), nonce, mode],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert parent.stdout is not None
    ready, _, _ = select.select([parent.stdout], [], [], 8)
    if not ready:
        parent.kill()
        _stdout, stderr = parent.communicate(timeout=3)
        pytest.fail(f"Synthetic parent did not initialize: {stderr}")
    raw = parent.stdout.readline()
    if not raw:
        _stdout, stderr = parent.communicate(timeout=3)
        pytest.fail(f"Synthetic parent failed: {stderr}")
    return parent, json.loads(raw)


def _pid_running(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip()) and "Z" not in result.stdout


def _stop_parent(parent, pids):
    if parent.poll() is None:
        parent.kill()
    for pid in pids:
        if _pid_running(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    parent.communicate(timeout=3)


def test_independent_cli_stops_blocked_parent_tools_and_preserves_other_launch(tmp_path):
    state = tmp_path / "shared-custom-state"
    first, first_pids = _start_parent(tmp_path / "first", state, _NONCE, "nested")
    second = None
    second_pids = []
    try:
        second, second_pids = _start_parent(tmp_path / "second", state, _OTHER_NONCE, "plain")
        started = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "opensquilla.process_tree", "--desktop-quit-cleanup"],
            input=json.dumps({
                "ownership_dir": str(tmp_path / "first"), "instance_nonce": _NONCE,
                "timeout_ms": 4500,
            }), capture_output=True, text=True, timeout=6,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert json.loads(result.stdout) == {"protocol": cleanup.PROTOCOL, "complete": True}
        assert time.monotonic() - started < 6
        assert first.poll() is None  # The Gateway loop is still blocked, proving independence.
        assert all(not _pid_running(pid) for pid in first_pids)
        assert all(_pid_running(pid) for pid in second_pids)
    finally:
        _stop_parent(first, first_pids)
        if second is not None:
            _stop_parent(second, second_pids)


@pytest.mark.asyncio
async def test_cleaner_controls_unarmed_anchor_without_releasing_target(tmp_path):
    control, state = _configure(tmp_path)
    scope = process_tree._current_task_process_scope()
    assert scope is not None
    owner_id = "a" * 32
    socket = process_tree._owner_control_path(process_tree._owner_database_path(state), owner_id)
    anchor = await process_tree._create_posix_anchor(owner_id, control_path=socket)
    try:
        process_tree._insert_owner_record(
            scope, owner_id=owner_id, platform=process_tree._platform_kind(),
            controller_pid=anchor.process.pid,
        )
        assert await asyncio.to_thread(_cleanup, control)
        await asyncio.wait_for(anchor.process.wait(), 2)
    finally:
        await process_tree._stop_unarmed_posix_anchor(anchor)


@pytest.mark.asyncio
async def test_cleaner_controls_empty_anchor_while_parent_has_not_released_it(tmp_path):
    control, state = _configure(tmp_path)
    scope = process_tree._current_task_process_scope()
    assert scope is not None
    owner_id = "a" * 32
    socket = process_tree._owner_control_path(process_tree._owner_database_path(state), owner_id)
    anchor = await process_tree._create_posix_anchor(owner_id, control_path=socket)
    try:
        process_tree._insert_owner_record(
            scope, owner_id=owner_id, platform=process_tree._platform_kind(),
            controller_pid=anchor.process.pid,
        )
        anchor.process.stdin.write(process_tree._POSIX_ANCHOR_ARM)
        await anchor.process.stdin.drain()
        assert await asyncio.wait_for(
            anchor.process.stdout.read(1), 3,
        ) == process_tree._POSIX_ANCHOR_ARMED
        assert await asyncio.wait_for(
            anchor.process.stdout.read(1), 3,
        ) == process_tree._POSIX_ANCHOR_EMPTY
        assert anchor.process.returncode is None
        assert await asyncio.to_thread(_cleanup, control)
        await asyncio.wait_for(anchor.process.wait(), 2)
    finally:
        await process_tree._stop_unarmed_posix_anchor(anchor)


@pytest.mark.asyncio
async def test_quit_after_registration_never_releases_later_gated_target(tmp_path, monkeypatch):
    import socket
    import threading

    _control, state = _configure(tmp_path)
    registered = threading.Event()
    resume = threading.Event()
    inserted = []
    original = process_tree._insert_owner_record
    def pause_after_registration(*args, **kwargs):
        reference = original(*args, **kwargs)
        inserted.append(reference)
        registered.set()
        assert resume.wait(5)
        return reference
    monkeypatch.setattr(process_tree, "_insert_owner_record", pause_after_registration)
    target_marker = tmp_path / "must-not-run"
    launch = asyncio.create_task(process_tree.create_owned_subprocess_exec(
        sys.executable, "-c", f"from pathlib import Path;Path({str(target_marker)!r}).touch()",
    ))
    try:
        assert await asyncio.to_thread(registered.wait, 3)
        reference = inserted[0]
        path = process_tree._owner_control_path(
            process_tree._owner_database_path(state), reference.record.owner_id,
        )
        def request_stop():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(path))
                client.sendall(process_tree._POSIX_ANCHOR_TERMINATE)
                assert client.recv(1) == process_tree._POSIX_ANCHOR_CAPTURED
        await asyncio.to_thread(request_stop)
        resume.set()
        with pytest.raises(process_tree.ProcessTreeOwnershipError):
            await asyncio.wait_for(launch, 4)
        assert not target_marker.exists()
        assert _cleanup(_control)
    finally:
        resume.set()
        if not launch.done():
            launch.cancel()
        with contextlib.suppress(BaseException):
            await launch


@pytest.mark.parametrize("scoped", [False, True])
def test_pty_lost_launch_ack_closes_control_fds_and_reaps_owned_anchor(
    tmp_path, monkeypatch, scoped,
):
    state = tmp_path / "state"
    if scoped:
        _control, state = _configure(tmp_path)
    pipes = []
    processes = []
    original_init = process_tree._PtyControlPipe.__init__
    original_read = os.read
    target_pid_file = tmp_path / "synthetic-pty-target"
    def track_pipe(self, fd, mode):
        original_init(self, fd, mode)
        pipes.append(self)
    def lose_ack(fd, size):
        value = original_read(fd, size)
        if value == process_tree._POSIX_ANCHOR_ARMED:
            deadline = time.monotonic() + 2
            while not target_pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert target_pid_file.exists()
            return b"?"
        return value
    def spawn(argv, **kwargs):
        process = subprocess.Popen(
            argv, start_new_session=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, **kwargs,
        )
        processes.append(process)
        return process
    monkeypatch.setattr(process_tree._PtyControlPipe, "__init__", track_pipe)
    monkeypatch.setattr(process_tree.os, "read", lose_ack)
    try:
        with pytest.raises(process_tree.ProcessTreeOwnershipError, match="refused target"):
            process_tree.create_owned_posix_pty(
                [sys.executable, "-c", (
                    "import os,time;from pathlib import Path;"
                    f"Path({str(target_pid_file)!r}).write_text(str(os.getpid()));time.sleep(120)"
                )], spawn,
            )
        assert not _pid_running(int(target_pid_file.read_text()))
        assert len(pipes) == 2 and all(pipe.file.closed for pipe in pipes)
        assert all(process.poll() is not None for process in processes)
        assert process_tree._load_owner_records(state) == ()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_desktop_ownership_acquire_failure_deactivates_process_scope(tmp_path, monkeypatch):
    from opensquilla.gateway import desktop_ownership
    from opensquilla.recovery.locking import profile_lock_key

    profile = tmp_path / "profile"
    monkeypatch.setattr(desktop_ownership, "_ACTIVE_DESKTOP_GATEWAY_OWNERSHIP", None)
    monkeypatch.setenv("OPENSQUILLA_DESKTOP", "1")
    monkeypatch.setenv(desktop_ownership.DESKTOP_GATEWAY_INSTANCE_NONCE_ENV, _NONCE)
    monkeypatch.setenv(
        desktop_ownership.DESKTOP_GATEWAY_OWNERSHIP_DIR_ENV,
        str(tmp_path / "control" / profile_lock_key(profile)),
    )
    def fail_acquire(_self):
        raise OSError("synthetic ownership write failure")
    monkeypatch.setattr(desktop_ownership.DesktopGatewayOwnership, "acquire", fail_acquire)
    with pytest.raises(OSError, match="synthetic"):
        desktop_ownership.activate_desktop_gateway_ownership(
            profile_home=profile, port=18791, process_state_dir=tmp_path / "state",
        )
    assert cleanup._active_scope is None
    assert desktop_ownership._ACTIVE_DESKTOP_GATEWAY_OWNERSHIP is None


def test_delayed_registration_retains_launch_fence_after_ownership_release(tmp_path):
    control, state = _configure(tmp_path)
    scope = process_tree._current_task_process_scope()
    assert scope is not None
    cleanup.release_desktop_process_cleanup(_NONCE)
    assert _cleanup(control)
    with pytest.raises(process_tree.ProcessTreeOwnershipError, match="closing"):
        process_tree._insert_owner_record(
            scope, owner_id="a" * 32, platform=process_tree._platform_kind(),
            controller_pid=os.getpid(),
        )
    assert process_tree._load_owner_records(state) == ()
