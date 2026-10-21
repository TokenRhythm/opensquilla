"""Opt-in, content-free observations of known in-process SQLite connections.

These snapshots inspect connection state only. They do not execute SQL, open
database files, change handlers, or establish which OS process owns a lock.
Weak owners ensure diagnostics cannot keep a connection alive.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import weakref
from dataclasses import dataclass
from typing import Any

_ROLES = frozenset({
    "session_writer", "transcript_reader", "recovery_reader", "committed_reader",
    "usage_index_writer", "content_length_writer", "router_writer", "error_writer",
})
_MAX_CONNECTIONS = 16
_MAX_REGISTRATIONS = 64
_lock = threading.Lock()
_sequence = 0
_omitted_registrations = 0


@dataclass(frozen=True, slots=True)
class _Registration:
    database: str
    role: str
    owner: weakref.ReferenceType[Any]
    attribute: str | None
    connection_id: int


_connections: list[_Registration] = []


def _database_key(path: str) -> str:
    # Used for matching only; never emitted. Memory databases cannot share a
    # file lock and are excluded rather than conflating unrelated connections.
    return "" if path == ":memory:" else os.path.normcase(os.path.abspath(path))


def connection_backend(connection: Any) -> str:
    module = type(connection).__module__
    if module == "sqlite3":
        return "sqlite3"
    if module == "opensquilla.compat.aiosqlite":
        return "sqlite3_fallback"
    if module.startswith("aiosqlite."):
        return "aiosqlite"
    return "unknown"


def _connection_state(
    connection: Any, role: str, connection_id: int | None, current_connection: Any = None,
) -> tuple[tuple[int, int, int], dict[str, Any]]:
    backend = "unknown"
    try:
        backend = connection_backend(connection)
        in_transaction = bool(connection.in_transaction) if connection is not None else None
        status = "open" if connection is not None else "closed"
    except (sqlite3.ProgrammingError, ValueError):
        in_transaction, status = None, "closed"
    except Exception:  # noqa: BLE001 - observation must not replace the original failure
        in_transaction, status = None, "unavailable"
    if connection is not None and connection is current_connection:
        rank = 0
    elif status == "open":
        rank = 1 if role.endswith("writer") else 2
    else:
        rank = 3 if status == "unavailable" else 4
    # Prefer active transactions and newer observations within each category.
    priority = (rank, 0 if in_transaction else 1, -(connection_id or 0))
    return priority, {
        "connection_id": connection_id, "role": role,
        "backend": backend, "status": status,
        "connection_in_transaction": in_transaction,
    }


def _observe_connection(
    entry: _Registration, current_connection: Any = None,
) -> tuple[tuple[int, int, int], dict[str, Any]] | None:
    owner = entry.owner()
    if owner is None:
        return None
    try:
        connection = getattr(owner, entry.attribute, None) if entry.attribute else owner
    except Exception:  # noqa: BLE001 - inaccessible owners are diagnostic gaps
        return (3, 1, -entry.connection_id), {
            "connection_id": entry.connection_id, "role": entry.role, "backend": "unknown",
            "status": "unavailable", "connection_in_transaction": None,
        }
    return _connection_state(connection, entry.role, entry.connection_id, current_connection)


def register_connection(
    path: str, role: str, owner: Any, *, attribute: str | None = None,
) -> None:
    """Register a weak connection or an owner of a non-weakrefable sqlite3 handle."""

    if os.environ.get("OPENSQUILLA_STORAGE_DIAGNOSTICS") != "1" or role not in _ROLES:
        return
    database = _database_key(path)
    if not database:
        return
    try:
        reference = weakref.ref(owner)
    except TypeError:
        return
    global _sequence, _omitted_registrations  # noqa: PLW0603
    with _lock:
        _connections[:] = [entry for entry in _connections if entry.owner() is not None]
        if any(entry.owner() is owner and entry.attribute == attribute
               and entry.database == database and entry.role == role for entry in _connections):
            return
        _sequence += 1
        incoming = _Registration(database, role, reference, attribute, _sequence)
        if len(_connections) == _MAX_REGISTRATIONS:
            # At the hard limit, retain open writers ahead of closed owners.
            # All inspection is bounded and uses state properties only.
            observed = [(entry, _observe_connection(entry)) for entry in _connections]
            _connections[:] = [entry for entry, state in observed if state is not None]
            if len(_connections) == _MAX_REGISTRATIONS:
                worst, state = max(
                    ((entry, state) for entry, state in observed if state is not None),
                    key=lambda item: item[1][0],
                )
                incoming_state = _observe_connection(incoming)
                _omitted_registrations += 1
                if incoming_state is None or incoming_state[0] > state[0]:
                    return
                _connections.remove(worst)
        _connections.append(incoming)


def connection_snapshot(path: str, *, current_connection: Any = None) -> dict[str, Any]:
    """Return bounded state for known connections to the same database file."""

    database = _database_key(path)
    with _lock:
        _connections[:] = [entry for entry in _connections if entry.owner() is not None]
        entries = [entry for entry in _connections if entry.database == database]
        omitted_registrations = _omitted_registrations
    observations = [state for entry in entries
                    if (state := _observe_connection(entry, current_connection)) is not None]
    current_unregistered = current_connection is not None and not any(
        priority[0] == 0 for priority, _row in observations
    )
    if current_unregistered:
        # The caller's canonical connection remains visible even if a full
        # process registry dropped it. A null ID declares that missing record.
        observations.append(_connection_state(
            current_connection, "session_writer", None, current_connection,
        ))
    observations.sort(key=lambda state: state[0])
    rows = [state[1] for state in observations[:_MAX_CONNECTIONS]]
    omitted = len(observations) - len(rows)
    return {
        "connections": rows,
        "connections_observed": len(observations),
        "connections_omitted": omitted,
        "connections_truncated": omitted > 0,
        "current_unregistered": current_unregistered,
        # This cumulative counter is process-wide, including other databases;
        # it must not be mistaken for this snapshot's omitted connection count.
        "process_registry_omitted": omitted_registrations,
        "process_registry_capacity": _MAX_REGISTRATIONS,
        "registry_truncated": omitted_registrations > 0,
        "process_id": os.getpid(),
        "sqlite_version": sqlite3.sqlite_version,
    }


def sqlite_failure(error: BaseException | None) -> dict[str, Any]:
    """Project integer SQLite codes and a verified stdlib name, never error prose."""

    seen: set[int] = set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        code = getattr(error, "sqlite_errorcode", None)
        if isinstance(code, int) and not isinstance(code, bool) and 0 <= code < 2**31:
            name = getattr(error, "sqlite_errorname", None)
            safe_name = name if isinstance(name, str) and name.startswith("SQLITE_") and (
                getattr(sqlite3, name, None) == code
            ) else None
            return {
                "sqlite_error_code": code,
                "sqlite_primary_code": code & 0xFF,
                "sqlite_error_name": safe_name,
            }
        error = error.__cause__
    return {}
