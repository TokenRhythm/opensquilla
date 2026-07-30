"""Session search tool — FTS5-powered transcript full-text search.

Registered at boot time when a SessionStorage is available.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import structlog

from opensquilla.tools.registry import ToolRegistry, tool
from opensquilla.tools.types import PlanAccess, ToolError

if TYPE_CHECKING:
    from opensquilla.session.storage import SessionStorage

logger = structlog.get_logger(__name__)

_storage: SessionStorage | None = None


def create_session_search_tool(
    storage: SessionStorage,
    *,
    registry: ToolRegistry | None = None,
) -> None:
    """Register session_search tool with the global registry."""
    global _storage
    _storage = storage
    active_storage = storage

    @tool(
        name="session_search",
        description=(
            "Full-text search across persisted session transcripts. Returns matching "
            "excerpts with session context. Use when exact prior chat wording, "
            "transcript context, or code snippets from persisted sessions are needed. "
            "Ordinary recall should start with memory_search, which defaults to "
            "curated memory source files. To search indexed session snippets through "
            "memory_search, use source=sessions or source=all. session_search does "
            "not search MEMORY.md or memory/**/*.md."
        ),
        params={
            "query": {
                "type": "string",
                "description": "Search query - natural language terms to find in transcripts.",
            },
            "session_id": {
                "type": "string",
                "description": "Optional: restrict search to a specific session ID.",
            },
            "limit": {
                "type": "integer",
                "description": "Max results to return (1-50, default 20).",
            },
        },
        required=["query"],
        owner_only=True,
        plan_access=PlanAccess.READ_ONLY,
        registry=registry,
    )
    async def session_search(
        query: str,
        session_id: str | None = None,
        limit: int = 20,
    ) -> str:
        if active_storage is None:
            raise ToolError("Session storage not available")

        if not query.strip():
            raise ToolError("Query must not be empty")

        limit = max(1, min(50, limit))

        try:
            results = await active_storage.search_transcript(
                query=query,
                session_id=session_id,
                limit=limit,
            )
        except Exception as exc:
            logger.warning("session_search.error", query=query[:80], error=str(exc))
            return json.dumps({"query": query, "results": [], "error": "Search failed"})

        if not results:
            return json.dumps({"query": query, "results": [], "note": "No matches found."})

        return json.dumps(
            {
                "query": query,
                "result_count": len(results),
                "results": [
                    {
                        "session_key": r["session_key"],
                        "role": r["role"],
                        "snippet": r["snippet"],
                        "created_at": r["created_at"],
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    logger.info("session_search_tool.registered")
