from __future__ import annotations

import builtins
import contextlib
import getpass
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import warnings
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from yoyo import exceptions
from yoyo import migrations as yoyo_migrations

from opensquilla.persistence import migrator
from opensquilla.persistence.migrator import apply_pending
from opensquilla.session.storage import SessionStorage

_REPO_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@pytest.mark.asyncio
async def test_meta_control_migration_chain_rolls_back_without_ordinary_data_loss(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    await storage.close()

    tail_ids = (
        "V030__meta_control_intents",
        "V031__meta_launch_drafts",
        "V032__meta_launch_discard_tombstones",
    )
    tail_tables = (
        "meta_control_intents",
        "meta_launch_drafts",
        "meta_launch_discard_tombstones",
    )
    meta_objects = {
        *tail_tables,
        "uq_meta_control_intents_correlation",
        "idx_meta_control_intents_session_status",
        "uq_meta_launch_drafts_request",
        "idx_meta_launch_drafts_session_expiry",
        "idx_meta_launch_discard_tombstones_expiry",
    }
    with sqlite3.connect(db_path) as connection:
        for table in reversed(tail_tables):
            connection.execute(f"DROP TABLE IF EXISTS {table}")

    backend = migrator.get_backend(migrator._to_yoyo_url(str(db_path)))
    try:
        migrations = migrator.read_migrations(str(_REPO_MIGRATIONS_DIR))
        by_id = {migration.id: migration for migration in migrations}
        migration_list_type = type(migrations)
        base = migration_list_type(
            [migration for migration in migrations if migration.id < tail_ids[0]],
            migrations.post_apply,
        )
        with backend.lock():
            backend.apply_migrations(backend.to_apply(base))

        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "INSERT INTO sessions (session_key, session_id, created_at, updated_at) "
                "VALUES ('agent:main:webchat:rollback-sentinel', 'session-sentinel', 1, 1)"
            )
            connection.execute(
                "INSERT INTO transcript_entries ("
                "session_id, session_key, message_id, role, content, created_at"
                ") VALUES ("
                "'session-sentinel', 'agent:main:webchat:rollback-sentinel', "
                "'message-sentinel', 'user', 'ordinary data survives', 1"
                ")"
            )

        tail = migration_list_type(
            [by_id[item] for item in tail_ids],
            migrations.post_apply,
        )
        with backend.lock():
            backend.apply_migrations(backend.to_apply(tail))

        with sqlite3.connect(db_path) as connection:
            present = {
                name
                for (name,) in connection.execute(
                    "SELECT name FROM sqlite_master WHERE name IN ("
                    + ",".join("?" for _item in meta_objects)
                    + ")",
                    tuple(meta_objects),
                )
            }
        assert present == meta_objects

        with backend.lock():
            backend.rollback_migrations([by_id[item] for item in reversed(tail_ids)])
    finally:
        backend.connection.close()

    with sqlite3.connect(db_path) as connection:
        ordinary_session = connection.execute(
            "SELECT session_id FROM sessions WHERE session_key = ?",
            ("agent:main:webchat:rollback-sentinel",),
        ).fetchone()
        ordinary_transcript = connection.execute(
            "SELECT content FROM transcript_entries WHERE message_id = ?",
            ("message-sentinel",),
        ).fetchone()
        remaining_meta_objects = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE name IN ("
                + ",".join("?" for _item in meta_objects)
                + ")",
                tuple(meta_objects),
            )
        }
    assert ordinary_session == ("session-sentinel",)
    assert ordinary_transcript == ("ordinary data survives",)
    assert remaining_meta_objects == set()

    pre_v025_migrations = tmp_path / "pre-v025-migrations"
    pre_v025_migrations.mkdir()
    for migration in base:
        (pre_v025_migrations / f"{migration.id}.py").write_text(
            "from yoyo import step\n"
            "__depends__ = set()\n"
            "steps = [step('SELECT 1')]\n",
            encoding="utf-8",
        )
    migrator.assert_schema_not_ahead(
        migrator._to_yoyo_url(str(db_path)),
        pre_v025_migrations,
    )


def test_apply_pending_registers_python312_datetime_adapter(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "V001__demo.py").write_text(
        "from yoyo import step\n"
        "__depends__ = set()\n"
        "steps = [step('CREATE TABLE demo (id INTEGER PRIMARY KEY)')]\n",
        encoding="utf-8",
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        applied = apply_pending(str(tmp_path / "demo.sqlite"), migrations_dir)

    assert applied == ["V001__demo"]


def test_apply_pending_forces_utf8_when_yoyo_loads_python_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real migration with non-ASCII text loads even under a legacy locale."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "V001__utf8.py").write_text(
        '"""Migration docstring with non-ASCII text — 界."""\n'
        "from yoyo import step\n"
        "__depends__ = set()\n"
        "steps = [step('CREATE TABLE utf8_demo (id INTEGER PRIMARY KEY)')]\n",
        encoding="utf-8",
    )

    real_open = builtins.open
    encodings: list[object] = []

    def legacy_locale_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if "b" not in mode and "encoding" not in kwargs:
            raise UnicodeDecodeError("gbk", b"\x80", 0, 1, "fake legacy locale")
        encodings.append(kwargs.get("encoding"))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", legacy_locale_open)

    applied = apply_pending(str(tmp_path / "demo.sqlite"), migrations_dir)

    assert applied == ["V001__utf8"]
    assert "utf-8" in encodings
    # The shim is removed from yoyo's module namespace afterwards.
    assert not hasattr(yoyo_migrations, "open")


def test_yoyo_utf8_open_patches_only_the_yoyo_module() -> None:
    before = builtins.open
    assert not hasattr(yoyo_migrations, "open")

    with migrator._yoyo_utf8_open():
        # Process-wide builtins are never touched; only yoyo.migrations sees
        # the UTF-8 shim as a module global shadowing the builtin.
        assert builtins.open is before
        assert yoyo_migrations.open is not before

    assert builtins.open is before
    assert not hasattr(yoyo_migrations, "open")


