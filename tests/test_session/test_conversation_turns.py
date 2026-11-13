from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opensquilla.engine.runtime import TurnRunner
from opensquilla.gateway.config import GatewayConfig
from opensquilla.session.conversation_turns import count_completed_conversation_turns
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionIntent
from opensquilla.session.storage import SessionStorage, StaleEpochError
from opensquilla.session.turn_context import turn_context_scope


async def append(manager, key, role, text, *, turn_context):
    with turn_context_scope(turn_context):
        return await manager.append_message(key, role, text)


def row(role, mid, turn=None, tools=None):
    return SimpleNamespace(
        role=role, message_id=mid, turn_context={"turn_id": turn}, tool_calls=tools
    )


def test_turn_identity_counts_one_conversation_after_many_tool_iterations():
    rows = [row("user", "first", "t1")]
    for index in range(12):
        rows += [
            row(
                "assistant",
                f"call-{index}",
                "t1",
                [{"type": "tool_use", "id": str(index), "name": "example", "input": {}}],
            ),
            row("tool", f"result-{index}", "t1"),
        ]
    rows += [
        row("user", "current", "t2"),
        row("user", "queued", "t3"),
        row("assistant", "final", "t1"),
        row("assistant", "duplicate-final", "t1"),
    ]
    assert count_completed_conversation_turns(rows, bound_user_message_id="current") == 1
    assert count_completed_conversation_turns(rows, bound_user_message_id="missing") == 0


def test_legacy_pairs_ignore_tools_queued_inputs_and_incomplete_conversations():
    rows = [
        row("assistant", "unanchored"),
        row("user", "first"),
        row(
            "assistant",
            "call",
            tools=[{"type": "tool_use", "id": "one", "name": "example", "input": {}}],
        ),
        row("tool", "result"),
        row("user", "current"),
        row("user", "queued"),
        row("assistant", "final"),
    ]
    assert count_completed_conversation_turns(rows, bound_user_message_id="current") == 1
    assert count_completed_conversation_turns([row("user", "unanswered")]) == 0
    assert count_completed_conversation_turns(rows[:4], exclude_last_user=False) == 0


