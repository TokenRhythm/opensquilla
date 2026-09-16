from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

from opensquilla.application.session_maintenance import (
    CompactSession,
    SessionCompactionDeadlineError,
    SessionCompactionEvent,
    SessionCompactionExecutionResult,
    SessionCompactionFlushSafetyError,
    SessionCompactionMemoryAssessment,
    SessionCompactionMemoryResult,
    SessionCompactionMilestone,
    SessionCompactionPhaseTimeoutError,
    SessionCompactionPlan,
    SessionCompactionSession,
    SessionCompactionTiming,
    SessionMaintenance,
)


@dataclass
class _RuntimeSession:
    session_key: str = "agent:main:webchat:one"


class _Lock:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def acquire(self) -> bool:
        self._calls.append("lock.acquire")
        return True

    def release(self) -> None:
        self._calls.append("lock.release")


class _Ports:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.events: list[SessionCompactionEvent] = []
        self.session = SessionCompactionSession(
            session_id="session-1",
            agent_id="main",
            runtime_value=_RuntimeSession(),
        )
        self.execution = SessionCompactionExecutionResult(
            applied=True,
            summary_len=11,
            summary_source="provider",
            removed_count=2,
            kept_count=1,
        )
        self.execution_error: BaseException | None = None
        self.allow_memory = True
        self.memory_available = True
        self.cancel_observed_broadcast = False
        self.executor_gate: asyncio.Event | None = None
        self.background_task: asyncio.Task[object] | None = None

    def timing(self) -> SessionCompactionTiming:
        return SessionCompactionTiming(
            total_timeout_seconds=5.0,
            heartbeat_interval_seconds=10.0,
        )

    def default_context_window_tokens(self) -> int:
        return 100_000

    async def load_session(self, session_key: str) -> SessionCompactionSession | None:
        self.calls.append(f"session.load:{session_key}")
        return self.session

    def is_ephemeral_session_key(self, session_key: str) -> bool:
        return False

    def resolve_context_window_tokens(
        self,
        session: SessionCompactionSession | None,
        requested_tokens: int,
    ) -> int:
        self.calls.append("budget.resolve")
        return min(requested_tokens, 8_192)

    def build_plan(
        self,
        session: SessionCompactionSession | None,
        requested_tokens: int,
        compaction_id: str,
        operation_deadline: float,
    ) -> SessionCompactionPlan:
        self.calls.append("plan.build")
        return SessionCompactionPlan(8_192, runtime_value=object())

    def for_session(self, session_key: str) -> _Lock:
        return _Lock(self.calls)

    @property
    def flush_enabled(self) -> bool:
        return True

    @property
    def flush_available(self) -> bool:
        return self.memory_available

    async def transcript(self, session_key: str) -> tuple[object, ...]:
        self.calls.append("memory.transcript")
        return ("entry",)

    async def flush(
        self,
        session: SessionCompactionSession,
        transcript: tuple[object, ...],
        plan: SessionCompactionPlan,
        compaction_id: str,
    ) -> object:
        self.calls.append("memory.flush")
        return {"status": "flushed"}

    def receipt_status(self, receipt: object | None) -> str:
        return "flushed" if receipt is not None else "missing"

    def receipt_is_successful(self, receipt: object) -> bool:
        return True

    @property
    def requires_safe_receipt(self) -> bool:
        return True

    async def checkpoint_covers(
        self,
        session: SessionCompactionSession,
        transcript: tuple[object, ...],
    ) -> bool:
        self.calls.append("memory.checkpoint")
        return self.allow_memory

    def assess(
        self,
        receipt: object | None,
        *,
        checkpoint_safe: bool,
        required: bool,
    ) -> SessionCompactionMemoryAssessment:
        self.calls.append("memory.assess")
        return SessionCompactionMemoryAssessment(
            allows_destructive_compaction=self.allow_memory,
            safety_status="safe" if self.allow_memory else "unsafe",
            semantic_status="durable" if self.allow_memory else "missing",
        )

    def record(self, outcome: str, **details: object) -> None:
        self.calls.append(f"memory.record:{outcome}")

    async def compact(
        self,
        command: CompactSession,
        plan: SessionCompactionPlan,
        memory: SessionCompactionMemoryResult,
    ) -> SessionCompactionExecutionResult:
        self.calls.append("executor.compact")
        if self.executor_gate is not None:
            await self.executor_gate.wait()
        if self.execution_error is not None:
            raise self.execution_error
        return self.execution

    async def prepare(self, event: SessionCompactionEvent) -> object:
        self.calls.append(f"event.prepare:{event.status}")
        return event

    def claim_and_buffer(
        self,
        prepared: object,
        event: SessionCompactionEvent,
        *,
        track_current_task: bool,
    ) -> object:
        self.calls.append(f"event.claim:{event.status}")
        self.events.append(event)
        return event

    async def broadcast(self, buffered: object) -> None:
        event = buffered
        assert isinstance(event, SessionCompactionEvent)
        self.calls.append(f"event.broadcast:{event.status}")
        if (
            self.cancel_observed_broadcast
            and event.milestone is SessionCompactionMilestone.CHUNK_SUMMARIZED
        ):
            self.cancel_observed_broadcast = False
            raise asyncio.CancelledError

    def register(
        self,
        session_key: str,
        compaction_id: str,
        task: asyncio.Task[object],
    ) -> None:
        self.calls.append("ownership.register")
        self.background_task = task

    def background_failed(
        self,
        session_key: str,
        compaction_id: str,
        error: Exception,
    ) -> None:
        self.calls.append(f"ownership.failed:{type(error).__name__}")

    @asynccontextmanager
    async def account(self, session_key: str) -> AsyncIterator[None]:
        self.calls.append("usage.enter")
        try:
            yield
        finally:
            self.calls.append("usage.exit")


