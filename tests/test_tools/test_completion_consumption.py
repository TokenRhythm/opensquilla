"""A direct result and its optional completion notice must share consumption."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from opensquilla.tools.builtin import shell
from opensquilla.tools.types import CallerKind, ToolContext, current_tool_context


@pytest.fixture
async def execution(monkeypatch):
    release = asyncio.Event()
    exited = asyncio.Event()
    events = []

    async def wait():
        await exited.wait()
        return 0

    async def emit(event):
        events.append(event)

    session = shell._BgSession(
        session_id="observed-execution",
        command="synthetic",
        process=SimpleNamespace(returncode=None, stdin=None, wait=wait),
        notify_on_exit=True,
        session_key="agent:main:completion-consumption",
        task_id="completion-owner",
        process_event_emitter=emit,
    )
    monkeypatch.setattr(shell, "_bg_sessions", {session.session_id: session})
    token = current_tool_context.set(ToolContext(
        is_owner=True, caller_kind=CallerKind.CLI,
        session_key=session.session_key, task_id=session.task_id,
    ))

    async def collect():
        await release.wait()
        session.process.returncode = 0
        session.output_capture.feed(b"FINAL-RESULT")
        exited.set()
        await shell._finalize_bg_session_async(session)

    async def start(**_kwargs):
        session.collector_task = asyncio.create_task(collect())
        return shell._background_process_result(session)

    monkeypatch.setattr(shell, "background_process", start)
    try:
        yield session, release, events
    finally:
        release.set()
        if session.collector_task is not None:
            await asyncio.wait_for(session.collector_task, 2)
        current_tool_context.reset(token)


async def observe(yield_time_ms):
    return json.loads(await shell._start_exec_command_session(
        "synthetic", workdir=None, timeout=10, env=None, stdin=None,
        io_mode="closed", yield_time_ms=yield_time_ms,
        sandbox_permissions="use_default", justification="",
        prefix_rule=None, approval_id=None, notify_on_exit=True,
    ))


async def test_initial_observation_delivers_final_result_without_extra_notice(execution):
    session, release, events = execution
    release.set()

    result = await observe(1000)

    assert result["exited"] is True
    assert result["session"]["returncode"] == 0
    assert result["output"] == "FINAL-RESULT"
    assert events
    assert all(event["completion_consumed"] for event in events)
    assert session.completion_consumed


@pytest.mark.parametrize("yield_time_ms", [0, 1])
async def test_yielded_running_execution_keeps_later_completion_notice(
    execution, yield_time_ms,
):
    session, release, events = execution

    result = await observe(yield_time_ms)

    assert result["session"]["status"] == "running"
    assert not result["session"]["completion_consumed"]
    assert not session.completion_consumed
    assert not events
    release.set()
    await session.collector_task
    assert len(events) == 1
    assert events[0]["completion_consumed"] is False
    assert events[0]["returncode"] == 0
    assert events[0]["output_tail"] == "FINAL-RESULT"


async def test_cancelled_initial_observation_returns_unseen_completion(
    execution, monkeypatch,
):
    session, release, events = execution

    async def interrupted_observation(_session, _timeout):
        release.set()
        await session.collector_task
        raise asyncio.CancelledError

    monkeypatch.setattr(shell, "_wait_bg_process", interrupted_observation)

    with pytest.raises(asyncio.CancelledError):
        await observe(1000)

    assert not session.completion_consumed
    assert [event["completion_consumed"] for event in events] == [True, False]
    assert events[-1]["output_tail"] == "FINAL-RESULT"


@pytest.mark.parametrize("consumed_before_read", [False, True])
async def test_cancelled_log_preserves_notification_eligibility(
    execution, monkeypatch, consumed_before_read,
):
    session, _release, events = execution
    session.done = True
    session.process.returncode = session.returncode = 0
    session.completion_consumed = consumed_before_read
    session.output_capture.spool = SimpleNamespace()
    entered = asyncio.Event()
    finished = asyncio.Event()
    release_read = threading.Event()
    loop = asyncio.get_running_loop()

    def read_slice(_start, _end):
        loop.call_soon_threadsafe(entered.set)
        try:
            if not release_read.wait(2):
                raise TimeoutError("test did not release the log reader")
            return "FINAL-RESULT", 12
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(session.output_capture, "read_slice", read_slice)
    reading = asyncio.create_task(shell.process("log", execution_id=session.session_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading

        assert session.completion_consumed is consumed_before_read
        if consumed_before_read:
            assert not events
        else:
            assert len(events) == 1
            assert events[0]["completion_consumed"] is False
    finally:
        release_read.set()
        await asyncio.wait_for(finished.wait(), 2)
        if not reading.done():
            reading.cancel()
            await asyncio.gather(reading, return_exceptions=True)


async def test_consumed_completion_still_allows_repeated_log_reads(execution):
    session, release, _events = execution
    release.set()
    await observe(1000)

    first = json.loads(await shell.process("log", execution_id=session.session_id))
    second = json.loads(await shell.process("log", execution_id=session.session_id))

    assert first["output"] == second["output"] == "FINAL-RESULT"
    assert first["session"]["status"] == second["session"]["status"] == "done"
    assert session.completion_consumed
