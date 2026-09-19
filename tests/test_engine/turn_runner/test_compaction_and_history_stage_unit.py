"""Unit coverage for the canonical before-turn compaction stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.hooks.types import CompactionState
from opensquilla.engine.turn_runner.compaction_and_history_stage import (
    CompactionAndHistoryStage,
    CompactionAndHistoryStageInput,
)
from opensquilla.engine.turn_runner.outcome import StageOutcome


@dataclass
class _RecordingPreflight:
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def maybe_compact(self, **kwargs: Any) -> None:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises("recording preflight boom")


@dataclass
class _RecordingHistoryLoader:
    return_value: str | None = None
    raises: type[BaseException] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def load(self, **kwargs: Any) -> str | None:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises("recording history boom")
        return self.return_value


@dataclass
class _RecordingPrepender:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def prepend(self, **kwargs: Any) -> str | None:
        self.calls.append(dict(kwargs))
        existing = kwargs.get("existing")
        prepended = kwargs.get("prepended")
        if not prepended or not str(prepended).strip():
            return existing
        if not existing or not str(existing).strip():
            return str(prepended).strip()
        return f"{str(prepended).strip()}\n\n{str(existing).strip()}"


@dataclass
class _RecordingCompactionHook:
    before_raises: type[BaseException] | None = None
    after_raises: type[BaseException] | None = None
    events: list[tuple[str, str, dict[str, Any] | None]] = field(default_factory=list)

    async def before_compact(self, state: CompactionState) -> None:
        self.events.append(("before", state.extra.get("phase", ""), None))
        if self.before_raises is not None:
            raise self.before_raises("hook before boom")

    async def after_compact(self, state: CompactionState, outcome: Any) -> None:
        payload = dict(outcome) if isinstance(outcome, dict) else None
        self.events.append(("after", state.extra.get("phase", ""), payload))
        if self.after_raises is not None:
            raise self.after_raises("hook after boom")


def _make_input(
    *,
    request_context_prompt: str | None = None,
    history_has_persisted_user: bool = True,
    bound_user_message_id: str | None = None,
    context_window_tokens: int = 200_000,
    compaction_context_window_tokens: int | None = None,
    skip_compaction: bool = False,
    transcript_snapshot: Any | None = None,
    expected_session_id: str | None = None,
    expected_session_epoch: int | None = None,
) -> CompactionAndHistoryStageInput:
    return CompactionAndHistoryStageInput(
        agent=SimpleNamespace(
            config=SimpleNamespace(request_context_prompt=request_context_prompt),
        ),
        context_window_tokens=context_window_tokens,
        compaction_context_window_tokens=compaction_context_window_tokens,
        provider=SimpleNamespace(name="prov"),
        resolved_model="claude-sonnet-4.5",
        session_key="agent:main:s1",
        agent_id="agent:main",
        history_has_persisted_user=history_has_persisted_user,
        bound_user_message_id=bound_user_message_id,
        expected_session_id=expected_session_id,
        expected_session_epoch=expected_session_epoch,
        skip_compaction=skip_compaction,
        transcript_snapshot=transcript_snapshot,
    )


def _make_stage(
    *,
    preflight: _RecordingPreflight | None = None,
    history: _RecordingHistoryLoader | None = None,
    prepender: _RecordingPrepender | None = None,
    hooks: tuple[Any, ...] = (),
) -> tuple[
    CompactionAndHistoryStage,
    _RecordingPreflight,
    _RecordingHistoryLoader,
    _RecordingPrepender,
]:
    preflight = preflight or _RecordingPreflight()
    history = history or _RecordingHistoryLoader()
    prepender = prepender or _RecordingPrepender()
    stage = CompactionAndHistoryStage(
        preflight=preflight,
        history_loader=history,
        request_context_prepender=prepender,
        compaction_hooks=hooks,
    )
    return stage, preflight, history, prepender


@pytest.mark.asyncio
async def test_stage_runs_one_preflight_before_history() -> None:
    stage, preflight, history, prepender = _make_stage()

    outcome = await stage.run(_make_input())

    assert isinstance(outcome, StageOutcome)
    assert outcome.terminate is False
    assert len(preflight.calls) == 1
    assert len(history.calls) == 1
    assert len(prepender.calls) == 1
    assert outcome.output is not None
    assert outcome.output.compaction_summary_context is None


@pytest.mark.asyncio
async def test_stage_forwards_target_budget_and_owner_context() -> None:
    stage, preflight, history, _ = _make_stage()
    snapshot = SimpleNamespace()

    await stage.run(
        _make_input(
            context_window_tokens=200_000,
            compaction_context_window_tokens=128_000,
            bound_user_message_id="active-user",
            expected_session_id="session-1",
            expected_session_epoch=7,
            transcript_snapshot=snapshot,
        )
    )

    call = preflight.calls[0]
    assert call["context_window_tokens"] == 128_000
    assert call["bound_user_message_id"] == "active-user"
    assert call["expected_session_id"] == "session-1"
    assert call["expected_session_epoch"] == 7
    assert call["transcript_snapshot"] is snapshot
    assert history.calls[0]["expected_session_id"] == "session-1"
    assert history.calls[0]["expected_session_epoch"] == 7


@pytest.mark.asyncio
async def test_skip_compaction_still_loads_history() -> None:
    hook = _RecordingCompactionHook()
    stage, preflight, history, prepender = _make_stage(hooks=(hook,))

    outcome = await stage.run(_make_input(skip_compaction=True))

    assert preflight.calls == []
    assert len(history.calls) == 1
    assert len(prepender.calls) == 1
    assert hook.events == []
    assert outcome.output is not None


@pytest.mark.asyncio
async def test_hooks_fire_once_for_preflight() -> None:
    hook = _RecordingCompactionHook()
    stage, _, _, _ = _make_stage(hooks=(hook,))

    await stage.run(_make_input())

    assert hook.events == [
        ("before", "preflight", None),
        ("after", "preflight", {"status": "ran"}),
    ]


@pytest.mark.asyncio
async def test_hook_failures_are_isolated() -> None:
    hook = _RecordingCompactionHook(
        before_raises=RuntimeError,
        after_raises=RuntimeError,
    )
    stage, preflight, history, _ = _make_stage(hooks=(hook,))

    await stage.run(_make_input())

    assert len(preflight.calls) == 1
    assert len(history.calls) == 1


@pytest.mark.asyncio
async def test_preflight_exception_propagates_before_history() -> None:
    stage, preflight, history, _ = _make_stage(
        preflight=_RecordingPreflight(raises=RuntimeError),
    )

    with pytest.raises(RuntimeError):
        await stage.run(_make_input())

    assert len(preflight.calls) == 1
    assert history.calls == []


@pytest.mark.asyncio
async def test_history_loader_exception_propagates() -> None:
    stage, _, history, _ = _make_stage(
        history=_RecordingHistoryLoader(raises=RuntimeError),
    )

    with pytest.raises(RuntimeError):
        await stage.run(_make_input())

    assert len(history.calls) == 1


@pytest.mark.asyncio
async def test_history_summary_is_prepended_without_mutating_agent() -> None:
    stage, _, _, prepender = _make_stage(
        history=_RecordingHistoryLoader(return_value="SUMMARY"),
    )
    inp = _make_input(request_context_prompt="EXISTING")

    outcome = await stage.run(inp)

    assert inp.agent.config.request_context_prompt == "EXISTING"
    assert prepender.calls == [{"existing": "EXISTING", "prepended": "SUMMARY"}]
    assert outcome.output is not None
    assert outcome.output.final_request_context_prompt == "SUMMARY\n\nEXISTING"


@pytest.mark.asyncio
async def test_history_loader_trim_and_bound_message_are_forwarded() -> None:
    stage, _, history, _ = _make_stage()

    await stage.run(
        _make_input(
            history_has_persisted_user=False,
            bound_user_message_id="bound-user",
        )
    )

    assert history.calls[0]["trim_last_user"] is False
    assert history.calls[0]["bound_user_message_id"] == "bound-user"
