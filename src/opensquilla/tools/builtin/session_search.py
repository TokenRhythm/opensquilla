"""Session search tool — transcript search with Unicode substring support.

Registered at boot time when a SessionStorage is available.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.resource_references import session_reference_v1
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
            # Match SessionDirectory's routing: the FTS sanitizer strips non-ASCII.
            search = (
                active_storage.search_transcript_like
                if any(ord(ch) > 127 for ch in query)
                else active_storage.search_transcript
            )
            results = await search(
                query=query,
                session_id=session_id,
                limit=limit,
            )
        except Exception as exc:
            logger.warning("session_search.error", query=query[:80], error=str(exc))
            return json.dumps({"query": query, "results": [], "error": "Search failed"})

        if not results:
            return json.dumps({"query": query, "results": [], "note": "No matches found."})

        # Transcript search rows contain excerpts, not session metadata. Read
        # the authoritative records and task ledger once for each matching key.
        keys = list(dict.fromkeys(str(row["session_key"]) for row in results))
        task_rows: dict[str, list[Any]] = {}
        list_tasks = getattr(active_storage, "list_agent_tasks_for_sessions", None)
        if callable(list_tasks):
            task_rows = await list_tasks(keys)
        references: dict[str, dict[str, Any]] = {}
        get_session = getattr(active_storage, "get_session", None)
        for key in keys:
            session = await get_session(key) if callable(get_session) else None
            title = next((
                value for field in ("display_name", "derived_title", "subject")
                if (value := getattr(session, field, None))
            ), key)
            tasks = sorted(
                task_rows.get(key, []),
                key=lambda task: getattr(task, "created_at", 0) or 0,
                reverse=True,
            )
            statuses = [str(getattr(task, "status", "")) for task in tasks]
            run_status = (
                "running" if "running" in statuses
                else "queued" if "queued" in statuses
                else "failed" if statuses and statuses[0] in {
                    "failed", "timeout", "abandoned", "cancelled",
                }
                else "idle"
            )
            available = session is not None
            references[key] = session_reference_v1(
                key, title=title, run_status=run_status if available else "missing",
                available=available, can_open=available,
            )

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
                        "title": references[r["session_key"]]["label"],
                        "runStatus": references[r["session_key"]]["state"]["runStatus"],
                        "reference": references[r["session_key"]],
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    logger.info("session_search_tool.registered")
