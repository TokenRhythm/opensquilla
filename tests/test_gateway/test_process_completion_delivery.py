"""Managed process notices use the existing session admission and steer lanes."""

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
    failures: int = 0
    completion_attempts: int = 0
    failure_callback: Callable[[], None] | None = None

    async def get_session(self, _session_key: str) -> Any:
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
        if record.run_kind == "runtime_send":
            self.completion_attempts += 1
            if self.failures:
                self.failures -= 1
                if self.failure_callback is not None:
                    self.failure_callback()
                raise OSError("synthetic transient storage failure")
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
async def test_idle_completion_survives_terminal_cache_eviction(
    harness: _Harness, intervening_turn: bool,
) -> None:
    await harness.enqueue("launch")
    if intervening_turn:
        await harness.enqueue("write documentation")
    assert SESSION_KEY not in harness.runtime._last_envelope_by_session

    await harness.emitters["launch"](_event())

    assert len(harness.completions()) == 1
    notice = harness.completions()[0]
    await harness.runtime.wait(notice.task_id, timeout=2)
    assert "execution_id=process-one status=done returncode=0" in harness.messages[-1]
    assert "synthetic output" in harness.messages[-1]
    assert notice.details["session_id"] == "owner"
    assert notice.details["session_epoch"] == 0


@pytest.mark.parametrize("replacement", [None, ("replacement", 0), ("owner", 1)])
async def test_stale_completion_cannot_steer_replacement_owner(
    harness: _Harness, replacement: tuple[str, int] | None,
) -> None:
    await harness.enqueue("launch")
    if replacement is None:
        harness.storage.owner = None
        await harness.emitters["launch"](_event())
        assert not harness.completions()
        return
    harness.storage.owner = SimpleNamespace(session_id=replacement[0], epoch=replacement[1])
    handle = await harness.enqueue("busy")

    await harness.emitters["launch"](_event())

    running = harness.runtime._tasks[handle.task_id]
    assert running.pending_input_provider.peek_pending() == []
    assert not harness.completions()


async def test_busy_same_generation_receives_notice_once(harness: _Harness) -> None:
    await harness.enqueue("launch")
    handle = await harness.enqueue("busy")
    await asyncio.gather(*(harness.emitters["launch"](_event()) for _ in range(3)))
    running = harness.runtime._tasks[handle.task_id]
    notices = running.pending_input_provider.peek_pending()
    assert len(notices) == 1
    assert "execution_id=process-one" in notices[0]
    assert not harness.completions()


@pytest.mark.parametrize("overrides", [{"notify_on_exit": False}, {"completion_consumed": True}])
async def test_unrequested_or_manually_consumed_notice_does_not_wake(
    harness: _Harness, overrides: dict[str, Any],
) -> None:
    await harness.enqueue("launch")
    await harness.emitters["launch"](_event(**overrides))
    assert not harness.completions()


async def test_transient_send_failure_retries_in_production_callback(harness: _Harness) -> None:
    await harness.enqueue("launch")
    harness.storage.failures = 1

    await harness.emitters["launch"](_event())
    assert harness.storage.completion_attempts == 2
    assert len(harness.completions()) == 1
    await harness.emitters["launch"](_event())
    assert harness.storage.completion_attempts == 2


async def test_concurrent_idle_notices_share_successful_retry(harness: _Harness) -> None:
    await harness.enqueue("launch")
    harness.storage.failures = 1
    await asyncio.gather(*(harness.emitters["launch"](_event()) for _ in range(3)))
    assert harness.storage.completion_attempts == 2
    assert len(harness.completions()) == 1


async def test_exhausted_retry_does_not_poison_later_delivery(harness: _Harness) -> None:
    await harness.enqueue("launch")
    harness.storage.failures = 3
    await harness.emitters["launch"](_event())
    assert harness.storage.completion_attempts == 3
    assert not harness.completions()
    await harness.emitters["launch"](_event())
    assert harness.storage.completion_attempts == 4
    assert len(harness.completions()) == 1


