"""Bounded historical title recovery shared by Gateway read projections."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from opensquilla.gateway.session_view import derive_transcript_title, has_refused_chat_title


async def read_refused_title_fallbacks(
    storage: Any,
    sessions: Sequence[Any],
    *,
    channel_types: dict[str, str] | None = None,
) -> dict[str, str]:
    """Read original user text only for sessions with a refused automatic title.

    Include the compaction archive so the fallback remains stable when older
    messages leave the active transcript. Empty values explicitly select the
    caller's default title when no visible user text is available.
    """

    session_ids = list(
        dict.fromkeys(
            str(getattr(session, "session_id", "") or "")
            for session in sessions
            if getattr(session, "session_id", None)
            and has_refused_chat_title(session, channel_types=channel_types)
        )
    )
    if not session_ids:
        return {}
    title_inputs = await storage.list_canonical_user_transcript_content_batch(
        session_ids, limit_per_session=3
    )
    return {
        session_id: next(
            (
                title
                for content in title_inputs.get(session_id, [])
                if (title := derive_transcript_title(content))
            ),
            "",
        )
        for session_id in session_ids
    }
