"""Diagnostic observations preserve locks, connection lifetimes and private data."""

from __future__ import annotations

import gc
import json
import sqlite3
import weakref

import pytest

from opensquilla.persistence import sqlite_diagnostics as diagnostics
from opensquilla.persistence.router_decision_writer import open_router_decision_writer
from opensquilla.persistence.turn_error_writer import open_turn_error_writer


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(diagnostics, "_connections", [])
    monkeypatch.setattr(diagnostics, "_omitted_registrations", 0, raising=False)
    monkeypatch.setenv("OPENSQUILLA_STORAGE_DIAGNOSTICS", "1")


class ConnectionOwner:
    def __init__(self, connection):
        self._conn = connection


class OpenConnectionState:
    in_transaction = False


class ActiveConnectionState:
    in_transaction = True


def test_diagnostics_are_opt_in_and_do_not_extend_owner_lifetimes(tmp_path, monkeypatch):
    path = str(tmp_path / "synthetic.sqlite")
    owner = ConnectionOwner(None)
    monkeypatch.delenv("OPENSQUILLA_STORAGE_DIAGNOSTICS")
    diagnostics.register_connection(path, "session_writer", owner, attribute="_conn")
    assert not diagnostics.connection_snapshot(path)["connections"]
    monkeypatch.setenv("OPENSQUILLA_STORAGE_DIAGNOSTICS", "1")
    diagnostics.register_connection(path, "session_writer", owner, attribute="_conn")
    reference = weakref.ref(owner)
    del owner
    gc.collect()
    assert reference() is None
    assert not diagnostics.connection_snapshot(path)["connections"]


def test_sync_writers_are_observed_without_sql_or_private_paths(tmp_path):
    path = str(tmp_path / "private-synthetic-database.sqlite")
    other_path = str(tmp_path / "another.sqlite")
    router = open_router_decision_writer(path)
    errors = open_turn_error_writer(path)
    other = open_router_decision_writer(other_path)
    statements = []
    try:
        router._conn.set_trace_callback(statements.append)
        errors._conn.set_trace_callback(statements.append)
        router._conn.execute("BEGIN IMMEDIATE").close()
        statements.clear()
        snapshot = diagnostics.connection_snapshot(path)
        assert statements == [], "diagnostics must not execute SQL"
        assert router._conn.in_transaction, "observation must not release the writer slot"
        rows = {row["role"]: row for row in snapshot["connections"]}
        assert rows["router_writer"]["connection_in_transaction"] is True
        assert rows["error_writer"]["connection_in_transaction"] is False
        assert rows["router_writer"]["backend"] == "sqlite3"
        assert rows["router_writer"]["status"] == "open"
        assert len(snapshot["connections"]) == 2
        assert path not in json.dumps(snapshot)
        assert "private-synthetic-database" not in json.dumps(snapshot)
        router._conn.rollback()
        router.close()
        closed = next(row for row in diagnostics.connection_snapshot(path)["connections"]
                      if row["role"] == "router_writer")
        assert closed["status"] == "closed"
        assert closed["connection_in_transaction"] is None
    finally:
        router.close()
        errors.close()
        other.close()


def test_snapshot_is_bounded_and_rejects_non_fixed_roles(tmp_path):
    path = str(tmp_path / "bounded.sqlite")
    owners = [ConnectionOwner(None) for _ in range(20)]
    for owner in owners:
        diagnostics.register_connection(path, "recovery_reader", owner, attribute="_conn")
    diagnostics.register_connection(path, "private role text", ConnectionOwner(None))
    snapshot = diagnostics.connection_snapshot(path)
    assert len(snapshot["connections"]) == 16
    assert snapshot["connections_truncated"] is True
    assert "private role" not in json.dumps(snapshot)
    assert len({row["connection_id"] for row in snapshot["connections"]}) == 16


def test_closed_readers_do_not_hide_a_current_connection_or_active_writer(tmp_path):
    path = str(tmp_path / "synthetic-active-writer.sqlite")
    closed = []
    for _ in range(16):
        connection = sqlite3.connect(path, isolation_level=None)
        connection.close()
        owner = ConnectionOwner(connection)
        closed.append(owner)
        diagnostics.register_connection(path, "recovery_reader", owner, attribute="_conn")
    current = ConnectionOwner(sqlite3.connect(path, isolation_level=None, timeout=0))
    holder = ConnectionOwner(sqlite3.connect(path, isolation_level=None))
    try:
        current._conn.execute("PRAGMA journal_mode=WAL").close()
        holder._conn.execute("BEGIN IMMEDIATE").close()
        diagnostics.register_connection(path, "session_writer", current, attribute="_conn")
        diagnostics.register_connection(path, "content_length_writer", holder, attribute="_conn")
        snapshot = diagnostics.connection_snapshot(path)
        rows = {row["role"]: row for row in snapshot["connections"]}
        assert rows["content_length_writer"]["connection_in_transaction"] is True
        assert rows["session_writer"]["connection_in_transaction"] is False
        assert snapshot["connections_observed"] == 18
        assert snapshot["connections_omitted"] == 2
        assert snapshot["connections_truncated"] is True
        current_snapshot = diagnostics.connection_snapshot(path, current_connection=current._conn)
        assert current_snapshot["connections"][0]["role"] == "session_writer"
        assert current_snapshot["connections"][1]["role"] == "content_length_writer"
        assert holder._conn.in_transaction
        with pytest.raises(sqlite3.OperationalError):
            current._conn.execute("BEGIN IMMEDIATE").close()
    finally:
        holder._conn.rollback()
        holder._conn.close()
        current._conn.close()


