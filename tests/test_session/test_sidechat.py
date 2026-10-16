from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio

from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.engine.history import HistoryReplayEntryProjection, project_history_replay
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionContextState,
    SessionIntent,
    SessionSummary,
    TranscriptEntry,
)
from opensquilla.session.sidechat import (
    SIDECHAT_REMINDER,
    SidechatAdmissionRejectedError,
    create_snapshot,
    set_state,
)
from opensquilla.session.storage import SessionStorage

PARENT = "agent:main:webchat:side-parent"


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    value = SessionManager(storage, inject_time_prefix=False)
    yield value
    await storage.close()


async def seed(manager):
    parent = await manager.create(
        PARENT,
        model="parent-model",
        origin={
            "sandbox_run_context": {"run_mode": "safe", "workspace": "/project"},
            "automation": "do not inherit",
        },
    )
    await manager.append_message(PARENT, "user", "The project name is Orion")
    await manager.append_message(PARENT, "assistant", "Remembered Orion")
    return parent


@pytest.mark.asyncio
async def test_close_cancels_owned_queued_inputs_and_fences_delayed_enqueue(manager):
    await seed(manager)
    child = await manager.create_sidechat(PARENT, "pending-close")
    descendant = await manager.create(
        "agent:main:subagent:side-pending", spawned_by=child.session_key,
    )

    async def enqueue(key, identity):
        return await manager._storage.enqueue_pending_chat_input(
            pending_input_id=identity, session_key=key,
            source_scope="web:web:operator", client_request_id=identity,
            client_message_id=identity, request_fingerprint=f"sha256:{identity}",
            payload={"key": key, "message": "follow up"},
        )

    await enqueue(PARENT, "parent-pending")
    await enqueue(child.session_key, "side-pending")
    await enqueue(descendant.session_key, "descendant-pending")
    await set_state(manager._storage, child.session_key, "closing")
    assert await manager._storage.list_pending_chat_inputs(child.session_key) == []
    assert await manager._storage.list_pending_chat_inputs(descendant.session_key) == []
    assert len(await manager._storage.list_pending_chat_inputs(PARENT)) == 1
    async with manager._storage._conn.execute(
        "SELECT pending_input_id FROM pending_chat_input_cancellations ORDER BY pending_input_id"
    ) as cursor:
        assert [row[0] for row in await cursor.fetchall()] == [
            "descendant-pending", "side-pending",
        ]
    for key in (child.session_key, descendant.session_key):
        with pytest.raises(SidechatAdmissionRejectedError):
            await enqueue(key, f"late-{key}")
    await set_state(manager._storage, child.session_key, "closed")
    await set_state(manager._storage, child.session_key, "closed")
    assert len(await manager._storage.list_pending_chat_inputs(PARENT)) == 1


@pytest.mark.asyncio
async def test_pending_enqueue_rejects_changed_parent_generation(manager):
    await seed(manager)
    child = await manager.create_sidechat(PARENT, "pending-parent-reset")
    async with manager._storage._write_transaction("test_parent_reset") as conn:
        await conn.execute("UPDATE sessions SET epoch = epoch + 1 WHERE session_key = ?", (PARENT,))
    with pytest.raises(SidechatAdmissionRejectedError, match="parent changed"):
        await manager._storage.enqueue_pending_chat_input(
            pending_input_id="late-reset", session_key=child.session_key,
            source_scope="web:web:operator", client_request_id="late-reset",
            client_message_id="late-reset", request_fingerprint="sha256:late-reset",
            payload={"key": child.session_key, "message": "late"},
        )


