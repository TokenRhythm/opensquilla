"""Session search tool — FTS5-powered transcript full-text search.

Registered at boot time when a SessionStorage is available.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from opensquilla.session.compaction import extract_anchors_from_summary
from opensquilla.tools.registry import ToolRegistry, tool
from opensquilla.tools.types import PlanAccess, ToolError, current_tool_context

if TYPE_CHECKING:
    from opensquilla.session.storage import SessionStorage

logger = structlog.get_logger(__name__)

_storage: SessionStorage | None = None


class AnchorResolution(StrEnum):
    """Epistemic result of resolving a declared compaction anchor."""

    RESOLVED = "resolved"
    DECLARED_UNAVAILABLE = "declared_unavailable"
    UNKNOWN = "unknown"


async def _lookup_anchor(
    storage: SessionStorage,
    *,
    session_id: str,
    anchor: str,
    limit: int,
) -> tuple[AnchorResolution, list[dict[str, Any]]]:
    """Resolve one anchor without turning missing evidence into non-existence."""
    results = await storage.search_transcript(
        session_id=session_id,
        limit=limit,
        anchor=anchor,
    )
    if results:
        return AnchorResolution.RESOLVED, results

    compaction_index_text, entry_anchor_id = anchor.split(":", 1)
    compaction_index = int(compaction_index_text)
    declared = False
    for summary in await storage.get_all_summaries(session_id):
        anchors = list(summary.extracted_anchors or [])
        # Legacy summaries may predate the extracted_anchors column while
        # still carrying valid references in their text.
        anchors.extend(extract_anchors_from_summary(summary.summary_text))
        if any(
            candidate.get("compaction_index") == compaction_index
            and candidate.get("entry_anchor_id") == entry_anchor_id
            for candidate in anchors
        ):
            declared = True
            break

    resolution = (
        AnchorResolution.DECLARED_UNAVAILABLE if declared else AnchorResolution.UNKNOWN
    )
    logger.warning(
        "session_search.anchor_resolution_obligation",
        session_id=session_id,
        anchor=anchor,
        resolution=resolution.value,
    )
    return resolution, []


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
            "Full-text search across persisted session transcripts, including entries "
            "archived during compaction. Returns matching excerpts with session context. "
            "Use when exact prior chat wording, transcript context, or code snippets "
            "from persisted sessions are needed. Ordinary recall should start with "
            "memory_search, which defaults to curated memory source files. To search "
            "indexed session snippets through memory_search, use source=sessions or "
            "source=all. Compaction summary anchors can be expanded by passing their "
            "'<compaction_index>:<entry_anchor_id>' value as anchor; the current "
            "session is selected automatically. Anchor lookup distinguishes resolved, "
            "declared-but-unavailable, and unknown references. session_search does not "
            "search MEMORY.md or memory/**/*.md."
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
            "anchor": {
                "type": "string",
                "description": (
                    "Exact anchor reference from a compaction summary, "
                    "format: '<compaction_index>:<entry_anchor_id>'. "
                    "When provided, returns the original transcript entry "
                    "verbatim. Defaults to the current session; session_id is "
                    "needed only for an explicit cross-session lookup. A missing "
                    "declared anchor is reported separately from an anchor the "
                    "session never declared."
                ),
            },
        },
        required=[],  # query or anchor — validated at runtime (B5)
        owner_only=True,
        plan_access=PlanAccess.READ_ONLY,
        registry=registry,
    )
    async def session_search(
        query: str = "",
        session_id: str | None = None,
        limit: int = 20,
        anchor: str | None = None,
    ) -> str:
        if active_storage is None:
            raise ToolError("Session storage not available")

        if not query.strip() and anchor is None:
            raise ToolError("Query must not be empty (unless anchor is provided)")

        limit = max(1, min(50, limit))
        if anchor is not None:
            parts = anchor.split(":", 1)
            if len(parts) != 2:
                raise ToolError(
                    "Invalid anchor; expected '<compaction_index>:entry_NNN'"
                )
            compaction_index_text, entry_anchor_id = parts
            if (
                not compaction_index_text.isdigit()
                or not entry_anchor_id.startswith("entry_")
                or not entry_anchor_id.removeprefix("entry_").isdigit()
            ):
                raise ToolError(
                    "Invalid anchor; expected '<compaction_index>:entry_NNN'"
                )
        resolved_session_id = session_id
        if anchor is not None and not resolved_session_id:
            ctx = current_tool_context.get()
            if ctx is not None and ctx.session_key:
                current_session = await active_storage.get_session(ctx.session_key)
                if current_session is not None:
                    resolved_session_id = current_session.session_id
            if not resolved_session_id:
                raise ToolError(
                    "Anchor lookup requires an active session context or session_id"
                )

        try:
            anchor_resolution: AnchorResolution | None = None
            if anchor is not None:
                assert resolved_session_id is not None
                anchor_resolution, results = await _lookup_anchor(
                    active_storage,
                    session_id=resolved_session_id,
                    anchor=anchor,
                    limit=limit,
                )
            else:
                results = await active_storage.search_transcript(
                    query=query,
                    session_id=resolved_session_id,
                    limit=limit,
                )
        except Exception as exc:
            logger.warning("session_search.error", query=query[:80], error=str(exc))
            return json.dumps({"query": query, "results": [], "error": "Search failed"})

        if not results:
            if anchor_resolution is AnchorResolution.DECLARED_UNAVAILABLE:
                return json.dumps(
                    {
                        "anchor": anchor,
                        "anchor_resolution": anchor_resolution.value,
                        "results": [],
                        "note": (
                            "This anchor was declared by the current session, but its "
                            "archived source is unavailable. Exact recovery is not "
                            "possible; try transcript keyword search."
                        ),
                    }
                )
            if anchor_resolution is AnchorResolution.UNKNOWN:
                return json.dumps(
                    {
                        "anchor": anchor,
                        "anchor_resolution": anchor_resolution.value,
                        "results": [],
                        "note": (
                            "This anchor is not declared by the current session. It may "
                            "be model-generated or belong to a different session."
                        ),
                    }
                )
            return json.dumps({"query": query, "results": [], "note": "No matches found."})

        return json.dumps(
            {
                "query": query,
                **(
                    {"anchor_resolution": anchor_resolution.value}
                    if anchor_resolution is not None
                    else {}
                ),
                "result_count": len(results),
                "results": [
                    {
                        "session_key": r["session_key"],
                        "role": r["role"],
                        "snippet": r["snippet"],
                        "created_at": r["created_at"],
                        "source": r.get("source", "active"),
                        "anchor": r.get("anchor"),
                    }
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

    logger.info("session_search_tool.registered")
