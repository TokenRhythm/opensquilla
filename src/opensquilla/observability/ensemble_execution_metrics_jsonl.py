"""Opt-in, bounded JSONL transport for ensemble execution metrics.

This is a local structured transport, not a metrics backend.  It intentionally
does not expose an HTTP endpoint, install a collector, or provision dashboard
or alerting rules.  The sink is disabled by default and all of its failures are
fail-open for the agent turn.

The writer targets POSIX deployments because its cross-process exclusion and
path hardening rely on ``flock(2)``, ``openat(2)``, and ``O_NOFOLLOW``.  An
enabled sink on another platform fails closed for the sink and leaves the turn
untouched.
"""

from __future__ import annotations

import atexit
import json
import os
import stat
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from opensquilla.observability.ensemble_execution_metrics_contract import (
    ENSEMBLE_EXECUTION_METRICS_EVENT,
    ENSEMBLE_EXECUTION_METRICS_JSONL_SCHEMA,
    validate_ensemble_execution_metrics,
)
from opensquilla.paths import default_opensquilla_home

try:  # pragma: no cover - exercised by the explicit unsupported-platform test
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

log = structlog.get_logger(__name__)

ENSEMBLE_METRICS_JSONL_ENV = "OPENSQUILLA_ENSEMBLE_METRICS_JSONL"
ENSEMBLE_METRICS_JSONL_DIR_ENV = "OPENSQUILLA_ENSEMBLE_METRICS_JSONL_DIR"
LOG_DIR_ENV = "OPENSQUILLA_LOG_DIR"
ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})

METRICS_DIRECTORY_NAME = "ensemble-metrics"
METRICS_FILE_NAME = "ensemble-execution-metrics-v1.jsonl"
LOCK_FILE_NAME = ".ensemble-execution-metrics-v1.lock"
MAX_FILE_BYTES = 5_000_000
BACKUP_COUNT = 3
MAX_ROW_BYTES = 65_536

_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700
_IDENTITY_FIELDS = ("st_dev", "st_ino")


class EnsembleMetricsJSONLTransportError(RuntimeError):
    """The optional local transport could not uphold its security contract."""


def ensemble_metrics_jsonl_enabled() -> bool:
    """Return whether the local JSONL transport was explicitly enabled."""

    return os.environ.get(ENSEMBLE_METRICS_JSONL_ENV, "").strip().lower() in ENABLED_VALUES


def resolve_ensemble_metrics_jsonl_directory() -> Path:
    """Resolve the dedicated owner-only directory without creating it."""

    explicit = os.environ.get(ENSEMBLE_METRICS_JSONL_DIR_ENV, "").strip()
    if explicit:
        raw = explicit
    else:
        log_dir = os.environ.get(LOG_DIR_ENV, "").strip()
        base = Path(log_dir) if log_dir else default_opensquilla_home() / "logs"
        raw = str(base / METRICS_DIRECTORY_NAME)
    return Path(os.path.abspath(os.path.expanduser(raw)))


def _identity(value: os.stat_result) -> tuple[int, int]:
    return tuple(int(getattr(value, field)) for field in _IDENTITY_FIELDS)  # type: ignore[return-value]


