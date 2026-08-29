"""Application seam for resolving session references.

The directory owns the lookup policy that used to live in the Gateway RPC
module: canonicalize first, try an exact storage lookup, then perform a
bounded compatibility scan for a unique id/title prefix.  Transport adapters
map :class:`SessionResolution` to their wire representation; this module does
not know about RPC frames, scopes, or ``RpcContext``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast

import structlog

from opensquilla.session_key import canonicalize_session_key

log = structlog.get_logger(__name__)


class SessionDirectoryStorage(Protocol):
    """Minimal persistence Port required by the directory."""

    async def get_session(self, key: str) -> Any | None: ...

    async def list_sessions(self, *, limit: int = 100) -> Sequence[Any]: ...


class SessionSearchStorage(SessionDirectoryStorage, Protocol):
    """Optional search ports implemented by the persistence adapter.

    Search is deliberately expressed as capability-shaped methods instead of
    importing the concrete SQLite store.  Older test doubles and storage
    implementations may omit the LIKE/title methods; ``SessionDirectory``
    keeps the historical bounded fallbacks in that case.
    """

    async def search_sessions_by_title(
        self, query: str, limit: int = 20
    ) -> Sequence[Any]: ...

    async def search_transcript(
        self, query: str, session_id: str | None = None, limit: int = 20
    ) -> Sequence[Mapping[str, Any]]: ...

    async def search_transcript_like(
        self, query: str, session_id: str | None = None, limit: int = 20
    ) -> Sequence[Mapping[str, Any]]: ...

    async def get_session(self, key: str) -> Any | None: ...


@dataclass(frozen=True, slots=True)
class SessionSearchProjection:
    """Presentation-neutral projection supplied by a boundary adapter."""

    title: str
    effective_agent_id: str | None = None
    surface: str | None = None
    updated_at: int | None = None


@dataclass(frozen=True, slots=True)
class SessionSearchSessionHit:
    key: str
    projection: SessionSearchProjection


@dataclass(frozen=True, slots=True)
class SessionSearchMessageHit:
    key: str
    title: str
    role: Any
    snippet: str
    created_at: Any


@dataclass(frozen=True, slots=True)
class SessionSearchResult:
    query: str
    sessions: tuple[SessionSearchSessionHit, ...]
    messages: tuple[SessionSearchMessageHit, ...]
    ts: int


@dataclass(frozen=True, slots=True)
class SessionResolution:
    """Stable application result independent of storage and transport."""

    key: str
    session_id: str
    status: str
    agent_id: str
    model: str | None
    workspace_id: str | None
    created_at: int
    updated_at: int


class SessionDirectory:
    """Resolve an external session reference through one application seam."""

    # Keep the historical compatibility bound.  A later query module may
    # replace this scan with an indexed lookup, but changing it here would
    # change the v4 behaviour of both resolve and bootstrap.
    _LEGACY_SCAN_LIMIT = 500

    def __init__(self, storage: SessionDirectoryStorage) -> None:
        self._storage = storage

    @staticmethod
    def normalize_search_input(query: object = "", limit: object = 20) -> tuple[str, int]:
        """Normalize the legacy ``sessions.search`` inputs in one place."""

        normalized_query = str(query or "").strip()
        try:
            normalized_limit = int(cast(Any, limit))
        except (TypeError, ValueError):
            normalized_limit = 20
        return normalized_query, max(1, min(normalized_limit, 50))

    async def search(
        self,
        query: object = "",
        limit: object = 20,
        *,
        now_ms: int,
        project: Callable[[Any, str], SessionSearchProjection],
        derive_transcript_title: Callable[[str], str] | None = None,
    ) -> SessionSearchResult:
        """Run the session directory query behind storage-independent ports.

        ``project`` is the only presentation hook.  It is supplied by the
        Gateway adapter and returns a small domain projection; the application
        module never imports Gateway view or RPC code.  Transcript index and
        enrichment failures remain best-effort, while title-index failures
        retain the historical propagation semantics of the v4 handler.
        """

        normalized_query, normalized_limit = self.normalize_search_input(query, limit)
        if not normalized_query:
            return SessionSearchResult(normalized_query, (), (), now_ms)

        title_sessions: Sequence[Any] = ()
        title_search = getattr(self._storage, "search_sessions_by_title", None)
        if callable(title_search):
            # The historical handler lets title-index failures propagate.  The
            # application seam must preserve that contract; only transcript
            # enrichment/search failures are best-effort.
            title_sessions = await title_search(normalized_query, normalized_limit)
        else:
            recent = await self._storage.list_sessions(limit=200)
            needle = normalized_query.lower()
            title_sessions = [
                session
                for session in recent
                if needle
                in " ".join(
                    text
                    for text in (
                        str(getattr(session, "display_name", "") or ""),
                        str(getattr(session, "derived_title", "") or ""),
                        str(getattr(session, "subject", "") or ""),
                    )
                    if text
                ).lower()
            ][:normalized_limit]

        title_inputs = await self._transcript_titles(title_sessions, derive_transcript_title)
        title_hits: list[SessionSearchSessionHit] = []
        title_keys: set[str] = set()
        for session in title_sessions:
            key = str(getattr(session, "session_key", "") or "")
            if not key:
                continue
            projection = project(
                session,
                title_inputs.get(str(getattr(session, "session_id", "") or ""), ""),
            )
            title_keys.add(canonicalize_session_key(key))
            title_hits.append(SessionSearchSessionHit(key=key, projection=projection))

        rows: Sequence[Mapping[str, Any]] = ()
        try:
            non_ascii = any(ord(ch) > 127 for ch in normalized_query)
            if non_ascii:
                search_like = getattr(self._storage, "search_transcript_like", None)
                if callable(search_like):
                    rows = await search_like(normalized_query, limit=normalized_limit)
            else:
                search_fts = getattr(self._storage, "search_transcript", None)
                if callable(search_fts):
                    rows = await search_fts(normalized_query, limit=normalized_limit)
        except Exception:
            log.warning("sessions.search.transcript_failed", exc_info=True)
            rows = ()

        pending: list[tuple[str, str, Mapping[str, Any]]] = []
        content_keys: set[str] = set()
        for row in rows:
            raw_key = str(row.get("session_key") or "")
            canonical_key = canonicalize_session_key(raw_key)
            if not canonical_key or canonical_key in title_keys or canonical_key in content_keys:
                continue
            content_keys.add(canonical_key)
            pending.append((raw_key, canonical_key, row))

        enriched_sessions: list[Any] = []
        get_session = getattr(self._storage, "get_session", None)
        if callable(get_session):
            for _, canonical_key, _ in pending:
                try:
                    session = await get_session(canonical_key)
                except Exception:
                    session = None
                if session is not None:
                    enriched_sessions.append(session)
        enriched_titles = await self._transcript_titles(enriched_sessions, derive_transcript_title)
        title_by_key: dict[str, str] = {}
        for session in enriched_sessions:
            key = str(getattr(session, "session_key", "") or "")
            title_by_key[canonicalize_session_key(key)] = project(
                session,
                enriched_titles.get(str(getattr(session, "session_id", "") or ""), ""),
            ).title

        message_hits = tuple(
            SessionSearchMessageHit(
                key=raw_key,
                title=title_by_key.get(canonical_key, ""),
                role=row.get("role"),
                snippet=row.get("snippet") or "",
                created_at=row.get("created_at"),
            )
            for raw_key, canonical_key, row in pending
        )
        return SessionSearchResult(
            normalized_query,
            tuple(title_hits),
            message_hits,
            now_ms,
        )

    async def _transcript_titles(
        self,
        sessions: Sequence[Any],
        derive_title: Callable[[str], str] | None,
    ) -> dict[str, str]:
        """Read a bounded amount of user transcript content for enrichment."""

        if derive_title is None:
            return {}
        session_ids = [
            str(getattr(session, "session_id", "") or "")
            for session in sessions
            if str(getattr(session, "session_id", "") or "")
        ]
        if not session_ids:
            return {}
        title_inputs: dict[str, list[str]] = {session_id: [] for session_id in session_ids}
        batch = getattr(self._storage, "list_user_transcript_content_batch", None)
        if callable(batch):
            try:
                grouped = await batch(session_ids, limit_per_session=3)
                title_inputs.update(
                    {
                        str(session_id): [str(value) for value in values if value]
                        for session_id, values in grouped.items()
                    }
                )
            except Exception:
                pass
        if not any(title_inputs.values()):
            get_transcript = getattr(self._storage, "get_transcript", None)
            if callable(get_transcript):
                for session_id in session_ids:
                    try:
                        entries = await get_transcript(session_id, limit=8)
                    except Exception:
                        continue
                    title_inputs[session_id] = [
                        str(getattr(entry, "content", "") or "")
                        for entry in entries
                        if str(getattr(entry, "role", "") or "").lower() == "user"
                    ][:3]
        titles: dict[str, str] = {}
        for session_id, values in title_inputs.items():
            for value in values:
                title = derive_title(value)
                if title:
                    titles[session_id] = title
                    break
        return titles

    async def resolve(self, reference: str) -> SessionResolution:
        session = await _resolve_session_record(self._storage, reference)
        return SessionResolution(
            key=session.session_key,
            session_id=session.session_id,
            status=session.status,
            agent_id=session.agent_id,
            model=getattr(session, "model", None),
            workspace_id=getattr(session, "workspace_id", None),
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


async def _resolve_session_record(
    storage: SessionDirectoryStorage,
    reference: str,
) -> Any:
    """Return the persisted record using the historical exact/prefix policy."""

    key = canonicalize_session_key(reference)
    session = await storage.get_session(key)
    if session is not None:
        return session

    sessions = await storage.list_sessions(limit=SessionDirectory._LEGACY_SCAN_LIMIT)
    matches: list[Any] = []
    for candidate in sessions:
        values = (
            getattr(candidate, "session_key", ""),
            getattr(candidate, "session_id", ""),
            getattr(candidate, "display_name", "") or "",
            getattr(candidate, "derived_title", "") or "",
        )
        if any(
            str(value) == key or str(value).startswith(key)
            for value in values
            if value
        ):
            matches.append(candidate)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        candidates = ", ".join(
            str(getattr(match, "session_key", "")) for match in matches[:5]
        )
        raise ValueError(f"Ambiguous session id {key!r}; matches: {candidates}")
    raise KeyError(f"Session not found: {key}")


async def _resolve_session_record_for_bootstrap(
    storage: SessionDirectoryStorage,
    reference: str,
) -> Any:
    """Transitional record Port for the legacy bootstrap composition.

    ``sessions.bootstrap`` still needs the complete persistence record for its
    existing history/epoch assembly.  ConversationRuntime (S10) will consume
    the application result directly and remove this escape hatch.
    """

    return await _resolve_session_record(storage, reference)


__all__ = [
    "SessionDirectory",
    "SessionDirectoryStorage",
    "SessionSearchMessageHit",
    "SessionSearchProjection",
    "SessionSearchResult",
    "SessionSearchSessionHit",
    "SessionSearchStorage",
    "SessionResolution",
]