def _create_yoyo_lock_table(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE yoyo_lock (locked INTEGER PRIMARY KEY, ctime TIMESTAMP, pid INTEGER)"
        )


def _create_yoyo_lock(db_path: Path, pid: int) -> None:
    _create_yoyo_lock_table(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO yoyo_lock (locked, ctime, pid) VALUES (1, CURRENT_TIMESTAMP, ?)",
            (pid,),
        )


def _yoyo_lock_pids(db_path: Path) -> list[int]:
    with sqlite3.connect(db_path) as connection:
        return [row[0] for row in connection.execute("SELECT pid FROM yoyo_lock")]


class _RaisingLock:
    def __init__(self, message: str = "Process 424242 has locked this database") -> None:
        self._message = message

    def __enter__(self) -> None:
        raise exceptions.LockTimeout(self._message)

    def __exit__(self, *_args) -> None:  # type: ignore[no-untyped-def]
        return None


class _FakeConnection:
    """Match yoyo's real backend surface: cleanup goes through .connection."""

    def __init__(self, closed: list[str]) -> None:
        self._closed = closed

    def close(self) -> None:
        self._closed.append("closed")


class _FakeBackend:
    def __init__(self, lock_context: object, applied: list[list[str]], closed: list[str]) -> None:
        self._lock_context = lock_context
        self._applied = applied
        self.connection = _FakeConnection(closed)

    def to_apply(self, migrations):  # type: ignore[no-untyped-def]
        return migrations

    def lock(self):
        return self._lock_context

    def apply_migrations(self, pending) -> None:  # type: ignore[no-untyped-def]
        self._applied.append([item.id for item in pending])


def test_apply_pending_clears_dead_yoyo_lock_and_retries_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sessions.db"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _create_yoyo_lock(db_path, 424242)

    applied: list[list[str]] = []
    closed: list[str] = []
    backends = [
        _FakeBackend(_RaisingLock(), applied, closed),
        _FakeBackend(contextlib.nullcontext(), applied, closed),
    ]

    monkeypatch.setattr(migrator, "_is_pid_alive", lambda _pid: False)
    monkeypatch.setattr(migrator, "read_migrations", lambda _path: [SimpleNamespace(id="V001")])
    monkeypatch.setattr(migrator, "get_backend", lambda _url: backends.pop(0))
    monkeypatch.setattr(migrator, "_read_applied_migration_ids", lambda _path: {"V001"})

    result = apply_pending(str(db_path), migrations_dir)

    assert result == ["V001"]
    assert applied == [["V001"]]
    assert closed == ["closed", "closed"]
    assert _yoyo_lock_pids(db_path) == []
    assert backends == []


def test_apply_pending_recovers_stale_yoyo_lock_with_real_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "V001__real_backend.py").write_text(
        "from yoyo import step\n"
        "__depends__ = set()\n"
        "steps = [step('CREATE TABLE real_backend_demo (id INTEGER PRIMARY KEY)')]\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "sessions.db"
    _create_yoyo_lock(db_path, 424242)
    real_get_backend = migrator.get_backend

    def get_fast_lock_backend(url: str):
        backend = real_get_backend(url)
        real_lock = backend.lock

        def fast_lock(timeout: float = 10):
            return real_lock(timeout=0.01)

        backend.lock = fast_lock  # type: ignore[method-assign]
        return backend

    monkeypatch.setattr(migrator, "_is_pid_alive", lambda _pid: False)
    monkeypatch.setattr(migrator, "get_backend", get_fast_lock_backend)

    result = apply_pending(str(db_path), migrations_dir)

    assert result == ["V001__real_backend"]
    assert _yoyo_lock_pids(db_path) == []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='real_backend_demo'"
        ).fetchall()
    assert rows == [("real_backend_demo",)]


def test_apply_pending_does_not_clear_live_yoyo_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sessions.db"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _create_yoyo_lock(db_path, 12345)

    applied: list[list[str]] = []
    closed: list[str] = []
    backends = [
        _FakeBackend(_RaisingLock("Process 12345 has locked this database"), applied, closed)
    ]

    monkeypatch.setattr(migrator, "_is_pid_alive", lambda pid: pid == 12345)
    monkeypatch.setattr(migrator, "read_migrations", lambda _path: [SimpleNamespace(id="V001")])
    monkeypatch.setattr(migrator, "get_backend", lambda _url: backends.pop(0))

    with pytest.raises(exceptions.LockTimeout, match="live process pid=12345") as excinfo:
        apply_pending(str(db_path), migrations_dir)

    # The message carries concrete remediation for operators.
    assert "Stop the other OpenSquilla process" in str(excinfo.value)
    assert "break-lock" in str(excinfo.value)
    assert applied == []
    assert closed == ["closed"]
    assert _yoyo_lock_pids(db_path) == [12345]
    assert backends == []


def test_apply_pending_does_not_loop_when_stale_lock_retry_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sessions.db"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _create_yoyo_lock(db_path, 424242)

    applied: list[list[str]] = []
    closed: list[str] = []
    backends = [
        _FakeBackend(_RaisingLock(), applied, closed),
        _FakeBackend(_RaisingLock("Database locked after cleanup"), applied, closed),
    ]

    monkeypatch.setattr(migrator, "_is_pid_alive", lambda _pid: False)
    monkeypatch.setattr(migrator, "read_migrations", lambda _path: [SimpleNamespace(id="V001")])
    monkeypatch.setattr(migrator, "get_backend", lambda _url: backends.pop(0))

    with pytest.raises(exceptions.LockTimeout, match="Database locked after cleanup"):
        apply_pending(str(db_path), migrations_dir)

    assert applied == []
    assert closed == ["closed", "closed"]
    assert _yoyo_lock_pids(db_path) == []
    assert backends == []


