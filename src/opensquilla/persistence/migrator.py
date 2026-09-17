"""Schema migrator — thin wrapper over yoyo-migrations.

Each migration module owns its versioned up/down policy; gateway boot applies
pending migrations before code paths depend on the new schema.
"""

from __future__ import annotations

import builtins
import contextlib
import getpass
import glob
import logging
import os
import socket
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, unquote, urlparse

import structlog
from yoyo import exceptions, get_backend, read_migrations
from yoyo import migrations as yoyo_migrations

log = logging.getLogger(__name__)
#: Separate structured logger: migration-directory resolution emits an
#: operator-facing event whose fields are asserted by contract tests, while the
#: rest of this module logs through the standard library.
_structured_log = structlog.get_logger(__name__)

#: PROCESS_QUERY_LIMITED_INFORMATION — the minimal access right needed to
#: probe whether a Windows process exists.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: ERROR_ACCESS_DENIED — the probed process exists but belongs to another user.
_ERROR_ACCESS_DENIED = 5
#: STILL_ACTIVE — Windows process exit code while the process is still running.
_STILL_ACTIVE = 259
#: How many pre-migration snapshot files to keep per database.
_BACKUP_KEEP = 2
#: Username recorded in yoyo's audit log when the environment has none.
_FALLBACK_AUDIT_USER = "opensquilla"
#: Local-only hostname used when the operating system cannot report one.
_FALLBACK_AUDIT_HOST = "localhost"
# A schema rewrite can legitimately take longer than yoyo's ten-second
# database-row lock timeout on an existing profile. New binaries serialize
# before opening SQLite at all, while yoyo's lock remains the compatibility
# authority for older binaries that do not know this external lock.
_MIGRATION_PROCESS_LOCK_TIMEOUT_SECONDS = 120.0

# PR #1222 originally used V036-V039 before V036__session_model_routing landed
# on main. Development profiles may therefore already record the old ids even
# though the released migration chain must use unique V037-V040 prefixes. Each
# alias is accepted only when its exact historical hash matches; the replacement
# is then marked under yoyo's lock without replaying the already-applied schema.
_LEGACY_MIGRATION_ALIASES: dict[str, tuple[str, str]] = {
    "V036__artifact_sessions": (
        "V037__artifact_sessions",
        "629c36c68995c8ad03b3ede54698658fa4ca1885bc66bca257b2961d5e01df4e",
    ),
    "V037__artifact_prompt_annotations": (
        "V038__artifact_prompt_annotations",
        "adeb19ffbc8c3dd64921b8282daec79e8cbf875ef010dacf00a41cbcee637ce7",
    ),
    "V038__artifact_mutation_attempts": (
        "V039__artifact_mutation_attempts",
        "9775f8aa161c6172d3f0010a6016d2881894f7b0bc504c59ca79f3e67138b11c",
    ),
    "V039__document_resources": (
        "V040__document_resources",
        "d3c739ce2989470f11cd8a8d8aef811a1e12fa7ac6dbc4dfccd5ce7323762390",
    ),
}


class SchemaAheadError(RuntimeError):
    """The database records migrations the running code does not know about.

    Signals that the data was created or upgraded by a NEWER OpenSquilla build
    than the one now running — e.g. a desktop auto-update that was later rolled
    back to an older app. Booting would run old code against a newer schema (no
    yoyo down-migration is invoked at boot), so we refuse loudly instead of
    risking silent corruption.
    """


def resolve_migrations_dir() -> Path:
    """Locate yoyo migrations in env override, installed package, or checkout.

    Lives beside :func:`apply_pending` because every caller that applies
    migrations needs it, including offline profile consolidation.  Keeping it in
    the gateway would force lower layers to import the gateway back.
    """

    env_dir = os.environ.get("OPENSQUILLA_MIGRATIONS_DIR")
    if env_dir:
        candidate = Path(env_dir)
        if any(candidate.glob("V*.py")):
            return candidate
        # A pinned-but-unusable override silently falling through to a
        # different migration set is a misconfiguration operators must see.
        # Structured rather than this module's stdlib logger: the event name and
        # its ``path``/``reason`` fields are a pinned operator-facing contract.
        _structured_log.warning(
            "resolve_migrations_dir.env_override_ignored",
            path=str(candidate),
            reason=(
                "directory does not exist"
                if not candidate.is_dir()
                else "no V*.py migration files found"
            ),
        )

    try:
        from importlib import resources as importlib_resources

        package_dir = importlib_resources.files("opensquilla").joinpath("_migrations")
        if package_dir.is_dir():
            path = Path(str(package_dir))
            if any(path.glob("V*.py")):
                return path
    except Exception:
        pass

    repo_dir = Path(__file__).resolve().parents[3] / "migrations"
    if any(repo_dir.glob("V*.py")):
        return repo_dir

    raise RuntimeError(
        "opensquilla migrations directory not found "
        "(checked OPENSQUILLA_MIGRATIONS_DIR, opensquilla/_migrations, "
        "and repo migrations/)"
    )


