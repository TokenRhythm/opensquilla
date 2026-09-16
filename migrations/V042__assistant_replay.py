"""V042 - retain accepted assistant messages without rewriting legacy history."""

from __future__ import annotations

from yoyo import step

__depends__: set[str] = {"V041__retire_html_editor"}

TABLES = ("transcript_entries", "compacted_transcript_entries")


def apply_step(conn) -> None:
    for table in TABLES:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if columns and "assistant_replay" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN assistant_replay TEXT")


def rollback_step(conn) -> None:
    # Replay data cannot be reconstructed after dropping it. Older application
    # queries tolerate this nullable column; retain it across explicit rollback.
    pass


steps = [step(apply_step, rollback_step)]
