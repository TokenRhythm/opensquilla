"""MetaRunWriter unit tests — round-trip, truncation, thread safety, redaction."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from yoyo import get_backend, read_migrations

from opensquilla.persistence.meta_run_writer import (
    MetaRunWriter,
    RunRecord,  # noqa: F401 — explicit public-API surface assertion
    StepRecord,  # noqa: F401 — explicit public-API surface assertion
    _gen_ulid,
    _redact_inputs_json,
    _serialize_plan,
    _truncate,
    open_meta_run_writer,
    summarize_run_record,
)
from opensquilla.persistence.migrator import apply_pending
from opensquilla.skills.meta.types import MetaPlan, MetaResult, MetaStep

MIGRATIONS_DIR = Path(__file__).resolve().parents[1].parent / "migrations"


@pytest.fixture
def writer(tmp_path: Path):
    db = str(tmp_path / "test.db")
    apply_pending(db, MIGRATIONS_DIR)
    w = open_meta_run_writer(db)
    yield w
    w.close()


def _make_plan(name: str = "demo") -> MetaPlan:
    return MetaPlan(
        name=name,
        triggers=("demo trigger",),
        priority=50,
        steps=(
            MetaStep(id="s1", skill="alpha", kind="agent"),
            MetaStep(id="s2", skill="beta", kind="agent", depends_on=("s1",)),
        ),
    )


def test_pragmas_set_on_connection(writer: MetaRunWriter) -> None:
    cur = writer._conn.execute("PRAGMA foreign_keys")
    assert cur.fetchone()[0] == 1
    cur = writer._conn.execute("PRAGMA journal_mode")
    assert cur.fetchone()[0].lower() == "wal"
    cur = writer._conn.execute("PRAGMA synchronous")
    assert cur.fetchone()[0] == 1  # NORMAL == 1
    cur = writer._conn.execute("PRAGMA busy_timeout")
    assert cur.fetchone()[0] == 5000


def test_begin_finish_run_roundtrip(writer: MetaRunWriter) -> None:
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="soft_meta_invoke",
        inputs={"user_message": "hi"},
        session_key="sess-1",
        turn_id="turn-1",
    )
    assert len(run_id) == 26  # ULID

    from opensquilla.skills.meta.types import MetaResult
    writer.finish_run_sync(
        run_id=run_id,
        status="ok",
        result=MetaResult(ok=True, final_text="hello"),
    )

    record = writer.get_run(run_id)
    assert record is not None
    assert record.meta_skill_name == "demo"
    assert record.triggered_by == "soft_meta_invoke"
    assert record.session_key == "sess-1"
    assert record.status == "ok"
    assert record.final_text == "hello"
    assert record.owner_pid == os.getpid()
    assert record.plan_snapshot_json  # non-empty
    assert len(record.meta_skill_digest) == 64  # sha256 hex


def test_begin_run_accepts_manual_command_trigger(writer: MetaRunWriter) -> None:
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="manual_command",
        inputs={"user_message": "/meta demo"},
        session_key="sess-manual",
        turn_id="turn-manual",
    )
    assert run_id is not None

    record = writer.get_run(run_id)
    assert record is not None
    assert record.triggered_by == "manual_command"
    assert record.status == "running"


def test_step_lifecycle(writer: MetaRunWriter) -> None:
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="hard_takeover",
        inputs={"q": "x"},
        session_key=None,
        turn_id=None,
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[0],
        effective_skill="alpha",
        rendered_inputs={"q": "x"},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="s1",
        status="ok",
        output_text="alpha-output",
    )
    steps = writer.get_steps(run_id)
    assert len(steps) == 1
    assert steps[0].status == "ok"
    assert steps[0].output_text == "alpha-output"
    assert steps[0].effective_skill == "alpha"


def test_finish_step_works_after_usage_column_rollback(tmp_path: Path) -> None:
    db = str(tmp_path / "v014_schema.db")
    apply_pending(db, MIGRATIONS_DIR)
    backend = get_backend(f"sqlite:///{db}")
    migrations = read_migrations(str(MIGRATIONS_DIR))
    by_id = {migration.id: migration for migration in migrations}
    backend.rollback_migrations([by_id["V015__meta_skill_step_usage"]])

    w = open_meta_run_writer(db)
    try:
        plan = _make_plan()
        run_id = w.begin_run_sync(
            meta_skill_name=plan.name,
            meta_plan=plan,
            triggered_by="hard_takeover",
            inputs={"q": "x"},
            session_key=None,
            turn_id=None,
        )
        w.begin_step_sync(
            run_id=run_id,
            step=plan.steps[0],
            effective_skill="alpha",
            rendered_inputs={"q": "x"},
        )
        w.finish_step_sync(
            run_id=run_id,
            step_id="s1",
            status="ok",
            output_text="alpha-output",
            usage={"input_tokens": 5, "output_tokens": 2},
        )

        [step] = w.get_steps(run_id)
        assert step.status == "ok"
        assert step.output_text == "alpha-output"
        record = w.get_run(run_id)
        assert record is not None
        assert summarize_run_record(record)["usage"]["available"] is False
    finally:
        w.close()


def test_run_summary_reports_step_counts_duration_and_unavailable_cost(
    writer: MetaRunWriter,
) -> None:
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="soft_meta_invoke",
        inputs={"user_message": "summarize"},
        session_key="sess-1",
        turn_id="turn-1",
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[0],
        effective_skill="alpha",
        rendered_inputs={"q": "x"},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="s1",
        status="ok",
        output_text="alpha-output",
    )
    writer.finish_run_sync(
        run_id=run_id,
        status="ok",
        result=MetaResult(ok=True, final_text="done"),
    )

    record = writer.get_run(run_id)
    assert record is not None
    summary = summarize_run_record(record)

    assert summary["run_id"] == run_id
    assert summary["step_count"] == 1
    assert summary["completed_step_count"] == 1
    assert summary["failed_step_count"] == 0
    assert summary["duration_ms"] is not None
    assert summary["final_text_chars"] == 4
    assert summary["step_output_chars"] == len("alpha-output")
    assert summary["usage"]["available"] is False
    assert summary["usage"]["cost_source"] == "unavailable"
    assert summary["steps"][0]["output_chars"] == len("alpha-output")


def test_run_summary_aggregates_persisted_step_usage(writer: MetaRunWriter) -> None:
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="soft_meta_invoke",
        inputs={"user_message": "summarize"},
        session_key="sess-1",
        turn_id="turn-1",
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[0],
        effective_skill="alpha",
        rendered_inputs={"q": "x"},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="s1",
        status="ok",
        output_text="alpha-output",
        usage={
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
            "cost_usd": 0.0042,
            "estimated_cost_usd": 0.0042,
            "billed_cost_usd": 0.0,
            "cost_source": "opensquilla_estimate",
            "model": "model-alpha",
        },
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[1],
        effective_skill="beta",
        rendered_inputs={"q": "y"},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="s2",
        status="ok",
        output_text="beta-output",
        usage={
            "input_tokens": 20,
            "output_tokens": 5,
            "total_tokens": 25,
            "cost_usd": 0.01,
            "billed_cost_usd": 0.01,
            "cost_source": "provider_billed",
            "model": "model-beta",
        },
    )
    writer.finish_run_sync(
        run_id=run_id,
        status="ok",
        result=MetaResult(ok=True, final_text="done"),
    )

    record = writer.get_run(run_id)
    assert record is not None
    summary = summarize_run_record(record)

    assert summary["usage"]["available"] is True
    assert summary["usage"]["input_tokens"] == 32
    assert summary["usage"]["output_tokens"] == 8
    assert summary["usage"]["total_tokens"] == 40
    assert summary["usage"]["cost_usd"] == pytest.approx(0.0142)
    assert summary["usage"]["cost_source"] == "mixed"
    assert summary["steps"][0]["usage"]["model"] == "model-alpha"
    assert summary["steps"][1]["usage"]["cost_source"] == "provider_billed"


def test_llm_chat_step_lifecycle(writer: MetaRunWriter) -> None:
    plan = MetaPlan(
        name="demo",
        triggers=("demo trigger",),
        priority=50,
        steps=(MetaStep(id="baseline", skill="baseline", kind="llm_chat"),),
    )
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="soft_meta_invoke",
        inputs={"user_message": "x"},
        session_key=None,
        turn_id=None,
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[0],
        effective_skill="baseline",
        rendered_inputs={"task": "same task"},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="baseline",
        status="ok",
        output_text="baseline-output",
    )

    steps = writer.get_steps(run_id)
    assert len(steps) == 1
    assert steps[0].step_kind == "llm_chat"
    assert steps[0].status == "ok"
    assert steps[0].output_text == "baseline-output"


def test_user_input_step_lifecycle(writer: MetaRunWriter) -> None:
    plan = MetaPlan(
        name="demo",
        triggers=("demo trigger",),
        priority=50,
        steps=(MetaStep(id="collect", skill="collect", kind="user_input"),),
    )
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="soft_meta_invoke",
        inputs={"user_message": "x"},
        session_key=None,
        turn_id=None,
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=plan.steps[0],
        effective_skill="collect",
        rendered_inputs={"topic": "travel"},
    )
    writer.finish_step_sync(
        run_id=run_id,
        step_id="collect",
        status="ok",
        output_text="collected",
    )

    steps = writer.get_steps(run_id)
    assert len(steps) == 1
    assert steps[0].step_kind == "user_input"
    assert steps[0].status == "ok"
    assert steps[0].output_text == "collected"


def test_on_step_failover_records_substitution(writer: MetaRunWriter) -> None:
    """C3: original failed step gets status='substituted' + substitute_step_id."""
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="hard_takeover",
        inputs={},
        session_key=None,
        turn_id=None,
    )
    writer.begin_step_sync(
        run_id=run_id, step=plan.steps[0], effective_skill="alpha", rendered_inputs={},
    )
    writer.on_step_failover_sync(
        run_id=run_id,
        failed_step_id="s1",
        substitute_step_id="s_fallback",
        error="alpha exploded",
    )
    steps = {s.step_id: s for s in writer.get_steps(run_id)}
    assert steps["s1"].status == "substituted"
    assert steps["s1"].substitute_step_id == "s_fallback"
    assert steps["s1"].error == "alpha exploded"


def test_truncate_64kib_utf8_boundary() -> None:
    """W4/§4.2: truncate clips at UTF-8 boundary safely."""
    multibyte = "中" * 30000  # each char = 3 bytes, total 90 KB
    out, truncated = _truncate(multibyte, "x", max_bytes=64 * 1024)
    assert truncated
    assert out is not None
    encoded = out.encode("utf-8")
    assert len(encoded) <= 64 * 1024
    # No malformed UTF-8: round-tripping must succeed
    encoded.decode("utf-8")


def test_truncate_passthrough_for_small() -> None:
    out, truncated = _truncate("hello", "x", max_bytes=64 * 1024)
    assert not truncated
    assert out == "hello"


def test_redactor_redacts_secret_keys() -> None:
    raw = {
        "user_message": "tell me about cats",
        "api_key": "sk-abc123",
        "nested": {"token": "Bearer xyz", "color": "blue"},
        "AUTH_HEADER": "Bearer real-secret",
    }
    out = _redact_inputs_json(raw, max_bytes=64 * 1024)
    parsed = json.loads(out)
    assert parsed["user_message"] == "tell me about cats"
    assert parsed["api_key"] == "[REDACTED]"
    assert parsed["nested"]["token"] == "[REDACTED]"
    assert parsed["nested"]["color"] == "blue"
    assert parsed["AUTH_HEADER"] == "[REDACTED]"


def test_redactor_clips_large_strings() -> None:
    raw = {"huge": "x" * 10_000}
    out = _redact_inputs_json(raw, max_bytes=64 * 1024)
    parsed = json.loads(out)
    assert len(parsed["huge"]) <= 4100  # 4 KiB + suffix


def test_redactor_total_size_budget() -> None:
    raw = {f"k{i}": "x" * 200 for i in range(1000)}
    out = _redact_inputs_json(raw, max_bytes=4 * 1024)
    assert len(out.encode("utf-8")) <= 4 * 1024 + 64  # tiny overhead allowed
    parsed = json.loads(out)
    assert parsed.get("_redaction_overflow") is True


def test_ulid_known_vector_length_and_alphabet() -> None:
    """I4: ULIDs are 26-char Crockford-base32 (no I, L, O, U)."""
    forbidden = set("ILOU")
    for _ in range(100):
        u = _gen_ulid()
        assert len(u) == 26
        assert all(c.isalnum() for c in u)
        assert not (set(u.upper()) & forbidden)


def test_ulid_same_ms_collision_uniqueness() -> None:
    """I4: 1000 ULIDs minted in a tight loop must all be unique."""
    ids = {_gen_ulid() for _ in range(1000)}
    assert len(ids) == 1000


def test_ulid_lexicographic_order_matches_time() -> None:
    """I3: time-ordered ULIDs sort lexicographically same as by start time."""
    import time
    pairs = []
    for _ in range(20):
        pairs.append((time.time_ns(), _gen_ulid()))
        time.sleep(0.005)
    sorted_by_time = [u for _, u in sorted(pairs)]
    sorted_by_ulid = sorted([u for _, u in pairs])
    assert sorted_by_time == sorted_by_ulid


def test_serialize_plan_deterministic() -> None:
    """C5: plan snapshot + digest must be deterministic for same plan."""
    plan1 = _make_plan()
    plan2 = _make_plan()
    snap1, dig1 = _serialize_plan(plan1)
    snap2, dig2 = _serialize_plan(plan2)
    assert snap1 == snap2
    assert dig1 == dig2
    assert len(dig1) == 64


def test_thread_safety_executor(writer: MetaRunWriter) -> None:
    """W1 v2: writer must survive cross-thread access from default ThreadPoolExecutor."""
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name, meta_plan=plan,
        triggered_by="soft_meta_invoke", inputs={}, session_key=None, turn_id=None,
    )

    def _do_step(i: int) -> None:
        step = MetaStep(id=f"par{i}", skill=f"s{i}", kind="agent")
        writer.begin_step_sync(
            run_id=run_id, step=step, effective_skill=f"s{i}", rendered_inputs={},
        )
        writer.finish_step_sync(
            run_id=run_id, step_id=f"par{i}", status="ok", output_text=f"o{i}",
        )

    # ThreadPoolExecutor default (multi-thread) — used to fail with check_same_thread=True
    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(_do_step, range(20)))

    steps = writer.get_steps(run_id)
    assert len(steps) == 20


def test_cancelled_status_distinct(writer: MetaRunWriter) -> None:
    """W5: cancelled is distinct from failed and ok."""
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name, meta_plan=plan,
        triggered_by="soft_meta_invoke", inputs={}, session_key=None, turn_id=None,
    )
    writer.finish_run_sync(run_id=run_id, status="cancelled", result=None)
    record = writer.get_run(run_id)
    assert record is not None
    assert record.status == "cancelled"
    assert record.final_text is None


def test_writer_failures_dont_raise(tmp_path: Path) -> None:
    """Fail-open contract: writer methods log + swallow."""
    db = str(tmp_path / "test.db")
    apply_pending(db, MIGRATIONS_DIR)
    w = open_meta_run_writer(db)
    w.close()  # connection now closed
    # Subsequent calls must not raise
    assert w.begin_run_sync(
        meta_skill_name="demo",
        meta_plan=_make_plan(),
        triggered_by="soft_meta_invoke",
        inputs={},
        session_key=None,
        turn_id=None,
    ) is None
    w.begin_step_sync(
        run_id="bogus", step=MetaStep(id="s", skill="x", kind="agent"),
        effective_skill="x", rendered_inputs={},
    )  # silently no-ops


def test_purge_for_session_cascades(writer: MetaRunWriter) -> None:
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name, meta_plan=plan,
        triggered_by="soft_meta_invoke", inputs={}, session_key="sess-purge", turn_id=None,
    )
    writer.begin_step_sync(
        run_id=run_id, step=plan.steps[0], effective_skill="alpha", rendered_inputs={},
    )
    writer.finish_step_sync(run_id=run_id, step_id="s1", status="ok", output_text="x")
    writer.finish_run_sync(run_id=run_id, status="ok", result=None)

    removed = writer.purge_for_session("sess-purge")
    assert removed == 1
    assert writer.get_run(run_id) is None
    assert writer.get_steps(run_id) == []


def test_list_runs_filtering_and_ordering(writer: MetaRunWriter) -> None:
    plan = _make_plan()
    ids = []
    for i in range(5):
        rid = writer.begin_run_sync(
            meta_skill_name=plan.name, meta_plan=plan,
            triggered_by="soft_meta_invoke", inputs={"i": i},
            session_key=f"s{i % 2}", turn_id=None,
        )
        writer.finish_run_sync(run_id=rid, status="ok" if i % 2 == 0 else "failed", result=None)
        ids.append(rid)

    all_runs = writer.list_runs(limit=10)
    assert len(all_runs) == 5
    # I3: list ordered by started_at_ms DESC, run_id DESC → newest first
    assert all_runs[0].run_id == ids[-1]

    failed = writer.list_runs(status="failed")
    assert len(failed) == 2
    assert all(r.status == "failed" for r in failed)

    by_session = writer.list_runs(session_key="s0")
    assert len(by_session) == 3


# ---------------------------------------------------------------------------
# Retention prune (opportunistic, write-time)
# ---------------------------------------------------------------------------

_DAY_MS = 24 * 60 * 60 * 1000


def _open_clocked_writer(
    tmp_path: Path,
    *,
    retention_days: int = 90,
    prune_every: int = 1,
    prune_batch: int = 1_000,
) -> tuple[MetaRunWriter, dict[str, int]]:
    """Migrated writer with a mutable fake clock for retention tests."""
    import sqlite3

    db = str(tmp_path / "retention.db")
    apply_pending(db, MIGRATIONS_DIR)
    clock = {"now_ms": 0}
    conn = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    writer = MetaRunWriter(
        conn,
        retention_days=retention_days,
        prune_every=prune_every,
        prune_batch=prune_batch,
        clock=lambda: clock["now_ms"],
    )
    return writer, clock


def _begin(writer: MetaRunWriter, *, session_key: str | None) -> str:
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name, meta_plan=plan,
        triggered_by="auto_cron", inputs={},
        session_key=session_key, turn_id=None,
    )
    assert run_id is not None
    return run_id


def test_retention_prunes_old_terminal_runs_and_cascades_steps(tmp_path: Path) -> None:
    writer, clock = _open_clocked_writer(tmp_path, retention_days=90, prune_every=1)
    try:
        plan = _make_plan()
        old_ok = _begin(writer, session_key="s-old-ok")
        writer.begin_step_sync(
            run_id=old_ok, step=plan.steps[0], effective_skill="alpha", rendered_inputs={},
        )
        writer.finish_step_sync(run_id=old_ok, step_id="s1", status="ok", output_text="x")
        writer.finish_run_sync(run_id=old_ok, status="ok", result=None)

        old_failed = _begin(writer, session_key="s-old-failed")
        writer.finish_run_sync(
            run_id=old_failed, status="failed",
            result=MetaResult(ok=False, error="boom"),
        )

        # Live/parked rows must survive regardless of age.
        old_running = _begin(writer, session_key="s-old-running")
        old_awaiting = _begin(writer, session_key="s-old-awaiting")
        assert writer.try_claim_awaiting(
            run_id=old_awaiting, step_id="collect", schema_json="{}",
            session_id="s-old-awaiting", inputs_json="{}",
            step_outputs_json="{}", awaiting_since=1.0,
        )

        clock["now_ms"] = 91 * _DAY_MS
        fresh = _begin(writer, session_key="s-fresh")  # prune_every=1 → prunes now

        assert writer.get_run(old_ok) is None
        assert writer.get_steps(old_ok) == []  # FK CASCADE removed the steps
        assert writer.get_run(old_failed) is None
        running = writer.get_run(old_running)
        assert running is not None and running.status == "running"
        awaiting = writer.get_run(old_awaiting)
        assert awaiting is not None and awaiting.status == "awaiting_user"
        assert writer.get_run(fresh) is not None
    finally:
        writer.close()


def test_retention_keeps_terminal_runs_inside_window(tmp_path: Path) -> None:
    writer, clock = _open_clocked_writer(tmp_path, retention_days=90, prune_every=1)
    try:
        recent_ok = _begin(writer, session_key="s-recent")
        writer.finish_run_sync(run_id=recent_ok, status="ok", result=None)

        clock["now_ms"] = 89 * _DAY_MS
        _begin(writer, session_key="s-trigger")

        assert writer.get_run(recent_ok) is not None
    finally:
        writer.close()


def test_retention_prune_cadence_honours_prune_every(tmp_path: Path) -> None:
    writer, clock = _open_clocked_writer(tmp_path, retention_days=90, prune_every=3)
    try:
        old = _begin(writer, session_key="s-old")  # begin #1 — no prune
        writer.finish_run_sync(run_id=old, status="ok", result=None)

        clock["now_ms"] = 200 * _DAY_MS
        _begin(writer, session_key="s-2")  # begin #2 — still no prune
        assert writer.get_run(old) is not None

        _begin(writer, session_key="s-3")  # begin #3 — prune fires
        assert writer.get_run(old) is None
    finally:
        writer.close()


def test_retention_pruning_is_bounded_and_keeps_live_runs(tmp_path: Path) -> None:
    writer, clock = _open_clocked_writer(
        tmp_path,
        retention_days=90,
        prune_every=8,
        prune_batch=2,
    )
    try:
        old_terminal: list[str] = []
        for index in range(5):
            run_id = _begin(writer, session_key=f"s-old-{index}")
            writer.finish_run_sync(run_id=run_id, status="ok", result=None)
            old_terminal.append(run_id)

        old_running = _begin(writer, session_key="s-running")
        old_awaiting = _begin(writer, session_key="s-awaiting")
        assert writer.try_claim_awaiting(
            run_id=old_awaiting,
            step_id="collect",
            schema_json="{}",
            session_id="s-awaiting",
            inputs_json="{}",
            step_outputs_json="{}",
            awaiting_since=1.0,
        )

        clock["now_ms"] = 91 * _DAY_MS
        fresh = _begin(writer, session_key="s-fresh")

        remaining_terminal = [
            run_id for run_id in old_terminal if writer.get_run(run_id) is not None
        ]
        assert len(remaining_terminal) == 3
        running = writer.get_run(old_running)
        assert running is not None and running.status == "running"
        awaiting = writer.get_run(old_awaiting)
        assert awaiting is not None and awaiting.status == "awaiting_user"
        assert writer.get_run(fresh) is not None
    finally:
        writer.close()


def test_retention_defaults_via_open_meta_run_writer_kwargs(tmp_path: Path) -> None:
    db = str(tmp_path / "kwargs.db")
    apply_pending(db, MIGRATIONS_DIR)
    w = open_meta_run_writer(db, retention_days=7, prune_every=16)
    try:
        assert w._retention_days == 7
        assert w._prune_every == 16
    finally:
        w.close()
    # Backward-compatible positional-only call keeps working with defaults.
    w2 = open_meta_run_writer(db)
    try:
        assert w2._retention_days == 90
        assert w2._prune_every == 64
    finally:
        w2.close()


# ---------------------------------------------------------------------------
# finish_run_sync status guard
# ---------------------------------------------------------------------------


def test_finish_run_sync_cannot_clobber_awaiting_user(writer: MetaRunWriter) -> None:
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name, meta_plan=plan,
        triggered_by="soft_meta_invoke", inputs={},
        session_key="s-guard", turn_id=None,
    )
    assert writer.try_claim_awaiting(
        run_id=run_id, step_id="collect", schema_json="{}",
        session_id="s-guard", inputs_json="{}",
        step_outputs_json="{}", awaiting_since=1.0,
    )

    # Late finalize (e.g. a stale stream teardown) must lose the race.
    writer.finish_run_sync(
        run_id=run_id, status="ok",
        result=MetaResult(ok=True, final_text="late finalize"),
    )

    record = writer.get_run(run_id)
    assert record is not None
    assert record.status == "awaiting_user"
    assert record.final_text is None


def test_finish_run_sync_running_to_terminal_still_works(writer: MetaRunWriter) -> None:
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name, meta_plan=plan,
        triggered_by="soft_meta_invoke", inputs={},
        session_key="s-normal", turn_id=None,
    )
    writer.finish_run_sync(
        run_id=run_id, status="ok",
        result=MetaResult(ok=True, final_text="done"),
    )
    record = writer.get_run(run_id)
    assert record is not None
    assert record.status == "ok"
    assert record.final_text == "done"


def test_finish_run_sync_allows_confirmed_preflight_rerun(writer: MetaRunWriter) -> None:
    """The preflight-confirmation flow re-runs a row parked as
    cancelled/preflight_required (see _is_confirmable_preflight_run) and
    must still be able to finalize it."""
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name, meta_plan=plan,
        triggered_by="soft_meta_invoke", inputs={},
        session_key="s-preflight", turn_id=None,
    )
    writer.finish_run_sync(
        run_id=run_id, status="cancelled",
        result=MetaResult(ok=False, error="preflight_required"),
    )
    writer.finish_run_sync(
        run_id=run_id, status="ok",
        result=MetaResult(ok=True, final_text="confirmed run"),
    )
    record = writer.get_run(run_id)
    assert record is not None
    assert record.status == "ok"
    assert record.final_text == "confirmed run"


def test_finish_run_sync_does_not_clobber_user_cancelled(writer: MetaRunWriter) -> None:
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name, meta_plan=plan,
        triggered_by="soft_meta_invoke", inputs={},
        session_key="s-cancel", turn_id=None,
    )
    assert writer.try_claim_awaiting(
        run_id=run_id, step_id="collect", schema_json="{}",
        session_id="s-cancel", inputs_json="{}",
        step_outputs_json="{}", awaiting_since=1.0,
    )
    writer.mark_cancelled(run_id=run_id, reason="user_cancel")

    writer.finish_run_sync(run_id=run_id, status="ok", result=None)

    record = writer.get_run(run_id)
    assert record is not None
    assert record.status == "cancelled"
    assert record.error == "cancelled:user_cancel"


def test_finish_run_sweeps_steps_left_running(writer: MetaRunWriter) -> None:
    """A cancelled turn can abandon an offloaded step write; finalizing the
    run must sweep still-running step rows so the record never reports
    in-flight work under a terminal run."""
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="soft_meta_invoke",
        inputs={},
        session_key="sess-sweep",
        turn_id=None,
    )
    assert run_id is not None
    done_step, abandoned_step = plan.steps[0], plan.steps[1]
    writer.begin_step_sync(
        run_id=run_id, step=done_step, effective_skill=done_step.skill, rendered_inputs={},
    )
    writer.finish_step_sync(
        run_id=run_id, step_id=done_step.id, status="ok", output_text="fine",
    )
    writer.begin_step_sync(
        run_id=run_id,
        step=abandoned_step,
        effective_skill=abandoned_step.skill,
        rendered_inputs={},
    )

    writer.finish_run_sync(run_id=run_id, status="cancelled", result=None)

    steps = {step.step_id: step for step in writer.get_steps(run_id)}
    assert steps[done_step.id].status == "ok"  # completed steps untouched
    swept = steps[abandoned_step.id]
    assert swept.status == "failed"
    assert swept.error == "run finalized before step completed"
    assert swept.ended_at_ms is not None


def test_mark_orphans_repairs_running_steps_under_terminal_runs(
    writer: MetaRunWriter,
) -> None:
    """A step INSERT abandoned by a cancelled turn can land after the run's
    finalize sweep; the boot cleanup must repair it. Parked awaiting_user
    runs keep their in-flight step untouched."""
    plan = _make_plan()
    run_id = writer.begin_run_sync(
        meta_skill_name=plan.name,
        meta_plan=plan,
        triggered_by="soft_meta_invoke",
        inputs={},
        session_key="sess-orphan-step",
        turn_id=None,
    )
    assert run_id is not None
    writer.finish_run_sync(run_id=run_id, status="cancelled", result=None)
    # Simulate the late, abandoned begin_step INSERT landing post-finalize.
    late_step = plan.steps[0]
    writer.begin_step_sync(
        run_id=run_id, step=late_step, effective_skill=late_step.skill, rendered_inputs={},
    )
    assert writer.get_steps(run_id)[0].status == "running"

    writer.mark_orphans_failed()

    swept = writer.get_steps(run_id)[0]
    assert swept.status == "failed"
    assert swept.error == "run finalized before step completed"
    assert swept.ended_at_ms is not None