@pytest.mark.asyncio
async def test_canonical_count_survives_compaction_and_does_not_duplicate_archive_rows(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("opensquilla.session.manager._archive_dir", lambda: tmp_path / "archive")
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:conversation-count")
        await append(
            manager,
            node.session_key,
            "user",
            "synthetic question",
            turn_context={"turn_id": "completed"},
        )
        await append(
            manager,
            node.session_key,
            "assistant",
            "synthetic answer",
            turn_context={"turn_id": "completed"},
        )
        before = await storage.get_transcript(node.session_id)
        async with storage._write_transaction("count_test_archive") as conn:
            await conn.execute(
                """INSERT INTO compacted_transcript_entries
                (session_id, session_key, original_entry_id, message_id, role, content,
                 turn_context, created_at, archived_at)
                SELECT session_id, session_key, id, message_id, role, content,
                  turn_context, created_at, created_at FROM transcript_entries WHERE session_id = ?
            """,
                (node.session_id,),
            )
        assert await storage.count_completed_conversation_turns(node.session_id) == 1
        async with storage._write_transaction("count_test_remove_active") as conn:
            await conn.execute(
                "DELETE FROM transcript_entries WHERE session_id = ?", (node.session_id,)
            )
        assert await storage.count_completed_conversation_turns(node.session_id) == 1
        canonical = await storage.get_canonical_transcript(node.session_id)
        assert [entry.message_id for entry in canonical] == [entry.message_id for entry in before]
        fresh = await manager.create("agent:main:conversation-count-fresh")
        assert await storage.count_completed_conversation_turns(fresh.session_id) == 0
        reset, _ = await manager.apply_intent(node.session_key, SessionIntent.RESET_SAME_KEY)
        assert reset.session_id != node.session_id
        assert await storage.count_completed_conversation_turns(reset.session_id) == 0


@pytest.mark.asyncio
async def test_runtime_uses_canonical_count_and_checks_owner_after_read(monkeypatch, tmp_path):
    monkeypatch.setattr("opensquilla.session.manager._archive_dir", lambda: tmp_path / "archive")
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:conversation-runtime")
        await append(manager, node.session_key, "user", "first", turn_context={"turn_id": "first"})
        current = await append(
            manager, node.session_key, "user", "current", turn_context={"turn_id": "current"}
        )
        await append(
            manager, node.session_key, "user", "queued", turn_context={"turn_id": "queued"}
        )
        await append(
            manager,
            node.session_key,
            "assistant",
            "previous completed answer",
            turn_context={"turn_id": "first"},
        )
        runner = TurnRunner(
            provider_selector=MagicMock(), session_manager=manager, config=GatewayConfig()
        )
        monkeypatch.setattr(runner, "_uses_jev_router", lambda _: True)
        result = await runner._router_previous_assistant_context(
            node.session_key,
            bound_user_message_id=current.message_id,
            exclude_last_user=True,
            expected_session_id=node.session_id,
            expected_session_epoch=node.epoch,
        )
        assert result["jev_completed_conversation_turns"] == 1
        original = storage.count_completed_conversation_turns

        async def racing_count(*args, **kwargs):
            count = await original(*args, **kwargs)
            await manager.apply_intent(node.session_key, SessionIntent.RESET_SAME_KEY)
            return count

        monkeypatch.setattr(storage, "count_completed_conversation_turns", racing_count)
        with pytest.raises(StaleEpochError):
            await runner._router_previous_assistant_context(
                node.session_key,
                bound_user_message_id=current.message_id,
                exclude_last_user=True,
                expected_session_id=node.session_id,
                expected_session_epoch=node.epoch,
            )


@pytest.mark.asyncio
async def test_legacy_session_port_uses_loaded_pairs_without_storage(monkeypatch):
    entries = [row("user", "past"), row("assistant", "reply"), row("user", "current")]
    manager = SimpleNamespace(get_transcript=lambda _: entries)
    runner = TurnRunner(
        provider_selector=MagicMock(), session_manager=manager, config=GatewayConfig()
    )
    monkeypatch.setattr(runner, "_uses_jev_router", lambda _: True)
    result = await runner._router_previous_assistant_context(
        "agent:main:legacy-count",
        bound_user_message_id="current",
        exclude_last_user=True,
    )
    assert result["jev_completed_conversation_turns"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_count_read_failure_keeps_model_history(monkeypatch, cancelled):
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:unavailable-count")
        await append(
            manager, node.session_key, "user", "prior question", turn_context={"turn_id": "prior"}
        )
        await append(
            manager,
            node.session_key,
            "assistant",
            "prior answer",
            turn_context={"turn_id": "prior"},
        )
        current = await append(
            manager,
            node.session_key,
            "user",
            "current question",
            turn_context={"turn_id": "current"},
        )
        runner = TurnRunner(
            provider_selector=MagicMock(), session_manager=manager, config=GatewayConfig()
        )
        monkeypatch.setattr(runner, "_uses_jev_router", lambda _: True)

        async def unavailable(*args, **kwargs):
            if cancelled:
                raise asyncio.CancelledError
            raise RuntimeError("Synthetic metadata read unavailable")

        monkeypatch.setattr(storage, "count_completed_conversation_turns", unavailable)
        operation = runner._router_previous_assistant_context(
            node.session_key,
            bound_user_message_id=current.message_id,
            exclude_last_user=True,
        )
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await operation
            return
        result = await operation
        assert result["jev_completed_conversation_turns"] == 0
        assert result["history_user_texts"] == ["prior question"]
        assert result["prev_assistant_text"] == "prior answer"


@pytest.mark.parametrize(
    "blocks, completed",
    [
        ([{"type": "text", "text": "Synthetic answer", "presentation": "answer"}], 1),
        (
            [
                {"type": "thinking", "thinking": "Synthetic reasoning"},
                {"type": "text", "text": "Synthetic answer"},
            ],
            1,
        ),
        ([{"type": "tool_use", "id": "call", "name": "example", "input": {}}], 0),
        ([{"type": "tool_call", "id": "call", "name": "example"}], 0),
        ([{"type": "function_call", "call_id": "call", "name": "example", "arguments": "{}"}], 0),
        (
            [
                {
                    "type": "function",
                    "id": "call",
                    "function": {"name": "example", "arguments": "{}"},
                }
            ],
            0,
        ),
        ([{"id": "call", "function": {"name": "example", "arguments": "{}"}}], 0),
        (
            [
                {"type": "text", "text": "Synthetic preamble"},
                {"type": "tool_use", "id": "call", "name": "example", "input": {}},
            ],
            0,
        ),
    ],
)
@pytest.mark.asyncio
async def test_content_block_answers_and_tool_calls_match_memory_and_sql(blocks, completed):
    entries = [row("user", "input", "turn"), row("assistant", "reply", "turn", blocks)]
    assert count_completed_conversation_turns(entries) == completed
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:answer-block-count")
        with turn_context_scope({"turn_id": "turn"}):
            await manager.append_message(node.session_key, "user", "Synthetic question")
            await manager.append_message(
                node.session_key, "assistant", "Synthetic answer", tool_calls=blocks
            )
        assert await storage.count_completed_conversation_turns(node.session_id) == completed


@pytest.mark.asyncio
async def test_twenty_text_block_answers_are_twenty_completed_conversations():
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:twenty-text-answers")
        for index in range(20):
            with turn_context_scope({"turn_id": f"turn-{index}"}):
                await manager.append_message(
                    node.session_key, "user", f"Synthetic question {index}"
                )
                await manager.append_message(
                    node.session_key,
                    "assistant",
                    "Synthetic answer",
                    tool_calls=[
                        {"type": "text", "text": "Synthetic answer", "presentation": "answer"}
                    ],
                )
        assert await storage.count_completed_conversation_turns(node.session_id) == 20


@pytest.mark.parametrize(
    "ending, completed",
    [
        ([{"type": "text", "text": "Synthetic final answer", "presentation": "answer"}], 1),
        ([{"type": "text", "text": "Synthetic next step", "presentation": "intermediate"}], 0),
        ([], 0),
    ],
)
@pytest.mark.asyncio
async def test_aggregated_tool_conversation_counts_only_with_a_final_answer(ending, completed):
    blocks = []
    for index in range(12):
        blocks += [
            {"type": "text", "text": "Synthetic progress", "presentation": "intermediate"},
            {"type": "tool_use", "id": f"call-{index}", "name": "example", "input": {}},
            {"type": "tool_result", "tool_use_id": f"call-{index}", "content": "Synthetic result"},
        ]
    blocks += ending
    entries = [row("user", "input", "turn"), row("assistant", "reply", "turn", blocks)]
    assert count_completed_conversation_turns(entries) == completed
    async with SessionStorage(":memory:") as storage:
        manager = SessionManager(storage, inject_time_prefix=False)
        node = await manager.create("agent:main:aggregate-tool-count")
        with turn_context_scope({"turn_id": "turn"}):
            await manager.append_message(node.session_key, "user", "Synthetic task")
            await manager.append_message(
                node.session_key, "assistant", "Synthetic progress", tool_calls=blocks
            )
        assert await storage.count_completed_conversation_turns(node.session_id) == completed
