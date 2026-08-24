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
import contextvars
import ctypes
import ctypes.wintypes as wintypes
import errno
import hashlib
import logging
import os
import re
import select
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from opensquilla.private_paths import apply_windows_private_dacl, create_windows_private_directory

log = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 0.01
_CONTROL_READY_TIMEOUT_SECONDS = 2.0
_WINDOWS_FROZEN_READY_TIMEOUT_SECONDS = 5.0
_WINDOWS_FROZEN_READY_RETRY_DELAY_SECONDS = 0.25
_WINDOWS_FROZEN_READY_ATTEMPTS = 2
_POSIX_ANCHOR_READY = b"Y"
_POSIX_ANCHOR_ARM = b"A"
_POSIX_ANCHOR_EMPTY = b"E"
_POSIX_ANCHOR_CAPTURED = b"C"
_POSIX_ANCHOR_INCOMPLETE = b"I"
_POSIX_ANCHOR_KILL_CAPTURED = b"D"
_POSIX_ANCHOR_KILL_INCOMPLETE = b"J"
_POSIX_ANCHOR_RELEASE = b"R"
_POSIX_ANCHOR_TERMINATE = b"T"
_POSIX_ANCHOR_KILL = b"K"
_POSIX_ANCHOR_LEGACY_TERMINATED = b"K"
_POSIX_TARGET_RELEASE = b"X"
_WINDOWS_LAUNCH_GATE_PREFIX = "Local\\OpenSquillaTaskLaunch-"
_WINDOWS_JOB_PREFIX = "Local\\OpenSquillaTaskJob-"
_WINDOWS_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_WINDOWS_HELPER_STRIP_ENV = "OPENSQUILLA_INTERNAL_PROCESS_TREE_STRIP_ENV"
_WINDOWS_HELPER_RUNTIME_ENV_KEYS = ("SystemRoot", "WINDIR", "ComSpec")
_OWNER_SCHEMA_VERSION = 1
_OWNER_DATABASE_FILENAME = "task-process-owners.sqlite3"
_OWNER_CONTROL_DIRECTORY = "task-process-control"
_OWNER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_OWNER_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_OWNER_IDENTITY_MAX_CHARS = 256
_OWNER_DATABASE_TIMEOUT_SECONDS = 5.0
_OWNER_DATABASE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_WINDOWS_REGISTRY_RETRY_DELAYS_SECONDS = (0.03, 0.08, 0.18)
_WINDOWS_TRANSIENT_FILE_ERRORS = frozenset({5, 32, 33})
_POSIX_DESCENDANT_CAPTURE_LIMIT = 1024
_DARWIN_PROC_PIDTBSDINFO = 3


def _process_tree_child_argv(*args: str) -> tuple[str, ...]:
    """Build the source or frozen argv for one process-tree helper."""

    prefix = (
        (sys.executable, "--internal-child", "process-tree")
        if getattr(sys, "frozen", False)
        else (sys.executable, "-m", "opensquilla.process_tree")
    )
    return (*prefix, *args)


def _wait_for_windows_helper_ready(gate: Any) -> None:
    """Wait longer, once more, for a cold frozen helper to become ready."""

    frozen = bool(getattr(sys, "frozen", False))
    timeout = (
        _WINDOWS_FROZEN_READY_TIMEOUT_SECONDS
        if frozen
        else _CONTROL_READY_TIMEOUT_SECONDS
    )
    attempts = _WINDOWS_FROZEN_READY_ATTEMPTS if frozen else 1
    for attempt in range(attempts):
        try:
            gate.wait_ready(timeout)
            return
        except TimeoutError:
            if attempt + 1 >= attempts:
                raise
            log.warning(
                "windows_process_tree_helper_ready_retry",
                extra={
                    "attempt": attempt + 1,
                    "timeout_seconds": timeout,
                },
            )
            time.sleep(_WINDOWS_FROZEN_READY_RETRY_DELAY_SECONDS)


@dataclass(frozen=True)
class _TaskProcessScope:
    state_dir: Path
    session_digest: str
    task_digest: str
    parent_session_digest: str | None = None
    parent_task_digest: str | None = None


@dataclass(frozen=True)
class _PosixProcessInfo:
    pid: int
    ppid: int
    pgid: int
    uid: int
    start_identity: str


@dataclass(frozen=True)
class _CapturedPosixProcess:
    pid: int
    uid: int
    start_identity: str
    depth: int
    pidfd: int | None = None


@dataclass(frozen=True)
class _PosixDescendantCapture:
    processes: tuple[_CapturedPosixProcess, ...]
    complete: bool


class _DarwinProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


_CURRENT_TASK_PROCESS_SCOPE: contextvars.ContextVar[_TaskProcessScope | None] = (
    contextvars.ContextVar("opensquilla_task_process_scope", default=None)
)


def _owner_digest(domain: str, value: str) -> str:
    return hashlib.sha256(f"opensquilla-process-owner-v1\0{domain}\0{value}".encode()).hexdigest()


def _normalize_owner_value(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


@contextmanager
def task_process_scope(
    state_dir: str | Path | None,
    *,
    session_key: str | None,
    task_id: str | None,
    parent_session_key: str | None = None,
    parent_task_id: str | None = None,
):
    """Bind privacy-preserving durable process ownership to one task turn."""

    normalized_session = _normalize_owner_value(session_key)
    normalized_task = _normalize_owner_value(task_id)
    if state_dir is None or normalized_session is None or normalized_task is None:
        yield
        return
    root = Path(state_dir).expanduser()
    normalized_parent_session = _normalize_owner_value(parent_session_key)
    normalized_parent_task = _normalize_owner_value(parent_task_id)
    scope = _TaskProcessScope(
        state_dir=root,
        session_digest=_owner_digest("session", normalized_session),
        task_digest=_owner_digest("task", normalized_task),
        parent_session_digest=(
            _owner_digest("session", normalized_parent_session)
            if normalized_parent_session is not None
            else None
        ),
        parent_task_digest=(
            _owner_digest("task", normalized_parent_task)
            if normalized_parent_task is not None
            else None
        ),
    )
    token = _CURRENT_TASK_PROCESS_SCOPE.set(scope)
    try:
        yield
    finally:
        _CURRENT_TASK_PROCESS_SCOPE.reset(token)


def _current_task_process_scope() -> _TaskProcessScope | None:
    return _CURRENT_TASK_PROCESS_SCOPE.get()


@dataclass(frozen=True)
class _PersistedOwnerRecord:
    owner_id: str
    session_digest: str
    task_digest: str
    parent_session_digest: str | None
    parent_task_digest: str | None
    platform: str
    controller_pid: int
    controller_start_identity: str

    @property
    def windows_job_name(self) -> str:
        return f"{_WINDOWS_JOB_PREFIX}{self.owner_id}"


@dataclass(frozen=True)
class _PersistedOwnerRef:
    database_path: Path
    record: _PersistedOwnerRecord


@dataclass(frozen=True)
class _PersistedOwnerClaim:
    reference: _PersistedOwnerRef
    claim_id: str


def _owner_database_path(state_dir: str | Path) -> Path:
    root = Path(state_dir).expanduser().resolve(strict=False)
    return root / _OWNER_DATABASE_FILENAME


def _owner_control_path(database_path: Path, owner_id: str) -> Path:
    state_control = database_path.parent / _OWNER_CONTROL_DIRECTORY
    candidate = state_control / f"{owner_id}.sock"
    # Darwin's sockaddr_un path is short. A deterministic, hashed temporary
    # root preserves restart discovery without storing or logging the state path.
    if len(os.fsencode(candidate)) >= 96:
        state_digest = hashlib.sha256(os.fsencode(database_path.parent)).hexdigest()[:24]
        candidate = (
            Path("/tmp")
            / f"opensquilla-task-process-{state_digest}"
            / f"{owner_id}.sock"
        )
    return candidate


def _remove_created_private_path(
    path: Path,
    metadata: os.stat_result,
    *,
    directory: bool,
) -> None:
    with contextlib.suppress(OSError):
        current = os.lstat(path)
        expected_type = stat.S_ISDIR if directory else stat.S_ISREG
        if (
            expected_type(current.st_mode)
            and not stat.S_ISLNK(current.st_mode)
            and bool(metadata.st_ino)
            and (int(current.st_dev), int(current.st_ino))
            == (int(metadata.st_dev), int(metadata.st_ino))
        ):
            (os.rmdir if directory else os.unlink)(path)


def _prepare_private_directory(path: Path) -> None:
    created = False
    metadata: os.stat_result | None = None
    try:
        if not os.path.lexists(path):
            if os.name == "nt":
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                try:
                    create_windows_private_directory(path)
                    created = True
                except FileExistsError:
                    pass
            else:
                path.mkdir(mode=0o700, parents=True, exist_ok=True)
                created = True
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ProcessTreeOwnershipError(
                "task process owner control root is not a private directory"
            )
        if os.name == "nt":
            apply_windows_private_dacl(
                path,
                directory=True,
                expected_device=int(metadata.st_dev),
                expected_inode=int(metadata.st_ino),
            )
            current = os.lstat(path)
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISDIR(current.st_mode)
                or (
                    metadata.st_ino
                    and (int(current.st_dev), int(current.st_ino))
                    != (int(metadata.st_dev), int(metadata.st_ino))
                )
            ):
                raise ProcessTreeOwnershipError(
                    "task process owner control root changed during privacy hardening"
                )
        _enforce_private_posix_path(path, mode=0o700, kind="control root")
    except ProcessTreeOwnershipError:
        if created and metadata is not None:
            _remove_created_private_path(path, metadata, directory=True)
        raise
    except OSError as exc:
        if created and metadata is not None:
            _remove_created_private_path(path, metadata, directory=True)
        if (
            os.name == "nt"
            and isinstance(exc, PermissionError)
            and getattr(exc, "winerror", None) in _WINDOWS_TRANSIENT_FILE_ERRORS
        ):
            raise
        raise ProcessTreeOwnershipError(
            "task process owner control root could not be prepared"
        ) from exc


