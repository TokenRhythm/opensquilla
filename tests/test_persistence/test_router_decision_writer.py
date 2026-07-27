"""RouterDecisionWriter: inserts, sanitization, pruning, purge, rehydration reads.

The sanitization tests enforce the V017 privacy bar: ``trail``/``flags``
columns may contain only enum-like tokens and numbers — never prompt text.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from opensquilla.persistence.migrator import apply_pending
from opensquilla.persistence.router_decision_writer import (
    RouterDecisionWriter,
    open_router_decision_writer,
    sanitize_flags,
    sanitize_probs,
    sanitize_trail,
)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

PROMPT_SENTINEL = "please summarize my confidential quarterly report"


def _make_writer(tmp_path: Path, **kwargs) -> tuple[RouterDecisionWriter, str]:
    db = str(tmp_path / "sessions.sqlite")
    apply_pending(db, MIGRATIONS_DIR)
    conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return RouterDecisionWriter(conn, **kwargs), db


def _base_record(**overrides) -> dict:
    record = {
        "decision_id": "a" * 32,
        "session_key": "agent:main:webchat:s1",
        "turn_index": 0,
        "ts_ms": 1_000_000,
        "classifier": "v4_phase3",
        "proposed_tier": "c1",
        "confidence": 0.91,
        "probs": [0.05, 0.91, 0.03, 0.01],
        "flags": ["code", "multi_step"],
        "final_tier": "c2",
        "requested_provider": "openrouter",
        "requested_model": "deepseek/deepseek-chat",
        "provider": "openrouter",
        "model": "deepseek/deepseek-chat",
        "executed_provider": "deepseek",
        "executed_model": "deepseek-chat",
        "fallback_reason": "profile_unavailable",
        "thinking_level": "medium",
        "source": "v4_phase3",
        "trail": [
            {"stage": "classify", "tier": "c1", "route_class": "R1"},
            {"stage": "anti_downgrade", "applied": True, "previous_tier": "c2"},
            {"stage": "final", "tier": "c2", "route_class": "R2"},
        ],
        "baseline_model": "anthropic/claude-sonnet",
        "savings_pct": 42.5,
        "executed_kind": "single",
        "ensemble_profile": None,
        "fallback_hops": 0,
    }
    record.update(overrides)
    return record


def _rows(db: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM router_decisions ORDER BY ts_ms").fetchall()
    finally:
        conn.close()


def test_open_router_decision_writer_boot_constructor(tmp_path: Path) -> None:
    """The boot-path constructor produces a working writer over a real DB."""
    db = str(tmp_path / "sessions.sqlite")
    apply_pending(db, MIGRATIONS_DIR)
    writer = open_router_decision_writer(db, retention_days=7)
    try:
        assert writer.record_decision(_base_record()) is True
        assert len(_rows(db)) == 1
    finally:
        writer.close()


def test_record_decision_inserts_row(tmp_path: Path) -> None:
    writer, db = _make_writer(tmp_path)
    assert writer.record_decision(_base_record()) is True
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_key"] == "agent:main:webchat:s1"
    assert row["proposed_tier"] == "c1"
    assert row["final_tier"] == "c2"
    assert row["requested_provider"] == "openrouter"
    assert row["requested_model"] == "deepseek/deepseek-chat"
    assert row["executed_provider"] == "deepseek"
    assert row["executed_model"] == "deepseek-chat"
    assert row["fallback_reason"] == "profile_unavailable"
    assert row["executed_kind"] == "single"
    assert row["savings_pct"] == 42.5
    assert json.loads(row["probs"]) == [0.05, 0.91, 0.03, 0.01]
    assert json.loads(row["flags"]) == ["code", "multi_step"]
    assert json.loads(row["trail"])[0] == {"stage": "classify", "tier": "c1", "route_class": "R1"}
    writer.close()


def test_record_decision_is_best_effort_after_close(tmp_path: Path) -> None:
    writer, _db = _make_writer(tmp_path)
    writer.close()
    assert writer.record_decision(_base_record()) is False  # no raise


def test_record_decision_rejects_missing_identity(tmp_path: Path) -> None:
    writer, db = _make_writer(tmp_path)
    assert writer.record_decision(_base_record(decision_id=None)) is False
    assert writer.record_decision(_base_record(session_key="")) is False
    assert _rows(db) == []
    writer.close()


def test_no_prompt_text_reaches_any_column(tmp_path: Path) -> None:
    """V017 privacy bar: free text in any field is dropped before insert."""
    writer, db = _make_writer(tmp_path)
    dirty = _base_record(
        classifier=PROMPT_SENTINEL,
        model=PROMPT_SENTINEL,
        flags=["code", PROMPT_SENTINEL, {"nested": PROMPT_SENTINEL}],
        probs=[0.5, PROMPT_SENTINEL, float("nan")],
        trail=[
            {"stage": "classify", "tier": "c1", "note": PROMPT_SENTINEL},
            {PROMPT_SENTINEL: "value"},
            {"stage": "final", "tier": PROMPT_SENTINEL},
            PROMPT_SENTINEL,
        ],
    )
    assert writer.record_decision(dirty) is True
    row = _rows(db)[0]
    serialized = json.dumps({key: row[key] for key in row.keys()})
    assert PROMPT_SENTINEL not in serialized
    assert json.loads(row["flags"]) == ["code"]
    assert json.loads(row["probs"]) == [0.5]
    trail = json.loads(row["trail"])
    assert trail == [
        {"stage": "classify", "tier": "c1"},
        {"stage": "final"},
    ]
    writer.close()


def test_trail_and_flags_contain_only_enum_tokens_and_numbers() -> None:
    """Schema test for the sanitizers themselves (no-prompt-text bar)."""
    token_ok = "c2"
    text_bad = "user said: my ssn is 123-45-6789"
    flags = sanitize_flags([token_ok, text_bad, 7, None, "R1"])
    assert flags == ["c2", "R1"]

    trail = sanitize_trail(
        [
            {
                "stage": "confidence_gate",
                "applied": False,
                "threshold": 0.5,
                "echo": text_bad,
            }
        ]
    )
    assert trail == [{"stage": "confidence_gate", "applied": False, "threshold": 0.5}]
    for entry in trail:
        for value in entry.values():
            assert isinstance(value, (bool, int, float)) or (
                isinstance(value, str) and " " not in value and len(value) <= 128
            )

    assert sanitize_probs([0.1, "text", None, True, 0.9]) == [0.1, 0.9]


def test_write_time_opportunistic_pruning(tmp_path: Path) -> None:
    now_ms = 10_000_000_000_000
    writer, db = _make_writer(
        tmp_path,
        retention_days=30,
        prune_every=4,
        clock=lambda: now_ms,
    )
    stale_ts = now_ms - 31 * 24 * 60 * 60 * 1000
    fresh_ts = now_ms - 1000
    writer.record_decision(_base_record(decision_id="old1", ts_ms=stale_ts))
    writer.record_decision(_base_record(decision_id="old2", ts_ms=stale_ts))
    writer.record_decision(_base_record(decision_id="new1", ts_ms=fresh_ts))
    assert len(_rows(db)) == 3  # prune not yet due
    writer.record_decision(_base_record(decision_id="new2", ts_ms=fresh_ts))
    remaining = {row["decision_id"] for row in _rows(db)}
    assert remaining == {"new1", "new2"}
    writer.close()


def test_write_time_pruning_is_bounded(tmp_path: Path) -> None:
    now_ms = 10_000_000_000_000
    writer, db = _make_writer(
        tmp_path,
        retention_days=30,
        prune_every=6,
        prune_batch=2,
        clock=lambda: now_ms,
    )
    stale_ts = now_ms - 31 * 24 * 60 * 60 * 1000
    for index in range(5):
        writer.record_decision(
            _base_record(decision_id=f"old{index}", ts_ms=stale_ts + index)
        )

    # The sixth insert triggers retention, but one foreground write may delete
    # only the configured batch instead of monopolizing SQLite for the backlog.
    writer.record_decision(_base_record(decision_id="new1", ts_ms=now_ms))

    remaining = {row["decision_id"] for row in _rows(db)}
    assert remaining == {"old2", "old3", "old4", "new1"}
    writer.close()


def test_purge_for_session(tmp_path: Path) -> None:
    writer, db = _make_writer(tmp_path)
    writer.record_decision(_base_record(decision_id="d1", session_key="agent:a"))
    writer.record_decision(_base_record(decision_id="d2", session_key="agent:a"))
    writer.record_decision(_base_record(decision_id="d3", session_key="agent:b"))
    assert writer.purge_for_session("agent:a") == 2
    assert {row["session_key"] for row in _rows(db)} == {"agent:b"}
    writer.close()


def test_load_recent_history_bounds_window_and_per_session(tmp_path: Path) -> None:
    now_ms = 5_000_000_000_000
    writer, _db = _make_writer(tmp_path, clock=lambda: now_ms)
    # 7 recent decisions in session a -> only the last 5 come back.
    for index in range(7):
        writer.record_decision(
            _base_record(
                decision_id=f"a{index}",
                session_key="agent:a",
                turn_index=index,
                ts_ms=now_ms - (7 - index) * 1000,
                final_tier="c2" if index % 2 else "c1",
            )
        )
    # One stale decision outside the 1800s window -> excluded.
    writer.record_decision(
        _base_record(decision_id="b0", session_key="agent:b", ts_ms=now_ms - 2000 * 1000)
    )
    # One fresh decision in session c.
    writer.record_decision(
        _base_record(decision_id="c0", session_key="agent:c", ts_ms=now_ms - 500)
    )

    grouped = writer.load_recent_history(window_seconds=1800, per_session=5)
    assert set(grouped) == {"agent:a", "agent:c"}
    assert [row["turn_index"] for row in grouped["agent:a"]] == [2, 3, 4, 5, 6]
    assert grouped["agent:a"][-1]["final_tier"] == "c1"  # index 6 -> even -> c1
    assert len(grouped["agent:c"]) == 1
    writer.close()


def test_list_decisions_orders_newest_first_and_parses_json(tmp_path: Path) -> None:
    writer, _db = _make_writer(tmp_path)
    for index in range(3):
        writer.record_decision(
            _base_record(decision_id=f"d{index}", ts_ms=1_000 * (index + 1))
        )
    rows = writer.list_decisions()
    assert [row["decision_id"] for row in rows] == ["d2", "d1", "d0"]
    top = rows[0]
    # Whitelisted columns only, JSON columns parsed back into structures.
    assert set(top) == {
        "decision_id", "session_key", "turn_index", "ts_ms", "classifier",
            "proposed_tier", "confidence", "probs", "flags", "final_tier",
            "requested_provider", "requested_model",
            "provider", "model", "thinking_level", "source", "trail",
            "executed_provider", "executed_model", "fallback_reason",
        "baseline_model", "savings_pct", "executed_kind", "ensemble_profile",
        "fallback_hops",
    }
    assert top["probs"] == [0.05, 0.91, 0.03, 0.01]
    assert top["flags"] == ["code", "multi_step"]
    assert top["trail"][0] == {"stage": "classify", "tier": "c1", "route_class": "R1"}
    assert top["savings_pct"] == 42.5
    writer.close()


def test_list_decisions_filters_pages_and_limits(tmp_path: Path) -> None:
    writer, _db = _make_writer(tmp_path)
    for index in range(4):
        writer.record_decision(
            _base_record(decision_id=f"a{index}", session_key="agent:a", ts_ms=1_000 + index)
        )
    writer.record_decision(
        _base_record(decision_id="b0", session_key="agent:b", ts_ms=9_000)
    )

    only_a = writer.list_decisions(session_key="agent:a")
    assert [row["decision_id"] for row in only_a] == ["a3", "a2", "a1", "a0"]

    page = writer.list_decisions(session_key="agent:a", before_ts_ms=1_002, limit=1)
    assert [row["decision_id"] for row in page] == ["a1"]

    assert len(writer.list_decisions(limit=2)) == 2
    writer.close()


def test_list_decisions_is_best_effort_after_close(tmp_path: Path) -> None:
    writer, _db = _make_writer(tmp_path)
    writer.record_decision(_base_record())
    writer.close()
    assert writer.list_decisions() == []  # no raise