def _adapt_sqlite_datetime(value: datetime) -> str:
    return value.isoformat(" ")


def _ensure_sqlite_datetime_adapter() -> None:
    """Register the Python 3.12 replacement for sqlite3's deprecated default."""

    sqlite3.register_adapter(datetime, _adapt_sqlite_datetime)


def _native_sqlite_path(path: str | Path) -> str:
    """Return a local SQLite spelling that bypasses legacy Windows MAX_PATH."""

    value = os.fspath(Path(path).expanduser())
    if os.name != "nt":
        return value
    absolute = os.path.abspath(value)
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return f"\\\\?\\UNC\\{absolute[2:]}"
    return f"\\\\?\\{absolute}"


def _sqlite_path(path: str | Path) -> Path:
    """Return one absolute local path, native on Windows and logical elsewhere."""

    if os.name == "nt":
        return Path(_native_sqlite_path(path))
    return Path(path).expanduser().resolve()


def _to_yoyo_url(db_url: str) -> str:
    """Normalise a local SQLite path or URL into a yoyo-compatible URL.

    Accepts: ``path/to.db``, ``:memory:``, or a pre-formed ``sqlite:///…`` URL.
    Returns a URL yoyo ``get_backend`` understands. Bare paths are
    percent-encoded because yoyo splits the URL with ``urlsplit`` (so a raw
    ``#`` truncates the path as a fragment and ``?`` as a query) and sqlite's
    URI parser percent-DECODES the result — without encoding, yoyo would
    silently migrate a different file than the one the inspection helpers
    (which use ``Path.as_uri()``) examine. ``:`` stays unescaped so Windows
    drive letters survive.
    """
    if "://" in db_url:
        if os.name != "nt":
            return db_url
        local_path = _sqlite_path_from_db_url(db_url)
        if local_path is None:
            return db_url
        parsed = urlparse(db_url)
        normalized = "sqlite:///" + quote(str(local_path), safe="/:")
        if parsed.query:
            normalized += f"?{parsed.query}"
        if parsed.fragment:
            normalized += f"#{parsed.fragment}"
        return normalized
    if db_url == ":memory:":
        return "sqlite:///:memory:"
    # bare filesystem path — normalise to absolute so yoyo opens the same db
    # regardless of the worker cwd.
    # On Windows always select the extended namespace. Gateway boot passes a
    # logical path, while consolidation may already pass ``\\?\``; both must
    # resolve to the same database without depending on LongPathsEnabled.
    normalized = _native_sqlite_path(db_url) if os.name == "nt" else _sqlite_path(db_url).as_posix()
    return "sqlite:///" + quote(normalized, safe="/:")


def _sqlite_read_only_uri(db_path: Path) -> str:
    value = str(db_path)
    if os.name == "nt" and value.startswith("\\\\?\\"):
        return f"file:{quote(value, safe='/:')}?mode=ro"
    return f"{db_path.as_uri()}?mode=ro"


def _sqlite_path_from_db_url(db_url: str) -> Path | None:
    """Return a local SQLite database path when direct lock inspection is safe."""

    if db_url == ":memory:":
        return None
    if "://" not in db_url:
        return _sqlite_path(db_url)

    parsed = urlparse(db_url)
    if parsed.scheme != "sqlite" or parsed.netloc:
        return None
    if parsed.path in {"", "/:memory:"}:
        return None

    path = unquote(parsed.path)
    if os.name == "nt" and path.startswith("/\\\\?\\"):
        path = path[1:]
    if os.name == "nt" and len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if os.name != "nt" and path.startswith("//") and not path.startswith("///"):
        path = path[1:]
    return _sqlite_path(path)