def _enforce_private_posix_path(path: Path, *, mode: int, kind: str) -> None:
    if os.name != "posix":
        return
    metadata = path.stat()
    if metadata.st_uid != os.geteuid():
        raise ProcessTreeOwnershipError(f"task process owner {kind} has an unsafe owner")
    os.chmod(path, mode)
    if path.stat().st_mode & 0o777 != mode:
        raise ProcessTreeOwnershipError(
            f"task process owner {kind} permissions could not be restricted"
        )


def _prepare_private_file_once(path: Path) -> None:
    _prepare_private_directory(path.parent)
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        metadata = os.lstat(path)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ProcessTreeOwnershipError(
                "task process owner registry is not a private regular file"
            ) from None
    else:
        created = True
        os.close(descriptor)
        metadata = os.lstat(path)
    try:
        if os.name == "nt":
            apply_windows_private_dacl(
                path,
                directory=False,
                expected_device=int(metadata.st_dev),
                expected_inode=int(metadata.st_ino),
            )
            current = os.lstat(path)
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or (
                    metadata.st_ino
                    and (int(current.st_dev), int(current.st_ino))
                    != (int(metadata.st_dev), int(metadata.st_ino))
                )
            ):
                raise ProcessTreeOwnershipError(
                    "task process owner registry changed during privacy hardening"
                )
        else:
            _enforce_private_posix_path(path, mode=0o600, kind="registry")
    except BaseException:
        if created:
            _remove_created_private_path(path, metadata, directory=False)
        raise


def _prepare_private_file(path: Path) -> None:
    """Create a private regular file without following a pre-existing symlink."""

    for delay in (*_WINDOWS_REGISTRY_RETRY_DELAYS_SECONDS, None):
        try:
            _prepare_private_file_once(path)
            return
        except PermissionError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) not in _WINDOWS_TRANSIENT_FILE_ERRORS
                or delay is None
            ):
                raise
            time.sleep(delay)


def _prepare_existing_private_file(path: Path) -> None:
    """Harden an existing SQLite sidecar without ever recreating it."""

    for delay in (*_WINDOWS_REGISTRY_RETRY_DELAYS_SECONDS, None):
        try:
            metadata = os.lstat(path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ProcessTreeOwnershipError(
                    "task process owner registry sidecar is not a private regular file"
                )
            if os.name == "nt":
                try:
                    apply_windows_private_dacl(
                        path,
                        directory=False,
                        expected_device=int(metadata.st_dev),
                        expected_inode=int(metadata.st_ino),
                    )
                except OSError as exc:
                    # The sidecar can be replaced between the initial lstat
                    # and the Windows bound-handle open.  Retry only when a
                    # fresh regular file with a different identity is now at
                    # the path; ACL failures on the same object remain fatal.
                    try:
                        current = os.lstat(path)
                    except FileNotFoundError:
                        return
                    changed = (
                        stat.S_ISREG(current.st_mode)
                        and bool(metadata.st_ino)
                        and (
                            int(current.st_dev),
                            int(current.st_ino),
                        )
                        != (int(metadata.st_dev), int(metadata.st_ino))
                    )
                    if not changed:
                        raise
                    if delay is None:
                        raise _OwnerRegistrySidecarChangedError(
                            "task process owner registry sidecar changed during privacy hardening"
                        ) from exc
                    time.sleep(delay)
                    continue
                current = os.lstat(path)
                if (
                    stat.S_ISLNK(current.st_mode)
                    or not stat.S_ISREG(current.st_mode)
                    or (
                        metadata.st_ino
                        and (int(current.st_dev), int(current.st_ino))
                        != (int(metadata.st_dev), int(metadata.st_ino))
                    )
                ):
                    raise _OwnerRegistrySidecarChangedError(
                        "task process owner registry sidecar changed during privacy hardening"
                    )
            else:
                _enforce_private_posix_path(path, mode=0o600, kind="registry sidecar")
            return
        except FileNotFoundError:
            # Rollback journals are intentionally ephemeral. A replacement can
            # only be created inside the already-hardened registry directory.
            return
        except _OwnerRegistrySidecarChangedError:
            # SQLite may replace a rollback journal while its ACL is being
            # hardened.  Retry that one ephemeral identity race for a bounded
            # period; a persistent change remains fail-closed below.
            if os.name != "nt" or delay is None:
                raise
            time.sleep(delay)
        except PermissionError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) not in _WINDOWS_TRANSIENT_FILE_ERRORS
                or delay is None
            ):
                raise
            time.sleep(delay)


def _prepare_owner_database_paths(path: Path) -> None:
    _prepare_private_file(path)
    for suffix in _OWNER_DATABASE_SIDECAR_SUFFIXES:
        sidecar = path.with_name(f"{path.name}{suffix}")
        if os.path.lexists(sidecar):
            _prepare_existing_private_file(sidecar)