def test_apply_pending_retries_when_lock_table_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty lock table means the holder released it — retry, don't brick boot."""
    db_path = tmp_path / "sessions.db"
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _create_yoyo_lock_table(db_path)  # table exists, no lock row

    applied: list[list[str]] = []
    closed: list[str] = []
    backends = [
        _FakeBackend(_RaisingLock("Database locked"), applied, closed),
        _FakeBackend(contextlib.nullcontext(), applied, closed),
    ]

    monkeypatch.setattr(migrator, "read_migrations", lambda _path: [SimpleNamespace(id="V001")])
    monkeypatch.setattr(migrator, "get_backend", lambda _url: backends.pop(0))
    monkeypatch.setattr(migrator, "_read_applied_migration_ids", lambda _path: {"V001"})

    result = apply_pending(str(db_path), migrations_dir)

    assert result == ["V001"]
    assert applied == [["V001"]]
    assert backends == []


def test_recover_stale_lock_retries_on_empty_lock_table(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    _create_yoyo_lock_table(db_path)

    assert migrator._recover_stale_yoyo_lock(str(db_path), exceptions.LockTimeout("t")) is True


def test_recover_stale_lock_propagates_when_lock_table_unreadable(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")

    assert migrator._recover_stale_yoyo_lock(str(db_path), exceptions.LockTimeout("t")) is False


def test_clear_yoyo_lock_deletes_only_verified_dead_pids(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    _create_yoyo_lock(db_path, 55555)

    # A pid that is not in the table: nothing is deleted, call still succeeds.
    assert migrator._clear_yoyo_lock(db_path, [424242]) is True
    assert _yoyo_lock_pids(db_path) == [55555]

    assert migrator._clear_yoyo_lock(db_path, [55555]) is True
    assert _yoyo_lock_pids(db_path) == []


def test_recover_stale_lock_never_erases_lock_reacquired_by_live_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TOCTOU regression: the delete is scoped to the pids verified dead."""
    db_path = tmp_path / "sessions.db"
    _create_yoyo_lock(db_path, 55555)  # a live process now owns the lock

    # Simulate the race window: inspection observed a dead pid just before
    # pid 55555 cleared that row and reacquired the lock.
    monkeypatch.setattr(migrator, "_read_yoyo_lock_pids", lambda _path: [424242])
    monkeypatch.setattr(migrator, "_is_pid_alive", lambda _pid: False)

    assert migrator._recover_stale_yoyo_lock(str(db_path), exceptions.LockTimeout("t")) is True
    # The live owner's lock row survives; the retry serializes on yoyo's lock.
    assert _yoyo_lock_pids(db_path) == [55555]


def test_sqlite_path_from_db_url_rejects_unsupported_database_urls() -> None:
    assert migrator._sqlite_path_from_db_url(":memory:") is None
    assert migrator._sqlite_path_from_db_url("sqlite:///:memory:") is None
    assert migrator._sqlite_path_from_db_url("postgresql://example/db") is None


def _write_demo_migration(migrations_dir: Path, name: str = "V001__demo") -> None:
    migrations_dir.mkdir(exist_ok=True)
    (migrations_dir / f"{name}.py").write_text(
        "from yoyo import step\n"
        "__depends__ = set()\n"
        f"steps = [step('CREATE TABLE {name.lower()} (id INTEGER PRIMARY KEY)')]\n",
        encoding="utf-8",
    )


def _write_renumbered_migration_chain(migrations_dir: Path) -> None:
    migrations_dir.mkdir(exist_ok=True)
    migrations = (
        ("V036__session_model_routing", None, "routing_state"),
        ("V037__artifact_sessions", "V036__session_model_routing", "artifact_sessions"),
        (
            "V038__artifact_prompt_annotations",
            "V037__artifact_sessions",
            "artifact_prompt_annotations",
        ),
        (
            "V039__artifact_mutation_attempts",
            "V038__artifact_prompt_annotations",
            "artifact_mutation_attempts",
        ),
        ("V040__document_resources", "V039__artifact_mutation_attempts", "document_resources"),
    )
    for migration_id, dependency, table in migrations:
        depends = "set()" if dependency is None else f"{{{dependency!r}}}"
        (migrations_dir / f"{migration_id}.py").write_text(
            "from yoyo import step\n"
            f"__depends__ = {depends}\n"
            f"steps = [step('CREATE TABLE IF NOT EXISTS {table} "
            "(id INTEGER PRIMARY KEY, value TEXT)')]\n",
            encoding="utf-8",
        )


def _replace_current_ledger_with_legacy_chain(
    db_path: Path,
    legacy_hashes: dict[str, tuple[str, str]],
) -> None:
    affected_ids = {
        "V036__session_model_routing",
        *(replacement_id for replacement_id, _legacy_hash in legacy_hashes.values()),
    }
    with sqlite3.connect(db_path) as connection:
        connection.executemany(
            "DELETE FROM _yoyo_migration WHERE migration_id = ?",
            ((migration_id,) for migration_id in affected_ids),
        )
        connection.executemany(
            "INSERT INTO _yoyo_migration "
            "(migration_hash, migration_id, applied_at_utc) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (
                (legacy_hash, legacy_id)
                for legacy_id, (_replacement_id, legacy_hash) in legacy_hashes.items()
            ),
        )


def _record_applied_migration(db_path: Path, migration_id: str) -> None:
    """Simulate a newer build having applied an extra migration."""
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO _yoyo_migration (migration_hash, migration_id, applied_at_utc) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (f"hash-{migration_id}", migration_id),
        )


def test_assert_schema_not_ahead_noop_for_fresh_and_memory_db(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir)
    # Non-existent file, in-memory, and never-migrated DBs must all pass quietly.
    migrator.assert_schema_not_ahead(str(tmp_path / "missing.db"), migrations_dir)
    migrator.assert_schema_not_ahead(":memory:", migrations_dir)


def test_apply_pending_is_idempotent_when_db_matches_code(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir)
    db_path = tmp_path / "sessions.db"

    first = apply_pending(str(db_path), migrations_dir)
    second = apply_pending(str(db_path), migrations_dir)

    assert first == ["V001__demo"]
    assert second == []  # no SchemaAheadError — every applied id is known


def test_apply_pending_atomically_remaps_exact_legacy_migration_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_renumbered_migration_chain(migrations_dir)
    db_path = tmp_path / "sessions.db"
    legacy_hashes = {
        "V036__artifact_sessions": ("V037__artifact_sessions", "legacy-artifact"),
        "V037__artifact_prompt_annotations": (
            "V038__artifact_prompt_annotations",
            "legacy-annotations",
        ),
        "V038__artifact_mutation_attempts": (
            "V039__artifact_mutation_attempts",
            "legacy-attempts",
        ),
        "V039__document_resources": ("V040__document_resources", "legacy-documents"),
    }
    monkeypatch.setattr(migrator, "_LEGACY_MIGRATION_ALIASES", legacy_hashes)

    assert apply_pending(str(db_path), migrations_dir) == [
        "V036__session_model_routing",
        "V037__artifact_sessions",
        "V038__artifact_prompt_annotations",
        "V039__artifact_mutation_attempts",
        "V040__document_resources",
    ]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO artifact_sessions (id, value) VALUES (1, 'preserved')"
        )
    _replace_current_ledger_with_legacy_chain(db_path, legacy_hashes)

    applied = apply_pending(str(db_path), migrations_dir)

    assert applied == ["V036__session_model_routing"]
    assert apply_pending(str(db_path), migrations_dir) == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT value FROM artifact_sessions WHERE id = 1"
        ).fetchone() == ("preserved",)
        recorded = dict(
            connection.execute(
                "SELECT migration_id, migration_hash FROM _yoyo_migration"
            ).fetchall()
        )
    current = {
        migration.id: migration.hash
        for migration in migrator.read_migrations(str(migrations_dir))
    }
    assert all(
        recorded[migration_id] == migration_hash
        for migration_id, migration_hash in current.items()
    )


def test_apply_pending_rejects_legacy_migration_with_unexpected_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_renumbered_migration_chain(migrations_dir)
    db_path = tmp_path / "sessions.db"
    legacy_hashes = {
        "V036__artifact_sessions": ("V037__artifact_sessions", "legacy-artifact"),
    }
    monkeypatch.setattr(migrator, "_LEGACY_MIGRATION_ALIASES", legacy_hashes)
    apply_pending(str(db_path), migrations_dir)
    _replace_current_ledger_with_legacy_chain(db_path, legacy_hashes)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE _yoyo_migration SET migration_hash = 'unexpected' "
            "WHERE migration_id = 'V036__artifact_sessions'"
        )

    with pytest.raises(migrator.SchemaAheadError, match="does not match the exact historical"):
        apply_pending(str(db_path), migrations_dir)


def test_apply_pending_raises_when_database_is_ahead_of_code(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir)
    db_path = tmp_path / "sessions.db"

    apply_pending(str(db_path), migrations_dir)
    # A newer build applied V999 that this code's migration set does not contain.
    _record_applied_migration(db_path, "V999__from_the_future")

    with pytest.raises(migrator.SchemaAheadError, match="V999__from_the_future"):
        apply_pending(str(db_path), migrations_dir)


def test_read_applied_migration_ids_handles_missing_ledger(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    # No yoyo ledger table → empty set (not None, not an error).
    assert migrator._read_applied_migration_ids(db_path) == set()


# ---------------------------------------------------------------------------
# Pending plan must be computed inside yoyo's lock (concurrency regressions)
# ---------------------------------------------------------------------------


def test_apply_pending_process_lock_precedes_sqlite_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from opensquilla.recovery.errors import ProfileLockBusyError
    from opensquilla.recovery.locking import ProfileOperationLock

    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir)
    db_path = tmp_path / "sessions.db"
    acquired = threading.Event()
    release = threading.Event()

    def hold_database_migration_lock() -> None:
        with ProfileOperationLock(db_path):
            acquired.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold_database_migration_lock)
    holder.start()
    assert acquired.wait(timeout=5)
    monkeypatch.setattr(migrator, "_MIGRATION_PROCESS_LOCK_TIMEOUT_SECONDS", 0.0)
    try:
        with pytest.raises(ProfileLockBusyError):
            apply_pending(str(db_path), migrations_dir)
        assert not db_path.exists(), "SQLite must not open before the external lock"
    finally:
        release.set()
        holder.join(timeout=5)
    assert not holder.is_alive()


def test_apply_pending_recomputes_pending_under_lock_after_losing_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-lock pending plan must be discarded once the lock is acquired.

    Simulates the exact interleaving of the production race deterministically:
    a competing migrator applies the full chain (including a recreate-and-copy
    migration) and seeds data while this caller is waiting for yoyo's lock.
    The caller must recompute the plan under the lock and apply nothing; a
    stale plan would re-run V001/V002 and either crash or drop the
    later-added column.
    """
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "V001__base.py").write_text(
        "from yoyo import step\n"
        "__depends__ = set()\n"
        "steps = [step('CREATE TABLE runs (id INTEGER PRIMARY KEY, name TEXT)')]\n",
        encoding="utf-8",
    )
    (migrations_dir / "V002__recreate.py").write_text(
        "from yoyo import step\n"
        "__depends__ = {'V001__base'}\n"
        "steps = [\n"
        "    step('CREATE TABLE runs_new "
        "(id INTEGER PRIMARY KEY, name TEXT, usage_json TEXT)'),\n"
        "    step('INSERT INTO runs_new (id, name) SELECT id, name FROM runs'),\n"
        "    step('DROP TABLE runs'),\n"
        "    step('ALTER TABLE runs_new RENAME TO runs'),\n"
        "]\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "sessions.db"
    real_get_backend = migrator.get_backend
    raced: list[bool] = []

    # Pre-create yoyo's internal schema so the only lock acquisition left in
    # the run below is the migration lock itself (yoyo otherwise takes the
    # lock once during its internal-schema upgrade, which would fire the race
    # hook before to_apply in either ordering and mask the regression).
    seed_backend = real_get_backend(migrator._to_yoyo_url(str(db_path)))
    try:
        with seed_backend.lock():
            seed_backend.get_applied_migration_hashes()
    finally:
        seed_backend.connection.close()

    def competing_migrator_wins(yoyo_url: str) -> None:
        other = real_get_backend(yoyo_url)
        try:
            migrations = migrator.read_migrations(str(migrations_dir))
            with other.lock():
                other.apply_migrations(other.to_apply(migrations))
        finally:
            other.connection.close()
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "INSERT INTO runs (id, name, usage_json) VALUES (1, 'keep', '{}')"
            )

    def racing_get_backend(yoyo_url: str):
        backend = real_get_backend(yoyo_url)
        real_lock = backend.lock

        @contextlib.contextmanager
        def racing_lock(timeout: float = 10):
            if not raced:
                raced.append(True)
                competing_migrator_wins(yoyo_url)
            with real_lock(timeout=timeout):
                yield

        backend.lock = racing_lock  # type: ignore[method-assign]
        return backend

    monkeypatch.setattr(migrator, "get_backend", racing_get_backend)

    applied = apply_pending(str(db_path), migrations_dir)

    assert applied == []  # the plan was recomputed under the lock
    assert raced == [True]
    with sqlite3.connect(db_path) as connection:
        ledger = sorted(
            row[0]
            for row in connection.execute("SELECT migration_id FROM _yoyo_migration")
        )
        rows = connection.execute("SELECT id, name, usage_json FROM runs").fetchall()
    assert ledger == ["V001__base", "V002__recreate"]  # each applied exactly once
    assert rows == [(1, "keep", "{}")]  # seeded data survived; column intact


_CONCURRENT_WORKER = """
import json
import sys
import time
from pathlib import Path

from opensquilla.persistence.migrator import apply_pending

db_url, migrations_dir, go_file = sys.argv[1], sys.argv[2], sys.argv[3]
print("READY", flush=True)
deadline = time.monotonic() + 30
while not Path(go_file).exists():
    if time.monotonic() > deadline:
        raise SystemExit("no go signal")
    time.sleep(0.01)
applied = apply_pending(db_url, Path(migrations_dir))
print("APPLIED:" + json.dumps(applied), flush=True)
"""


def test_apply_pending_two_concurrent_callers_real_migrations(tmp_path: Path) -> None:
    """Two boot-style processes race on one DB using the repo migration chain."""
    from opensquilla.recovery.locking import ProfileOperationLock

    assert _REPO_MIGRATIONS_DIR.is_dir()
    expected = sorted(entry.stem for entry in _REPO_MIGRATIONS_DIR.glob("V*.py"))
    assert expected

    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE user_data (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO user_data VALUES (1, 'precious')")
    # Runtime bootstrap has already established the external lock authority
    # before migrations start. Seed the same authority here so this test
    # isolates simultaneous migration ownership rather than also racing the
    # first creation of the OS user-state directory tree.
    with ProfileOperationLock(db_path):
        pass

    go_file = tmp_path / "go"
    workers = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CONCURRENT_WORKER,
                str(db_path),
                str(_REPO_MIGRATIONS_DIR),
                str(go_file),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(tmp_path),
        )
        for _ in range(2)
    ]
    try:
        for worker in workers:
            assert worker.stdout is not None
            assert worker.stdout.readline().strip() == "READY"
        go_file.touch()  # release both workers as close to simultaneously as possible

        applied_per_worker: list[list[str]] = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=120)
            assert worker.returncode == 0, stderr
            payload = [line for line in stdout.splitlines() if line.startswith("APPLIED:")]
            assert payload, stdout
            applied_per_worker.append(json.loads(payload[0].removeprefix("APPLIED:")))
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()

    combined = sorted(applied_per_worker[0] + applied_per_worker[1])
    assert combined == expected  # every id applied exactly once across the callers
    with sqlite3.connect(db_path) as connection:
        ledger = sorted(
            row[0]
            for row in connection.execute("SELECT migration_id FROM _yoyo_migration")
        )
        rows = connection.execute("SELECT note FROM user_data").fetchall()
    assert ledger == expected
    assert rows == [("precious",)]


