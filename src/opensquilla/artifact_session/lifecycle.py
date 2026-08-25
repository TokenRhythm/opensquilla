"""Session-bound ArtifactSession lifecycle cleanup.

Reset and delete rotate or remove the owning ``session_id``.  ArtifactSession
state must be retired in the *same* SQLite transaction as that boundary so a
failed cleanup cannot leave a newly-created session generation pointing at old
or partially removed document state.

The filesystem is deliberately not touched here.  Internal revision material
is removed only after the database transaction commits; a failed disk cleanup
therefore leaks reclaimable bytes rather than corrupting durable state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ArtifactSessionBoundary = Literal["session_reset", "session_delete"]


@dataclass(frozen=True, slots=True)
class ArtifactSessionPurgeResult:
    """Metadata about one transactional session-boundary purge."""

    document_count: int = 0
    edit_session_count: int = 0
    writer_lease_count: int = 0


async def _table_exists(conn: Any, table: str) -> bool:
    async with conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ) as cursor:
        return await cursor.fetchone() is not None


async def purge_session_on_connection(
    conn: Any,
    *,
    session_id: str,
    boundary: ArtifactSessionBoundary,
) -> ArtifactSessionPurgeResult:
    """Fence and delete ArtifactSession rows on an existing write transaction.

    This helper is intentionally compatible with databases predating V036: if
    the additive ArtifactSession tables are absent, it is a no-op.  Once the
    tables exist, any SQL/FK failure propagates so the caller's encompassing
    reset/delete transaction rolls back as a unit.
    """

    if not session_id:
        raise ValueError("session_id is required")
    if boundary not in {"session_reset", "session_delete"}:
        raise ValueError("unsupported ArtifactSession lifecycle boundary")
    if not await _table_exists(conn, "artifact_documents"):
        return ArtifactSessionPurgeResult()

    # Import reservations can exist before a Document is created. Retire every
    # session-owned journal row before the early no-document return; applied
    # receipts and publish attempts are removed here as part of the same
    # boundary instead of relying on foreign-key cascade ordering.
    for table in ("document_import_attempts", "document_publish_attempts"):
        if await _table_exists(conn, table):
            await conn.execute(
                f"DELETE FROM {table} WHERE session_id = ?",  # noqa: S608 - fixed literals
                (session_id,),
            )

    async with conn.execute(
        "SELECT COUNT(*) FROM artifact_documents WHERE session_id = ?",
        (session_id,),
    ) as cursor:
        row = await cursor.fetchone()
    document_count = int(row[0] if row is not None else 0)
    if document_count == 0:
        return ArtifactSessionPurgeResult()

    edit_session_count = 0
    if await _table_exists(conn, "artifact_edit_sessions"):
        cursor = await conn.execute(
            """
            UPDATE artifact_edit_sessions
            SET status = 'stale', state_revision = state_revision + 1,
                updated_at = CASE WHEN updated_at < last_access_at
                                  THEN last_access_at ELSE updated_at END
            WHERE document_id IN (
                SELECT document_id FROM artifact_documents WHERE session_id = ?
            ) AND status NOT IN ('closed', 'stale')
            """,
            (session_id,),
        )
        try:
            edit_session_count = max(0, int(cursor.rowcount or 0))
        finally:
            await cursor.close()

    writer_lease_count = 0
    if await _table_exists(conn, "artifact_writer_leases"):
        cursor = await conn.execute(
            """
            DELETE FROM artifact_writer_leases
            WHERE document_id IN (
                SELECT document_id FROM artifact_documents WHERE session_id = ?
            )
            """,
            (session_id,),
        )
        try:
            writer_lease_count = max(0, int(cursor.rowcount or 0))
        finally:
            await cursor.close()

    # Incrementing the document fence prevents a stale writer from winning if
    # it began before this boundary.  The rows are then removed by the same
    # transaction, cascading to revisions, annotations, edit sessions, and audit.
    await conn.execute(
        """
        UPDATE artifact_documents
        SET writer_fencing_token = writer_fencing_token + 1,
            state_revision = state_revision + 1
        WHERE session_id = ?
        """,
        (session_id,),
    )
    await conn.execute(
        "DELETE FROM artifact_documents WHERE session_id = ?",
        (session_id,),
    )
    return ArtifactSessionPurgeResult(
        document_count=document_count,
        edit_session_count=edit_session_count,
        writer_lease_count=writer_lease_count,
    )


__all__ = ["ArtifactSessionPurgeResult", "purge_session_on_connection"]
