"""Page messages and their complete turn outcomes share one transport budget."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.rpc.registry import RpcContext, RpcHandlerError
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus, TranscriptEntry
from opensquilla.session.storage import SessionStorage

PAGE_BYTES = 1_048_576
KEY = "agent:main:webchat:outcome-budget"


def encoded_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def message(index, *, turn_id=None, promoted=False, role="assistant", text_bytes=600):
    result = {
        "message_id": f"message-{index}", "transcript_id": index, "timestamp": index,
        "role": role, "text": "x" * text_bytes,
    }
    turn_id = turn_id or f"turn-{index}"
    if promoted:
        result["turn_context"] = {
            "disposition": "promoted", "turn_id": f"original-{index}",
            "promoted_turn_id": turn_id,
        }
    else:
        result["turn_context"] = {"turn_id": turn_id}
    return result


def message_turn(value):
    context = value.get("turn_context", {})
    return context.get("promoted_turn_id") or context.get("turn_id")


def outcome(turn_id, *, snapshot_bytes=48_000):
    return {
        "turn_id": turn_id, "task_id": turn_id, "status": "succeeded",
        "outcome": {"kind": "completed"},
        "activity_snapshot": {
            "version": 1, "task_id": turn_id, "turn_id": turn_id, "complete": True,
            "reasoning_utf16_length": snapshot_bytes,
            "entries": [{"kind": "reasoning", "text": "r" * snapshot_bytes}],
            "checksum": f"checksum-{turn_id}",
        },
    }


def install_history(monkeypatch, messages, outcomes):
    """Model canonical reader limits and cursors, without a live model or user data."""
    summaries = [{"compaction_id": "synthetic-checkpoint", "summary_text": "summary"}]
    all_turns = {message_turn(item) for item in messages}

    async def identity(*args):
        return "session-id", 1

    async def history(params, ctx):
        candidates = messages
        direction = "after" if params.get("after") is not None else "before"
        for edge in ("before", "after"):
            if params.get(edge) is not None:
                boundary = int(params[edge].split("|")[1])
                candidates = [
                    item for item in candidates
                    if (item["transcript_id"] < boundary if edge == "before"
                        else item["transcript_id"] > boundary)
                ]
        limit = params["limit"]
        selected = candidates[:limit] if direction == "after" else candidates[-limit:]
        selected_turns = {message_turn(item) for item in selected}
        return copy.deepcopy({
            "messages": selected, "has_more": len(selected) < len(candidates),
            "oldest_cursor": f"{selected[0]['timestamp']}|{selected[0]['transcript_id']}"
            if selected else None,
            "newest_cursor": f"{selected[-1]['timestamp']}|{selected[-1]['transcript_id']}"
            if selected else None,
            "canonical_available": True, "canonical_complete": True,
            "history_scope": "compacted", "compaction_summaries": summaries,
            "turn_outcomes": [
                item for item in outcomes
                if item.get("turn_id") in selected_turns or item.get("turn_id") not in all_turns
            ],
        })

    monkeypatch.setattr(rpc_sessions, "_snapshot_session_identity", identity)
    monkeypatch.setattr(rpc_sessions, "read_chat_history_v4", history)
    return summaries


@pytest.mark.parametrize("target_items", [50, 100])
@pytest.mark.parametrize("direction", ["before", "after"])
async def test_large_outcomes_paginate_both_directions_without_loss(
    monkeypatch, target_items, direction,
):
    messages = [message(index) for index in range(1, target_items + 1)]
    outcomes = [outcome(message_turn(item)) for item in messages]
    assert encoded_bytes(outcomes) > PAGE_BYTES
    summaries = install_history(monkeypatch, messages, outcomes)
    params = {
        "key": KEY, "direction": direction, "target_items": target_items,
        "target_bytes": PAGE_BYTES,
    }
    pages = []
    for _ in range(target_items + 1):
        page = await rpc_sessions._handle_sessions_history_page_v2(params, None)
        assert encoded_bytes(page) <= PAGE_BYTES
        assert page["items"]
        page_turns = {message_turn(item["message"]) for item in page["items"]}
        assert page["turn_outcomes"] == [
            item for item in outcomes if item["turn_id"] in page_turns
        ]
        assert page["compaction_summaries"] == summaries
        assert page["canonical_available"] and page["canonical_complete"]
        pages.append([item["message_id"] for item in page["items"]])
        if not page[f"has_more_{direction}"]:
            break
        assert not page["complete_for_requested_window"]
        params["cursor"] = page[f"{direction}_cursor"]
        assert params["cursor"]
    else:
        pytest.fail("A byte-bounded cursor must make progress")
    assert len(pages) > 1
    ordered_pages = reversed(pages) if direction == "before" else pages
    observed = [item for page in ordered_pages for item in page]
    assert observed == [item["message_id"] for item in messages]
    assert len(observed) == len(set(observed))


async def test_multiple_messages_share_one_complete_snapshot_budget(monkeypatch):
    messages = [message(index, turn_id="shared-turn") for index in range(1, 7)]
    complete = outcome("shared-turn", snapshot_bytes=650_000)
    install_history(monkeypatch, messages, [complete])
    page = await rpc_sessions._handle_sessions_history_page_v2({
        "key": KEY, "direction": "before", "target_items": 50, "target_bytes": 700_000,
    }, None)
    assert [item["message_id"] for item in page["items"]] == [
        item["message_id"] for item in messages
    ]
    assert page["turn_outcomes"] == [complete]
    assert encoded_bytes(page) <= 700_000
    assert page["complete_for_requested_window"]


async def test_promoted_user_turn_id_retains_its_outcome(monkeypatch):
    messages = [message(1, promoted=True), message(2, promoted=True, role="user")]
    outcomes = [outcome(message_turn(item), snapshot_bytes=12_000) for item in messages]
    install_history(monkeypatch, messages, outcomes)
    page = await rpc_sessions._handle_sessions_history_page_v2({
        "key": KEY, "direction": "before", "target_items": 50, "target_bytes": 20_000,
    }, None)
    assert [item["message_id"] for item in page["items"]] == ["message-2"]
    assert page["turn_outcomes"] == [outcomes[1]]
    assert page["has_more_before"]


@pytest.mark.parametrize("unattributed_turn", [None, "hidden-source-turn"])
async def test_unattributable_outcomes_are_retained(monkeypatch, unattributed_turn):
    messages = [message(1), message(2)]
    outcomes = [outcome(message_turn(item), snapshot_bytes=12_000) for item in messages]
    unattributed = {"status": "unknown", "outcome": {"kind": "failed"}}
    if unattributed_turn is not None:
        unattributed["turn_id"] = unattributed_turn
    install_history(monkeypatch, messages, [*outcomes, unattributed])
    page = await rpc_sessions._handle_sessions_history_page_v2({
        "key": KEY, "direction": "before", "target_items": 50, "target_bytes": 20_000,
    }, None)
    assert [item["message_id"] for item in page["items"]] == ["message-2"]
    assert page["turn_outcomes"] == [outcomes[1], unattributed]


async def test_oversized_unattributable_outcome_fails_without_silent_loss(monkeypatch):
    install_history(monkeypatch, [message(1)], [outcome("hidden-turn", snapshot_bytes=PAGE_BYTES)])
    with pytest.raises(RpcHandlerError) as caught:
        await rpc_sessions._handle_sessions_history_page_v2({
            "key": KEY, "direction": "before", "target_items": 50, "target_bytes": PAGE_BYTES,
        }, None)
    assert caught.value.code == "RESPONSE_TOO_LARGE"


@pytest.fixture
async def stored_case(tmp_path):
    storage = await SessionStorage.open(str(tmp_path / "outcome-budget.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    node = await manager.create(KEY)
    for index in range(1, 4):
        turn_id = f"turn-{index}"
        await storage.append_transcript_entry(TranscriptEntry(
            session_id=node.session_id, session_key=KEY, message_id=f"message-{index}",
            role="assistant", content=f"Answer {index}", created_at=index,
            turn_context={"turn_id": turn_id},
        ))
        await storage.create_agent_task(AgentTaskRecord(
            task_id=turn_id, session_key=KEY, status=AgentTaskStatus.SUCCEEDED,
            details={"turn_id": turn_id, "turn_outcome": {"kind": "completed"}},
        ))
    try:
        yield SimpleNamespace(
            storage=storage, manager=manager, node=node,
            context=RpcContext(conn_id="outcome-budget", session_manager=manager),
        )
    finally:
        await storage.close()


@pytest.mark.parametrize("direction", ["before", "after"])
async def test_post_snapshot_alias_trim_removes_only_excluded_turn_outcomes(
    stored_case, monkeypatch, direction,
):
    params = {"key": KEY, "direction": direction, "target_items": 50}
    original = await rpc_sessions._handle_sessions_history_page_v2(params, stored_case.context)
    assert len(original["turn_outcomes"]) == 3
    budget = encoded_bytes(original) + 64

    async def aliases(messages, **kwargs):
        assert not stored_case.storage.in_committed_history_view
        return [dict(item, projected_alias="a" * 1000) for item in messages]

    monkeypatch.setattr(rpc_sessions, "_project_fork_workspace_previews", aliases)
    page = await rpc_sessions._handle_sessions_history_page_v2(
        dict(params, target_bytes=budget), stored_case.context,
    )
    assert 0 < len(page["items"]) < len(original["items"])
    turns = {message_turn(item["message"]) for item in page["items"]}
    assert page["turn_outcomes"] == [
        item for item in original["turn_outcomes"] if item["turn_id"] in turns
    ]
    assert page["items"][-1 if direction == "before" else 0]["message_id"] == (
        "message-3" if direction == "before" else "message-1"
    )
    assert encoded_bytes(page) <= budget
    assert page[f"has_more_{direction}"] and page[f"{direction}_cursor"]
    assert not page["complete_for_requested_window"]


async def test_anchored_page_retains_hit_outcome_after_alias_trim(stored_case, monkeypatch):
    params = {"key": KEY, "direction": "before", "target_items": 50,
              "anchor_message_id": "message-2"}
    original = await rpc_sessions._handle_sessions_history_page_v2(params, stored_case.context)
    assert [item["message_id"] for item in original["items"]] == ["message-1", "message-2"]
    budget = encoded_bytes(original) + 64

    async def aliases(messages, **kwargs):
        return [dict(item, projected_alias="a" * 500) for item in messages]

    monkeypatch.setattr(rpc_sessions, "_project_fork_workspace_previews", aliases)
    page = await rpc_sessions._handle_sessions_history_page_v2(
        dict(params, target_bytes=budget), stored_case.context,
    )
    assert [item["message_id"] for item in page["items"]] == ["message-2"]
    assert page["turn_outcomes"] == [
        item for item in original["turn_outcomes"] if item["turn_id"] == "turn-2"
    ]
    assert page["has_more_before"] and page["has_more_after"]
    assert page["before_cursor"] and page["after_cursor"]
    assert encoded_bytes(page) <= budget