async def test_completion_rechecks_owner_after_waiting_for_admission(harness: _Harness) -> None:
    await harness.enqueue("launch")
    async with harness.runtime.collect_admission(SESSION_KEY):
        delivery = asyncio.create_task(harness.emitters["launch"](_event()))
        await asyncio.sleep(0)
        harness.storage.owner = SimpleNamespace(session_id="replacement", epoch=1)
    await delivery
    assert not harness.completions()


async def test_process_projection_uses_session_owner_not_execution_id(harness: _Harness) -> None:
    events: list[dict[str, Any]] = []

    async def observer(_key: str, name: str, payload: dict[str, Any]) -> None:
        if name == "session.event.process_completed":
            events.append(payload)

    harness.runtime._event_emitter = observer
    await harness.enqueue("launch")
    await harness.emitters["launch"](_event(notify_on_exit=False))
    assert events[0]["session_id"] == "owner"
    assert events[0]["epoch"] == events[0]["session_epoch"] == 0
    assert events[0]["execution_id"] == "process-one"


@pytest.mark.parametrize("during_owner_read", [False, True])
async def test_manual_consumption_is_rechecked_before_delivery(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, during_owner_read: bool,
) -> None:
    handle = await harness.enqueue("launch")
    process = shell._BgSession(
        session_id="process-one", command="synthetic",
        process=SimpleNamespace(returncode=0), done=True,
        session_key=SESSION_KEY, task_id=handle.task_id, completion_consumed=False,
    )
    monkeypatch.setitem(shell._bg_sessions, "process-one", process)

    def consume() -> None:
        process.completion_consumed = True

    if during_owner_read:
        async def get_session(_key: str) -> Any:
            consume()
            return harness.storage.owner
        monkeypatch.setattr(harness.storage, "get_session", get_session)
    else:
        harness.storage.failures = 1
        harness.storage.failure_callback = consume

    await harness.emitters["launch"](_event())

    assert harness.storage.completion_attempts == (0 if during_owner_read else 1)
    assert not harness.completions()


async def test_retry_does_not_cross_session_replacement(harness: _Harness) -> None:
    await harness.enqueue("launch")

    def replace_owner() -> None:
        harness.storage.owner = SimpleNamespace(session_id="replacement", epoch=1)

    harness.storage.failures = 1
    harness.storage.failure_callback = replace_owner
    await harness.emitters["launch"](_event())
    assert harness.storage.completion_attempts == 1
    assert not harness.completions()


async def test_retry_does_not_revive_removed_execution(harness: _Harness) -> None:
    await harness.enqueue("launch")

    def remove_process() -> None:
        shell._bg_sessions.pop("process-one")

    harness.storage.failures = 1
    harness.storage.failure_callback = remove_process
    await harness.emitters["launch"](_event())
    assert harness.storage.completion_attempts == 1
    assert not harness.completions()


async def test_observer_failure_does_not_suppress_completion(harness: _Harness) -> None:
    async def observer(_key: str, name: str, _payload: dict[str, Any]) -> None:
        if name in {"session.event.process_completed", "task.queued"}:
            raise OSError("synthetic observer failure")

    await harness.enqueue("launch")
    harness.runtime._event_emitter = observer
    await harness.emitters["launch"](_event())
    await harness.emitters["launch"](_event())
    assert len(harness.completions()) == 1


async def test_terminal_settlement_queues_notice_instead_of_losing_steer(
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
        assert len(harness.completions()) == 1
    finally:
        finish_terminal.set()
    await harness.runtime.wait(handle.task_id, timeout=2)
    await harness.runtime.wait(harness.completions()[0].task_id, timeout=2)


async def test_shutdown_prevents_completion_retry(harness: _Harness) -> None:
    await harness.enqueue("launch")
    await harness.runtime.shutdown()
    await harness.emitters["launch"](_event())
    assert harness.storage.completion_attempts == 0
