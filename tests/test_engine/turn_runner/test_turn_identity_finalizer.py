"""Vertical finalizer coverage for caller-supplied assistant identity."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from opensquilla.contracts.turn_execution import TurnExecutionContext, TurnIdentity
from opensquilla.engine.runtime import TurnRunner
from opensquilla.engine.turn_runner.harness import (
    _TurnRunnerSessionTotalsAdapter,
    _TurnRunnerTurnMemoryCaptureAdapter,
)
from opensquilla.engine.turn_runner.turn_finalizer_stage import (
    TranscriptAppendResult,
    TurnFinalizerStage,
    TurnFinalizerStageInput,
)
from opensquilla.engine.types import DoneEvent
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import SessionIntent
from opensquilla.session.storage import SessionStorage, StaleEpochError


@dataclass
class _SessionTranscriptPort:
    manager: SessionManager
    calls: list[dict[str, Any]] = field(default_factory=list)
    after_append: Callable[[], Awaitable[None]] | None = None

    async def append_message(
        self,
        session_key: str,
        *,
        role: str,
        content: str,
        tool_calls: list[Any] | None,
        reasoning_content: str | None,
        turn_usage: dict[str, Any] | None,
        token_count: int | None,
        assistant_message_id: str | None = None,
        expected_session_id: str | None = None,
        expected_session_epoch: int | None = None,
    ) -> TranscriptAppendResult:
        self.calls.append(
            {
                "session_key": session_key,
                "assistant_message_id": assistant_message_id,
                "expected_session_id": expected_session_id,
                "expected_session_epoch": expected_session_epoch,
            }
        )
        entry = await self.manager.append_message(
            session_key,
            role,
            content,
            message_id=assistant_message_id,
            tool_calls=tool_calls,
            reasoning_content=reasoning_content,
            turn_usage=turn_usage,
            token_count=token_count,
            expected_session_id=expected_session_id,
            expected_session_epoch=expected_session_epoch,
        )
        if self.after_append is not None:
            await self.after_append()
        return TranscriptAppendResult(appended=True, message_id=entry.message_id)


class _NoopMemory:
    async def capture_turn(self, **_: Any) -> None:
        return None


@dataclass
class _RecordingMemory:
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def capture_turn(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class _NoopError:
    async def persist_error(self, **_: Any) -> None:
        return None


class _NoopTotals:
    async def rollup(self, **_: Any) -> None:
        return None


@pytest_asyncio.fixture
async def manager() -> SessionManager:
    storage = SessionStorage(":memory:")
    await storage.connect()
    value = SessionManager(storage, inject_time_prefix=False)
    await value.create("agent:main:finalizer-identity")
    yield value
    await storage.close()


def _make_input(
    context: TurnExecutionContext,
    *,
    text: str,
    expected_session_id: str | None = None,
    expected_session_epoch: int | None = None,
    done_event: DoneEvent | None = None,
    no_memory_capture: bool = True,
) -> TurnFinalizerStageInput:
    return TurnFinalizerStageInput(
        final_text_parts=[text] if text else [],
        turn_segments=[],
        turn_artifacts=[],
        error_message=None,
        pending_error_event=None,
        done_event=done_event,
        runtime_message="question",
        input_mode="user",
        input_provenance=None,
        resolved_model="model",
        agent_id="main",
        session_key="agent:main:finalizer-identity",
        tool_context=None,
        run_kind="default",
        heartbeat_ack_max_chars=300,
        no_memory_capture=no_memory_capture,
        expected_session_id=expected_session_id,
        expected_session_epoch=expected_session_epoch,
        execution_context=context,
    )


@pytest.mark.asyncio
async def test_finalizer_reuses_caller_id_and_is_idempotent(
    manager: SessionManager,
) -> None:
    key = "agent:main:finalizer-identity"
    message_id = "assistant-finalizer-id"
    context = TurnExecutionContext.create(TurnIdentity("turn-finalizer", message_id, key))
    transcript = _SessionTranscriptPort(manager)
    stage = TurnFinalizerStage(
        transcript_append=transcript,
        turn_memory_capture=_NoopMemory(),
        session_totals=_NoopTotals(),
        turn_error_persist=_NoopError(),
    )

    first = await stage.run(_make_input(context, text="draft"))
    second = await stage.run(_make_input(context, text="final"))

    assert first.require_output().assistant_message_id == message_id
    assert second.require_output().assistant_message_id == message_id
    assert [call["assistant_message_id"] for call in transcript.calls] == [
        message_id,
        message_id,
    ]
    entries = await manager.get_transcript(key)
    assert len(entries) == 1
    assert entries[0].message_id == message_id
    assert entries[0].content == "final"


@pytest.mark.asyncio
async def test_finalizer_empty_output_releases_without_append(
    manager: SessionManager,
) -> None:
    key = "agent:main:finalizer-identity"
    context = TurnExecutionContext.create(
        TurnIdentity("turn-empty", "assistant-empty-id", key)
    )
    transcript = _SessionTranscriptPort(manager)
    stage = TurnFinalizerStage(
        transcript_append=transcript,
        turn_memory_capture=_NoopMemory(),
        session_totals=_NoopTotals(),
        turn_error_persist=_NoopError(),
    )

    outcome = await stage.run(_make_input(context, text=""))

    assert outcome.require_output().transcript_appended is False
    assert transcript.calls == []
    assert context.publication_ledger.released is True
    assert await manager.get_transcript(key) == []


@pytest.mark.asyncio
async def test_reset_rejects_retired_finalizer_owner_without_new_transcript(
    manager: SessionManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "agent:main:finalizer-identity"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    admitted = await manager.get_session(key)
    assert admitted is not None
    context = TurnExecutionContext.create(
        TurnIdentity("turn-before-reset", "assistant-before-reset", key)
    )
    transcript = _SessionTranscriptPort(manager)
    stage = TurnFinalizerStage(
        transcript_append=transcript,
        turn_memory_capture=_NoopMemory(),
        session_totals=_NoopTotals(),
        turn_error_persist=_NoopError(),
    )

    replacement, rotated = await manager.apply_intent(key, SessionIntent.RESET_SAME_KEY)
    assert rotated is True
    assert replacement.session_id != admitted.session_id

    with pytest.raises(StaleEpochError, match="owner mismatch"):
        await stage.run(
            _make_input(
                context,
                text="late answer",
                expected_session_id=admitted.session_id,
                expected_session_epoch=int(admitted.epoch or 0),
            )
        )

    assert transcript.calls == [
        {
            "session_key": key,
            "assistant_message_id": "assistant-before-reset",
            "expected_session_id": admitted.session_id,
            "expected_session_epoch": int(admitted.epoch or 0),
        }
    ]
    assert await manager.get_transcript(key) == []


@pytest.mark.asyncio
async def test_reset_after_append_fences_memory_capture_and_totals(
    manager: SessionManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "agent:main:finalizer-identity"
    monkeypatch.setenv("OPENSQUILLA_SESSION_ARCHIVE_DIR", str(tmp_path / "archives"))
    admitted = await manager.get_session(key)
    assert admitted is not None
    replacement = None

    async def reset_after_append() -> None:
        nonlocal replacement
        replacement, rotated = await manager.apply_intent(key, SessionIntent.RESET_SAME_KEY)
        assert rotated is True

    transcript = _SessionTranscriptPort(manager, after_append=reset_after_append)
    capture = _RecordingMemory()
    runner = TurnRunner(
        provider_selector=None,
        session_manager=manager,
        turn_capture_services={"main": capture},
    )
    stage = TurnFinalizerStage(
        transcript_append=transcript,
        turn_memory_capture=_TurnRunnerTurnMemoryCaptureAdapter(runner),
        session_totals=_TurnRunnerSessionTotalsAdapter(runner),
        turn_error_persist=_NoopError(),
    )
    context = TurnExecutionContext.create(
        TurnIdentity("turn-before-reset", "assistant-before-reset", key)
    )

    outcome = await stage.run(
        _make_input(
            context,
            text="answer from retired owner",
            expected_session_id=admitted.session_id,
            expected_session_epoch=int(admitted.epoch or 0),
            done_event=DoneEvent(input_tokens=11, output_tokens=7),
            no_memory_capture=False,
        )
    )

    assert replacement is not None
    assert capture.calls == []
    assert outcome.require_output().memory_captured is False
    assert outcome.require_output().cost_rollup is None
    current = await manager.get_session(key)
    assert current is not None
    assert current.session_id == replacement.session_id
    assert (current.input_tokens, current.output_tokens, current.total_tokens) == (0, 0, 0)
