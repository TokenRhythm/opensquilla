"""Request-only recovery must preserve durable state and current source ownership."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.session.compaction import CompactionResult
from opensquilla.session.compaction_lifecycle import (
    CompactionTimeoutError,
    ConsumerAdmissionStaleError,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionSummary
from opensquilla.session.storage import SessionStorage


class FailingSummaryManager(SessionManager):
    attempts = 0
    failure = "empty"

    async def compact_with_result(self, session_key, context_window_tokens, config=None, **kwargs):
        self.attempts += 1
        if self.failure == "cancel":
            raise asyncio.CancelledError
        if self.failure == "timeout":
            raise CompactionTimeoutError("summarizing", 0.01)
        return CompactionResult(
            summary="", kept_entries=[], removed_count=0, chunks_processed=1,
            summary_source="skipped", skip_reason="summary_failed",
        )


@pytest.fixture
async def history(tmp_path):
    storage = SessionStorage(str(tmp_path / "request-window.sqlite"))
    await storage.connect()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = FailingSummaryManager(
        storage, inject_time_prefix=False, checkpoint_workspace_dir=str(workspace),
    )
    key = "agent:main:synthetic-request-window"
    session = await manager.create(key)
    try:
        yield storage, manager, key, session
    finally:
        await storage.close()


async def populate(manager, key, *, tokens=300):
    for index in range(8):
        await manager.append_message(
            key, "user" if index % 2 == 0 else "assistant",
            f"synthetic item {index} " + "x" * 500, token_count=tokens,
        )
    return list(await manager.get_transcript(key))


def history_agent():
    return SimpleNamespace(
        provider=SimpleNamespace(provider_name="test"),
        config=SimpleNamespace(
            materialize_historical_attachments=False, preserve_historical_images=False,
            workspace_dir=None, model_capabilities=None,
        ),
        set_history=MagicMock(), set_request_image_context=MagicMock(),
    )


async def fingerprint(manager, key):
    return [entry.model_dump(mode="json") for entry in await manager.get_canonical_transcript(key)]


async def test_ten_failed_turns_open_circuit_without_changing_durable_history(history):
    storage, manager, key, session = history
    await populate(manager, key)
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    for _ in range(10):
        runner.clear_compaction_turn_state(key)
        await runner._maybe_preflight_compact(key, 1000)
        agent = history_agent()
        context = await runner._load_history(agent, key, trim_last_user=False)
        assert context and "Temporary history window" in context
        assert len(agent.set_history.call_args.args[0]) < 8
        assert await fingerprint(manager, key) == before
        assert await storage.get_all_summaries(session.session_id) == []
        async with storage._conn.execute(
            "SELECT count(*) FROM compacted_transcript_entries WHERE session_id = ?",
            (session.session_id,),
        ) as cursor:
            assert (await cursor.fetchone())[0] == 0
    assert manager.attempts == 3
    assert runner._compaction_failures[key].count == 3


async def test_soft_pressure_failure_keeps_full_history_without_window(history):
    _, manager, key, _ = history
    entries = await populate(manager, key, tokens=100)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    await runner._maybe_preflight_compact(key, 900)
    assert manager.attempts == 1
    assert key not in runner._emergency_compaction_overrides
    agent = history_agent()
    context = await runner._load_history(agent, key, trim_last_user=False)
    assert context is None
    assert [message.content for message in agent.set_history.call_args.args[0]] == [
        entry.content for entry in entries
    ]
    assert runner._compaction_failures[key].count == 1


async def test_load_recomputes_window_after_append_and_preserves_new_messages(history):
    _, manager, key, _ = history
    entries = await populate(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    assert await runner._record_emergency_ephemeral_compaction(
        key, entries, 1000, compaction_id="synthetic-append", phase="preflight",
        reason="summary_failed",
    )
    await manager.append_message(key, "user", "new queued request", token_count=10)
    await manager.append_message(key, "assistant", "new queued answer", token_count=10)
    before_load = await fingerprint(manager, key)
    agent = history_agent()
    context = await runner._load_history(agent, key, trim_last_user=False)
    loaded = [message.content for message in agent.set_history.call_args.args[0]]
    assert loaded[-2:] == ["new queued request", "new queued answer"]
    assert entries[-2].content in loaded
    assert entries[-1].content in loaded
    assert context and "Temporary history window" in context
    assert await fingerprint(manager, key) == before_load
    assert key not in runner._emergency_compaction_overrides


@pytest.mark.parametrize("quoted_headers", [False, True])
async def test_window_preserves_complete_previous_checkpoint_once(history, quoted_headers):
    storage, manager, key, session = history
    entries = await populate(manager, key)
    text = "SYNTHETIC_CHECKPOINT_FACT: retain violet setting"
    if quoted_headers:
        text = (
            "[Structured Compaction Summary]\n\nCurrent Status:\n"
            "[Structured Compaction Summary]\n\nCurrent Status:\n" + text
        )
    await storage.save_summary(SessionSummary(
        session_id=session.session_id, session_key=key, summary_text=text,
    ))
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    assert await runner._record_emergency_ephemeral_compaction(
        key, entries, 1000, compaction_id="synthetic-old-checkpoint", phase="preflight",
        reason="summary_failed",
    )
    agent = history_agent()
    context = await runner._load_history(agent, key, trim_last_user=False)
    assert context and "Temporary history window" in context
    # Legacy checkpoint prose can quote section markers. Preserve its complete
    # body once; only the renderer-owned wrapper determines replay integrity.
    assert text in context
    assert context.count("SYNTHETIC_CHECKPOINT_FACT") == 1
    assert context.count("[Compacted Session Summaries]") == 1
    assert await fingerprint(manager, key) == before
    summaries = await storage.get_all_summaries(session.session_id)
    assert len(summaries) == 1 and summaries[0].summary_text == text


@pytest.mark.parametrize("stale", [False, True])
async def test_exact_consumer_gate_can_reject_every_local_window(history, stale):
    _, manager, key, _ = history
    entries = await populate(manager, key)
    before = await fingerprint(manager, key)
    observed = []

    def reject(summary, kept):
        observed.append((summary, kept))
        if stale:
            raise ConsumerAdmissionStaleError("synthetic deployment changed")
        return False

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    assert not await runner._record_emergency_ephemeral_compaction(
        key, entries, 1000, compaction_id="synthetic-reject", phase="preflight",
        reason="summary_failed", consumer_admission=reject,
    )
    assert observed
    assert key not in runner._emergency_compaction_overrides
    assert await fingerprint(manager, key) == before


@pytest.mark.parametrize("failure", ["cancel", "timeout"])
async def test_cancel_and_timeout_preserve_storage_and_only_timeout_can_window(history, failure):
    _, manager, key, _ = history
    await populate(manager, key)
    manager.failure = failure
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await runner._maybe_preflight_compact(key, 1000)
        assert key not in runner._emergency_compaction_overrides
        assert key not in runner._compaction_failures
    else:
        await runner._maybe_preflight_compact(key, 1000)
        assert key in runner._emergency_compaction_overrides
        assert runner._compaction_failures[key].count == 1
    assert await fingerprint(manager, key) == before


@pytest.mark.parametrize("status", ["pending", "error"])
async def test_window_retains_complete_tool_round_with_dict_execution_status(history, status):
    _, manager, key, _ = history
    await manager.append_message(key, "user", "old background", token_count=300)
    await manager.append_message(key, "assistant", "old response", token_count=300)
    await manager.append_message(key, "user", "inspect status", token_count=300)
    tool_segments = [
        {"type": "tool_use", "tool_use_id": "synthetic-call", "name": "inspect", "input": {}},
        {
            "type": "tool_result", "tool_use_id": "synthetic-call", "result": "raw evidence " * 200,
            "is_error": status == "error", "execution_status": {
                "status": status,
                "reason": "pending" if status == "pending" else "nonzero_exit",
            },
        },
    ]
    await manager.append_message(
        key, "assistant", "inspection", tool_calls=tool_segments, token_count=300,
    )
    for index in range(4):
        await manager.append_message(
            key, "user" if index % 2 == 0 else "assistant", f"latest {index}", token_count=10,
        )
    entries = list(await manager.get_transcript(key))
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    assert await runner._record_emergency_ephemeral_compaction(
        key, entries, 1000, compaction_id="synthetic-tool-round", phase="preflight",
        reason="summary_failed", consumer_admission=lambda summary, kept: len(kept) <= 6,
    )
    override = runner._emergency_compaction_overrides[key]
    assert entries[2].message_id in [entry.message_id for entry in override.kept_entries]
    preserved = next(entry for entry in override.kept_entries if entry.tool_calls)
    assert preserved.tool_calls == tool_segments
    assert await fingerprint(manager, key) == before
