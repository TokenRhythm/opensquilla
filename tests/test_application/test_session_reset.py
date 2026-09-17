from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

from opensquilla.application.session_reset import (
    ResetSession,
    SessionResetApplication,
    SessionResetForcePermissionError,
    SessionResetNotFoundError,
    SessionResetRotation,
    SessionResetSnapshot,
    SessionResetUnavailableError,
)


@dataclass
class _Quiescence:
    events: list[str]

    @asynccontextmanager
    async def quiesce(self, session_key: str) -> AsyncIterator[None]:
        self.events.append(f"quiesce:{session_key}")
        yield


@dataclass
class _Lock:
    events: list[str]

    @asynccontextmanager
    async def hold(self, session_key: str) -> AsyncIterator[None]:
        self.events.append(f"lock.enter:{session_key}")
        try:
            yield
        finally:
            self.events.append(f"lock.exit:{session_key}")


@dataclass
class _Store:
    events: list[str]
    snapshot: SessionResetSnapshot | None
    is_available: bool = True
    durable_epoch: int | None = None
    rotation_error: Exception | None = None

    @property
    def storage_available(self) -> bool:
        return self.is_available

    async def load(self, session_key: str) -> SessionResetSnapshot | None:
        self.events.append(f"snapshot:{session_key}")
        return self.snapshot

    async def rotate(self, session_key: str) -> SessionResetRotation:
        self.events.append(f"rotate:{session_key}")
        if self.rotation_error is not None:
            raise self.rotation_error
        return SessionResetRotation(session_id="session-new", rotated=True)

    async def ensure_durable_epoch(self, session_key: str, previous_epoch: int) -> int:
        self.events.append(f"epoch.persist:{session_key}:{previous_epoch}")
        return previous_epoch + 1 if self.durable_epoch is None else self.durable_epoch


@dataclass
class _GoalLeases:
    events: list[str]

    def revoke(self, session_key: str) -> None:
        self.events.append(f"goal.revoke:{session_key}")


@dataclass
class _Epochs:
    events: list[str]

    def update_cache(self, session_key: str, epoch: int) -> None:
        self.events.append(f"epoch.cache:{session_key}:{epoch}")

    async def publish(self, session_key: str, epoch: int) -> None:
        self.events.append(f"epoch.publish:{session_key}:{epoch}")


@dataclass
class _PromptCache:
    events: list[str]

    async def invalidate(self, session_key: str) -> None:
        self.events.append(f"prompt.invalidate:{session_key}")


def _application(
    events: list[str],
    *,
    rotation_error: Exception | None = None,
    store_available: bool = True,
    session_exists: bool = True,
    durable_epoch: int | None = None,
) -> SessionResetApplication:
    snapshot = SessionResetSnapshot(
        session_key="agent:main:webchat:one",
        session_id="session-old",
        epoch=4,
    )
    return SessionResetApplication(
        quiescence=_Quiescence(events),
        lock=_Lock(events),
        store=_Store(
            events,
            snapshot if session_exists else None,
            is_available=store_available,
            durable_epoch=durable_epoch,
            rotation_error=rotation_error,
        ),
        goal_leases=_GoalLeases(events),
        epochs=_Epochs(events),
        prompt_cache=_PromptCache(events),
    )


async def test_reset_owns_quiesce_rotate_epoch_and_invalidation_order() -> None:
    events: list[str] = []

    result = await _application(events).reset(ResetSession(" agent:main:webchat:one "))

    assert result.session_id == "session-new"
    assert result.previous_session_id == "session-old"
    assert result.epoch == 5
    assert events == [
        "quiesce:agent:main:webchat:one",
        "lock.enter:agent:main:webchat:one",
        "snapshot:agent:main:webchat:one",
        "rotate:agent:main:webchat:one",
        "epoch.persist:agent:main:webchat:one:4",
        "goal.revoke:agent:main:webchat:one",
        "epoch.cache:agent:main:webchat:one:5",
        "epoch.publish:agent:main:webchat:one:5",
        "lock.exit:agent:main:webchat:one",
        "prompt.invalidate:agent:main:webchat:one",
    ]


async def test_force_without_authority_never_rotates() -> None:
    events: list[str] = []

    try:
        await _application(events).reset(
            ResetSession(
                "agent:main:webchat:one",
                force=True,
                force_authorized=False,
            )
        )
    except SessionResetForcePermissionError as exc:
        assert exc.session_id == "session-old"
    else:
        raise AssertionError("force reset must require explicit authority")

    assert not any(event.startswith("rotate:") for event in events)


async def test_missing_store_and_session_raise_typed_failures_after_quiescence() -> None:
    unavailable_events: list[str] = []
    with pytest.raises(SessionResetUnavailableError):
        await _application(unavailable_events, store_available=False).reset(
            ResetSession("agent:main:webchat:one")
        )

    missing_events: list[str] = []
    with pytest.raises(SessionResetNotFoundError):
        await _application(missing_events, session_exists=False).reset(
            ResetSession("agent:main:webchat:one")
        )

    assert unavailable_events[0].startswith("quiesce:")
    assert missing_events[0].startswith("quiesce:")
    assert not any(event.startswith("rotate:") for event in unavailable_events)
    assert not any(event.startswith("rotate:") for event in missing_events)


async def test_missing_durable_epoch_never_publishes_a_false_generation() -> None:
    events: list[str] = []

    result = await _application(events, durable_epoch=0).reset(
        ResetSession("agent:main:webchat:one")
    )

    assert result.epoch == 0
    assert "goal.revoke:agent:main:webchat:one" in events
    assert not any(event.startswith("epoch.cache:") for event in events)
    assert not any(event.startswith("epoch.publish:") for event in events)
    assert events[-1] == "prompt.invalidate:agent:main:webchat:one"


@pytest.mark.parametrize("force", [False, True])
async def test_archive_failure_never_advances_epoch_or_invalidates_state(force: bool) -> None:
    events: list[str] = []
    with pytest.raises(OSError, match="archive unavailable"):
        await _application(events, rotation_error=OSError("archive unavailable")).reset(
            ResetSession("agent:main:webchat:one", force=force, force_authorized=True)
        )
    assert "rotate:agent:main:webchat:one" in events
    assert not any(event.startswith(("epoch.", "goal.", "prompt.")) for event in events)
    assert events[-1] == "lock.exit:agent:main:webchat:one"


async def test_authorized_force_reset_rotates() -> None:
    events: list[str] = []
    result = await _application(events).reset(
        ResetSession("agent:main:webchat:one", force=True, force_authorized=True)
    )
    assert result.rotated
    assert "rotate:agent:main:webchat:one" in events