@pytest.mark.asyncio
async def test_snapshot_inherits_provider_history_but_hides_ui(manager):
    parent = await seed(manager)
    await manager._storage.save_summary(
        SessionSummary(
            session_id=parent.session_id,
            session_key=PARENT,
            summary_text="Earlier design decisions",
        )
    )
    await manager._storage.save_context_state(
        SessionContextState(
            session_id=parent.session_id,
            session_key=PARENT,
            state_kind="portable_notes",
            payload={"note": "important"},
            portable=True,
        )
    )
    child = await manager.create_sidechat(PARENT, "panel-1")
    rows = await manager.get_transcript(child.session_key)
    assert [entry.content for entry in rows] == [
        "The project name is Orion",
        "Remembered Orion",
        SIDECHAT_REMINDER,
    ]
    replay = project_history_replay(
        rows,
        trim_last_user=False,
        entry_projector=lambda e, _: HistoryReplayEntryProjection(
            role=e.role,
            content=e.content,
            turn_context=e.turn_context,
            assistant_replay=e.assistant_replay,
        ),
    )
    assert [message.content for message in replay.messages] == [entry.content for entry in rows]
    assert replay.messages[-1].role == "user"
    assert transcript_entries_to_chat_messages(rows) == []
    page = await manager.get_canonical_transcript_page(child.session_key, limit=1)
    assert not page.entries and not page.has_more
    assert page.canonical_complete is True
    assert (await manager.get_summaries(child.session_key))[
        0
    ].summary_text == "Earlier design decisions"
    assert (await manager.get_context_states(child.session_key))[0].payload == {"note": "important"}
    assert child.model == "parent-model"
    assert child.origin["sandbox_run_context"]["run_mode"] == "safe"
    assert "automation" not in child.origin
    assert child.parent_session_key == PARENT
    await manager.append_message(child.session_key, "user", "What is the project name?")
    visible = await manager.get_canonical_transcript_page(child.session_key, limit=1)
    assert len(visible.entries) == 1 and not visible.has_more
    assert visible.entries[0].content == "What is the project name?"
    assert visible.canonical_complete is True


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_archive_complete", [True, False])
async def test_display_coverage_uses_frozen_local_boundary_not_parent_replay(
    manager, parent_archive_complete,
):
    parent = await seed(manager)
    await manager.append_message(PARENT, "user", "Parent tail")
    await manager.persist_compaction_result(
        PARENT, "Parent summary", [{"role": "user", "content": "Parent tail"}],
        compaction_id="parent-display-compaction",
    )
    if not parent_archive_complete:
        async with manager._storage._write_transaction("test_missing_parent_archive") as conn:
            await conn.execute(
                "DELETE FROM compacted_transcript_entries WHERE session_id = ?",
                (parent.session_id,),
            )
    assert await manager._storage.is_canonical_transcript_complete(
        parent.session_id,
    ) is parent_archive_complete
    child = await manager.create_sidechat(PARENT, "display-proof")
    proof = child.origin["sidechat"]["displayHistory"]
    assert proof["inheritedCompactionCount"] == 1
    assert proof["inheritedSummaryThroughId"] > 0
    empty = await manager.get_canonical_transcript_page(child.session_key, limit=1)
    assert empty.canonical_complete is True
    assert empty.entries == [] and not empty.has_more
    # This display proof must never certify the model's inherited replay.
    assert not await manager._storage.is_canonical_transcript_complete(child.session_id)

    await manager.append_message(child.session_key, "user", "Local question")
    await manager.append_message(child.session_key, "assistant", "Local answer")
    await manager.persist_compaction_result(
        child.session_key, "Local summary",
        [{"role": "assistant", "content": "Local answer"}],
        compaction_id="child-display-compaction",
    )
    page = await manager.get_canonical_transcript_page(child.session_key, limit=1)
    assert page.canonical_complete is True and page.has_more
    assert [entry.content for entry in page.entries] == ["Local answer"]
    cursor = (page.entries[0].created_at, page.entries[0].id)
    earlier = await manager.get_canonical_transcript_page(
        child.session_key, limit=1, before=cursor,
    )
    assert earlier.canonical_complete is True and not earlier.has_more
    assert [entry.content for entry in earlier.entries] == ["Local question"]
    await manager.append_message(child.session_key, "user", "Local follow-up")
    await manager.persist_compaction_result(
        child.session_key, "Second local summary",
        [{"role": "user", "content": "Local follow-up"}],
        compaction_id="child-display-second-compaction",
    )
    second = await manager.get_canonical_transcript_page(child.session_key, limit=10)
    assert second.canonical_complete is True and not second.has_more
    assert [entry.content for entry in second.entries] == [
        "Local question", "Local answer", "Local follow-up",
    ]
    # Resetting the parent cannot revoke or fabricate the frozen local proof.
    await manager.apply_intent(PARENT, SessionIntent.RESET_SAME_KEY)
    assert (await manager.get_canonical_transcript_page(
        child.session_key, limit=10,
    )).canonical_complete is True


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["archive", "cursor", "summary", "count"])
async def test_display_coverage_rejects_missing_child_compaction_evidence(manager, damage):
    await seed(manager)
    child = await manager.create_sidechat(PARENT, "local-damage")
    await manager.append_message(child.session_key, "user", "Local question")
    await manager.append_message(child.session_key, "assistant", "Local answer")
    await manager.persist_compaction_result(
        child.session_key, "Local summary",
        [{"role": "assistant", "content": "Local answer"}],
        compaction_id="child-damage-compaction",
    )
    assert (await manager.get_canonical_transcript_page(
        child.session_key, limit=10,
    )).canonical_complete is True
    statements = {
        "archive": "DELETE FROM compacted_transcript_entries WHERE session_id = ? "
                   "AND content = 'Local question'",
        "cursor": "UPDATE compacted_transcript_entries SET original_entry_id = NULL "
                  "WHERE session_id = ? AND content = 'Local question'",
        "summary": "DELETE FROM session_summaries WHERE session_id = ?",
        "count": "UPDATE sessions SET compaction_count = compaction_count + 1 "
                 "WHERE session_id = ?",
    }
    async with manager._storage._write_transaction("test_damage_local_archive") as conn:
        await conn.execute(statements[damage], (child.session_id,))
    assert (await manager.get_canonical_transcript_page(
        child.session_key, limit=10,
    )).canonical_complete is False


