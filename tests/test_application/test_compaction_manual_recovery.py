"""Manual maintenance failure recovery without changing the client contract."""

from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from opensquilla.application.session_maintenance import (
    CompactSession,
    SessionCompactionDeadlineError,
    SessionCompactionMilestone,
    SessionCompactionPhaseTimeoutError,
    SessionCompactionTiming,
)
from opensquilla.engine.usage_accounting import (
    UsageAccountingBusyError,
    UsageAccountingUnavailableError,
)
from opensquilla.session.manager import _await_compaction_commit_barrier
from tests.test_application.test_session_maintenance import _application, _Ports


class _RecoveryPorts(_Ports):
    def __init__(self, *, blocked_phase: str = "summarizing") -> None:
        super().__init__()
        self.blocked_phase = blocked_phase
        self.entered = asyncio.Event()
        self.closed = False

    def timing(self):
        return SessionCompactionTiming(total_timeout_seconds=0.06, heartbeat_interval_seconds=10)

    def for_session(self, session_key):
        if self.blocked_phase == "admission":
            return self
        return super().for_session(session_key)

    async def acquire(self):
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("The blocked lock must not be acquired")

    def release(self):
        raise AssertionError("An unacquired lock must not be released")

    async def compact(self, command, plan):
        self.calls.append("executor.compact")
        self.entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.closed = True


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("phase", ["admission", "summarizing"])
async def test_manual_deadline_returns_one_skipped_result_and_event(background, phase):
    ports = _RecoveryPorts(blocked_phase=phase)
    started = time.monotonic()
    result = await asyncio.wait_for(_application(ports).compact(CompactSession(
        "agent:main:webchat:one", wait=not background,
    )), timeout=1)
    if background:
        assert result.status == "started"
        assert ports.background_task is not None
        result = await asyncio.wait_for(ports.background_task, timeout=1)

    assert time.monotonic() - started < 1
    assert result.status == "skipped"
    assert result.applied is False
    assert result.reason == "compaction_deadline_exceeded"
    assert [event.status for event in ports.events] == ["started", "skipped"]
    terminal = ports.events[-1]
    assert terminal.stage == phase
    assert terminal.result is not None and terminal.result.applied is False
    assert terminal.compaction_id == result.compaction_id
    assert not any(call.startswith("ownership.failed") for call in ports.calls)
    assert ports.closed is (phase == "summarizing")


@pytest.mark.parametrize("error", [
    TimeoutError("storage read timed out"),
    sqlite3.OperationalError("storage unavailable"),
    UsageAccountingUnavailableError("usage ledger unavailable"),
    UsageAccountingBusyError("usage ledger busy"),
])
async def test_storage_errors_are_not_reclassified_as_optional_deadlines(error):
    ports = _Ports()
    ports.execution_error = error
    with pytest.raises(type(error), match=str(error)):
        await _application(ports).compact(CompactSession("agent:main:webchat:one"))
    assert [event.status for event in ports.events if event.terminal] == ["failed"]
    assert ports.events[-1].reason == "compaction_failed"


async def test_commit_phase_timeout_does_not_claim_unapplied_recovery():
    ports = _Ports()
    ports.execution_error = SessionCompactionPhaseTimeoutError("committing")
    with pytest.raises(SessionCompactionDeadlineError) as error:
        await _application(ports).compact(CompactSession("agent:main:webchat:one"))
    assert error.value.phase == "committing"
    assert [event.status for event in ports.events if event.terminal] == ["failed"]


async def test_deadline_racing_failed_commit_does_not_hide_storage_cause():
    class FailedCommitPorts(_RecoveryPorts):
        async def compact(self, command, plan):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as cancellation:
                # The production commit barrier preserves a failed commit as
                # the cancellation cause when both race.
                raise cancellation from sqlite3.OperationalError("commit failed")

    ports = FailedCommitPorts()
    with pytest.raises(SessionCompactionDeadlineError):
        await _application(ports).compact(CompactSession("agent:main:webchat:one"))
    assert [event.status for event in ports.events if event.terminal] == ["failed"]


@pytest.mark.parametrize("background", [False, True])
async def test_commit_barrier_completion_wins_operation_deadline(background):
    class CommitPorts(_RecoveryPorts):
        async def compact(self, command, plan):
            async def commit():
                await asyncio.sleep(0.12)
                return self.execution

            result, cancellation_reconciled = await _await_compaction_commit_barrier(
                asyncio.create_task(commit()),
            )
            assert cancellation_reconciled
            return result

    ports = CommitPorts()
    result = await _application(ports).compact(CompactSession(
        "agent:main:webchat:one", wait=not background,
    ))
    if background:
        assert result.status == "started"
        result = await asyncio.wait_for(ports.background_task, timeout=1)
    assert result.status == "completed" and result.applied is True
    assert [event.status for event in ports.events if event.terminal] == ["completed"]


async def test_deadline_during_post_commit_publication_returns_committed_result():
    class PublicationPorts(_RecoveryPorts):
        async def compact(self, command, plan):
            return self.execution

        async def broadcast(self, event):
            if event.milestone is SessionCompactionMilestone.CHUNK_SUMMARIZED:
                await asyncio.Event().wait()

    ports = PublicationPorts()
    result = await _application(ports).compact(CompactSession("agent:main:webchat:one"))
    assert result.status == "completed" and result.applied is True
    assert result.reason == "deadline_after_commit"
    assert [event.status for event in ports.events if event.terminal] == ["completed"]


@pytest.mark.parametrize("background", [False, True])
async def test_user_cancel_remains_cancelled_not_skipped(background):
    ports = _RecoveryPorts()
    application = _application(ports)
    if background:
        await application.compact(CompactSession("agent:main:webchat:one", wait=False))
        task = ports.background_task
    else:
        task = asyncio.create_task(application.compact(CompactSession("agent:main:webchat:one")))
    await asyncio.wait_for(ports.entered.wait(), timeout=1)
    assert task is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert [event.status for event in ports.events if event.terminal] == ["cancelled"]
    assert ports.closed
