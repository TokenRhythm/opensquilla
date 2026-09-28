"""Managed process notices belong to their originating active turn only."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.session.models import AgentTaskRecord, AgentTaskStatus
from opensquilla.tools.builtin import shell

SESSION_KEY = "agent:main:webchat:process-completion"
Emitter = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class _Storage:
    records: dict[str, AgentTaskRecord] = field(default_factory=dict)
    owner: Any = field(default_factory=lambda: SimpleNamespace(session_id="owner", epoch=0))
    owner_read_failures: int = 0
    owner_read_attempts: int = 0
    failure_callback: Callable[[], None] | None = None

    async def get_session(self, _session_key: str) -> Any:
        self.owner_read_attempts += 1
        if self.owner_read_failures:
            self.owner_read_failures -= 1
            if self.failure_callback is not None:
                self.failure_callback()
            raise OSError("synthetic transient owner read failure")
        return self.owner

    async def create_agent_task(
        self,
        record: AgentTaskRecord,
        *,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
    ) -> None:
        assert self.owner is not None
        assert expected_session_id == self.owner.session_id
        assert expected_session_epoch == self.owner.epoch
        self.records[record.task_id] = record

    async def get_agent_task(self, task_id: str) -> AgentTaskRecord | None:
        return self.records.get(task_id)

    async def list_agent_tasks(self, **_: Any) -> list[AgentTaskRecord]:
        return list(self.records.values())

    async def update_agent_task(self, task_id: str, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self.records[task_id], name, value)


class _Harness:
    def __init__(self) -> None:
        self.storage = _Storage()
        self.emitters: dict[str, Emitter] = {}
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.messages: list[str] = []
        self.runtime = TaskRuntime(storage=self.storage, turn_handler=self.handle)
        self.process: Any = None

    async def handle(self, run: Any) -> None:
        self.messages.append(run.message)
        self.emitters[run.message] = run.envelope.runtime_services["process_event_emitter"]
        self.started.set()
        if run.message == "busy":
            await self.finish.wait()

    async def enqueue(self, message: str) -> Any:
        self.started.clear()
        owner = self.storage.owner
        handle = await self.runtime.enqueue(
            RouteEnvelope(
                source_kind=SourceKind.WEB,
                source_name="completion-test",
                agent_id="main",
                session_key=SESSION_KEY,
                session_id=owner.session_id,
                session_epoch=owner.epoch,
            ),
            message,
        )
        await asyncio.wait_for(self.started.wait(), 2)
        if self.process is None:
            self.process = shell._BgSession(
                session_id="process-one", command="synthetic",
                process=SimpleNamespace(returncode=0), done=True,
                session_key=SESSION_KEY, task_id=handle.task_id, completion_consumed=False,
                owner_session_id=owner.session_id, owner_session_epoch=owner.epoch,
            )
            shell._bg_sessions["process-one"] = self.process
        if message != "busy":
            await self.runtime.wait(handle.task_id, timeout=2)
        return handle

    def completions(self) -> list[AgentTaskRecord]:
        return [row for row in self.storage.records.values() if row.run_kind == "runtime_send"]


@pytest_asyncio.fixture
async def harness(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Harness]:
    harness = _Harness()
    monkeypatch.setattr(shell, "_bg_sessions", {})
    try:
        yield harness
    finally:
        harness.finish.set()
        await harness.runtime.shutdown()


def _event(**overrides: Any) -> dict[str, Any]:
    return {
        "execution_id": "process-one",
        "session_id": "process-one",
        "notify_on_exit": True,
        "completion_consumed": False,
        "status": "done",
        "returncode": 0,
        "output_tail": "synthetic output",
        **overrides,
    }


@pytest.mark.parametrize("intervening_turn", [False, True])
@pytest.mark.parametrize("status,returncode", [("done", 0), ("killed", -15)])
async def test_idle_completion_never_starts_another_turn(
    harness: _Harness, intervening_turn: bool, status: str, returncode: int,
) -> None:
    handle = await harness.enqueue("launch")
    if intervening_turn:
        await harness.enqueue("write documentation")
    assert SESSION_KEY not in harness.runtime._last_envelope_by_session
    assert handle.task_id not in harness.runtime._tasks
    original_tasks = set(harness.storage.records)
    original_messages = list(harness.messages)

    await harness.emitters["launch"](_event(status=status, returncode=returncode))

    assert set(harness.storage.records) == original_tasks
    assert harness.messages == original_messages
    assert not harness.completions()


async def test_old_completion_cannot_steer_later_turn_in_same_generation(
    harness: _Harness,
) -> None:
    await harness.enqueue("launch")
    handle = await harness.enqueue("busy")

    await harness.emitters["launch"](_event())

    assert harness.runtime._tasks[handle.task_id].pending_input_provider.peek_pending() == []
    assert len(harness.storage.records) == 2
    assert not harness.completions()


@pytest.mark.parametrize("replacement", [None, ("replacement", 0), ("owner", 1)])
async def test_stale_completion_cannot_steer_replacement_owner(
    harness: _Harness, replacement: tuple[str, int] | None,
) -> None:
    handle = await harness.enqueue("busy")
    harness.storage.owner = (
        None if replacement is None
        else SimpleNamespace(session_id=replacement[0], epoch=replacement[1])
    )

    await harness.emitters["busy"](_event())

    running = harness.runtime._tasks[handle.task_id]
    assert running.pending_input_provider.peek_pending() == []
    assert not harness.completions()


async def test_originating_active_turn_receives_notice_once(harness: _Harness) -> None:
    handle = await harness.enqueue("busy")
    await asyncio.gather(*(harness.emitters["busy"](_event()) for _ in range(3)))
    running = harness.runtime._tasks[handle.task_id]
    notices = running.pending_input_provider.peek_pending()
    assert len(notices) == 1
    assert "execution_id=process-one status=done returncode=0" in notices[0]
    assert "synthetic output" in notices[0]
    assert len(harness.storage.records) == 1
    assert not harness.completions()


@pytest.mark.parametrize("overrides", [{"notify_on_exit": False}, {"completion_consumed": True}])
async def test_unrequested_or_manually_consumed_notice_does_not_wake(
    harness: _Harness, overrides: dict[str, Any],
) -> None:
    handle = await harness.enqueue("busy")
    await harness.emitters["busy"](_event(**overrides))
    assert harness.runtime._tasks[handle.task_id].pending_input_provider.peek_pending() == []
    assert not harness.completions()


async def test_transient_owner_read_failure_retries_in_production_callback(
    harness: _Harness,
) -> None:
    handle = await harness.enqueue("busy")
    harness.storage.owner_read_attempts = 0
    harness.storage.owner_read_failures = 1

    await harness.emitters["busy"](_event())
    assert harness.storage.owner_read_attempts == 2
    running = harness.runtime._tasks[handle.task_id]
    assert len(running.pending_input_provider.peek_pending()) == 1
    await harness.emitters["busy"](_event())
    assert harness.storage.owner_read_attempts == 2
    assert not harness.completions()


async def test_concurrent_active_notices_share_successful_retry(harness: _Harness) -> None:
    handle = await harness.enqueue("busy")
    harness.storage.owner_read_attempts = 0
    harness.storage.owner_read_failures = 1
    await asyncio.gather(*(harness.emitters["busy"](_event()) for _ in range(3)))
    assert harness.storage.owner_read_attempts == 2
    assert len(harness.runtime._tasks[handle.task_id].pending_input_provider.peek_pending()) == 1
    assert not harness.completions()


async def test_exhausted_retry_does_not_poison_later_delivery(harness: _Harness) -> None:
    handle = await harness.enqueue("busy")
    harness.storage.owner_read_attempts = 0
    harness.storage.owner_read_failures = 3
    await harness.emitters["busy"](_event())
    assert harness.storage.owner_read_attempts == 3
    running = harness.runtime._tasks[handle.task_id]
    assert running.pending_input_provider.peek_pending() == []
    assert not harness.completions()
    await harness.emitters["busy"](_event())
    assert harness.storage.owner_read_attempts == 4
    assert len(running.pending_input_provider.peek_pending()) == 1


async def test_completion_rechecks_owner_after_waiting_for_admission(harness: _Harness) -> None:
    handle = await harness.enqueue("busy")
    async with harness.runtime.collect_admission(SESSION_KEY):
        delivery = asyncio.create_task(harness.emitters["busy"](_event()))
        await asyncio.sleep(0)
        harness.storage.owner = SimpleNamespace(session_id="replacement", epoch=1)
    await delivery
    assert harness.runtime._tasks[handle.task_id].pending_input_provider.peek_pending() == []
    assert not harness.completions()


@pytest.mark.parametrize("message", ["launch", "busy"])
@pytest.mark.parametrize("notify", [False, True])
async def test_process_projection_still_emits_with_session_owner(
    harness: _Harness, message: str, notify: bool,
) -> None:
    events: list[dict[str, Any]] = []

    async def observer(_key: str, name: str, payload: dict[str, Any]) -> None:
        if name == "session.event.process_completed":
            events.append(payload)

    harness.runtime._event_emitter = observer
    await harness.enqueue(message)
    await harness.emitters[message](_event(notify_on_exit=notify))
    assert len(events) == 1
    assert events[0]["session_id"] == "owner"
    assert events[0]["epoch"] == events[0]["session_epoch"] == 0
    assert events[0]["execution_id"] == "process-one"
    assert not harness.completions()


@pytest.mark.parametrize("during_owner_read", [False, True])
async def test_manual_consumption_is_rechecked_before_delivery(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, during_owner_read: bool,
) -> None:
    handle = await harness.enqueue("busy")
    process = harness.process
    harness.storage.owner_read_attempts = 0

    def consume() -> None:
        process.completion_consumed = True

    if during_owner_read:
        async def get_session(_key: str) -> Any:
            consume()
            return harness.storage.owner
        monkeypatch.setattr(harness.storage, "get_session", get_session)
    else:
        harness.storage.owner_read_failures = 1
        harness.storage.failure_callback = consume

    await harness.emitters["busy"](_event())

    assert harness.storage.owner_read_attempts == (0 if during_owner_read else 1)
    assert harness.runtime._tasks[handle.task_id].pending_input_provider.peek_pending() == []
    assert not harness.completions()


async def test_retry_does_not_cross_session_replacement(harness: _Harness) -> None:
    handle = await harness.enqueue("busy")
    harness.storage.owner_read_attempts = 0

    def replace_owner() -> None:
        harness.storage.owner = SimpleNamespace(session_id="replacement", epoch=1)

    harness.storage.owner_read_failures = 1
    harness.storage.failure_callback = replace_owner
    await harness.emitters["busy"](_event())
    assert harness.storage.owner_read_attempts == 2
    assert harness.runtime._tasks[handle.task_id].pending_input_provider.peek_pending() == []
    assert not harness.completions()


async def test_retry_does_not_revive_removed_execution(harness: _Harness) -> None:
    handle = await harness.enqueue("busy")
    harness.storage.owner_read_attempts = 0

    def remove_process() -> None:
        shell._bg_sessions.pop("process-one")

    harness.storage.owner_read_failures = 1
    harness.storage.failure_callback = remove_process
    await harness.emitters["busy"](_event())
    assert harness.storage.owner_read_attempts == 1
    assert harness.runtime._tasks[handle.task_id].pending_input_provider.peek_pending() == []
    assert not harness.completions()


async def test_observer_failure_does_not_suppress_completion(harness: _Harness) -> None:
    async def observer(_key: str, name: str, _payload: dict[str, Any]) -> None:
        if name == "session.event.process_completed":
            raise OSError("synthetic observer failure")

    handle = await harness.enqueue("busy")
    harness.runtime._event_emitter = observer
    await harness.emitters["busy"](_event())
    await harness.emitters["busy"](_event())
    assert len(harness.runtime._tasks[handle.task_id].pending_input_provider.peek_pending()) == 1
    assert not harness.completions()


async def test_terminal_closing_neither_injects_nor_promotes_notice(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_entered = asyncio.Event()
    finish_terminal = asyncio.Event()
    update = harness.storage.update_agent_task
    handle = await harness.enqueue("busy")
    running = harness.runtime._tasks[handle.task_id]

    async def paused_update(task_id: str, **fields: Any) -> None:
        if task_id == handle.task_id and fields.get("status") is AgentTaskStatus.SUCCEEDED:
            terminal_entered.set()
            await finish_terminal.wait()
        await update(task_id, **fields)

    monkeypatch.setattr(harness.storage, "update_agent_task", paused_update)
    harness.finish.set()
    try:
        await asyncio.wait_for(terminal_entered.wait(), 2)
        assert running.terminal_closing
        await asyncio.wait_for(harness.emitters["busy"](_event()), 2)
        assert running.pending_input_provider.peek_pending() == []
        assert not harness.completions()
    finally:
        finish_terminal.set()
    await harness.runtime.wait(handle.task_id, timeout=2)
    assert harness.messages == ["busy"]
    assert len(harness.storage.records) == 1


async def test_shutdown_prevents_completion_retry(harness: _Harness) -> None:
    await harness.enqueue("busy")
    await harness.runtime.shutdown()
    harness.storage.owner_read_attempts = 0
    await harness.emitters["busy"](_event())
    assert harness.storage.owner_read_attempts == 0
    assert not harness.completions()


@pytest.mark.parametrize("claim", [False, True])
async def test_unapplied_notice_expires_with_originating_turn(
    harness: _Harness, claim: bool,
) -> None:
    handle = await harness.enqueue("busy")
    provider = harness.runtime._tasks[handle.task_id].pending_input_provider
    await harness.emitters["busy"](_event())
    assert len(provider.peek_pending()) == 1
    if claim:
        claimed = await provider.claim_pending()
        assert len(claimed.texts) == 1
        assert "[Managed process completed]" in claimed.texts[0]
        assert provider.peek_pending() == []

    harness.finish.set()
    await harness.runtime.wait(handle.task_id, timeout=2)

    assert harness.messages == ["busy"]
    assert len(harness.storage.records) == 1
    assert not harness.completions()


@pytest.mark.parametrize("claim", [False, True])
@pytest.mark.parametrize("notice_first", [False, True])
async def test_mixed_unapplied_inputs_promote_only_user_steer(
    harness: _Harness, claim: bool, notice_first: bool,
) -> None:
    handle = await harness.enqueue("busy")
    provider = harness.runtime._tasks[handle.task_id].pending_input_provider
    user_message = "Also explain the test failures"
    if notice_first:
        await harness.emitters["busy"](_event())
    assert await harness.runtime.steer(SESSION_KEY, user_message) == handle.task_id
    if not notice_first:
        await harness.emitters["busy"](_event())
    assert len(provider.peek_pending()) == 2
    if claim:
        assert len((await provider.claim_pending()).texts) == 2
        assert provider.peek_pending() == []

    harness.finish.set()
    await harness.runtime.wait(handle.task_id, timeout=2)
    promoted = [row for row in harness.storage.records.values() if row.task_id != handle.task_id]
    assert len(promoted) == 1
    await harness.runtime.wait(promoted[0].task_id, timeout=2)

    assert harness.messages == ["busy", user_message]
    assert not harness.completions()


async def test_turn_finishing_during_owner_read_does_not_receive_notice(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = await harness.enqueue("busy")
    running = harness.runtime._tasks[handle.task_id]
    owner_read_entered = asyncio.Event()
    release_owner_read = asyncio.Event()

    async def paused_get_session(_key: str) -> Any:
        owner_read_entered.set()
        await release_owner_read.wait()
        return harness.storage.owner

    monkeypatch.setattr(harness.storage, "get_session", paused_get_session)
    delivery = asyncio.create_task(harness.emitters["busy"](_event()))
    try:
        await asyncio.wait_for(owner_read_entered.wait(), 2)
        harness.finish.set()
        await harness.runtime.wait(handle.task_id, timeout=2)
    finally:
        release_owner_read.set()
        await asyncio.wait_for(delivery, 2)

    assert running.pending_input_provider.peek_pending() == []
    assert harness.messages == ["busy"]
    assert len(harness.storage.records) == 1


@pytest.mark.parametrize("notice_before_cancel", [False, True])
async def test_cancelled_turn_never_resumes_from_process_notice(
    harness: _Harness, notice_before_cancel: bool,
) -> None:
    handle = await harness.enqueue("busy")
    if notice_before_cancel:
        await harness.emitters["busy"](_event())
    assert await harness.runtime.cancel_exact(
        task_id=handle.task_id, session_key=SESSION_KEY,
        source="webui_stop", reason="user_abort",
    ) == 1
    terminal = await harness.runtime.wait(handle.task_id, timeout=2)
    assert terminal.status is AgentTaskStatus.CANCELLED

    await harness.emitters["busy"](_event(status="killed", returncode=-15))

    assert harness.messages == ["busy"]
    assert len(harness.storage.records) == 1
    assert not harness.completions()


async def test_late_completion_keeps_process_output_readable(harness: _Harness) -> None:
    await harness.enqueue("launch")
    harness.process.output_lines.append("synthetic saved output\n")

    await harness.emitters["launch"](_event())
    result = await shell.read_session_process_log(
        "process-one", session_key=SESSION_KEY, session_id="owner", session_epoch=0,
    )

    assert result["status"] == "done"
    assert result["output"] == "synthetic saved output\n"
    assert harness.process.completion_consumed is False
    assert harness.messages == ["launch"]
    assert len(harness.storage.records) == 1
