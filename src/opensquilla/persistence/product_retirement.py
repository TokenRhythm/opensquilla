"""Forward-only retirement of discontinued execution controls.

Historical audit rows stay in SQLite under ``retired_`` names for offline export.
Unaccepted drafts and execution authorizations are removed, so upgrading cannot
restart a discontinued workflow. No transcript or ordinary skill data is erased.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

LEGACY_TABLES_SQL = """
SELECT name, sql FROM sqlite_master WHERE type = 'table' AND EXISTS (
    SELECT 1 FROM sqlite_master WHERE type = 'table' AND name IN (
        'meta_skill_runs', 'meta_skill_run_steps', 'meta_control_intents',
        'meta_launch_drafts', 'meta_launch_discard_tombstones'
    )
)
"""


def product_retirement_statements(tables: Mapping[str, str]) -> list[str]:
    """Return an idempotent SQL plan for known historical tables only."""
    statements: list[str] = []
    if "agent_tasks" in tables:
        control_match = (
            "task_id IN (SELECT accepted_task_id FROM meta_control_intents) OR "
            if "meta_control_intents" in tables else ""
        )
        statements.append(f"""
            UPDATE agent_tasks SET status = 'cancelled',
                terminal_reason = 'feature_retired',
                error_class = 'FeatureRetired', error_message = 'MetaSkill was removed.',
                finished_at = COALESCE(finished_at, CAST(strftime('%s','now') AS INTEGER)*1000),
                updated_at = CAST(strftime('%s','now') AS INTEGER)*1000
            WHERE status IN ('queued', 'running', 'abandoned') AND (
                {control_match}
                json_type(CASE WHEN json_valid(details) THEN details ELSE '{{}}' END,
                          '$.metadata.meta_control') = 'object'
                OR terminal_reason = 'meta_control_restart_before_start'
            )
        """)
        if "sessions" in tables:
            statements.append("""
                UPDATE sessions SET status = 'killed'
                WHERE status = 'running' AND session_key IN (
                    SELECT session_key FROM agent_tasks
                    WHERE terminal_reason = 'feature_retired'
                ) AND NOT EXISTS (
                    SELECT 1 FROM agent_tasks WHERE agent_tasks.session_key = sessions.session_key
                    AND status IN ('queued', 'running')
                )
            """)
    statements.extend(_archive_history_statements(tables))
    for table in (
        "meta_launch_drafts", "meta_launch_discard_tombstones", "meta_control_intents",
    ):
        if table in tables:
            statements.append(f'DROP TABLE "{table}"')
    return statements


def _archive_schema(schema: str, source: str, destination: str) -> str:
    """Preserve the actual historical columns and constraints when copying."""
    schema = re.sub(rf"\b{re.escape(source)}\b", destination, schema, count=1)
    return re.sub(r"\bmeta_skill_runs\b", "retired_meta_skill_runs", schema)


def _archive_history_statements(tables: Mapping[str, str]) -> list[str]:
    """Rename ordinary history in place; merge only partially archived state.

    SQLite retargets child foreign keys when the parent is renamed, preserving
    indexes without copying historical rows. Existing archives need both tables
    copied before dropping children, then parents.
    """
    statements: list[str] = []
    parent = "meta_skill_runs"
    child = "meta_skill_run_steps"
    archived_parent = f"retired_{parent}"
    archived_child = f"retired_{child}"
    if parent in tables and archived_parent not in tables and archived_child not in tables:
        statements.append(f'ALTER TABLE "{parent}" RENAME TO "{archived_parent}"')
        if child in tables:
            statements.append(f'ALTER TABLE "{child}" RENAME TO "{archived_child}"')
        return statements

    if parent in tables:
        if archived_parent not in tables:
            statements.append(_archive_schema(tables[parent], parent, archived_parent))
        statements.append(f'INSERT OR IGNORE INTO "{archived_parent}" SELECT * FROM "{parent}"')

    # Repair a partially renamed archive before removing the live parent.
    # Normal archives already point to retired_meta_skill_runs and need no DDL.
    child_schema = tables.get(archived_child, "")
    if re.search(r"\bREFERENCES\s+[\"`\[]?meta_skill_runs\b", child_schema, re.I):
        temporary = "retired_meta_skill_run_steps__retirement"
        statements.extend((
            _archive_schema(child_schema, archived_child, temporary),
            f'INSERT INTO "{temporary}" SELECT * FROM "{archived_child}"',
            f'DROP TABLE "{archived_child}"',
            f'ALTER TABLE "{temporary}" RENAME TO "{archived_child}"',
        ))
    if child in tables:
        if archived_child not in tables:
            statements.append(_archive_schema(tables[child], child, archived_child))
        statements.append(f'INSERT OR IGNORE INTO "{archived_child}" SELECT * FROM "{child}"')
        statements.append(f'DROP TABLE "{child}"')
    if parent in tables:
        statements.append(f'DROP TABLE "{parent}"')
    return statements
