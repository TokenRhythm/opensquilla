from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from opensquilla.application.approval_queue import ApprovalQueue


@pytest.mark.parametrize("operation", ["get", "list_pending"])
@pytest.mark.parametrize("claim", ["none", "live", "expired", "missing_start"])
def test_query_preserves_claim_ttl_without_empty_write(tmp_path, monkeypatch, operation, claim):
    monkeypatch.setattr("opensquilla.application.approval_queue.time.time", lambda: 1000.0)
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.db"), claim_ttl_seconds=60)
    approval_id = queue.request("exec", {})
    started = {"live": 990.0, "expired": 940.0, "missing_start": None}
    if claim != "none":
        queue._conn.execute(
            "UPDATE approval_queue SET claim_token = 'claim', claim_started_at = ? "
            "WHERE approval_id = ?",
            (started[claim], approval_id),
        )
    sql: list[str] = []
    queue._conn.set_trace_callback(sql.append)
    try:
        result = (
            getattr(queue, operation)(approval_id) if operation == "get" else queue.list_pending()
        )
        if operation == "get":
            assert result.claim_token == ("claim" if claim == "live" else None)
            assert result.resolved is False
        else:
            assert [row["id"] for row in result] == ([] if claim == "live" else [approval_id])
        writes = [query for query in sql if query.startswith(("BEGIN", "UPDATE", "COMMIT"))]
        assert len(writes) == (3 if claim in {"expired", "missing_start"} else 0)
        row = queue._conn.execute(
            "SELECT claim_token, claim_started_at FROM approval_queue WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        assert tuple(row) == (("claim", 990.0) if claim == "live" else (None, None))
        assert not queue._conn.in_transaction
    finally:
        queue.close()


def test_empty_cleanup_check_is_not_cached_across_queries(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("opensquilla.application.approval_queue.time.time", lambda: now[0])
    path = str(tmp_path / "approvals.db")
    queue = ApprovalQueue(db_path=path, claim_ttl_seconds=60)
    other = ApprovalQueue(db_path=path, claim_ttl_seconds=60)
    approval_id = queue.request("exec", {})
    try:
        assert queue.get(approval_id).claim_token is None
        other.claim_resolution(approval_id)
        assert queue.get(approval_id).claim_token is not None
        now[0] += 61
        assert queue.get(approval_id).claim_token is None
    finally:
        queue.close()
        other.close()


@pytest.mark.parametrize("operation", ["get", "list_pending"])
def test_stale_claim_on_resolved_approval_is_preserved(tmp_path, monkeypatch, operation):
    monkeypatch.setattr("opensquilla.application.approval_queue.time.time", lambda: 1000.0)
    queue = ApprovalQueue(db_path=str(tmp_path / "approvals.db"), claim_ttl_seconds=60)
    approval_id = queue.request("exec", {})
    queue._conn.execute(
        "UPDATE approval_queue SET resolved = 1, claim_token = 'finalizing', "
        "claim_started_at = 0 WHERE approval_id = ?",
        (approval_id,),
    )
    sql: list[str] = []
    queue._conn.set_trace_callback(sql.append)
    try:
        if operation == "get":
            entry = queue.get(approval_id)
            assert entry.resolved is True
            assert entry.claim_token == "finalizing"
        else:
            assert queue.list_pending() == []
        assert not any(query.startswith(("BEGIN", "UPDATE", "COMMIT")) for query in sql)
        assert queue._get_row(approval_id)["claim_token"] == "finalizing"
    finally:
        queue.close()


def test_cleanup_rechecks_claim_renewed_before_writer_acquisition(tmp_path, monkeypatch):
    monkeypatch.setattr("opensquilla.application.approval_queue.time.time", lambda: 1000.0)
    path = str(tmp_path / "approvals.db")
    queue = ApprovalQueue(db_path=path, claim_ttl_seconds=60)
    approval_id = queue.request("exec", {})
    queue._conn.execute(
        "UPDATE approval_queue SET claim_token = 'expired', claim_started_at = 940 "
        "WHERE approval_id = ?",
        (approval_id,),
    )
    with sqlite3.connect(path, isolation_level=None) as other:
        renewed = []

        def before_begin(sql):
            if sql == "BEGIN IMMEDIATE" and not renewed:
                other.execute(
                    "UPDATE approval_queue SET claim_token = 'renewed', claim_started_at = 1000 "
                    "WHERE approval_id = ?",
                    (approval_id,),
                )
                renewed.append(True)

        queue._conn.set_trace_callback(before_begin)
        try:
            entry = queue.get(approval_id)
            assert renewed == [True]
            assert entry.claim_token == "renewed"
            assert entry.claim_started_at == 1000.0
            assert not queue._conn.in_transaction
        finally:
            queue.close()


@pytest.mark.parametrize("operation", ["get", "list_pending"])
@pytest.mark.parametrize("live_claim", [False, True])
async def test_empty_cleanup_query_finishes_while_other_writer_is_held(
    tmp_path,
    operation,
    live_claim,
):
    path = tmp_path / "approvals.db"
    queue = ApprovalQueue(db_path=str(path))
    approval_id = queue.request("exec", {})
    if live_claim:
        queue.claim_resolution(approval_id)
    locked = threading.Event()
    release = threading.Event()
    errors = []

    def hold_writer():
        try:
            with sqlite3.connect(path, timeout=3) as connection:
                connection.execute("BEGIN IMMEDIATE")
                locked.set()
                assert release.wait(5), "query failed to finish before writer release"
                connection.rollback()
        except BaseException as exc:
            errors.append(exc)
            locked.set()

    holder = threading.Thread(target=hold_writer)
    holder.start()
    reader = None
    try:
        assert await asyncio.to_thread(locked.wait, 3)
        assert not errors
        read = queue.get_async(approval_id) if operation == "get" else queue.list_pending_async()
        reader = asyncio.create_task(read)
        result = await asyncio.wait_for(asyncio.shield(reader), timeout=2)
        assert not release.is_set()
        if operation == "get":
            assert result.approval_id == approval_id
        else:
            assert len(result) == (0 if live_claim else 1)
    finally:
        release.set()
        await asyncio.to_thread(holder.join, 5)
        if reader is not None:
            await reader
        queue.close()
        assert not holder.is_alive()
        assert not errors