@contextlib.contextmanager
def _migration_process_lock(db_path: Path | None) -> Iterator[None]:
    """Serialize local SQLite migration callers before either opens the DB.

    Yoyo's lock row prevents two owners from applying a stale migration plan,
    but a waiter must open SQLite and repeatedly attempt writes in order to
    acquire that row. On Windows those attempts can block the owner's DDL even
    though the logical yoyo lock is working correctly. The external lock is
    keyed by the normalized database path and closes that pre-lock connection
    race. Non-local backends keep using yoyo's native lock only.
    """

    if db_path is None:
        yield
        return

    # Import lazily so non-SQLite migration discovery does not load the
    # platform-specific profile lock implementation.
    from opensquilla.profile_operation_lock import ProfileOperationLock

    with ProfileOperationLock(
        db_path,
        timeout=_MIGRATION_PROCESS_LOCK_TIMEOUT_SECONDS,
    ):
        yield


def _is_pid_alive_windows(pid: int, ctypes_module: Any = None) -> bool:
    """Return whether *pid* is a live process on Windows.

    ``use_last_error=True`` makes ctypes capture ``GetLastError()`` in
    thread-local storage immediately after the foreign call; reading it
    through a separate ``windll.kernel32.GetLastError()`` invocation can
    observe a value clobbered by intervening ctypes machinery, flipping the
    access-denied verdict in either direction (a wedged boot, or a live lock
    treated as stale).
    """
    if ctypes_module is None:
        import ctypes

        ctypes_module = ctypes
    kernel32 = ctypes_module.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes_module.c_void_p
    kernel32.OpenProcess.argtypes = (
        ctypes_module.c_uint32,
        ctypes_module.c_int,
        ctypes_module.c_uint32,
    )
    kernel32.CloseHandle.argtypes = (ctypes_module.c_void_p,)
    kernel32.CloseHandle.restype = ctypes_module.c_int
    kernel32.GetExitCodeProcess.argtypes = (
        ctypes_module.c_void_p,
        ctypes_module.c_void_p,
    )
    kernel32.GetExitCodeProcess.restype = ctypes_module.c_int
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    last_error = int(ctypes_module.get_last_error())
    if handle:
        exit_code = ctypes_module.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes_module.byref(exit_code)):
            kernel32.CloseHandle(handle)
            return True
        kernel32.CloseHandle(handle)
        return int(exit_code.value) == _STILL_ACTIVE
    # Access denied: the process exists but is owned by someone else.
    return last_error == _ERROR_ACCESS_DENIED


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _is_pid_alive_windows(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_yoyo_lock_pids(db_path: Path) -> list[int] | None:
    try:
        connection = sqlite3.connect(_sqlite_read_only_uri(db_path), uri=True)
    except sqlite3.Error as exc:
        log.warning(
            "migrator.lock_inspect_failed",
            extra={"db_path": str(db_path), "error": str(exc)},
        )
        return None

    try:
        rows = connection.execute("SELECT pid FROM yoyo_lock").fetchall()
    except sqlite3.Error as exc:
        log.warning(
            "migrator.lock_inspect_failed",
            extra={"db_path": str(db_path), "error": str(exc)},
        )
        return None
    finally:
        connection.close()

    pids: list[int] = []
    for (raw_pid,) in rows:
        try:
            pids.append(int(raw_pid))
        except (TypeError, ValueError):
            pids.append(0)
    return pids


def _clear_yoyo_lock(db_path: Path, dead_pids: list[int]) -> bool:
    """Delete only the lock rows owned by *dead_pids* — never a blanket delete.

    Between the pid-liveness read and this delete another process can release
    and reacquire the lock; an unscoped ``DELETE FROM yoyo_lock`` would then
    erase a live owner's lock and let two migrators run concurrently. Scoping
    the delete to verified-dead pids makes that impossible, and the caller
    retries regardless of how many rows were removed — the retry serializes on
    yoyo's own lock, so a lost race merely times out again.
    """
    if not dead_pids:
        return True
    placeholders = ",".join("?" for _ in dead_pids)
    try:
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                f"DELETE FROM yoyo_lock WHERE pid IN ({placeholders})",
                tuple(dead_pids),
            )
    except sqlite3.Error as exc:
        log.warning(
            "migrator.stale_lock_clear_failed",
            extra={"db_path": str(db_path), "error": str(exc)},
        )
        return False
    return True