# ---------------------------------------------------------------------------
# Migration discovery: glob metacharacters and fail-loud empty discovery
# ---------------------------------------------------------------------------


def test_apply_pending_handles_glob_metacharacters_in_paths(tmp_path: Path) -> None:
    weird = tmp_path / "weird [x] dir"
    weird.mkdir()
    migrations_dir = weird / "migrations"
    _write_demo_migration(migrations_dir, name="V001__globsafe")
    db_path = weird / "sessions.db"

    first = apply_pending(str(db_path), migrations_dir)
    second = apply_pending(str(db_path), migrations_dir)

    assert first == ["V001__globsafe"]
    assert second == []  # no spurious SchemaAheadError from empty discovery
    with sqlite3.connect(db_path) as connection:
        ledger = [
            row[0]
            for row in connection.execute("SELECT migration_id FROM _yoyo_migration")
        ]
    assert ledger == ["V001__globsafe"]


def test_apply_pending_fails_loud_when_discovery_finds_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir)
    monkeypatch.setattr(migrator, "read_migrations", lambda _source: [])

    with pytest.raises(RuntimeError, match="zero migrations") as excinfo:
        apply_pending(str(tmp_path / "sessions.db"), migrations_dir)

    assert str(migrations_dir) in str(excinfo.value)


