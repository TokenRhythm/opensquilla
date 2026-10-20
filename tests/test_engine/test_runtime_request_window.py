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
        if config.on_summary_call_started is not None:
            config.on_summary_call_started()
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


async def populate(manager, key, *, tokens=300, content_chars=500):
    for index in range(8):
        await manager.append_message(
            key, "user" if index % 2 == 0 else "assistant",
            f"synthetic item {index} " + "x" * content_chars, token_count=tokens,
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


async def test_twenty_failed_turns_open_circuit_without_changing_durable_history(history):
    storage, manager, key, session = history
    await populate(manager, key)
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    for _ in range(20):
        runner.clear_compaction_turn_state(key)
        prepared_window = await runner._maybe_preflight_compact(key, 1000)
        agent = history_agent()
        context = await runner._load_history(
            agent, key, trim_last_user=False, prepared_window=prepared_window,
        )
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
    # Keep both tokenizer and fallback estimates below the persisted floor:
    # soft pressure is 0.85 * 900 < 8 * 100 <= 900, even without tiktoken.
    entries = await populate(manager, key, tokens=100, content_chars=100)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    prepared_window = await runner._maybe_preflight_compact(key, 900)
    assert manager.attempts == 1
    assert not hasattr(runner, "_emergency_compaction_overrides")
    agent = history_agent()
    context = await runner._load_history(
            agent, key, trim_last_user=False, prepared_window=prepared_window,
        )
    assert context is None
    assert [message.content for message in agent.set_history.call_args.args[0]] == [
        entry.content for entry in entries
    ]
    assert runner._compaction_failures[key].count == 1


@pytest.mark.parametrize("backup_timeout", [False, True])
async def test_failed_backup_with_fitting_raw_history_settles_without_durable_write(
    history, monkeypatch, backup_timeout,
):
    _, manager, key, _ = history
    entries = await populate(manager, key, tokens=3600, content_chars=100)
    before = await fingerprint(manager, key)
    events = []

    async def refused_backup(*args, **kwargs):
        if backup_timeout:
            raise CompactionTimeoutError("checkpointing", 0.01)
        return SimpleNamespace(scope="checkpoint", status="checkpoint_failed")

    monkeypatch.setattr(manager, "record_memory_checkpoint", refused_backup)
    monkeypatch.setattr("opensquilla.engine.runtime.notify_compaction",
                        lambda _key, **payload: events.append(payload))
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    window = await runner._maybe_preflight_compact(key, 32_768)
    assert window is None
    assert manager.attempts == 0
    assert events[0]["status"] == "started"
    assert events[-1]["status"] == ("timed_out" if backup_timeout else "failed")
    assert events[-1]["reason"] == (
        "compaction_deadline_exceeded" if backup_timeout else "checkpoint_failed"
    )
    agent = history_agent()
    await runner._load_history(agent, key, trim_last_user=False, prepared_window=window)
    assert [message.content for message in agent.set_history.call_args.args[0]] == [
        entry.content for entry in entries
    ]
    assert await fingerprint(manager, key) == before


async def test_parent_deadline_after_backup_failure_has_only_timeout_terminal(history, monkeypatch):
    import time
    from unittest.mock import AsyncMock

    import opensquilla.engine.runtime as runtime

    _, manager, key, _ = history
    await populate(manager, key, tokens=3600, content_chars=100)
    clock = time.monotonic()
    events = []

    async def refused_backup(*args, **kwargs):
        nonlocal clock
        clock += 120
        return SimpleNamespace(scope="checkpoint", status="checkpoint_failed")

    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr(manager, "record_memory_checkpoint", refused_backup)
    monkeypatch.setattr(
        runtime, "notify_compaction", lambda _key, **payload: events.append(payload),
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    prepare = AsyncMock()
    monkeypatch.setattr(runner, "_prepare_request_window", prepare)
    context = SimpleNamespace(chat_config=SimpleNamespace(turn_deadline_at_monotonic=clock + 60))
    with pytest.raises(TimeoutError, match="Turn deadline expired"):
        await runner._maybe_preflight_compact(key, 32_768, compaction_request_context=context)
    assert manager.attempts == 0
    prepare.assert_not_awaited()
    assert [(event["status"], event.get("reason")) for event in events] == [
        ("started", None), ("timed_out", "turn_deadline_exceeded"),
    ]


async def test_window_stops_planning_when_first_candidate_is_admitted(history, monkeypatch):
    _, manager, key, _ = history
    entries = await populate(manager, key)
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)

    def candidates(size, **kwargs):
        yield tuple(range(2, size))
        raise AssertionError("The admitted request does not need further history windows")

    monkeypatch.setattr(
        "opensquilla.engine.request_window.iter_window_index_candidates", candidates,
    )
    window = await runner._prepare_request_window(
        key, entries, 1000, compaction_id="lazy-window", phase="preflight",
        reason="summary_failed", consumer_admission=lambda _summary, kept: len(kept) <= 6,
    )
    assert window is not None and len(window.kept_entries) == 6
    assert await fingerprint(manager, key) == before


@pytest.mark.parametrize("window", [32_768, 131_072])
@pytest.mark.parametrize("with_tool", [False, True])
async def test_window_restores_small_omitted_round_with_same_final_budget(
    history, window, with_tool,
):
    from opensquilla.engine import Agent, AgentConfig
    from opensquilla.provider import OpenAIProvider

    storage, manager, key, session = history
    await manager.append_message(key, "user", "Keep the deployment task and its current status.")
    await manager.append_message(key, "assistant", "Understood.")
    await manager.append_message(key, "user", "Read the release identifier.")
    if with_tool:
        await manager.append_message(key, "assistant", "", tool_calls=[{
            "type": "tool_use", "tool_use_id": "release-read", "name": "read", "input": {},
        }])
        await manager.append_message(key, "user", "", tool_calls=[{
            "type": "tool_result", "tool_use_id": "release-read", "result": "release-violet-42",
        }])
    else:
        await manager.append_message(key, "assistant", "release-violet-42")
    small_end = len(await manager.get_transcript(key))
    await manager.append_message(key, "user", "Disposable deployment log. " * window)
    await manager.append_message(key, "assistant", "Logs received.")
    await manager.append_message(key, "user", "Do not repeat the release read.")
    await manager.append_message(key, "assistant", "The read is complete; deployment is pending.")
    active = await manager.append_message(key, "user", "Report the identifier and current status.")
    entries = list(await manager.get_transcript(key))
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    provider = OpenAIProvider(api_key="synthetic-unused", model="synthetic-qwen")
    agent = Agent(provider=provider, config=AgentConfig(
        context_window_tokens=window, context_window_tokens_global_override=window,
        context_window_known=True, max_tokens=4096,
    ))
    budget = agent.resolve_compaction_budget(
        consumer_provider=provider, active_user_message=active.content,
        active_user_in_history=True, bound_user_message_id=active.message_id,
        attachment_messages=None, context_window_tokens=window, max_output_tokens=4096,
    )
    attempts = []

    def admitted(summary, kept):
        accepted = budget.consumer_admission(summary, kept)
        attempts.append((tuple(row["message_id"] for row in kept), accepted))
        return accepted

    prepared = await runner._prepare_request_window(
        key, entries, budget.history_capacity_tokens, compaction_id="restore-small-round",
        phase="preflight", reason="summary_failed", protected_recent_messages=1,
        consumer_admission=admitted,
    )
    assert prepared is not None
    ids = [row.message_id for row in prepared.kept_entries]
    small_ids = [row.message_id for row in entries[2:small_end]]
    assert all(message_id in ids for message_id in small_ids)
    assert ids == [row.message_id for row in entries if row.message_id in ids]
    assert ids[-1] == active.message_id
    assert entries[small_end].message_id not in ids  # the large intervening request
    first_fit, _ = next(attempt for attempt in attempts if attempt[1])
    assert not set(small_ids).intersection(first_fit)
    assert sum(set(small_ids).issubset(trial) for trial, _ in attempts[attempts.index(
        (first_fit, True),
    ) + 1:]) == 1
    assert admitted(prepared.summary, [runner._entry_for_emergency_compaction(row)
                                      for row in prepared.kept_entries])
    assert f"{len(entries) - len(ids)} earlier messages" in prepared.summary
    assert await fingerprint(manager, key) == before
    assert await storage.get_all_summaries(session.session_id) == []


@pytest.mark.parametrize("failure", ["no_room", "stale", "cancel"])
async def test_window_backfill_respects_admission_and_cancellation(history, failure):
    _, manager, key, _ = history
    for text in ("original", "old-small", "old-large", "latest"):
        await manager.append_message(key, "user", text)
        await manager.append_message(key, "assistant", text + " response")
    active = await manager.append_message(key, "user", "continue")
    entries = list(await manager.get_transcript(key))
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    first_fit = None
    backfill_attempts = []

    def admitted(summary, kept):
        nonlocal first_fit
        ids = tuple(row["message_id"] for row in kept)
        if first_fit is not None:
            backfill_attempts.append(ids)
            if failure == "stale":
                raise ConsumerAdmissionStaleError("source changed during local selection")
            if failure == "cancel":
                raise asyncio.CancelledError
            return False
        if entries[2].message_id in ids or entries[4].message_id in ids:
            return False
        first_fit = ids
        return True

    async def prepare():
        return await runner._prepare_request_window(
            key, entries, 32_768 - 4096, compaction_id="restore-budget-boundary",
            phase="preflight", reason="summary_failed", protected_recent_messages=1,
            consumer_admission=admitted,
        )

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await prepare()
    else:
        prepared = await prepare()
        if failure == "stale":
            assert prepared is None
        else:
            assert prepared is not None
            assert tuple(row.message_id for row in prepared.kept_entries) == first_fit
            assert prepared.kept_entries[-1].message_id == active.message_id
    assert backfill_attempts
    assert len(backfill_attempts) <= 2
    assert await fingerprint(manager, key) == before


async def test_load_recomputes_window_after_append_and_preserves_new_messages(history):
    _, manager, key, _ = history
    entries = await populate(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    prepared_window = await runner._prepare_request_window(
        key, entries, 1000, compaction_id="synthetic-append", phase="preflight",
        reason="summary_failed",
    )
    await manager.append_message(key, "user", "new queued request", token_count=10)
    await manager.append_message(key, "assistant", "new queued answer", token_count=10)
    before_load = await fingerprint(manager, key)
    agent = history_agent()
    context = await runner._load_history(
            agent, key, trim_last_user=False, prepared_window=prepared_window,
        )
    loaded = [message.content for message in agent.set_history.call_args.args[0]]
    assert loaded[-2:] == ["new queued request", "new queued answer"]
    assert entries[-2].content in loaded
    assert entries[-1].content in loaded
    assert context and "Temporary history window" in context
    assert await fingerprint(manager, key) == before_load
    assert not hasattr(runner, "_emergency_compaction_overrides")


async def test_append_can_release_optional_old_round_to_fit_32k(history):
    """A retained historical answer must not become a new hard obligation."""
    _, manager, key, _ = history
    from opensquilla.token_estimation import estimate_tokens

    unit = " log 17;"
    for index in range(8):
        await manager.append_message(
            key, "user" if index % 2 == 0 else "assistant",
            f"old-{index}" + unit * (5000 // estimate_tokens(unit)),
        )
    entries = list(await manager.get_transcript(key))
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    prepared = await runner._prepare_request_window(
        key, entries, 32768, compaction_id="append-32k", phase="preflight",
        reason="summary_failed",
    )
    assert prepared is not None
    assert prepared.kept_entries[-1].message_id == entries[-1].message_id
    assert prepared.protected_recent_messages == 0
    new_messages = []
    for index in range(2):
        content = f"new-{index}" + unit * (12000 // estimate_tokens(unit))
        await manager.append_message(key, "user" if index == 0 else "assistant", content)
        new_messages.append(content)
    before = await fingerprint(manager, key)
    agent = history_agent()
    context = await runner._load_history(
        agent, key, trim_last_user=False, prepared_window=prepared,
    )
    loaded = [message.content for message in agent.set_history.call_args.args[0]]
    assert context and "Temporary history window" in context
    assert loaded[-2:] == new_messages
    assert entries[-1].content not in loaded
    assert sum(estimate_tokens(content) for content in loaded) + estimate_tokens(context) < 32768
    assert await fingerprint(manager, key) == before


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
    prepared_window = await runner._prepare_request_window(
        key, entries, 1000, compaction_id="synthetic-old-checkpoint", phase="preflight",
        reason="summary_failed",
    )
    agent = history_agent()
    context = await runner._load_history(
            agent, key, trim_last_user=False, prepared_window=prepared_window,
        )
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
    assert not await runner._prepare_request_window(
        key, entries, 1000, compaction_id="synthetic-reject", phase="preflight",
        reason="summary_failed", consumer_admission=reject,
    )
    assert observed
    assert not hasattr(runner, "_emergency_compaction_overrides")
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
            prepared_window = await runner._maybe_preflight_compact(key, 1000)
        assert not hasattr(runner, "_emergency_compaction_overrides")
        assert key not in runner._compaction_failures
    else:
        prepared_window = await runner._maybe_preflight_compact(key, 1000)
        assert prepared_window is not None
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
    prepared_window = await runner._prepare_request_window(
        key, entries, 1000, compaction_id="synthetic-tool-round", phase="preflight",
        reason="summary_failed", consumer_admission=lambda summary, kept: len(kept) <= 6,
    )
    override = prepared_window
    assert override is not None
    assert entries[2].message_id in [entry.message_id for entry in override.kept_entries]
    preserved = next(entry for entry in override.kept_entries if entry.tool_calls)
    assert preserved.tool_calls == tool_segments
    assert await fingerprint(manager, key) == before


@pytest.mark.parametrize("stale", [False, True])
async def test_prepared_window_rechecks_consumer_before_use(history, stale):
    _, manager, key, _ = history
    entries = await populate(manager, key)
    gate_changed = False
    checks = 0

    def admit(summary, kept):
        nonlocal checks
        checks += 1
        if gate_changed:
            if stale:
                raise ConsumerAdmissionStaleError("deployment changed after preparation")
            return False
        return len(kept) <= 4

    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    prepared = await runner._prepare_request_window(
        key, entries, 1000, compaction_id="synthetic-stale-gate", phase="preflight",
        reason="summary_failed", consumer_admission=admit,
    )
    assert prepared is not None
    prior_checks = checks
    gate_changed = True
    agent = history_agent()
    context = await runner._load_history(
        agent, key, trim_last_user=False, prepared_window=prepared,
    )
    assert checks == prior_checks + 1
    assert context is None
    assert len(agent.set_history.call_args.args[0]) == len(entries)
    assert not hasattr(runner, "_emergency_compaction_overrides")


@pytest.mark.parametrize("window", [32_768, 131_072])
async def test_failed_summary_window_retains_request_and_middle_correction(history, window):
    from opensquilla.token_estimation import estimate_tokens

    storage, manager, key, session = history
    first = "Original task: deploy violet to east. " + "context " * 30 + "Owner: Mira."
    correction = "Cancel deployment to east. Use west; never repeat the completed verification."
    for request in (first, correction, "Continue the corrected task."):
        await manager.append_message(key, "user", request)
        await manager.append_message(key, "assistant", "historical output " * (window // 4))
    await manager.append_message(key, "user", "Report the retained task state.")
    entries = list(await manager.get_transcript(key))
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)

    def admitted(summary, kept):
        tokens = estimate_tokens(summary) + sum(estimate_tokens(row["content"]) for row in kept)
        return tokens <= window - 2048

    prepared = await runner._prepare_request_window(
        key, entries, window - 2048, compaction_id="request-continuity", phase="preflight",
        reason="summary_failed", protected_recent_messages=1, consumer_admission=admitted,
    )
    assert prepared is not None
    kept = [entry.content for entry in prepared.kept_entries]
    assert first in kept and correction in kept
    assert kept.index(first) < kept.index(correction) < len(kept) - 1
    assert kept[-1] == entries[-1].content
    assert admitted(
        prepared.summary,
        [runner._entry_for_emergency_compaction(e) for e in prepared.kept_entries],
    )
    assert len(kept) < len(entries)
    assert await fingerprint(manager, key) == before
    assert await storage.get_all_summaries(session.session_id) == []


@pytest.mark.parametrize("window", [32_768, 131_072])
async def test_oversized_initial_request_does_not_block_safe_window(history, window):
    from opensquilla.token_estimation import estimate_tokens

    _, manager, key, _ = history
    await manager.append_message(key, "user", "oversized original request " * window)
    await manager.append_message(key, "assistant", "old response")
    await manager.append_message(
        key, "user", "Cancel the original task; only report current status.",
    )
    await manager.append_message(key, "assistant", "No previous action was executed.")
    await manager.append_message(key, "user", "Continue with the status report.")
    entries = list(await manager.get_transcript(key))
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    calls = []

    def admitted(summary, kept):
        calls.append(len(kept))
        tokens = estimate_tokens(summary) + sum(estimate_tokens(row["content"]) for row in kept)
        return tokens <= window - 2048

    prepared = await runner._prepare_request_window(
        key, entries, window - 2048, compaction_id="oversized-request", phase="preflight",
        reason="compaction_deadline_exceeded", protected_recent_messages=1,
        consumer_admission=admitted,
    )
    assert prepared is not None
    kept = [entry.content for entry in prepared.kept_entries]
    assert entries[0].content not in kept
    assert entries[2].content in kept and entries[-1].content in kept
    assert "omitted" in prepared.summary
    assert len(calls) <= 2 * len(entries) + 2
    assert await fingerprint(manager, key) == before


async def test_user_anchor_does_not_retain_a_legacy_tool_result_without_its_call(history):
    _, manager, key, _ = history
    await manager.append_message(key, "user", "Original task: inspect the archive.")
    await manager.append_message(key, "assistant", "", tool_calls=[{
        "type": "tool_use", "tool_use_id": "completed-read", "name": "read", "input": {},
    }])
    await manager.append_message(key, "user", "Legacy result", tool_calls=[{
        "type": "tool_result", "tool_use_id": "completed-read", "result": "archive content",
    }])
    await manager.append_message(key, "assistant", "Inspection completed.")
    await manager.append_message(key, "user", "Only report status now.")
    await manager.append_message(key, "assistant", "The current status is known.")
    await manager.append_message(key, "user", "Continue the status report.")
    entries = list(await manager.get_transcript(key))
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    prepared = await runner._prepare_request_window(
        key, entries, 32_768 - 2048, compaction_id="legacy-tool-row", phase="preflight",
        reason="summary_failed", protected_recent_messages=1,
        consumer_admission=lambda _summary, kept: len(kept) <= 6,
    )
    assert prepared is not None
    kept_ids = {row.message_id for row in prepared.kept_entries}
    assert entries[0].message_id in kept_ids
    assert (entries[1].message_id in kept_ids) == (entries[2].message_id in kept_ids)
    assert entries[2].message_id in kept_ids
    assert await fingerprint(manager, key) == before


@pytest.mark.parametrize("window", [32_768, 131_072])
async def test_three_message_projection_recounts_tokens_without_changing_source(history, window):
    from opensquilla.token_estimation import estimate_tokens

    storage, manager, key, session = history
    text = "Original task. " + "historical notes " * window + " Owner is Mira."
    await manager.append_message(key, "user", text, token_count=window * 4)
    await manager.append_message(key, "assistant", "Recorded.")
    await manager.append_message(key, "user", "Continue the same task.")
    entries = list(await manager.get_transcript(key))
    before = await fingerprint(manager, key)
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    prepared = await runner._prepare_request_window(
        key, entries, window - 4096, compaction_id="user-text-view", phase="preflight",
        reason="compaction_deadline_exceeded", protected_recent_messages=1,
    )
    assert prepared is not None and len(prepared.kept_entries) == 3
    assert [entry.message_id for entry in prepared.kept_entries] == [e.message_id for e in entries]
    first = prepared.kept_entries[0]
    assert first.content.startswith("Original task.") and first.content.endswith(" Owner is Mira.")
    assert "sha256=" in first.content and "not a summary" in first.content
    assert first.token_count == estimate_tokens(first.content) < entries[0].token_count
    assert "historical text" in prepared.summary
    assert prepared.kept_entries[-1].content == entries[-1].content
    assert await fingerprint(manager, key) == before
    assert await storage.get_all_summaries(session.session_id) == []
