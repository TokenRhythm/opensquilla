"""Offline, session-granular consolidation for ``sessions.db``.

The recovery bootstrap imports conversation-owned rows only.  Machine-local
control state (scheduler ticks, router calibration, global upload cursors, and
similar tables) remains available in the archived source profile instead of
being guessed together across installations.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from opensquilla.recovery.atomic import _native_io_path

_CORE_TABLES = (
    "sessions",
    "transcript_entries",
    "compacted_transcript_entries",
    "session_summaries",
    "session_context_states",
)
_EXCLUDED_OPERATIONAL_TABLES = (
    "agent_tasks",
    "heartbeat_ticks",
    "memory_durable_receipts",
    "meta_skill_runs",
    "meta_skill_run_steps",
    "router_decisions",
    "telemetry_daily_usage",
    "turn_ingress_receipts",
    "turn_errors",
    "usage_billing_receipt_state",
    "usage_ledger_state",
)
_SESSION_KEY_FIELDS = frozenset(
    {
        "accepted_session_key",
        "parent_session_key",
        "provenance_source_session_key",
        "request_session_key",
        "session_key",
        "spawned_by",
    }
)
_SESSION_ID_FIELDS = frozenset(
    {
        "provenance_origin_session_id",
        "session_id",
    }
)
_FINGERPRINT_IGNORED_FIELDS = frozenset(
    {
        "covered_through_id",
        "id",
        "original_entry_id",
    }
)
_UUID_NAMESPACE = uuid.UUID("edc9a806-a99c-4b24-a7df-0b6d25a147ab")

SessionSchemaPreparer = Callable[[Path], None]


@dataclass(frozen=True)
class SessionMergeResult:
    """Privacy-safe summary of one source database import."""

    source_id: str
    imported_sessions: int
    deduplicated_sessions: int
    remapped_session_keys: dict[str, str] = field(default_factory=dict)
    remapped_session_ids: dict[str, str] = field(default_factory=dict)
    imported_rows: dict[str, int] = field(default_factory=dict)
    excluded_tables: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "imported_sessions": self.imported_sessions,
            "deduplicated_sessions": self.deduplicated_sessions,
            "imported_rows": dict(self.imported_rows),
            "excluded_tables": list(self.excluded_tables),
        }


@dataclass(frozen=True)
class _SessionImport:
    source_key: str
    source_id: str
    target_key: str
    target_id: str


def _quote(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum():
        raise ValueError(f"unsafe SQLite identifier: {identifier!r}")
    return f'"{identifier}"'


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    if not _table_exists(connection, table):
        return ()
    return tuple(
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    )


def _rows(
    connection: sqlite3.Connection,
    table: str,
    where: str = "",
    parameters: Iterable[object] = (),
) -> list[dict[str, Any]]:
    if not _table_exists(connection, table):
        return []
    statement = f"SELECT * FROM {_quote(table)}"
    if where:
        statement += f" WHERE {where}"
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(statement, tuple(parameters)).fetchall()]


def _quick_check(connection: sqlite3.Connection, *, label: str) -> None:
    result = connection.execute("PRAGMA quick_check").fetchone()
    if result is None or str(result[0]).lower() != "ok":
        raise sqlite3.DatabaseError(f"{label} quick_check failed: {result!r}")


@contextlib.contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Open ``path`` read-only and always close the handle on exit.

    ``sqlite3.Connection`` is itself a context manager, but its ``__exit__``
    only commits or rolls back the active transaction — it leaves the handle
    open.  Windows applies mandatory file locking, so a leaked read handle makes
    the private-snapshot ``TemporaryDirectory`` teardown fail with
    ``PermissionError`` and turns consolidation into ``blocked``.
    """

    absolute = path.expanduser().absolute()
    if os.name == "nt":
        encoded_path = quote(_native_io_path(absolute), safe="/:")
        uri = f"file:{encoded_path}?mode=ro"
    else:
        uri = f"{absolute.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        connection.close()