# ---------------------------------------------------------------------------
# URL normalisation: yoyo must migrate the exact file the guards inspect
# ---------------------------------------------------------------------------


def _expected_sqlite_path(raw: str) -> Path:
    logical = Path(raw).expanduser().resolve()
    if os.name != "nt":
        return logical
    value = str(logical)
    if value.startswith("\\\\?\\"):
        return logical
    if value.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{value[2:]}")
    return Path(f"\\\\?\\{value}")


def _expected_yoyo_url(raw: str) -> str:
    expected = _expected_sqlite_path(raw)
    value = str(expected) if os.name == "nt" else expected.as_posix()
    return "sqlite:///" + quote(value, safe="/:")


def test_to_yoyo_url_percent_encodes_url_metacharacters() -> None:
    assert migrator._to_yoyo_url("/tmp/a#b/demo.db") == _expected_yoyo_url(
        "/tmp/a#b/demo.db"
    )
    assert migrator._to_yoyo_url("/tmp/a?b/demo.db") == _expected_yoyo_url(
        "/tmp/a?b/demo.db"
    )
    # A literal '%41' must not decay into 'A' inside sqlite's URI parser.
    assert migrator._to_yoyo_url("/tmp/pct%41/demo.db") == _expected_yoyo_url(
        "/tmp/pct%41/demo.db"
    )


