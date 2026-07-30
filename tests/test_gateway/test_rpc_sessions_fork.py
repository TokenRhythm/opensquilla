"""Tests for the sessions.fork RPC handler."""

from __future__ import annotations

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
from opensquilla.session.models import SessionStatus
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


def _list_row(list_res: Any, key: str) -> dict[str, Any]:
    rows = [row for row in list_res.payload["sessions"] if row["key"] == key]
    assert rows, f"session {key} missing from sessions.list"
    return rows[0]


def test_fork_requires_write_scope() -> None:
    assert METHOD_SCOPES["sessions.fork"] == WRITE_SCOPE
    assert METHOD_SCOPES["sessions.fork"] == METHOD_SCOPES["sessions.create"]


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
async def test_fork_without_title_copies_parent_title_verbatim(dispatcher, ctx, manager):
    await _seed_parent(manager, display_name="Budget planning")

    res = await dispatcher.dispatch("r1", "sessions.fork", {"key": PARENT_KEY}, ctx)
    assert res.ok is True
    child = await manager.get_session(res.payload["key"])
    assert child.display_name == "Budget planning"


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