@contextlib.contextmanager
def _private_source_snapshot(source: Path) -> Iterator[Path]:
    """Copy a stable SQLite bundle before allowing SQLite to inspect it.

    SQLite can create a ``-shm`` file beside a WAL database even when it is
    opened with ``mode=ro``.  Recovery profiles are immutable inputs, so copy
    the database and durable sidecars into a private directory first.  The
    no-follow copy and post-copy validation also prevent a mixed database/WAL
    snapshot when the source changes concurrently.
    """

    # Keep the hardened file-copy implementation shared with profile
    # inspection without introducing an import-time cycle through
    # ``opensquilla.recovery.__init__``.
    from opensquilla.recovery.engine import (
        _copy_source_file_no_follow,
        _regular_source_stat,
        _source_snapshot_is_current,
    )

    bundle = (
        source,
        source.with_name(f"{source.name}-wal"),
        source.with_name(f"{source.name}-journal"),
    )
    present_before = tuple(_regular_source_stat(path) is not None for path in bundle)
    if not present_before[0]:
        raise FileNotFoundError(source)

    with tempfile.TemporaryDirectory(prefix="opensquilla-session-snapshot-") as temporary:
        snapshot_root = Path(temporary)
        snapshots = [
            _copy_source_file_no_follow(path, snapshot_root / path.name)
            for path, exists in zip(bundle, present_before, strict=True)
            if exists
        ]
        present_after = tuple(_regular_source_stat(path) is not None for path in bundle)
        if present_after != present_before or not all(
            _source_snapshot_is_current(snapshot) for snapshot in snapshots
        ):
            raise sqlite3.OperationalError("source sessions database changed during snapshot")
        yield snapshot_root / source.name