def _recover_stale_yoyo_lock(db_url: str, error: exceptions.LockTimeout) -> bool:
    log.warning("migrator.lock_timeout", extra={"db_url": db_url, "error": str(error)})
    db_path = _sqlite_path_from_db_url(db_url)
    if db_path is None:
        return False

    pids = _read_yoyo_lock_pids(db_path)
    if pids is None:
        # Uninspectable database — nothing safe to do, let the timeout stand.
        return False
    if not pids:
        # The lock row was released between yoyo's timeout and this
        # inspection; the lock is free now, so a straight retry should
        # succeed.
        log.info(
            "migrator.lock_released_between_checks",
            extra={"db_path": str(db_path)},
        )
        return True

    live_pids = [pid for pid in pids if _is_pid_alive(pid)]
    if live_pids:
        log.warning(
            "migrator.lock_held_by_live_process",
            extra={"db_path": str(db_path), "pids": live_pids},
        )
        pid_text = ", ".join(str(pid) for pid in live_pids)
        raise exceptions.LockTimeout(
            f"Gateway migration database is locked by live process pid={pid_text} "
            f"at {db_path}. Stop the other OpenSquilla process and try again. If "
            "you are certain no other OpenSquilla instance is running, delete the "
            "lock row from the yoyo_lock table (or run 'yoyo break-lock' against "
            "this database) and restart the gateway."
        ) from error

    dead_pids = [pid for pid in pids if pid not in live_pids]
    if not _clear_yoyo_lock(db_path, dead_pids):
        return False
    log.warning(
        "migrator.stale_lock_cleared",
        extra={"db_path": str(db_path), "pids": dead_pids},
    )
    return True


@contextlib.contextmanager
def _yoyo_utf8_open() -> Iterator[None]:
    """Force yoyo's Migration.load() to read .py migrations as UTF-8.

    Why: yoyo's ``Migration.load`` calls ``open(self.path, "r")`` without an
    explicit encoding, so on Windows locales whose default codec is not UTF-8
    (e.g. zh-CN → GBK), any migration file containing non-ASCII docstrings
    (em-dashes, Chinese, etc.) raises UnicodeDecodeError at gateway boot.

    The shim is installed as a module global on ``yoyo.migrations`` — a module
    global shadows the builtin for code defined in that module only — so no
    other module or concurrent thread ever observes a patched ``open``.
    """
    real_open = builtins.open

    def utf8_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if "b" not in mode and "encoding" not in kwargs:
            kwargs["encoding"] = "utf-8"
        return real_open(file, mode, *args, **kwargs)

    yoyo_migrations.open = utf8_open
    try:
        yield
    finally:
        try:
            del yoyo_migrations.open
        except AttributeError:
            pass


def _has_migration_files(migrations_dir: Path) -> bool:
    try:
        entries = list(migrations_dir.iterdir())
    except OSError:
        return False
    return any(entry.name.startswith("V") and entry.suffix == ".py" for entry in entries)


def _discover_migrations(migrations_dir: Path) -> Any:
    """Read migrations with the directory path shielded from glob expansion.

    yoyo's ``read_migrations`` glob-expands its raw source strings, so a path
    containing ``[``, ``]``, ``*`` or ``?`` silently discovers ZERO migrations
    — boot would then proceed unmigrated, or the downgrade guard would raise a
    spurious :class:`SchemaAheadError`. Escape the path, and fail loudly if
    migration files are visibly present but discovery still returned nothing.
    """
    migrations = read_migrations(glob.escape(str(migrations_dir)))
    if not migrations and _has_migration_files(migrations_dir):
        raise RuntimeError(
            f"Migration discovery returned zero migrations for {migrations_dir} "
            "even though the directory contains V*.py migration files; refusing "
            "to proceed with an unmigrated schema."
        )
    return migrations


