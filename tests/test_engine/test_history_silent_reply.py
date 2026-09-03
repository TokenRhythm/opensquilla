from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opensquilla.engine import runtime as runtime_module
from opensquilla.engine.history import reconstruct_messages_from_entry
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.transcript_snapshot import TurnTranscriptSnapshot
from opensquilla.provider import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    SessionContextState,
    SessionIntent,
    SessionSummary,
)
from opensquilla.session.storage import SessionStorage, StaleEpochError


def test_provider_history_cleans_goal_text_segments_without_mutating_source() -> None:
    raw_segments = [
        {"type": "text", "text": "NO_REPLY\nStill checking."},
        {
            "type": "tool_use",
            "tool_use_id": "call-1",
            "name": "read_status",
            "input": {"id": "job-1"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "result": "pending",
            "is_error": False,
        },
        {"type": "text", "text": "HEARTBEAT_OK"},
    ]

    messages = reconstruct_messages_from_entry(
        "assistant",
        "NO_REPLY\nStill checking.",
        raw_segments,
        turn_context={"intent": "goal_continuation"},
    )

    assert len(messages) == 2
    assert messages[0].role == "assistant"
    assert isinstance(messages[0].content, list)
    assert isinstance(messages[0].content[0], ContentBlockText)
    assert messages[0].content[0].text == "Still checking."
    assert isinstance(messages[0].content[1], ContentBlockToolUse)
    assert messages[1].role == "user"
    assert isinstance(messages[1].content, list)
    assert isinstance(messages[1].content[0], ContentBlockToolResult)
    assert messages[1].content[0].content == "pending"
    assert raw_segments[0]["text"] == "NO_REPLY\nStill checking."
    assert raw_segments[-1]["text"] == "HEARTBEAT_OK"


def test_provider_history_suppresses_split_goal_marker_around_tool_pair() -> None:
    raw_segments = [
        {"type": "text", "text": "NO_"},
        {
            "type": "tool_use",
            "tool_use_id": "call-split",
            "name": "read_status",
            "input": {"id": "job-split"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-split",
            "name": "read_status",
            "result": "pending",
            "is_error": False,
        },
        {"type": "text", "text": "REPLY"},
    ]

    messages = reconstruct_messages_from_entry(
        "assistant",
        "NO_REPLY",
        raw_segments,
        turn_context={"intent": "goal_continuation"},
    )

    assert [message.role for message in messages] == ["assistant", "user"]
    first_assistant = messages[0].content
    tool_results = messages[1].content
    assert isinstance(first_assistant, list)
    assert isinstance(first_assistant[0], ContentBlockToolUse)
    assert first_assistant[0].id == "call-split"
    assert isinstance(tool_results, list)
    assert isinstance(tool_results[0], ContentBlockToolResult)
    assert tool_results[0].tool_use_id == "call-split"
    assert "NO_" not in repr(messages)
    assert "REPLY" not in repr(messages)
    assert raw_segments[0]["text"] == "NO_"
    assert raw_segments[-1]["text"] == "REPLY"


def test_provider_history_keeps_unattributed_mixed_sentinel_text() -> None:
    messages = reconstruct_messages_from_entry(
        "assistant",
        "NO_REPLY\nThis is ordinary historical prose.",
        None,
    )

    assert len(messages) == 1
    assert messages[0].content == "NO_REPLY\nThis is ordinary historical prose."


def test_provider_history_drops_exact_sentinel_without_provenance() -> None:
    assert reconstruct_messages_from_entry("assistant", "HEARTBEAT_OK", None) == []


class _HistoryManager:
    def __init__(self, entries: list[object]) -> None:
        self.entries = entries

    async def get_transcript(self, _session_key: str) -> list[object]:
        return list(self.entries)

    async def get_context_states(self, _session_key: str) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_turn_runner_load_history_passes_goal_provenance_to_sanitizer() -> None:
    raw = "NO_REPLY\nStill checking."
    entry = SimpleNamespace(
        role="assistant",
        content=raw,
        tool_calls=None,
        reasoning_content=None,
        message_id="message-1",
        turn_context={"intent": "goal_continuation"},
    )
    manager = _HistoryManager([entry])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    agent = SimpleNamespace(
        provider=SimpleNamespace(provider_name="test"),
        config=SimpleNamespace(
            materialize_historical_attachments=False,
            preserve_historical_images=False,
        ),
        set_history=MagicMock(),
    )

    await runner._load_history(agent, "agent:main:test", trim_last_user=False)

    history = agent.set_history.call_args.args[0]
    assert [message.content for message in history] == ["Still checking."]
    assert entry.content == raw


@pytest.mark.asyncio
async def test_exact_history_fails_closed_after_reset_without_mixing_replacement_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-owner.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    key = "agent:main:history-owner"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    admitted = await manager.create(key)
    await manager.append_message(
        key,
        "user",
        json.dumps(
            {
                "text": "retired owner attachment",
                "attachments": [
                    {
                        "mime": "image/png",
                        "name": "retired.png",
                        "data": base64.b64encode(b"retired owner private bytes").decode(),
                    }
                ],
            }
        ),
    )
    admitted_entries = tuple(await manager.get_transcript(key))
    replacement, rotated = await manager.apply_intent(
        key,
        SessionIntent.RESET_SAME_KEY,
    )
    assert rotated is True
    await storage.save_summary(
        SessionSummary(
            session_id=replacement.session_id,
            session_key=key,
            summary_text="replacement private summary",
        )
    )
    await storage.save_context_state(
        SessionContextState(
            session_id=replacement.session_id,
            session_key=key,
            provider="anthropic",
            state_kind="anthropic_compaction_block",
            payload={"content": "replacement private context"},
        )
    )
    snapshot = TurnTranscriptSnapshot(
        lambda: asyncio.sleep(0, result=admitted_entries)
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    replay_session_ids: list[str | None] = []
    original_project_history_replay = runner._project_history_replay

    def record_project_history_replay(*args: object, **kwargs: object):
        replay_session_ids.append(kwargs.get("session_id"))
        return original_project_history_replay(*args, **kwargs)

    monkeypatch.setattr(
        runner,
        "_project_history_replay",
        record_project_history_replay,
    )
    agent = SimpleNamespace(
        provider=SimpleNamespace(provider_name="anthropic"),
        config=SimpleNamespace(
            materialize_historical_attachments=True,
            preserve_historical_images=False,
            workspace_dir=str(tmp_path / "workspace"),
            model_capabilities=None,
        ),
        set_history=MagicMock(),
    )
    try:
        with pytest.raises(StaleEpochError, match="context-state read"):
            await runner._load_history(
                agent,
                key,
                trim_last_user=False,
                transcript_snapshot=snapshot,
                expected_session_id=admitted.session_id,
                expected_session_epoch=int(admitted.epoch or 0),
            )
    finally:
        await storage.close()

    assert replay_session_ids == []
    assert not (tmp_path / "workspace" / ".opensquilla" / "attachments").exists()
    agent.set_history.assert_not_called()


@pytest.mark.asyncio
async def test_exact_history_discards_retired_owner_emergency_override_after_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = SessionStorage(str(tmp_path / "history-emergency-owner.db"))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    key = "agent:main:history-emergency-owner"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    admitted = await manager.create(key)
    for index in range(8):
        await manager.append_message(
            key,
            "user" if index % 2 == 0 else "assistant",
            f"retired owner secret {index} " + ("x" * 500),
        )
    admitted_entries = list(await manager.get_transcript(key))
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    recorded = await runner._record_emergency_ephemeral_compaction(
        key,
        admitted_entries,
        200,
        compaction_id="cmp-retired-owner",
        phase="preflight",
        reason="compact_failed",
        expected_session_id=admitted.session_id,
        expected_session_epoch=int(admitted.epoch or 0),
    )
    assert recorded is True
    retired_override = runner._emergency_compaction_overrides[key]
    assert retired_override.expected_session_id == admitted.session_id
    assert retired_override.expected_session_epoch == int(admitted.epoch or 0)
    replacement, rotated = await manager.apply_intent(
        key,
        SessionIntent.RESET_SAME_KEY,
    )
    assert rotated is True
    await manager.append_message(key, "user", "replacement owner content")
    agent = SimpleNamespace(
        provider=SimpleNamespace(provider_name="test"),
        config=SimpleNamespace(
            materialize_historical_attachments=False,
            preserve_historical_images=False,
            workspace_dir=None,
            model_capabilities=None,
        ),
        set_history=MagicMock(),
    )

    try:
        summary_context = await runner._load_history(
            agent,
            key,
            trim_last_user=False,
            expected_session_id=replacement.session_id,
            expected_session_epoch=int(replacement.epoch or 0),
        )
    finally:
        await storage.close()

    history = agent.set_history.call_args.args[0]
    assert [message.content for message in history] == ["replacement owner content"]
    assert summary_context is None
    assert "retired owner" not in repr(history)
    assert key not in runner._emergency_compaction_overrides


def test_clear_compaction_turn_state_discards_emergency_override() -> None:
    key = "agent:main:history-emergency-cleanup"
    runner = TurnRunner(provider_selector=MagicMock())
    runner.mark_compaction_attempted_this_turn(key)
    runner.mark_compacted_this_turn(key)
    runner._emergency_compaction_overrides[key] = (
        runtime_module._EmergencyCompactionOverride(
            summary="request-scoped summary",
            kept_entries=[],
            reason="compact_failed",
            compaction_id="cmp-cleanup",
            expected_session_id="session-owner",
            expected_session_epoch=3,
        )
    )

    runner.clear_compaction_turn_state(key)

    assert not runner.has_attempted_compaction_this_turn(key)
    assert not runner.has_compacted_this_turn(key)
    assert key not in runner._emergency_compaction_overrides


@pytest.mark.parametrize("reader", ["context", "summary"])
@pytest.mark.asyncio
async def test_exact_history_reader_does_not_trust_storage_backed_kwargs_capability(
    reader: str,
) -> None:
    calls: list[dict[str, object]] = []

    async def kwargs_reader(_session_key: str, **kwargs: object) -> list[object]:
        calls.append(kwargs)
        return []

    manager = SimpleNamespace(
        _storage=object(),
        get_context_states=kwargs_reader,
        get_summaries=kwargs_reader,
    )
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)

    with pytest.raises(RuntimeError, match="does not support exact ownership"):
        if reader == "context":
            await runner._load_context_states(
                "agent:main:owner-capability",
                expected_session_id="session-owner",
                expected_session_epoch=4,
            )
        else:
            await runner._compaction_summary_context(
                "agent:main:owner-capability",
                [],
                context_states=[],
                expected_session_id="session-owner",
                expected_session_epoch=4,
            )

    assert calls == []


def test_emergency_compaction_projection_sanitizes_without_mutating_source() -> None:
    raw_segments = [{"type": "text", "text": "HEARTBEAT_OK\nVisible warning."}]
    entry = SimpleNamespace(
        role="assistant",
        content="HEARTBEAT_OK\nVisible warning.",
        tool_calls=raw_segments,
        message_id="message-2",
        token_count=10,
        tool_call_id=None,
        reasoning_content=None,
        turn_usage=None,
        turn_context={"intent": "goal_continuation"},
    )

    projected = TurnRunner._entry_for_emergency_compaction(entry)

    assert projected["content"] == "Visible warning."
    assert projected["tool_calls"] == [
        {"type": "text", "text": "Visible warning."}
    ]
    assert projected["turn_context"] == {"intent": "goal_continuation"}
    assert entry.content == "HEARTBEAT_OK\nVisible warning."
    assert raw_segments[0]["text"].startswith("HEARTBEAT_OK")
