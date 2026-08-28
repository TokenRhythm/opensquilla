"""PID file + exclusive lock for the gateway process.

Prevents two gateway instances from sharing the same STATE_DIR.

Design:
- The pid file (``gateway.pid``) is always readable: written atomically then fsynced.
- The lock file (``gateway.pid.lock``) carries the OS exclusive byte-range lock so the
  pid file itself stays open for readers even while the lock is held.

Platform locking:
- Windows: msvcrt.locking(lock_fd, LK_NBLCK, 1) on gateway.pid.lock
- POSIX:   fcntl.flock(lock_fd, LOCK_EX | LOCK_NB) on gateway.pid.lock

Usage::

    lock = GatewayPidLock(state_dir)
    lock.acquire()          # raises SystemExit(1) if another live instance holds it
    # lock released automatically via atexit + signal handlers registered in acquire()
"""

from __future__ import annotations

import atexit
import datetime
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import IO, Any, cast

from opensquilla.recovery.atomic import _native_io_path
from opensquilla.recovery.locking import (
    GatewayLegacyLease,
    acquire_gateway_legacy_lease,
    release_gateway_legacy_lease,
)

log = logging.getLogger(__name__)

_PID_FILENAME = "gateway.pid"
_LOCK_FILENAME = "gateway.pid.lock"


class GatewayPidLock:
    """Exclusive PID-file lock for one gateway instance per STATE_DIR."""

    def __init__(self, state_dir: str | Path) -> None:
        self._state_dir = Path(state_dir)
        self._pid_path = self._state_dir / _PID_FILENAME
        self._lock_path = self._state_dir / _LOCK_FILENAME
        self._lock_lease: GatewayLegacyLease | None = None
        # Cached payload written by this instance (readable without reopening the file).
        self._written: dict | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> None:
        """Acquire the PID file lock.

        Algorithm:
        1. Acquire exclusive OS lock on gateway.pid.lock (separate file so
           gateway.pid stays freely readable while the lock is held).
           - Lock fails → SystemExit(1), reporting the readable pid payload.
        2. With the OS lock held, treat any existing gateway.pid as stale,
           remove it, and continue. The lock is the authority; PID-only
           liveness is vulnerable to PID reuse and unrelated live processes.
        3. Write pid + start_ts (ISO 8601) to gateway.pid, fsync.
        4. Register atexit + SIGTERM/SIGINT cleanup.
        """
        os.makedirs(_native_io_path(self._state_dir), exist_ok=True)

        # ── Step 1: exclusive OS lock on the lock file ────────────────
        lock_lease = acquire_gateway_legacy_lease(self._state_dir)
        if lock_lease is None:
            existing_pid = _read_pid_from_path(self._pid_path)
            pid_str = str(existing_pid) if existing_pid is not None else "unknown"
            log.error(
                "gateway.pidlock.already_running",
                extra={"pid": pid_str, "state_dir": str(self._state_dir)},
            )
            print(
                f"ERROR: Another gateway is already running "
                f"(pid={pid_str}, state_dir={self._state_dir}). "
                f"Stop it first or remove {self._pid_path}.",
                file=sys.stderr,
            )
            sys.exit(1)

        # ── Step 2: clear stale pid file now that this process owns the lock ─
        self._lock_lease = lock_lease
        existing_pid = (
            _read_pid_from_path(self._pid_path)
            if os.path.exists(_native_io_path(self._pid_path))
            else None
        )
        if existing_pid is not None:
            log.warning(
                "gateway.pidlock.stale_overwritten",
                extra={"stale_pid": existing_pid, "state_dir": str(self._state_dir)},
            )
        try:
            os.unlink(_native_io_path(self._pid_path))
        except OSError:
            pass

        # ── Step 3: write pid + start_ts to the pid file ─────────────
        self._write_pid()

        # ── Step 4: register cleanup ──────────────────────────────────
        self._register_cleanup()

    def release(self) -> None:
        """Release the lock and remove the PID file. Safe to call multiple times.

        Keep ``gateway.pid.lock`` on disk. The file path is the stable inode that
        all contenders lock; unlinking it after unlock can let a successor lock a
        removed inode while a third process creates and locks a new path.
        """
        if self._lock_lease is None:
            return
        lease = self._lock_lease
        self._lock_lease = None
        release_gateway_legacy_lease(lease)
        try:
            os.unlink(_native_io_path(self._pid_path))
        except OSError:
            pass

    @property
    def pid(self) -> int | None:
        """The PID written to the pid file by this instance, or None before acquire()."""
        return self._written.get("pid") if self._written else None

    @property
    def start_ts(self) -> str | None:
        """The start_ts written to the pid file by this instance, or None before acquire()."""
        return self._written.get("start_ts") if self._written else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_pid(self) -> None:
        self._written = {
            "pid": os.getpid(),
            "start_ts": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        payload = json.dumps(self._written).encode()
        # Write to the pid file (not the lock file) so readers can open it freely.
        with open(_native_io_path(self._pid_path), "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

    def _register_cleanup(self) -> None:
        atexit.register(self.release)

        def _handler(signum: int, frame: object) -> None:
            self.release()
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except (OSError, ValueError):
                pass


# ---------------------------------------------------------------------------
# Module-level helpers (no self state needed)
# ---------------------------------------------------------------------------


def _try_lock(fh: IO[bytes]) -> bool:
    if os.name == "nt":
        import msvcrt

        msvcrt_mod = cast(Any, msvcrt)
        try:
            fh.seek(0)
            msvcrt_mod.locking(fh.fileno(), msvcrt_mod.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl

        fcntl_mod = cast(Any, fcntl)
        try:
            fcntl_mod.flock(fh.fileno(), fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
            return True
        except OSError:
            return False


def _unlock(fh: IO[bytes]) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt_mod = cast(Any, msvcrt)
        try:
            fh.seek(0)
            msvcrt_mod.locking(fh.fileno(), msvcrt_mod.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        fcntl_mod = cast(Any, fcntl)
        try:
            fcntl_mod.flock(fh.fileno(), fcntl_mod.LOCK_UN)
        except OSError:
            pass


def _read_pid_from_path(path: Path) -> int | None:
    try:
        with open(_native_io_path(path), "rb") as handle:
            info = json.load(handle)
        return int(info["pid"])
    except Exception:  # noqa: BLE001
        return None


def _is_alive(pid: int) -> bool:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            ctypes_mod = cast(Any, ctypes)
            kernel32 = ctypes_mod.windll.kernel32
            process_query_limited_information = 0x1000
            still_active = 259
            handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes_mod.byref(exit_code)):
                    return True
                return int(exit_code.value) == still_active
            finally:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but not owned by us
