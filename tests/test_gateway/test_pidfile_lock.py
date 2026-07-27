"""Tests for GatewayPidLock PID file placement (AC-C1)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import types
from pathlib import Path

import pytest

from opensquilla.gateway import pidlock
from opensquilla.gateway.pidlock import GatewayPidLock
from opensquilla.recovery.atomic import _native_io_path


def test_pid_file_in_state_dir_not_parent(tmp_path: Path) -> None:
    """AC-C1-1/AC-C1-2: PID file must land in state_dir, not state_dir.parent."""
    state_dir = tmp_path / "state"
    lock = GatewayPidLock(state_dir)
    lock.acquire()
    try:
        # PID file must be inside state_dir
        assert (state_dir / "gateway.pid").exists(), (
            f"gateway.pid not found in {state_dir}"
        )
        # PID file must NOT be in the parent directory
        assert not (tmp_path / "gateway.pid").exists(), (
            f"gateway.pid incorrectly written to parent {tmp_path}"
        )
    finally:
        lock.release()


def test_pid_lock_rejects_second_acquisition_while_first_is_held(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    first = GatewayPidLock(state_dir)
    first.acquire()
    try:
        second = GatewayPidLock(state_dir)
        with pytest.raises(SystemExit) as exc_info:
            second.acquire()

        assert exc_info.value.code == 1
    finally:
        first.release()


def test_pid_lock_overwrites_live_stale_pid_when_lock_is_free(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    pid_path = state_dir / "gateway.pid"
    pid_path.write_text(
        json.dumps({"pid": os.getpid(), "start_ts": "2000-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )

    lock = GatewayPidLock(state_dir)
    lock.acquire()
    try:
        payload = json.loads(pid_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
        assert payload["start_ts"] != "2000-01-01T00:00:00+00:00"
    finally:
        lock.release()


def test_pid_lock_release_is_idempotent(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    lock = GatewayPidLock(state_dir)
    lock.acquire()

    lock.release()
    lock.release()

    assert not (state_dir / "gateway.pid").exists()
    assert (state_dir / "gateway.pid.lock").exists()


def test_pid_lock_release_keeps_lock_file_identity_for_successors(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    first = GatewayPidLock(state_dir)
    first.acquire()
    first.release()

    second = GatewayPidLock(state_dir)
    second.acquire()
    try:
        assert (state_dir / "gateway.pid.lock").exists()
        third = GatewayPidLock(state_dir)
        with pytest.raises(SystemExit) as exc_info:
            third.acquire()

        assert exc_info.value.code == 1
    finally:
        second.release()


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path contract")
def test_pid_lock_supports_extended_length_state_directory(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    long_root = tmp_path / "long-state"
    state_dir = long_root
    native_root = _native_io_path(long_root)

    def cleanup() -> None:
        if os.path.exists(native_root):
            shutil.rmtree(native_root)

    request.addfinalizer(cleanup)
    index = 0
    while len(str(state_dir)) < 275:
        state_dir /= f"state-segment-{index:02d}-0123456789"
        index += 1
    pid_path = state_dir / "gateway.pid"
    lock_path = state_dir / "gateway.pid.lock"
    first = GatewayPidLock(state_dir)

    first.acquire()
    try:
        assert os.path.isfile(_native_io_path(pid_path))
        assert os.path.isfile(_native_io_path(lock_path))
        with open(_native_io_path(pid_path), encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["pid"] == os.getpid()

        second = GatewayPidLock(state_dir)
        with pytest.raises(SystemExit) as exc_info:
            second.acquire()
        assert exc_info.value.code == 1
    finally:
        first.release()

    assert not os.path.exists(_native_io_path(pid_path))
    assert os.path.isfile(_native_io_path(lock_path))


def test_windows_is_alive_rejects_opened_process_with_non_active_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dword:
        def __init__(self) -> None:
            self.value = 0

    class Kernel32:
        def __init__(self) -> None:
            self.closed_handles: list[int] = []

        def open_process(self, access: int, inherit_handle: bool, process_id: int) -> int:
            return 123

        def get_exit_code_process(self, handle: int, exit_code: Dword) -> int:
            exit_code.value = 0
            return 1

        def close_handle(self, handle: int) -> int:
            self.closed_handles.append(handle)
            return 1

    def byref(value: Dword) -> Dword:
        return value

    kernel32 = Kernel32()
    fake_ctypes = types.ModuleType("ctypes")
    fake_ctypes.windll = types.SimpleNamespace(
        kernel32=types.SimpleNamespace(
            OpenProcess=kernel32.open_process,
            GetExitCodeProcess=kernel32.get_exit_code_process,
            CloseHandle=kernel32.close_handle,
        )
    )
    fake_ctypes.wintypes = types.SimpleNamespace(DWORD=Dword)
    fake_ctypes.byref = byref

    monkeypatch.setattr(pidlock.os, "name", "nt")
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    assert pidlock._is_alive(456) is False
    assert kernel32.closed_handles == [123]
