"""Generate disposable session-title startup data; never open a real profile."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


async def seed(database: Path, named: int, unnamed: int, source: Path) -> dict:
    sys.path.insert(0, str(source / "src"))
    from opensquilla.session.storage import SessionStorage

    if database.exists():
        raise RuntimeError("Refuse to overwrite an existing database")
    database.parent.mkdir(parents=True, exist_ok=True)
    sessions = []
    async with SessionStorage(str(database)) as storage:
        for index in range(named + unnamed):
            suffix = f"{index:03d}"
            key = f"agent:main:webchat:synthetic-title-startup-{suffix}"
            session_id = f"synthetic-title-startup-{suffix}"
            title = f"Synthetic saved title {suffix}" if index < named else None
            body = f"Synthetic body title {suffix}"
            timestamp = 1_792_497_600_000 - index * 1000
            await storage.conn.execute(
                """INSERT INTO sessions
                (session_key, session_id, created_at, updated_at, status,
                 display_name, derived_title, channel, chat_type, model_routing_mode)
                VALUES (?, ?, ?, ?, 'done', 'WebChat', ?, 'webchat', 'direct', 'direct')""",
                (key, session_id, timestamp, timestamp, title),
            )
            for message_index in range(3):
                await storage.conn.execute(
                    """INSERT INTO transcript_entries
                    (session_id, session_key, message_id, role, content,
                     content_byte_length, created_at)
                    VALUES (?, ?, ?, 'user', ?, NULL, ?)""",
                    (
                        session_id, key, f"{session_id}-message-{message_index}",
                        body if message_index == 0 else f"{body} followup {message_index}",
                        timestamp + message_index,
                    ),
                )
            sessions.append({
                "key": key, "storedTitle": title, "bodyTitle": body,
                "expectedTitle": title or body,
            })
        await storage.conn.commit()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        pending = connection.execute(
            "SELECT COUNT(*) FROM transcript_entries WHERE content_byte_length IS NULL"
        ).fetchone()[0]
    manifest = {
        "kind": "opensquilla-synthetic-session-title-startup-v1",
        "named": named, "unnamed": unnamed, "pendingRows": pending,
        "databaseSha256": hashlib.sha256(database.read_bytes()).hexdigest(),
        "sessions": sessions,
    }
    database.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8",
    )
    return manifest


def complete_metadata(database: Path) -> dict:
    """Prepare the second-launch index state only after the process has exited."""
    with sqlite3.connect(database) as connection:
        keys = connection.execute("SELECT session_key FROM sessions").fetchall()
        if not keys or any(
            not key.startswith("agent:main:webchat:synthetic-title-startup-")
            for (key,) in keys
        ):
            raise RuntimeError("Refuse metadata writes outside the synthetic fixture")
        before = connection.execute(
            "SELECT COUNT(*) FROM transcript_entries WHERE content_byte_length IS NULL"
        ).fetchone()[0]
        connection.execute(
            """UPDATE transcript_entries
            SET content_byte_length = length(CAST(content AS BLOB))
            WHERE content_byte_length IS NULL"""
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"completedRows": before, "method": "offline-synthetic-metadata-completion"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--named", type=int, default=50)
    parser.add_argument("--unnamed", type=int, default=0)
    parser.add_argument("--complete-metadata", action="store_true")
    args = parser.parse_args()
    if args.complete_metadata:
        result = complete_metadata(args.database)
    else:
        if args.source is None or min(args.named, args.unnamed) < 0:
            parser.error("seed requires --source and nonnegative session counts")
        result = asyncio.run(seed(args.database, args.named, args.unnamed, args.source))
    print(json.dumps(result))
