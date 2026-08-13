"""Durable ownership and bounded termination for task-owned subprocess trees.

Ownership is established at spawn time and is deliberately independent from
the leader process lifecycle.  POSIX uses a verified, isolated process group;
Windows uses a Job Object whose kernel handle remains valid after the leader
exits.  If neither ownership primitive can be established, cleanup is limited
to the direct child so a task can never signal the Gateway's process group.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.wintypes as wintypes
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.01
_CONTROL_READY_TIMEOUT_SECONDS = 2.0
_POSIX_ANCHOR_READY = b"Y"
_POSIX_ANCHOR_ARM = b"A"
_POSIX_ANCHOR_EMPTY = b"E"
_POSIX_ANCHOR_RELEASE = b"R"
_WINDOWS_LAUNCH_GATE_PREFIX = "Local\\OpenSquillaTaskLaunch-"
_WINDOWS_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_WINDOWS_HELPER_STRIP_ENV = "OPENSQUILLA_INTERNAL_PROCESS_TREE_STRIP_ENV"
_WINDOWS_HELPER_RUNTIME_ENV_KEYS = ("SystemRoot", "WINDIR", "ComSpec")


class ProcessTreeOwnershipError(RuntimeError):
    """Raised when a platform cannot safely own a requested process tree."""


def _windows_error(code: int | None = None) -> OSError:
    if code is None:
        code = int(getattr(ctypes, "get_last_error")())
    message = str(getattr(ctypes, "FormatError")(code)).strip()
    return OSError(code, message)


class _WindowsJob:
    """Small ctypes wrapper around one kill-on-close Windows Job Object."""

    def __init__(self, kernel32: Any, handle: Any) -> None:
        self._kernel32 = kernel32
        self._handle = handle
        self._lock = threading.Lock()

    @classmethod
    def create(cls) -> _WindowsJob:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable on this platform")

        handle_type = wintypes.HANDLE
        dword = wintypes.DWORD
        bool_type = wintypes.BOOL
        ulong_ptr = ctypes.POINTER(ctypes.c_ulong)

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", dword),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", dword),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", dword),
                ("SchedulingClass", dword),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = handle_type
        kernel32.SetInformationJobObject.argtypes = [
            handle_type,
            ctypes.c_int,
            ctypes.c_void_p,
            dword,
        ]
        kernel32.SetInformationJobObject.restype = bool_type
        kernel32.OpenProcess.argtypes = [dword, bool_type, dword]
        kernel32.OpenProcess.restype = handle_type
        kernel32.AssignProcessToJobObject.argtypes = [handle_type, handle_type]
        kernel32.AssignProcessToJobObject.restype = bool_type
        kernel32.TerminateJobObject.argtypes = [handle_type, dword]
        kernel32.TerminateJobObject.restype = bool_type
        kernel32.QueryInformationJobObject.argtypes = [
            handle_type,
            ctypes.c_int,
            ctypes.c_void_p,
            dword,
            ulong_ptr,
        ]
        kernel32.QueryInformationJobObject.restype = bool_type
        kernel32.CloseHandle.argtypes = [handle_type]
        kernel32.CloseHandle.restype = bool_type

        job_object_extended_limit_information = 9
        job_object_limit_kill_on_job_close = 0x00002000
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise _windows_error()
        try:
            limits = ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
            if not kernel32.SetInformationJobObject(
                job,
                job_object_extended_limit_information,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise _windows_error()
            return cls(kernel32, job)
        except BaseException:
            kernel32.CloseHandle(job)
            raise

    def assign_pid(self, pid: int) -> None:
        process_rights = 0x0001 | 0x0100 | 0x1000
        process_handle = self._kernel32.OpenProcess(process_rights, False, pid)
        if not process_handle:
            raise _windows_error()
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
                raise _windows_error()
        finally:
            self._kernel32.CloseHandle(process_handle)

    def terminate(self) -> None:
        with self._lock:
            if not self._handle:
                return
            if not self._kernel32.TerminateJobObject(self._handle, 1):
                error = int(getattr(ctypes, "get_last_error")())
                # ERROR_ACCESS_DENIED is also returned when the job has no live
                # processes; the active-count check below is authoritative.
                if error != 5:
                    raise _windows_error(error)

    def active_process_count(self) -> int:
        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        with self._lock:
            if not self._handle:
                return 0
            accounting = BasicAccountingInformation()
            if not self._kernel32.QueryInformationJobObject(
                self._handle,
                1,
                ctypes.byref(accounting),
                ctypes.sizeof(accounting),
                None,
            ):
                raise _windows_error()
            return int(accounting.ActiveProcesses)

    def close_if_empty(self) -> bool:
        with self._lock:
            if not self._handle:
                return True
        if self.active_process_count() != 0:
            return False
        with self._lock:
            if self._handle:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None
        return True

    def close(self) -> None:
        with self._lock:
            if self._handle:
                self._kernel32.CloseHandle(self._handle)
                self._handle = None


class _WindowsLaunchGate:
    """Two-event handshake used by the controlled Windows helper."""

    def __init__(
        self,
        kernel32: Any,
        gate_handle: Any,
        gate_name: str,
        ready_handle: Any,
        ready_name: str,
    ) -> None:
        self._kernel32 = kernel32
        self._gate_handle = gate_handle
        self._ready_handle = ready_handle
        self.gate_name = gate_name
        self.ready_name = ready_name

    @classmethod
    def create(cls) -> _WindowsLaunchGate:
        if os.name != "nt":
            raise OSError("Windows launch gates are unavailable on this platform")
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel32.CreateEventW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        token = uuid.uuid4()
        gate_name = f"{_WINDOWS_LAUNCH_GATE_PREFIX}{token}-gate"
        ready_name = f"{_WINDOWS_LAUNCH_GATE_PREFIX}{token}-ready"
        gate_handle = kernel32.CreateEventW(None, True, False, gate_name)
        if not gate_handle:
            raise _windows_error()
        ready_handle = kernel32.CreateEventW(None, True, False, ready_name)
        if not ready_handle:
            kernel32.CloseHandle(gate_handle)
            raise _windows_error()
        return cls(kernel32, gate_handle, gate_name, ready_handle, ready_name)

    def wait_ready(self, timeout: float) -> None:
        wait_object_0 = 0
        wait_timeout = 258
        result = self._kernel32.WaitForSingleObject(
            self._ready_handle,
            max(0, int(timeout * 1000)),
        )
        if result == wait_object_0:
            return
        if result == wait_timeout:
            raise TimeoutError("Windows controlled launch helper readiness timed out")
        raise _windows_error()

    def release(self) -> None:
        if not self._gate_handle or not self._kernel32.SetEvent(self._gate_handle):
            raise _windows_error()

    def close(self) -> None:
        if self._gate_handle:
            self._kernel32.CloseHandle(self._gate_handle)
            self._gate_handle = None
        if self._ready_handle:
            self._kernel32.CloseHandle(self._ready_handle)
            self._ready_handle = None


@dataclass
class _PosixGroupAnchor:
    process: Any
    pgid: int
    empty: bool = False
    _owner: ProcessTreeOwner | None = field(default=None, repr=False)
    _monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def alive(self) -> bool:
        return getattr(self.process, "returncode", None) is None

    async def wait_ready(self) -> None:
        stdout = getattr(self.process, "stdout", None)
        if stdout is None:
            raise ProcessTreeOwnershipError("POSIX ownership anchor has no status pipe")
        try:
            marker = await asyncio.wait_for(
                stdout.readexactly(1),
                timeout=_CONTROL_READY_TIMEOUT_SECONDS,
            )
        except (TimeoutError, asyncio.IncompleteReadError) as exc:
            raise ProcessTreeOwnershipError(
                "POSIX ownership anchor did not become ready"
            ) from exc
        if marker != _POSIX_ANCHOR_READY:
            raise ProcessTreeOwnershipError("POSIX ownership anchor sent invalid readiness")

    async def arm(self) -> None:
        stdin = getattr(self.process, "stdin", None)
        stdout = getattr(self.process, "stdout", None)
        if stdin is None or stdout is None:
            raise ProcessTreeOwnershipError("POSIX ownership anchor has incomplete control pipes")
        stdin.write(_POSIX_ANCHOR_ARM)
        await stdin.drain()
        self._monitor_task = asyncio.create_task(self._watch_empty(stdout))

    def bind(self, owner: ProcessTreeOwner) -> None:
        self._owner = owner

    async def _watch_empty(self, stdout: Any) -> None:
        try:
            marker = await stdout.read(1)
        except (BrokenPipeError, ConnectionResetError):
            marker = b""
        if marker == _POSIX_ANCHOR_EMPTY:
            self.empty = True
            owner = self._owner
            if owner is not None:
                # The event loop cannot interleave synchronous group signalling
                # between closing the token and releasing the still-live anchor.
                owner._close_empty_posix_owner()
        with contextlib.suppress(Exception):
            await self.process.wait()

    def release(self) -> None:
        stdin = getattr(self.process, "stdin", None)
        if stdin is None or stdin.is_closing():
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.write(_POSIX_ANCHOR_RELEASE)
        stdin.close()

    async def settle(self, timeout: float) -> None:
        task = self._monitor_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        done, _pending = await asyncio.wait({task}, timeout=max(0.0, timeout))
        if task in done:
            with contextlib.suppress(BaseException):
                task.result()


@dataclass
class ProcessTreeOwner:
    """Spawn-time ownership token for exactly one task-owned process tree."""

    process: Any
    pid: int
    pgid: int | None = None
    posix_anchor: _PosixGroupAnchor | None = None
    windows_job: _WindowsJob | None = None
    ownership_error: str | None = None
    _closed: bool = False
    _terminate_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def durable(self) -> bool:
        return (
            (self.pgid is not None and self.posix_anchor is not None)
            or self.windows_job is not None
        )

    def is_active(self) -> bool:
        if self._closed:
            return False
        if self.pgid is not None:
            # The anchor is the non-reusable identity boundary. It is the group
            # leader and remains alive until it reports that no task member
            # remains. The owner closes before releasing that anchor, so it
            # never touches a numeric PGID after reuse becomes possible.
            active = self.posix_anchor is not None and self.posix_anchor.alive
            if not active:
                self._closed = True
            return active
        if self.windows_job is not None:
            try:
                active = self.windows_job.active_process_count() > 0
            except OSError:
                log.warning("process_tree_job_query_failed", exc_info=True)
                return True
            if not active:
                self.windows_job.close_if_empty()
                self._closed = True
            return active
        active = getattr(self.process, "returncode", None) is None
        if not active:
            self._closed = True
        return active

    def _close_empty_posix_owner(self) -> None:
        if self._closed or self.posix_anchor is None:
            return
        self._closed = True
        self.posix_anchor.release()

    def _signal_posix_group(self, sig: signal.Signals) -> None:
        if self.pgid is None or self.posix_anchor is None or self._closed:
            return
        # No probe-then-signal sequence: the live, parent-owned anchor itself
        # prevents numeric PGID reuse for the duration of this synchronous call.
        if not self.is_active():
            return
        try:
            os.killpg(self.pgid, sig)
        except ProcessLookupError:
            self._closed = True

    async def _wait_inactive(self, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while self.is_active():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
        return True

    async def terminate(self, *, graceful_timeout: float, kill_timeout: float) -> bool:
        """Idempotently terminate this owner, bounded by the supplied timeouts."""

        async with self._terminate_lock:
            if not self.is_active():
                if self.posix_anchor is not None:
                    await self.posix_anchor.settle(kill_timeout)
                return True
            if self.pgid is not None:
                self._signal_posix_group(signal.SIGTERM)
                if await self._wait_inactive(graceful_timeout):
                    if self.posix_anchor is not None:
                        await self.posix_anchor.settle(kill_timeout)
                    return True
                self._signal_posix_group(getattr(signal, "SIGKILL", signal.SIGTERM))
                stopped = await self._wait_inactive(kill_timeout)
                if stopped and self.posix_anchor is not None:
                    await self.posix_anchor.settle(kill_timeout)
                return stopped
            if self.windows_job is not None:
                try:
                    await asyncio.to_thread(self.windows_job.terminate)
                except OSError:
                    log.warning(
                        "process_tree_job_terminate_failed",
                        extra={"pid": self.pid},
                        exc_info=True,
                    )
                return await self._wait_inactive(kill_timeout)

            # No durable tree primitive was established. Direct-child cleanup
            # is safe; taskkill/process-name scans are not, because PID reuse or
            # shared services could widen the blast radius.
            if getattr(self.process, "returncode", None) is not None:
                self._closed = True
                return True
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            if await _wait_direct_process(self.process, graceful_timeout):
                self._closed = True
                return True
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            stopped = await _wait_direct_process(self.process, kill_timeout)
            self._closed = stopped
            return stopped


def capture_process_tree_owner(process: Any, *, isolated: bool) -> ProcessTreeOwner:
    """Capture an ownership token immediately after a task-owned spawn."""

    attached = getattr(process, "_opensquilla_process_tree_owner", None)
    if isinstance(attached, ProcessTreeOwner):
        return attached
    pid = int(process.pid)
    if not isolated:
        return ProcessTreeOwner(
            process=process,
            pid=pid,
            ownership_error="process was not spawned in an isolated tree",
        )
    if os.name == "posix":
        # A bare numeric PGID is reusable after the leader exits. Only the
        # unified launcher can provide the required live anchor identity.
        return ProcessTreeOwner(
            process=process,
            pid=pid,
            ownership_error="POSIX process was not spawned with a durable group anchor",
        )
    if os.name == "nt":
        # Assigning an already-running process is both racy and invalid when
        # the host itself belongs to a restrictive Job Object. Only the
        # controlled breakaway launcher can provide durable Windows ownership.
        return ProcessTreeOwner(
            process=process,
            pid=pid,
            ownership_error="Windows process was not spawned with a controlled Job Object",
        )
    return ProcessTreeOwner(
        process=process,
        pid=pid,
        ownership_error=f"unsupported process-tree platform: {os.name}",
    )


async def _stop_failed_async_process(process: Any) -> None:
    if getattr(process, "returncode", None) is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.terminate()
        if not await _wait_direct_process(process, 0.5):
            with contextlib.suppress(ProcessLookupError, OSError):
                process.kill()
            await _wait_direct_process(process, 1.0)


async def _stop_unarmed_posix_anchor(anchor: _PosixGroupAnchor) -> None:
    stdin = getattr(anchor.process, "stdin", None)
    if stdin is not None and not stdin.is_closing():
        stdin.close()
    if not await _wait_direct_process(anchor.process, 0.5):
        with contextlib.suppress(ProcessLookupError):
            anchor.process.kill()
        await _wait_direct_process(anchor.process, 1.0)


async def _create_posix_anchor() -> _PosixGroupAnchor:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "opensquilla.process_tree",
        "--posix-group-anchor",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        process_group=0,
    )
    anchor = _PosixGroupAnchor(process=process, pgid=int(process.pid))
    try:
        await anchor.wait_ready()
    except BaseException:
        await _stop_unarmed_posix_anchor(anchor)
        raise
    return anchor


def _attach_owner(process: Any, owner: ProcessTreeOwner) -> Any:
    setattr(process, "_opensquilla_process_tree_owner", owner)
    return process


def _windows_helper_env(target_env: Mapping[str, str] | None) -> dict[str, str]:
    """Build a bootable helper environment without widening the target env."""

    if target_env is None:
        helper_env = dict(os.environ)
    else:
        helper_env = {str(key): str(value) for key, value in dict(target_env).items()}

    present = {key.casefold() for key in helper_env}
    injected: list[str] = []
    for key in _WINDOWS_HELPER_RUNTIME_ENV_KEYS:
        if key.casefold() in present:
            continue
        value = os.environ.get(key)
        if value is None:
            continue
        helper_env[key] = value
        present.add(key.casefold())
        injected.append(key)
    _pop_windows_env(helper_env, _WINDOWS_HELPER_STRIP_ENV)
    helper_env[_WINDOWS_HELPER_STRIP_ENV] = ";".join(injected)
    return helper_env


def _pop_windows_env(env: dict[str, str], key: str) -> str | None:
    folded = key.casefold()
    for candidate in tuple(env):
        if candidate.casefold() == folded:
            return env.pop(candidate)
    return None


def _windows_target_env_from_helper(helper_env: Mapping[str, str]) -> dict[str, str]:
    """Recover the caller-requested env after the helper has booted."""

    target_env = {str(key): str(value) for key, value in dict(helper_env).items()}
    injected = _pop_windows_env(target_env, _WINDOWS_HELPER_STRIP_ENV) or ""
    for key in injected.split(";"):
        if key:
            _pop_windows_env(target_env, key)
    return target_env


async def create_owned_subprocess_exec(*argv: str, **kwargs: Any) -> Any:
    """Spawn an argv under durable task-owned tree containment."""

    if os.name == "posix":
        anchor = await _create_posix_anchor()
        child_kwargs = dict(kwargs)
        child_kwargs.pop("start_new_session", None)
        child_kwargs.pop("process_group", None)
        child_kwargs["process_group"] = anchor.pgid
        try:
            process = await asyncio.create_subprocess_exec(*argv, **child_kwargs)
        except BaseException:
            await _stop_unarmed_posix_anchor(anchor)
            raise
        owner = ProcessTreeOwner(
            process=process,
            pid=int(process.pid),
            pgid=anchor.pgid,
            posix_anchor=anchor,
        )
        anchor.bind(owner)
        _attach_owner(process, owner)
        try:
            await anchor.arm()
        except BaseException as exc:
            await owner.terminate(graceful_timeout=0.2, kill_timeout=1.0)
            raise ProcessTreeOwnershipError(
                f"failed to arm POSIX process-tree owner for pid {process.pid}: {exc}"
            ) from exc
        return process

    if os.name == "nt":
        gate = _WindowsLaunchGate.create()
        job = _WindowsJob.create()
        child_kwargs = dict(kwargs)
        child_kwargs.pop("start_new_session", None)
        child_kwargs["creationflags"] = (
            int(child_kwargs.get("creationflags", 0))
            | _WINDOWS_CREATE_BREAKAWAY_FROM_JOB
        )
        child_kwargs["env"] = _windows_helper_env(child_kwargs.get("env"))
        helper_argv = (
            sys.executable,
            "-m",
            "opensquilla.process_tree",
            "--windows-owned-launch",
            gate.gate_name,
            gate.ready_name,
            "--",
            *argv,
        )
        windows_process: Any | None = None
        try:
            windows_process = await asyncio.create_subprocess_exec(
                *helper_argv,
                **child_kwargs,
            )
            job.assign_pid(int(windows_process.pid))
            await asyncio.to_thread(
                gate.wait_ready,
                _CONTROL_READY_TIMEOUT_SECONDS,
            )
            owner = ProcessTreeOwner(
                process=windows_process,
                pid=int(windows_process.pid),
                windows_job=job,
            )
            _attach_owner(windows_process, owner)
            gate.release()
            return windows_process
        except BaseException as exc:
            if windows_process is not None:
                await _stop_failed_async_process(windows_process)
            job.close()
            raise ProcessTreeOwnershipError(
                f"Windows controlled process launch failed closed: {exc}"
            ) from exc
        finally:
            gate.close()

    raise ProcessTreeOwnershipError(f"unsupported process-tree platform: {os.name}")


async def create_owned_subprocess_shell(command: str, **kwargs: Any) -> Any:
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return await create_owned_subprocess_exec(comspec, "/d", "/s", "/c", command, **kwargs)
    if os.name != "posix":
        raise ProcessTreeOwnershipError(f"unsupported process-tree platform: {os.name}")
    anchor = await _create_posix_anchor()
    child_kwargs = dict(kwargs)
    child_kwargs.pop("start_new_session", None)
    child_kwargs.pop("process_group", None)
    child_kwargs["process_group"] = anchor.pgid
    try:
        process = await asyncio.create_subprocess_shell(command, **child_kwargs)
    except BaseException:
        await _stop_unarmed_posix_anchor(anchor)
        raise
    owner = ProcessTreeOwner(
        process=process,
        pid=int(process.pid),
        pgid=anchor.pgid,
        posix_anchor=anchor,
    )
    anchor.bind(owner)
    _attach_owner(process, owner)
    try:
        await anchor.arm()
    except BaseException as exc:
        await owner.terminate(graceful_timeout=0.2, kill_timeout=1.0)
        raise ProcessTreeOwnershipError(
            f"failed to arm POSIX process-tree owner for pid {process.pid}: {exc}"
        ) from exc
    return process


def create_owned_popen(argv: list[str] | tuple[str, ...], **kwargs: Any) -> Any:
    """Synchronous Windows controlled-helper launcher for blocking pipe I/O."""

    if os.name != "nt":
        raise ProcessTreeOwnershipError("synchronous owned launcher is Windows-only")
    gate = _WindowsLaunchGate.create()
    job = _WindowsJob.create()
    child_kwargs = dict(kwargs)
    child_kwargs.pop("start_new_session", None)
    child_kwargs["creationflags"] = (
        int(child_kwargs.get("creationflags", 0))
        | _WINDOWS_CREATE_BREAKAWAY_FROM_JOB
    )
    child_kwargs["env"] = _windows_helper_env(child_kwargs.get("env"))
    helper_argv = [
        sys.executable,
        "-m",
        "opensquilla.process_tree",
        "--windows-owned-launch",
        gate.gate_name,
        gate.ready_name,
        "--",
        *argv,
    ]
    process: Any | None = None
    try:
        process = subprocess.Popen(helper_argv, **child_kwargs)
        job.assign_pid(int(process.pid))
        gate.wait_ready(_CONTROL_READY_TIMEOUT_SECONDS)
        owner = ProcessTreeOwner(
            process=process,
            pid=int(process.pid),
            windows_job=job,
        )
        _attach_owner(process, owner)
        gate.release()
        return process
    except BaseException as exc:
        if process is not None and process.poll() is None:
            with contextlib.suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=1.0)
        job.close()
        raise ProcessTreeOwnershipError(
            f"Windows controlled process launch failed closed: {exc}"
        ) from exc
    finally:
        gate.close()


def _posix_group_members(pgid: int) -> tuple[int, ...] | None:
    proc_root = "/proc"
    if os.path.isdir(proc_root):
        members: list[int] = []
        try:
            names = os.listdir(proc_root)
        except OSError:
            return None
        for name in names:
            if not name.isdigit():
                continue
            try:
                with open(
                    os.path.join(proc_root, name, "stat"),
                    encoding="utf-8",
                ) as stat_file:
                    stat = stat_file.read()
                rest = stat[stat.rfind(")") + 2 :].split()
                if len(rest) > 2 and int(rest[2]) == pgid:
                    members.append(int(name))
            except (OSError, ValueError):
                # Numeric /proc entries routinely disappear between listdir
                # and open as unrelated processes exit. The anchor's own stat
                # is mandatory; other vanished or malformed entries cannot be
                # members of the final live snapshot.
                if int(name) == pgid:
                    return None
                continue
        if not members or pgid not in members:
            return None
        return tuple(members)
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid="],
            check=False,
            capture_output=True,
            text=True,
            start_new_session=True,
            timeout=1.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            return None
        try:
            pid, candidate_pgid = (int(field) for field in fields)
        except ValueError:
            return None
        if candidate_pgid == pgid:
            members.append(pid)
    if not members or pgid not in members:
        return None
    return tuple(members)


def _run_posix_group_anchor() -> int:
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    own_pid = os.getpid()
    pgid = os.getpgrp()
    if pgid != own_pid:
        return 125
    sys.stdout.buffer.write(_POSIX_ANCHOR_READY)
    sys.stdout.buffer.flush()
    if sys.stdin.buffer.read(1) != _POSIX_ANCHOR_ARM:
        return 125
    poll_delay = _POLL_INTERVAL_SECONDS
    poll_cap = 0.25 if os.path.isdir("/proc") else 1.0
    while True:
        members = _posix_group_members(pgid)
        if members is not None and len(members) == 1 and members[0] == own_pid:
            sys.stdout.buffer.write(_POSIX_ANCHOR_EMPTY)
            sys.stdout.buffer.flush()
            return 0 if sys.stdin.buffer.read(1) == _POSIX_ANCHOR_RELEASE else 125
        time.sleep(poll_delay)
        poll_delay = min(poll_cap, poll_delay * 1.5)


def _run_windows_owned_launch(
    gate_name: str,
    ready_name: str,
    argv: list[str],
) -> int:
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    synchronize = 0x00100000
    event_modify_state = 0x0002
    infinite = 0xFFFFFFFF
    wait_failed = 0xFFFFFFFF
    gate = kernel32.OpenEventW(synchronize, False, gate_name)
    if not gate:
        raise _windows_error()
    ready = kernel32.OpenEventW(event_modify_state, False, ready_name)
    if not ready:
        kernel32.CloseHandle(gate)
        raise _windows_error()
    try:
        if not kernel32.SetEvent(ready):
            raise _windows_error()
        if kernel32.WaitForSingleObject(gate, infinite) == wait_failed:
            raise _windows_error()
    finally:
        kernel32.CloseHandle(ready)
        kernel32.CloseHandle(gate)
    if not argv:
        return 127
    try:
        process = subprocess.Popen(
            argv,
            env=_windows_target_env_from_helper(os.environ),
        )
    except OSError as exc:
        print(f"OpenSquilla controlled launch failed: {exc}", file=sys.stderr)
        return 127
    return int(process.wait())


def _main() -> int:
    args = sys.argv[1:]
    if args == ["--posix-group-anchor"]:
        return _run_posix_group_anchor()
    if len(args) >= 4 and args[0] == "--windows-owned-launch" and args[3] == "--":
        return _run_windows_owned_launch(args[1], args[2], args[4:])
    return 2


async def _wait_direct_process(process: Any, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
    while getattr(process, "returncode", None) is None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
    return True


__all__ = [
    "ProcessTreeOwner",
    "ProcessTreeOwnershipError",
    "capture_process_tree_owner",
    "create_owned_popen",
    "create_owned_subprocess_exec",
    "create_owned_subprocess_shell",
]


if __name__ == "__main__":
    raise SystemExit(_main())
