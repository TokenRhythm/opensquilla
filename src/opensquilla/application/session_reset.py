"""Transport-neutral session reset coordination.

The application service owns the destructive-reset ordering.  Runtime
quiescence, locking, archive-backed persistence, epoch publication and
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
    epoch: int


@dataclass(frozen=True, slots=True)
class SessionResetRotation:
    session_id: str
    rotated: bool


@dataclass(slots=True)
class SessionResetForcePermissionError(PermissionError):
    session_key: str
    session_id: str

    def __str__(self) -> str:
        return "force reset requires administrator authority"


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


class SessionQuiescencePort(Protocol):
    def quiesce(self, session_key: str) -> AbstractAsyncContextManager[None]: ...


class SessionResetLockPort(Protocol):
    def hold(self, session_key: str) -> AbstractAsyncContextManager[None]: ...


class SessionResetStorePort(Protocol):
    @property
    def storage_available(self) -> bool: ...

    async def load(self, session_key: str) -> SessionResetSnapshot | None: ...

    async def rotate(self, session_key: str) -> SessionResetRotation:
        """Archive the canonical history before atomically rotating the session."""
        ...

    async def ensure_durable_epoch(self, session_key: str, previous_epoch: int) -> int: ...


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
        goal_leases: GoalLeasePort,
        epochs: SessionEpochPort,
        prompt_cache: PromptCacheInvalidationPort,
    ) -> None:
        self._quiescence = quiescence
        self._lock = lock
        self._store = store
        self._goal_leases = goal_leases
        self._epochs = epochs
        self._prompt_cache = prompt_cache

    async def reset(self, command: ResetSession) -> SessionResetResult:
        key = canonicalize_session_key(command.session_key)
        if not key:
            raise ValueError("session_key must be non-empty")
        command = replace(command, session_key=key)

        async with self._quiescence.quiesce(key):
            if not self._store.storage_available:
                raise SessionResetUnavailableError
            async with self._lock.hold(key):
                snapshot = await self._store.load(key)
                if snapshot is None:
                    raise SessionResetNotFoundError(key)
                if command.force and not command.force_authorized:
                    raise SessionResetForcePermissionError(
                        session_key=key, session_id=snapshot.session_id,
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
        )


__all__ = [
    "GoalLeasePort",
    "PromptCacheInvalidationPort",
    "ResetSession",
    "SessionEpochPort",
    "SessionQuiescencePort",
    "SessionResetApplication",
    "SessionResetForcePermissionError",
    "SessionResetLockPort",
    "SessionResetNotFoundError",
    "SessionResetResult",
    "SessionResetRotation",
    "SessionResetSnapshot",
    "SessionResetStorePort",
    "SessionResetUnavailableError",
]
