"""One-time retirement of obsolete LLM-flush metadata, preserving checkpoints.

The SQL plan is shared by the versioned upgrade and direct SessionStorage opens.
It uses transactional table replacement rather than depending on DROP COLUMN,
which is unavailable in older SQLite libraries on supported Python platforms.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence

RETIRED_MEMORY_COLUMNS: dict[str, frozenset[str]] = {
    "session_summaries": frozenset({"flush_receipt_status"}),
    "memory_durable_receipts": frozenset({"target_path", "attempt_count", "next_retry_at_ms"}),
}


def memory_flush_retirement_statements(
    *,
    table_sql: Mapping[str, str],
    columns: Mapping[str, Sequence[str]],
    schema_objects: Sequence[tuple[str, str]],
) -> list[str]:
    """Plan changes only for legacy tables; leave current schemas untouched.

    Reuse the original DDL and surviving indexes/triggers so unrelated columns,
    constraints, defaults, and custom indexes are not silently discarded.
    """
    statements: list[str] = []
    for table, retired in RETIRED_MEMORY_COLUMNS.items():
        present = retired.intersection(columns.get(table, ()))
        if not present:
            continue
        ddl = table_sql[table]
        for column in present:
            # These historical columns have simple, comma-free declarations.
            # Require exactly one match before touching any persisted data.
            ddl, count = re.subn(
                rf',\s*["`\[]?{column}["`\]]?\s+[^,)]*', '', ddl, flags=re.IGNORECASE
            )
            if count != 1:
                raise ValueError(f"Cannot retire legacy memory column {table}.{column}")
        replacement = f"{table}_without_legacy_flush"
        ddl, count = re.subn(
            rf'(?i)(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)["`\[]?{table}["`\]]?',
            rf'\1"{replacement}"', ddl, count=1,
        )
        if count != 1:
            raise ValueError(f"Cannot retire legacy memory metadata in {table}")
        retained = ', '.join(f'"{name}"' for name in columns[table] if name not in retired)
        statements.append(ddl)
        where = " WHERE scope NOT IN ('flush', 'preimage', 'repair')" if (
            table == "memory_durable_receipts"
        ) else ""
        statements.append(
            f'INSERT INTO "{replacement}" ({retained}) '
            f'SELECT {retained} FROM "{table}"{where}'
        )
        if "AUTOINCREMENT" in ddl.upper():
            statements.append(
                f"UPDATE sqlite_sequence SET seq = MAX(seq, "
                f"COALESCE((SELECT seq FROM sqlite_sequence WHERE name = '{table}'), 0)) "
                f"WHERE name = '{replacement}'"
            )
        statements.extend((f'DROP TABLE "{table}"',
                           f'ALTER TABLE "{replacement}" RENAME TO "{table}"'))
        for owner, sql in schema_objects:
            if owner == table and not any(
                re.search(rf'\b{column}\b', sql, re.IGNORECASE) for column in retired
            ):
                statements.append(sql)
    return statements


def retire_memory_flush_metadata(conn: sqlite3.Connection) -> None:
    """Apply the plan with a writer lock acquired before inspecting the schema.

    The versioned migration opts out of yoyo's deferred transaction because
    this operation owns its complete BEGIN IMMEDIATE/commit/rollback boundary.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        tables = {
            row[0]: row[1] for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('session_summaries', 'memory_durable_receipts')"
            )
        }
        columns = {
            table: [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
            for table in tables
        }
        objects = list(conn.execute(
            "SELECT tbl_name, sql FROM sqlite_master WHERE type IN ('index', 'trigger') "
            "AND tbl_name IN ('session_summaries', 'memory_durable_receipts') "
            "AND sql IS NOT NULL"
        ))
        for statement in memory_flush_retirement_statements(
            table_sql=tables, columns=columns, schema_objects=objects,
        ):
            conn.execute(statement)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
