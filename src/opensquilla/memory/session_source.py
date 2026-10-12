"""Derived memory-source documents for persisted session transcripts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .redaction import redact_memory_text
from .types import MemorySource

_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_INVALID_AGENT_CHARS_RE = re.compile(r"[^a-z0-9_-]")
_LEADING_TRAILING_DASHES_RE = re.compile(r"^-+|-+$")
_ROLE_LABELS = {
    "user": "User",
    "assistant": "Assistant",
}


@dataclass(frozen=True)
class SessionSourceDocument:
    path: str
    content: str
    mtime: float


@dataclass(frozen=True)
class SessionSourceSyncResult:
    indexed: int = 0
    removed: int = 0
    skipped: int = 0


def _safe_segment(value: str, *, fallback: str) -> str:
    cleaned = _SAFE_SEGMENT_RE.sub("-", value.strip()).strip("-._")
    return cleaned or fallback


def _normalize_agent_id(agent_id: object) -> str:
    raw = str(agent_id or "").strip().lower()
    if not raw or raw == "default":
        return "main"
    normalized = _INVALID_AGENT_CHARS_RE.sub("-", raw)
    normalized = _LEADING_TRAILING_DASHES_RE.sub("", normalized)[:64]
    if not normalized or normalized == "default":
        return "main"
    return normalized


def _redact_transcript_text(text: str) -> str:
    return redact_memory_text(text)


def _format_timestamp(ms: int | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def build_session_source_document(
    session: Any,
    entries: list[Any],
) -> SessionSourceDocument:
    """Render a session transcript into a deterministic derived source document."""
    agent_id = _normalize_agent_id(getattr(session, "agent_id", None) or "main")
    safe_agent = _safe_segment(agent_id, fallback="main")
    safe_session_id = _safe_segment(session.session_id, fallback="session")
    path = f"sessions/{safe_agent}/{safe_session_id}.md"

    lines = [
        "---",
        "source: sessions",
        f"session_id: {session.session_id}",
        f"agent_id: {agent_id}",
    ]
    updated_at = _format_timestamp(getattr(session, "updated_at", None))
    if updated_at:
        lines.append(f"updated_at: {updated_at}")
    label_value = (
        getattr(session, "label", None) or getattr(session, "display_name", None) or ""
    )
    label = label_value.strip()
    if label:
        lines.append(f"label: {_redact_transcript_text(label)}")
    lines.extend(["---", ""])

    rendered_entries = 0
    for entry in entries:
        label_for_role = _ROLE_LABELS.get(str(entry.role).lower())
        content = (entry.content or "").strip()
        if label_for_role is None or not content:
            continue
        rendered_entries += 1
        entry_id = entry.id if entry.id is not None else entry.message_id
        safe_content = _redact_transcript_text(content)
        lines.append(f"[entry {entry_id}] {label_for_role}: {safe_content}")

    if rendered_entries == 0:
        lines.append("(no user or assistant transcript content)")

    mtime = (getattr(session, "updated_at", None) or 0) / 1000
    return SessionSourceDocument(
        path=path,
        content="\n".join(lines).rstrip() + "\n",
        mtime=mtime,
    )


class SessionSourceIndexer:
    """Synchronize persisted session transcripts into the memory index."""

    def __init__(
        self,
        *,
        storage: Any,
        store: Any,
        agent_id: str = "main",
        max_sessions: int = 1000,
    ) -> None:
        self._storage: Any = storage
        self._store = store
        self._agent_id = _normalize_agent_id(agent_id)
        self._max_sessions = max(1, max_sessions)

    async def sync(self, *, force: bool = False) -> SessionSourceSyncResult:
        sessions, complete_scan = await self._list_sessions()
        expected_paths: set[str] = set()
        indexed = 0
        skipped = 0

        for session in sessions:
            entries = await self._storage.get_transcript(session.session_id)
            if not entries:
                skipped += 1
                continue
            document = build_session_source_document(session, entries)
            expected_paths.add(document.path)
            chunks = await self._store.index_file(
                path=document.path,
                content=document.content,
                source=MemorySource.sessions,
                mtime=document.mtime,
            )
            if chunks > 0 or force:
                indexed += 1

        removed = 0
        list_paths = getattr(self._store, "list_paths", None)
        if complete_scan and callable(list_paths):
            existing_paths = set(await list_paths(source=MemorySource.sessions))
            for stale_path in sorted(existing_paths - expected_paths):
                await self._store.remove_file(stale_path)
                removed += 1

        return SessionSourceSyncResult(indexed=indexed, removed=removed, skipped=skipped)

    async def _list_sessions(self) -> tuple[list[Any], bool]:
        page_size = min(100, self._max_sessions)
        offset = 0
        sessions: list[Any] = []
        while len(sessions) < self._max_sessions:
            limit = min(page_size, self._max_sessions - len(sessions))
            page = await self._storage.list_sessions(
                agent_id=self._agent_id,
                limit=limit,
                offset=offset,
            )
            sessions.extend(page)
            offset += len(page)
            if len(page) < limit:
                return sessions, True
            if not page:
                return sessions, True
        return sessions, False