def _connect_owner_database(path: Path) -> sqlite3.Connection:
    _prepare_owner_database_paths(path)
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode=rwc&nofollow=1",
        timeout=_OWNER_DATABASE_TIMEOUT_SECONDS,
        isolation_level=None,
        uri=True,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS task_process_owners (
                owner_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                session_digest TEXT NOT NULL,
                task_digest TEXT NOT NULL,
                parent_session_digest TEXT,
                parent_task_digest TEXT,
                platform TEXT NOT NULL,
                controller_pid INTEGER NOT NULL,
                controller_start_identity TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_process_owners_task "
            "ON task_process_owners(task_digest)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_process_owners_session "
            "ON task_process_owners(session_digest)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_process_owners_parent "
            "ON task_process_owners(parent_task_digest)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS task_process_owner_claims (
                owner_id TEXT PRIMARY KEY,
                claim_id TEXT NOT NULL,
                claimant_pid INTEGER NOT NULL,
                claimant_start_identity TEXT NOT NULL
            )
            """
        )
    except BaseException:
        connection.close()
        raise
    return connection


def _platform_kind() -> str:
    if os.name == "nt" or sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    if os.name == "posix":
        return "posix"
    return "unsupported"


def _linux_process_start_identity(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        suffix = raw[raw.rfind(")") + 1 :].split()
        return f"linux-proc-start-ticks:{suffix[19]}"
    except (OSError, IndexError, ValueError):
        return None


def _windows_process_start_identity(pid: int) -> str | None:
    try:
        class FileTime(ctypes.Structure):
            _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            created = FileTime()
            exited = FileTime()
            kernel = FileTime()
            user = FileTime()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return f"windows-creation-filetime:{(int(created.high) << 32) | int(created.low)}"
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _posix_process_start_identity(pid: int) -> str | None:
    try:
        command = "/bin/ps" if Path("/bin/ps").is_file() else "ps"
        result = subprocess.run(
            [command, "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(result.stdout.split())
    if result.returncode != 0 or not value:
        return None
    return f"posix-ps-lstart:{value}"


def _strict_process_start_identity(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        return _linux_process_start_identity(pid)
    if os.name == "nt" or sys.platform == "win32":
        return _windows_process_start_identity(pid)
    if os.name == "posix":
        return _posix_process_start_identity(pid)
    return None


def _linux_process_info(pid: int) -> _PosixProcessInfo | None:
    proc_path = Path(f"/proc/{pid}")
    try:
        raw = (proc_path / "stat").read_text(encoding="utf-8")
        closing_paren = raw.rfind(")")
        if closing_paren < 0:
            return None
        suffix = raw[closing_paren + 1 :].split()
        return _PosixProcessInfo(
            pid=pid,
            ppid=int(suffix[1]),
            pgid=int(suffix[2]),
            uid=int(proc_path.stat().st_uid),
            start_identity=f"linux-proc-start-ticks:{suffix[19]}",
        )
    except (OSError, IndexError, ValueError):
        return None


def _linux_process_snapshot() -> dict[int, _PosixProcessInfo] | None:
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    snapshot: dict[int, _PosixProcessInfo] = {}
    for name in names:
        if not name.isdigit():
            continue
        info = _linux_process_info(int(name))
        if info is not None:
            snapshot[info.pid] = info
    return snapshot


@lru_cache(maxsize=1)
def _darwin_libproc() -> Any | None:
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return None
    library.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.proc_listallpids.restype = ctypes.c_int
    library.proc_listpgrppids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_listpgrppids.restype = ctypes.c_int
    library.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    return library


def _darwin_process_info(pid: int, library: Any | None = None) -> _PosixProcessInfo | None:
    library = library or _darwin_libproc()
    if library is None:
        return None
    raw = _DarwinProcBsdInfo()
    size = ctypes.sizeof(raw)
    if library.proc_pidinfo(
        int(pid),
        _DARWIN_PROC_PIDTBSDINFO,
        0,
        ctypes.byref(raw),
        size,
    ) != size:
        return None
    return _PosixProcessInfo(
        pid=int(raw.pbi_pid),
        ppid=int(raw.pbi_ppid),
        pgid=int(raw.pbi_pgid),
        uid=int(raw.pbi_uid),
        start_identity=(
            f"darwin-libproc-start:{int(raw.pbi_start_tvsec)}:"
            f"{int(raw.pbi_start_tvusec)}"
        ),
    )


def _darwin_process_snapshot() -> dict[int, _PosixProcessInfo] | None:
    library = _darwin_libproc()
    if library is None:
        return None
    estimated = library.proc_listallpids(None, 0)
    if estimated <= 0:
        return None
    pids: Any = None
    count = 0
    for _attempt in range(3):
        capacity = max(estimated + 64, 128)
        pids = (ctypes.c_int * capacity)()
        count = library.proc_listallpids(pids, ctypes.sizeof(pids))
        if count < 0:
            return None
        if count < capacity:
            break
        estimated = capacity * 2
    if pids is None or count >= len(pids):
        return None
    snapshot: dict[int, _PosixProcessInfo] = {}
    for raw_pid in pids[:count]:
        pid = int(raw_pid)
        if pid <= 1:
            continue
        info = _darwin_process_info(pid, library)
        if info is not None:
            snapshot[pid] = info
    return snapshot


def _darwin_group_members(pgid: int) -> tuple[int, ...] | None:
    library = _darwin_libproc()
    if library is None:
        return None
    estimated = library.proc_listpgrppids(int(pgid), None, 0)
    if estimated <= 0:
        return None
    pids: Any = None
    count = 0
    for _attempt in range(3):
        capacity = max(estimated + 8, 16)
        pids = (ctypes.c_int * capacity)()
        count = library.proc_listpgrppids(int(pgid), pids, ctypes.sizeof(pids))
        if count < 0:
            return None
        if count < capacity:
            break
        estimated = capacity * 2
    if pids is None or count >= len(pids):
        return None
    members = tuple(int(pid) for pid in pids[:count] if int(pid) > 1)
    if pgid not in members:
        return None
    return members


def _posix_process_info(pid: int) -> _PosixProcessInfo | None:
    if sys.platform.startswith("linux"):
        return _linux_process_info(pid)
    if sys.platform == "darwin":
        return _darwin_process_info(pid)
    return None


def _posix_process_snapshot() -> dict[int, _PosixProcessInfo] | None:
    if sys.platform.startswith("linux"):
        return _linux_process_snapshot()
    if sys.platform == "darwin":
        return _darwin_process_snapshot()
    return None


def _captured_posix_process_matches(
    info: _PosixProcessInfo | None,
    captured: _CapturedPosixProcess,
) -> bool:
    return (
        info is not None
        and info.pid == captured.pid
        and info.uid == captured.uid
        and info.start_identity == captured.start_identity
    )


def _capture_posix_group_descendants(
    pgid: int,
    anchor_pid: int,
) -> _PosixDescendantCapture:
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        return _PosixDescendantCapture((), True)
    snapshot = _posix_process_snapshot()
    if snapshot is None:
        return _PosixDescendantCapture((), False)
    anchor = snapshot.get(anchor_pid)
    if anchor is None or anchor.pgid != pgid or anchor.uid != os.geteuid():
        return _PosixDescendantCapture((), False)
    roots = tuple(
        info for info in snapshot.values() if info.pgid == pgid and info.pid != anchor_pid
    )
    if not roots:
        return _PosixDescendantCapture((), False)
    children: dict[int, list[_PosixProcessInfo]] = {}
    for info in snapshot.values():
        children.setdefault(info.ppid, []).append(info)
    seen = {root.pid for root in roots}
    pending = [(root.pid, 0) for root in roots]
    candidates: list[tuple[_PosixProcessInfo, int]] = []
    complete = True
    while pending:
        parent_pid, parent_depth = pending.pop(0)
        for child in children.get(parent_pid, ()):
            if child.pid in seen:
                continue
            seen.add(child.pid)
            if len(seen) > _POSIX_DESCENDANT_CAPTURE_LIMIT:
                return _PosixDescendantCapture((), False)
            if child.uid != anchor.uid:
                complete = False
                continue
            depth = parent_depth + 1
            pending.append((child.pid, depth))
            if child.pgid != pgid:
                candidates.append((child, depth))
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    captured: list[_CapturedPosixProcess] = []
    for candidate, depth in sorted(candidates, key=lambda item: item[1]):
        current = _posix_process_info(candidate.pid)
        if current is None or current.start_identity != candidate.start_identity:
            continue
        if current.uid != candidate.uid:
            complete = False
            continue
        pidfd: int | None = None
        if sys.platform.startswith("linux"):
            if not callable(pidfd_open) or not callable(pidfd_send_signal):
                complete = False
                continue
            else:
                try:
                    pidfd = int(pidfd_open(candidate.pid, 0))
                except OSError as exc:
                    if exc.errno == errno.ESRCH:
                        continue
                    complete = False
                    continue
                if pidfd is not None and not _captured_posix_process_matches(
                    _posix_process_info(candidate.pid),
                    _CapturedPosixProcess(
                        pid=candidate.pid,
                        uid=candidate.uid,
                        start_identity=candidate.start_identity,
                        depth=depth,
                    ),
                ):
                    os.close(pidfd)
                    continue
        captured.append(
            _CapturedPosixProcess(
                pid=candidate.pid,
                uid=candidate.uid,
                start_identity=candidate.start_identity,
                depth=depth,
                pidfd=pidfd,
            )
        )
    return _PosixDescendantCapture(tuple(captured), complete)


def _signal_captured_posix_processes(
    captured: tuple[_CapturedPosixProcess, ...],
    sig: int,
) -> bool:
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    complete = True
    for process in captured:
        try:
            if process.pidfd is not None and callable(pidfd_send_signal):
                pidfd_send_signal(process.pidfd, sig, None, 0)
            elif _captured_posix_process_matches(
                _posix_process_info(process.pid),
                process,
            ):
                os.kill(process.pid, sig)
        except ProcessLookupError:
            continue
        except OSError:
            complete = False
    return complete


def _captured_posix_processes_alive(
    captured: tuple[_CapturedPosixProcess, ...],
) -> bool:
    pidfds = tuple(process.pidfd for process in captured if process.pidfd is not None)
    exited_pidfds: set[int] = set()
    if pidfds:
        try:
            poller = select.poll()
            for pidfd in pidfds:
                poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
            exited_pidfds = {int(pidfd) for pidfd, _events in poller.poll(0)}
        except (AttributeError, OSError, ValueError):
            return True
    for process in captured:
        if process.pidfd is not None:
            if process.pidfd not in exited_pidfds:
                return True
        elif _captured_posix_process_matches(
            _posix_process_info(process.pid),
            process,
        ):
            return True
    return False


def _close_captured_posix_processes(
    captured: tuple[_CapturedPosixProcess, ...],
) -> None:
    for process in captured:
        if process.pidfd is not None:
            with contextlib.suppress(OSError):
                os.close(process.pidfd)


def _valid_owner_record(record: _PersistedOwnerRecord) -> bool:
    return (
        _OWNER_ID_RE.fullmatch(record.owner_id) is not None
        and _OWNER_DIGEST_RE.fullmatch(record.session_digest) is not None
        and _OWNER_DIGEST_RE.fullmatch(record.task_digest) is not None
        and (
            record.parent_session_digest is None
            or _OWNER_DIGEST_RE.fullmatch(record.parent_session_digest) is not None
        )
        and (
            record.parent_task_digest is None
            or _OWNER_DIGEST_RE.fullmatch(record.parent_task_digest) is not None
        )
        and record.platform in {"linux", "posix", "windows"}
        and record.controller_pid > 1
        and 0 < len(record.controller_start_identity) <= _OWNER_IDENTITY_MAX_CHARS
    )


def _insert_owner_record(
    scope: _TaskProcessScope,
    *,
    owner_id: str,
    platform: str,
    controller_pid: int,
) -> _PersistedOwnerRef:
    identity = _strict_process_start_identity(controller_pid)
    if identity is None:
        raise ProcessTreeOwnershipError(
            "task process ownership could not capture a stable controller identity"
        )
    record = _PersistedOwnerRecord(
        owner_id=owner_id,
        session_digest=scope.session_digest,
        task_digest=scope.task_digest,
        parent_session_digest=scope.parent_session_digest,
        parent_task_digest=scope.parent_task_digest,
        platform=platform,
        controller_pid=int(controller_pid),
        controller_start_identity=identity,
    )
    if not _valid_owner_record(record):
        raise ProcessTreeOwnershipError("task process ownership record was invalid")
    path = _owner_database_path(scope.state_dir)
    try:
        with _connect_owner_database(path) as connection:
            connection.execute(
                """
                INSERT INTO task_process_owners (
                    owner_id, schema_version, session_digest, task_digest,
                    parent_session_digest, parent_task_digest, platform,
                    controller_pid, controller_start_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.owner_id,
                    _OWNER_SCHEMA_VERSION,
                    record.session_digest,
                    record.task_digest,
                    record.parent_session_digest,
                    record.parent_task_digest,
                    record.platform,
                    record.controller_pid,
                    record.controller_start_identity,
                ),
            )
    except (OSError, sqlite3.Error) as exc:
        raise ProcessTreeOwnershipError(
            "task process ownership could not be persisted before launch"
        ) from exc
    return _PersistedOwnerRef(database_path=path, record=record)


