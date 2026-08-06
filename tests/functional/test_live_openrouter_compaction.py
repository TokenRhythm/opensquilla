"""Opt-in live OpenRouter compaction smoke.

Uses only public synthetic text and skips unless explicitly enabled with local
credentials. The goal is to prove the compaction path can complete with a real
LLM call without making normal test runs credentialed or costful.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from opensquilla.session.compaction import CompactionConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

pytestmark = [pytest.mark.llm, pytest.mark.llm_smoke, pytest.mark.agent_context_boundary]


@pytest.mark.asyncio
async def test_live_openrouter_session_compaction_succeeds(tmp_path: Path) -> None:
    if os.environ.get("OPENSQUILLA_LIVE_COMPACTION_E2E") != "1":
        pytest.skip("set OPENSQUILLA_LIVE_COMPACTION_E2E=1 to run live compaction smoke")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY not set")
    assert api_key is not None

    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:live-compaction"
    try:
        await manager.create(session_key)
        for index in range(12):
            await manager.append_message(
                session_key,
                "user" if index % 2 == 0 else "assistant",
                (
                    f"Synthetic compaction fact {index}: "
                    "the public dummy project uses alpha, beta, and gamma markers. "
                    "Keep the current goal, completed steps, failures, and next action. " * 20
                ),
                token_count=450,
            )

        result = await manager.compact_with_result(
            session_key,
            context_window_tokens=int(
                os.environ.get("LLM_TEST_CONTEXT_WINDOW_TOKENS", "2000")
            ),
            config=CompactionConfig(
                model=os.environ.get("LLM_TEST_MODEL", "openai/gpt-4o-mini"),
                api_key=api_key,
                base_url=os.environ.get(
                    "OPENROUTER_BASE_URL",
                    "https://openrouter.ai/api/v1",
                ),
                timeout_seconds=90,
            ),
        )

        assert result.summary
        assert result.summary_source == "llm"
        assert result.removed_count > 0
        assert result.chunks_processed >= 1
        assert result.tokens_after < result.tokens_before

        node = await manager._storage.get_session(session_key)
        assert node is not None
        assert node.compaction_count == 1
        assert await manager.get_transcript(session_key)
        summaries = await manager.get_summaries(session_key)
        assert [summary.summary_text for summary in summaries] == [result.summary]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_live_openrouter_coding_profile_preserves_recent_tail(
    tmp_path: Path,
) -> None:
    if os.environ.get("OPENSQUILLA_LIVE_COMPACTION_E2E") != "1":
        pytest.skip("set OPENSQUILLA_LIVE_COMPACTION_E2E=1 to run live compaction smoke")
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY not set")
    assert api_key is not None

    storage = SessionStorage(str(tmp_path / "sessions.db"))
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:live-coding-profile-compaction"
    protected_tail = [
        (
            "user",
            "Current task: keep the beta migration open and verify the next shell check.",
        ),
        ("assistant", "Acknowledged current task and pending verification."),
        ("user", "Do not lose marker PROFILE-LIVE-TAIL-001."),
        ("assistant", "Marker PROFILE-LIVE-TAIL-001 is still active."),
    ]
    try:
        await manager.create(session_key)
        for index in range(14):
            await manager.append_message(
                session_key,
                "user" if index % 2 == 0 else "assistant",
                (
                    f"Synthetic coding history {index}: "
                    "completed setup, reviewed logs, and kept neutral public markers. "
                    "Summarize this older context without private data. " * 20
                ),
                token_count=450,
            )
        for role, content in protected_tail:
            await manager.append_message(
                session_key,
                role,
                content,
                token_count=20,
            )

        result = await manager.compact_with_result(
            session_key,
            context_window_tokens=2_000,
            config=CompactionConfig(
                model=os.environ.get("LLM_TEST_MODEL", "openai/gpt-4o-mini"),
                api_key=api_key,
                base_url=os.environ.get(
                    "OPENROUTER_BASE_URL",
                    "https://openrouter.ai/api/v1",
                ),
                timeout_seconds=90,
                compaction_profile="coding",
                protected_recent_messages=len(protected_tail),
            ),
        )

        assert result.summary
        assert result.summary_source == "llm"
        assert result.removed_count > 0
        assert result.quality_report["profile"] == "coding"
        assert result.quality_report["protected_tail_preserved"] is True
        assert result.quality_report["fits_context_window"] is True
        assert result.quality_report["passes_structural_gate"] is True

        transcript = await manager.get_transcript(session_key)
        tail_contents = [
            entry.content or ""
            for entry in transcript[-len(protected_tail) :]
        ]
        assert all(
            expected_content in actual_content
            for actual_content, (_, expected_content) in zip(
                tail_contents,
                protected_tail,
                strict=True,
            )
        )
    finally:
        await storage.close()