def test_registry_capacity_preserves_new_writers_and_reports_omissions(tmp_path):
    path = str(tmp_path / "synthetic-capacity.sqlite")
    closed = [ConnectionOwner(None) for _ in range(100)]
    for owner in closed:
        diagnostics.register_connection(path, "recovery_reader", owner, attribute="_conn")
    holder = ConnectionOwner(sqlite3.connect(path, isolation_level=None))
    try:
        holder._conn.execute("BEGIN IMMEDIATE").close()
        diagnostics.register_connection(path, "content_length_writer", holder, attribute="_conn")
        assert len(diagnostics._connections) == 64
        snapshot = diagnostics.connection_snapshot(path, current_connection=holder._conn)
        assert snapshot["connections"][0]["role"] == "content_length_writer"
        assert snapshot["connections"][0]["connection_in_transaction"] is True
        assert snapshot["connections_observed"] == 64
        assert snapshot["connections_omitted"] == 48
        assert snapshot["registry_truncated"] is True
        assert snapshot["process_registry_omitted"] == 37
        assert snapshot["process_registry_capacity"] == 64
    finally:
        holder._conn.rollback()
        holder._conn.close()


def test_full_open_registry_rejects_closed_metadata_and_prioritizes_current(tmp_path):
    path = str(tmp_path / "synthetic-open-capacity.sqlite")
    owners = [ConnectionOwner(OpenConnectionState()) for _ in range(64)]
    for owner in owners:
        diagnostics.register_connection(path, "router_writer", owner, attribute="_conn")
    first_id = diagnostics._connections[0].connection_id
    closed = ConnectionOwner(None)
    diagnostics.register_connection(path, "recovery_reader", closed, attribute="_conn")
    assert len(diagnostics._connections) == 64
    assert not any(entry.owner() is closed for entry in diagnostics._connections)
    snapshot = diagnostics.connection_snapshot(path, current_connection=owners[0]._conn)
    assert snapshot["connections"][0]["connection_id"] == first_id
    assert snapshot["connections_observed"] == 64
    assert snapshot["connections_omitted"] == 48
    assert snapshot["process_registry_omitted"] == 1
    assert snapshot["registry_truncated"] is True


def test_current_canonical_connection_is_visible_after_cross_database_capacity_loss(tmp_path):
    path = str(tmp_path / "synthetic-current.sqlite")
    current = ConnectionOwner(OpenConnectionState())
    diagnostics.register_connection(path, "session_writer", current, attribute="_conn")
    owners = [ConnectionOwner(ActiveConnectionState()) for _ in range(64)]
    for index, owner in enumerate(owners):
        diagnostics.register_connection(str(tmp_path / f"other-{index}.sqlite"), "session_writer",
                                        owner, attribute="_conn")
    assert len(diagnostics._connections) == 64
    snapshot = diagnostics.connection_snapshot(path, current_connection=current._conn)
    assert snapshot["connections"] == [{
        "connection_id": None, "role": "session_writer", "backend": "unknown",
        "status": "open", "connection_in_transaction": False,
    }]
    assert snapshot["current_unregistered"] is True
    assert snapshot["connections_observed"] == 1
    assert snapshot["connections_omitted"] == 0
    assert snapshot["connections_truncated"] is False
    assert snapshot["registry_truncated"] is True
    assert snapshot["process_registry_omitted"] == 1


def test_memory_databases_and_non_weakrefable_handles_are_not_retained():
    connection = sqlite3.connect(":memory:")
    try:
        owner = ConnectionOwner(connection)
        diagnostics.register_connection(":memory:", "session_writer", owner, attribute="_conn")
        diagnostics.register_connection("synthetic.sqlite", "session_writer", connection)
        assert diagnostics.connection_snapshot(":memory:")["connections"] == []
        assert diagnostics.connection_snapshot("synthetic.sqlite")["connections"] == []
    finally:
        connection.close()


@pytest.mark.parametrize("code,name", [
    (sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
    (sqlite3.SQLITE_BUSY_SNAPSHOT, "SQLITE_BUSY_SNAPSHOT"),
    (sqlite3.SQLITE_LOCKED, "SQLITE_LOCKED"),
])
def test_original_sqlite_code_survives_wrapping_without_error_prose(code, name):
    original = sqlite3.OperationalError("private exception text and database path")
    original.sqlite_errorcode = code
    original.sqlite_errorname = name
    wrapped = RuntimeError("private wrapper text")
    wrapped.__cause__ = original
    result = diagnostics.sqlite_failure(wrapped)
    assert result == {
        "sqlite_error_code": code, "sqlite_primary_code": code & 0xFF,
        "sqlite_error_name": name,
    }
    assert "private" not in json.dumps(result)


def test_error_name_is_verified_and_cause_cycles_are_bounded():
    original = sqlite3.OperationalError("private exception")
    original.sqlite_errorcode = sqlite3.SQLITE_BUSY
    original.sqlite_errorname = "SQLITE_PRIVATE_PAYLOAD"
    assert diagnostics.sqlite_failure(original)["sqlite_error_name"] is None
    original.sqlite_errorname = "SQLITE_LOCKED"
    assert diagnostics.sqlite_failure(original)["sqlite_error_name"] is None
    first, second = RuntimeError(), RuntimeError()
    first.__cause__, second.__cause__ = second, first
    assert diagnostics.sqlite_failure(first) == {}