def test_to_yoyo_url_round_trips_through_inspection_helper() -> None:
    for raw in ("/tmp/a#b/demo.db", "/tmp/pct%41/demo.db", "/tmp/weird [x]/demo.db"):
        assert migrator._sqlite_path_from_db_url(
            migrator._to_yoyo_url(raw)
        ) == _expected_sqlite_path(raw)


@pytest.mark.parametrize("dirname", ["note#1", "pct%41dir"])
def test_apply_pending_migrates_the_exact_file_the_guard_inspects(
    tmp_path: Path, dirname: str
) -> None:
    base = tmp_path / dirname
    base.mkdir()
    migrations_dir = base / "migrations"
    _write_demo_migration(migrations_dir, name="V001__exactfile")
    db_path = base / "demo.db"

    first = apply_pending(str(db_path), migrations_dir)
    second = apply_pending(str(db_path), migrations_dir)

    assert first == ["V001__exactfile"]
    assert second == []  # the downgrade guard inspected the same file yoyo migrated
    assert db_path.exists()
    with sqlite3.connect(db_path) as connection:
        ledger = [
            row[0]
            for row in connection.execute("SELECT migration_id FROM _yoyo_migration")
        ]
    assert ledger == ["V001__exactfile"]
    if "%41" in dirname:
        decoded_variant = tmp_path / dirname.replace("%41", "A")
        assert not decoded_variant.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path semantics only")