def _read_applied_migration_ids(db_path: Path) -> set[str] | None:
    """Return the migration ids recorded as applied in *db_path*.

    Returns an empty set when the yoyo ledger table does not exist yet (a fresh
    database), or ``None`` when the database cannot be inspected.
    """
    try:
        connection = sqlite3.connect(_sqlite_read_only_uri(db_path), uri=True)
    except sqlite3.Error as exc:
        log.warning(
            "migrator.applied_inspect_failed",
            extra={"db_path": str(db_path), "error": str(exc)},
        )
        return None
    try:
        try:
            table_rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE '%yoyo_migration'"
            ).fetchall()
        except sqlite3.Error as exc:
            log.warning(
                "migrator.applied_inspect_failed",
                extra={"db_path": str(db_path), "error": str(exc)},
            )
            return None
        table = next(
            (
                name
                for (name,) in table_rows
                if isinstance(name, str) and name.endswith("yoyo_migration")
            ),
            None,
        )
        if table is None:
            return set()
        try:
            rows = connection.execute(f'SELECT migration_id FROM "{table}"').fetchall()
        except sqlite3.OperationalError as exc:
            log.warning(
                "migrator.applied_inspect_failed",
                extra={"db_path": str(db_path), "error": str(exc)},
            )
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                # Transient contention — "uninspectable", tolerated by the
                # post-apply verifier rather than failing a healthy boot.
                return None
            # Structural: the table exists but its rows cannot be read (e.g.
            # a future yoyo release renaming migration_id). Report "readable
            # but records nothing" so the post-apply verifier fails closed
            # instead of running forever with a blind downgrade guard.
            return set()
    finally:
        connection.close()
    return {str(migration_id) for (migration_id,) in rows if migration_id}


def assert_schema_not_ahead(db_url: str, migrations_dir: Path) -> None:
    """Refuse to run when the database is ahead of the code's migration set.

    Forward migrations are applied automatically at boot; the reverse — running
    older code against a database a newer build already migrated — has no
    guardrail in yoyo, so we add one here. This is a no-op for fresh, in-memory,
    or non-inspectable databases; it only raises :class:`SchemaAheadError` on a
    genuine mismatch.
    """
    path = Path(migrations_dir)
    if not path.is_dir():
        return
    db_path = _sqlite_path_from_db_url(db_url)
    if db_path is None or not db_path.exists():
        return
    applied = _read_applied_migration_ids(db_path)
    if not applied:
        return
    with _yoyo_utf8_open():
        known = {migration.id for migration in _discover_migrations(path)}
    known.update(
        legacy_id
        for legacy_id, (replacement_id, _legacy_hash) in _LEGACY_MIGRATION_ALIASES.items()
        if replacement_id in known
    )
    unknown = sorted(applied - known)
    if not unknown:
        return
    log.error(
        "migrator.schema_ahead",
        extra={"db_path": str(db_path), "unknown_migrations": unknown},
    )
    raise SchemaAheadError(
        f"The OpenSquilla database at {db_path} was created by a newer version "
        f"and records {len(unknown)} migration(s) this build does not know about "
        f"({', '.join(unknown)}). Update OpenSquilla to a matching or newer "
        "version, or restore a backup taken with this version."
    )


def _ensure_yoyo_audit_user() -> None:
    """Guarantee yoyo's audit logging can resolve a username.

    yoyo calls ``getpass.getuser()`` between committing a migration's steps
    and marking the migration applied. Under a container UID with no passwd
    entry and none of LOGNAME/USER/LNAME/USERNAME set (``docker run --user
    12345``, K8s ``runAsUser``), that lookup raises and crashes every boot in
    the worst possible window. Pre-seed a stable fallback instead.
    """
    try:
        getpass.getuser()
    except (ImportError, KeyError, OSError):
        # getuser() ignores empty env values, so a plain setdefault is not
        # enough when e.g. LOGNAME is set but empty.
        if not os.environ.get("LOGNAME"):
            os.environ["LOGNAME"] = _FALLBACK_AUDIT_USER
        if not os.environ.get("USERNAME"):
            os.environ["USERNAME"] = _FALLBACK_AUDIT_USER
        log.info(
            "migrator.audit_user_fallback",
            extra={"user": _FALLBACK_AUDIT_USER},
        )


