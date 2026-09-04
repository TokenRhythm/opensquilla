from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest

from opensquilla.application.session_reset import (
    ResetSession,
    SessionResetApplication,
    SessionResetFlushExecutionError,
    SessionResetFlushSafetyError,
    SessionResetFlushUnavailableError,
    SessionResetForcePermissionError,
    SessionResetMemoryAssessment,
    SessionResetNotFoundError,
    SessionResetRotation,
    SessionResetSnapshot,
    SessionResetUnavailableError,
)
from opensquilla.memory.session_flush import FlushReceipt


@dataclass
class _Quiescence:
    events: list[str]

    async def quiesce(self, session_key: str) -> None:
        self.events.append(f"quiesce:{session_key}")


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

    @property
    def storage_available(self) -> bool:
        return self.is_available

    async def load(self, session_key: str) -> SessionResetSnapshot | None:
        self.events.append(f"snapshot:{session_key}")
        return self.snapshot

    async def rotate(self, session_key: str) -> SessionResetRotation:
        self.events.append(f"rotate:{session_key}")
        return SessionResetRotation(session_id="session-new", rotated=True)

    async def ensure_durable_epoch(self, session_key: str, previous_epoch: int) -> int:
        self.events.append(f"epoch.persist:{session_key}:{previous_epoch}")
        return previous_epoch + 1 if self.durable_epoch is None else self.durable_epoch


@dataclass
class _Memory:
    events: list[str]
    receipt: FlushReceipt
    assessment: SessionResetMemoryAssessment = SessionResetMemoryAssessment(
        allows_reset=True,
        flush_status="safe",
        safety_status="safe",
        semantic_status="healthy",
    )
    enabled_value: bool = True
    available_value: bool = True
    checkpoint_safe: bool = False
    flush_error: Exception | None = None

    @property
    def flush_enabled(self) -> bool:
        return self.enabled_value

    @property
    def flush_available(self) -> bool:
        return self.available_value

    async def checkpoint_covers(self, snapshot: SessionResetSnapshot) -> bool:
        self.events.append(f"checkpoint:{snapshot.session_id}")
        return self.checkpoint_safe

    def skipped_receipt(self) -> FlushReceipt:
        return FlushReceipt(
            mode="skipped",
            flushed_paths=[],
            slug=None,
            message_count=0,
            duration_ms=0,
            raw_reason=None,
            error=None,
        )

    def failed_receipt(self, *, message_count: int, error: str) -> FlushReceipt:
        return FlushReceipt(
            mode="error",
            flushed_paths=[],
            slug=None,
            message_count=message_count,
            duration_ms=0,
            raw_reason=None,
            error=error,
            result_status="archive_failed",
        )

    async def flush(self, snapshot: SessionResetSnapshot) -> FlushReceipt:
        self.events.append(f"flush:{snapshot.session_id}")
        if self.flush_error is not None:
            raise self.flush_error
        return self.receipt

    async def assess(
        self,
        snapshot: SessionResetSnapshot,
        receipt: FlushReceipt,
    ) -> SessionResetMemoryAssessment:
        self.events.append(f"assess:{snapshot.session_id}")
        return self.assessment


@dataclass
class _Usage:
    events: list[str]

    @asynccontextmanager
    async def account_memory_flush(self, session_key: str) -> AsyncIterator[None]:
        self.events.append(f"usage.enter:{session_key}")
        try:
            yield
        finally:
            self.events.append(f"usage.exit:{session_key}")


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
    assessment: SessionResetMemoryAssessment | None = None,
    flush_enabled: bool = True,
    flush_available: bool = True,
    flush_error: Exception | None = None,
    checkpoint_safe: bool = False,
    transcript: tuple[object, ...] = (object(),),
    store_available: bool = True,
    session_exists: bool = True,
    durable_epoch: int | None = None,
) -> SessionResetApplication:
    receipt = cast(
        FlushReceipt,
        SimpleNamespace(mode="llm", error=None, to_dict=lambda: {"mode": "llm"}),
    )
    snapshot = SessionResetSnapshot(
        session_key="agent:main:webchat:one",
        session_id="session-old",
        agent_id="main",
        epoch=4,
        transcript=transcript,
    )
    return SessionResetApplication(
        quiescence=_Quiescence(events),
        lock=_Lock(events),
        store=_Store(
            events,
            snapshot if session_exists else None,
            is_available=store_available,
            durable_epoch=durable_epoch,
        ),
        memory=_Memory(
            events,
            receipt,
            assessment
            or SessionResetMemoryAssessment(
                allows_reset=True,
                flush_status="safe",
                safety_status="safe",
                semantic_status="healthy",
            ),
            enabled_value=flush_enabled,
            available_value=flush_available,
            checkpoint_safe=checkpoint_safe,
            flush_error=flush_error,
        ),
        usage=_Usage(events),
        goal_leases=_GoalLeases(events),
        epochs=_Epochs(events),
        prompt_cache=_PromptCache(events),
    )