async def test_bare_logical_long_path_runs_migrations_and_session_storage(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__long_path")

    long_root = tmp_path / "long-db"
    parent = long_root
    native_root = migrator._native_sqlite_path(long_root)

    def cleanup() -> None:
        if os.path.exists(native_root):
            shutil.rmtree(native_root)

    request.addfinalizer(cleanup)
    while len(str(parent / "sessions.db")) <= 280:
        parent /= "nested-" + ("x" * 40)
    db_path = parent / "sessions.db"
    assert len(str(db_path)) > 260
    assert not str(db_path).startswith("\\\\?\\")

    native_parent = migrator._native_sqlite_path(parent)
    os.makedirs(native_parent, exist_ok=True)

    assert apply_pending(str(db_path), migrations_dir) == ["V001__long_path"]

    native_db_path = migrator._native_sqlite_path(db_path)
    storage = SessionStorage(native_db_path)
    await storage.connect()
    await storage.close()

    with contextlib.closing(sqlite3.connect(native_db_path)) as connection, connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM _yoyo_migration WHERE migration_id = ?",
            ("V001__long_path",),
        ).fetchone() == (1,)


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path semantics only")
def test_preformed_sqlite_url_with_long_local_path_runs_migrations(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__preformed_long_url")

    long_root = tmp_path / "long-url-db"
    parent = long_root
    native_root = migrator._native_sqlite_path(long_root)

    def cleanup() -> None:
        if os.path.exists(native_root):
            shutil.rmtree(native_root)

    request.addfinalizer(cleanup)
    while len(str(parent / "sessions.db")) <= 280:
        parent /= "nested-" + ("u" * 40)
    db_path = parent / "sessions.db"
    os.makedirs(migrator._native_sqlite_path(parent), exist_ok=True)

    db_url = db_path.as_uri().replace("file://", "sqlite://", 1)
    assert "\\\\?\\" not in db_url
    assert apply_pending(db_url, migrations_dir) == ["V001__preformed_long_url"]

    native_db_path = migrator._native_sqlite_path(db_path)
    with contextlib.closing(sqlite3.connect(native_db_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM _yoyo_migration WHERE migration_id = ?",
            ("V001__preformed_long_url",),
        ).fetchone() == (1,)


# ---------------------------------------------------------------------------
# Pre-migration backups
# ---------------------------------------------------------------------------


def test_apply_pending_skips_backup_on_fresh_install(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__fresh")

    apply_pending(str(tmp_path / "sessions.db"), migrations_dir)

    assert list(tmp_path.glob("*.bak")) == []


def test_apply_pending_backs_up_existing_db_before_applying(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__first")
    db_path = tmp_path / "sessions.db"
    apply_pending(str(db_path), migrations_dir)

    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE user_data (id INTEGER PRIMARY KEY, note TEXT)")
        connection.execute("INSERT INTO user_data VALUES (1, 'precious')")

    _write_demo_migration(migrations_dir, name="V002__second")
    applied = apply_pending(str(db_path), migrations_dir)

    assert applied == ["V002__second"]
    backup_path = tmp_path / "sessions.db.pre-V002__second.bak"
    assert backup_path.exists()
    with sqlite3.connect(backup_path) as connection:
        rows = connection.execute("SELECT note FROM user_data").fetchall()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert rows == [("precious",)]
    assert "v001__first" in tables  # pre-apply state is present
    assert "v002__second" not in tables  # snapshot taken BEFORE applying V002
    if os.name != "nt":
        assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600


def test_apply_pending_rotates_backups_keeping_two_most_recent(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__m1")
    db_path = tmp_path / "sessions.db"
    apply_pending(str(db_path), migrations_dir)

    for number in (2, 3, 4):
        _write_demo_migration(migrations_dir, name=f"V00{number}__m{number}")
        apply_pending(str(db_path), migrations_dir)

    backups = sorted(entry.name for entry in tmp_path.glob("sessions.db.pre-*.bak"))
    assert backups == [
        "sessions.db.pre-V003__m3.bak",
        "sessions.db.pre-V004__m4.bak",
    ]


def test_apply_pending_continues_when_backup_fails(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__first")
    db_path = tmp_path / "sessions.db"
    apply_pending(str(db_path), migrations_dir)

    # A directory squatting on the backup path makes the snapshot fail.
    blocker = tmp_path / "sessions.db.pre-V002__second.bak"
    blocker.mkdir()
    _write_demo_migration(migrations_dir, name="V002__second")

    applied = apply_pending(str(db_path), migrations_dir)

    assert applied == ["V002__second"]  # backup failure never aborts boot
    assert blocker.is_dir()


# ---------------------------------------------------------------------------
# Database file permissions
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions only")
def test_apply_pending_restricts_database_file_permissions(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__perms")
    db_path = tmp_path / "sessions.db"

    apply_pending(str(db_path), migrations_dir)

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# Ledger self-check after apply
# ---------------------------------------------------------------------------


def test_apply_pending_fails_closed_when_ledger_missing_applied_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    applied: list[list[str]] = []
    closed: list[str] = []
    backends = [_FakeBackend(contextlib.nullcontext(), applied, closed)]
    monkeypatch.setattr(migrator, "read_migrations", lambda _path: [SimpleNamespace(id="V001")])
    monkeypatch.setattr(migrator, "get_backend", lambda _url: backends.pop(0))

    # The fake backend "applies" V001 but the real ledger never records it.
    with pytest.raises(RuntimeError, match="ledger"):
        apply_pending(str(db_path), migrations_dir)


def test_apply_pending_tolerates_uninspectable_ledger_after_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()

    applied: list[list[str]] = []
    closed: list[str] = []
    backends = [_FakeBackend(contextlib.nullcontext(), applied, closed)]
    monkeypatch.setattr(migrator, "read_migrations", lambda _path: [SimpleNamespace(id="V001")])
    monkeypatch.setattr(migrator, "get_backend", lambda _url: backends.pop(0))
    monkeypatch.setattr(migrator, "_read_applied_migration_ids", lambda _path: None)

    assert apply_pending(str(db_path), migrations_dir) == ["V001"]


# ---------------------------------------------------------------------------
# Audit username fallback
# ---------------------------------------------------------------------------


def test_apply_pending_audit_never_depends_on_fqdn_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__local_audit")
    db_path = tmp_path / "sessions.db"

    monkeypatch.setattr(
        migrator.socket,
        "getfqdn",
        lambda *_args, **_kwargs: pytest.fail("migration audit must not resolve DNS"),
    )
    monkeypatch.setattr(migrator.socket, "gethostname", lambda: "synthetic-local-host")

    applied = apply_pending(str(db_path), migrations_dir)

    assert applied == ["V001__local_audit"]
    with sqlite3.connect(db_path) as connection:
        audit_rows = connection.execute(
            "SELECT migration_id, username, hostname, operation FROM _yoyo_log"
        ).fetchall()
    assert audit_rows == [
        ("V001__local_audit", getpass.getuser(), "synthetic-local-host", "apply")
    ]


@pytest.mark.skipif(os.name == "nt", reason="pwd module is POSIX only")
def test_apply_pending_survives_unresolvable_username(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pwd

    for var in ("LOGNAME", "USER", "LNAME", "USERNAME"):
        monkeypatch.delenv(var, raising=False)

    def no_passwd_entry(_uid: int):
        raise KeyError("no passwd entry for uid")

    monkeypatch.setattr(pwd, "getpwuid", no_passwd_entry)

    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__nouser")

    applied = apply_pending(str(tmp_path / "sessions.db"), migrations_dir)

    assert applied == ["V001__nouser"]
    assert getpass.getuser() == "opensquilla"


# ---------------------------------------------------------------------------
# Process liveness probes
# ---------------------------------------------------------------------------


def test_is_pid_alive_true_for_own_process() -> None:
    assert migrator._is_pid_alive(os.getpid()) is True


def test_is_pid_alive_false_for_reaped_child() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    assert migrator._is_pid_alive(child.pid) is False


def test_is_pid_alive_rejects_nonpositive_pids() -> None:
    assert migrator._is_pid_alive(0) is False
    assert migrator._is_pid_alive(-5) is False


def _fake_ctypes(
    open_result: int,
    last_error: int,
    calls: dict[str, object],
    *,
    exit_code: int = 259,
    get_exit_code_result: int = 1,
) -> SimpleNamespace:
    class FakeUInt32:
        def __init__(self, value: int = 0) -> None:
            self.value = value

    def open_process(access: object, inherit: object, pid: object) -> int:
        calls["open_process"] = (access, inherit, pid)
        return open_result

    def get_exit_code_process(handle: object, code_ref: FakeUInt32) -> int:
        calls["get_exit_code_process"] = handle
        code_ref.value = exit_code
        return get_exit_code_result

    def close_handle(handle: object) -> int:
        calls["closed_handle"] = handle
        return 1

    def win_dll(name: str, use_last_error: bool = False) -> SimpleNamespace:
        calls["win_dll"] = (name, use_last_error)
        return SimpleNamespace(
            OpenProcess=open_process,
            GetExitCodeProcess=get_exit_code_process,
            CloseHandle=close_handle,
        )

    def get_last_error() -> int:
        calls["read_last_error"] = True
        return last_error

    def byref(value: FakeUInt32) -> FakeUInt32:
        calls["byref"] = value
        return value

    return SimpleNamespace(
        WinDLL=win_dll,
        get_last_error=get_last_error,
        byref=byref,
        c_void_p=object(),
        c_uint32=FakeUInt32,
        c_int=object(),
    )


def test_is_pid_alive_windows_open_handle_means_alive() -> None:
    calls: dict[str, object] = {}
    fake = _fake_ctypes(open_result=1234, last_error=0, calls=calls)

    assert migrator._is_pid_alive_windows(4321, ctypes_module=fake) is True
    assert calls["win_dll"] == ("kernel32", True)  # use_last_error must be set
    assert calls["get_exit_code_process"] == 1234
    assert calls["closed_handle"] == 1234


def test_is_pid_alive_windows_exited_handle_means_dead() -> None:
    calls: dict[str, object] = {}
    fake = _fake_ctypes(open_result=1234, last_error=0, calls=calls, exit_code=0)

    assert migrator._is_pid_alive_windows(4321, ctypes_module=fake) is False
    assert calls["get_exit_code_process"] == 1234
    assert calls["closed_handle"] == 1234


def test_is_pid_alive_windows_access_denied_means_alive() -> None:
    calls: dict[str, object] = {}
    fake = _fake_ctypes(open_result=0, last_error=5, calls=calls)

    assert migrator._is_pid_alive_windows(4321, ctypes_module=fake) is True
    assert calls["read_last_error"] is True
    assert "closed_handle" not in calls


def test_is_pid_alive_windows_other_error_means_dead() -> None:
    calls: dict[str, object] = {}
    fake = _fake_ctypes(open_result=0, last_error=87, calls=calls)

    assert migrator._is_pid_alive_windows(4321, ctypes_module=fake) is False


def test_is_pid_alive_dispatches_to_windows_probe_on_nt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed: list[int] = []
    monkeypatch.setattr(migrator.os, "name", "nt")
    monkeypatch.setattr(migrator, "_is_pid_alive_windows", lambda pid: probed.append(pid) or True)

    assert migrator._is_pid_alive(777) is True
    assert probed == [777]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions only")
def test_apply_pending_preserves_existing_database_permissions(tmp_path: Path) -> None:
    """Only databases created by this boot are tightened; deliberate operator
    permissions on pre-existing files must survive every subsequent boot."""
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__perms_existing")
    db_path = tmp_path / "sessions.db"
    sqlite3.connect(db_path).close()
    os.chmod(db_path, 0o644)

    apply_pending(str(db_path), migrations_dir)

    assert stat.S_IMODE(db_path.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="chmod failure semantics POSIX only")
def test_backup_survives_chmod_failure_and_still_rotates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem that rejects chmod (CIFS, root-squashed NFS) must not
    relabel a good snapshot as failed nor skip rotation."""
    migrations_dir = tmp_path / "migrations"
    _write_demo_migration(migrations_dir, name="V001__first")
    db_path = tmp_path / "sessions.db"
    apply_pending(str(db_path), migrations_dir)

    # Pre-seed stale backups so rotation has something to prune.
    for stale in ("sessions.db.pre-V000__a.bak", "sessions.db.pre-V000__b.bak"):
        (tmp_path / stale).write_bytes(b"stale")

    def _chmod_unsupported(path: object, mode: int) -> None:
        raise OSError("chmod unsupported on this filesystem")

    monkeypatch.setattr("opensquilla.persistence.migrator.os.chmod", _chmod_unsupported)
    _write_demo_migration(migrations_dir, name="V002__second")

    applied = apply_pending(str(db_path), migrations_dir)

    assert applied == ["V002__second"]
    backups = sorted(entry.name for entry in tmp_path.glob("sessions.db.pre-*.bak"))
    # The fresh snapshot exists and rotation still enforced the keep bound.
    assert "sessions.db.pre-V002__second.bak" in backups
    assert len(backups) == 2


def test_read_applied_migration_ids_structural_failure_reads_as_empty(tmp_path: Path) -> None:
    """A ledger table whose migration_id column cannot be read is a schema-
    tracking failure: report set() so the post-apply verifier fails closed
    instead of tolerating a permanently blind downgrade guard."""
    db_path = tmp_path / "structural.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE _yoyo_migration (id TEXT PRIMARY KEY)")  # no migration_id
    conn.commit()
    conn.close()

    assert migrator._read_applied_migration_ids(db_path) == set()


def test_read_applied_migration_ids_lock_contention_reads_as_uninspectable(
    tmp_path: Path,
) -> None:
    """Transient 'database is locked' must map to None (tolerated), not to an
    empty-but-readable ledger that would fail a healthy boot closed."""
    db_path = tmp_path / "locked.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE _yoyo_migration (migration_id TEXT PRIMARY KEY)")
    conn.commit()
    holder = sqlite3.connect(db_path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        assert migrator._read_applied_migration_ids(db_path) is None
    finally:
        holder.rollback()
        holder.close()
        conn.close()