@pytest.mark.asyncio
@pytest.mark.parametrize("compacted", [False, True])
async def test_older_side_without_boundary_only_proves_uncompacted_display(manager, compacted):
    await seed(manager)
    child = await manager.create_sidechat(PARENT, "old-display-proof")
    del child.origin["sidechat"]["displayHistory"]
    await manager._storage.upsert_session(child)
    await manager.append_message(child.session_key, "user", "Local question")
    if compacted:
        await manager.persist_compaction_result(
            child.session_key, "Local summary", [{"role": "user", "content": "Local question"}],
            compaction_id="old-child-compaction",
        )
    assert (await manager.get_canonical_transcript_page(
        child.session_key, limit=10,
    )).canonical_complete is (not compacted)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("version", 2), ("inheritedCompactionCount", 1),
    ("inheritedSummaryThroughId", True), ("inheritedArchiveThroughId", -1),
])
async def test_display_coverage_rejects_unknown_or_invalid_boundary(manager, field, value):
    await seed(manager)
    child = await manager.create_sidechat(PARENT, "invalid-display-proof")
    child.origin["sidechat"]["displayHistory"][field] = value
    await manager._storage.upsert_session(child)
    assert (await manager.get_canonical_transcript_page(
        child.session_key, limit=10,
    )).canonical_complete is False


@pytest.mark.asyncio
async def test_snapshot_inherits_mode_without_parent_once_grants(manager):
    parent = await seed(manager)
    parent.origin["sandbox_run_context"].update({
        "mounts": [
            {"path": "/durable", "access": "ro", "scope": "chat"},
            {"path": "/single-action", "access": "rw", "scope": "once"},
        ],
        "domains": [{"domain": "example.com", "scope": "once"}],
        "temporary_grants": [
            {"kind": "file", "value": "/single-action", "fingerprint": "parent-approval"},
        ],
    })
    await manager._storage.upsert_session(parent)
    child = await manager.create_sidechat(PARENT, "permissions")
    context = child.origin["sandbox_run_context"]
    assert context["run_mode"] == "safe"
    assert context["mounts"] == [
        {"path": str(Path("/durable").resolve()), "access": "ro", "scope": "chat"},
    ]
    assert context["domains"] == []
    assert context["temporary_grants"] == []


@pytest.mark.asyncio
async def test_idempotency_immutable_context_and_hidden_list(manager):
    await seed(manager)
    child = await manager.create_sidechat(PARENT, "panel-1")
    await manager.append_message(PARENT, "user", "New parent message")
    retried = await manager.create_sidechat(PARENT, "panel-1")
    assert retried.session_id == child.session_id
    assert "New parent message" not in [
        e.content for e in await manager.get_transcript(child.session_key)
    ]
    sibling = await manager.create_sidechat(PARENT, "panel-2")
    assert sibling.session_id != child.session_id
    assert "New parent message" in [
        e.content for e in await manager.get_transcript(sibling.session_key)
    ]
    assert [n.session_key for n in await manager._storage.list_sessions()] == [PARENT]
    assert [
        n.session_key for n in (await manager._storage.list_sessions_page(limit=10)).sessions
    ] == [PARENT]
    assert len(await manager._storage.list_sidechat_children(PARENT)) == 2
    assert not await manager._storage.list_sessions(spawned_by=PARENT)