async def test_reset_owns_quiesce_flush_rotate_epoch_and_invalidation_order() -> None:
    events: list[str] = []

    result = await _application(events).reset(
        ResetSession(" agent:main:webchat:one ")
    )

    assert result.session_id == "session-new"
    assert result.previous_session_id == "session-old"
    assert result.epoch == 5
    assert events == [
        "quiesce:agent:main:webchat:one",
        "lock.enter:agent:main:webchat:one",
        "usage.enter:agent:main:webchat:one",
        "snapshot:agent:main:webchat:one",
        "flush:session-old",
        "assess:session-old",
        "rotate:agent:main:webchat:one",
        "epoch.persist:agent:main:webchat:one:4",
        "goal.revoke:agent:main:webchat:one",
        "epoch.cache:agent:main:webchat:one:5",
        "epoch.publish:agent:main:webchat:one:5",
        "usage.exit:agent:main:webchat:one",
        "lock.exit:agent:main:webchat:one",
        "prompt.invalidate:agent:main:webchat:one",
    ]


async def test_unsafe_flush_fails_before_rotation_and_post_commit_effects() -> None:
    events: list[str] = []
    application = _application(
        events,
        assessment=SessionResetMemoryAssessment(
            allows_reset=False,
            flush_status="unsafe",
            safety_status="unsafe",
            semantic_status="failed",
        ),
    )

    try:
        await application.reset(ResetSession("agent:main:webchat:one"))
    except SessionResetFlushSafetyError as exc:
        assert exc.assessment.safety_status == "unsafe"
    else:
        raise AssertionError("unsafe flush must fail closed")

    assert not any(event.startswith("rotate:") for event in events)
    assert not any(event.startswith("epoch.") for event in events)
    assert not any(event.startswith("goal.") for event in events)
    assert not any(event.startswith("prompt.") for event in events)
    assert events[-2:] == [
        "usage.exit:agent:main:webchat:one",
        "lock.exit:agent:main:webchat:one",
    ]


async def test_unavailable_flush_without_checkpoint_fails_before_rotation() -> None:
    events: list[str] = []

    try:
        await _application(events, flush_available=False).reset(
            ResetSession("agent:main:webchat:one")
        )
    except SessionResetFlushUnavailableError as exc:
        assert exc.session_id == "session-old"
        assert exc.message_count == 1
    else:
        raise AssertionError("non-empty reset without a durable backup must fail closed")

    assert "checkpoint:session-old" in events
    assert not any(event.startswith("rotate:") for event in events)


async def test_force_without_authority_never_bypasses_memory_gate() -> None:
    events: list[str] = []

    try:
        await _application(events, flush_available=False).reset(
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


async def test_flush_exception_becomes_typed_failure_without_rotation() -> None:
    events: list[str] = []

    try:
        await _application(events, flush_error=OSError("synthetic disk failure")).reset(
            ResetSession("agent:main:webchat:one")
        )
    except SessionResetFlushExecutionError as exc:
        assert exc.receipt.mode == "error"
        assert exc.receipt.error == "synthetic disk failure"
        assert exc.receipt.message_count == 1
    else:
        raise AssertionError("a failed flush must fail the reset")

    assert not any(event.startswith("rotate:") for event in events)


async def test_empty_transcript_rotates_with_skipped_receipt_without_flushing() -> None:
    events: list[str] = []

    result = await _application(events, transcript=()).reset(
        ResetSession("agent:main:webchat:one")
    )

    assert result.flush_receipt is not None
    assert result.flush_receipt.mode == "skipped"
    assert result.flush_receipt.message_count == 0
    assert not any(event.startswith("flush:") for event in events)
    assert not any(event.startswith("assess:") for event in events)
    assert "rotate:agent:main:webchat:one" in events


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


@pytest.mark.parametrize(
    ("command", "checkpoint_safe", "expects_checkpoint"),
    [
        (ResetSession("agent:main:webchat:one", force=True, force_authorized=True), False, False),
        (ResetSession("agent:main:webchat:one"), True, True),
    ],
)
async def test_authorized_discard_or_covering_checkpoint_allows_unflushed_reset(
    command: ResetSession,
    checkpoint_safe: bool,
    expects_checkpoint: bool,
) -> None:
    events: list[str] = []

    result = await _application(
        events,
        flush_available=False,
        checkpoint_safe=checkpoint_safe,
    ).reset(command)

    assert result.rotated is True
    assert result.flush_receipt is None
    assert ("checkpoint:session-old" in events) is expects_checkpoint
    assert not any(event.startswith("flush:") for event in events)


async def test_disabled_flush_policy_rotates_without_receipt_or_backup_gate() -> None:
    events: list[str] = []

    result = await _application(events, flush_enabled=False).reset(
        ResetSession("agent:main:webchat:one")
    )

    assert result.rotated is True
    assert result.flush_receipt is None
    assert not any(event.startswith("checkpoint:") for event in events)
    assert not any(event.startswith("flush:") for event in events)