def _snapshot_session_database_from_private(source: Path, destination: Path) -> None:
    """Create a SQLite backup from an already-private source bundle."""

    os.makedirs(_native_io_path(destination.parent), exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.snapshot.tmp")
    if os.path.exists(_native_io_path(temporary)):
        raise FileExistsError(f"SQLite snapshot staging already exists: {temporary}")
    try:
        with _read_only_connection(source) as source_connection:
            _quick_check(source_connection, label="source sessions database")
            target_connection = sqlite3.connect(_native_io_path(temporary))
            try:
                source_connection.backup(target_connection)
                _quick_check(target_connection, label="snapshot sessions database")
            finally:
                target_connection.close()
        os.replace(_native_io_path(temporary), _native_io_path(destination))
    finally:
        try:
            os.unlink(_native_io_path(temporary))
        except FileNotFoundError:
            pass


def snapshot_session_database(source: str | Path, destination: str | Path) -> None:
    """Create a WAL-aware SQLite snapshot without modifying the source bundle."""

    source_path = Path(source).expanduser().absolute()
    destination_path = Path(destination).expanduser().absolute()
    with _private_source_snapshot(source_path) as private_source:
        _snapshot_session_database_from_private(private_source, destination_path)


def _normalized_row(
    row: Mapping[str, Any],
    *,
    session_key: str,
    session_id: str,
    included_columns: frozenset[str],
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in row.items():
        if key not in included_columns or key in _FINGERPRINT_IGNORED_FIELDS:
            continue
        if key in _SESSION_KEY_FIELDS and isinstance(value, str):
            normalized[key] = "<session-key>" if value == session_key else "<related-session-key>"
        elif key in _SESSION_ID_FIELDS and isinstance(value, str):
            normalized[key] = "<session-id>" if value == session_id else "<related-session-id>"
        else:
            normalized[key] = value
    return normalized


def _shared_fingerprint_columns(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
) -> dict[str, frozenset[str]]:
    """Return the source fields that the target can actually preserve."""

    shared_by_table: dict[str, frozenset[str]] = {}
    for table in _CORE_TABLES:
        shared = set(_columns(source, table)).intersection(_columns(target, table))
        shared.difference_update(_FINGERPRINT_IGNORED_FIELDS)
        if table == "session_context_states":
            shared.difference_update({"valid", "invalid_reason"})
        if shared:
            shared_by_table[table] = frozenset(shared)
    return shared_by_table


def _session_fingerprint(
    connection: sqlite3.Connection,
    *,
    session_key: str,
    session_id: str,
    included_columns: Mapping[str, frozenset[str]],
) -> str:
    material: list[tuple[str, list[dict[str, Any]]]] = []
    for table in _CORE_TABLES:
        table_columns = included_columns.get(table)
        if not table_columns:
            continue
        columns = _columns(connection, table)
        if not columns:
            continue
        if table == "sessions":
            selected = _rows(connection, table, "session_key = ?", (session_key,))
        elif "session_key" in columns:
            selected = _rows(connection, table, "session_key = ?", (session_key,))
        elif "session_id" in columns:
            selected = _rows(connection, table, "session_id = ?", (session_id,))
        else:
            continue
        normalized = [
            _normalized_row(
                row,
                session_key=session_key,
                session_id=session_id,
                included_columns=table_columns,
            )
            for row in selected
        ]
        normalized.sort(
            key=lambda value: json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        )
        material.append((table, normalized))
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8", "surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


def _source_suffix(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _remapped_session_key(source_key: str, source_id: str) -> str:
    return f"{source_key}:recovered:{_source_suffix(source_id)}"


def _remapped_session_id(source_session_id: str, source_id: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, f"{source_id}\0{source_session_id}"))


def _replace_session_references(
    row: Mapping[str, Any],
    *,
    key_map: Mapping[str, str],
    id_map: Mapping[str, str],
) -> dict[str, Any]:
    replaced = dict(row)
    for column_name in _SESSION_KEY_FIELDS:
        value = replaced.get(column_name)
        if isinstance(value, str) and value in key_map:
            replaced[column_name] = key_map[value]
    for column_name in _SESSION_ID_FIELDS:
        value = replaced.get(column_name)
        if isinstance(value, str) and value in id_map:
            replaced[column_name] = id_map[value]
    return replaced


def _insert(
    connection: sqlite3.Connection,
    table: str,
    row: Mapping[str, Any],
    *,
    omit: frozenset[str] = frozenset(),
) -> int:
    target_columns = set(_columns(connection, table))
    columns = [key for key in row if key in target_columns and key not in omit]
    if not columns:
        raise sqlite3.DatabaseError(f"no compatible columns for {table}")
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {_quote(table)} "
        f"({', '.join(_quote(column) for column in columns)}) "
        f"VALUES ({placeholders})",
        tuple(row[column] for column in columns),
    )
    value = connection.execute("SELECT last_insert_rowid()").fetchone()
    return int(value[0]) if value is not None else 0


def _increment(counts: dict[str, int], table: str) -> None:
    counts[table] = counts.get(table, 0) + 1


def _primary_key_exists(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    value: object,
) -> bool:
    return (
        connection.execute(
            f"SELECT 1 FROM {_quote(table)} WHERE {_quote(column)}=? LIMIT 1",
            (value,),
        ).fetchone()
        is not None
    )


def _unique_text_key(
    connection: sqlite3.Connection,
    *,
    table: str,
    column: str,
    original: str,
    source_id: str,
) -> str:
    if not _primary_key_exists(connection, table, column, original):
        return original
    candidate = f"{original}:recovered:{_source_suffix(source_id)}"
    ordinal = 1
    while _primary_key_exists(connection, table, column, candidate):
        ordinal += 1
        candidate = f"{original}:recovered:{_source_suffix(source_id)}:{ordinal}"
    return candidate


def _copy_core_rows(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    imports: list[_SessionImport],
    *,
    key_map: Mapping[str, str],
    id_map: Mapping[str, str],
    workspace_map: Mapping[str, str | None],
    counts: dict[str, int],
) -> dict[tuple[str, int], int]:
    transcript_ids: dict[tuple[str, int], int] = {}
    for item in imports:
        session_rows = _rows(source, "sessions", "session_key = ?", (item.source_key,))
        if len(session_rows) != 1:
            raise sqlite3.DatabaseError(f"source session is not unique: {item.source_key}")
        row = _replace_session_references(session_rows[0], key_map=key_map, id_map=id_map)
        row["session_key"] = item.target_key
        row["session_id"] = item.target_id
        source_workspace_id = row.get("workspace_id")
        if source_workspace_id is not None:
            row["workspace_id"] = workspace_map.get(str(source_workspace_id))
        _insert(target, "sessions", row)
        _increment(counts, "sessions")

    for item in imports:
        if _table_exists(source, "transcript_entries") and _table_exists(
            target, "transcript_entries"
        ):
            selected = _rows(
                source,
                "transcript_entries",
                "session_key = ?",
                (item.source_key,),
            )
            selected.sort(key=lambda row: int(row.get("id", 0)))
            for source_row in selected:
                row = _replace_session_references(source_row, key_map=key_map, id_map=id_map)
                new_id = _insert(target, "transcript_entries", row, omit=frozenset({"id"}))
                old_id = int(source_row.get("id", 0))
                transcript_ids[(item.source_key, old_id)] = new_id
                _increment(counts, "transcript_entries")

        for table in (
            "compacted_transcript_entries",
            "session_summaries",
            "session_context_states",
        ):
            if not _table_exists(source, table) or not _table_exists(target, table):
                continue
            for source_row in _rows(source, table, "session_key = ?", (item.source_key,)):
                row = _replace_session_references(source_row, key_map=key_map, id_map=id_map)
                if (
                    table == "session_context_states"
                    and "portable" in row
                    and int(row.get("portable") or 0) != 1
                ):
                    if "valid" in row:
                        row["valid"] = 0
                    if "invalid_reason" in row:
                        row["invalid_reason"] = "profile_consolidation"
                if "original_entry_id" in row and row["original_entry_id"] is not None:
                    row["original_entry_id"] = transcript_ids.get(
                        (item.source_key, int(row["original_entry_id"])),
                        row["original_entry_id"],
                    )
                if "covered_through_id" in row:
                    row["covered_through_id"] = transcript_ids.get(
                        (item.source_key, int(row["covered_through_id"] or 0)),
                        row["covered_through_id"],
                    )
                _insert(target, table, row, omit=frozenset({"id"}))
                _increment(counts, table)
    return transcript_ids


def _copy_referenced_workspaces(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    imports: list[_SessionImport],
    *,
    source_id: str,
    counts: dict[str, int],
) -> dict[str, str | None]:
    """Import session-bound path bookmarks without importing host trust."""

    if "workspace_id" not in _columns(source, "sessions"):
        return {}
    source_workspace_ids = {
        str(row["workspace_id"])
        for item in imports
        for row in _rows(source, "sessions", "session_key = ?", (item.source_key,))
        if row.get("workspace_id") is not None
    }
    if not source_workspace_ids:
        return {}
    if not (
        _table_exists(source, "project_workspaces")
        and _table_exists(target, "project_workspaces")
        and "workspace_id" in _columns(target, "sessions")
    ):
        return {workspace_id: None for workspace_id in source_workspace_ids}

    workspace_map: dict[str, str | None] = {}
    for source_workspace_id in sorted(source_workspace_ids):
        rows = _rows(
            source,
            "project_workspaces",
            "workspace_id = ?",
            (source_workspace_id,),
        )
        if len(rows) != 1:
            workspace_map[source_workspace_id] = None
            continue
        row = dict(rows[0])
        path_key = str(row.get("path_key") or "")
        existing_by_path = (
            target.execute(
                "SELECT workspace_id FROM project_workspaces WHERE path_key=?",
                (path_key,),
            ).fetchone()
            if path_key
            else None
        )
        if existing_by_path is not None:
            workspace_map[source_workspace_id] = str(existing_by_path[0])
            continue
        target_workspace_id = _unique_text_key(
            target,
            table="project_workspaces",
            column="workspace_id",
            original=source_workspace_id,
            source_id=source_id,
        )
        row["workspace_id"] = target_workspace_id
        if "trusted_at" in row:
            row["trusted_at"] = None
        _insert(target, "project_workspaces", row)
        _increment(counts, "project_workspaces")
        workspace_map[source_workspace_id] = target_workspace_id
    return workspace_map


def _copy_usage_rows(
    target: sqlite3.Connection,
    source: sqlite3.Connection,
    imports: list[_SessionImport],
    *,
    source_id: str,
    id_map: Mapping[str, str],
    counts: dict[str, int],
) -> None:
    if not _table_exists(source, "usage_events") or not _table_exists(target, "usage_events"):
        return
    imported_ids = {item.source_id for item in imports}
    selected_events: dict[str, dict[str, Any]] = {}
    for session_id in imported_ids:
        for row in _rows(source, "usage_events", "session_id = ?", (session_id,)):
            selected_events[str(row["event_id"])] = row

    event_map: dict[str, str] = {}
    for source_row in selected_events.values():
        row = _replace_session_references(source_row, key_map={}, id_map=id_map)
        original = str(source_row["event_id"])
        event_id = _unique_text_key(
            target,
            table="usage_events",
            column="event_id",
            original=original,
            source_id=source_id,
        )
        event_map[original] = event_id
        row["event_id"] = event_id
        execution_id = row.get("execution_id")
        call_index = row.get("call_index")
        if isinstance(execution_id, str) and call_index is not None:
            collision = target.execute(
                "SELECT 1 FROM usage_events WHERE execution_id=? AND call_index=? LIMIT 1",
                (execution_id, call_index),
            ).fetchone()
            if collision is not None:
                row["execution_id"] = f"{execution_id}:recovered:{_source_suffix(source_id)}"
        _insert(target, "usage_events", row)
        _increment(counts, "usage_events")

    for table in ("usage_event_items", "usage_item_billing_receipts"):
        if not _table_exists(source, table) or not _table_exists(target, table):
            continue
        for original, mapped in event_map.items():
            for source_row in _rows(source, table, "event_id = ?", (original,)):
                row = dict(source_row)
                row["event_id"] = mapped
                _insert(target, table, row)
                _increment(counts, table)

    if _table_exists(source, "usage_legacy_baselines") and _table_exists(
        target, "usage_legacy_baselines"
    ):
        for source_session_id in imported_ids:
            for source_row in _rows(
                source,
                "usage_legacy_baselines",
                "session_id = ?",
                (source_session_id,),
            ):
                row = dict(source_row)
                row["session_id"] = id_map.get(source_session_id, source_session_id)
                _insert(target, "usage_legacy_baselines", row)
                _increment(counts, "usage_legacy_baselines")


def _excluded_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(
        table for table in _EXCLUDED_OPERATIONAL_TABLES if _table_exists(connection, table)
    )


def _snapshot_result(path: Path, source_id: str) -> SessionMergeResult:
    with _read_only_connection(path) as connection:
        imported = (
            int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            if _table_exists(connection, "sessions")
            else 0
        )
        imported_rows = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
            for table in (
                *_CORE_TABLES,
                "usage_events",
                "usage_event_items",
                "usage_item_billing_receipts",
                "usage_legacy_baselines",
            )
            if _table_exists(connection, table)
        }
        return SessionMergeResult(
            source_id=source_id,
            imported_sessions=imported,
            deduplicated_sessions=0,
            imported_rows=imported_rows,
            excluded_tables=_excluded_tables(connection),
        )


