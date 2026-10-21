from __future__ import annotations

import sqlite3

import pytest

from opensquilla.sandbox.policy_store import PolicyVersionConflict, SandboxPolicyStore


def _track_connections(monkeypatch, *, fail_on=None):
    connections = []

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

        def execute(self, sql, *args, **kwargs):
            if fail_on and fail_on in sql:
                raise sqlite3.OperationalError("synthetic statement failure")
            return super().execute(sql, *args, **kwargs)

    def connect(store):
        connection = sqlite3.connect(
            store._path, isolation_level=None, factory=TrackedConnection,
        )
        connection.row_factory = sqlite3.Row
        # Strong references ensure GC cannot hide a missing close().
        connections.append(connection)
        return connection

    monkeypatch.setattr(SandboxPolicyStore, "_connect", connect)
    return connections


def test_policy_connections_close_before_constructor_read_and_cas_return(tmp_path, monkeypatch):
    connections = _track_connections(monkeypatch)
    store = SandboxPolicyStore(tmp_path / "sessions.db")
    assert len(connections) == 1 and all(conn.closed for conn in connections)
    policy = store.read()
    assert len(connections) == 2 and all(conn.closed for conn in connections)
    saved = store.compare_and_swap(policy.policy_version, policy)
    assert saved.policy_version == 1
    assert len(connections) == 3 and all(conn.closed for conn in connections)
    with pytest.raises(PolicyVersionConflict):
        store.compare_and_swap(policy.policy_version, policy)
    assert len(connections) == 4 and all(conn.closed for conn in connections)
    SandboxPolicyStore(tmp_path / "sessions.db")
    assert len(connections) == 5 and all(conn.closed for conn in connections)


@pytest.mark.parametrize("operation", ["schema", "read", "write"])
def test_policy_connections_close_when_sql_fails(tmp_path, monkeypatch, operation):
    path = tmp_path / "sessions.db"
    store = SandboxPolicyStore(path)
    policy = store.read()
    fail_on = {
        "schema": "SELECT 1 FROM sqlite_master",
        "read": "SELECT policy_version, policy_json",
        "write": "UPDATE sandbox_policy",
    }[operation]
    connections = _track_connections(monkeypatch, fail_on=fail_on)
    with pytest.raises(sqlite3.OperationalError, match="synthetic statement"):
        if operation == "schema":
            SandboxPolicyStore(path)
        elif operation == "read":
            store.read()
        else:
            store.compare_and_swap(policy.policy_version, policy)
    assert len(connections) == 1 and all(conn.closed for conn in connections)
