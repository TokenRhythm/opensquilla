"""Resource supervision for untrusted macOS code.

Darwin does not offer a usable RLIMIT_AS for these processes. CPU gets an
inherited hard limit; aggregate resident memory and process count are sampled
by the host. The watchdog can overshoot between samples: it is not a kernel
memory quota. Failure to inspect the owned tree aborts execution.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import sys
from collections.abc import Callable
from typing import Any

from opensquilla.sandbox.types import ResourceLimits, SandboxBackendError

_POLL_SECONDS = 0.05
_PROC_PIDTASKINFO = 4


class _TaskInfo(ctypes.Structure):
    # Darwin's proc_taskinfo from sys/proc_info.h, stable 64-bit ABI.
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "virtual_size", "resident_size", "total_user", "total_system",
            "threads_user", "threads_system",
        )
    ] + [("counters", ctypes.c_int32 * 12)]


class DarwinResourceGuard:
    def __init__(self, limits: ResourceLimits) -> None:
        from opensquilla.process_tree import _darwin_libproc

        if sys.platform != "darwin" or min(
            limits.cpu_seconds, limits.memory_mb, limits.pids,
        ) <= 0:
            raise SandboxBackendError("Seatbelt resource limits are unavailable or invalid")
        self.limits: ResourceLimits = limits
        self.library: Any = _darwin_libproc()
        if self.library is None or self._read_task(os.getpid()) is None:
            raise SandboxBackendError("Seatbelt resource monitor is unavailable")
        self.known: dict[int, str] = {}

    def cpu_preexec(self) -> Callable[[], None]:
        # Import before fork; the child only calls the already-loaded C API.
        import resource

        requested = self.limits.cpu_seconds
        _, inherited_hard = resource.getrlimit(resource.RLIMIT_CPU)
        hard = (
            requested if inherited_hard == resource.RLIM_INFINITY
            else min(requested, inherited_hard)
        )

        def apply() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (hard, hard))

        return apply

    def _read_task(self, pid: int) -> _TaskInfo | None:
        if self.library is None:
            return None
        info = _TaskInfo()
        size = ctypes.sizeof(info)
        if self.library.proc_pidinfo(
            pid, _PROC_PIDTASKINFO, 0, ctypes.byref(info), size,
        ) != size:
            return None
        return info

    def _check(self, owner: Any) -> None:
        from opensquilla.process_tree import _darwin_process_info, _darwin_process_snapshot

        if not owner.is_active():
            return
        snapshot = _darwin_process_snapshot()
        if snapshot is None:
            raise SandboxBackendError("Seatbelt resource monitor lost process visibility")
        # The launch target may exit while its children still run. Only the
        # durable group anchor owns the tree lifetime; the target PID can even
        # be reused by an unrelated process before the tree becomes empty.
        anchor = snapshot.get(owner.pgid)
        if anchor is None or anchor.pgid != owner.pgid:
            if not owner.is_active():
                return
            raise SandboxBackendError("Seatbelt resource monitor lost its process owner")
        # Retain identities of descendants that change their process group.
        selected = {
            pid for pid, info in snapshot.items()
            if info.pgid == owner.pgid or self.known.get(pid) == info.start_identity
        }
        while True:
            children = {
                pid for pid, info in snapshot.items() if info.ppid in selected
            } - selected
            if not children:
                break
            selected.update(children)
        selected.discard(owner.pgid)  # backend-owned containment anchor
        if len(selected) > self.limits.pids:
            raise SandboxBackendError("Seatbelt process count limit exceeded")
        resident = 0
        for pid in selected:
            identity = snapshot[pid].start_identity
            self.known[pid] = identity
            info = self._read_task(pid)
            current = _darwin_process_info(pid, self.library)
            if current is None or current.start_identity != identity:
                continue  # exited or PID reused during observation
            if info is None:
                raise SandboxBackendError("Seatbelt resource monitor cannot inspect a task")
            resident += int(info.resident_size)
        self.known = {pid: self.known[pid] for pid in selected}
        if resident > self.limits.memory_mb * 1024 * 1024:
            raise SandboxBackendError("Seatbelt resident memory limit exceeded")

    async def watch(self, process: Any) -> bool:
        owner = getattr(process, "_opensquilla_process_tree_owner", None)
        if owner is None or owner.pgid is None or owner.posix_anchor is None:
            raise SandboxBackendError("Seatbelt resource monitor requires an owned process tree")
        while True:
            await asyncio.to_thread(self._check, owner)
            if not owner.is_active():
                return False
            await asyncio.sleep(_POLL_SECONDS)
