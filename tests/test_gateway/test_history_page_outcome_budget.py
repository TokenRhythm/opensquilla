"""Page messages and their complete turn outcomes share one transport budget."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.rpc.registry import RpcContext, RpcHandlerError
from opensquilla.gateway.terminal_activity import terminal_activity_snapshot
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionSummary,
    TranscriptEntry,
)
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


async def test_oversized_unattributable_snapshot_keeps_terminal_outcome(monkeypatch):
    complete = outcome("hidden-turn", snapshot_bytes=PAGE_BYTES)
    install_history(monkeypatch, [message(1)], [complete])
    page = await rpc_sessions._handle_sessions_history_page_v2({
        "key": KEY, "direction": "before", "target_items": 50, "target_bytes": PAGE_BYTES,
    }, None)
    assert page["items"][0]["message_id"] == "message-1"
    assert page["turn_outcomes"] == [{
        **{key: value for key, value in complete.items() if key != "activity_snapshot"},
        "activity_snapshot_omitted": "history_page_budget",
    }]
    assert encoded_bytes(page) <= PAGE_BYTES
    assert "activity_snapshot" in complete


async def test_oversized_core_outcome_fails_without_silent_loss(monkeypatch):
    complete = outcome("hidden-turn", snapshot_bytes=1)
    complete["outcome"]["durable_facts"] = "x" * PAGE_BYTES
    install_history(monkeypatch, [message(1)], [complete])
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


@pytest.mark.parametrize("direction", ["before", "after"])
async def test_oversized_persisted_snapshot_keeps_history_pagination_progress(
    stored_case, direction,
):
    entries = [
        {
            "type": "segment",
            "id": f"tool-{index:04d}-" + "a" * 220,
            "order": index + 1,
            "segment_type": "tool",
            "tool_use_id": f"call-{index:04d}-" + "b" * 180,
            "name": "read_" + "x" * 120,
            "started_at": 1,
            "ended_at": 2,
            "is_error": False,
        }
        for index in range(2048)
    ]
    snapshot = terminal_activity_snapshot(
        {
            "version": 2,
            "task_id": "turn-2",
            "turn_id": "turn-2",
            "complete": True,
            "reasoning_utf16_length": 0,
            "entries": entries,
        },
        task_id="turn-2",
        turn_id="turn-2",
    )
    assert snapshot is not None
    assert encoded_bytes(snapshot) > PAGE_BYTES
    await stored_case.storage.update_agent_task(
        "turn-2", details_patch={"activity_snapshot": snapshot},
    )
    params = {
        "key": KEY,
        "direction": direction,
        "target_items": 50,
        "target_bytes": PAGE_BYTES,
    }
    latest = await rpc_sessions._handle_sessions_history_page_v2(params, stored_case.context)
    assert [item["message_id"] for item in latest["items"]] == [
        "message-3" if direction == "before" else "message-1",
    ]
    assert latest[f"has_more_{direction}"]
    assert latest[f"{direction}_cursor"]

    params["cursor"] = latest[f"{direction}_cursor"]
    for _ in range(3):
        older = await rpc_sessions._handle_sessions_history_page_v2(params, stored_case.context)
        assert [item["message_id"] for item in older["items"]] == (
            ["message-1", "message-2"] if direction == "before"
            else ["message-2", "message-3"]
        )
        assert not older[f"has_more_{direction}"]
        assert encoded_bytes(older) <= PAGE_BYTES
        turn = next(item for item in older["turn_outcomes"] if item["turn_id"] == "turn-2")
        assert turn["activity_snapshot_omitted"] == "history_page_budget"
        assert "activity_snapshot" not in turn
        assert turn["outcome"] == {"kind": "completed"}
        assert turn["status"] == "succeeded"

    anchored = await rpc_sessions._handle_sessions_history_page_v2({
        "key": KEY, "direction": "before", "anchor_message_id": "message-2",
        "target_items": 50, "target_bytes": PAGE_BYTES,
    }, stored_case.context)
    assert [item["message_id"] for item in anchored["items"]] == ["message-1", "message-2"]
    assert encoded_bytes(anchored) <= PAGE_BYTES

    stored = await rpc_sessions.read_chat_history_v4(
        {"sessionKey": KEY, "limit": 3}, stored_case.context,
    )
    assert [item["message_id"] for item in stored["messages"]] == [
        "message-1", "message-2", "message-3",
    ]
    assert stored["turn_outcomes"][1]["activity_snapshot"] == snapshot


async def test_accumulated_summaries_keep_previously_readable_history(stored_case):
    summary_text = "摘要。" * 2000
    assert len(summary_text.encode()) == 18_000

    async def save_summary(index):
        await stored_case.storage.save_summary(SessionSummary(
            session_id=stored_case.node.session_id,
            session_key=KEY,
            compaction_index=index,
            compaction_id=f"synthetic-checkpoint-{index}",
            summary_text=summary_text,
            created_at=index,
        ))

    for index in range(1, 57):
        await save_summary(index)
    params = {
        "key": KEY,
        "direction": "before",
        "target_items": 1,
        "target_bytes": PAGE_BYTES,
    }
    latest = await rpc_sessions._handle_sessions_history_page_v2(params, stored_case.context)
    assert [item["message_id"] for item in latest["items"]] == ["message-3"]
    assert len(latest["compaction_summaries"]) == 56
    assert latest["compaction_summaries"][0]["summary_text"] == summary_text
    assert encoded_bytes(latest) <= PAGE_BYTES
    assert latest["has_more_before"]
    assert latest["before_cursor"]

    for index in range(57, 65):
        await save_summary(index)
    for cursor in (None, latest["before_cursor"]):
        for _ in range(3):
            page = await rpc_sessions._handle_sessions_history_page_v2(
                dict(params, cursor=cursor), stored_case.context,
            )
            assert [item["message_id"] for item in page["items"]] == [
                "message-3" if cursor is None else "message-2",
            ]
            assert len(page["compaction_summaries"]) == 64
            assert all(item["summary_text"] == "" for item in page["compaction_summaries"])
            assert all(item["summary_text_omitted"] == "history_page_budget"
                       for item in page["compaction_summaries"])
            assert page["canonical_available"] == latest["canonical_available"]
            assert page["canonical_complete"] == latest["canonical_complete"]
            assert encoded_bytes(page) <= PAGE_BYTES

    for direction in ("before", "after"):
        request = dict(params, direction=direction)
        seen = []
        if direction == "after":
            request["cursor"] = rpc_sessions._v2_cursor_encode(
                key=KEY, session_id=stored_case.node.session_id,
                session_epoch=stored_case.node.epoch, direction="after", cursor="1|1",
            )
            seen.append("message-1")
        for _ in range(4):
            page = await rpc_sessions._handle_sessions_history_page_v2(request, stored_case.context)
            seen.extend(item["message_id"] for item in page["items"])
            assert encoded_bytes(page) <= PAGE_BYTES
            if not page[f"has_more_{direction}"]:
                break
            request["cursor"] = page[f"{direction}_cursor"]
        assert sorted(seen) == ["message-1", "message-2", "message-3"]

    stored = await rpc_sessions.read_chat_history_v4(
        {"sessionKey": KEY, "limit": 3}, stored_case.context,
    )
    assert [item["message_id"] for item in stored["messages"]] == [
        "message-1", "message-2", "message-3",
    ]
    assert len(stored["compaction_summaries"]) == 64
    assert encoded_bytes(stored["compaction_summaries"]) > PAGE_BYTES
    assert all(item["summary_text"] == summary_text for item in stored["compaction_summaries"])


@pytest.mark.parametrize("empty", [False, True])
async def test_summary_marker_count_cannot_block_transcript_progress(monkeypatch, empty):
    summaries = install_history(monkeypatch, [] if empty else [message(6000)], [])
    summaries[:] = [
        {
            "id": index,
            "compaction_id": f"checkpoint-{index}",
            "compaction_index": index,
            "trigger_reason": "automatic",
            "summary_text": "",
            "summary_format": "text",
            "coverage_status": "complete",
            "removed_count": 1,
            "kept_count": 1,
            "covered_through_id": index,
            "created_at": index,
        }
        for index in range(1, 6001)
    ]
    original = copy.deepcopy(summaries)
    assert encoded_bytes(summaries) > PAGE_BYTES
    page = await rpc_sessions._handle_sessions_history_page_v2({
        "key": KEY, "direction": "before", "target_bytes": PAGE_BYTES,
    }, None)
    assert encoded_bytes(page) <= PAGE_BYTES
    assert len(page["items"]) == (0 if empty else 1)
    assert page["canonical_available"] and page["canonical_complete"]
    retained = page["compaction_summaries"]
    assert 0 < len(retained) < len(summaries)
    assert retained[-1]["id"] == 6000
    omitted = sum(item.get("summary_markers_omitted", 0) for item in retained)
    assert omitted == len(summaries) - len(retained)
    assert summaries == original


async def test_post_snapshot_alias_budget_can_omit_optional_activity(stored_case, monkeypatch):
    snapshot = {
        "version": 1,
        "task_id": "turn-3",
        "turn_id": "turn-3",
        "phases": [
            {"kind": "provider", "phase": "requesting", "at": index}
            for index in range(1, 33)
        ],
    }
    await stored_case.storage.update_agent_task(
        "turn-3", details_patch={"activity_snapshot": snapshot},
    )
    params = {"key": KEY, "direction": "before", "target_items": 1}
    original = await rpc_sessions._handle_sessions_history_page_v2(params, stored_case.context)
    assert original["turn_outcomes"][0]["activity_snapshot"] == snapshot
    budget = encoded_bytes(original) + 64

    async def aliases(messages, **kwargs):
        return [dict(item, projected_alias="a" * 1000) for item in messages]

    monkeypatch.setattr(rpc_sessions, "_project_fork_workspace_previews", aliases)
    page = await rpc_sessions._handle_sessions_history_page_v2(
        dict(params, target_bytes=budget), stored_case.context,
    )
    assert encoded_bytes(page) <= budget
    assert [item["message_id"] for item in page["items"]] == ["message-3"]
    assert page["turn_outcomes"] == [{
        **{key: value for key, value in original["turn_outcomes"][0].items()
           if key != "activity_snapshot"},
        "activity_snapshot_omitted": "history_page_budget",
    }]
    assert original["turn_outcomes"][0]["activity_snapshot"] == snapshot
    task = await stored_case.storage.get_agent_task("turn-3")
    assert task.details["activity_snapshot"] == snapshot


@pytest.mark.parametrize("oversized_nearest", [False, True])
async def test_summary_markers_prioritize_modern_page_time_with_legacy_seconds(
    monkeypatch, oversized_nearest,
):
    item = message(1)
    item["timestamp"] = 1_800_000_000_000
    summaries = install_history(monkeypatch, [item], [])
    summaries[:] = [
        {"id": 1, "created_at": 1_799_000_000_000},
        {"id": 2, "created_at": 1_800_000_000},
        {"id": 3, "created_at": 1_801_000_000_000},
    ]
    for summary in summaries:
        summary.update(summary_text="", summary_format="x" * 1000)
    if oversized_nearest:
        summaries[1]["compaction_id"] = "x" * PAGE_BYTES
    original = copy.deepcopy(summaries)
    expected = summaries[2 if oversized_nearest else 1]
    base = await rpc_sessions._handle_sessions_history_page_v2({
        "key": KEY, "direction": "before", "target_bytes": PAGE_BYTES,
    }, None)
    budget = encoded_bytes({
        **base,
        "compaction_summaries": [{**expected, "summary_markers_omitted": 2}],
    }) + 4
    page = await rpc_sessions._handle_sessions_history_page_v2({
        "key": KEY, "direction": "before", "target_bytes": budget,
    }, None)
    assert page["items"][0]["message_id"] == "message-1"
    assert page["compaction_summaries"] == [{**expected, "summary_markers_omitted": 2}]
    assert encoded_bytes(page) <= budget
    assert summaries == original
