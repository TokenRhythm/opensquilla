"""Transport-neutral coordination for manual session compaction.

The Module owns the operation lifecycle, deadline, memory-safety ordering and
post-commit reconciliation. Concrete Ports terminate runtime and transport
details: the shared conversation runtime still owns event sequence, replay and
reconnect fencing, while the existing compactor remains the only durable
compaction implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from opensquilla.session_key import canonicalize_session_key

_STALE_SKIP_REASONS = frozenset(
    {
        "stale_preimage",
        "stale_context_state",
        "consumer_admission_stale_or_failed",
    }
)


@dataclass(frozen=True, slots=True)
class CompactSession:
    session_key: str
    wait: bool = True
    context_window_tokens: int | None = None
    instructions: str | None = None


@dataclass(frozen=True, slots=True)
class SessionCompactionTiming:
    total_timeout_seconds: float = 120.0
    heartbeat_interval_seconds: float = 15.0


@dataclass(frozen=True, slots=True)
class SessionCompactionSession:
    session_id: str | None
    agent_id: str
    runtime_value: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SessionCompactionPlan:
    context_window_tokens: int
    runtime_value: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class SessionCompactionMemoryAssessment:
    allows_destructive_compaction: bool
    safety_status: str
    semantic_status: str


@dataclass(frozen=True, slots=True)
class SessionCompactionMemoryResult:
    receipt: object | None = field(default=None, repr=False, compare=False)
    receipt_status: str | None = None


@dataclass(frozen=True, slots=True)
class SessionCompactionExecutionResult:
    applied: bool
    summary_len: int
    summary_source: str = "unknown"
    tokens_before: int = 0
    tokens_after: int = 0
    remaining_budget_tokens: int = 0
    removed_count: int = 0
    kept_count: int = 0
    chunk_count: int = 0
    coverage_status: str = "unknown"
    missing_obligation_count: int = 0
    critical_carry_forward_count: int = 0
    state_kind: str = "text"
    skip_reason: str = ""
    quality_report: Mapping[str, object] = field(default_factory=dict)


class SessionCompactionMilestone(StrEnum):
    TRIGGERED = "triggered"
    CHUNK_SUMMARIZED = "chunk_summarized"
    SUMMARY_VERIFIED = "summary_verified"
    PERSISTED = "persisted"


@dataclass(frozen=True, slots=True)
class SessionCompactionResult:
    session_key: str
    compaction_id: str
    status: str
    applied: bool
    context_window_tokens: int
    summary_len: int = 0
    summary_source: str = "none"
    tokens_before: int = 0
    tokens_after: int = 0
    remaining_budget_tokens: int = 0
    removed_count: int = 0
    kept_count: int = 0
    chunk_count: int = 0
    coverage_status: str = "unknown"
    missing_obligation_count: int = 0
    critical_carry_forward_count: int = 0
    state_kind: str = "text"
    quality_report: Mapping[str, object] = field(default_factory=dict)
    reason: str | None = None
    flush_receipt: object | None = field(default=None, repr=False, compare=False)
    flush_receipt_status: str | None = None


@dataclass(frozen=True, slots=True)
class SessionCompactionEvent:
    session_key: str
    compaction_id: str
    status: str
    context_window_tokens: int
    milestone: SessionCompactionMilestone = SessionCompactionMilestone.TRIGGERED
    reason: str | None = None
    message: str | None = None
    stage: str | None = None
    heartbeat: bool = False
    heartbeat_at: int | None = None
    elapsed_ms: int | None = None
    heartbeat_interval_seconds: float | None = None
    result: SessionCompactionExecutionResult | None = None
    flush_receipt_status: str | None = None
    cancellation_reconciled: bool = False
    deadline_reconciled: bool = False
    observation_error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status.lower() in {
            "completed",
            "skipped",
            "stale",
            "failed",
            "error",
            "cancelled",
            "timed_out",
            "emergency_ephemeral",
        }


@dataclass(slots=True)
class SessionCompactionFlushSafetyError(RuntimeError):
    session_key: str
    session_id: str | None
    receipt: object | None
    receipt_status: str | None
    assessment: SessionCompactionMemoryAssessment

    def __str__(self) -> str:
        return "manual compaction requires a safe memory flush"


@dataclass(slots=True)
class SessionCompactionDeadlineError(TimeoutError):
    session_key: str
    compaction_id: str
    phase: str

    def __str__(self) -> str:
        return "compaction exceeded its absolute deadline"


@dataclass(slots=True)
class SessionCompactionPhaseTimeoutError(TimeoutError):
    phase: str

    def __str__(self) -> str:
        return f"compaction phase timed out: {self.phase}"


class SessionCompactionUnavailableError(RuntimeError):
    pass


@dataclass(slots=True)
class SessionCompactionNotFoundError(LookupError):
    session_key: str


class SessionCompactionPlanningPort(Protocol):
    def timing(self) -> SessionCompactionTiming: ...

    def default_context_window_tokens(self) -> int: ...

    async def load_session(self, session_key: str) -> SessionCompactionSession | None: ...

    def is_ephemeral_session_key(self, session_key: str) -> bool: ...

    def resolve_context_window_tokens(
        self,
        session: SessionCompactionSession | None,
        requested_tokens: int,
    ) -> int: ...

    def build_plan(
        self,
        session: SessionCompactionSession | None,
        requested_tokens: int,
        compaction_id: str,
        operation_deadline: float,
    ) -> SessionCompactionPlan: ...


class SessionCompactionLock(Protocol):
    async def acquire(self) -> bool: ...

    def release(self) -> None: ...


class SessionCompactionLockPort(Protocol):
    def for_session(self, session_key: str) -> SessionCompactionLock | None: ...


class SessionCompactionMemoryPort(Protocol):
    @property
    def flush_enabled(self) -> bool: ...

    @property
    def flush_available(self) -> bool: ...

    async def transcript(self, session_key: str) -> tuple[object, ...] | None: ...

    async def flush(
        self,
        session: SessionCompactionSession,
        transcript: tuple[object, ...],
        plan: SessionCompactionPlan,
        compaction_id: str,
    ) -> object: ...

    def receipt_status(self, receipt: object | None) -> str: ...

    def receipt_is_successful(self, receipt: object) -> bool: ...

    @property
    def requires_safe_receipt(self) -> bool: ...

    async def checkpoint_covers(
        self,
        session: SessionCompactionSession,
        transcript: tuple[object, ...],
    ) -> bool: ...

    def assess(
        self,
        receipt: object | None,
        *,
        checkpoint_safe: bool,
        required: bool,
    ) -> SessionCompactionMemoryAssessment: ...

    def record(self, outcome: str, **details: object) -> None: ...


class SessionCompactionExecutorPort(Protocol):
    async def compact(
        self,
        command: CompactSession,
        plan: SessionCompactionPlan,
        memory: SessionCompactionMemoryResult,
    ) -> SessionCompactionExecutionResult: ...


class SessionCompactionLifecyclePort(Protocol):
    async def prepare(self, event: SessionCompactionEvent) -> object: ...

    def claim_and_buffer(
        self,
        prepared: object,
        event: SessionCompactionEvent,
        *,
        track_current_task: bool,
    ) -> object | None: ...

    async def broadcast(self, buffered: object) -> None: ...


class SessionCompactionOwnershipPort(Protocol):
    def register(
        self,
        session_key: str,
        compaction_id: str,
        task: asyncio.Task[object],
    ) -> None: ...

    def background_failed(
        self,
        session_key: str,
        compaction_id: str,
        error: Exception,
    ) -> None: ...


class SessionCompactionUsagePort(Protocol):
    def account(self, session_key: str) -> AbstractAsyncContextManager[None]: ...


type CompactionIdFactory = Callable[[], str]


class SessionMaintenance:
    """Coordinate one manual compaction through narrow runtime Ports."""

    def __init__(
        self,
        *,
        planning: SessionCompactionPlanningPort,
        locking: SessionCompactionLockPort,
        memory: SessionCompactionMemoryPort,
        executor: SessionCompactionExecutorPort,
        lifecycle: SessionCompactionLifecyclePort,
        ownership: SessionCompactionOwnershipPort,
        usage: SessionCompactionUsagePort,
        new_compaction_id: CompactionIdFactory,
    ) -> None:
        self._planning = planning
        self._locking = locking
        self._memory = memory
        self._executor = executor
        self._lifecycle = lifecycle
        self._ownership = ownership
        self._usage = usage
        self._new_compaction_id = new_compaction_id

    async def compact(self, command: CompactSession) -> SessionCompactionResult:
        key = canonicalize_session_key(command.session_key)
        if not key:
            raise ValueError("session_key must be non-empty")
        if command.context_window_tokens is not None and command.context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be positive")
        if command.instructions is not None and not isinstance(command.instructions, str):
            raise ValueError("instructions must be a string when provided")
        command = replace(command, session_key=key)
        requested_tokens = (
            command.context_window_tokens
            if command.context_window_tokens is not None
            else self._planning.default_context_window_tokens()
        )
        initial_session = await self._planning.load_session(key)
        initial_context_window_tokens = self._planning.resolve_context_window_tokens(
            initial_session,
            requested_tokens,
        )
        timing = self._planning.timing()
        operation = _ManualCompactionOperation(
            command=command,
            requested_tokens=requested_tokens,
            initial_context_window_tokens=initial_context_window_tokens,
            timing=timing,
            planning=self._planning,
            locking=self._locking,
            memory=self._memory,
            executor=self._executor,
            lifecycle=self._lifecycle,
            ownership=self._ownership,
            usage=self._usage,
            compaction_id=self._new_compaction_id(),
        )
        if command.wait:
            return await operation.execute()
        return await operation.start_background()


class _ManualCompactionOperation:
    def __init__(
        self,
        *,
        command: CompactSession,
        requested_tokens: int,
        initial_context_window_tokens: int,
        timing: SessionCompactionTiming,
        planning: SessionCompactionPlanningPort,
        locking: SessionCompactionLockPort,
        memory: SessionCompactionMemoryPort,
        executor: SessionCompactionExecutorPort,
        lifecycle: SessionCompactionLifecyclePort,
        ownership: SessionCompactionOwnershipPort,
        usage: SessionCompactionUsagePort,
        compaction_id: str,
    ) -> None:
        self._command = command
        self._requested_tokens = requested_tokens
        self._context_window_tokens = initial_context_window_tokens
        self._timing = timing
        self._planning = planning
        self._locking = locking
        self._memory = memory
        self._executor = executor
        self._lifecycle = lifecycle
        self._ownership = ownership
        self._usage = usage
        self._compaction_id = compaction_id
        self._started_emitted = False
        self._terminal_emitted = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stage = "admission"
        self._operation_deadline = time.monotonic() + timing.total_timeout_seconds

    async def _publish(self, event: SessionCompactionEvent) -> None:
        if event.terminal and self._terminal_emitted:
            return
        prepared = await self._lifecycle.prepare(event)
        buffered = self._lifecycle.claim_and_buffer(
            prepared,
            event,
            track_current_task=self._command.wait,
        )
        if buffered is None:
            return
        if event.status.lower() == "started":
            self._started_emitted = True
        if event.terminal:
            self._terminal_emitted = True
        await self._lifecycle.broadcast(buffered)

    def _event(self, status: str, **changes: object) -> SessionCompactionEvent:
        values: dict[str, object] = {
            "session_key": self._command.session_key,
            "compaction_id": self._compaction_id,
            "status": status,
            "context_window_tokens": self._context_window_tokens,
        }
        values.update(changes)
        return SessionCompactionEvent(**values)  # type: ignore[arg-type]

    async def _heartbeat(self) -> None:
        started = time.monotonic()
        try:
            while not self._terminal_emitted:
                await asyncio.sleep(self._timing.heartbeat_interval_seconds)
                if self._terminal_emitted:
                    return
                await self._publish(
                    self._event(
                        "observed",
                        heartbeat=True,
                        heartbeat_at=int(time.time() * 1000),
                        elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
                        stage=self._stage,
                    )
                )
        except asyncio.CancelledError:
            return

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat())

    async def _stop_heartbeat(self) -> None:
        task, self._heartbeat_task = self._heartbeat_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _prepare_memory(
        self,
        session: SessionCompactionSession,
        plan: SessionCompactionPlan,
    ) -> SessionCompactionMemoryResult:
        if not self._memory.flush_enabled:
            return SessionCompactionMemoryResult()
        transcript = await self._memory.transcript(self._command.session_key)
        if transcript is None:
            self._memory.record("flush_skipped", reason="transcript_reader_unavailable")
            return SessionCompactionMemoryResult()
        if not transcript:
            return SessionCompactionMemoryResult()

        receipt: object | None = None
        receipt_status: str | None = None
        if not self._memory.flush_available:
            self._memory.record("flush_skipped", reason="flush_service_unavailable")
            receipt_status = self._memory.receipt_status(None)
        else:
            self._stage = "flushing"
            try:
                receipt = await self._memory.flush(
                    session,
                    transcript,
                    plan,
                    self._compaction_id,
                )
            except SessionCompactionPhaseTimeoutError:
                raise
            except Exception as exc:
                self._memory.record("flush_failed", error=str(exc))
                receipt_status = self._memory.receipt_status(None)
            else:
                receipt_status = self._memory.receipt_status(receipt)
                self._memory.record(
                    (
                        "flush_done"
                        if self._memory.receipt_is_successful(receipt)
                        else "flush_degraded"
                    ),
                    receipt_status=receipt_status,
                    receipt=receipt,
                )

        if self._memory.requires_safe_receipt:
            checkpoint_safe = await self._memory.checkpoint_covers(session, transcript)
            assessment = self._memory.assess(
                receipt,
                checkpoint_safe=checkpoint_safe,
                required=True,
            )
            if not assessment.allows_destructive_compaction:
                raise SessionCompactionFlushSafetyError(
                    session_key=self._command.session_key,
                    session_id=session.session_id,
                    receipt=receipt,
                    receipt_status=receipt_status,
                    assessment=assessment,
                )
        return SessionCompactionMemoryResult(
            receipt=receipt,
            receipt_status=receipt_status,
        )

    async def _run_locked(self) -> SessionCompactionResult:
        session = await self._planning.load_session(self._command.session_key)
        if session is None:
            if self._planning.is_ephemeral_session_key(self._command.session_key):
                if not self._started_emitted:
                    await self._publish(
                        self._event(
                            "started",
                            heartbeat_interval_seconds=self._timing.heartbeat_interval_seconds,
                        )
                    )
                ephemeral_reason = "empty_ephemeral_webchat_session"
                await self._publish(self._event("skipped", reason=ephemeral_reason))
                return SessionCompactionResult(
                    session_key=self._command.session_key,
                    compaction_id=self._compaction_id,
                    status="skipped",
                    applied=False,
                    context_window_tokens=self._context_window_tokens,
                    remaining_budget_tokens=self._context_window_tokens,
                    reason=ephemeral_reason,
                )
            raise SessionCompactionNotFoundError(self._command.session_key)

        self._context_window_tokens = self._planning.resolve_context_window_tokens(
            session,
            self._requested_tokens,
        )
        plan = self._planning.build_plan(
            session,
            self._requested_tokens,
            self._compaction_id,
            self._operation_deadline,
        )
        self._context_window_tokens = plan.context_window_tokens
        if not self._started_emitted:
            await self._publish(
                self._event(
                    "started",
                    heartbeat_interval_seconds=self._timing.heartbeat_interval_seconds,
                )
            )
        self._start_heartbeat()

        committed: SessionCompactionExecutionResult | None = None
        memory = SessionCompactionMemoryResult()
        try:
            memory = await self._prepare_memory(session, plan)
            self._stage = "summarizing"
            outcome = await self._executor.compact(self._command, plan, memory)
            if outcome.applied:
                committed = outcome
                for milestone in (
                    SessionCompactionMilestone.CHUNK_SUMMARIZED,
                    SessionCompactionMilestone.SUMMARY_VERIFIED,
                ):
                    await self._publish(
                        self._event(
                            "observed",
                            milestone=milestone,
                            result=outcome,
                            flush_receipt_status=memory.receipt_status,
                        )
                    )
        except asyncio.CancelledError:
            if committed is not None:
                await self._publish(
                    self._event(
                        "completed",
                        milestone=SessionCompactionMilestone.PERSISTED,
                        reason="cancelled_after_commit",
                        result=committed,
                        flush_receipt_status=memory.receipt_status,
                        cancellation_reconciled=True,
                    )
                )
            raise
        except SessionCompactionPhaseTimeoutError:
            if committed is not None:
                await self._publish(
                    self._event(
                        "completed",
                        milestone=SessionCompactionMilestone.PERSISTED,
                        reason="deadline_after_commit",
                        result=committed,
                        flush_receipt_status=memory.receipt_status,
                        deadline_reconciled=True,
                    )
                )
            raise
        except Exception as exc:
            if committed is not None:
                await self._publish(
                    self._event(
                        "completed",
                        milestone=SessionCompactionMilestone.PERSISTED,
                        reason="post_commit_observation_failed",
                        result=committed,
                        flush_receipt_status=memory.receipt_status,
                        observation_error=str(exc),
                    )
                )
            raise

        status = "completed" if outcome.applied else (
            "stale" if outcome.skip_reason in _STALE_SKIP_REASONS else "skipped"
        )
        reason = None if outcome.applied else (outcome.skip_reason or "empty_summary")
        await self._publish(
            self._event(
                status,
                milestone=(
                    SessionCompactionMilestone.PERSISTED
                    if outcome.applied
                    else SessionCompactionMilestone.TRIGGERED
                ),
                reason=reason,
                result=outcome,
                flush_receipt_status=memory.receipt_status,
            )
        )
        return SessionCompactionResult(
            session_key=self._command.session_key,
            compaction_id=self._compaction_id,
            status=status,
            applied=outcome.applied,
            context_window_tokens=self._context_window_tokens,
            summary_len=outcome.summary_len,
            summary_source=outcome.summary_source,
            tokens_before=outcome.tokens_before,
            tokens_after=outcome.tokens_after,
            remaining_budget_tokens=outcome.remaining_budget_tokens,
            removed_count=outcome.removed_count,
            kept_count=outcome.kept_count,
            chunk_count=outcome.chunk_count,
            coverage_status=outcome.coverage_status,
            missing_obligation_count=outcome.missing_obligation_count,
            critical_carry_forward_count=outcome.critical_carry_forward_count,
            state_kind=outcome.state_kind,
            quality_report=outcome.quality_report,
            reason=reason,
            flush_receipt=memory.receipt,
            flush_receipt_status=memory.receipt_status,
        )

    async def _run_accounted(self) -> SessionCompactionResult:
        async with self._usage.account(self._command.session_key):
            return await self._run_locked()

    async def execute(self) -> SessionCompactionResult:
        lock = self._locking.for_session(self._command.session_key)
        acquired = False
        try:
            if lock is not None:
                remaining = max(0.0, self._operation_deadline - time.monotonic())
                try:
                    async with asyncio.timeout(remaining):
                        await lock.acquire()
                except TimeoutError as exc:
                    raise SessionCompactionPhaseTimeoutError("admission") from exc
                acquired = True
            remaining = max(0.0, self._operation_deadline - time.monotonic())
            if remaining <= 0:
                raise SessionCompactionPhaseTimeoutError("admission")
            try:
                async with asyncio.timeout(remaining):
                    return await self._run_accounted()
            except SessionCompactionPhaseTimeoutError:
                raise
            except TimeoutError as exc:
                raise SessionCompactionPhaseTimeoutError(self._stage) from exc
        except asyncio.CancelledError:
            if self._started_emitted and not self._terminal_emitted:
                await self._publish(
                    self._event(
                        "cancelled",
                        reason="cancelled",
                        message="Compaction was cancelled.",
                    )
                )
            raise
        except SessionCompactionPhaseTimeoutError as exc:
            if self._started_emitted and not self._terminal_emitted:
                await self._publish(
                    self._event(
                        "timed_out",
                        reason="compaction_deadline_exceeded",
                        message=str(exc),
                        stage=exc.phase,
                    )
                )
            raise SessionCompactionDeadlineError(
                session_key=self._command.session_key,
                compaction_id=self._compaction_id,
                phase=exc.phase,
            ) from exc
        except Exception as exc:
            if self._started_emitted and not self._terminal_emitted:
                await self._publish(
                    self._event(
                        "failed",
                        reason="compaction_failed",
                        message=str(exc),
                    )
                )
            raise
        finally:
            if acquired and lock is not None:
                lock.release()
            await self._stop_heartbeat()
            if self._started_emitted and not self._terminal_emitted:
                await self._publish(
                    self._event(
                        "failed",
                        reason="terminal_missing",
                        message="Compaction ended without a terminal result.",
                    )
                )

    async def start_background(self) -> SessionCompactionResult:
        entered = asyncio.Event()
        start = asyncio.Event()

        async def run() -> object:
            entered.set()
            try:
                await start.wait()
                return await self.execute()
            except asyncio.CancelledError:
                if self._started_emitted and not self._terminal_emitted:
                    await self._publish(
                        self._event(
                            "cancelled",
                            reason="cancelled",
                            message="Compaction was cancelled.",
                        )
                    )
                return None
            except Exception as exc:
                self._ownership.background_failed(
                    self._command.session_key,
                    self._compaction_id,
                    exc,
                )
                return None

        task = asyncio.create_task(run())
        self._ownership.register(
            self._command.session_key,
            self._compaction_id,
            task,
        )
        await entered.wait()
        try:
            await self._publish(
                self._event(
                    "started",
                    heartbeat_interval_seconds=self._timing.heartbeat_interval_seconds,
                )
            )
            if not self._terminal_emitted:
                self._start_heartbeat()
        except BaseException:
            task.cancel()
            start.set()
            with contextlib.suppress(BaseException):
                await task
            raise
        start.set()
        return SessionCompactionResult(
            session_key=self._command.session_key,
            compaction_id=self._compaction_id,
            status="started",
            applied=False,
            context_window_tokens=self._context_window_tokens,
        )


__all__ = [
    "CompactSession",
    "SessionCompactionDeadlineError",
    "SessionCompactionEvent",
    "SessionCompactionExecutionResult",
    "SessionCompactionExecutorPort",
    "SessionCompactionFlushSafetyError",
    "SessionCompactionLifecyclePort",
    "SessionCompactionLockPort",
    "SessionCompactionMemoryAssessment",
    "SessionCompactionMemoryPort",
    "SessionCompactionMemoryResult",
    "SessionCompactionMilestone",
    "SessionCompactionNotFoundError",
    "SessionCompactionOwnershipPort",
    "SessionCompactionPhaseTimeoutError",
    "SessionCompactionPlan",
    "SessionCompactionPlanningPort",
    "SessionCompactionResult",
    "SessionCompactionSession",
    "SessionCompactionTiming",
    "SessionCompactionUnavailableError",
    "SessionCompactionUsagePort",
    "SessionMaintenance",
]