def _require_posix_primitives() -> None:
    if (
        os.name != "posix"
        or fcntl is None
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "geteuid")
    ):
        raise EnsembleMetricsJSONLTransportError(
            "secure ensemble metrics JSONL transport requires POSIX flock/O_NOFOLLOW/O_DIRECTORY"
        )


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | int(getattr(os, "O_DIRECTORY", 0))
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def _open_existing_directory_without_symlinks(path: Path) -> int:
    """Open an absolute directory one component at a time with O_NOFOLLOW."""

    _require_posix_primitives()
    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute():  # defensive; abspath should make this true
        raise EnsembleMetricsJSONLTransportError("ensemble metrics directory must be absolute")
    flags = _directory_open_flags()
    current_fd = os.open(os.path.sep, flags)
    try:
        for component in absolute.parts[1:]:
            if component in {"", ".", ".."}:
                raise EnsembleMetricsJSONLTransportError(
                    "unsafe ensemble metrics directory component"
                )
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _validate_parent_directory(fd: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise EnsembleMetricsJSONLTransportError("ensemble metrics parent is not a directory")
    if info.st_uid != os.geteuid():
        raise EnsembleMetricsJSONLTransportError(
            "ensemble metrics parent must be owned by the current uid"
        )
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise EnsembleMetricsJSONLTransportError(
            "ensemble metrics parent must not be group/world writable"
        )


def _validate_private_directory(fd: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise EnsembleMetricsJSONLTransportError("ensemble metrics path is not a directory")
    if info.st_uid != os.geteuid():
        raise EnsembleMetricsJSONLTransportError(
            "ensemble metrics directory must be owned by the current uid"
        )
    if stat.S_IMODE(info.st_mode) != _DIRECTORY_MODE:
        raise EnsembleMetricsJSONLTransportError("ensemble metrics directory mode must be 0700")
    return info


def _open_or_create_private_directory(path: Path) -> int:
    if path.parent == path or path.name in {"", ".", ".."}:
        raise EnsembleMetricsJSONLTransportError("refusing broad ensemble metrics directory")
    parent_fd = _open_existing_directory_without_symlinks(path.parent)
    try:
        _validate_parent_directory(parent_fd)
        created = False
        try:
            os.mkdir(path.name, _DIRECTORY_MODE, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        directory_fd = os.open(
            path.name,
            _directory_open_flags(),
            dir_fd=parent_fd,
        )
        try:
            if created:
                os.fchmod(directory_fd, _DIRECTORY_MODE)
            _validate_private_directory(directory_fd)
            return directory_fd
        except BaseException:
            os.close(directory_fd)
            raise
    finally:
        os.close(parent_fd)


def _validate_regular_file(info: os.stat_result, *, name: str) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise EnsembleMetricsJSONLTransportError(f"{name} is not a regular file")
    if info.st_uid != os.geteuid():
        raise EnsembleMetricsJSONLTransportError(f"{name} must be owned by the current uid")
    if stat.S_IMODE(info.st_mode) != _FILE_MODE:
        raise EnsembleMetricsJSONLTransportError(f"{name} mode must be 0600")
    if info.st_nlink != 1:
        raise EnsembleMetricsJSONLTransportError(f"{name} must have exactly one hard link")


def build_ensemble_metrics_jsonl_row(
    metrics: Mapping[str, Any],
    *,
    emitted_at: datetime | None = None,
    max_row_bytes: int = MAX_ROW_BYTES,
) -> bytes:
    """Build one validated, flat JSON row with a versioned transport envelope."""

    validate_ensemble_execution_metrics(metrics)
    timestamp = emitted_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("emitted_at must be timezone-aware")
    emitted_at_utc = (
        timestamp.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    envelope: dict[str, Any] = {
        "transport_schema": ENSEMBLE_EXECUTION_METRICS_JSONL_SCHEMA,
        "event": ENSEMBLE_EXECUTION_METRICS_EVENT,
        "emitted_at": emitted_at_utc,
        **metrics,
    }
    encoded = (
        json.dumps(
            envelope,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    if max_row_bytes <= 0 or len(encoded) > max_row_bytes:
        raise EnsembleMetricsJSONLTransportError(
            "ensemble metrics JSONL row exceeds the hard byte cap"
        )
    if encoded.count(b"\n") != 1 or not encoded.endswith(b"\n"):
        raise EnsembleMetricsJSONLTransportError(
            "ensemble metrics transport must emit exactly one JSON line"
        )
    return encoded


class _SecureRotatingJSONLWriter:
    """Single-row writer with owner-only openat paths and flock rotation."""

    def __init__(
        self,
        directory: Path,
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        backup_count: int = BACKUP_COUNT,
        max_row_bytes: int = MAX_ROW_BYTES,
    ) -> None:
        if max_file_bytes <= 0 or backup_count < 1 or max_row_bytes <= 0:
            raise ValueError("invalid ensemble metrics rotation bounds")
        self.directory = Path(os.path.abspath(directory))
        self.max_file_bytes = max_file_bytes
        self.backup_count = backup_count
        self.max_row_bytes = max_row_bytes
        self._thread_lock = threading.Lock()
        self._closed = False
        self._active_data_descriptor: tuple[int, tuple[int, int]] | None = None
        self._directory_fd = _open_or_create_private_directory(self.directory)
        self._directory_identity = _identity(_validate_private_directory(self._directory_fd))
        try:
            self._lock_fd = self._open_secure_file(
                LOCK_FILE_NAME,
                access_flags=os.O_RDWR | os.O_APPEND,
                create=True,
            )
            self._lock_identity = _identity(os.fstat(self._lock_fd))
        except BaseException:
            os.close(self._directory_fd)
            raise

    @property
    def path(self) -> Path:
        return self.directory / METRICS_FILE_NAME

    def _open_secure_file(
        self,
        name: str,
        *,
        access_flags: int,
        create: bool,
        exclusive: bool = False,
    ) -> int:
        base_flags = (
            access_flags | int(getattr(os, "O_NOFOLLOW", 0)) | int(getattr(os, "O_CLOEXEC", 0))
        )
        created = False
        if create:
            try:
                fd = os.open(
                    name,
                    base_flags | os.O_CREAT | os.O_EXCL,
                    _FILE_MODE,
                    dir_fd=self._directory_fd,
                )
                created = True
            except FileExistsError:
                if exclusive:
                    raise EnsembleMetricsJSONLTransportError(
                        f"unexpected existing transport file: {name}"
                    ) from None
                fd = os.open(name, base_flags, dir_fd=self._directory_fd)
        else:
            fd = os.open(name, base_flags, dir_fd=self._directory_fd)
        try:
            if created:
                os.fchmod(fd, _FILE_MODE)
            descriptor_info = os.fstat(fd)
            _validate_regular_file(descriptor_info, name=name)
            path_info = os.stat(
                name,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
            _validate_regular_file(path_info, name=name)
            if _identity(descriptor_info) != _identity(path_info):
                raise EnsembleMetricsJSONLTransportError(
                    f"{name} path/descriptor identity mismatch"
                )
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _validate_named_file(self, name: str) -> os.stat_result | None:
        try:
            info = os.stat(
                name,
                dir_fd=self._directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        _validate_regular_file(info, name=name)
        return info

    def _track_active_data_fd(self, fd: int) -> None:
        if self._active_data_descriptor is not None:
            raise EnsembleMetricsJSONLTransportError(
                "ensemble metrics writer already tracks an active data fd"
            )
        info = os.fstat(fd)
        _validate_regular_file(info, name=METRICS_FILE_NAME)
        # Keep fd and inode in one assignment so the fork hook cannot observe
        # a torn descriptor/identity pair between Python bytecodes.
        self._active_data_descriptor = (fd, _identity(info))

    def _open_tracked_data_file(self, *, exclusive: bool = False) -> int:
        fd = self._open_secure_file(
            METRICS_FILE_NAME,
            access_flags=os.O_WRONLY | os.O_APPEND,
            create=True,
            exclusive=exclusive,
        )
        try:
            self._track_active_data_fd(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _close_active_data_fd(self, fd: int) -> None:
        tracked = self._active_data_descriptor
        if tracked is None or tracked[0] != fd:
            raise EnsembleMetricsJSONLTransportError(
                "ensemble metrics active data fd tracking mismatch"
            )
        # Close first and only then clear the identity.  A fork between these
        # operations is safe because the child revalidates fd/inode identity.
        os.close(fd)
        if self._active_data_descriptor == tracked:
            self._active_data_descriptor = None

    def _revalidate_directory_and_lock(self) -> None:
        if self._closed:
            raise EnsembleMetricsJSONLTransportError("ensemble metrics JSONL writer is closed")
        directory_info = _validate_private_directory(self._directory_fd)
        if _identity(directory_info) != self._directory_identity:
            raise EnsembleMetricsJSONLTransportError(
                "ensemble metrics directory descriptor changed identity"
            )
        fresh_fd = _open_existing_directory_without_symlinks(self.directory)
        try:
            fresh_info = _validate_private_directory(fresh_fd)
            if _identity(fresh_info) != self._directory_identity:
                raise EnsembleMetricsJSONLTransportError(
                    "ensemble metrics directory path changed identity"
                )
        finally:
            os.close(fresh_fd)

        descriptor_info = os.fstat(self._lock_fd)
        _validate_regular_file(descriptor_info, name=LOCK_FILE_NAME)
        path_info = self._validate_named_file(LOCK_FILE_NAME)
        if (
            path_info is None
            or _identity(descriptor_info) != self._lock_identity
            or _identity(path_info) != self._lock_identity
        ):
            raise EnsembleMetricsJSONLTransportError("ensemble metrics lock path changed identity")

    def _rotate(self) -> None:
        oldest = f"{METRICS_FILE_NAME}.{self.backup_count}"
        if self._validate_named_file(oldest) is not None:
            os.unlink(oldest, dir_fd=self._directory_fd)
        for index in range(self.backup_count - 1, 0, -1):
            source = f"{METRICS_FILE_NAME}.{index}"
            destination = f"{METRICS_FILE_NAME}.{index + 1}"
            if self._validate_named_file(source) is None:
                continue
            if self._validate_named_file(destination) is not None:
                raise EnsembleMetricsJSONLTransportError(
                    f"unexpected occupied rotation destination: {destination}"
                )
            os.replace(
                source,
                destination,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )
        if self._validate_named_file(METRICS_FILE_NAME) is not None:
            destination = f"{METRICS_FILE_NAME}.1"
            if self._validate_named_file(destination) is not None:
                raise EnsembleMetricsJSONLTransportError(
                    f"unexpected occupied rotation destination: {destination}"
                )
            os.replace(
                METRICS_FILE_NAME,
                destination,
                src_dir_fd=self._directory_fd,
                dst_dir_fd=self._directory_fd,
            )

    def write_row(self, row: bytes) -> None:
        """Append one bounded row under a process and cross-process lock."""

        if type(row) is not bytes or not row.endswith(b"\n"):
            raise EnsembleMetricsJSONLTransportError(
                "ensemble metrics row must be newline-terminated bytes"
            )
        if len(row) > self.max_row_bytes or row.count(b"\n") != 1:
            raise EnsembleMetricsJSONLTransportError(
                "ensemble metrics row violates the hard line cap"
            )
        # ``os.open`` makes a descriptor process-visible before Python can
        # record it in ``_active_data_descriptor``.  Holding the module fork
        # guard across the entire open/write/close interval lets the at-fork
        # ``before`` callback wait until there is no untracked transient fd.
        with _fork_guard, self._thread_lock:
            self._revalidate_directory_and_lock()
            assert fcntl is not None
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                self._revalidate_directory_and_lock()
                data_fd = self._open_tracked_data_file()
                try:
                    before = os.fstat(data_fd)
                    if before.st_size and (before.st_size + len(row) > self.max_file_bytes):
                        self._close_active_data_fd(data_fd)
                        data_fd = -1
                        self._rotate()
                        data_fd = self._open_tracked_data_file(exclusive=True)
                        before = os.fstat(data_fd)
                    original_size = before.st_size
                    try:
                        written = os.write(data_fd, row)
                    except BaseException:
                        os.ftruncate(data_fd, original_size)
                        raise
                    if written != len(row):
                        os.ftruncate(data_fd, original_size)
                        raise EnsembleMetricsJSONLTransportError(
                            "short write to ensemble metrics JSONL"
                        )
                    after = os.fstat(data_fd)
                    _validate_regular_file(after, name=METRICS_FILE_NAME)
                    path_info = self._validate_named_file(METRICS_FILE_NAME)
                    if (
                        path_info is None
                        or _identity(after) != _identity(path_info)
                        or after.st_size != original_size + len(row)
                    ):
                        raise EnsembleMetricsJSONLTransportError(
                            "ensemble metrics data path changed during write"
                        )
                finally:
                    if data_fd >= 0:
                        self._close_active_data_fd(data_fd)
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _close_with_fork_guard_held(self) -> None:
        """Close persistent descriptors while the caller excludes fork."""

        with self._thread_lock:
            if self._closed:
                return
            self._closed = True
            os.close(self._lock_fd)
            os.close(self._directory_fd)

    def close(self) -> None:
        with _fork_guard:
            self._close_with_fork_guard_held()

    def _discard_inherited_fds_after_fork(self) -> None:
        """Close child-only fd copies without touching inherited locks."""

        # Only the thread that called fork survives in the child.  Either of
        # these locks may have been held by a vanished parent thread, so this
        # child-only path must not call ``close()`` or acquire either lock.
        lock_fd = self._lock_fd
        directory_fd = self._directory_fd
        active_data_descriptor = self._active_data_descriptor
        self._lock_fd = -1
        self._directory_fd = -1
        self._active_data_descriptor = None
        self._closed = True
        self._thread_lock = threading.Lock()
        inherited_descriptors: tuple[tuple[int, tuple[int, int]], ...] = (
            (lock_fd, self._lock_identity),
            (directory_fd, self._directory_identity),
        )
        if active_data_descriptor is not None:
            inherited_descriptors += (active_data_descriptor,)
        for fd, expected_identity in inherited_descriptors:
            if type(fd) is not int or fd < 0:
                continue
            try:
                if _identity(os.fstat(fd)) != expected_identity:
                    # A concurrent parent close may have detached this number
                    # before fork and another subsystem may now own it.
                    continue
                os.close(fd)
            except OSError:
                # Another inherited cleanup path may already have closed it.
                pass


_fork_guard = threading.Lock()
_writer_guard = threading.Lock()
_writer: _SecureRotatingJSONLWriter | None = None
_writer_key: tuple[int, str] | None = None


def _acquire_fork_guard_before_fork() -> None:
    """Wait until no writer owns an untracked or active data descriptor."""

    _fork_guard.acquire()


def _release_fork_guard_after_fork_in_parent() -> None:
    _fork_guard.release()


def _reset_writer_after_fork_in_child() -> None:
    """Drop inherited singleton state without acquiring parent-held locks."""

    global _fork_guard, _writer_guard, _writer, _writer_key
    inherited_writer = _writer
    # The parent-side before callback owns the inherited fork guard, while
    # either of the other locks may be owned by a vanished parent thread.
    # Replace all guards before touching the inherited writer; never acquire
    # or release a parent lock in this child-only callback.
    _fork_guard = threading.Lock()
    _writer_guard = threading.Lock()
    _writer = None
    _writer_key = None
    if inherited_writer is not None:
        inherited_writer._discard_inherited_fds_after_fork()


def _get_writer() -> _SecureRotatingJSONLWriter:
    global _writer, _writer_key
    directory = resolve_ensemble_metrics_jsonl_directory()
    key = (os.getpid(), str(directory))
    with _fork_guard, _writer_guard:
        if _writer is not None and _writer_key == key:
            return _writer
        if _writer is not None:
            _writer._close_with_fork_guard_held()
        _writer = _SecureRotatingJSONLWriter(directory)
        _writer_key = key
        return _writer


def _close_writer() -> None:
    global _writer, _writer_key
    with _fork_guard, _writer_guard:
        if _writer is not None:
            try:
                _writer._close_with_fork_guard_held()
            except Exception:
                pass
        _writer = None
        _writer_key = None


def _reset_ensemble_metrics_jsonl_writer_for_tests() -> None:
    """Close process globals; private test seam, never part of runtime API."""

    _close_writer()


def write_ensemble_execution_metrics_jsonl(
    metrics: Mapping[str, Any],
) -> Path | None:
    """Best-effort append of one reviewed row; return its path on success."""

    if not ensemble_metrics_jsonl_enabled():
        return None
    try:
        writer = _get_writer()
        row = build_ensemble_metrics_jsonl_row(
            metrics,
            max_row_bytes=writer.max_row_bytes,
        )
        writer.write_row(row)
        return writer.path
    except Exception as exc:  # noqa: BLE001 - telemetry must never break a turn
        try:
            log.debug(
                "llm_ensemble.execution.metrics_jsonl_failed",
                error_type=type(exc).__name__,
            )
        except Exception:  # noqa: BLE001 - a broken logger is also fail-open
            pass
        return None


atexit.register(_close_writer)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_acquire_fork_guard_before_fork,
        after_in_parent=_release_fork_guard_after_fork_in_parent,
        after_in_child=_reset_writer_after_fork_in_child,
    )