def _clear_excluded_operational_rows(path: Path) -> tuple[str, ...]:
    connection = sqlite3.connect(_native_io_path(path))
    try:
        excluded = _excluded_tables(connection)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in (
                "agent_tasks",
                "meta_skill_run_steps",
                "meta_skill_runs",
                "heartbeat_ticks",
                "memory_durable_receipts",
                "router_decisions",
                "telemetry_daily_usage",
                "turn_ingress_receipts",
                "turn_errors",
                "usage_billing_receipt_state",
                "usage_ledger_state",
            ):
                if table in excluded:
                    connection.execute(f"DELETE FROM {_quote(table)}")
            context_columns = _columns(connection, "session_context_states")
            if {"portable", "valid", "invalid_reason"}.issubset(context_columns):
                connection.execute(
                    """
                    UPDATE session_context_states
                    SET valid=0, invalid_reason='profile_consolidation'
                    WHERE COALESCE(portable, 0) != 1
                    """
                )
            if (
                _table_exists(connection, "project_workspaces")
                and "trusted_at" in _columns(connection, "project_workspaces")
            ):
                connection.execute("UPDATE project_workspaces SET trusted_at=NULL")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        _quick_check(connection, label="sanitized sessions snapshot")
        return excluded
    finally:
        connection.close()


