from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from opensquilla.engine.tool_result_store import ToolResultStore
from opensquilla.engine.types import ToolResultEvent
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.channel_dispatch import _tool_result_payload
from opensquilla.gateway.guest_rpc_policy import guest_owned_session_key
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage


@pytest.fixture
async def log_rpc_env(tmp_path: Path):
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    config = SimpleNamespace(
        attachments=SimpleNamespace(media_root=str(tmp_path / "media")),
        state_dir=None,
        config_path=None,
    )
    ctx = RpcContext(conn_id="test", session_manager=manager, config=config)
    try:
        yield manager, ToolResultStore(tmp_path / "media" / "tool-results"), ctx
    finally:
        await storage.close()


def write_log(store, session, content: str, *, complete=True):
    spool = store.open_output_spool(
        tool_name="exec", session_id=session.session_id,
        session_key=session.session_key, agent_id="main",
    )
    spool.append(content.encode())
    return spool.finish(complete=complete)


async def read(ctx, key, log_handle, **params):
    return await get_dispatcher().dispatch(
        "read-log", "sessions.executionLog.read",
        {"sessionKey": key, "handle": log_handle, **params}, ctx,
    )


async def test_log_rpc_reads_middle_beyond_snapshot_cap_and_pages_unicode(log_rpc_env):
    manager, store, ctx = log_rpc_env
    session = await manager.create("agent:main:webchat:log-page")
    prefix = "x" * (9 * 1024 * 1024)
    content = prefix + "\nMIDDLE: 故障🙂\n" + "z" * 14000
    handle = write_log(store, session, content)
    page = await read(ctx, session.session_key, handle, offset=len(prefix), limit=12000)
    assert page.error is None
    assert page.payload["content"].startswith("\nMIDDLE: 故障🙂\n")
    assert len(page.payload["content"]) == 12000
    assert page.payload["chars"] == len(content)
    assert page.payload["complete"] is True
    second = await read(ctx, session.session_key, handle, offset=page.payload["next_offset"])
    assert second.error is None
    assert page.payload["content"] + second.payload["content"] == content[len(prefix):]
    assert second.payload["next_offset"] is None


async def test_log_rpc_rejects_cross_session_and_snapshot_handles(log_rpc_env):
    manager, store, ctx = log_rpc_env
    session = await manager.create("agent:main:webchat:log-owner")
    other = await manager.create("agent:main:webchat:other")
    handle = write_log(store, session, "private log")
    denied = await read(ctx, other.session_key, handle)
    assert denied.error is not None
    assert denied.error.code == "NOT_FOUND"
    snapshot = store.write(
        "snapshot", tool_name="exec", tool_use_id="call", session_id=session.session_id,
        session_key=session.session_key, agent_id="main",
    )
    denied_snapshot = await read(ctx, session.session_key, snapshot.handle)
    assert denied_snapshot.error is not None
    assert denied_snapshot.error.code == "NOT_FOUND"


async def test_log_rpc_guest_is_limited_to_own_session(log_rpc_env):
    manager, store, ctx = log_rpc_env
    owner_id = "a" * 64
    own = await manager.create(guest_owned_session_key(owner_id, "logs"))
    other = await manager.create(guest_owned_session_key("b" * 64, "logs"))
    own_handle = write_log(store, own, "own")
    other_handle = write_log(store, other, "other")
    guest = replace(ctx, principal=Principal(
        role="operator", scopes=frozenset({"operator.read"}), is_owner=False,
        authenticated=False, capabilities=frozenset({"guest.safe"}),
        auth_state="guest", guest_owner_id=owner_id,
    ))
    assert (await read(guest, own.session_key, own_handle)).error is None
    denied = await read(guest, other.session_key, other_handle)
    assert denied.error is not None
    assert denied.error.code == "UNAUTHORIZED"
    substituted = await read(guest, own.session_key, other_handle)
    assert substituted.error is not None
    assert substituted.error.code == "NOT_FOUND"


@pytest.mark.parametrize("params", [
    {"limit": 12001}, {"limit": 0}, {"offset": -1}, {"offset": True},
    {"offset": 0.5}, {"handle": "../private"}, {"unknown": "ignored?"},
    {"offset": None}, {"limit": None},
])
async def test_log_rpc_rejects_unbounded_or_invalid_page(log_rpc_env, params):
    manager, store, ctx = log_rpc_env
    session = await manager.create("agent:main:webchat:log-bounds")
    handle = write_log(store, session, "output")
    response = await read(ctx, session.session_key, handle, **params)
    assert response.error is not None
    assert response.error.code == "INVALID_REQUEST"


async def test_log_rpc_preserves_incomplete_capture_status(log_rpc_env):
    manager, store, ctx = log_rpc_env
    session = await manager.create("agent:main:webchat:partial")
    handle = write_log(store, session, "before cancellation", complete=False)
    page = await read(ctx, session.session_key, handle)
    assert page.error is None
    assert page.payload["content"] == "before cancellation"
    assert page.payload["complete"] is False


def test_tool_result_event_exposes_only_structured_execution_log_handle():
    event = ToolResultEvent(tool_use_id="call", tool_name="exec", result="preview")
    assert "execution_log_handle" not in _tool_result_payload(event)
    event.execution_log_handle = "tr-" + "a" * 32
    assert _tool_result_payload(event)["execution_log_handle"] == event.execution_log_handle


async def test_log_rpc_pending_writer_is_retryable_then_becomes_readable(log_rpc_env):
    manager, store, ctx = log_rpc_env
    session = await manager.create("agent:main:webchat:pending-log")
    other = await manager.create("agent:main:webchat:other-pending-log")
    spool = store.open_output_spool(
        tool_name="exec", session_id=session.session_id,
        session_key=session.session_key, agent_id="main",
    )
    try:
        spool.append(b"still running\n")
        pending = await read(ctx, session.session_key, spool.handle)
        assert pending.error is not None
        assert pending.error.code == "NOT_READY"
        assert pending.error.retryable is True
        assert pending.payload is None
        # Pending status cannot reveal a log belonging to a different session.
        denied = await read(ctx, other.session_key, spool.handle)
        assert denied.error.code == "NOT_FOUND"
        handle = spool.finish()
        ready = await read(ctx, session.session_key, handle)
        assert ready.error is None
        assert ready.payload["content"] == "still running\n"
        assert ready.payload["complete"] is True
    finally:
        spool.close()
