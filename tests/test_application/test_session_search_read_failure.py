"""Budgeted search failures cannot become authoritative empty results."""

import asyncio
import time
from types import SimpleNamespace

import pytest

from opensquilla.application.session_directory import SessionDirectory, SessionSearchProjection
from opensquilla.session.models import SessionNode
from opensquilla.session.recovery_reads import ReadCapacityError, recovery_read_scope
from opensquilla.session.storage import SessionStorage, StorageBusyError


@pytest.mark.parametrize("query", ["search", "搜索"])
@pytest.mark.parametrize("cause_type", [ReadCapacityError, TimeoutError])
async def test_budgeted_transcript_search_propagates_storage_failure(query, cause_type) -> None:
    class Storage:
        async def search_sessions_by_title(self, *args):
            return []

        async def search_transcript(self, *args, **kwargs):
            # Match SessionStorage._run_recovery_read's public error chain.
            raise StorageBusyError(
                "search", waited_ms=7, retry_after_ms=100, stage="permit",
            ) from cause_type("read unavailable")

        search_transcript_like = search_transcript

    with recovery_read_scope("search:test", deadline=time.monotonic() + 1):
        with pytest.raises(StorageBusyError, match="temporarily busy"):
            await SessionDirectory(Storage()).search(query, now_ms=1, project=lambda *args: None)


async def test_budgeted_missing_fts_keeps_actual_title_hits(tmp_path) -> None:
    storage = await SessionStorage.open(tmp_path / "missing-fts.db")
    key = "agent:main:webchat:search-title"
    await storage.upsert_session(SessionNode(
        session_key=key, session_id="sid", agent_id="main", display_name="Search planning",
    ))
    await storage.conn.execute("DROP TABLE transcript_fts")
    try:
        with recovery_read_scope("search:test", deadline=time.monotonic() + 2):
            result = await SessionDirectory(storage).search(
                "search", now_ms=1,
                project=lambda node, _: SessionSearchProjection(title=node.display_name),
            )
        assert [hit.key for hit in result.sessions] == [key]
        assert result.messages == ()
    finally:
        await storage.close()


class OptionalFailureStorage:
    async def search_sessions_by_title(self, *args):
        return [SimpleNamespace(
            session_key="agent:main:webchat:title", session_id="title", display_name="Search title",
        )]

    async def search_transcript(self, *args, **kwargs):
        return [{"session_key": "agent:main:webchat:message", "snippet": "search message"}]

    async def get_session(self, *args):
        raise ValueError("optional session enrichment unavailable")

    async def list_user_transcript_content_batch(self, *args, **kwargs):
        raise RuntimeError("optional batched title enrichment unavailable")

    async def get_transcript(self, *args, **kwargs):
        raise RuntimeError("optional legacy title enrichment unavailable")


async def test_budgeted_optional_enrichment_failures_keep_title_and_message_hits() -> None:
    with recovery_read_scope("search:test", deadline=time.monotonic() + 1):
        result = await SessionDirectory(OptionalFailureStorage()).search(
            "search", now_ms=1,
            project=lambda node, _: SessionSearchProjection(title=node.display_name),
            derive_transcript_title=lambda text: text,
        )
    assert result.sessions[0].projection.title == "Search title"
    assert result.messages[0].snippet == "search message"
    assert result.messages[0].title == ""


@pytest.mark.parametrize("failure", ["deadline", "cancelled"])
async def test_lost_budget_is_not_hidden_by_optional_enrichment_failure(failure: str) -> None:
    with recovery_read_scope("search:test", deadline=time.monotonic() + 1) as budget:
        if failure == "deadline":
            budget.deadline = time.monotonic() - 1
            error = TimeoutError
        else:
            budget.cancel_token.cancel()
            error = asyncio.CancelledError
        with pytest.raises(error):
            await SessionDirectory(OptionalFailureStorage()).search(
                "search", now_ms=1,
                project=lambda node, _: SessionSearchProjection(title=node.display_name),
                derive_transcript_title=lambda text: text,
            )