def _merge_session_database_from_private(
    target: str | Path,
    source: Path,
    *,
    source_id: str,
    prepare_target_schema: SessionSchemaPreparer,
) -> SessionMergeResult:
    target_path = Path(target).expanduser().absolute()
    source_path = source
    if not os.path.isfile(_native_io_path(source_path)):
        raise FileNotFoundError(source_path)
    if not os.path.exists(_native_io_path(target_path)):
        _snapshot_session_database_from_private(source_path, target_path)
        excluded = _clear_excluded_operational_rows(target_path)
        result = _snapshot_result(target_path, source_id)
        return SessionMergeResult(
            source_id=result.source_id,
            imported_sessions=result.imported_sessions,
            deduplicated_sessions=result.deduplicated_sessions,
            remapped_session_keys=result.remapped_session_keys,
            remapped_session_ids=result.remapped_session_ids,
            imported_rows=result.imported_rows,
            excluded_tables=excluded,
        )

    # Reject corrupt or unrelated inputs before schema migration writes to the
    # existing target.  The checks are repeated below after migration so the
    # row-copy transaction never relies on stale preflight state.
    with (
        _read_only_connection(target_path) as target_preflight,
        _read_only_connection(source_path) as source_preflight,
    ):
        _quick_check(target_preflight, label="target sessions database")
        _quick_check(source_preflight, label="source sessions database")
        if not _table_exists(source_preflight, "sessions"):
            return SessionMergeResult(
                source_id=source_id,
                imported_sessions=0,
                deduplicated_sessions=0,
                excluded_tables=_excluded_tables(source_preflight),
            )
        if not _table_exists(target_preflight, "sessions"):
            raise sqlite3.DatabaseError("target sessions database has no sessions table")
        required_session_columns = {"session_key", "session_id"}
        for label, connection in (
            ("source", source_preflight),
            ("target", target_preflight),
        ):
            missing = required_session_columns.difference(_columns(connection, "sessions"))
            if missing:
                raise sqlite3.DatabaseError(
                    f"{label} sessions table is missing required columns: "
                    f"{', '.join(sorted(missing))}"
                )

    prepare_target_schema(target_path)

    imported_rows: dict[str, int] = {}
    remapped_keys: dict[str, str] = {}
    remapped_ids: dict[str, str] = {}
    resolved_keys: dict[str, str] = {}
    resolved_ids: dict[str, str] = {}
    imports: list[_SessionImport] = []
    deduplicated = 0

    target_connection = sqlite3.connect(_native_io_path(target_path))
    target_connection.row_factory = sqlite3.Row
    try:
        with _read_only_connection(source_path) as source_connection:
            _quick_check(target_connection, label="target sessions database")
            _quick_check(source_connection, label="source sessions database")
            if not _table_exists(source_connection, "sessions"):
                return SessionMergeResult(
                    source_id=source_id,
                    imported_sessions=0,
                    deduplicated_sessions=0,
                    excluded_tables=_excluded_tables(source_connection),
                )
            if not _table_exists(target_connection, "sessions"):
                raise sqlite3.DatabaseError("target sessions database has no sessions table")

            source_sessions = _rows(source_connection, "sessions")
            source_sessions.sort(key=lambda row: str(row["session_key"]))
            target_session_ids = {
                str(row[0])
                for row in target_connection.execute("SELECT session_id FROM sessions").fetchall()
            }
            reserved_keys = {
                str(row[0])
                for row in target_connection.execute("SELECT session_key FROM sessions").fetchall()
            }
            reserved_ids = set(target_session_ids)
            fingerprint_columns = _shared_fingerprint_columns(
                source_connection,
                target_connection,
            )

            for source_row in source_sessions:
                source_key = str(source_row["session_key"])
                source_session_id = str(source_row["session_id"])
                source_fingerprint = _session_fingerprint(
                    source_connection,
                    session_key=source_key,
                    session_id=source_session_id,
                    included_columns=fingerprint_columns,
                )
                existing = target_connection.execute(
                    "SELECT session_id FROM sessions WHERE session_key=?",
                    (source_key,),
                ).fetchone()
                if existing is not None:
                    existing_id = str(existing[0])
                    if (
                        _session_fingerprint(
                            target_connection,
                            session_key=source_key,
                            session_id=existing_id,
                            included_columns=fingerprint_columns,
                        )
                        == source_fingerprint
                    ):
                        resolved_keys[source_key] = source_key
                        resolved_ids[source_session_id] = existing_id
                        if source_session_id != existing_id:
                            remapped_ids[source_session_id] = existing_id
                        deduplicated += 1
                        continue
                    target_key = _remapped_session_key(source_key, source_id)
                    remapped_keys[source_key] = target_key
                else:
                    target_key = source_key

                if target_key in reserved_keys:
                    candidate = target_connection.execute(
                        "SELECT session_id FROM sessions WHERE session_key=?",
                        (target_key,),
                    ).fetchone()
                    if candidate is not None:
                        candidate_id = str(candidate[0])
                        if (
                            _session_fingerprint(
                                target_connection,
                                session_key=target_key,
                                session_id=candidate_id,
                                included_columns=fingerprint_columns,
                            )
                            == source_fingerprint
                        ):
                            remapped_keys[source_key] = target_key
                            remapped_ids[source_session_id] = candidate_id
                            resolved_keys[source_key] = target_key
                            resolved_ids[source_session_id] = candidate_id
                            deduplicated += 1
                            continue
                    target_key = f"{target_key}:{source_fingerprint[:8]}"
                    ordinal = 1
                    while target_key in reserved_keys:
                        ordinal += 1
                        target_key = (
                            f"{_remapped_session_key(source_key, source_id)}:"
                            f"{source_fingerprint[:8]}:{ordinal}"
                        )
                    remapped_keys[source_key] = target_key

                # Renumber only on a real identifier collision. A colliding
                # session KEY does not require a new id: `session_key` is the
                # primary key while `session_id` carries no unique constraint and
                # no reverse lookup, so the imported session stays addressable
                # under its original id. That matters because several stores are
                # keyed by session id on disk — artifacts under
                # `media/artifacts/s/<sha256(session_id)>/`, tool results under
                # `media/tool-results/s/<session_id>/`, transcript material under
                # `media/transcripts/<session_id>/` — and each also records the id
                # inside its metadata and rejects a mismatch on read. Renumbering
                # a session whose id never collided would strand every one of the
                # user's recovered attachments and generated files.
                if source_session_id in reserved_ids:
                    target_session_id = _remapped_session_id(source_session_id, source_id)
                    ordinal = 1
                    while target_session_id in reserved_ids:
                        ordinal += 1
                        target_session_id = str(
                            uuid.uuid5(
                                _UUID_NAMESPACE,
                                f"{source_id}\0{source_session_id}\0{ordinal}",
                            )
                        )
                    remapped_ids[source_session_id] = target_session_id
                else:
                    target_session_id = source_session_id
                reserved_keys.add(target_key)
                reserved_ids.add(target_session_id)
                imports.append(
                    _SessionImport(
                        source_key=source_key,
                        source_id=source_session_id,
                        target_key=target_key,
                        target_id=target_session_id,
                    )
                )

            key_map = {
                **resolved_keys,
                **{item.source_key: item.target_key for item in imports},
            }
            id_map = {
                **resolved_ids,
                **{item.source_id: item.target_id for item in imports},
            }
            target_connection.execute("PRAGMA foreign_keys=OFF")
            target_connection.execute("BEGIN IMMEDIATE")
            try:
                workspace_map = _copy_referenced_workspaces(
                    target_connection,
                    source_connection,
                    imports,
                    source_id=source_id,
                    counts=imported_rows,
                )
                _copy_core_rows(
                    target_connection,
                    source_connection,
                    imports,
                    key_map=key_map,
                    id_map=id_map,
                    workspace_map=workspace_map,
                    counts=imported_rows,
                )
                _copy_usage_rows(
                    target_connection,
                    source_connection,
                    imports,
                    source_id=source_id,
                    id_map=id_map,
                    counts=imported_rows,
                )
                _quick_check(target_connection, label="merged sessions database")
                target_connection.commit()
            except BaseException:
                target_connection.rollback()
                raise

            return SessionMergeResult(
                source_id=source_id,
                imported_sessions=len(imports),
                deduplicated_sessions=deduplicated,
                remapped_session_keys=remapped_keys,
                remapped_session_ids=remapped_ids,
                imported_rows=imported_rows,
                excluded_tables=_excluded_tables(source_connection),
            )
    finally:
        target_connection.close()


def merge_session_database(
    target: str | Path,
    source: str | Path,
    *,
    source_id: str,
    prepare_target_schema: SessionSchemaPreparer,
) -> SessionMergeResult:
    """Merge one offline source into a primary ``sessions.db``.

    A missing target is created with SQLite's backup API, preserving every
    source schema object and committed WAL page.  Existing targets receive the
    supported session graph row-by-row.  Exact conversation duplicates are
    ignored; divergent session-key collisions get deterministic key and ID
    remaps so both conversations remain addressable.

    SQLite only opens a stable private copy.  The recovery source bundle is
    never opened by SQLite and therefore cannot gain a transient ``-shm`` file.
    The upper-layer caller supplies schema preparation so this offline package
    does not import the runtime session or persistence packages back.
    """

    source_path = Path(source).expanduser().absolute()
    with _private_source_snapshot(source_path) as private_source:
        return _merge_session_database_from_private(
            target,
            private_source,
            source_id=source_id,
            prepare_target_schema=prepare_target_schema,
        )


__all__ = [
    "SessionMergeResult",
    "SessionSchemaPreparer",
    "merge_session_database",
    "snapshot_session_database",
]
