"""DAO tests for clarify state on MetaRunWriter (PR2)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from opensquilla.persistence.meta_run_writer import (
    AwaitingPeek,
    MetaRunWriter,
)


@pytest.fixture
def writer(migrated_db: Path) -> Iterator[MetaRunWriter]:
    conn = sqlite3.connect(migrated_db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    result = MetaRunWriter(conn)
    yield result
    result.close()


def _seed_awaiting(writer: MetaRunWriter, *, run_id: str, session_key: str,
                   step_id: str = "collect", since: float = 1700000000.0) -> None:
    schema_json = json.dumps({"mode": "form", "fields": []})
    with writer._lock:
        writer._conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json, "
            " awaiting_step_id, awaiting_schema_json, awaiting_since, "
            " awaiting_filled_json, step_outputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, "tskill", "d", "{}", "soft_meta_invoke", session_key,
             "awaiting_user", 0, "{}", step_id, schema_json, since,
             "{}", "{}"),
        )
        writer._conn.commit()


def test_peek_awaiting_returns_none_for_unknown_session(writer):
    assert writer.peek_awaiting(session_id="nope") is None


def test_peek_awaiting_returns_record_for_matching_session(writer):
    _seed_awaiting(writer, run_id="r1", session_key="S1")
    peek = writer.peek_awaiting(session_id="S1")
    assert peek is not None
    assert isinstance(peek, AwaitingPeek)
    assert peek.run_id == "r1"
    assert peek.step_id == "collect"
    assert peek.awaiting_session_id == "S1"


def test_peek_awaiting_ignores_non_awaiting_status(writer):
    with writer._lock:
        writer._conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r2", "tskill", "d", "{}", "soft_meta_invoke", "S2",
             "ok", 0, "{}"),
        )
        writer._conn.commit()
    assert writer.peek_awaiting(session_id="S2") is None


def test_try_claim_awaiting_succeeds_for_running_run(writer):
    with writer._lock:
        writer._conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r1", "t", "d", "{}", "soft_meta_invoke", "S1", "running", 0, "{}"),
        )
        writer._conn.commit()

    ok = writer.try_claim_awaiting(
        run_id="r1",
        step_id="collect",
        schema_json='{"mode":"form","fields":[]}',
        session_id="S1",
        inputs_json='{"user_message":"hi"}',
        step_outputs_json='{}',
        awaiting_since=1700000000.0,
    )
    assert ok is True

    peek = writer.peek_awaiting(session_id="S1")
    assert peek is not None
    assert peek.run_id == "r1"
    assert peek.step_id == "collect"


def test_try_claim_awaiting_fails_when_run_already_finished(writer):
    with writer._lock:
        writer._conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r1", "t", "d", "{}", "soft_meta_invoke", "S1", "ok", 0, "{}"),
        )
        writer._conn.commit()

    ok = writer.try_claim_awaiting(
        run_id="r1",
        step_id="collect",
        schema_json="{}",
        session_id="S1",
        inputs_json="{}",
        step_outputs_json="{}",
        awaiting_since=0.0,
    )
    assert ok is False
    assert writer.peek_awaiting(session_id="S1") is None


def test_try_claim_awaiting_partial_unique_index_blocks_double_awaiting(writer):
    with writer._lock:
        for run_id in ("r1", "r2"):
            writer._conn.execute(
                "INSERT INTO meta_skill_runs "
                "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
                " triggered_by, session_key, status, started_at_ms, inputs_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, "t", "d", "{}", "soft_meta_invoke", "S1",
                 "running", 0, "{}"),
            )
        writer._conn.commit()

    assert writer.try_claim_awaiting(
        run_id="r1", step_id="collect", schema_json="{}",
        session_id="S1", inputs_json="{}", step_outputs_json="{}",
        awaiting_since=0.0,
    ) is True
    assert writer.try_claim_awaiting(
        run_id="r2", step_id="collect", schema_json="{}",
        session_id="S1", inputs_json="{}", step_outputs_json="{}",
        awaiting_since=0.0,
    ) is False


def test_try_claim_resume_wins_on_first_call(writer):
    _seed_awaiting(writer, run_id="r1", session_key="S1")
    payload = writer.try_claim_resume(run_id="r1", session_id="S1")
    assert payload is not None
    assert payload.run_id == "r1"
    assert payload.awaiting_step_id == "collect"
    assert payload.inputs_json == "{}"
    peek_after = writer.peek_awaiting(session_id="S1")
    assert peek_after is None


def test_try_claim_resume_loses_when_already_consumed(writer):
    _seed_awaiting(writer, run_id="r1", session_key="S1")
    first = writer.try_claim_resume(run_id="r1", session_id="S1")
    assert first is not None
    second = writer.try_claim_resume(run_id="r1", session_id="S1")
    assert second is None


def test_try_claim_resume_rejects_session_mismatch(writer):
    _seed_awaiting(writer, run_id="r1", session_key="S1")
    payload = writer.try_claim_resume(run_id="r1", session_id="DIFFERENT")
    assert payload is None
    assert writer.peek_awaiting(session_id="S1") is not None


def test_mark_expired_moves_awaiting_to_expired(writer):
    _seed_awaiting(writer, run_id="r1", session_key="S1")
    writer.mark_expired(run_id="r1")
    assert writer.peek_awaiting(session_id="S1") is None
    with writer._lock:
        row = writer._conn.execute(
            "SELECT status FROM meta_skill_runs WHERE run_id=?",
            ("r1",),
        ).fetchone()
    assert row["status"] == "expired"


def test_mark_cancelled_records_reason_in_error_column(writer):
    _seed_awaiting(writer, run_id="r1", session_key="S1")
    writer.mark_cancelled(run_id="r1", reason="user_cancel")
    with writer._lock:
        row = writer._conn.execute(
            "SELECT status, error FROM meta_skill_runs WHERE run_id=?",
            ("r1",),
        ).fetchone()
    assert row["status"] == "cancelled"
    assert "user_cancel" in (row["error"] or "")


def test_increment_parse_failures_returns_new_count(writer):
    _seed_awaiting(writer, run_id="r1", session_key="S1")
    assert writer.increment_parse_failures(run_id="r1") == 1
    assert writer.increment_parse_failures(run_id="r1") == 2
    assert writer.increment_parse_failures(run_id="r1") == 3


def test_increment_parse_failures_atomic_across_connections(migrated_db: Path):
    def _open():
        c = sqlite3.connect(migrated_db, check_same_thread=False, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return MetaRunWriter(c)

    w1 = _open()
    w2 = _open()
    try:
        _seed_awaiting(w1, run_id="r1", session_key="S1")

        seen = []
        for w in (w1, w2, w1, w2, w1):
            seen.append(w.increment_parse_failures(run_id="r1"))

        assert seen == [1, 2, 3, 4, 5], (
            f"increment_parse_failures must be atomic per connection; got {seen}"
        )
    finally:
        w2.close()
        w1.close()


def test_increment_parse_failures_zero_for_non_awaiting(writer):
    with writer._lock:
        writer._conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r9", "t", "d", "{}", "soft_meta_invoke", "S9", "ok", 0, "{}"),
        )
        writer._conn.commit()
    assert writer.increment_parse_failures(run_id="r9") == 0


def test_update_awaiting_partial_merges_filled_json(writer):
    _seed_awaiting(writer, run_id="r1", session_key="S1")
    ok = writer.update_awaiting_partial(
        run_id="r1",
        filled_json='{"destination":"Tokyo"}',
        awaiting_since=1700001000.0,
    )
    assert ok is True
    with writer._lock:
        row = writer._conn.execute(
            "SELECT awaiting_filled_json, awaiting_since "
            "FROM meta_skill_runs WHERE run_id=?",
            ("r1",),
        ).fetchone()
    assert row["awaiting_filled_json"] == '{"destination":"Tokyo"}'
    assert float(row["awaiting_since"]) == pytest.approx(1700001000.0)


def test_update_awaiting_partial_rejects_non_awaiting(writer):
    with writer._lock:
        writer._conn.execute(
            "INSERT INTO meta_skill_runs "
            "(run_id, meta_skill_name, meta_skill_digest, plan_snapshot_json, "
            " triggered_by, session_key, status, started_at_ms, inputs_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r9", "t", "d", "{}", "soft_meta_invoke", "S9", "ok", 0, "{}"),
        )
        writer._conn.commit()
    ok = writer.update_awaiting_partial(
        run_id="r9", filled_json="{}", awaiting_since=0.0,
    )
    assert ok is False


def test_increment_parse_failures_is_portable_no_returning_clause():
    """SQLite < 3.35 (RHEL 8 / Ubuntu 20.04 system builds) has no
    ``UPDATE ... RETURNING``; the fail-open handler used to convert the
    syntax error into a permanent 0, so the parse-failure limit never
    triggered. Pin the portable UPDATE-then-SELECT form by asserting no
    SQL string literal in the method body uses the clause."""
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(MetaRunWriter.increment_parse_failures))
    fn = ast.parse(src).body[0]
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    ):
        body = body[1:]  # drop the docstring — SQL literals only
    literals = [
        node.value
        for stmt in body
        for node in ast.walk(stmt)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert literals, "expected SQL literals in increment_parse_failures"
    assert not any("returning" in literal.lower() for literal in literals)


def test_increment_parse_failures_survives_and_counts_after_partial_update(writer):
    """Increment keeps returning the fresh value across repeated calls and
    drops back to 0 once the run leaves awaiting_user."""
    _seed_awaiting(writer, run_id="r1", session_key="S1")
    assert writer.increment_parse_failures(run_id="r1") == 1
    assert writer.increment_parse_failures(run_id="r1") == 2
    writer.mark_cancelled(run_id="r1", reason="parse_failure_limit")
    assert writer.increment_parse_failures(run_id="r1") == 0
