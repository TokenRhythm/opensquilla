"""A rejected or interrupted manual wait must not consume an unseen result."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture
def sessions(monkeypatch):
    values = [
        shell._BgSession(
            session_id=f"wait-{index}", command="synthetic",
            process=SimpleNamespace(returncode=None),
            notify_on_exit=True, session_key="agent:main:wait", task_id="wait-task",
        )
        for index in range(2)
    ]
    for session in values:
        monkeypatch.setitem(shell._bg_sessions, session.session_id, session)
    token = current_tool_context.set(ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI, session_key="agent:main:wait",
        task_id="wait-task",
    ))
    try:
        yield values
    finally:
        current_tool_context.reset(token)


@pytest.mark.parametrize("mode", ["invalid", "one"])
async def test_invalid_wait_does_not_consume_completion(sessions, mode):
    result = json.loads(await shell.process(
        "wait", execution_ids=[session.session_id for session in sessions], wait_mode=mode,
    ))
    assert result["status"] == "invalid_request"
    assert all(not session.completion_consumed for session in sessions)


@pytest.mark.parametrize("mode", ["one", "any", "all"])
@pytest.mark.parametrize("completes_during_wait", [False, True])
async def test_cancelled_wait_restores_unconsumed_notice(
    sessions, monkeypatch, mode, completes_during_wait,
):
    entered = asyncio.Event()
    notices = []

    async def emitter(event):
        notices.append(event)

    sessions[0].process_event_emitter = emitter

    async def wait_process(_session, _timeout):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(shell, "_wait_bg_process", wait_process)
    ids = [session.session_id for session in sessions[:1 if mode == "one" else 2]]
    waiting = asyncio.create_task(shell.process("wait", execution_ids=ids, wait_mode=mode))
    await asyncio.wait_for(entered.wait(), 2)
    if completes_during_wait:
        sessions[0].process.returncode = 0
        await shell._finalize_bg_session_async(sessions[0])
        assert notices[0]["completion_consumed"] is True
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert all(not session.completion_consumed for session in sessions)
    assert len(notices) == 2 * int(completes_during_wait)
    if notices:
        assert notices[-1]["completion_consumed"] is False


async def test_removed_execution_cannot_deliver_pending_completion(sessions):
    session = sessions[0]
    session.done = True
    session.process.returncode = session.returncode = 0
    await shell.process("remove", execution_id=session.session_id)
    assert shell.is_background_process_completion_consumed(
        session.session_id, session_key=session.session_key, task_id=session.task_id,
    )