def _delete_owner_record(reference: _PersistedOwnerRef) -> None:
    record = reference.record
    try:
        with _connect_owner_database(reference.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM task_process_owners "
                "WHERE owner_id = ? AND controller_pid = ? "
                "AND controller_start_identity = ?",
                (
                    record.owner_id,
                    record.controller_pid,
                    record.controller_start_identity,
                ),
            )
            connection.execute(
                "DELETE FROM task_process_owner_claims WHERE owner_id = ?",
                (record.owner_id,),
            )
            connection.execute("COMMIT")
    except (OSError, sqlite3.Error):
        log.warning("process_tree_owner_record_delete_failed")
        return
    if record.platform in {"linux", "posix"}:
        control_path = _owner_control_path(reference.database_path, record.owner_id)
        with contextlib.suppress(OSError):
            control_path.unlink()


def _claim_owner_record(reference: _PersistedOwnerRef) -> _PersistedOwnerClaim | None:
    claimant_pid = os.getpid()
    claimant_identity = _strict_process_start_identity(claimant_pid)
    if claimant_identity is None:
        log.warning("process_tree_owner_claim_identity_unavailable")
        return None
    claim_id = uuid.uuid4().hex
    try:
        with _connect_owner_database(reference.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT claim_id, claimant_pid, claimant_start_identity "
                "FROM task_process_owner_claims WHERE owner_id = ?",
                (reference.record.owner_id,),
            ).fetchone()
            if existing is not None:
                existing_pid = int(existing["claimant_pid"])
                existing_identity = str(existing["claimant_start_identity"])
                if _strict_process_start_identity(existing_pid) == existing_identity:
                    connection.execute("ROLLBACK")
                    return None
                connection.execute(
                    "DELETE FROM task_process_owner_claims WHERE owner_id = ?",
                    (reference.record.owner_id,),
                )
            connection.execute(
                "INSERT INTO task_process_owner_claims ("
                "owner_id, claim_id, claimant_pid, claimant_start_identity"
                ") VALUES (?, ?, ?, ?)",
                (
                    reference.record.owner_id,
                    claim_id,
                    claimant_pid,
                    claimant_identity,
                ),
            )
            connection.execute("COMMIT")
    except (OSError, sqlite3.Error, TypeError, ValueError):
        log.warning("process_tree_owner_claim_failed")
        return None
    return _PersistedOwnerClaim(reference=reference, claim_id=claim_id)


def _release_owner_claim(claim: _PersistedOwnerClaim) -> None:
    try:
        with _connect_owner_database(claim.reference.database_path) as connection:
            connection.execute(
                "DELETE FROM task_process_owner_claims "
                "WHERE owner_id = ? AND claim_id = ?",
                (claim.reference.record.owner_id, claim.claim_id),
            )
    except (OSError, sqlite3.Error):
        log.warning("process_tree_owner_claim_release_failed")


def _load_owner_records(state_dir: str | Path) -> tuple[_PersistedOwnerRef, ...]:
    path = _owner_database_path(state_dir)
    if not path.exists():
        return ()
    try:
        with _connect_owner_database(path) as connection:
            rows = connection.execute(
                "SELECT owner_id, schema_version, session_digest, task_digest, "
                "parent_session_digest, parent_task_digest, platform, "
                "controller_pid, controller_start_identity FROM task_process_owners"
            ).fetchall()
    except (OSError, sqlite3.Error):
        log.warning("process_tree_owner_registry_read_failed")
        return ()
    records: list[_PersistedOwnerRef] = []
    for row in rows:
        try:
            schema_version = int(row["schema_version"])
        except (TypeError, ValueError):
            log.warning("process_tree_owner_record_invalid")
            continue
        if schema_version != _OWNER_SCHEMA_VERSION:
            log.warning("process_tree_owner_record_schema_unsupported")
            continue
        try:
            record = _PersistedOwnerRecord(
                owner_id=str(row["owner_id"]),
                session_digest=str(row["session_digest"]),
                task_digest=str(row["task_digest"]),
                parent_session_digest=(
                    str(row["parent_session_digest"])
                    if row["parent_session_digest"] is not None
                    else None
                ),
                parent_task_digest=(
                    str(row["parent_task_digest"])
                    if row["parent_task_digest"] is not None
                    else None
                ),
                platform=str(row["platform"]),
                controller_pid=int(row["controller_pid"]),
                controller_start_identity=str(row["controller_start_identity"]),
            )
        except (TypeError, ValueError):
            log.warning("process_tree_owner_record_invalid")
            continue
        if not _valid_owner_record(record):
            log.warning("process_tree_owner_record_invalid")
            continue
        records.append(_PersistedOwnerRef(database_path=path, record=record))
    return tuple(records)


class ProcessTreeOwnershipError(RuntimeError):
    """Raised when a platform cannot safely own a requested process tree."""


class _OwnerRegistrySidecarChangedError(ProcessTreeOwnershipError):
    """An SQLite sidecar changed during one bounded privacy-hardening attempt."""


class _PosixTargetExecError(RuntimeError):
    def __init__(self, error_number: int, executable: str) -> None:
        super().__init__(error_number, executable)
        self.error_number = error_number
        self.executable = executable


def _windows_error(code: int | None = None) -> OSError:
    if code is None:
        code = int(getattr(ctypes, "get_last_error")())
    message = str(getattr(ctypes, "FormatError")(code)).strip()
    return OSError(code, message)


