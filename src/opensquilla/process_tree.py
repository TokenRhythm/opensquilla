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
import logging
import os
import signal
import threading
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.01


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
    def assign(cls, pid: int) -> _WindowsJob:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable on this platform")

        from ctypes import wintypes

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
        process_rights = 0x0001 | 0x0100 | 0x1000

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise _windows_error()
        process_handle = None
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
            process_handle = kernel32.OpenProcess(process_rights, False, pid)
            if not process_handle:
                raise _windows_error()
            if not kernel32.AssignProcessToJobObject(job, process_handle):
                raise _windows_error()
            return cls(kernel32, job)
        except BaseException:
            kernel32.CloseHandle(job)
            raise
        finally:
            if process_handle:
                kernel32.CloseHandle(process_handle)

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
        from ctypes import wintypes

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


@dataclass
class ProcessTreeOwner:
    """Spawn-time ownership token for exactly one task-owned process tree."""

    process: Any
    pid: int
    pgid: int | None = None
    windows_job: _WindowsJob | None = None
    ownership_error: str | None = None
    _closed: bool = False
    _terminate_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def durable(self) -> bool:
        return self.pgid is not None or self.windows_job is not None

    def is_active(self) -> bool:
        if self._closed:
            return False
        if self.pgid is not None:
            try:
                os.killpg(self.pgid, 0)
            except ProcessLookupError:
                self._closed = True
                return False
            except PermissionError:
                return True
            except OSError:
                return True
            return True
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

    def _signal_posix_group(self, sig: signal.Signals) -> None:
        if self.pgid is None or self._closed:
            return
        # The group id was verified at spawn and is never recomputed from the
        # leader. Once absence is observed, close permanently before a future
        # process can reuse the numeric PGID.
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
                return True
            if self.pgid is not None:
                self._signal_posix_group(signal.SIGTERM)
                if await self._wait_inactive(graceful_timeout):
                    return True
                self._signal_posix_group(getattr(signal, "SIGKILL", signal.SIGTERM))
                return await self._wait_inactive(kill_timeout)
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

    pid = int(process.pid)
    if not isolated:
        return ProcessTreeOwner(
            process=process,
            pid=pid,
            ownership_error="process was not spawned in an isolated tree",
        )
    if os.name == "posix":
        try:
            pgid = os.getpgid(pid)
        except OSError as exc:
            return ProcessTreeOwner(process=process, pid=pid, ownership_error=str(exc))
        if pgid != pid:
            return ProcessTreeOwner(
                process=process,
                pid=pid,
                ownership_error=f"unverified process group pgid={pgid} pid={pid}",
            )
        return ProcessTreeOwner(process=process, pid=pid, pgid=pgid)
    if os.name == "nt":
        try:
            return ProcessTreeOwner(process=process, pid=pid, windows_job=_WindowsJob.assign(pid))
        except OSError as exc:
            log.warning(
                "process_tree_job_assignment_failed",
                extra={"pid": pid, "error": str(exc)},
            )
            # Job ownership is the Windows tree-safety boundary. Do not run a
            # task under the fiction that taskkill/direct-parent cleanup will
            # still cover descendants when assignment is unavailable.
            with contextlib.suppress(ProcessLookupError, OSError):
                process.terminate()
            raise ProcessTreeOwnershipError(
                f"Windows Job Object assignment failed for task-owned pid {pid}: {exc}"
            ) from exc
    return ProcessTreeOwner(
        process=process,
        pid=pid,
        ownership_error=f"unsupported process-tree platform: {os.name}",
    )


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
]