def _bind_local_yoyo_audit_identity(backend: Any) -> None:
    """Keep yoyo's migration audit complete without doing DNS during boot.

    Yoyo resolves ``socket.getfqdn()`` synchronously for every applied
    migration. A resolver or mDNS stall can therefore wedge an otherwise
    entirely local SQLite upgrade after its schema step has committed but
    before the ledger is marked. Bind the current backend instance to a
    process-local identity once, preserving every audit column and transaction
    while avoiding network name resolution. No global socket behavior changes.
    """

    username = getpass.getuser()
    try:
        hostname = socket.gethostname().strip() or _FALLBACK_AUDIT_HOST
    except OSError:
        hostname = _FALLBACK_AUDIT_HOST

    def local_log_data(migration: Any = None, operation: str = "apply") -> dict[str, Any]:
        if operation not in {"apply", "rollback", "mark", "unmark"}:
            raise ValueError(f"unsupported migration audit operation: {operation}")
        return {
            "id": str(uuid.uuid1()),
            "migration_id": migration.id if migration else None,
            "migration_hash": migration.hash if migration else None,
            "username": username,
            "hostname": hostname,
            "created_at_utc": datetime.now(UTC).replace(tzinfo=None),
            "operation": operation,
        }

    backend.get_log_data = local_log_data


def _restrict_db_file_permissions(db_path: Path | None) -> None:
    """Restrict the database file (and WAL siblings) to the owning user.

    Called only for databases this boot creates: the migrator is the first
    creator of the session database on fresh installs, and without this the
    default umask commonly leaves transcripts world-readable. Pre-existing
    databases are left untouched so deliberate operator permissions survive.
    POSIX only — Windows ACLs are out of scope here.
    """
    if db_path is None or os.name == "nt":
        return
    for candidate in (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ):
        try:
            if candidate.exists():
                os.chmod(candidate, 0o600)
        except OSError as exc:
            log.debug(
                "migrator.chmod_failed",
                extra={"path": str(candidate), "error": str(exc)},
            )


def _prune_old_backups(db_path: Path) -> None:
    """Keep only the newest pre-migration snapshots for *db_path*."""
    prefix = db_path.name + ".pre-"
    try:
        backups = [
            entry
            for entry in db_path.parent.iterdir()
            if entry.is_file() and entry.name.startswith(prefix) and entry.name.endswith(".bak")
        ]
        backups.sort(key=lambda entry: (entry.stat().st_mtime, entry.name))
        for stale in backups[:-_BACKUP_KEEP]:
            stale.unlink(missing_ok=True)
    except OSError as exc:
        log.debug(
            "migrator.backup_prune_failed",
            extra={"db_path": str(db_path), "error": str(exc)},
        )


def _snapshot_before_apply(db_path: Path, first_pending_id: str) -> None:
    """Snapshot *db_path* before applying migrations; failure never aborts boot.

    Several migrations in the chain recreate-and-copy whole tables, and yoyo
    commits each migration's steps before marking it applied, so a crash in
    that window can leave a half-migrated database. The downgrade guard also
    tells users to "restore a backup taken with this version" — this is what
    creates one. Uses the sqlite3 backup API so a consistent copy is taken
    even with the migration connection open.
    """
    backup_path = db_path.with_name(f"{db_path.name}.pre-{first_pending_id}.bak")
    try:
        source = sqlite3.connect(db_path)
        try:
            target = sqlite3.connect(backup_path)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except (sqlite3.Error, OSError) as exc:
        log.warning(
            "migrator.backup_failed",
            extra={
                "db_path": str(db_path),
                "backup_path": str(backup_path),
                "error": str(exc),
            },
        )
        return
    log.info(
        "migrator.backup_created",
        extra={"db_path": str(db_path), "backup_path": str(backup_path)},
    )
    # The snapshot exists from here on; permission tightening and rotation are
    # best-effort extras and must not relabel a good backup as failed or skip
    # pruning (filesystems that reject chmod would otherwise accumulate
    # full-size backups forever).
    if os.name != "nt":
        with contextlib.suppress(OSError):
            os.chmod(backup_path, 0o600)
    _prune_old_backups(db_path)