@pytest.mark.asyncio
async def test_active_parent_only_copies_real_input_not_partial_answer(manager):
    parent = await seed(manager)
    await manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id="active-turn",
            session_key=PARENT,
            status=AgentTaskStatus.RUNNING,
        )
    )
    for role, content in (
        ("user", "current question"),
        ("assistant", "partial tool call"),
        ("tool", "partial result"),
    ):
        await manager._storage.append_transcript_entry(
            TranscriptEntry(
                session_id=parent.session_id,
                session_key=PARENT,
                role=role,
                content=content,
                turn_context={"turn_id": "active-turn"},
            )
        )
    child = await manager.create_sidechat(PARENT, "active")
    contents = [row.content for row in await manager.get_transcript(child.session_key)]
    assert "current question" in contents
    assert "partial tool call" not in contents and "partial result" not in contents
    assert not await manager._storage.list_agent_tasks(session_key=child.session_key)


@pytest.mark.asyncio
async def test_empty_parent_rolls_back_and_closed_child_cannot_reopen(manager):
    await manager.create(PARENT)
    with pytest.raises(ValueError, match="started parent"):
        await create_snapshot(manager._storage, PARENT, "empty")
    assert not await manager._storage.list_sidechat_children(PARENT)
    await manager.append_message(PARENT, "user", "started")
    child = await manager.create_sidechat(PARENT, "close")
    await set_state(manager._storage, child.session_key, "closing")
    await set_state(manager._storage, child.session_key, "closed")
    with pytest.raises(ValueError, match="closed"):
        await manager.create_sidechat(PARENT, "close")
    with pytest.raises(ValueError, match="cannot be forked"):
        await manager.branch(child.session_key, "agent:main:webchat:bad", fork_transcript=True)


@pytest.mark.asyncio
async def test_sidechat_requires_a_real_parent_user_turn(manager):
    """A persisted assistant/system row alone is not a started parent thread."""
    await manager.create(PARENT)
    await manager.append_message(PARENT, "assistant", "bootstrap output")
    with pytest.raises(ValueError, match="started parent"):
        await manager.create_sidechat(PARENT, "assistant-only")
    assert not await manager._storage.list_sidechat_children(PARENT)


@pytest.mark.asyncio
async def test_sidechat_is_ephemeral_and_does_not_materialize_working_attachments(manager):
    parent = await seed(manager)
    parent.origin["attachment_working_files"] = {
        ".opensquilla/attachments/source/note.txt": {
            "path": ".opensquilla/attachments/source/working/note.txt",
            "sha256": "a" * 64,
            "session_id": parent.session_id,
        }
    }
    await manager._storage.upsert_session(parent)

    child = await manager.create_sidechat(PARENT, "ephemeral-metadata")
    metadata = child.origin["sidechat"]
    assert metadata["ephemeral"] is True
    assert metadata["canAcceptDirectInput"] is True
    assert metadata["forkedFromSessionId"] == parent.session_id
    assert "attachment_working_files" not in (child.origin or {})


@pytest.mark.asyncio
async def test_closed_child_rejects_atomic_admission(manager):
    await seed(manager)
    child = await manager.create_sidechat(PARENT, "closed")
    await set_state(manager._storage, child.session_key, "closing")
    with pytest.raises(ValueError, match="closed or not ready"):
        await manager._storage.accept_turn(
            TranscriptEntry(
                session_id=child.session_id,
                session_key=child.session_key,
                role="user",
                content="raced send",
            ),
            expected_epoch=0,
            updated_at=10,
            task_record=None,
            source_scope="test",
            request_session_key=child.session_key,
            client_request_id="send",
            request_fingerprint="fingerprint",
        )
    assert "raced send" not in [e.content for e in await manager.get_transcript(child.session_key)]


