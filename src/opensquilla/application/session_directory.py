"""Application seam for resolving session references.

The directory owns the lookup policy that used to live in the Gateway RPC
module: canonicalize first, try an exact storage lookup, then perform a
bounded compatibility scan for a unique id/title prefix.  Transport adapters
map :class:`SessionResolution` to their wire representation; this module does
not know about RPC frames, scopes, or ``RpcContext``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from opensquilla.session_key import canonicalize_session_key


class SessionDirectoryStorage(Protocol):
    """Minimal persistence Port required by the directory."""

    async def get_session(self, key: str) -> Any | None: ...

    async def list_sessions(self, *, limit: int = 100) -> Sequence[Any]: ...


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
    "SessionResolution",
]