class _WindowsJob:
    """Small ctypes wrapper around one kill-on-close Windows Job Object."""

    def __init__(self, kernel32: Any, handle: Any, *, name: str | None = None) -> None:
        self._kernel32 = kernel32
        self._handle = handle
        self.name = name
        self._lock = threading.Lock()

    @classmethod
    def create(cls, name: str | None = None) -> _WindowsJob:
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
        kernel32.IsProcessInJob.argtypes = [handle_type, handle_type, ctypes.POINTER(bool_type)]
        kernel32.IsProcessInJob.restype = bool_type
        kernel32.CloseHandle.argtypes = [handle_type]
        kernel32.CloseHandle.restype = bool_type

        job_object_extended_limit_information = 9
        job_object_limit_kill_on_job_close = 0x00002000
        job = kernel32.CreateJobObjectW(None, name)
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
            return cls(kernel32, job, name=name)
        except BaseException:
            kernel32.CloseHandle(job)
            raise

    @classmethod
    def open(cls, name: str) -> _WindowsJob:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable on this platform")
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        kernel32.OpenJobObjectW.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.OpenJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        job = kernel32.OpenJobObjectW(0x0004 | 0x0008, False, name)
        if not job:
            raise _windows_error()
        return cls(kernel32, job, name=name)

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

    def contains_pid(self, pid: int) -> bool:
        process_handle = self._kernel32.OpenProcess(0x1000, False, int(pid))
        if not process_handle:
            return False
        try:
            contained = wintypes.BOOL()
            if not self._kernel32.IsProcessInJob(
                process_handle,
                self._handle,
                ctypes.byref(contained),
            ):
                raise _windows_error()
            return bool(contained.value)
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
class _PosixLaunchGate:
    read_fd: int
    write_fd: int

    @classmethod
    def create(cls) -> _PosixLaunchGate:
        read_fd, write_fd = os.pipe()
        return cls(read_fd=read_fd, write_fd=write_fd)

    def child_pass_fds(self, existing: Any) -> tuple[int, ...]:
        descriptors = tuple(int(value) for value in (existing or ()))
        return (*descriptors, self.read_fd)

    def close_child_end(self) -> None:
        if self.read_fd >= 0:
            os.close(self.read_fd)
            self.read_fd = -1

    def release(self) -> None:
        if self.write_fd < 0:
            raise ProcessTreeOwnershipError("POSIX controlled launch gate is closed")
        os.write(self.write_fd, _POSIX_TARGET_RELEASE)
        os.close(self.write_fd)
        self.write_fd = -1

    def close(self) -> None:
        for field_name in ("read_fd", "write_fd"):
            descriptor = getattr(self, field_name)
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                setattr(self, field_name, -1)