def _verify_ledger_after_apply(db_path: Path | None, applied_ids: list[str]) -> None:
    """Fail closed when the ledger no longer records what was just applied.

    :func:`_read_applied_migration_ids` locates yoyo's private
    ``_yoyo_migration`` table by name, so a future yoyo internal rename would
    make it return an empty set and silently disable the downgrade guard. If
    the ledger is readable but missing ids we know were just applied, raise
    instead of continuing with a blind guard. ``None`` (transient lock
    contention or an unopenable file) stays tolerated — that is an
    environment problem, not a schema-tracking one.
    """
    if db_path is None or not db_path.exists():
        return
    recorded = _read_applied_migration_ids(db_path)
    if recorded is None:
        return
    missing = [migration_id for migration_id in applied_ids if migration_id not in recorded]
    if missing:
        raise RuntimeError(
            f"Migrations {', '.join(missing)} were applied to {db_path} but the "
            "migration ledger does not record them — the ledger has become "
            "unreadable to this build (possibly a yoyo internal schema change). "
            "Refusing to continue with the downgrade guard disabled."
        )


def _mark_legacy_migration_aliases(backend: Any, migrations: Any) -> list[str]:
    """Atomically replace exact historical ids with their released equivalents.

    The old V036-V039 chain overlaps the released V036-V039 ids, so adding the
    replacement rows is impossible until every exact legacy row has first been
    removed. Both operations run in one transaction while the caller holds
    yoyo's migration lock; no other process can observe the temporary gap.
    """

    migration_by_id = {str(migration.id): migration for migration in migrations}
    relevant_aliases = {
        legacy_id: (replacement_id, legacy_hash)
        for legacy_id, (replacement_id, legacy_hash) in _LEGACY_MIGRATION_ALIASES.items()
        if replacement_id in migration_by_id
    }
    if not relevant_aliases:
        return []

    backend.ensure_internal_schema_updated()
    rows = backend.execute(
        f"SELECT migration_id, migration_hash FROM {backend.migration_table_quoted}"
    ).fetchall()
    recorded = {
        str(migration_id): str(migration_hash)
        for migration_id, migration_hash in rows
        if migration_id
    }

    legacy_rows: dict[str, tuple[str, str]] = {}
    for legacy_id, (replacement_id, expected_hash) in relevant_aliases.items():
        recorded_hash = recorded.get(legacy_id)
        if recorded_hash is None:
            continue
        if recorded_hash == expected_hash:
            legacy_rows[legacy_id] = (replacement_id, expected_hash)
            continue
        current = migration_by_id.get(legacy_id)
        if current is not None and recorded_hash == str(current.hash):
            continue
        if recorded_hash != expected_hash:
            raise SchemaAheadError(
                f"Migration {legacy_id} in this database does not match the exact "
                "historical OpenSquilla migration that was renamed; update from the "
                "matching build or restore a compatible backup."
            )

    if not legacy_rows:
        return []

    marked_ids: list[str] = []
    with backend.transaction():
        for legacy_id, (_replacement_id, legacy_hash) in reversed(legacy_rows.items()):
            backend.unmark_one(SimpleNamespace(id=legacy_id, hash=legacy_hash))
        for legacy_id, (replacement_id, _legacy_hash) in legacy_rows.items():
            replacement = migration_by_id[replacement_id]
            replacement_hash = str(replacement.hash)
            if (
                recorded.get(replacement_id) == replacement_hash
                and replacement_id not in legacy_rows
            ):
                continue
            backend.mark_one(replacement)
            marked_ids.append(replacement_id)

    marked = {
        str(migration_id): str(migration_hash)
        for migration_id, migration_hash in backend.execute(
            f"SELECT migration_id, migration_hash FROM {backend.migration_table_quoted}"
        ).fetchall()
        if migration_id
    }
    mismatched = [
        migration_id
        for migration_id in marked_ids
        if marked.get(migration_id) != str(migration_by_id[migration_id].hash)
    ]
    if mismatched:
        raise RuntimeError(
            "Migration alias registration did not persist: " + ", ".join(mismatched)
        )
    return marked_ids


