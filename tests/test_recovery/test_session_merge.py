from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from opensquilla.cli.session_schema import prepare_session_schema
from opensquilla.recovery.session_merge import (
    SessionMergeResult,
    snapshot_session_database,
)
from opensquilla.recovery.session_merge import (
    merge_session_database as _merge_session_database,
)
from opensquilla.session.storage import SessionStorage


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
CREATE TABLE agent_tasks (
    task_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at INTEGER
);
CREATE TABLE turn_ingress_receipts (
    receipt_id TEXT PRIMARY KEY,
    source_scope TEXT NOT NULL,
    request_session_key TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    accepted_session_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    task_id TEXT,
    accepted_at INTEGER,
    UNIQUE(source_scope, request_session_key, client_request_id)
);
CREATE TABLE memory_durable_receipts (
    receipt_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    scope TEXT,
    created_at INTEGER
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
CREATE TABLE usage_event_items (
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    model TEXT,
    provider TEXT,
    PRIMARY KEY(event_id, ordinal)
);
CREATE TABLE usage_item_billing_receipts (
    event_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    currency TEXT NOT NULL,
    PRIMARY KEY(event_id, ordinal)
);
CREATE TABLE usage_legacy_baselines (
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    captured_at_ms INTEGER,
    PRIMARY KEY(session_id, session_epoch)
);
CREATE TABLE router_decisions (
    decision_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    ts_ms INTEGER
);
"""


def _database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(_SCHEMA)
    return connection


def _add_session(
    connection: sqlite3.Connection,
    *,
    key: str,
    session_id: str,
    content: str,
    suffix: str,
) -> None:
    connection.execute(
        "INSERT INTO sessions(session_key, session_id, updated_at, label) VALUES (?, ?, 1, ?)",
        (key, session_id, content),
    )
    cursor = connection.execute(
        """
        INSERT INTO transcript_entries(
            session_id, session_key, message_id, role, content, created_at
        ) VALUES (?, ?, ?, 'user', ?, 1)
        """,
        (session_id, key, f"message-{suffix}", content),
    )
    transcript_id = int(cursor.lastrowid)
    connection.execute(
        """
        INSERT INTO compacted_transcript_entries(
            session_id, session_key, original_entry_id, message_id, role, content, created_at
        ) VALUES (?, ?, ?, ?, 'user', ?, 1)
        """,
        (session_id, key, transcript_id, f"archived-{suffix}", content),
    )
    connection.execute(
        """
        INSERT INTO session_summaries(
            session_id, session_key, summary_text, covered_through_id
        ) VALUES (?, ?, ?, ?)
        """,
        (session_id, key, f"summary-{content}", transcript_id),
    )
    connection.execute(
        """
        INSERT INTO session_context_states(
            session_id, session_key, state_kind, payload, covered_through_id
        ) VALUES (?, ?, 'portable', '{}', ?)
        """,
        (session_id, key, transcript_id),
    )
    task_id = f"task-{suffix}"
    connection.execute(
        "INSERT INTO agent_tasks(task_id, session_key, status) VALUES (?, ?, 'complete')",
        (task_id, key),
    )
    connection.execute(
        """
        INSERT INTO turn_ingress_receipts(
            receipt_id, source_scope, request_session_key, client_request_id,
            accepted_session_key, session_id, message_id, task_id
        ) VALUES (?, 'web', ?, ?, ?, ?, ?, ?)
        """,
        (
            f"ingress-{suffix}",
            key,
            f"request-{suffix}",
            key,
            session_id,
            f"message-{suffix}",
            task_id,
        ),
    )
    connection.execute(
        """
        INSERT INTO memory_durable_receipts(
            receipt_id, session_key, session_id, idempotency_key, status
        ) VALUES (?, ?, ?, ?, 'complete')
        """,
        (f"memory-{suffix}", key, session_id, f"memory-key-{suffix}"),
    )
    connection.execute(
        """
        INSERT INTO usage_events(event_id, execution_id, call_index, session_id, status)
        VALUES (?, ?, 0, ?, 'finalized')
        """,
        (f"event-{suffix}", f"execution-{suffix}", session_id),
    )
    connection.execute(
        "INSERT INTO usage_event_items(event_id, ordinal, model) VALUES (?, 0, 'model')",
        (f"event-{suffix}",),
    )
    connection.execute(
        """
        INSERT INTO usage_item_billing_receipts(event_id, ordinal, currency)
        VALUES (?, 0, 'USD')
        """,
        (f"event-{suffix}",),
    )
    connection.execute(
        """
        INSERT INTO usage_legacy_baselines(session_id, session_epoch, total_tokens)
        VALUES (?, 0, 7)
        """,
        (session_id,),
    )
    connection.commit()


def test_merge_session_database_imports_complete_supported_session_graph(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "target.db"
    source_path = tmp_path / "source.db"
    target = _database(target_path)
    source = _database(source_path)
    _add_session(
        target,
        key="agent:main:main",
        session_id="primary-session",
        content="primary",
        suffix="primary",
    )
    _add_session(
        source,
        key="agent:main:other",
        session_id="recovery-session",
        content="recovery",
        suffix="recovery",
    )
    source.execute(
        "INSERT INTO router_decisions(decision_id, session_key) VALUES ('private', ?)",
        ("agent:main:other",),
    )
    source.commit()
    target.close()
    source.close()

    result = merge_session_database(
        target_path,
        source_path,
        source_id="11111111-1111-4111-8111-111111111111",
    )

    assert result.imported_sessions == 1
    assert result.deduplicated_sessions == 0
    assert result.remapped_session_keys == {}
    assert "router_decisions" in result.excluded_tables
    assert "agent_tasks" in result.excluded_tables
    assert "turn_ingress_receipts" in result.excluded_tables
    assert "memory_durable_receipts" in result.excluded_tables
    with sqlite3.connect(target_path) as merged:
        assert merged.execute("SELECT COUNT(*) FROM sessions").fetchone() == (2,)
        assert merged.execute(
            "SELECT content FROM transcript_entries WHERE session_key='agent:main:other'"
        ).fetchone() == ("recovery",)
        assert merged.execute(
            "SELECT summary_text FROM session_summaries WHERE session_id='recovery-session'"
        ).fetchone() == ("summary-recovery",)
        assert (
            merged.execute(
                "SELECT status FROM agent_tasks WHERE session_key='agent:main:other'"
            ).fetchone()
            is None
        )
        assert merged.execute(
            "SELECT status FROM usage_events WHERE session_id='recovery-session'"
        ).fetchone() == ("finalized",)
        assert merged.execute("SELECT COUNT(*) FROM usage_event_items").fetchone() == (2,)
        assert merged.execute("SELECT COUNT(*) FROM usage_item_billing_receipts").fetchone() == (2,)
        assert merged.execute("SELECT COUNT(*) FROM router_decisions").fetchone() == (0,)


def test_merge_uses_injected_schema_preparer_for_existing_target(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "target.db"
    source_path = tmp_path / "source.db"
    target = _database(target_path)
    source = _database(source_path)
    _add_session(
        target,
        key="agent:main:main",
        session_id="primary-session",
        content="primary",
        suffix="primary",
    )
    _add_session(
        source,
        key="agent:main:recovered",
        session_id="recovery-session",
        content="recovery",
        suffix="recovery",
    )
    target.close()
    source.close()
    prepared: list[Path] = []

    result = _merge_session_database(
        target_path,
        source_path,
        source_id="12121212-1212-4212-8212-121212121212",
        prepare_target_schema=prepared.append,
    )

    assert result.imported_sessions == 1
    assert prepared == [target_path.absolute()]


def test_merge_session_database_remaps_divergent_collision_and_is_idempotent(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "target.db"
    source_path = tmp_path / "source.db"
    target = _database(target_path)
    source = _database(source_path)
    key = "agent:main:main"
    _add_session(
        target,
        key=key,
        session_id="primary-session",
        content="primary",
        suffix="same",
    )
    _add_session(
        source,
        key=key,
        session_id="recovery-session",
        content="recovery",
        suffix="same",
    )
    target.close()
    source.close()

    first = merge_session_database(
        target_path,
        source_path,
        source_id="22222222-2222-4222-8222-222222222222",
    )

    assert first.imported_sessions == 1
    remapped_key = first.remapped_session_keys[key]
    assert remapped_key.startswith(f"{key}:recovered:")
    with sqlite3.connect(target_path) as merged:
        remapped_id = merged.execute(
            "SELECT session_id FROM sessions WHERE session_key=?",
            (remapped_key,),
        ).fetchone()[0]
        # The key collided, the identifier did not, so the imported session keeps
        # its original id. Renumbering it would strand the artifacts, tool
        # results, and transcript material stored on disk under that id.
        assert remapped_id == "recovery-session"
        assert "recovery-session" not in first.remapped_session_ids
        assert merged.execute(
            "SELECT content, session_id FROM transcript_entries WHERE session_key=?",
            (remapped_key,),
        ).fetchone() == ("recovery", remapped_id)
        new_entry_id = merged.execute(
            "SELECT id FROM transcript_entries WHERE session_key=?",
            (remapped_key,),
        ).fetchone()[0]
        assert merged.execute(
            """
            SELECT original_entry_id
            FROM compacted_transcript_entries
            WHERE session_key=?
            """,
            (remapped_key,),
        ).fetchone() == (new_entry_id,)
        assert merged.execute(
            "SELECT covered_through_id FROM session_summaries WHERE session_key=?",
            (remapped_key,),
        ).fetchone() == (new_entry_id,)
        assert (
            merged.execute(
                "SELECT accepted_session_key FROM turn_ingress_receipts "
                "WHERE accepted_session_key=?",
                (remapped_key,),
            ).fetchone()
            is None
        )
        assert merged.execute(
            "SELECT session_id FROM usage_events WHERE session_id=?",
            (remapped_id,),
        ).fetchone() == (remapped_id,)
        counts_before = {
            table: merged.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "sessions",
                "transcript_entries",
                "compacted_transcript_entries",
                "session_summaries",
                "session_context_states",
                "agent_tasks",
                "turn_ingress_receipts",
                "memory_durable_receipts",
                "usage_events",
                "usage_event_items",
                "usage_item_billing_receipts",
                "usage_legacy_baselines",
            )
        }

    second = merge_session_database(
        target_path,
        source_path,
        source_id="22222222-2222-4222-8222-222222222222",
    )

    assert second.imported_sessions == 0
    assert second.deduplicated_sessions == 1
    assert second.remapped_session_keys[key] == remapped_key
    with sqlite3.connect(target_path) as merged:
        assert {
            table: merged.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts_before
        } == counts_before


def test_merge_session_database_is_idempotent_across_schema_skew(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "old-target.db"
    source_path = tmp_path / "new-source.db"
    with sqlite3.connect(target_path) as target:
        target.executescript(
            """
            CREATE TABLE sessions (
                session_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                label TEXT,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0
            );
            INSERT INTO sessions(session_key, session_id, updated_at, label)
            VALUES ('agent:main:main', 'primary-session', 1, 'primary');
            """
        )
    with sqlite3.connect(source_path) as source:
        source.executescript(
            """
            CREATE TABLE sessions (
                session_key TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                label TEXT,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                recovery_metadata TEXT
            );
            INSERT INTO sessions(
                session_key, session_id, updated_at, label, recovery_metadata
            ) VALUES (
                'agent:main:main', 'recovery-session', 1, 'recovery', 'source-only'
            );
            """
        )

    results = []
    row_counts = []
    for _ in range(3):
        results.append(
            merge_session_database(
                target_path,
                source_path,
                source_id="66666666-6666-4666-8666-666666666666",
            )
        )
        with sqlite3.connect(target_path) as merged:
            row_counts.append(merged.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    assert [result.imported_sessions for result in results] == [1, 0, 0]
    assert [result.deduplicated_sessions for result in results] == [0, 1, 1]
    assert row_counts == [2, 2, 2]
    with sqlite3.connect(target_path) as merged:
        assert merged.execute("SELECT label FROM sessions ORDER BY label").fetchall() == [
            ("primary",),
            ("recovery",),
        ]


@pytest.mark.asyncio
async def test_merge_upgrades_old_target_before_importing_current_source_fields(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "old-target.db"
    source_path = tmp_path / "current-source.db"
    target_storage = await SessionStorage.open(str(target_path))
    source_storage = await SessionStorage.open(str(source_path))
    await target_storage.close()
    await source_storage.close()

    with sqlite3.connect(target_path) as target:
        target.execute(
            """
            INSERT INTO sessions(
                session_key, session_id, created_at, updated_at, label
            ) VALUES ('agent:main:primary', 'primary-session', 1, 1, 'primary')
            """
        )
        target.execute("ALTER TABLE sessions DROP COLUMN derived_title")
        for column in (
            "total_cost_usd",
            "billed_cost_usd",
            "estimated_cost_component_usd",
            "cost_source",
            "missing_cost_entries",
        ):
            target.execute(f"ALTER TABLE sessions DROP COLUMN {column}")
        target.execute("DROP TABLE compacted_transcript_entries")
        target.execute("DROP TABLE session_context_states")

    with sqlite3.connect(source_path) as source:
        source.execute(
            """
            INSERT INTO sessions(
                session_key, session_id, created_at, updated_at, label,
                derived_title, total_cost_usd, billed_cost_usd, cost_source
            ) VALUES (
                'agent:main:recovered', 'recovered-session', 2, 2, 'recovered',
                'Recovered title', 1.25, 1.25, 'provider_reported'
            )
            """
        )
        transcript = source.execute(
            """
            INSERT INTO transcript_entries(
                session_id, session_key, message_id, role, content,
                reasoning_content, turn_context, created_at
            ) VALUES (
                'recovered-session', 'agent:main:recovered', 'message-current',
                'assistant', 'current transcript', 'current reasoning',
                '{"turn_id":"turn-current"}', 2
            )
            """
        )
        transcript_id = int(transcript.lastrowid)
        source.execute(
            """
            INSERT INTO compacted_transcript_entries(
                session_id, session_key, compaction_id, compaction_index,
                original_entry_id, message_id, role, content, turn_context,
                created_at, archived_at
            ) VALUES (
                'recovered-session', 'agent:main:recovered', 'compact-current', 1,
                ?, 'message-current', 'assistant', 'archived current transcript',
                '{"turn_id":"turn-current"}', 2, 3
            )
            """,
            (transcript_id,),
        )
        source.execute(
            """
            INSERT INTO session_context_states(
                session_id, session_key, provider, state_kind, payload,
                covered_through_id, created_at, portable
            ) VALUES (
                'recovered-session', 'agent:main:recovered', 'portable',
                'provider_state', '{"cursor":"current"}', ?, 3, 1
            )
            """,
            (transcript_id,),
        )

    result = merge_session_database(
        target_path,
        source_path,
        source_id="77777777-7777-4777-8777-777777777777",
    )

    assert result.imported_sessions == 1
    assert result.imported_rows["compacted_transcript_entries"] == 1
    assert result.imported_rows["session_context_states"] == 1
    with sqlite3.connect(target_path) as merged:
        assert merged.execute(
            """
            SELECT derived_title, total_cost_usd, billed_cost_usd, cost_source
            FROM sessions
            WHERE session_key='agent:main:recovered'
            """
        ).fetchone() == ("Recovered title", 1.25, 1.25, "provider_reported")
        imported_transcript_id = merged.execute(
            """
            SELECT id
            FROM transcript_entries
            WHERE session_key='agent:main:recovered'
            """
        ).fetchone()[0]
        assert merged.execute(
            """
            SELECT content, turn_context, original_entry_id
            FROM compacted_transcript_entries
            WHERE session_key='agent:main:recovered'
            """
        ).fetchone() == (
            "archived current transcript",
            '{"turn_id":"turn-current"}',
            imported_transcript_id,
        )
        assert merged.execute(
            """
            SELECT payload, portable, valid
            FROM session_context_states
            WHERE session_key='agent:main:recovered'
            """
        ).fetchone() == ('{"cursor":"current"}', 1, 1)


def test_merge_session_database_snapshots_wal_when_target_is_missing(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    source = _database(source_path)
    source.execute("PRAGMA journal_mode=WAL")
    _add_session(
        source,
        key="agent:main:wal",
        session_id="wal-session",
        content="from wal",
        suffix="wal",
    )

    result = merge_session_database(
        target_path,
        source_path,
        source_id="33333333-3333-4333-8333-333333333333",
    )

    assert result.imported_sessions == 1
    with sqlite3.connect(target_path) as merged:
        assert merged.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert merged.execute("SELECT label FROM sessions").fetchone() == ("from wal",)
    source.close()


@pytest.mark.parametrize("operation", ["snapshot", "merge-existing"])
def test_wal_source_without_shm_is_never_opened_by_sqlite(
    tmp_path: Path,
    operation: str,
) -> None:
    origin_path = tmp_path / "origin.db"
    source_root = tmp_path / "recovery" / "state"
    source_root.mkdir(parents=True)
    source_path = source_root / "sessions.db"
    source_wal = source_path.with_name(f"{source_path.name}-wal")
    source_shm = source_path.with_name(f"{source_path.name}-shm")
    target_path = tmp_path / "primary" / "state" / "sessions.db"

    origin = _database(origin_path)
    try:
        assert origin.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        origin.execute("PRAGMA wal_autocheckpoint=0")
        _add_session(
            origin,
            key="agent:main:private-wal",
            session_id="private-wal-session",
            content="committed only in wal",
            suffix="private-wal",
        )
        origin_wal = origin_path.with_name(f"{origin_path.name}-wal")
        assert origin_wal.stat().st_size > 32
        shutil.copyfile(origin_path, source_path)
        shutil.copyfile(origin_wal, source_wal)

        if operation == "merge-existing":
            target_path.parent.mkdir(parents=True)
            target = _database(target_path)
            target.close()

        assert not source_shm.exists()
        source_before = {
            path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for path in (source_path, source_wal)
        }
        source_root_before = source_root.stat().st_mtime_ns

        if operation == "snapshot":
            snapshot_session_database(source_path, target_path)
        else:
            result = merge_session_database(
                target_path,
                source_path,
                source_id="44444444-4444-4444-8444-444444444444",
            )
            assert result.imported_sessions == 1

        source_after = {
            path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for path in (source_path, source_wal)
        }
        assert source_after == source_before
        assert source_root.stat().st_mtime_ns == source_root_before
        assert not source_shm.exists(), "session merge must not create source SQLite sidecars"
        with sqlite3.connect(target_path) as merged:
            assert merged.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert merged.execute(
                "SELECT label FROM sessions WHERE session_key=?",
                ("agent:main:private-wal",),
            ).fetchone() == ("committed only in wal",)
    finally:
        origin.close()


@pytest.mark.asyncio
async def test_deduplicated_parent_id_is_remapped_in_imported_child_provenance(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "deduplicated-parent-target.db"
    source_path = tmp_path / "deduplicated-parent-source.db"
    target_storage = await SessionStorage.open(str(target_path))
    source_storage = await SessionStorage.open(str(source_path))
    await target_storage.close()
    await source_storage.close()

    parent_key = "agent:main:deduplicated-parent"
    child_key = "agent:main:deduplicated-child"
    with sqlite3.connect(target_path) as target:
        target.execute(
            """
            INSERT INTO sessions(
                session_key, session_id, created_at, updated_at, label
            ) VALUES (?, 'target-parent-id', 1, 1, 'same parent')
            """,
            (parent_key,),
        )
        target.execute(
            """
            INSERT INTO transcript_entries(
                session_id, session_key, message_id, role, content, created_at
            ) VALUES ('target-parent-id', ?, 'same-message', 'user', 'same body', 1)
            """,
            (parent_key,),
        )
    with sqlite3.connect(source_path) as source:
        source.execute(
            """
            INSERT INTO sessions(
                session_key, session_id, created_at, updated_at, label
            ) VALUES (?, 'source-parent-id', 1, 1, 'same parent')
            """,
            (parent_key,),
        )
        source.execute(
            """
            INSERT INTO transcript_entries(
                session_id, session_key, message_id, role, content, created_at
            ) VALUES ('source-parent-id', ?, 'same-message', 'user', 'same body', 1)
            """,
            (parent_key,),
        )
        source.execute(
            """
            INSERT INTO sessions(
                session_key, session_id, created_at, updated_at, label, spawned_by,
                parent_session_key
            ) VALUES (?, 'source-child-id', 2, 2, 'child', ?, ?)
            """,
            (child_key, parent_key, parent_key),
        )
        source.execute(
            """
            INSERT INTO transcript_entries(
                session_id, session_key, message_id, role, content, created_at,
                provenance_origin_session_id, provenance_source_session_key
            ) VALUES ('source-child-id', ?, 'child-message', 'user', 'child body', 2, ?, ?)
            """,
            (child_key, "source-parent-id", parent_key),
        )

    result = merge_session_database(
        target_path,
        source_path,
        source_id="55555555-5555-4555-8555-555555555555",
    )

    assert result.imported_sessions == 1
    assert result.deduplicated_sessions == 1
    assert result.remapped_session_ids["source-parent-id"] == "target-parent-id"
    with sqlite3.connect(target_path) as merged:
        assert merged.execute(
            """
            SELECT provenance_origin_session_id, provenance_source_session_key
            FROM transcript_entries
            WHERE session_key=?
            """,
            (child_key,),
        ).fetchone() == ("target-parent-id", parent_key)


@pytest.mark.asyncio
async def test_merge_session_database_supports_current_production_schema_and_fts(
    tmp_path: Path,
) -> None:
    target_path = tmp_path / "production-target.db"
    source_path = tmp_path / "production-source.db"
    target_storage = await SessionStorage.open(str(target_path))
    source_storage = await SessionStorage.open(str(source_path))
    await target_storage.close()
    await source_storage.close()

    parent_key = "agent:main:parent"
    child_key = "agent:main:child"
    with sqlite3.connect(target_path) as target:
        target.execute(
            """
            INSERT INTO sessions(
                session_key, session_id, created_at, updated_at, label
            ) VALUES (?, 'target-parent', 1, 1, 'primary')
            """,
            (parent_key,),
        )
        target.execute(
            """
            INSERT INTO transcript_entries(
                session_id, session_key, message_id, role, content, created_at
            ) VALUES ('target-parent', ?, 'target-message', 'user', 'primary body', 1)
            """,
            (parent_key,),
        )
    with sqlite3.connect(source_path) as source:
        source.execute(
            """
            INSERT INTO sessions(
                session_key, session_id, created_at, updated_at, label
            ) VALUES (?, 'source-parent', 1, 2, 'recovery')
            """,
            (parent_key,),
        )
        source.execute(
            """
            INSERT INTO sessions(
                session_key, session_id, created_at, updated_at, label, spawned_by,
                parent_session_key
            ) VALUES (?, 'source-child', 1, 2, 'child', ?, ?)
            """,
            (child_key, parent_key, parent_key),
        )
        parent_entry = source.execute(
            """
            INSERT INTO transcript_entries(
                session_id, session_key, message_id, role, content, created_at
            ) VALUES ('source-parent', ?, 'source-message', 'user',
                      'recovery production body', 2)
            """,
            (parent_key,),
        )
        parent_entry_id = int(parent_entry.lastrowid)
        source.execute(
            """
            INSERT INTO compacted_transcript_entries(
                session_id, session_key, original_entry_id, message_id, role,
                content, created_at, archived_at
            ) VALUES ('source-parent', ?, ?, 'archived-message', 'user',
                      'recovery archived body', 2, 3)
            """,
            (parent_key, parent_entry_id),
        )
        source.execute(
            """
            INSERT INTO session_summaries(
                session_id, session_key, summary_text, covered_through_id, created_at
            ) VALUES ('source-parent', ?, 'production summary', ?, 3)
            """,
            (parent_key, parent_entry_id),
        )
        source.execute(
            """
            INSERT INTO session_context_states(
                session_id, session_key, state_kind, payload, covered_through_id,
                created_at
            ) VALUES ('source-parent', ?, 'portable', '{}', ?, 3)
            """,
            (parent_key, parent_entry_id),
        )
        source.execute(
            """
            INSERT INTO agent_tasks(
                task_id, session_key, source_kind, queue_mode, status, created_at,
                updated_at
            ) VALUES ('production-task', ?, 'user', 'steer', 'complete', 1, 2)
            """,
            (parent_key,),
        )
        source.execute(
            """
            INSERT INTO transcript_entries(
                session_id, session_key, message_id, role, content, created_at
            ) VALUES ('source-child', ?, 'child-message', 'user', 'child body', 2)
            """,
            (child_key,),
        )
        source.execute(
            """
            INSERT INTO telemetry_daily_usage(
                day, conversation_turns, updated_at
            ) VALUES ('2026-07-25', 99, 1)
            """
        )

    result = merge_session_database(
        target_path,
        source_path,
        source_id="44444444-4444-4444-8444-444444444444",
    )

    remapped_parent = result.remapped_session_keys[parent_key]
    with sqlite3.connect(target_path) as merged:
        assert merged.execute(
            "SELECT spawned_by, parent_session_key FROM sessions WHERE session_key=?",
            (child_key,),
        ).fetchone() == (remapped_parent, remapped_parent)
        assert merged.execute(
            """
            SELECT content
            FROM transcript_fts
            WHERE transcript_fts MATCH 'production'
            """
        ).fetchone() == ("recovery production body",)
        transcript_id = merged.execute(
            "SELECT id FROM transcript_entries WHERE session_key=?",
            (remapped_parent,),
        ).fetchone()[0]
        assert merged.execute(
            "SELECT original_entry_id FROM compacted_transcript_entries WHERE session_key=?",
            (remapped_parent,),
        ).fetchone() == (transcript_id,)
        assert merged.execute(
            "SELECT covered_through_id FROM session_summaries WHERE session_key=?",
            (remapped_parent,),
        ).fetchone() == (transcript_id,)
        assert merged.execute(
            "SELECT valid, invalid_reason FROM session_context_states WHERE session_key=?",
            (remapped_parent,),
        ).fetchone() == (0, "profile_consolidation")
        assert merged.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE session_key=?",
            (remapped_parent,),
        ).fetchone() == (0,)
        assert merged.execute("SELECT COUNT(*) FROM telemetry_daily_usage").fetchone() == (0,)
        counts_before = {
            table: merged.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "sessions",
                "transcript_entries",
                "compacted_transcript_entries",
                "session_summaries",
                "session_context_states",
            )
        }

    repeated = merge_session_database(
        target_path,
        source_path,
        source_id="44444444-4444-4444-8444-444444444444",
    )

    assert repeated.imported_sessions == 0
    assert repeated.deduplicated_sessions == 2
    with sqlite3.connect(target_path) as merged:
        assert {
            table: merged.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in counts_before
        } == counts_before


def test_true_identifier_collision_still_renumbers_the_import(tmp_path: Path) -> None:
    """Keeping the source id on a key collision must not weaken id uniqueness.

    Recovery profiles are historical copies of one install, so a diverged copy of
    the same conversation can legitimately carry the same identifier. That case
    must still be renumbered, otherwise two distinct sessions would share an id
    and every id-keyed lookup in the merged database would become ambiguous.
    """

    target_path = tmp_path / "target.db"
    source_path = tmp_path / "source.db"
    target = _database(target_path)
    source = _database(source_path)
    _add_session(
        target,
        key="agent:alpha:main",
        session_id="shared-identifier",
        content="primary",
        suffix="primary",
    )
    _add_session(
        source,
        key="agent:beta:main",
        session_id="shared-identifier",
        content="recovery",
        suffix="recovery",
    )
    target.close()
    source.close()

    result = merge_session_database(
        target_path,
        source_path,
        source_id="33333333-3333-4333-8333-333333333333",
    )

    assert result.imported_sessions == 1
    assert "shared-identifier" in result.remapped_session_ids
    # The key did not collide, so only the identifier moved.
    assert result.remapped_session_keys == {}
    with sqlite3.connect(target_path) as merged:
        rows = dict(merged.execute("SELECT session_key, session_id FROM sessions").fetchall())
    assert rows["agent:alpha:main"] == "shared-identifier"
    assert rows["agent:beta:main"] == result.remapped_session_ids["shared-identifier"]
    assert len(set(rows.values())) == 2
