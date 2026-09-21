"""Session process RPCs use the production managed-command registry, without a model."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from opensquilla.gateway.auth import Principal
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.sandbox.config import SandboxSettings
from opensquilla.sandbox.integration import configure_runtime, reset_runtime
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionIntent
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext, ToolError, current_tool_context


def python_command(source: str) -> str:
    if os.name == "nt":
        def quote(value: str) -> str:
            return "'" + value.replace("'", "''") + "'"
        return "& " + " ".join(quote(part) for part in [sys.executable, "-u", "-c", source])
    return shlex.join([sys.executable, "-u", "-c", source])


@pytest.fixture
async def process_env(tmp_path):
    storage = SessionStorage(":memory:")
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False)
    session = await manager.create("agent:main:webchat:process-rpc")
    configure_runtime(SandboxSettings(run_mode="full", backend="noop"), workspace=tmp_path)
    token = current_tool_context.set(ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, run_mode="full",
        workspace_dir=str(tmp_path), session_key=session.session_key,
        artifact_session_id=session.session_id, session_epoch=session.epoch,
        task_id="task-created-process",
    ))
    previous_ids = set(shell._bg_sessions)
    try:
        ctx = RpcContext(conn_id="process-test", session_manager=manager)
        yield manager, session, ctx, tmp_path
    finally:
        for execution_id in set(shell._bg_sessions) - previous_ids:
            await shell.process("kill", execution_id=execution_id)
            await shell.process("remove", execution_id=execution_id)
        current_tool_context.reset(token)
        reset_runtime()
        await storage.close()


async def call(ctx, key, action, **params):
    return await get_dispatcher().dispatch(
        "process-test", "sessions.processes." + action, {"sessionKey": key, **params}, ctx,
    )


async def start(env, source, *, timeout=30):
    result = json.loads(await shell.exec_command(
        python_command(source), workdir=str(env[3]), yield_time_ms=0, io_mode="pipe",
        timeout=timeout,
    ))
    assert result["status"] == "ok", result
    return result["execution_id"]


async def wait_output(ctx, key, execution_id, marker):
    async with asyncio.timeout(15):
        while True:
            response = await call(ctx, key, "log", executionId=execution_id)
            assert response.error is None, response.error
            if marker in response.payload["output"]:
                return response.payload
            await asyncio.sleep(0.03)


async def test_managed_process_survives_turn_context_and_reconnect_then_stops(process_env):
    _, session, ctx, _ = process_env
    execution_id = await start(
        process_env, "import time; print('READY', flush=True); time.sleep(30)",
    )
    await wait_output(ctx, session.session_key, execution_id, "READY")
    # A later turn and a replacement connection still see the original process.
    token = current_tool_context.set(replace(current_tool_context.get(), task_id="later-task"))
    try:
        listed = await call(replace(ctx, conn_id="reconnected"), session.session_key, "list")
        assert listed.error is None
        assert listed.payload["session_id"] == session.session_id
        [process] = listed.payload["processes"]
        assert process["execution_id"] == execution_id
        assert process["task_id"] == "task-created-process"
        assert process["status"] == "running"
        assert process["returncode"] is None
        assert process["ended_at"] is None
        stopped = await call(ctx, session.session_key, "stop", executionId=execution_id)
        assert stopped.error is None
        assert stopped.payload["process"]["status"] == "killed"
        assert stopped.payload["process"]["ended_at"] is not None
        assert not shell._bg_sessions[execution_id].process_tree.is_active()
        again = await call(ctx, session.session_key, "stop", executionId=execution_id)
        assert again.payload["process"] == stopped.payload["process"]
    finally:
        current_tool_context.reset(token)


async def test_completion_log_and_read_rpc_do_not_consume_notifications(process_env):
    _, session, ctx, _ = process_env
    execution_id = await start(process_env, "print('界' * 15000, flush=True)")
    process = shell._bg_sessions[execution_id]
    async with asyncio.timeout(15):
        await asyncio.shield(process.collector_task)
        # Output collection can finish before the POSIX anchor confirms that
        # the process group is empty. Wait for the public lifecycle boundary.
        while True:
            page = await call(ctx, session.session_key, "log", executionId=execution_id, limit=40)
            assert page.error is None
            if page.payload["status"] != "running":
                break
            await asyncio.sleep(0.03)
    assert page.payload["status"] == "done"
    assert len(page.payload["output"]) == 40
    assert page.payload["truncated"] is True
    assert "界" in page.payload["output"]
    assert not process.completion_consumed
    listed = await call(ctx, session.session_key, "list")
    assert listed.payload["processes"][0]["returncode"] == 0
    stopped = await call(ctx, session.session_key, "stop", executionId=execution_id)
    assert stopped.payload["process"]["status"] == "done"


async def test_cross_session_reset_and_deleted_generation_cannot_access_process(process_env):
    manager, session, ctx, _ = process_env
    execution_id = await start(process_env, "import time; time.sleep(30)")
    other = await manager.create("agent:main:webchat:another-process-session")
    for action in ("log", "stop"):
        denied = await call(ctx, other.session_key, action, executionId=execution_id)
        assert denied.error.code == "NOT_FOUND"
    assert (await call(ctx, other.session_key, "list")).payload["processes"] == []
    reset, _ = await manager.apply_intent(session.session_key, SessionIntent.RESET_SAME_KEY)
    assert reset.session_id != session.session_id
    assert (await call(ctx, session.session_key, "list")).payload["processes"] == []
    for action in ("log", "stop"):
        denied = await call(ctx, session.session_key, action, executionId=execution_id)
        assert denied.error.code == "NOT_FOUND"
    assert shell._bg_sessions[execution_id].process_tree.is_active()
    await manager.storage.delete_session(session.session_key)
    assert (await call(ctx, session.session_key, "list")).error.code == "NOT_FOUND"


async def test_process_rpc_scopes_guest_policy_and_discovery(process_env):
    _, session, ctx, _ = process_env
    read_only = replace(ctx, principal=Principal(
        role="operator", scopes=frozenset({"operator.read"}), is_owner=True, authenticated=True,
    ))
    assert (await call(read_only, session.session_key, "list")).error is None
    denied = await call(read_only, session.session_key, "stop", executionId="missing")
    assert denied.error.code == "UNAUTHORIZED"
    guest = replace(ctx, principal=Principal(
        role="operator", scopes=frozenset({"operator.read", "operator.write"}),
        is_owner=False, authenticated=False, auth_state="guest",
        capabilities=frozenset({"guest.safe"}), guest_owner_id="a" * 64,
    ))
    for action in ("list", "log", "stop"):
        params = {} if action == "list" else {"executionId": "missing"}
        denied = await call(guest, session.session_key, action, **params)
        assert denied.error.code == "UNAUTHORIZED"
        entry = get_dispatcher().get_entry("sessions.processes." + action)
        assert entry is not None
        assert entry.required_scope == ("operator.write" if action == "stop" else "operator.read")


@pytest.mark.parametrize("invalid", [
    {"limit": 0}, {"limit": 12001}, {"limit": True}, {"limit": None},
    {"executionId": ""}, {"command": "arbitrary execution"},
])
async def test_process_log_rejects_invalid_or_unbounded_requests(process_env, invalid):
    _, session, ctx, _ = process_env
    response = await call(ctx, session.session_key, "log", **{"executionId": "missing", **invalid})
    assert response.error.code == "INVALID_REQUEST"


async def test_process_timeout_remains_distinct_from_exit_or_manual_stop(process_env):
    _, session, ctx, _ = process_env
    execution_id = await start(process_env, "import time; time.sleep(30)", timeout=0.2)
    await asyncio.wait_for(asyncio.shield(shell._bg_sessions[execution_id].collector_task), 15)
    process = (await call(ctx, session.session_key, "list")).payload["processes"][0]
    assert process["status"] == "timed_out"
    assert process["ended_at"] is not None


async def test_log_preview_rechecks_owner_after_await(process_env, monkeypatch):
    manager, session, ctx, _ = process_env
    execution_id = await start(process_env, "import time; time.sleep(30)")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_preview(_capture):
        entered.set()
        await release.wait()
        return "old generation output"

    monkeypatch.setattr(shell.BoundedOutputCapture, "preview_async", delayed_preview)
    reading = asyncio.create_task(call(ctx, session.session_key, "log", executionId=execution_id))
    await asyncio.wait_for(entered.wait(), 5)
    await manager.apply_intent(session.session_key, SessionIntent.RESET_SAME_KEY)
    release.set()
    response = await reading
    assert response.error.code == "NOT_FOUND"
    assert response.payload is None


async def test_failed_pty_stop_stays_running_and_available_to_stop(monkeypatch):
    process = shell._BgSession(
        session_id="pty-still-live", command="managed command",
        process=SimpleNamespace(returncode=None), pty_handle=SimpleNamespace(returncode=None),
    )

    async def termination_failed(_session):
        return False

    monkeypatch.setattr(shell, "_terminate_bg_session", termination_failed)
    with pytest.raises(ToolError, match="did not stop"):
        await shell._stop_bg_session(process)
    assert process.killed is True
    assert shell._session_process_snapshot(process)["status"] == "running"
    assert shell._session_process_snapshot(process)["ended_at"] is None


def test_snapshot_bounds_finished_history_without_hiding_live_processes(monkeypatch):
    for index in range(56):
        execution_id = f"bounded-{index}"
        process = shell._BgSession(
            session_id=execution_id, command="managed", started_at=float(index),
            process=SimpleNamespace(returncode=0 if index else None),
            done=bool(index), session_key="bounded", owner_session_id="owner",
            owner_session_epoch=0,
        )
        monkeypatch.setitem(shell._bg_sessions, execution_id, process)
    listed = shell.list_session_processes(
        session_key="bounded", session_id="owner", session_epoch=0,
    )
    assert len(listed) == 51
    assert listed[0]["execution_id"] == "bounded-0"
    assert listed[1]["execution_id"] == "bounded-55"
    assert listed[-1]["execution_id"] == "bounded-6"