def apply_pending(db_url: str, migrations_dir: Path) -> list[str]:
    """Apply every migration in *migrations_dir* not yet recorded in *db_url*.

    Returns the ordered list of migration ids that were applied in this call.
    If no migrations are pending, returns ``[]``. Callers running at boot
    should log the return value for audit.

    Raises :class:`SchemaAheadError` first if the database records migrations
    unknown to this build (i.e. a downgrade onto newer data). When migrations
    are pending against an existing local database file, a pre-apply snapshot
    (``<dbname>.pre-<first_pending_id>.bak``) is written next to it before any
    schema change runs; snapshot failures are logged but never abort boot.
    """
    path = Path(migrations_dir)
    if not path.is_dir():
        log.warning("migrator.missing_dir", extra={"migrations_dir": str(path)})
        return []

    db_path = _sqlite_path_from_db_url(db_url)
    with _migration_process_lock(db_path):
        # The downgrade check and pre-existing-file decision belong inside the
        # same cross-process boundary as backend creation. Otherwise a second
        # caller can inspect a schema while the lock owner is rewriting it.
        assert_schema_not_ahead(db_url, path)

        _ensure_sqlite_datetime_adapter()
        _ensure_yoyo_audit_user()

        db_preexisted = db_path is not None and db_path.exists()
        backup_db_path = db_path if db_preexisted else None
        # Only tighten permissions on files this boot creates: pre-existing
        # databases may carry deliberate operator permissions (group readers,
        # split-user setups) that silently reverting every boot would break.
        tighten_new_db = db_path is not None and not db_preexisted

        try:
            ids = _apply_pending_once(
                db_url, path, backup_db_path=backup_db_path, tighten_new_db=tighten_new_db
            )
        except exceptions.LockTimeout as exc:
            if not _recover_stale_yoyo_lock(db_url, exc):
                raise
            try:
                ids = _apply_pending_once(
                    db_url,
                    path,
                    backup_db_path=backup_db_path,
                    tighten_new_db=tighten_new_db,
                )
            except exceptions.LockTimeout:
                log.warning("migrator.stale_lock_retry_failed", extra={"db_url": db_url})
                raise

        if ids:
            log.info("migrator.applied", extra={"count": len(ids), "ids": ids})
            _verify_ledger_after_apply(db_path, ids)
        return ids


def _apply_pending_once(
    db_url: str,
    migrations_dir: Path,
    *,
    backup_db_path: Path | None = None,
    tighten_new_db: bool = False,
) -> list[str]:
    log.debug("migrator.backend_open_started")
    backend = get_backend(_to_yoyo_url(db_url))
    _bind_local_yoyo_audit_identity(backend)
    log.debug("migrator.backend_open_ready")
    try:
        if tighten_new_db:
            _restrict_db_file_permissions(_sqlite_path_from_db_url(db_url))
        with _yoyo_utf8_open():
            log.debug("migrator.discovery_started")
            migrations = _discover_migrations(migrations_dir)
            log.debug("migrator.discovery_ready", extra={"count": len(migrations)})
            # The pending plan MUST be computed inside yoyo's lock: yoyo's
            # apply path never re-checks appliedness, so a plan computed
            # before the lock can re-apply migrations a concurrent process
            # just finished — a ledger IntegrityError at best, silently
            # re-running recreate-and-copy migrations (dropping later-added
            # columns) at worst.
            log.debug("migrator.lock_wait_started")
            with backend.lock():
                log.debug("migrator.lock_acquired")
                marked_aliases = _mark_legacy_migration_aliases(backend, migrations)
                if marked_aliases:
                    log.info(
                        "migrator.aliases_marked",
                        extra={"ids": marked_aliases},
                    )
                pending = backend.to_apply(migrations)
                ids = [m.id for m in pending]
                if not ids:
                    log.debug("migrator.plan_ready", extra={"count": 0})
                    return []
                log.debug("migrator.plan_ready", extra={"count": len(ids)})
                if backup_db_path is not None:
                    log.debug("migrator.snapshot_started")
                    _snapshot_before_apply(backup_db_path, ids[0])
                    log.debug("migrator.snapshot_ready")
                log.debug("migrator.apply_started", extra={"count": len(ids)})
                backend.apply_migrations(pending)
                log.debug("migrator.apply_ready", extra={"count": len(ids)})
        return ids
    finally:
        # yoyo backends expose no close(); the connection object is the only
        # cleanup surface. Leaking it would pin the (shared-cache) sqlite
        # connection for the process lifetime.
        try:
            backend.connection.close()
        except sqlite3.Error as exc:
            log.warning(
                "migrator.backend_close_failed",
                extra={"db_url": db_url, "error": str(exc)},
            )