@pytest.mark.asyncio
async def test_parent_reset_does_not_create_second_child_during_heartbeat(manager):
    await seed(manager)
    child = await manager.create_sidechat(PARENT, "same-owner")
    await manager._storage.increment_epoch(PARENT)
    with pytest.raises(ValueError, match="parent changed"):
        await manager.create_sidechat(PARENT, "same-owner")
    children = await manager._storage.list_sidechat_children(PARENT)
    assert [entry.session_key for entry in children] == [child.session_key]


@pytest.mark.asyncio
async def test_close_during_material_copy_cannot_reopen_child(manager, monkeypatch):
    await seed(manager)
    started, release = asyncio.Event(), asyncio.Event()

    async def copy(*args, **kwargs):
        started.set()
        await release.wait()

    monkeypatch.setattr(manager, "_copy_fork_materials", copy)
    preparation = asyncio.create_task(manager.create_sidechat(PARENT, "raced-close"))
    await started.wait()
    child = (await manager._storage.list_sidechat_children(PARENT))[0]
    await set_state(manager._storage, child.session_key, "closed")
    release.set()
    with pytest.raises(ValueError, match="closed during preparation"):
        await preparation
    node = await manager._storage.get_session(child.session_key)
    assert node.origin["sidechat"]["state"] == "closed"


@pytest.mark.asyncio
async def test_legacy_attachment_occurrence_keeps_parent_identity(manager):
    from opensquilla.session.attachment_manifest import extract_attachment_occurrences

    parent = await seed(manager)
    await manager.append_message(
        PARENT,
        "user",
        json.dumps(
            {
                "text": "Read this",
                "attachments": [
                    {
                        "kind": "attachment_ref",
                        "sha256_ref": "a" * 64,
                        "name": "note.txt",
                        "mime": "text/plain",
                        "size": 5,
                    }
                ],
            }
        ),
    )
    original = extract_attachment_occurrences(
        await manager.get_transcript(PARENT), session_id=parent.session_id
    )
    assert original
    child = await manager.create_sidechat(PARENT, "attachment")
    copied = extract_attachment_occurrences(
        await manager.get_transcript(child.session_key), session_id=child.session_id
    )
    assert [ref.attachment_id for ref in copied] == [ref.attachment_id for ref in original]


@pytest.mark.asyncio
async def test_normal_turn_with_optional_goal_probe_is_admitted(manager):
    from opensquilla.session.goals import ClaimCurrentGoalMutation

    await seed(manager)
    child = await manager.create_sidechat(PARENT, "normal-send")
    task = AgentTaskRecord(session_key=child.session_key)
    result = await manager._storage.accept_turn(
        TranscriptEntry(
            session_id=child.session_id,
            session_key=child.session_key,
            role="user",
            content="side question",
        ),
        expected_epoch=0,
        updated_at=10,
        task_record=task,
        source_scope="test",
        request_session_key=child.session_key,
        client_request_id="send",
        request_fingerprint="fingerprint",
        goal_mutation=ClaimCurrentGoalMutation(),
    )
    assert result.receipt.accepted_session_key == child.session_key
    assert any(
        e.content == "side question" for e in await manager.get_transcript(child.session_key)
    )


@pytest.mark.asyncio
async def test_agent_goal_creation_is_rejected_by_storage_for_side_but_allowed_for_main(manager):
    from opensquilla.session.goals import GoalConflictError, new_goal

    parent = await seed(manager)
    child = await manager.create_sidechat(PARENT, "goal-tool")
    for node in (parent, child):
        task_id = f"goal-task-{node.session_id}"
        await manager._storage.create_agent_task(AgentTaskRecord(
            task_id=task_id, session_key=node.session_key, status=AgentTaskStatus.RUNNING,
            details={"session_id": node.session_id, "session_epoch": node.epoch},
        ))
        goal = new_goal(
            goal_id=f"goal-{node.session_id}", session_key=node.session_key,
            session_id=node.session_id, session_epoch=node.epoch,
            objective="A goal requested inside an existing running turn", task_id=task_id,
        )
        if node is parent:
            saved = await manager._storage.create_goal_for_running_task(goal, task_id=task_id)
            assert saved.goal_id == goal.goal_id
        else:
            with pytest.raises(GoalConflictError, match="side conversation"):
                await manager._storage.create_goal_for_running_task(goal, task_id=task_id)
            assert await manager._storage.get_goal(node.session_key) is None
