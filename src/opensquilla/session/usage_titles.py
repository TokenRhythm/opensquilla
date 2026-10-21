"""Meaningful usage titles, fenced to the exact live session generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from opensquilla.session.keys import parse_agent_id
from opensquilla.session.title_quality import is_refusal_title
from opensquilla.session.title_text import (
    _SESSION_TITLE_CONTENT_SQL,
    SESSION_TITLE_MAX_BODY_BYTES,
    _is_generic_webchat_title,
    derive_transcript_title,
)


def usage_metadata_title(node: Mapping[str, Any]) -> str:
    """Keep meaningful manual names ahead of generated titles and subjects."""
    key = str(node.get("session_key") or "")
    parsed_agent = parse_agent_id(key)
    agent_id = parsed_agent if parsed_agent != "main" else str(node.get("agent_id") or "main")
    webchat = ":webchat:" in key
    for field in ("display_name", "derived_title", "subject"):
        value = str(node.get(field) or "").strip()
        if not value or (webchat and _is_generic_webchat_title(value, agent_id)):
            continue
        if field == "derived_title" and is_refusal_title(value):
            continue
        return value[:240]
    return ""


async def resolve_usage_titles_on_conn(
    conn: Any, nodes: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int], str]:
    """Read at most three bounded user messages only for unnamed live owners.

    The generation check lives in the candidate query as well as its caller:
    a reset between metadata and transcript reads cannot lend the new task's
    first message to an older usage row. Content is read only by the captured
    immutable row IDs, using the same envelope projection as session lists.
    """
    titles = {
        (str(node["session_id"]), int(node["epoch"])): usage_metadata_title(node)
        for node in nodes
    }
    missing = [identity for identity, title in titles.items() if not title]
    for start in range(0, len(missing), 300):
        batch = missing[start:start + 300]
        values = ",".join("(?,?)" for _ in batch)
        async with conn.execute(
            f"""
            WITH requested(session_id, epoch) AS (VALUES {values}),
            live AS (
                SELECT * FROM requested WHERE EXISTS (
                    SELECT 1 FROM sessions
                    WHERE session_id = requested.session_id AND epoch = requested.epoch
                )
            ),
            candidates AS (
                SELECT 'active' AS source, live.session_id, live.epoch, entry.id,
                       entry.created_at, entry.id AS original_entry_id
                FROM live JOIN transcript_entries AS entry ON entry.id IN (
                    SELECT id FROM transcript_entries
                    WHERE session_id = live.session_id
                      AND role = 'user'
                      AND content_byte_length IS NOT NULL
                      AND content_byte_length > 0
                      AND content_byte_length <= {SESSION_TITLE_MAX_BODY_BYTES}
                    ORDER BY created_at ASC, id ASC LIMIT 3
                )
                UNION ALL
                SELECT 'archived' AS source, live.session_id, live.epoch, entry.id,
                       entry.created_at, entry.original_entry_id
                FROM live JOIN compacted_transcript_entries AS entry ON entry.id IN (
                    SELECT id FROM compacted_transcript_entries
                    WHERE session_id = live.session_id
                      AND role = 'user'
                      AND content_byte_length IS NOT NULL
                      AND content_byte_length > 0
                      AND content_byte_length <= {SESSION_TITLE_MAX_BODY_BYTES}
                    ORDER BY created_at ASC, original_entry_id ASC, id ASC LIMIT 3
                )
            ),
            ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY session_id, epoch
                    ORDER BY created_at, original_entry_id, id
                ) AS rn FROM candidates
            )
            SELECT source, session_id, epoch, id FROM ranked WHERE rn <= 3
            ORDER BY session_id, epoch, rn
            """,
            [value for identity in batch for value in identity],
        ) as cur:
            candidates = await cur.fetchall()
        if not candidates:
            continue
        active_ids = [int(row[3]) for row in candidates if row[0] == "active"]
        archived_ids = [int(row[3]) for row in candidates if row[0] == "archived"]
        active_placeholders = ",".join("?" for _ in active_ids) or "NULL"
        archived_placeholders = ",".join("?" for _ in archived_ids) or "NULL"
        async with conn.execute(
            f"SELECT 'active' AS source, id, {_SESSION_TITLE_CONTENT_SQL} AS content "
            f"FROM transcript_entries WHERE id IN ({active_placeholders}) "
            f"UNION ALL SELECT 'archived', id, {_SESSION_TITLE_CONTENT_SQL} "
            f"FROM compacted_transcript_entries WHERE id IN ({archived_placeholders})",
            [*active_ids, *archived_ids],
        ) as cur:
            contents = {(row[0], int(row[1])): row[2] for row in await cur.fetchall()}
        for source, session_id, epoch, entry_id in candidates:
            identity = (str(session_id), int(epoch))
            if not titles[identity]:
                titles[identity] = derive_transcript_title(contents.get((source, int(entry_id))))
    return titles
