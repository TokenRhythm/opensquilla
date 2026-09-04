"""Transport-neutral session reset coordination.

The application service owns the destructive-reset ordering.  Runtime
quiescence, locking, persistence, memory durability, epoch publication and
cache invalidation are supplied as narrow Ports so no transport context enters
this module.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Protocol

from opensquilla.session_key import canonicalize_session_key


@dataclass(frozen=True, slots=True)
class ResetSession:
    session_key: str
    force: bool = False
    force_authorized: bool = False


@dataclass(frozen=True, slots=True)
class SessionResetSnapshot:
    session_key: str
    session_id: str
    agent_id: str
    epoch: int
    transcript: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class SessionResetRotation:
    session_id: str
    rotated: bool


@dataclass(frozen=True, slots=True)
class SessionResetMemoryAssessment:
    allows_reset: bool
    flush_status: str
    safety_status: str
    semantic_status: str


class SessionResetFlushReceipt(Protocol):
    @property
    def error(self) -> str | None: ...


@dataclass(slots=True)
class SessionResetFlushSafetyError(RuntimeError):
    snapshot: SessionResetSnapshot
    receipt: SessionResetFlushReceipt
    assessment: SessionResetMemoryAssessment

    def __str__(self) -> str:
        return (
            "session reset requires a durable memory flush "
            f"(status={self.assessment.flush_status})"
        )


@dataclass(slots=True)
class SessionResetFlushUnavailableError(RuntimeError):
    session_key: str
    session_id: str
    message_count: int

    def __str__(self) -> str:
        return "session reset requires a durable checkpoint or an available flush service"


@dataclass(slots=True)
class SessionResetForcePermissionError(PermissionError):
    session_key: str
    session_id: str

    def __str__(self) -> str:
        return "force reset requires administrator authority"


@dataclass(slots=True)
class SessionResetFlushExecutionError(RuntimeError):
    snapshot: SessionResetSnapshot
    receipt: SessionResetFlushReceipt

    def __str__(self) -> str:
        return f"session reset memory flush failed ({self.receipt.error})"


class SessionResetUnavailableError(RuntimeError):
    def __str__(self) -> str:
        return "session reset storage is unavailable"


@dataclass(slots=True)
class SessionResetNotFoundError(LookupError):
    session_key: str

    def __str__(self) -> str:
        return f"session not found: {self.session_key}"


@dataclass(frozen=True, slots=True)
class SessionResetResult:
    session_key: str
    previous_session_id: str
    session_id: str
    rotated: bool
    epoch: int
    flush_receipt: SessionResetFlushReceipt | None = None


class SessionQuiescencePort(Protocol):
    async def quiesce(self, session_key: str) -> None: ...


class SessionResetLockPort(Protocol):
    def hold(self, session_key: str) -> AbstractAsyncContextManager[None]: ...


class SessionResetStorePort(Protocol):
    @property
    def storage_available(self) -> bool: ...

    async def load(self, session_key: str) -> SessionResetSnapshot | None: ...

    async def rotate(self, session_key: str) -> SessionResetRotation: ...

    async def ensure_durable_epoch(self, session_key: str, previous_epoch: int) -> int: ...


class SessionResetMemoryPort(Protocol):
    @property
    def flush_enabled(self) -> bool: ...

    @property
    def flush_available(self) -> bool: ...

    async def checkpoint_covers(self, snapshot: SessionResetSnapshot) -> bool: ...

    def skipped_receipt(self) -> SessionResetFlushReceipt: ...

    def failed_receipt(
        self,
        *,
        message_count: int,
        error: str,
    ) -> SessionResetFlushReceipt: ...

    async def flush(self, snapshot: SessionResetSnapshot) -> SessionResetFlushReceipt: ...

    async def assess(
        self,
        snapshot: SessionResetSnapshot,
        receipt: SessionResetFlushReceipt,
    ) -> SessionResetMemoryAssessment: ...


class SessionResetUsagePort(Protocol):
    def account_memory_flush(
        self,
        session_key: str,
    ) -> AbstractAsyncContextManager[None]: ...


class GoalLeasePort(Protocol):
    def revoke(self, session_key: str) -> None: ...


class SessionEpochPort(Protocol):
    def update_cache(self, session_key: str, epoch: int) -> None: ...

    async def publish(self, session_key: str, epoch: int) -> None: ...


class PromptCacheInvalidationPort(Protocol):
    async def invalidate(self, session_key: str) -> None: ...


class SessionResetApplication:
    """Coordinate one reset while preserving the durable generation fence."""

    def __init__(
        self,
        *,
        quiescence: SessionQuiescencePort,
        lock: SessionResetLockPort,
        store: SessionResetStorePort,
        memory: SessionResetMemoryPort,
        usage: SessionResetUsagePort,
        goal_leases: GoalLeasePort,
        epochs: SessionEpochPort,
        prompt_cache: PromptCacheInvalidationPort,
    ) -> None:
        self._quiescence = quiescence
        self._lock = lock
        self._store = store
        self._memory = memory
        self._usage = usage
        self._goal_leases = goal_leases
        self._epochs = epochs
        self._prompt_cache = prompt_cache

    async def reset(self, command: ResetSession) -> SessionResetResult:
        key = canonicalize_session_key(command.session_key)
        if not key:
            raise ValueError("session_key must be non-empty")
        command = replace(command, session_key=key)

        await self._quiescence.quiesce(key)
        if not self._store.storage_available:
            raise SessionResetUnavailableError
        async with self._lock.hold(key):
            async with self._usage.account_memory_flush(key):
                snapshot = await self._store.load(key)
                if snapshot is None:
                    raise SessionResetNotFoundError(key)
                receipt: SessionResetFlushReceipt | None = None
                if self._memory.flush_enabled and not self._memory.flush_available:
                    if snapshot.transcript and not command.force:
                        checkpoint_safe = await self._memory.checkpoint_covers(snapshot)
                        if not checkpoint_safe:
                            raise SessionResetFlushUnavailableError(
                                session_key=key,
                                session_id=snapshot.session_id,
                                message_count=len(snapshot.transcript),
                            )
                    if (
                        snapshot.transcript
                        and command.force
                        and not command.force_authorized
                    ):
                        raise SessionResetForcePermissionError(
                            session_key=key,
                            session_id=snapshot.session_id,
                        )
                elif self._memory.flush_enabled and not snapshot.transcript:
                    receipt = self._memory.skipped_receipt()
                elif self._memory.flush_enabled:
                    try:
                        receipt = await self._memory.flush(snapshot)
                    except Exception as exc:
                        failed_receipt = self._memory.failed_receipt(
                            message_count=len(snapshot.transcript),
                            error=str(exc),
                        )
                        raise SessionResetFlushExecutionError(
                            snapshot=snapshot,
                            receipt=failed_receipt,
                        ) from exc
                    assessment = await self._memory.assess(snapshot, receipt)
                    if not assessment.allows_reset:
                        raise SessionResetFlushSafetyError(
                            snapshot=snapshot,
                            receipt=receipt,
                            assessment=assessment,
                        )
                rotation = await self._store.rotate(key)
                epoch = await self._store.ensure_durable_epoch(key, snapshot.epoch)
                self._goal_leases.revoke(key)
                if epoch > 0:
                    self._epochs.update_cache(key, epoch)
                    try:
                        await self._epochs.publish(key, epoch)
                    except Exception:
                        # The durable epoch is authoritative; reconnect replay heals
                        # a best-effort process-local publication failure.
                        pass

        await self._prompt_cache.invalidate(key)
        return SessionResetResult(
            session_key=key,
            previous_session_id=snapshot.session_id,
            session_id=rotation.session_id,
            rotated=rotation.rotated,
            epoch=epoch,
            flush_receipt=receipt,
        )


__all__ = [
    "GoalLeasePort",
    "PromptCacheInvalidationPort",
    "ResetSession",
    "SessionEpochPort",
    "SessionQuiescencePort",
    "SessionResetApplication",
    "SessionResetForcePermissionError",
    "SessionResetFlushExecutionError",
    "SessionResetFlushReceipt",
    "SessionResetFlushSafetyError",
    "SessionResetFlushUnavailableError",
    "SessionResetLockPort",
    "SessionResetMemoryAssessment",
    "SessionResetMemoryPort",
    "SessionResetNotFoundError",
    "SessionResetResult",
    "SessionResetRotation",
    "SessionResetSnapshot",
    "SessionResetStorePort",
    "SessionResetUsagePort",
    "SessionResetUnavailableError",
]