def _application(ports: _Ports) -> SessionMaintenance:
    return SessionMaintenance(
        planning=ports,
        locking=ports,
        memory=ports,
        executor=ports,
        lifecycle=ports,
        ownership=ports,
        usage=ports,
        new_compaction_id=lambda: "compact-1",
    )


async def test_compaction_orders_safety_before_destructive_execution() -> None:
    ports = _Ports()

    result = await _application(ports).compact(
        CompactSession(
            " agent:main:webchat:one ",
            context_window_tokens=16_384,
            instructions="Preserve active decisions.",
        )
    )

    assert result.status == "completed"
    assert result.context_window_tokens == 8_192
    assert result.flush_receipt_status == "flushed"
    assert ports.calls.index("lock.acquire") < ports.calls.index("memory.flush")
    assert ports.calls.index("memory.assess") < ports.calls.index("executor.compact")
    assert ports.calls.index("executor.compact") < ports.calls.index("lock.release")
    assert ports.calls.index("usage.exit") < ports.calls.index("lock.release")
    assert [event.status for event in ports.events].count("completed") == 1
    assert ports.events[-1].terminal is True


async def test_unsafe_memory_flush_blocks_compactor() -> None:
    ports = _Ports()
    ports.allow_memory = False

    with pytest.raises(SessionCompactionFlushSafetyError):
        await _application(ports).compact(CompactSession("agent:main:webchat:one"))

    assert "executor.compact" not in ports.calls
    assert [event.status for event in ports.events if event.terminal] == ["failed"]


async def test_missing_flush_service_still_enforces_checkpoint_safety() -> None:
    ports = _Ports()
    ports.memory_available = False
    ports.allow_memory = False

    with pytest.raises(SessionCompactionFlushSafetyError):
        await _application(ports).compact(CompactSession("agent:main:webchat:one"))

    assert "memory.flush" not in ports.calls
    assert ports.calls.index("memory.checkpoint") < ports.calls.index("memory.assess")
    assert "executor.compact" not in ports.calls


async def test_timeout_claims_one_terminal_result() -> None:
    ports = _Ports()
    ports.execution_error = SessionCompactionPhaseTimeoutError("summarizing")

    with pytest.raises(SessionCompactionDeadlineError) as raised:
        await _application(ports).compact(CompactSession("agent:main:webchat:one"))

    assert raised.value.phase == "summarizing"
    assert [event.status for event in ports.events if event.terminal] == ["timed_out"]


async def test_cancel_after_commit_reconciles_exactly_one_completed_terminal() -> None:
    ports = _Ports()
    ports.cancel_observed_broadcast = True

    task = asyncio.create_task(
        _application(ports).compact(CompactSession("agent:main:webchat:one"))
    )
    with pytest.raises(asyncio.CancelledError):
        await task

    terminals = [event for event in ports.events if event.terminal]
    assert len(terminals) == 1
    assert terminals[0].status == "completed"
    assert terminals[0].reason == "cancelled_after_commit"
    assert terminals[0].cancellation_reconciled is True


async def test_background_owner_is_registered_before_started_event() -> None:
    ports = _Ports()
    ports.executor_gate = asyncio.Event()

    result = await _application(ports).compact(
        CompactSession("agent:main:webchat:one", wait=False)
    )

    assert result.status == "started"
    assert ports.calls.index("ownership.register") < ports.calls.index(
        "event.prepare:started"
    )
    assert ports.background_task is not None
    ports.executor_gate.set()
    await ports.background_task
    assert [event.status for event in ports.events if event.terminal] == ["completed"]


async def test_invalid_compaction_budget_never_loads_session() -> None:
    ports = _Ports()

    with pytest.raises(ValueError, match="positive"):
        await _application(ports).compact(
            CompactSession("agent:main:webchat:one", context_window_tokens=0)
        )

    assert ports.calls == []
