"""Tests for the sessions.fork RPC handler."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import pytest_asyncio

from opensquilla.gateway import rpc_sessions
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.scopes import METHOD_SCOPES, WRITE_SCOPE
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    SessionStatus,
    TranscriptEntry,
)
from opensquilla.session.storage import SessionStorage

_PRINCIPAL = Principal(
    role="operator", scopes=frozenset(["operator.admin"]), is_owner=True, authenticated=True
)

PARENT_KEY = "agent:main:webchat:parent01"


@pytest.fixture
def dispatcher():
    return get_dispatcher()


@pytest_asyncio.fixture
async def manager():
    storage = SessionStorage(":memory:")
    await storage.connect()
    mgr = SessionManager(storage, inject_time_prefix=False)
    yield mgr
    await storage.close()


@pytest.fixture
def ctx(manager):
    context = RpcContext(
        conn_id="test-conn",
        principal=_PRINCIPAL,
        config=GatewayConfig(memory={"flush_enabled": False}),
    )
    context.session_manager = manager
    return context


async def _seed_parent(manager, *, display_name: str | None = None) -> None:
    await manager.create(PARENT_KEY, agent_id="main", display_name=display_name)
    await manager.append_message(PARENT_KEY, "user", "original question", token_count=5)
    await manager.append_message(PARENT_KEY, "assistant", "original answer", token_count=5)


async def _seed_parent_with_markers(manager) -> tuple[Any, Any, Any]:
    await manager.create(PARENT_KEY, agent_id="main")
    first = await manager.append_message(PARENT_KEY, "user", "A marker", token_count=1)
    middle = await manager.append_message(PARENT_KEY, "user", "B marker", token_count=1)
    final = await manager.append_message(PARENT_KEY, "user", "C marker", token_count=1)
    return first, middle, final


async def _seed_parent_with_terminal_turn(
    manager,
    *,
    status=AgentTaskStatus.SUCCEEDED,
    accepted_routing_mode: str | None = None,
) -> str:
    parent = await manager.create(PARENT_KEY, agent_id="main")
    turn_id = "turn-rpc-terminal"
    for role, content in (
        ("user", "turn question"),
        ("assistant", "tool request"),
        ("tool", "tool output"),
        ("assistant", "turn answer"),
    ):
        await manager._storage.append_transcript_entry(
            TranscriptEntry(
                session_id=parent.session_id,
                session_key=parent.session_key,
                role=role,
                content=content,
                turn_context={"turn_id": turn_id},
            )
        )
    await manager._storage.append_transcript_entry(
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="user",
            content="later question",
            turn_context={"turn_id": "turn-rpc-later"},
        )
    )
    details = {}
    if accepted_routing_mode is not None:
        details["accepted_model_routing"] = {
            "effective_mode": accepted_routing_mode,
        }
    await manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id=turn_id,
            session_key=parent.session_key,
            status=status,
            details=details,
        )
    )
    return turn_id


def _list_row(list_res: Any, key: str) -> dict[str, Any]:
    rows = [row for row in list_res.payload["sessions"] if row["key"] == key]
    assert rows, f"session {key} missing from sessions.list"
    return rows[0]


def test_fork_requires_write_scope() -> None:
    assert METHOD_SCOPES["sessions.fork"] == WRITE_SCOPE
    assert METHOD_SCOPES["sessions.fork"] == METHOD_SCOPES["sessions.create"]


def test_fork_through_turn_requires_write_scope() -> None:
    assert METHOD_SCOPES["sessions.forkThroughTurn"] == WRITE_SCOPE
    assert METHOD_SCOPES["sessions.forkThroughTurn"] == METHOD_SCOPES["sessions.fork"]


@pytest.mark.asyncio
async def test_fork_copies_transcript_and_marks_fork(dispatcher, ctx, manager):
    await _seed_parent(manager)

    res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert res.ok is True
    child_key = res.payload["key"]
    assert res.payload["parentKey"] == PARENT_KEY
    assert child_key != PARENT_KEY
    assert child_key.startswith("agent:main:webchat:")

    entries = await manager.get_transcript(child_key)
    assert [entry.content for entry in entries] == ["original question", "original answer"]

    list_res = await dispatcher.dispatch("r2", "sessions.list", None, ctx)
    assert list_res.ok is True
    child_row = _list_row(list_res, child_key)
    assert child_row["forkedFromParent"] is True
    assert child_row["forked_from_parent"] is True
    assert child_row["parentSessionKey"] == PARENT_KEY
    assert child_row["parent_session_key"] == PARENT_KEY
    assert child_row["spawnDepth"] == 1
    assert child_row["spawn_depth"] == 1
    parent_row = _list_row(list_res, PARENT_KEY)
    assert parent_row["forkedFromParent"] is False
    assert parent_row["spawnDepth"] == 0


@pytest.mark.asyncio
async def test_fork_before_message_copies_only_prefix(dispatcher, ctx, manager):
    _first, middle, _final = await _seed_parent_with_markers(manager)

    res = await dispatcher.dispatch(
        "r1",
        "sessions.fork",
        {"key": PARENT_KEY, "beforeMessageId": middle.message_id},
        ctx,
    )

    assert res.ok is True
    child_key = res.payload["key"]
    assert child_key != PARENT_KEY

    child_entries = await manager.get_transcript(child_key)
    assert [entry.content for entry in child_entries] == ["A marker"]

    parent_entries = await manager.get_transcript(PARENT_KEY)
    assert [entry.content for entry in parent_entries] == [
        "A marker",
        "B marker",
        "C marker",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("param_name", ["throughTurnId", "through_turn_id"])
async def test_fork_through_turn_aliases_copy_complete_turn_inclusively(
    dispatcher,
    ctx,
    manager,
    param_name,
):
    turn_id = await _seed_parent_with_terminal_turn(manager)

    res = await dispatcher.dispatch(
        "r1",
        "sessions.fork",
        {"key": PARENT_KEY, param_name: turn_id},
        ctx,
    )

    assert res.ok is True
    child_entries = await manager.get_transcript(res.payload["key"])
    assert [(entry.role, entry.content) for entry in child_entries] == [
        ("user", "turn question"),
        ("assistant", "tool request"),
        ("tool", "tool output"),
        ("assistant", "turn answer"),
    ]
    assert [entry.content for entry in await manager.get_transcript(PARENT_KEY)] == [
        "turn question",
        "tool request",
        "tool output",
        "turn answer",
        "later question",
    ]
    assert res.payload["forkMode"] == "through_turn"
    assert res.payload["throughTurnId"] == turn_id


@pytest.mark.asyncio
@pytest.mark.parametrize("param_name", ["throughTurnId", "through_turn_id"])
async def test_capability_safe_fork_through_turn_copies_exact_prefix_and_echoes_mode(
    dispatcher,
    ctx,
    manager,
    param_name,
):
    turn_id = await _seed_parent_with_terminal_turn(manager)

    res = await dispatcher.dispatch(
        "r1",
        "sessions.forkThroughTurn",
        {"key": PARENT_KEY, param_name: turn_id},
        ctx,
    )

    assert res.ok is True
    assert res.payload["parentKey"] == PARENT_KEY
    assert res.payload["forkMode"] == "through_turn"
    assert res.payload["throughTurnId"] == turn_id
    assert [entry.content for entry in await manager.get_transcript(res.payload["key"])] == [
        "turn question",
        "tool request",
        "tool output",
        "turn answer",
    ]


@pytest.mark.asyncio
async def test_capability_safe_fork_through_turn_requires_anchor_without_side_effects(
    dispatcher,
    ctx,
    manager,
):
    await _seed_parent(manager)

    res = await dispatcher.dispatch(
        "r1",
        "sessions.forkThroughTurn",
        {"key": PARENT_KEY},
        ctx,
    )

    assert res.ok is False
    assert res.error.code == "INVALID_REQUEST"
    assert "throughTurnId is required" in res.error.message
    list_res = await dispatcher.dispatch("r2", "sessions.list", None, ctx)
    assert [row["key"] for row in list_res.payload["sessions"]] == [PARENT_KEY]


@pytest.mark.asyncio
async def test_legacy_fork_without_anchor_still_copies_full_transcript(
    dispatcher,
    ctx,
    manager,
):
    await _seed_parent(manager)

    res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)

    assert res.ok is True
    assert "forkMode" not in res.payload
    assert [entry.content for entry in await manager.get_transcript(res.payload["key"])] == [
        "original question",
        "original answer",
    ]


@pytest.mark.asyncio
async def test_fork_through_inherited_turn_accepts_verified_parent_task_owner(
    dispatcher,
    ctx,
    manager,
):
    turn_id = await _seed_parent_with_terminal_turn(manager)
    first = await dispatcher.dispatch(
        "r1",
        "sessions.fork",
        {"key": PARENT_KEY, "throughTurnId": turn_id},
        ctx,
    )
    assert first.ok is True

    second = await dispatcher.dispatch(
        "r2",
        "sessions.fork",
        {"key": first.payload["key"], "throughTurnId": turn_id},
        ctx,
    )

    assert second.ok is True
    assert second.payload["parentKey"] == first.payload["key"]
    assert [entry.content for entry in await manager.get_transcript(second.payload["key"])] == [
        "turn question",
        "tool request",
        "tool output",
        "turn answer",
    ]


@pytest.mark.asyncio
async def test_fork_rejects_conflicting_history_anchors(dispatcher, ctx, manager):
    turn_id = await _seed_parent_with_terminal_turn(manager)
    parent_entries = await manager.get_transcript(PARENT_KEY)

    res = await dispatcher.dispatch(
        "r1",
        "sessions.fork",
        {
            "key": PARENT_KEY,
            "beforeMessageId": parent_entries[-1].message_id,
            "throughTurnId": turn_id,
        },
        ctx,
    )

    assert res.ok is False
    assert res.error.code == "INVALID_REQUEST"
    assert "mutually exclusive" in res.error.message


@pytest.mark.asyncio
@pytest.mark.parametrize("param_name", ["throughTurnId", "through_turn_id"])
async def test_fork_rejects_empty_through_turn_anchor(dispatcher, ctx, manager, param_name):
    await _seed_parent(manager)

    res = await dispatcher.dispatch(
        "r1",
        "sessions.fork",
        {"key": PARENT_KEY, param_name: "   "},
        ctx,
    )

    assert res.ok is False
    assert res.error.code == "INVALID_REQUEST"
    assert "must not be empty" in res.error.message
    list_res = await dispatcher.dispatch("r2", "sessions.list", None, ctx)
    assert len(list_res.payload["sessions"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "aliases",
    [
        {"throughTurnId": None, "through_turn_id": "turn-rpc-terminal"},
        {"throughTurnId": "turn-rpc-terminal", "through_turn_id": None},
        {"throughTurnId": "turn-rpc-terminal", "through_turn_id": "turn-other"},
        {"throughTurnId": "", "through_turn_id": "turn-rpc-terminal"},
    ],
)
async def test_fork_rejects_invalid_dual_through_turn_aliases_without_side_effects(
    dispatcher,
    ctx,
    manager,
    aliases,
):
    await _seed_parent_with_terminal_turn(manager)

    res = await dispatcher.dispatch(
        "r1",
        "sessions.fork",
        {"key": PARENT_KEY, **aliases},
        ctx,
    )

    assert res.ok is False
    assert res.error.code == "INVALID_REQUEST"
    list_res = await dispatcher.dispatch("r2", "sessions.list", None, ctx)
    assert [row["key"] for row in list_res.payload["sessions"]] == [PARENT_KEY]


@pytest.mark.asyncio
async def test_fork_accepts_matching_dual_through_turn_aliases(dispatcher, ctx, manager):
    turn_id = await _seed_parent_with_terminal_turn(manager)

    res = await dispatcher.dispatch(
        "r1",
        "sessions.fork",
        {
            "key": PARENT_KEY,
            "throughTurnId": turn_id,
            "through_turn_id": turn_id,
        },
        ctx,
    )

    assert res.ok is True


@pytest.mark.asyncio
async def test_fork_deep_material_json_fails_before_child_or_transcript_writes(
    dispatcher,
    ctx,
    manager,
    monkeypatch,
):
    parent = await manager.create(PARENT_KEY, agent_id="main")
    turn_id = "turn-deep-material-json"
    nested_json = '{"text":"deep","nested":' + "[" * 10_000 + "0" + "]" * 10_000 + "}"
    await manager._storage.append_transcript_entry(
        TranscriptEntry(
            session_id=parent.session_id,
            session_key=parent.session_key,
            role="assistant",
            content=nested_json,
            turn_context={"turn_id": turn_id},
        )
    )
    await manager._storage.create_agent_task(
        AgentTaskRecord(
            task_id=turn_id,
            session_key=parent.session_key,
            status=AgentTaskStatus.SUCCEEDED,
        )
    )
    child_key = "agent:main:webchat:deep-json-child"
    monkeypatch.setattr(rpc_sessions, "_create_session_key", lambda *_args: child_key)

    res = await dispatcher.dispatch(
        "r1",
        "sessions.fork",
        {"key": PARENT_KEY, "throughTurnId": turn_id},
        ctx,
    )

    assert res.ok is False
    assert res.error.code == "INVALID_REQUEST"
    assert "nested too deeply" in res.error.message
    assert await manager.get_session(child_key) is None
    for table in ("transcript_entries", "compacted_transcript_entries"):
        async with manager._storage.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_key = ?",  # noqa: S608
            (child_key,),
        ) as cursor:
            assert int((await cursor.fetchone())[0]) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn_id", "status", "expected_code", "expected_message"),
    [
        ("turn-does-not-exist", AgentTaskStatus.SUCCEEDED, "NOT_FOUND", "turn not found"),
        ("turn-rpc-terminal", AgentTaskStatus.RUNNING, "INVALID_REQUEST", "active"),
    ],
)
async def test_fork_through_turn_rejects_missing_or_active_turn(
    dispatcher,
    ctx,
    manager,
    turn_id,
    status,
    expected_code,
    expected_message,
):
    await _seed_parent_with_terminal_turn(manager, status=status)

    res = await dispatcher.dispatch(
        "r1",
        "sessions.fork",
        {"key": PARENT_KEY, "throughTurnId": turn_id},
        ctx,
    )

    assert res.ok is False
    assert res.error.code == expected_code
    assert expected_message in res.error.message


@pytest.mark.asyncio
async def test_forked_child_rests_outside_active_statuses(dispatcher, ctx, manager):
    await _seed_parent(manager)

    res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert res.ok is True
    child_key = res.payload["key"]

    child = await manager.get_session(child_key)
    assert child is not None
    assert child.status == SessionStatus.DONE

    list_res = await dispatcher.dispatch("r2", "sessions.list", None, ctx)
    child_row = _list_row(list_res, child_key)
    assert str(child_row["status"]) not in {"running", "queued"}
    assert child_row["runStatus"] == "idle"


@pytest.mark.asyncio
async def test_fork_title_param_sets_child_display_name(dispatcher, ctx, manager):
    await _seed_parent(manager, display_name="Budget planning")

    res = await dispatcher.dispatch(
        "r1", "sessions.fork", {"key": PARENT_KEY, "title": "Budget variant"}, ctx
    )
    assert res.ok is True
    child = await manager.get_session(res.payload["key"])
    assert child.display_name == "Budget variant"


@pytest.mark.asyncio
async def test_fork_without_title_adds_stable_copy_number(dispatcher, ctx, manager):
    await _seed_parent(manager, display_name="Budget planning")

    res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert res.ok is True
    child = await manager.get_session(res.payload["key"])
    assert child.display_name == "Budget planning (2)"

    second_res = await dispatcher.dispatch("r2", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert second_res.ok is True
    second_child = await manager.get_session(second_res.payload["key"])
    assert second_child.display_name == "Budget planning (3)"


@pytest.mark.asyncio
async def test_nested_fork_continues_title_family(dispatcher, ctx, manager):
    await _seed_parent(manager, display_name="Budget planning")

    first_res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert first_res.ok is True
    nested_res = await dispatcher.dispatch(
        "r2",
        "sessions.fork",
        {"key": first_res.payload["key"]},
        ctx,
    )

    assert nested_res.ok is True
    nested_child = await manager.get_session(nested_res.payload["key"])
    assert nested_child.display_name == "Budget planning (3)"


@pytest.mark.asyncio
async def test_fork_uses_sidebar_transcript_title_as_numbering_base(dispatcher, ctx, manager):
    await _seed_parent(manager)

    res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)

    assert res.ok is True
    child = await manager.get_session(res.payload["key"])
    assert child.display_name == "original question (2)"
    list_res = await dispatcher.dispatch("r2", "sessions.list", None, ctx)
    assert _list_row(list_res, child.session_key)["title"] == "original question (2)"


@pytest.mark.asyncio
async def test_fork_after_manual_rename_starts_new_title_family(dispatcher, ctx, manager):
    await _seed_parent(manager, display_name="Budget planning")
    first_res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert first_res.ok is True
    await manager.update(first_res.payload["key"], display_name="Cost review")

    renamed_res = await dispatcher.dispatch(
        "r2",
        "sessions.fork",
        {"key": first_res.payload["key"]},
        ctx,
    )

    assert renamed_res.ok is True
    renamed_child = await manager.get_session(renamed_res.payload["key"])
    assert renamed_child.display_name == "Cost review (2)"


@pytest.mark.asyncio
async def test_fork_preserves_natural_numeric_suffix_on_root_title(dispatcher, ctx, manager):
    await _seed_parent(manager, display_name="Release (2)")

    first_res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    second_res = await dispatcher.dispatch("r2", "sessions.fork", {"key": PARENT_KEY}, ctx)

    assert first_res.ok is True
    assert second_res.ok is True
    first_child = await manager.get_session(first_res.payload["key"])
    second_child = await manager.get_session(second_res.payload["key"])
    assert first_child.display_name == "Release (2) (2)"
    assert second_child.display_name == "Release (2) (3)"


@pytest.mark.asyncio
async def test_fork_preserves_natural_numeric_suffix_after_manual_rename(
    dispatcher,
    ctx,
    manager,
):
    await _seed_parent(manager, display_name="Budget planning")
    first_res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert first_res.ok is True
    await manager.update(first_res.payload["key"], display_name="Release (2)")

    renamed_res = await dispatcher.dispatch(
        "r2",
        "sessions.fork",
        {"key": first_res.payload["key"]},
        ctx,
    )

    assert renamed_res.ok is True
    renamed_child = await manager.get_session(renamed_res.payload["key"])
    assert renamed_child.display_name == "Release (2) (2)"


@pytest.mark.asyncio
async def test_deleting_earlier_fork_does_not_renumber_survivors(dispatcher, ctx, manager):
    await _seed_parent(manager, display_name="Budget planning")
    first_res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    second_res = await dispatcher.dispatch("r2", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert first_res.ok is True
    assert second_res.ok is True
    await manager._storage.delete_session(first_res.payload["key"])

    third_res = await dispatcher.dispatch("r3", "sessions.fork", {"key": PARENT_KEY}, ctx)

    assert third_res.ok is True
    survivor = await manager.get_session(second_res.payload["key"])
    third_child = await manager.get_session(third_res.payload["key"])
    assert survivor.display_name == "Budget planning (3)"
    assert third_child.display_name == "Budget planning (4)"


@pytest.mark.asyncio
async def test_concurrent_forks_of_same_parent_allocate_distinct_titles(dispatcher, ctx, manager):
    class LockingTurnRunner:
        def __init__(self) -> None:
            self._locks: dict[str, asyncio.Lock] = {}

        def get_session_lock(self, session_key: str) -> asyncio.Lock:
            return self._locks.setdefault(session_key, asyncio.Lock())

    await _seed_parent(manager, display_name="Budget planning")
    ctx.turn_runner = LockingTurnRunner()

    first_res, second_res = await asyncio.gather(
        dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx),
        dispatcher.dispatch("r2", "sessions.fork", {"key": PARENT_KEY}, ctx),
    )

    assert first_res.ok is True
    assert second_res.ok is True
    children = [
        await manager.get_session(first_res.payload["key"]),
        await manager.get_session(second_res.payload["key"]),
    ]
    assert sorted(child.display_name for child in children) == [
        "Budget planning (2)",
        "Budget planning (3)",
    ]


@pytest.mark.asyncio
async def test_concurrent_forks_of_siblings_allocate_distinct_titles_without_turn_runner(
    dispatcher,
    ctx,
    manager,
):
    await _seed_parent(manager, display_name="Budget planning")
    first_res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    second_res = await dispatcher.dispatch("r2", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert first_res.ok is True
    assert second_res.ok is True

    nested_first, nested_second = await asyncio.gather(
        dispatcher.dispatch("r3", "sessions.fork", {"key": first_res.payload["key"]}, ctx),
        dispatcher.dispatch("r4", "sessions.fork", {"key": second_res.payload["key"]}, ctx),
    )

    assert nested_first.ok is True
    assert nested_second.ok is True
    children = [
        await manager.get_session(nested_first.payload["key"]),
        await manager.get_session(nested_second.payload["key"]),
    ]
    assert sorted(child.display_name for child in children) == [
        "Budget planning (4)",
        "Budget planning (5)",
    ]


@pytest.mark.asyncio
async def test_fork_title_persistence_failure_does_not_leave_visible_child(
    dispatcher,
    ctx,
    manager,
    monkeypatch,
):
    await _seed_parent(manager, display_name="Budget planning")
    original_upsert = manager._storage.upsert_session

    async def fail_named_child_upsert(node, **kwargs):
        if node.parent_session_key == PARENT_KEY and node.display_name:
            raise RuntimeError("injected named child write failure")
        return await original_upsert(node, **kwargs)

    monkeypatch.setattr(manager._storage, "upsert_session", fail_named_child_upsert)

    res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)

    assert res.ok is False
    sessions = await manager._storage.list_sessions(limit=20)
    assert [session for session in sessions if session.parent_session_key == PARENT_KEY] == []


@pytest.mark.asyncio
async def test_fork_preserves_sandbox_workspace_run_context(dispatcher, ctx, manager, tmp_path):
    workspace = tmp_path / "project-gamma"
    workspace.mkdir()
    await manager.create(
        PARENT_KEY,
        agent_id="main",
        origin={
            RUN_CONTEXT_ORIGIN_KEY: {
                "workspace": str(workspace),
                "run_mode": "trusted",
            }
        },
    )

    res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)

    assert res.ok is True
    child = await manager.get_session(res.payload["key"])
    assert child is not None
    assert child.origin == {
        RUN_CONTEXT_ORIGIN_KEY: {
            "workspace": str(workspace),
            "run_mode": "trusted",
        }
    }
    list_res = await dispatcher.dispatch("r2", "sessions.list", None, ctx)
    child_row = _list_row(list_res, res.payload["key"])
    assert child_row["workspace"] == str(workspace)
    assert child_row["workspaceLabel"] == "project-gamma"


@pytest.mark.asyncio
async def test_fork_missing_parent_returns_not_found(dispatcher, ctx):
    res = await dispatcher.dispatch(
        "r1", "sessions.fork", {"key": "agent:main:webchat:missing0"}, ctx
    )
    assert res.ok is False
    assert res.error.code == "NOT_FOUND"


@pytest.mark.asyncio
async def test_fork_emits_sessions_changed(dispatcher, ctx, manager, monkeypatch):
    await _seed_parent(manager)
    emitted: list[tuple[str, str, dict[str, Any]]] = []

    async def _record_emit(_ctx, session_key, event_name, payload):
        emitted.append((session_key, event_name, payload))

    monkeypatch.setattr(rpc_sessions, "_emit_to_subscribers", _record_emit)

    res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert res.ok is True
    child_key = res.payload["key"]

    assert len(emitted) == 1
    session_key, event_name, payload = emitted[0]
    assert session_key == child_key
    assert event_name == "sessions.changed"
    assert payload["key"] == child_key
    assert payload["reason"] == "forked"


@pytest.mark.asyncio
async def test_delete_parent_leaves_forked_child_intact(dispatcher, ctx, manager):
    await _seed_parent(manager)

    fork_res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert fork_res.ok is True
    child_key = fork_res.payload["key"]

    delete_res = await dispatcher.dispatch("r2", "sessions.delete", {"key": PARENT_KEY}, ctx)
    assert delete_res.ok is True
    assert delete_res.payload["deleted"] == [PARENT_KEY]

    assert await manager.get_session(PARENT_KEY) is None
    child = await manager.get_session(child_key)
    assert child is not None
    assert child.parent_session_key == PARENT_KEY
    entries = await manager.get_transcript(child_key)
    assert [entry.content for entry in entries] == ["original question", "original answer"]

    list_res = await dispatcher.dispatch("r3", "sessions.list", None, ctx)
    keys = [row["key"] for row in list_res.payload["sessions"]]
    assert child_key in keys
    assert PARENT_KEY not in keys


@pytest.mark.asyncio
async def test_forked_turn_outcome_and_nested_fork_survive_parent_delete(
    dispatcher,
    ctx,
    manager,
):
    turn_id = await _seed_parent_with_terminal_turn(manager)
    fork_res = await dispatcher.dispatch(
        "r1",
        "sessions.forkThroughTurn",
        {"key": PARENT_KEY, "throughTurnId": turn_id},
        ctx,
    )
    assert fork_res.ok is True
    child_key = fork_res.payload["key"]

    delete_res = await dispatcher.dispatch(
        "r2",
        "sessions.delete",
        {"key": PARENT_KEY},
        ctx,
    )
    assert delete_res.ok is True
    assert await manager._storage.get_agent_task(turn_id) is None

    history_res = await dispatcher.dispatch(
        "r3",
        "chat.history",
        {"sessionKey": child_key},
        ctx,
    )
    assert history_res.ok is True
    assert history_res.payload["turn_outcomes"] == [
        {
            "turn_id": turn_id,
            "task_id": turn_id,
            "status": "succeeded",
            "started_at": None,
            "finished_at": None,
            "outcome": {"kind": "completed", "reason": "succeeded"},
        }
    ]
    assert all(
        "_opensquilla_fork_terminal_outcome_v1"
        not in message.get("turn_context", {})
        for message in history_res.payload["messages"]
    )

    nested_res = await dispatcher.dispatch(
        "r4",
        "sessions.forkThroughTurn",
        {"key": child_key, "throughTurnId": turn_id},
        ctx,
    )
    assert nested_res.ok is True
    assert nested_res.payload["parentKey"] == child_key
    assert [
        entry.content for entry in await manager.get_transcript(nested_res.payload["key"])
    ] == ["turn question", "tool request", "tool output", "turn answer"]


@pytest.mark.asyncio
async def test_forked_turn_accepted_routing_mode_survives_parent_delete(
    dispatcher,
    ctx,
    manager,
):
    turn_id = await _seed_parent_with_terminal_turn(
        manager,
        accepted_routing_mode="ensemble",
    )
    fork_res = await dispatcher.dispatch(
        "r1",
        "sessions.forkThroughTurn",
        {"key": PARENT_KEY, "throughTurnId": turn_id},
        ctx,
    )
    assert fork_res.ok is True
    child_key = fork_res.payload["key"]

    delete_res = await dispatcher.dispatch(
        "r2",
        "sessions.delete",
        {"key": PARENT_KEY},
        ctx,
    )
    assert delete_res.ok is True
    assert await manager._storage.get_agent_task(turn_id) is None

    history_res = await dispatcher.dispatch(
        "r3",
        "chat.history",
        {"sessionKey": child_key},
        ctx,
    )
    assert history_res.ok is True
    expected_outcomes = [
        {
            "turn_id": turn_id,
            "task_id": turn_id,
            "status": "succeeded",
            "started_at": None,
            "finished_at": None,
            "outcome": {"kind": "completed", "reason": "succeeded"},
            "accepted_routing_mode": "ensemble",
        }
    ]
    assert history_res.payload["turn_outcomes"] == expected_outcomes

    nested_res = await dispatcher.dispatch(
        "r4",
        "sessions.forkThroughTurn",
        {"key": child_key, "throughTurnId": turn_id},
        ctx,
    )
    assert nested_res.ok is True
    nested_history_res = await dispatcher.dispatch(
        "r5",
        "chat.history",
        {"sessionKey": nested_res.payload["key"]},
        ctx,
    )
    assert nested_history_res.ok is True
    assert nested_history_res.payload["turn_outcomes"] == expected_outcomes


@pytest.mark.asyncio
async def test_nested_fork_survives_deleted_intermediate_ancestor(
    dispatcher,
    ctx,
    manager,
):
    turn_id = await _seed_parent_with_terminal_turn(manager)
    first = await dispatcher.dispatch(
        "r1",
        "sessions.forkThroughTurn",
        {"key": PARENT_KEY, "throughTurnId": turn_id},
        ctx,
    )
    assert first.ok is True
    second = await dispatcher.dispatch(
        "r2",
        "sessions.forkThroughTurn",
        {"key": first.payload["key"], "throughTurnId": turn_id},
        ctx,
    )
    assert second.ok is True

    delete_res = await dispatcher.dispatch(
        "r3",
        "sessions.delete",
        {"key": first.payload["key"]},
        ctx,
    )
    assert delete_res.ok is True
    assert delete_res.payload["errors"] == []
    assert await manager.get_session(PARENT_KEY) is not None
    assert await manager._storage.get_agent_task(turn_id) is not None

    third = await dispatcher.dispatch(
        "r4",
        "sessions.forkThroughTurn",
        {"key": second.payload["key"], "throughTurnId": turn_id},
        ctx,
    )
    assert third.ok is True
    assert third.payload["parentKey"] == second.payload["key"]
