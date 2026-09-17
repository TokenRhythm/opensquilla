"""SQLite handle-lifetime contracts for the recovery session merge.

Windows applies mandatory file locking: removing a file that a process still
holds open fails with ``PermissionError``.  POSIX allows it, so a leaked SQLite
connection is invisible on macOS and Linux and surfaces only as a blocked
consolidation on Windows.  These tests pin the invariant directly and emulate
the Windows removal semantics so the regression reproduces everywhere.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from opensquilla.cli.session_schema import prepare_session_schema
from opensquilla.recovery.session_merge import (
    SessionMergeResult,
    snapshot_session_database,
)
from opensquilla.recovery.session_merge import (
    merge_session_database as _merge_session_database,
)


def merge_session_database(
    target: str | Path,
    source: str | Path,
    *,
    source_id: str,
) -> SessionMergeResult:
    return _merge_session_database(
        target,
        source,
        source_id=source_id,
        prepare_target_schema=prepare_session_schema,
    )


_SCHEMA = """
CREATE TABLE sessions (
    session_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    label TEXT,
    estimated_cost_usd REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE transcript_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE compacted_transcript_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    original_entry_id INTEGER,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    created_at INTEGER NOT NULL,
    compaction_id TEXT
);
CREATE TABLE session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    summary_text TEXT NOT NULL,
    covered_through_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE session_context_states (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    state_kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    covered_through_id INTEGER NOT NULL DEFAULT 0,
    provider TEXT NOT NULL DEFAULT 'portable',
    valid INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE usage_events (
    event_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    call_index INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    agent_id TEXT,
    started_at_ms INTEGER,
    completed_at_ms INTEGER,
    UNIQUE(execution_id, call_index)
);
"""

_SOURCE_ID = "55555555-5555-5555-8555-555555555555"


def _seed(path: Path, *, key: str, session_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO sessions(session_key, session_id, updated_at, label) "
            "VALUES (?, ?, 1, 'seeded')",
            (key, session_id),
        )
        connection.execute(
            "INSERT INTO transcript_entries("
            "session_id, session_key, message_id, role, content, created_at"
            ") VALUES (?, ?, 'm1', 'user', 'hello', 1)",
            (session_id, key),
        )
        connection.commit()
    finally:
        connection.close()


def _database_path(database: object) -> Path | None:
    """Best-effort mapping from a ``sqlite3.connect`` argument to a real path."""

    if isinstance(database, Path):
        return database.absolute()
    if not isinstance(database, str) or database == ":memory:":
        return None
    if database.startswith("file:"):
        split = urlsplit(database)
        return Path(unquote(split.path)).absolute() if split.path else None
    return Path(database).absolute()


def _is_open(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    except sqlite3.Error:
        return True
    return True


class _ConnectionRecorder:
    """Record SQLite connections and the paths they hold open."""

    def __init__(self) -> None:
        self.connections: list[sqlite3.Connection] = []
        self.by_path: dict[Path, list[sqlite3.Connection]] = {}
        self._real_connect = sqlite3.connect

    def connect(self, database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        connection = self._real_connect(database, *args, **kwargs)  # type: ignore[arg-type]
        self.connections.append(connection)
        resolved = _database_path(database)
        if resolved is not None:
            self.by_path.setdefault(resolved, []).append(connection)
        return connection

    def open_under(self, root: Path) -> list[Path]:
        held = []
        for path, connections in self.by_path.items():
            if not any(_is_open(connection) for connection in connections):
                continue
            if path == root or root in path.parents:
                held.append(path)
        return sorted(held)

    def leaked(self) -> list[sqlite3.Connection]:
        return [connection for connection in self.connections if _is_open(connection)]

    def close_all(self) -> None:
        for connection in self.connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ConnectionRecorder]:
    """Track every SQLite connection opened while the fixture is active.

    ``sqlite3.Connection`` is a C type that rejects method patching, so closure
    is detected by probing each connection after the operation completes.
    """

    instance = _ConnectionRecorder()
    monkeypatch.setattr(sqlite3, "connect", instance.connect)
    try:
        yield instance
    finally:
        monkeypatch.undo()
        instance.close_all()


def _assert_no_leaks(instance: _ConnectionRecorder, operation: str) -> None:
    leaked = instance.leaked()
    assert not leaked, (
        f"{len(leaked)} of {len(instance.connections)} SQLite connection(s) were still open "
        f"after {operation}; on Windows this fails the private-snapshot teardown with "
        "WinError 32 and turns consolidation into outcome='blocked'"
    )


def test_snapshot_closes_every_sqlite_connection(
    tmp_path: Path,
    recorder: _ConnectionRecorder,
) -> None:
    source = tmp_path / "recovery" / "state" / "sessions.db"
    destination = tmp_path / "primary" / "state" / "sessions.db"
    _seed(source, key="agent:main:main", session_id="source-session")

    snapshot_session_database(source, destination)

    _assert_no_leaks(recorder, "snapshot_session_database()")


def test_merge_closes_every_sqlite_connection(
    tmp_path: Path,
    recorder: _ConnectionRecorder,
) -> None:
    source = tmp_path / "recovery" / "state" / "sessions.db"
    target = tmp_path / "primary" / "state" / "sessions.db"
    _seed(source, key="agent:main:main", session_id="source-session")
    _seed(target, key="agent:main:other", session_id="target-session")

    merge_session_database(target, source, source_id=_SOURCE_ID)

    _assert_no_leaks(recorder, "merge_session_database()")


@pytest.fixture()
def windows_like_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_ConnectionRecorder]:
    """Refuse to remove any tree that still holds a live SQLite handle.

    ``shutil.rmtree`` is guarded rather than ``os.unlink`` because CPython's
    POSIX implementation removes entries through ``dir_fd`` with relative names,
    which a path-based guard cannot resolve.  Guarding the tree root reproduces
    the Windows ``PermissionError`` that ``TemporaryDirectory`` teardown raises.
    """

    instance = _ConnectionRecorder()
    real_rmtree = shutil.rmtree

    def guarded_rmtree(path: object, *args: object, **kwargs: object) -> None:
        root = Path(os.fsdecode(path)).absolute()  # type: ignore[arg-type]
        held = instance.open_under(root)
        if held:
            raise PermissionError(
                32,
                "The process cannot access the file because it is being used by "
                f"another process: '{held[0]}'",
            )
        real_rmtree(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", instance.connect)
    monkeypatch.setattr(shutil, "rmtree", guarded_rmtree)
    try:
        yield instance
    finally:
        monkeypatch.undo()
        instance.close_all()


def test_snapshot_survives_windows_like_removal_semantics(
    tmp_path: Path,
    windows_like_removal: _ConnectionRecorder,
) -> None:
    source = tmp_path / "recovery" / "state" / "sessions.db"
    destination = tmp_path / "primary" / "state" / "sessions.db"
    _seed(source, key="agent:main:main", session_id="source-session")

    snapshot_session_database(source, destination)

    assert destination.exists()


def test_merge_survives_windows_like_removal_semantics(
    tmp_path: Path,
    windows_like_removal: _ConnectionRecorder,
) -> None:
    source = tmp_path / "recovery" / "state" / "sessions.db"
    target = tmp_path / "primary" / "state" / "sessions.db"
    _seed(source, key="agent:main:main", session_id="source-session")
    _seed(target, key="agent:main:other", session_id="target-session")

    result = merge_session_database(target, source, source_id=_SOURCE_ID)

    assert result.imported_sessions == 1


def test_merge_never_removes_the_recovery_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consolidation reads recovery sources; it must never unlink them."""

    source = tmp_path / "recovery" / "state" / "sessions.db"
    target = tmp_path / "primary" / "state" / "sessions.db"
    _seed(source, key="agent:main:main", session_id="source-session")
    _seed(target, key="agent:main:other", session_id="target-session")
    before = source.read_bytes()

    removed: list[Path] = []
    real_unlink = os.unlink

    def recording_unlink(path: object, *args: object, **kwargs: object) -> None:
        if not kwargs.get("dir_fd"):
            removed.append(Path(os.fsdecode(path)).absolute())  # type: ignore[arg-type]
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", recording_unlink)

    merge_session_database(target, source, source_id=_SOURCE_ID)

    assert source.read_bytes() == before
    assert source.absolute() not in removed