@dataclass
class _PosixGroupAnchor:
    process: Any
    pgid: int
    control_path: Path | None = None
    empty: bool = False
    cleanup_incomplete: bool = False
    _owner: ProcessTreeOwner | None = field(default=None, repr=False)
    _monitor_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _term_reports: asyncio.Queue[bool] = field(default_factory=asyncio.Queue, repr=False)
    _kill_reports: asyncio.Queue[bool] = field(default_factory=asyncio.Queue, repr=False)
    _kill_reported: bool = field(default=False, repr=False)

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
        while True:
            try:
                marker = await stdout.read(1)
            except (BrokenPipeError, ConnectionResetError):
                marker = b""
            if marker == _POSIX_ANCHOR_CAPTURED:
                self._term_reports.put_nowait(True)
                continue
            if marker == _POSIX_ANCHOR_INCOMPLETE:
                self.cleanup_incomplete = True
                self._term_reports.put_nowait(True)
                continue
            if marker == _POSIX_ANCHOR_KILL_CAPTURED:
                self._kill_reported = True
                self._kill_reports.put_nowait(True)
                continue
            if marker == _POSIX_ANCHOR_KILL_INCOMPLETE:
                self.cleanup_incomplete = True
                self._kill_reported = True
                self._kill_reports.put_nowait(True)
                continue
            if marker == _POSIX_ANCHOR_EMPTY:
                self.empty = True
                owner = self._owner
                if owner is not None:
                    await owner._close_empty_posix_owner()
            elif not self._kill_reported:
                self.cleanup_incomplete = True
                self._term_reports.put_nowait(False)
                self._kill_reports.put_nowait(False)
            break
        with contextlib.suppress(Exception):
            await self.process.wait()

    def release(self) -> None:
        stdin = getattr(self.process, "stdin", None)
        if stdin is None or stdin.is_closing():
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.write(_POSIX_ANCHOR_RELEASE)
        stdin.close()

    async def request_signal(self, command: bytes) -> bool:
        stdin = getattr(self.process, "stdin", None)
        if command not in {_POSIX_ANCHOR_TERMINATE, _POSIX_ANCHOR_KILL}:
            raise ValueError("invalid POSIX anchor signal command")
        if stdin is None or stdin.is_closing() or not self.alive:
            if not self.empty and not self._kill_reported:
                self.cleanup_incomplete = True
            return False
        reports = (
            self._term_reports
            if command == _POSIX_ANCHOR_TERMINATE
            else self._kill_reports
        )
        while not reports.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                reports.get_nowait()
        try:
            stdin.write(command)
            await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            self.cleanup_incomplete = True
            return False
        if self._monitor_task is not None:
            try:
                acknowledged = await asyncio.wait_for(
                    reports.get(), timeout=_CONTROL_READY_TIMEOUT_SECONDS
                )
            except TimeoutError:
                self.cleanup_incomplete = True
                return False
            if not acknowledged:
                return False
        return True

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
    persisted_owner: _PersistedOwnerRef | None = field(default=None, repr=False)
    _closed: bool = False
    _terminate_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _completion_monitor: asyncio.Task[None] | None = field(default=None, repr=False)

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
            return self.posix_anchor is not None and self.posix_anchor.alive
        if self.windows_job is not None:
            try:
                active = self.windows_job.active_process_count() > 0
            except OSError:
                log.warning("process_tree_job_query_failed", exc_info=True)
                return True
            if not active:
                self.windows_job.close_if_empty()
            return active
        return getattr(self.process, "returncode", None) is None

    def _take_persisted_owner(self) -> _PersistedOwnerRef | None:
        if self._closed:
            return None
        self._closed = True
        persisted_owner = self.persisted_owner
        self.persisted_owner = None
        return persisted_owner

    async def _mark_closed(self) -> None:
        persisted_owner = self._take_persisted_owner()
        if persisted_owner is not None:
            await asyncio.to_thread(_delete_owner_record, persisted_owner)

    async def _close_empty_posix_owner(self) -> None:
        if self._closed or self.posix_anchor is None:
            return
        await self._mark_closed()
        self.posix_anchor.release()

    def start_completion_monitor(self) -> None:
        if (
            not isinstance(self.windows_job, _WindowsJob)
            or self._completion_monitor is not None
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._completion_monitor = loop.create_task(self._watch_windows_job_empty())

    async def _watch_windows_job_empty(self) -> None:
        while not self._closed and self.is_active():
            await asyncio.sleep(0.05)
        if not self._closed:
            await self._mark_closed()

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
                await self._mark_closed()
                if self.posix_anchor is not None:
                    await self.posix_anchor.settle(kill_timeout)
                    return not self.posix_anchor.cleanup_incomplete
                return True
            if self.pgid is not None:
                assert self.posix_anchor is not None
                if not await self.posix_anchor.request_signal(_POSIX_ANCHOR_TERMINATE):
                    if not self.is_active():
                        await self._mark_closed()
                        await self.posix_anchor.settle(kill_timeout)
                        return not self.posix_anchor.cleanup_incomplete
                    return False
                if await self._wait_inactive(graceful_timeout):
                    await self._mark_closed()
                    if self.posix_anchor is not None:
                        await self.posix_anchor.settle(kill_timeout)
                    return not self.posix_anchor.cleanup_incomplete
                if not await self.posix_anchor.request_signal(_POSIX_ANCHOR_KILL):
                    if not self.is_active():
                        await self._mark_closed()
                        await self.posix_anchor.settle(kill_timeout)
                        return not self.posix_anchor.cleanup_incomplete
                    return False
                stopped = await self._wait_inactive(kill_timeout)
                if stopped:
                    await self._mark_closed()
                    if self.posix_anchor is not None:
                        await self.posix_anchor.settle(kill_timeout)
                return stopped and not self.posix_anchor.cleanup_incomplete
            if self.windows_job is not None:
                try:
                    await asyncio.to_thread(self.windows_job.terminate)
                except OSError:
                    log.warning(
                        "process_tree_job_terminate_failed",
                        extra={"pid": self.pid},
                        exc_info=True,
                    )
                stopped = await self._wait_inactive(kill_timeout)
                if stopped:
                    await self._mark_closed()
                return stopped

            # No durable tree primitive was established. Direct-child cleanup
            # is safe; taskkill/process-name scans are not, because PID reuse or
            # shared services could widen the blast radius.
            if getattr(self.process, "returncode", None) is not None:
                await self._mark_closed()
                return True
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            if await _wait_direct_process(self.process, graceful_timeout):
                await self._mark_closed()
                return True
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            stopped = await _wait_direct_process(self.process, kill_timeout)
            if stopped:
                await self._mark_closed()
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


async def _create_posix_anchor(
    owner_id: str | None = None,
    *,
    control_path: Path | None = None,
) -> _PosixGroupAnchor:
    owner_id = owner_id or uuid.uuid4().hex
    if control_path is not None:
        _prepare_private_directory(control_path.parent)
    process = await asyncio.create_subprocess_exec(
        *_process_tree_child_argv(
            "--posix-group-anchor",
            owner_id,
            *(str(control_path) if control_path is not None else "-",),
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        process_group=0,
    )
    anchor = _PosixGroupAnchor(
        process=process,
        pgid=int(process.pid),
        control_path=control_path,
    )
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


async def _cleanup_failed_posix_launch(
    *,
    gate: _PosixLaunchGate,
    persisted_owner: _PersistedOwnerRef | None,
    process: Any | None,
    anchor: _PosixGroupAnchor,
) -> None:
    with contextlib.suppress(BaseException):
        gate.close()
    if process is not None:
        with contextlib.suppress(BaseException):
            await _stop_failed_async_process(process)
    with contextlib.suppress(BaseException):
        await _stop_unarmed_posix_anchor(anchor)
    if persisted_owner is not None:
        with contextlib.suppress(BaseException):
            await asyncio.to_thread(_delete_owner_record, persisted_owner)


async def _cleanup_failed_windows_launch(
    *,
    persisted_owner: _PersistedOwnerRef | None,
    process: Any | None,
    job: _WindowsJob,
) -> None:
    if process is not None:
        with contextlib.suppress(BaseException):
            await _stop_failed_async_process(process)
    with contextlib.suppress(BaseException):
        job.close()
    if persisted_owner is not None:
        with contextlib.suppress(BaseException):
            await asyncio.to_thread(_delete_owner_record, persisted_owner)


async def _await_cleanup_before_cancellation(cleanup: asyncio.Task[None]) -> None:
    """Finish an isolated cleanup task despite repeated caller cancellation."""

    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            continue
    with contextlib.suppress(BaseException):
        cleanup.result()


async def _create_owned_posix_subprocess(
    argv: tuple[str, ...],
    kwargs: dict[str, Any],
) -> Any:
    owner_id = uuid.uuid4().hex
    task_scope = _current_task_process_scope()
    database_path = (
        _owner_database_path(task_scope.state_dir) if task_scope is not None else None
    )
    control_path = (
        _owner_control_path(database_path, owner_id) if database_path is not None else None
    )
    anchor = await _create_posix_anchor(owner_id, control_path=control_path)
    persisted_owner: _PersistedOwnerRef | None = None
    gate = _PosixLaunchGate.create()
    status_read_fd, status_write_fd = os.pipe()
    process: Any | None = None
    try:
        if task_scope is not None:
            persisted_owner = _insert_owner_record(
                task_scope,
                owner_id=owner_id,
                platform=_platform_kind(),
                controller_pid=int(anchor.process.pid),
            )
        child_kwargs = dict(kwargs)
        child_kwargs.pop("start_new_session", None)
        child_kwargs.pop("process_group", None)
        child_kwargs["process_group"] = anchor.pgid
        child_kwargs["pass_fds"] = (
            *gate.child_pass_fds(child_kwargs.get("pass_fds")),
            status_write_fd,
        )
        process = await asyncio.create_subprocess_exec(
            *_process_tree_child_argv(
                "--posix-owned-launch",
                str(gate.read_fd),
                str(status_write_fd),
                "--",
                *argv,
            ),
            **child_kwargs,
        )
        gate.close_child_end()
        os.close(status_write_fd)
        status_write_fd = -1
        owner = ProcessTreeOwner(
            process=process,
            pid=int(process.pid),
            pgid=anchor.pgid,
            posix_anchor=anchor,
            persisted_owner=persisted_owner,
        )
        anchor.bind(owner)
        _attach_owner(process, owner)
        await anchor.arm()
        gate.release()
        exec_error = await asyncio.to_thread(os.read, status_read_fd, 16)
        if exec_error:
            try:
                error_number = int(exec_error.decode("ascii"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise ProcessTreeOwnershipError(
                    "POSIX controlled launch returned an invalid exec status"
                ) from exc
            raise _PosixTargetExecError(error_number, argv[0])
        return process
    except BaseException as exc:
        cleanup = asyncio.create_task(
            _cleanup_failed_posix_launch(
                gate=gate,
                persisted_owner=persisted_owner,
                process=process,
                anchor=anchor,
            )
        )
        if isinstance(exc, asyncio.CancelledError):
            await _await_cleanup_before_cancellation(cleanup)
            raise
        await cleanup
        if isinstance(exc, _PosixTargetExecError):
            raise OSError(
                exc.error_number,
                os.strerror(exc.error_number),
                exc.executable,
            ) from None
        raise ProcessTreeOwnershipError(
            "POSIX controlled process launch failed closed"
        ) from exc
    finally:
        gate.close()
        for descriptor in (status_read_fd, status_write_fd):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)


async def create_owned_subprocess_exec(*argv: str, **kwargs: Any) -> Any:
    """Spawn an argv under durable task-owned tree containment."""

    if os.name == "posix":
        return await _create_owned_posix_subprocess(tuple(argv), dict(kwargs))

    owner_id = uuid.uuid4().hex
    task_scope = _current_task_process_scope()

    if os.name == "nt":
        gate = _WindowsLaunchGate.create()
        job = (
            _WindowsJob.create(f"{_WINDOWS_JOB_PREFIX}{owner_id}")
            if task_scope is not None
            else _WindowsJob.create()
        )
        child_kwargs = dict(kwargs)
        child_kwargs.pop("start_new_session", None)
        child_kwargs["creationflags"] = (
            int(child_kwargs.get("creationflags", 0))
            | _WINDOWS_CREATE_BREAKAWAY_FROM_JOB
        )
        child_kwargs["env"] = _windows_helper_env(child_kwargs.get("env"))
        helper_argv = _process_tree_child_argv(
            "--windows-owned-launch",
            gate.gate_name,
            gate.ready_name,
            "--",
            *argv,
        )
        windows_process: Any | None = None
        persisted_owner = None
        try:
            windows_process = await asyncio.create_subprocess_exec(
                *helper_argv,
                **child_kwargs,
            )
            job.assign_pid(int(windows_process.pid))
            await asyncio.to_thread(
                _wait_for_windows_helper_ready,
                gate,
            )
            if task_scope is not None:
                persisted_owner = _insert_owner_record(
                    task_scope,
                    owner_id=owner_id,
                    platform="windows",
                    controller_pid=int(windows_process.pid),
                )
            owner = ProcessTreeOwner(
                process=windows_process,
                pid=int(windows_process.pid),
                windows_job=job,
                persisted_owner=persisted_owner,
            )
            _attach_owner(windows_process, owner)
            gate.release()
            owner.start_completion_monitor()
            return windows_process
        except BaseException as exc:
            cleanup = asyncio.create_task(
                _cleanup_failed_windows_launch(
                    persisted_owner=persisted_owner,
                    process=windows_process,
                    job=job,
                )
            )
            if isinstance(exc, asyncio.CancelledError):
                await _await_cleanup_before_cancellation(cleanup)
                raise
            await cleanup
            raise ProcessTreeOwnershipError(
                "Windows controlled process launch failed closed"
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
    child_kwargs = dict(kwargs)
    shell = str(child_kwargs.pop("executable", None) or "/bin/sh")
    return await _create_owned_posix_subprocess(
        (shell, "-c", command),
        child_kwargs,
    )


def create_owned_popen(argv: list[str] | tuple[str, ...], **kwargs: Any) -> Any:
    """Synchronous Windows controlled-helper launcher for blocking pipe I/O."""

    if os.name != "nt":
        raise ProcessTreeOwnershipError("synchronous owned launcher is Windows-only")
    owner_id = uuid.uuid4().hex
    task_scope = _current_task_process_scope()
    gate = _WindowsLaunchGate.create()
    job = (
        _WindowsJob.create(f"{_WINDOWS_JOB_PREFIX}{owner_id}")
        if task_scope is not None
        else _WindowsJob.create()
    )
    child_kwargs = dict(kwargs)
    child_kwargs.pop("start_new_session", None)
    child_kwargs["creationflags"] = (
        int(child_kwargs.get("creationflags", 0))
        | _WINDOWS_CREATE_BREAKAWAY_FROM_JOB
    )
    child_kwargs["env"] = _windows_helper_env(child_kwargs.get("env"))
    helper_argv = _process_tree_child_argv(
        "--windows-owned-launch",
        gate.gate_name,
        gate.ready_name,
        "--",
        *argv,
    )
    process: Any | None = None
    persisted_owner: _PersistedOwnerRef | None = None
    try:
        process = subprocess.Popen(helper_argv, **child_kwargs)
        job.assign_pid(int(process.pid))
        _wait_for_windows_helper_ready(gate)
        if task_scope is not None:
            persisted_owner = _insert_owner_record(
                task_scope,
                owner_id=owner_id,
                platform="windows",
                controller_pid=int(process.pid),
            )
        owner = ProcessTreeOwner(
            process=process,
            pid=int(process.pid),
            windows_job=job,
            persisted_owner=persisted_owner,
        )
        _attach_owner(process, owner)
        gate.release()
        owner.start_completion_monitor()
        return process
    except BaseException as exc:
        if persisted_owner is not None:
            _delete_owner_record(persisted_owner)
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
            "Windows controlled process launch failed closed"
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
    if sys.platform == "darwin":
        members = _darwin_group_members(pgid)
        if members is not None:
            return members
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


def _run_posix_group_anchor(control_path_raw: str) -> int:
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, signal.SIG_IGN)
    own_pid = os.getpid()
    pgid = os.getpgrp()
    if pgid != own_pid:
        return 125
    control_path = Path(control_path_raw) if control_path_raw != "-" else None
    server: socket.socket | None = None
    captured: tuple[_CapturedPosixProcess, ...] = ()
    capture_attempted = False
    cleanup_complete = True

    def prepare_capture() -> bytes:
        nonlocal captured, capture_attempted, cleanup_complete
        if not capture_attempted:
            try:
                result = _capture_posix_group_descendants(pgid, own_pid)
            except Exception:
                result = _PosixDescendantCapture((), False)
            captured = result.processes
            capture_attempted = True
            cleanup_complete = cleanup_complete and result.complete
        return _POSIX_ANCHOR_CAPTURED if cleanup_complete else _POSIX_ANCHOR_INCOMPLETE

    def report_capture(command: bytes, marker: bytes) -> None:
        pipe_marker = marker
        if command == _POSIX_ANCHOR_KILL:
            pipe_marker = (
                _POSIX_ANCHOR_KILL_CAPTURED
                if marker == _POSIX_ANCHOR_CAPTURED
                else _POSIX_ANCHOR_KILL_INCOMPLETE
            )
        try:
            sys.stdout.buffer.write(pipe_marker)
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError):
            pass

    def signal_owned(command: bytes) -> bytes:
        nonlocal cleanup_complete
        prepare_capture()
        if command == _POSIX_ANCHOR_TERMINATE:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                cleanup_complete = False
            signal_complete = _signal_captured_posix_processes(
                captured,
                signal.SIGTERM,
            )
            cleanup_complete = cleanup_complete and signal_complete
            marker = _POSIX_ANCHOR_CAPTURED if cleanup_complete else _POSIX_ANCHOR_INCOMPLETE
            report_capture(command, marker)
            return marker
        signal_complete = _signal_captured_posix_processes(
            captured,
            getattr(signal, "SIGKILL", signal.SIGTERM),
        )
        cleanup_complete = cleanup_complete and signal_complete
        marker = _POSIX_ANCHOR_CAPTURED if cleanup_complete else _POSIX_ANCHOR_INCOMPLETE
        report_capture(command, marker)
        return marker

    try:
        if control_path is not None:
            if control_path.exists() or control_path.is_symlink():
                return 125
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(control_path))
            with contextlib.suppress(OSError):
                os.chmod(control_path, 0o600)
            server.listen(2)
        sys.stdout.buffer.write(_POSIX_ANCHOR_READY)
        sys.stdout.buffer.flush()
        if sys.stdin.buffer.read(1) != _POSIX_ANCHOR_ARM:
            return 125
        stdin_fd = sys.stdin.fileno()
        os.set_blocking(stdin_fd, False)
        stdin_open = True
        poll_delay = _POLL_INTERVAL_SECONDS
        poll_cap = 0.25 if os.path.isdir("/proc") else 1.0
        while True:
            members = _posix_group_members(pgid)
            if (
                members is not None
                and len(members) == 1
                and members[0] == own_pid
                and not _captured_posix_processes_alive(captured)
            ):
                try:
                    sys.stdout.buffer.write(_POSIX_ANCHOR_EMPTY)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    return 0
                if not stdin_open:
                    return 0
                os.set_blocking(stdin_fd, True)
                return 0 if os.read(stdin_fd, 1) == _POSIX_ANCHOR_RELEASE else 125
            pipe_command = b""
            readers: list[Any] = [stdin_fd] if stdin_open else []
            if server is not None:
                readers.append(server)
            try:
                readable, _writable, _exceptional = select.select(
                    readers,
                    [],
                    [],
                    poll_delay,
                )
            except OSError:
                readable = []
                stdin_open = False
            if stdin_open and stdin_fd in readable:
                pipe_command = os.read(stdin_fd, 1)
                if not pipe_command:
                    stdin_open = False
            if pipe_command == _POSIX_ANCHOR_TERMINATE:
                signal_owned(pipe_command)
                poll_delay = _POLL_INTERVAL_SECONDS
            elif pipe_command == _POSIX_ANCHOR_KILL:
                signal_owned(pipe_command)
                try:
                    os.killpg(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
                except OSError:
                    cleanup_complete = False
                    report_capture(pipe_command, _POSIX_ANCHOR_INCOMPLETE)
                    poll_delay = _POLL_INTERVAL_SECONDS
                else:
                    return 0
            connection: socket.socket | None = None
            if server is not None and server in readable:
                connection, _address = server.accept()
            if connection is not None:
                with connection:
                    connection.settimeout(1.5)
                    try:
                        command = connection.recv(1)
                    except OSError:
                        command = b""
                    if command not in {
                        _POSIX_ANCHOR_TERMINATE,
                        _POSIX_ANCHOR_KILL,
                    }:
                        continue
                    if command == _POSIX_ANCHOR_TERMINATE:
                        marker = signal_owned(command)
                        with contextlib.suppress(OSError):
                            connection.sendall(marker)
                        poll_delay = _POLL_INTERVAL_SECONDS
                    else:
                        marker = signal_owned(command)
                        with contextlib.suppress(OSError):
                            connection.sendall(marker)
                        try:
                            os.killpg(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
                        except OSError:
                            cleanup_complete = False
                            marker = _POSIX_ANCHOR_INCOMPLETE
                            report_capture(command, marker)
                            with contextlib.suppress(OSError):
                                connection.sendall(marker)
                            poll_delay = _POLL_INTERVAL_SECONDS
                        else:
                            return 0
            poll_delay = min(poll_cap, poll_delay * 1.5)
    finally:
        _close_captured_posix_processes(captured)
        if server is not None:
            server.close()
        if control_path is not None:
            with contextlib.suppress(OSError):
                control_path.unlink()


def _run_posix_owned_launch(gate_fd: int, status_fd: int, argv: list[str]) -> int:
    try:
        with os.fdopen(gate_fd, "rb", closefd=True) as gate:
            if gate.read(1) != _POSIX_TARGET_RELEASE:
                return 125
        if not argv:
            return 127
        os.set_inheritable(status_fd, False)
        os.execvpe(argv[0], argv, os.environ)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.write(status_fd, str(int(exc.errno or 5)).encode("ascii"))
        return 127
    finally:
        with contextlib.suppress(OSError):
            os.close(status_fd)
    return 127


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
    except OSError:
        print("OpenSquilla controlled launch failed", file=sys.stderr)
        return 127
    return int(process.wait())


def _posix_anchor_command_matches(record: _PersistedOwnerRecord) -> bool:
    pid = record.controller_pid
    if sys.platform.startswith("linux"):
        try:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return False
        decoded = [value.decode("utf-8", errors="replace") for value in argv if value]
        return (
            "opensquilla.process_tree" in decoded
            and "--posix-group-anchor" in decoded
            and record.owner_id in decoded
        )
    try:
        command = "/bin/ps" if Path("/bin/ps").is_file() else "ps"
        result = subprocess.run(
            [command, "-o", "command=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    command_line = result.stdout.strip()
    return (
        result.returncode == 0
        and "opensquilla.process_tree" in command_line
        and "--posix-group-anchor" in command_line
        and record.owner_id in command_line.split()
    )


def _controller_identity_matches(record: _PersistedOwnerRecord) -> bool:
    return (
        _strict_process_start_identity(record.controller_pid)
        == record.controller_start_identity
    )


def _posix_controller_matches(record: _PersistedOwnerRecord) -> bool:
    if not _controller_identity_matches(record):
        return False
    try:
        if os.getpgid(record.controller_pid) != record.controller_pid:
            return False
    except (OSError, AttributeError):
        return False
    return _posix_anchor_command_matches(record)


async def _wait_until(check: Any, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
    while check():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_POLL_INTERVAL_SECONDS, remaining))
    return True


def _terminate_persisted_posix_owner_sync(reference: _PersistedOwnerRef) -> bool:
    record = reference.record
    if not _posix_controller_matches(record):
        _delete_owner_record(reference)
        return False
    control_path = _owner_control_path(reference.database_path, record.owner_id)

    def request(command: bytes) -> bytes:
        response = b""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.5)
            client.connect(str(control_path))
            if not _posix_controller_matches(record):
                raise ProcessLookupError
            client.sendall(command)
            if command == _POSIX_ANCHOR_TERMINATE:
                with contextlib.suppress(OSError, TimeoutError):
                    response = client.recv(1)
            elif command == _POSIX_ANCHOR_KILL:
                with contextlib.suppress(OSError, TimeoutError):
                    markers = bytearray()
                    while len(markers) < 2:
                        marker = client.recv(1)
                        if not marker:
                            break
                        markers.extend(marker)
                    response = bytes(markers[-1:])
        return response

    try:
        capture_marker = request(_POSIX_ANCHOR_TERMINATE)
    except OSError:
        if not _posix_controller_matches(record):
            _delete_owner_record(reference)
        else:
            log.warning("process_tree_persisted_posix_control_unavailable")
        return False
    legacy_anchor = capture_marker == _POSIX_ANCHOR_LEGACY_TERMINATED
    graceful_deadline = time.monotonic() + 0.2
    while _posix_controller_matches(record) and time.monotonic() < graceful_deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
    if _posix_controller_matches(record):
        try:
            kill_marker = request(_POSIX_ANCHOR_KILL)
            if not legacy_anchor and kill_marker != _POSIX_ANCHOR_CAPTURED:
                capture_marker = _POSIX_ANCHOR_INCOMPLETE
        except OSError:
            if not _posix_controller_matches(record):
                _delete_owner_record(reference)
            else:
                log.warning("process_tree_persisted_posix_control_unavailable")
            return False
    deadline = time.monotonic() + 1.5
    while _posix_controller_matches(record) and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL_SECONDS)
    if _posix_controller_matches(record):
        log.warning("process_tree_persisted_posix_stop_timeout")
        return False
    _delete_owner_record(reference)
    return capture_marker in {
        _POSIX_ANCHOR_CAPTURED,
        _POSIX_ANCHOR_LEGACY_TERMINATED,
    }


def _terminate_persisted_windows_owner_sync(reference: _PersistedOwnerRef) -> bool:
    record = reference.record
    if not _controller_identity_matches(record):
        _delete_owner_record(reference)
        return False
    try:
        job = _WindowsJob.open(record.windows_job_name)
    except OSError:
        # A crashed Gateway closes the last kill-on-close handle, so the Job and
        # its controller normally disappear before recovery reaches this path.
        # A still-live matching controller without its named Job is not safe to
        # terminate by PID alone; retain the row for a later, exact retry.
        if not _controller_identity_matches(record):
            _delete_owner_record(reference)
        else:
            log.warning("process_tree_persisted_windows_job_unavailable")
        return False
    try:
        if not job.contains_pid(record.controller_pid):
            log.warning("process_tree_persisted_windows_job_mismatch")
            return False
        job.terminate()
        deadline = time.monotonic() + 1.0
        while job.active_process_count() > 0 and time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL_SECONDS)
        stopped = job.active_process_count() == 0
    except OSError:
        log.warning("process_tree_persisted_windows_terminate_failed")
        return False
    finally:
        job.close()
    if stopped:
        _delete_owner_record(reference)
    else:
        log.warning("process_tree_persisted_windows_stop_timeout")
    return stopped


async def _terminate_persisted_owner(reference: _PersistedOwnerRef) -> bool:
    record = reference.record
    if record.platform != _platform_kind():
        log.warning("process_tree_persisted_platform_mismatch")
        return False
    if record.platform in {"linux", "posix"}:
        return await asyncio.to_thread(_terminate_persisted_posix_owner_sync, reference)
    if record.platform == "windows":
        return await asyncio.to_thread(_terminate_persisted_windows_owner_sync, reference)
    return False


async def _terminate_owner_records(records: tuple[_PersistedOwnerRef, ...]) -> int:
    if not records:
        return 0
    async def terminate_claimed(reference: _PersistedOwnerRef) -> bool:
        claim = await asyncio.to_thread(_claim_owner_record, reference)
        if claim is None:
            return False
        try:
            return await _terminate_persisted_owner(reference)
        finally:
            await asyncio.to_thread(_release_owner_claim, claim)

    results = await asyncio.gather(
        *(terminate_claimed(record) for record in records),
        return_exceptions=True,
    )
    cancelled = 0
    for result in results:
        if result is True:
            cancelled += 1
        elif isinstance(result, BaseException):
            log.warning("process_tree_persisted_cleanup_failed")
    return cancelled


def _task_owned_records(
    records: tuple[_PersistedOwnerRef, ...],
    *,
    session_digest: str,
    task_digest: str,
) -> tuple[_PersistedOwnerRef, ...]:
    owned_tasks = {(session_digest, task_digest)}
    selected: dict[str, _PersistedOwnerRef] = {}
    changed = True
    while changed:
        changed = False
        for reference in records:
            record = reference.record
            exact_root = (
                record.session_digest == session_digest
                and record.task_digest == task_digest
            )
            descendant = (
                record.parent_session_digest,
                record.parent_task_digest,
            ) in owned_tasks
            if not exact_root and not descendant:
                continue
            if record.owner_id not in selected:
                selected[record.owner_id] = reference
                changed = True
            owned_identity = (record.session_digest, record.task_digest)
            if owned_identity not in owned_tasks:
                owned_tasks.add(owned_identity)
                changed = True
    return tuple(selected.values())


async def cancel_persisted_processes_for_task(
    state_dir: str | Path | None,
    session_key: str,
    task_id: str,
) -> int:
    """Terminate recovered process trees owned by one exact task lineage."""

    normalized_session = _normalize_owner_value(session_key)
    normalized_task = _normalize_owner_value(task_id)
    if state_dir is None or normalized_session is None or normalized_task is None:
        return 0
    records = await asyncio.to_thread(_load_owner_records, state_dir)
    owned = _task_owned_records(
        records,
        session_digest=_owner_digest("session", normalized_session),
        task_digest=_owner_digest("task", normalized_task),
    )
    return await _terminate_owner_records(owned)


async def cancel_persisted_processes_for_session(
    state_dir: str | Path | None,
    session_key: str,
) -> int:
    """Terminate recovered process trees owned directly by one session."""

    normalized_session = _normalize_owner_value(session_key)
    if state_dir is None or normalized_session is None:
        return 0
    session_digest = _owner_digest("session", normalized_session)
    records = await asyncio.to_thread(_load_owner_records, state_dir)
    selected = tuple(
        reference
        for reference in records
        if reference.record.session_digest == session_digest
    )
    return await _terminate_owner_records(selected)


async def reconcile_persisted_processes(state_dir: str | Path | None) -> int:
    """Clean exact task process owners left by an earlier Gateway lifecycle."""

    if state_dir is None:
        return 0
    records = await asyncio.to_thread(_load_owner_records, state_dir)
    return await _terminate_owner_records(records)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one fixed process-tree helper mode."""

    args = list(sys.argv[1:] if argv is None else argv)
    if (
        len(args) == 3
        and args[0] == "--posix-group-anchor"
        and _OWNER_ID_RE.fullmatch(args[1]) is not None
    ):
        return _run_posix_group_anchor(args[2])
    if len(args) >= 5 and args[0] == "--posix-owned-launch" and args[3] == "--":
        try:
            gate_fd = int(args[1])
            status_fd = int(args[2])
        except ValueError:
            return 2
        return _run_posix_owned_launch(gate_fd, status_fd, args[4:])
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
    "cancel_persisted_processes_for_session",
    "cancel_persisted_processes_for_task",
    "capture_process_tree_owner",
    "create_owned_popen",
    "create_owned_subprocess_exec",
    "create_owned_subprocess_shell",
    "main",
    "reconcile_persisted_processes",
    "task_process_scope",
]


if __name__ == "__main__":
    raise SystemExit(main())
